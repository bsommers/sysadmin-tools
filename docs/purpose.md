# Purpose, Origin Incident & Trust Boundaries

[← Back to Documentation Hub](README.md)

## 1. Problem Statement

When a Linux workstation or build host stalls under heavy load, standard diagnostic habits often fail at the exact moment they are needed:

- **Load average conflates CPU compute with uninterruptible I/O sleep (`D`-state):** A 32-core host can report load 150 while actual CPU compute is below 10%.
- **Point-in-time tools mislead:** `ps %cpu` reports process lifetime average rather than instantaneous CPU usage; single-snapshot commands cannot distinguish active throughput from wedged writeback queues.
- **Shelling out during a storage storm stalls the diagnostics themselves:** Spawning pipelines of `top`, `iostat`, `vmstat`, `ps`, `df`, and `du` touches disk blocks, invokes blocking `statvfs()` calls on hung mounts, and breaks across procps/sysstat version drift.
- **Symlinked developer tools create circular mount dependencies:** If `~/.local/bin/my-tool` symlinks to a repository on a secondary data SSD (`/datanapps`), diagnosing a stalled `/datanapps` hangs before Python even opens the script.
- **Post-crash forensics are scattered and noisy:** After an unexpected reboot, evidence spans `journalctl --list-boots`, AMD FCH reset registers, ext4 orphan-inode recovery logs, NUL-byte gaps in `/var/log/syslog`, `/var/lib/systemd/pstore`, `/var/crash`, EDAC counters, and `/etc/default/kdump-tools` — buried beneath high-volume `audit` and Bluetooth log chatter.

`sysadmin-tools` solves these failure modes with five self-contained, zero-dependency Python 3.10+ CLIs that read Linux kernel virtual filesystems directly, separate root-NVMe binaries and tmpfs locks from secondary storage, and emit deterministic, actionable verdicts.

## 2. Empirical Origin Incident (2026-11-21)

`sysadmin-tools` was born out of a live production workstation stall where the operator asked: *"What is driving up CPU? And is it productive?"*

### Observed Telemetry

| Signal | Measured Value | Diagnostic Meaning |
| --- | --- | --- |
| Host & Load Average | 32-core host; load **101–151** | Extreme scheduler queue pressure; writing 3 tiny files to `/datanapps` stalled ~20 minutes; a shell diagnostic pipeline stalled 9m 45s |
| CPU Breakdown | **`3.8–8.0% us`**, `1.7–6.1% sy`, **`85.8–94.3% wa`** | Almost zero CPU compute; the host was waiting on block I/O |
| Pressure Stall Information (PSI) | `cpu some10=0.39%` vs **`io some10=98.34%` / `full10=88.33%`** | Confirmed storage stall, not CPU starvation |
| Scheduler States (`vmstat`) | `r=2`, **`b=259–262`** | Only 2 runnable threads; ~260 threads blocked in `D`-state inflated Linux load average above 150 |
| Backing SSD (`nvme2n1`, `/datanapps`) | **`99.2% util`**, **`aqu-sz=515.9`**, **`w_await=1,312 ms`**, throughput **`48 MB/s`** | SPCC M.2 PCIe SSD (34% full) exhausted its SLC write cache under concurrent linker/random writes |
| Kernel Dirty Writeback (`/proc/meminfo`) | **`Dirty: 12.8–13.9 GiB`** | Massive unflushed writeback backlog |
| Kernel Wait Channels (`/proc/<pid>/task/*/wchan`) | **~260 `rust-lld` threads** stuck in **`balance_dirty_pages`** | Kernel actively throttling writers waiting for `nvme2n1` writeback to drain |

### Root Cause & Engineering Lesson

Three overlapping `bash scripts/verify.sh` invocations spawned concurrently under a single automation session against `/datanapps/cargo-target/...` and a secondary worktree target directory. Together they launched ~33 `rustc` processes and 28+ concurrent `rust-lld` linkers writing large debug test binaries simultaneously, collapsing NVMe throughput to `48 MB/s` and stretching a multi-minute verification run past `1h 35m`. A secondary GPU workload (`sdxl_gen.py`) also stalled in `folio_wait_bit_common` waiting on `/datanapps`.

From this single incident emerged the core toolkit architecture:
1. **`sys-investigate`** to distinguish I/O writeback thrashing from productive compute or CPU spin loops in 2 seconds without touching secondary disks.
2. **`sys-io-guard`** to serialize concurrent builds per physical disk via tmpfs `flock` locks and cap `CARGO_BUILD_JOBS`.
3. **`sys-mem-watch`**, **`sys-disk-hogs`**, and **`sys-crash-triage`** to close adjacent failure modes (memory leaks/OOM, unbounded Rust/Node/cache growth, and silent post-crash reboots) under the same zero-dependency reliability guarantees.

## 3. Six Non-Negotiable Engineering Tenets

Every tool in `sysadmin-tools` adheres to six strict invariants documented in [BUILDSPEC.md](BUILDSPEC.md):

1. **Zero Third-Party Runtime Dependencies**
   Python 3.10+ standard library only (`pyproject.toml` declares `dependencies = []`). No virtualenvs, pip wheels, or native extensions can break during an incident.
2. **Self-Contained Standalone Executables on Root NVMe**
   Each CLI in `src/sysadmin_tools/*.py` has zero intra-package imports. `./scripts/install.sh` copies each script directly (`install -m 0755`) into `~/.local/bin` on the root NVMe — never symlinks pointing to secondary data disks.
