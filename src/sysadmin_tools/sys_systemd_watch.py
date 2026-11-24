#!/usr/bin/env python3
"""sys-systemd-watch: monitor systemd cgroup v2 resource pressure, crash loops, and journal bursts.

Reads Linux /sys/fs/cgroup, /proc, and journal directories directly using Python 3.10+
stdlib only (zero third-party dependencies, zero systemctl/journalctl subprocesses), so it
stays responsive even during severe CPU, memory, or storage stalls.

Examples:
  sys-systemd-watch
  sys-systemd-watch --interval 1.0 --samples 3 --top 15
  sys-systemd-watch --unit docker --json | jq '.verdict'
  sys-systemd-watch --cgroup-root /sys/fs/cgroup --exit-on-issue --json
  sys-systemd-watch --markdown
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fnmatch
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Sequence

VERSION = "0.5.0"

UNIT_SUFFIXES = (".service", ".scope", ".slice")
DEFAULT_CGROUP_ROOT = "/sys/fs/cgroup"
DEFAULT_PROC_ROOT = "/proc"
DEFAULT_JOURNAL_DIRS = ("/run/log/journal", "/var/log/journal")
CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"]) if hasattr(os, "sysconf") else 100

MEMORY_EVENT_KEYS = ("low", "high", "max", "oom", "oom_kill", "oom_group_kill")
CPU_STAT_KEYS = ("usage_usec", "user_usec", "system_usec", "nr_periods", "nr_throttled", "throttled_usec")
IO_STAT_KEYS = ("rbytes", "wbytes", "rios", "wios", "dbytes", "dios")


def read_text(path: str | Path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def parse_cgroup_int(txt: str | None) -> int | None:
    """Parse a cgroup integer file where 'max' or missing means unbounded (None)."""
    if txt is None:
        return None
    s = txt.strip()
    if not s or s == "max":
        return None
    try:
        val = int(s)
        return val if val >= 0 else None
    except ValueError:
        return None


def parse_kv_int_file(txt: str | None, keys: Sequence[str] | None = None) -> dict[str, int]:
    """Parse space-separated 'key int_value' cgroup stat/event files."""
    out: dict[str, int] = {k: 0 for k in keys} if keys is not None else {}
    if not txt:
        return out
    wanted = set(keys) if keys is not None else None
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        k = parts[0]
        if wanted is not None and k not in wanted:
            continue
        try:
            out[k] = max(0, int(parts[1]))
        except ValueError:
            pass
    return out


def parse_psi_file(txt: str | None) -> dict[str, dict[str, float | int]]:
    """Parse cgroup v2 PSI file (memory.pressure, cpu.pressure, io.pressure)."""
    out: dict[str, dict[str, float | int]] = {}
    if not txt:
        return out
    for line in txt.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("some", "full"):
            continue
        scope = parts[0]
        vals: dict[str, float | int] = {}
        for tok in parts[1:]:
            if "=" not in tok:
                continue
            k, v = tok.split("=", 1)
            if k in ("avg10", "avg60", "avg300"):
                try:
                    vals[k] = float(v)
                except ValueError:
                    pass
            elif k == "total":
                try:
                    vals[k] = max(0, int(v))
                except ValueError:
                    pass
        if vals:
            out[scope] = vals
    return out


def parse_io_stat(txt: str | None) -> dict[str, int]:
    """Parse and aggregate cgroup v2 io.stat across all MAJ:MIN block devices."""
    totals = {k: 0 for k in IO_STAT_KEYS}
    if not txt:
        return totals
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        for tok in parts[1:]:
            if "=" not in tok:
                continue
            k, v = tok.split("=", 1)
            if k in totals:
                try:
                    totals[k] += max(0, int(v))
                except ValueError:
                    pass
    return totals


def parse_cgroup_procs(txt: str | None) -> set[int]:
    """Parse PIDs listed in cgroup.procs."""
    pids: set[int] = set()
    if not txt:
        return pids
    for line in txt.splitlines():
        s = line.strip()
        if s.isdigit():
            pids.add(int(s))
    return pids


def parse_proc_stat(stat_txt: str) -> tuple[str, str, int, int] | None:
    """Return (comm, state, ppid, starttime_ticks) from /proc/<pid>/stat."""
    lp = stat_txt.find("(")
    rp = stat_txt.rfind(")")
    if lp == -1 or rp == -1 or rp <= lp:
        return None
    comm = stat_txt[lp + 1 : rp]
    fields = stat_txt[rp + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        state = fields[0]
        ppid = int(fields[1])
        starttime = int(fields[19])
        return comm, state, ppid, starttime
    except ValueError:
        return None


def parse_proc_cgroup_v2(cgroup_txt: str | None) -> str | None:
    """Extract the unified cgroup v2 path (0::<path>) from /proc/<pid>/cgroup."""
    if not cgroup_txt:
        return None
    for raw_line in cgroup_txt.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0].strip() == "0" and parts[1].strip() == "":
            path = parts[2].strip()
            return path or "/"
    return None


def read_uptime_sec(proc_root: str | Path = DEFAULT_PROC_ROOT) -> float:
    txt = read_text(Path(proc_root) / "uptime")
    if not txt:
        return 0.0
    try:
        return max(0.0, float(txt.split()[0]))
    except (ValueError, IndexError):
        return 0.0


def matches_unit_filter(unit_name: str, cgroup_path: str, unit_filter: str | None) -> bool:
    if not unit_filter:
        return True
    pat = unit_filter.strip().lower()
    if not pat:
        return True
    u_low = unit_name.lower()
    p_low = cgroup_path.lower()
    if any(ch in pat for ch in ("*", "?", "[")):
        return fnmatch.fnmatchcase(u_low, pat) or fnmatch.fnmatchcase(p_low, pat)
    return pat in u_low or pat in p_low


def discover_cgroup_units(
    cgroup_root: str | Path,
    unit_filter: str | None = None,
    max_depth: int = 12,
) -> list[tuple[str, str, str, Path]]:
    """Iteratively discover *.service, *.scope, and *.slice directories under cgroup_root."""
    root = Path(cgroup_root)
    results: list[tuple[str, str, str, Path]] = []
    # Stack entries: (directory_path, relative_cgroup_path, depth)
    stack: list[tuple[Path, str, int]] = [(root, "", 0)]

    while stack:
        cur_dir, rel_prefix, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            with os.scandir(cur_dir) as it:
                entries = list(it)
        except OSError:
            continue

        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            name = entry.name
            rel_path = f"{rel_prefix}/{name}"
            entry_path = Path(entry.path)
            if name.endswith(UNIT_SUFFIXES):
                unit_type = name.rsplit(".", 1)[1]
                if matches_unit_filter(name, rel_path, unit_filter):
                    results.append((name, rel_path, unit_type, entry_path))
            if depth < max_depth:
                stack.append((entry_path, rel_path, depth + 1))

    results.sort(key=lambda item: item[1])
    return results


@dataclasses.dataclass
class ProcInfo:
    pid: int
    comm: str
    state: str
    ppid: int
    starttime: int
    cgroup_path: str | None
    age_sec: float | None


def snapshot_procs(proc_root: str | Path = DEFAULT_PROC_ROOT) -> tuple[dict[int, ProcInfo], dict[str, set[int]]]:
    """Read /proc/<pid>/{stat,cgroup} directly to track process states, ages, and cgroup membership."""
    p_root = Path(proc_root)
    procs: dict[int, ProcInfo] = {}
    by_cgroup: dict[str, set[int]] = {}
    my_pid = os.getpid()
    uptime = read_uptime_sec(p_root)

    try:
        with os.scandir(p_root) as it:
            entries = [e.name for e in it if e.name.isdigit()]
    except OSError:
        return procs, by_cgroup

    for name in entries:
        pid = int(name)
        if pid == my_pid:
            continue
        pdir = p_root / name
        st_txt = read_text(pdir / "stat")
        if not st_txt:
            continue
        parsed = parse_proc_stat(st_txt)
        if not parsed:
            continue
        comm, state, ppid, starttime = parsed
        cg_path = parse_proc_cgroup_v2(read_text(pdir / "cgroup"))
        age_sec: float | None = None
        if uptime > 0.0 and starttime > 0 and CLK_TCK > 0:
            age_sec = max(0.0, uptime - (starttime / CLK_TCK))
        procs[pid] = ProcInfo(
            pid=pid,
            comm=comm,
            state=state,
            ppid=ppid,
            starttime=starttime,
            cgroup_path=cg_path,
            age_sec=age_sec,
        )
        if cg_path:
            by_cgroup.setdefault(cg_path, set()).add(pid)

    return procs, by_cgroup


@dataclasses.dataclass
class UnitSnapshot:
    unit: str
    cgroup_path: str
    unit_type: str
    memory_current: int | None
    memory_peak: int | None
    memory_high: int | None
    memory_max: int | None
    memory_events: dict[str, int]
    memory_pressure: dict[str, dict[str, float | int]]
    cpu_stat: dict[str, int]
    cpu_pressure: dict[str, dict[str, float | int]]
    io_stat: dict[str, int]
    io_pressure: dict[str, dict[str, float | int]]
    pids_current: int | None
    pids_max: int | None
    procs: set[int]


def read_unit_snapshot(
    unit: str,
    cgroup_path: str,
    unit_type: str,
    unit_dir: Path,
    extra_pids: set[int] | None = None,
) -> UnitSnapshot:
    procs = parse_cgroup_procs(read_text(unit_dir / "cgroup.procs"))
    if extra_pids:
        procs |= extra_pids
    return UnitSnapshot(
        unit=unit,
        cgroup_path=cgroup_path,
        unit_type=unit_type,
        memory_current=parse_cgroup_int(read_text(unit_dir / "memory.current")),
        memory_peak=parse_cgroup_int(read_text(unit_dir / "memory.peak")),
        memory_high=parse_cgroup_int(read_text(unit_dir / "memory.high")),
        memory_max=parse_cgroup_int(read_text(unit_dir / "memory.max")),
        memory_events=parse_kv_int_file(read_text(unit_dir / "memory.events"), MEMORY_EVENT_KEYS),
        memory_pressure=parse_psi_file(read_text(unit_dir / "memory.pressure")),
        cpu_stat=parse_kv_int_file(read_text(unit_dir / "cpu.stat"), CPU_STAT_KEYS),
        cpu_pressure=parse_psi_file(read_text(unit_dir / "cpu.pressure")),
        io_stat=parse_io_stat(read_text(unit_dir / "io.stat")),
        io_pressure=parse_psi_file(read_text(unit_dir / "io.pressure")),
        pids_current=parse_cgroup_int(read_text(unit_dir / "pids.current")),
        pids_max=parse_cgroup_int(read_text(unit_dir / "pids.max")),
        procs=procs,
    )


@dataclasses.dataclass
class JournalSnapshot:
    files: dict[tuple[int, int], tuple[int, int]]  # (dev, ino) -> (st_size, st_blocks*512)
    paths_scanned: list[str]
    unreadable_paths: list[str]
    total_bytes: int
    allocated_bytes: int


def snapshot_journal_dirs(
    journal_dirs: Sequence[str | Path] = DEFAULT_JOURNAL_DIRS,
    max_depth: int = 4,
) -> JournalSnapshot:
    """Stat files under journal directories via os.scandir only (zero subprocesses)."""
    files: dict[tuple[int, int], tuple[int, int]] = {}
    paths_scanned: list[str] = []
    unreadable_paths: list[str] = []
    total_bytes = 0
    allocated_bytes = 0

    for raw_dir in journal_dirs:
        root = Path(raw_dir)
        if not root.exists():
            continue
        try:
            with os.scandir(root) as it:
                root_entries = list(it)
        except OSError:
            unreadable_paths.append(str(root))
            continue

        paths_scanned.append(str(root))
        stack: list[tuple[list[os.DirEntry[str]], int]] = [(root_entries, 0)]
        while stack:
            entries, depth = stack.pop()
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if depth < max_depth:
                            try:
                                with os.scandir(entry.path) as sub_it:
                                    stack.append((list(sub_it), depth + 1))
                            except OSError:
                                unreadable_paths.append(entry.path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue

                key = (st.st_dev, st.st_ino)
                if key in files:
                    continue
                sz = max(0, int(st.st_size))
                alloc = max(0, int(getattr(st, "st_blocks", 0)) * 512)
                files[key] = (sz, alloc)
                total_bytes += sz
                allocated_bytes += alloc

    return JournalSnapshot(
        files=files,
        paths_scanned=sorted(set(paths_scanned)),
        unreadable_paths=sorted(set(unreadable_paths)),
        total_bytes=total_bytes,
        allocated_bytes=allocated_bytes,
    )


def compute_journal_delta(
    j0: JournalSnapshot,
    j1: JournalSnapshot,
    dt_sec: float,
) -> dict[str, Any]:
    """Compute journal byte growth across [j0, j1], handling preallocated & rotated files."""
    growth_bytes = 0
    new_files = 0
    if dt_sec > 0.0:
        for key, (s1, a1) in j1.files.items():
            prev = j0.files.get(key)
            if prev is None:
                new_files += 1
                growth_bytes += max(s1, a1)
            else:
                s0, a0 = prev
                d_size = max(0, s1 - s0)
                d_alloc = max(0, a1 - a0)
                growth_bytes += max(d_size, d_alloc)

    rate_bps = (growth_bytes / dt_sec) if dt_sec > 0.0 else 0.0
    return {
        "readable": bool(j1.paths_scanned),
        "paths_scanned": j1.paths_scanned,
        "unreadable_paths": j1.unreadable_paths,
        "file_count": len(j1.files),
        "new_files": new_files,
        "total_bytes": j1.total_bytes,
        "total_mib": round(j1.total_bytes / (1024.0 * 1024.0), 2),
        "allocated_bytes": j1.allocated_bytes,
        "growth_bytes": growth_bytes,
        "growth_kib": round(growth_bytes / 1024.0, 2),
        "growth_mib": round(growth_bytes / (1024.0 * 1024.0), 3),
        "growth_rate_bps": round(rate_bps, 1),
        "growth_rate_kibs": round(rate_bps / 1024.0, 2),
        "growth_rate_mibs": round(rate_bps / (1024.0 * 1024.0), 3),
    }


def _psi_val(psi: dict[str, dict[str, float | int]], scope: str, field: str) -> float:
    return float(psi.get(scope, {}).get(field, 0.0))


def _psi_total_delta(
    p0: dict[str, dict[str, float | int]],
    p1: dict[str, dict[str, float | int]],
    scope: str,
) -> int:
    t0 = int(p0.get(scope, {}).get("total", 0))
    t1 = int(p1.get(scope, {}).get("total", 0))
    return max(0, t1 - t0)


def build_unit_metrics(
    snapshots: list[dict[str, UnitSnapshot]],
    final_procs: dict[int, ProcInfo],
    dt_sec: float,
) -> list[dict[str, Any]]:
    """Aggregate multi-snapshot deltas, PSI, process states, and crash-loop signals per unit."""
    if not snapshots:
        return []

    first_map = snapshots[0]
    last_map = snapshots[-1]
    all_paths = sorted(last_map.keys())
    rows: list[dict[str, Any]] = []

    for cg_path in all_paths:
        u1 = last_map[cg_path]
        u0 = first_map.get(cg_path)
        has_delta = len(snapshots) >= 2 and u0 is not None and dt_sec > 0.0

        # Memory stats & events
        mem_cur = u1.memory_current
        mem_peak = u1.memory_peak
        mem_high = u1.memory_high
        mem_max = u1.memory_max
        mem_pct_max = (
            round(100.0 * mem_cur / mem_max, 1)
            if (mem_cur is not None and mem_max is not None and mem_max > 0)
            else None
        )
        mem_ev_delta = {
            k: max(0, u1.memory_events.get(k, 0) - u0.memory_events.get(k, 0)) if has_delta else 0
            for k in MEMORY_EVENT_KEYS
        }

        # CPU deltas & throttling
        if has_delta:
            assert u0 is not None
            d_usage = max(0, u1.cpu_stat.get("usage_usec", 0) - u0.cpu_stat.get("usage_usec", 0))
            d_user = max(0, u1.cpu_stat.get("user_usec", 0) - u0.cpu_stat.get("user_usec", 0))
            d_sys = max(0, u1.cpu_stat.get("system_usec", 0) - u0.cpu_stat.get("system_usec", 0))
            d_periods = max(0, u1.cpu_stat.get("nr_periods", 0) - u0.cpu_stat.get("nr_periods", 0))
            d_throttled = max(0, u1.cpu_stat.get("nr_throttled", 0) - u0.cpu_stat.get("nr_throttled", 0))
            d_throttled_usec = max(0, u1.cpu_stat.get("throttled_usec", 0) - u0.cpu_stat.get("throttled_usec", 0))
            denom_usec = dt_sec * 1_000_000.0
            cpu_pct = round(100.0 * d_usage / denom_usec, 2)
            user_pct = round(100.0 * d_user / denom_usec, 2)
            system_pct = round(100.0 * d_sys / denom_usec, 2)
            throttled_pct = round(100.0 * d_throttled_usec / denom_usec, 2)
            throttled_ratio_pct = round(100.0 * d_throttled / d_periods, 1) if d_periods > 0 else 0.0
        else:
            d_usage = d_user = d_sys = d_periods = d_throttled = d_throttled_usec = 0
            cpu_pct = user_pct = system_pct = throttled_pct = throttled_ratio_pct = 0.0

        life_periods = u1.cpu_stat.get("nr_periods", 0)
        life_throttled = u1.cpu_stat.get("nr_throttled", 0)
        life_throttled_usec = u1.cpu_stat.get("throttled_usec", 0)
        life_throttled_ratio_pct = (
            round(100.0 * life_throttled / life_periods, 1) if life_periods > 0 else 0.0
        )

        # I/O deltas
        if has_delta:
            assert u0 is not None
            d_rbytes = max(0, u1.io_stat.get("rbytes", 0) - u0.io_stat.get("rbytes", 0))
            d_wbytes = max(0, u1.io_stat.get("wbytes", 0) - u0.io_stat.get("wbytes", 0))
            d_rios = max(0, u1.io_stat.get("rios", 0) - u0.io_stat.get("rios", 0))
            d_wios = max(0, u1.io_stat.get("wios", 0) - u0.io_stat.get("wios", 0))
            io_read_mibs = round(d_rbytes / (1024.0 * 1024.0 * dt_sec), 3)
            io_write_mibs = round(d_wbytes / (1024.0 * 1024.0 * dt_sec), 3)
        else:
            d_rbytes = d_wbytes = d_rios = d_wios = 0
            io_read_mibs = io_write_mibs = 0.0
        io_total_mibs = round(io_read_mibs + io_write_mibs, 3)

        # PSI total deltas
        mem_psi_full_delta = _psi_total_delta(u0.memory_pressure, u1.memory_pressure, "full") if has_delta and u0 else 0
        cpu_psi_some_delta = _psi_total_delta(u0.cpu_pressure, u1.cpu_pressure, "some") if has_delta and u0 else 0
        io_psi_full_delta = _psi_total_delta(u0.io_pressure, u1.io_pressure, "full") if has_delta and u0 else 0

        # PID churn across all consecutive snapshot transitions
        pids_added_total = 0
        pids_exited_total = 0
        full_pid_replacements = 0
        for idx in range(len(snapshots) - 1):
            prev_u = snapshots[idx].get(cg_path)
            curr_u = snapshots[idx + 1].get(cg_path)
            if prev_u is None or curr_u is None:
                continue
            p_prev = prev_u.procs
            p_curr = curr_u.procs
            added = p_curr - p_prev
            exited = p_prev - p_curr
            pids_added_total += len(added)
            pids_exited_total += len(exited)
            if p_prev and p_curr and not (p_prev & p_curr):
                full_pid_replacements += 1

        # Process states & ages in final snapshot
        d_state_pids = 0
        zombie_pids = 0
        ages: list[float] = []
        for pid in u1.procs:
            pinfo = final_procs.get(pid)
            if pinfo is None:
                continue
            if pinfo.state == "D":
                d_state_pids += 1
            elif pinfo.state == "Z":
                zombie_pids += 1
            if pinfo.age_sec is not None:
                ages.append(pinfo.age_sec)

        min_proc_age_sec = round(min(ages), 2) if ages else None
        max_proc_age_sec = round(max(ages), 2) if ages else None
        very_young_pids = sum(1 for a in ages if a < 5.0)
        churn_count = pids_added_total + pids_exited_total

        # Crash-loop heuristic applies to *.service units (not scopes/slices).
        crash_loop_suspected = False
        crash_loop_reason = ""
        if u1.unit_type == "service":
            if full_pid_replacements >= 1:
                crash_loop_suspected = True
                crash_loop_reason = (
                    f"full PID set replacement ({full_pid_replacements}x, +{pids_added_total}/-{pids_exited_total} PIDs)"
                )
            elif pids_added_total >= 2 and pids_exited_total >= 2:
                crash_loop_suspected = True
                crash_loop_reason = f"rapid PID churn (+{pids_added_total}/-{pids_exited_total} PIDs in {dt_sec:.2f}s)"
            elif (
                pids_added_total >= 1
                and max_proc_age_sec is not None
                and max_proc_age_sec <= 3.0
                and (pids_exited_total >= 1 or zombie_pids >= 1 or mem_ev_delta["oom_kill"] > 0)
            ):
                crash_loop_suspected = True
                crash_loop_reason = (
                    f"newly restarted service (max_age={max_proc_age_sec:.1f}s, +{pids_added_total}/-{pids_exited_total}, zombies={zombie_pids})"
                )

        # Per-unit issue flags
        mem_oom_active = (
            mem_ev_delta["oom_kill"] > 0
            or mem_ev_delta["oom"] > 0
            or u1.memory_events["oom_kill"] > 0
            or u1.memory_events["oom"] > 0
        )
        mem_pressure_issue = (
            mem_oom_active
            or mem_ev_delta["max"] > 0
            or mem_ev_delta["high"] >= 5
            or _psi_val(u1.memory_pressure, "full", "avg10") >= 5.0
            or _psi_val(u1.memory_pressure, "some", "avg10") >= 15.0
            or (mem_pct_max is not None and mem_pct_max >= 95.0)
            or (has_delta and mem_psi_full_delta >= int(150_000 * dt_sec))
        )

        io_pressure_issue = (
            _psi_val(u1.io_pressure, "full", "avg10") >= 10.0
            or _psi_val(u1.io_pressure, "some", "avg10") >= 25.0
            or (has_delta and io_psi_full_delta >= int(200_000 * dt_sec))
            or (d_state_pids >= 4 and (_psi_val(u1.io_pressure, "some", "avg10") >= 5.0 or io_total_mibs >= 50.0))
        )

        cpu_throttling_issue = (
            throttled_pct >= 10.0
            or (d_throttled >= 3 and throttled_ratio_pct >= 20.0)
            or _psi_val(u1.cpu_pressure, "some", "avg10") >= 25.0
            or _psi_val(u1.cpu_pressure, "full", "avg10") >= 10.0
            or (
                life_throttled >= 50
                and life_throttled_usec >= 1_000_000
                and (life_periods == 0 or life_throttled_ratio_pct >= 25.0)
            )
        )

        issues: list[str] = []
        if mem_pressure_issue:
            issues.append("MEMORY_PRESSURE")
        if crash_loop_suspected:
            issues.append("CRASH_LOOP_SUSPECTED")
        if io_pressure_issue:
            issues.append("IO_PRESSURE")
        if cpu_throttling_issue:
            issues.append("CPU_THROTTLING")

        rows.append(
            {
                "unit": u1.unit,
                "cgroup_path": cg_path,
                "unit_type": u1.unit_type,
                "memory_current_bytes": mem_cur,
                "memory_current_mib": round(mem_cur / (1024.0 * 1024.0), 2) if mem_cur is not None else 0.0,
                "memory_peak_bytes": mem_peak,
                "memory_peak_mib": round(mem_peak / (1024.0 * 1024.0), 2) if mem_peak is not None else None,
                "memory_high_bytes": mem_high,
                "memory_max_bytes": mem_max,
                "memory_pct_of_max": mem_pct_max,
                "memory_events": dict(u1.memory_events),
                "memory_events_delta": mem_ev_delta,
                "memory_pressure": u1.memory_pressure,
                "memory_psi_full_usec_delta": mem_psi_full_delta,
                "cpu_usage_usec_delta": d_usage,
                "cpu_pct": cpu_pct,
                "cpu_user_pct": user_pct,
                "cpu_system_pct": system_pct,
                "cpu_nr_periods": life_periods,
                "cpu_nr_throttled": life_throttled,
                "cpu_throttled_usec": life_throttled_usec,
                "cpu_nr_throttled_delta": d_throttled,
                "cpu_throttled_usec_delta": d_throttled_usec,
                "cpu_throttled_pct": throttled_pct,
                "cpu_throttled_ratio_pct": throttled_ratio_pct,
                "cpu_lifetime_throttled_ratio_pct": life_throttled_ratio_pct,
                "cpu_pressure": u1.cpu_pressure,
                "cpu_psi_some_usec_delta": cpu_psi_some_delta,
                "io_read_bytes_delta": d_rbytes,
                "io_write_bytes_delta": d_wbytes,
                "io_rios_delta": d_rios,
                "io_wios_delta": d_wios,
                "io_read_mibs": io_read_mibs,
                "io_write_mibs": io_write_mibs,
                "io_total_mibs": io_total_mibs,
                "io_pressure": u1.io_pressure,
                "io_psi_full_usec_delta": io_psi_full_delta,
                "pids_current": u1.pids_current if u1.pids_current is not None else len(u1.procs),
                "pids_max": u1.pids_max,
                "procs_count": len(u1.procs),
                "pids_added": pids_added_total,
                "pids_exited": pids_exited_total,
                "pids_churn": churn_count,
                "full_pid_replacements": full_pid_replacements,
                "d_state_pids": d_state_pids,
                "zombie_pids": zombie_pids,
                "very_young_pids": very_young_pids,
                "min_proc_age_sec": min_proc_age_sec,
                "max_proc_age_sec": max_proc_age_sec,
                "crash_loop_suspected": crash_loop_suspected,
                "crash_loop_reason": crash_loop_reason,
                "issues": issues,
            }
        )

    return rows


def _prefer_leaf_units(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer service/scope leaf units over parent slices when attributing cgroup issues."""
    leaves = [r for r in rows if r["unit_type"] != "slice"]
    return leaves if leaves else rows


