# As-Built Buildspec — sysadmin-tools v0.5.0

Derived directly from live source (`src/sysadmin_tools/*.py`, 2026-11-24).

## 1. Purpose & Non-Negotiable Design Properties

`sysadmin-tools` provides fast, deterministic, zero-dependency Linux diagnostic and guard CLIs that stay responsive during severe CPU, memory, network, or storage stalls.

**Non-negotiable invariants (a rebuild that drops these is invalid):**
1. **Zero third-party dependencies:** Python 3.10+ standard library only.
2. **Self-contained standalone scripts:** each CLI in `src/sysadmin_tools/*.py` uses zero cross-module imports so `scripts/install.sh` can `install -m 0755` direct copies into `~/.local/bin` on root NVMe without `PYTHONPATH` or symlinks to secondary disks.
3. **Zero external CLI subprocesses on hot diagnostic paths:** `sys-investigate`, `sys-mem-watch`, `sys-disk-hogs`, `sys-net-watch`, `sys-systemd-watch`, and `sys-io-guard` disk resolution read `/proc`, `/sys`, and `os.scandir`/`fcntl.flock` directly (no `top`, `iostat`, `vmstat`, `ps`, `lsblk`, `df`, `du`, `ss`, `netstat`, `ethtool`, `systemctl`, `smartctl`, or `nvme` subprocesses).
4. **Two-snapshot delta accuracy:** CPU %, disk utilization/latency/throughput, swap paging, per-process `%CPU`, RSS/swap growth, disk MB/s, process turnover, NIC/SNMP/softnet packet deltas, and cgroup v2 CPU/IO/PID churn are computed strictly over measured `[t0, t1]` wall-clock intervals (`time.monotonic()`).
5. **Mount-stall resilience:** `os.statvfs()` skips non-local/FUSE/network/automount filesystems automatically and can be disabled via `--no-statvfs`; `sys-io-guard` stores locks on tmpfs/root (`$XDG_RUNTIME_DIR` or `/tmp`), never on secondary storage.
6. **Script-first UX:** data to `stdout`, diagnostics/verbose config to `stderr`; deterministic JSON (`sort_keys=True`); single-line stderr errors without tracebacks; documented exit codes.

## 2. Stack & Packaging

- **OS:** Linux (`/proc`, `/sys`, `/sys/fs/cgroup`, `fcntl.flock`).
- **Runtime:** Python `>=3.10` (stdlib only).
- **Packaging:** `pyproject.toml` (`setuptools`, v0.5.0) with console scripts `sys-investigate`, `sys-io-guard`, `sys-mem-watch`, `sys-disk-hogs`, `sys-crash-triage`, `sys-net-watch`, `sys-systemd-watch`, direct-copy installer `scripts/install.sh`, and user systemd sampler templates in `packaging/systemd/`.

## 3. Tool Contracts & Module Map

### 3.1 `sys-investigate` (`src/sysadmin_tools/sys_investigate.py`)
- **Core telemetry:** `/proc/stat`, `/proc/loadavg`, `/proc/pressure/{cpu,io,memory}`, `/proc/meminfo`, `/proc/vmstat`, `/proc/diskstats`, `/proc/mounts`, `/proc/<pid>/{stat,cmdline,statm,io,status,oom_score,cgroup,cwd,fd/*,task/*}`, `/sys/class/drm/card*/device/gpu_busy_percent`, `/sys/class/nvme/nvme*/{model,state,hwmon*/temp*_*}`, `/sys/class/thermal/thermal_zone*/{type,temp}`.
- **Key functions:** `read_cpu_times`, `compute_cpu_pct`, `read_loadavg`, `read_psi`, `read_meminfo`, `read_vmstat_swap`, `is_whole_physical_disk`, `read_diskstats`, `is_safe_statvfs_fstype`, `map_disks_to_mounts`, `resolve_mount_for_path`, `attribute_pid_mounts`, `parse_cgroup_text`, `read_proc_cgroup`, `read_nvme_health`, `read_thermal_zones`, `compute_disk_deltas`, `read_gpu_busy`, `parse_proc_stat`, `snapshot_procs`, `build_ancestry`, `detect_overlapping_workloads`, `classify_verdict`, `compare_windows`, `append_jsonl_log`, `load_jsonl_log`, `classify_log_trend`, `diff_jsonl_logs`, `render_diff_text`, `investigate`, `investigate_progress`, `inspect_pid_threads`, `classify_pid_verdict`, `investigate_pid`, `render_text`, `render_pid_text`, `main`.
- **Verdict rules:**
  - `sat_disks`: `util_pct >= 85` OR `w_await_ms >= 50` OR `aqu_sz >= 8`.
  - `thrashing`: `balance_dirty_pages >= 4` OR `dirty_gib >= 2.0` OR (`util >= 90%` & `w_await >= 100ms`) OR (`w_await >= 250ms` & `aqu_sz >= 16`) OR `mem_thrash`.
  - `NVMe thermal`: `CRITICAL` (`alarm=True`, `temp_c >= temp_crit_c`, or `state != "live"`) forces `is_issue=True` and adds a thermal recommendation; `WARM` (`temp_c >= temp_max_c` or `>= 70°C`) adds a warning reason & recommendation.
  - `--diff-log` trend verdicts: `DEGRADING`, `PERSISTENT_ISSUE`, `RECOVERING`, `INTERMITTENT_ISSUES`, `STABLE_HEALTHY`, `SINGLE_SNAPSHOT_ISSUE`, `SINGLE_SNAPSHOT_HEALTHY` (splits single file into early vs late halves or `prior N` vs `last N` with `--diff-window N`, or compares `--diff-baseline PATH` against `--diff-log PATH`, skipping corrupt JSONL lines with stderr warning).
  - Invariant: `is_issue=True` never emits `"No immediate intervention needed."`.
