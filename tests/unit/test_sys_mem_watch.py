#!/usr/bin/env python3
"""Unit and CLI error-path tests for sysadmin_tools.sys_mem_watch."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
SCRIPT = SRC_DIR / "sysadmin_tools" / "sys_mem_watch.py"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sysadmin_tools import sys_mem_watch as smw  # noqa: E402


class TestParsersAndHelpers(unittest.TestCase):
    def test_parse_meminfo(self) -> None:
        sample = "\n".join(
            [
                "MemTotal:       16777216 kB",
                "MemFree:         2097152 kB",
                "MemAvailable:    8388608 kB",
                "Cached:          4194304 kB",
                "Dirty:            524288 kB",
                "SReclaimable:    1048576 kB",
                "SUnreclaim:       262144 kB",
                "SwapTotal:       4194304 kB",
                "SwapFree:        1048576 kB",
            ]
        )
        m = smw.parse_meminfo(sample)
        self.assertEqual(m["mem_total_gib"], 16.0)
        self.assertEqual(m["mem_avail_gib"], 8.0)
        self.assertEqual(m["mem_avail_pct"], 50.0)
        self.assertEqual(m["mem_free_gib"], 2.0)
        self.assertEqual(m["cached_gib"], 4.0)
        self.assertEqual(m["dirty_gib"], 0.5)
        self.assertEqual(m["slab_reclaimable_gib"], 1.0)
        self.assertEqual(m["slab_unreclaimable_gib"], 0.25)
        self.assertEqual(m["swap_total_gib"], 4.0)
        self.assertEqual(m["swap_used_gib"], 3.0)
        self.assertEqual(m["swap_used_pct"], 75.0)

    def test_parse_meminfo_empty(self) -> None:
        m = smw.parse_meminfo("")
        self.assertEqual(m["mem_total_gib"], 0.0)
        self.assertEqual(m["mem_avail_pct"], 0.0)
        self.assertEqual(m["swap_used_pct"], 0.0)

    def test_parse_vmstat_and_deltas(self) -> None:
        raw0 = "\n".join(
            [
                "nr_free_pages 12345",
                "pswpin 100",
                "pswpout 200",
                "pgscan_kswapd 5000",
                "pgscan_direct 10",
                "pgsteal_kswapd 4000",
                "pgsteal_direct 5",
                "oom_kill 1",
                "bad_line_no_value",
            ]
        )
        raw1 = "\n".join(
            [
                "pswpin 150",
                "pswpout 500",
                "pgscan_kswapd 5600",
                "pgscan_direct 80",
                "pgsteal_kswapd 4500",
                "pgsteal_direct 2",  # lower than v0 -> clamped to 0
                "oom_kill 2",
            ]
        )
        v0 = smw.parse_vmstat(raw0)
        v1 = smw.parse_vmstat(raw1)
        self.assertEqual(v0["pswpin"], 100)
        self.assertEqual(v0["oom_kill"], 1)
        d = smw.compute_vmstat_deltas(v0, v1)
        self.assertEqual(d["swap_in_pages"], 50)
        self.assertEqual(d["swap_out_pages"], 300)
        self.assertEqual(d["pgscan_kswapd_delta"], 600)
        self.assertEqual(d["pgscan_direct_delta"], 70)
        self.assertEqual(d["pgsteal_kswapd_delta"], 500)
        self.assertEqual(d["pgsteal_direct_delta"], 0)
        self.assertEqual(d["oom_kill_delta"], 1)

    def test_parse_psi_memory(self) -> None:
        txt = (
            "some avg10=12.34 avg60=5.67 avg300=1.23 total=99999\n"
            "full avg10=4.50 avg60=2.10 avg300=0.50 total=12345\n"
        )
        psi = smw.parse_psi_memory(txt)
        self.assertEqual(psi["some"], {"avg10": 12.34, "avg60": 5.67, "avg300": 1.23})
        self.assertEqual(psi["full"], {"avg10": 4.5, "avg60": 2.1, "avg300": 0.5})

    def test_parse_proc_stat_spaces_and_parens(self) -> None:
        fields = ["S", "123"] + ["0"] * 17 + ["45678"]
        line = f"999 (my (weird) proc) {' '.join(fields)}"
        parsed = smw.parse_proc_stat(line)
        self.assertEqual(parsed, ("my (weird) proc", "S", 123, 45678))
        self.assertIsNone(smw.parse_proc_stat("malformed line"))

    def test_parse_proc_status(self) -> None:
        txt = "\n".join(
            [
                "Name:\tpython3",
                "State:\tS (sleeping)",
                "Pid:\t4242",
                "VmPeak:\t  500000 kB",
                "VmRSS:\t  102400 kB",
                "RssAnon:\t   81920 kB",
                "RssFile:\t   16384 kB",
                "RssShmem:\t    4096 kB",
                "VmSwap:\t    8192 kB",
                "Threads:\t6",
            ]
        )
        st = smw.parse_proc_status(txt)
        self.assertEqual(st["rss_kb"], 102400)
        self.assertEqual(st["anon_kb"], 81920)
        self.assertEqual(st["file_kb"], 16384)
        self.assertEqual(st["shmem_kb"], 4096)
        self.assertEqual(st["swap_kb"], 8192)
        self.assertEqual(st["threads"], 6)

        kthread = smw.parse_proc_status("Name:\tkthreadd\nThreads:\t1\n")
        self.assertEqual(kthread["rss_kb"], 0)
        self.assertEqual(kthread["swap_kb"], 0)
        self.assertEqual(kthread["threads"], 1)

    def test_parse_cgroup_v2_v1_and_edge_cases(self) -> None:
        v2 = "0::/user.slice/user-1000.slice/session-2.scope\n"
        self.assertEqual(smw.parse_cgroup(v2), "/user.slice/user-1000.slice/session-2.scope")

        v1 = "\n".join(
            [
                "12:cpuset:/",
                "8:memory:/docker/abc123def456",
                "4:cpu,cpuacct:/system.slice/docker.service",
            ]
        )
        self.assertEqual(smw.parse_cgroup(v1), "/docker/abc123def456")

        hybrid = "\n".join(
            [
                "5:memory:/old-v1-mem",
                "0::/system.slice/worker.service",
            ]
        )
        self.assertEqual(smw.parse_cgroup(hybrid), "/system.slice/worker.service")

        self.assertEqual(smw.parse_cgroup("0::/\n"), "/")
        self.assertEqual(smw.parse_cgroup(""), "-")
        self.assertEqual(smw.parse_cgroup("not-valid-cgroup"), "-")

    def test_append_jsonl_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "nested" / "mem.jsonl"
            smw.append_jsonl_log(log_path, {"b": 2, "a": 1})
            smw.append_jsonl_log(log_path, {"z": 9, "c": 3})
            lines = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0], '{"a":1,"b":2}')
            self.assertEqual(json.loads(lines[1]), {"c": 3, "z": 9})

    def test_watch_memory_only_reads_cgroup_for_top_rows(self) -> None:
        # 5 processes, top_n=2 -> only PIDs landing in top sections should invoke read_proc_cgroup.
        p0 = {
            10: smw.ProcMemSnap(10, 1, "big", 100, 500_000, 450_000, 50_000, 0, 10_000, 4, 0, 0, ""),
            20: smw.ProcMemSnap(20, 1, "grower", 100, 200_000, 200_000, 0, 0, 0, 2, 0, 0, ""),
            30: smw.ProcMemSnap(30, 1, "swapper", 100, 50_000, 50_000, 0, 0, 300_000, 1, 0, 0, ""),
            40: smw.ProcMemSnap(40, 1, "small1", 100, 10_000, 5_000, 5_000, 0, 0, 1, 0, 0, ""),
            50: smw.ProcMemSnap(50, 1, "small2", 100, 8_000, 4_000, 4_000, 0, 0, 1, 0, 0, ""),
        }
        p1 = {
            10: smw.ProcMemSnap(10, 1, "big", 100, 500_000, 450_000, 50_000, 0, 10_000, 4, 700, 0, "big --run"),
            20: smw.ProcMemSnap(20, 1, "grower", 100, 260_000, 260_000, 0, 0, 0, 2, 400, 0, "grower --leak"),
            30: smw.ProcMemSnap(30, 1, "swapper", 100, 50_000, 50_000, 0, 0, 310_000, 1, 200, 0, "swapper"),
            40: smw.ProcMemSnap(40, 1, "small1", 100, 10_000, 5_000, 5_000, 0, 0, 1, 5, 0, "small1"),
            50: smw.ProcMemSnap(50, 1, "small2", 100, 8_000, 4_000, 4_000, 0, 0, 1, 2, 0, "small2"),
        }
        base_vm = {k: 0 for k in smw.VMSTAT_KEYS}
        base_mem = {
            "mem_total_gib": 64.0,
            "mem_avail_gib": 40.0,
            "mem_avail_pct": 62.5,
            "mem_free_gib": 20.0,
            "cached_gib": 18.0,
            "dirty_gib": 0.1,
            "slab_reclaimable_gib": 1.0,
            "slab_unreclaimable_gib": 0.5,
            "swap_total_gib": 8.0,
            "swap_used_gib": 1.0,
            "swap_used_pct": 12.5,
        }
        with (
            mock.patch.object(smw, "snapshot_procs", side_effect=[p0, p1]),
            mock.patch.object(smw, "read_vmstat", side_effect=[base_vm, base_vm]),
            mock.patch.object(smw, "read_meminfo", return_value=base_mem),
            mock.patch.object(smw, "read_psi_memory", return_value={}),
            mock.patch.object(smw, "read_uptime_sec", return_value=1000.0),
            mock.patch.object(smw.time, "sleep", return_value=None),
            mock.patch.object(smw.time, "monotonic", side_effect=[10.0, 12.0]),
            mock.patch.object(smw, "read_proc_cgroup", side_effect=lambda pid, proc_root="/proc": f"/cg/{pid}") as m_cg,
        ):
            rep = smw.watch_memory(interval=2.0, top_n=2)

        queried_pids = {c.args[0] for c in m_cg.call_args_list}
        self.assertEqual(queried_pids, {10, 20, 30})
        self.assertNotIn(40, queried_pids)
        self.assertNotIn(50, queried_pids)
        self.assertEqual([r["pid"] for r in rep["top_rss"]], [10, 20])
        self.assertEqual([r["pid"] for r in rep["top_growers"]], [20])
        self.assertEqual([r["pid"] for r in rep["top_swap"]], [30, 10])
        self.assertEqual([r["pid"] for r in rep["top_oom_risk"]], [10, 20])
        self.assertEqual(rep["top_growers"][0]["cgroup"], "/cg/20")


class TestMemoryClassifier(unittest.TestCase):
    @staticmethod
    def _base_mem(**overrides: float | int) -> dict[str, float | int]:
        mem: dict[str, float | int] = {
            "mem_total_gib": 64.0,
            "mem_avail_gib": 40.0,
            "mem_avail_pct": 62.5,
            "mem_free_gib": 20.0,
            "cached_gib": 18.0,
            "dirty_gib": 0.1,
            "slab_reclaimable_gib": 1.0,
            "slab_unreclaimable_gib": 0.5,
            "swap_total_gib": 8.0,
            "swap_used_gib": 0.5,
            "swap_used_pct": 6.2,
            "swap_in_pages": 0,
            "swap_out_pages": 0,
            "pgscan_direct_delta": 0,
            "pgscan_kswapd_delta": 0,
            "pgsteal_direct_delta": 0,
            "pgsteal_kswapd_delta": 0,
            "oom_kill_delta": 0,
        }
        mem.update(overrides)
        return mem

    def test_verdict_oom_imminent_from_oom_kill_and_low_mem(self) -> None:
        oom_proc = [{"pid": 1234, "comm": "python3", "rss_mib": 48000.0, "oom_score": 880, "oom_score_adj": 0}]
        v1 = smw.classify_memory_verdict(
            mem=self._base_mem(oom_kill_delta=1, mem_avail_gib=2.0, mem_avail_pct=3.1),
            psi={"some": {"avg10": 45.0}, "full": {"avg10": 30.0}},
            top_rss=oom_proc,
            top_growers=[],
            top_swap=[],
            top_oom_risk=oom_proc,
        )
        self.assertEqual(v1["status"], "OOM_IMMINENT")
        self.assertTrue(v1["is_issue"])
        self.assertNotIn("No immediate intervention needed.", v1["recommendations"])
        self.assertTrue(any("1234:python3" in r for r in v1["recommendations"]))
        self.assertTrue(any("dmesg" in r for r in v1["recommendations"]))

        v2 = smw.classify_memory_verdict(
            mem=self._base_mem(mem_avail_gib=0.35, mem_avail_pct=0.5),
            psi={"some": {"avg10": 15.0}, "full": {"avg10": 8.0}},
            top_rss=oom_proc,
            top_growers=[],
            top_swap=[],
            top_oom_risk=oom_proc,
        )
        self.assertEqual(v2["status"], "OOM_IMMINENT")
        self.assertTrue(v2["is_issue"])

    def test_verdict_swap_thrashing(self) -> None:
        swap_proc = [{"pid": 2222, "comm": "java", "rss_mib": 12000.0, "swap_mib": 4500.0}]
        v = smw.classify_memory_verdict(
            mem=self._base_mem(swap_in_pages=800, swap_out_pages=700, swap_used_pct=88.0, mem_avail_gib=6.0, mem_avail_pct=9.4),
            psi={"some": {"avg10": 18.0}, "full": {"avg10": 7.0}},
            top_rss=swap_proc,
            top_growers=[],
            top_swap=swap_proc,
            top_oom_risk=swap_proc,
        )
        self.assertEqual(v["status"], "SWAP_THRASHING")
        self.assertTrue(v["is_issue"])
        self.assertNotIn("No immediate intervention needed.", v["recommendations"])
        self.assertTrue(any("2222:java" in r for r in v["recommendations"]))

    def test_verdict_rapid_growth_leak_risk(self) -> None:
        grower = [{"pid": 3333, "comm": "node", "rss_mib": 3200.0, "rss_delta_mib": 160.0, "rss_rate_mibs": 80.0}]
        v = smw.classify_memory_verdict(
            mem=self._base_mem(),
            psi={"some": {"avg10": 0.0}, "full": {"avg10": 0.0}},
            top_rss=grower,
            top_growers=grower,
            top_swap=[],
            top_oom_risk=grower,
        )
        self.assertEqual(v["status"], "RAPID_GROWTH_LEAK_RISK")
        self.assertTrue(v["is_issue"])
        self.assertNotIn("No immediate intervention needed.", v["recommendations"])
        self.assertTrue(any("3333:node" in r for r in v["recommendations"]))

    def test_verdict_high_memory_pressure(self) -> None:
        heavy = [{"pid": 4444, "comm": "rustc", "rss_mib": 14000.0, "oom_score": 320}]
        v = smw.classify_memory_verdict(
            mem=self._base_mem(mem_avail_gib=7.5, mem_avail_pct=11.7, pgscan_direct_delta=2500),
            psi={"some": {"avg10": 14.2}, "full": {"avg10": 6.1}},
            top_rss=heavy,
            top_growers=[],
            top_swap=[],
            top_oom_risk=heavy,
        )
        self.assertEqual(v["status"], "HIGH_MEMORY_PRESSURE")
        self.assertTrue(v["is_issue"])
        self.assertNotIn("No immediate intervention needed.", v["recommendations"])
        self.assertTrue(any("4444:rustc" in r for r in v["recommendations"]))

    def test_verdict_healthy_even_with_stale_swap_usage(self) -> None:
        v = smw.classify_memory_verdict(
            mem=self._base_mem(swap_used_pct=99.8, swap_in_pages=0, swap_out_pages=0),
            psi={"some": {"avg10": 0.0}, "full": {"avg10": 0.0}},
            top_rss=[{"pid": 5555, "comm": "firefox", "rss_mib": 2000.0}],
            top_growers=[],
            top_swap=[],
            top_oom_risk=[],
        )
        self.assertEqual(v["status"], "HEALTHY")
        self.assertFalse(v["is_issue"])
        self.assertEqual(v["recommendations"], ["No immediate intervention needed."])

    def test_issue_recommendation_invariant_even_with_empty_process_lists(self) -> None:
        cases = [
            ("OOM_IMMINENT", self._base_mem(oom_kill_delta=1), {}),
            ("SWAP_THRASHING", self._base_mem(swap_in_pages=1200, swap_out_pages=400), {}),
            ("HIGH_MEMORY_PRESSURE", self._base_mem(pgscan_direct_delta=2000), {"some": {"avg10": 15.0}}),
        ]
        for expected, mem, psi in cases:
            v = smw.classify_memory_verdict(mem=mem, psi=psi, top_rss=[], top_growers=[], top_swap=[], top_oom_risk=[])
            self.assertEqual(v["status"], expected)
            self.assertTrue(v["is_issue"])
            self.assertTrue(v["recommendations"])
            self.assertNotIn("No immediate intervention needed.", v["recommendations"])


class TestCLIAndExitCodes(unittest.TestCase):
    def test_help(self) -> None:
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0)
        for flag in ("--interval", "--top", "--json", "--markdown", "--watch", "--append-log", "--exit-on-issue", "--verbose"):
            self.assertIn(flag, r.stdout)

    def test_invalid_interval_fails_cleanly(self) -> None:
        for bad in ("0.01", "61.0"):
            r = subprocess.run([sys.executable, str(SCRIPT), "--interval", bad], capture_output=True, text=True, check=False)
            self.assertEqual(r.returncode, 1)
            self.assertIn("error:", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_invalid_top_and_watch_fail_cleanly(self) -> None:
        for args in (["--top", "0"], ["--top", "101"], ["--watch", "-1"]):
            r = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False)
            self.assertEqual(r.returncode, 1)
            self.assertIn("error:", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_mutually_exclusive_format_exits_2(self) -> None:
        r = subprocess.run([sys.executable, str(SCRIPT), "--json", "--markdown"], capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)

    def test_invalid_env_var_exits_1_without_traceback(self) -> None:
        env = dict(os.environ)
        env["SYS_MEM_WATCH_INTERVAL"] = "not-a-float"
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("error:", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_exit_on_issue_returns_3_when_issue(self) -> None:
        fake_issue_rep = {
            "timestamp": "2026-11-21T21:00:00-05:00",
            "sample_sec": 0.2,
            "verdict": {
                "status": "HIGH_MEMORY_PRESSURE",
                "is_issue": True,
                "reasons": ["Elevated memory pressure"],
                "recommendations": ["Inspect 100:python3"],
            },
            "memory": TestMemoryClassifier._base_mem(mem_avail_pct=10.0),
            "psi": {"some": {"avg10": 15.0, "avg60": 5.0, "avg300": 1.0}, "full": {"avg10": 6.0, "avg60": 2.0, "avg300": 0.5}},
            "turnover": {"started": 0, "exited": 0, "total_procs": 100},
            "top_rss": [],
            "top_growers": [],
            "top_swap": [],
            "top_oom_risk": [],
        }
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with (
            mock.patch.object(smw, "watch_memory", return_value=fake_issue_rep),
            mock.patch.object(sys, "stdout", buf_out),
            mock.patch.object(sys, "stderr", buf_err),
        ):
            rc = smw.main(["--interval", "0.2", "--json", "--exit-on-issue", "--verbose"])
        self.assertEqual(rc, 3)
        self.assertIn("config:", buf_err.getvalue())
        self.assertEqual(json.loads(buf_out.getvalue())["verdict"]["status"], "HIGH_MEMORY_PRESSURE")


if __name__ == "__main__":
    unittest.main()
