# AGENTS.md — sysadmin-tools

Start every fresh session by reading:
1. `docs/CHECKPOINT.md` — current status, verified state, and exact next steps.
2. `docs/BUILDSPEC.md` — as-built contracts, non-negotiable invariants, and planned roadmap.
3. `docs/NOTES.md` — incident archaeology and pitfalls discovered during live testing.

## Non-Negotiable Constraints
- **Zero third-party runtime dependencies:** Python 3.10+ stdlib only.
- **No shelling out on the hot diagnostic path:** read `/proc` and `/sys` directly so tools stay responsive when disks are 99%+ saturated.
- **No symlinks to secondary disks for installed binaries:** `scripts/install.sh` installs direct copies into `~/.local/bin` (on root NVMe) so diagnostics work even if secondary mounts stall.
- **Verify locally before reporting done:** run `./scripts/verify.sh` and confirm all stages pass.
