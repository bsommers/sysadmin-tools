#!/usr/bin/env python3
"""Unit and CLI error-path tests for sysadmin_tools.sys_net_watch."""

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
SCRIPT = SRC_DIR / "sysadmin_tools" / "sys_net_watch.py"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sysadmin_tools import sys_net_watch as snw  # noqa: E402


def _healthy_inputs() -> tuple[list[dict], dict, dict, dict, dict]:
    ifaces = [
        {
            "iface": "eno1",
            "rx_bytes_delta": 10000,
            "tx_bytes_delta": 5000,
            "rx_kbps": 80.0,
            "tx_kbps": 40.0,
            "rx_packets_delta": 100,
            "tx_packets_delta": 50,
            "rx_pps": 100.0,
            "tx_pps": 50.0,
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
            "rx_bytes_total": 100000,
            "tx_bytes_total": 50000,
        }
    ]
    summary = {
        "total_interfaces": 1,
        "shown_interfaces": 1,
        "rx_bytes_delta": 10000,
        "tx_bytes_delta": 5000,
        "rx_packets_delta": 100,
        "tx_packets_delta": 50,
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
    sockets = {
        "tcp": {
            "total": 20,
            "v4_total": 15,
            "v6_total": 5,
            "states": {s: 0 for s in snw.TCP_STATE_ORDER} | {"ESTABLISHED": 12, "LISTEN": 6, "TIME_WAIT": 2},
            "close_wait_delta": 0,
            "tx_queue_bytes": 0,
            "rx_queue_bytes": 0,
            "queued_sockets": 0,
        },
        "udp": {
            "total": 10,
            "v4_total": 6,
            "v6_total": 4,
            "tx_queue_bytes": 0,
            "rx_queue_bytes": 0,
            "max_tx_queue_bytes": 0,
            "max_rx_queue_bytes": 0,
            "queued_sockets": 0,
            "socket_drops_total": 0,
        },
    }
    snmp = {
        "tcp": {
            "retrans_segs_delta": 0,
            "out_segs_delta": 500,
            "in_segs_delta": 480,
            "in_errs_delta": 0,
            "estab_resets_delta": 0,
            "attempt_fails_delta": 0,
            "out_rsts_delta": 0,
            "in_csum_errs_delta": 0,
            "retrans_rate_pct": 0.0,
            "curr_estab": 12,
        },
        "udp": {
            "in_datagrams_delta": 50,
            "out_datagrams_delta": 20,
            "no_ports_delta": 0,
            "in_errors_delta": 0,
            "rcvbuf_errors_delta": 0,
            "sndbuf_errors_delta": 0,
            "in_csum_errs_delta": 0,
            "mem_errors_delta": 0,
        },
    }
    softnet = {
        "cpu_count": 4,
        "processed_delta": 150,
        "dropped_delta": 0,
        "time_squeeze_delta": 0,
        "cpus_with_drops": 0,
        "cpus_with_squeeze": 0,
        "top_cpus": [{"cpu": 0, "processed_delta": 150, "dropped_delta": 0, "time_squeeze_delta": 0}],
    }
    return ifaces, summary, sockets, snmp, softnet


class TestParsers(unittest.TestCase):
    def test_parse_net_dev_standard_and_compact_colon(self) -> None:
        raw = "\n".join(
            [
                "Inter-|   Receive                                                |  Transmit",
                " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed",
                "    lo: 1000 10 0 0 0 0 0 0 1000 10 0 0 0 0 0 0",
                "enp3s0:999999 500 2 3 4 5 0 7 888888 400 1 6 8 9 11 0",
                "bad_line_without_enough_fields: 1 2 3",
            ]
        )
        parsed = snw.parse_net_dev(raw)
        self.assertEqual(set(parsed.keys()), {"lo", "enp3s0"})
        e = parsed["enp3s0"]
        self.assertEqual(e["rx_bytes"], 999999)
        self.assertEqual(e["rx_packets"], 500)
        self.assertEqual(e["rx_errs"], 2)
        self.assertEqual(e["rx_drop"], 3)
        self.assertEqual(e["rx_fifo"], 4)
        self.assertEqual(e["rx_frame"], 5)
        self.assertEqual(e["tx_bytes"], 888888)
        self.assertEqual(e["tx_packets"], 400)
        self.assertEqual(e["tx_errs"], 1)
        self.assertEqual(e["tx_drop"], 6)
        self.assertEqual(e["tx_fifo"], 8)
        self.assertEqual(e["tx_colls"], 9)
        self.assertEqual(e["tx_carrier"], 11)

    def test_parse_proc_net_tcp_all_states_and_queues(self) -> None:
        lines = ["  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode"]
        for idx, (hex_code, _name) in enumerate(snw.TCP_STATE_MAP.items()):
            txq = "00000100" if idx == 0 else "00000000"
            rxq = "00000200" if idx == 1 else "00000000"
            lines.append(f"  {idx}: 0100007F:1F90 0100007F:C350 {hex_code} {txq}:{rxq} 00:00000000 00000000 1000 0 12345")
        # Unknown state hex
        lines.append("  99: 0100007F:1F90 0100007F:C351 FF 00000000:00000000 00:00000000 00000000 1000 0 99999")
        res = snw.parse_proc_net_tcp("\n".join(lines))
        self.assertEqual(res["total"], len(snw.TCP_STATE_MAP) + 1)
        for name in snw.TCP_STATE_ORDER:
            self.assertEqual(res["states"][name], 1, msg=f"missing state {name}")
        self.assertEqual(res["states"]["UNKNOWN"], 1)
        self.assertEqual(res["tx_queue_bytes"], 0x100)
        self.assertEqual(res["rx_queue_bytes"], 0x200)
        self.assertEqual(res["queued_sockets"], 2)

    def test_parse_proc_net_udp_queues_and_drops(self) -> None:
        raw = "\n".join(
            [
                "   sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops",
                "10: 00000000:0035 00000000:0000 07 00000400:00001000 00:00000000 00000000  1000 0 101 2 0000000000000000 12",
                "11: 00000000:0036 00000000:0000 07 00000000:00000200 00:00000000 00000000  1000 0 102 2 0000000000000000 3",
                "12: 00000000:0037 00000000:0000 07 00000000:00000000 00:00000000 00000000  1000 0 103",
            ]
        )
        res = snw.parse_proc_net_udp(raw)
        self.assertEqual(res["total"], 3)
        self.assertEqual(res["tx_queue_bytes"], 0x400)
        self.assertEqual(res["rx_queue_bytes"], 0x1200)
        self.assertEqual(res["max_tx_queue_bytes"], 0x400)
        self.assertEqual(res["max_rx_queue_bytes"], 0x1000)
        self.assertEqual(res["queued_sockets"], 2)
        self.assertEqual(res["socket_drops_total"], 15)

    def test_parse_net_snmp(self) -> None:
        raw = "\n".join(
            [
                "Ip: Forwarding DefaultTTL",
                "Ip: 1 64",
                "Tcp: RtoAlgorithm MaxConn ActiveOpens PassiveOpens AttemptFails EstabResets CurrEstab InSegs OutSegs RetransSegs InErrs OutRsts InCsumErrors",
                "Tcp: 1 -1 100 50 7 4 22 5000 4000 12 3 9 1",
                "Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors InCsumErrors MemErrors",
                "Udp: 900 11 2 800 5 6 1 0",
            ]
        )
        res = snw.parse_net_snmp(raw)
        self.assertEqual(res["Tcp"]["RetransSegs"], 12)
        self.assertEqual(res["Tcp"]["OutSegs"], 4000)
        self.assertEqual(res["Tcp"]["InErrs"], 3)
        self.assertEqual(res["Tcp"]["EstabResets"], 4)
        self.assertEqual(res["Tcp"]["AttemptFails"], 7)
        self.assertEqual(res["Tcp"]["CurrEstab"], 22)
        self.assertEqual(res["Udp"]["InErrors"], 2)
        self.assertEqual(res["Udp"]["RcvbufErrors"], 5)
        self.assertEqual(res["Udp"]["SndbufErrors"], 6)

    def test_parse_softnet_stat_cpu_index_and_fallback(self) -> None:
        raw = "\n".join(
            [
                "00000100 00000002 0000000a 0 0 0 0 0 0 0 0 0 00000000 0 0",
                "00000200 00000000 00000001 0 0 0 0 0 0 0 0 0 00000004 0 0",
                "00000050 00000001 00000000",  # short line -> falls back to line idx 2
            ]
        )
        parsed = snw.parse_softnet_stat(raw)
        self.assertEqual(parsed[0], {"processed": 0x100, "dropped": 2, "time_squeeze": 10})
        self.assertEqual(parsed[4], {"processed": 0x200, "dropped": 0, "time_squeeze": 1})
        self.assertEqual(parsed[2], {"processed": 0x50, "dropped": 1, "time_squeeze": 0})

    def test_parse_iface_args(self) -> None:
        self.assertIsNone(snw.parse_iface_args(None))
        self.assertEqual(snw.parse_iface_args(["eno1,lo", "lo", " eth0 , eno1 "]), ["eno1", "lo", "eth0"])
        with self.assertRaises(ValueError):
            snw.parse_iface_args([" , , "])


class TestDeltaMath(unittest.TestCase):
    def test_net_dev_deltas_rates_clamp_and_filter(self) -> None:
        d0 = {
            "eno1": {
                "rx_bytes": 1000, "rx_packets": 10, "rx_errs": 1, "rx_drop": 0, "rx_fifo": 0, "rx_frame": 0,
                "tx_bytes": 2000, "tx_packets": 20, "tx_errs": 0, "tx_drop": 5, "tx_fifo": 0, "tx_colls": 0, "tx_carrier": 0,
            },
            "lo": {
                "rx_bytes": 500, "rx_packets": 5, "rx_errs": 0, "rx_drop": 0, "rx_fifo": 0, "rx_frame": 0,
                "tx_bytes": 500, "tx_packets": 5, "tx_errs": 0, "tx_drop": 0, "tx_fifo": 0, "tx_colls": 0, "tx_carrier": 0,
            },
        }
        d1 = {
            "eno1": {
                "rx_bytes": 13500, "rx_packets": 110, "rx_errs": 3, "rx_drop": 4, "rx_fifo": 1, "rx_frame": 2,
                "tx_bytes": 7000, "tx_packets": 70, "tx_errs": 1, "tx_drop": 2,  # lower -> clamped to 0
                "tx_fifo": 0, "tx_colls": 3, "tx_carrier": 1,
            },
            "lo": {
                "rx_bytes": 1500, "rx_packets": 15, "rx_errs": 0, "rx_drop": 0, "rx_fifo": 0, "rx_frame": 0,
                "tx_bytes": 1500, "tx_packets": 15, "tx_errs": 0, "tx_drop": 0, "tx_fifo": 0, "tx_colls": 0, "tx_carrier": 0,
            },
        }
        rows, summary = snw.compute_net_dev_deltas(d0, d1, dt_sec=2.0, iface_filter=["eno1"])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["iface"], "eno1")
        self.assertEqual(r["rx_bytes_delta"], 12500)
        self.assertEqual(r["rx_kbps"], 50.0)  # 12500*8 / (2*1000)
        self.assertEqual(r["tx_bytes_delta"], 5000)
        self.assertEqual(r["tx_kbps"], 20.0)
        self.assertEqual(r["rx_pps"], 50.0)
        self.assertEqual(r["tx_pps"], 25.0)
        self.assertEqual(r["rx_errs_delta"], 2)
        self.assertEqual(r["tx_errs_delta"], 1)
        self.assertEqual(r["rx_drop_delta"], 4)
        self.assertEqual(r["tx_drop_delta"], 0)  # clamped
        self.assertEqual(r["link_errors_delta"], 2 + 1 + 1 + 0 + 2 + 1)
        self.assertEqual(summary["total_interfaces"], 1)
        self.assertEqual(summary["link_errors_delta"], 7)

        # Single-sample mode (dt_sec == 0) yields zero deltas
        rows_zero, summary_zero = snw.compute_net_dev_deltas(d1, d1, dt_sec=0.0)
        self.assertEqual(summary_zero["rx_bytes_delta"], 0)
        self.assertEqual(rows_zero[0]["rx_kbps"], 0.0)

    def test_snmp_deltas_and_retransmit_rate(self) -> None:
        s0 = {"Tcp": {"RetransSegs": 100, "OutSegs": 1000, "InErrs": 5, "EstabResets": 10, "AttemptFails": 2, "CurrEstab": 40},
              "Udp": {"InErrors": 1, "RcvbufErrors": 2, "SndbufErrors": 0}}
        s1 = {"Tcp": {"RetransSegs": 125, "OutSegs": 2000, "InErrs": 3, "EstabResets": 15, "AttemptFails": 6, "CurrEstab": 42},
              "Udp": {"InErrors": 4, "RcvbufErrors": 5, "SndbufErrors": 1}}
        d = snw.compute_snmp_deltas(s0, s1)
        self.assertEqual(d["tcp"]["retrans_segs_delta"], 25)
        self.assertEqual(d["tcp"]["out_segs_delta"], 1000)
        self.assertEqual(d["tcp"]["retrans_rate_pct"], 2.5)
        self.assertEqual(d["tcp"]["in_errs_delta"], 0)  # clamped
        self.assertEqual(d["tcp"]["estab_resets_delta"], 5)
        self.assertEqual(d["tcp"]["attempt_fails_delta"], 4)
        self.assertEqual(d["udp"]["in_errors_delta"], 3)
        self.assertEqual(d["udp"]["rcvbuf_errors_delta"], 3)
        self.assertEqual(d["udp"]["sndbuf_errors_delta"], 1)

        # OutSegs delta == 0 with RetransSegs delta > 0 -> 100.0%
        d_zero_out = snw.compute_snmp_deltas(
            {"Tcp": {"RetransSegs": 10, "OutSegs": 500}, "Udp": {}},
            {"Tcp": {"RetransSegs": 15, "OutSegs": 500}, "Udp": {}},
        )
        self.assertEqual(d_zero_out["tcp"]["retrans_rate_pct"], 100.0)

        # No interval -> all deltas 0
        d_single = snw.compute_snmp_deltas(s0, s1, has_interval=False)
        self.assertEqual(d_single["tcp"]["retrans_segs_delta"], 0)
        self.assertEqual(d_single["tcp"]["retrans_rate_pct"], 0.0)

    def test_softnet_deltas_wrap_and_reset_clamp(self) -> None:
        sn0 = {
            0: {"processed": 0xFFFFFFF0, "dropped": 10, "time_squeeze": 5},
            1: {"processed": 1000, "dropped": 20, "time_squeeze": 10},
        }
        sn1 = {
            0: {"processed": 0x00000010, "dropped": 14, "time_squeeze": 12},  # u32 wrap = 0x20 (32)
            1: {"processed": 500, "dropped": 5, "time_squeeze": 10},  # counter reset -> clamp to 0
        }
        d = snw.compute_softnet_deltas(sn0, sn1, top_n=1)
        self.assertEqual(d["cpu_count"], 2)
        self.assertEqual(d["processed_delta"], 32)
        self.assertEqual(d["dropped_delta"], 4)
        self.assertEqual(d["time_squeeze_delta"], 7)
        self.assertEqual(d["cpus_with_drops"], 1)
        self.assertEqual(d["cpus_with_squeeze"], 1)
        self.assertEqual(len(d["top_cpus"]), 1)
        self.assertEqual(d["top_cpus"][0]["cpu"], 0)


