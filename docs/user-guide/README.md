[← Back to Project README](../../README.md)

# sysadmin-tools User's Guide (v0.4.0)

Welcome to the operator manual for **sysadmin-tools** — five zero-dependency, standalone Linux diagnostic and workload-guard CLIs written in Python 3.10+ stdlib only. Every hot-path diagnostic reads `/proc` and `/sys` directly without shelling out, and every binary installs as an independent copy on your root filesystem so diagnostics keep working even when secondary NVMe or data mounts are 99%+ saturated.

![Incident Triage Workflow](../assets/examples/incident-triage-workflow.svg)

---

## Table of Contents

| Chapter | Topic | Primary Tools |
|---|---|---|
| [Chapter 1: Getting Started](01-getting-started.md) | Prerequisites, root-NVMe installation, 106-test local verification, and a first-run tour of all 5 CLIs | `install.sh`, `verify.sh`, all 5 CLIs |
| [Chapter 2: Live System Investigation](02-live-system-investigation.md) | Diagnosing load spikes, `D`-state threads, disk/NVMe thermal stalls, mount & cgroup attribution, `--progress-check`, and `--diagnose-pid` | `sys-investigate` |
| [Chapter 3: Preventing I/O Storms](03-preventing-io-storms.md) | Serializing heavy Rust/C++ builds per physical disk via tmpfs `flock`, Cargo job caps, `nice` throttling, `--dry-run`, and `--status` | `sys-io-guard` |
| [Chapter 4: Memory & Disk Auditing](04-memory-and-disk-auditing.md) | Tracking RSS/swap growth, reclaim pressure, and OOM risk; auditing workspace disk hogs and reclaimable caches safely | `sys-mem-watch`, `sys-disk-hogs` |
| [Chapter 5: Post-Crash Forensics](05-post-crash-forensics.md) | Investigating unexpected reboots, AMD FCH `S5_RESET_STATUS` registers, NUL-byte log gaps, crash signatures, and `kdump`/`pstore` readiness | `sys-crash-triage` |
| [Chapter 6: Automation & Troubleshooting](06-automation-and-troubleshooting.md) | `jq` pipelines, exit-code alerting, JSONL telemetry + systemd user timers, and a practical FAQ | All 5 CLIs |

---

## Which Tool Should I Run Right Now?

| Symptom / Operational Goal | Run This Command First | What You Will Learn |
|---|---|---|
| **Load average is spiking, desktop/shell feels sluggish, or disk LED is solid** | `sys-investigate --progress-check --interval 2` | Separates CPU compute vs I/O wait vs swap thrash, lists `D`-state wait channels (`balance_dirty_pages`, etc.), NVMe thermals, and confirms whether work is progressing or wedged. |
| **A single service, build, or PID looks stuck or hot** | `sys-investigate --diagnose-pid <PID> --interval 2` | Shows per-thread `R`/`D`/`S` breakdown, kernel `wchan`, active mounts, cgroup, and aggregated CPU/RSS/I/O across the entire descendant subtree. |
| **Secondary mount is hung (NFS/FUSE/stalled SSD) and normal commands freeze** | `sys-investigate --no-statvfs --interval 2` | Skips `os.statvfs()` completely and reads only kernel virtual files in `/proc` and `/sys`. |
| **Multiple `cargo test`, `verify.sh`, or C++ linkers are fighting over the same SSD** | `sys-io-guard --path /datanapps/cargo-target -- cargo test` | Serializes builds per backing physical disk using tmpfs `flock`, caps `CARGO_BUILD_JOBS`, and lowers CPU priority (`--nice`). |
| **Check whether a guarded build is currently holding a disk lock** | `sys-io-guard --status` | Probes `*.lock` files non-blockingly and prints holder PID, PPID, start timestamp, target path, and command. |
| **RAM is disappearing, swap is active, or a process might be leaking memory** | `sys-mem-watch --interval 2 --top 15` | Ranks top RSS, fastest RSS growers (`MiB/s`), swap consumers, and `oom_score` targets alongside memory PSI and `pgscan_direct`. |
| **A workspace or SSD is filling up and you need safe space back** | `sys-disk-hogs ~/src /datanapps --top 20 --depth 2` | Finds largest directories/files and separates reclaimable build/cache hogs (`target/`, `node_modules/`, `.venv/`) from non-reclaimable `.git` history. |
| **The machine just rebooted unexpectedly or hard-locked** | `sys-crash-triage --show-steps` | Builds boot downtime timeline, decodes AMD `S5_RESET_STATUS` (`0xCF9`, `sync_flood`, `thermal_trip`, etc.), detects NUL-byte log gaps and crash signatures, and audits `kdump`/`pstore`. |
| **Need machine-readable telemetry for alerting or cron/systemd timers** | `<tool> --exit-on-issue --append-log /var/tmp/diag.jsonl --json` | Emits deterministic JSON (`sort_keys=True`), appends compact JSONL records, and exits `3` on detected issues. |

---

## System Architecture at a Glance

![Architecture Overview](../assets/examples/architecture-overview.svg)

For deeper engineering contracts, design invariants, and origin-incident notes, see:
- [Running & CLI Reference](../running.md)
- [As-Built Buildspec](../BUILDSPEC.md)
- [Engineering Notes & Incident Journal](../NOTES.md)

---

**Navigation:** [Next: Chapter 1 — Getting Started →](01-getting-started.md)
