# Checkpoint — sysadmin-tools v0.5.0 Batch 1 Complete (Session Handoff) — 2026-11-24

**Status:** IN PROGRESS — v0.5.0 Batch 1 committed (`399626d`), verified, and installed to `~/.local/bin`; ready for Batch 2 & 3 in fresh session

## User Goal & Verbatim Decisions
- **Goal:** Execute all `v0.5.0` roadmap and polish options in order:
  1. `sys-investigate --diff-log`, `sys-net-watch`, `sys-systemd-watch`
  2. GitHub Pages showcase site (`docs/index.html`)
  3. CI workflow (`.github/workflows/ci.yml`) & `systemd` user timer templates (`packaging/systemd/`)
  4. Read-only live operational host check
  5. Sync icons/diagrams/docs, verify everything, and release `v0.5.0`
- **Verbatim user instruction:** `"All in subagents in order. Feel free to run them without asking me to be granted me."`
- **Verbatim handoff decision:** `"1"` (commit Batch 1 + refresh checkpoint + start a fresh session before running Batch 2 & 3).

## Authorizations & Scope
- Local edits, subagent dispatch, tests, local installation (`~/.local/bin`), and local commits were authorized unattended before session reset.
- **Re-confirm outward-facing actions after reset:** ask/confirm before `git push origin main`, `git push origin v0.5.0`, `gh release create v0.5.0`, or enabling GitHub Pages via `gh api` unless the user explicitly re-authorizes pushing/releasing in the fresh session.

## Done (with evidence)
- **Batch 1 Commit:** `399626d` (`feat: add sys-investigate --diff-log, sys-net-watch, sys-systemd-watch, CI & systemd templates`) on `main`.
  - `src/sysadmin_tools/sys_investigate.py` (`VERSION = "0.5.0"`): added `--diff-log PATH`, `--diff-baseline PATH`, `--diff-window N`, `--version` (`40/40` tests pass: 35 unit + 5 smoke).
  - `src/sysadmin_tools/sys_net_watch.py` (`VERSION = "0.5.0"`): standalone pure `/proc/net/{dev,tcp,tcp6,udp,udp6,snmp,softnet_stat}` investigator, zero subprocesses (`22/22` tests pass: 18 unit + 4 smoke; live run healthy).
  - `src/sysadmin_tools/sys_systemd_watch.py` (`VERSION = "0.5.0"`): standalone pure `/sys/fs/cgroup` + `/proc` + journal stat unit pressure/crash-loop/burst investigator, zero subprocesses (`21/21` tests pass: 18 unit + 3 smoke; live run healthy).
  - `.github/workflows/ci.yml`: hardened CI workflow (`permissions: { contents: read }`, `concurrency` w/ `cancel-in-progress: true`, `timeout-minutes: 10`, `paths-ignore`, SHA-pinned `actions/checkout` `v4` and `actions/setup-python` `v5`) + `packaging/systemd/sys-investigate-sampler.{service,timer}` + `packaging/systemd/README.md` + `tests/unit/test_packaging_templates.py` (`6/6` tests pass).
  - Packaging & install wired to `v0.5.0`: `pyproject.toml`, `src/sysadmin_tools/__init__.py`, `scripts/install.sh`.
- **Full Verification & Install Parity:**
  - `./scripts/verify.sh`: PASS (`[PASS] syntax (0s)`, `[PASS] unit (5s)`, `[PASS] smoke (10s)`).
  - `./scripts/install.sh`: PASS — all 7 CLIs installed in `~/.local/bin/` with `diff -u` parity `0` (`ALL_7_PARITY_OK`):
    `sys-investigate`, `sys-io-guard`, `sys-mem-watch`, `sys-disk-hogs`, `sys-crash-triage`, `sys-net-watch`, `sys-systemd-watch`.

## Working-Tree & Process State
- **Repo path:** `~/src/sysadmin/sysadmin-tools`
- **Remote:** `origin` -> `https://github.com/bsommers/sysadmin-tools.git`
- **Branch:** `main` (ahead of `origin/main` by `399626d` + this checkpoint commit; prior release tag `v0.4.0` is pushed at `28166b4`).
- **Stashes / Background tasks / Subagents:** None (0 running tasks or subagents; clean working tree once checkpoint is committed).

