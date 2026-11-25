# sysadmin-tools Documentation Hub

[← Back to Project README](../README.md) · [Live Showcase](https://bsommers.github.io/sysadmin-tools/)

Complete technical documentation for **`sysadmin-tools` v0.5.0** — seven standalone, zero-dependency Python 3.10+ Linux diagnostic and workload-guard CLIs (`sys-investigate`, `sys-io-guard`, `sys-mem-watch`, `sys-disk-hogs`, `sys-crash-triage`, `sys-net-watch`, and `sys-systemd-watch`) designed to stay responsive during severe CPU, memory, network, and storage stalls.

## Documentation Index

| Document | Scope | Audience |
| --- | --- | --- |
| [Live Showcase](https://bsommers.github.io/sysadmin-tools/) | Interactive GitHub Pages overview of all 7 CLIs, architecture, and incident workflows | Everyone |
| [Purpose & Engineering Principles](purpose.md) | Problem statement, origin I/O-storm incident data, 6 non-negotiable engineering tenets, and threat model / trust boundaries | Everyone |
| [Architecture & Subsystem Internals](architecture.md) | System overview diagram, root-NVMe deployment topology, Mermaid dataflows, deep-dives for all 7 CLIs, deterministic verdict tables, and JSON/JSONL schemas | Engineers, Architects |
| [Installation & CLI Reference](running.md) | Prerequisites, direct-copy installer vs `pip`, complete CLI flag tables for all 7 tools, environment variables, output formats, and exit codes | Operators, Automation Engineers |
| [How-To Recipes & Troubleshooting](how-to.md) | End-to-end incident triage workflow, copy-pasteable operational recipes, `packaging/systemd/` timer + `--diff-log` JSONL trending, local CI verification, and troubleshooting matrix | Operators, Incident Responders |
| [User Guides](user-guide/README.md) | 7-chapter task-oriented walkthroughs and per-tool usage guides | End Users, Operators |
| [Architecture & Workflow Diagrams](diagrams/README.md) | Visual architecture schematics and workflow diagrams | Visual Learners, Reviewers |
| [As-Built Buildspec](BUILDSPEC.md) | Machine-verifiable contracts, module map, invariants, and verified baselines | Maintainers, Contributors |
| [Engineering Notes & Incident Journal](NOTES.md) | Raw origin-incident telemetry, design rationale, and kernel/procfs pitfalls discovered during live testing | Maintainers, Kernel/Systems Engineers |
| [Session Checkpoint](CHECKPOINT.md) | Latest verified milestone state, test evidence, and next steps | Maintainers |
| [Changelog](../CHANGELOG.md) | Release history and version-by-version changes | Everyone |

## Tool Suite at a Glance

| CLI | Purpose | Primary Data Sources |
| --- | --- | --- |
| `sys-investigate` | Diagnose CPU/load spikes, storage I/O stalls, NVMe thermals, container cgroups, per-PID subtrees, and offline `--diff-log` JSONL trends | `/proc`, `/sys/class/drm`, `/sys/class/nvme`, `/sys/class/thermal` |
| `sys-io-guard` | Serialize heavy builds/tests per physical disk via advisory `flock` with `CARGO_BUILD_JOBS` and `nice` caps | `/proc/mounts`, `/sys/block/dm-*/slaves`, tmpfs lock dir |
| `sys-mem-watch` | Track RSS/swap growth rates, direct/kswapd reclaim pressure, memory PSI, and OOM-kill risk | `/proc/meminfo`, `/proc/vmstat`, `/proc/pressure/memory`, `/proc/<pid>/*` |
| `sys-disk-hogs` | Fast workspace/target/cache disk usage analyzer separating reclaimable build hogs from `.git` metadata | Iterative `os.scandir()`, `/proc/mounts`, guarded `os.statvfs()` |
| `sys-crash-triage` | Post-crash and unexpected-reboot forensics (AMD FCH reset register, NUL log gaps, panic/MCE/GPU signatures, kdump/pstore readiness) | `/proc`, `/sys`, `/var/log/{kern.log,syslog}`, `/var/lib/systemd/pstore`, `/var/crash`, bounded `journalctl` |
| `sys-net-watch` | Diagnose NIC link/drop deltas, TCP/UDP socket states & queue backlogs, SNMP retransmits, and per-CPU `softnet_stat` drops | `/proc/net/dev`, `/proc/net/{tcp,tcp6,udp,udp6}`, `/proc/net/snmp`, `/proc/net/softnet_stat` |
| `sys-systemd-watch` | Monitor systemd cgroup v2 memory/CPU/I/O pressure, OOM events, rapid PID-churn crash loops, and journal growth bursts | `/sys/fs/cgroup`, `/proc`, `/run/log/journal`, `/var/log/journal` |

## Recommended Reading Order by Persona

### Operators & SREs (Daily Usage & Automation)
1. [Installation & CLI Reference](running.md) — install standalone copies to `~/.local/bin` and review flags, environment variables, and exit codes.
2. [How-To Recipes & Troubleshooting](how-to.md) — apply copy-pasteable recipes for build serialization, disk cleanup, and systemd JSONL monitoring.
3. [User Guides](user-guide/README.md) — explore guided workflows per tool.

### Incident Responders (Active Stall or Post-Crash Triage)
1. [How-To Recipes & Troubleshooting](how-to.md) — start with the Incident Triage Workflow diagram and Recipes 1, 3, and 5.
2. [Installation & CLI Reference](running.md) — quick lookup for `--no-statvfs`, `--diagnose-pid`, `--progress-check`, `--no-journalctl`, and `--exit-on-issue`.
3. [Purpose & Engineering Principles](purpose.md) — understand trust boundaries (unprivileged `/proc/*/io`, hung FUSE/NFS mounts, NUL-byte crash boundaries).

### Developers & Maintainers
1. [Purpose & Engineering Principles](purpose.md) — internalize the 6 non-negotiable invariants before modifying code.
2. [Architecture & Subsystem Internals](architecture.md) — study the two-snapshot engine, deterministic classifiers, and JSON/JSONL schema contracts.
3. [As-Built Buildspec](BUILDSPEC.md) & [Engineering Notes](NOTES.md) — review known kernel/procfs gotchas and run `./scripts/verify.sh` (166 tests: syntax, 143 unit, 23 smoke).

---

[← Back to Project README](../README.md)
