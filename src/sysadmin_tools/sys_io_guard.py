#!/usr/bin/env python3
"""sys-io-guard: serialize heavy I/O workloads per target physical disk via flock.

Resolves a target directory to its backing physical block device using /proc/mounts
(zero external commands), acquires an advisory flock in a root/tmpfs lock directory,
caps Cargo build parallelism and child CPU niceness, and runs the command directly.

Examples:
  sys-io-guard -- cargo test
  sys-io-guard --path /datanapps/cargo-target --timeout 300 -- cargo build --release
  sys-io-guard --lock-key nvme2n1 --cargo-jobs 4 --nice 12 -- bash scripts/verify.sh
  sys-io-guard --dry-run --json --path /datanapps -- cargo test
  sys-io-guard --status
  sys-io-guard --status --json
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Standalone stdlib-only script: no imports from sysadmin_tools.* so copying to
# ~/.local/bin/sys-io-guard (on root NVMe) works even if secondary mounts stall.

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_BUSY = 4

_LOCK_KEY_INVALID_RE = re.compile(r"[^A-Za-z0-9._-]")
_NVME_PART_RE = re.compile(r"^(nvme\d+n\d+)(?:p\d+)?$")
_MMC_PART_RE = re.compile(r"^(mmcblk\d+)(?:p\d+)?$")
_SD_PART_RE = re.compile(r"^((?:sd|vd|hd|xvd)[a-z]+)\d*$")
_MD_LOOP_NBD_RE = re.compile(r"^((?:md|loop|nbd)\d+)(?:p\d+)?$")
_DM_RE = re.compile(r"^dm-\d+$")
_BCACHE_RE = re.compile(r"^bcache\d+$")

_VIRTUAL_FSTYPES = frozenset(
    {
        "autofs",
        "cgroup",
        "cgroup2",
        "cifs",
        "davfs",
        "devpts",
        "devtmpfs",
        "fuse",
        "fusectl",
        "nfs",
        "nfs4",
        "overlay",
        "proc",
        "ramfs",
        "rootfs",
        "smb3",
        "smbfs",
        "sshfs",
        "sysfs",
        "tmpfs",
        "virtiofs",
        "9p",
    }
)


class _SignalExit(BaseException):
    """Internal exception used to unwind and release lock/meta cleanly on SIGTERM."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"terminated by signal {signum}")
        self.signum = signum


class LockBusyError(Exception):
    """Raised when an advisory lock cannot be acquired within timeout."""

    def __init__(
        self,
        lock_key: str,
        lock_file: Path,
        timeout: float,
        holder: dict[str, Any] | None,
    ) -> None:
        super().__init__(f"lock '{lock_key}' is busy")
        self.lock_key = lock_key
        self.lock_file = lock_file
        self.timeout = timeout
        self.holder = holder


