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

    def test_parse_cgroup_text_v2_v1_and_containers(self) -> None:
        self.assertEqual(
            si.parse_cgroup_text("0::/system.slice/docker-0123456789abcdef0123456789abcdef.scope\n"),
            "docker:0123456789ab",
        )
        self.assertEqual(
            si.parse_cgroup_text("12:memory:/docker/abcdef1234567890abcdef1234567890\n11:cpuset:/\n"),
            "docker:abcdef123456",
        )
        self.assertEqual(
            si.parse_cgroup_text("0::/user.slice/user-1000.slice/libpod-deadbeef1234567890.scope\n"),
            "podman:deadbeef1234",
        )
        self.assertEqual(
            si.parse_cgroup_text("0::/kubepods.slice/cri-containerd-99887766554433221100.scope\n"),
            "containerd:998877665544",
        )
        self.assertEqual(
            si.parse_cgroup_text("0::/system.slice/postgresql@16-main.service\n"),
            "systemd:postgresql@16-main.service",
        )
        long_scope = (
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
            "ptyxis-spawn-0238cfe1-32a9-4f20-917c-5f536bd2202c.scope\n"
        )
        parsed_long = si.parse_cgroup_text(long_scope)
        self.assertTrue(parsed_long.startswith("systemd:ptyxis-spawn-"))
        self.assertLessEqual(len(parsed_long), 40)

        v1_systemd = "12:cpuset:/\n11:pids:/system.slice/nginx.service\n1:name=systemd:/system.slice/nginx.service\n"
        self.assertEqual(si.parse_cgroup_text(v1_systemd), "systemd:nginx.service")
        self.assertEqual(si.parse_cgroup_text("0::/\n"), "/")
        self.assertEqual(si.parse_cgroup_text("12:cpuset:/\n1:name=systemd:/\n"), "/")
        self.assertEqual(si.parse_cgroup_text(""), "-")
        self.assertEqual(si.parse_cgroup_text("   \n"), "-")

    def test_read_proc_cgroup_synthetic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pdir = Path(td) / "1234"
            pdir.mkdir()
            (pdir / "cgroup").write_text("0::/system.slice/sshd.service\n", encoding="utf-8")
            self.assertEqual(si.read_proc_cgroup(1234, proc_root=td), "systemd:sshd.service")
            self.assertEqual(si.read_proc_cgroup(999999, proc_root=td), "-")

    def test_read_nvme_health_and_thermal_zones_synthetic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            nvme_root = Path(td) / "nvme"
            # nvme0: OK with 2 sensors
            n0 = nvme_root / "nvme0" / "hwmon0"
            n0.mkdir(parents=True)
            (nvme_root / "nvme0" / "model").write_text("Samsung 980 PRO   \n", encoding="utf-8")
            (nvme_root / "nvme0" / "state").write_text("live\n", encoding="utf-8")
            (n0 / "temp1_input").write_text("35850\n", encoding="utf-8")
            (n0 / "temp1_max").write_text("70000\n", encoding="utf-8")
            (n0 / "temp1_crit").write_text("85000\n", encoding="utf-8")
            (n0 / "temp1_alarm").write_text("0\n", encoding="utf-8")
            (n0 / "temp1_label").write_text("Composite\n", encoding="utf-8")
            (n0 / "temp2_input").write_text("41200\n", encoding="utf-8")
            (n0 / "temp2_label").write_text("Sensor 1\n", encoding="utf-8")

            # nvme1: WARM (>= temp_max_c)
            n1 = nvme_root / "nvme1" / "hwmon1"
            n1.mkdir(parents=True)
            (nvme_root / "nvme1" / "model").write_text("WD Black SN850X\n", encoding="utf-8")
            (nvme_root / "nvme1" / "state").write_text("live\n", encoding="utf-8")
            (n1 / "temp1_input").write_text("72500\n", encoding="utf-8")
            (n1 / "temp1_max").write_text("70000\n", encoding="utf-8")
            (n1 / "temp1_crit").write_text("85000\n", encoding="utf-8")
            (n1 / "temp1_alarm").write_text("0\n", encoding="utf-8")
            (n1 / "temp1_label").write_text("Composite\n", encoding="utf-8")

            # nvme2: CRITICAL via alarm=1
            n2 = nvme_root / "nvme2" / "hwmon2"
            n2.mkdir(parents=True)
            (nvme_root / "nvme2" / "model").write_text("SPCC SSD\n", encoding="utf-8")
            (nvme_root / "nvme2" / "state").write_text("live\n", encoding="utf-8")
            (n2 / "temp1_input").write_text("60000\n", encoding="utf-8")
            (n2 / "temp1_crit").write_text("90000\n", encoding="utf-8")
            (n2 / "temp1_alarm").write_text("1\n", encoding="utf-8")

            # nvme3: CRITICAL via non-live state
            n3 = nvme_root / "nvme3"
            n3.mkdir(parents=True)
            (n3 / "model").write_text("Faulty NVMe\n", encoding="utf-8")
            (n3 / "state").write_text("resetting\n", encoding="utf-8")

            res = si.read_nvme_health(str(nvme_root))
            self.assertEqual([r["controller"] for r in res], ["nvme0", "nvme1", "nvme2", "nvme3"])
            self.assertEqual(res[0]["model"], "Samsung 980 PRO")
            self.assertEqual(res[0]["temp_c"], 35.9)
            self.assertEqual(res[0]["thermal_status"], "OK")
            self.assertEqual(len(res[0]["sensors"]), 2)
            self.assertEqual(res[1]["thermal_status"], "WARM")
            self.assertEqual(res[2]["thermal_status"], "CRITICAL")
            self.assertTrue(res[2]["alarm"])
            self.assertEqual(res[3]["thermal_status"], "CRITICAL")
            self.assertEqual(res[3]["state"], "resetting")

            self.assertEqual(si.read_nvme_health(str(Path(td) / "missing")), [])

            # Thermal zones
            tz_root = Path(td) / "thermal"
            z0 = tz_root / "thermal_zone0"
            z1 = tz_root / "thermal_zone1"
            z0.mkdir(parents=True)
            z1.mkdir(parents=True)
            (z0 / "type").write_text("x86_pkg_temp\n", encoding="utf-8")
            (z0 / "temp").write_text("48500\n", encoding="utf-8")
            (z1 / "type").write_text("acpitz\n", encoding="utf-8")
            (z1 / "temp").write_text("not_a_number\n", encoding="utf-8")
            zones = si.read_thermal_zones(str(tz_root))
            self.assertEqual(zones, [{"zone": "thermal_zone0", "type": "x86_pkg_temp", "temp_c": 48.5}])
            self.assertEqual(si.read_thermal_zones(str(Path(td) / "missing_tz")), [])

    def test_inspect_pid_threads_synthetic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            troot = Path(td) / "500" / "task"
            fields_r = ["R", "1"] + ["0"] * 18
            fields_d = ["D", "1"] + ["0"] * 18
            fields_s = ["S", "1"] + ["0"] * 18
            for tid, comm, fields, wchan in (
                ("500", "main", fields_s, "ep_poll"),
                ("501", "worker-1", fields_r, "0"),
                ("502", "writer-1", fields_d, "balance_dirty_pages"),
                ("503", "writer-2", fields_d, "balance_dirty_pages"),
            ):
                d = troot / tid
                d.mkdir(parents=True)
                (d / "stat").write_text(f"{tid} ({comm}) {' '.join(fields)}\n", encoding="utf-8")
                (d / "wchan").write_text(wchan, encoding="utf-8")

            summary = si.inspect_pid_threads(500, proc_root=td)
            self.assertEqual(summary["total_threads"], 4)
            self.assertEqual(summary["r_threads"], 1)
            self.assertEqual(summary["d_threads"], 2)
            self.assertEqual(summary["s_threads"], 1)
            self.assertEqual(summary["d_wchans"], {"balance_dirty_pages": 2})
            self.assertEqual(len(summary["blocked_threads"]), 2)
            self.assertEqual(summary["blocked_threads"][0]["tid"], 502)


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

    def test_nvme_thermal_warnings_in_classify_verdict(self) -> None:
        base_kwargs = dict(
            cpu={"us": 3.0, "sy": 1.0, "ni": 0.0, "id": 95.5, "wa": 0.5, "hi": 0.0, "si": 0.0, "st": 0.0},
            load={"load1": 0.8, "load5": 0.6, "load15": 0.5, "cpus": 32},
            psi={"cpu": {"some": {"avg10": 0.0}}, "io": {"some": {"avg10": 0.0}}, "memory": {"some": {"avg10": 0.0}}},
            mem={"dirty_gib": 0.01, "mem_avail_gib": 100.0, "swap_used_pct": 0.0},
            swap_delta=(0, 0),
            disks=[],
            d_state_total=0,
            d_wchan_counts=Counter(),
            top_cpu=[],
            overlaps=[],
            turnover={"started": 0, "exited": 0, "total_procs": 400},
        )
        warm = si.classify_verdict(
            **base_kwargs,
            nvme_health=[
                {
                    "controller": "nvme1",
                    "model": "SSD",
                    "state": "live",
                    "temp_c": 73.5,
                    "temp_max_c": 70.0,
                    "temp_crit_c": 85.0,
                    "alarm": False,
                    "thermal_status": "WARM",
                    "sensors": [],
                }
            ],
        )
        self.assertFalse(warm["is_issue"])
        self.assertTrue(any("nvme1" in r and "73.5" in r for r in warm["reasons"]))
        self.assertTrue(any("thermal" in r.lower() for r in warm["recommendations"]))

        crit = si.classify_verdict(
            **base_kwargs,
            nvme_health=[
                {
                    "controller": "nvme2",
                    "model": "SSD",
                    "state": "live",
                    "temp_c": 86.0,
                    "temp_max_c": 70.0,
                    "temp_crit_c": 85.0,
                    "alarm": True,
                    "thermal_status": "CRITICAL",
                    "sensors": [],
                }
            ],
        )
        self.assertTrue(crit["is_issue"])
        self.assertTrue(any("nvme2" in r and "critical" in r.lower() for r in crit["reasons"]))
        self.assertTrue(any("nvme2" in r for r in crit["recommendations"]))


