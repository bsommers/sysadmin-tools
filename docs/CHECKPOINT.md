# Checkpoint — sysadmin-tools v0.3.0 (full roadmap suite) — 2026-11-21

**Status:** COMPLETE — all roadmap items implemented, verified, installed, and pushed to `origin/main`

## Done (with evidence)
- Created public GitHub remote `https://github.com/bsommers/sysadmin-tools.git` and pushed `main`.
- Implemented all roadmap items in order (zero third-party deps, standalone Python 3.10+ stdlib executables):
  1. **`sys-io-guard`** (`src/sysadmin_tools/sys_io_guard.py`): per-disk `fcntl.flock` build serializer using `/proc/mounts` + `/sys/block/dm-*/slaves`, tmpfs/root lock dir, `.meta` holder inspection (`--status`), `--dry-run`, `--timeout` (exit `4`), `CARGO_BUILD_JOBS` cap, `--nice`, `--env`. (13 unit + 2 smoke tests)
  2. **`sys-mem-watch`** (`src/sysadmin_tools/sys_mem_watch.py`): pure `/proc` two-snapshot RSS/swap growth, reclaim/PSI, and OOM-risk investigator with 5-status classifier, `--watch`, `--append-log`, `--exit-on-issue`. (21 unit + 3 smoke tests)
  3. **`sys-disk-hogs`** (`src/sysadmin_tools/sys_disk_hogs.py`): pure-stdlib iterative `os.scandir` analyzer with hardlink dedup, single-filesystem default (`--one-file-system`), safe `statvfs` / `--no-statvfs`, regenerable build/cache hog classification (`rust-target`, `node-modules`, `python-venv`, `python-cache`, `build-artifacts`, `general-cache`), and non-reclaimable `.git` tracking. (15 unit + 3 smoke tests)
  4. **`sys-investigate` v0.3.0** (`src/sysadmin_tools/sys_investigate.py`): added cgroup/container attribution (`docker:`, `podman:`, `containerd:`, `systemd:`), pure `/sys` NVMe/SMART thermal telemetry + thermal alerting in `classify_verdict`, and `--diagnose-pid PID` single-process/subtree drill-down. (28 unit + 4 smoke tests)
- Updated packaging & scripts: `pyproject.toml` (v0.3.0, all 4 console scripts), `src/sysadmin_tools/__init__.py`, `scripts/install.sh` (installs all 4 standalone copies), `scripts/verify.sh` (auto-discovers all Python files).
- Updated docs: `README.md`, `docs/BUILDSPEC.md`, `docs/NOTES.md`, `docs/CHECKPOINT.md`.
- Verified locally via `./scripts/verify.sh`: `syntax` PASS (0s), `unit` PASS (77 tests in 2.955s), `smoke` PASS (12 tests in 6.003s) — **89/89 tests passing**.
- Installed all 4 standalone CLIs into `~/.local/bin/{sys-investigate,sys-io-guard,sys-mem-watch,sys-disk-hogs}` via `./scripts/install.sh` and verified byte-identical parity (`diff -u` exit `0` for all 4).

## Working-Tree & Git State
- Repo path: `~/src/sysadmin/sysadmin-tools`
- Remote: `origin` -> `https://github.com/bsommers/sysadmin-tools.git`
- Branch: `main` (clean, pushed to `origin/main`).
- No background processes/watchers running.

## Next Steps (exact commands, in order)
1. Verify repo & installed CLI health:
   ```bash
   cd ~/src/sysadmin/sysadmin-tools
   git status -sb
   ./scripts/verify.sh
   ```
2. Optional future expansions from `docs/BUILDSPEC.md` §6 (`sys-systemd-watch`, `sys-net-watch`, or `sys-investigate --diff-log`).
