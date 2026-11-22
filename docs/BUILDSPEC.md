# As-Built Buildspec — sysadmin-tools (`sys-investigate` v0.1.0)

Derived directly from live source (`src/sysadmin_tools/sys_investigate.py`, 2026-11-21).

## 1. Purpose & Non-Negotiable Design Properties

`sysadmin-tools` provides fast, deterministic, zero-dependency Linux diagnostic CLIs that answer *"what is driving CPU/load right now, and is it productive?"*

**Non-negotiable invariants (a rebuild that drops these is invalid):**
1. **Zero third-party dependencies:** Python 3.10+ standard library only.
2. **Zero external CLI dependencies on the sampling path:** all telemetry comes from `/proc` and `/sys` directly (no `top`, `iostat`, `vmstat`, `ps`, `lsblk` subprocesses).
3. **Resilient under severe storage stalls:** installed binary is a standalone file on root filesystem (`~/.local/bin/sys-investigate`), never a symlink into a secondary data mount.
4. **Two-snapshot delta accuracy:** CPU %, disk utilization/latency/throughput, swap paging, per-process `%CPU`, per-process disk MB/s, and process turnover (`+started/-exited`) are all computed strictly over the measured `[t0, t1]` wall-clock interval (`time.monotonic()`).
5. **Graceful permission handling:** unreadable `/proc/<pid>/io` or vanished short-lived PIDs are skipped cleanly without tracebacks.
6. **Script-first UX:** data to `stdout`, diagnostics/verbose config to `stderr`; deterministic JSON (`sort_keys=True`); documented exit codes (`0`, `1`, `2`, `3`).

## 2. Stack & Target Environment

- **OS:** Linux (kernel with `/proc/stat`, `/proc/loadavg`, `/proc/meminfo`, `/proc/vmstat`, `/proc/diskstats`, `/proc/mounts`, `/proc/<pid>/*`, optional `/proc/pressure/*` PSI and `/sys/class/drm/card*/device/gpu_busy_percent`).
- **Runtime:** Python `>=3.10` (stdlib only).
- **Packaging:** `pyproject.toml` (`setuptools`), console script `sys-investigate = sysadmin_tools.sys_investigate:main`, plus direct copy installer `scripts/install.sh`.

## 3. Module Map & Signatures

### `src/sysadmin_tools/sys_investigate.py`

| Symbol | Signature / Role |
|---|---|
| `read_cpu_times()` | `() -> list[int]` — parses aggregate `cpu ` jiffies from `/proc/stat` |
| `compute_cpu_pct(t0, t1)` | `(list[int], list[int]) -> dict[str, float]` — returns `us, ni, sy, id, wa, hi, si, st` % |
| `read_loadavg()` | `() -> dict[str, Any]` — `load1, load5, load15, runnable_kernel, entities_total, cpus` |
| `read_psi()` | `() -> dict` — parses `/proc/pressure/{cpu,io,memory}` `some`/`full` avg10/60/300 |
| `read_meminfo()` | `() -> dict[str, float]` — total/avail/free GiB, `dirty_gib`/`dirty_mib`, `writeback_mib`, swap |
| `read_vmstat_swap()` | `() -> tuple[int, int]` — `(pswpin, pswpout)` cumulative pages |
| `is_whole_physical_disk(name)` | `(str) -> bool` — filters out loop/ram/zram/dm/sr/fd and partitions (`nvme*p*`, `sd*[0-9]`, `mmcblk*p*`) |
| `read_diskstats()` | `() -> dict[str, dict[str, int]]` — parses `/proc/diskstats` for whole physical disks |
| `map_disks_to_mounts()` | `() -> dict[str, list[dict]]` — maps parent disk -> mountpoints + fstype + `statvfs` `used_pct` |
| `compute_disk_deltas(d0, d1, dt_sec, mount_map)` | Computes `r_iops, w_iops, r_mbs, w_mbs, r_await_ms, w_await_ms, aqu_sz, in_flight, util_pct`, sorted by `(util_pct, aqu_sz, throughput)` desc |
| `read_gpu_busy()` | `() -> list[dict]` — reads AMD DRM `/sys/class/drm/card*/device/gpu_busy_percent` |
| `parse_proc_stat(stat_txt)` | Handles arbitrary spaces/parens in `comm` via `find("(")` / `rfind(")")`; returns `(comm, state, ppid, utime, stime, starttime)` |
| `snapshot_procs(inspect_threads=True)` | Scans `/proc/<pid>/{stat,cmdline,statm,io}` and (when `inspect_threads=True`) `/proc/<pid>/task/<tid>/{stat,wchan}` for `D` and `R` thread counts |
| `build_ancestry(pid, snaps, uptime, max_depth=8)` | Formats `pid:comm(elapsed) <- ...` up to `ppid <= 1` |
| `detect_overlapping_workloads(snaps, uptime)` | Groups concurrent shell scripts (`script:<basename>`, threshold `>=2`) and build/test tools (`tool:cargo/pytest/make/ninja` `>=2`; `rustc/rust-lld` `>=4`) with ancestry trees |
| `classify_verdict(...)` | Deterministic rule engine returning `{bottleneck, productivity, is_issue, reasons, recommendations}` |
| `investigate(interval=2.0, top_n=10)` | Orchestrates `[t0 -> sleep(interval) -> t1]` capture and returns full structured report dict |
| `render_text(rep, markdown=False)` | Formats human-readable ASCII or GitHub Markdown report |
| `main(argv=None)` | Parses CLI flags (`--interval`, `--top`, `--json`, `--markdown`, `--watch`, `--exit-on-issue`, `--verbose`), validates bounds, enforces exit codes |

