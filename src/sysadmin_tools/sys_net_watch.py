#!/usr/bin/env python3
"""sys-net-watch: diagnose NIC errors/drops, TCP retransmits, socket leaks, and softnet pressure.

Reads Linux /proc/net/* directly (zero subprocesses, Python 3.10+ stdlib only),
so it stays fast and reliable even during severe CPU, memory, or storage stalls.

Examples:
  sys-net-watch
  sys-net-watch --interval 1 --samples 2 --top 8
  sys-net-watch --iface eno1,lo --json
  sys-net-watch --iface eth0 --iface eth1 --samples 3 --interval 0.5
  sys-net-watch --samples 1 --markdown
  sys-net-watch --exit-on-issue --json > /tmp/net-diag.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

VERSION = "0.5.0"

TCP_STATE_MAP: dict[str, str] = {
    "01": "ESTABLISHED",
    "02": "SYN_SENT",
    "03": "SYN_RECV",
    "04": "FIN_WAIT1",
    "05": "FIN_WAIT2",
    "06": "TIME_WAIT",
    "07": "CLOSE",
    "08": "CLOSE_WAIT",
    "09": "LAST_ACK",
    "0A": "LISTEN",
    "0B": "CLOSING",
    "0C": "NEW_SYN_RECV",
}

TCP_STATE_ORDER: tuple[str, ...] = (
    "ESTABLISHED",
    "SYN_SENT",
    "SYN_RECV",
    "FIN_WAIT1",
    "FIN_WAIT2",
    "TIME_WAIT",
    "CLOSE",
    "CLOSE_WAIT",
    "LAST_ACK",
    "LISTEN",
    "CLOSING",
    "NEW_SYN_RECV",
)

SNMP_TCP_KEYS: tuple[str, ...] = (
    "ActiveOpens",
    "PassiveOpens",
    "AttemptFails",
    "EstabResets",
    "CurrEstab",
    "InSegs",
    "OutSegs",
    "RetransSegs",
    "InErrs",
    "OutRsts",
    "InCsumErrors",
)

SNMP_UDP_KEYS: tuple[str, ...] = (
    "InDatagrams",
    "NoPorts",
    "InErrors",
    "OutDatagrams",
    "RcvbufErrors",
    "SndbufErrors",
    "InCsumErrors",
    "MemErrors",
)

# Deterministic issue classification thresholds.
LINK_ERR_DELTA_THRESHOLD = 1
LINK_COLLS_DELTA_THRESHOLD = 10
SNMP_TCP_IN_ERRS_THRESHOLD = 5
IFACE_DROP_DELTA_THRESHOLD = 5
SOFTNET_DROP_DELTA_THRESHOLD = 1
SOFTNET_SQUEEZE_DELTA_THRESHOLD = 50
UDP_IN_ERRORS_DELTA_THRESHOLD = 5
RETRANS_MIN_SEGS_FOR_RATE = 5
RETRANS_RATE_PCT_THRESHOLD = 1.0
RETRANS_ABS_DELTA_THRESHOLD = 50
ESTAB_RESETS_DELTA_THRESHOLD = 20
ATTEMPT_FAILS_DELTA_THRESHOLD = 20
CLOSE_WAIT_THRESHOLD = 50
CLOSE_WAIT_GROWING_COUNT = 20
CLOSE_WAIT_GROWING_DELTA = 10
TIME_WAIT_THRESHOLD = 2000
SYN_BACKLOG_THRESHOLD = 100
CLOSING_STATES_THRESHOLD = 500
UDP_QUEUE_BYTES_THRESHOLD = 262144  # 256 KiB
UDP_QUEUED_SOCKETS_THRESHOLD = 20
TCP_QUEUE_BYTES_THRESHOLD = 16777216  # 16 MiB

U32_MOD = 1 << 32
U32_HALF = 1 << 31


def read_text(path: str | Path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _delta_nonneg(v0: int, v1: int) -> int:
    return max(0, v1 - v0)


def _delta_u32(v0: int, v1: int) -> int:
    """Compute delta for 32-bit unsigned counters, handling wrap-around while clamping resets."""
    if v1 >= v0:
        return v1 - v0
    if (v0 - v1) > U32_HALF:
        return (v1 + U32_MOD) - v0
    return 0


def parse_net_dev(txt: str) -> dict[str, dict[str, int]]:
    """Parse /proc/net/dev into per-interface counter dicts."""
    out: dict[str, dict[str, int]] = {}
    for line in txt.splitlines():
        if ":" not in line:
            continue
        left, right = line.split(":", 1)
        iface = left.strip()
        if not iface or "|" in iface:
            continue
        fields = right.split()
        if len(fields) < 16:
            continue
        try:
            nums = [int(x) for x in fields[:16]]
        except ValueError:
            continue
        out[iface] = {
            "rx_bytes": nums[0],
            "rx_packets": nums[1],
            "rx_errs": nums[2],
            "rx_drop": nums[3],
            "rx_fifo": nums[4],
            "rx_frame": nums[5],
            "rx_compressed": nums[6],
            "rx_multicast": nums[7],
            "tx_bytes": nums[8],
            "tx_packets": nums[9],
            "tx_errs": nums[10],
            "tx_drop": nums[11],
            "tx_fifo": nums[12],
            "tx_colls": nums[13],
            "tx_carrier": nums[14],
            "tx_compressed": nums[15],
        }
    return out


def read_net_dev(proc_root: str = "/proc") -> dict[str, dict[str, int]]:
    return parse_net_dev(read_text(f"{proc_root}/net/dev") or "")


def compute_net_dev_deltas(
    d0: dict[str, dict[str, int]],
    d1: dict[str, dict[str, int]],
    dt_sec: float,
    iface_filter: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compute per-interface and aggregate deltas over [t0, t1]."""
    allowed = set(iface_filter) if iface_filter else None
    rows: list[dict[str, Any]] = []
    totals = {
        "rx_bytes_delta": 0,
        "tx_bytes_delta": 0,
        "rx_packets_delta": 0,
        "tx_packets_delta": 0,
        "rx_errs_delta": 0,
        "tx_errs_delta": 0,
        "rx_drop_delta": 0,
        "tx_drop_delta": 0,
        "rx_fifo_delta": 0,
        "tx_fifo_delta": 0,
        "rx_frame_delta": 0,
        "tx_colls_delta": 0,
        "tx_carrier_delta": 0,
        "link_errors_delta": 0,
        "drops_delta": 0,
    }

    for iface, c1 in d1.items():
        if allowed is not None and iface not in allowed:
            continue
        c0 = d0.get(iface)
        if c0 is None or dt_sec <= 0.0:
            deltas = {k: 0 for k in (
                "rx_bytes", "rx_packets", "rx_errs", "rx_drop", "rx_fifo", "rx_frame",
                "tx_bytes", "tx_packets", "tx_errs", "tx_drop", "tx_fifo", "tx_colls", "tx_carrier",
            )}
        else:
            deltas = {
                k: _delta_nonneg(c0.get(k, 0), c1.get(k, 0))
                for k in (
                    "rx_bytes", "rx_packets", "rx_errs", "rx_drop", "rx_fifo", "rx_frame",
                    "tx_bytes", "tx_packets", "tx_errs", "tx_drop", "tx_fifo", "tx_colls", "tx_carrier",
                )
            }

        link_errs = (
            deltas["rx_errs"]
            + deltas["tx_errs"]
            + deltas["rx_fifo"]
            + deltas["tx_fifo"]
            + deltas["rx_frame"]
            + deltas["tx_carrier"]
        )
        drops = deltas["rx_drop"] + deltas["tx_drop"]

        if dt_sec > 0.0:
            rx_kbps = round((deltas["rx_bytes"] * 8.0) / (dt_sec * 1000.0), 2)
            tx_kbps = round((deltas["tx_bytes"] * 8.0) / (dt_sec * 1000.0), 2)
            rx_pps = round(deltas["rx_packets"] / dt_sec, 2)
            tx_pps = round(deltas["tx_packets"] / dt_sec, 2)
        else:
            rx_kbps = 0.0
            tx_kbps = 0.0
            rx_pps = 0.0
            tx_pps = 0.0

        row: dict[str, Any] = {
            "iface": iface,
            "rx_bytes_delta": deltas["rx_bytes"],
            "tx_bytes_delta": deltas["tx_bytes"],
            "rx_kbps": rx_kbps,
            "tx_kbps": tx_kbps,
            "rx_packets_delta": deltas["rx_packets"],
            "tx_packets_delta": deltas["tx_packets"],
            "rx_pps": rx_pps,
            "tx_pps": tx_pps,
            "rx_errs_delta": deltas["rx_errs"],
            "tx_errs_delta": deltas["tx_errs"],
            "rx_drop_delta": deltas["rx_drop"],
            "tx_drop_delta": deltas["tx_drop"],
            "rx_fifo_delta": deltas["rx_fifo"],
            "tx_fifo_delta": deltas["tx_fifo"],
            "rx_frame_delta": deltas["rx_frame"],
            "tx_colls_delta": deltas["tx_colls"],
            "tx_carrier_delta": deltas["tx_carrier"],
            "link_errors_delta": link_errs,
            "drops_delta": drops,
            "rx_bytes_total": c1["rx_bytes"],
            "tx_bytes_total": c1["tx_bytes"],
        }
        rows.append(row)

        for key in (
            "rx_bytes_delta",
            "tx_bytes_delta",
            "rx_packets_delta",
            "tx_packets_delta",
            "rx_errs_delta",
            "tx_errs_delta",
            "rx_drop_delta",
            "tx_drop_delta",
            "rx_fifo_delta",
            "tx_fifo_delta",
            "rx_frame_delta",
            "tx_colls_delta",
            "tx_carrier_delta",
            "link_errors_delta",
            "drops_delta",
        ):
            totals[key] += int(row[key])

    rows.sort(
        key=lambda r: (
            r["link_errors_delta"] + r["tx_colls_delta"],
            r["drops_delta"],
            r["rx_bytes_delta"] + r["tx_bytes_delta"],
            r["rx_bytes_total"] + r["tx_bytes_total"],
            r["iface"],
        ),
        reverse=True,
    )
    summary: dict[str, Any] = {
        "total_interfaces": len(rows),
        **totals,
    }
    return rows, summary


