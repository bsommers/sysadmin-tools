# sysadmin-tools

Zero-dependency Linux sysadmin investigation and diagnostic CLIs designed to remain responsive during severe CPU, memory, or storage stalls.

## Tools

| CLI | Source | Purpose |
|---|---|---|
| `sys-investigate` | `src/sysadmin_tools/sys_investigate.py` | Pure `/proc` + `/sys` live CPU/load/I/O/memory/NVMe-thermal investigator, productivity classifier, mount/cgroup attributor, `--progress-check`, and `--diagnose-pid` drill-down |
| `sys-io-guard` | `src/sysadmin_tools/sys_io_guard.py` | Per-disk `fcntl.flock` build serializer (locks on tmpfs/root) with `CARGO_BUILD_JOBS` cap and `nice` throttling to prevent SSD writeback collapse |
| `sys-mem-watch` | `src/sysadmin_tools/sys_mem_watch.py` | Pure `/proc` two-snapshot RSS/swap growth, reclaim/PSI, and OOM-risk investigator |
| `sys-disk-hogs` | `src/sysadmin_tools/sys_disk_hogs.py` | Pure-stdlib `os.scandir` workspace/target/cache disk usage analyzer with hardlink dedup and regenerable-cache classification |

## Layout

| Path | Purpose |
|---|---|
| `src/sysadmin_tools/` | Standalone Python 3.10+ stdlib-only CLI scripts (no third-party runtime deps, no cross-module imports) |
| `tests/unit/` | Offline unit and CLI error-path tests (77 tests) |
| `tests/smoke/` | Live smoke tests against real Linux `/proc`, `/sys`, and filesystems (12 tests) |
| `scripts/install.sh` | Installs standalone executable copies into `~/.local/bin` (safe on root NVMe — never symlinks to secondary disks) |
| `scripts/verify.sh` | Composable verification runner (`syntax`, `unit`, `smoke`) writing reports to `target/verify/` |
| `docs/CHECKPOINT.md` | Active session handoff checkpoint |
| `docs/NOTES.md` | Incident archaeology, engineering notes, and design gotchas |
| `docs/BUILDSPEC.md` | As-built architecture, contracts, verdict rules, and expansion roadmap |

## Quickstart

```bash
# Run verification suite (syntax + unit + smoke)
./scripts/verify.sh

# Install all standalone binaries into ~/.local/bin/
./scripts/install.sh

# Live system & single-PID investigation
sys-investigate
sys-investigate --interval 2 --top 15
sys-investigate --progress-check --interval 2
sys-investigate --diagnose-pid 12345 --interval 2
sys-investigate --no-statvfs --append-log /var/tmp/sys-investigate.jsonl
sys-investigate --json | jq '.verdict'

# Serialize heavy builds per target physical SSD
sys-io-guard --path /datanapps/cargo-target -- cargo test
sys-io-guard --status
sys-io-guard --timeout 30 --cargo-jobs 4 -- bash scripts/verify.sh

# Track memory growth, swap, and OOM candidates
sys-mem-watch --interval 2 --top 15
sys-mem-watch --exit-on-issue --append-log /var/tmp/sys-mem.jsonl --json

# Audit workspace disk hogs & reclaimable build/cache dirs
sys-disk-hogs ~/src /datanapps --top 20 --depth 2
sys-disk-hogs . --markdown
```

## Status

`sysadmin-tools` v0.3.0 implemented, installed, verified (89 tests passing: 77 unit + 12 smoke), and pushed to `origin/main` (see `docs/CHECKPOINT.md` and `docs/BUILDSPEC.md`).