- **CLI flags:** `--interval` (0.2..60.0, env `SYS_INVESTIGATE_INTERVAL`), `--top` (1..100, env `SYS_INVESTIGATE_TOP`), `--json` / `--markdown`, `--watch [COUNT]`, `--progress-check`, `--diagnose-pid PID`, `--no-statvfs`, `--append-log PATH`, `--diff-log PATH`, `--diff-baseline PATH`, `--diff-window N` (`>= 1`), `--exit-on-issue`, `--verbose`, `--version`.
- **Exit codes:** `0` ok, `1` validation/missing-PID/missing-or-empty-log/runtime error, `2` argparse error, `3` issue detected (or degrading/persistent/high-issue trend in `--diff-log`) with `--exit-on-issue`.

### 3.2 `sys-io-guard` (`src/sysadmin_tools/sys_io_guard.py`)
- **Core mechanics:** resolves `--path` (default `$CARGO_TARGET_DIR` or cwd) via `/proc/mounts` (+ `/sys/block/dm-*/slaves`) to parent physical disk (`nvme2n1`, `sda`, `mmcblk0`) or `mnt-<sha1[:8]>` fallback; acquires advisory `fcntl.flock` in `$SYS_IO_GUARD_LOCK_DIR` / `$XDG_RUNTIME_DIR/sys-io-guard` / `/tmp/sys-io-guard-<uid>`; writes atomic `.meta` holder JSON (`pid`, `ppid`, `cmd`, `lock_key`, `target_path`, `started_at`); runs command directly without shell (`subprocess.run`) with `CARGO_BUILD_JOBS` cap and optional `os.nice`.
- **CLI flags:** `--path DIR`, `--lock-key NAME`, `--lock-dir DIR`, `--timeout SEC`, `--cargo-jobs N` (env `SYS_IO_GUARD_CARGO_JOBS`, default `min(8, max(2, cpus // 4))`, `0` disables), `--nice N` (0..19, default `10`), `--env KEY=VAL` (repeatable), `--status`, `--dry-run`, `--json`, `--verbose`, `-- COMMAND [ARGS...]`.
- **Exit codes:** child exit code on command execution; `0` for `--status`/`--dry-run`; `1` validation/runtime error; `2` argparse error; `4` lock busy/timeout.

### 3.3 `sys-mem-watch` (`src/sysadmin_tools/sys_mem_watch.py`)
- **Core telemetry:** two-snapshot `/proc` reads of `/proc/meminfo`, `/proc/vmstat` (`pswpin`, `pswpout`, `pgscan_*`, `pgsteal_*`, `oom_kill`), `/proc/pressure/memory`, and `/proc/<pid>/{stat,cmdline,status,oom_score,oom_score_adj,cgroup}`.
- **Ranked outputs:** `top_rss`, `top_growers` (`rss_delta_mib > 0`), `top_swap` (`swap_mib > 0`), `top_oom_risk` (`oom_score`, `rss_mib`).
- **Verdict statuses:** `OOM_IMMINENT`, `SWAP_THRASHING`, `RAPID_GROWTH_LEAK_RISK`, `HIGH_MEMORY_PRESSURE`, `HEALTHY` (`is_issue = status != "HEALTHY"`, never emits `"No immediate intervention needed."` when `is_issue=True`).
- **CLI flags:** `--interval` (0.2..60.0, env `SYS_MEM_WATCH_INTERVAL`), `--top` (1..100, env `SYS_MEM_WATCH_TOP`), `--json` / `--markdown`, `--watch [COUNT]`, `--append-log PATH`, `--exit-on-issue`, `--verbose`.
- **Exit codes:** `0` ok, `1` validation/runtime error, `2` argparse error, `3` issue detected with `--exit-on-issue`.