def _parse_hex_queue_pair(tok: str) -> tuple[int, int]:
    if ":" not in tok:
        return 0, 0
    tx_hex, rx_hex = tok.split(":", 1)
    try:
        return int(tx_hex, 16), int(rx_hex, 16)
    except ValueError:
        return 0, 0


def parse_proc_net_tcp(txt: str) -> dict[str, Any]:
    """Parse /proc/net/tcp or /proc/net/tcp6 socket states and queue pressure."""
    states = {name: 0 for name in TCP_STATE_ORDER}
    total = 0
    tx_queue_bytes = 0
    rx_queue_bytes = 0
    queued_sockets = 0

    for line in txt.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].endswith(":"):
            continue
        st_hex = parts[3].upper()
        st_name = TCP_STATE_MAP.get(st_hex, "UNKNOWN")
        if st_name not in states:
            states[st_name] = 0
        states[st_name] += 1
        total += 1
        tx_q, rx_q = _parse_hex_queue_pair(parts[4])
        tx_queue_bytes += tx_q
        rx_queue_bytes += rx_q
        if tx_q > 0 or rx_q > 0:
            queued_sockets += 1

    return {
        "total": total,
        "states": states,
        "tx_queue_bytes": tx_queue_bytes,
        "rx_queue_bytes": rx_queue_bytes,
        "queued_sockets": queued_sockets,
    }


