#!/usr/bin/env python3
"""Live smoke tests executing sys-investigate against real Linux /proc and /sys."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "src" / "sysadmin_tools" / "sys_investigate.py"


class TestCLISmoke(unittest.TestCase):
    def test_live_json_output(self) -> None:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--interval", "0.3", "--top", "5", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        data = json.loads(r.stdout)
        for key in ("timestamp", "verdict", "cpu_pct", "load", "memory", "disks", "top_cpu", "d_state_top"):
            self.assertIn(key, data)
        self.assertIn(
            data["verdict"]["bottleneck"],
            {
                "IO_WAIT_SATURATION",
                "MEMORY_SWAP_THRASH",
                "MEMORY_SWAP_AND_IO_THRASH",
                "MIXED_CPU_AND_IO",
                "CPU_COMPUTE_BOUND",
                "NORMAL_OR_IDLE",
            },
        )

    def test_live_markdown_output(self) -> None:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--interval", "0.3", "--top", "3", "--markdown"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("# System Investigation @", r.stdout)
        self.assertIn("## Recommendations", r.stdout)

    def test_live_no_statvfs_progress_check_and_append_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_file = Path(td) / "logs" / "telemetry.jsonl"
            r = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--interval",
                    "0.2",
                    "--top",
                    "5",
                    "--no-statvfs",
                    "--progress-check",
                    "--append-log",
                    str(log_file),
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            data = json.loads(r.stdout)
            self.assertIn("progress_check", data)
            self.assertIn(
                data["progress_check"]["status"],
                {"CONFIRMED_STALL", "PERSISTENT_THRASH", "RECOVERING", "WORSENING", "STEADY_PROGRESS", "IDLE"},
            )
            for disk in data["disks"]:
                for mnt in disk["mounts"]:
                    self.assertIsNone(mnt["used_pct"])
            for row in data["top_io"] + data["d_state_top"]:
                self.assertIn("mounts", row)
                self.assertIsInstance(row["mounts"], list)

            lines = log_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            logged = json.loads(lines[0])
            self.assertEqual(logged["timestamp"], data["timestamp"])
            self.assertEqual(logged["progress_check"]["status"], data["progress_check"]["status"])


if __name__ == "__main__":
    unittest.main()
