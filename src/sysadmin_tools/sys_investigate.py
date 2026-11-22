#!/usr/bin/env python3
"""sys-investigate: diagnose what is driving CPU/load and whether it is productive.

Reads Linux /proc and /sys directly (no shelling out to top/iostat/vmstat), so it
stays responsive even when disks are severely stalled.

Examples:
  sys-investigate
  sys-investigate --interval 3 --top 15
  sys-investigate --json | jq '.verdict'
  sys-investigate --watch 5 --interval 2
  sys-investigate --progress-check --interval 2
  sys-investigate --append-log /var/tmp/sys-investigate.jsonl --no-statvfs
  sys-investigate --exit-on-issue --json > /tmp/diag.json
  sys-investigate --diagnose-pid 12345 --interval 2
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import signal
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# Pure-procfs implementation: avoids fork/exec and disk I/O during severe stalls.

SECTOR_BYTES = 512
CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"]) if hasattr(os, "sysconf") else 100
CPU_COUNT = os.cpu_count() or 1

BUILD_OR_HEAVY_COMMS = {
    "cargo",
    "cargo-clippy",
    "rustc",
    "rust-lld",
    "ld",
    "lld",
    "mold",
    "cc1",
    "cc1plus",
    "gcc",
    "g++",
    "clang",
    "clang++",
    "ninja",
    "make",
    "cmake",
    "pytest",
    "python",
    "python3",
    "node",
    "node-MainThread",
    "go",
    "javac",
    "ffmpeg",
}


def read_text(path: str | Path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def read_bytes(path: str | Path) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def read_cpu_times() -> list[int]:
    txt = read_text("/proc/stat")
    if not txt:
        return [0] * 10
    for line in txt.splitlines():
        if line.startswith("cpu "):
            parts = [int(x) for x in line.split()[1:11]]
            return parts + [0] * max(0, 10 - len(parts))
    return [0] * 10


def compute_cpu_pct(t0: list[int], t1: list[int]) -> dict[str, float]:
    deltas = [max(0, b - a) for a, b in zip(t0, t1)]
    total = sum(deltas)
    if total <= 0:
        return {k: 0.0 for k in ("us", "ni", "sy", "id", "wa", "hi", "si", "st")}
    names = ("us", "ni", "sy", "id", "wa", "hi", "si", "st")
    return {name: round(100.0 * deltas[i] / total, 1) for i, name in enumerate(names)}


def read_loadavg() -> dict[str, Any]:
    txt = read_text("/proc/loadavg") or "0 0 0 0/0 0"
    parts = txt.split()
    run_tot = parts[3].split("/") if len(parts) > 3 and "/" in parts[3] else ["0", "0"]
    return {
        "load1": float(parts[0]),
        "load5": float(parts[1]),
        "load15": float(parts[2]),
        "runnable_kernel": int(run_tot[0]),
        "entities_total": int(run_tot[1]),
        "cpus": CPU_COUNT,
    }


def read_uptime_sec() -> float:
    txt = read_text("/proc/uptime")
    if not txt:
        return 0.0
    try:
        return float(txt.split()[0])
    except ValueError:
        return 0.0


def read_psi() -> dict[str, dict[str, dict[str, float]]]:
    out: dict[str, dict[str, dict[str, float]]] = {}
    for kind in ("cpu", "io", "memory"):
        txt = read_text(f"/proc/pressure/{kind}")
        if not txt:
            continue
        entry: dict[str, dict[str, float]] = {}
        for line in txt.splitlines():
            parts = line.split()
            if not parts:
                continue
            scope = parts[0]
            vals: dict[str, float] = {}
            for tok in parts[1:]:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    try:
                        vals[k] = float(v)
                    except ValueError:
                        pass
            entry[scope] = vals
        out[kind] = entry
    return out


def read_meminfo() -> dict[str, float]:
    txt = read_text("/proc/meminfo") or ""
    raw: dict[str, int] = {}
    for line in txt.splitlines():
        if ":" not in line:
            continue
        k, rest = line.split(":", 1)
        toks = rest.strip().split()
        if toks and toks[0].isdigit():
            raw[k] = int(toks[0])  # kB
    kb = lambda k: raw.get(k, 0)
    mib = lambda k: round(kb(k) / 1024.0, 1)
    gib = lambda k: round(kb(k) / (1024.0 * 1024.0), 2)
    swap_total = kb("SwapTotal")
    swap_free = kb("SwapFree")
    return {
        "mem_total_gib": gib("MemTotal"),
        "mem_avail_gib": gib("MemAvailable"),
        "mem_free_gib": gib("MemFree"),
        "dirty_gib": gib("Dirty"),
        "dirty_mib": mib("Dirty"),
        "writeback_mib": mib("Writeback"),
        "swap_total_gib": round(swap_total / (1024.0 * 1024.0), 2),
        "swap_used_gib": round((swap_total - swap_free) / (1024.0 * 1024.0), 2),
        "swap_used_pct": round(100.0 * (swap_total - swap_free) / swap_total, 1) if swap_total > 0 else 0.0,
    }


def read_vmstat_swap() -> tuple[int, int]:
    txt = read_text("/proc/vmstat") or ""
    pswpin = pswo = 0
    for line in txt.splitlines():
        if line.startswith("pswpin "):
            pswpin = int(line.split()[1])
        elif line.startswith("pswpout "):
            pswo = int(line.split()[1])
    return pswpin, pswo


def is_whole_physical_disk(name: str) -> bool:
    if name.startswith(("loop", "ram", "zram", "dm-", "sr", "fd")):
        return False
    if name.startswith("nvme"):
        return "p" not in name.split("n", 2)[-1]
    if name.startswith(("sd", "vd", "hd", "xvd")):
        return not name[-1].isdigit()
    if name.startswith("mmcblk"):
        return "p" not in name
    return False


def read_diskstats() -> dict[str, dict[str, int]]:
    txt = read_text("/proc/diskstats") or ""
    stats: dict[str, dict[str, int]] = {}
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue
        name = parts[2]
        if not is_whole_physical_disk(name):
            continue
        stats[name] = {
            "r_ios": int(parts[3]),
            "r_sec": int(parts[5]),
            "r_ms": int(parts[6]),
            "w_ios": int(parts[7]),
            "w_sec": int(parts[9]),
            "w_ms": int(parts[10]),
            "in_flight": int(parts[11]),
            "io_ms": int(parts[12]),
            "weighted_ms": int(parts[13]),
        }
    return stats


NON_LOCAL_FSTYPES = frozenset(
    {
        "fuse",
        "fuseblk",
        "fusectl",
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "sshfs",
        "autofs",
        "davfs",
        "glusterfs",
        "ceph",
        "lustre",
        "9p",
        "virtiofs",
        "afs",
        "coda",
        "ncpfs",
        "gfs",
        "gfs2",
        "ocfs2",
    }
)

PSEUDO_PATH_PREFIXES = ("/proc", "/sys", "/dev", "/run")


def is_safe_statvfs_fstype(fstype: str) -> bool:
    """Return False for network, FUSE, or automount filesystems where statvfs can hang."""
    ft = fstype.lower()
    if ft in NON_LOCAL_FSTYPES or ft.startswith("fuse."):
        return False
    return True


def map_disks_to_mounts(
    allow_statvfs: bool = True,
    mounts_txt: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    txt = read_text("/proc/mounts") or "" if mounts_txt is None else mounts_txt
    mapping: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].startswith("/dev/"):
            continue
        fstype = parts[2]
        try:
            resolved_dev = os.path.realpath(parts[0])
        except OSError:
            resolved_dev = parts[0]
        dev_name = os.path.basename(resolved_dev)
        mnt = parts[1].replace("\\040", " ")
        parent = dev_name
        if dev_name.startswith("nvme") and "p" in dev_name:
            parent = dev_name.rsplit("p", 1)[0]
        elif dev_name.startswith(("sd", "vd", "hd", "xvd")):
            parent = dev_name.rstrip("0123456789")
        elif dev_name.startswith("mmcblk") and "p" in dev_name:
            parent = dev_name.rsplit("p", 1)[0]
        if (parent, mnt) in seen:
            continue
        seen.add((parent, mnt))
        used_pct = None
        if allow_statvfs and is_safe_statvfs_fstype(fstype):
            try:
                st = os.statvfs(mnt)
                if st.f_blocks > 0:
                    used_pct = round(100.0 * (st.f_blocks - st.f_bfree) / st.f_blocks, 1)
            except OSError:
                pass
        mapping[parent].append({"mount": mnt, "fstype": fstype, "used_pct": used_pct})
    return dict(mapping)


def resolve_mount_for_path(path: str, mount_points: list[str]) -> str | None:
    """Resolve a file path to its deepest matching mountpoint, ignoring pseudo-filesystems."""
    if not path.startswith("/"):
        return None
    if path.endswith(" (deleted)"):
        path = path[: -len(" (deleted)")]
    for prefix in PSEUDO_PATH_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return None
    best: str | None = None
    best_len = -1
    for mnt in mount_points:
        if mnt == "/":
            matched = True
        else:
            norm = mnt.rstrip("/")
            matched = path == norm or path.startswith(norm + "/")
        if matched and len(mnt) > best_len:
            best = mnt
            best_len = len(mnt)
    return best


def attribute_pid_mounts(
    pid: int,
    mount_points: list[str],
    max_fds: int = 128,
    max_mounts: int = 3,
    proc_root: str = "/proc",
) -> list[str]:
    """Inspect /proc/<pid>/cwd and bounded /proc/<pid>/fd/* symlinks to attribute active mounts."""
    if not mount_points:
        return []
    hits: Counter[str] = Counter()
    pdir = f"{proc_root}/{pid}"
    try:
        cwd = os.readlink(f"{pdir}/cwd")
        m = resolve_mount_for_path(cwd, mount_points)
        if m:
            hits[m] += 1
    except OSError:
        pass

    try:
        fd_names = os.listdir(f"{pdir}/fd")
    except OSError:
        fd_names = []
    for fd in fd_names[:max_fds]:
        try:
            target = os.readlink(f"{pdir}/fd/{fd}")
        except OSError:
            continue
        m = resolve_mount_for_path(target, mount_points)
        if m:
            hits[m] += 1

    ranked = sorted(hits.items(), key=lambda kv: (-kv[1], kv[0] == "/", -len(kv[0]), kv[0]))
    return [m for m, _ in ranked[:max_mounts]]


def read_disk_model(dev: str) -> str:
    m = read_text(f"/sys/block/{dev}/device/model")
    return m.strip() if m else ""


def compute_disk_deltas(
    d0: dict[str, dict[str, int]],
    d1: dict[str, dict[str, int]],
    dt_sec: float,
    mount_map: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if dt_sec <= 0:
        return rows
    for dev, s1 in sorted(d1.items()):
        s0 = d0.get(dev)
        if not s0:
            continue
        r_ios = max(0, s1["r_ios"] - s0["r_ios"])
        w_ios = max(0, s1["w_ios"] - s0["w_ios"])
        r_sec = max(0, s1["r_sec"] - s0["r_sec"])
        w_sec = max(0, s1["w_sec"] - s0["w_sec"])
        r_ms = max(0, s1["r_ms"] - s0["r_ms"])
        w_ms = max(0, s1["w_ms"] - s0["w_ms"])
        io_ms = max(0, s1["io_ms"] - s0["io_ms"])
        w_q_ms = max(0, s1["weighted_ms"] - s0["weighted_ms"])

        util_pct = min(100.0, round(100.0 * io_ms / (dt_sec * 1000.0), 1))
        aqu_sz = round(w_q_ms / (dt_sec * 1000.0), 2)
        r_mbs = round((r_sec * SECTOR_BYTES) / (1e6 * dt_sec), 2)
        w_mbs = round((w_sec * SECTOR_BYTES) / (1e6 * dt_sec), 2)
        r_await = round(r_ms / r_ios, 1) if r_ios > 0 else 0.0
        w_await = round(w_ms / w_ios, 1) if w_ios > 0 else 0.0

        rows.append(
            {
                "device": dev,
                "model": read_disk_model(dev),
                "mounts": mount_map.get(dev, []),
                "r_iops": round(r_ios / dt_sec, 1),
                "w_iops": round(w_ios / dt_sec, 1),
                "r_mbs": r_mbs,
                "w_mbs": w_mbs,
                "r_await_ms": r_await,
                "w_await_ms": w_await,
                "aqu_sz": aqu_sz,
                "in_flight": s1["in_flight"],
                "util_pct": util_pct,
            }
        )
    rows.sort(key=lambda r: (r["util_pct"], r["aqu_sz"], r["w_mbs"] + r["r_mbs"]), reverse=True)
    return rows


def read_gpu_busy() -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    drm = Path("/sys/class/drm")
    if not drm.exists():
        return gpus
    for card in sorted(drm.glob("card[0-9]*")):
        if "-" in card.name:
            continue
        busy_txt = read_text(card / "device" / "gpu_busy_percent")
        if busy_txt is None:
            continue
        try:
            busy = int(busy_txt.strip())
        except ValueError:
            continue
        gpus.append({"card": card.name, "busy_pct": busy})
    return gpus


def _read_sysfs_int(path: str | Path) -> int | None:
    txt = read_text(path)
    if txt is None:
        return None
    s = txt.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _natural_suffix_key(name: str, prefix: str) -> tuple[int, int | str, str]:
    suffix = name[len(prefix) :] if name.startswith(prefix) else name
    if suffix.isdigit():
        return (0, int(suffix), name)
    return (1, suffix, name)


def read_nvme_health(sys_nvme_root: str = "/sys/class/nvme") -> list[dict[str, Any]]:
    """Read NVMe controller state and hwmon thermal sensors directly from /sys."""
    root = Path(sys_nvme_root)
    if not root.exists():
        return []
    try:
        entries = [
            p
            for p in root.iterdir()
            if p.name.startswith("nvme") and "n" not in p.name[4:] and p.is_dir()
        ]
    except OSError:
        return []

    entries.sort(key=lambda p: _natural_suffix_key(p.name, "nvme"))
    results: list[dict[str, Any]] = []
    for ctrl_dir in entries:
        model = (read_text(ctrl_dir / "model") or "").strip()
        state = (read_text(ctrl_dir / "state") or "unknown").strip() or "unknown"

        sensors: list[dict[str, Any]] = []
        try:
            hwmons = sorted(
                (p for p in ctrl_dir.iterdir() if p.name.startswith("hwmon") and p.is_dir()),
                key=lambda p: _natural_suffix_key(p.name, "hwmon"),
            )
        except OSError:
            hwmons = []

        for hwmon in hwmons:
            try:
                hw_files = [p.name for p in hwmon.iterdir()]
            except OSError:
                continue
            indices: list[int] = []
            for fname in hw_files:
                if fname.startswith("temp") and fname.endswith("_input"):
                    mid = fname[4:-6]
                    if mid.isdigit():
                        indices.append(int(mid))
            for idx in sorted(set(indices)):
                raw_temp = _read_sysfs_int(hwmon / f"temp{idx}_input")
                if raw_temp is None:
                    continue
                t_c = round(raw_temp / 1000.0, 1)
                raw_max = _read_sysfs_int(hwmon / f"temp{idx}_max")
                raw_crit = _read_sysfs_int(hwmon / f"temp{idx}_crit")
                raw_alarm = _read_sysfs_int(hwmon / f"temp{idx}_alarm")
                label = (read_text(hwmon / f"temp{idx}_label") or "").strip() or f"temp{idx}"
                sensors.append(
                    {
                        "label": label,
                        "temp_c": t_c,
                        "max_c": round(raw_max / 1000.0, 1) if raw_max is not None else None,
                        "crit_c": round(raw_crit / 1000.0, 1) if raw_crit is not None else None,
                        "alarm": bool(raw_alarm != 0) if raw_alarm is not None else False,
                    }
                )

        primary = next((s for s in sensors if s["label"].lower() == "composite"), None)
        if primary is None and sensors:
            primary = sensors[0]

        temp_c = primary["temp_c"] if primary else None
        temp_max_c = primary["max_c"] if primary else None
        temp_crit_c = primary["crit_c"] if primary else None
        alarm = any(s["alarm"] for s in sensors)

        if (
            alarm
            or (temp_crit_c is not None and temp_c is not None and temp_c >= temp_crit_c)
            or state != "live"
        ):
            thermal_status = "CRITICAL"
        elif (
            (temp_max_c is not None and temp_c is not None and temp_c >= temp_max_c)
            or (temp_c is not None and temp_c >= 70.0)
        ):
            thermal_status = "WARM"
        else:
            thermal_status = "OK"

        results.append(
            {
                "controller": ctrl_dir.name,
                "model": model,
                "state": state,
                "temp_c": temp_c,
                "temp_max_c": temp_max_c,
                "temp_crit_c": temp_crit_c,
                "alarm": alarm,
                "thermal_status": thermal_status,
                "sensors": sensors,
            }
        )
    return results


def read_thermal_zones(sys_thermal_root: str = "/sys/class/thermal") -> list[dict[str, Any]]:
    """Read Linux thermal_zone* temperatures directly from /sys."""
    root = Path(sys_thermal_root)
    if not root.exists():
        return []
    try:
        zones = [p for p in root.iterdir() if p.name.startswith("thermal_zone") and p.is_dir()]
    except OSError:
        return []

    zones.sort(key=lambda p: _natural_suffix_key(p.name, "thermal_zone"))
    out: list[dict[str, Any]] = []
    for zdir in zones:
        raw_temp = _read_sysfs_int(zdir / "temp")
        if raw_temp is None:
            continue
        ztype = (read_text(zdir / "type") or "").strip() or zdir.name
        out.append({"zone": zdir.name, "type": ztype, "temp_c": round(raw_temp / 1000.0, 1)})
    return out


def _truncate_label(label: str, max_len: int = 40) -> str:
    if len(label) <= max_len:
        return label
    return label[: max_len - 3] + "..."


def _classify_cgroup_path(path: str) -> str:
    p = path.strip()
    if not p:
        return "-"
    if p == "/":
        return "/"
    parts = [seg for seg in p.split("/") if seg]
    if not parts:
        return "/"

    # 1. Container runtimes (check deepest-first for scope names and path segments).
    for seg in reversed(parts):
        if seg.startswith("docker-") and seg.endswith(".scope"):
            cid = seg[len("docker-") : -len(".scope")]
            if cid:
                return f"docker:{cid[:12]}"
        if seg.startswith("libpod-") and seg.endswith(".scope"):
            cid = seg[len("libpod-") : -len(".scope")]
            if cid:
                return f"podman:{cid[:12]}"
        if seg.startswith("cri-containerd-") and seg.endswith(".scope"):
            cid = seg[len("cri-containerd-") : -len(".scope")]
            if cid:
                return f"containerd:{cid[:12]}"

    for i, seg in enumerate(parts[:-1]):
        nxt = parts[i + 1]
        if seg == "docker" and nxt:
            return f"docker:{nxt[:12]}"
        if seg == "libpod" and nxt:
            return f"podman:{nxt[:12]}"
        if seg in ("cri-containerd", "containerd") and nxt:
            return f"containerd:{nxt[:12]}"

    # 2. Systemd service/scope unit (deepest match).
    for seg in reversed(parts):
        if seg.endswith((".service", ".scope")):
            return _truncate_label(f"systemd:{seg}", 40)

    # 3. Other non-root cgroup path.
    return _truncate_label(p if len(p) <= 40 else parts[-1], 40)


def parse_cgroup_text(txt: str) -> str:
    """Parse /proc/<pid>/cgroup (v2 unified or v1 fallback) into a concise attribution label."""
    if not txt or not txt.strip():
        return "-"

    v2_path: str | None = None
    v1_entries: list[tuple[str, str]] = []
    for raw_line in txt.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        hier, ctrl, path = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if hier == "0" and ctrl == "":
            v2_path = path
        else:
            v1_entries.append((ctrl, path))

    if v2_path is not None and v2_path not in ("", "/"):
        return _classify_cgroup_path(v2_path)

    if v1_entries:
        # Prefer container-specific paths, then name=systemd, then first non-root path.
        non_root = [(c, p) for c, p in v1_entries if p and p != "/"]
        if non_root:
            for _, p in non_root:
                lbl = _classify_cgroup_path(p)
                if lbl.startswith(("docker:", "podman:", "containerd:")):
                    return lbl
            for c, p in non_root:
                if "systemd" in c:
                    return _classify_cgroup_path(p)
            return _classify_cgroup_path(non_root[0][1])

    if v2_path == "/" or any(p == "/" for _, p in v1_entries):
        return "/"
    return "-"


def read_proc_cgroup(pid: int, proc_root: str = "/proc") -> str:
    """Read and classify /proc/<pid>/cgroup; returns '-' when missing or unreadable."""
    txt = read_text(f"{proc_root}/{pid}/cgroup")
    if txt is None:
        return "-"
    return parse_cgroup_text(txt)


def parse_proc_stat(stat_txt: str) -> tuple[str, str, int, int, int, int] | None:
    """Return (comm, state, ppid, utime, stime, starttime) from /proc/<pid>/stat."""
    lp = stat_txt.find("(")
    rp = stat_txt.rfind(")")
    if lp == -1 or rp == -1 or rp <= lp:
        return None
    comm = stat_txt[lp + 1 : rp]
    fields = stat_txt[rp + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        state = fields[0]
        ppid = int(fields[1])
        utime = int(fields[11])
        stime = int(fields[12])
        starttime = int(fields[19])
        return comm, state, ppid, utime, stime, starttime
    except ValueError:
        return None


@dataclasses.dataclass
class ProcSnap:
    pid: int
    ppid: int
    comm: str
    state: str
    cpu_ticks: int
    starttime: int
    cmdline: str
    read_bytes: int | None
    write_bytes: int | None
    rchar: int | None
    wchar: int | None
    rss_mib: float
    d_threads: dict[str, int]
    r_threads: int


def snapshot_procs(inspect_threads: bool = True, include_self: bool = False) -> dict[int, ProcSnap]:
    snaps: dict[int, ProcSnap] = {}
    my_pid = os.getpid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return snaps

    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if not include_self and pid == my_pid:
            continue
        pdir = f"/proc/{pid}"
        st_txt = read_text(f"{pdir}/stat")
        if not st_txt:
            continue
        parsed = parse_proc_stat(st_txt)
        if not parsed:
            continue
        comm, state, ppid, utime, stime, starttime = parsed

        raw_cmd = read_bytes(f"{pdir}/cmdline")
        if raw_cmd:
            cmdline = raw_cmd.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        else:
            cmdline = f"[{comm}]"

        rss_mib = 0.0
        statm = read_text(f"{pdir}/statm")
        if statm:
            parts = statm.split()
            if len(parts) >= 2 and parts[1].isdigit():
                rss_mib = round(int(parts[1]) * os.sysconf("SC_PAGE_SIZE") / (1024.0 * 1024.0), 1)

        rb = wb = rc = wc = None
        io_txt = read_text(f"{pdir}/io")
        if io_txt:
            vals: dict[str, int] = {}
            for line in io_txt.splitlines():
                if ": " in line:
                    k, v = line.split(": ", 1)
                    if v.isdigit():
                        vals[k] = int(v)
            rb = vals.get("read_bytes")
            wb = vals.get("write_bytes")
            rc = vals.get("rchar")
            wc = vals.get("wchar")

        d_threads: dict[str, int] = {}
        r_threads = 0
        if inspect_threads:
            try:
                tids = os.listdir(f"{pdir}/task")
            except OSError:
                tids = []
            for tid in tids:
                tst = read_text(f"{pdir}/task/{tid}/stat")
                if not tst:
                    continue
                rp = tst.rfind(")")
                if rp == -1 or rp + 2 >= len(tst):
                    continue
                tstate = tst[rp + 2]
                if tstate == "D":
                    wchan = (read_text(f"{pdir}/task/{tid}/wchan") or "-").strip() or "-"
                    d_threads[wchan] = d_threads.get(wchan, 0) + 1
                elif tstate == "R":
                    r_threads += 1

        snaps[pid] = ProcSnap(
            pid=pid,
            ppid=ppid,
            comm=comm,
            state=state,
            cpu_ticks=utime + stime,
            starttime=starttime,
            cmdline=cmdline,
            read_bytes=rb,
            write_bytes=wb,
            rchar=rc,
            wchar=wc,
            rss_mib=rss_mib,
            d_threads=d_threads,
            r_threads=r_threads,
        )
    return snaps


def format_elapsed(seconds: float) -> str:
    sec = max(0, int(seconds))
    days, rem = divmod(sec, 86400)
    hrs, rem = divmod(rem, 3600)
    mins, s = divmod(rem, 60)
    if days > 0:
        return f"{days}d{hrs:02d}h{mins:02d}m"
    if hrs > 0:
        return f"{hrs}h{mins:02d}m{s:02d}s"
    return f"{mins:02d}m{s:02d}s"


def build_ancestry(pid: int, snaps: dict[int, ProcSnap], uptime: float, max_depth: int = 8) -> str:
    chain: list[str] = []
    cur = pid
    seen: set[int] = set()
    while cur in snaps and cur not in seen and len(chain) < max_depth:
        seen.add(cur)
        p = snaps[cur]
        el = format_elapsed(uptime - p.starttime / CLK_TCK)
        chain.append(f"{p.pid}:{p.comm}({el})")
        if p.ppid <= 1:
            break
        cur = p.ppid
    return " <- ".join(chain)


def detect_overlapping_workloads(snaps: dict[int, ProcSnap], uptime: float) -> list[dict[str, Any]]:
    """Detect duplicate/concurrent build, test, or script families competing for resources."""
    groups: dict[str, list[ProcSnap]] = defaultdict(list)
    for p in snaps.values():
        cmd = p.cmdline
        if not cmd or cmd.startswith("["):
            continue
        toks = cmd.split()
        if p.comm in ("bash", "sh", "zsh") and len(toks) >= 2 and not toks[1].startswith("-"):
            key = f"script:{os.path.basename(toks[1])}"
            groups[key].append(p)
        elif p.comm in ("cargo", "cargo-clippy", "rustc", "rust-lld", "pytest", "ninja", "make"):
            groups[f"tool:{p.comm}"].append(p)

    findings: list[dict[str, Any]] = []
    for key, plist in sorted(groups.items()):
        min_count = 2 if key.startswith("script:") or key in ("tool:cargo", "tool:pytest", "tool:make", "tool:ninja") else 4
        if len(plist) < min_count:
            continue
        plist_sorted = sorted(plist, key=lambda x: x.starttime)
        parents = Counter(p.ppid for p in plist_sorted)
        findings.append(
            {
                "group": key,
                "count": len(plist_sorted),
                "pids": [p.pid for p in plist_sorted[:12]],
                "oldest_elapsed": format_elapsed(uptime - plist_sorted[0].starttime / CLK_TCK),
                "shared_parent_top": parents.most_common(1)[0][0] if parents else 0,
                "sample_cmds": [p.cmdline[:140] for p in plist_sorted[:5]],
                "sample_trees": [build_ancestry(p.pid, snaps, uptime) for p in plist_sorted[:3]],
            }
        )
    findings.sort(key=lambda x: (x["group"].startswith("script:"), x["count"]), reverse=True)
    return findings


def classify_verdict(
    cpu: dict[str, float],
    load: dict[str, Any],
    psi: dict[str, Any],
    mem: dict[str, float],
    swap_delta: tuple[int, int],
    disks: list[dict[str, Any]],
    d_state_total: int,
    d_wchan_counts: Counter[str],
    top_cpu: list[dict[str, Any]],
    overlaps: list[dict[str, Any]],
    turnover: dict[str, int],
    top_io: list[dict[str, Any]] | None = None,
    d_state_top: list[dict[str, Any]] | None = None,
    nvme_health: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    wa = cpu.get("wa", 0.0)
    busy_cpu = cpu.get("us", 0.0) + cpu.get("sy", 0.0) + cpu.get("ni", 0.0)
    io_some10 = psi.get("io", {}).get("some", {}).get("avg10", 0.0)
    mem_some10 = psi.get("memory", {}).get("some", {}).get("avg10", 0.0)
    cpu_some10 = psi.get("cpu", {}).get("some", {}).get("avg10", 0.0)

    sat_disks = [d for d in disks if d["util_pct"] >= 85.0 or d["w_await_ms"] >= 50.0 or d["aqu_sz"] >= 8.0]
    dirty_gb = mem["dirty_gib"]
    bdp_threads = d_wchan_counts.get("balance_dirty_pages", 0)
    swap_pages_delta = swap_delta[0] + swap_delta[1]

    io_bound = wa >= 20.0 or io_some10 >= 30.0 or bool(sat_disks) or d_state_total >= 16
    mem_thrash = mem_some10 >= 20.0 or swap_pages_delta >= 1000 or mem["mem_avail_gib"] < 1.0
    cpu_bound = busy_cpu >= 70.0 or cpu_some10 >= 30.0

    if mem_thrash and io_bound:
        bottleneck = "MEMORY_SWAP_AND_IO_THRASH"
    elif mem_thrash:
        bottleneck = "MEMORY_SWAP_THRASH"
    elif io_bound and cpu_bound:
        bottleneck = "MIXED_CPU_AND_IO"
    elif io_bound:
        bottleneck = "IO_WAIT_SATURATION"
    elif cpu_bound:
        bottleneck = "CPU_COMPUTE_BOUND"
    else:
        bottleneck = "NORMAL_OR_IDLE"

    reasons: list[str] = []
    recommendations: list[str] = []

    concurrent_heavy = [
        o for o in overlaps if o["group"].startswith("script:") or o["group"] in ("tool:cargo", "tool:rust-lld", "tool:rustc")
    ]
    total_disk_mbs = sum(d["r_mbs"] + d["w_mbs"] for d in (sat_disks or disks))
    worst_w_await = max((d["w_await_ms"] for d in disks), default=0.0)
    worst_util = max((d["util_pct"] for d in disks), default=0.0)
    worst_aqu = max((d["aqu_sz"] for d in disks), default=0.0)

    if bottleneck == "NORMAL_OR_IDLE":
        productivity = "HEALTHY_IDLE_OR_LIGHT"
        reasons.append(f"CPU active={busy_cpu:.1f}%, iowait={wa:.1f}%, load1={load['load1']:.2f}/{load['cpus']} cores.")
    elif bottleneck in ("IO_WAIT_SATURATION", "MEMORY_SWAP_AND_IO_THRASH", "MIXED_CPU_AND_IO"):
        thrashing = (
            bdp_threads >= 4
            or dirty_gb >= 2.0
            or (worst_util >= 90.0 and worst_w_await >= 100.0)
            or (worst_w_await >= 250.0 and worst_aqu >= 16.0)
            or mem_thrash
        )
        making_progress = turnover["started"] > 0 or turnover["exited"] > 0 or total_disk_mbs >= 5.0
        if thrashing and not making_progress:
            productivity = "WEDGED_OR_STALLED"
            reasons.append("Severe disk/memory stall with negligible throughput or process turnover in sample window.")
        elif thrashing:
            productivity = "THRASHING_LOW_EFFICIENCY"
            reasons.append(
                f"Making slow forward progress ({turnover['started']} started, {turnover['exited']} exited in window) "
                f"but heavily throttled by I/O contention."
            )
        else:
            productivity = "PRODUCTIVE_IO"
            reasons.append(f"Active I/O workload moving {total_disk_mbs:.1f} MB/s without severe writeback stall.")

        if dirty_gb >= 2.0 or bdp_threads > 0:
            reasons.append(
                f"Dirty page backlog={dirty_gb:.2f} GiB with {bdp_threads} thread(s) stalled in balance_dirty_pages."
            )
        for d in sat_disks[:3]:
            mnts = ",".join(m["mount"] for m in d["mounts"]) or "unmounted"
            reasons.append(
                f"{d['device']} ({mnts}): util={d['util_pct']}%, aqu-sz={d['aqu_sz']}, "
                f"w_await={d['w_await_ms']}ms, w={d['w_mbs']} MB/s, r={d['r_mbs']} MB/s."
            )
        if concurrent_heavy:
            summary = ", ".join(f"{o['group']} x{o['count']}" for o in concurrent_heavy[:4])
            reasons.append(f"Overlapping build/test workloads detected: {summary}.")
            recommendations.append("Serialize overlapping build/verify scripts so only one runs per SSD at a time.")
            if any("cargo" in o["group"] or "rust" in o["group"] for o in concurrent_heavy):
                recommendations.append("Cap Cargo parallelism (e.g. CARGO_BUILD_JOBS=4..8) to avoid SSD writeback collapse.")
        if mem_thrash:
            recommendations.append("Reduce concurrent memory consumers to stop swap/page-cache thrashing.")

        sat_targets = [
            f"{d['device']}({','.join(m['mount'] for m in d['mounts']) or 'unmounted'})"
            for d in sat_disks[:2]
        ]
        sat_label = ", ".join(sat_targets) if sat_targets else "saturated storage"
        if productivity == "WEDGED_OR_STALLED":
            recommendations.append(
                f"Storage/memory appears stalled on {sat_label}; inspect top D-state PIDs, "
                "run --progress-check to confirm stall, and check dmesg/NVMe health."
            )
        elif productivity == "THRASHING_LOW_EFFICIENCY" and not recommendations:
            top_writers = [
                f"{p['pid']}:{p['comm']}"
                for p in (top_io or [])[:3]
                if p.get("w_mbs", 0.0) > 0 or p.get("io_total_mbs", 0.0) > 0
            ]
            writer_note = f" ({', '.join(top_writers)})" if top_writers else ""
            recommendations.append(
                f"Throttle or pause heavy writers{writer_note} targeting {sat_label} "
                f"(dirty={dirty_gb:.2f} GiB, w_await={worst_w_await:.0f}ms) or run with ionice -c3 / lower parallelism."
            )
    elif bottleneck == "MEMORY_SWAP_THRASH":
        productivity = "THRASHING_LOW_EFFICIENCY"
        reasons.append(f"Memory PSI some10={mem_some10}%, swap pages delta={swap_pages_delta}, avail={mem['mem_avail_gib']} GiB.")
        recommendations.append("Kill or throttle largest RSS processes to relieve memory pressure.")
    else:
        single_spinners = [p for p in top_cpu if p["cpu_pct"] >= 95.0 and p["io_total_mbs"] == 0.0 and p["elapsed_sec"] >= 300]
        if single_spinners and busy_cpu < 15.0 * len(single_spinners) and turnover["started"] == 0 and turnover["exited"] == 0:
            productivity = "SUSPECT_SPIN_LOOP"
            reasons.append(
                f"Long-running CPU spinner(s) with zero I/O: "
                + ", ".join(f"{p['pid']}:{p['comm']}({p['cpu_pct']}%)" for p in single_spinners[:3])
            )
            recommendations.append("Inspect suspected spin-loop PIDs with strace/perf or terminate if unintended.")
        else:
            productivity = "PRODUCTIVE_COMPUTE"
            reasons.append(f"CPU actively computing ({busy_cpu:.1f}% busy, iowait={wa:.1f}%, load1={load['load1']:.2f}).")

    crit_nvme = [n for n in (nvme_health or []) if n.get("thermal_status") == "CRITICAL"]
    warm_nvme = [n for n in (nvme_health or []) if n.get("thermal_status") == "WARM"]
    if crit_nvme:
        details = ", ".join(
            f"{n['controller']}({n['temp_c']}°C, state={n['state']}, alarm={n['alarm']})"
            if n.get("temp_c") is not None
            else f"{n['controller']}(state={n['state']}, alarm={n['alarm']})"
            for n in crit_nvme
        )
        reasons.append(f"NVMe critical thermal/health alert: {details}.")
        ctrls = ", ".join(n["controller"] for n in crit_nvme)
        recommendations.append(
            f"Check NVMe cooling, airflow, and SMART/dmesg health immediately for critical controller(s): {ctrls}."
        )
    if warm_nvme:
        details = ", ".join(
            f"{n['controller']}({n['temp_c']}°C"
            + (f"/max {n['temp_max_c']}°C" if n.get("temp_max_c") is not None else "")
            + ")"
            for n in warm_nvme
        )
        reasons.append(f"NVMe elevated temperature warning: {details}.")
        ctrls = ", ".join(n["controller"] for n in warm_nvme)
        recommendations.append(
            f"Monitor NVMe thermals and airflow for warm controller(s) ({ctrls}) to prevent thermal throttling."
        )

    is_issue = (
        productivity in ("THRASHING_LOW_EFFICIENCY", "WEDGED_OR_STALLED", "SUSPECT_SPIN_LOOP")
        or bool(crit_nvme)
    )
    if not recommendations:
        if is_issue:
            recommendations.append("Inspect top offending PIDs and wait channels above; throttle or restart stalled workloads.")
        else:
            recommendations.append("No immediate intervention needed.")

    return {
        "bottleneck": bottleneck,
        "productivity": productivity,
        "is_issue": is_issue,
        "reasons": reasons,
        "recommendations": recommendations,
    }


def compare_windows(rep1: dict[str, Any], rep2: dict[str, Any]) -> dict[str, Any]:
    """Compare two consecutive investigation windows to confirm stalls vs transient spikes."""
    v1 = rep1["verdict"]
    v2 = rep2["verdict"]
    dirty_delta = round(rep2["memory"]["dirty_gib"] - rep1["memory"]["dirty_gib"], 2)
    total_started = rep1["turnover"]["started"] + rep2["turnover"]["started"]
    total_exited = rep1["turnover"]["exited"] + rep2["turnover"]["exited"]

    d1_pids = {g["pid"] for g in rep1.get("d_state_top", [])}
    d2_pids = {g["pid"] for g in rep2.get("d_state_top", [])}
    persistent_d = sorted(d1_pids & d2_pids)

    c1_pids = {p["pid"] for p in rep1.get("top_cpu", []) if p.get("cpu_pct", 0.0) >= 50.0}
    c2_pids = {p["pid"] for p in rep2.get("top_cpu", []) if p.get("cpu_pct", 0.0) >= 50.0}
    persistent_cpu = sorted(c1_pids & c2_pids)

    stall_states = ("WEDGED_OR_STALLED", "SUSPECT_SPIN_LOOP")
    if v1["productivity"] in stall_states and v2["productivity"] in stall_states:
        status = "CONFIRMED_STALL"
    elif v1["is_issue"] and v2["is_issue"]:
        status = "PERSISTENT_THRASH"
    elif v1["is_issue"] and not v2["is_issue"]:
        status = "RECOVERING"
    elif not v1["is_issue"] and v2["is_issue"]:
        status = "WORSENING"
    elif v2["bottleneck"] == "NORMAL_OR_IDLE":
        status = "IDLE"
    else:
        status = "STEADY_PROGRESS"

    sign = "+" if dirty_delta >= 0 else ""
    summary = (
        f"{status}: w1={v1['productivity']} -> w2={v2['productivity']}; "
        f"dirty_delta={sign}{dirty_delta:.2f} GiB; "
        f"turnover=+{total_started}/-{total_exited}; "
        f"persistent_d_pids={len(persistent_d)}, persistent_cpu_pids={len(persistent_cpu)}."
    )
    return {
        "windows": 2,
        "status": status,
        "window1_verdict": {
            "bottleneck": v1["bottleneck"],
            "productivity": v1["productivity"],
            "is_issue": v1["is_issue"],
        },
        "window2_verdict": {
            "bottleneck": v2["bottleneck"],
            "productivity": v2["productivity"],
            "is_issue": v2["is_issue"],
        },
        "dirty_gib_delta": dirty_delta,
        "total_turnover": {"started": total_started, "exited": total_exited},
        "persistent_d_pids": persistent_d,
        "persistent_cpu_pids": persistent_cpu,
        "summary": summary,
    }


def append_jsonl_log(path: str | Path, rep: dict[str, Any]) -> None:
    """Append one deterministic compact JSON line to PATH, creating parent directories if needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rep, sort_keys=True, separators=(",", ":")) + "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(line)


def investigate(interval: float = 2.0, top_n: int = 10, no_statvfs: bool = False) -> dict[str, Any]:
    ts_start = time.monotonic()
    cpu0 = read_cpu_times()
    disk0 = read_diskstats()
    swap0 = read_vmstat_swap()
    procs0 = snapshot_procs(inspect_threads=False)

    time.sleep(interval)

    cpu1 = read_cpu_times()
    disk1 = read_diskstats()
    swap1 = read_vmstat_swap()
    procs1 = snapshot_procs(inspect_threads=True)
    dt_sec = max(0.05, time.monotonic() - ts_start)

    uptime = read_uptime_sec()
    cpu_pct = compute_cpu_pct(cpu0, cpu1)
    load = read_loadavg()
    psi = read_psi()
    mem = read_meminfo()
    mount_map = map_disks_to_mounts(allow_statvfs=not no_statvfs)
    disks = compute_disk_deltas(disk0, disk1, dt_sec, mount_map)
    gpus = read_gpu_busy()
    nvme_health = read_nvme_health()
    thermal_zones = read_thermal_zones()

    all_mount_points = sorted({m["mount"] for mlist in mount_map.values() for m in mlist})

    swap_delta = (max(0, swap1[0] - swap0[0]), max(0, swap1[1] - swap0[1]))
    pids0, pids1 = set(procs0.keys()), set(procs1.keys())
    turnover = {
        "started": len(pids1 - pids0),
        "exited": len(pids0 - pids1),
        "total_procs": len(procs1),
    }

    proc_metrics: list[dict[str, Any]] = []
    d_groups: list[dict[str, Any]] = []
    d_wchan_counts: Counter[str] = Counter()
    d_state_total = 0
    r_state_total = 0

    for pid, p1 in procs1.items():
        r_state_total += p1.r_threads
        if p1.d_threads:
            for wchan, cnt in p1.d_threads.items():
                d_state_total += cnt
                d_wchan_counts[wchan] += cnt
                d_groups.append(
                    {
                        "threads": cnt,
                        "pid": pid,
                        "comm": p1.comm,
                        "wchan": wchan,
                        "elapsed": format_elapsed(uptime - p1.starttime / CLK_TCK),
                        "cmdline": p1.cmdline[:160],
                    }
                )

        p0 = procs0.get(pid)
        if not p0 or p0.starttime != p1.starttime:
            continue
        dticks = max(0, p1.cpu_ticks - p0.cpu_ticks)
        pct = round(100.0 * (dticks / CLK_TCK) / dt_sec, 1)

        r_mbs = w_mbs = wchar_mbs = 0.0
        if p1.read_bytes is not None and p0.read_bytes is not None:
            r_mbs = round(max(0, p1.read_bytes - p0.read_bytes) / (1e6 * dt_sec), 2)
        if p1.write_bytes is not None and p0.write_bytes is not None:
            w_mbs = round(max(0, p1.write_bytes - p0.write_bytes) / (1e6 * dt_sec), 2)
        if p1.wchar is not None and p0.wchar is not None:
            wchar_mbs = round(max(0, p1.wchar - p0.wchar) / (1e6 * dt_sec), 2)

        el_sec = max(0.0, uptime - p1.starttime / CLK_TCK)
        if pct >= 0.5 or (r_mbs + w_mbs) >= 0.05 or wchar_mbs >= 1.0:
            proc_metrics.append(
                {
                    "pid": pid,
                    "comm": p1.comm,
                    "state": p1.state,
                    "cpu_pct": pct,
                    "rss_mib": p1.rss_mib,
                    "r_mbs": r_mbs,
                    "w_mbs": w_mbs,
                    "io_total_mbs": round(r_mbs + w_mbs, 2),
                    "wchar_mbs": wchar_mbs,
                    "elapsed_sec": int(el_sec),
                    "elapsed": format_elapsed(el_sec),
                    "cmdline": p1.cmdline[:160],
                    "ancestry": build_ancestry(pid, procs1, uptime),
                }
            )

    top_cpu = sorted(proc_metrics, key=lambda x: (x["cpu_pct"], x["io_total_mbs"]), reverse=True)[:top_n]
    top_io = sorted(
        [m for m in proc_metrics if m["io_total_mbs"] > 0 or m["wchar_mbs"] >= 1.0],
        key=lambda x: (x["io_total_mbs"], x["wchar_mbs"]),
        reverse=True,
    )[:top_n]
    d_groups.sort(key=lambda x: (x["threads"], x["comm"], x["pid"]), reverse=True)
    d_state_top = d_groups[:top_n]

    # Bounded mount and cgroup attribution only for selected top rows (avoids scanning all PIDs).
    pid_mount_cache: dict[int, list[str]] = {}
    for row in d_state_top:
        pid = row["pid"]
        if pid not in pid_mount_cache:
            pid_mount_cache[pid] = attribute_pid_mounts(pid, all_mount_points)
        row["mounts"] = pid_mount_cache[pid]
    for row in top_io:
        pid = row["pid"]
        if pid not in pid_mount_cache:
            pid_mount_cache[pid] = attribute_pid_mounts(pid, all_mount_points)
        row["mounts"] = pid_mount_cache[pid]

    pid_cgroup_cache: dict[int, str] = {}
    for row in (*top_cpu, *top_io, *d_state_top):
        pid = row["pid"]
        if pid not in pid_cgroup_cache:
            pid_cgroup_cache[pid] = read_proc_cgroup(pid)
        row["cgroup"] = pid_cgroup_cache[pid]

    overlaps = detect_overlapping_workloads(procs1, uptime)
    verdict = classify_verdict(
        cpu=cpu_pct,
        load=load,
        psi=psi,
        mem=mem,
        swap_delta=swap_delta,
        disks=disks,
        d_state_total=d_state_total,
        d_wchan_counts=d_wchan_counts,
        top_cpu=top_cpu,
        overlaps=overlaps,
        turnover=turnover,
        top_io=top_io,
        d_state_top=d_state_top,
        nvme_health=nvme_health,
    )

    return {
        "timestamp": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "sample_sec": round(dt_sec, 2),
        "verdict": verdict,
        "cpu_pct": cpu_pct,
        "load": {**load, "r_threads": r_state_total, "d_threads": d_state_total},
        "psi": psi,
        "memory": {**mem, "swap_in_pages": swap_delta[0], "swap_out_pages": swap_delta[1]},
        "turnover": turnover,
        "disks": disks,
        "gpus": gpus,
        "nvme_health": nvme_health,
        "thermal_zones": thermal_zones,
        "d_state_wchans": dict(d_wchan_counts.most_common(10)),
        "d_state_top": d_state_top,
        "top_cpu": top_cpu,
        "top_io": top_io,
        "overlapping_workloads": overlaps[:top_n],
    }


def read_pid_swap_mib(pid: int, proc_root: str = "/proc") -> float:
    txt = read_text(f"{proc_root}/{pid}/status") or ""
    for line in txt.splitlines():
        if line.startswith("VmSwap:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return round(int(parts[1]) / 1024.0, 1)
    return 0.0


def read_pid_oom_score(pid: int, proc_root: str = "/proc") -> int | None:
    return _read_sysfs_int(f"{proc_root}/{pid}/oom_score")


def inspect_pid_threads(pid: int, proc_root: str = "/proc") -> dict[str, Any]:
    """Inspect all threads under /proc/<pid>/task for state counts and D-state wait channels."""
    tdir = f"{proc_root}/{pid}/task"
    try:
        tids = [t for t in os.listdir(tdir) if t.isdigit()]
    except OSError:
        tids = []

    total = r_cnt = d_cnt = s_cnt = 0
    d_wchans: Counter[str] = Counter()
    blocked: list[dict[str, Any]] = []

    for tid_str in sorted(tids, key=int):
        tst = read_text(f"{tdir}/{tid_str}/stat")
        if not tst:
            continue
        parsed = parse_proc_stat(tst)
        if not parsed:
            continue
        tcomm, tstate, _, _, _, _ = parsed
        total += 1
        if tstate == "R":
            r_cnt += 1
        elif tstate == "D":
            d_cnt += 1
            wchan = (read_text(f"{tdir}/{tid_str}/wchan") or "-").strip() or "-"
            d_wchans[wchan] += 1
            blocked.append({"tid": int(tid_str), "comm": tcomm, "state": tstate, "wchan": wchan})
        elif tstate in ("S", "I"):
            s_cnt += 1

    blocked.sort(key=lambda b: (b["wchan"], b["tid"]))
    return {
        "total_threads": total,
        "r_threads": r_cnt,
        "d_threads": d_cnt,
        "s_threads": s_cnt,
        "d_wchans": dict(d_wchans.most_common(10)),
        "blocked_threads": blocked,
    }


def classify_pid_verdict(
    target: dict[str, Any],
    threads: dict[str, Any],
    subtree: dict[str, Any],
    mem: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Classify a single process + descendant subtree into an actionable verdict."""
    pid = target.get("pid", 0)
    comm = target.get("comm", "?")
    state = target.get("state", "")
    d_threads = int(threads.get("d_threads", 0))
    subtree_d = int(subtree.get("subtree_d_threads", d_threads))
    d_wchans = threads.get("d_wchans") or {}

    cpu_pct = float(target.get("cpu_pct", 0.0))
    sub_cpu_pct = float(subtree.get("subtree_cpu_pct", cpu_pct))
    eff_cpu = max(cpu_pct, sub_cpu_pct)

    r_mbs = float(target.get("r_mbs", 0.0))
    w_mbs = float(target.get("w_mbs", 0.0))
    wchar_mbs = float(target.get("wchar_mbs", 0.0))
    sub_io_mbs = float(subtree.get("subtree_r_mbs", r_mbs)) + float(subtree.get("subtree_w_mbs", w_mbs))
    eff_io_mbs = max(r_mbs + w_mbs, sub_io_mbs)

    rss_mib = float(target.get("rss_mib", 0.0))
    sub_rss_mib = float(subtree.get("subtree_rss_mib", rss_mib))
    eff_rss_mib = max(rss_mib, sub_rss_mib)
    swap_mib = float(target.get("swap_mib", 0.0))
    oom_score = target.get("oom_score") or 0
    mem_total_mib = (float(mem.get("mem_total_gib", 0.0)) * 1024.0) if mem else 0.0
    rss_pct = (100.0 * eff_rss_mib / mem_total_mib) if mem_total_mib > 0 else 0.0

    reasons: list[str] = []
    recommendations: list[str] = []

    if state == "D" or d_threads > 0 or subtree_d >= 2:
        status = "BLOCKED_ON_IO"
        is_issue = True
        wchan_str = ", ".join(f"{k}={v}" for k, v in list(d_wchans.items())[:4]) or "uninterruptible sleep"
        reasons.append(
            f"PID {pid} ({comm}) has D-state threads (target={d_threads}, subtree={subtree_d}; wchans: {wchan_str})."
        )
        mnts = target.get("mounts") or []
        if mnts:
            reasons.append(f"Active mounts for PID {pid}: {', '.join(mnts)}.")
        if "balance_dirty_pages" in d_wchans:
            recommendations.append(
                "Process is throttled in balance_dirty_pages writeback; reduce concurrent writers or lower I/O priority (ionice -c3)."
            )
        else:
            recommendations.append(
                "Check backing disk latency/utilization and dmesg for storage stalls affecting this PID's mounts."
            )
    elif (
        swap_mib >= 512.0
        or (oom_score >= 500 and eff_rss_mib >= 1024.0)
        or rss_pct >= 25.0
        or eff_rss_mib >= 8192.0
    ):
        status = "MEMORY_HEAVY"
        is_issue = True
        reasons.append(
            f"PID {pid} ({comm}) has heavy memory footprint: rss={rss_mib:.1f} MiB "
            f"(subtree={sub_rss_mib:.1f} MiB), swap={swap_mib:.1f} MiB, oom_score={oom_score}."
        )
        recommendations.append(
            "Inspect memory growth/caches or set cgroup memory limits to avoid swap thrashing or OOM kills."
        )
    elif eff_cpu >= 80.0:
        status = "CPU_BOUND"
        is_spinner = (
            cpu_pct >= 95.0
            and eff_io_mbs == 0.0
            and int(target.get("elapsed_sec", 0)) >= 300
            and int(subtree.get("descendant_count", 0)) == 0
        )
        is_issue = is_spinner
        reasons.append(
            f"PID {pid} ({comm}) is CPU-bound: target={cpu_pct:.1f}% CPU, subtree={sub_cpu_pct:.1f}% CPU "
            f"(R-threads={threads.get('r_threads', 0)})."
        )
        if is_spinner:
            reasons.append("Sustained >=95% single-process CPU with zero I/O for >=5m (possible spin loop).")
            recommendations.append("Inspect with strace/perf or stack sampler to verify forward progress.")
        else:
            recommendations.append("CPU compute appears active; use nice/cpulimit/cgroup quota if throttling is needed.")
    elif eff_io_mbs >= 1.0 or wchar_mbs >= 5.0:
        status = "IO_ACTIVE"
        is_issue = False
        reasons.append(
            f"PID {pid} ({comm}) is performing active I/O: r={r_mbs:.2f} MB/s, w={w_mbs:.2f} MB/s, "
            f"wchar={wchar_mbs:.2f} MB/s (subtree total={sub_io_mbs:.2f} MB/s)."
        )
        recommendations.append("I/O is progressing without D-state stalls; no immediate intervention needed.")
    else:
        status = "IDLE_OR_SLEEPING"
        is_issue = False
        reasons.append(
            f"PID {pid} ({comm}) is mostly idle/sleeping (state={state or '?'}, cpu={eff_cpu:.1f}%, "
            f"io={eff_io_mbs:.2f} MB/s, rss={rss_mib:.1f} MiB)."
        )
        recommendations.append("No immediate intervention needed.")

    return {
        "status": status,
        "is_issue": is_issue,
        "reasons": reasons,
        "recommendations": recommendations,
    }


def investigate_pid(
    pid: int,
    interval: float = 2.0,
    top_n: int = 10,
    no_statvfs: bool = False,
) -> dict[str, Any]:
    """Run a two-snapshot targeted investigation of a single PID and its descendant subtree."""
    if pid <= 0:
        raise ValueError("PID must be > 0")
    include_self = pid == os.getpid()

    ts_start = time.monotonic()
    procs0 = snapshot_procs(inspect_threads=False, include_self=include_self)
    if pid not in procs0:
        raise ProcessLookupError(f"PID {pid} not found in /proc")

    time.sleep(interval)

    procs1 = snapshot_procs(inspect_threads=True, include_self=include_self)
    dt_sec = max(0.05, time.monotonic() - ts_start)
    if pid not in procs1:
        raise ProcessLookupError(f"PID {pid} exited during sample window")

    p0 = procs0[pid]
    p1 = procs1[pid]
    uptime = read_uptime_sec()
    mem = read_meminfo()
    mount_map = map_disks_to_mounts(allow_statvfs=not no_statvfs)
    all_mount_points = sorted({m["mount"] for mlist in mount_map.values() for m in mlist})

    # Target process deltas.
    same_instance = p0.starttime == p1.starttime
    dticks = max(0, p1.cpu_ticks - p0.cpu_ticks) if same_instance else 0
    cpu_pct = round(100.0 * (dticks / CLK_TCK) / dt_sec, 1)
    r_mbs = w_mbs = wchar_mbs = 0.0
    if same_instance:
        if p1.read_bytes is not None and p0.read_bytes is not None:
            r_mbs = round(max(0, p1.read_bytes - p0.read_bytes) / (1e6 * dt_sec), 2)
        if p1.write_bytes is not None and p0.write_bytes is not None:
            w_mbs = round(max(0, p1.write_bytes - p0.write_bytes) / (1e6 * dt_sec), 2)
        if p1.wchar is not None and p0.wchar is not None:
            wchar_mbs = round(max(0, p1.wchar - p0.wchar) / (1e6 * dt_sec), 2)

    el_sec = max(0.0, uptime - p1.starttime / CLK_TCK)
    cgroup = read_proc_cgroup(pid)
    mounts = attribute_pid_mounts(pid, all_mount_points)
    swap_mib = read_pid_swap_mib(pid)
    oom_score = read_pid_oom_score(pid)
    thread_summary = inspect_pid_threads(pid)

    # Build descendant subtree from ppid relationships in procs1.
    children_map: dict[int, list[int]] = defaultdict(list)
    for cpid, psnap in procs1.items():
        if cpid != psnap.ppid:
            children_map[psnap.ppid].append(cpid)

    descendant_pids: list[int] = []
    queue = list(children_map.get(pid, []))
    seen: set[int] = {pid}
    while queue:
        cur = queue.pop(0)
        if cur in seen or cur not in procs1:
            continue
        seen.add(cur)
        descendant_pids.append(cur)
        queue.extend(children_map.get(cur, []))

    desc_rows: list[dict[str, Any]] = []
    for dpid in descendant_pids:
        dp1 = procs1[dpid]
        dp0 = procs0.get(dpid)
        d_same = dp0 is not None and dp0.starttime == dp1.starttime
        d_cpu = round(100.0 * (max(0, dp1.cpu_ticks - dp0.cpu_ticks) / CLK_TCK) / dt_sec, 1) if d_same and dp0 else 0.0
        d_r = d_w = 0.0
        if d_same and dp0:
            if dp1.read_bytes is not None and dp0.read_bytes is not None:
                d_r = round(max(0, dp1.read_bytes - dp0.read_bytes) / (1e6 * dt_sec), 2)
            if dp1.write_bytes is not None and dp0.write_bytes is not None:
                d_w = round(max(0, dp1.write_bytes - dp0.write_bytes) / (1e6 * dt_sec), 2)
        d_el = max(0.0, uptime - dp1.starttime / CLK_TCK)
        desc_rows.append(
            {
                "pid": dpid,
                "ppid": dp1.ppid,
                "comm": dp1.comm,
                "state": dp1.state,
                "cpu_pct": d_cpu,
                "rss_mib": dp1.rss_mib,
                "r_mbs": d_r,
                "w_mbs": d_w,
                "d_threads": sum(dp1.d_threads.values()),
                "elapsed": format_elapsed(d_el),
                "cmdline": dp1.cmdline[:160],
            }
        )

    desc_rows.sort(key=lambda r: (r["d_threads"], r["cpu_pct"], r["w_mbs"] + r["r_mbs"], r["rss_mib"]), reverse=True)
    top_descendants = desc_rows[:top_n]
    for row in top_descendants:
        row["cgroup"] = read_proc_cgroup(row["pid"])

    subtree = {
        "descendant_count": len(desc_rows),
        "subtree_cpu_pct": round(cpu_pct + sum(r["cpu_pct"] for r in desc_rows), 1),
        "subtree_rss_mib": round(p1.rss_mib + sum(r["rss_mib"] for r in desc_rows), 1),
        "subtree_w_mbs": round(w_mbs + sum(r["w_mbs"] for r in desc_rows), 2),
        "subtree_r_mbs": round(r_mbs + sum(r["r_mbs"] for r in desc_rows), 2),
        "subtree_d_threads": int(thread_summary["d_threads"]) + sum(r["d_threads"] for r in desc_rows),
        "top_descendants": top_descendants,
    }

    target = {
        "pid": pid,
        "ppid": p1.ppid,
        "comm": p1.comm,
        "state": p1.state,
        "elapsed_sec": int(el_sec),
        "elapsed": format_elapsed(el_sec),
        "cmdline": p1.cmdline[:240],
        "ancestry": build_ancestry(pid, procs1, uptime),
        "cgroup": cgroup,
        "mounts": mounts,
        "cpu_pct": cpu_pct,
        "rss_mib": p1.rss_mib,
        "swap_mib": swap_mib,
        "r_mbs": r_mbs,
        "w_mbs": w_mbs,
        "wchar_mbs": wchar_mbs,
        "oom_score": oom_score,
    }

    verdict = classify_pid_verdict(target=target, threads=thread_summary, subtree=subtree, mem=mem)
    return {
        "mode": "pid",
        "timestamp": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "sample_sec": round(dt_sec, 2),
        "verdict": verdict,
        "target": target,
        "threads": thread_summary,
        "subtree": subtree,
    }


def render_pid_text(rep: dict[str, Any], markdown: bool = False) -> str:
    """Format a PID diagnostic report in plain text or Markdown."""
    t = rep["target"]
    th = rep["threads"]
    sub = rep["subtree"]
    v = rep["verdict"]
    h = (lambda s: f"## {s}") if markdown else (lambda s: f"=== {s} ===")

    lines: list[str] = []
    lines.append(
        f"{'# ' if markdown else ''}PID Investigation: {t['pid']} ({t['comm']}) @ {rep['timestamp']} (window={rep['sample_sec']}s)"
    )
    lines.append(f"Status      : {v['status']}")
    for r in v["reasons"]:
        lines.append(f"  - {r}")
    lines.append("")

    lines.append(h("Process Identity"))
    lines.append(
        f"PID={t['pid']} PPID={t['ppid']} State={t['state']} Elapsed={t['elapsed']} ({t['elapsed_sec']}s)"
    )
    lines.append(f"Cgroup  : {t['cgroup']}")
    lines.append(f"Mounts  : {', '.join(t['mounts']) if t['mounts'] else '-'}")
    lines.append(f"Ancestry: {t['ancestry']}")
    lines.append(f"Cmdline : {t['cmdline']}")
    lines.append("")

    lines.append(h("Resource Metrics"))
    oom_str = str(t["oom_score"]) if t["oom_score"] is not None else "-"
    lines.append(
        f"CPU={t['cpu_pct']:.1f}% | RSS={t['rss_mib']:.1f} MiB | Swap={t['swap_mib']:.1f} MiB | OOM={oom_str}"
    )
    lines.append(
        f"I/O: R={t['r_mbs']:.2f} MB/s | W={t['w_mbs']:.2f} MB/s | WChar={t['wchar_mbs']:.2f} MB/s"
    )
    lines.append("")

    lines.append(h("Threads"))
    lines.append(
        f"Total={th['total_threads']} | R={th['r_threads']} | D={th['d_threads']} | S={th['s_threads']}"
    )
    if th["d_wchans"]:
        lines.append("D-Wchans: " + ", ".join(f"{k}={val}" for k, val in th["d_wchans"].items()))
    if th["blocked_threads"]:
        lines.append(f"{'TID':>8} {'STATE':<5} {'COMM':<16} WCHAN")
        for b in th["blocked_threads"][:20]:
            lines.append(f"{b['tid']:>8} {b['state']:<5} {b['comm']:<16} {b['wchan']}")
    lines.append("")

    lines.append(h("Descendant Subtree"))
    lines.append(
        f"Descendants={sub['descendant_count']} | Subtree CPU={sub['subtree_cpu_pct']:.1f}% | "
        f"RSS={sub['subtree_rss_mib']:.1f} MiB | W={sub['subtree_w_mbs']:.2f} MB/s | "
        f"R={sub['subtree_r_mbs']:.2f} MB/s | D-threads={sub['subtree_d_threads']}"
    )
    if sub["top_descendants"]:
        lines.append(f"{'PID':>8} {'PPID':>8} {'ST':<3} {'%CPU':>6} {'RSS_M':>7} {'W_MB/s':>7} {'D':>3} {'COMM':<16} CMD")
        for d in sub["top_descendants"]:
            lines.append(
                f"{d['pid']:>8} {d['ppid']:>8} {d['state']:<3} {d['cpu_pct']:>6.1f} {d['rss_mib']:>7.1f} "
                f"{d['w_mbs']:>7.2f} {d['d_threads']:>3} {d['comm']:<16} {d['cmdline'][:72]}"
            )
    lines.append("")

    lines.append(h("Recommendations"))
    for rec in v["recommendations"]:
        lines.append(f"- {rec}")
    return "\n".join(lines)


def investigate_progress(
    interval: float = 2.0,
    top_n: int = 10,
    no_statvfs: bool = False,
) -> dict[str, Any]:
    """Run two consecutive windows and attach stall/progress comparison to the second report."""
    rep1 = investigate(interval=interval, top_n=top_n, no_statvfs=no_statvfs)
    rep2 = investigate(interval=interval, top_n=top_n, no_statvfs=no_statvfs)
    rep2["progress_check"] = compare_windows(rep1, rep2)
    return rep2


def render_text(rep: dict[str, Any], markdown: bool = False) -> str:
    v = rep["verdict"]
    c = rep["cpu_pct"]
    l = rep["load"]
    m = rep["memory"]
    psi = rep["psi"]
    h = (lambda s: f"## {s}") if markdown else (lambda s: f"=== {s} ===")

    lines: list[str] = []
    lines.append(
        f"{'# ' if markdown else ''}System Investigation @ {rep['timestamp']} (window={rep['sample_sec']}s)"
    )
    lines.append(f"Bottleneck  : {v['bottleneck']}")
    lines.append(f"Productivity: {v['productivity']}")
    for r in v["reasons"]:
        lines.append(f"  - {r}")
    lines.append("")

    lines.append(h("CPU / Load / PSI / Memory"))
    lines.append(
        f"CPU  : us={c['us']}% sy={c['sy']}% wa={c['wa']}% id={c['id']}% (ni={c['ni']}% hi={c['hi']}% si={c['si']}%)"
    )
    lines.append(
        f"Load : {l['load1']:.2f}, {l['load5']:.2f}, {l['load15']:.2f} ({l['cpus']} cores) | "
        f"threads R={l['r_threads']} D={l['d_threads']} | "
        f"proc turnover +{rep['turnover']['started']}/-{rep['turnover']['exited']} (total {rep['turnover']['total_procs']})"
    )
    if psi:
        psi_parts = []
        for k in ("cpu", "io", "memory"):
            if k in psi:
                s10 = psi[k].get("some", {}).get("avg10", 0.0)
                f10 = psi[k].get("full", {}).get("avg10", 0.0)
                psi_parts.append(f"{k}(some10={s10}%,full10={f10}%)")
        lines.append("PSI  : " + "  ".join(psi_parts))
    lines.append(
        f"Mem  : avail={m['mem_avail_gib']}/{m['mem_total_gib']} GiB | "
        f"Dirty={m['dirty_gib']} GiB ({m['dirty_mib']} MiB) Writeback={m['writeback_mib']} MiB | "
        f"Swap={m['swap_used_gib']}/{m['swap_total_gib']} GiB ({m['swap_used_pct']}%, "
        f"in={m['swap_in_pages']} out={m['swap_out_pages']} pg)"
    )
    if rep["gpus"]:
        lines.append("GPU  : " + "  ".join(f"{g['card']}={g['busy_pct']}%" for g in rep["gpus"]))
    lines.append("")

    if rep["disks"]:
        lines.append(h("Disk I/O"))
        lines.append(f"{'DEV':<10} {'UTIL%':>6} {'AQU':>7} {'W_MS':>8} {'R_MS':>7} {'W_MB/s':>8} {'R_MB/s':>8}  MOUNTS / MODEL")
        for d in rep["disks"]:
            mnts = ",".join(
                f"{x['mount']}({x['used_pct']}%)" if x["used_pct"] is not None else x["mount"]
                for x in d["mounts"]
            ) or "-"
            lines.append(
                f"{d['device']:<10} {d['util_pct']:>6.1f} {d['aqu_sz']:>7.2f} {d['w_await_ms']:>8.1f} "
                f"{d['r_await_ms']:>7.1f} {d['w_mbs']:>8.2f} {d['r_mbs']:>8.2f}  {mnts} {d['model']}"
            )
        lines.append("")

    nvme_rows = rep.get("nvme_health") or []
    tz_rows = rep.get("thermal_zones") or []
    if nvme_rows or tz_rows:
        lines.append(h("NVMe / Thermal Telemetry"))
        if nvme_rows:
            lines.append(f"{'CTRL':<8} {'STATE':<8} {'TEMP_C':>7} {'MAX_C':>6} {'CRIT_C':>7} {'ALARM':<5} {'STATUS':<9} MODEL")
            for n in nvme_rows:
                tc = f"{n['temp_c']:.1f}" if n.get("temp_c") is not None else "-"
                tm = f"{n['temp_max_c']:.1f}" if n.get("temp_max_c") is not None else "-"
                tcr = f"{n['temp_crit_c']:.1f}" if n.get("temp_crit_c") is not None else "-"
                alm = "YES" if n.get("alarm") else "no"
                lines.append(
                    f"{n['controller']:<8} {n['state']:<8} {tc:>7} {tm:>6} {tcr:>7} {alm:<5} {n['thermal_status']:<9} {n['model']}"
                )
        if tz_rows:
            tz_str = ", ".join(f"{z['type']}({z['zone']})={z['temp_c']:.1f}°C" for z in tz_rows[:12])
            lines.append(f"Thermal zones: {tz_str}")
        lines.append("")

    if rep["d_state_top"]:
        lines.append(h(f"Blocked (D-State) Threads (total={l['d_threads']})"))
        if rep["d_state_wchans"]:
            lines.append("Wchans: " + ", ".join(f"{k}={v}" for k, v in rep["d_state_wchans"].items()))
        lines.append(f"{'THRD':>5} {'PID':>8} {'ELAPSED':>10} {'COMM':<16} {'WCHAN':<24} {'MOUNTS':<14} {'CGROUP':<22} CMD")
        for g in rep["d_state_top"]:
            mnts = ",".join(g.get("mounts") or []) or "-"
            cg = _truncate_label(g.get("cgroup") or "-", 22)
            lines.append(
                f"{g['threads']:>5} {g['pid']:>8} {g['elapsed']:>10} {g['comm']:<16} {g['wchan']:<24} {mnts:<14} {cg:<22} {g['cmdline'][:72]}"
            )
        lines.append("")

    if rep["top_cpu"]:
        lines.append(h("Top CPU Consumers (instantaneous %)"))
        lines.append(f"{'PID':>8} {'%CPU':>6} {'RSS_M':>7} {'W_MB/s':>7} {'R_MB/s':>7} {'ELAPSED':>10} {'COMM':<16} {'CGROUP':<22} CMD")
        for p in rep["top_cpu"]:
            cg = _truncate_label(p.get("cgroup") or "-", 22)
            lines.append(
                f"{p['pid']:>8} {p['cpu_pct']:>6.1f} {p['rss_mib']:>7.1f} {p['w_mbs']:>7.2f} "
                f"{p['r_mbs']:>7.2f} {p['elapsed']:>10} {p['comm']:<16} {cg:<22} {p['cmdline'][:72]}"
            )
        lines.append("")

    if rep["top_io"]:
        lines.append(h("Top Process I/O (readable /proc/*/io)"))
        lines.append(f"{'PID':>8} {'W_MB/s':>8} {'R_MB/s':>8} {'WCH_M/s':>8} {'ELAPSED':>10} {'COMM':<16} {'MOUNTS':<14} {'CGROUP':<22} CMD")
        for p in rep["top_io"]:
            mnts = ",".join(p.get("mounts") or []) or "-"
            cg = _truncate_label(p.get("cgroup") or "-", 22)
            lines.append(
                f"{p['pid']:>8} {p['w_mbs']:>8.2f} {p['r_mbs']:>8.2f} {p['wchar_mbs']:>8.2f} "
                f"{p['elapsed']:>10} {p['comm']:<16} {mnts:<14} {cg:<22} {p['cmdline'][:64]}"
            )
        lines.append("")

    if rep["overlapping_workloads"]:
        lines.append(h("Overlapping / Concurrent Workloads"))
        for o in rep["overlapping_workloads"]:
            lines.append(
                f"- {o['group']} x{o['count']} (oldest={o['oldest_elapsed']}, pids={o['pids'][:6]})"
            )
            for t in o["sample_trees"][:2]:
                lines.append(f"    tree: {t}")
        lines.append("")

    if rep.get("progress_check"):
        pc = rep["progress_check"]
        lines.append(h("Progress Check (2 windows)"))
        lines.append(f"Status: {pc['status']}")
        lines.append(f"Detail: {pc['summary']}")
        if pc["persistent_d_pids"]:
            lines.append(f"Persistent D-state PIDs: {pc['persistent_d_pids'][:10]}")
        if pc["persistent_cpu_pids"]:
            lines.append(f"Persistent >=50% CPU PIDs: {pc['persistent_cpu_pids'][:10]}")
        lines.append("")

    lines.append(h("Recommendations"))
    for rec in v["recommendations"]:
        lines.append(f"- {rec}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    env_interval = os.environ.get("SYS_INVESTIGATE_INTERVAL", "2.0")
    env_top = os.environ.get("SYS_INVESTIGATE_TOP", "10")
    p = argparse.ArgumentParser(
        prog="sys-investigate",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--interval",
        type=float,
        default=float(env_interval) if env_interval else 2.0,
        help="Sampling window in seconds (0.2..60.0, env SYS_INVESTIGATE_INTERVAL, default: 2.0)",
    )
    p.add_argument(
        "--top",
        type=int,
        default=int(env_top) if env_top else 10,
        help="Max rows per section (1..100, env SYS_INVESTIGATE_TOP, default: 10)",
    )
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="Emit deterministic JSON report to stdout")
    fmt.add_argument("--markdown", action="store_true", help="Emit Markdown report to stdout")
    p.add_argument(
        "--watch",
        type=int,
        nargs="?",
        const=0,
        default=None,
        metavar="COUNT",
        help="Repeat investigation COUNT times (0 or omitted = run until Ctrl-C)",
    )
    p.add_argument(
        "--progress-check",
        action="store_true",
        help="Run two consecutive sampling windows to confirm whether load/stalls are progressing or wedged",
    )
    p.add_argument(
        "--no-statvfs",
        action="store_true",
        help="Skip os.statvfs() mount fill checks (extra protection when storage mounts are hung)",
    )
    p.add_argument(
        "--append-log",
        metavar="PATH",
        default=None,
        help="Append each report as a compact JSONL line to PATH (creates parent directories if needed)",
    )
    p.add_argument(
        "--diagnose-pid",
        type=int,
        default=None,
        metavar="PID",
        help="Run a targeted two-snapshot diagnosis of PID and its descendant process tree",
    )
    p.add_argument(
        "--exit-on-issue",
        action="store_true",
        help="Exit with code 3 when bottleneck is thrashing, wedged, or suspected spin loop",
    )
    p.add_argument("--verbose", action="store_true", help="Print effective configuration to stderr")
    return p


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL) if hasattr(signal, "SIGPIPE") else None
    parser = build_parser()
    args = parser.parse_args(argv)

    if not (0.2 <= args.interval <= 60.0):
        print("error: --interval must be between 0.2 and 60.0 seconds", file=sys.stderr)
        return 1
    if not (1 <= args.top <= 100):
        print("error: --top must be between 1 and 100", file=sys.stderr)
        return 1
    if args.watch is not None and args.watch < 0:
        print("error: --watch COUNT must be >= 0", file=sys.stderr)
        return 1
    if args.diagnose_pid is not None:
        if args.diagnose_pid <= 0:
            print("error: --diagnose-pid PID must be > 0", file=sys.stderr)
            return 1
        if not Path(f"/proc/{args.diagnose_pid}").is_dir():
            print(f"error: PID {args.diagnose_pid} does not exist in /proc", file=sys.stderr)
            return 1

    if args.verbose:
        print(
            f"config: interval={args.interval}s top={args.top} "
            f"format={'json' if args.json else 'markdown' if args.markdown else 'text'} "
            f"watch={args.watch} progress_check={args.progress_check} "
            f"diagnose_pid={args.diagnose_pid} "
            f"no_statvfs={args.no_statvfs} append_log={args.append_log} "
            f"exit_on_issue={args.exit_on_issue}",
            file=sys.stderr,
        )

    runs = 1 if args.watch is None else args.watch
    i = 0
    last_issue = False
    try:
        while runs == 0 or i < runs:
            if args.diagnose_pid is not None:
                rep = investigate_pid(
                    pid=args.diagnose_pid,
                    interval=args.interval,
                    top_n=args.top,
                    no_statvfs=args.no_statvfs,
                )
            elif args.progress_check:
                rep = investigate_progress(interval=args.interval, top_n=args.top, no_statvfs=args.no_statvfs)
            else:
                rep = investigate(interval=args.interval, top_n=args.top, no_statvfs=args.no_statvfs)
            last_issue = bool(rep["verdict"]["is_issue"])
            if args.append_log:
                append_jsonl_log(args.append_log, rep)
            if args.json:
                print(json.dumps(rep, indent=2, sort_keys=True), flush=True)
            else:
                if i > 0:
                    print("\n" + "=" * 78 + "\n", flush=True)
                if args.diagnose_pid is not None:
                    print(render_pid_text(rep, markdown=args.markdown), flush=True)
                else:
                    print(render_text(rep, markdown=args.markdown), flush=True)
            i += 1
    except KeyboardInterrupt:
        return 0
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.exit_on_issue and last_issue:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
