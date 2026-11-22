#!/usr/bin/env python3
"""sys-mem-watch: diagnose RSS/swap growth, reclaim pressure, and OOM risk.

Reads Linux /proc directly (zero subprocesses, Python 3.10+ stdlib only), so it
stays fast and reliable even under severe memory pressure or storage stalls.

Examples:
  sys-mem-watch
  sys-mem-watch --interval 3 --top 15
  sys-mem-watch --json | jq '.verdict'
  sys-mem-watch --markdown
  sys-mem-watch --watch 5 --interval 2
  sys-mem-watch --append-log /var/tmp/sys-mem-watch.jsonl
  sys-mem-watch --exit-on-issue --json > /tmp/mem-diag.json
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"]) if hasattr(os, "sysconf") else 100
VMSTAT_KEYS = (
    "pswpin",
    "pswpout",
    "pgscan_kswapd",
    "pgscan_direct",
    "pgsteal_kswapd",
    "pgsteal_direct",
    "oom_kill",
)


def read_text(path: str | Path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def read_bytes(path: str | Path) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def read_proc_int(path: str | Path, default: int = 0) -> int:
    txt = read_text(path)
    if txt is None:
        return default
    try:
        return int(txt.strip())
    except ValueError:
        return default


def read_uptime_sec(proc_root: str = "/proc") -> float:
    txt = read_text(f"{proc_root}/uptime")
    if not txt:
        return 0.0
    try:
        return float(txt.split()[0])
    except (ValueError, IndexError):
        return 0.0


def parse_meminfo(txt: str) -> dict[str, float]:
    raw: dict[str, int] = {}
    for line in txt.splitlines():
        if ":" not in line:
            continue
        k, rest = line.split(":", 1)
        toks = rest.strip().split()
        if toks and toks[0].isdigit():
            raw[k.strip()] = int(toks[0])  # kB

    kb = lambda k: raw.get(k, 0)
    gib = lambda v: round(v / (1024.0 * 1024.0), 2)

    mem_total = kb("MemTotal")
    mem_avail = kb("MemAvailable")
    mem_free = kb("MemFree")
    swap_total = kb("SwapTotal")
    swap_free = kb("SwapFree")
    swap_used = max(0, swap_total - swap_free)

    return {
        "mem_total_gib": gib(mem_total),
        "mem_avail_gib": gib(mem_avail),
        "mem_avail_pct": round(100.0 * mem_avail / mem_total, 1) if mem_total > 0 else 0.0,
        "mem_free_gib": gib(mem_free),
        "cached_gib": gib(kb("Cached")),
        "dirty_gib": gib(kb("Dirty")),
        "slab_reclaimable_gib": gib(kb("SReclaimable")),
        "slab_unreclaimable_gib": gib(kb("SUnreclaim")),
        "swap_total_gib": gib(swap_total),
        "swap_used_gib": gib(swap_used),
        "swap_used_pct": round(100.0 * swap_used / swap_total, 1) if swap_total > 0 else 0.0,
    }


def read_meminfo(proc_root: str = "/proc") -> dict[str, float]:
    return parse_meminfo(read_text(f"{proc_root}/meminfo") or "")


def parse_vmstat(txt: str) -> dict[str, int]:
    wanted = set(VMSTAT_KEYS)
    out = {k: 0 for k in VMSTAT_KEYS}
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in wanted:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def read_vmstat(proc_root: str = "/proc") -> dict[str, int]:
    return parse_vmstat(read_text(f"{proc_root}/vmstat") or "")


def compute_vmstat_deltas(v0: dict[str, int], v1: dict[str, int]) -> dict[str, int]:
    return {
        "swap_in_pages": max(0, v1.get("pswpin", 0) - v0.get("pswpin", 0)),
        "swap_out_pages": max(0, v1.get("pswpout", 0) - v0.get("pswpout", 0)),
        "pgscan_kswapd_delta": max(0, v1.get("pgscan_kswapd", 0) - v0.get("pgscan_kswapd", 0)),
        "pgscan_direct_delta": max(0, v1.get("pgscan_direct", 0) - v0.get("pgscan_direct", 0)),
        "pgsteal_kswapd_delta": max(0, v1.get("pgsteal_kswapd", 0) - v0.get("pgsteal_kswapd", 0)),
        "pgsteal_direct_delta": max(0, v1.get("pgsteal_direct", 0) - v0.get("pgsteal_direct", 0)),
        "oom_kill_delta": max(0, v1.get("oom_kill", 0) - v0.get("oom_kill", 0)),
    }


def parse_psi_memory(txt: str) -> dict[str, dict[str, float]]:
    entry: dict[str, dict[str, float]] = {}
    for line in txt.splitlines():
        parts = line.split()
        if not parts:
            continue
        scope = parts[0]
        if scope not in ("some", "full"):
            continue
        vals: dict[str, float] = {}
        for tok in parts[1:]:
            if "=" not in tok:
                continue
            k, v = tok.split("=", 1)
            if k in ("avg10", "avg60", "avg300"):
                try:
                    vals[k] = float(v)
                except ValueError:
                    pass
        if vals:
            entry[scope] = vals
    return entry


def read_psi_memory(proc_root: str = "/proc") -> dict[str, dict[str, float]]:
    txt = read_text(f"{proc_root}/pressure/memory")
    if not txt:
        return {}
    return parse_psi_memory(txt)


def parse_proc_stat(stat_txt: str) -> tuple[str, str, int, int] | None:
    """Return (comm, state, ppid, starttime) from /proc/<pid>/stat."""
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


def parse_proc_status(status_txt: str) -> dict[str, int]:
    """Parse /proc/<pid>/status memory fields (in kB) and thread count."""
    out = {
        "rss_kb": 0,
        "anon_kb": 0,
        "file_kb": 0,
        "shmem_kb": 0,
        "swap_kb": 0,
        "threads": 0,
    }
    key_map = {
        "VmRSS": "rss_kb",
        "RssAnon": "anon_kb",
        "RssFile": "file_kb",
        "RssShmem": "shmem_kb",
        "VmSwap": "swap_kb",
        "Threads": "threads",
    }
    for line in status_txt.splitlines():
        if ":" not in line:
            continue
        k, rest = line.split(":", 1)
        dest = key_map.get(k.strip())
        if dest is None:
            continue
        toks = rest.strip().split()
        if toks:
            try:
                out[dest] = max(0, int(toks[0]))
            except ValueError:
                pass
    return out


def parse_cgroup(cgroup_txt: str) -> str:
    """Extract the most informative cgroup path (v2 unified preferred, then v1 memory)."""
    if not cgroup_txt:
        return "-"
    v2_path: str | None = None
    v1_mem_path: str | None = None
    first_non_root: str | None = None
    any_path: str | None = None

    for raw_line in cgroup_txt.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        hier_id, controllers, path = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not path:
            continue
        if any_path is None:
            any_path = path
        if first_non_root is None and path != "/":
            first_non_root = path
        if hier_id == "0" and controllers == "":
            v2_path = path
        elif "memory" in controllers.split(","):
            v1_mem_path = path

    return v2_path or v1_mem_path or first_non_root or any_path or "-"


def read_proc_cgroup(pid: int, proc_root: str = "/proc") -> str:
    txt = read_text(f"{proc_root}/{pid}/cgroup")
    if not txt:
        return "-"
    return parse_cgroup(txt)


def format_elapsed(seconds: float) -> str:
    sec = max(0, int(seconds))
    days, rem = divmod(sec, 86400)
    hrs, rem = divmod(rem, 3600)
    mins, s = divmod(rem, 60)
    if days > 0:
        return f"{days}d{hrs:02d}h{mins:02d}m"
    if hrs > 0:
        return f"{hrs}h{mins:02d}m{s:02d}s"
    return f"{mins:02d}m{s:02d}s"


def short_cgroup(cg: str, max_len: int = 32) -> str:
    if len(cg) <= max_len:
        return cg
    parts = [p for p in cg.split("/") if p]
    if parts:
        tail = "/" + parts[-1]
        if len(tail) <= max_len:
            return "..." + tail if len(tail) + 3 <= max_len else tail
    return "..." + cg[-(max_len - 3) :]


@dataclasses.dataclass
class ProcMemSnap:
    pid: int
    ppid: int
    comm: str
    starttime: int
    rss_kb: int
    anon_kb: int
    file_kb: int
    shmem_kb: int
    swap_kb: int
    threads: int
    oom_score: int
    oom_score_adj: int
    cmdline: str


def snapshot_procs(full_detail: bool = True, proc_root: str = "/proc") -> dict[int, ProcMemSnap]:
    snaps: dict[int, ProcMemSnap] = {}
    my_pid = os.getpid()
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return snaps

    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == my_pid:
            continue
        pdir = f"{proc_root}/{pid}"

        st_txt = read_text(f"{pdir}/stat")
        if not st_txt:
            continue
        parsed_stat = parse_proc_stat(st_txt)
        if not parsed_stat:
            continue
        comm, _state, ppid, starttime = parsed_stat

        status_txt = read_text(f"{pdir}/status")
        if not status_txt:
            continue
        st_mem = parse_proc_status(status_txt)

        if full_detail:
            raw_cmd = read_bytes(f"{pdir}/cmdline")
            if raw_cmd:
                cmdline = raw_cmd.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
            else:
                cmdline = f"[{comm}]"
            oom_score = read_proc_int(f"{pdir}/oom_score", default=0)
            oom_score_adj = read_proc_int(f"{pdir}/oom_score_adj", default=0)
        else:
            cmdline = ""
            oom_score = 0
            oom_score_adj = 0

        snaps[pid] = ProcMemSnap(
            pid=pid,
            ppid=ppid,
            comm=comm,
            starttime=starttime,
            rss_kb=st_mem["rss_kb"],
            anon_kb=st_mem["anon_kb"],
            file_kb=st_mem["file_kb"],
            shmem_kb=st_mem["shmem_kb"],
            swap_kb=st_mem["swap_kb"],
            threads=st_mem["threads"],
            oom_score=oom_score,
            oom_score_adj=oom_score_adj,
            cmdline=cmdline,
        )
    return snaps


def _format_proc_labels(rows: list[dict[str, Any]], limit: int = 3, detail: str = "rss") -> str:
    labels: list[str] = []
    for p in rows[:limit]:
        pid = p.get("pid", 0)
        comm = p.get("comm", "?")
        if detail == "growth":
            labels.append(
                f"{pid}:{comm}(+{p.get('rss_delta_mib', 0.0):.1f} MiB, {p.get('rss_rate_mibs', 0.0):.1f} MiB/s)"
            )
        elif detail == "swap":
            labels.append(f"{pid}:{comm}(swap={p.get('swap_mib', 0.0):.1f} MiB, rss={p.get('rss_mib', 0.0):.1f} MiB)")
        elif detail == "oom":
            labels.append(
                f"{pid}:{comm}(oom_score={p.get('oom_score', 0)}, rss={p.get('rss_mib', 0.0):.1f} MiB)"
            )
        else:
            labels.append(f"{pid}:{comm}(rss={p.get('rss_mib', 0.0):.1f} MiB)")
    return ", ".join(labels)


def classify_memory_verdict(
    mem: dict[str, float | int],
    psi: dict[str, Any],
    top_rss: list[dict[str, Any]],
    top_growers: list[dict[str, Any]],
    top_swap: list[dict[str, Any]],
    top_oom_risk: list[dict[str, Any]],
    dt_sec: float = 2.0,
) -> dict[str, Any]:
    """Deterministic memory pressure, swap thrashing, leak risk, and OOM classifier."""
    mem_total_gib = float(mem.get("mem_total_gib", 0.0))
    mem_avail_gib = float(mem.get("mem_avail_gib", 0.0))
    mem_avail_pct = float(mem.get("mem_avail_pct", 100.0))
    swap_total_gib = float(mem.get("swap_total_gib", 0.0))
    swap_used_pct = float(mem.get("swap_used_pct", 0.0))
    swap_in = int(mem.get("swap_in_pages", 0))
    swap_out = int(mem.get("swap_out_pages", 0))
    swap_pages = swap_in + swap_out
    safe_dt = max(0.2, float(dt_sec))
    swap_pages_per_sec = swap_pages / safe_dt
    pgscan_direct = int(mem.get("pgscan_direct_delta", 0))
    pgscan_kswapd = int(mem.get("pgscan_kswapd_delta", 0))
    oom_kill_delta = int(mem.get("oom_kill_delta", 0))

    mpsi = psi.get("memory", psi) if isinstance(psi, dict) else {}
    psi_some10 = float(mpsi.get("some", {}).get("avg10", 0.0))
    psi_full10 = float(mpsi.get("full", {}).get("avg10", 0.0))

    fast_growers = [
        p
        for p in top_growers
        if float(p.get("rss_rate_mibs", 0.0)) >= 50.0 and float(p.get("rss_delta_mib", 0.0)) >= 20.0
    ]
    total_growth_mib = sum(max(0.0, float(p.get("rss_delta_mib", 0.0))) for p in top_growers)
    total_growth_rate = total_growth_mib / safe_dt

    oom_imminent = (
        oom_kill_delta > 0
        or (mem_total_gib > 0.0 and mem_avail_gib < 0.5)
        or (mem_total_gib > 0.0 and mem_avail_pct < 5.0 and mem_avail_gib < 1.0)
        or (psi_full10 >= 25.0 and mem_avail_pct < 10.0)
        or (
            mem_avail_pct < 8.0
            and swap_total_gib > 0.0
            and swap_used_pct >= 95.0
            and (pgscan_direct > 0 or psi_some10 >= 10.0)
        )
    )

    swap_thrashing = (
        swap_pages >= 1000
        or swap_pages_per_sec >= 500.0
        or (swap_pages >= 200 and (psi_some10 >= 10.0 or psi_full10 >= 5.0 or pgscan_direct >= 500))
    )

    rapid_growth = bool(fast_growers) or (total_growth_rate >= 100.0 and total_growth_mib >= 50.0)

    high_pressure = (
        psi_some10 >= 10.0
        or psi_full10 >= 5.0
        or pgscan_direct >= 1000
        or (pgscan_kswapd >= 10000 and mem_avail_pct < 20.0)
        or (mem_total_gib > 0.0 and (mem_avail_pct < 15.0 or mem_avail_gib < 2.0))
    )

    reasons: list[str] = []
    recommendations: list[str] = []

    if oom_imminent:
        status = "OOM_IMMINENT"
        if oom_kill_delta > 0:
            reasons.append(f"Kernel OOM killer triggered {oom_kill_delta} time(s) during the sample window.")
        reasons.append(
            f"Critical memory exhaustion: avail={mem_avail_gib:.2f}/{mem_total_gib:.2f} GiB ({mem_avail_pct:.1f}%), "
            f"PSI some10={psi_some10:.1f}% full10={psi_full10:.1f}%, swap_used={swap_used_pct:.1f}%."
        )
        oom_targets = _format_proc_labels(top_oom_risk or top_rss, detail="oom")
        if oom_targets:
            recommendations.append(
                f"Immediately terminate or restart top OOM-risk process(es): {oom_targets}."
            )
        else:
            recommendations.append(
                "Immediately free RAM by terminating memory-heavy services or reducing concurrent workloads."
            )
        if fast_growers:
            recommendations.append(
                f"Stop rapidly growing process(es) ({_format_proc_labels(fast_growers, detail='growth')}) "
                "and enforce a cgroup MemoryMax cap."
            )
        if oom_kill_delta > 0:
            recommendations.append("Inspect kernel OOM logs (`dmesg -T | grep -i -E 'oom|killed process'`) to identify killed tasks.")

    elif swap_thrashing:
        status = "SWAP_THRASHING"
        reasons.append(
            f"Active swap thrashing: in={swap_in} out={swap_out} pages ({swap_pages_per_sec:.0f} pg/s), "
            f"swap_used={swap_used_pct:.1f}%, PSI some10={psi_some10:.1f}% full10={psi_full10:.1f}%."
        )
        if pgscan_direct > 0:
            reasons.append(f"Synchronous direct reclaim active (pgscan_direct={pgscan_direct} pages).")
        swap_targets = _format_proc_labels(top_swap or top_rss, detail="swap" if top_swap else "rss")
        if swap_targets:
            recommendations.append(
                f"Reduce memory footprint or restart top swap/RSS consumers ({swap_targets}) to halt swap I/O."
            )
        else:
            recommendations.append("Stop or throttle concurrent memory-heavy workloads to relieve swap paging.")
        if top_growers:
            recommendations.append(
                f"Check active memory growers ({_format_proc_labels(top_growers, detail='growth')}) contributing to page-outs."
            )

    elif rapid_growth:
        status = "RAPID_GROWTH_LEAK_RISK"
        grow_list = fast_growers or top_growers
        grow_labels = _format_proc_labels(grow_list, detail="growth")
        reasons.append(
            f"Rapid RSS growth detected ({total_growth_mib:.1f} MiB total, {total_growth_rate:.1f} MiB/s): {grow_labels}."
        )
        reasons.append(
            f"Available memory={mem_avail_gib:.2f}/{mem_total_gib:.2f} GiB ({mem_avail_pct:.1f}%), "
            f"PSI some10={psi_some10:.1f}%."
        )
        recommendations.append(
            f"Investigate potential memory leak or runaway allocation in {grow_labels}; "
            "profile heap usage, bound with systemd MemoryMax/cgroup limits, or restart before OOM."
        )

    elif high_pressure:
        status = "HIGH_MEMORY_PRESSURE"
        reasons.append(
            f"Elevated memory pressure: avail={mem_avail_gib:.2f}/{mem_total_gib:.2f} GiB ({mem_avail_pct:.1f}%), "
            f"PSI some10={psi_some10:.1f}% full10={psi_full10:.1f}%, "
            f"pgscan direct={pgscan_direct} kswapd={pgscan_kswapd}."
        )
        rss_targets = _format_proc_labels(top_rss or top_oom_risk, detail="rss")
        if rss_targets:
            recommendations.append(
                f"Lower concurrency or reclaim memory from largest consumers ({rss_targets}) before swap thrashing or OOM occurs."
            )
        else:
            recommendations.append(
                "Free cached/anonymous memory or reduce concurrent jobs to restore available RAM headroom."
            )
        if top_growers:
            recommendations.append(
                f"Monitor growing process(es) ({_format_proc_labels(top_growers, detail='growth')}) with --watch."
            )

    else:
        status = "HEALTHY"
        reasons.append(
            f"Memory healthy: avail={mem_avail_gib:.2f}/{mem_total_gib:.2f} GiB ({mem_avail_pct:.1f}%), "
            f"swap_io=in:{swap_in}/out:{swap_out} pg, PSI some10={psi_some10:.1f}% full10={psi_full10:.1f}%."
        )

    is_issue = status != "HEALTHY"
    if not recommendations:
        if is_issue:
            recommendations.append(
                "Inspect top RSS, growing, and high-OOM-score PIDs above; throttle or restart memory-heavy workloads."
            )
        else:
            recommendations.append("No immediate intervention needed.")

    return {
        "status": status,
        "is_issue": is_issue,
        "reasons": reasons,
        "recommendations": recommendations,
    }


def append_jsonl_log(path: str | Path, rep: dict[str, Any]) -> None:
    """Append one deterministic compact JSON line to PATH, creating parent directories if needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rep, sort_keys=True, separators=(",", ":")) + "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(line)