3. **Zero External Subprocesses on Hot Diagnostic Paths**
   `sys-investigate`, `sys-mem-watch`, `sys-disk-hogs`, and `sys-io-guard` (device resolution and locking) read `/proc`, `/sys`, `os.scandir()`, and `fcntl.flock()` directly. No `top`, `iostat`, `vmstat`, `ps`, `lsblk`, `df`, `du`, `smartctl`, or `nvme` subprocesses. (`sys-crash-triage` uses bounded, timeout-guarded `journalctl` calls only for binary systemd journals, with a pure-file `--no-journalctl` mode.)
4. **Two-Snapshot Wall-Clock Delta Accuracy**
   Instantaneous CPU %, disk utilization, `w_await`, `aqu_sz`, swap paging, per-process `%CPU`, RSS/swap growth rates, disk MB/s, and process turnover are computed strictly across `[t0, t1]` measured with `time.monotonic()`.
5. **Mount-Stall & Hang Resilience**
   `os.statvfs()` automatically skips non-local, FUSE, network, and automount filesystems (`fuse*`, `nfs*`, `cifs`, `sshfs`, `autofs`, etc.) and can be completely disabled via `--no-statvfs`. `sys-io-guard` places locks on tmpfs/root (`$XDG_RUNTIME_DIR/sys-io-guard` or `/tmp/sys-io-guard-<uid>`), never on the guarded secondary SSD.
6. **Script-First, Deterministic UX**
   Structured reports go strictly to `stdout`; verbose config and single-line errors (no Python tracebacks) go to `stderr`. JSON output uses `sort_keys=True`; exit codes follow a uniform contract (`0/1/2/3/4`); issue verdicts never emit `"No immediate intervention needed."`.

## 4. Threat Model & Trust Boundaries

`sysadmin-tools` operates primarily as an unprivileged local diagnostic and guard utility against untrusted or degraded host states (stalled mounts, high-churn process tables, arbitrary process command lines, and truncated crash logs).

| Boundary / Attack Surface | Failure Mode or Risk | Mitigation Implemented |
| --- | --- | --- |
| **Unprivileged `/proc/<pid>/io` access** | Reading `/proc/<pid>/io` for processes owned by `root` or other UIDs raises `PermissionError`; reporting `0.0 MB/s` would falsely exonerate a root daemon saturating disk. | Fails open to `None` (excluded from I/O totals rather than crashing or faking zeros) and labels output as readable `/proc/*/io`. |
| **Hung FUSE / NFS / CIFS / automount `statvfs()`** | Calling `os.statvfs()` on a wedged network or FUSE mount blocks the diagnostic tool indefinitely in kernel `D`-state. | `is_safe_statvfs_fstype()` filters `/proc/mounts` fstypes before calling `os.statvfs()`; `--no-statvfs` bypasses all `statvfs()` calls in `sys-investigate` and `sys-disk-hogs`. |
| **Lockfile placement (`sys-io-guard`)** | Placing lockfiles on the guarded secondary SSD causes lock acquisition itself to stall during an I/O storm, and stale lock files on crashed disks block recovery. | Lockfiles and atomic `.meta` files live exclusively on tmpfs/root (`$SYS_IO_GUARD_LOCK_DIR`, `$XDG_RUNTIME_DIR/sys-io-guard`, or `/tmp/sys-io-guard-<uid>`). Advisory kernel `fcntl.flock` releases automatically on process exit or crash. |
| **Bounded FD, cgroup, and log reads** | Scanning `/proc/<pid>/fd/*` or `/proc/<pid>/cgroup` for thousands of PIDs, or reading multi-gigabyte `/var/log/syslog` files, causes memory spikes and multi-second latency. | Mount attribution via `os.readlink()` runs only on top offending PIDs (cached per PID, capped at `max_fds=128`); cgroup parsing runs only on displayed top rows and `--diagnose-pid`; `sys-crash-triage` caps raw log scans to the trailing **8 MiB** (`DEFAULT_MAX_LOG_BYTES`). |
| **Command execution (`shell=True` injection)** | Wrapping user commands or invoking `journalctl` through a shell enables shell metacharacter injection and quoting bugs. | Zero `shell=True` anywhere in the codebase. `sys-io-guard` executes `subprocess.run(cmd, ...)` directly with argv list; `sys-crash-triage` executes fixed argv lists `["journalctl", "--no-pager", ...]` with bounded `--journal-timeout`. |
| **Untrusted `/proc/<pid>/stat` `comm` fields** | Kernel thread or user process names can contain spaces and parentheses (e.g., `(kworker/u129:15+flush-259:3)`), breaking naive `.split()`. | `parse_proc_stat()` strictly brackets `comm` using the first `(` and last `)` (`find("(")` / `rfind(")")`). |
| **Symlink loops & cross-mount traversal (`sys-disk-hogs`)** | Following symlinks or crossing bind/network mounts during workspace scans can cause infinite loops, double-counting, or hangs. | Iterative DFS stack (no recursion limit crash), `follow_symlinks=False`, single-filesystem `st_dev` guard enabled by default (`--one-file-system`), and `(st_dev, st_ino)` hardlink deduplication. |
| **Lock key & environment sanitization (`sys-io-guard`)** | Arbitrary `--lock-key` strings could traverse directories (`../`); `--env` strings containing NUL bytes corrupt `execve`. | `--lock-key` is sanitized to `[A-Za-z0-9._-]`; `--env` validates non-empty `KEY=VAL` free of `\x00`. |
| **IPC vs. storage write attribution** | `/proc/<pid>/io` `wchar` counts pipes, PTYs, sockets, and Wayland IPC — misattributing terminal chatter (`400+ MB/s`) to SSD saturation. | `sys-investigate` separates `write_bytes` (actual block writeback) from `wchar` in metrics and verdict scoring. |

---

[← Back to Documentation Hub](README.md) | [Next: Architecture & Subsystem Internals →](architecture.md)
