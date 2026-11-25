[← Back to User's Guide](README.md) | [← Project README](../../README.md)

# Chapter 1: Getting Started

This chapter covers runtime prerequisites, root-NVMe installation, local verification across all 166 tests, and a quick first-run tour of all seven `sysadmin-tools` CLIs.

---

## 1. Prerequisites

`sysadmin-tools` is engineered to run out of the box on virtually any modern Linux host with zero external runtime packages:

- **Operating System:** Linux with `/proc` and `/sys` mounted (kernel 4.20+ recommended for `/proc/pressure/{cpu,io,memory}` PSI metrics and unified cgroup v2 `/sys/fs/cgroup`; tools gracefully handle missing PSI or optional sysfs files).
- **Python:** Python **3.10 or newer** (`python3 --version`).
- **Third-Party Runtime Dependencies:** **None.** Every tool uses Python standard library modules exclusively.
- **Optional Permissions & Tools:**
  - Running as a regular user works for all 7 CLIs.
  - Reading other users' `/proc/<pid>/io` counters in `sys-investigate` requires `root` (otherwise those PIDs fail open cleanly without crashing).
  - Reading system journals in `sys-crash-triage` requires membership in the `adm` or `systemd-journal` group (or `sudo`), or you can pass `--no-journalctl` for pure file-based triage.
  - `jq` is optional and helpful for scripting `--json` pipelines (covered in [Chapter 6](06-automation-and-troubleshooting.md)).

---

## 2. Installation

Clone or navigate to the repository, preview the install plan with `--dry-run`, and then install the seven standalone executables:

```bash
# Preview what will be installed (no files modified)
./scripts/install.sh --dry-run

# Install standalone copies into ~/.local/bin (default)
./scripts/install.sh

# Or install into a custom directory (e.g., /usr/local/bin)
sudo ./scripts/install.sh --prefix /usr/local/bin
```

Ensure `~/.local/bin` is on your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Installed Executables

| Command | Source Script | Installed Copy (Default) |
|---|---|---|
| `sys-investigate` | `src/sysadmin_tools/sys_investigate.py` | `~/.local/bin/sys-investigate` |
| `sys-io-guard` | `src/sysadmin_tools/sys_io_guard.py` | `~/.local/bin/sys-io-guard` |
| `sys-mem-watch` | `src/sysadmin_tools/sys_mem_watch.py` | `~/.local/bin/sys-mem-watch` |
| `sys-disk-hogs` | `src/sysadmin_tools/sys_disk_hogs.py` | `~/.local/bin/sys-disk-hogs` |
| `sys-crash-triage` | `src/sysadmin_tools/sys_crash_triage.py` | `~/.local/bin/sys-crash-triage` |
| `sys-net-watch` | `src/sysadmin_tools/sys_net_watch.py` | `~/.local/bin/sys-net-watch` |
| `sys-systemd-watch` | `src/sysadmin_tools/sys_systemd_watch.py` | `~/.local/bin/sys-systemd-watch` |

### Why Direct Root-NVMe Copies Beat Symlinks

`scripts/install.sh` uses `install -m 0755` to write **direct file copies** rather than symlinks pointing back into your source tree, and every script in `src/sysadmin_tools/` has **zero cross-module imports** (no `PYTHONPATH` or editable package imports required).

Why this matters during an incident:
1. Developer repositories often live on secondary data SSDs (such as `/datanapps`) where heavy builds or databases run.
2. When that secondary SSD enters writeback collapse (`99% util`, `>1,000 ms` write latency, hundreds of `D`-state threads), resolving a symlink or importing Python modules from that mount can hang your diagnostic tool for minutes.
3. Because `~/.local/bin/sys-*` lives as a self-contained file on your root NVMe, you can diagnose a wedged secondary disk immediately with zero dependency on the failing mount.

> **Tip:** After pulling updates to the repository, re-run `./scripts/install.sh` to refresh the installed copies in `~/.local/bin`.

---

## 3. Verification

Run the composable local verification suite before relying on installed tools or after any local edit:

```bash
# Run all stages: syntax + unit + smoke (166 tests total)
./scripts/verify.sh
```

`./scripts/verify.sh` runs three stages and writes structured reports to `target/verify/` (`summary.tsv`, `report.md`, `syntax.log`, `unit.log`, `smoke.log`):

| Stage | What It Checks | Verified Coverage |
|---|---|---|
| `syntax` | `python3 -m py_compile` on `src/sysadmin_tools/*.py`, `tests/unit/test_*.py`, `tests/smoke/test_*.py` | All source and test files |
| `unit` | Offline deterministic unit & CLI error-path tests in `tests/unit/` | **143 tests** |
| `smoke` | Live end-to-end smoke tests against real Linux `/proc`, `/sys`, and filesystems in `tests/smoke/` | **23 tests** |
| **Total** | Full suite (`./scripts/verify.sh`) | **166 tests** |

### Targeted Verification Flags

```bash
# Run syntax + offline unit tests only (fast feedback)
./scripts/verify.sh --quick

# Run a specific tier: unit, smoke, or all
./scripts/verify.sh --tier smoke

# Filter test methods/classes by pattern (unittest -k)
./scripts/verify.sh --tier unit --match crash
```

---

## 4. First-Run Tour of All 7 CLIs

Run each CLI once on a healthy system to establish a baseline and verify your environment:

### 4.1 `sys-investigate` — Live System & Bottleneck Investigator

```bash
sys-investigate --interval 2 --top 10
```

Takes two `/proc` + `/sys` snapshots separated by `--interval` seconds (default `2.0`), then prints a verdict (`Bottleneck`, `Productivity`), CPU/Load/PSI/Memory summary, per-disk latency & utilization, NVMe thermals, blocked `D`-state threads, instantaneous `%CPU` consumers, process I/O, overlapping build workloads, and actionable recommendations.

### 4.2 `sys-io-guard` — Per-Disk Build & I/O Serializer

```bash
# Preview lock resolution and env overrides without executing anything
sys-io-guard --dry-run --path . -- echo "hello guarded world"

# Inspect active/idle locks in the tmpfs lock directory
sys-io-guard --status
```

Resolves the target directory to its backing physical disk via `/proc/mounts` (and `/sys/block/dm-*/slaves`), acquires an exclusive `flock` on tmpfs, sets `CARGO_BUILD_JOBS` and child `nice`, and executes your command directly without a shell.

### 4.3 `sys-mem-watch` — Memory Growth, Swap & OOM Investigator

```bash
sys-mem-watch --interval 2 --top 10
```

Measures two-snapshot RAM/swap deltas, `pgscan_direct` / `pgscan_kswapd` reclaim activity, memory PSI, and ranks top RSS consumers, fastest RSS growers (`MiB/s`), swap users, and OOM-killer candidates (`oom_score` / `oom_score_adj`).

### 4.4 `sys-disk-hogs` — Workspace & Cache Disk Usage Analyzer

```bash
sys-disk-hogs . --top 10 --depth 2
```

Walks directories iteratively via `os.scandir()` (zero `du`/`find` subprocesses), deduplicates hardlinks, stays on one filesystem by default, and separates reclaimable build/cache directories (`target/`, `node_modules/`, `.venv/`, `__pycache__/`, etc.) from non-reclaimable `.git` repositories.

### 4.5 `sys-crash-triage` — Post-Crash & Unexpected-Reboot Forensics

```bash
sys-crash-triage --show-steps
```

Correlates boot history, downtime gaps, AMD FCH `S5_RESET_STATUS` hardware reset bits, unclean journal/filesystem recovery markers, NUL-byte (`\x00`) gaps in `/var/log/kern.log` and `/var/log/syslog`, fatal/context crash signatures, and `kdump` / `pstore` / EDAC readiness.

### 4.6 `sys-net-watch` — Network, Socket & Softnet Investigator

```bash
sys-net-watch --interval 1 --samples 2 --top 8
```

Reads `/proc/net/{dev,tcp,tcp6,udp,udp6,snmp,softnet_stat}` directly to track NIC RX/TX throughput, link errors and drops, TCP/UDP socket queues, `CLOSE_WAIT` leaks, TCP retransmit percentage, and per-CPU softnet `dropped`/`time_squeeze` deltas.

### 4.7 `sys-systemd-watch` — Systemd Cgroup v2 Pressure & Churn Watch

```bash
sys-systemd-watch --interval 1 --samples 2 --top 10
```

Reads `/sys/fs/cgroup`, `/proc`, and journal directories directly with zero `systemctl` or `journalctl` subprocesses to surface cgroup memory/OOM events, CFS CPU throttling, I/O PSI, rapid service crash-loop PID churn, and journal byte-growth bursts.

---

**Navigation:** [← Back to User's Guide](README.md) | [Next: Chapter 2 — Live System Investigation →](02-live-system-investigation.md)