def watch_memory(interval: float = 2.0, top_n: int = 10, proc_root: str = "/proc") -> dict[str, Any]:
    ts_start = time.monotonic()
    vm0 = read_vmstat(proc_root=proc_root)
    procs0 = snapshot_procs(full_detail=False, proc_root=proc_root)

    time.sleep(interval)

    vm1 = read_vmstat(proc_root=proc_root)
    procs1 = snapshot_procs(full_detail=True, proc_root=proc_root)
    dt_sec = max(0.05, time.monotonic() - ts_start)

    uptime = read_uptime_sec(proc_root=proc_root)
    mem_base = read_meminfo(proc_root=proc_root)
    vm_deltas = compute_vmstat_deltas(vm0, vm1)
    memory = {**mem_base, **vm_deltas}
    psi = read_psi_memory(proc_root=proc_root)

    pids0, pids1 = set(procs0.keys()), set(procs1.keys())
    turnover = {
        "started": len(pids1 - pids0),
        "exited": len(pids0 - pids1),
        "total_procs": len(procs1),
    }

    rows: list[dict[str, Any]] = []
    for pid, p1 in procs1.items():
        p0 = procs0.get(pid)
        if p0 is not None and p0.starttime == p1.starttime:
            rss_delta_mib = round((p1.rss_kb - p0.rss_kb) / 1024.0, 2)
            rss_rate_mibs = round(rss_delta_mib / dt_sec, 2)
            swap_delta_mib = round((p1.swap_kb - p0.swap_kb) / 1024.0, 2)
        else:
            rss_delta_mib = 0.0
            rss_rate_mibs = 0.0
            swap_delta_mib = 0.0

        el_sec = max(0.0, uptime - p1.starttime / CLK_TCK)
        rows.append(
            {
                "pid": pid,
                "ppid": p1.ppid,
                "comm": p1.comm,
                "rss_mib": round(p1.rss_kb / 1024.0, 2),
                "rss_delta_mib": rss_delta_mib,
                "rss_rate_mibs": rss_rate_mibs,
                "anon_mib": round(p1.anon_kb / 1024.0, 2),
                "file_mib": round(p1.file_kb / 1024.0, 2),
                "shmem_mib": round(p1.shmem_kb / 1024.0, 2),
                "swap_mib": round(p1.swap_kb / 1024.0, 2),
                "swap_delta_mib": swap_delta_mib,
                "oom_score": p1.oom_score,
                "oom_score_adj": p1.oom_score_adj,
                "threads": p1.threads,
                "elapsed_sec": int(el_sec),
                "elapsed": format_elapsed(el_sec),
                "cgroup": "-",
                "cmdline": p1.cmdline[:160],
            }
        )

    top_rss = [
        dict(r)
        for r in sorted(
            (r for r in rows if r["rss_mib"] > 0.0),
            key=lambda x: (x["rss_mib"], x["anon_mib"], x["pid"]),
            reverse=True,
        )[:top_n]
    ]
    top_growers = [
        dict(r)
        for r in sorted(
            (r for r in rows if r["rss_delta_mib"] > 0.0),
            key=lambda x: (x["rss_delta_mib"], x["rss_rate_mibs"], x["rss_mib"]),
            reverse=True,
        )[:top_n]
    ]
    top_swap = [
        dict(r)
        for r in sorted(
            (r for r in rows if r["swap_mib"] > 0.0),
            key=lambda x: (x["swap_mib"], x["swap_delta_mib"], x["rss_mib"]),
            reverse=True,
        )[:top_n]
    ]
    top_oom_risk = [
        dict(r)
        for r in sorted(
            (r for r in rows if r["oom_score"] > 0 or r["rss_mib"] > 0.0),
            key=lambda x: (x["oom_score"], x["rss_mib"], x["oom_score_adj"]),
            reverse=True,
        )[:top_n]
    ]

    # Parse /proc/<pid>/cgroup ONLY for top ranked rows to keep hot-path procfs I/O minimal.
    cgroup_cache: dict[int, str] = {}
    for section in (top_rss, top_growers, top_swap, top_oom_risk):
        for row in section:
            pid = row["pid"]
            if pid not in cgroup_cache:
                cgroup_cache[pid] = read_proc_cgroup(pid, proc_root=proc_root)
            row["cgroup"] = cgroup_cache[pid]

    verdict = classify_memory_verdict(
        mem=memory,
        psi=psi,
        top_rss=top_rss,
        top_growers=top_growers,
        top_swap=top_swap,
        top_oom_risk=top_oom_risk,
        dt_sec=dt_sec,
    )

    return {
        "timestamp": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "sample_sec": round(dt_sec, 2),
        "verdict": verdict,
        "memory": memory,
        "psi": psi,
        "turnover": turnover,
        "top_rss": top_rss,
        "top_growers": top_growers,
        "top_swap": top_swap,
        "top_oom_risk": top_oom_risk,
    }


