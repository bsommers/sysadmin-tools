[← Back to User's Guide](README.md) | [← Project README](../../README.md)

# Chapter 3: Preventing I/O Storms (`sys-io-guard`)

`sys-investigate` tells you *when* your storage is collapsing; **`sys-io-guard`** prevents the collapse from happening in the first place. It serializes heavy I/O jobs (builds, test suites, verification scripts, data pipelines) per target physical disk using kernel advisory locks (`fcntl.flock`), while capping Cargo parallelism and lowering child CPU niceness.

---

## 1. Why Parallel Rust/C++ Builds Collapse NVMe SLC Write Caches

Consumer and prosumer TLC/QLC NVMe SSDs advertise 3,000–7,000 MB/s write speeds only while their **pseudo-SLC write cache** has free headroom. When two or three overlapping `cargo test`, `ninja`, or `verify.sh` pipelines hit the same SSD simultaneously:

1. Dozens of concurrent `rust-lld` / `ld` / `rustc` processes emit gigabytes of multi-stream random debug-binary writes.
2. The drive's SLC write cache fills and switches to direct TLC/QLC folding, dropping sustained write throughput from thousands of MB/s down to **~40–50 MB/s**.
3. The Linux page cache buffers 10–14+ GiB of `Dirty` pages; queue depth (`aqu-sz`) climbs into the hundreds and write latency (`w_await`) exceeds **1,000 ms**.
4. Hundreds of threads enter uninterruptible `D`-state inside `balance_dirty_pages`, stalling unrelated desktop and shell writes to that mount for 10–20+ minutes — and making every build take **longer** in parallel than if they had run one after another.

Serializing builds per physical SSD with `sys-io-guard` keeps total throughput higher, latency predictable, and the desktop responsive.

---

## 2. How Disk Resolution & Tmpfs Locking Work

### 2.1 Zero-Subprocess Physical Disk Resolution

Given `--path DIR` (defaulting to `$CARGO_TARGET_DIR`, or the current working directory if unset), `sys-io-guard`:
1. Resolves `realpath(DIR)` and finds the deepest matching mountpoint in `/proc/mounts` (handling stacked mounts and octal-escaped spaces).
2. If the mount uses a real block device (`/dev/...`) and is not a virtual/FUSE/network filesystem, it resolves device symlinks (e.g., `/dev/disk/by-uuid/...`, `/dev/mapper/...`) and strips partition suffixes:
   - `nvme2n1p1` → **`nvme2n1`**
   - `sda2` / `vda1` → **`sda`** / **`vda`**
   - `mmcblk0p2` → **`mmcblk0`**
   - `md0p1` / `loop0p1` / `nbd0p1` → **`md0`** / **`loop0`** / **`nbd0`**
   - `dm-0` (LVM / LUKS device-mapper) → follows `/sys/block/dm-*/slaves` recursively (up to 4 layers deep) to find the single backing physical disk.
3. For virtual, tmpfs, FUSE, or network mounts (where no single physical block device applies), it generates a deterministic fallback key: **`mnt-<sha1(mountpoint)[:8]>`**.

Notice that two completely different paths on the same SSD — e.g., `/datanapps/cargo-target` and `/datanapps/worktrees/app/target` — automatically map to the **same lock key** (`nvme2n1`) and cleanly serialize against each other.

### 2.2 Why Locks Always Live on Tmpfs / Root

`sys-io-guard` **never** creates lockfiles on the guarded secondary SSD. Lock directory precedence is:

1. `--lock-dir DIR` (explicit CLI override)
2. `$SYS_IO_GUARD_LOCK_DIR` (environment variable)
3. `$XDG_RUNTIME_DIR/sys-io-guard` (standard per-user tmpfs, e.g. `/run/user/1000/sys-io-guard`)
4. `/tmp/sys-io-guard-<uid>` (fallback when `$XDG_RUNTIME_DIR` is unset)

Keeping `<lock_key>.lock` and `<lock_key>.meta` on tmpfs guarantees that checking lock status (`--status`) or waiting on a lock never blocks on a saturated data SSD, and all lock state is automatically cleared on reboot.

---

## 3. Complete CLI Options & Defaults

| Flag | Default / Env | Purpose |
|---|---|---|
| `--path DIR` | `$CARGO_TARGET_DIR`, else cwd | Target path used to resolve backing physical disk lock key. |
| `--lock-key NAME` | derived from `--path` | Explicit lock key override (sanitized to `[A-Za-z0-9._-]`, leading dots stripped, max 128 chars). |
| `--lock-dir DIR` | `$SYS_IO_GUARD_LOCK_DIR` → `$XDG_RUNTIME_DIR/sys-io-guard` → `/tmp/sys-io-guard-<uid>` | Override lockfile directory. |
| `--timeout SEC` | omitted (wait indefinitely) | Lock wait timeout in seconds (`>= 0`; `0` = non-blocking fail-fast; exits **`4`** if busy). |
| `--cargo-jobs N` | `SYS_IO_GUARD_CARGO_JOBS`, else `min(8, max(2, cpus // 4))` | Sets `CARGO_BUILD_JOBS=N` for the child process. Pass `0` to disable setting `CARGO_BUILD_JOBS`. |
| `--nice N` | `10` (range `0..19`) | Applies `os.nice(N)` to child process via `preexec_fn`. Pass `0` to disable. |
| `--env KEY=VAL` | none (repeatable) | Extra environment variable override passed to child (takes precedence over `CARGO_BUILD_JOBS` if same key). |
| `--status` | mutually exclusive with `--dry-run` | Probe all `*.lock` files in lock directory non-blockingly and print active/idle state + holder metadata. |
| `--dry-run` | mutually exclusive with `--status` | Print resolved device, lock file, env overrides, and command without locking or executing. |
| `--json` | only with `--status` or `--dry-run` | Emit deterministic JSON (`sort_keys=True`). |
| `--verbose` | flag | Log wait duration, resolved device, lock file, nice, and env overrides to `stderr`. |
| `-- COMMAND [ARGS...]` | required unless `--status`/`--dry-run` | Command executed directly via `subprocess.run` (no shell interpolation). |

### Exit Codes

| Exit Code | Meaning |
|---|---|
| Child's exit code | Command executed under lock (`128 + signum` if child died from a signal). |
| `0` | `--status` or `--dry-run` succeeded (or child exited `0`). |
| `1` | Validation error (invalid `--nice`, `--timeout`, `--env`, `--json` without `--status`/`--dry-run`, missing command) or runtime `OSError`. |
| `2` | `argparse` syntax error. |
| `4` | Lock busy after `--timeout SEC` expired (`LockBusyError`). |
| `130` / `128+SIGTERM` | Interrupted by `Ctrl-C` or `SIGTERM` (lock and `.meta` are cleanly released in `finally`). |

---

## 4. Previewing with `--dry-run` and Auditing with `--status`

### Preview Resolution Before Running

Always test a new path or custom environment with `--dry-run` (optionally `--json`):

```bash
sys-io-guard --dry-run --json --path /datanapps/cargo-target --cargo-jobs 4 --env RUSTFLAGS="-C debuginfo=1" -- cargo test
```

Example JSON output:

```json
{
  "cargo_jobs": 4,
  "command": ["cargo", "test"],
  "device": "/dev/nvme2n1p1",
  "disk": "nvme2n1",
  "env_overrides": {
    "CARGO_BUILD_JOBS": "4",
    "RUSTFLAGS": "-C debuginfo=1"
  },
  "fstype": "ext4",
  "lock_dir": "/run/user/1000/sys-io-guard",
  "lock_file": "/run/user/1000/sys-io-guard/nvme2n1.lock",
  "lock_key": "nvme2n1",
  "lock_key_source": "disk",
  "meta_file": "/run/user/1000/sys-io-guard/nvme2n1.meta",
  "mountpoint": "/datanapps",
  "nice": 10,
  "target_path": "/datanapps/cargo-target",
  "timeout": null
}
```

### Auditing Active & Idle Locks

```bash
sys-io-guard --status
sys-io-guard --status --json
```

`--status` probes every `*.lock` file with `LOCK_EX | LOCK_NB` (releasing immediately if unlocked) and reads the corresponding `.meta` file (`pid`, `ppid`, `started_at`, `target_path`, `cmd`) written atomically while the holder owns the lock:

```text
Lock directory: /run/user/1000/sys-io-guard
Locks: 2 total (1 active, 1 idle)
  [ACTIVE] nvme2n1 (/run/user/1000/sys-io-guard/nvme2n1.lock): pid=41230 ppid=41190 started_at=2026-11-23T17:10:02+00:00 target_path=/datanapps/cargo-target cmd='cargo test'
  [IDLE]   nvme0n1 (/run/user/1000/sys-io-guard/nvme0n1.lock)
```

---

## 5. Practical Recipes: Wrapping Build & Verify Scripts Safely

### 5.1 Basic Cargo Build / Test Guard

```bash
# Uses $CARGO_TARGET_DIR (or cwd), default nice=10, and auto CARGO_BUILD_JOBS
sys-io-guard -- cargo test --workspace
```

### 5.2 Non-Blocking / Bounded Timeout for Automated Agents & Cron

Prevent build agents or CI watchers from piling up indefinitely when a disk is already busy:

```bash
# Wait up to 30 seconds; exit 4 if another build still holds the disk lock
sys-io-guard --path /datanapps/cargo-target --timeout 30 --cargo-jobs 4 --verbose -- bash scripts/verify.sh
rc=$?
if [[ "$rc" -eq 4 ]]; then
  echo "Build skipped: target SSD is already running another guarded build." >&2
fi
```

Use `--timeout 0` for instant fail-fast behavior (useful in background timers or git hooks).

### 5.3 Non-Cargo Workloads (C++/CMake/Ninja, Python, Docker, Backups)

Disable `CARGO_BUILD_JOBS` with `--cargo-jobs 0` and pass your own concurrency caps via `--env` or tool flags:

```bash
# C++ Ninja build capped to 6 jobs on the same SSD lock
sys-io-guard --path ./build --cargo-jobs 0 --nice 12 -- ninja -C build -j 6

# Cross-project manual lock group (e.g., serialize heavy ML jobs and builds together)
sys-io-guard --lock-key workstation-heavy --cargo-jobs 0 --timeout 600 -- python3 train.py
```

### 5.4 Self-Guarding Inside `scripts/verify.sh` (Re-Entrancy Pattern)

If you want a script to guard itself automatically without deadlocking on recursive invocation, guard it with an environment marker:

```bash
#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${MY_VERIFY_GUARDED:-}" ]] && command -v sys-io-guard >/dev/null 2>&1; then
  exec sys-io-guard --path "${CARGO_TARGET_DIR:-$PWD}" --env MY_VERIFY_GUARDED=1 -- "$0" "$@"
fi

# Real verification steps run here under the disk lock
cargo test --workspace
```

> **Safety Note:** `sys-io-guard` executes `COMMAND` directly via `subprocess.run(cmd, ...)` without invoking a shell, so shell metacharacters in arguments are never re-interpreted. If you need shell pipes or redirects inside the guarded block, wrap them explicitly with `-- bash -c '...'`.

---

**Navigation:** [← Previous: Chapter 2 — Live System Investigation](02-live-system-investigation.md) | [Next: Chapter 4 — Memory & Disk Auditing →](04-memory-and-disk-auditing.md)
