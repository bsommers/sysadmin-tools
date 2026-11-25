[← Back to User's Guide](README.md) | [← Project README](../../README.md)

# Chapter 6: Automation & Troubleshooting

Every CLI in `sysadmin-tools` follows a script-first contract: structured data goes to `stdout`, warnings/verbose configuration go to `stderr`, JSON output is deterministic (`sort_keys=True`), and exit codes are consistent across tools.

---

## 1. Scripting Pipelines with `--json | jq`

### `sys-investigate`

```bash
# Extract verdict summary
sys-investigate --json | jq '{bottleneck: .verdict.bottleneck, productivity: .verdict.productivity, is_issue: .verdict.is_issue}'

# Find saturated disks (util >= 85% or write latency >= 50ms)
sys-investigate --json | jq '.disks[] | select(.util_pct >= 85 or .w_await_ms >= 50) | {device, util_pct, aqu_sz, w_await_ms, w_mbs}'

# Check NVMe thermal status across all controllers
sys-investigate --json | jq '.nvme_health[] | {controller, state, temp_c, thermal_status}'

# Extract --progress-check status and persistent D-state PIDs
sys-investigate --progress-check --json | jq '.progress_check | {status, dirty_gib_delta, persistent_d_pids}'

# Inspect a single PID's subtree rollup
sys-investigate --diagnose-pid 12345 --json | jq '{status: .verdict.status, subtree: .subtree | del(.top_descendants)}'
```

### `sys-io-guard`

```bash
# List currently active disk locks and holder PIDs
sys-io-guard --status --json | jq '.locks[] | select(.active) | {lock_key, pid: .holder.pid, cmd: .holder.cmd}'

# Resolve which physical disk a path maps to before scheduling jobs
sys-io-guard --dry-run --json --path /datanapps/cargo-target -- true | jq '{disk, lock_key, cargo_jobs}'
```

### `sys-mem-watch`

```bash
# Verdict + top 3 fastest-growing processes
sys-mem-watch --json | jq '{status: .verdict.status, growers: [.top_growers[:3][] | {pid, comm, rss_delta_mib, rss_rate_mibs, cgroup}]}'
```

### `sys-disk-hogs`

```bash
# Total reclaimable GiB and top 5 build/cache hogs
sys-disk-hogs ~/src /datanapps --json | jq '{reclaimable_gib: .summary.reclaimable_gib, top_hogs: .reclaimable_hogs[:5]}'
```

### `sys-crash-triage`

```bash
# Post-reboot summary: verdict, AMD reset class, and kdump misconfiguration check
sys-crash-triage --json | jq '{verdict: .verdict.status, confidence: .verdict.confidence, reset: .hardware_reset.reset_class, kdump_misconfigured: .crash_capture.kdump.reserved_but_disabled}'
```

### `sys-net-watch` & `sys-systemd-watch`

```bash
# Network summary: status, interface drops/link errors, and TCP retransmits
sys-net-watch --json | jq '{status: .verdict.status, drops: .interfaces_summary.drops_delta, retrans_pct: .snmp.tcp.retrans_rate_pct, softnet_drops: .softnet.dropped_delta}'

# Systemd cgroup summary: status, flagged units, and journal growth
sys-systemd-watch --json | jq '{status: .verdict.status, active_issues: .verdict.active_issues, problem_units: [.problem_units[] | {unit, issues}]}'
```

---

## 2. Exit-Code Alerting Matrix

| Exit Code | `sys-investigate` | `sys-mem-watch` | `sys-crash-triage` | `sys-net-watch` | `sys-systemd-watch` | `sys-io-guard` | `sys-disk-hogs` |
|---|---|---|---|---|---|---|---|
| **`0`** | Healthy / OK | `HEALTHY` / OK | Clean / OK | `HEALTHY` / OK | `HEALTHY` / OK | Child `0`, or `--status`/`--dry-run` OK | OK |
| **`1`** | Validation / missing PID / log error | Validation / runtime error | Validation / runtime error | Validation / missing iface / runtime error | Validation / missing cgroup root / error | Validation / runtime error | Validation / missing path / runtime error |
| **`2`** | `argparse` usage error | `argparse` usage error | `argparse` usage error | `argparse` usage error | `argparse` usage error | `argparse` usage error | `argparse` usage error |
| **`3`** | Issue detected (`--exit-on-issue`) | Status `!= HEALTHY` (`--exit-on-issue`) | Crash/fault detected (`--exit-on-issue`) | Status `!= HEALTHY` (`--exit-on-issue`) | Status `!= HEALTHY` (`--exit-on-issue`) | *(not used)* | *(not used)* |
| **`4`** | *(not used)* | *(not used)* | *(not used)* | *(not used)* | *(not used)* | Lock busy after `--timeout` | *(not used)* |
| **Other** | — | — | — | — | — | Child's exit code (`128+sig` on signal) | — |

### Example Alerting Wrapper

```bash
#!/usr/bin/env bash
set -u

REPORT="$(sys-investigate --progress-check --no-statvfs --exit-on-issue --json)"
rc=$?
if [[ "$rc" -eq 3 ]]; then
  MSG="$(printf '%s' "$REPORT" | jq -r '.verdict | "\(.bottleneck) / \(.productivity): \(.reasons[0])"')"
  logger -t sys-investigate -p user.warning "$MSG"
  # Optionally forward to desktop notification or webhook here
fi
```

---

## 3. Append-Only JSONL Telemetry & Systemd User Timers

`sys-investigate`, `sys-mem-watch`, and `sys-crash-triage` all support `--append-log PATH`, writing one compact JSON object per line and creating parent directories automatically.

### Querying & Diffing JSONL Logs (`jq` + `--diff-log`)

```bash
# Find every recorded incident window in a JSONL telemetry log
jq -c 'select(.verdict.is_issue == true) | {ts: .timestamp, b: .verdict.bottleneck, p: .verdict.productivity, dirty: .memory.dirty_gib}' ~/.local/state/sysadmin-tools/investigate.jsonl

# Offline trend summary and sliding-window diff
sys-investigate --diff-log ~/.local/state/sysadmin-tools/investigate.jsonl --diff-window 5
```

### Setting Up the Packaged Systemd User Timer (`packaging/systemd/`)

Ready-to-use user unit templates live in [`packaging/systemd/`](../../packaging/systemd/README.md) (`Nice=10`, `IOSchedulingClass=best-effort`, `MemoryMax=128M`, `--no-statvfs`, writing to `~/.local/state/sysadmin-tools/investigate.jsonl` every 5 minutes):

```bash
mkdir -p ~/.config/systemd/user
cp packaging/systemd/sys-investigate-sampler.service ~/.config/systemd/user/
cp packaging/systemd/sys-investigate-sampler.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sys-investigate-sampler.timer
systemctl --user list-timers sys-investigate-sampler.timer
```

> **Tip:** Pair this with a simple `logrotate` rule (or periodic truncation) for `~/.local/state/sysadmin-tools/investigate.jsonl` so long-running telemetry stays bounded, and run `./scripts/verify.sh` locally to mirror [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml).

---

## 4. Troubleshooting & FAQ

### Q1: Why do some processes show `0.00 MB/s` I/O or missing I/O in `sys-investigate`? (`PermissionError` on `/proc/*/io`)

Linux restricts `/proc/<pid>/io` to the owning UID (or `CAP_SYS_PTRACE` / `root`). When you run `sys-investigate` unprivileged, processes owned by `root` or other service accounts raise `PermissionError`. By design, `sys-investigate` fails open (`None` -> `0.00 MB/s`) and labels the section **`Top Process I/O (readable /proc/*/io)`** rather than crashing.
- **Fix:** If system-level disk I/O is high but `Top Process I/O` looks empty, re-run with `sudo ~/.local/bin/sys-investigate` to read root-owned daemons, VMs, and container processes.

### Q2: Will `sys-investigate` or `sys-disk-hogs` hang if an NFS, CIFS, SSHFS, or FUSE mount is wedged?

No, under normal operation both tools parse `/proc/mounts` first and **automatically skip** `os.statvfs()` on non-local/network/FUSE/automount filesystems (`nfs`, `nfs4`, `cifs`, `smb3`, `sshfs`, `fuse`, `fuseblk`, `fuse.*`, `autofs`, `glusterfs`, `ceph`, `virtiofs`, `9p`, etc.).
- **Extra protection:** If a *local* block mount itself is hung (e.g., failing NVMe or stalled ext4 journal), pass **`--no-statvfs`** to guarantee zero `statvfs()` syscalls.
- In `sys-disk-hogs`, `--one-file-system` is enabled by default so scanning `/` or `~` never crosses into mounted network/secondary filesystems unless you pass `--cross-mounts`.

### Q3: Why is `used_pct` equal to `null` (`None`) in JSON or omitted in text output?

`used_pct` (in `sys-investigate`) and `fs_used_pct` (in `sys-disk-hogs`) are intentionally `null` whenever:
1. You passed `--no-statvfs`.
2. The mount's `fstype` is classified as non-local/FUSE/network/automount.
3. `os.statvfs()` raised an `OSError` (e.g., permission denied or unmounted race).

### Q4: `sys-crash-triage` says `journalctl` timed out, is unavailable, or returned no boot history. What should I do?

1. **Permissions:** Ensure your user is in the `adm` or `systemd-journal` group (`groups`), or run with `sudo ~/.local/bin/sys-crash-triage`.
2. **Huge/slow journals:** Raise or lower `--journal-timeout SEC` (default `5.0`, range `0.5..30.0`) or lower `--tail-lines` (default `80`).
3. **Offline / degraded mode:** Run `sys-crash-triage --no-journalctl --verbose` to bypass `journalctl` completely and inspect `/proc`, `/sys`, `/var/log/kern.log`, `/var/log/syslog`, `/var/lib/systemd/pstore`, and `/var/crash` directly.

### Q5: I see `.lock` or `(stale .meta present)` files in `sys-io-guard --status`. Do I need to delete them?

- **`[IDLE]` `.lock` files are completely normal and harmless.** `sys-io-guard` deliberately never unlinks `.lock` files (unlinking an `flock` file creates a classic inode race where two processes lock two different inodes!). Because `flock` is kernel-enforced per open file descriptor, the kernel automatically releases the lock the instant a holder exits or crashes — even on `SIGKILL`.
- **`(stale .meta present)`** means a previous holder was killed hard (e.g. `kill -9`) before its `finally` block could unlink `<key>.meta`. It does **not** block new acquisitions; the next holder atomically overwrites `.meta`. You can safely ignore it or delete the `.meta` file while the lock is `[IDLE]`.

### Q6: Why does a `D`-state thread show `0` in the `WCHAN` column?

On Linux, reading `/proc/<pid>/task/<tid>/wchan` returns `"0"` when:
- The thread transitioned out of `D` state into running (`R`) between reading `task/<tid>/stat` and reading `task/<tid>/wchan`, **or**
- The thread is a kernel thread whose wait channel cannot be resolved (or is restricted by kernel hardening/config without symbol resolution).

This is normal race/kernel behavior; look at the dominant symbolic wait channels (`balance_dirty_pages`, `folio_wait_bit_common`, `io_schedule`, etc.) in the `Wchans:` summary line, or run `--progress-check` to confirm whether the PID remains blocked across consecutive windows.

### Q7: Why does a process show huge `WCH_M/s` (`wchar_mbs`) while `W_MB/s` (`write_bytes`) is `0.00`?

In `/proc/<pid>/io`, `wchar` counts **all** bytes written via `write()` syscalls — including pipes, Unix sockets, PTYs, and Wayland/X11 IPC (for example, a terminal emulator or GUI app can easily show 400+ MB/s `WCH_M/s` with `0.00 MB/s` actual disk writes). `W_MB/s` (`write_bytes`) tracks bytes actually sent to the storage layer. Always check `W_MB/s` (not `WCH_M/s`) when hunting disk saturation culprits.

### Q8: Why does `sys-investigate` `%CPU` differ from `ps aux`?

`ps aux` reports a process's **lifetime average** CPU since it started, which hides recent bursts in long-running daemons and exaggerates short startup spikes. `sys-investigate` computes **instantaneous `%CPU`** strictly from two-snapshot `/proc/<pid>/stat` tick deltas (`utime + stime`) divided by measured wall-clock `dt_sec`.

---

**Navigation:** [← Previous: Chapter 5 — Post-Crash Forensics](05-post-crash-forensics.md) | [Next: Chapter 7 — Network & Systemd Cgroup Watch →](07-network-and-systemd-watch.md) | [↑ Back to User's Guide](README.md)
