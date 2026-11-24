#!/usr/bin/env python3
"""Unit and CLI error-path tests for sysadmin_tools.sys_crash_triage."""

from __future__ import annotations

import datetime as dt
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
SCRIPT = SRC_DIR / "sysadmin_tools" / "sys_crash_triage.py"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sysadmin_tools import sys_crash_triage as sct  # noqa: E402


class TestBootAndResetParsers(unittest.TestCase):
    def test_parse_journal_boots(self) -> None:
        sample = "\n".join(
            [
                " -2 a8a1f96d308c442ba997b78d89fb3ec7 Wed 2026-10-28 20:17:15 EDT Tue 2026-11-03 02:23:19 EST",
                " -1 d7c06553ea8f45be915c8d22853bcee2 Tue 2026-11-03 08:02:32 EST Sun 2026-11-22 00:15:13 EST",
                "  0 fd4d17a9b6bf4b7da614de44bd7f67b4 Sun 2026-11-22 00:18:40 EST Sun 2026-11-22 00:45:06 EST",
                "malformed line",
            ]
        )
        boots = sct.parse_journal_boots(sample)
        self.assertEqual(len(boots), 3)
        self.assertEqual([b["index"] for b in boots], [-2, -1, 0])
        self.assertEqual(boots[-1]["first_ts"], "2026-11-22 00:18:40")
        self.assertEqual(boots[-1]["duration_sec"], 1586.0)

    def test_decode_amd_reset_mask_variants(self) -> None:
        d_cf9_full = sct.decode_amd_reset_mask(0x00300800)
        self.assertEqual(d_cf9_full["reset_class"], "CF9_FULL_RESET")
        codes = {f["code"] for f in d_cf9_full["decoded_flags"]}
        self.assertIn("cf9_full_reset", codes)
        self.assertIn("acpi_power_transition", codes)

        self.assertEqual(sct.decode_amd_reset_mask(0x00080800)["reset_class"], "CF9_WARM_RESET")
        self.assertEqual(sct.decode_amd_reset_mask(0x01000800)["reset_class"], "FAILED_BOOT_TIMER")
        self.assertEqual(sct.decode_amd_reset_mask(0x00010800)["reset_class"], "THERMAL_TRIP")
        self.assertEqual(sct.decode_amd_reset_mask(0x00040800)["reset_class"], "WATCHDOG_RESET")
        self.assertEqual(sct.decode_amd_reset_mask(0x00000010)["reset_class"], "SYNC_FLOOD_HW_FAULT")
        self.assertEqual(sct.decode_amd_reset_mask(0x04000000)["reset_class"], "POWER_LOSS_OR_BUTTON")
        self.assertEqual(sct.decode_amd_reset_mask(0x00000800)["reset_class"], "UNKNOWN")

    def test_parse_amd_reset_reasons(self) -> None:
        lines = [
            "Nov 22 00:18:40 Monster kernel: x86/amd: Previous system reset reason [0x00300800]: software wrote 0xE to reset control register 0xCF9",
            "Nov 22 00:18:40 Monster kernel: x86/amd: Previous system reset reason [0x00300800]: ACPI power state transition occurred",
        ]
        res = sct.parse_amd_reset_reasons(lines)
        self.assertTrue(res["present"])
        self.assertEqual(res["raw_mask"], "0x00300800")
        self.assertEqual(res["reset_class"], "CF9_FULL_RESET")
        self.assertEqual(len(res["kernel_descriptions"]), 2)

        empty = sct.parse_amd_reset_reasons(["Nov 22 00:18:40 host kernel: booting"])
        self.assertFalse(empty["present"])
        self.assertEqual(empty["reset_class"], "NONE")