### 3.4 `sys-disk-hogs` (`src/sysadmin_tools/sys_disk_hogs.py`)
- **Core mechanics:** iterative stack-based `os.scandir()` post-order walker (`follow_symlinks=False`), hardlink deduplication by `(st_dev, st_ino)` when `st_nlink > 1`, single-filesystem default (`st_dev` check, opt-out via `--cross-mounts`), safe `os.statvfs()` gated by `/proc/mounts` fstype check and `--no-statvfs`.
- **Hog categories:** `rust-target`, `node-modules`, `python-venv`, `python-cache`, `build-artifacts`, `general-cache` (reclaimable, outer-hog rollup prevents nested double-counting), plus `vcs-metadata` (`.git`, tracked separately and excluded from `reclaimable_bytes`).
- **CLI flags:** `paths` (default `["."]`), `--top` (1..100, default `15`), `--depth` (1..10, default `2`), `--min-mib` (default `1.0`), `--min-file-mib` (default `10.0`), `--cross-mounts`, `--no-statvfs`, `--json` / `--markdown`, `--verbose`.
- **Exit codes:** `0` ok, `1` validation/missing-path/runtime error, `2` argparse error.

### 3.5 `sys-crash-triage` (`src/sysadmin_tools/sys_crash_triage.py`)
- **Core telemetry:** reads `/proc`, `/sys`, `/var/log/{kern.log,syslog}` (bounded 8 MiB tail read), `/var/lib/systemd/pstore`, `/var/crash`, and `/etc/default/kdump-tools` directly, plus optional timeout-guarded `journalctl` queries (disabled via `--no-journalctl`).
- **Capabilities:** boot timeline & downtime-gap analysis, AMD FCH `S5_RESET_STATUS` bit decoder, unclean-shutdown detection, categorized fatal/context signature scanner (`kernel_panic`, `mce_hardware`, `thermal`, `oom_killer`, `watchdog_lockup`, `gpu_fault`, `storage_fault`) with noise filtering, and `efi_pstore` / `kdump-tools` readiness audit.
- **Verdict statuses:** `KERNEL_PANIC_OR_OOPS`, `HARDWARE_MCE_OR_THERMAL`, `OOM_OR_LOCKUP_CRASH`, `GPU_OR_STORAGE_FAULT_RESET`, `UNCLEAN_HARD_RESET`, `INSUFFICIENT_DATA`, `CLEAN_REBOOT`.
- **CLI flags:** `--boot N` (<= 0), `--boots-history N` (1..50, default 5), `--tail-lines N` (10..500, default 80), `--no-journalctl`, `--journal-timeout SEC` (0.5..30.0, default 5.0), `--show-steps`, `--json` / `--markdown`, `--append-log PATH`, `--exit-on-issue`, `--verbose`.
- **Exit codes:** `0` ok, `1` validation/runtime error, `2` argparse error, `3` unclean crash/reset or fatal fault detected with `--exit-on-issue`.

### 3.6 `sys-net-watch` (`src/sysadmin_tools/sys_net_watch.py`)
- **Core telemetry:** pure `/proc/net/*` multi-snapshot reads of `/proc/net/dev` (per-interface RX/TX bytes, packets, errors, drops, FIFO/frame/carrier/collisions), `/proc/net/{tcp,tcp6}` (all 12 socket states, TX/RX hex queue bytes, `close_wait_delta`), `/proc/net/{udp,udp6}` (queue bytes, `socket_drops_total`), `/proc/net/snmp` (`Tcp` & `Udp` counter deltas and `retrans_rate_pct`), and `/proc/net/softnet_stat` (32-bit wrap-safe per-CPU `processed`, `dropped`, `time_squeeze`).
- **Key functions:** `parse_net_dev`, `read_net_dev`, `compute_net_dev_deltas`, `parse_proc_net_tcp`, `read_tcp_sockets`, `parse_proc_net_udp`, `read_udp_sockets`, `parse_net_snmp`, `read_net_snmp`, `compute_snmp_deltas`, `parse_softnet_stat`, `read_softnet_stat`, `compute_softnet_deltas`, `classify_network_verdict`, `watch_network`, `render_text`, `parse_iface_args`, `main`.
- **Verdict statuses (priority order):** `LINK_ERRORS`, `PACKET_DROPS`, `ELEVATED_RETRANSMITS`, `SOCKET_PRESSURE`, `HEALTHY` (`is_issue = status != "HEALTHY"`).
- **CLI flags:** `--interval` (0.1..60.0, env `SYS_NET_WATCH_INTERVAL`, default `1.0`), `--samples` (`>= 1`, default `2`), `--iface IFACE` (repeatable or comma-separated), `--top` (1..100, env `SYS_NET_WATCH_TOP`, default `8`), `--json` / `--markdown`, `--exit-on-issue`, `--verbose`, `--version`.
- **Exit codes:** `0` ok, `1` validation/missing-iface/runtime error, `2` argparse error, `3` issue detected with `--exit-on-issue`.