def read_tcp_sockets(proc_root: str = "/proc") -> dict[str, Any]:
    v4 = parse_proc_net_tcp(read_text(f"{proc_root}/net/tcp") or "")
    v6 = parse_proc_net_tcp(read_text(f"{proc_root}/net/tcp6") or "")

    merged_states: dict[str, int] = {name: 0 for name in TCP_STATE_ORDER}
    for src in (v4["states"], v6["states"]):
        for k, val in src.items():
            merged_states[k] = merged_states.get(k, 0) + val

    return {
        "total": v4["total"] + v6["total"],
        "v4_total": v4["total"],
        "v6_total": v6["total"],
        "states": merged_states,
        "tx_queue_bytes": v4["tx_queue_bytes"] + v6["tx_queue_bytes"],
        "rx_queue_bytes": v4["rx_queue_bytes"] + v6["rx_queue_bytes"],
        "queued_sockets": v4["queued_sockets"] + v6["queued_sockets"],
    }


def parse_proc_net_udp(txt: str) -> dict[str, Any]:
    """Parse /proc/net/udp or /proc/net/udp6 socket counts, queue bytes, and drops."""
    total = 0
    tx_queue_bytes = 0
    rx_queue_bytes = 0
    max_tx_queue_bytes = 0
    max_rx_queue_bytes = 0
    queued_sockets = 0
    socket_drops_total = 0

    for line in txt.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].endswith(":"):
            continue
        total += 1
        tx_q, rx_q = _parse_hex_queue_pair(parts[4])
        tx_queue_bytes += tx_q
        rx_queue_bytes += rx_q
        if tx_q > max_tx_queue_bytes:
            max_tx_queue_bytes = tx_q
        if rx_q > max_rx_queue_bytes:
            max_rx_queue_bytes = rx_q
        if tx_q > 0 or rx_q > 0:
            queued_sockets += 1
        if len(parts) >= 13:
            try:
                socket_drops_total += max(0, int(parts[12]))
            except ValueError:
                pass

    return {
        "total": total,
        "tx_queue_bytes": tx_queue_bytes,
        "rx_queue_bytes": rx_queue_bytes,
        "max_tx_queue_bytes": max_tx_queue_bytes,
        "max_rx_queue_bytes": max_rx_queue_bytes,
        "queued_sockets": queued_sockets,
        "socket_drops_total": socket_drops_total,
    }


def read_udp_sockets(proc_root: str = "/proc") -> dict[str, Any]:
    v4 = parse_proc_net_udp(read_text(f"{proc_root}/net/udp") or "")
    v6 = parse_proc_net_udp(read_text(f"{proc_root}/net/udp6") or "")
    return {
        "total": v4["total"] + v6["total"],
        "v4_total": v4["total"],
        "v6_total": v6["total"],
        "tx_queue_bytes": v4["tx_queue_bytes"] + v6["tx_queue_bytes"],
        "rx_queue_bytes": v4["rx_queue_bytes"] + v6["rx_queue_bytes"],
        "max_tx_queue_bytes": max(v4["max_tx_queue_bytes"], v6["max_tx_queue_bytes"]),
        "max_rx_queue_bytes": max(v4["max_rx_queue_bytes"], v6["max_rx_queue_bytes"]),
        "queued_sockets": v4["queued_sockets"] + v6["queued_sockets"],
        "socket_drops_total": v4["socket_drops_total"] + v6["socket_drops_total"],
    }


def parse_net_snmp(txt: str) -> dict[str, dict[str, int]]:
    """Parse /proc/net/snmp paired header/value lines into {Proto: {Field: int}}."""
    headers: dict[str, list[str]] = {}
    parsed: dict[str, dict[str, int]] = {}

    for line in txt.splitlines():
        if ":" not in line:
            continue
        proto, rest = line.split(":", 1)
        proto = proto.strip()
        tokens = rest.split()
        if not proto or not tokens:
            continue
        if proto not in headers:
            headers[proto] = tokens
        else:
            cols = headers.pop(proto)
            vals: dict[str, int] = {}
            for k, v in zip(cols, tokens):
                try:
                    vals[k] = int(v)
                except ValueError:
                    pass
            parsed[proto] = vals

    tcp_raw = parsed.get("Tcp", {})
    udp_raw = parsed.get("Udp", {})
    return {
        "Tcp": {k: tcp_raw.get(k, 0) for k in SNMP_TCP_KEYS},
        "Udp": {k: udp_raw.get(k, 0) for k in SNMP_UDP_KEYS},
    }


def read_net_snmp(proc_root: str = "/proc") -> dict[str, dict[str, int]]:
    return parse_net_snmp(read_text(f"{proc_root}/net/snmp") or "")


