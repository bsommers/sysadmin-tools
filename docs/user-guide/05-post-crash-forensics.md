[← Back to User's Guide](README.md) | [← Project README](../../README.md)

# Chapter 5: Post-Crash Forensics (`sys-crash-triage`)

When a Linux workstation or server reboots unexpectedly, locks up, or powers off cleanly vs. abruptly, **`sys-crash-triage`** automates the entire post-crash forensic checklist in a single pass — separating **proven facts**, **circumstantial signals**, and **missing telemetry / blind spots**.

---

## 1. Running Immediately After an Unexpected Reboot

Run this as soon as the machine comes back online:

```bash
# Default triage of the current boot (boot 0) vs previous boot (-1)
sys-crash-triage

# Include the 6-step manual verification runbook alongside findings
sys-crash-triage --show-steps

# Inspect an older crash (e.g., comparing boot -1 against boot -2) with deeper history
sys-crash-triage --boot -1 --boots-history 10 --tail-lines 150

# Record structured JSONL evidence + alert exit code (3 on crash/fault)
sys-crash-triage --append-log /var/tmp/sys-crash-triage.jsonl --exit-on-issue
```

### Complete CLI Options

| Flag | Range / Default | Purpose |
|---|---|---|
| `--boot BOOT` | integer `<= 0` (default `0`) | Target boot index (`0` = current boot after crash, `-1` = previous boot). Automatically compares against `BOOT - 1`. |
| `--boots-history N` | `1..50` (default `5`) | Number of recent boots included in the timeline (`boots_history`). |
| `--tail-lines N` | `10..500` (default `80`) | Number of trailing lines inspected from the end of `BOOT - 1` (bounded `-n` seeks from the end in milliseconds). |
| `--no-journalctl` | flag | Skip all `journalctl` subprocesses; inspect `/proc`, `/sys`, `/var/log/{kern.log,syslog}`, pstore, and `/var/crash` directly. |
| `--journal-timeout SEC` | `0.5..30.0` (default `5.0`) | Per-call timeout in seconds for each bounded `journalctl` query. |
| `--show-steps` | flag | Append the 6-step manual troubleshooting runbook (commands + rationale) to text or Markdown output. |
| `--json` | mutually exclusive with `--markdown` | Emit deterministic JSON (`sort_keys=True`). |
| `--markdown` | mutually exclusive with `--json` | Emit Markdown incident summary. |
| `--append-log PATH` | file path | Append one JSONL record per run. |
| `--exit-on-issue` | flag | Exit with code `3` when an unclean crash/reset or fatal fault is detected (`status` not in `CLEAN_REBOOT`, `INSUFFICIENT_DATA`). |
| `--verbose` | flag | Print diagnostic warnings (e.g., `journalctl` permission or timeout warnings) to `stderr`. |

**Exit codes:** `0` clean reboot or insufficient data (or `--exit-on-issue` omitted), `1` validation/runtime error, `2` usage error, `3` crash/unclean reset/fatal fault detected with `--exit-on-issue`.

---

## 2. Decoding AMD FCH `S5_RESET_STATUS` Hardware Reset Bits

On AMD platforms, the Linux kernel logs the hardware FCH PM `S5_RESET_STATUS` (`0xFED803C0`) register during early boot:

```text
x86/amd: Previous system reset reason [0x00100000]: Software wrote 0xE to reset control register 0xCF9
```

`sys-crash-triage` parses and bitwise-ORs all reported masks and decodes all 14 known hardware reset bits:

| Bit Mask | Code | `reset_class` Priority | Operational Meaning |
|---|---|---|---|
| `0x00010000` | `thermal_trip` | `THERMAL_TRIP` | CPU thermal trip shutdown — inspect cooling, paste, fans, pump. |
| `0x00000010` | `sync_flood` | `SYNC_FLOOD_HW_FAULT` | CPU/Infinity Fabric sync flood — fatal hardware/memory/PCIe/AGESA fault. |
| `0x00040000` | `watchdog_timeout` | `WATCHDOG_RESET` | Hardware watchdog timer expired (kernel/system hung). |
| `0x04000000` | `ac_power_loss` | `POWER_LOSS_OR_BUTTON` | AC power loss or PSU protection trip (OCP/OPP/brownout). |
| `0x02000000` | `sleep_state_power_loss` | `POWER_LOSS_OR_BUTTON` | Power loss while suspended in sleep state. |
| `0x00020000` | `power_button_override` | `POWER_LOSS_OR_BUTTON` | 4-second physical power button hold. |
| `0x01000000` | `failed_boot_timer` | `FAILED_BOOT_TIMER` | BIOS/UEFI failed-boot timer expired (e.g., DDR5 memory training retry). |
| `0x00100000` | `cf9_full_reset` | `CF9_FULL_RESET` | Software/firmware wrote `0xE` to port `0xCF9` (instant full platform reset). |
| `0x00080000` | `cf9_warm_reset` | `CF9_WARM_RESET` | Software wrote `0x6` to port `0xCF9` (warm reset). |
| `0x00200000` | `acpi_power_transition` | `ACPI_TRANSITION` | Normal ACPI power state transition. |
| `0x00800000` | `software_shutdown` | `SOFTWARE_SHUTDOWN` | Software-initiated shutdown. |
| `0x00000004` | `user_reset_button` | `USER_RESET_BUTTON` | Physical reset pin / chassis reset button. |
| `0x00000200` | `remote_reset` | `UNKNOWN` | Remote reset / ASF. |
| `0x00400000` | `asf_reset` | `UNKNOWN` | ASF / management controller reset. |

> **Why `0xCF9=0xE` matters:** A `cf9_full_reset` combined with unclean journal/filesystem recovery and **no** kernel panic/MCE indicates an abrupt platform reset (firmware/driver/triple-fault/transient power event) that bypassed normal kernel crash flushing.

---

## 3. Unclean Shutdown Evidence & Crash Signatures

### 3.1 Unclean Shutdown Indicators

`sys-crash-triage` correlates three independent signals (and checks for `systemd-shutdown` / `Rebooting.` / `Powering off.` clean-shutdown markers at the end of the previous boot):
1. **Corrupted journal:** `systemd-journald` reporting *"corrupted or uncleanly shut down"* on the new boot.
2. **Filesystem recovery:** `EXT4-fs`, `XFS`, or `BTRFS` reporting *"orphan inodes deleted"*, *"recovering journal"*, or *"recovery complete"*.
3. **NUL-byte (`\x00`) log gaps:** Pure binary scan of the tail (up to 8 MiB) of `/var/log/kern.log` and `/var/log/syslog`. When a machine hard-resets mid-write while buffered rsyslog pages are extended on disk but not yet flushed, the filesystem leaves contiguous `\x00` byte runs right at the crash boundary — pinpointing the exact last log line written before power/reset cut.

### 3.2 Fatal vs. Context Signature Categories

| Category | Severity | What It Matches |
|---|---|---|
| `kernel_panic` | **fatal** | `Kernel panic`, `BUG: unable to handle`, `BUG: kernel NULL`, `Oops:`, `general protection fault`, `divide error:`, `double fault:`, `Kernel BUG at`. |
| `mce_hardware` | **fatal** / context | **Fatal:** `Machine check events logged`, `[Hardware Error]`, `EDAC ... UE` (uncorrectable), PCIe/AER `Uncorrected`. **Context:** `EDAC ... CE` (correctable), PCIe/AER `Corrected`. |
| `thermal` | **fatal** | `critical temperature reached`, `thermal ... shutting down`, `over-temperature`, `package temperature above threshold`. |
| `oom_killer` | **fatal** | `Out of memory: Killed process`, `oom-kill:`, `invoked oom-killer`. |
| `watchdog_lockup` | **fatal** / context | **Fatal:** `Watchdog detected hard LOCKUP`, `BUG: soft lockup`, `rcu_sched`/`rcu_preempt` stalls. **Context:** `INFO: task ... blocked for more than N seconds`, writeback stalls. |
| `gpu_fault` | **fatal** / context | **Fatal:** `amdgpu` GPU reset / ring timeout / `vm_fault` / `MES failed to respond`, `NVRM: Xid`, `i915` GPU HANG, `nouveau` failed to idle. **Context:** `amdgpu` ROCm KFD workqueue CPU hog (`svm_range_deferred_list_work`, `kfd_process_wq_release`), page fault / RAS errors. |
| `storage_fault` | **fatal** | `nvme` I/O timeout / controller down / resetting / `Device not ready`, `blk_update_request: I/O error`, `EXT4-fs error`, `XFS ... Corruption`, `BTRFS error`. |

### 3.3 Built-In Noise Filtering

High-volume benign messages are automatically stripped from crash context, NUL-boundary excerpts, and previous-boot tails so real faults stand out immediately:
- `audit: type=1326` (seccomp syscall audit spam)
- `qt.bluetooth.bluez: No uuids found for`
- `ring <name> uses VM inv eng` (harmless `amdgpu` boot info)

### 3.4 Verdict Statuses

| Verdict Status | `is_issue` | Meaning |
|---|---|---|
| `KERNEL_PANIC_OR_OOPS` | `True` | Fatal kernel panic, NULL dereference, or oops matched. |
| `HARDWARE_MCE_OR_THERMAL` | `True` | Fatal MCE/AER/thermal signature, `EDAC UE > 0`, `THERMAL_TRIP`, or `SYNC_FLOOD_HW_FAULT`. |
| `OOM_OR_LOCKUP_CRASH` | `True` | OOM killer, kernel hard/soft lockup, RCU stall, or `WATCHDOG_RESET`. |
| `GPU_OR_STORAGE_FAULT_RESET` | `True` | Unclean reset preceded by fatal GPU (`amdgpu`/`NVRM`/`i915`) or NVMe/FS fault. |
| `UNCLEAN_HARD_RESET` | `True` | Confirmed unclean shutdown or `FAILED_BOOT_TIMER` / `POWER_LOSS_OR_BUTTON` / `CF9_FULL_RESET` without a saved panic/MCE. |
| `CLEAN_REBOOT` | `False` | Graceful reboot with no unclean indicators or fatal faults. |
| `INSUFFICIENT_DATA` | `False` | Neither `journalctl` nor `/var/log/{kern.log,syslog}` was readable. |

---

## 4. Offline / Degraded Mode & `--show-steps` Runbook

If `systemd-journald` is slow, locked, missing, or you want pure zero-subprocess file inspection:

```bash
# Pure file-based triage (/proc, /sys, /var/log/*, pstore, /var/crash)
sys-crash-triage --no-journalctl --verbose

# Or tighten journalctl timeouts on huge/slow journals
sys-crash-triage --journal-timeout 2.0 --tail-lines 100
```

Passing **`--show-steps`** appends the exact 6-step manual verification commands (`uptime; journalctl --list-boots`, unclean-shutdown grep, AMD reset/MCE grep, bounded `-b -1 -n 100` tail, `pstore`/`kdump` check, and OOM/`unattended-upgrades` check) so you can dig deeper into raw logs without memorizing flags.

---

## 5. Fixing `efi_pstore` & `kdump-tools` Readiness Gaps

`sys-crash-triage` audits crash-capture readiness on every run:
- **`/proc/cmdline` (`crashkernel=`) vs. `/etc/default/kdump-tools` (`USE_KDUMP=`):** Catches a very common misconfiguration where `crashkernel=` reserves 512 MiB+ of RAM on every boot, yet `USE_KDUMP=0` disables crash capture!
- **`/sys/fs/pstore` & `/var/lib/systemd/pstore` & `/var/crash`:** Checks for recent crash dumps near the reboot timestamp.
- **`/sys/devices/system/edac/mc/mc*/{ce_count,ue_count}`:** Checks ECC memory correctable (`CE`) and uncorrectable (`UE`) error counters.
- **`/var/log/unattended-upgrades/unattended-upgrades.log`:** Rules out scheduled automatic package reboots within ±30 minutes of the reboot.

### Remediating Common Blind Spots (Debian/Ubuntu Example)

```bash
# 1. Enable kdump-tools when crashkernel= is already reserved in /proc/cmdline
sudo sed -i 's/^USE_KDUMP=0/USE_KDUMP=1/' /etc/default/kdump-tools
sudo systemctl enable --now kdump-tools
kdump-config show

# 2. Verify systemd-pstore archives efi_pstore panic dumps into /var/lib/systemd/pstore
lsmod | grep pstore
systemctl status systemd-pstore.service

# 3. Install rasdaemon to persist MCE, PCIe AER, and memory errors across reboots
sudo apt install rasdaemon
sudo systemctl enable --now rasdaemon
```

---

**Navigation:** [← Previous: Chapter 4 — Memory & Disk Auditing](04-memory-and-disk-auditing.md) | [Next: Chapter 6 — Automation & Troubleshooting →](06-automation-and-troubleshooting.md)
