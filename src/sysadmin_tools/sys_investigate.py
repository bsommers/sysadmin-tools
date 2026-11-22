#!/usr/bin/env python3
"""sys-investigate: diagnose what is driving CPU/load and whether it is productive.

Reads Linux /proc and /sys directly (no shelling out to top/iostat/vmstat), so it
stays responsive even when disks are severely stalled.

Examples:
  sys-investigate
  sys-investigate --interval 3 --top 15
  sys-investigate --json | jq '.verdict'
  sys-investigate --watch 5 --interval 2
  sys-investigate --exit-on-issue --json > /tmp/diag.json
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
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# Pure-procfs implementation: avoids fork/exec and disk I/O during severe stalls.

SECTOR_BYTES = 512
CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"]) if hasattr(os, "sysconf") else 100
CPU_COUNT = os.cpu_count() or 1

BUILD_OR_HEAVY_COMMS = {
    "cargo",
    "cargo-clippy",
    "rustc",
    "rust-lld",
    "ld",
    "lld",
    "mold",
    "cc1",
    "cc1plus",
    "gcc",
    "g++",
    "clang",
    "clang++",
    "ninja",
    "make",
    "cmake",
    "pytest",
    "python",
    "python3",
    "node",
    "node-MainThread",
    "go",
    "javac",
    "ffmpeg",
}


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


def read_cpu_times() -> list[int]:
    txt = read_text("/proc/stat")
    if not txt:
        return [0] * 10
    for line in txt.splitlines():
        if line.startswith("cpu "):
            parts = [int(x) for x in line.split()[1:11]]
            return parts + [0] * max(0, 10 - len(parts))
    return [0] * 10


def compute_cpu_pct(t0: list[int], t1: list[int]) -> dict[str, float]:
    deltas = [max(0, b - a) for a, b in zip(t0, t1)]
    total = sum(deltas)
    if total <= 0:
        return {k: 0.0 for k in ("us", "ni", "sy", "id", "wa", "hi", "si", "st")}
    names = ("us", "ni", "sy", "id", "wa", "hi", "si", "st")
    return {name: round(100.0 * deltas[i] / total, 1) for i, name in enumerate(names)}


def read_loadavg() -> dict[str, Any]:
    txt = read_text("/proc/loadavg") or "0 0 0 0/0 0"
    parts = txt.split()
    run_tot = parts[3].split("/") if len(parts) > 3 and "/" in parts[3] else ["0", "0"]
    return {
        "load1": float(parts[0]),
        "load5": float(parts[1]),
        "load15": float(parts[2]),
        "runnable_kernel": int(run_tot[0]),
        "entities_total": int(run_tot[1]),
        "cpus": CPU_COUNT,
    }


def read_uptime_sec() -> float:
    txt = read_text("/proc/uptime")
    if not txt:
        return 0.0
    try:
        return float(txt.split()[0])
    except ValueError:
        return 0.0


def read_psi() -> dict[str, dict[str, dict[str, float]]]:
    out: dict[str, dict[str, dict[str, float]]] = {}
    for kind in ("cpu", "io", "memory"):
        txt = read_text(f"/proc/pressure/{kind}")
        if not txt:
            continue
        entry: dict[str, dict[str, float]] = {}
        for line in txt.splitlines():
            parts = line.split()
            if not parts:
                continue
            scope = parts[0]
            vals: dict[str, float] = {}
            for tok in parts[1:]:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    try:
                        vals[k] = float(v)
                    except ValueError:
                        pass
            entry[scope] = vals
        out[kind] = entry
    return out


def read_meminfo() -> dict[str, float]:
    txt = read_text("/proc/meminfo") or ""
    raw: dict[str, int] = {}
    for line in txt.splitlines():
        if ":" not in line:
            continue
        k, rest = line.split(":", 1)
        toks = rest.strip().split()
        if toks and toks[0].isdigit():
            raw[k] = int(toks[0])  # kB
    kb = lambda k: raw.get(k, 0)
    mib = lambda k: round(kb(k) / 1024.0, 1)
    gib = lambda k: round(kb(k) / (1024.0 * 1024.0), 2)
    swap_total = kb("SwapTotal")
    swap_free = kb("SwapFree")
    return {
        "mem_total_gib": gib("MemTotal"),
        "mem_avail_gib": gib("MemAvailable"),
        "mem_free_gib": gib("MemFree"),
        "dirty_gib": gib("Dirty"),
        "dirty_mib": mib("Dirty"),
        "writeback_mib": mib("Writeback"),
        "swap_total_gib": round(swap_total / (1024.0 * 1024.0), 2),
        "swap_used_gib": round((swap_total - swap_free) / (1024.0 * 1024.0), 2),
        "swap_used_pct": round(100.0 * (swap_total - swap_free) / swap_total, 1) if swap_total > 0 else 0.0,
    }


def read_vmstat_swap() -> tuple[int, int]:
    txt = read_text("/proc/vmstat") or ""
    pswpin = pswo = 0
    for line in txt.splitlines():
        if line.startswith("pswpin "):
            pswpin = int(line.split()[1])
        elif line.startswith("pswpout "):
            pswo = int(line.split()[1])
    return pswpin, pswo


def is_whole_physical_disk(name: str) -> bool:
    if name.startswith(("loop", "ram", "zram", "dm-", "sr", "fd")):
        return False
    if name.startswith("nvme"):
        return "p" not in name.split("n", 2)[-1]
    if name.startswith(("sd", "vd", "hd", "xvd")):
        return not name[-1].isdigit()
    if name.startswith("mmcblk"):
        return "p" not in name
    return False


def read_diskstats() -> dict[str, dict[str, int]]:
    txt = read_text("/proc/diskstats") or ""
    stats: dict[str, dict[str, int]] = {}
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue
        name = parts[2]
        if not is_whole_physical_disk(name):
            continue
        stats[name] = {
            "r_ios": int(parts[3]),
            "r_sec": int(parts[5]),
            "r_ms": int(parts[6]),
            "w_ios": int(parts[7]),
            "w_sec": int(parts[9]),
            "w_ms": int(parts[10]),
            "in_flight": int(parts[11]),
            "io_ms": int(parts[12]),
            "weighted_ms": int(parts[13]),
        }
    return stats


def map_disks_to_mounts() -> dict[str, list[dict[str, Any]]]:
    txt = read_text("/proc/mounts") or ""
    mapping: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].startswith("/dev/"):
            continue
        dev_name = os.path.basename(os.path.realpath(parts[0]))
        mnt = parts[1].replace("\\040", " ")
        parent = dev_name
        if dev_name.startswith("nvme") and "p" in dev_name:
            parent = dev_name.rsplit("p", 1)[0]
        elif dev_name.startswith(("sd", "vd", "hd", "xvd")):
            parent = dev_name.rstrip("0123456789")
        elif dev_name.startswith("mmcblk") and "p" in dev_name:
            parent = dev_name.rsplit("p", 1)[0]
        if (parent, mnt) in seen:
            continue
        seen.add((parent, mnt))
        used_pct = None
        try:
            st = os.statvfs(mnt)
            if st.f_blocks > 0:
                used_pct = round(100.0 * (st.f_blocks - st.f_bfree) / st.f_blocks, 1)
        except OSError:
            pass
        mapping[parent].append({"mount": mnt, "fstype": parts[2], "used_pct": used_pct})
    return dict(mapping)


def read_disk_model(dev: str) -> str:
    m = read_text(f"/sys/block/{dev}/device/model")
    return m.strip() if m else ""


def compute_disk_deltas(
    d0: dict[str, dict[str, int]],
    d1: dict[str, dict[str, int]],
    dt_sec: float,
    mount_map: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if dt_sec <= 0:
        return rows
    for dev, s1 in sorted(d1.items()):
        s0 = d0.get(dev)
        if not s0:
            continue
        r_ios = max(0, s1["r_ios"] - s0["r_ios"])
        w_ios = max(0, s1["w_ios"] - s0["w_ios"])
        r_sec = max(0, s1["r_sec"] - s0["r_sec"])
        w_sec = max(0, s1["w_sec"] - s0["w_sec"])
        r_ms = max(0, s1["r_ms"] - s0["r_ms"])
        w_ms = max(0, s1["w_ms"] - s0["w_ms"])
        io_ms = max(0, s1["io_ms"] - s0["io_ms"])
        w_q_ms = max(0, s1["weighted_ms"] - s0["weighted_ms"])

        util_pct = min(100.0, round(100.0 * io_ms / (dt_sec * 1000.0), 1))
        aqu_sz = round(w_q_ms / (dt_sec * 1000.0), 2)
        r_mbs = round((r_sec * SECTOR_BYTES) / (1e6 * dt_sec), 2)
        w_mbs = round((w_sec * SECTOR_BYTES) / (1e6 * dt_sec), 2)
        r_await = round(r_ms / r_ios, 1) if r_ios > 0 else 0.0
        w_await = round(w_ms / w_ios, 1) if w_ios > 0 else 0.0

        rows.append(
            {
                "device": dev,
                "model": read_disk_model(dev),
                "mounts": mount_map.get(dev, []),
                "r_iops": round(r_ios / dt_sec, 1),
                "w_iops": round(w_ios / dt_sec, 1),
                "r_mbs": r_mbs,
                "w_mbs": w_mbs,
                "r_await_ms": r_await,
                "w_await_ms": w_await,
                "aqu_sz": aqu_sz,
                "in_flight": s1["in_flight"],
                "util_pct": util_pct,
            }
        )
    rows.sort(key=lambda r: (r["util_pct"], r["aqu_sz"], r["w_mbs"] + r["r_mbs"]), reverse=True)
    return rows


def read_gpu_busy() -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    drm = Path("/sys/class/drm")
    if not drm.exists():
        return gpus
    for card in sorted(drm.glob("card[0-9]*")):
        if "-" in card.name:
            continue
        busy_txt = read_text(card / "device" / "gpu_busy_percent")
        if busy_txt is None:
            continue
        try:
            busy = int(busy_txt.strip())
        except ValueError:
            continue
        gpus.append({"card": card.name, "busy_pct": busy})
    return gpus


def parse_proc_stat(stat_txt: str) -> tuple[str, str, int, int, int, int] | None:
    """Return (comm, state, ppid, utime, stime, starttime) from /proc/<pid>/stat."""
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
        utime = int(fields[11])
        stime = int(fields[12])
        starttime = int(fields[19])
        return comm, state, ppid, utime, stime, starttime
    except ValueError:
        return None


@dataclasses.dataclass
class ProcSnap:
    pid: int
    ppid: int
    comm: str
    state: str
    cpu_ticks: int
    starttime: int
    cmdline: str
    read_bytes: int | None
    write_bytes: int | None
    rchar: int | None
    wchar: int | None
    rss_mib: float
    d_threads: dict[str, int]
    r_threads: int


def snapshot_procs(inspect_threads: bool = True) -> dict[int, ProcSnap]:
    snaps: dict[int, ProcSnap] = {}
    my_pid = os.getpid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return snaps

    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == my_pid:
            continue
        pdir = f"/proc/{pid}"
        st_txt = read_text(f"{pdir}/stat")
        if not st_txt:
            continue
        parsed = parse_proc_stat(st_txt)
        if not parsed:
            continue
        comm, state, ppid, utime, stime, starttime = parsed

        raw_cmd = read_bytes(f"{pdir}/cmdline")
        if raw_cmd:
            cmdline = raw_cmd.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        else:
            cmdline = f"[{comm}]"

        rss_mib = 0.0
        statm = read_text(f"{pdir}/statm")
        if statm:
            parts = statm.split()
            if len(parts) >= 2 and parts[1].isdigit():
                rss_mib = round(int(parts[1]) * os.sysconf("SC_PAGE_SIZE") / (1024.0 * 1024.0), 1)

        rb = wb = rc = wc = None
        io_txt = read_text(f"{pdir}/io")
        if io_txt:
            vals: dict[str, int] = {}
            for line in io_txt.splitlines():
                if ": " in line:
                    k, v = line.split(": ", 1)
                    if v.isdigit():
                        vals[k] = int(v)
            rb = vals.get("read_bytes")
            wb = vals.get("write_bytes")
            rc = vals.get("rchar")
            wc = vals.get("wchar")

        d_threads: dict[str, int] = {}
        r_threads = 0
        if inspect_threads:
            try:
                tids = os.listdir(f"{pdir}/task")
            except OSError:
                tids = []
            for tid in tids:
                tst = read_text(f"{pdir}/task/{tid}/stat")
                if not tst:
                    continue
                rp = tst.rfind(")")
                if rp == -1 or rp + 2 >= len(tst):
                    continue
                tstate = tst[rp + 2]
                if tstate == "D":
                    wchan = (read_text(f"{pdir}/task/{tid}/wchan") or "-").strip() or "-"
                    d_threads[wchan] = d_threads.get(wchan, 0) + 1
                elif tstate == "R":
                    r_threads += 1

        snaps[pid] = ProcSnap(
            pid=pid,
            ppid=ppid,
            comm=comm,
            state=state,
            cpu_ticks=utime + stime,
            starttime=starttime,
            cmdline=cmdline,
            read_bytes=rb,
            write_bytes=wb,
            rchar=rc,
            wchar=wc,
            rss_mib=rss_mib,
            d_threads=d_threads,
            r_threads=r_threads,
        )
    return snaps


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


def build_ancestry(pid: int, snaps: dict[int, ProcSnap], uptime: float, max_depth: int = 8) -> str:
    chain: list[str] = []
    cur = pid
    seen: set[int] = set()
    while cur in snaps and cur not in seen and len(chain) < max_depth:
        seen.add(cur)
        p = snaps[cur]
        el = format_elapsed(uptime - p.starttime / CLK_TCK)
        chain.append(f"{p.pid}:{p.comm}({el})")
        if p.ppid <= 1:
            break
        cur = p.ppid
    return " <- ".join(chain)


def detect_overlapping_workloads(snaps: dict[int, ProcSnap], uptime: float) -> list[dict[str, Any]]:
    """Detect duplicate/concurrent build, test, or script families competing for resources."""
    groups: dict[str, list[ProcSnap]] = defaultdict(list)
    for p in snaps.values():
        cmd = p.cmdline
        if not cmd or cmd.startswith("["):
            continue
        toks = cmd.split()
        if p.comm in ("bash", "sh", "zsh") and len(toks) >= 2 and not toks[1].startswith("-"):
            key = f"script:{os.path.basename(toks[1])}"
            groups[key].append(p)
        elif p.comm in ("cargo", "cargo-clippy", "rustc", "rust-lld", "pytest", "ninja", "make"):
            groups[f"tool:{p.comm}"].append(p)

    findings: list[dict[str, Any]] = []
    for key, plist in sorted(groups.items()):
        min_count = 2 if key.startswith("script:") or key in ("tool:cargo", "tool:pytest", "tool:make", "tool:ninja") else 4
        if len(plist) < min_count:
            continue
        plist_sorted = sorted(plist, key=lambda x: x.starttime)
        parents = Counter(p.ppid for p in plist_sorted)
        findings.append(
            {
                "group": key,
                "count": len(plist_sorted),
                "pids": [p.pid for p in plist_sorted[:12]],
                "oldest_elapsed": format_elapsed(uptime - plist_sorted[0].starttime / CLK_TCK),
                "shared_parent_top": parents.most_common(1)[0][0] if parents else 0,
                "sample_cmds": [p.cmdline[:140] for p in plist_sorted[:5]],
                "sample_trees": [build_ancestry(p.pid, snaps, uptime) for p in plist_sorted[:3]],
            }
        )
    findings.sort(key=lambda x: (x["group"].startswith("script:"), x["count"]), reverse=True)
    return findings


def classify_verdict(
    cpu: dict[str, float],
    load: dict[str, Any],
    psi: dict[str, Any],
    mem: dict[str, float],
    swap_delta: tuple[int, int],
    disks: list[dict[str, Any]],
    d_state_total: int,
    d_wchan_counts: Counter[str],
    top_cpu: list[dict[str, Any]],
    overlaps: list[dict[str, Any]],
    turnover: dict[str, int],
) -> dict[str, Any]:
    wa = cpu.get("wa", 0.0)
    busy_cpu = cpu.get("us", 0.0) + cpu.get("sy", 0.0) + cpu.get("ni", 0.0)
    io_some10 = psi.get("io", {}).get("some", {}).get("avg10", 0.0)
    mem_some10 = psi.get("memory", {}).get("some", {}).get("avg10", 0.0)
    cpu_some10 = psi.get("cpu", {}).get("some", {}).get("avg10", 0.0)

    sat_disks = [d for d in disks if d["util_pct"] >= 85.0 or d["w_await_ms"] >= 50.0 or d["aqu_sz"] >= 8.0]
    dirty_gb = mem["dirty_gib"]
    bdp_threads = d_wchan_counts.get("balance_dirty_pages", 0)
    swap_pages_delta = swap_delta[0] + swap_delta[1]

    io_bound = wa >= 20.0 or io_some10 >= 30.0 or bool(sat_disks) or d_state_total >= 16
    mem_thrash = mem_some10 >= 20.0 or swap_pages_delta >= 1000 or mem["mem_avail_gib"] < 1.0
    cpu_bound = busy_cpu >= 70.0 or cpu_some10 >= 30.0

    if mem_thrash and io_bound:
        bottleneck = "MEMORY_SWAP_AND_IO_THRASH"
    elif mem_thrash:
        bottleneck = "MEMORY_SWAP_THRASH"
    elif io_bound and cpu_bound:
        bottleneck = "MIXED_CPU_AND_IO"
    elif io_bound:
        bottleneck = "IO_WAIT_SATURATION"
    elif cpu_bound:
        bottleneck = "CPU_COMPUTE_BOUND"
    else:
        bottleneck = "NORMAL_OR_IDLE"

    reasons: list[str] = []
    recommendations: list[str] = []

    concurrent_heavy = [
        o for o in overlaps if o["group"].startswith("script:") or o["group"] in ("tool:cargo", "tool:rust-lld", "tool:rustc")
    ]
    total_disk_mbs = sum(d["r_mbs"] + d["w_mbs"] for d in (sat_disks or disks))
    worst_w_await = max((d["w_await_ms"] for d in disks), default=0.0)
    worst_util = max((d["util_pct"] for d in disks), default=0.0)

    if bottleneck == "NORMAL_OR_IDLE":
        productivity = "HEALTHY_IDLE_OR_LIGHT"
        reasons.append(f"CPU active={busy_cpu:.1f}%, iowait={wa:.1f}%, load1={load['load1']:.2f}/{load['cpus']} cores.")
    elif bottleneck in ("IO_WAIT_SATURATION", "MEMORY_SWAP_AND_IO_THRASH", "MIXED_CPU_AND_IO"):
        thrashing = (
            bdp_threads >= 4
            or dirty_gb >= 2.0
            or (worst_util >= 90.0 and worst_w_await >= 100.0)
            or mem_thrash
        )
        making_progress = turnover["started"] > 0 or turnover["exited"] > 0 or total_disk_mbs >= 5.0
        if thrashing and not making_progress:
            productivity = "WEDGED_OR_STALLED"
            reasons.append("Severe disk/memory stall with negligible throughput or process turnover in sample window.")
        elif thrashing:
            productivity = "THRASHING_LOW_EFFICIENCY"
            reasons.append(
                f"Making slow forward progress ({turnover['started']} started, {turnover['exited']} exited in window) "
                f"but heavily throttled by I/O contention."
            )
        else:
            productivity = "PRODUCTIVE_IO"
            reasons.append(f"Active I/O workload moving {total_disk_mbs:.1f} MB/s without severe writeback stall.")

        if dirty_gb >= 2.0 or bdp_threads > 0:
            reasons.append(
                f"Dirty page backlog={dirty_gb:.2f} GiB with {bdp_threads} thread(s) stalled in balance_dirty_pages."
            )
        for d in sat_disks[:3]:
            mnts = ",".join(m["mount"] for m in d["mounts"]) or "unmounted"
            reasons.append(
                f"{d['device']} ({mnts}): util={d['util_pct']}%, aqu-sz={d['aqu_sz']}, "
                f"w_await={d['w_await_ms']}ms, w={d['w_mbs']} MB/s, r={d['r_mbs']} MB/s."
            )
        if concurrent_heavy:
            summary = ", ".join(f"{o['group']} x{o['count']}" for o in concurrent_heavy[:4])
            reasons.append(f"Overlapping build/test workloads detected: {summary}.")
            recommendations.append("Serialize overlapping build/verify scripts so only one runs per SSD at a time.")
            if any("cargo" in o["group"] or "rust" in o["group"] for o in concurrent_heavy):
                recommendations.append("Cap Cargo parallelism (e.g. CARGO_BUILD_JOBS=4..8) to avoid SSD writeback collapse.")
        if mem_thrash:
            recommendations.append("Reduce concurrent memory consumers to stop swap/page-cache thrashing.")
    elif bottleneck == "MEMORY_SWAP_THRASH":
        productivity = "THRASHING_LOW_EFFICIENCY"
        reasons.append(f"Memory PSI some10={mem_some10}%, swap pages delta={swap_pages_delta}, avail={mem['mem_avail_gib']} GiB.")
        recommendations.append("Kill or throttle largest RSS processes to relieve memory pressure.")
    else:
        single_spinners = [p for p in top_cpu if p["cpu_pct"] >= 95.0 and p["io_total_mbs"] == 0.0 and p["elapsed_sec"] >= 300]
        if single_spinners and busy_cpu < 15.0 * len(single_spinners) and turnover["started"] == 0 and turnover["exited"] == 0:
            productivity = "SUSPECT_SPIN_LOOP"
            reasons.append(
                f"Long-running CPU spinner(s) with zero I/O: "
                + ", ".join(f"{p['pid']}:{p['comm']}({p['cpu_pct']}%)" for p in single_spinners[:3])
            )
            recommendations.append("Inspect suspected spin-loop PIDs with strace/perf or terminate if unintended.")
        else:
            productivity = "PRODUCTIVE_COMPUTE"
            reasons.append(f"CPU actively computing ({busy_cpu:.1f}% busy, iowait={wa:.1f}%, load1={load['load1']:.2f}).")

    if not recommendations:
        recommendations.append("No immediate intervention needed.")

    is_issue = productivity in ("THRASHING_LOW_EFFICIENCY", "WEDGED_OR_STALLED", "SUSPECT_SPIN_LOOP")
    return {
        "bottleneck": bottleneck,
        "productivity": productivity,
        "is_issue": is_issue,
        "reasons": reasons,
        "recommendations": recommendations,
    }


def investigate(interval: float = 2.0, top_n: int = 10) -> dict[str, Any]:
    ts_start = time.monotonic()
    cpu0 = read_cpu_times()
    disk0 = read_diskstats()
    swap0 = read_vmstat_swap()
    procs0 = snapshot_procs(inspect_threads=False)

    time.sleep(interval)

    cpu1 = read_cpu_times()
    disk1 = read_diskstats()
    swap1 = read_vmstat_swap()
    procs1 = snapshot_procs(inspect_threads=True)
    dt_sec = max(0.05, time.monotonic() - ts_start)

    uptime = read_uptime_sec()
    cpu_pct = compute_cpu_pct(cpu0, cpu1)
    load = read_loadavg()
    psi = read_psi()
    mem = read_meminfo()
    mount_map = map_disks_to_mounts()
    disks = compute_disk_deltas(disk0, disk1, dt_sec, mount_map)
    gpus = read_gpu_busy()

    swap_delta = (max(0, swap1[0] - swap0[0]), max(0, swap1[1] - swap0[1]))
    pids0, pids1 = set(procs0.keys()), set(procs1.keys())
    turnover = {
        "started": len(pids1 - pids0),
        "exited": len(pids0 - pids1),
        "total_procs": len(procs1),
    }

    proc_metrics: list[dict[str, Any]] = []
    d_groups: list[dict[str, Any]] = []
    d_wchan_counts: Counter[str] = Counter()
    d_state_total = 0
    r_state_total = 0

    for pid, p1 in procs1.items():
        r_state_total += p1.r_threads
        if p1.d_threads:
            for wchan, cnt in p1.d_threads.items():
                d_state_total += cnt
                d_wchan_counts[wchan] += cnt
                d_groups.append(
                    {
                        "threads": cnt,
                        "pid": pid,
                        "comm": p1.comm,
                        "wchan": wchan,
                        "elapsed": format_elapsed(uptime - p1.starttime / CLK_TCK),
                        "cmdline": p1.cmdline[:160],
                    }
                )

        p0 = procs0.get(pid)
        if not p0 or p0.starttime != p1.starttime:
            continue
        dticks = max(0, p1.cpu_ticks - p0.cpu_ticks)
        pct = round(100.0 * (dticks / CLK_TCK) / dt_sec, 1)

        r_mbs = w_mbs = wchar_mbs = 0.0
        if p1.read_bytes is not None and p0.read_bytes is not None:
            r_mbs = round(max(0, p1.read_bytes - p0.read_bytes) / (1e6 * dt_sec), 2)
        if p1.write_bytes is not None and p0.write_bytes is not None:
            w_mbs = round(max(0, p1.write_bytes - p0.write_bytes) / (1e6 * dt_sec), 2)
        if p1.wchar is not None and p0.wchar is not None:
            wchar_mbs = round(max(0, p1.wchar - p0.wchar) / (1e6 * dt_sec), 2)

        el_sec = max(0.0, uptime - p1.starttime / CLK_TCK)
        if pct >= 0.5 or (r_mbs + w_mbs) >= 0.05 or wchar_mbs >= 1.0:
            proc_metrics.append(
                {
                    "pid": pid,
                    "comm": p1.comm,
                    "state": p1.state,
                    "cpu_pct": pct,
                    "rss_mib": p1.rss_mib,
                    "r_mbs": r_mbs,
                    "w_mbs": w_mbs,
                    "io_total_mbs": round(r_mbs + w_mbs, 2),
                    "wchar_mbs": wchar_mbs,
                    "elapsed_sec": int(el_sec),
                    "elapsed": format_elapsed(el_sec),
                    "cmdline": p1.cmdline[:160],
                    "ancestry": build_ancestry(pid, procs1, uptime),
                }
            )

    top_cpu = sorted(proc_metrics, key=lambda x: (x["cpu_pct"], x["io_total_mbs"]), reverse=True)[:top_n]
    top_io = sorted(
        [m for m in proc_metrics if m["io_total_mbs"] > 0 or m["wchar_mbs"] >= 1.0],
        key=lambda x: (x["io_total_mbs"], x["wchar_mbs"]),
        reverse=True,
    )[:top_n]
    d_groups.sort(key=lambda x: (x["threads"], x["comm"], x["pid"]), reverse=True)

    overlaps = detect_overlapping_workloads(procs1, uptime)
    verdict = classify_verdict(
        cpu=cpu_pct,
        load=load,
        psi=psi,
        mem=mem,
        swap_delta=swap_delta,
        disks=disks,
        d_state_total=d_state_total,
        d_wchan_counts=d_wchan_counts,
        top_cpu=top_cpu,
        overlaps=overlaps,
        turnover=turnover,
    )

    return {
        "timestamp": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "sample_sec": round(dt_sec, 2),
        "verdict": verdict,
        "cpu_pct": cpu_pct,
        "load": {**load, "r_threads": r_state_total, "d_threads": d_state_total},
        "psi": psi,
        "memory": {**mem, "swap_in_pages": swap_delta[0], "swap_out_pages": swap_delta[1]},
        "turnover": turnover,
        "disks": disks,
        "gpus": gpus,
        "d_state_wchans": dict(d_wchan_counts.most_common(10)),
        "d_state_top": d_groups[:top_n],
        "top_cpu": top_cpu,
        "top_io": top_io,
        "overlapping_workloads": overlaps[:top_n],
    }


def render_text(rep: dict[str, Any], markdown: bool = False) -> str:
    v = rep["verdict"]
    c = rep["cpu_pct"]
    l = rep["load"]
    m = rep["memory"]
    psi = rep["psi"]
    h = (lambda s: f"## {s}") if markdown else (lambda s: f"=== {s} ===")

    lines: list[str] = []
    lines.append(
        f"{'# ' if markdown else ''}System Investigation @ {rep['timestamp']} (window={rep['sample_sec']}s)"
    )
    lines.append(f"Bottleneck  : {v['bottleneck']}")
    lines.append(f"Productivity: {v['productivity']}")
    for r in v["reasons"]:
        lines.append(f"  - {r}")
    lines.append("")

    lines.append(h("CPU / Load / PSI / Memory"))
    lines.append(
        f"CPU  : us={c['us']}% sy={c['sy']}% wa={c['wa']}% id={c['id']}% (ni={c['ni']}% hi={c['hi']}% si={c['si']}%)"
    )
    lines.append(
        f"Load : {l['load1']:.2f}, {l['load5']:.2f}, {l['load15']:.2f} ({l['cpus']} cores) | "
        f"threads R={l['r_threads']} D={l['d_threads']} | "
        f"proc turnover +{rep['turnover']['started']}/-{rep['turnover']['exited']} (total {rep['turnover']['total_procs']})"
    )
    if psi:
        psi_parts = []
        for k in ("cpu", "io", "memory"):
            if k in psi:
                s10 = psi[k].get("some", {}).get("avg10", 0.0)
                f10 = psi[k].get("full", {}).get("avg10", 0.0)
                psi_parts.append(f"{k}(some10={s10}%,full10={f10}%)")
        lines.append("PSI  : " + "  ".join(psi_parts))
    lines.append(
        f"Mem  : avail={m['mem_avail_gib']}/{m['mem_total_gib']} GiB | "
        f"Dirty={m['dirty_gib']} GiB ({m['dirty_mib']} MiB) Writeback={m['writeback_mib']} MiB | "
        f"Swap={m['swap_used_gib']}/{m['swap_total_gib']} GiB ({m['swap_used_pct']}%, "
        f"in={m['swap_in_pages']} out={m['swap_out_pages']} pg)"
    )
    if rep["gpus"]:
        lines.append("GPU  : " + "  ".join(f"{g['card']}={g['busy_pct']}%" for g in rep["gpus"]))
    lines.append("")

    if rep["disks"]:
        lines.append(h("Disk I/O"))
        lines.append(f"{'DEV':<10} {'UTIL%':>6} {'AQU':>7} {'W_MS':>8} {'R_MS':>7} {'W_MB/s':>8} {'R_MB/s':>8}  MOUNTS / MODEL")
        for d in rep["disks"]:
            mnts = ",".join(
                f"{x['mount']}({x['used_pct']}%)" if x["used_pct"] is not None else x["mount"]
                for x in d["mounts"]
            ) or "-"
            lines.append(
                f"{d['device']:<10} {d['util_pct']:>6.1f} {d['aqu_sz']:>7.2f} {d['w_await_ms']:>8.1f} "
                f"{d['r_await_ms']:>7.1f} {d['w_mbs']:>8.2f} {d['r_mbs']:>8.2f}  {mnts} {d['model']}"
            )
        lines.append("")

    if rep["d_state_top"]:
        lines.append(h(f"Blocked (D-State) Threads (total={l['d_threads']})"))
        if rep["d_state_wchans"]:
            lines.append("Wchans: " + ", ".join(f"{k}={v}" for k, v in rep["d_state_wchans"].items()))
        lines.append(f"{'THRD':>5} {'PID':>8} {'ELAPSED':>10} {'COMM':<16} {'WCHAN':<24} CMD")
        for g in rep["d_state_top"]:
            lines.append(
                f"{g['threads']:>5} {g['pid']:>8} {g['elapsed']:>10} {g['comm']:<16} {g['wchan']:<24} {g['cmdline'][:90]}"
            )
        lines.append("")

    if rep["top_cpu"]:
        lines.append(h("Top CPU Consumers (instantaneous %)"))
        lines.append(f"{'PID':>8} {'%CPU':>6} {'RSS_M':>7} {'W_MB/s':>7} {'R_MB/s':>7} {'ELAPSED':>10} {'COMM':<16} CMD")
        for p in rep["top_cpu"]:
            lines.append(
                f"{p['pid']:>8} {p['cpu_pct']:>6.1f} {p['rss_mib']:>7.1f} {p['w_mbs']:>7.2f} "
                f"{p['r_mbs']:>7.2f} {p['elapsed']:>10} {p['comm']:<16} {p['cmdline'][:80]}"
            )
        lines.append("")

    if rep["top_io"]:
        lines.append(h("Top Process I/O (readable /proc/*/io)"))
        lines.append(f"{'PID':>8} {'W_MB/s':>8} {'R_MB/s':>8} {'WCH_M/s':>8} {'ELAPSED':>10} {'COMM':<16} CMD")
        for p in rep["top_io"]:
            lines.append(
                f"{p['pid']:>8} {p['w_mbs']:>8.2f} {p['r_mbs']:>8.2f} {p['wchar_mbs']:>8.2f} "
                f"{p['elapsed']:>10} {p['comm']:<16} {p['cmdline'][:80]}"
            )
        lines.append("")

    if rep["overlapping_workloads"]:
        lines.append(h("Overlapping / Concurrent Workloads"))
        for o in rep["overlapping_workloads"]:
            lines.append(
                f"- {o['group']} x{o['count']} (oldest={o['oldest_elapsed']}, pids={o['pids'][:6]})"
            )
            for t in o["sample_trees"][:2]:
                lines.append(f"    tree: {t}")
        lines.append("")

    lines.append(h("Recommendations"))
    for rec in v["recommendations"]:
        lines.append(f"- {rec}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    env_interval = os.environ.get("SYS_INVESTIGATE_INTERVAL", "2.0")
    env_top = os.environ.get("SYS_INVESTIGATE_TOP", "10")
    p = argparse.ArgumentParser(
        prog="sys-investigate",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--interval",
        type=float,
        default=float(env_interval) if env_interval else 2.0,
        help="Sampling window in seconds (0.2..60.0, env SYS_INVESTIGATE_INTERVAL, default: 2.0)",
    )
    p.add_argument(
        "--top",
        type=int,
        default=int(env_top) if env_top else 10,
        help="Max rows per section (1..100, env SYS_INVESTIGATE_TOP, default: 10)",
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
        help="Repeat investigation COUNT times (0 or omitted = run until Ctrl-C)",
    )
    p.add_argument(
        "--exit-on-issue",
        action="store_true",
        help="Exit with code 3 when bottleneck is thrashing, wedged, or suspected spin loop",
    )
    p.add_argument("--verbose", action="store_true", help="Print effective configuration to stderr")
    return p


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL) if hasattr(signal, "SIGPIPE") else None
    parser = build_parser()
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
            f"watch={args.watch} exit_on_issue={args.exit_on_issue}",
            file=sys.stderr,
        )

    runs = 1 if args.watch is None else args.watch
    i = 0
    last_issue = False
    try:
        while runs == 0 or i < runs:
            rep = investigate(interval=args.interval, top_n=args.top)
            last_issue = bool(rep["verdict"]["is_issue"])
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