class TestPidClassifierAndRender(unittest.TestCase):
    def _base_target(self, **overrides: object) -> dict:
        t = {
            "pid": 1234,
            "ppid": 1,
            "comm": "worker",
            "state": "S",
            "elapsed_sec": 60,
            "elapsed": "01m00s",
            "cmdline": "worker --run",
            "ancestry": "1234:worker(01m00s)",
            "cgroup": "systemd:worker.service",
            "mounts": ["/datanapps"],
            "cpu_pct": 1.0,
            "rss_mib": 128.0,
            "swap_mib": 0.0,
            "r_mbs": 0.0,
            "w_mbs": 0.0,
            "wchar_mbs": 0.0,
            "oom_score": 10,
        }
        t.update(overrides)
        return t

    def _base_threads(self, **overrides: object) -> dict:
        th = {
            "total_threads": 2,
            "r_threads": 0,
            "d_threads": 0,
            "s_threads": 2,
            "d_wchans": {},
            "blocked_threads": [],
        }
        th.update(overrides)
        return th

    def _base_subtree(self, **overrides: object) -> dict:
        sub = {
            "descendant_count": 0,
            "subtree_cpu_pct": 1.0,
            "subtree_rss_mib": 128.0,
            "subtree_w_mbs": 0.0,
            "subtree_r_mbs": 0.0,
            "subtree_d_threads": 0,
            "top_descendants": [],
        }
        sub.update(overrides)
        return sub

    def test_classify_pid_verdict_all_statuses(self) -> None:
        v_blocked = si.classify_pid_verdict(
            self._base_target(state="D"),
            self._base_threads(d_threads=2, d_wchans={"balance_dirty_pages": 2}),
            self._base_subtree(subtree_d_threads=2),
        )
        self.assertEqual(v_blocked["status"], "BLOCKED_ON_IO")
        self.assertTrue(v_blocked["is_issue"])
        self.assertTrue(any("balance_dirty_pages" in r for r in v_blocked["recommendations"]))

        v_mem = si.classify_pid_verdict(
            self._base_target(rss_mib=10240.0, swap_mib=600.0, oom_score=650),
            self._base_threads(),
            self._base_subtree(subtree_rss_mib=10240.0),
            mem={"mem_total_gib": 32.0},
        )
        self.assertEqual(v_mem["status"], "MEMORY_HEAVY")
        self.assertTrue(v_mem["is_issue"])

        v_cpu = si.classify_pid_verdict(
            self._base_target(cpu_pct=150.0, state="R"),
            self._base_threads(r_threads=2),
            self._base_subtree(subtree_cpu_pct=150.0),
        )
        self.assertEqual(v_cpu["status"], "CPU_BOUND")
        self.assertFalse(v_cpu["is_issue"])

        v_io = si.classify_pid_verdict(
            self._base_target(w_mbs=25.0),
            self._base_threads(),
            self._base_subtree(subtree_w_mbs=25.0),
        )
        self.assertEqual(v_io["status"], "IO_ACTIVE")
        self.assertFalse(v_io["is_issue"])

        v_idle = si.classify_pid_verdict(
            self._base_target(),
            self._base_threads(),
            self._base_subtree(),
        )
        self.assertEqual(v_idle["status"], "IDLE_OR_SLEEPING")
        self.assertFalse(v_idle["is_issue"])

    def test_render_pid_text_and_markdown(self) -> None:
        rep = {
            "mode": "pid",
            "timestamp": "2026-11-21T21:00:00-05:00",
            "sample_sec": 0.5,
            "target": self._base_target(),
            "threads": self._base_threads(
                d_threads=1,
                d_wchans={"rq_qos_wait": 1},
                blocked_threads=[{"tid": 1235, "comm": "io-wr", "state": "D", "wchan": "rq_qos_wait"}],
            ),
            "subtree": self._base_subtree(
                descendant_count=1,
                top_descendants=[
                    {
                        "pid": 1299,
                        "ppid": 1234,
                        "comm": "child",
                        "state": "S",
                        "cpu_pct": 0.5,
                        "rss_mib": 12.0,
                        "r_mbs": 0.0,
                        "w_mbs": 0.1,
                        "d_threads": 0,
                        "elapsed": "00m10s",
                        "cmdline": "child --arg",
                    }
                ],
            ),
            "verdict": {
                "status": "BLOCKED_ON_IO",
                "is_issue": True,
                "reasons": ["Blocked on disk"],
                "recommendations": ["Check disk"],
            },
        }
        txt = si.render_pid_text(rep, markdown=False)
        self.assertIn("PID Investigation: 1234 (worker)", txt)
        self.assertIn("=== Process Identity ===", txt)
        self.assertIn("rq_qos_wait", txt)
        md = si.render_pid_text(rep, markdown=True)
        self.assertIn("# PID Investigation: 1234 (worker)", md)
        self.assertIn("## Recommendations", md)


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


