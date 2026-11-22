# sysadmin-tools

Zero-dependency Linux sysadmin investigation and diagnostic CLIs designed to remain responsive during severe CPU, memory, or storage stalls.

## Layout

| Path | Purpose |
|---|---|
| `src/sysadmin_tools/sys_investigate.py` | Pure `/proc` + `/sys` live CPU/load/I/O/memory investigator & productivity classifier |
| `tests/unit/` | Offline unit + CLI argument validation tests |
| `tests/smoke/` | Live smoke tests against `/proc` and `/sys` |
| `scripts/install.sh` | Installs standalone executable copies into `~/.local/bin` (safe on root NVMe) |
| `scripts/verify.sh` | Composable verification runner (`syntax`, `unit`, `smoke`) writing reports to `target/verify/` |
| `docs/CHECKPOINT.md` | Active session handoff checkpoint for resuming in a fresh agent session |
| `docs/NOTES.md` | Incident archaeology, engineering notes, and design gotchas |
| `docs/BUILDSPEC.md` | As-built architecture, contracts, verdict rules, and expansion spec |

## Quickstart

```bash
# Run verification suite
./scripts/verify.sh

# Install standalone binary into ~/.local/bin/sys-investigate
./scripts/install.sh

# Run live investigation
sys-investigate
sys-investigate --interval 2 --top 15
sys-investigate --json | jq '.verdict'
sys-investigate --markdown
sys-investigate --watch 5 --interval 3
sys-investigate --exit-on-issue --json > /tmp/sys-diag.json
```

## Status

Initial repository scaffold and `sys-investigate` v0.1.0 implemented and verified; ready for handoff and expansion (see `docs/CHECKPOINT.md` and `docs/BUILDSPEC.md`).
