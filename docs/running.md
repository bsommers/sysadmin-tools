# Installation & CLI Reference

[← Back to Documentation Hub](README.md)

## 1. Prerequisites

- **Operating System:** Linux with `/proc` and `/sys` mounted (`fcntl.flock` support).
- **Python Runtime:** Python **3.10+** (standard library only; zero third-party packages).
- **Optional Permissions for Full Visibility:**
  - Running `sys-investigate` unprivileged reads all `/proc/<pid>/stat`, `wchan`, and `cgroup` data, but `/proc/<pid>/io` for processes owned by `root` or other users requires elevated privileges (`sudo` or `CAP_DAC_READ_SEARCH`).
  - Running `sys-crash-triage` with full systemd journal and `/var/log/{kern.log,syslog}` visibility works best when the user is in the `adm` / `systemd-journal` groups (or via `sudo`), though `--no-journalctl` works purely on readable files.

## 2. Installation

### Recommended: Standalone Direct Copies on Root NVMe (`scripts/install.sh`)

Installing direct copies into `~/.local/bin` (on the root OS disk) ensures every CLI remains executable even if a secondary SSD or workspace mount stalls:

```bash
# Preview actions without modifying files
./scripts/install.sh --dry-run

# Install all 7 standalone CLIs into ~/.local/bin (mode 0755)
./scripts/install.sh

# Install to a custom prefix (e.g., /usr/local/bin)
sudo ./scripts/install.sh --prefix /usr/local/bin
```

`scripts/install.sh` installs:
- `~/.local/bin/sys-investigate`
- `~/.local/bin/sys-io-guard`
- `~/.local/bin/sys-mem-watch`
- `~/.local/bin/sys-disk-hogs`
- `~/.local/bin/sys-crash-triage`
- `~/.local/bin/sys-net-watch`
- `~/.local/bin/sys-systemd-watch`

Ensure `~/.local/bin` is on your `PATH`.

### Alternative: Standard Python Package (`pip`)

```bash
pip install .
```

> **Note:** Avoid editable installs (`pip install -e .`) if the repository lives on a secondary data disk — an editable install depends on reading `src/` from that disk during an I/O stall.

## 3. CLI Option Reference

### 3.1 `sys-investigate`

Diagnoses what is driving CPU/load and whether the workload is productive, thrashing, wedged, or spinning.

```text
sys-investigate [--interval INTERVAL] [--top TOP] [--json | --markdown]
                [--watch [COUNT]] [--progress-check] [--no-statvfs]
                [--append-log PATH] [--diff-log PATH]
                [--diff-baseline PATH] [--diff-window N]
                [--diagnose-pid PID] [--exit-on-issue] [--verbose]
                [--version]
```

| Flag | Value / Range | Default | Description |
| --- | --- | --- | --- |
| `--interval INTERVAL` | `0.2..60.0` (sec) | `2.0` (or `SYS_INVESTIGATE_INTERVAL`) | Two-snapshot sampling window duration |
| `--top TOP` | `1..100` | `10` (or `SYS_INVESTIGATE_TOP`) | Maximum rows per section (`top_cpu`, `top_io`, `d_state_top`, `overlapping_workloads`, `recurring_offenders`) |
| `--json` | flag | off | Emit deterministic JSON (`sort_keys=True`) to `stdout` (mutually exclusive with `--markdown`) |
| `--markdown` | flag | off | Emit Markdown report to `stdout` (mutually exclusive with `--json`) |
| `--watch [COUNT]` | optional int (`>=0`) | off | Repeat investigation `COUNT` times (`0` or omitted = run until `Ctrl-C`) |
| `--progress-check` | flag | off | Run two consecutive sampling windows to confirm whether stalls are progressing or wedged |
| `--no-statvfs` | flag | off | Skip `os.statvfs()` mount fill checks for extra protection when mounts are hung |
| `--append-log PATH` | file path | off | Append each report as a compact JSONL line to `PATH` (creates parent dirs) |
| `--diff-log PATH` | file path | off | Summarize trends and diff snapshots in an `--append-log` JSONL file (offline mode) |
| `--diff-baseline PATH` | file path | off | Baseline JSONL log file to compare against `--diff-log PATH` |
| `--diff-window N` | int `>= 1` | off | Compare the last `N` snapshots against the prior `N` snapshots (or last `N` of `--diff-baseline`) |
| `--diagnose-pid PID` | positive int | off | Run targeted two-snapshot diagnosis of `PID` and its descendant process subtree |
| `--exit-on-issue` | flag | off | Exit with code `3` when verdict `is_issue` is `True` (or log trend degrades in `--diff-log` mode) |
| `--verbose` | flag | off | Print effective configuration to `stderr` |
| `--version` | flag | off | Print `sys-investigate 0.5.0` and exit |