class TestVerdictClassification(unittest.TestCase):
    def _assert_issue_invariants(self, v: dict, expected_status: str) -> None:
        self.assertEqual(v["status"], expected_status)
        self.assertTrue(v["is_issue"])
        self.assertTrue(len(v["reasons"]) > 0)
        self.assertTrue(len(v["recommendations"]) > 0)
        for rec in v["recommendations"]:
            self.assertNotIn("No immediate intervention needed.", rec)

    def test_healthy_verdict(self) -> None:
        ifaces, summary, sockets, snmp, softnet = _healthy_inputs()
        v = snw.classify_network_verdict(ifaces, summary, sockets, snmp, softnet)
        self.assertEqual(v["status"], "HEALTHY")
        self.assertFalse(v["is_issue"])
        self.assertEqual(v["recommendations"], ["No immediate intervention needed."])

    def test_link_errors_verdict(self) -> None:
        ifaces, summary, sockets, snmp, softnet = _healthy_inputs()
        ifaces[0]["rx_errs_delta"] = 2
        ifaces[0]["link_errors_delta"] = 2
        summary["rx_errs_delta"] = 2
        summary["link_errors_delta"] = 2
        v = snw.classify_network_verdict(ifaces, summary, sockets, snmp, softnet)
        self._assert_issue_invariants(v, "LINK_ERRORS")

    def test_packet_drops_nic_and_softnet_and_udp(self) -> None:
        # NIC drops
        ifaces, summary, sockets, snmp, softnet = _healthy_inputs()
        ifaces[0]["rx_drop_delta"] = 12
        ifaces[0]["drops_delta"] = 12
        summary["rx_drop_delta"] = 12
        summary["drops_delta"] = 12
        v = snw.classify_network_verdict(ifaces, summary, sockets, snmp, softnet)
        self._assert_issue_invariants(v, "PACKET_DROPS")

        # Softnet drops
        ifaces, summary, sockets, snmp, softnet = _healthy_inputs()
        softnet["dropped_delta"] = 3
        softnet["cpus_with_drops"] = 1
        v2 = snw.classify_network_verdict(ifaces, summary, sockets, snmp, softnet)
        self._assert_issue_invariants(v2, "PACKET_DROPS")

        # UDP rcvbuf errors
        ifaces, summary, sockets, snmp, softnet = _healthy_inputs()
        snmp["udp"]["rcvbuf_errors_delta"] = 4
        v3 = snw.classify_network_verdict(ifaces, summary, sockets, snmp, softnet)
        self._assert_issue_invariants(v3, "PACKET_DROPS")

    def test_elevated_retransmits_verdict_and_low_volume_guard(self) -> None:
        # 1 retrans out of 2 segs -> low volume, stays HEALTHY
        ifaces, summary, sockets, snmp, softnet = _healthy_inputs()
        snmp["tcp"]["retrans_segs_delta"] = 1
        snmp["tcp"]["out_segs_delta"] = 2
        snmp["tcp"]["retrans_rate_pct"] = 50.0
        v_low = snw.classify_network_verdict(ifaces, summary, sockets, snmp, softnet)
        self.assertEqual(v_low["status"], "HEALTHY")

        # 15 retrans out of 500 segs (3.0%) -> ELEVATED_RETRANSMITS
        snmp["tcp"]["retrans_segs_delta"] = 15
        snmp["tcp"]["out_segs_delta"] = 500
        snmp["tcp"]["retrans_rate_pct"] = 3.0
        v_high = snw.classify_network_verdict(ifaces, summary, sockets, snmp, softnet)
        self._assert_issue_invariants(v_high, "ELEVATED_RETRANSMITS")

    def test_socket_pressure_close_wait_time_wait_syn_udp_queue(self) -> None:
        # CLOSE_WAIT leak
        ifaces, summary, sockets, snmp, softnet = _healthy_inputs()
        sockets["tcp"]["states"]["CLOSE_WAIT"] = 75
        v_cw = snw.classify_network_verdict(ifaces, summary, sockets, snmp, softnet)
        self._assert_issue_invariants(v_cw, "SOCKET_PRESSURE")
        self.assertTrue(any("CLOSE_WAIT" in r for r in v_cw["reasons"]))

        # TIME_WAIT flood
        ifaces, summary, sockets, snmp, softnet = _healthy_inputs()
        sockets["tcp"]["states"]["TIME_WAIT"] = 2500
        v_tw = snw.classify_network_verdict(ifaces, summary, sockets, snmp, softnet)
        self._assert_issue_invariants(v_tw, "SOCKET_PRESSURE")
        self.assertTrue(any("TIME_WAIT" in r for r in v_tw["reasons"]))

        # SYN backlog buildup
        ifaces, summary, sockets, snmp, softnet = _healthy_inputs()
        sockets["tcp"]["states"]["SYN_RECV"] = 150
        v_syn = snw.classify_network_verdict(ifaces, summary, sockets, snmp, softnet)
        self._assert_issue_invariants(v_syn, "SOCKET_PRESSURE")
        self.assertTrue(any("SYN_RECV" in r for r in v_syn["reasons"]))

        # UDP queue pressure
        ifaces, summary, sockets, snmp, softnet = _healthy_inputs()
        sockets["udp"]["rx_queue_bytes"] = 524288
        sockets["udp"]["queued_sockets"] = 4
        v_udp = snw.classify_network_verdict(ifaces, summary, sockets, snmp, softnet)
        self._assert_issue_invariants(v_udp, "SOCKET_PRESSURE")
        self.assertTrue(any("udp_queue" in r for r in v_udp["reasons"]))

    def test_verdict_precedence(self) -> None:
        ifaces, summary, sockets, snmp, softnet = _healthy_inputs()
        summary["link_errors_delta"] = 1
        summary["drops_delta"] = 20
        snmp["tcp"]["retrans_segs_delta"] = 60
        snmp["tcp"]["retrans_rate_pct"] = 5.0
        sockets["tcp"]["states"]["CLOSE_WAIT"] = 100
        v = snw.classify_network_verdict(ifaces, summary, sockets, snmp, softnet)
        self._assert_issue_invariants(v, "LINK_ERRORS")
        # All 4 categories should contribute reasons
        self.assertEqual(len(v["reasons"]), 4)


