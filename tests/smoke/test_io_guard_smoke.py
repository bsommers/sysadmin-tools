#!/usr/bin/env python3
"""Live smoke tests executing sys-io-guard against real Linux /proc/mounts and flock."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "src" / "sysadmin_tools" / "sys_io_guard.py"


class TestIOGuardSmoke(unittest.TestCase):
    def test_live_dry_run_json_against_real_proc_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_dir = Path(td) / "unused-locks"
            r = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--path",
                    str(REPO_ROOT),
                    "--lock-dir",
                    str(lock_dir),
                    "--dry-run",
                    "--json",
                    "--",
                    "echo",
                    "hello",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertFalse(lock_dir.exists())
            data = json.loads(r.stdout)
            for key in (
                "target_path",
                "mountpoint",
                "device",
                "disk",
                "lock_key",
                "lock_file",
                "meta_file",
                "env_overrides",
                "command",
            ):
                self.assertIn(key, data)
            self.assertTrue(data["lock_key"])
            self.assertEqual(data["command"], ["echo", "hello"])
            self.assertEqual(data["target_path"], os.path.realpath(str(REPO_ROOT)))

    def test_live_standalone_execution_and_status_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_dir = Path(td) / "locks"
            ready_file = Path(td) / "ready"
            release_file = Path(td) / "release"

            # Verify standalone invocation (no PYTHONPATH pointing at repo)
            clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            r_exec = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--path",
                    str(REPO_ROOT),
                    "--lock-dir",
                    str(lock_dir),
                    "--verbose",
                    "--",
                    sys.executable,
                    "-c",
                    "import os; print(os.environ.get('CARGO_BUILD_JOBS', ''))",
                ],
                capture_output=True,
                text=True,
                env=clean_env,
                check=False,
            )
            self.assertEqual(r_exec.returncode, 0, msg=r_exec.stderr)
            self.assertTrue(r_exec.stdout.strip().isdigit())
            self.assertIn("sys-io-guard: acquired", r_exec.stderr)

            # Hold lock live, verify --status sees active and contender exits 4
            hold_code = (
                "import pathlib, time; "
                f"pathlib.Path({str(ready_file)!r}).write_text('1'); "
                f"end = time.monotonic() + 10.0; "
                f"[time.sleep(0.02) for _ in iter(lambda: (not pathlib.Path({str(release_file)!r}).exists()) and time.monotonic() < end, False)]"
            )
            holder = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--path",
                    str(REPO_ROOT),
                    "--lock-dir",
                    str(lock_dir),
                    "--",
                    sys.executable,
                    "-c",
                    hold_code,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=clean_env,
            )
            try:
                deadline = time.monotonic() + 5.0
                while not ready_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready_file.exists(), "holder failed to start")

                r_status = subprocess.run(
                    [sys.executable, str(SCRIPT), "--lock-dir", str(lock_dir), "--status", "--json"],
                    capture_output=True,
                    text=True,
                    env=clean_env,
                    check=False,
                )
                self.assertEqual(r_status.returncode, 0, msg=r_status.stderr)
                status = json.loads(r_status.stdout)
                self.assertEqual(status["active_count"], 1)
                self.assertEqual(status["locks"][0]["holder"]["pid"], holder.pid)

                r_busy = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--path",
                        str(REPO_ROOT),
                        "--lock-dir",
                        str(lock_dir),
                        "--timeout",
                        "0",
                        "--",
                        sys.executable,
                        "-c",
                        "print('nope')",
                    ],
                    capture_output=True,
                    text=True,
                    env=clean_env,
                    check=False,
                )
                self.assertEqual(r_busy.returncode, 4)
                self.assertIn(f"pid={holder.pid}", r_busy.stderr)
            finally:
                release_file.write_text("1", encoding="utf-8")
                holder.communicate(timeout=5)
                self.assertEqual(holder.returncode, 0)

            r_after = subprocess.run(
                [sys.executable, str(SCRIPT), "--lock-dir", str(lock_dir), "--status", "--json"],
                capture_output=True,
                text=True,
                env=clean_env,
                check=False,
            )
            self.assertEqual(r_after.returncode, 0, msg=r_after.stderr)
            after = json.loads(r_after.stdout)
            self.assertEqual(after["active_count"], 0)
            self.assertEqual(after["idle_count"], 1)


if __name__ == "__main__":
    unittest.main()
