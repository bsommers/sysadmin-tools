# Checkpoint — sysadmin-tools v0.5.0 Local Release Ready — 2026-11-25

**Status:** COMPLETE (LOCAL) — v0.5.0 verified, committed, and tagged locally; awaiting confirmation for push/release/Pages enable

## User Goal & Verbatim Decisions
- **Goal:** Execute all `v0.5.0` roadmap and polish options in order:
  1. `sys-investigate --diff-log`, `sys-net-watch`, `sys-systemd-watch`
  2. GitHub Pages showcase site (`docs/index.html`)
  3. CI workflow (`.github/workflows/ci.yml`) & `systemd` user timer templates (`packaging/systemd/`)
  4. Read-only live operational host check
  5. Sync icons/diagrams/docs, verify everything, and release `v0.5.0`
- **Verbatim user instruction (prior session):** `"All in subagents in order. Feel free to run them without asking me to be granted me."`
- **Verbatim resume request (this session):** `Read AGENTS.md and docs/CHECKPOINT.md. Verify git status and baseline state, then execute Batch 2 & 3 (v0.5.0 icons/docs, GitHub Pages showcase, live operational host check, verification, and v0.5.0 release).`

## Authorizations & Scope
- Local edits, subagent dispatch, tests, local installation (`~/.local/bin`), local commits, and local tag `v0.5.0` completed cleanly.
- **Outward-facing actions gated for explicit user confirmation:** `git push origin main`, `git push origin v0.5.0`, `gh release create v0.5.0`, and enabling GitHub Pages (`gh api -X POST repos/bsommers/sysadmin-tools/pages -f build_type=workflow`).

## Done (with evidence)
- **Batch 1 Commit (`399626d`):** `feat: add sys-investigate --diff-log, sys-net-watch, sys-systemd-watch, CI & systemd templates`
  - `src/sysadmin_tools/sys_investigate.py` (`0.5.0`), `src/sysadmin_tools/sys_net_watch.py` (`0.5.0`), `src/sysadmin_tools/sys_systemd_watch.py` (`0.5.0`), `.github/workflows/ci.yml`, `packaging/systemd/*`, `tests/unit/test_packaging_templates.py`, `pyproject.toml`, `scripts/install.sh`.
- **Batch 2 Commit (`cc6f0d7`):** `docs: add GitHub Pages showcase site, v0.5.0 icons, and synced docs`
  - `scripts/render_assets.py` updated for 7 CLIs + `v0.5.0 · 166 TESTS PASS`; regenerated `assets/logo.{svg,png}`, `docs/assets/{logo.svg,logo.png,icon.svg}`, `docs/assets/icons/icon-*.svg` (7 icons including `icon-net-watch.svg` and `icon-systemd-watch.svg`), and `docs/assets/examples/{architecture-overview,incident-triage-workflow}.svg`.
  - Synced `README.md`, `CHANGELOG.md` (`## [v0.5.0]`), `docs/{README,purpose,architecture,running,how-to,BUILDSPEC}.md`, `docs/user-guide/{README,01,02,06,07-network-and-systemd-watch}.md`, and `docs/diagrams/{README.md,ascii_architecture.txt,architecture.mmd}`.
  - Created GitHub Pages site (`docs/index.html`, `docs/.nojekyll`) and hardened SHA-pinned `.github/workflows/pages.yml`.
  - Added `TestGitHubPagesShowcase` (3 tests) to `tests/unit/test_packaging_templates.py`.
- **Full Verification Gate:**
  - `./scripts/verify.sh`: PASS (`syntax` 0s, `unit` 143 tests in 5.125s, `smoke` 23 tests in 12.060s — **166 tests total**).
  - Asset/XML/HTML/Markdown checks: `ALL_ASSET_XML_HTML_MD_CHECKS_OK` (all `.svg` and `.drawio` XML-valid with zero external URLs or `/home/` paths; all `docs/index.html` and Markdown relative links valid).
  - `./scripts/install.sh --dry-run` + `diff -u` parity across all 7 `~/.local/bin/sys-*` binaries: `ALL_7_PARITY_OK`.
- **Read-Only Live Host Operational Check (All 7 Installed CLIs, zero tracebacks, `/datanapps` untouched):**
  - `sys-investigate` (`0.5.0`, exit `0`): `NORMAL_OR_IDLE` / `HEALTHY_IDLE_OR_LIGHT` (`us=4.1%`, `sy=0.9%`, `wa=0.1%`, `id=94.8%`, `load1=4.17` on 32 CPUs, PSI `0.0%`, NVMe `43.9°C` / `24.9°C` / `34.9°C`).
  - `sys-crash-triage` (exit `0`): `CLEAN_REBOOT` (kernel `7.0.0-44-generic`, uptime `2d 08h 07m`, `is_unclean=false`, AMD FCH reset `0x00300800` `CF9_FULL_RESET`).
  - `sys-net-watch` (`0.5.0`, exit `0`): `HEALTHY` (30 interfaces, `eno1` `rx=77.22 kbps` / `tx=99.91 kbps`, `0` link errors/drops/retransmits/softnet drops, `tcp_estab=87`, `close_wait=9`, `time_wait=71`).
  - `sys-systemd-watch` (`0.5.0`, exit `0`): `HEALTHY` (145 monitored cgroup units, `0` throttling/OOM/IO-pressure/crash-loop units, `d_state_pids=0`, `zombie_pids=3`, journal `3488.0 MiB` with `0.0 MiB/s` growth).
  - `sys-mem-watch` (exit `0`): `HEALTHY` (`mem_avail=108.33/183.66 GiB`, `swap_used=0.0/8.0 GiB`, `oom_kill_delta=0`, PSI `0.0%`).
  - `sys-disk-hogs` on `.` (exit `0`): `3.32 MiB` total (`1.02 MiB` `.git`, `0.85 MiB` reclaimable `__pycache__`, 0 skipped).
  - `sys-io-guard --status` (exit `0`): `0` active locks, `0` idle locks.

## Working-Tree & Process State
- **Repo path:** `~/src/sysadmin/sysadmin-tools`
- **Remote:** `origin` -> `https://github.com/bsommers/sysadmin-tools.git`
- **Branch:** `main` (ahead of `origin/main`, locally tagged `v0.5.0`)
- **Stashes / Background tasks / Subagents:** None running; clean working tree.

## Exact Next Commands (Outward-Facing — Pending User Confirmation)
```bash
git push origin main
git push origin v0.5.0
gh release create v0.5.0 --title "sysadmin-tools v0.5.0" --notes-from-tag
gh api -X POST repos/bsommers/sysadmin-tools/pages -f build_type=workflow
```
