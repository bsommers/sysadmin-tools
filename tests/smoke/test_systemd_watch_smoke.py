#!/usr/bin/env python3
"""Live smoke tests executing sys-systemd-watch against real Linux /sys/fs/cgroup and /proc."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "src" / "sysadmin_tools" / "sys_systemd_watch.py"

VALID_STATUSES = {
    "HEALTHY",
    "MEMORY_PRESSURE",
    "CPU_THROTTLING",
    "IO_PRESSURE",
    "CRASH_LOOP_SUSPECTED",
    "JOURNAL_BURST",
}


class TestSystemdWatchSmoke(unittest.TestCase):
    def test_live_json_output_standalone(self) -> None:
        # Execute without repo on PYTHONPATH to verify standalone self-containment.
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--interval", "0.2", "--samples", "2", "--top", "5", "--json"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        data = json.loads(r.stdout)
        for key in (
            "version",
            "timestamp",
            "sample_sec",
            "samples",
            "cgroup_root",
            "verdict",
            "summary",
            "journal",
            "problem_units",
            "top_memory",
            "top_cpu",
            "top_io",
            "top_slices",
        ):
            self.assertIn(key, data)
        self.assertEqual(data["version"], "0.5.0")
        self.assertEqual(data["samples"], 2)
        self.assertIn(data["verdict"]["status"], VALID_STATUSES)
        self.assertEqual(data["verdict"]["is_issue"], data["verdict"]["status"] != "HEALTHY")
        if data["verdict"]["is_issue"]:
            self.assertNotIn("No immediate intervention needed.", data["verdict"]["recommendations"])
        self.assertGreater(data["summary"]["units_total"], 0)
        self.assertLessEqual(len(data["top_memory"]), 5)
        self.assertLessEqual(len(data["top_cpu"]), 5)
        # Verify deterministic sort_keys formatting matches re-serialization.
        self.assertEqual(r.stdout.strip(), json.dumps(data, indent=2, sort_keys=True))

    def test_live_single_sample_unit_filter_and_markdown(self) -> None:
        r = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--samples",
                "1",
                "--unit",
                "*.slice",
                "--top",
                "3",
                "--markdown",
                "--verbose",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("config:", r.stderr)
        self.assertIn("# Systemd Cgroup Watch @", r.stdout)
        self.assertIn("## Summary & Journal", r.stdout)
        self.assertIn("## Recommendations", r.stdout)

    def test_live_version(self) -> None:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("0.5.0", r.stdout)


if __name__ == "__main__":
    unittest.main()
