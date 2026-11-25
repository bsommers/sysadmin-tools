[← Back to User's Guide](README.md) | [← Project README](../../README.md)

# Chapter 7: Network & Systemd Cgroup Watch (`sys-net-watch`, `sys-systemd-watch`)

Both `sys-net-watch` and `sys-systemd-watch` extend `sysadmin-tools` with zero-dependency, zero-subprocess network and cgroup v2 observability. Neither tool invokes `ss`, `netstat`, `ethtool`, `systemctl`, or `journalctl`, keeping them responsive even when storage or user space is severely degraded.

---

## 1. Network Investigation (`sys-net-watch`)

`sys-net-watch` reads `/proc/net/dev`, `/proc/net/{tcp,tcp6,udp,udp6}`, `/proc/net/snmp`, and `/proc/net/softnet_stat` directly across `--samples` snapshots (default `2`) separated by `--interval` seconds (default `1.0`).

### Quick Commands

```bash
# Default 1-second delta across all interfaces and socket tables
sys-net-watch

# Filter specific interfaces across 3 samples (0.5s apart)
sys-net-watch --iface eno1,lo --samples 3 --interval 0.5

# Single snapshot of socket queues/states without sleeping
sys-net-watch --samples 1

# Automated alerting (exits 3 when status != HEALTHY)
sys-net-watch --exit-on-issue --json | jq '.verdict'
sys-net-watch --markdown
```

### Complete CLI Options

| Flag | Range / Default | Environment Variable | Purpose |
|---|---|---|---|
| `--interval SEC` | `0.1..60.0` (default `1.0`) | `SYS_NET_WATCH_INTERVAL` | Sampling interval in seconds between snapshots. |
| `--samples N` | `>= 1` (default `2`) | — | Number of snapshots to collect (`2` = one delta window). |
| `--iface IFACE` | repeatable or comma-separated | — | Filter `/proc/net/dev` interfaces (e.g., `--iface eno1,lo`). |
| `--top N` | `1..100` (default `8`) | `SYS_NET_WATCH_TOP` | Maximum interface and softnet CPU rows to display. |
| `--json` | flag (mutually exclusive with `--markdown`) | — | Emit deterministic JSON (`sort_keys=True`) to `stdout`. |
| `--markdown` | flag (mutually exclusive with `--json`) | — | Emit Markdown report to `stdout`. |
| `--exit-on-issue` | flag | — | Exit with code `3` when verdict status is not `HEALTHY`. |
| `--verbose` | flag | — | Print effective configuration to `stderr`. |
| `--version` | flag | — | Print `sys-net-watch 0.5.0` and exit. |

### What Each Section Measures

- **Interfaces (`/proc/net/dev`):** `RX_kbps`, `TX_kbps`, `RX_pps`, `TX_pps`, and delta counters for `rx_errs`, `tx_errs`, `rx_drop`, `tx_drop`, `fifo`, `frame`, `carrier`, and `colls`.
- **Sockets (`/proc/net/{tcp,tcp6,udp,udp6}`):** Totals across IPv4/IPv6, all 12 TCP states (`ESTABLISHED`, `LISTEN`, `TIME_WAIT`, `CLOSE_WAIT` + `close_wait_delta`, `SYN_SENT`, `SYN_RECV`, `FIN_WAIT1`, `FIN_WAIT2`, `LAST_ACK`, `CLOSING`, `CLOSE`, `NEW_SYN_RECV`), hex TX/RX socket queue byte sums, and UDP `socket_drops_total`.
- **SNMP Deltas (`/proc/net/snmp`):** `dOutSegs`, `dRetrans` (`retrans_rate_pct`), `dInErrs`, `dEstabResets`, `dAttemptFails`, `dInCsumErrs`, and UDP `dInErrors` / `dRcvbufErrs` / `dSndbufErrs` / `dMemErrs`.
- **Softnet Deltas (`/proc/net/softnet_stat`):** 32-bit wrap-safe per-CPU `processed_delta`, `dropped_delta`, and `time_squeeze_delta`.

### Verdict Statuses (Priority Order)

| Priority | Status | `is_issue` | Trigger Thresholds |
|---|---|---|---|
| 1 | `LINK_ERRORS` | `True` | `link_errors_delta >= 1` OR `tx_colls_delta >= 10` OR `tcp_in_errs >= 5` OR `tcp_csum_errs >= 1` OR `udp_csum_errs >= 1` |
| 2 | `PACKET_DROPS` | `True` | `iface_drops_delta >= 5` OR `softnet_drops >= 1` OR `softnet_time_squeeze >= 50` OR `udp_buf_errs >= 1` OR `udp_in_errs >= 5` OR `udp_mem_errs >= 1` |
| 3 | `ELEVATED_RETRANSMITS` | `True` | (`retrans >= 5` & `retrans_rate >= 1.0%`) OR `retrans >= 50` OR `estab_resets >= 20` OR `attempt_fails >= 20` |
| 4 | `SOCKET_PRESSURE` | `True` | `CLOSE_WAIT >= 50` (or `>= 20` & delta `>= 10`) OR `TIME_WAIT >= 2000` OR `SYN_RECV/SENT >= 100` OR `FIN_WAIT1/2+LAST_ACK >= 500` OR `udp_queue >= 256 KiB` / `>= 20` sockets OR `tcp_queue >= 16 MiB` |
| 5 | `HEALTHY` | `False` | None of the above |

---

## 2. Systemd Cgroup v2 Watch (`sys-systemd-watch`)

`sys-systemd-watch` walks `/sys/fs/cgroup` directly to discover `*.service`, `*.scope`, and `*.slice` units, correlates `/proc/<pid>/{stat,cgroup}` for process states (`D`/`Z`) and ages, and measures `/run/log/journal` + `/var/log/journal` directory size deltas without invoking `systemctl` or `journalctl`.

### Quick Commands

```bash
# Default 1-second, 2-sample cgroup v2 & journal check
sys-systemd-watch

# Filter units matching "docker" or a glob pattern across 3 samples
sys-systemd-watch --unit docker --samples 3 --interval 1.0
sys-systemd-watch --unit '*.service' --top 15

# Custom cgroup root (e.g., container or fixture testing) and alert exit code
sys-systemd-watch --cgroup-root /sys/fs/cgroup --exit-on-issue --json | jq '.verdict'
sys-systemd-watch --markdown
```

### Complete CLI Options

| Flag | Range / Default | Environment Variable | Purpose |
|---|---|---|---|
| `--interval SEC` | `0.05..60.0` (default `1.0`) | `SYS_SYSTEMD_WATCH_INTERVAL` | Seconds between cgroup snapshots. |
| `--samples N` | `1..60` (default `2`) | — | Number of cgroup snapshots to collect. |
| `--top N` | `1..100` (default `10`) | `SYS_SYSTEMD_WATCH_TOP` | Maximum rows per section (`problem_units`, `top_memory`, `top_cpu`, `top_io`, `top_slices`). |
| `--unit PATTERN` | string (substring or glob) | — | Filter by unit name or cgroup path (e.g., `docker`, `'*.service'`). |
| `--cgroup-root PATH` | path (default `/sys/fs/cgroup`) | — | Cgroup v2 root directory to inspect. |
| `--json` | flag (mutually exclusive with `--markdown`) | — | Emit deterministic JSON (`sort_keys=True`) to `stdout`. |
| `--markdown` | flag (mutually exclusive with `--json`) | — | Emit Markdown report to `stdout`. |
| `--exit-on-issue` | flag | — | Exit with code `3` when verdict status is not `HEALTHY`. |
| `--verbose` | flag | — | Print effective configuration to `stderr`. |
| `--version` | flag | — | Print `sys-systemd-watch 0.5.0` and exit. |

### Verdict Statuses (Priority Order)

| Priority | Status | `is_issue` | Trigger Summary |
|---|---|---|---|
| 1 | `MEMORY_PRESSURE` | `True` | Unit `oom` / `oom_kill` events `> 0`, `memory.events` `max > 0` / `high >= 5`, `memory.pressure` `full10 >= 5%` / `some10 >= 15%`, or `memory.current >= 95%` of `memory.max`. |
| 2 | `CRASH_LOOP_SUSPECTED` | `True` | `*.service` unit with `full_pid_replacements >= 1`, rapid PID churn (`+>=2 / ->=2`), or newly restarted young PIDs (`max_age <= 3s`) with exits/zombies/OOM. |
| 3 | `IO_PRESSURE` | `True` | `io.pressure` `full10 >= 10%` / `some10 >= 25%`, large PSI delta, or `d_state_pids >= 4` with active I/O / PSI. |
| 4 | `CPU_THROTTLING` | `True` | `throttled_pct >= 10%`, `d_throttled >= 3` & ratio `>= 20%`, `cpu.pressure` `some10 >= 25%` / `full10 >= 10%`, or sustained lifetime CFS throttling. |
| 5 | `JOURNAL_BURST` | `True` | Journal directory growth `>= 1.0 MiB/s` AND `>= 512 KiB` across the window. |
| 6 | `HEALTHY` | `False` | No flagged unit pressure, crash loops, or journal bursts. |

> **Leaf-unit preference:** When attributing `MEMORY_PRESSURE`, `IO_PRESSURE`, or `CPU_THROTTLING`, `sys-systemd-watch` prefers leaf `*.service` and `*.scope` units over parent `*.slice` containers so you see the exact offending service first.

---

**Navigation:** [← Previous: Chapter 6 — Automation & Troubleshooting](06-automation-and-troubleshooting.md) | [↑ Back to User's Guide](README.md) | [↑ Project README](../../README.md)
