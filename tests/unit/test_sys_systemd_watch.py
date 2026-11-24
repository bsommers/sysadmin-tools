#!/usr/bin/env python3
"""Unit and CLI error-path tests for sysadmin_tools.sys_systemd_watch."""

from __future__ import annotations

import ast
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
SCRIPT = SRC_DIR / "sysadmin_tools" / "sys_systemd_watch.py"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sysadmin_tools import sys_systemd_watch as ssw  # noqa: E402


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_proc_stat(pid: int, comm: str, state: str, ppid: int, starttime_ticks: int) -> str:
    # fields after ') ': state(0), ppid(1), ..., starttime(19)
    mid = ["0"] * 17
    return f"{pid} ({comm}) {state} {ppid} {' '.join(mid)} {starttime_ticks}\n"


def _create_unit(
    root: Path,
    rel_path: str,
    *,
    mem_current: int | None = 10 * 1024 * 1024,
    mem_peak: int | None = 20 * 1024 * 1024,
    mem_high: str | None = "max",
    mem_max: str | None = "max",
    mem_events: dict[str, int] | None = None,
    mem_psi: str | None = "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
    cpu_stat: dict[str, int] | None = None,
    cpu_psi: str | None = "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
    io_stat: str | None = "259:0 rbytes=1024 wbytes=2048 rios=1 wios=2 dbytes=0 dios=0\n",
    io_psi: str | None = "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
    pids_current: int | None = 1,
    pids_max: str | None = "max",
    procs: list[int] | None = None,
) -> Path:
    udir = root / rel_path.lstrip("/")
    udir.mkdir(parents=True, exist_ok=True)
    if mem_current is not None:
        _write(udir / "memory.current", f"{mem_current}\n")
    if mem_peak is not None:
        _write(udir / "memory.peak", f"{mem_peak}\n")
    if mem_high is not None:
        _write(udir / "memory.high", f"{mem_high}\n")
    if mem_max is not None:
        _write(udir / "memory.max", f"{mem_max}\n")
    if mem_events is not None:
        lines = [f"{k} {v}" for k, v in mem_events.items()]
        _write(udir / "memory.events", "\n".join(lines) + "\n")
    else:
        _write(udir / "memory.events", "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
    if mem_psi is not None:
        _write(udir / "memory.pressure", mem_psi)
    if cpu_stat is not None:
        lines = [f"{k} {v}" for k, v in cpu_stat.items()]
        _write(udir / "cpu.stat", "\n".join(lines) + "\n")
    else:
        _write(
            udir / "cpu.stat",
            "usage_usec 100000\nuser_usec 80000\nsystem_usec 20000\nnr_periods 10\nnr_throttled 0\nthrottled_usec 0\n",
        )
    if cpu_psi is not None:
        _write(udir / "cpu.pressure", cpu_psi)
    if io_stat is not None:
        _write(udir / "io.stat", io_stat)
    if io_psi is not None:
        _write(udir / "io.pressure", io_psi)
    if pids_current is not None:
        _write(udir / "pids.current", f"{pids_current}\n")
    if pids_max is not None:
        _write(udir / "pids.max", f"{pids_max}\n")
    if procs is not None:
        _write(udir / "cgroup.procs", "\n".join(str(p) for p in procs) + ("\n" if procs else ""))
    return udir


class TestParsersAndStaticInvariants(unittest.TestCase):
    def test_zero_subprocess_and_standalone_imports(self) -> None:
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name, "subprocess")
                    self.assertFalse(alias.name.startswith("sysadmin_tools"))
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                self.assertNotEqual(mod, "subprocess")
                self.assertFalse(mod.startswith("sysadmin_tools"))

    def test_parse_cgroup_int(self) -> None:
        self.assertEqual(ssw.parse_cgroup_int("4096\n"), 4096)
        self.assertEqual(ssw.parse_cgroup_int("0"), 0)
        self.assertIsNone(ssw.parse_cgroup_int("max\n"))
        self.assertIsNone(ssw.parse_cgroup_int(""))
        self.assertIsNone(ssw.parse_cgroup_int(None))
        self.assertIsNone(ssw.parse_cgroup_int("not-a-number"))

    def test_parse_psi_and_io_stat(self) -> None:
        psi = ssw.parse_psi_file(
            "some avg10=12.50 avg60=5.25 avg300=1.00 total=987654\n"
            "full avg10=3.40 avg60=1.20 avg300=0.50 total=123456\n"
        )
        self.assertEqual(psi["some"]["avg10"], 12.5)
        self.assertEqual(psi["some"]["total"], 987654)
        self.assertEqual(psi["full"]["avg10"], 3.4)
        self.assertEqual(psi["full"]["total"], 123456)

        io_s = ssw.parse_io_stat(
            "259:3 rbytes=1000 wbytes=2000 rios=10 wios=20 dbytes=0 dios=0\n"
            "259:6 rbytes=500 wbytes=3000 rios=5 wios=30 dbytes=100 dios=1\n"
        )
        self.assertEqual(io_s["rbytes"], 1500)
        self.assertEqual(io_s["wbytes"], 5000)
        self.assertEqual(io_s["rios"], 15)
        self.assertEqual(io_s["wios"], 50)
        self.assertEqual(io_s["dbytes"], 100)
        self.assertEqual(io_s["dios"], 1)

    def test_parse_proc_stat_with_parens_and_spaces(self) -> None:
        line = _make_proc_stat(4242, "kworker (weird 1)", "D", 2, 123456)
        self.assertEqual(ssw.parse_proc_stat(line), ("kworker (weird 1)", "D", 2, 123456))
        self.assertIsNone(ssw.parse_proc_stat("malformed"))

    def test_parse_proc_cgroup_v2(self) -> None:
        self.assertEqual(ssw.parse_proc_cgroup_v2("0::/system.slice/docker.service\n"), "/system.slice/docker.service")
        self.assertEqual(ssw.parse_proc_cgroup_v2("1:name=systemd:/foo\n0::/init.scope\n"), "/init.scope")
        self.assertIsNone(ssw.parse_proc_cgroup_v2("1:memory:/legacy\n"))

    def test_matches_unit_filter(self) -> None:
        self.assertTrue(ssw.matches_unit_filter("docker.service", "/system.slice/docker.service", None))
        self.assertTrue(ssw.matches_unit_filter("docker.service", "/system.slice/docker.service", "dock"))
        self.assertTrue(ssw.matches_unit_filter("docker.service", "/system.slice/docker.service", "*.service"))
        self.assertFalse(ssw.matches_unit_filter("docker.service", "/system.slice/docker.service", "nginx"))
        self.assertFalse(ssw.matches_unit_filter("init.scope", "/init.scope", "*.service"))