def read_text(path: str | Path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _unescape_mount_field(field: str) -> str:
    return (
        field.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def default_cargo_jobs(cpu_count: int | None = None) -> int:
    cpus = os.cpu_count() if cpu_count is None else cpu_count
    return min(8, max(2, (cpus or 4) // 4))


def sanitize_lock_key(name: str) -> str:
    """Sanitize a lock key to [A-Za-z0-9._-] without path traversal or hidden names."""
    stripped = name.strip()
    cleaned = _LOCK_KEY_INVALID_RE.sub("_", stripped)
    # Strip leading dots so '.', '..', and hidden dot-files cannot be produced.
    cleaned = cleaned.lstrip(".")
    cleaned = cleaned[:128]
    if not cleaned or not any(ch.isalnum() for ch in cleaned):
        raise ValueError(f"invalid lock key {name!r}: must contain at least one alphanumeric character")
    return cleaned


def resolve_lock_dir(
    cli_lock_dir: str | None = None,
    env: Mapping[str, str] | None = None,
    uid: int | None = None,
) -> Path:
    """Resolve lock directory: --lock-dir > $SYS_IO_GUARD_LOCK_DIR > $XDG_RUNTIME_DIR/sys-io-guard > /tmp/sys-io-guard-<uid>."""
    env_map = os.environ if env is None else env
    if cli_lock_dir:
        return Path(os.path.expanduser(cli_lock_dir))
    env_dir = env_map.get("SYS_IO_GUARD_LOCK_DIR", "").strip()
    if env_dir:
        return Path(os.path.expanduser(env_dir))
    xdg = env_map.get("XDG_RUNTIME_DIR", "").strip()
    if xdg:
        return Path(os.path.expanduser(xdg)) / "sys-io-guard"
    eff_uid = os.getuid() if uid is None else uid
    return Path(f"/tmp/sys-io-guard-{eff_uid}")


def _resolve_dm_slave(dm_name: str, sys_block_root: str = "/sys/block", _depth: int = 0) -> str:
    if _depth >= 4:
        return dm_name
    slaves_dir = os.path.join(sys_block_root, dm_name, "slaves")
    try:
        slaves = sorted(os.listdir(slaves_dir))
    except OSError:
        return dm_name
    if len(slaves) != 1:
        return dm_name
    slave = slaves[0]
    parent = parent_block_device(slave, allow_sysfs=True, sys_block_root=sys_block_root, _depth=_depth + 1)
    return parent or dm_name


def parent_block_device(
    dev_name: str,
    *,
    allow_sysfs: bool = False,
    sys_block_root: str = "/sys/block",
    _depth: int = 0,
) -> str | None:
    """Map a Linux block device basename (e.g. nvme2n1p1, sda2, mmcblk0p1) to its parent disk."""
    for rx in (_NVME_PART_RE, _MMC_PART_RE, _SD_PART_RE, _MD_LOOP_NBD_RE):
        m = rx.match(dev_name)
        if m:
            return m.group(1)
    if _BCACHE_RE.match(dev_name):
        return dev_name
    if _DM_RE.match(dev_name):
        if allow_sysfs:
            return _resolve_dm_slave(dev_name, sys_block_root=sys_block_root, _depth=_depth)
        return dev_name
    return None


def fallback_mount_key(mountpoint: str) -> str:
    digest = hashlib.sha1(mountpoint.encode("utf-8")).hexdigest()[:8]
    return f"mnt-{digest}"


def parse_mounts(mounts_txt: str) -> list[tuple[str, str, str]]:
    """Parse /proc/mounts into (device, mountpoint, fstype) tuples."""
    entries: list[tuple[str, str, str]] = []
    for line in mounts_txt.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        dev = _unescape_mount_field(parts[0])
        mnt = _unescape_mount_field(parts[1])
        fstype = parts[2]
        entries.append((dev, mnt, fstype))
    return entries


def resolve_path_to_disk(
    path: str | Path,
    mounts_txt: str | None = None,
) -> dict[str, Any]:
    """Resolve target path -> mountpoint -> parent physical block device using /proc/mounts.

    Never invokes external binaries (df, lsblk, findmnt). Falls back to
    `mnt-<sha1[:8]>` when the backing mount is non-block or virtual.
    """
    use_live = mounts_txt is None
    raw_path = os.path.expanduser(str(path))
    if use_live:
        try:
            target_path = os.path.realpath(os.path.abspath(raw_path))
        except OSError:
            target_path = os.path.abspath(raw_path)
    else:
        target_path = os.path.normpath(os.path.abspath(raw_path))

    txt = (read_text("/proc/mounts") or "") if use_live else mounts_txt
    entries = parse_mounts(txt)

    best_dev: str | None = None
    best_mnt: str = "/"
    best_fstype: str | None = None
    best_len = -1

    for dev, mnt, fstype in entries:
        if mnt == "/":
            matched = target_path.startswith("/")
            norm_len = 0
        else:
            norm = mnt.rstrip("/")
            matched = target_path == norm or target_path.startswith(norm + "/")
            norm_len = len(norm)
        # Use >= so later stacked mounts on the same mountpoint win over earlier ones (e.g. ext4 over rootfs).
        if matched and norm_len >= best_len:
            best_len = norm_len
            best_dev = dev
            best_mnt = mnt
            best_fstype = fstype

    disk: str | None = None
    if best_dev and best_dev.startswith("/dev/"):
        ft_lower = (best_fstype or "").lower()
        if ft_lower not in _VIRTUAL_FSTYPES and not ft_lower.startswith("fuse."):
            resolved_dev = best_dev
            if use_live:
                try:
                    resolved_dev = os.path.realpath(best_dev)
                except OSError:
                    resolved_dev = best_dev
            dev_name = os.path.basename(resolved_dev)
            disk = parent_block_device(dev_name, allow_sysfs=use_live)

    lock_key = sanitize_lock_key(disk) if disk else fallback_mount_key(best_mnt)
    return {
        "target_path": target_path,
        "mountpoint": best_mnt,
        "device": best_dev,
        "fstype": best_fstype,
        "disk": disk,
        "lock_key": lock_key,
    }


def read_holder_meta(meta_path: Path) -> dict[str, Any] | None:
    txt = read_text(meta_path)
    if not txt:
        return None
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def write_holder_meta(meta_path: Path, meta: dict[str, Any]) -> None:
    """Atomically write holder metadata JSON while holding the advisory lock."""
    tmp_path = meta_path.parent / f".{meta_path.name}.{os.getpid()}.tmp"
    payload = json.dumps(meta, indent=2, sort_keys=True) + "\n"
    fd = os.open(str(tmp_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
        os.replace(tmp_path, meta_path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def remove_holder_meta(meta_path: Path) -> None:
    try:
        meta_path.unlink(missing_ok=True)
    except OSError:
        pass


def acquire_lock(
    lock_path: Path,
    meta_path: Path,
    lock_key: str,
    timeout: float | None,
) -> tuple[int, float]:
    """Acquire exclusive advisory flock on lock_path. Returns (fd, wait_seconds)."""
    os.makedirs(lock_path.parent, mode=0o700, exist_ok=True)

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    start = time.monotonic()
    try:
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return fd, round(time.monotonic() - start, 4)

        deadline = start + timeout
        poll_interval = 0.01
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd, round(time.monotonic() - start, 4)
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    holder = read_holder_meta(meta_path)
                    raise LockBusyError(lock_key, lock_path, timeout, holder) from None
                time.sleep(min(poll_interval, remaining))
                poll_interval = min(0.05, poll_interval * 1.5)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def release_lock(fd: int, meta_path: Path) -> None:
    """Remove holder metadata while still holding flock, then unlock and close fd."""
    remove_holder_meta(meta_path)
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def probe_locks(lock_dir: Path) -> dict[str, Any]:
    """Probe all *.lock files in lock_dir with non-blocking LOCK_EX|LOCK_NB."""
    locks: list[dict[str, Any]] = []
    if lock_dir.is_dir():
        lock_files = sorted(p for p in lock_dir.iterdir() if p.is_file() and p.name.endswith(".lock"))
    else:
        lock_files = []

    active_count = 0
    idle_count = 0
    for lock_file in lock_files:
        lock_key = lock_file.name[: -len(".lock")]
        meta_file = lock_dir / f"{lock_key}.meta"
        active = False
        probe_error: str | None = None
        fd: int | None = None
        try:
            fd = os.open(str(lock_file), os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                active = False
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    active = True
                else:
                    probe_error = str(exc)
        except OSError as exc:
            probe_error = str(exc)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

        meta = read_holder_meta(meta_file)
        if active:
            active_count += 1
        else:
            idle_count += 1

        entry: dict[str, Any] = {
            "active": active,
            "holder": meta if active else None,
            "lock_file": str(lock_file),
            "lock_key": lock_key,
            "meta_file": str(meta_file),
            "stale_meta": bool(meta is not None and not active),
            "state": "active" if active else "idle",
        }
        if probe_error is not None:
            entry["error"] = probe_error
        locks.append(entry)

    return {
        "active_count": active_count,
        "idle_count": idle_count,
        "lock_dir": str(lock_dir),
        "locks": locks,
    }


def format_holder_summary(holder: dict[str, Any] | None) -> str:
    if not holder:
        return "holder metadata unavailable"
    pid = holder.get("pid", "?")
    ppid = holder.get("ppid", "?")
    started = holder.get("started_at", "?")
    target = holder.get("target_path", "?")
    cmd = holder.get("cmd")
    cmd_str = " ".join(str(x) for x in cmd) if isinstance(cmd, list) else str(cmd or "?")
    return f"pid={pid} ppid={ppid} started_at={started} target_path={target} cmd={cmd_str!r}"


def render_status_text(status: dict[str, Any]) -> str:
    lines = [
        f"Lock directory: {status['lock_dir']}",
        f"Locks: {len(status['locks'])} total ({status['active_count']} active, {status['idle_count']} idle)",
    ]
    if not status["locks"]:
        lines.append("  (no lock files found)")
        return "\n".join(lines)
    for item in status["locks"]:
        state = "ACTIVE" if item["active"] else "IDLE"
        if item["active"]:
            detail = format_holder_summary(item.get("holder"))
            lines.append(f"  [{state}] {item['lock_key']} ({item['lock_file']}): {detail}")
        else:
            suffix = " (stale .meta present)" if item.get("stale_meta") else ""
            lines.append(f"  [{state}]   {item['lock_key']} ({item['lock_file']}){suffix}")
    return "\n".join(lines)


def render_dry_run_text(plan: dict[str, Any]) -> str:
    env_str = (
        ", ".join(f"{k}={v}" for k, v in sorted(plan["env_overrides"].items()))
        if plan["env_overrides"]
        else "(none)"
    )
    cmd_str = " ".join(plan["command"]) if plan["command"] else "(none)"
    timeout_str = "wait" if plan["timeout"] is None else f"{plan['timeout']}s"
    lines = [
        f"target_path:     {plan['target_path']}",
        f"mountpoint:      {plan['mountpoint']}",
        f"device:          {plan['device'] or '(virtual)'}",
        f"fstype:          {plan['fstype'] or '(unknown)'}",
        f"disk:            {plan['disk'] or '(fallback)'}",
        f"lock_key:        {plan['lock_key']} (source={plan['lock_key_source']})",
        f"lock_file:       {plan['lock_file']}",
        f"meta_file:       {plan['meta_file']}",
        f"timeout:         {timeout_str}",
        f"nice:            {plan['nice']}",
        f"cargo_jobs:      {plan['cargo_jobs']}",
        f"env_overrides:   {env_str}",
        f"command:         {cmd_str}",
    ]
    return "\n".join(lines)


def parse_env_overrides(items: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"invalid --env {item!r}: expected KEY=VAL")
        key, val = item.split("=", 1)
        if not key or "\x00" in key or "\x00" in val:
            raise ValueError(f"invalid --env {item!r}: KEY must be non-empty and free of NUL bytes")
        overrides[key] = val
    return overrides


def _apply_child_nice(nice_value: int) -> None:
    if nice_value > 0:
        try:
            os.nice(nice_value)
        except OSError:
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sys-io-guard",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--path",
        metavar="DIR",
        default=None,
        help="Path on target filesystem used to resolve disk lock key (default: $CARGO_TARGET_DIR or cwd)",
    )
    p.add_argument(
        "--lock-key",
        metavar="NAME",
        default=None,
        help="Explicit lock key override (sanitized to [A-Za-z0-9._-])",
    )
    p.add_argument(
        "--lock-dir",
        metavar="DIR",
        default=None,
        help="Override lock directory (default: $SYS_IO_GUARD_LOCK_DIR, $XDG_RUNTIME_DIR/sys-io-guard, or /tmp/sys-io-guard-<uid>)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        metavar="SEC",
        default=None,
        help="Lock wait timeout in seconds (>=0; 0 = non-blocking; omitted = wait indefinitely)",
    )
    p.add_argument(
        "--cargo-jobs",
        type=int,
        metavar="N",
        default=None,
        help="Set CARGO_BUILD_JOBS=N (0 disables; env SYS_IO_GUARD_CARGO_JOBS; default: min(8, max(2, cpus//4)))",
    )
    p.add_argument(
        "--nice",
        type=int,
        metavar="N",
        default=10,
        help="Child process nice increment 0..19 (0 disables os.nice; default: 10)",
    )
    p.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VAL",
        help="Extra environment variable override passed to child (repeatable)",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--status",
        action="store_true",
        help="Probe *.lock files in lock directory to show active vs idle locks and holder metadata",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Print resolved device, lock file, env overrides, and command without locking or executing",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit deterministic JSON (sort_keys=True) for --status or --dry-run",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Log wait duration, resolved device, lock file, and env overrides to stderr",
    )
    p.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to execute under the lock (optionally preceded by '--')",
    )
    return p


def _resolve_cargo_jobs(cli_val: int | None, env: Mapping[str, str] | None = None) -> int:
    if cli_val is not None:
        if cli_val < 0:
            raise ValueError("--cargo-jobs must be >= 0")
        return cli_val
    env_map = os.environ if env is None else env
    raw = env_map.get("SYS_IO_GUARD_CARGO_JOBS", "").strip()
    if raw:
        try:
            val = int(raw)
        except ValueError:
            raise ValueError(f"invalid SYS_IO_GUARD_CARGO_JOBS={raw!r}: expected non-negative integer") from None
        if val < 0:
            raise ValueError("SYS_IO_GUARD_CARGO_JOBS must be >= 0")
        return val
    return default_cargo_jobs()


def main(argv: list[str] | None = None) -> int:
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    parser = build_parser()
    args = parser.parse_args(argv)

    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    if args.json and not (args.status or args.dry_run):
        print("error: --json is only supported with --status or --dry-run", file=sys.stderr)
        return EXIT_ERROR

    if args.timeout is not None:
        if math.isnan(args.timeout) or math.isinf(args.timeout) or args.timeout < 0:
            print("error: --timeout must be a finite number >= 0", file=sys.stderr)
            return EXIT_ERROR

    if not (0 <= args.nice <= 19):
        print("error: --nice must be between 0 and 19", file=sys.stderr)
        return EXIT_ERROR

    try:
        cargo_jobs = _resolve_cargo_jobs(args.cargo_jobs)
        extra_env = parse_env_overrides(args.env)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    lock_dir = resolve_lock_dir(args.lock_dir)

    if args.status:
        try:
            status = probe_locks(lock_dir)
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        if args.json:
            print(json.dumps(status, indent=2, sort_keys=True))
        else:
            print(render_status_text(status))
        return EXIT_OK

    target_input = args.path or os.environ.get("CARGO_TARGET_DIR") or os.getcwd()
    disk_info = resolve_path_to_disk(target_input)

    if args.lock_key is not None:
        try:
            lock_key = sanitize_lock_key(args.lock_key)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        lock_key_source = "override"
    else:
        lock_key = disk_info["lock_key"]
        lock_key_source = "disk" if disk_info["disk"] else "mount_fallback"

    lock_file = lock_dir / f"{lock_key}.lock"
    meta_file = lock_dir / f"{lock_key}.meta"

    env_overrides: dict[str, str] = {}
    if cargo_jobs > 0:
        env_overrides["CARGO_BUILD_JOBS"] = str(cargo_jobs)
    env_overrides.update(extra_env)

    if args.dry_run:
        plan = {
            "cargo_jobs": cargo_jobs,
            "command": cmd,
            "device": disk_info["device"],
            "disk": disk_info["disk"],
            "env_overrides": env_overrides,
            "fstype": disk_info["fstype"],
            "lock_dir": str(lock_dir),
            "lock_file": str(lock_file),
            "lock_key": lock_key,
            "lock_key_source": lock_key_source,
            "meta_file": str(meta_file),
            "mountpoint": disk_info["mountpoint"],
            "nice": args.nice,
            "target_path": disk_info["target_path"],
            "timeout": args.timeout,
        }
        if args.json:
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            print(render_dry_run_text(plan))
        return EXIT_OK

    if not cmd:
        print("error: missing command (pass command after '--', or use --status / --dry-run)", file=sys.stderr)
        return EXIT_ERROR

    prev_sigterm = None

    def _on_sigterm(signum: int, _frame: Any) -> None:
        raise _SignalExit(signum)

    if hasattr(signal, "SIGTERM"):
        prev_sigterm = signal.signal(signal.SIGTERM, _on_sigterm)

    lock_fd: int | None = None
    try:
        lock_fd, wait_sec = acquire_lock(lock_file, meta_file, lock_key, args.timeout)
        meta = {
            "cmd": cmd,
            "lock_key": lock_key,
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "target_path": disk_info["target_path"],
        }
        write_holder_meta(meta_file, meta)

        if args.verbose:
            env_desc = ", ".join(f"{k}={v}" for k, v in sorted(env_overrides.items())) or "(none)"
            print(
                f"sys-io-guard: acquired lock_key={lock_key} device={disk_info['device'] or '(virtual)'} "
                f"disk={disk_info['disk'] or '(fallback)'} lock_file={lock_file} "
                f"wait={wait_sec:.3f}s nice={args.nice} env={env_desc}",
                file=sys.stderr,
            )

        child_env = os.environ.copy()
        child_env.update(env_overrides)
        preexec = (lambda: _apply_child_nice(args.nice)) if args.nice > 0 else None
        proc = subprocess.run(cmd, env=child_env, preexec_fn=preexec, check=False)
        if proc.returncode < 0:
            return 128 + abs(proc.returncode)
        return proc.returncode
    except LockBusyError as exc:
        print(
            f"error: lock '{exc.lock_key}' ({exc.lock_file}) busy after {exc.timeout}s; "
            f"{format_holder_summary(exc.holder)}",
            file=sys.stderr,
        )
        return EXIT_BUSY
    except _SignalExit as exc:
        return 128 + exc.signum
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if lock_fd is not None:
            release_lock(lock_fd, meta_file)
        if prev_sigterm is not None and hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, prev_sigterm)


if __name__ == "__main__":
    sys.exit(main())
