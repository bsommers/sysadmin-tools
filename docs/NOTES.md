# Engineering Notes & Incident Journal

## 2026-11-21 — Origin Incident: "What is driving up CPU? And is it productive?"

### Live Incident Summary
- **Observed symptom:** Load average spiked to **101–151** on a 32-core host; desktop and shell commands became sluggish (writing 3 tiny files to `/datanapps` stalled for ~20 minutes; a shell diagnostic pipeline stalled ~9m 45s).
- **Actual root cause (NOT CPU compute):**
  - CPU compute was tiny: `3.8–8.0% us`, `1.7–6.1% sy`.
  - CPU I/O wait was **`85.8–94.3% wa`**.
  - PSI confirmed: `cpu some10=0.39%` vs **`io some10=98.34%` / `full10=88.33%`**.
  - `vmstat` showed `r=2`, **`b=259–262`** (Linux load average counts `D`-state uninterruptible I/O sleep threads, explaining load 150 with only 2 runnable threads).
  - `nvme2n1` (`/datanapps`, SPCC M.2 PCIe SSD, 34% used) was pinned at **`99.2% util`**, queue depth **`aqu-sz=515.9`**, write latency **`w_await=1,312 ms`**, throughput collapsed to **`48 MB/s`** (SLC write cache exhausted under concurrent random/linker writes).
  - `/proc/meminfo` showed **`Dirty: 12.8–13.9 GiB`** waiting to flush; ~260 `rust-lld` threads were blocked inside kernel `balance_dirty_pages`.
- **Workload attribution:**
  - 3 overlapping `bash scripts/verify.sh` runs spawned under a single `claude` session (`herdr` -> `bash` -> `claude`) running concurrently against `/datanapps/cargo-target/...` and `/datanapps/worktrees/bilterm-chords/target/...`.
  - Collectively spawning ~33 `rustc` + 28+ concurrent `rust-lld` linkers writing large debug test binaries simultaneously.
  - Secondary workload `sdxl_gen.py` (`pawedracer`, AMD ROCm) was only ~57% of 1 core but also stalled in `folio_wait_bit_common` waiting on `/datanapps`.
- **Productivity verdict:** Technically progressing (linker PIDs and test binaries rotated slowly), but severely thrashing (`1h 35m+` for a single `verify.sh` that should finish in minutes when serialized).

### Design Decisions & Why
1. **Pure `/proc` + `/sys` reads (no `top`, `iostat`, `vmstat`, `ps` subprocesses):**
   - Shelling out during an I/O storm can block on binary loading, dynamic libs, or stdout/tmp I/O, and introduces format drift across distros/procps versions.
   - Reading virtual files in `/proc` and `/sys` touches zero disk blocks.
2. **Thread-level `D`-state + `wchan` aggregation (`/proc/<pid>/task/<tid>/{stat,wchan}`):**
   - Process-level `/proc/<pid>/stat` hides the fact that a single `rust-lld` process has 15–16 threads blocked in `balance_dirty_pages`. Scanning `/proc/<pid>/task/*` on the second snapshot exposes the exact kernel bottleneck immediately.
   - Only scanned on the second snapshot (`inspect_threads=True`) to keep the first snapshot fast.
3. **Standalone copy in `~/.local/bin/sys-investigate` (root NVMe) instead of symlink to `/datanapps`:**
   - Avoids circular dependency on a stalled data disk when you need the tool most.
4. **`os.statvfs()` caveat (hardened in v0.2.0):**
   - `map_disks_to_mounts()` skips non-local/FUSE/network/automount filesystems (`is_safe_statvfs_fstype`) before calling `os.statvfs(mnt)` and supports `--no-statvfs` for zero-`statvfs` operation during severe storage hangs.
5. **Bounded `/proc/<pid>/{cwd,fd}` mount attribution (v0.2.0):**
   - Only runs on the selected `top_io` + `d_state_top` PIDs (cached per PID, capped at `max_fds=128`) using `os.readlink()`, which reads kernel dentry paths from memory without touching target filesystems.
6. **Self-contained companion CLIs (v0.3.0):**
   - Each tool (`sys-investigate`, `sys-io-guard`, `sys-mem-watch`, `sys-disk-hogs`) stays 100% standalone stdlib-only with zero intra-package imports so `scripts/install.sh` copies directly into `~/.local/bin` on root NVMe.
   - `sys-io-guard` places lockfiles and holder `.meta` files under `$XDG_RUNTIME_DIR/sys-io-guard` (tmpfs) or `/tmp/sys-io-guard-<uid>`, never on the guarded secondary SSD, and resolves `/dev/mapper/*` / `dm-*` via `/sys/block/dm-*/slaves` without shelling out.
   - `sys-investigate` v0.3.0 reads NVMe controller temperatures/thresholds directly from `/sys/class/nvme/nvme*/hwmon*/temp*_*` (millidegrees C -> °C) and bounds `/proc/<pid>/cgroup` parsing strictly to displayed top rows + `--diagnose-pid`.

### Dead Ends & Gotchas Observed
- `ps %cpu` reports **lifetime average** CPU, not instantaneous CPU — misleading for long-lived processes or newly spawned bursts. Always use two-snapshot `/proc/<pid>/stat` tick deltas (`utime + stime`).
- `/proc/<pid>/io` raises `PermissionError` for processes owned by other users (e.g. `root` VMs/daemons) when run unprivileged; must fail open (`None`) and clearly note "readable `/proc/*/io`" rather than crashing or reporting false zeros.
- `/proc/<pid>/stat` `comm` field can contain spaces and parentheses (e.g. `(kworker/u129:15+flush-259:3)`), so parsing MUST split on `find("(")` and `rfind(")")`, never naive `.split()`.
- `wchar` in `/proc/<pid>/io` includes pipes, sockets, PTYs, and Wayland IPC (e.g., `bilterm --gui --tron` showed `400–640 MB/s` `wchar` with `0.00 MB/s` `write_bytes`). Keep `write_bytes` (actual storage writeback) distinct from `wchar` so terminal/IPC chatter isn't misdiagnosed as disk saturation.
- `nvme0n1` vs `nvme0n1p1`: filtering whole physical disks requires checking `"p" not in name.split("n", 2)[-1]` for NVMe and `not name[-1].isdigit()` for `sd*`/`vd*`.
- Live check during v0.2.0 caught two classifier edge cases on `nvme2n1`: (1) single-process `THRASHING_LOW_EFFICIENCY` (`Dirty=13.98 GiB`, `w_await=1862ms`) with no overlapping scripts previously fell through to `"No immediate intervention needed."`; (2) `util=82.4%` with `aqu_sz=202.71` and `w_await=1388.5ms` (`Dirty=1.65 GiB`) previously missed the `util >= 90%` thrashing check and reported `PRODUCTIVE_IO`. Both are now fixed and locked with unit tests.