class TestFakeCgroupScenarios(unittest.TestCase):
    def test_healthy_baseline_and_missing_files_graceful(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cg = base / "cgroup"
            proc = base / "proc"
            jdir = base / "journal"
            jdir.mkdir()
            _write(proc / "uptime", "5000.00 10000.00\n")
            _write(proc / "101" / "stat", _make_proc_stat(101, "healthy-daemon", "S", 1, 1000))
            _write(proc / "101" / "cgroup", "0::/system.slice/healthy.service\n")

            _create_unit(cg, "/system.slice", procs=[])
            _create_unit(cg, "/system.slice/healthy.service", procs=[101])
            # Completely empty service dir (all cgroup files missing) must not crash.
            (cg / "system.slice" / "minimal.service").mkdir(parents=True, exist_ok=True)

            rep = ssw.watch_systemd(
                interval=0.05,
                samples=2,
                top_n=10,
                cgroup_root=cg,
                proc_root=proc,
                journal_dirs=[jdir],
            )
            self.assertEqual(rep["verdict"]["status"], "HEALTHY")
            self.assertFalse(rep["verdict"]["is_issue"])
            self.assertIn("No immediate intervention needed.", rep["verdict"]["recommendations"])
            self.assertEqual(rep["summary"]["services"], 2)
            self.assertEqual(rep["summary"]["slices"], 1)

    def test_oom_events_and_memory_pressure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cg = base / "cgroup"
            proc = base / "proc"
            proc.mkdir()
            _write(proc / "uptime", "5000.00 10000.00\n")

            _create_unit(
                cg,
                "/system.slice/leaky.service",
                mem_current=980 * 1024 * 1024,
                mem_max=str(1024 * 1024 * 1024),
                mem_events={"low": 0, "high": 4, "max": 2, "oom": 1, "oom_kill": 2},
                mem_psi="some avg10=28.50 avg60=12.00 avg300=4.00 total=500000\nfull avg10=12.00 avg60=5.00 avg300=1.00 total=250000\n",
                procs=[201],
            )

            rep = ssw.watch_systemd(
                interval=0.05,
                samples=1,
                cgroup_root=cg,
                proc_root=proc,
                journal_dirs=[base / "missing-journal"],
            )
            self.assertEqual(rep["verdict"]["status"], "MEMORY_PRESSURE")
            self.assertTrue(rep["verdict"]["is_issue"])
            self.assertNotIn("No immediate intervention needed.", rep["verdict"]["recommendations"])
            self.assertFalse(rep["journal"]["readable"])
            self.assertEqual(rep["summary"]["units_memory_pressure"], 1)

    def test_cpu_throttling_delta(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cg = base / "cgroup"
            proc = base / "proc"
            proc.mkdir()
            _write(proc / "uptime", "5000.00 10000.00\n")

            udir = _create_unit(
                cg,
                "/system.slice/worker.service",
                cpu_stat={
                    "usage_usec": 1_000_000,
                    "user_usec": 900_000,
                    "system_usec": 100_000,
                    "nr_periods": 100,
                    "nr_throttled": 10,
                    "throttled_usec": 100_000,
                },
                procs=[301],
            )

            def mutate_on_sleep(_sec: float) -> None:
                _write(
                    udir / "cpu.stat",
                    "\n".join(
                        [
                            "usage_usec 1500000",
                            "user_usec 1350000",
                            "system_usec 150000",
                            "nr_periods 110",
                            "nr_throttled 18",
                            "throttled_usec 450000",
                        ]
                    )
                    + "\n",
                )

            with mock.patch.object(ssw.time, "sleep", side_effect=mutate_on_sleep):
                with mock.patch.object(ssw.time, "monotonic", side_effect=[100.0, 101.0]):
                    rep = ssw.watch_systemd(
                        interval=1.0,
                        samples=2,
                        cgroup_root=cg,
                        proc_root=proc,
                        journal_dirs=[],
                    )

            self.assertEqual(rep["verdict"]["status"], "CPU_THROTTLING")
            self.assertTrue(rep["verdict"]["is_issue"])
            self.assertNotIn("No immediate intervention needed.", rep["verdict"]["recommendations"])
            u = rep["top_cpu"][0]
            self.assertEqual(u["unit"], "worker.service")
            self.assertEqual(u["cpu_nr_throttled_delta"], 8)
            self.assertAlmostEqual(u["cpu_throttled_pct"], 35.0, places=1)
            self.assertAlmostEqual(u["cpu_pct"], 50.0, places=1)

    def test_io_pressure_and_d_state_pids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cg = base / "cgroup"
            proc = base / "proc"
            _write(proc / "uptime", "5000.00 10000.00\n")
            for pid in (401, 402, 403, 404):
                _write(proc / str(pid) / "stat", _make_proc_stat(pid, "db-writer", "D", 1, 1000))
                _write(proc / str(pid) / "cgroup", "0::/system.slice/postgres.service\n")

            udir = _create_unit(
                cg,
                "/system.slice/postgres.service",
                io_stat="259:0 rbytes=1048576 wbytes=2097152 rios=10 wios=20 dbytes=0 dios=0\n",
                io_psi="some avg10=42.00 avg60=25.00 avg300=10.00 total=1000000\nfull avg10=18.50 avg60=9.00 avg300=4.00 total=500000\n",
                procs=[401, 402, 403, 404],
            )

            def mutate_io(_sec: float) -> None:
                _write(
                    udir / "io.stat",
                    "259:0 rbytes=11534336 wbytes=54525952 rios=210 wios=820 dbytes=0 dios=0\n",
                )

            with mock.patch.object(ssw.time, "sleep", side_effect=mutate_io):
                with mock.patch.object(ssw.time, "monotonic", side_effect=[200.0, 201.0]):
                    rep = ssw.watch_systemd(
                        interval=1.0,
                        samples=2,
                        cgroup_root=cg,
                        proc_root=proc,
                        journal_dirs=[],
                    )

            self.assertEqual(rep["verdict"]["status"], "IO_PRESSURE")
            self.assertTrue(rep["verdict"]["is_issue"])
            self.assertNotIn("No immediate intervention needed.", rep["verdict"]["recommendations"])
            self.assertEqual(rep["summary"]["total_d_state_pids"], 4)
            self.assertGreater(rep["top_io"][0]["io_write_mibs"], 40.0)

    def test_crash_loop_pid_replacement_and_scope_non_false_positive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cg = base / "cgroup"
            proc = base / "proc"
            _write(proc / "uptime", "5000.00 10000.00\n")
            _write(proc / "501" / "stat", _make_proc_stat(501, "flaky-svc", "S", 1, int(4999 * ssw.CLK_TCK)))
            _write(proc / "501" / "cgroup", "0::/system.slice/flaky.service\n")

            svc_dir = _create_unit(cg, "/system.slice/flaky.service", procs=[501])
            scope_dir = _create_unit(cg, "/user.slice/session-1.scope", procs=[601])

            def rotate_pids(_sec: float) -> None:
                # Service PID completely replaced (501 -> 502) and zombie 503 present
                (proc / "501" / "stat").unlink()
                (proc / "501" / "cgroup").unlink()
                _write(proc / "502" / "stat", _make_proc_stat(502, "flaky-svc", "S", 1, int(4999.8 * ssw.CLK_TCK)))
                _write(proc / "502" / "cgroup", "0::/system.slice/flaky.service\n")
                _write(proc / "503" / "stat", _make_proc_stat(503, "flaky-child", "Z", 502, int(4999.5 * ssw.CLK_TCK)))
                _write(proc / "503" / "cgroup", "0::/system.slice/flaky.service\n")
                _write(svc_dir / "cgroup.procs", "502\n503\n")
                # Scope PID also changes; scopes should NOT be flagged as crash-looping services.
                _write(scope_dir / "cgroup.procs", "602\n")

            with mock.patch.object(ssw.time, "sleep", side_effect=rotate_pids):
                with mock.patch.object(ssw.time, "monotonic", side_effect=[300.0, 301.0]):
                    rep = ssw.watch_systemd(
                        interval=1.0,
                        samples=2,
                        cgroup_root=cg,
                        proc_root=proc,
                        journal_dirs=[],
                    )

            self.assertEqual(rep["verdict"]["status"], "CRASH_LOOP_SUSPECTED")
            self.assertTrue(rep["verdict"]["is_issue"])
            self.assertNotIn("No immediate intervention needed.", rep["verdict"]["recommendations"])
            self.assertEqual(rep["summary"]["units_crash_loop"], 1)
            self.assertEqual(rep["summary"]["total_zombie_pids"], 1)
            flagged = [u["unit"] for u in rep["problem_units"] if u["crash_loop_suspected"]]
            self.assertEqual(flagged, ["flaky.service"])

    def test_journal_burst_detection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cg = base / "cgroup"
            proc = base / "proc"
            jdir = base / "journal" / "machine-id"
            jdir.mkdir(parents=True)
            proc.mkdir()
            _write(proc / "uptime", "5000.00 10000.00\n")
            _create_unit(cg, "/system.slice/quiet.service", procs=[])

            jfile = jdir / "system.journal"
            jfile.write_bytes(b"A" * 4096)

            def burst_journal(_sec: float) -> None:
                # Append 2 MiB in 1.0s -> 2.0 MiB/s (> 1 MiB/s threshold)
                with open(jfile, "ab") as f:
                    f.write(b"B" * (2 * 1024 * 1024))

            with mock.patch.object(ssw.time, "sleep", side_effect=burst_journal):
                with mock.patch.object(ssw.time, "monotonic", side_effect=[400.0, 401.0]):
                    rep = ssw.watch_systemd(
                        interval=1.0,
                        samples=2,
                        cgroup_root=cg,
                        proc_root=proc,
                        journal_dirs=[base / "journal"],
                    )

            self.assertEqual(rep["verdict"]["status"], "JOURNAL_BURST")
            self.assertTrue(rep["verdict"]["is_issue"])
            self.assertNotIn("No immediate intervention needed.", rep["verdict"]["recommendations"])
            self.assertGreaterEqual(rep["journal"]["growth_bytes"], 2 * 1024 * 1024)
            self.assertGreaterEqual(rep["journal"]["growth_rate_mibs"], 2.0)

    def test_unit_filter_descends_slices(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cg = base / "cgroup"
            proc = base / "proc"
            proc.mkdir()
            _create_unit(cg, "/system.slice", procs=[])
            _create_unit(cg, "/system.slice/docker.service", procs=[])
            _create_unit(cg, "/system.slice/cron.service", procs=[])

            rep = ssw.watch_systemd(
                interval=0.05,
                samples=1,
                unit_filter="dock*",
                cgroup_root=cg,
                proc_root=proc,
                journal_dirs=[],
            )
            self.assertEqual(rep["summary"]["units_total"], 1)
            self.assertEqual(rep["top_memory"][0]["unit"], "docker.service")


class TestCLIAndExitCodes(unittest.TestCase):
    def test_version_flag(self) -> None:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), f"sys-systemd-watch {ssw.VERSION}")

    def test_cli_exit_on_issue_and_json_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cg = Path(td) / "cgroup"
            _create_unit(
                cg,
                "/system.slice/oom.service",
                mem_events={"low": 0, "high": 0, "max": 1, "oom": 1, "oom_kill": 1},
            )
            r = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--cgroup-root",
                    str(cg),
                    "--samples",
                    "1",
                    "--json",
                    "--exit-on-issue",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(r.returncode, 3, msg=r.stderr)
            payload = json.loads(r.stdout)
            self.assertEqual(payload["verdict"]["status"], "MEMORY_PRESSURE")
            self.assertTrue(payload["verdict"]["is_issue"])
            self.assertEqual(r.stdout.strip(), json.dumps(payload, indent=2, sort_keys=True))

    def test_cli_validation_errors_no_traceback(self) -> None:
        bad_invocations = [
            ["--interval", "0.01"],
            ["--samples", "0"],
            ["--top", "0"],
            ["--cgroup-root", "/nonexistent/cgroup/path/xyz"],
        ]
        for argv in bad_invocations:
            r = subprocess.run(
                [sys.executable, str(SCRIPT), *argv],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(r.returncode, 1, msg=f"argv={argv} stderr={r.stderr}")
            self.assertNotIn("Traceback", r.stderr)
            self.assertEqual(len(r.stderr.strip().splitlines()), 1)

    def test_cli_argparse_error_exit_2(self) -> None:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--does-not-exist"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 2)

    def test_invalid_env_var_exit_1(self) -> None:
        env = dict(os.environ)
        env["SYS_SYSTEMD_WATCH_INTERVAL"] = "abc"
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--samples", "1"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