def _format_unit_labels(rows: list[dict[str, Any]], limit: int = 3, mode: str = "memory") -> str:
    labels: list[str] = []
    for u in rows[:limit]:
        name = u["unit"]
        if mode == "memory":
            oom = u["memory_events"].get("oom_kill", 0)
            p_full = _psi_val(u["memory_pressure"], "full", "avg10")
            p_some = _psi_val(u["memory_pressure"], "some", "avg10")
            extra = f"oom_kill={oom}" if oom > 0 else f"psi={p_some:.1f}%/{p_full:.1f}%"
            labels.append(f"{name}({u['memory_current_mib']:.1f} MiB, {extra})")
        elif mode == "cpu":
            labels.append(
                f"{name}(cpu={u['cpu_pct']:.1f}%, thr={u['cpu_throttled_pct']:.1f}%, "
                f"nr_thr=+{u['cpu_nr_throttled_delta']}/{u['cpu_nr_throttled']})"
            )
        elif mode == "io":
            p_full = _psi_val(u["io_pressure"], "full", "avg10")
            p_some = _psi_val(u["io_pressure"], "some", "avg10")
            labels.append(
                f"{name}(io={u['io_total_mibs']:.2f} MiB/s, psi={p_some:.1f}%/{p_full:.1f}%, D={u['d_state_pids']})"
            )
        elif mode == "crash":
            detail = u["crash_loop_reason"] or f"+{u['pids_added']}/-{u['pids_exited']}"
            labels.append(f"{name}({detail})")
        else:
            labels.append(name)
    return ", ".join(labels)