def compute_snmp_deltas(
    s0: dict[str, dict[str, int]],
    s1: dict[str, dict[str, int]],
    has_interval: bool = True,
) -> dict[str, dict[str, int | float]]:
    """Compute Tcp and Udp SNMP counter deltas and TCP retransmit percentage."""
    t0 = s0.get("Tcp", {})
    t1 = s1.get("Tcp", {})
    u0 = s0.get("Udp", {})
    u1 = s1.get("Udp", {})

    def td(k: str) -> int:
        return _delta_nonneg(t0.get(k, 0), t1.get(k, 0)) if has_interval else 0

    def ud(k: str) -> int:
        return _delta_nonneg(u0.get(k, 0), u1.get(k, 0)) if has_interval else 0

    retrans_delta = td("RetransSegs")
    out_segs_delta = td("OutSegs")
    if out_segs_delta > 0:
        retrans_rate_pct = round(100.0 * retrans_delta / out_segs_delta, 2)
    elif retrans_delta > 0:
        retrans_rate_pct = 100.0
    else:
        retrans_rate_pct = 0.0

    return {
        "tcp": {
            "retrans_segs_delta": retrans_delta,
            "out_segs_delta": out_segs_delta,
            "in_segs_delta": td("InSegs"),
            "in_errs_delta": td("InErrs"),
            "estab_resets_delta": td("EstabResets"),
            "attempt_fails_delta": td("AttemptFails"),
            "out_rsts_delta": td("OutRsts"),
            "in_csum_errs_delta": td("InCsumErrors"),
            "retrans_rate_pct": retrans_rate_pct,
            "curr_estab": max(0, int(t1.get("CurrEstab", 0))),
        },
        "udp": {
            "in_datagrams_delta": ud("InDatagrams"),
            "out_datagrams_delta": ud("OutDatagrams"),
            "no_ports_delta": ud("NoPorts"),
            "in_errors_delta": ud("InErrors"),
            "rcvbuf_errors_delta": ud("RcvbufErrors"),
            "sndbuf_errors_delta": ud("SndbufErrors"),
            "in_csum_errs_delta": ud("InCsumErrors"),
            "mem_errors_delta": ud("MemErrors"),
        },
    }


def parse_softnet_stat(txt: str) -> dict[int, dict[str, int]]:
    """Parse /proc/net/softnet_stat hex columns per CPU."""
    out: dict[int, dict[str, int]] = {}
    for idx, line in enumerate(txt.splitlines()):
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            processed = int(parts[0], 16)
            dropped = int(parts[1], 16)
            time_squeeze = int(parts[2], 16)
            cpu_id = int(parts[12], 16) if len(parts) >= 13 else idx
        except ValueError:
            continue
        out[cpu_id] = {
            "processed": processed,
            "dropped": dropped,
            "time_squeeze": time_squeeze,
        }
    return out


def read_softnet_stat(proc_root: str = "/proc") -> dict[int, dict[str, int]]:
    return parse_softnet_stat(read_text(f"{proc_root}/net/softnet_stat") or "")


def compute_softnet_deltas(
    sn0: dict[int, dict[str, int]],
    sn1: dict[int, dict[str, int]],
    top_n: int = 8,
    has_interval: bool = True,
) -> dict[str, Any]:
    """Compute per-CPU softnet packet drop and time_squeeze deltas."""
    rows: list[dict[str, int]] = []
    processed_total = 0
    dropped_total = 0
    squeeze_total = 0
    cpus_with_drops = 0
    cpus_with_squeeze = 0

    for cpu_id, c1 in sorted(sn1.items()):
        c0 = sn0.get(cpu_id)
        if c0 is None or not has_interval:
            p_d = 0
            dr_d = 0
            sq_d = 0
        else:
            p_d = _delta_u32(c0.get("processed", 0), c1.get("processed", 0))
            dr_d = _delta_u32(c0.get("dropped", 0), c1.get("dropped", 0))
            sq_d = _delta_u32(c0.get("time_squeeze", 0), c1.get("time_squeeze", 0))

        processed_total += p_d
        dropped_total += dr_d
        squeeze_total += sq_d
        if dr_d > 0:
            cpus_with_drops += 1
        if sq_d > 0:
            cpus_with_squeeze += 1
        rows.append(
            {
                "cpu": cpu_id,
                "processed_delta": p_d,
                "dropped_delta": dr_d,
                "time_squeeze_delta": sq_d,
            }
        )

    rows.sort(
        key=lambda r: (r["dropped_delta"], r["time_squeeze_delta"], r["processed_delta"], -r["cpu"]),
        reverse=True,
    )
    active_rows = [r for r in rows if r["dropped_delta"] > 0 or r["time_squeeze_delta"] > 0 or r["processed_delta"] > 0]
    top_cpus = (active_rows or rows)[:top_n]

    return {
        "cpu_count": len(sn1),
        "processed_delta": processed_total,
        "dropped_delta": dropped_total,
        "time_squeeze_delta": squeeze_total,
        "cpus_with_drops": cpus_with_drops,
        "cpus_with_squeeze": cpus_with_squeeze,
        "top_cpus": top_cpus,
    }


