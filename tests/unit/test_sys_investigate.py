#!/usr/bin/env python3
"""Unit and CLI error-path tests for sysadmin_tools.sys_investigate."""

from __future__ import annotations

import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
SCRIPT = SRC_DIR / "sysadmin_tools" / "sys_investigate.py"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sysadmin_tools import sys_investigate as si  # noqa: E402


class TestParsersAndHelpers(unittest.TestCase):
    def test_is_whole_physical_disk(self) -> None:
        self.assertTrue(si.is_whole_physical_disk("nvme0n1"))
        self.assertTrue(si.is_whole_physical_disk("nvme2n1"))
        self.assertFalse(si.is_whole_physical_disk("nvme2n1p1"))
        self.assertTrue(si.is_whole_physical_disk("sda"))
        self.assertFalse(si.is_whole_physical_disk("sda1"))
        self.assertFalse(si.is_whole_physical_disk("loop0"))
        self.assertFalse(si.is_whole_physical_disk("dm-0"))
        self.assertTrue(si.is_whole_physical_disk("mmcblk0"))
        self.assertFalse(si.is_whole_physical_disk("mmcblk0p1"))

    def test_parse_proc_stat_handles_spaces_and_parens_in_comm(self) -> None:
        # 20 fields after ') '
        fields = ["D", "2"] + ["0"] * 9 + ["100", "50"] + ["0"] * 6 + ["9999"]
        line = f"872954 (kworker/u129:15 (flush)) {' '.join(fields)}"
        parsed = si.parse_proc_stat(line)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        comm, state, ppid, utime, stime, starttime = parsed
        self.assertEqual(comm, "kworker/u129:15 (flush)")
        self.assertEqual(state, "D")
        self.assertEqual(ppid, 2)
        self.assertEqual(utime, 100)
        self.assertEqual(stime, 50)
        self.assertEqual(starttime, 9999)


