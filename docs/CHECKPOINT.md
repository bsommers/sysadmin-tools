# Checkpoint — sysadmin-tools `sys-investigate` v0.2.0 — 2026-11-21

**Status:** COMPLETE — ready for next roadmap tool or feature

## Done (with evidence)
- Created initial git commit `bf90d69` (`chore: bootstrap sysadmin-tools repo with sys-investigate v0.1.0`) on `main` (local-only, no remote configured).
- Retired legacy prototype directory `/datanapps/tools/sys-investigate/`.
- Upgraded `src/sysadmin_tools/sys_investigate.py` to v0.2.0:
  - **Mount-safety hardening:** `is_safe_statvfs_fstype()` skips non-local/FUSE/network/automount filesystems (`nfs*`, `cifs`, `sshfs`, `fuse*`, `autofs`, etc.) before `os.statvfs()`; added `--no-statvfs` flag.
  - **Process-to-mount attribution:** `resolve_mount_for_path()` + `attribute_pid_mounts()` inspect `/proc/<pid>/cwd` and bounded `/proc/<pid>/fd/*` (`max_fds=128`) via `os.readlink()` (zero target-FS I/O) only for `top_io` and `d_state_top`, rendered in a `MOUNTS` column and exported in JSON.
  - **Two-window stall verification (`--progress-check`):** `compare_windows()` + `investigate_progress()` run two consecutive windows and classify `CONFIRMED_STALL` / `PERSISTENT_THRASH` / `RECOVERING` / `WORSENING` / `STEADY_PROGRESS` / `IDLE` with `dirty_gib_delta`, cumulative turnover, and persistent `D`-state / `>=50%` CPU PIDs.
  - **JSONL telemetry logging (`--append-log PATH`):** `append_jsonl_log()` appends one deterministic compact JSON line per report, creating parent directories as needed.
  - **Classifier fixes:** guaranteed actionable recommendations whenever `is_issue=True`, and classified high write latency + deep queue (`w_await >= 250ms` and `aqu_sz >= 16`) as `THRASHING_LOW_EFFICIENCY` even if `util_pct < 90%`.
- Synchronized `pyproject.toml`, `src/sysadmin_tools/__init__.py`, `README.md`, `docs/BUILDSPEC.md`, and `docs/NOTES.md`.
- Verified locally with `./scripts/verify.sh`: `syntax` PASS (0s), `unit` PASS (20 tests, 0.217s), `smoke` PASS (3 tests, 2.565s).
- Reinstalled standalone copy to `~/.local/bin/sys-investigate` via `./scripts/install.sh` and verified byte-identical parity (`diff -u` exit `0`).

## Bugs Fixed This Session (do not reintroduce)
1. **Empty recommendation on single-process `THRASHING_LOW_EFFICIENCY` / `WEDGED_OR_STALLED`:** previously fell through to `"No immediate intervention needed."` when no overlapping workload group matched. Fixed with targeted writer/mount recommendations and locked with `test_thrashing_without_overlaps_never_emits_no_intervention` and `test_wedged_stalled_emits_actionable_recommendation`.
2. **Severe write latency (`1388.5ms`) + deep queue (`aqu_sz=202.71`) misclassified as `PRODUCTIVE_IO`:** when `util_pct` briefly dipped to `82.4%` (`< 90%`) and `dirty_gib` was `1.65 GiB` (`< 2.0 GiB`), `thrashing` evaluated `False`. Fixed by adding `(worst_w_await >= 250.0 and worst_aqu >= 16.0)` and locked with `test_high_write_latency_and_queue_depth_classified_as_thrashing`.

## Working-Tree & Git State
- Repo path: `~/src/sysadmin/sysadmin-tools`
- Branch: `main` (local-only, no remote configured).
- All changes committed cleanly; no background processes/watchers running.

## Next Steps (exact commands, in order)
1. Verify repo & installed CLI health:
   ```bash
   cd ~/src/sysadmin/sysadmin-tools
   git status -sb
   ./scripts/verify.sh
   ```
2. Pick next capability from `docs/BUILDSPEC.md` Roadmap:
   - Companion CLIs: `sys-mem-watch`, `sys-io-guard`, or `sys-disk-hogs`.
   - Or additional `sys-investigate` features: `--diagnose-pid PID`, NVMe/SMART thermal telemetry, or cgroup/container attribution.