def render_text(rep: dict[str, Any], markdown: bool = False) -> str:
    v = rep["verdict"]
    m = rep["memory"]
    psi = rep.get("psi") or {}
    h = (lambda s: f"## {s}") if markdown else (lambda s: f"=== {s} ===")

    lines: list[str] = []
    lines.append(
        f"{'# ' if markdown else ''}Memory Investigation @ {rep['timestamp']} (window={rep['sample_sec']}s)"
    )
    lines.append(f"Status: {v['status']}")
    for r in v["reasons"]:
        lines.append(f"  - {r}")
    lines.append("")

    lines.append(h("System Memory / Swap / Reclaim / PSI"))
    lines.append(
        f"RAM   : avail={m['mem_avail_gib']:.2f}/{m['mem_total_gib']:.2f} GiB ({m['mem_avail_pct']:.1f}%) | "
        f"free={m['mem_free_gib']:.2f} GiB | cached={m['cached_gib']:.2f} GiB | dirty={m['dirty_gib']:.2f} GiB"
    )
    lines.append(
        f"Slab  : reclaimable={m['slab_reclaimable_gib']:.2f} GiB | unreclaimable={m['slab_unreclaimable_gib']:.2f} GiB"
    )
    lines.append(
        f"Swap  : used={m['swap_used_gib']:.2f}/{m['swap_total_gib']:.2f} GiB ({m['swap_used_pct']:.1f}%) | "
        f"paging in={m['swap_in_pages']} out={m['swap_out_pages']} pg"
    )
    lines.append(
        f"VMstat: pgscan direct={m['pgscan_direct_delta']} kswapd={m['pgscan_kswapd_delta']} | "
        f"pgsteal direct={m.get('pgsteal_direct_delta', 0)} kswapd={m.get('pgsteal_kswapd_delta', 0)} | "
        f"oom_kill={m['oom_kill_delta']}"
    )
    if psi:
        some = psi.get("some", {})
        full = psi.get("full", {})
        lines.append(
            f"PSI   : some(10={some.get('avg10', 0.0):.2f}%, 60={some.get('avg60', 0.0):.2f}%, 300={some.get('avg300', 0.0):.2f}%) | "
            f"full(10={full.get('avg10', 0.0):.2f}%, 60={full.get('avg60', 0.0):.2f}%, 300={full.get('avg300', 0.0):.2f}%)"
        )
    if rep.get("turnover"):
        t = rep["turnover"]
        lines.append(f"Procs : +{t['started']}/-{t['exited']} (total {t['total_procs']})")
    lines.append("")

    if rep.get("top_rss"):
        lines.append(h("Top RSS Consumers"))
        lines.append(
            f"{'PID':>8} {'RSS_M':>8} {'dRSS_M':>8} {'ANON_M':>8} {'FILE_M':>8} {'SWAP_M':>7} {'OOM':>4} {'THR':>4} {'ELAPSED':>10} {'COMM':<16} {'CGROUP':<26} CMD"
        )
        for p in rep["top_rss"]:
            lines.append(
                f"{p['pid']:>8} {p['rss_mib']:>8.1f} {p['rss_delta_mib']:>+8.1f} {p['anon_mib']:>8.1f} "
                f"{p['file_mib']:>8.1f} {p['swap_mib']:>7.1f} {p['oom_score']:>4} {p['threads']:>4} "
                f"{p['elapsed']:>10} {p['comm']:<16} {short_cgroup(p['cgroup'], 26):<26} {p['cmdline'][:64]}"
            )
        lines.append("")

    if rep.get("top_growers"):
        lines.append(h("Top Memory Growers (RSS Delta > 0)"))
        lines.append(
            f"{'PID':>8} {'dRSS_M':>8} {'RATE_M/s':>9} {'RSS_M':>8} {'ANON_M':>8} {'dSWP_M':>8} {'ELAPSED':>10} {'COMM':<16} {'CGROUP':<26} CMD"
        )
        for p in rep["top_growers"]:
            lines.append(
                f"{p['pid']:>8} {p['rss_delta_mib']:>+8.2f} {p['rss_rate_mibs']:>9.2f} {p['rss_mib']:>8.1f} "
                f"{p['anon_mib']:>8.1f} {p['swap_delta_mib']:>+8.2f} {p['elapsed']:>10} {p['comm']:<16} "
                f"{short_cgroup(p['cgroup'], 26):<26} {p['cmdline'][:64]}"
            )
        lines.append("")

    if rep.get("top_swap"):
        lines.append(h("Top Swap Consumers"))
        lines.append(
            f"{'PID':>8} {'SWAP_M':>8} {'dSWP_M':>8} {'RSS_M':>8} {'OOM':>4} {'ELAPSED':>10} {'COMM':<16} {'CGROUP':<26} CMD"
        )
        for p in rep["top_swap"]:
            lines.append(
                f"{p['pid']:>8} {p['swap_mib']:>8.1f} {p['swap_delta_mib']:>+8.2f} {p['rss_mib']:>8.1f} "
                f"{p['oom_score']:>4} {p['elapsed']:>10} {p['comm']:<16} {short_cgroup(p['cgroup'], 26):<26} {p['cmdline'][:64]}"
            )
        lines.append("")

    if rep.get("top_oom_risk"):
        lines.append(h("Top OOM Risk"))
        lines.append(
            f"{'PID':>8} {'OOM':>4} {'ADJ':>5} {'RSS_M':>8} {'SWAP_M':>7} {'dRSS_M':>8} {'ELAPSED':>10} {'COMM':<16} {'CGROUP':<26} CMD"
        )
        for p in rep["top_oom_risk"]:
            lines.append(
                f"{p['pid']:>8} {p['oom_score']:>4} {p['oom_score_adj']:>5} {p['rss_mib']:>8.1f} "
                f"{p['swap_mib']:>7.1f} {p['rss_delta_mib']:>+8.1f} {p['elapsed']:>10} {p['comm']:<16} "
                f"{short_cgroup(p['cgroup'], 26):<26} {p['cmdline'][:64]}"
            )
        lines.append("")

    lines.append(h("Recommendations"))
    for rec in v["recommendations"]:
        lines.append(f"- {rec}")
    return "\n".join(lines)