---

### 3.2 `sys-io-guard`

Serializes heavy I/O workloads per target physical disk via tmpfs advisory `flock`, capping Cargo build jobs and child CPU niceness.

```text
sys-io-guard [--path DIR] [--lock-key NAME] [--lock-dir DIR] [--timeout SEC]
             [--cargo-jobs N] [--nice N] [--env KEY=VAL]
             [--status | --dry-run] [--json] [--verbose] [-- COMMAND [ARGS...]]
```

| Flag / Arg | Value / Range | Default | Description |
| --- | --- | --- | --- |
| `command` | positional (`-- CMD...`) | — | Command to execute under the disk lock (optionally preceded by `--`) |
| `--path DIR` | directory path | `$CARGO_TARGET_DIR` or cwd | Path on target filesystem used to resolve backing physical disk lock key |
| `--lock-key NAME` | string | auto from disk/mount | Explicit lock key override (sanitized to `[A-Za-z0-9._-]`) |
| `--lock-dir DIR` | directory path | `$SYS_IO_GUARD_LOCK_DIR`, `$XDG_RUNTIME_DIR/sys-io-guard`, or `/tmp/sys-io-guard-<uid>` | Override tmpfs/root directory for `*.lock` and `*.meta` files |
| `--timeout SEC` | float `>= 0` | wait indefinitely | Lock wait timeout in seconds (`0` = non-blocking fail-fast) |
| `--cargo-jobs N` | int `>= 0` | `min(8, max(2, cpus//4))` (or `SYS_IO_GUARD_CARGO_JOBS`) | Set `CARGO_BUILD_JOBS=N` for child (`0` disables setting the variable) |
| `--nice N` | `0..19` | `10` | Child process `os.nice(N)` increment (`0` disables) |
| `--env KEY=VAL` | repeatable | none | Extra environment variable override passed to child process |
| `--status` | flag | off | Probe `*.lock` files in lock dir to show active vs idle locks and holder metadata (mutually exclusive with `--dry-run`) |
| `--dry-run` | flag | off | Print resolved device, lock file, env overrides, and command without locking or executing |
| `--json` | flag | off | Emit deterministic JSON (`sort_keys=True`) for `--status` or `--dry-run` only |
| `--verbose` | flag | off | Log wait duration, resolved device, lock file, and env overrides to `stderr` |

---

### 3.3 `sys-mem-watch`

Diagnoses RSS/swap growth rates, page reclaim pressure, memory PSI, and OOM-kill risk via pure `/proc` two-snapshot deltas.

```text
sys-mem-watch [--interval INTERVAL] [--top TOP] [--json | --markdown]
              [--watch [COUNT]] [--append-log PATH] [--exit-on-issue]
              [--verbose]
```

| Flag | Value / Range | Default | Description |
| --- | --- | --- | --- |
| `--interval INTERVAL` | `0.2..60.0` (sec) | `2.0` (or `SYS_MEM_WATCH_INTERVAL`) | Two-snapshot sampling window duration |
| `--top TOP` | `1..100` | `10` (or `SYS_MEM_WATCH_TOP`) | Maximum rows per leaderboard (`top_rss`, `top_growers`, `top_swap`, `top_oom_risk`) |
| `--json` | flag | off | Emit deterministic JSON (`sort_keys=True`) to `stdout` (mutually exclusive with `--markdown`) |
| `--markdown` | flag | off | Emit Markdown report to `stdout` (mutually exclusive with `--json`) |
| `--watch [COUNT]` | optional int (`>=0`) | off | Repeat sampling `COUNT` times (`0` or omitted = run until `Ctrl-C`) |
| `--append-log PATH` | file path | off | Append each report as a compact JSONL line to `PATH` (creates parent dirs) |
| `--exit-on-issue` | flag | off | Exit with code `3` when verdict status is not `HEALTHY` |
| `--verbose` | flag | off | Print effective configuration to `stderr` |

