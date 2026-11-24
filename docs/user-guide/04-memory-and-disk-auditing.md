[← Back to User's Guide](README.md) | [← Project README](../../README.md)

# Chapter 4: Memory & Disk Auditing (`sys-mem-watch` & `sys-disk-hogs`)

This chapter covers two focused auditing CLIs:
- **`sys-mem-watch`** for live memory consumption, RSS leak growth rates, swap thrashing, kernel page reclaim, and OOM-killer risk.
- **`sys-disk-hogs`** for fast, hardlink-aware directory/file usage auditing and safe identification of reclaimable build/cache directories.

---

## Part A: Memory Auditing with `sys-mem-watch`

`sys-mem-watch` performs two `/proc` snapshots separated by `--interval` seconds (zero subprocesses) to measure **rates of change** — catching active leaks and swap storms before the kernel OOM killer strikes.

### A.1 Quick Commands & CLI Options

```bash
# Standard 2-second memory investigation
sys-mem-watch

# Longer window with more rows to catch slower leaks
sys-mem-watch --interval 5 --top 15

# Continuous monitoring with JSONL logging
sys-mem-watch --watch 10 --interval 3 --append-log /var/tmp/sys-mem-watch.jsonl

# Alerting / automation mode (exits 3 if status != HEALTHY)
sys-mem-watch --exit-on-issue --json | jq '.verdict'
sys-mem-watch --markdown
```

| Flag | Range / Default | Env Variable | Purpose |
|---|---|---|---|
| `--interval SEC` | `0.2..60.0` (default `2.0`) | `SYS_MEM_WATCH_INTERVAL` | Sampling window in seconds. |
| `--top N` | `1..100` (default `10`) | `SYS_MEM_WATCH_TOP` | Max rows per ranked table (`top_rss`, `top_growers`, `top_swap`, `top_oom_risk`). |
| `--json` | mutually exclusive with `--markdown` | — | Emit deterministic JSON (`sort_keys=True`). |
| `--markdown` | mutually exclusive with `--json` | — | Emit Markdown report. |
| `--watch [COUNT]` | `>= 0` (`0` or omitted = infinite) | — | Repeat sampling `COUNT` times. |
| `--append-log PATH` | file path | — | Append compact JSONL record per sample. |
| `--exit-on-issue` | flag | — | Exit `3` when verdict `status != "HEALTHY"`. |
| `--verbose` | flag | — | Print effective config to `stderr`. |

**Exit codes:** `0` healthy/ok, `1` validation/runtime error, `2` usage error, `3` non-`HEALTHY` verdict with `--exit-on-issue`.

### A.2 Understanding the Memory Metrics

#### Per-Process Fields (`/proc/<pid>/status`, `/proc/<pid>/oom_score*`)

| Field / Column | Kernel Source | What It Tells You |
|---|---|---|
| `RSS_M` (`rss_mib`) | `VmRSS` | Total resident RAM currently mapped by the process (`RssAnon + RssFile + RssShmem`). |
| `dRSS_M` / `RATE_M/s` | Δ `VmRSS` over `[t0, t1]` | Net RAM growth during the sample window and growth velocity (`MiB/s`). Only computed when PID `starttime` matches between snapshots (immune to PID reuse). |
| `ANON_M` (`anon_mib`) | `RssAnon` | Private heap/stack anonymous memory. Steady growth in `ANON_M` is the classic signature of a memory leak. |
| `FILE_M` (`file_mib`) | `RssFile` | File-backed mappings (shared libraries, `mmap`'d datasets/models). Usually reclaimable by the kernel without swap. |
| `shmem_mib` (JSON) | `RssShmem` | Shared memory / `tmpfs` mappings (e.g., `/dev/shm`, IPC buffers, browser/GPU shared surfaces). |
| `SWAP_M` / `dSWP_M` | `VmSwap` & Δ `VmSwap` | Process memory currently pushed to swap space, and whether swap usage grew during the sample. |
| `OOM` (`oom_score`) | `/proc/<pid>/oom_score` | Kernel's live OOM badness score (`0..1000+`); highest score is killed first under OOM. |
| `ADJ` (`oom_score_adj`) | `/proc/<pid>/oom_score_adj` | Configured adjustment (`-1000` = unkillable, `+1000` = kill first). |

#### System Reclaim & Pressure Counters (`/proc/vmstat`, `/proc/pressure/memory`)

- **`pgscan_kswapd` / `pgsteal_kswapd`:** Background page scanning/reclaim by `kswapd`. Moderate activity is normal when RAM fills with page cache.
- **`pgscan_direct` / `pgsteal_direct`:** **Synchronous direct reclaim** — allocating processes are stalled in the kernel while reclaiming pages themselves. High `pgscan_direct` means the system is falling behind memory demand.
- **`swap_in_pages` (`pswpin`) / `swap_out_pages` (`pswpout`):** Active paging between RAM and swap during the sample window.
- **`oom_kill_delta` (`oom_kill`):** Number of kernel OOM kills triggered *during* the sample window.
- **Memory PSI (`some` / `full`):** Percentage of time tasks spent stalled on memory reclaim/refaults over 10s, 60s, and 300s averages.

### A.3 The 5 Verdict Statuses

`sys-mem-watch` evaluates conditions in strict priority order:

| Priority | Status | `is_issue` | Exact Trigger Conditions |
|---|---|---|---|
| 1 | `OOM_IMMINENT` | `True` | `oom_kill_delta > 0`, OR `mem_avail < 0.5 GiB`, OR (`mem_avail_pct < 5%` & `mem_avail < 1.0 GiB`), OR (`psi_full10 >= 25%` & `mem_avail_pct < 10%`), OR (`mem_avail_pct < 8%` & `swap_used_pct >= 95%` & (`pgscan_direct > 0` or `psi_some10 >= 10%`)). |
| 2 | `SWAP_THRASHING` | `True` | `swap_in + swap_out >= 1000 pages`, OR `>= 500 pages/s`, OR (`>= 200 pages` with `psi_some10 >= 10%`, `psi_full10 >= 5%`, or `pgscan_direct >= 500`). |
| 3 | `RAPID_GROWTH_LEAK_RISK` | `True` | Any single process growing at `>= 50 MiB/s` with `>= 20 MiB` delta, OR combined top growers `>= 100 MiB/s` with `>= 50 MiB` total delta. |
| 4 | `HIGH_MEMORY_PRESSURE` | `True` | `psi_some10 >= 10%`, OR `psi_full10 >= 5%`, OR `pgscan_direct >= 1000`, OR (`pgscan_kswapd >= 10000` & `mem_avail_pct < 20%`), OR `mem_avail_pct < 15%`, OR `mem_avail < 2.0 GiB`. |
| 5 | `HEALTHY` | `False` | None of the above thresholds tripped. |

### A.4 Tracking a Suspected Memory Leak

To catch a slow leak (e.g., 5–15 MiB/s) that might not trip `RAPID_GROWTH_LEAK_RISK` in a 2-second window, widen `--interval` and watch across multiple cycles:

```bash
sys-mem-watch --watch 6 --interval 10 --top 10 --append-log /var/tmp/leak-trace.jsonl
```

Inspect the **Top Memory Growers (RSS Delta > 0)** table: confirm whether `dRSS_M` is consistently positive across windows and whether `ANON_M` tracks `RSS_M` upward. Note the `CGROUP` column so you know which systemd unit or container needs a `MemoryMax=` cap or restart.

---

## Part B: Workspace & Disk Usage Auditing with `sys-disk-hogs`

`sys-disk-hogs` walks one or more root directories iteratively using `os.scandir()` (zero `du`/`find` subprocesses, never hits Python recursion limits on deep trees) to pinpoint large subdirectories, individual files, and **regenerable build/cache hogs**.

### B.1 Quick Commands & CLI Options

```bash
# Audit current directory (default: depth <= 2, top 15, single filesystem)
sys-disk-hogs

# Audit multiple roots at once (e.g. home + secondary data SSD)
sys-disk-hogs ~ /datanapps --top 20 --depth 3

# Show even tiny directories/files in JSON
sys-disk-hogs . --min-mib 0 --min-file-mib 1 --json

# Scan root filesystem safely without crossing into /datanapps or calling statvfs
sys-disk-hogs / --no-statvfs --markdown

# Explicitly cross into child mountpoints
sys-disk-hogs /mnt/data --cross-mounts --verbose
```

| Flag | Range / Default | Purpose |
|---|---|---|
| `paths` (positional) | default `["."]` | One or more root directories (or files) to analyze. |
| `--top N` | `1..100` (default `15`) | Max rows in `top_dirs`, `reclaimable_hogs`, `vcs_metadata`, and `largest_files`. |
| `--depth N` | `1..10` (default `2`) | Maximum subdirectory depth included in `top_dirs` (full tree is always scanned for accurate rollups and hog detection). |
| `--min-mib FLOAT` | `>= 0` (default `1.0`) | Minimum size in MiB for `top_dirs` rows. |
| `--min-file-mib FLOAT` | `>= 0` (default `10.0`) | Minimum file size in MiB for `largest_files` rows. |
| `--one-file-system` | **default: enabled** (mutually exclusive with `--cross-mounts`) | Stay on each root's filesystem by comparing `st_dev`. |
| `--cross-mounts` | mutually exclusive with `--one-file-system` | Allow traversal across mountpoint boundaries. |
| `--no-statvfs` | flag | Skip `os.statvfs()` filesystem fill-percentage checks. |
| `--json` | mutually exclusive with `--markdown` | Emit deterministic JSON (`sort_keys=True`). |
| `--markdown` | mutually exclusive with `--json` | Emit Markdown report. |
| `--verbose` | flag | Print effective configuration to `stderr`. |

**Exit codes:** `0` ok, `1` validation error or missing/unreadable root path, `2` usage error.

### B.2 Key Accuracy & Safety Guarantees

1. **Single-Filesystem Default (`--one-file-system`):** Scanning `/` or `~` will not accidentally traverse into mounted NAS shares, `/proc`, `/sys`, FUSE mounts, or secondary SSDs unless you explicitly pass `--cross-mounts`.
2. **Hardlink Deduplication:** Regular files with `st_nlink > 1` are tracked by `(st_dev, st_ino)` across the entire scan so hardlinked caches (e.g., pnpm stores, Nix stores, deduplicated backups) are counted once and reported in `deduped_hardlinks`. Symlinks are never followed (`follow_symlinks=False`).
3. **Mount-Safe `statvfs()`:** Non-local/FUSE/network/automount filesystems (`nfs*`, `cifs`, `sshfs`, `fuse.*`, `autofs`, etc.) are automatically skipped before calling `os.statvfs()`, or disable all `statvfs()` calls with `--no-statvfs`.
4. **Outer-Hog Rollup (Zero Double-Counting):** Once a directory is classified as a hog (e.g., `my-app/target/`), nested build/cache subdirectories inside it are rolled into that outer hog rather than counted twice in `reclaimable_bytes`.

### B.3 Reclaimable Hog Categories vs. Non-Reclaimable `.git`

| Category | Reclaimable? | Matched Directory Names / Rules |
|---|---|---|
| `rust-target` | **Yes** | `cargo-target`, or `target` when parent contains `Cargo.toml` OR directory contains `debug`, `release`, `deps`, or `.rustc_info.json`. |
| `node-modules` | **Yes** | `node_modules`. |
| `python-venv` | **Yes** | `.venv`, `venv`, `.tox`, `.nox`. |
| `python-cache` | **Yes** | `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`. |
| `build-artifacts` | **Yes** | `build`, `dist`, `.next`, `.nuxt`, `.turbo`, `.parcel-cache`, `.gradle`, `CMakeFiles`. |
| `general-cache` | **Yes** | `.cache`, `.cargo`, `.npm`, `.pnpm-store`, `.yarn`, `go-build`. |
| `vcs-metadata` | **No** | `.git` — tracked in its own section and **strictly excluded** from `reclaimable_bytes` so you never accidentally delete commit history. |

### B.4 Safe Cleanup Workflow

Follow this three-step sequence when freeing disk space:

1. **Audit & inspect reclaimable candidates:**
   ```bash
   sys-disk-hogs ~/src /datanapps --top 25 --depth 2
   ```
2. **Verify no active builds are writing to those target directories:**
   ```bash
   sys-io-guard --status
   ```
3. **Clean selectively using native tools first, or targeted removals for stale directories:**
   ```bash
   # Rust projects: prefer cargo clean (or remove inactive target/ dirs)
   cargo clean --manifest-path ~/src/old-project/Cargo.toml

   # Python caches / stale virtualenvs / node_modules in inactive repos
   rm -rf ~/src/old-project/.venv ~/src/old-project/node_modules
   ```
4. **Re-run `sys-disk-hogs` to confirm reclaimed headroom** (keeping SSDs under ~80–85% full helps preserve NVMe SLC write-cache performance).

---

**Navigation:** [← Previous: Chapter 3 — Preventing I/O Storms](03-preventing-io-storms.md) | [Next: Chapter 5 — Post-Crash Forensics →](05-post-crash-forensics.md)
