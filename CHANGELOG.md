# Changelog

All notable changes to `sysadmin-tools` are documented in this file.

The format adheres to [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.4.0] - 2026-11-22

### Added
- **`sys-crash-triage` CLI** (`src/sysadmin_tools/sys_crash_triage.py`): standalone zero-dependency Linux post-crash and unexpected-reboot forensic investigator:
  - Boot timeline, kernel version change, and downtime-gap analysis (`--boot`, `--boots-history`, `--tail-lines`).
  - Hardware reset-reason decoder for AMD FCH `S5_RESET_STATUS` (`0xCF9` warm/full reset, `sync_flood`, `thermal_trip`, `watchdog_timeout`, `ac_power_loss`, `sleep_state_power_loss`, ACPI transitions).
  - Unclean-shutdown detection (journal corruption, filesystem recovery, NUL log gaps).
  - Crash signature scanner covering kernel panic/oops, MCE/EDAC/AER hardware errors, thermal trips, OOM killer, watchdog/hung-task lockups, GPU faults, and NVMe/filesystem storage faults, with high-volume audit/Bluetooth noise filtering.
  - Crash-capture readiness audit (`efi_pstore`, `/var/lib/systemd/pstore`, `kdump-tools` `USE_KDUMP` vs `crashkernel=`, `/var/crash`, EDAC ECC counters, `unattended-upgrades` correlation).
  - Offline/degraded `--no-journalctl` mode, bounded `--journal-timeout`, `--show-steps` manual runbook, deterministic `--json` / `--markdown`, `--append-log` JSONL, and `--exit-on-issue` (exit `3`).
  - 14 unit tests (`tests/unit/test_sys_crash_triage.py`) and 3 live smoke tests (`tests/smoke/test_crash_triage_smoke.py`).
- **Complete Documentation Suite & Multi-Chapter User's Guide**:
  - Core docs: `docs/README.md`, `docs/purpose.md`, `docs/architecture.md`, `docs/running.md`, `docs/how-to.md`.
  - 6-chapter User's Guide under `docs/user-guide/` (`README.md`, `01-getting-started.md` through `06-automation-and-troubleshooting.md`).
  - Multi-format diagram catalog under `docs/diagrams/` (ASCII `ascii_architecture.txt`, Mermaid `*.mmd`, native draw.io XML `*.drawio`).
- **Brand, Icon & Visual Showcase Kit** (`scripts/render_assets.py`):
  - Futuristic obsidian-black + light-blue neon grid hero banner (`docs/assets/logo.svg`, `docs/assets/logo.png`), square app/repo icon (`assets/logo.svg`, `assets/logo.png`, `docs/assets/icon.svg`), 5 cohesive `64x64` CLI icons (`docs/assets/icons/icon-*.svg`), and 2 architecture/workflow showcase graphics (`docs/assets/examples/*.svg`), linked from root `README.md` and `docs/`.

### Changed
- Updated `pyproject.toml`, `src/sysadmin_tools/__init__.py`, and `scripts/install.sh` to package and install all 5 standalone CLIs (`v0.4.0`).
- Expanded verification baseline to **106 passing tests** (`syntax` 0s, `unit` 91 tests, `smoke` 15 tests via `./scripts/verify.sh`).

## [v0.3.0] - 2026-11-21

### Added
- **`sys-io-guard`** (`src/sysadmin_tools/sys_io_guard.py`): per-disk `fcntl.flock` build serializer using `/proc/mounts` + `/sys/block/dm-*/slaves`, tmpfs/root lock dir, atomic `.meta` holder inspection (`--status`), `--dry-run`, `--timeout` (exit `4`), `CARGO_BUILD_JOBS` cap, `--nice`, and `--env`.
- **`sys-mem-watch`** (`src/sysadmin_tools/sys_mem_watch.py`): pure `/proc` two-snapshot RSS/swap growth, reclaim/PSI, and OOM-risk investigator with 5-status verdict classifier, `--watch`, `--append-log`, and `--exit-on-issue`.
- **`sys-disk-hogs`** (`src/sysadmin_tools/sys_disk_hogs.py`): iterative `os.scandir` workspace/target/cache disk usage analyzer with hardlink deduplication, single-filesystem default (`--one-file-system`), safe `statvfs` / `--no-statvfs`, regenerable build/cache hog classification, and non-reclaimable `.git` tracking.
- **`sys-investigate` v0.3.0 telemetry**: `--diagnose-pid PID` process/subtree drill-down, pure `/sys` NVMe thermal telemetry + thermal alerting in `classify_verdict()`, and bounded `/proc/<pid>/cgroup` attribution (`docker:`, `podman:`, `containerd:`, `systemd:`).

### Changed
- Updated `scripts/install.sh` and `scripts/verify.sh` to install and syntax-check all standalone CLIs automatically.

## [v0.2.0] - 2026-11-21

### Added
- **Mount-safety hardening**: `is_safe_statvfs_fstype()` skips non-local/FUSE/network/automount filesystems before `os.statvfs()`, plus `--no-statvfs` flag.
- **Process-to-mount attribution**: bounded `/proc/<pid>/{cwd,fd}` `os.readlink()` resolution for `top_io` and `d_state_top` offenders.
- **Two-window stall verification (`--progress-check`)**: compares consecutive sampling windows to distinguish transient spikes from confirmed stalls.
- **JSONL telemetry logging (`--append-log PATH`)**: appends deterministic compact JSON lines per report.

### Fixed
- Single-process `THRASHING_LOW_EFFICIENCY` and `WEDGED_OR_STALLED` verdicts now always emit actionable recommendations instead of falling through to `"No immediate intervention needed."`.
- Deep-queue high-latency writes (`w_await >= 250ms` and `aqu_sz >= 16`) now classify as `THRASHING_LOW_EFFICIENCY` even when `util_pct < 90%`.

## [v0.1.0] - 2026-11-21

### Added
- Initial `sysadmin-tools` repository scaffold (`pyproject.toml`, `AGENTS.md`, `scripts/install.sh`, `scripts/verify.sh`, `docs/{CHECKPOINT,NOTES,BUILDSPEC}.md`).
- **`sys-investigate` v0.1.0**: pure `/proc` + `/sys` two-snapshot CPU/load/PSI/memory/disk/GPU investigator, thread-level `D`-state + `wchan` aggregation, overlapping workload detector, and deterministic productivity verdict engine.
- Standalone root-NVMe installation (`install -m 0755`) avoiding symlinks to secondary disks.

### Fixed
- `total_disk_mbs` falls back to all physical disks (`sat_disks or disks`) when rolling PSI triggers `IO_WAIT_SATURATION` without an instantaneously saturated disk.