class TestClassifier(unittest.TestCase):
    def test_io_thrashing_verdict(self) -> None:
        verdict = si.classify_verdict(
            cpu={"us": 4.0, "sy": 2.0, "ni": 0.0, "id": 1.0, "wa": 93.0, "hi": 0.0, "si": 0.0, "st": 0.0},
            load={"load1": 140.0, "load5": 110.0, "load15": 95.0, "cpus": 32},
            psi={"cpu": {"some": {"avg10": 0.4}}, "io": {"some": {"avg10": 98.0}, "full": {"avg10": 88.0}}, "memory": {"some": {"avg10": 0.0}}},
            mem={"dirty_gib": 13.2, "mem_avail_gib": 75.0, "swap_used_pct": 99.0},
            swap_delta=(0, 0),
            disks=[
                {
                    "device": "nvme2n1",
                    "mounts": [{"mount": "/datanapps", "used_pct": 34.0}],
                    "util_pct": 99.2,
                    "aqu_sz": 515.0,
                    "w_await_ms": 1312.0,
                    "r_await_ms": 80.0,
                    "w_mbs": 48.0,
                    "r_mbs": 1.2,
                }
            ],
            d_state_total=260,
            d_wchan_counts=Counter({"balance_dirty_pages": 255}),
            top_cpu=[],
            overlaps=[{"group": "script:verify.sh", "count": 3}, {"group": "tool:cargo", "count": 3}],
            turnover={"started": 4, "exited": 3, "total_procs": 1500},
        )
        self.assertEqual(verdict["bottleneck"], "IO_WAIT_SATURATION")
        self.assertEqual(verdict["productivity"], "THRASHING_LOW_EFFICIENCY")
        self.assertTrue(verdict["is_issue"])
        self.assertTrue(any("CARGO_BUILD_JOBS" in r for r in verdict["recommendations"]))

    def test_healthy_idle_verdict(self) -> None:
        verdict = si.classify_verdict(
            cpu={"us": 3.0, "sy": 1.0, "ni": 0.0, "id": 95.5, "wa": 0.5, "hi": 0.0, "si": 0.0, "st": 0.0},
            load={"load1": 0.8, "load5": 0.6, "load15": 0.5, "cpus": 32},
            psi={"cpu": {"some": {"avg10": 0.0}}, "io": {"some": {"avg10": 0.0}}, "memory": {"some": {"avg10": 0.0}}},
            mem={"dirty_gib": 0.01, "mem_avail_gib": 100.0, "swap_used_pct": 0.0},
            swap_delta=(0, 0),
            disks=[{"device": "nvme0n1", "mounts": [{"mount": "/", "used_pct": 50.0}], "util_pct": 1.0, "aqu_sz": 0.02, "w_await_ms": 0.5, "r_await_ms": 0.3, "w_mbs": 1.0, "r_mbs": 0.1}],
            d_state_total=0,
            d_wchan_counts=Counter(),
            top_cpu=[],
            overlaps=[],
            turnover={"started": 0, "exited": 0, "total_procs": 400},
        )
        self.assertEqual(verdict["bottleneck"], "NORMAL_OR_IDLE")
        self.assertEqual(verdict["productivity"], "HEALTHY_IDLE_OR_LIGHT")
        self.assertFalse(verdict["is_issue"])

    def test_productive_compute_verdict(self) -> None:
        verdict = si.classify_verdict(
            cpu={"us": 85.0, "sy": 4.0, "ni": 0.0, "id": 10.0, "wa": 1.0, "hi": 0.0, "si": 0.0, "st": 0.0},
            load={"load1": 28.0, "load5": 25.0, "load15": 20.0, "cpus": 32},
            psi={"cpu": {"some": {"avg10": 15.0}}, "io": {"some": {"avg10": 0.2}}, "memory": {"some": {"avg10": 0.0}}},
            mem={"dirty_gib": 0.1, "mem_avail_gib": 80.0, "swap_used_pct": 0.0},
            swap_delta=(0, 0),
            disks=[],
            d_state_total=0,
            d_wchan_counts=Counter(),
            top_cpu=[{"pid": 10, "comm": "rustc", "cpu_pct": 800.0, "io_total_mbs": 12.0, "elapsed_sec": 30}],
            overlaps=[],
            turnover={"started": 5, "exited": 4, "total_procs": 500},
        )
        self.assertEqual(verdict["bottleneck"], "CPU_COMPUTE_BOUND")
        self.assertEqual(verdict["productivity"], "PRODUCTIVE_COMPUTE")
        self.assertFalse(verdict["is_issue"])

    def test_productive_io_uses_all_disks_when_none_saturated(self) -> None:
        verdict = si.classify_verdict(
            cpu={"us": 4.0, "sy": 2.0, "ni": 0.0, "id": 83.0, "wa": 11.0, "hi": 0.0, "si": 0.0, "st": 0.0},
            load={"load1": 1.3, "load5": 5.0, "load15": 10.0, "cpus": 32},
            psi={"cpu": {"some": {"avg10": 0.0}}, "io": {"some": {"avg10": 55.0}, "full": {"avg10": 50.0}}, "memory": {"some": {"avg10": 0.0}}},
            mem={"dirty_gib": 0.01, "mem_avail_gib": 90.0, "swap_used_pct": 0.0},
            swap_delta=(0, 0),
            disks=[{"device": "nvme2n1", "mounts": [{"mount": "/datanapps", "used_pct": 30.6}], "util_pct": 72.6, "aqu_sz": 2.45, "w_await_ms": 0.0, "r_await_ms": 1.4, "w_mbs": 0.02, "r_mbs": 118.7}],
            d_state_total=2,
            d_wchan_counts=Counter({"folio_wait_bit_common": 2}),
            top_cpu=[],
            overlaps=[],
            turnover={"started": 4, "exited": 6, "total_procs": 1300},
        )
        self.assertEqual(verdict["bottleneck"], "IO_WAIT_SATURATION")
        self.assertEqual(verdict["productivity"], "PRODUCTIVE_IO")
        self.assertTrue(any("118.7 MB/s" in r for r in verdict["reasons"]))


class TestCLIValidation(unittest.TestCase):
    def test_help(self) -> None:
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--interval", r.stdout)
        self.assertIn("--exit-on-issue", r.stdout)

    def test_invalid_interval_fails_cleanly(self) -> None:
        r = subprocess.run([sys.executable, str(SCRIPT), "--interval", "0.01"], capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("error:", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_invalid_top_fails_cleanly(self) -> None:
        r = subprocess.run([sys.executable, str(SCRIPT), "--top", "0"], capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("error:", r.stderr)

    def test_invalid_watch_fails_cleanly(self) -> None:
        r = subprocess.run([sys.executable, str(SCRIPT), "--watch", "-1"], capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("error:", r.stderr)


if __name__ == "__main__":
    unittest.main()