def classify_verdict(
    units: list[dict[str, Any]],
    journal: dict[str, Any],
    dt_sec: float = 1.0,
) -> dict[str, Any]:
    """Classify systemd cgroup health into deterministic verdicts with actionable recommendations."""
    mem_units = _prefer_leaf_units([u for u in units if "MEMORY_PRESSURE" in u["issues"]])
    crash_units = [u for u in units if "CRASH_LOOP_SUSPECTED" in u["issues"]]
    io_units = _prefer_leaf_units([u for u in units if "IO_PRESSURE" in u["issues"]])
    cpu_units = _prefer_leaf_units([u for u in units if "CPU_THROTTLING" in u["issues"]])

    mem_units.sort(
        key=lambda u: (
            u["memory_events_delta"].get("oom_kill", 0),
            u["memory_events"].get("oom_kill", 0),
            u["memory_events"].get("oom", 0),
            _psi_val(u["memory_pressure"], "full", "avg10"),
            _psi_val(u["memory_pressure"], "some", "avg10"),
            u["memory_current_mib"],
        ),
        reverse=True,
    )
    crash_units.sort(
        key=lambda u: (u["full_pid_replacements"], u["pids_churn"], u["zombie_pids"]),
        reverse=True,
    )
    io_units.sort(
        key=lambda u: (
            _psi_val(u["io_pressure"], "full", "avg10"),
            _psi_val(u["io_pressure"], "some", "avg10"),
            u["d_state_pids"],
            u["io_total_mibs"],
        ),
        reverse=True,
    )
    cpu_units.sort(
        key=lambda u: (
            u["cpu_throttled_pct"],
            u["cpu_nr_throttled_delta"],
            u["cpu_lifetime_throttled_ratio_pct"],
            _psi_val(u["cpu_pressure"], "some", "avg10"),
            u["cpu_pct"],
        ),
        reverse=True,
    )

    journal_burst = (
        float(journal.get("growth_rate_bps", 0.0)) >= 1_048_576.0
        and int(journal.get("growth_bytes", 0)) >= 512 * 1024
    )

    active_issues: list[str] = []
    reasons: list[str] = []
    recommendations: list[str] = []

    if mem_units:
        active_issues.append("MEMORY_PRESSURE")
        labels = _format_unit_labels(mem_units, mode="memory")
        reasons.append(f"Cgroup memory pressure / OOM events detected in {len(mem_units)} unit(s): {labels}.")
        recommendations.append(
            f"Inspect memory limits (MemoryHigh/MemoryMax) and recent OOM kills for {mem_units[0]['unit']}; "
            "increase limit or cap worker concurrency to prevent OOM loops."
        )

    if crash_units:
        active_issues.append("CRASH_LOOP_SUSPECTED")
        labels = _format_unit_labels(crash_units, mode="crash")
        reasons.append(f"Suspected crash-loop / rapid PID turnover in {len(crash_units)} service(s): {labels}.")
        recommendations.append(
            f"Check crash reason and restart policy for {crash_units[0]['unit']} "
            f"(e.g., `journalctl -u {crash_units[0]['unit']} -n 50` and `systemctl status {crash_units[0]['unit']}`)."
        )

    if io_units:
        active_issues.append("IO_PRESSURE")
        labels = _format_unit_labels(io_units, mode="io")
        reasons.append(f"Elevated cgroup I/O pressure or D-state stalls in {len(io_units)} unit(s): {labels}.")
        recommendations.append(
            f"Throttle or serialize disk-heavy work in {io_units[0]['unit']} "
            "(consider IOWeight/IOWriteBandwidthMax or sys-io-guard)."
        )

    if cpu_units:
        active_issues.append("CPU_THROTTLING")
        labels = _format_unit_labels(cpu_units, mode="cpu")
        reasons.append(f"Cgroup CPU throttling / contention detected in {len(cpu_units)} unit(s): {labels}.")
        recommendations.append(
            f"Review CPUQuota for {cpu_units[0]['unit']} (raise CPUQuota or reduce worker threads to eliminate CFS throttling)."
        )

    if journal_burst:
        active_issues.append("JOURNAL_BURST")
        rate_mibs = float(journal.get("growth_rate_mibs", 0.0))
        growth_kib = float(journal.get("growth_kib", 0.0))
        reasons.append(
            f"Journal directory burst detected: +{growth_kib:.1f} KiB ({rate_mibs:.2f} MiB/s) across {dt_sec:.2f}s."
        )
        recommendations.append(
            "Identify noisy logging unit(s) (check top CPU/IO units or `journalctl --since -2m`), "
            "lower log verbosity, or tighten RateLimitBurst in journald.conf."
        )

    if active_issues:
        status = active_issues[0]
        is_issue = True
    else:
        status = "HEALTHY"
        is_issue = False
        total_d = sum(u["d_state_pids"] for u in units)
        total_z = sum(u["zombie_pids"] for u in units)
        reasons.append(
            f"All {len(units)} monitored cgroup unit(s) healthy over {dt_sec:.2f}s "
            f"(no OOM/memory pressure, CPU throttling, IO stalls, crash loops, or journal bursts; "
            f"D-state={total_d}, zombies={total_z})."
        )

    if not recommendations:
        if is_issue:
            recommendations.append(
                "Inspect flagged systemd cgroup units above and adjust resource limits or service configuration."
            )
        else:
            recommendations.append("No immediate intervention needed.")

    return {
        "status": status,
        "is_issue": is_issue,
        "active_issues": active_issues,
        "reasons": reasons,
        "recommendations": recommendations,
    }


