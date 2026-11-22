#!/usr/bin/env python3
"""Live smoke tests executing sys-investigate against real Linux /proc and /sys."""

from __future__ import annotations

import json
import subprocess
import sys
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


if __name__ == "__main__":
    unittest.main()