def build_parser(default_interval: float = 2.0, default_top: int = 10) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sys-mem-watch",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--interval",
        type=float,
        default=default_interval,
        help="Sampling window in seconds (0.2..60.0, env SYS_MEM_WATCH_INTERVAL, default: 2.0)",
    )
    p.add_argument(
        "--top",
        type=int,
        default=default_top,
        help="Max rows per section (1..100, env SYS_MEM_WATCH_TOP, default: 10)",
    )
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="Emit deterministic JSON report to stdout")
    fmt.add_argument("--markdown", action="store_true", help="Emit Markdown report to stdout")
    p.add_argument(
        "--watch",
        type=int,
        nargs="?",
        const=0,
        default=None,
        metavar="COUNT",
        help="Repeat sampling COUNT times (0 or omitted = run until Ctrl-C)",
    )
    p.add_argument(
        "--append-log",
        metavar="PATH",
        default=None,
        help="Append each report as a compact JSONL line to PATH (creates parent directories if needed)",
    )
    p.add_argument(
        "--exit-on-issue",
        action="store_true",
        help="Exit with code 3 when verdict status is not HEALTHY",
    )
    p.add_argument("--verbose", action="store_true", help="Print effective configuration to stderr")
    return p


def _parse_env_defaults() -> tuple[float, int]:
    raw_interval = os.environ.get("SYS_MEM_WATCH_INTERVAL", "").strip()
    raw_top = os.environ.get("SYS_MEM_WATCH_TOP", "").strip()
    try:
        default_interval = float(raw_interval) if raw_interval else 2.0
    except ValueError as e:
        raise ValueError(f"invalid SYS_MEM_WATCH_INTERVAL={raw_interval!r}") from e
    try:
        default_top = int(raw_top) if raw_top else 10
    except ValueError as e:
        raise ValueError(f"invalid SYS_MEM_WATCH_TOP={raw_top!r}") from e
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

    if not (0.2 <= args.interval <= 60.0):
        print("error: --interval must be between 0.2 and 60.0 seconds", file=sys.stderr)
        return 1
    if not (1 <= args.top <= 100):
        print("error: --top must be between 1 and 100", file=sys.stderr)
        return 1
    if args.watch is not None and args.watch < 0:
        print("error: --watch COUNT must be >= 0", file=sys.stderr)
        return 1

    if args.verbose:
        print(
            f"config: interval={args.interval}s top={args.top} "
            f"format={'json' if args.json else 'markdown' if args.markdown else 'text'} "
            f"watch={args.watch} append_log={args.append_log} "
            f"exit_on_issue={args.exit_on_issue}",
            file=sys.stderr,
        )

    runs = 1 if args.watch is None else args.watch
    i = 0
    last_issue = False
    try:
        while runs == 0 or i < runs:
            rep = watch_memory(interval=args.interval, top_n=args.top)
            last_issue = bool(rep["verdict"]["is_issue"])
            if args.append_log:
                append_jsonl_log(args.append_log, rep)
            if args.json:
                print(json.dumps(rep, indent=2, sort_keys=True), flush=True)
            else:
                if i > 0:
                    print("\n" + "=" * 78 + "\n", flush=True)
                print(render_text(rep, markdown=args.markdown), flush=True)
            i += 1
    except KeyboardInterrupt:
        return 0
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.exit_on_issue and last_issue:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