def watch_systemd(
    interval: float = 1.0,
    samples: int = 2,
    top_n: int = 10,
    unit_filter: str | None = None,
    cgroup_root: str | Path = DEFAULT_CGROUP_ROOT,
    proc_root: str | Path = DEFAULT_PROC_ROOT,
    journal_dirs: Sequence[str | Path] = DEFAULT_JOURNAL_DIRS,
) -> dict[str, Any]:
    """Collect multi-snapshot cgroup v2, /proc, and journal telemetry with zero subprocesses."""
    cg_root = Path(cgroup_root)
    if not cg_root.exists() or not cg_root.is_dir():
        raise OSError(f"cgroup root does not exist or is not a directory: {cg_root}")

    unit_snapshots: list[dict[str, UnitSnapshot]] = []
    final_procs: dict[int, ProcInfo] = {}
    j0: JournalSnapshot | None = None
    j1: JournalSnapshot | None = None

    t_start = time.monotonic()
    for idx in range(samples):
        if idx > 0:
            time.sleep(interval)
        procs_map, by_cgroup = snapshot_procs(proc_root=proc_root)
        final_procs = procs_map
        discovered = discover_cgroup_units(cg_root, unit_filter=unit_filter)
        snap: dict[str, UnitSnapshot] = {}
        for u_name, rel_path, u_type, u_dir in discovered:
            snap[rel_path] = read_unit_snapshot(
                unit=u_name,
                cgroup_path=rel_path,
                unit_type=u_type,
                unit_dir=u_dir,
                extra_pids=by_cgroup.get(rel_path),
            )
        unit_snapshots.append(snap)
        if idx == 0:
            j0 = snapshot_journal_dirs(journal_dirs=journal_dirs)
        if idx == samples - 1:
            j1 = j0 if samples == 1 else snapshot_journal_dirs(journal_dirs=journal_dirs)

    t_end = time.monotonic()
    dt_sec = max(0.05, t_end - t_start) if samples >= 2 else 0.0

    assert j0 is not None and j1 is not None
    journal = compute_journal_delta(j0, j1, dt_sec=dt_sec)
    unit_rows = build_unit_metrics(unit_snapshots, final_procs=final_procs, dt_sec=dt_sec)

    # Prefer leaf units (services/scopes) for top resource tables unless only slices exist.
    rank_pool = [u for u in unit_rows if u["unit_type"] != "slice"] or unit_rows
    slices_pool = [u for u in unit_rows if u["unit_type"] == "slice"]

    top_memory = sorted(
        rank_pool,
        key=lambda u: (u["memory_current_mib"], u["memory_events"].get("oom_kill", 0), u["unit"]),
        reverse=True,
    )[:top_n]
    top_cpu = sorted(
        rank_pool,
        key=lambda u: (u["cpu_pct"], u["cpu_throttled_pct"], u["cpu_nr_throttled_delta"], u["unit"]),
        reverse=True,
    )[:top_n]
    top_io = sorted(
        [u for u in rank_pool if u["io_total_mibs"] > 0.0 or _psi_val(u["io_pressure"], "some", "avg10") > 0.0 or u["d_state_pids"] > 0],
        key=lambda u: (u["io_total_mibs"], _psi_val(u["io_pressure"], "full", "avg10"), u["d_state_pids"], u["unit"]),
        reverse=True,
    )[:top_n]
    problem_units = sorted(
        [u for u in unit_rows if u["issues"] or u["d_state_pids"] > 0 or u["zombie_pids"] > 0],
        key=lambda u: (len(u["issues"]), u["crash_loop_suspected"], u["d_state_pids"], u["zombie_pids"], u["memory_current_mib"]),
        reverse=True,
    )[:top_n]
    top_slices = sorted(
        slices_pool,
        key=lambda u: (u["memory_current_mib"], u["cpu_pct"], u["unit"]),
        reverse=True,
    )[:top_n]

    verdict = classify_verdict(unit_rows, journal=journal, dt_sec=dt_sec)

    summary = {
        "units_total": len(unit_rows),
        "services": sum(1 for u in unit_rows if u["unit_type"] == "service"),
        "scopes": sum(1 for u in unit_rows if u["unit_type"] == "scope"),
        "slices": len(slices_pool),
        "units_memory_pressure": sum(1 for u in unit_rows if "MEMORY_PRESSURE" in u["issues"]),
        "units_cpu_throttling": sum(1 for u in unit_rows if "CPU_THROTTLING" in u["issues"]),
        "units_io_pressure": sum(1 for u in unit_rows if "IO_PRESSURE" in u["issues"]),
        "units_crash_loop": sum(1 for u in unit_rows if u["crash_loop_suspected"]),
        "total_d_state_pids": sum(u["d_state_pids"] for u in unit_rows),
        "total_zombie_pids": sum(u["zombie_pids"] for u in unit_rows),
    }

    return {
        "version": VERSION,
        "timestamp": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "sample_sec": round(dt_sec, 2),
        "samples": samples,
        "cgroup_root": str(cg_root),
        "unit_filter": unit_filter,
        "verdict": verdict,
        "summary": summary,
        "journal": journal,
        "problem_units": problem_units,
        "top_memory": top_memory,
        "top_cpu": top_cpu,
        "top_io": top_io,
        "top_slices": top_slices,
    }


