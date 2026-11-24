#!/usr/bin/env python3
"""Live smoke tests executing sys-net-watch against real Linux /proc/net/*."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "src" / "sysadmin_tools" / "sys_net_watch.py"

VALID_STATUSES = {
    "HEALTHY",
    "LINK_ERRORS",
    "PACKET_DROPS",
    "ELEVATED_RETRANSMITS",
    "SOCKET_PRESSURE",
}

REQUIRED_TOP_KEYS = (
    "timestamp",
    "version",
    "sample_sec",
    "samples",
    "iface_filter",
    "verdict",
    "interfaces",
    "interfaces_summary",
    "sockets",
    "snmp",
    "softnet",
)


class TestNetWatchSmoke(unittest.TestCase):
    def test_live_json_output_standalone(self) -> None:
        # Execute without repo on PYTHONPATH to verify standalone self-containment.
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--interval", "0.15", "--samples", "2", "--top", "5", "--json"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        data = json.loads(r.stdout)
        for key in REQUIRED_TOP_KEYS:
            self.assertIn(key, data)
        self.assertEqual(data["version"], "0.5.0")
        self.assertEqual(data["samples"], 2)
        self.assertGreater(data["sample_sec"], 0.0)
        self.assertIn(data["verdict"]["status"], VALID_STATUSES)
        self.assertEqual(data["verdict"]["is_issue"], data["verdict"]["status"] != "HEALTHY")
        if data["verdict"]["is_issue"]:
            for rec in data["verdict"]["recommendations"]:
                self.assertNotIn("No immediate intervention needed.", rec)
        self.assertGreater(len(data["interfaces"]), 0)
        self.assertLessEqual(len(data["interfaces"]), 5)
        self.assertIn("ESTABLISHED", data["sockets"]["tcp"]["states"])
        self.assertIn("retrans_rate_pct", data["snmp"]["tcp"])
        self.assertIn("dropped_delta", data["softnet"])
        # Verify deterministic sort_keys formatting matches re-serialization.
        self.assertEqual(r.stdout.strip(), json.dumps(data, indent=2, sort_keys=True))

    def test_live_single_sample_and_iface_filter(self) -> None:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--samples", "1", "--iface", "lo", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["samples"], 1)
        self.assertEqual(data["sample_sec"], 0.0)
        self.assertEqual(data["iface_filter"], ["lo"])
        self.assertEqual([i["iface"] for i in data["interfaces"]], ["lo"])

    def test_live_markdown_and_verbose(self) -> None:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--interval", "0.15", "--samples", "2", "--top", "4", "--markdown", "--verbose"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("config:", r.stderr)
        self.assertIn("# Network Investigation @", r.stdout)
        self.assertIn("## Interfaces (/proc/net/dev)", r.stdout)
        self.assertIn("## Sockets (/proc/net/{tcp,tcp6,udp,udp6})", r.stdout)
        self.assertIn("## SNMP Deltas (/proc/net/snmp)", r.stdout)
        self.assertIn("## Softnet Deltas (/proc/net/softnet_stat)", r.stdout)
        self.assertIn("## Recommendations", r.stdout)

    def test_live_error_paths(self) -> None:
        r_missing = subprocess.run(
            [sys.executable, str(SCRIPT), "--samples", "1", "--iface", "__nonexistent_iface_999__"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r_missing.returncode, 1)
        self.assertNotIn("Traceback", r_missing.stderr)
        self.assertEqual(len(r_missing.stderr.strip().splitlines()), 1)

        r_ver = subprocess.run(
            [sys.executable, str(SCRIPT), "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r_ver.returncode, 0)
        self.assertEqual(r_ver.stdout.strip(), "sys-net-watch 0.5.0")


if __name__ == "__main__":
    unittest.main()