class TestSyntheticProcAndCLI(unittest.TestCase):
    def _write_fake_proc(self, root: Path, retrans: int = 0, out_segs: int = 100, cw_count: int = 0) -> None:
        net = root / "net"
        net.mkdir(parents=True, exist_ok=True)
        (net / "dev").write_text(
            "Inter-| Receive | Transmit\n"
            " face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"
            "    lo: 1000 10 0 0 0 0 0 0 1000 10 0 0 0 0 0 0\n"
            "  eth0: 5000 50 0 0 0 0 0 0 4000 40 0 0 0 0 0 0\n",
            encoding="utf-8",
        )
        tcp_lines = ["  sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode"]
        for i in range(cw_count):
            tcp_lines.append(f"  {i}: 0100007F:1F90 0100007F:C350 08 00000000:00000000 00:00000000 00000000 1000 0 {i}")
        (net / "tcp").write_text("\n".join(tcp_lines) + "\n", encoding="utf-8")
        (net / "tcp6").write_text(tcp_lines[0] + "\n", encoding="utf-8")
        (net / "udp").write_text("  sl local_address rem_address st tx_queue rx_queue\n", encoding="utf-8")
        (net / "udp6").write_text("  sl local_address rem_address st tx_queue rx_queue\n", encoding="utf-8")
        (net / "snmp").write_text(
            "Tcp: RetransSegs OutSegs InSegs InErrs EstabResets AttemptFails OutRsts InCsumErrors CurrEstab\n"
            f"Tcp: {retrans} {out_segs} 100 0 0 0 0 0 5\n"
            "Udp: InDatagrams OutDatagrams NoPorts InErrors RcvbufErrors SndbufErrors InCsumErrors MemErrors\n"
            "Udp: 10 10 0 0 0 0 0 0\n",
            encoding="utf-8",
        )
        (net / "softnet_stat").write_text(
            "00000010 00000000 00000000 0 0 0 0 0 0 0 0 0 00000000 0 0\n",
            encoding="utf-8",
        )

    def test_watch_network_synthetic_samples_and_missing_iface(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_fake_proc(root, cw_count=60)
            rep = snw.watch_network(interval=0.1, samples=1, iface_filter=["eth0"], top_n=4, proc_root=str(root))
            self.assertEqual(rep["version"], snw.VERSION)
            self.assertEqual(rep["samples"], 1)
            self.assertEqual(rep["sample_sec"], 0.0)
            self.assertEqual(rep["verdict"]["status"], "SOCKET_PRESSURE")
            self.assertEqual([r["iface"] for r in rep["interfaces"]], ["eth0"])

            txt = snw.render_text(rep, markdown=False)
            self.assertIn("Status: SOCKET_PRESSURE", txt)
            md = snw.render_text(rep, markdown=True)
            self.assertIn("# Network Investigation @", md)
            self.assertIn("## Recommendations", md)

            with self.assertRaises(ValueError):
                snw.watch_network(interval=0.1, samples=1, iface_filter=["nope0"], proc_root=str(root))

            with self.assertRaises(OSError):
                snw.watch_network(interval=0.1, samples=1, proc_root=str(root / "missing"))

    def test_cli_validation_and_exit_codes(self) -> None:
        # Invalid --interval
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            rc = snw.main(["--interval", "0.01"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(stderr.getvalue().strip().splitlines()), 1)

        # Invalid --samples
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            rc = snw.main(["--samples", "0"])
        self.assertEqual(rc, 1)
        self.assertIn("--samples", stderr.getvalue())

        # Invalid --top
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            rc = snw.main(["--top", "0"])
        self.assertEqual(rc, 1)

        # Empty --iface
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            rc = snw.main(["--iface", " , "])
        self.assertEqual(rc, 1)

        # Invalid env var
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"SYS_NET_WATCH_INTERVAL": "notanumber"}), mock.patch.object(sys, "stderr", stderr):
            rc = snw.main([])
        self.assertEqual(rc, 1)
        self.assertIn("SYS_NET_WATCH_INTERVAL", stderr.getvalue())

        # --exit-on-issue returns 3 when is_issue=True
        fake_issue_rep = {
            "timestamp": "2026-11-23T00:00:00+00:00",
            "version": snw.VERSION,
            "sample_sec": 1.0,
            "samples": 2,
            "iface_filter": [],
            "verdict": {
                "status": "PACKET_DROPS",
                "is_issue": True,
                "reasons": ["Packet drops detected."],
                "recommendations": ["Tune netdev_max_backlog."],
            },
            "interfaces": [],
            "interfaces_summary": {},
            "sockets": {"tcp": {"states": {}}, "udp": {}},
            "snmp": {"tcp": {}, "udp": {}},
            "softnet": {"top_cpus": []},
        }
        stdout = io.StringIO()
        with mock.patch.object(snw, "watch_network", return_value=fake_issue_rep), mock.patch.object(sys, "stdout", stdout):
            rc = snw.main(["--json", "--exit-on-issue"])
        self.assertEqual(rc, 3)
        parsed = json.loads(stdout.getvalue())
        self.assertEqual(parsed["verdict"]["status"], "PACKET_DROPS")

    def test_cli_version_and_argparse_exit_2(self) -> None:
        r_ver = subprocess.run([sys.executable, str(SCRIPT), "--version"], capture_output=True, text=True, check=False)
        self.assertEqual(r_ver.returncode, 0)
        self.assertEqual(r_ver.stdout.strip(), f"sys-net-watch {snw.VERSION}")

        r_bad = subprocess.run([sys.executable, str(SCRIPT), "--unknown-flag"], capture_output=True, text=True, check=False)
        self.assertEqual(r_bad.returncode, 2)
        self.assertNotIn("Traceback", r_bad.stderr)


if __name__ == "__main__":
    unittest.main()