### 3.7 `sys-systemd-watch` (`src/sysadmin_tools/sys_systemd_watch.py`)
- **Core telemetry:** zero-subprocess reads of `/sys/fs/cgroup` v2 (`memory.{current,peak,high,max,events,pressure}`, `cpu.{stat,pressure}`, `io.{stat,pressure}`, `pids.{current,max}`, `cgroup.procs`), `/proc` (`uptime`, `<pid>/{stat,cgroup}`), and journal directories (`/run/log/journal`, `/var/log/journal`) for byte-growth bursts.
- **Key functions:** `discover_cgroup_units`, `snapshot_procs`, `read_unit_snapshot`, `snapshot_journal_dirs`, `compute_journal_delta`, `build_unit_metrics`, `classify_verdict`, `watch_systemd`, `render_text`, `matches_unit_filter`, `main`.
- **Verdict statuses (priority order):** `MEMORY_PRESSURE`, `CRASH_LOOP_SUSPECTED`, `IO_PRESSURE`, `CPU_THROTTLING`, `JOURNAL_BURST`, `HEALTHY` (`is_issue = status != "HEALTHY"`, leaf service/scope units preferred over parent slices for attribution; crash-loop heuristic scoped to `*.service` units).
- **CLI flags:** `--interval` (0.05..60.0, env `SYS_SYSTEMD_WATCH_INTERVAL`, default `1.0`), `--samples` (`1..60`, default `2`), `--top` (1..100, env `SYS_SYSTEMD_WATCH_TOP`, default `10`), `--unit PATTERN` (substring or glob), `--cgroup-root PATH` (default `/sys/fs/cgroup`), `--json` / `--markdown`, `--exit-on-issue`, `--verbose`, `--version`.
- **Exit codes:** `0` ok, `1` validation/missing-cgroup-root/runtime error, `2` argparse error, `3` issue detected with `--exit-on-issue`.

## 4. Known Pitfalls & Fixed Bugs

- **Sum all disks when `sat_disks` is empty:** rolling PSI (`io_some10`) can stay elevated seconds after disk util drops below 85%; fall back to `sat_disks or disks`.
- **High write latency + deep queue under <90% util:** an SSD draining `aqu_sz >= 16` at `w_await >= 250ms` while `util_pct` dips to ~82% must still classify as `THRASHING_LOW_EFFICIENCY`.
- **Actionable recommendations on all issue verdicts:** guaranteed across both `sys-investigate` and `sys-mem-watch`.
- **Kernel `comm` parsing:** always bracket with first `(` and last `)`.
- **Distinguish `write_bytes` from `wchar`:** avoids misattributing terminal/IPC chatter to disk saturation.
- **Avoid blocking `os.statvfs()` on non-local mounts:** skip `fuse*`, `nfs*`, `cifs`, `sshfs`, `autofs`, etc. automatically and offer `--no-statvfs`.

## 5. Verification Commands

```bash
cd ~/src/sysadmin/sysadmin-tools
./scripts/verify.sh
./scripts/install.sh --dry-run
```

Verified counts (2026-11-24): `syntax` PASS, `unit` PASS (143 tests), `smoke` PASS (23 tests) — **166 tests total**.

## 6. Future Expansion Candidates

All v0.5.0 roadmap items (`sys-systemd-watch`, `sys-net-watch`, `sys-investigate --diff-log`) are implemented and verified. Potential future extensions:
- `sys-net-watch --append-log PATH` and `sys-systemd-watch --append-log PATH` JSONL telemetry parity with `sys-investigate`.
- `sys-investigate --diff-log` support for multi-file globbing or baseline comparison across `sys-mem-watch` JSONL logs.
