[← Back to User's Guide](README.md) | [← Project README](../../README.md)

# Chapter 2: Live System Investigation (`sys-investigate`)

`sys-investigate` answers two urgent operational questions in seconds: **"What is driving CPU/load right now?"** and **"Is the workload making productive progress, or thrashing/wedged?"**

It reads `/proc` and `/sys` directly over a measured two-snapshot wall-clock window (`time.monotonic()`), with zero external CLI subprocesses (`top`, `iostat`, `vmstat`, `ps`, `lsblk`), so it remains fast and accurate even when storage is 99%+ saturated.

---

## 1. Why Load Average Alone Misleads: Runnable (`R`) vs. Blocked (`D`) Threads

On Linux, `/proc/loadavg` counts **both** runnable threads (`R` state) **and** threads in uninterruptible sleep (`D` state — typically waiting on disk I/O or dirty-page writeback).

A classic failure mode documented in [Engineering Notes](../NOTES.md):
- A 32-core workstation hit a load average of **101–151**, making it look like runaway CPU usage.
- Actual CPU compute was only `3.8–8.0% us` and `1.7–6.1% sy`, with only **2 runnable threads (`R=2`)**.
- Meanwhile, **`85.8–94.3% wa`** (I/O wait) and **`259–262` threads in `D` state** were stalled inside the kernel function `balance_dirty_pages` because three overlapping `verify.sh` runs had filled `13.9 GiB` of dirty pages onto a single NVMe SSD (`w_await=1,312 ms`, `aqu-sz=515.9`, throughput collapsed to `48 MB/s`).

`sys-investigate` inspects thread-level `/proc/<pid>/task/<tid>/{stat,wchan}` on the second snapshot so you immediately see whether high load is healthy parallel compute or a blocked I/O pileup.

---

## 2. Quick Command Reference

```bash
# Default 2-second two-snapshot investigation
sys-investigate

# Wider sampling window and more rows per table
sys-investigate --interval 3 --top 15

# Confirm whether a stall is transient or persistent (2 consecutive windows)
sys-investigate --progress-check --interval 2

# Drill down into a single PID and its entire descendant subtree
sys-investigate --diagnose-pid 12345 --interval 2

# Repeat continuously (or N times) like a structured monitor
sys-investigate --watch --interval 3
sys-investigate --watch 5 --interval 2

# Protect against hung mounts + record JSONL telemetry + scriptable exit code
sys-investigate --no-statvfs --append-log /var/tmp/sys-investigate.jsonl --exit-on-issue

# Emit deterministic JSON or GitHub-flavored Markdown
sys-investigate --json | jq '.verdict'
sys-investigate --markdown
```

### Complete CLI Options

| Flag | Range / Default | Environment Variable | Purpose |
|---|---|---|---|
| `--interval SEC` | `0.2..60.0` (default `2.0`) | `SYS_INVESTIGATE_INTERVAL` | Sampling window in seconds between snapshot 1 and snapshot 2. |
| `--top N` | `1..100` (default `10`) | `SYS_INVESTIGATE_TOP` | Maximum rows shown per table (`d_state_top`, `top_cpu`, `top_io`, `overlapping_workloads`, `top_descendants`). |
| `--json` | flag (mutually exclusive with `--markdown`) | — | Emit deterministic JSON (`sort_keys=True`) to stdout. |
| `--markdown` | flag (mutually exclusive with `--json`) | — | Emit GitHub-flavored Markdown tables/headers to stdout. |
| `--watch [COUNT]` | `>= 0` (`0` or omitted = infinite until Ctrl-C) | — | Repeat investigation `COUNT` times. |
| `--progress-check` | flag | — | Run two consecutive sampling windows and attach stall/progress comparison. |
| `--diagnose-pid PID` | integer `> 0` | — | Run a targeted two-snapshot diagnosis of `PID` and its descendant process tree. |
| `--no-statvfs` | flag | — | Skip `os.statvfs()` mount fill percentage checks for maximum hang immunity. |
| `--append-log PATH` | file path | — | Append one compact JSONL line per sample to `PATH` (creates parent dirs automatically). |
| `--exit-on-issue` | flag | — | Exit with code `3` when `verdict.is_issue` is `true` (last run in `--watch` mode). |
| `--verbose` | flag | — | Print effective configuration to `stderr`. |

**Exit codes:** `0` healthy / ok (or Ctrl-C), `1` validation / missing-PID / runtime error, `2` CLI usage error, `3` issue detected with `--exit-on-issue`.

---

## 3. Reading Every Report Section

### 3.1 Header & Verdict

```text
System Investigation @ 2026-11-23T12:00:00-05:00 (window=2.01s)
Bottleneck  : IO_WAIT_SATURATION
Productivity: THRASHING_LOW_EFFICIENCY
  - Making slow forward progress (12 started, 10 exited in window) but heavily throttled by I/O contention.
  - Dirty page backlog=12.84 GiB with 258 thread(s) stalled in balance_dirty_pages.
```

Look here first: `Bottleneck` names the constrained resource, `Productivity` states whether the workload is healthy, thrashing, wedged, or spinning, and the bullets provide the exact metrics driving that verdict.

### 3.2 CPU / Load / PSI / Memory / GPU

- **CPU:** Two-snapshot delta percentages (`us`, `sy`, `wa`, `id`, `ni`, `hi`, `si`, `st`). High `wa` (`>= 20%`) with low `us+sy` means CPUs are idle waiting on block I/O.
- **Load:** `load1, load5, load15 (N cores)` plus live thread counts `R=` (runnable) and `D=` (uninterruptible sleep), and process turnover `+started/-exited (total N)`. Process turnover helps prove whether builds are actually advancing or completely frozen.
- **PSI (Pressure Stall Information):** `some10` and `full10` (10-second averages) for `cpu`, `io`, and `memory` from `/proc/pressure/*`. `io(some10=98%, full10=88%)` means at least one task was stalled on I/O 98% of the time and *all* non-idle tasks were stalled 88% of the time.
- **Mem:** `avail/total GiB`, `Dirty` GiB/MiB (modified pages not yet flushed to disk), `Writeback` MiB (actively flushing), and `Swap` usage + `in`/`out` page deltas during the sample window. `Dirty >= 2.0 GiB` is a strong writeback saturation indicator.
- **GPU:** Shown when `/sys/class/drm/card*/device/gpu_busy_percent` is exposed (e.g., AMD GPUs).

### 3.3 Disk I/O

Displays whole physical disks (`nvme0n1`, `sda`, `vda`, `mmcblk0`, excluding partitions, `loop*`, `zram*`, `dm-*`, `ram*`):

| Column | Meaning | Watch Threshold |
|---|---|---|
| `UTIL%` | Percentage of sample window with at least one I/O in flight (`io_ms / dt`) | `>= 85%` saturated; `>= 90%` severe |
| `AQU` | Average queue depth (`weighted_ms / dt`) | `>= 8.0` saturated; `>= 16.0` deep queue |
| `W_MS` / `R_MS` | Mean write / read await latency in milliseconds | `W_MS >= 50ms` saturated; `>= 100ms`–`250ms` thrashing |
| `W_MB/s` / `R_MB/s` | Sequential/random throughput in MB/s (`sectors * 512 / 1e6 / dt`) | Low MB/s + high `UTIL%` + high `W_MS` = SLC cache exhaustion / random-write collapse |
| `MOUNTS / MODEL` | Backing mountpoints with fill % (unless `--no-statvfs` or non-local) + sysfs drive model | Watch near-full SSDs (`> 85%`), which amplify write stalls |

### 3.4 NVMe / Thermal Telemetry

Reads `/sys/class/nvme/nvme*/{model,state,hwmon*/temp*_*}` and `/sys/class/thermal/thermal_zone*/{type,temp}` directly from `/sys` (no `nvme-cli` or `smartctl` subprocesses):

| `STATUS` | Trigger Condition | Verdict Impact |
|---|---|---|
| `OK` | `state == "live"`, `alarm == False`, below warning thresholds | None |
| `WARM` | `temp_c >= temp_max_c` OR `temp_c >= 70.0°C` | Adds warning reason & cooling recommendation |
| `CRITICAL` | `alarm == True` OR `temp_c >= temp_crit_c` OR `state != "live"` | **Forces `is_issue=True`** and adds immediate thermal/SMART recommendation |

### 3.5 Blocked (`D`-State) Threads

Aggregates blocked threads by PID and kernel wait channel (`wchan`) from `/proc/<pid>/task/<tid>/wchan`:
- **`balance_dirty_pages`:** Kernel throttling writers because dirty RAM exceeded dirty-ratio limits while backing storage cannot flush fast enough.
- **`folio_wait_bit_common` / `wait_on_page_bit` / `io_schedule`:** Threads blocked waiting for page/block I/O completion.
- **`ext4_*` / `jbd2_*` / `xfs_*`:** Filesystem journal or metadata lock contention.

### 3.6 Top CPU Consumers & Top Process I/O

- **Top CPU Consumers:** Instantaneous `%CPU` computed from two-snapshot `/proc/<pid>/stat` tick deltas (`utime + stime`), **not** `ps %cpu` lifetime averages. Includes `RSS_M`, `W_MB/s`, `R_MB/s`, `ELAPSED`, `CGROUP`, and `CMD`.
- **Top Process I/O:** Shows **storage** `W_MB/s` and `R_MB/s` (`write_bytes`/`read_bytes` from `/proc/<pid>/io`) alongside `WCH_M/s` (`wchar` — total bytes passed to `write()` syscalls, which includes pipes, sockets, PTYs, and Wayland IPC). Keeping `W_MB/s` separate from `WCH_M/s` prevents mistaking noisy terminal/IPC traffic for actual disk writes.

### 3.7 Mount & Cgroup Attribution

To avoid scanning every PID on the system during an I/O storm, `sys-investigate` bounds attribution strictly to displayed top rows (plus `--diagnose-pid`):
- **Mounts (`MOUNTS` column on `D`-State & Top I/O tables):** Reads `/proc/<pid>/cwd` and up to 128 `/proc/<pid>/fd/*` symlinks via `os.readlink()` (kernel dentry memory only — zero target disk I/O), ignores `/proc`, `/sys`, `/dev`, `/run`, and ranks up to 3 active mounts.
- **Cgroups (`CGROUP` column on `D`-State, Top CPU, & Top I/O tables):** Parses `/proc/<pid>/cgroup` (cgroups v2 unified or v1 fallback) into clean labels:
  - `docker:<12-char-id>`
  - `podman:<12-char-id>`
  - `containerd:<12-char-id>`
  - `systemd:<unit.service|scope>`

### 3.8 Overlapping / Concurrent Workloads

Automatically detects duplicate script runners (`bash`, `sh`, `zsh` running the same script basename) and concurrent heavy tool families (`cargo`, `cargo-clippy`, `rustc`, `rust-lld`, `pytest`, `ninja`, `make`), complete with oldest elapsed time and parent ancestry chains (`pid:comm(elapsed) <- ...`).

---

## 4. Separating Transient Spikes from Confirmed Stalls (`--progress-check`)

A single 2-second snapshot can catch a brief compaction or burst flush. Pass `--progress-check` to run **two consecutive sampling windows** (`w1` and `w2`) and compare dirty-page delta, combined process turnover, persistent `D`-state PIDs, and persistent `>= 50%` CPU PIDs:

```bash
sys-investigate --progress-check --interval 2
```

| Progress Status | Meaning | Recommended Action |
|---|---|---|
| `CONFIRMED_STALL` | Both `w1` and `w2` were `WEDGED_OR_STALLED` or `SUSPECT_SPIN_LOOP` | Immediate intervention: inspect `persistent_d_pids` / `persistent_cpu_pids`, check `dmesg` / NVMe health, or kill/throttle culprit. |
| `PERSISTENT_THRASH` | Both `w1` and `w2` reported `is_issue=True` (e.g., sustained `THRASHING_LOW_EFFICIENCY`) | Serialize competing workloads with `sys-io-guard` or reduce concurrency. |
| `WORSENING` | `w1` had `is_issue=False`, `w2` flipped to `is_issue=True` | Monitor with `--watch` — pressure is actively building. |
| `RECOVERING` | `w1` had `is_issue=True`, `w2` recovered to `is_issue=False` | Transient spike clearing; watch `dirty_gib_delta` (negative = draining). |
| `STEADY_PROGRESS` | Active compute or I/O across both windows with `is_issue=False` | Healthy productive work; leave it running. |
| `IDLE` | Both healthy and `w2` is `NORMAL_OR_IDLE` | System is quiet. |

---

## 5. Single-Process & Subtree Drill-Down (`--diagnose-pid`)

Once a system scan surfaces a suspicious PID (or you want to check a specific build script, daemon, or container entrypoint), run:

```bash
sys-investigate --diagnose-pid 12345 --interval 2
```

`--diagnose-pid` inspects both the target PID and its entire descendant process tree (resolved via `ppid`), reporting:
- **Process Identity:** PID, PPID, state, elapsed time, cgroup, active mounts, full ancestry chain, command line.
- **Resource Metrics:** Target `%CPU`, `RSS`, `VmSwap`, `oom_score`, `R`/`W`/`WChar` MB/s.
- **Threads:** `Total`, `R`, `D`, `S` thread counts, aggregated `D`-state `wchan` histogram, and up to 20 individual blocked TIDs.
- **Descendant Subtree:** Total descendants, aggregated subtree `%CPU`, subtree `RSS`, subtree `R`/`W` MB/s, subtree `D`-threads, and top descendants table.

### `--diagnose-pid` Verdict Statuses

| Status | `is_issue` | Trigger Summary |
|---|---|---|
| `BLOCKED_ON_IO` | `True` | Target is in `D` state, has `d_threads > 0`, or `subtree_d_threads >= 2`. |
| `MEMORY_HEAVY` | `True` | `swap_mib >= 512`, OR (`oom_score >= 500` & `eff_rss >= 1024 MiB`), OR `eff_rss >= 25%` of RAM, OR `eff_rss >= 8192 MiB`. |
| `CPU_BOUND` | `True` only if spin-loop suspected | `eff_cpu >= 80%`. Flags `is_issue=True` if target has `>= 95%` CPU, `0.0 MB/s` I/O, elapsed `>= 5m`, and `0` descendants. |
| `IO_ACTIVE` | `False` | `eff_io_mbs >= 1.0` or `wchar_mbs >= 5.0` without `D`-state stalls. |
| `IDLE_OR_SLEEPING` | `False` | Low CPU, low I/O, no `D`-state blocks. |

---

## 6. Interpreting All System Bottleneck & Productivity Verdicts

### Bottleneck Classifications

| Bottleneck | Trigger Criteria |
|---|---|
| `MEMORY_SWAP_AND_IO_THRASH` | Both `mem_thrash` and `io_bound` active simultaneously. |
| `MEMORY_SWAP_THRASH` | `memory PSI some10 >= 20%`, OR swap `in+out >= 1000 pages`, OR `mem_avail < 1.0 GiB`. |
| `MIXED_CPU_AND_IO` | Both `io_bound` and `cpu_bound` active simultaneously. |
| `IO_WAIT_SATURATION` | `wa >= 20%`, OR `io PSI some10 >= 30%`, OR any disk saturated (`util >= 85%`, `w_await >= 50ms`, or `aqu_sz >= 8`), OR `d_threads >= 16`. |
| `CPU_COMPUTE_BOUND` | Active CPU (`us+sy+ni`) `>= 70%` OR `cpu PSI some10 >= 30%`. |
| `NORMAL_OR_IDLE` | None of the above thresholds tripped. |

### Productivity Verdicts

| Productivity | `is_issue` | Meaning |
|---|---|---|
| `HEALTHY_IDLE_OR_LIGHT` | `False` | Normal/idle system (unless an NVMe controller is `CRITICAL`). |
| `PRODUCTIVE_COMPUTE` | `False` | High CPU usage doing real work (multi-process, I/O active, or normal turnover). |
| `PRODUCTIVE_IO` | `False` | Active disk throughput without dirty-page backlog or severe write latency. |
| `THRASHING_LOW_EFFICIENCY` | `True` | Still making slow progress (`started > 0`, `exited > 0`, or `>= 5 MB/s`), but severely throttled (`balance_dirty_pages >= 4`, `Dirty >= 2.0 GiB`, `util >= 90% & w_await >= 100ms`, `w_await >= 250ms & aqu_sz >= 16`, or `mem_thrash`). |
| `WEDGED_OR_STALLED` | `True` | Thrashing conditions met **and** zero process turnover with `< 5 MB/s` disk throughput. |
| `SUSPECT_SPIN_LOOP` | `True` | Long-running (`>= 5m`) `>= 95%` CPU consumer(s) with `0.0 MB/s` I/O and zero process turnover. |

> **Design Guarantee:** Whenever `is_issue=True` (including `CRITICAL` NVMe thermal/state alerts), `sys-investigate` **never** prints `"No immediate intervention needed."` — it always emits at least one concrete remediation step.

---

## 7. Continuous Monitoring & Hang-Proofing

- **`--no-statvfs`:** Non-local/FUSE/network/automount filesystems (`nfs*`, `cifs`, `sshfs`, `fuse.*`, `autofs`, etc.) are skipped automatically, but if even a local block mount is completely wedged, pass `--no-statvfs` to guarantee zero `os.statvfs()` calls.
- **`--watch [COUNT]` + `--append-log PATH`:** Stream periodic snapshots to a compact JSONL file while watching live output:

```bash
sys-investigate --watch 30 --interval 5 --no-statvfs --append-log /var/tmp/sys-investigate.jsonl
```

---

**Navigation:** [← Previous: Chapter 1 — Getting Started](01-getting-started.md) | [Next: Chapter 3 — Preventing I/O Storms →](03-preventing-io-storms.md)