---

### 3.4 `sys-disk-hogs`

Iterative pure-stdlib workspace/target/cache disk usage analyzer with hardlink deduplication and reclaimable hog classification.

```text
sys-disk-hogs [--top TOP] [--depth DEPTH] [--min-mib MIN_MIB]
              [--min-file-mib MIN_FILE_MIB] [--one-file-system | --cross-mounts]
              [--no-statvfs] [--json | --markdown] [--verbose] [paths ...]
```

| Flag / Arg | Value / Range | Default | Description |
| --- | --- | --- | --- |
| `paths` | 0 or more paths | `["."]` | Root directories (or individual files) to analyze |
| `--top TOP` | `1..100` | `15` | Maximum rows per section (`top_dirs`, `reclaimable_hogs`, `vcs_metadata`, `largest_files`) |
| `--depth DEPTH` | `1..10` | `2` | Maximum subdirectory depth included in `top_dirs` |
| `--min-mib MIN_MIB` | float `>= 0` | `1.0` | Minimum subdirectory size in MiB for `top_dirs` |
| `--min-file-mib MIN_FILE_MIB` | float `>= 0` | `10.0` | Minimum regular file size in MiB for `largest_files` |
| `--one-file-system` | flag | **enabled** | Stay on each root's filesystem by comparing `st_dev` (mutually exclusive with `--cross-mounts`) |
| `--cross-mounts` | flag | off | Allow traversal across mount boundaries |
| `--no-statvfs` | flag | off | Skip `os.statvfs()` filesystem fill checks |
| `--json` | flag | off | Emit deterministic JSON (`sort_keys=True`) to `stdout` (mutually exclusive with `--markdown`) |
| `--markdown` | flag | off | Emit Markdown report to `stdout` (mutually exclusive with `--json`) |
| `--verbose` | flag | off | Print effective configuration to `stderr` |

---

### 3.5 `sys-crash-triage`

Automates post-crash and unexpected-reboot forensic investigation across boot timelines, AMD reset registers, NUL log gaps, crash signatures, and kdump/pstore readiness.

```text
sys-crash-triage [--boot BOOT] [--boots-history BOOTS_HISTORY]
                 [--tail-lines TAIL_LINES] [--no-journalctl]
                 [--journal-timeout JOURNAL_TIMEOUT] [--show-steps]
                 [--json | --markdown] [--append-log APPEND_LOG]
                 [--exit-on-issue] [--verbose]
```

| Flag | Value / Range | Default | Description |
| --- | --- | --- | --- |
| `--boot BOOT` | int `<= 0` | `0` | Target boot index to inspect (`0` = current boot after crash, `-1` = previous boot) |
| `--boots-history N` | `1..50` | `5` | Number of recent boots included in timeline |
| `--tail-lines N` | `10..500` | `80` | Tail lines to inspect from end of previous boot |
| `--no-journalctl` | flag | off | Skip `journalctl` subprocesses; inspect `/proc`, `/sys`, `/var/log/*`, pstore, and `/var/crash` only |
| `--journal-timeout SEC` | `0.5..30.0` | `5.0` | Per-call timeout in seconds for bounded `journalctl` queries |
| `--show-steps` | flag | off | Include step-by-step manual troubleshooting commands alongside findings |
| `--json` | flag | off | Emit deterministic JSON (`sort_keys=True`) to `stdout` (mutually exclusive with `--markdown`) |
| `--markdown` | flag | off | Emit Markdown report to `stdout` (mutually exclusive with `--json`) |
| `--append-log PATH` | file path | off | Append one JSONL record to `PATH` per run (creates parent dirs) |
| `--exit-on-issue` | flag | off | Exit with code `3` when an unclean crash/reset or fatal fault is detected |
| `--verbose` | flag | off | Print diagnostic warnings to `stderr` |

---

### 3.6 `sys-net-watch`

