#!/usr/bin/env python3
"""Live smoke tests executing sys-crash-triage against the real Linux host."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "src" / "sysadmin_tools" / "sys_crash_triage.py"

VALID_STATUSES = {
    "KERNEL_PANIC_OR_OOPS",
    "HARDWARE_MCE_OR_THERMAL",
    "OOM_OR_LOCKUP_CRASH",
    "GPU_OR_STORAGE_FAULT_RESET",
    "UNCLEAN_HARD_RESET",
    "CLEAN_REBOOT",
    "INSUFFICIENT_DATA",
}


class TestCrashTriageSmoke(unittest.TestCase):
    def test_live_json_output_standalone(self) -> None:
        # Execute without repo on PYTHONPATH to verify standalone self-containment.
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--boots-history", "5", "--tail-lines", "50", "--json"],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        data = json.loads(r.stdout)
        for key in (
            "timestamp",
            "target_boot_index",
            "system",
            "verdict",
            "transition",
            "boots_history",
            "hardware_reset",
            "unclean_evidence",
            "nul_log_scans",
            "signatures",
            "crash_capture",
            "manual_steps",
        ):
            self.assertIn(key, data)
        self.assertIn(data["verdict"]["status"], VALID_STATUSES)
        self.assertTrue(len(data["verdict"]["recommendations"]) >= 1)
        if data["verdict"]["is_issue"]:
            self.assertNotIn("No immediate intervention needed.", data["verdict"]["recommendations"])
        # Verify deterministic sort_keys formatting matches re-serialization.
        self.assertEqual(r.stdout.strip(), json.dumps(data, indent=2, sort_keys=True))

    def test_live_no_journalctl_and_markdown_steps(self) -> None:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--no-journalctl", "--markdown", "--show-steps"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("# Crash & Reboot Triage @", r.stdout)
        self.assertIn("## Recommendations", r.stdout)
        self.assertIn("## Step-by-Step Manual Commands", r.stdout)

    def test_live_append_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_file = Path(td) / "logs" / "crash.jsonl"
            r = subprocess.run(
                [sys.executable, str(SCRIPT), "--no-journalctl", "--append-log", str(log_file)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            lines = log_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            row = json.loads(lines[0])
            self.assertIn(row["verdict"]["status"], VALID_STATUSES)


if __name__ == "__main__":
    unittest.main()