def classify_network_verdict(
    interfaces: list[dict[str, Any]],
    interfaces_summary: dict[str, Any],
    sockets: dict[str, Any],
    snmp: dict[str, Any],
    softnet: dict[str, Any],
) -> dict[str, Any]:
    """Deterministic classifier for link errors, packet drops, retransmits, and socket pressure."""
    tcp_snmp = snmp.get("tcp", {})
    udp_snmp = snmp.get("udp", {})
    tcp_sock = sockets.get("tcp", {})
    tcp_states = tcp_sock.get("states", {})
    udp_sock = sockets.get("udp", {})

    # 1. Link / framing / checksum errors
    link_err_ifaces = [
        r for r in interfaces
        if int(r.get("link_errors_delta", 0)) >= LINK_ERR_DELTA_THRESHOLD
        or int(r.get("tx_colls_delta", 0)) >= LINK_COLLS_DELTA_THRESHOLD
    ]
    total_link_errs = int(interfaces_summary.get("link_errors_delta", 0))
    total_colls = int(interfaces_summary.get("tx_colls_delta", 0))
    tcp_in_errs = int(tcp_snmp.get("in_errs_delta", 0))
    tcp_csum_errs = int(tcp_snmp.get("in_csum_errs_delta", 0))
    udp_csum_errs = int(udp_snmp.get("in_csum_errs_delta", 0))

    has_link_errors = (
        total_link_errs >= LINK_ERR_DELTA_THRESHOLD
        or total_colls >= LINK_COLLS_DELTA_THRESHOLD
        or tcp_in_errs >= SNMP_TCP_IN_ERRS_THRESHOLD
        or tcp_csum_errs >= 1
        or udp_csum_errs >= 1
    )

    # 2. Packet drops (NIC drops, softnet backlog/squeeze, UDP buffer errors)
    drop_ifaces = [r for r in interfaces if int(r.get("drops_delta", 0)) > 0]
    total_iface_drops = int(interfaces_summary.get("drops_delta", 0))
    softnet_drops = int(softnet.get("dropped_delta", 0))
    softnet_squeeze = int(softnet.get("time_squeeze_delta", 0))
    udp_rcvbuf_errs = int(udp_snmp.get("rcvbuf_errors_delta", 0))
    udp_sndbuf_errs = int(udp_snmp.get("sndbuf_errors_delta", 0))
    udp_in_errs = int(udp_snmp.get("in_errors_delta", 0))
    udp_mem_errs = int(udp_snmp.get("mem_errors_delta", 0))

    has_packet_drops = (
        total_iface_drops >= IFACE_DROP_DELTA_THRESHOLD
        or softnet_drops >= SOFTNET_DROP_DELTA_THRESHOLD
        or softnet_squeeze >= SOFTNET_SQUEEZE_DELTA_THRESHOLD
        or (udp_rcvbuf_errs + udp_sndbuf_errs) >= 1
        or udp_in_errs >= UDP_IN_ERRORS_DELTA_THRESHOLD
        or udp_mem_errs >= 1
    )

    # 3. Elevated TCP retransmits / resets / attempt fails
    retrans_delta = int(tcp_snmp.get("retrans_segs_delta", 0))
    out_segs_delta = int(tcp_snmp.get("out_segs_delta", 0))
    retrans_rate_pct = float(tcp_snmp.get("retrans_rate_pct", 0.0))
    estab_resets = int(tcp_snmp.get("estab_resets_delta", 0))
    attempt_fails = int(tcp_snmp.get("attempt_fails_delta", 0))

    has_elevated_retransmits = (
        (retrans_delta >= RETRANS_MIN_SEGS_FOR_RATE and retrans_rate_pct >= RETRANS_RATE_PCT_THRESHOLD)
        or retrans_delta >= RETRANS_ABS_DELTA_THRESHOLD
        or estab_resets >= ESTAB_RESETS_DELTA_THRESHOLD
        or attempt_fails >= ATTEMPT_FAILS_DELTA_THRESHOLD
    )

    # 4. Socket state & queue pressure
    close_wait = int(tcp_states.get("CLOSE_WAIT", 0))
    close_wait_delta = int(tcp_sock.get("close_wait_delta", 0))
    time_wait = int(tcp_states.get("TIME_WAIT", 0))
    syn_recv = int(tcp_states.get("SYN_RECV", 0)) + int(tcp_states.get("NEW_SYN_RECV", 0))
    syn_sent = int(tcp_states.get("SYN_SENT", 0))
    closing_states = (
        int(tcp_states.get("FIN_WAIT1", 0))
        + int(tcp_states.get("FIN_WAIT2", 0))
        + int(tcp_states.get("LAST_ACK", 0))
    )
    tcp_queue_bytes = int(tcp_sock.get("tx_queue_bytes", 0)) + int(tcp_sock.get("rx_queue_bytes", 0))
    udp_queue_bytes = int(udp_sock.get("tx_queue_bytes", 0)) + int(udp_sock.get("rx_queue_bytes", 0))
    udp_queued_sockets = int(udp_sock.get("queued_sockets", 0))

    close_wait_issue = (
        close_wait >= CLOSE_WAIT_THRESHOLD
        or (close_wait >= CLOSE_WAIT_GROWING_COUNT and close_wait_delta >= CLOSE_WAIT_GROWING_DELTA)
    )
    time_wait_issue = time_wait >= TIME_WAIT_THRESHOLD
    syn_issue = syn_recv >= SYN_BACKLOG_THRESHOLD or syn_sent >= SYN_BACKLOG_THRESHOLD
    closing_issue = closing_states >= CLOSING_STATES_THRESHOLD
    udp_queue_issue = (
        udp_queue_bytes >= UDP_QUEUE_BYTES_THRESHOLD
        or udp_queued_sockets >= UDP_QUEUED_SOCKETS_THRESHOLD
    )
    tcp_queue_issue = tcp_queue_bytes >= TCP_QUEUE_BYTES_THRESHOLD

    has_socket_pressure = (
        close_wait_issue
        or time_wait_issue
        or syn_issue
        or closing_issue
        or udp_queue_issue
        or tcp_queue_issue
    )

    reasons: list[str] = []
    recommendations: list[str] = []

    if has_link_errors:
        iface_desc = ", ".join(
            f"{r['iface']}(errs={r['link_errors_delta']},colls={r['tx_colls_delta']})"
            for r in link_err_ifaces[:3]
        )
        reasons.append(
            f"Link/checksum errors detected: iface_link_errs={total_link_errs}, colls={total_colls}, "
            f"snmp tcp_in_errs={tcp_in_errs} tcp_csum={tcp_csum_errs} udp_csum={udp_csum_errs}"
            + (f" [{iface_desc}]" if iface_desc else "")
            + "."
        )
        recommendations.append(
            "Inspect NIC cabling, duplex/speed negotiation, SFP/transceiver health, and driver ring counters "
            "(`ethtool -S <iface>` and `dmesg | grep -i -E 'link|eth|nic|eno|enp'`)."
        )

    if has_packet_drops:
        drop_desc = ", ".join(
            f"{r['iface']}(rx_drop={r['rx_drop_delta']},tx_drop={r['tx_drop_delta']})"
            for r in drop_ifaces[:3]
        )
        reasons.append(
            f"Packet drops detected: iface_drops={total_iface_drops}, softnet_drops={softnet_drops}, "
            f"softnet_time_squeeze={softnet_squeeze}, udp_buf_errs={udp_rcvbuf_errs + udp_sndbuf_errs}, "
            f"udp_in_errs={udp_in_errs}"
            + (f" [{drop_desc}]" if drop_desc else "")
            + "."
        )
        if softnet_drops > 0 or softnet_squeeze >= SOFTNET_SQUEEZE_DELTA_THRESHOLD:
            recommendations.append(
                "Tune kernel softnet backlog/NAPI budget (`net.core.netdev_max_backlog`, `net.core.netdev_budget`) "
                "and check RSS/IRQ affinity across CPUs."
            )
        if total_iface_drops >= IFACE_DROP_DELTA_THRESHOLD:
            recommendations.append(
                "Increase NIC RX/TX ring buffer sizes (`ethtool -G <iface>`) or check qdisc/firewall drops on affected interface(s)."
            )
        if (udp_rcvbuf_errs + udp_sndbuf_errs) >= 1 or udp_in_errs >= UDP_IN_ERRORS_DELTA_THRESHOLD or udp_mem_errs >= 1:
            recommendations.append(
                "Raise UDP socket buffer limits (`net.core.rmem_max`, `net.core.wmem_max`) and verify UDP consumers drain sockets promptly."
            )

    if has_elevated_retransmits:
        reasons.append(
            f"Elevated TCP retransmits/resets: retrans={retrans_delta}/{out_segs_delta} segs "
            f"({retrans_rate_pct:.2f}%), estab_resets={estab_resets}, attempt_fails={attempt_fails}."
        )
        recommendations.append(
            "Check upstream link congestion, MTU/PMTUD mismatches, peer packet loss, or saturated switch ports "
            "driving TCP retransmissions and resets."
        )

    if has_socket_pressure:
        sock_parts: list[str] = []
        if close_wait_issue:
            sock_parts.append(f"CLOSE_WAIT={close_wait} (delta={close_wait_delta:+d})")
            recommendations.append(
                "Investigate CLOSE_WAIT socket leak: identify owning processes (`ss -tanp state close-wait`) "
                "not closing sockets after peer FIN."
            )
        if time_wait_issue:
            sock_parts.append(f"TIME_WAIT={time_wait}")
            recommendations.append(
                "Reduce short-lived TCP connection churn using connection pooling/HTTP keepalive, or review "
                "`net.ipv4.ip_local_port_range` and `net.ipv4.tcp_tw_reuse`."
            )
        if syn_issue:
            sock_parts.append(f"SYN_RECV={syn_recv}, SYN_SENT={syn_sent}")
            recommendations.append(
                "Check SYN backlog saturation or unreachable peers (`ss -tan state syn-recv`, "
                "`net.core.somaxconn`, `net.ipv4.tcp_max_syn_backlog`, `net.ipv4.tcp_syncookies`)."
            )
        if closing_issue:
            sock_parts.append(f"FIN_WAIT/LAST_ACK={closing_states}")
            recommendations.append(
                "Inspect peers stalling connection teardown (`ss -tan state fin-wait-1 state fin-wait-2 state last-ack`)."
            )
        if udp_queue_issue:
            sock_parts.append(f"udp_queue={udp_queue_bytes}B ({udp_queued_sockets} queued sockets)")
            recommendations.append(
                "Inspect stalled UDP receivers (`ss -uanp`) failing to read queued datagrams and verify `net.core.rmem_max`."
            )
        if tcp_queue_issue:
            sock_parts.append(f"tcp_queue={tcp_queue_bytes}B")
            recommendations.append(
                "Inspect stalled TCP readers/writers with deep send/receive queues (`ss -tanp`)."
            )
        reasons.append("Socket state/queue pressure detected: " + "; ".join(sock_parts) + ".")

    if has_link_errors:
        status = "LINK_ERRORS"
    elif has_packet_drops:
        status = "PACKET_DROPS"
    elif has_elevated_retransmits:
        status = "ELEVATED_RETRANSMITS"
    elif has_socket_pressure:
        status = "SOCKET_PRESSURE"
    else:
        status = "HEALTHY"
        reasons.append(
            f"Network healthy: ifaces={interfaces_summary.get('total_interfaces', 0)}, "
            f"link_errs=0, drops={total_iface_drops}, softnet_drops={softnet_drops}, "
            f"tcp_retrans={retrans_delta} ({retrans_rate_pct:.2f}%), "
            f"tcp_estab={tcp_states.get('ESTABLISHED', 0)}, close_wait={close_wait}, time_wait={time_wait}."
        )

    is_issue = status != "HEALTHY"
    if not recommendations:
        if is_issue:
            recommendations.append(
                "Inspect interface counters, SNMP deltas, softnet stats, and socket states above to isolate the network fault."
            )
        else:
            recommendations.append("No immediate intervention needed.")

    return {
        "status": status,
        "is_issue": is_issue,
        "reasons": reasons,
        "recommendations": recommendations,
    }


