#!/usr/bin/env python3
"""Live smoke tests executing sys-investigate against real Linux /proc and /sys."""

from __future__ import annotations

import json
import os
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
        for key in (
            "timestamp",
            "verdict",
            "cpu_pct",
            "load",
            "memory",
            "disks",
            "nvme_health",
            "thermal_zones",
            "top_cpu",
            "d_state_top",
        ):
            self.assertIn(key, data)
        self.assertIsInstance(data["nvme_health"], list)
        self.assertIsInstance(data["thermal_zones"], list)
        for nvme in data["nvme_health"]:
            for k in ("controller", "model", "state", "temp_c", "temp_max_c", "temp_crit_c", "alarm", "thermal_status", "sensors"):
                self.assertIn(k, nvme)
            self.assertIn(nvme["thermal_status"], {"OK", "WARM", "CRITICAL"})
        for row in data["top_cpu"] + data["top_io"] + data["d_state_top"]:
            self.assertIn("cgroup", row)
            self.assertIsInstance(row["cgroup"], str)
            self.assertTrue(len(row["cgroup"]) >= 1)
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

    def test_live_diagnose_pid_json_and_markdown(self) -> None:
        target_pid = os.getpid()
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--diagnose-pid", str(target_pid), "--interval", "0.2", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["mode"], "pid")
        self.assertEqual(data["target"]["pid"], target_pid)
        for k in (
            "pid",
            "ppid",
            "comm",
            "state",
            "elapsed_sec",
            "elapsed",
            "cmdline",
            "ancestry",
            "cgroup",
            "mounts",
            "cpu_pct",
            "rss_mib",
            "swap_mib",
            "r_mbs",
            "w_mbs",
            "wchar_mbs",
            "oom_score",
        ):
            self.assertIn(k, data["target"])
        for k in ("total_threads", "r_threads", "d_threads", "s_threads", "d_wchans", "blocked_threads"):
            self.assertIn(k, data["threads"])
        self.assertGreaterEqual(data["threads"]["total_threads"], 1)
        for k in (
            "descendant_count",
            "subtree_cpu_pct",
            "subtree_rss_mib",
            "subtree_w_mbs",
            "subtree_r_mbs",
            "subtree_d_threads",
            "top_descendants",
        ):
            self.assertIn(k, data["subtree"])
        self.assertIn(
            data["verdict"]["status"],
            {"BLOCKED_ON_IO", "CPU_BOUND", "IO_ACTIVE", "MEMORY_HEAVY", "IDLE_OR_SLEEPING"},
        )

        with tempfile.TemporaryDirectory() as td:
            log_file = Path(td) / "pid.jsonl"
            r_md = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--diagnose-pid",
                    str(target_pid),
                    "--interval",
                    "0.2",
                    "--no-statvfs",
                    "--markdown",
                    "--append-log",
                    str(log_file),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(r_md.returncode, 0, msg=r_md.stderr)
            self.assertIn(f"# PID Investigation: {target_pid}", r_md.stdout)
            self.assertIn("## Recommendations", r_md.stdout)
            lines = log_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["target"]["pid"], target_pid)


if __name__ == "__main__":
    unittest.main()
