#!/usr/bin/env python3
"""Unit and CLI error-path tests for sysadmin_tools.sys_investigate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

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

    def test_is_safe_statvfs_fstype(self) -> None:
        for safe in ("ext4", "xfs", "btrfs", "vfat", "f2fs"):
            self.assertTrue(si.is_safe_statvfs_fstype(safe), msg=safe)
        for unsafe in ("nfs", "nfs4", "cifs", "sshfs", "fuse", "fuse.sshfs", "fuse.rclone", "fuseblk", "autofs", "9p"):
            self.assertFalse(si.is_safe_statvfs_fstype(unsafe), msg=unsafe)

    def test_map_disks_to_mounts_skips_unsafe_and_respects_no_statvfs(self) -> None:
        mounts_txt = "\n".join(
            [
                "/dev/nvme0n1p2 / ext4 rw,relatime 0 0",
                "/dev/nvme2n1p1 /datanapps ext4 rw,relatime 0 0",
                "/dev/sda1 /mnt/usb fuseblk rw 0 0",
                "/dev/sdb1 /mnt/rclone fuse.rclone rw 0 0",
                "10.0.0.1:/export /mnt/nfs nfs4 rw 0 0",
            ]
        )
        fake_stat = mock.MagicMock(f_blocks=1000, f_bfree=250)
        with mock.patch("os.statvfs", return_value=fake_stat) as m_statvfs, mock.patch(
            "os.path.realpath", side_effect=lambda p: p
        ):
            res = si.map_disks_to_mounts(allow_statvfs=True, mounts_txt=mounts_txt)
            called_paths = [c.args[0] for c in m_statvfs.call_args_list]
            self.assertEqual(called_paths, ["/", "/datanapps"])
            self.assertEqual(res["nvme0n1"][0]["used_pct"], 75.0)
            self.assertIsNone(res["sda"][0]["used_pct"])
            self.assertIsNone(res["sdb"][0]["used_pct"])

        with mock.patch("os.statvfs", return_value=fake_stat) as m_statvfs, mock.patch(
            "os.path.realpath", side_effect=lambda p: p
        ):
            res_no = si.map_disks_to_mounts(allow_statvfs=False, mounts_txt=mounts_txt)
            m_statvfs.assert_not_called()
            self.assertIsNone(res_no["nvme0n1"][0]["used_pct"])
            self.assertIsNone(res_no["nvme2n1"][0]["used_pct"])

    def test_resolve_mount_for_path(self) -> None:
        mounts = ["/", "/boot/efi", "/datanapps"]
        self.assertEqual(si.resolve_mount_for_path("/datanapps/cargo-target/debug/bin", mounts), "/datanapps")
        self.assertEqual(si.resolve_mount_for_path("/datanapps/tmp.o (deleted)", mounts), "/datanapps")
        self.assertEqual(si.resolve_mount_for_path("/home/bill/file.txt", mounts), "/")
        self.assertEqual(si.resolve_mount_for_path("/datanapps2/other", mounts), "/")
        self.assertIsNone(si.resolve_mount_for_path("socket:[12345]", mounts))
        self.assertIsNone(si.resolve_mount_for_path("pipe:[67890]", mounts))
        self.assertIsNone(si.resolve_mount_for_path("/proc/123/fd/1", mounts))
        self.assertIsNone(si.resolve_mount_for_path("/dev/pts/0", mounts))

    def test_attribute_pid_mounts_synthetic_proc(self) -> None:
        mounts = ["/", "/datanapps", "/mnt/data"]
        with tempfile.TemporaryDirectory() as td:
            pdir = Path(td) / "4242"
            fddir = pdir / "fd"
            fddir.mkdir(parents=True)
            os.symlink("/datanapps/worktrees/proj", pdir / "cwd")
            os.symlink("/datanapps/cargo-target/a.o", fddir / "3")
            os.symlink("/datanapps/cargo-target/b.o", fddir / "4")
            os.symlink("/home/bill/.cargo/config.toml", fddir / "5")
            os.symlink("socket:[999]", fddir / "6")
            os.symlink("/dev/null", fddir / "0")

            attributed = si.attribute_pid_mounts(4242, mounts, proc_root=td)
            self.assertEqual(attributed, ["/datanapps", "/"])
            self.assertEqual(si.attribute_pid_mounts(999999, mounts, proc_root=td), [])

    def test_append_jsonl_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "nested" / "diag.jsonl"
            si.append_jsonl_log(log_path, {"b": 2, "a": 1})
            si.append_jsonl_log(log_path, {"z": 9, "c": 3})
            lines = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0], '{"a":1,"b":2}')
            self.assertEqual(json.loads(lines[1]), {"c": 3, "z": 9})


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

    def test_thrashing_without_overlaps_never_emits_no_intervention(self) -> None:
        verdict = si.classify_verdict(
            cpu={"us": 25.0, "sy": 5.0, "ni": 0.0, "id": 58.0, "wa": 12.0, "hi": 0.0, "si": 0.0, "st": 0.0},
            load={"load1": 18.0, "load5": 9.0, "load15": 4.0, "cpus": 32},
            psi={"cpu": {"some": {"avg10": 1.2}}, "io": {"some": {"avg10": 56.5}, "full": {"avg10": 49.9}}, "memory": {"some": {"avg10": 0.0}}},
            mem={"dirty_gib": 13.98, "mem_avail_gib": 85.0, "swap_used_pct": 99.0},
            swap_delta=(0, 0),
            disks=[{"device": "nvme2n1", "mounts": [{"mount": "/datanapps", "used_pct": 29.7}], "util_pct": 89.1, "aqu_sz": 195.6, "w_await_ms": 1862.6, "r_await_ms": 0.0, "w_mbs": 12.8, "r_mbs": 0.0}],
            d_state_total=4,
            d_wchan_counts=Counter({"balance_dirty_pages": 1}),
            top_cpu=[],
            overlaps=[],
            turnover={"started": 1, "exited": 22, "total_procs": 1400},
            top_io=[{"pid": 2803213, "comm": "cargo", "w_mbs": 35.4, "io_total_mbs": 35.4, "mounts": ["/datanapps"]}],
        )
        self.assertEqual(verdict["productivity"], "THRASHING_LOW_EFFICIENCY")
        self.assertTrue(verdict["is_issue"])
        self.assertNotIn("No immediate intervention needed.", verdict["recommendations"])
        self.assertTrue(any("nvme2n1(/datanapps)" in r and "2803213:cargo" in r for r in verdict["recommendations"]))

    def test_wedged_stalled_emits_actionable_recommendation(self) -> None:
        verdict = si.classify_verdict(
            cpu={"us": 1.0, "sy": 1.0, "ni": 0.0, "id": 10.0, "wa": 88.0, "hi": 0.0, "si": 0.0, "st": 0.0},
            load={"load1": 60.0, "load5": 50.0, "load15": 40.0, "cpus": 32},
            psi={"cpu": {"some": {"avg10": 0.0}}, "io": {"some": {"avg10": 95.0}}, "memory": {"some": {"avg10": 0.0}}},
            mem={"dirty_gib": 8.0, "mem_avail_gib": 50.0, "swap_used_pct": 0.0},
            swap_delta=(0, 0),
            disks=[{"device": "nvme2n1", "mounts": [{"mount": "/datanapps", "used_pct": 30.0}], "util_pct": 100.0, "aqu_sz": 300.0, "w_await_ms": 2500.0, "r_await_ms": 0.0, "w_mbs": 0.5, "r_mbs": 0.0}],
            d_state_total=40,
            d_wchan_counts=Counter({"balance_dirty_pages": 40}),
            top_cpu=[],
            overlaps=[],
            turnover={"started": 0, "exited": 0, "total_procs": 1000},
        )
        self.assertEqual(verdict["productivity"], "WEDGED_OR_STALLED")
        self.assertTrue(verdict["is_issue"])
        self.assertNotIn("No immediate intervention needed.", verdict["recommendations"])
        self.assertTrue(any("--progress-check" in r for r in verdict["recommendations"]))

    def test_high_write_latency_and_queue_depth_classified_as_thrashing(self) -> None:
        verdict = si.classify_verdict(
            cpu={"us": 4.6, "sy": 4.1, "ni": 0.0, "id": 78.8, "wa": 12.3, "hi": 0.0, "si": 0.0, "st": 0.0},
            load={"load1": 9.9, "load5": 15.4, "load15": 16.3, "cpus": 32},
            psi={"cpu": {"some": {"avg10": 0.2}}, "io": {"some": {"avg10": 56.1}, "full": {"avg10": 51.6}}, "memory": {"some": {"avg10": 0.0}}},
            mem={"dirty_gib": 1.65, "mem_avail_gib": 80.6, "swap_used_pct": 99.8},
            swap_delta=(0, 0),
            disks=[{"device": "nvme2n1", "mounts": [{"mount": "/datanapps", "used_pct": 29.8}], "util_pct": 82.4, "aqu_sz": 202.71, "w_await_ms": 1388.5, "r_await_ms": 0.0, "w_mbs": 18.56, "r_mbs": 0.0}],
            d_state_total=4,
            d_wchan_counts=Counter({"rq_qos_wait": 2}),
            top_cpu=[],
            overlaps=[],
            turnover={"started": 1, "exited": 1, "total_procs": 1391},
        )
        self.assertEqual(verdict["bottleneck"], "IO_WAIT_SATURATION")
        self.assertEqual(verdict["productivity"], "THRASHING_LOW_EFFICIENCY")
        self.assertTrue(verdict["is_issue"])
        self.assertNotIn("No immediate intervention needed.", verdict["recommendations"])


class TestProgressComparison(unittest.TestCase):
    @staticmethod
    def _make_rep(
        bottleneck: str,
        productivity: str,
        is_issue: bool,
        dirty_gib: float = 0.1,
        started: int = 0,
        exited: int = 0,
        d_pids: list[int] | None = None,
        cpu_pids: list[tuple[int, float]] | None = None,
    ) -> dict:
        return {
            "verdict": {"bottleneck": bottleneck, "productivity": productivity, "is_issue": is_issue},
            "memory": {"dirty_gib": dirty_gib},
            "turnover": {"started": started, "exited": exited, "total_procs": 500},
            "d_state_top": [{"pid": p} for p in (d_pids or [])],
            "top_cpu": [{"pid": p, "cpu_pct": c} for p, c in (cpu_pids or [])],
        }

    def test_compare_windows_confirmed_stall(self) -> None:
        r1 = self._make_rep("IO_WAIT_SATURATION", "WEDGED_OR_STALLED", True, dirty_gib=10.0, d_pids=[101, 102])
        r2 = self._make_rep("IO_WAIT_SATURATION", "WEDGED_OR_STALLED", True, dirty_gib=10.5, d_pids=[102, 103])
        pc = si.compare_windows(r1, r2)
        self.assertEqual(pc["status"], "CONFIRMED_STALL")
        self.assertEqual(pc["dirty_gib_delta"], 0.5)
        self.assertEqual(pc["persistent_d_pids"], [102])

    def test_compare_windows_persistent_thrash_recovering_worsening_idle(self) -> None:
        thrash = self._make_rep("IO_WAIT_SATURATION", "THRASHING_LOW_EFFICIENCY", True, dirty_gib=5.0, started=2, exited=1, cpu_pids=[(55, 80.0)])
        thrash2 = self._make_rep("IO_WAIT_SATURATION", "THRASHING_LOW_EFFICIENCY", True, dirty_gib=4.2, started=3, exited=2, cpu_pids=[(55, 75.0)])
        idle = self._make_rep("NORMAL_OR_IDLE", "HEALTHY_IDLE_OR_LIGHT", False, dirty_gib=0.05)
        compute = self._make_rep("CPU_COMPUTE_BOUND", "PRODUCTIVE_COMPUTE", False, dirty_gib=0.1)

        pc_thrash = si.compare_windows(thrash, thrash2)
        self.assertEqual(pc_thrash["status"], "PERSISTENT_THRASH")
        self.assertEqual(pc_thrash["dirty_gib_delta"], -0.8)
        self.assertEqual(pc_thrash["persistent_cpu_pids"], [55])
        self.assertEqual(pc_thrash["total_turnover"], {"started": 5, "exited": 3})

        self.assertEqual(si.compare_windows(thrash, idle)["status"], "RECOVERING")
        self.assertEqual(si.compare_windows(idle, thrash)["status"], "WORSENING")
        self.assertEqual(si.compare_windows(idle, idle)["status"], "IDLE")
        self.assertEqual(si.compare_windows(compute, compute)["status"], "STEADY_PROGRESS")


class TestCLIValidation(unittest.TestCase):
    def test_help(self) -> None:
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0)
        for flag in ("--interval", "--exit-on-issue", "--progress-check", "--no-statvfs", "--append-log"):
            self.assertIn(flag, r.stdout)

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