## Pitfalls & Guardrails
- Keep every CLI in `src/sysadmin_tools/*.py` strictly standalone (zero `sysadmin_tools.*` cross-imports) so `~/.local/bin/*` direct copies remain self-contained.
- **Do NOT run `sys-disk-hogs` against `/datanapps`** during live checks — secondary NVMe (`/datanapps`) can stall under writeback load.
- `sys-investigate` uses `--watch 1` and `--no-statvfs` (not `--samples` or `--no-fs-stat`).
- Resolve exact commit SHAs via `gh api` when pinning GitHub Actions in `.github/workflows/pages.yml` (never guess SHAs).

## Exact Next Steps (Batch 2 & 3, in order)
1. **Verify baseline in fresh session:**
   ```bash
   cd ~/src/sysadmin/sysadmin-tools && git status --short && git log --oneline -n 5 && ./scripts/verify.sh
   ```
2. **Dispatch 3 parallel disjoint `self` subagents (Batch 2):**
   - **Subagent 2A (`Model: "flash"` — icons, showcase SVGs, and `v0.5.0` docs sync):**
     - Update `scripts/render_assets.py` to generate `docs/assets/icons/icon-net-watch.svg` and `docs/assets/icons/icon-systemd-watch.svg` (`64x64`), update hero/architecture/workflow SVGs for 7 CLIs (`v0.5.0`), and run `python3 scripts/render_assets.py` to regenerate PNGs (`docs/assets/logo.png`, `assets/logo.png`).
     - Sync `README.md`, `CHANGELOG.md` (`## [0.5.0]`), `docs/{README,purpose,architecture,running,how-to,BUILDSPEC}.md`, `docs/user-guide/`, and `docs/diagrams/{README.md,ascii_architecture.txt,architecture.mmd}` for all 7 CLIs, `--diff-log`, CI/systemd sampler, and GitHub Pages link (`https://bsommers.github.io/sysadmin-tools/`). Remove implemented items from `docs/BUILDSPEC.md` §6.
     - Leave `docs/index.html`, `docs/.nojekyll`, `.github/workflows/pages.yml`, and `docs/CHECKPOINT.md` untouched. Zero absolute `/home/` paths in Markdown/SVG files.
   - **Subagent 2B (`Model: "inherit"` — GitHub Pages showcase site):**
     - Read `building-github-pages-sites/SKILL.md`.
     - Create `docs/index.html` (obsidian `#04070f` & neon cyan `#00e5ff` / emerald `#00ff9d` grid, zero external CDN URLs/fonts/scripts, cards for all 7 CLIs with `assets/icons/icon-*.svg`, showcase SVGs, install & systemd quickstart), `docs/.nojekyll`, and hardened SHA-pinned `.github/workflows/pages.yml`.
   - **Subagent 2C (`Model: "flash"` — read-only live host check):**
     - Run installed `~/.local/bin/` binaries (`sys-investigate --interval 1 --watch 1 --no-statvfs --json`, `sys-crash-triage --no-journalctl --json`, `sys-net-watch --interval 0.5 --samples 2 --top 5 --json`, `sys-systemd-watch --interval 0.5 --samples 2 --top 5 --json`, `sys-mem-watch --interval 0.5 --samples 2 --top 5 --json`, `sys-disk-hogs --max-depth 2 --top 5 --json .`). Do NOT touch `/datanapps`.
3. **Full Verification Gate:**
   - `./scripts/verify.sh`
   - XML parse check (`xml.etree.ElementTree.parse`) + no external URLs on all SVGs and `.drawio` files
   - Validate all `docs/index.html` asset links + relative Markdown links (`0` broken links, `0` `/home/` paths)
   - Re-verify `tests/unit/test_packaging_templates.py` and `.github/workflows/pages.yml`
4. **Update `docs/CHECKPOINT.md` (to `COMPLETE`), commit Batch 2/3, and confirm before outward-facing `v0.5.0` push/release/Pages enable:**
   - Commit Batch 2 assets/docs/Pages (`docs: add GitHub Pages showcase site, v0.5.0 icons, and synced docs`), update `docs/CHECKPOINT.md`, commit (`chore(release): prepare release v0.5.0`), tag `v0.5.0`, and (once confirmed) push `main` + `v0.5.0`, run `gh release create v0.5.0 --title "sysadmin-tools v0.5.0" --notes-from-tag`, and configure GitHub Pages (`gh api -X POST repos/bsommers/sysadmin-tools/pages -f build_type=workflow`).

## Exact Resume Prompt (for fresh session)
```text
Read AGENTS.md and docs/CHECKPOINT.md. Verify git status and baseline state, then execute Batch 2 & 3 (v0.5.0 icons/docs, GitHub Pages showcase, live operational host check, verification, and v0.5.0 release).
```