def _short(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def render_text(rep: dict[str, Any], markdown: bool = False) -> str:
    v = rep["verdict"]
    s = rep["summary"]
    j = rep["journal"]
    h = (lambda title: f"## {title}") if markdown else (lambda title: f"=== {title} ===")

    lines: list[str] = [
        f"{'# ' if markdown else ''}Systemd Cgroup Watch @ {rep['timestamp']} "
        f"(window={rep['sample_sec']}s, samples={rep['samples']})",
        f"Status: {v['status']} (is_issue={v['is_issue']})",
    ]
    for reason in v["reasons"]:
        lines.append(f"  - {reason}")
    lines.append("")

    lines.append(h("Summary & Journal"))
    lines.append(
        f"Units   : total={s['units_total']} (services={s['services']}, scopes={s['scopes']}, slices={s['slices']}) | "
        f"D-state={s['total_d_state_pids']} | zombies={s['total_zombie_pids']}"
    )
    lines.append(
        f"Alerts  : mem={s['units_memory_pressure']} cpu_thr={s['units_cpu_throttling']} "
        f"io={s['units_io_pressure']} crash_loop={s['units_crash_loop']}"
    )
    if j["readable"]:
        lines.append(
            f"Journal : files={j['file_count']} total={j['total_mib']:.2f} MiB | "
            f"growth=+{j['growth_kib']:.1f} KiB ({j['growth_rate_kibs']:.2f} KiB/s, {j['growth_rate_mibs']:.3f} MiB/s)"
        )
    else:
        lines.append("Journal : no readable journal directories found (skipped)")
    lines.append("")

    if rep.get("problem_units"):
        lines.append(h("Flagged / Problem Units"))
        lines.append(
            f"{'UNIT':<34} {'MEM_M':>8} {'CPU%':>6} {'THR%':>6} {'IO_M/s':>7} {'CHURN':>7} {'D/Z':>5} ISSUES"
        )
        for u in rep["problem_units"]:
            churn = f"+{u['pids_added']}/-{u['pids_exited']}"
            dz = f"{u['d_state_pids']}/{u['zombie_pids']}"
            iss = ",".join(u["issues"]) if u["issues"] else "PROC_STATE"
            lines.append(
                f"{_short(u['unit'], 34):<34} {u['memory_current_mib']:>8.1f} {u['cpu_pct']:>6.1f} "
                f"{u['cpu_throttled_pct']:>6.1f} {u['io_total_mibs']:>7.2f} {churn:>7} {dz:>5} {iss}"
            )
        lines.append("")

    if rep.get("top_memory"):
        lines.append(h("Top Units by Memory"))
        lines.append(
            f"{'UNIT':<36} {'MEM_M':>9} {'PEAK_M':>9} {'MAX%':>6} {'OOM':>4} {'PSI_M10':>8} {'PIDS':>6}"
        )
        for u in rep["top_memory"]:
            peak = f"{u['memory_peak_mib']:.1f}" if u["memory_peak_mib"] is not None else "-"
            mpct = f"{u['memory_pct_of_max']:.1f}%" if u["memory_pct_of_max"] is not None else "-"
            psi_m = _psi_val(u["memory_pressure"], "some", "avg10")
            lines.append(
                f"{_short(u['unit'], 36):<36} {u['memory_current_mib']:>9.1f} {peak:>9} {mpct:>6} "
                f"{u['memory_events'].get('oom_kill', 0):>4} {psi_m:>7.1f}% {u['pids_current']:>6}"
            )
        lines.append("")

    if rep.get("top_cpu"):
        lines.append(h("Top Units by CPU & Throttling"))
        lines.append(
            f"{'UNIT':<36} {'CPU%':>7} {'USR%':>6} {'SYS%':>6} {'THR%':>6} {'dTHR':>6} {'PSI_C10':>8}"
        )
        for u in rep["top_cpu"]:
            psi_c = _psi_val(u["cpu_pressure"], "some", "avg10")
            lines.append(
                f"{_short(u['unit'], 36):<36} {u['cpu_pct']:>7.2f} {u['cpu_user_pct']:>6.1f} "
                f"{u['cpu_system_pct']:>6.1f} {u['cpu_throttled_pct']:>6.1f} "
                f"{u['cpu_nr_throttled_delta']:>6} {psi_c:>7.1f}%"
            )
        lines.append("")

    if rep.get("top_io"):
        lines.append(h("Top Units by I/O & Pressure"))
        lines.append(
            f"{'UNIT':<36} {'R_M/s':>8} {'W_M/s':>8} {'PSI_I10':>8} {'PSI_F10':>8} {'D_PID':>5}"
        )
        for u in rep["top_io"]:
            psi_s = _psi_val(u["io_pressure"], "some", "avg10")
            psi_f = _psi_val(u["io_pressure"], "full", "avg10")
            lines.append(
                f"{_short(u['unit'], 36):<36} {u['io_read_mibs']:>8.2f} {u['io_write_mibs']:>8.2f} "
                f"{psi_s:>7.1f}% {psi_f:>7.1f}% {u['d_state_pids']:>5}"
            )
        lines.append("")

    lines.append(h("Recommendations"))
    for rec in v["recommendations"]:
        lines.append(f"- {rec}")
    return "\n".join(lines)


def build_parser(default_interval: float = 1.0, default_top: int = 10) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sys-systemd-watch",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--interval",
        type=float,
        default=default_interval,
        help="Seconds between samples (0.05..60.0, env SYS_SYSTEMD_WATCH_INTERVAL, default: 1.0)",
    )
    p.add_argument(
        "--samples",
        type=int,
        default=2,
        help="Number of cgroup snapshots to collect (>=1, max 60, default: 2)",
    )
    p.add_argument(
        "--top",
        type=int,
        default=default_top,
        help="Max rows per section (1..100, env SYS_SYSTEMD_WATCH_TOP, default: 10)",
    )
    p.add_argument(
        "--unit",
        default=None,
        metavar="PATTERN",
        help="Optional substring or glob filter for unit name or cgroup path (e.g., docker, '*.service')",
    )
    p.add_argument(
        "--cgroup-root",
        default=DEFAULT_CGROUP_ROOT,
        metavar="PATH",
        help=f"Cgroup v2 root directory (default: {DEFAULT_CGROUP_ROOT})",
    )
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="Emit deterministic JSON report to stdout")
    fmt.add_argument("--markdown", action="store_true", help="Emit Markdown report to stdout")
    p.add_argument(
        "--exit-on-issue",
        action="store_true",
        help="Exit with code 3 when verdict status is not HEALTHY",
    )
    p.add_argument("--verbose", action="store_true", help="Print effective configuration to stderr")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return p


