# Architecture Diagrams — `sysadmin-tools` (v0.4.0)

Multi-format architecture, execution-flow, state-machine, and storage-isolation diagrams for the `sysadmin-tools` suite (Python 3.10+ stdlib only, zero third-party runtime dependencies).

Navigation: [Docs Index](../README.md) | [Repository README](../../README.md)

## Diagram Catalog

| File | Format | Diagram Type | Scope |
| --- | --- | --- | --- |
| [ascii_architecture.txt](./ascii_architecture.txt) | ASCII (`.txt`) | 4 terminal schematics | Storage isolation, two-snapshot delta sampling, `sys-io-guard` per-disk flock, `sys-crash-triage` forensic pipeline |
| [architecture.mmd](./architecture.mmd) | Mermaid (`.mmd`) | `flowchart TD` | Full 5-CLI suite, root-NVMe installation, `/proc` & `/sys` kernel interfaces, JSON/text output & exit-code contracts |
| [execution_flow.mmd](./execution_flow.mmd) | Mermaid (`.mmd`) | `sequenceDiagram` | Monotonic two-snapshot sampling (`sys-investigate` / `sys-mem-watch`) and `sys-io-guard` concurrent lock acquisition/timeout handoff |
| [verdict_states.mmd](./verdict_states.mmd) | Mermaid (`.mmd`) | `stateDiagram-v2` | `sys-investigate` bottleneck & productivity state classification and `sys-crash-triage` verdict classification |
| [architecture.drawio](./architecture.drawio) | draw.io (`.drawio`) | Swimlane architecture | End-to-end 4-lane architecture (Installation & CLI, Core Engines, Kernel/OS Interfaces, Verdicts & Exit Codes) |
| [execution_flow.drawio](./execution_flow.drawio) | draw.io (`.drawio`) | Lifecycle diagram | Two-snapshot delta sampling pipeline and `sys-io-guard` lock-guard contention lifecycle |
| [storage_and_isolation.drawio](./storage_and_isolation.drawio) | draw.io (`.drawio`) | Fault-domain isolation | Why root NVMe (`~/.local/bin`) and tmpfs lockdir (`$XDG_RUNTIME_DIR`) stay responsive during secondary NVMe stalls and hung FUSE/NFS mounts |

## Viewing Instructions

### Terminal (ASCII)
```bash
cat docs/diagrams/ascii_architecture.txt
less -S docs/diagrams/ascii_architecture.txt
```
All ASCII diagrams are formatted within 80–100 columns for clean rendering in SSH sessions, tmux panes, and narrow terminals.

### Mermaid (`.mmd`)
- **GitHub:** Open any `.mmd` file directly in GitHub's web viewer for native diagram rendering.
- **Mermaid Live Editor:** Paste the contents of any `.mmd` file into [mermaid.live](https://mermaid.live) to inspect or export SVG/PNG.
- **VS Code:** Install *Markdown Preview Mermaid Support* or *Mermaid Chart* extension to preview `.mmd` files locally.

### draw.io (`.drawio`)
- **Web:** Open [app.diagrams.net](https://app.diagrams.net) and choose **File -> Open from -> Device...** to load any `.drawio` file.
- **VS Code:** Install the *Draw.io Integration* (`hediet.vscode-drawio`) extension to view and edit `.drawio` diagrams directly inside the editor.
- **CLI Export (optional):** If `drawio` desktop CLI is installed, export to SVG/PNG with `drawio -x -f svg docs/diagrams/architecture.drawio`.

## Suite Invariants Reflected in These Diagrams
- **Direct-copy root-NVMe installation:** `scripts/install.sh` copies all 5 standalone CLIs into `~/.local/bin` (never symlinks to secondary disks).
- **Hot-path kernel reads:** Diagnostic hot paths read `/proc` and `/sys` directly without shelling out (except optional bounded `journalctl` in `sys-crash-triage`, disabled via `--no-journalctl`).
- **Unified exit-code contract:** `0` ok, `1` validation/runtime error, `2` argparse usage error, `3` issue detected (`--exit-on-issue`), `4` lock busy/timeout (`sys-io-guard`).