Diagnoses NIC errors/drops, TCP retransmits, socket leaks (`CLOSE_WAIT`, `TIME_WAIT`, SYN backlog), and per-CPU softnet pressure via pure `/proc/net/*` snapshots.

```text
sys-net-watch [--interval INTERVAL] [--samples N] [--iface IFACE] [--top TOP]
              [--json | --markdown] [--exit-on-issue] [--verbose] [--version]
```

| Flag | Value / Range | Default | Description |
| --- | --- | --- | --- |
| `--interval INTERVAL` | `0.1..60.0` (sec) | `1.0` (or `SYS_NET_WATCH_INTERVAL`) | Sampling interval between snapshots |
| `--samples N` | int `>= 1` | `2` | Number of `/proc/net/*` snapshots (`2` = one delta window) |
| `--iface IFACE` | string (repeatable / comma-separated) | all interfaces | Filter `/proc/net/dev` interfaces (e.g. `--iface eno1,lo`) |
| `--top TOP` | `1..100` | `8` (or `SYS_NET_WATCH_TOP`) | Maximum interface and softnet CPU rows to display |
| `--json` | flag | off | Emit deterministic JSON (`sort_keys=True`) to `stdout` (mutually exclusive with `--markdown`) |
| `--markdown` | flag | off | Emit Markdown report to `stdout` (mutually exclusive with `--json`) |
| `--exit-on-issue` | flag | off | Exit with code `3` when verdict status is not `HEALTHY` |
| `--verbose` | flag | off | Print effective configuration to `stderr` |
| `--version` | flag | off | Print `sys-net-watch 0.5.0` and exit |

---

### 3.7 `sys-systemd-watch`

Monitors systemd cgroup v2 resource pressure (memory/OOM, CFS CPU throttling, I/O PSI), service crash-loop PID churn, and journal growth bursts with zero `systemctl` or `journalctl` subprocesses.

```text
sys-systemd-watch [--interval INTERVAL] [--samples N] [--top TOP]
                  [--unit PATTERN] [--cgroup-root PATH]
                  [--json | --markdown] [--exit-on-issue] [--verbose] [--version]
```

| Flag | Value / Range | Default | Description |
| --- | --- | --- | --- |
| `--interval INTERVAL` | `0.05..60.0` (sec) | `1.0` (or `SYS_SYSTEMD_WATCH_INTERVAL`) | Seconds between cgroup snapshots |
| `--samples N` | `1..60` | `2` | Number of cgroup snapshots to collect |
| `--top TOP` | `1..100` | `10` (or `SYS_SYSTEMD_WATCH_TOP`) | Maximum rows per section (`top_memory`, `top_cpu`, `top_io`, `problem_units`, `top_slices`) |
| `--unit PATTERN` | string | all units | Optional substring or glob filter for unit name or cgroup path (e.g. `docker`, `'*.service'`) |
| `--cgroup-root PATH` | directory path | `/sys/fs/cgroup` | Cgroup v2 root directory |
| `--json` | flag | off | Emit deterministic JSON (`sort_keys=True`) to `stdout` (mutually exclusive with `--markdown`) |
| `--markdown` | flag | off | Emit Markdown report to `stdout` (mutually exclusive with `--json`) |
| `--exit-on-issue` | flag | off | Exit with code `3` when verdict status is not `HEALTHY` |
| `--verbose` | flag | off | Print effective configuration to `stderr` |
| `--version` | flag | off | Print `sys-systemd-watch 0.5.0` and exit |

## 4. Environment Variables

