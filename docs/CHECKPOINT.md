# Checkpoint — sysadmin-tools bootstrap & sys-investigate v0.1.0 — 2026-11-21

**Status:** IN PROGRESS — ready for resume in fresh agent session

## User Decisions & Intent (verbatim)
1. Diagnose what was driving up CPU/load and whether it was productive.
2. Build a reusable investigator script to re-run later.
3. Add it to a `sysadmin-tools` repo in `~/src/sysadmin/sysadmin-tools` with a checkpoint, notes, and buildspec for pickup in a new agent session.

## Done (with evidence)
- Built `src/sysadmin_tools/sys_investigate.py` (pure `/proc` + `/sys`, zero third-party deps, zero external subprocess calls during sampling).
- Scaffolded `~/src/sysadmin/sysadmin-tools` with `pyproject.toml`, `.gitignore`, `README.md`, `AGENTS.md`, `tests/unit/`, `tests/smoke/`, `scripts/install.sh`, `scripts/verify.sh`, and `docs/{CHECKPOINT,NOTES,BUILDSPEC}.md`.
- Verified live diagnosis against active NVMe saturation incident (`IO_WAIT_SATURATION` / `THRASHING_LOW_EFFICIENCY`, `Dirty=6.14–13.9 GiB`, `nvme2n1` `aqu-sz=39–516`, `balance_dirty_pages` stalls).
- Installed standalone executable copy to `~/.local/bin/sys-investigate` (root NVMe `/` — independent of `/datanapps`).
- Legacy first prototype still exists at `/datanapps/tools/sys-investigate/` (safe to retire or keep as archive after confirming new repo).

## Bugs Fixed This Session (do not reintroduce)
1. **`total_disk_mbs` reported `0.0 MB/s` during `PRODUCTIVE_IO`:** when rolling 10s PSI (`io_some10 >= 30%`) triggered `IO_WAIT_SATURATION` while instantaneous `sat_disks` was empty (`util=72.6%`, `r_mbs=118.7`), summing only `sat_disks` yielded `0.0`. Fixed to `sum(d["r_mbs"] + d["w_mbs"] for d in (sat_disks or disks))` and locked with `test_productive_io_uses_all_disks_when_none_saturated`.
2. **Symlinking installed CLI onto stalled secondary SSD:** symlinking `~/.local/bin/sys-investigate -> /datanapps/...` risks stalling startup when `/datanapps` (`nvme2n1`) is wedged. Changed `scripts/install.sh` to `install -m 0755` (direct copy onto root NVMe).

## Working-Tree & Git State
- Repo path: `~/src/sysadmin/sysadmin-tools`
- Git initialized on branch `main`; **no commit created yet** and **no remote configured** (left uncommitted pending user confirmation).
- No background processes/watchers started by this session.

## State That May Have Changed
- Live host I/O contention on `/datanapps` (`nvme2n1`) fluctuates with concurrent `claude`/`herdr` builds (`scripts/verify.sh` / `cargo` / `rust-lld` / `sdxl_gen.py`).
- Re-run `./scripts/verify.sh` and `sys-investigate --interval 2` before making edits.

## Next Steps (exact commands, in order)
1. Verify repo health and baseline tests:
   ```bash
   cd ~/src/sysadmin/sysadmin-tools
   git status -sb
   ./scripts/verify.sh
   ```
2. Verify installed binary matches repo source:
   ```bash
   diff -u src/sysadmin_tools/sys_investigate.py ~/.local/bin/sys-investigate || ./scripts/install.sh
   sys-investigate --interval 2 --top 10
   ```
3. Make initial git commit (once user confirms) and optionally clean up legacy prototype `/datanapps/tools/sys-investigate/`.
4. Pick next capability from `docs/BUILDSPEC.md` Roadmap (e.g., `--diagnose-pid`, NVMe/SMART & thermal telemetry, cgroup/docker attribution, historical JSONL trend recorder, or companion remediation helper).

## Open Questions for Next Session
- Should `sysadmin-tools` get a GitHub remote, or remain local-only for now?
- Do you want to retire `/datanapps/tools/sys-investigate/` once committed here?
- Which additional sysadmin investigators should join `sysadmin-tools` next (e.g., disk/inode hog finder, swap/RSS growth tracker, systemd/daemon health inspector, Cargo/build contention guard)?