def _snapshot_proc_net(proc_root: str) -> dict[str, Any]:
    return {
        "dev": read_net_dev(proc_root=proc_root),
        "tcp": read_tcp_sockets(proc_root=proc_root),
        "udp": read_udp_sockets(proc_root=proc_root),
        "snmp": read_net_snmp(proc_root=proc_root),
        "softnet": read_softnet_stat(proc_root=proc_root),
    }


def watch_network(
    interval: float = 1.0,
    samples: int = 2,
    iface_filter: list[str] | None = None,
    top_n: int = 8,
    proc_root: str = "/proc",
) -> dict[str, Any]:
    net_dir = Path(proc_root) / "net"
    if not net_dir.is_dir():
        raise OSError(f"{net_dir} does not exist or is not accessible")

    t0 = time.monotonic()
    snap0 = _snapshot_proc_net(proc_root)
    snap_last = snap0

    if samples > 1:
        for _ in range(samples - 1):
            time.sleep(interval)
            snap_last = _snapshot_proc_net(proc_root)
        dt_sec = max(0.05, time.monotonic() - t0)
        has_interval = True
    else:
        dt_sec = 0.0
        has_interval = False

    if iface_filter:
        available = set(snap0["dev"].keys()) | set(snap_last["dev"].keys())
        missing = [name for name in iface_filter if name not in available]
        if missing:
            raise ValueError(f"requested interface(s) not found: {', '.join(missing)}")

    all_ifaces, iface_summary = compute_net_dev_deltas(
        snap0["dev"],
        snap_last["dev"],
        dt_sec=dt_sec,
        iface_filter=iface_filter,
    )
    shown_ifaces = all_ifaces[:top_n]
    iface_summary["shown_interfaces"] = len(shown_ifaces)

    tcp_sock = dict(snap_last["tcp"])
    cw0 = int(snap0["tcp"]["states"].get("CLOSE_WAIT", 0))
    cw1 = int(snap_last["tcp"]["states"].get("CLOSE_WAIT", 0))
    tcp_sock["close_wait_delta"] = (cw1 - cw0) if has_interval else 0

    sockets = {
        "tcp": tcp_sock,
        "udp": snap_last["udp"],
    }
    snmp = compute_snmp_deltas(snap0["snmp"], snap_last["snmp"], has_interval=has_interval)
    softnet = compute_softnet_deltas(
        snap0["softnet"],
        snap_last["softnet"],
        top_n=top_n,
        has_interval=has_interval,
    )

    verdict = classify_network_verdict(
        interfaces=all_ifaces,
        interfaces_summary=iface_summary,
        sockets=sockets,
        snmp=snmp,
        softnet=softnet,
    )

    return {
        "timestamp": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "version": VERSION,
        "sample_sec": round(dt_sec, 2),
        "samples": samples,
        "iface_filter": list(iface_filter) if iface_filter else [],
        "verdict": verdict,
        "interfaces": shown_ifaces,
        "interfaces_summary": iface_summary,
        "sockets": sockets,
        "snmp": snmp,
        "softnet": softnet,
    }