| Variable | Read / Set By | Purpose & Precedence |
| --- | --- | --- |
| `SYS_INVESTIGATE_INTERVAL` | `sys-investigate` (read) | Default `--interval` (`0.2..60.0`); CLI `--interval` takes precedence |
| `SYS_INVESTIGATE_TOP` | `sys-investigate` (read) | Default `--top` (`1..100`); CLI `--top` takes precedence |
| `CARGO_TARGET_DIR` | `sys-io-guard` (read) | Default target directory for `--path` when `--path` is omitted (falls back to cwd) |
| `SYS_IO_GUARD_LOCK_DIR` | `sys-io-guard` (read) | Highest-priority default for `--lock-dir` when `--lock-dir` is omitted |
| `XDG_RUNTIME_DIR` | `sys-io-guard` (read) | Second-priority tmpfs lock dir (`$XDG_RUNTIME_DIR/sys-io-guard`), falling back to `/tmp/sys-io-guard-<uid>` |
| `SYS_IO_GUARD_CARGO_JOBS` | `sys-io-guard` (read) | Default `--cargo-jobs` (`>=0`); CLI `--cargo-jobs` takes precedence |
| `CARGO_BUILD_JOBS` | `sys-io-guard` (**exported to child**) | Automatically exported to guarded child processes when `--cargo-jobs > 0` |
| `SYS_MEM_WATCH_INTERVAL` | `sys-mem-watch` (read) | Default `--interval` (`0.2..60.0`); CLI `--interval` takes precedence |
| `SYS_MEM_WATCH_TOP` | `sys-mem-watch` (read) | Default `--top` (`1..100`); CLI `--top` takes precedence |
| `SYS_NET_WATCH_INTERVAL` | `sys-net-watch` (read) | Default `--interval` (`0.1..60.0`); CLI `--interval` takes precedence |
| `SYS_NET_WATCH_TOP` | `sys-net-watch` (read) | Default `--top` (`1..100`); CLI `--top` takes precedence |
| `SYS_SYSTEMD_WATCH_INTERVAL` | `sys-systemd-watch` (read) | Default `--interval` (`0.05..60.0`); CLI `--interval` takes precedence |
| `SYS_SYSTEMD_WATCH_TOP` | `sys-systemd-watch` (read) | Default `--top` (`1..100`); CLI `--top` takes precedence |

## 5. Output Formats & Stream Discipline

- **Stream separation:** Report data is always written to `stdout`. Configuration summaries (`--verbose`), warnings, and single-line `error: ...` diagnostics are always written to `stderr`, keeping pipes like `sys-investigate --json | jq '.verdict'` clean.
- **Plain text (default):** Compact, aligned terminal report tailored for rapid visual scanning during incidents.
- **Markdown (`--markdown`):** Structured headings and bullet/table output ready to paste directly into incident tickets, PRs, or post-mortems (`sys-investigate`, `sys-mem-watch`, `sys-disk-hogs`, `sys-crash-triage`, `sys-net-watch`, `sys-systemd-watch`).
- **Deterministic JSON (`--json`):** Pretty-printed with `indent=2, sort_keys=True` for reliable diffing, snapshot testing, and `jq` pipelines.
- **JSONL telemetry (`--append-log PATH` & `--diff-log PATH`):** Appends one JSON object per line to `PATH` (creating parent directories as needed) while still emitting the selected `stdout` format (`sys-investigate`, `sys-mem-watch`, `sys-crash-triage`), and compares/diffs historical JSONL snapshots offline via `sys-investigate --diff-log PATH`.

## 6. Unified Exit Code Reference

| Exit Code | Meaning | Applies To |
| --- | --- | --- |
| `0` | Success (or healthy / no issue detected; also `--status` and `--dry-run` on `sys-io-guard`) | All 7 CLIs |
| `1` | Validation error (out-of-range flag, invalid env var, missing PID/path/iface/cgroup-root, `--json` without `--status`/`--dry-run` on `sys-io-guard`) or OS/runtime error | All 7 CLIs |
| `2` | Command-line syntax / `argparse` error (unknown flag, missing required value, mutually exclusive flags) | All 7 CLIs |
| `3` | Issue detected while `--exit-on-issue` is enabled (`verdict.is_issue == true`) | `sys-investigate`, `sys-mem-watch`, `sys-crash-triage`, `sys-net-watch`, `sys-systemd-watch` |
| `4` | Lock busy / wait `--timeout` expired before acquiring exclusive disk lock | `sys-io-guard` |
| *child rc* / `128+N` | When `sys-io-guard` executes a command, it transparently returns the child's exit code (or `128 + signal` if killed by a signal, e.g., `130` on `SIGINT`) | `sys-io-guard` |

---

[← Previous: Architecture & Internals](architecture.md) | [Next: How-To Recipes & Troubleshooting →](how-to.md)
