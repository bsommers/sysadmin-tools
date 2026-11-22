#!/usr/bin/env python3
"""Live smoke tests executing sys-mem-watch against real Linux /proc."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "src" / "sysadmin_tools" / "sys_mem_watch.py"

VALID_STATUSES = {
    "OOM_IMMINENT",
    "SWAP_THRASHING",
    "RAPID_GROWTH_LEAK_RISK",
    "HIGH_MEMORY_PRESSURE",
    "HEALTHY",
}

REQUIRED_MEMORY_KEYS = (
    "mem_total_gib",
    "mem_avail_gib",
    "mem_avail_pct",
    "mem_free_gib",
    "cached_gib",
    "dirty_gib",
    "slab_reclaimable_gib",
    "slab_unreclaimable_gib",
    "swap_total_gib",
    "swap_used_gib",
    "swap_used_pct",
    "swap_in_pages",
    "swap_out_pages",
    "pgscan_direct_delta",
    "pgscan_kswapd_delta",
    "oom_kill_delta",
)

REQUIRED_PROC_KEYS = (
    "pid",
    "ppid",
    "comm",
    "rss_mib",
    "rss_delta_mib",
    "rss_rate_mibs",
    "anon_mib",
    "file_mib",
    "shmem_mib",
    "swap_mib",
    "swap_delta_mib",
    "oom_score",
    "oom_score_adj",
    "threads",
    "elapsed_sec",
    "elapsed",
    "cgroup",
    "cmdline",
)


class TestMemWatchSmoke(unittest.TestCase):
    def test_live_json_output_standalone(self) -> None:
        # Execute without repo on PYTHONPATH to verify standalone self-containment.
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--interval", "0.25", "--top", "5", "--json"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        data = json.loads(r.stdout)
        for key in ("timestamp", "sample_sec", "verdict", "memory", "psi", "top_rss", "top_growers", "top_swap", "top_oom_risk"):
            self.assertIn(key, data)
        self.assertIn(data["verdict"]["status"], VALID_STATUSES)
        self.assertEqual(data["verdict"]["is_issue"], data["verdict"]["status"] != "HEALTHY")
        for mkey in REQUIRED_MEMORY_KEYS:
            self.assertIn(mkey, data["memory"])
        self.assertTrue(len(data["top_rss"]) > 0)
        self.assertLessEqual(len(data["top_rss"]), 5)
        for pkey in REQUIRED_PROC_KEYS:
            self.assertIn(pkey, data["top_rss"][0])
        # Verify deterministic sort_keys formatting matches re-serialization.
        self.assertEqual(r.stdout.strip(), json.dumps(data, indent=2, sort_keys=True))

    def test_live_markdown_output(self) -> None:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--interval", "0.25", "--top", "3", "--markdown"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("# Memory Investigation @", r.stdout)
        self.assertIn("## System Memory / Swap / Reclaim / PSI", r.stdout)
        self.assertIn("## Recommendations", r.stdout)

    def test_live_append_log_and_watch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_file = Path(td) / "logs" / "mem_telemetry.jsonl"
            r = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--interval",
                    "0.2",
                    "--top",
                    "4",
                    "--watch",
                    "2",
                    "--append-log",
                    str(log_file),
                    "--verbose",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("config:", r.stderr)
            lines = log_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            for line in lines:
                rec = json.loads(line)
                self.assertIn(rec["verdict"]["status"], VALID_STATUSES)
                self.assertEqual(line, json.dumps(rec, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    unittest.main()