def render_text(rep: dict[str, Any], markdown: bool = False) -> str:
    v = rep["verdict"]
    isum = rep["interfaces_summary"]
    tcp_s = rep["sockets"]["tcp"]
    udp_s = rep["sockets"]["udp"]
    st = tcp_s["states"]
    snmp_t = rep["snmp"]["tcp"]
    snmp_u = rep["snmp"]["udp"]
    sn = rep["softnet"]
    h = (lambda s: f"## {s}") if markdown else (lambda s: f"=== {s} ===")

    lines: list[str] = []
    lines.append(
        f"{'# ' if markdown else ''}Network Investigation @ {rep['timestamp']} "
        f"(window={rep['sample_sec']}s, samples={rep['samples']})"
    )
    lines.append(f"Status: {v['status']}")
    for r in v["reasons"]:
        lines.append(f"  - {r}")
    lines.append("")

    lines.append(h("Interfaces (/proc/net/dev)"))
    lines.append(
        f"Total : shown={isum['shown_interfaces']}/{isum['total_interfaces']} | "
        f"dRX={isum['rx_bytes_delta']}B ({isum['rx_packets_delta']} pkt) | "
        f"dTX={isum['tx_bytes_delta']}B ({isum['tx_packets_delta']} pkt) | "
        f"link_errs={isum['link_errors_delta']} | drops={isum['drops_delta']} | colls={isum['tx_colls_delta']}"
    )
    if rep["interfaces"]:
        lines.append(
            f"{'IFACE':<18} {'RX_kbps':>9} {'TX_kbps':>9} {'RX_pps':>8} {'TX_pps':>8} "
            f"{'dRX_ERR':>7} {'dTX_ERR':>7} {'dRX_DRP':>7} {'dTX_DRP':>7} {'dFIFO':>6} {'dFRM':>5} {'dCAR':>5} {'dCOL':>5}"
        )
        for r in rep["interfaces"]:
            fifo_d = r["rx_fifo_delta"] + r["tx_fifo_delta"]
            lines.append(
                f"{r['iface']:<18} {r['rx_kbps']:>9.2f} {r['tx_kbps']:>9.2f} "
                f"{r['rx_pps']:>8.1f} {r['tx_pps']:>8.1f} "
                f"{r['rx_errs_delta']:>7} {r['tx_errs_delta']:>7} "
                f"{r['rx_drop_delta']:>7} {r['tx_drop_delta']:>7} "
                f"{fifo_d:>6} {r['rx_frame_delta']:>5} {r['tx_carrier_delta']:>5} {r['tx_colls_delta']:>5}"
            )
    lines.append("")

    lines.append(h("Sockets (/proc/net/{tcp,tcp6,udp,udp6})"))
    lines.append(
        f"TCP   : total={tcp_s['total']} (v4={tcp_s['v4_total']}, v6={tcp_s['v6_total']}) | "
        f"ESTAB={st.get('ESTABLISHED', 0)} LISTEN={st.get('LISTEN', 0)} "
        f"TIME_WAIT={st.get('TIME_WAIT', 0)} CLOSE_WAIT={st.get('CLOSE_WAIT', 0)}({tcp_s.get('close_wait_delta', 0):+d}) "
        f"SYN_SENT={st.get('SYN_SENT', 0)} SYN_RECV={st.get('SYN_RECV', 0)} "
        f"FIN1={st.get('FIN_WAIT1', 0)} FIN2={st.get('FIN_WAIT2', 0)} LAST_ACK={st.get('LAST_ACK', 0)}"
    )
    lines.append(
        f"TCP Q : tx_queue={tcp_s['tx_queue_bytes']}B rx_queue={tcp_s['rx_queue_bytes']}B "
        f"queued_sockets={tcp_s['queued_sockets']}"
    )
    lines.append(
        f"UDP   : total={udp_s['total']} (v4={udp_s['v4_total']}, v6={udp_s['v6_total']}) | "
        f"tx_queue={udp_s['tx_queue_bytes']}B rx_queue={udp_s['rx_queue_bytes']}B "
        f"queued_sockets={udp_s['queued_sockets']} sock_drops_total={udp_s['socket_drops_total']}"
    )
    lines.append("")

    lines.append(h("SNMP Deltas (/proc/net/snmp)"))
    lines.append(
        f"TCP   : dOutSegs={snmp_t['out_segs_delta']} dRetrans={snmp_t['retrans_segs_delta']} "
        f"({snmp_t['retrans_rate_pct']:.2f}%) | dInErrs={snmp_t['in_errs_delta']} "
        f"dEstabResets={snmp_t['estab_resets_delta']} dAttemptFails={snmp_t['attempt_fails_delta']} "
        f"dInCsumErrs={snmp_t['in_csum_errs_delta']}"
    )
    lines.append(
        f"UDP   : dInDgrams={snmp_u['in_datagrams_delta']} dOutDgrams={snmp_u['out_datagrams_delta']} | "
        f"dInErrors={snmp_u['in_errors_delta']} dRcvbufErrs={snmp_u['rcvbuf_errors_delta']} "
        f"dSndbufErrs={snmp_u['sndbuf_errors_delta']} dMemErrs={snmp_u['mem_errors_delta']}"
    )
    lines.append("")

    lines.append(h("Softnet Deltas (/proc/net/softnet_stat)"))
    lines.append(
        f"Total : cpus={sn['cpu_count']} dProcessed={sn['processed_delta']} "
        f"dDropped={sn['dropped_delta']} (cpus={sn['cpus_with_drops']}) "
        f"dTimeSqueeze={sn['time_squeeze_delta']} (cpus={sn['cpus_with_squeeze']})"
    )
    if sn["top_cpus"]:
        lines.append(f"{'CPU':>5} {'dPROCESSED':>12} {'dDROPPED':>10} {'dSQUEEZE':>10}")
        for c in sn["top_cpus"]:
            lines.append(
                f"{c['cpu']:>5} {c['processed_delta']:>12} {c['dropped_delta']:>10} {c['time_squeeze_delta']:>10}"
            )
    lines.append("")

    lines.append(h("Recommendations"))
    for rec in v["recommendations"]:
        lines.append(f"- {rec}")
    return "\n".join(lines)


