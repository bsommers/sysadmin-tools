#!/usr/bin/env python3
"""Unit and concurrency tests for sysadmin_tools.sys_io_guard."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
SCRIPT = SRC_DIR / "sysadmin_tools" / "sys_io_guard.py"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sysadmin_tools import sys_io_guard as ig  # noqa: E402


SYNTHETIC_MOUNTS = "\n".join(
    [
        "rootfs / rootfs rw 0 0",
        "/dev/nvme0n1p2 / ext4 rw,relatime 0 0",
        "/dev/nvme2n1p1 /datanapps ext4 rw,relatime 0 0",
        "/dev/nvme1n1 /mnt/scratch xfs rw 0 0",
        "/dev/sda12 /mnt/archive ext4 rw 0 0",
        "/dev/vda2 /mnt/vm ext4 rw 0 0",
        "/dev/xvda1 /mnt/xen ext4 rw 0 0",
        "/dev/mmcblk0p2 /mnt/sd ext4 rw 0 0",
        "/dev/sdb /mnt/my\\040drive ext4 rw 0 0",
        "tmpfs /tmp tmpfs rw,nosuid,nodev 0 0",
        "overlay /var/lib/containers overlay rw 0 0",
        "10.0.0.5:/export/share /mnt/nfs nfs4 rw 0 0",
        "/dev/fuse /mnt/fuse_mount fuse.sshfs rw 0 0",
    ]
)


def _run_cli(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env is not None:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=run_env,
        check=False,
    )


def _wait_for_file(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


class TestDeviceAndLockResolution(unittest.TestCase):
    def test_parent_block_device(self) -> None:
        cases = {
            "nvme0n1": "nvme0n1",
            "nvme2n1p1": "nvme2n1",
            "nvme10n2p12": "nvme10n2",
            "sda": "sda",
            "sda1": "sda",
            "sda12": "sda",
            "vda2": "vda",
            "xvda1": "xvda",
            "hdb3": "hdb",
            "mmcblk0": "mmcblk0",
            "mmcblk0p2": "mmcblk0",
            "md0p1": "md0",
            "loop3p1": "loop3",
            "nbd0p2": "nbd0",
            "bcache0": "bcache0",
            "dm-0": "dm-0",
        }
        for raw, expected in cases.items():
            self.assertEqual(ig.parent_block_device(raw), expected, msg=raw)
        self.assertIsNone(ig.parent_block_device("fuse"))
        self.assertIsNone(ig.parent_block_device("null"))

    def test_dm_slave_sysfs_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            slaves = Path(td) / "dm-0" / "slaves"
            slaves.mkdir(parents=True)
            (slaves / "nvme0n1p3").touch()
            self.assertEqual(
                ig.parent_block_device("dm-0", allow_sysfs=True, sys_block_root=td),
                "nvme0n1",
            )
            # Multi-slave dm stays dm-0
            (slaves / "nvme1n1p3").touch()
            self.assertEqual(
                ig.parent_block_device("dm-0", allow_sysfs=True, sys_block_root=td),
                "dm-0",
            )

    def test_resolve_path_to_disk_physical_and_prefix_boundaries(self) -> None:
        r_data = ig.resolve_path_to_disk("/datanapps/cargo-target/debug", mounts_txt=SYNTHETIC_MOUNTS)
        self.assertEqual(r_data["mountpoint"], "/datanapps")
        self.assertEqual(r_data["device"], "/dev/nvme2n1p1")
        self.assertEqual(r_data["disk"], "nvme2n1")
        self.assertEqual(r_data["lock_key"], "nvme2n1")

        # Must NOT match /datanapps prefix for /datanapps2; must pick /dev/nvme0n1p2 over rootfs.
        r_boundary = ig.resolve_path_to_disk("/datanapps2/work", mounts_txt=SYNTHETIC_MOUNTS)
        self.assertEqual(r_boundary["mountpoint"], "/")
        self.assertEqual(r_boundary["disk"], "nvme0n1")
        self.assertEqual(r_boundary["lock_key"], "nvme0n1")

        self.assertEqual(ig.resolve_path_to_disk("/mnt/scratch/build", mounts_txt=SYNTHETIC_MOUNTS)["disk"], "nvme1n1")
        self.assertEqual(ig.resolve_path_to_disk("/mnt/archive/a", mounts_txt=SYNTHETIC_MOUNTS)["disk"], "sda")
        self.assertEqual(ig.resolve_path_to_disk("/mnt/vm/a", mounts_txt=SYNTHETIC_MOUNTS)["disk"], "vda")
        self.assertEqual(ig.resolve_path_to_disk("/mnt/xen/a", mounts_txt=SYNTHETIC_MOUNTS)["disk"], "xvda")
        self.assertEqual(ig.resolve_path_to_disk("/mnt/sd/a", mounts_txt=SYNTHETIC_MOUNTS)["disk"], "mmcblk0")

        # Escaped space in mountpoint
        r_space = ig.resolve_path_to_disk("/mnt/my drive/proj", mounts_txt=SYNTHETIC_MOUNTS)
        self.assertEqual(r_space["mountpoint"], "/mnt/my drive")
        self.assertEqual(r_space["disk"], "sdb")

    def test_resolve_path_to_disk_virtual_fallback(self) -> None:
        for path, expected_mnt in (
            ("/tmp/build", "/tmp"),
            ("/var/lib/containers/layer", "/var/lib/containers"),
            ("/mnt/nfs/repo", "/mnt/nfs"),
            ("/mnt/fuse_mount/repo", "/mnt/fuse_mount"),
        ):
            res = ig.resolve_path_to_disk(path, mounts_txt=SYNTHETIC_MOUNTS)
            self.assertIsNone(res["disk"], msg=path)
            self.assertEqual(res["mountpoint"], expected_mnt, msg=path)
            expected_key = f"mnt-{hashlib.sha1(expected_mnt.encode('utf-8')).hexdigest()[:8]}"
            self.assertEqual(res["lock_key"], expected_key, msg=path)

    def test_sanitize_lock_key(self) -> None:
        self.assertEqual(ig.sanitize_lock_key("nvme2n1"), "nvme2n1")
        self.assertEqual(ig.sanitize_lock_key("build.key-1_ok"), "build.key-1_ok")
        self.assertEqual(ig.sanitize_lock_key("../evil/path key!"), "_evil_path_key_")
        self.assertEqual(ig.sanitize_lock_key("...hidden.key"), "hidden.key")
        self.assertEqual(len(ig.sanitize_lock_key("a" * 200)), 128)
        for bad in ("", "   ", ".", "..", "///", "---", "___"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ig.sanitize_lock_key(bad)

    def test_resolve_lock_dir_precedence(self) -> None:
        env = {"SYS_IO_GUARD_LOCK_DIR": "/var/tmp/custom", "XDG_RUNTIME_DIR": "/run/user/1000"}
        self.assertEqual(ig.resolve_lock_dir("/tmp/cli-dir", env=env, uid=1000), Path("/tmp/cli-dir"))
        self.assertEqual(ig.resolve_lock_dir(None, env=env, uid=1000), Path("/var/tmp/custom"))
        self.assertEqual(
            ig.resolve_lock_dir(None, env={"XDG_RUNTIME_DIR": "/run/user/1000"}, uid=1000),
            Path("/run/user/1000/sys-io-guard"),
        )
        self.assertEqual(ig.resolve_lock_dir(None, env={}, uid=1234), Path("/tmp/sys-io-guard-1234"))

    def test_default_cargo_jobs_and_env(self) -> None:
        with mock.patch("os.cpu_count", return_value=None):
            self.assertEqual(ig.default_cargo_jobs(None), 2)
        self.assertEqual(ig.default_cargo_jobs(4), 2)
        self.assertEqual(ig.default_cargo_jobs(8), 2)
        self.assertEqual(ig.default_cargo_jobs(16), 4)
        self.assertEqual(ig.default_cargo_jobs(32), 8)
        self.assertEqual(ig.default_cargo_jobs(64), 8)
        self.assertEqual(ig._resolve_cargo_jobs(None, env={"SYS_IO_GUARD_CARGO_JOBS": "5"}), 5)
        self.assertEqual(ig._resolve_cargo_jobs(0, env={"SYS_IO_GUARD_CARGO_JOBS": "5"}), 0)
        with self.assertRaises(ValueError):
            ig._resolve_cargo_jobs(None, env={"SYS_IO_GUARD_CARGO_JOBS": "nope"})
        with self.assertRaises(ValueError):
            ig._resolve_cargo_jobs(-1, env={})


class TestFlockContentionAndCLI(unittest.TestCase):
    def test_concurrent_flock_timeout_holder_meta_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_dir = Path(td) / "locks"
            ready_file = Path(td) / "holder.ready"
            release_file = Path(td) / "holder.release"

            holder_script = (
                "import pathlib, time; "
                f"pathlib.Path({str(ready_file)!r}).write_text('ready'); "
                f"end = time.monotonic() + 10.0; "
                f"[time.sleep(0.02) for _ in iter(lambda: (not pathlib.Path({str(release_file)!r}).exists()) and time.monotonic() < end, False)]"
            )
            holder = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--lock-dir",
                    str(lock_dir),
                    "--lock-key",
                    "disk-test",
                    "--nice",
                    "0",
                    "--",
                    sys.executable,
                    "-c",
                    holder_script,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertTrue(_wait_for_file(ready_file), "holder did not acquire lock in time")

                meta_path = lock_dir / "disk-test.meta"
                self.assertTrue(meta_path.exists())
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                for field in ("pid", "ppid", "cmd", "lock_key", "target_path", "started_at"):
                    self.assertIn(field, meta)
                self.assertEqual(meta["pid"], holder.pid)
                self.assertEqual(meta["lock_key"], "disk-test")

                # Non-blocking contender fails immediately with exit 4 + holder info on stderr
                r_nb = _run_cli(
                    [
                        "--lock-dir",
                        str(lock_dir),
                        "--lock-key",
                        "disk-test",
                        "--timeout",
                        "0",
                        "--",
                        sys.executable,
                        "-c",
                        "print('should-not-run')",
                    ]
                )
                self.assertEqual(r_nb.returncode, 4)
                self.assertEqual(r_nb.stdout, "")
                self.assertIn("disk-test", r_nb.stderr)
                self.assertIn(f"pid={holder.pid}", r_nb.stderr)

                # Timed contender waits then exits 4
                t0 = time.monotonic()
                r_to = _run_cli(
                    [
                        "--lock-dir",
                        str(lock_dir),
                        "--lock-key",
                        "disk-test",
                        "--timeout",
                        "0.15",
                        "--",
                        sys.executable,
                        "-c",
                        "print('should-not-run')",
                    ]
                )
                elapsed = time.monotonic() - t0
                self.assertEqual(r_to.returncode, 4)
                self.assertGreaterEqual(elapsed, 0.12)
                self.assertIn(f"pid={holder.pid}", r_to.stderr)

                # --status --json reports active lock with holder metadata
                r_st = _run_cli(["--lock-dir", str(lock_dir), "--status", "--json"])
                self.assertEqual(r_st.returncode, 0, msg=r_st.stderr)
                st = json.loads(r_st.stdout)
                self.assertEqual(st["active_count"], 1)
                self.assertEqual(st["idle_count"], 0)
                self.assertEqual(len(st["locks"]), 1)
                self.assertTrue(st["locks"][0]["active"])
                self.assertEqual(st["locks"][0]["state"], "active")
                self.assertEqual(st["locks"][0]["holder"]["pid"], holder.pid)

                # Start waiter that will succeed once holder releases
                waiter = subprocess.Popen(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--lock-dir",
                        str(lock_dir),
                        "--lock-key",
                        "disk-test",
                        "--timeout",
                        "5",
                        "--verbose",
                        "--",
                        sys.executable,
                        "-c",
                        "print('waiter-ok')",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                time.sleep(0.08)
                release_file.write_text("go", encoding="utf-8")
                h_out, h_err = holder.communicate(timeout=5)
                self.assertEqual(holder.returncode, 0, msg=h_err)
                self.assertEqual(h_out, "")

                w_out, w_err = waiter.communicate(timeout=5)
                self.assertEqual(waiter.returncode, 0, msg=w_err)
                self.assertEqual(w_out.strip(), "waiter-ok")
                self.assertIn("acquired lock_key=disk-test", w_err)
                self.assertIn("wait=", w_err)
            finally:
                if holder.poll() is None:
                    holder.kill()
                    holder.wait(timeout=5)

            # After both exit, .meta is removed and --status shows idle
            self.assertFalse((lock_dir / "disk-test.meta").exists())
            r_idle = _run_cli(["--lock-dir", str(lock_dir), "--status", "--json"])
            self.assertEqual(r_idle.returncode, 0, msg=r_idle.stderr)
            st_idle = json.loads(r_idle.stdout)
            self.assertEqual(st_idle["active_count"], 0)
            self.assertEqual(st_idle["idle_count"], 1)
            self.assertFalse(st_idle["locks"][0]["active"])
            self.assertIsNone(st_idle["locks"][0]["holder"])
            self.assertFalse(st_idle["locks"][0]["stale_meta"])

    def test_status_missing_dir_and_stale_meta(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "does-not-exist"
            r = _run_cli(["--lock-dir", str(missing), "--status", "--json"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            data = json.loads(r.stdout)
            self.assertEqual(data["active_count"], 0)
            self.assertEqual(data["idle_count"], 0)
            self.assertEqual(data["locks"], [])

            # Simulate stale .meta left after SIGKILL
            lock_dir = Path(td) / "stale"
            lock_dir.mkdir()
            (lock_dir / "nvme2n1.lock").touch()
            (lock_dir / "nvme2n1.meta").write_text(json.dumps({"pid": 999999}), encoding="utf-8")
            r2 = _run_cli(["--lock-dir", str(lock_dir), "--status", "--json"])
            self.assertEqual(r2.returncode, 0, msg=r2.stderr)
            d2 = json.loads(r2.stdout)
            self.assertEqual(d2["idle_count"], 1)
            self.assertTrue(d2["locks"][0]["stale_meta"])
            self.assertIsNone(d2["locks"][0]["holder"])

            # Human-readable status text
            r_txt = _run_cli(["--lock-dir", str(lock_dir), "--status"])
            self.assertEqual(r_txt.returncode, 0)
            self.assertIn("[IDLE]", r_txt.stdout)
            self.assertIn("stale .meta present", r_txt.stdout)

    def test_dry_run_creates_nothing_and_is_deterministic_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_dir = Path(td) / "never-created"
            r = _run_cli(
                [
                    "--lock-dir",
                    str(lock_dir),
                    "--lock-key",
                    "nvme2n1",
                    "--cargo-jobs",
                    "6",
                    "--nice",
                    "12",
                    "--env",
                    "FOO=bar=baz",
                    "--dry-run",
                    "--json",
                    "--",
                    "cargo",
                    "test",
                    "--release",
                ]
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertFalse(lock_dir.exists())
            data = json.loads(r.stdout)
            self.assertEqual(data["lock_key"], "nvme2n1")
            self.assertEqual(data["lock_key_source"], "override")
            self.assertEqual(data["env_overrides"], {"CARGO_BUILD_JOBS": "6", "FOO": "bar=baz"})
            self.assertEqual(data["nice"], 12)
            self.assertEqual(data["command"], ["cargo", "test", "--release"])
            # Deterministic key sorting check
            self.assertEqual(r.stdout.strip(), json.dumps(data, indent=2, sort_keys=True))

            # Text output works too
            r_txt = _run_cli(["--lock-dir", str(lock_dir), "--dry-run", "--", "echo", "hi"])
            self.assertEqual(r_txt.returncode, 0)
            self.assertIn("lock_file:", r_txt.stdout)
            self.assertIn("command:", r_txt.stdout)
            self.assertFalse(lock_dir.exists())

    def test_cargo_jobs_env_nice_and_exit_code_propagation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_dir = Path(td) / "locks"
            # --cargo-jobs 4 sets CARGO_BUILD_JOBS=4; --env adds custom var; --nice 5 raises niceness
            probe_code = (
                "import json, os; "
                "print(json.dumps({'jobs': os.environ.get('CARGO_BUILD_JOBS'), "
                "'extra': os.environ.get('EXTRA_VAR'), 'nice': os.nice(0)}))"
            )
            base_nice = os.nice(0)
            r = _run_cli(
                [
                    "--lock-dir",
                    str(lock_dir),
                    "--lock-key",
                    "envtest",
                    "--cargo-jobs",
                    "4",
                    "--nice",
                    "5",
                    "--env",
                    "EXTRA_VAR=a=b",
                    "--",
                    sys.executable,
                    "-c",
                    probe_code,
                ]
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            res = json.loads(r.stdout)
            self.assertEqual(res["jobs"], "4")
            self.assertEqual(res["extra"], "a=b")
            self.assertEqual(res["nice"], min(19, base_nice + 5))

            # --cargo-jobs 0 does NOT set CARGO_BUILD_JOBS
            clean_env = {k: v for k, v in os.environ.items() if k != "CARGO_BUILD_JOBS"}
            r0 = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--lock-dir",
                    str(lock_dir),
                    "--lock-key",
                    "envtest",
                    "--cargo-jobs",
                    "0",
                    "--nice",
                    "0",
                    "--",
                    sys.executable,
                    "-c",
                    probe_code,
                ],
                capture_output=True,
                text=True,
                env=clean_env,
                check=False,
            )
            self.assertEqual(r0.returncode, 0, msg=r0.stderr)
            res0 = json.loads(r0.stdout)
            self.assertIsNone(res0["jobs"])
            self.assertEqual(res0["nice"], base_nice)

            # Propagates non-zero child exit code
            r_rc = _run_cli(
                [
                    "--lock-dir",
                    str(lock_dir),
                    "--lock-key",
                    "envtest",
                    "--nice",
                    "0",
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(7)",
                ]
            )
            self.assertEqual(r_rc.returncode, 7)

    def test_default_path_uses_cargo_target_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            r = _run_cli(["--dry-run", "--json"], env={"CARGO_TARGET_DIR": td})
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            data = json.loads(r.stdout)
            self.assertEqual(data["target_path"], os.path.realpath(td))

    def test_cli_validation_and_error_paths(self) -> None:
        bad_cases = [
            ["--timeout", "-1", "--", "true"],
            ["--timeout", "nan", "--", "true"],
            ["--timeout", "inf", "--", "true"],
            ["--cargo-jobs", "-2", "--", "true"],
            ["--nice", "-1", "--", "true"],
            ["--nice", "20", "--", "true"],
            ["--env", "MISSING_EQUALS", "--", "true"],
            ["--env", "=empty_key", "--", "true"],
            ["--lock-key", "..", "--", "true"],
            ["--json", "--", "true"],
            [],
        ]
        for argv in bad_cases:
            r = _run_cli(argv)
            self.assertEqual(r.returncode, 1, msg=f"argv={argv} stderr={r.stderr}")
            self.assertNotIn("Traceback", r.stderr)
            self.assertEqual(len(r.stderr.strip().splitlines()), 1, msg=f"argv={argv} stderr={r.stderr}")

        # Bad SYS_IO_GUARD_CARGO_JOBS env var -> exit 1, no traceback
        r_env = _run_cli(["--dry-run"], env={"SYS_IO_GUARD_CARGO_JOBS": "not_an_int"})
        self.assertEqual(r_env.returncode, 1)
        self.assertNotIn("Traceback", r_env.stderr)

        # Missing executable -> exit 1, no traceback
        with tempfile.TemporaryDirectory() as td:
            r_nf = _run_cli(["--lock-dir", td, "--lock-key", "nf", "--", "/no/such/binary-xyz-999"])
            self.assertEqual(r_nf.returncode, 1)
            self.assertNotIn("Traceback", r_nf.stderr)

        # Argparse errors -> exit 2
        self.assertEqual(_run_cli(["--unknown-flag"]).returncode, 2)
        self.assertEqual(_run_cli(["--status", "--dry-run"]).returncode, 2)


if __name__ == "__main__":
    unittest.main()