class TestDiffLog(unittest.TestCase):
    @staticmethod
    def _make_snapshot(
        ts: str,
        bottleneck: str = "NORMAL_OR_IDLE",
        productivity: str = "HEALTHY_IDLE_OR_LIGHT",
        is_issue: bool = False,
        cpu_us: float = 5.0,
        cpu_wa: float = 0.5,
        load1: float = 0.5,
        d_threads: int = 0,
        dirty_gib: float = 0.05,
        mem_avail_gib: float = 64.0,
        swap_used_pct: float = 0.0,
        util_pct: float = 2.0,
        w_await_ms: float = 0.5,
        aqu_sz: float = 0.05,
        io_psi: float = 0.0,
        nvme_temp: float | None = 38.0,
        wchans: dict[str, int] | None = None,
        d_comm: str | None = None,
        io_comm: str | None = None,
    ) -> dict:
        nvme = (
            [{"controller": "nvme0", "temp_c": nvme_temp, "thermal_status": "OK"}]
            if nvme_temp is not None
            else []
        )
        return {
            "timestamp": ts,
            "sample_sec": 1.0,
            "verdict": {
                "bottleneck": bottleneck,
                "productivity": productivity,
                "is_issue": is_issue,
                "reasons": [],
                "recommendations": [],
            },
            "cpu_pct": {"us": cpu_us, "sy": 1.0, "ni": 0.0, "id": max(0.0, 99.0 - cpu_us - cpu_wa), "wa": cpu_wa, "hi": 0.0, "si": 0.0, "st": 0.0},
            "load": {"load1": load1, "load5": load1, "load15": load1, "cpus": 32, "r_threads": 1, "d_threads": d_threads},
            "psi": {"cpu": {"some": {"avg10": 0.0}}, "io": {"some": {"avg10": io_psi}}, "memory": {"some": {"avg10": 0.0}}},
            "memory": {"mem_total_gib": 128.0, "mem_avail_gib": mem_avail_gib, "dirty_gib": dirty_gib, "swap_used_pct": swap_used_pct},
            "disks": [{"device": "nvme2n1", "util_pct": util_pct, "w_await_ms": w_await_ms, "r_await_ms": 0.2, "aqu_sz": aqu_sz, "w_mbs": 10.0, "r_mbs": 1.0, "mounts": []}],
            "nvme_health": nvme,
            "d_state_wchans": wchans or {},
            "d_state_top": [{"pid": 100, "comm": d_comm, "threads": max(1, d_threads)}] if d_comm else [],
            "top_io": [{"pid": 100, "comm": io_comm, "w_mbs": 20.0}] if io_comm else [],
            "top_cpu": [{"pid": 200, "comm": "rustc", "cpu_pct": cpu_us}] if cpu_us >= 20.0 else [],
            "overlapping_workloads": [{"group": "tool:cargo", "count": 2}] if is_issue else [],
        }

    def test_version_constant_and_cli_flag(self) -> None:
        self.assertEqual(si.VERSION, "0.5.0")
        r = subprocess.run([sys.executable, str(SCRIPT), "--version"], capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0)
        self.assertIn("0.5.0", r.stdout)

    def test_load_jsonl_log_skips_blanks_and_records_corrupt_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "mixed.jsonl"
            s1 = self._make_snapshot("2026-11-23T10:00:00+00:00")
            s2 = self._make_snapshot("2026-11-23T10:01:00+00:00")
            p.write_text(
                "\n"
                + json.dumps(s1) + "\n"
                + "   \n"
                + "{not valid json\n"
                + "[1, 2, 3]\n"
                + '{"unrelated": true}\n'
                + json.dumps(s2) + "\n",
                encoding="utf-8",
            )
            snaps, corrupt = si.load_jsonl_log(p)
            self.assertEqual(len(snaps), 2)
            self.assertEqual(len(corrupt), 3)
            self.assertEqual([c["line"] for c in corrupt], [4, 5, 6])

    def test_diff_single_file_degrading_and_peaks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "degrade.jsonl"
            rows = [
                self._make_snapshot("2026-11-23T10:00:00+00:00", dirty_gib=0.05, util_pct=3.0, w_await_ms=0.8, cpu_wa=0.5, mem_avail_gib=80.0, nvme_temp=40.0),
                self._make_snapshot("2026-11-23T10:01:00+00:00", dirty_gib=0.10, util_pct=5.0, w_await_ms=1.0, cpu_wa=1.0, mem_avail_gib=78.0, nvme_temp=42.0),
                self._make_snapshot(
                    "2026-11-23T10:02:00+00:00",
                    bottleneck="IO_WAIT_SATURATION",
                    productivity="THRASHING_LOW_EFFICIENCY",
                    is_issue=True,
                    dirty_gib=6.5,
                    util_pct=96.0,
                    w_await_ms=650.0,
                    aqu_sz=120.0,
                    cpu_wa=45.0,
                    d_threads=24,
                    mem_avail_gib=40.0,
                    nvme_temp=68.5,
                    wchans={"balance_dirty_pages": 24},
                    d_comm="cargo",
                    io_comm="cargo",
                ),
                self._make_snapshot(
                    "2026-11-23T10:03:00+00:00",
                    bottleneck="IO_WAIT_SATURATION",
                    productivity="THRASHING_LOW_EFFICIENCY",
                    is_issue=True,
                    dirty_gib=8.2,
                    util_pct=99.0,
                    w_await_ms=920.0,
                    aqu_sz=210.0,
                    cpu_wa=62.0,
                    d_threads=40,
                    mem_avail_gib=32.0,
                    nvme_temp=71.0,
                    wchans={"balance_dirty_pages": 40},
                    d_comm="cargo",
                    io_comm="cargo",
                ),
            ]
            p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

            rep = si.diff_jsonl_logs(p)
            self.assertEqual(rep["mode"], "diff_log")
            self.assertEqual(rep["comparison_mode"], "single_file")
            self.assertEqual(rep["verdict"]["status"], "DEGRADING")
            self.assertTrue(rep["verdict"]["is_issue"])
            self.assertNotIn("No immediate intervention needed.", rep["verdict"]["recommendations"])

            summ = rep["summary"]
            self.assertEqual(summ["snapshot_count"], 4)
            self.assertEqual(summ["issue_count"], 2)
            self.assertEqual(summ["issue_rate_pct"], 50.0)
            self.assertEqual(summ["time_range"]["span_sec"], 180.0)
            self.assertEqual(summ["verdict_distribution"]["bottleneck"], {"IO_WAIT_SATURATION": 2, "NORMAL_OR_IDLE": 2})
            self.assertEqual(summ["peaks"]["dirty_gib"]["value"], 8.2)
            self.assertEqual(summ["peaks"]["w_await_ms"]["value"], 920.0)
            self.assertEqual(summ["peaks"]["w_await_ms"]["device"], "nvme2n1")
            self.assertEqual(summ["peaks"]["disk_util_pct"]["value"], 99.0)
            self.assertEqual(summ["peaks"]["min_mem_avail_gib"]["value"], 32.0)
            self.assertEqual(summ["peaks"]["nvme_temp_c"]["value"], 71.0)

            evl = rep["earliest_vs_latest"]
            self.assertEqual(evl["deltas"]["dirty_gib"]["delta"], 8.15)
            self.assertEqual(evl["deltas"]["w_await_ms" if "w_await_ms" in evl["deltas"] else "max_w_await_ms"]["delta"], 919.2)
            self.assertEqual(rep["window_diff"]["baseline_issue_rate_pct"], 0.0)
            self.assertEqual(rep["window_diff"]["recent_issue_rate_pct"], 100.0)
            self.assertEqual(rep["recurring_offenders"]["d_state_wchans"]["balance_dirty_pages"], 64)
            self.assertEqual(rep["recurring_offenders"]["d_state_comms"][0]["comm"], "cargo")

            txt = si.render_diff_text(rep, markdown=False)
            self.assertIn("Log Trend Summary:", txt)
            self.assertIn("DEGRADING", txt)
            self.assertIn("nvme2n1", txt)
            md = si.render_diff_text(rep, markdown=True)
            self.assertIn("# Log Trend Summary:", md)
            self.assertIn("## Observed Peaks", md)

    def test_diff_window_and_recovering_vs_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # 6 snapshots: first 2 healthy, middle 2 issue, last 2 healthy -> window=2 compares middle 2 vs last 2 => RECOVERING
            p = Path(td) / "recover.jsonl"
            rows = [
                self._make_snapshot("2026-11-23T10:00:00+00:00"),
                self._make_snapshot("2026-11-23T10:01:00+00:00"),
                self._make_snapshot("2026-11-23T10:02:00+00:00", bottleneck="IO_WAIT_SATURATION", productivity="THRASHING_LOW_EFFICIENCY", is_issue=True, dirty_gib=5.0, util_pct=95.0, w_await_ms=400.0),
                self._make_snapshot("2026-11-23T10:03:00+00:00", bottleneck="IO_WAIT_SATURATION", productivity="THRASHING_LOW_EFFICIENCY", is_issue=True, dirty_gib=4.5, util_pct=92.0, w_await_ms=350.0),
                self._make_snapshot("2026-11-23T10:04:00+00:00"),
                self._make_snapshot("2026-11-23T10:05:00+00:00"),
            ]
            p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

            rep = si.diff_jsonl_logs(p, window=2)
            self.assertEqual(rep["window_diff"]["baseline_count"], 2)
            self.assertEqual(rep["window_diff"]["recent_count"], 2)
            self.assertEqual(rep["window_diff"]["baseline_issue_rate_pct"], 100.0)
            self.assertEqual(rep["window_diff"]["recent_issue_rate_pct"], 0.0)
            self.assertEqual(rep["verdict"]["status"], "RECOVERING")
            self.assertFalse(rep["verdict"]["is_issue"])

            # Persistent issue log
            p2 = Path(td) / "persist.jsonl"
            bad = [
                self._make_snapshot(f"2026-11-23T10:0{i}:00+00:00", bottleneck="IO_WAIT_SATURATION", productivity="THRASHING_LOW_EFFICIENCY", is_issue=True, dirty_gib=4.0, util_pct=95.0, w_await_ms=200.0)
                for i in range(4)
            ]
            p2.write_text("\n".join(json.dumps(r) for r in bad) + "\n", encoding="utf-8")
            rep2 = si.diff_jsonl_logs(p2)
            self.assertEqual(rep2["verdict"]["status"], "PERSISTENT_ISSUE")
            self.assertTrue(rep2["verdict"]["is_issue"])

            # Single snapshot healthy & issue
            p3 = Path(td) / "single_ok.jsonl"
            p3.write_text(json.dumps(self._make_snapshot("2026-11-23T10:00:00+00:00")) + "\n", encoding="utf-8")
            rep3 = si.diff_jsonl_logs(p3)
            self.assertEqual(rep3["verdict"]["status"], "SINGLE_SNAPSHOT_HEALTHY")
            self.assertFalse(rep3["verdict"]["is_issue"])

            p4 = Path(td) / "single_bad.jsonl"
            p4.write_text(json.dumps(bad[0]) + "\n", encoding="utf-8")
            rep4 = si.diff_jsonl_logs(p4)
            self.assertEqual(rep4["verdict"]["status"], "SINGLE_SNAPSHOT_ISSUE")
            self.assertTrue(rep4["verdict"]["is_issue"])

    def test_diff_two_files_baseline_vs_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "base.jsonl"
            target = Path(td) / "target.jsonl"
            base.write_text(
                "\n".join(
                    json.dumps(self._make_snapshot(f"2026-11-23T09:0{i}:00+00:00", dirty_gib=0.05, util_pct=4.0, w_await_ms=1.0))
                    for i in range(3)
                )
                + "\n",
                encoding="utf-8",
            )
            target.write_text(
                "\n".join(
                    json.dumps(self._make_snapshot(f"2026-11-23T10:0{i}:00+00:00", dirty_gib=3.5, util_pct=92.0, w_await_ms=180.0, is_issue=False))
                    for i in range(3)
                )
                + "\n",
                encoding="utf-8",
            )
            rep = si.diff_jsonl_logs(target, baseline_path=base, window=2)
            self.assertEqual(rep["comparison_mode"], "two_file")
            self.assertIsNotNone(rep["baseline_summary"])
            self.assertEqual(rep["window_diff"]["baseline_count"], 2)
            self.assertEqual(rep["window_diff"]["recent_count"], 2)
            # Metric degradation triggers DEGRADING + is_issue even if individual snapshots had is_issue=False
            self.assertEqual(rep["verdict"]["status"], "DEGRADING")
            self.assertTrue(rep["verdict"]["is_issue"])
            self.assertIn("Log Trend Diff:", si.render_diff_text(rep))

    def test_diff_log_cli_exit_codes_corrupt_warning_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ok_log = Path(td) / "ok.jsonl"
            ok_log.write_text(
                "\n"
                + json.dumps(self._make_snapshot("2026-11-23T10:00:00+00:00")) + "\n"
                + "{corrupt\n"
                + json.dumps(self._make_snapshot("2026-11-23T10:01:00+00:00")) + "\n",
                encoding="utf-8",
            )
            r_ok = subprocess.run(
                [sys.executable, str(SCRIPT), "--diff-log", str(ok_log), "--json", "--exit-on-issue"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(r_ok.returncode, 0, msg=r_ok.stderr)
            self.assertIn("warning: skipped 1 corrupt JSON line(s)", r_ok.stderr)
            data_ok = json.loads(r_ok.stdout)
            self.assertEqual(data_ok["verdict"]["status"], "STABLE_HEALTHY")
            self.assertEqual(data_ok["summary"]["corrupt_line_count"], 1)

            bad_log = Path(td) / "bad.jsonl"
            bad_log.write_text(
                json.dumps(self._make_snapshot("2026-11-23T10:00:00+00:00")) + "\n"
                + json.dumps(self._make_snapshot("2026-11-23T10:01:00+00:00", bottleneck="IO_WAIT_SATURATION", productivity="THRASHING_LOW_EFFICIENCY", is_issue=True, dirty_gib=6.0, util_pct=98.0, w_await_ms=500.0)) + "\n",
                encoding="utf-8",
            )
            r_bad = subprocess.run(
                [sys.executable, str(SCRIPT), "--diff-log", str(bad_log), "--json", "--exit-on-issue"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(r_bad.returncode, 3, msg=r_bad.stderr)
            self.assertEqual(json.loads(r_bad.stdout)["verdict"]["status"], "DEGRADING")

            # Missing file -> exit 1, single-line stderr, no traceback
            r_miss = subprocess.run(
                [sys.executable, str(SCRIPT), "--diff-log", str(Path(td) / "nope.jsonl")],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(r_miss.returncode, 1)
            self.assertIn("error:", r_miss.stderr)
            self.assertNotIn("Traceback", r_miss.stderr)
            self.assertEqual(len(r_miss.stderr.strip().splitlines()), 1)

            # Empty / all-corrupt file -> exit 1, single-line stderr, no traceback
            empty_log = Path(td) / "empty.jsonl"
            empty_log.write_text("\n  \n{bad\n", encoding="utf-8")
            r_empty = subprocess.run(
                [sys.executable, str(SCRIPT), "--diff-log", str(empty_log)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(r_empty.returncode, 1)
            self.assertIn("error:", r_empty.stderr)
            self.assertNotIn("Traceback", r_empty.stderr)
            self.assertEqual(len(r_empty.stderr.strip().splitlines()), 1)


class TestCLIValidation(unittest.TestCase):
    def test_help(self) -> None:
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0)
        for flag in (
            "--interval",
            "--exit-on-issue",
            "--progress-check",
            "--no-statvfs",
            "--append-log",
            "--diff-log",
            "--diff-baseline",
            "--diff-window",
            "--diagnose-pid",
            "--version",
        ):
            self.assertIn(flag, r.stdout)

    def test_invalid_diff_flag_combinations_fail_cleanly(self) -> None:
        cases = (
            ["--diff-baseline", "/tmp/a.jsonl"],
            ["--diff-window", "3"],
            ["--diff-log", "/tmp/a.jsonl", "--diff-window", "0"],
            ["--diff-log", "/tmp/a.jsonl", "--watch", "2"],
            ["--diff-log", "/tmp/a.jsonl", "--progress-check"],
            ["--diff-log", "/tmp/a.jsonl", "--append-log", "/tmp/b.jsonl"],
        )
        for args in cases:
            r = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False)
            self.assertEqual(r.returncode, 1, msg=f"args={args} stderr={r.stderr}")
            self.assertIn("error:", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

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

    def test_invalid_and_missing_diagnose_pid_fails_cleanly(self) -> None:
        for bad_pid in ("0", "-42", "99999999"):
            r = subprocess.run(
                [sys.executable, str(SCRIPT), "--diagnose-pid", bad_pid, "--interval", "0.2"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(r.returncode, 1, msg=f"bad_pid={bad_pid} stderr={r.stderr}")
            self.assertIn("error:", r.stderr)
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