def parse_iface_args(raw_values: list[str] | None) -> list[str] | None:
    """Normalize repeatable/comma-separated --iface arguments."""
    if not raw_values:
        return None
    ifaces: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        for tok in item.split(","):
            name = tok.strip()
            if name and name not in seen:
                seen.add(name)
                ifaces.append(name)
    if not ifaces:
        raise ValueError("--iface filter cannot be empty")
    return ifaces


def build_parser(default_interval: float = 1.0, default_top: int = 8) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sys-net-watch",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--interval",
        type=float,
        default=default_interval,
        help="Sampling interval in seconds between snapshots (0.1..60.0, env SYS_NET_WATCH_INTERVAL, default: 1.0)",
    )
    p.add_argument(
        "--samples",
        type=int,
        default=2,
        help="Number of snapshots to collect (>= 1; 2 = one delta window, default: 2)",
    )
    p.add_argument(
        "--iface",
        action="append",
        default=None,
        metavar="IFACE",
        help="Filter interfaces (repeatable or comma-separated, e.g. --iface eno1,lo)",
    )
    p.add_argument(
        "--top",
        type=int,
        default=default_top,
        help="Max interface and softnet CPU rows to display (1..100, env SYS_NET_WATCH_TOP, default: 8)",
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
    raw_interval = os.environ.get("SYS_NET_WATCH_INTERVAL", "").strip()
    raw_top = os.environ.get("SYS_NET_WATCH_TOP", "").strip()
    try:
        default_interval = float(raw_interval) if raw_interval else 1.0
    except ValueError as e:
        raise ValueError(f"invalid SYS_NET_WATCH_INTERVAL={raw_interval!r}") from e
    try:
        default_top = int(raw_top) if raw_top else 8
    except ValueError as e:
        raise ValueError(f"invalid SYS_NET_WATCH_TOP={raw_top!r}") from e
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

    if not (0.1 <= args.interval <= 60.0):
        print("error: --interval must be between 0.1 and 60.0 seconds", file=sys.stderr)
        return 1
    if args.samples < 1:
        print("error: --samples must be >= 1", file=sys.stderr)
        return 1
    if not (1 <= args.top <= 100):
        print("error: --top must be between 1 and 100", file=sys.stderr)
        return 1

    try:
        iface_filter = parse_iface_args(args.iface)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.verbose:
        print(
            f"config: interval={args.interval}s samples={args.samples} "
            f"iface={iface_filter or 'all'} top={args.top} "
            f"format={'json' if args.json else 'markdown' if args.markdown else 'text'} "
            f"exit_on_issue={args.exit_on_issue}",
            file=sys.stderr,
        )

    try:
        rep = watch_network(
            interval=args.interval,
            samples=args.samples,
            iface_filter=iface_filter,
            top_n=args.top,
        )
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as e:
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