def _parse_env_defaults() -> tuple[float, int]:
    raw_interval = os.environ.get("SYS_SYSTEMD_WATCH_INTERVAL", "").strip()
    raw_top = os.environ.get("SYS_SYSTEMD_WATCH_TOP", "").strip()
    try:
        default_interval = float(raw_interval) if raw_interval else 1.0
    except ValueError as e:
        raise ValueError(f"invalid SYS_SYSTEMD_WATCH_INTERVAL={raw_interval!r}") from e
    try:
        default_top = int(raw_top) if raw_top else 10
    except ValueError as e:
        raise ValueError(f"invalid SYS_SYSTEMD_WATCH_TOP={raw_top!r}") from e
    return default_interval, default_top


def main(argv: list[str] | None = None) -> int:
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    try:
        default_interval, default_top = _parse_env_defaults()
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    parser = build_parser(default_interval=default_interval, default_top=default_top)
    args = parser.parse_args(argv)

    if not (0.05 <= args.interval <= 60.0):
        print("error: --interval must be between 0.05 and 60.0 seconds", file=sys.stderr)
        return 1
    if not (1 <= args.samples <= 60):
        print("error: --samples must be between 1 and 60", file=sys.stderr)
        return 1
    if not (1 <= args.top <= 100):
        print("error: --top must be between 1 and 100", file=sys.stderr)
        return 1

    if args.verbose:
        print(
            f"config: interval={args.interval}s samples={args.samples} top={args.top} "
            f"unit={args.unit!r} cgroup_root={args.cgroup_root} "
            f"format={'json' if args.json else 'markdown' if args.markdown else 'text'} "
            f"exit_on_issue={args.exit_on_issue}",
            file=sys.stderr,
        )

    try:
        rep = watch_systemd(
            interval=args.interval,
            samples=args.samples,
            top_n=args.top,
            unit_filter=args.unit,
            cgroup_root=args.cgroup_root,
        )
    except KeyboardInterrupt:
        return 0
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True), flush=True)
    else:
        print(render_text(rep, markdown=args.markdown), flush=True)

    if args.exit_on_issue and rep["verdict"]["is_issue"]:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