class TestLogAndArtifactInspection(unittest.TestCase):
    def test_scan_nul_crash_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "syslog"
            payload = (
                b"2026-11-22T00:13:57 Monster im-config[1]: session ok\n"
                b"2026-11-22T00:15:43 Monster kernel: audit: type=1326 noisy line\n"
                + b"\x00" * 325
                + b"2026-11-22T00:18:52 Monster kernel: Linux version 7.0.0-44-generic\n"
            )
            log.write_bytes(payload)
            scan = sct.scan_nul_crash_boundary(log)
            self.assertTrue(scan["readable"])
            self.assertTrue(scan["has_nul_gap"])
            self.assertEqual(scan["nul_runs"], 1)
            self.assertEqual(scan["last_gap_nul_count"], 325)
            self.assertIn("audit: type=1326", scan["last_raw_before_nul"])
            self.assertTrue(any("im-config" in ln for ln in scan["lines_before_nul"]))
            self.assertTrue(any("7.0.0-44-generic" in ln for ln in scan["lines_after_nul"]))

    def test_scan_nul_clean_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            clean = Path(td) / "clean.log"
            clean.write_text("2026-11-22T00:18:52 host kernel: ok\n", encoding="utf-8")
            s_clean = sct.scan_nul_crash_boundary(clean)
            self.assertTrue(s_clean["readable"])
            self.assertFalse(s_clean["has_nul_gap"])

            s_miss = sct.scan_nul_crash_boundary(Path(td) / "nonexistent.log")
            self.assertFalse(s_miss["readable"])
            self.assertIsNotNone(s_miss["error"])

    def test_detect_unclean_vs_clean_shutdown(self) -> None:
        target = [
            "Nov 22 00:18:47 Monster systemd-journald[749]: File system.journal corrupted or uncleanly shut down, renaming and replacing.",
            "Nov 22 00:18:51 Monster kernel: EXT4-fs (nvme2n1p1): 19 orphan inodes deleted",
        ]
        unc = sct.detect_unclean_signals(target, ["Nov 22 00:15:13 Monster kernel: normal"], [{"path": "/var/log/syslog", "has_nul_gap": True}])
        self.assertTrue(unc["is_unclean"])
        self.assertEqual(len(unc["journal_corrupted"]), 1)
        self.assertEqual(len(unc["fs_recovery"]), 1)
        self.assertEqual(unc["nul_gap_files"], ["/var/log/syslog"])

        clean = sct.detect_unclean_signals(
            ["Nov 22 00:18:47 Monster kernel: booted ok"],
            ["Nov 22 00:15:00 Monster systemd[1]: Reached target reboot.target - System Reboot."],
            [{"path": "/var/log/syslog", "has_nul_gap": False}],
        )
        self.assertFalse(clean["is_unclean"])
        self.assertEqual(len(clean["clean_shutdown_markers"]), 1)

    def test_scan_crash_signatures_filters_noise(self) -> None:
        prev = [
            "Nov 22 00:15:13 host kernel: audit: type=1326 audit(1:2): syscall=425",
            'Nov 22 00:15:34 host kdeconnect[1]: qt.bluetooth.bluez: No uuids found for "AA:BB"',
            "Nov 21 21:08:12 host kernel: workqueue: svm_range_deferred_list_work [amdgpu] hogged CPU for >10000us 67 times",
            "Nov 21 15:12:07 host kernel: INFO: task kworker/21:1:1088163 blocked for more than 122 seconds.",
        ]
        sigs = sct.scan_crash_signatures([], prev)
        self.assertEqual(sigs["fatal_categories"], [])
        self.assertIn("gpu_fault", sigs["context_categories"])
        self.assertIn("watchdog_lockup", sigs["context_categories"])

        panic = sct.scan_crash_signatures([], ["Nov 22 00:15:10 host kernel: Kernel panic - not syncing: Fatal exception"])
        self.assertIn("kernel_panic", panic["fatal_categories"])

    def test_inspect_kdump_config_reserved_but_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "kdump-tools"
            cfg.write_text("# comment\nUSE_KDUMP=0\nKDUMP_COREDIR=\"/var/crash\"\n", encoding="utf-8")
            res = sct.inspect_kdump_config(cfg, crashkernel_reserved=True)
            self.assertTrue(res["config_present"])
            self.assertFalse(res["enabled"])
            self.assertTrue(res["reserved_but_disabled"])

            cfg.write_text("USE_KDUMP=1\n", encoding="utf-8")
            res2 = sct.inspect_kdump_config(cfg, crashkernel_reserved=True)
            self.assertTrue(res2["enabled"])
            self.assertFalse(res2["reserved_but_disabled"])

    def test_inspect_edac_and_unattended(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mc0 = Path(td) / "mc0"
            mc0.mkdir()
            (mc0 / "ce_count").write_text("3\n", encoding="utf-8")
            (mc0 / "ue_count").write_text("1\n", encoding="utf-8")
            edac = sct.inspect_edac_counters(td)
            self.assertTrue(edac["available"])
            self.assertEqual(edac["total_ce"], 3)
            self.assertEqual(edac["total_ue"], 1)

            ulog = Path(td) / "unattended.log"
            ulog.write_text("2026-11-22 00:10:00,000 INFO Starting unattended upgrades\n", encoding="utf-8")
            u = sct.inspect_unattended_upgrades(ulog, reference_ts=dt.datetime(2026, 11, 22, 0, 18, 40))
            self.assertTrue(u["recent_activity_near_reboot"])
            u_far = sct.inspect_unattended_upgrades(ulog, reference_ts=dt.datetime(2026, 11, 22, 6, 0, 0))
            self.assertFalse(u_far["recent_activity_near_reboot"])


class TestVerdictClassifier(unittest.TestCase):
    def _base(self) -> dict:
        return {
            "amd_reset": {"present": False, "raw_mask": None, "reset_class": "NONE", "kernel_descriptions": [], "decoded_flags": []},
            "unclean": {"is_unclean": False, "journal_corrupted": [], "fs_recovery": [], "nul_gap_files": [], "clean_shutdown_markers": []},
            "signatures": {"fatal": {}, "context": {}, "fatal_categories": [], "context_categories": []},
            "kdump": {"config_present": True, "config_path": "/etc/default/kdump-tools", "use_kdump": 1, "enabled": True, "crashkernel_reserved": True, "reserved_but_disabled": False},
            "edac": {"available": False, "controllers": [], "total_ce": 0, "total_ue": 0},
            "unattended": {"readable": True, "near_reboot_lines": [], "recent_activity_near_reboot": False},
            "has_data": True,
        }

    def test_unclean_cf9_full_reset_with_kdump_misconfig(self) -> None:
        kw = self._base()
        kw["amd_reset"] = {
            "present": True,
            "raw_mask": "0x00300800",
            "reset_class": "CF9_FULL_RESET",
            "kernel_descriptions": ["software wrote 0xE to reset control register 0xCF9"],
            "decoded_flags": [],
        }
        kw["unclean"]["is_unclean"] = True
        kw["unclean"]["journal_corrupted"] = ["corrupted"]
        kw["kdump"]["enabled"] = False
        kw["kdump"]["use_kdump"] = 0
        kw["kdump"]["reserved_but_disabled"] = True
        kw["signatures"]["context"] = {"gpu_fault": ["[prev-boot] amdgpu hogged CPU"]}
        kw["signatures"]["context_categories"] = ["gpu_fault"]

        v = sct.classify_crash_verdict(**kw)
        self.assertEqual(v["status"], "UNCLEAN_HARD_RESET")
        self.assertTrue(v["is_issue"])
        self.assertEqual(v["confidence"], "MEDIUM")
        self.assertNotIn("No immediate intervention needed.", v["recommendations"])
        self.assertTrue(any("USE_KDUMP=1" in r for r in v["recommendations"]))
        self.assertTrue(any("rasdaemon" in r for r in v["recommendations"]))

    def test_all_issue_verdicts_never_emit_no_intervention(self) -> None:
        scenarios = [
            ("KERNEL_PANIC_OR_OOPS", lambda kw: kw["signatures"].update({"fatal": {"kernel_panic": ["panic"]}, "fatal_categories": ["kernel_panic"]})),
            ("HARDWARE_MCE_OR_THERMAL", lambda kw: kw["edac"].update({"total_ue": 2})),
            ("OOM_OR_LOCKUP_CRASH", lambda kw: kw["signatures"].update({"fatal": {"oom_killer": ["oom"]}, "fatal_categories": ["oom_killer"]})),
            (
                "GPU_OR_STORAGE_FAULT_RESET",
                lambda kw: (
                    kw["unclean"].update({"is_unclean": True}),
                    kw["signatures"].update({"fatal": {"gpu_fault": ["GPU reset"]}, "fatal_categories": ["gpu_fault"]}),
                ),
            ),
        ]
        for expected_status, mutate in scenarios:
            kw = self._base()
            mutate(kw)
            v = sct.classify_crash_verdict(**kw)
            self.assertEqual(v["status"], expected_status)
            self.assertTrue(v["is_issue"])
            self.assertNotIn("No immediate intervention needed.", v["recommendations"])
            self.assertTrue(len(v["recommendations"]) >= 1)

    def test_clean_reboot_and_insufficient_data(self) -> None:
        kw = self._base()
        kw["unclean"]["clean_shutdown_markers"] = ["Reached target reboot.target"]
        v = sct.classify_crash_verdict(**kw)
        self.assertEqual(v["status"], "CLEAN_REBOOT")
        self.assertFalse(v["is_issue"])
        self.assertIn("No immediate intervention needed.", v["recommendations"])

        kw2 = self._base()
        kw2["has_data"] = False
        v2 = sct.classify_crash_verdict(**kw2)
        self.assertEqual(v2["status"], "INSUFFICIENT_DATA")
        self.assertFalse(v2["is_issue"])


class TestCLIAndRenderers(unittest.TestCase):
    def test_cli_validation_errors(self) -> None:
        r = subprocess.run([sys.executable, str(SCRIPT), "--boot", "1"], capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("--boot must be <= 0", r.stderr)

        r2 = subprocess.run([sys.executable, str(SCRIPT), "--boots-history", "99"], capture_output=True, text=True, check=False)
        self.assertEqual(r2.returncode, 1)
        self.assertIn("--boots-history", r2.stderr)

        r3 = subprocess.run([sys.executable, str(SCRIPT), "--json", "--markdown"], capture_output=True, text=True, check=False)
        self.assertEqual(r3.returncode, 2)

    def test_main_exit_on_issue_and_renderers(self) -> None:
        fake_report = {
            "timestamp": "2026-11-22T09:58:00-05:00",
            "target_boot_index": 0,
            "system": {
                "hostname": "testbox",
                "uptime_sec": 120.0,
                "uptime_human": "2m 00s",
                "kernel_release": "7.0.0-44-generic",
                "crashkernel_param": "128G-:4096M",
                "crashkernel_reserved": True,
            },
            "verdict": {
                "status": "UNCLEAN_HARD_RESET",
                "is_issue": True,
                "confidence": "MEDIUM",
                "summary": "Abrupt reset via 0xCF9=0xE.",
                "proven_facts": ["AMD FCH reset register [0x00300800]"],
                "circumstantial_signals": [],
                "missing_telemetry": ["kdump disabled"],
                "recommendations": ["Enable USE_KDUMP=1"],
            },
            "transition": {
                "previous_boot": {"index": -1, "first_ts": "2026-11-03 08:02:32", "last_ts": "2026-11-22 00:15:13", "duration_human": "18d 16h 12m"},
                "target_boot": {"index": 0, "first_ts": "2026-11-22 00:18:40", "last_ts": "2026-11-22 00:45:06", "duration_human": "26m 26s"},
                "downtime_sec": 207.0,
                "downtime_human": "3m 27s",
                "previous_kernel": "7.0.0-42-generic",
                "target_kernel": "7.0.0-44-generic",
                "kernel_changed": True,
            },
            "boots_history": [],
            "hardware_reset": {"present": True, "raw_mask": "0x00300800", "reset_class": "CF9_FULL_RESET", "kernel_descriptions": ["software wrote 0xE to reset control register 0xCF9"], "decoded_flags": []},
            "unclean_evidence": {"is_unclean": True, "journal_corrupted": ["corrupted"], "fs_recovery": [], "nul_gap_files": ["/var/log/syslog"], "clean_shutdown_markers": []},
            "nul_log_scans": [{"path": "/var/log/syslog", "has_nul_gap": True, "last_gap_nul_count": 325, "last_gap_offset": 83517, "last_raw_before_nul": "last line"}],
            "signatures": {"fatal": {}, "context": {}, "fatal_categories": [], "context_categories": []},
            "previous_boot_tail": [],
            "crash_capture": {
                "kdump": {"enabled": False, "use_kdump": 0, "crashkernel_reserved": True, "reserved_but_disabled": True},
                "artifacts": {"systemd_pstore": {"total_entries": 0, "recent_entries": []}, "var_crash": {"total_entries": 0, "recent_entries": []}},
                "edac": {"available": False, "total_ce": 0, "total_ue": 0},
                "unattended_upgrades": {"readable": True, "near_reboot_lines": [], "recent_activity_near_reboot": False},
            },
            "warnings": [],
            "manual_steps": list(sct.MANUAL_STEPS),
        }

        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "out.jsonl"
            buf = io.StringIO()
            with mock.patch.object(sct, "investigate_crash", return_value=fake_report), mock.patch.object(sys, "stdout", buf):
                rc = sct.main(["--exit-on-issue", "--show-steps", "--append-log", str(log_path)])
            self.assertEqual(rc, 3)
            out = buf.getvalue()
            self.assertIn("UNCLEAN_HARD_RESET", out)
            self.assertIn("Step-by-Step Manual Troubleshooting Commands", out)
            self.assertTrue(log_path.exists())
            parsed_line = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual(parsed_line["verdict"]["status"], "UNCLEAN_HARD_RESET")

            md = sct.render_markdown(fake_report, show_steps=True)
            self.assertIn("# Crash & Reboot Triage @", md)
            self.assertIn("## Step-by-Step Manual Commands", md)


if __name__ == "__main__":
    unittest.main()