### Verdict Classification Rules (`classify_verdict`)

- **Trigger thresholds:**
  - `sat_disks`: `util_pct >= 85` OR `w_await_ms >= 50` OR `aqu_sz >= 8`
  - `io_bound`: `wa >= 20%` OR `io_some10 >= 30%` OR `bool(sat_disks)` OR `d_state_total >= 16`
  - `mem_thrash`: `mem_some10 >= 20%` OR `swap_pages_delta >= 1000` OR `mem_avail_gib < 1.0`
  - `cpu_bound`: `us + sy + ni >= 70%` OR `cpu_some10 >= 30%`
- **Bottleneck states:** `MEMORY_SWAP_AND_IO_THRASH`, `MEMORY_SWAP_THRASH`, `MIXED_CPU_AND_IO`, `IO_WAIT_SATURATION`, `CPU_COMPUTE_BOUND`, `NORMAL_OR_IDLE`.
- **Productivity states:**
  - `WEDGED_OR_STALLED` (`is_issue=True`): thrashing (`balance_dirty_pages >= 4` OR `dirty_gib >= 2.0` OR `util >= 90% & w_await >= 100ms` OR `mem_thrash`) AND zero turnover AND `< 5 MB/s` throughput.
  - `THRASHING_LOW_EFFICIENCY` (`is_issue=True`): thrashing with slow forward progress.
  - `SUSPECT_SPIN_LOOP` (`is_issue=True`): CPU-bound with long-running (`>= 300s`) `>= 95%` CPU consumer doing `0 MB/s` I/O and zero process turnover.
  - `PRODUCTIVE_IO` / `PRODUCTIVE_COMPUTE` / `HEALTHY_IDLE_OR_LIGHT` (`is_issue=False`).

### CLI Contract & Exit Codes

- Precedence: CLI flag > environment variable (`SYS_INVESTIGATE_INTERVAL`, `SYS_INVESTIGATE_TOP`) > built-in default (`2.0`, `10`).
- Validation bounds: `0.2 <= interval <= 60.0`, `1 <= top <= 100`, `watch >= 0`.
- Exit codes: `0` ok, `1` validation/runtime error (single-line stderr, no traceback), `2` argparse usage error, `3` issue detected when `--exit-on-issue` is passed.

## 4. Known Pitfalls & Fixed Bugs

- **Sum all disks when `sat_disks` is empty:** rolling PSI (`io_some10`) can stay elevated seconds after disk util drops below 85%. `total_disk_mbs` must fall back to `disks` (`sat_disks or disks`) so throughput is never falsely reported as `0.0 MB/s`.
- **Kernel thread/comm parsing:** always bracket `comm` with first `(` and last `)`.
- **Distinguish `write_bytes` from `wchar`:** GUI terminals/IPC generate hundreds of MB/s of `wchar` without touching disk.

## 5. Verification Commands

```bash
cd ~/src/sysadmin/sysadmin-tools
./scripts/verify.sh
./scripts/install.sh --dry-run
sys-investigate --interval 1 --top 5 --verbose
```

## 6. Expansion Roadmap (Candidates for Next Session)

1. **Mount-safety hardening:** skip network/FUSE filesystems in `map_disks_to_mounts()` before `os.statvfs()` so a hung NFS/SSHFS mount cannot block `sys-investigate`.
2. **Process-to-mount attribution:** inspect `/proc/<pid>/cwd` and open FDs (`/proc/<pid>/fd/*`) for top I/O writers and `D`-state processes to prove *which* mountpoint each offender is hitting.
3. **Multi-window progress check (`--progress-check`):** compare 2 consecutive windows to confirm whether long-running high-CPU or `D`-state tasks are actually advancing or permanently wedged.
4. **JSONL recorder + diff (`--append-log PATH`):** lightweight append-only telemetry log for cron/systemd timer monitoring.
5. **Companion tools in `sysadmin-tools`:**
   - `sys-mem-watch`: RSS/swap growth & OOM-risk investigator.
   - `sys-io-guard`: lock/serialize wrapper for heavy local builds (`flock` per target SSD + `CARGO_BUILD_JOBS` cap).
   - `sys-disk-hogs`: fast workspace/target/cache disk usage analyzer.
