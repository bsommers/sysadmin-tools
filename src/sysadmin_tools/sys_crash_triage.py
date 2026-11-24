#!/usr/bin/env python3
"""sys-crash-triage: Linux post-crash and unexpected-reboot forensic investigator.

Automates the step-by-step Linux crash/reboot triage workflow using Python 3.10+
stdlib only (zero third-party runtime dependencies):
  1. Boot timeline, kernel version change, and downtime-gap analysis
  2. Hardware reset-reason decoding (AMD FCH S5_RESET_STATUS / 0xCF9 / ACPI)
  3. Unclean-shutdown detection (journal corruption, FS recovery, NUL log gaps)
  4. Root-cause signature scanning (panic/oops, MCE/EDAC/AER, OOM, thermal,
     watchdog/hung-task, GPU/storage faults) with audit/Bluetooth noise filtering
  5. Crash-capture readiness audit (efi_pstore, kdump USE_KDUMP vs crashkernel=,
     /var/crash, EDAC ECC counters, unattended-upgrades correlation)

Reads /proc, /sys, /var/log/{kern.log,syslog}, /var/lib/systemd/pstore,
/var/crash, and /etc/default/kdump-tools directly; uses bounded, timeout-guarded
journalctl queries (disable with --no-journalctl) to read binary systemd journals.

Examples:
  sys-crash-triage
  sys-crash-triage --show-steps
  sys-crash-triage --boot -1 --boots-history 10
  sys-crash-triage --no-journalctl --json | jq '.verdict'
  sys-crash-triage --markdown
  sys-crash-triage --append-log /var/tmp/sys-crash-triage.jsonl --exit-on-issue
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

# Bounded log read cap (8 MiB) so scanning huge syslog/kern.log files stays fast.
DEFAULT_MAX_LOG_BYTES = 8 * 1024 * 1024

# AMD FCH PM S5_RESET_STATUS (0xFED803C0) known reset-reason bits.
AMD_RESET_BITS: tuple[tuple[int, str, str], ...] = (
    (0x00000004, "user_reset_button", "User reset button or Pin reset"),
    (0x00000010, "sync_flood", "CPU/fabric sync flood (hardware fault)"),
    (0x00000200, "remote_reset", "Remote reset / ASF"),
    (0x00010000, "thermal_trip", "CPU thermal trip shutdown"),
    (0x00020000, "power_button_override", "4-second power button override"),
    (0x00040000, "watchdog_timeout", "Hardware watchdog timer expired"),
    (0x00080000, "cf9_warm_reset", "Software wrote 0x6 to reset control register 0xCF9 (warm reset)"),
    (0x00100000, "cf9_full_reset", "Software wrote 0xE to reset control register 0xCF9 (full reset)"),
    (0x00200000, "acpi_power_transition", "ACPI power state transition occurred"),
    (0x00400000, "asf_reset", "ASF / management controller reset"),
    (0x00800000, "software_shutdown", "Software initiated shutdown"),
    (0x01000000, "failed_boot_timer", "System failed to boot before failed boot timer expired"),
    (0x02000000, "sleep_state_power_loss", "Power loss during sleep state"),
    (0x04000000, "ac_power_loss", "AC power loss / PSU cut"),
)

# High-volume benign log lines filtered when extracting crash context.
NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"audit:\s+type=1326\b"),
    re.compile(r"qt\.bluetooth\.bluez:\s+No uuids found for"),
    re.compile(r"ring\s+\S+\s+uses VM inv eng"),
)

BOOT_LINE_RE = re.compile(
    r"^\s*(?P<idx>-?\d+)\s+(?P<boot_id>[0-9a-f]{32})\s+"
    r"(?:[A-Za-z]{3}\s+)?(?P<first>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})(?:\s+\S+)?\s+"
    r"(?:[A-Za-z]{3}\s+)?(?P<last>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})(?:\s+\S+)?\s*$"
)

AMD_RESET_LINE_RE = re.compile(
    r"x86/amd:\s+Previous system reset reason\s+\[0x(?P<mask>[0-9a-fA-F]+)\]:\s*(?P<desc>.+)$"
)

LINUX_VERSION_RE = re.compile(r"Linux version\s+(\S+)")

# Signature rules: (category, severity, regex)
# severity: "fatal" (direct crash cause) vs "context" (contributing/warning signal)
SIGNATURE_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "kernel_panic",
        "fatal",
        re.compile(
            r"Kernel panic|BUG:\s+unable to handle|BUG:\s+kernel NULL|Oops:\s|"
            r"general protection fault|divide error:\s|double fault:\s|Kernel BUG at",
            re.IGNORECASE,
        ),
    ),
    (
        "mce_hardware",
        "fatal",
        re.compile(
            r"Machine check events logged|\[Hardware Error\]|mce:\s+\[Hardware Error\]|"
            r"Hardware Error.*(?:CPU|Bank|MC\d)|EDAC\s+\S+:\s+\d+\s+UE\b|"
            r"PCIe Bus Error:.*severity=Uncorrected|AER:.*Uncorrected",
            re.IGNORECASE,
        ),
    ),
    (
        "thermal",
        "fatal",
        re.compile(
            r"critical temperature reached|thermal.*shutting down|over-temperature|"
            r"cpu clock throttled.*critical|package temperature above threshold",
            re.IGNORECASE,
        ),
    ),
    (
        "oom_killer",
        "fatal",
        re.compile(r"Out of memory:\s+Killed process|oom-kill:|invoked oom-killer", re.IGNORECASE),
    ),
    (
        "watchdog_lockup",
        "fatal",
        re.compile(
            r"Watchdog detected hard LOCKUP|BUG:\s+soft lockup|rcu_sched.*detected stalls|"
            r"rcu_preempt.*detected stalls",
            re.IGNORECASE,
        ),
    ),
    (
        "watchdog_lockup",
        "context",
        re.compile(
            r"INFO:\s+task\s+\S+\s+blocked for more than \d+ seconds|"
            r"waiting for writeback completion for more than \d+ seconds",
            re.IGNORECASE,
        ),
    ),
    (
        "gpu_fault",
        "fatal",
        re.compile(
            r"amdgpu.*(?:GPU reset|ring\s+\S+\s+timeout|failed to reset GPU|fatal error|"
            r"GPU fault detected|vm_fault|MES failed to respond)|"
            r"NVRM:\s+Xid|i915.*GPU HANG|nouveau.*failed to idle",
            re.IGNORECASE,
        ),
    ),
    (
        "gpu_fault",
        "context",
        re.compile(
            r"workqueue:\s+(?:svm_range_deferred_list_work|kfd_process_wq_release)\s+\[amdgpu\]\s+hogged CPU|"
            r"amdgpu.*(?:page fault|ring.*reset|ras.*error)",
            re.IGNORECASE,
        ),
    ),
    (
        "storage_fault",
        "fatal",
        re.compile(
            r"nvme\d+.*(?:I/O tag.*timed out|controller is down|resetting controller|Device not ready)|"
            r"blk_update_request:\s+I/O error|EXT4-fs error|XFS.*Corruption|BTRFS error",
            re.IGNORECASE,
        ),
    ),
    (
        "mce_hardware",
        "context",
        re.compile(
            r"PCIe Bus Error:.*severity=Corrected|AER:.*Corrected|EDAC\s+\S+:\s+\d+\s+CE\b",
            re.IGNORECASE,
        ),
    ),
)

UNCLEAN_JOURNAL_RE = re.compile(r"corrupted or uncleanly shut down", re.IGNORECASE)
UNCLEAN_FS_RE = re.compile(
    r"(?:EXT4-fs|XFS|BTRFS).*?(?:orphan inodes deleted|recovering journal|recovery complete|unclean)",
    re.IGNORECASE,
)
CLEAN_SHUTDOWN_RE = re.compile(
    r"systemd-shutdown\[|Shutting down\.|Reached target (?:reboot|poweroff|shutdown)\.target|"
    r"Rebooting\.|Powering off\.",
    re.IGNORECASE,
)
UNATTENDED_REBOOT_RE = re.compile(r"reboot|shutdown", re.IGNORECASE)

MANUAL_STEPS: tuple[dict[str, str], ...] = (
    {
        "step": "1. Confirm boot timeline and downtime gap",
        "command": "uptime; journalctl --list-boots | tail -n 10",
        "why": "Identifies exact timestamp when the previous boot stopped logging vs. when the new boot started.",
    },
    {
        "step": "2. Check for unclean shutdown vs. graceful reboot",
        "command": "journalctl -b 0 -k -u systemd-journald --no-pager | grep -iE 'uncleanly|orphan|recovering journal|previous system reset'",
        "why": "Corrupted journals, ext4 orphan inodes, and hardware reset status prove whether the machine crashed or rebooted cleanly.",
    },
    {
        "step": "3. Check AMD/hardware reset reason and MCE/hardware errors on the new boot",
        "command": "journalctl -b 0 -k --no-pager | grep -iE 'Previous system reset reason|Hardware Error|mce:|Machine check|EDAC|AER:'",
        "why": "AMD CPUs log the FCH S5_RESET_STATUS register (e.g., 0xCF9=0xE full reset, thermal trip, watchdog, sync flood) on the next boot; MCE banks also report on boot.",
    },
    {
        "step": "4. Inspect the final lines before the crash in journald and rsyslog",
        "command": "journalctl -b -1 -n 100 --no-pager | grep -vE 'audit: type=1326|qt\\.bluetooth\\.bluez'",
        "why": "Bounded -n seeks from the end in milliseconds and avoids scanning multi-week journals. Also inspect /var/log/syslog for NUL-byte (\\x00) gaps.",
    },
    {
        "step": "5. Check pstore, kdump, and /var/crash artifacts",
        "command": "ls -la /var/lib/systemd/pstore /var/crash; grep -E '^USE_KDUMP' /etc/default/kdump-tools; grep -o 'crashkernel=[^ ]*' /proc/cmdline",
        "why": "Confirms whether kernel panics were captured (or missed because USE_KDUMP=0 despite crashkernel= being reserved).",
    },
    {
        "step": "6. Rule out OOM and unattended package reboots",
        "command": "journalctl -b -1 -k -n 200 --no-pager | grep -iE 'oom|killed process|panic|oops|amdgpu|nvme'; tail -n 25 /var/log/unattended-upgrades/unattended-upgrades.log",
        "why": "Distinguishes memory exhaustion, driver faults, and scheduled package updates from sudden hardware/firmware resets.",
    },
)


def read_text(path: str | Path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def is_noise_line(line: str) -> bool:
    return any(p.search(line) for p in NOISE_PATTERNS)


def parse_local_ts(ts_str: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def fmt_duration(sec: float | None) -> str:
    if sec is None or sec < 0:
        return "n/a"
    s = int(round(sec))
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours:02d}h {mins:02d}m"
    if hours > 0:
        return f"{hours}h {mins:02d}m {secs:02d}s"
    if mins > 0:
        return f"{mins}m {secs:02d}s"
    return f"{secs}s"


def read_system_info(proc_root: str = "/proc") -> dict[str, Any]:
    uptime_txt = read_text(f"{proc_root}/uptime") or ""
    uptime_sec = 0.0
    parts = uptime_txt.split()
    if parts:
        try:
            uptime_sec = float(parts[0])
        except ValueError:
            uptime_sec = 0.0

    version_txt = (read_text(f"{proc_root}/version") or "").strip()
    m_ver = LINUX_VERSION_RE.search(version_txt)
    kernel_release = m_ver.group(1) if m_ver else (os.uname().release if hasattr(os, "uname") else "unknown")

    cmdline = (read_text(f"{proc_root}/cmdline") or "").strip()
    crashkernel_param = None
    for tok in cmdline.split():
        if tok.startswith("crashkernel="):
            crashkernel_param = tok.split("=", 1)[1]
            break

    return {
        "hostname": socket.gethostname(),
        "uptime_sec": round(uptime_sec, 1),
        "uptime_human": fmt_duration(uptime_sec),
        "kernel_release": kernel_release,
        "crashkernel_param": crashkernel_param,
        "crashkernel_reserved": crashkernel_param is not None,
    }


def parse_journal_boots(text: str) -> list[dict[str, Any]]:
    boots: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        m = BOOT_LINE_RE.match(raw_line)
        if not m:
            continue
        idx = int(m.group("idx"))
        first_s = m.group("first")
        last_s = m.group("last")
        t0 = parse_local_ts(first_s)
        t1 = parse_local_ts(last_s)
        dur = (t1 - t0).total_seconds() if (t0 and t1 and t1 >= t0) else None
        boots.append(
            {
                "index": idx,
                "boot_id": m.group("boot_id"),
                "first_ts": first_s,
                "last_ts": last_s,
                "duration_sec": round(dur, 1) if dur is not None else None,
                "duration_human": fmt_duration(dur),
            }
        )
    boots.sort(key=lambda b: b["index"])
    return boots


def decode_amd_reset_mask(mask: int) -> dict[str, Any]:
    flags: list[dict[str, str]] = []
    for bit, code, desc in AMD_RESET_BITS:
        if mask & bit:
            flags.append({"code": code, "mask": f"0x{bit:08x}", "description": desc})

    codes = {f["code"] for f in flags}
    if "thermal_trip" in codes:
        reset_class = "THERMAL_TRIP"
    elif "sync_flood" in codes:
        reset_class = "SYNC_FLOOD_HW_FAULT"
    elif "watchdog_timeout" in codes:
        reset_class = "WATCHDOG_RESET"
    elif "ac_power_loss" in codes or "sleep_state_power_loss" in codes or "power_button_override" in codes:
        reset_class = "POWER_LOSS_OR_BUTTON"
    elif "failed_boot_timer" in codes:
        reset_class = "FAILED_BOOT_TIMER"
    elif "cf9_full_reset" in codes:
        reset_class = "CF9_FULL_RESET"
    elif "cf9_warm_reset" in codes:
        reset_class = "CF9_WARM_RESET"
    elif "acpi_power_transition" in codes:
        reset_class = "ACPI_TRANSITION"
    elif "software_shutdown" in codes:
        reset_class = "SOFTWARE_SHUTDOWN"
    elif "user_reset_button" in codes:
        reset_class = "USER_RESET_BUTTON"
    else:
        reset_class = "UNKNOWN"

    return {
        "raw_mask": f"0x{mask:08x}",
        "reset_class": reset_class,
        "decoded_flags": flags,
    }


def parse_amd_reset_reasons(kernel_lines: list[str]) -> dict[str, Any]:
    raw_mask: int | None = None
    kernel_descriptions: list[str] = []
    for line in kernel_lines:
        m = AMD_RESET_LINE_RE.search(line)
        if not m:
            continue
        mask_val = int(m.group("mask"), 16)
        raw_mask = mask_val if raw_mask is None else (raw_mask | mask_val)
        desc = m.group("desc").strip()
        if desc not in kernel_descriptions:
            kernel_descriptions.append(desc)

    if raw_mask is None:
        return {
            "present": False,
            "raw_mask": None,
            "reset_class": "NONE",
            "kernel_descriptions": [],
            "decoded_flags": [],
        }

    decoded = decode_amd_reset_mask(raw_mask)
    return {
        "present": True,
        "raw_mask": decoded["raw_mask"],
        "reset_class": decoded["reset_class"],
        "kernel_descriptions": kernel_descriptions,
        "decoded_flags": decoded["decoded_flags"],
    }


def extract_kernel_version(lines: list[str]) -> str | None:
    for line in lines:
        m = LINUX_VERSION_RE.search(line)
        if m:
            return m.group(1)
    return None


def scan_nul_crash_boundary(
    path: str | Path,
    max_bytes: int = DEFAULT_MAX_LOG_BYTES,
    context_before: int = 8,
    context_after: int = 5,
) -> dict[str, Any]:
    """Scan a log file for NUL-byte (\\x00) runs left by interrupted buffered writes."""
    p = Path(path)
    result: dict[str, Any] = {
        "path": str(p),
        "readable": False,
        "file_size": 0,
        "scanned_from_offset": 0,
        "nul_runs": 0,
        "has_nul_gap": False,
        "last_gap_offset": None,
        "last_gap_nul_count": 0,
        "last_raw_before_nul": None,
        "lines_before_nul": [],
        "lines_after_nul": [],
        "error": None,
    }
    try:
        st = p.stat()
        size = st.st_size
        start = max(0, size - max_bytes)
        with open(p, "rb") as f:
            if start > 0:
                f.seek(start)
            data = f.read(max_bytes)
    except OSError as exc:
        result["error"] = str(exc)
        return result

    result["readable"] = True
    result["file_size"] = size
    result["scanned_from_offset"] = start

    # Locate all contiguous runs of \x00 bytes; report the most recent (last) run.
    runs: list[tuple[int, int]] = []
    pos = 0
    n = len(data)
    while pos < n:
        idx = data.find(b"\x00", pos)
        if idx == -1:
            break
        end = idx + 1
        while end < n and data[end] == 0:
            end += 1
        runs.append((idx, end))
        pos = end

    if not runs:
        return result

    last_start, last_end = runs[-1]
    result["nul_runs"] = len(runs)
    result["has_nul_gap"] = True
    result["last_gap_offset"] = start + last_start
    result["last_gap_nul_count"] = last_end - last_start

    # Grab up to 32 KiB before and 8 KiB after the last NUL run for clean line context.
    before_chunk = data[max(0, last_start - 32768) : last_start].decode("utf-8", errors="replace")
    after_chunk = data[last_end : min(n, last_end + 8192)].decode("utf-8", errors="replace")

    before_lines = [ln for ln in before_chunk.splitlines() if ln.strip()]
    if before_lines:
        result["last_raw_before_nul"] = before_lines[-1]
    filtered_before = [ln for ln in before_lines if not is_noise_line(ln)]
    result["lines_before_nul"] = (filtered_before or before_lines)[-context_before:]

    after_lines = [ln for ln in after_chunk.splitlines() if ln.strip() and not is_noise_line(ln)]
    result["lines_after_nul"] = after_lines[:context_after]
    return result


def detect_unclean_signals(
    target_boot_lines: list[str],
    prev_boot_tail: list[str],
    nul_scans: list[dict[str, Any]],
) -> dict[str, Any]:
    journal_corrupted: list[str] = []
    fs_recovery: list[str] = []
    clean_shutdown_markers: list[str] = []

    for line in target_boot_lines:
        if UNCLEAN_JOURNAL_RE.search(line):
            journal_corrupted.append(line.strip())
        elif UNCLEAN_FS_RE.search(line):
            fs_recovery.append(line.strip())

    for line in prev_boot_tail:
        if CLEAN_SHUTDOWN_RE.search(line):
            clean_shutdown_markers.append(line.strip())

    nul_files = [s["path"] for s in nul_scans if s.get("has_nul_gap")]
    unclean_indicators = len(journal_corrupted) + len(fs_recovery) + len(nul_files)
    is_unclean = unclean_indicators > 0 and len(clean_shutdown_markers) == 0

    return {
        "is_unclean": is_unclean,
        "journal_corrupted": journal_corrupted[:10],
        "fs_recovery": fs_recovery[:10],
        "nul_gap_files": nul_files,
        "clean_shutdown_markers": clean_shutdown_markers[:10],
    }


def scan_crash_signatures(
    target_boot_kernel: list[str],
    prev_boot_lines: list[str],
    max_per_category: int = 8,
) -> dict[str, Any]:
    fatal: dict[str, list[str]] = {}
    context: dict[str, list[str]] = {}

    def _scan(lines: list[str], source: str) -> None:
        for raw in lines:
            line = raw.strip()
            if not line or is_noise_line(line):
                continue
            for category, severity, pattern in SIGNATURE_RULES:
                if pattern.search(line):
                    bucket = fatal if severity == "fatal" else context
                    items = bucket.setdefault(category, [])
                    entry = f"[{source}] {line}"
                    if entry not in items and len(items) < max_per_category:
                        items.append(entry)
                    break

    _scan(target_boot_kernel, "boot0-kernel")
    _scan(prev_boot_lines, "prev-boot")

    return {
        "fatal": fatal,
        "context": context,
        "fatal_categories": sorted(fatal.keys()),
        "context_categories": sorted(context.keys()),
    }


def inspect_kdump_config(
    kdump_cfg_path: str | Path = "/etc/default/kdump-tools",
    crashkernel_reserved: bool = False,
) -> dict[str, Any]:
    txt = read_text(kdump_cfg_path)
    use_kdump: int | None = None
    coredir = "/var/crash"
    if txt is not None:
        for line in txt.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("USE_KDUMP="):
                val = s.split("=", 1)[1].strip().strip('"').strip("'")
                if val.isdigit():
                    use_kdump = int(val)
            elif s.startswith("KDUMP_COREDIR="):
                coredir = s.split("=", 1)[1].strip().strip('"').strip("'")

    enabled = use_kdump == 1
    reserved_but_disabled = crashkernel_reserved and (use_kdump == 0)
    return {
        "config_path": str(kdump_cfg_path),
        "config_present": txt is not None,
        "use_kdump": use_kdump,
        "enabled": enabled,
        "coredir": coredir,
        "crashkernel_reserved": crashkernel_reserved,
        "reserved_but_disabled": reserved_but_disabled,
    }


def inspect_pstore_and_crash_dirs(
    pstore_archive: str | Path = "/var/lib/systemd/pstore",
    sys_pstore: str | Path = "/sys/fs/pstore",
    crash_dir: str | Path = "/var/crash",
    reference_ts: dt.datetime | None = None,
) -> dict[str, Any]:
    ref_epoch = reference_ts.timestamp() if reference_ts else None

    def _list_entries(dir_path: Path, recent_window_sec: float = 3600.0) -> dict[str, Any]:
        info: dict[str, Any] = {
            "path": str(dir_path),
            "exists": dir_path.exists(),
            "readable": False,
            "total_entries": 0,
            "recent_entries": [],
            "latest_entries": [],
            "error": None,
        }
        if not info["exists"]:
            return info
        try:
            entries = []
            for entry in os.scandir(dir_path):
                try:
                    st = entry.stat(follow_symlinks=False)
                    mtime = st.st_mtime
                    size = st.st_size
                except OSError:
                    mtime = 0.0
                    size = 0
                entries.append(
                    {
                        "name": entry.name,
                        "is_dir": entry.is_dir(follow_symlinks=False),
                        "size": size,
                        "mtime_epoch": round(mtime, 1),
                        "mtime_iso": dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                        if mtime > 0
                        else "unknown",
                    }
                )
            entries.sort(key=lambda e: e["mtime_epoch"], reverse=True)
            info["readable"] = True
            info["total_entries"] = len(entries)
            info["latest_entries"] = entries[:8]
            if ref_epoch is not None:
                info["recent_entries"] = [
                    e for e in entries if abs(e["mtime_epoch"] - ref_epoch) <= recent_window_sec
                ]
        except OSError as exc:
            info["error"] = str(exc)
        return info

    return {
        "sys_pstore": _list_entries(Path(sys_pstore)),
        "systemd_pstore": _list_entries(Path(pstore_archive)),
        "var_crash": _list_entries(Path(crash_dir)),
    }


def inspect_edac_counters(edac_root: str | Path = "/sys/devices/system/edac/mc") -> dict[str, Any]:
    root = Path(edac_root)
    if not root.exists():
        return {"available": False, "controllers": [], "total_ce": 0, "total_ue": 0}
    controllers: list[dict[str, Any]] = []
    total_ce = 0
    total_ue = 0
    try:
        for entry in sorted(os.scandir(root), key=lambda e: e.name):
            if not entry.name.startswith("mc") or not entry.is_dir():
                continue
            ce_txt = read_text(Path(entry.path) / "ce_count")
            ue_txt = read_text(Path(entry.path) / "ue_count")
            ce = int(ce_txt.strip()) if ce_txt and ce_txt.strip().isdigit() else 0
            ue = int(ue_txt.strip()) if ue_txt and ue_txt.strip().isdigit() else 0
            total_ce += ce
            total_ue += ue
            controllers.append({"mc": entry.name, "ce_count": ce, "ue_count": ue})
    except OSError:
        return {"available": False, "controllers": [], "total_ce": 0, "total_ue": 0}
    return {
        "available": len(controllers) > 0,
        "controllers": controllers,
        "total_ce": total_ce,
        "total_ue": total_ue,
    }


def inspect_unattended_upgrades(
    log_path: str | Path = "/var/log/unattended-upgrades/unattended-upgrades.log",
    reference_ts: dt.datetime | None = None,
    window_sec: float = 1800.0,
) -> dict[str, Any]:
    txt = read_text(log_path)
    if txt is None:
        return {"readable": False, "near_reboot_lines": [], "recent_activity_near_reboot": False}
    tail_lines = txt.splitlines()[-100:]
    near: list[str] = []
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
    if reference_ts is not None:
        ref_epoch = reference_ts.timestamp()
        for line in tail_lines:
            m = ts_re.match(line)
            if not m:
                continue
            ts = parse_local_ts(m.group(1))
            if ts and abs(ts.timestamp() - ref_epoch) <= window_sec:
                near.append(line.strip())
    return {
        "readable": True,
        "near_reboot_lines": near[:10],
        "recent_activity_near_reboot": len(near) > 0,
    }


def run_journalctl(args: list[str], timeout_sec: float) -> tuple[bool, list[str], str | None]:
    cmd = ["journalctl", "--no-pager", *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except FileNotFoundError:
        return False, [], "journalctl not installed"
    except subprocess.TimeoutExpired:
        return False, [], f"journalctl timed out after {timeout_sec:.1f}s: {' '.join(args)}"
    except OSError as exc:
        return False, [], str(exc)

    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        return False, [], err
    return True, proc.stdout.splitlines(), None


def classify_crash_verdict(
    amd_reset: dict[str, Any],
    unclean: dict[str, Any],
    signatures: dict[str, Any],
    kdump: dict[str, Any],
    edac: dict[str, Any],
    unattended: dict[str, Any],
    has_data: bool,
) -> dict[str, Any]:
    fatal = signatures.get("fatal", {})
    context = signatures.get("context", {})
    is_unclean = bool(unclean.get("is_unclean"))
    reset_class = amd_reset.get("reset_class", "NONE")

    proven_facts: list[str] = []
    circumstantial: list[str] = []
    missing_telemetry: list[str] = []
    recommendations: list[str] = []

    if is_unclean:
        parts = []
        if unclean.get("journal_corrupted"):
            parts.append("journald reported corrupted/uncleanly shut down journal")
        if unclean.get("fs_recovery"):
            parts.append("filesystem recovery/orphan inode cleanup ran on boot")
        if unclean.get("nul_gap_files"):
            parts.append(f"NUL-byte crash gap in {', '.join(unclean['nul_gap_files'])}")
        proven_facts.append("Unclean crash/reset confirmed: " + "; ".join(parts) + ".")
    elif unclean.get("clean_shutdown_markers"):
        proven_facts.append("Graceful shutdown markers present at end of previous boot.")

    if amd_reset.get("present"):
        descs = "; ".join(amd_reset.get("kernel_descriptions") or [reset_class])
        proven_facts.append(f"AMD FCH reset register [{amd_reset['raw_mask']}]: {descs}.")

    if edac.get("total_ue", 0) > 0:
        proven_facts.append(f"EDAC reported {edac['total_ue']} uncorrectable memory error(s) (UE).")
    if edac.get("total_ce", 0) > 0:
        circumstantial.append(f"EDAC reported {edac['total_ce']} correctable memory error(s) (CE).")

    for cat in signatures.get("fatal_categories", []):
        proven_facts.append(f"Fatal log signature matched '{cat}' ({len(fatal[cat])} match(es)).")
    for cat in signatures.get("context_categories", []):
        circumstantial.append(f"Non-fatal context signal matched '{cat}' ({len(context[cat])} match(es)).")

    if unattended.get("recent_activity_near_reboot"):
        circumstantial.append("unattended-upgrades logged activity within 30 minutes of the reboot.")
    elif unattended.get("readable"):
        proven_facts.append("No unattended-upgrades activity within 30 minutes of the reboot.")

    if kdump.get("reserved_but_disabled"):
        missing_telemetry.append(
            f"kdump is disabled (USE_KDUMP=0 in {kdump['config_path']}) despite crashkernel={kdump.get('crashkernel_reserved')} reserving RAM."
        )
    elif kdump.get("config_present") and not kdump.get("enabled"):
        missing_telemetry.append(f"kdump is disabled (USE_KDUMP={kdump.get('use_kdump')}).")

    # Determine primary status and confidence.
    if not has_data:
        status = "INSUFFICIENT_DATA"
        confidence = "LOW"
        summary = "Insufficient boot/log data readable to determine reboot cause."
        recommendations.append("Re-run with journal access (adm/systemd-journal group) or readable /var/log/kern.log.")
    elif "kernel_panic" in fatal:
        status = "KERNEL_PANIC_OR_OOPS"
        confidence = "HIGH"
        summary = "Kernel panic / oops signature detected before reboot."
        recommendations.append("Inspect the matched kernel panic/oops stack trace and check for matching kernel/driver regressions.")
    elif (
        "mce_hardware" in fatal
        or "thermal" in fatal
        or edac.get("total_ue", 0) > 0
        or reset_class in {"THERMAL_TRIP", "SYNC_FLOOD_HW_FAULT"}
    ):
        status = "HARDWARE_MCE_OR_THERMAL"
        confidence = "HIGH"
        summary = f"Hardware/MCE/thermal fault detected (reset_class={reset_class})."
        recommendations.append("Check CPU/GPU/VRM thermals, RAM stability (memtest86+), BIOS/AGESA updates, and PSU rails.")
    elif "oom_killer" in fatal or "watchdog_lockup" in fatal or reset_class == "WATCHDOG_RESET":
        status = "OOM_OR_LOCKUP_CRASH"
        confidence = "HIGH"
        summary = f"OOM killer or kernel hard/soft lockup detected (reset_class={reset_class})."
        recommendations.append("Run sys-mem-watch / sys-investigate during workload peaks to catch memory or lockup culprits.")
    elif is_unclean and ("gpu_fault" in fatal or "storage_fault" in fatal):
        status = "GPU_OR_STORAGE_FAULT_RESET"
        confidence = "HIGH"
        summary = "Unclean reset preceded by fatal GPU or storage driver fault."
        recommendations.append("Inspect GPU/NVMe driver & firmware versions, PCIe link stability, and power delivery.")
    elif is_unclean or reset_class in {"FAILED_BOOT_TIMER", "POWER_LOSS_OR_BUTTON"}:
        status = "UNCLEAN_HARD_RESET"
        confidence = "MEDIUM" if amd_reset.get("present") else "LOW"
        if reset_class == "CF9_FULL_RESET":
            summary = (
                "Abrupt unclean hard reset via 0xCF9=0xE (full platform reset) with no kernel panic or MCE saved."
            )
            missing_telemetry.append(
                "No kernel panic/oops or MCE was flushed to journal/pstore/kdump before the instant 0xCF9=0xE reset."
            )
        elif reset_class == "FAILED_BOOT_TIMER":
            summary = "System rebooted after BIOS/UEFI failed-boot timer expired (e.g. memory training or POST retry)."
        elif reset_class == "POWER_LOSS_OR_BUTTON":
            summary = "Abrupt reset caused by power loss or power-button override."
        else:
            summary = "Abrupt unclean shutdown/reset detected with no kernel panic or MCE recorded."
            missing_telemetry.append("No kernel panic/oops, MCE, or pstore crash dump was captured.")
    else:
        status = "CLEAN_REBOOT"
        confidence = "HIGH" if unclean.get("clean_shutdown_markers") else "MEDIUM"
        summary = "No unclean-shutdown or fatal crash indicators detected; reboot appears clean."

    is_issue = status not in {"CLEAN_REBOOT", "INSUFFICIENT_DATA"}

    # Guarantee actionable recommendations whenever is_issue=True.
    if kdump.get("reserved_but_disabled"):
        recommendations.append(
            "Enable crash dumps: set USE_KDUMP=1 in /etc/default/kdump-tools (crashkernel= is already reserving RAM) and run sudo systemctl restart kdump-tools."
        )
    if is_issue and "gpu_fault" in context:
        recommendations.append(
            "Investigate AMD GPU / ROCm KFD workqueue stalls (svm_range_deferred_list_work / kfd_process_wq_release) and verify GPU firmware, PCIe ASPM, and PSU transient headroom."
        )
    if is_issue and status == "UNCLEAN_HARD_RESET":
        recommendations.append(
            "Install and enable rasdaemon (`sudo apt install rasdaemon`) to persistently record MCE, PCIe AER, and memory errors across reboots."
        )
        recommendations.append(
            "Keep efi_pstore enabled and check BIOS/UEFI AGESA + DDR5/EXPO stability if silent 0xCF9 resets recur."
        )
    if not is_issue and not recommendations:
        recommendations.append("No immediate intervention needed.")

    return {
        "status": status,
        "is_issue": is_issue,
        "confidence": confidence,
        "summary": summary,
        "proven_facts": proven_facts,
        "circumstantial_signals": circumstantial,
        "missing_telemetry": missing_telemetry,
        "recommendations": recommendations,
    }


def investigate_crash(
    boot_index: int = 0,
    boots_history: int = 5,
    tail_lines: int = 80,
    use_journalctl: bool = True,
    journal_timeout: float = 5.0,
    proc_root: str = "/proc",
    kern_log_path: str = "/var/log/kern.log",
    syslog_path: str = "/var/log/syslog",
    kdump_cfg_path: str = "/etc/default/kdump-tools",
    pstore_archive: str = "/var/lib/systemd/pstore",
    sys_pstore: str = "/sys/fs/pstore",
    crash_dir: str = "/var/crash",
    edac_root: str = "/sys/devices/system/edac/mc",
    unattended_log: str = "/var/log/unattended-upgrades/unattended-upgrades.log",
    verbose: bool = False,
) -> dict[str, Any]:
    now_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    sys_info = read_system_info(proc_root=proc_root)
    warnings: list[str] = []

    boots: list[dict[str, Any]] = []
    target_boot_kernel: list[str] = []
    target_boot_services: list[str] = []
    prev_boot_tail: list[str] = []
    prev_boot_kernel_tail: list[str] = []
    prev_boot_err_tail: list[str] = []
    journal_used = False

    if use_journalctl:
        ok, out, err = run_journalctl(["--list-boots"], journal_timeout)
        if ok:
            journal_used = True
            boots = parse_journal_boots("\n".join(out))
        elif err:
            warnings.append(f"journalctl --list-boots: {err}")

        ok_k, out_k, err_k = run_journalctl(["-b", str(boot_index), "-k"], journal_timeout)
        if ok_k:
            target_boot_kernel = out_k
            journal_used = True
        elif err_k:
            warnings.append(f"journalctl -b {boot_index} -k: {err_k}")

        ok_s, out_s, err_s = run_journalctl(
            ["-b", str(boot_index), "-u", "systemd-journald", "-u", "systemd-pstore", "-u", "kdump-tools", "-n", "80"],
            journal_timeout,
        )
        if ok_s:
            target_boot_services = out_s
        elif err_s and verbose:
            warnings.append(f"journalctl -b {boot_index} services: {err_s}")

        prev_idx = boot_index - 1
        ok_pt, out_pt, err_pt = run_journalctl(["-b", str(prev_idx), "-n", str(tail_lines)], journal_timeout)
        if ok_pt:
            prev_boot_tail = out_pt
        elif err_pt:
            warnings.append(f"journalctl -b {prev_idx} -n {tail_lines}: {err_pt}")

        ok_pk, out_pk, _ = run_journalctl(["-b", str(prev_idx), "-k", "-n", str(tail_lines)], journal_timeout)
        if ok_pk:
            prev_boot_kernel_tail = out_pk

        ok_pe, out_pe, _ = run_journalctl(["-b", str(prev_idx), "-p", "0..3", "-n", "60"], journal_timeout)
        if ok_pe:
            prev_boot_err_tail = out_pe

    # Scan syslog and kern.log for NUL-byte crash boundaries (pure file reads).
    nul_scans = [
        scan_nul_crash_boundary(kern_log_path),
        scan_nul_crash_boundary(syslog_path),
    ]

    # When journalctl is disabled/unavailable, seed kernel/prev lines from log files.
    if not target_boot_kernel:
        for scan in nul_scans:
            if scan["path"] == kern_log_path and scan["readable"]:
                txt = read_text(kern_log_path) or ""
                # Filter out NULs and take lines after the last NUL gap (or tail).
                if scan["has_nul_gap"] and scan["lines_after_nul"]:
                    target_boot_kernel = scan["lines_after_nul"]
                else:
                    target_boot_kernel = txt.replace("\x00", "").splitlines()[-200:]
            if scan["has_nul_gap"] and scan["lines_before_nul"]:
                prev_boot_tail.extend(scan["lines_before_nul"])

    # Determine boot transition metadata.
    boot_map = {b["index"]: b for b in boots}
    cur_boot = boot_map.get(boot_index)
    prev_boot = boot_map.get(boot_index - 1)
    downtime_sec: float | None = None
    ref_ts: dt.datetime | None = None
    if cur_boot:
        ref_ts = parse_local_ts(cur_boot["first_ts"])
    if cur_boot and prev_boot:
        t_prev_end = parse_local_ts(prev_boot["last_ts"])
        t_cur_start = parse_local_ts(cur_boot["first_ts"])
        if t_prev_end and t_cur_start and t_cur_start >= t_prev_end:
            downtime_sec = round((t_cur_start - t_prev_end).total_seconds(), 1)

    target_kernel_ver = extract_kernel_version(target_boot_kernel) or (
        sys_info["kernel_release"] if boot_index == 0 else None
    )
    prev_kernel_ver = extract_kernel_version(prev_boot_kernel_tail)
    if prev_kernel_ver is None and use_journalctl and prev_boot is not None:
        ok_pv, out_pv, _ = run_journalctl(["-b", str(boot_index - 1), "-k", "-n", "1", "--reverse"], journal_timeout)
        if not ok_pv:
            ok_pv, out_pv, _ = run_journalctl(["-b", str(boot_index - 1), "-k", "--lines=+1"], journal_timeout)
        if ok_pv:
            prev_kernel_ver = extract_kernel_version(out_pv[:5])

    amd_reset = parse_amd_reset_reasons(target_boot_kernel)
    # Include kern.log lines if journalctl didn't have target boot kernel lines.
    if not amd_reset["present"] and not journal_used:
        ktxt = (read_text(kern_log_path) or "").replace("\x00", "")
        amd_reset = parse_amd_reset_reasons(ktxt.splitlines())

    combined_target_lines = target_boot_kernel + target_boot_services
    combined_prev_lines = prev_boot_tail + prev_boot_kernel_tail + prev_boot_err_tail
    for s in nul_scans:
        if s.get("has_nul_gap"):
            combined_prev_lines.extend(s.get("lines_before_nul", []))

    unclean = detect_unclean_signals(combined_target_lines, prev_boot_tail, nul_scans)
    signatures = scan_crash_signatures(target_boot_kernel, combined_prev_lines)
    kdump = inspect_kdump_config(kdump_cfg_path, crashkernel_reserved=sys_info["crashkernel_reserved"])
    artifacts = inspect_pstore_and_crash_dirs(
        pstore_archive=pstore_archive,
        sys_pstore=sys_pstore,
        crash_dir=crash_dir,
        reference_ts=ref_ts,
    )
    edac = inspect_edac_counters(edac_root=edac_root)
    unattended = inspect_unattended_upgrades(log_path=unattended_log, reference_ts=ref_ts)

    has_data = bool(
        journal_used
        or any(s["readable"] for s in nul_scans)
        or amd_reset["present"]
        or target_boot_kernel
    )
    verdict = classify_crash_verdict(
        amd_reset=amd_reset,
        unclean=unclean,
        signatures=signatures,
        kdump=kdump,
        edac=edac,
        unattended=unattended,
        has_data=has_data,
    )

    if verbose and warnings:
        for w in warnings:
            print(f"[verbose] {w}", file=sys.stderr)

    # Extract concise non-noise previous-boot tail for display.
    prev_tail_clean = [ln.strip() for ln in prev_boot_tail if ln.strip() and not is_noise_line(ln)][-15:]

    return {
        "timestamp": now_iso,
        "target_boot_index": boot_index,
        "system": sys_info,
        "verdict": verdict,
        "transition": {
            "previous_boot": prev_boot,
            "target_boot": cur_boot,
            "downtime_sec": downtime_sec,
            "downtime_human": fmt_duration(downtime_sec),
            "previous_kernel": prev_kernel_ver,
            "target_kernel": target_kernel_ver,
            "kernel_changed": bool(
                prev_kernel_ver and target_kernel_ver and prev_kernel_ver != target_kernel_ver
            ),
        },
        "boots_history": boots[-boots_history:],
        "hardware_reset": amd_reset,
        "unclean_evidence": unclean,
        "nul_log_scans": nul_scans,
        "signatures": signatures,
        "previous_boot_tail": prev_tail_clean,
        "crash_capture": {
            "kdump": kdump,
            "artifacts": artifacts,
            "edac": edac,
            "unattended_upgrades": unattended,
        },
        "warnings": warnings,
        "manual_steps": list(MANUAL_STEPS),
    }


def append_jsonl_log(path: str | Path, payload: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True) + "\n")


def render_text(report: dict[str, Any], show_steps: bool = False) -> str:
    v = report["verdict"]
    sys_info = report["system"]
    tr = report["transition"]
    hw = report["hardware_reset"]
    unc = report["unclean_evidence"]
    sigs = report["signatures"]
    kdump = report["crash_capture"]["kdump"]
    edac = report["crash_capture"]["edac"]
    art = report["crash_capture"]["artifacts"]

    lines: list[str] = [
        f"=== Crash & Reboot Triage @ {report['timestamp']} (host={sys_info['hostname']}, boot={report['target_boot_index']}) ===",
        f"VERDICT     : {v['status']} (confidence={v['confidence']}, is_issue={v['is_issue']})",
        f"SUMMARY     : {v['summary']}",
        f"CURRENT OS  : kernel={sys_info['kernel_release']} uptime={sys_info['uptime_human']}",
    ]

    if tr["previous_boot"] and tr["target_boot"]:
        pb = tr["previous_boot"]
        tb = tr["target_boot"]
        lines.extend(
            [
                "",
                "--- Boot Transition ---",
                f"  Previous boot ({pb['index']}): {pb['first_ts']} -> {pb['last_ts']} (up {pb['duration_human']})",
                f"  Target boot   ({tb['index']}): started {tb['first_ts']} (downtime gap: {tr['downtime_human']})",
                f"  Kernel        : {tr['previous_kernel'] or 'unknown'} -> {tr['target_kernel'] or 'unknown'}"
                + (" [CHANGED]" if tr["kernel_changed"] else ""),
            ]
        )

    lines.append("")
    lines.append("--- Hardware Reset Reason ---")
    if hw["present"]:
        lines.append(f"  AMD FCH S5_RESET_STATUS: {hw['raw_mask']} (class={hw['reset_class']})")
        for d in hw["kernel_descriptions"]:
            lines.append(f"  - {d}")
    else:
        lines.append("  No AMD x86/amd hardware reset reason line found in target boot kernel log.")

    lines.append("")
    lines.append("--- Unclean Shutdown Evidence ---")
    lines.append(f"  Unclean shutdown detected: {unc['is_unclean']}")
    for m in unc["journal_corrupted"][:4]:
        lines.append(f"  [journal] {m}")
    for m in unc["fs_recovery"][:4]:
        lines.append(f"  [fs]      {m}")
    for scan in report["nul_log_scans"]:
        if scan["has_nul_gap"]:
            lines.append(
                f"  [nul-gap] {scan['path']}: {scan['last_gap_nul_count']} NUL bytes at offset {scan['last_gap_offset']}"
            )
            if scan["last_raw_before_nul"]:
                lines.append(f"            last line before NUL: {scan['last_raw_before_nul'][:160]}")
    if unc["clean_shutdown_markers"]:
        lines.append(f"  [clean]   {unc['clean_shutdown_markers'][0]}")

    lines.append("")
    lines.append("--- Crash Signatures ---")
    if sigs["fatal"]:
        for cat, items in sorted(sigs["fatal"].items()):
            lines.append(f"  [FATAL:{cat}] ({len(items)})")
            for item in items[:3]:
                lines.append(f"    {item[:180]}")
    else:
        lines.append("  Fatal signatures  : none matched")
    if sigs["context"]:
        for cat, items in sorted(sigs["context"].items()):
            lines.append(f"  [CONTEXT:{cat}] ({len(items)})")
            for item in items[:3]:
                lines.append(f"    {item[:180]}")
    else:
        lines.append("  Context signatures: none matched")

    lines.append("")
    lines.append("--- Crash-Capture Readiness ---")
    lines.append(
        f"  kdump : enabled={kdump['enabled']} (USE_KDUMP={kdump['use_kdump']}) "
        f"crashkernel_reserved={kdump['crashkernel_reserved']}"
        + (" [MISCONFIGURED: RAM reserved but USE_KDUMP=0]" if kdump["reserved_but_disabled"] else "")
    )
    lines.append(
        f"  pstore: /var/lib/systemd/pstore={art['systemd_pstore']['total_entries']} entries "
        f"(recent={len(art['systemd_pstore']['recent_entries'])}); "
        f"/var/crash={art['var_crash']['total_entries']} entries "
        f"(recent={len(art['var_crash']['recent_entries'])})"
    )
    lines.append(
        f"  EDAC  : available={edac['available']} CE={edac['total_ce']} UE={edac['total_ue']}"
    )

    if v["proven_facts"]:
        lines.append("")
        lines.append("--- Proven Facts ---")
        for f_line in v["proven_facts"]:
            lines.append(f"  - {f_line}")
    if v["missing_telemetry"]:
        lines.append("")
        lines.append("--- Missing Telemetry / Blind Spots ---")
        for m_line in v["missing_telemetry"]:
            lines.append(f"  - {m_line}")

    lines.append("")
    lines.append("--- Recommendations ---")
    for rec in v["recommendations"]:
        lines.append(f"  - {rec}")

    if show_steps:
        lines.append("")
        lines.append("--- Step-by-Step Manual Troubleshooting Commands ---")
        for st in report["manual_steps"]:
            lines.append(f"  {st['step']}")
            lines.append(f"    $ {st['command']}")
            lines.append(f"    Why: {st['why']}")

    return "\n".join(lines) + "\n"


def render_markdown(report: dict[str, Any], show_steps: bool = False) -> str:
    v = report["verdict"]
    sys_info = report["system"]
    tr = report["transition"]
    hw = report["hardware_reset"]
    unc = report["unclean_evidence"]
    sigs = report["signatures"]
    kdump = report["crash_capture"]["kdump"]

    lines: list[str] = [
        f"# Crash & Reboot Triage @ {report['timestamp']}",
        "",
        f"- **Host:** `{sys_info['hostname']}` | **Kernel:** `{sys_info['kernel_release']}` | **Uptime:** `{sys_info['uptime_human']}`",
        f"- **Verdict:** `{v['status']}` (confidence: `{v['confidence']}`, issue: `{v['is_issue']}`)",
        f"- **Summary:** {v['summary']}",
        "",
        "## Boot Transition & Hardware Reset",
    ]
    if tr["previous_boot"] and tr["target_boot"]:
        pb = tr["previous_boot"]
        tb = tr["target_boot"]
        lines.append(f"- **Previous boot (`{pb['index']}`):** `{pb['first_ts']}` -> `{pb['last_ts']}` (uptime `{pb['duration_human']}`)")
        lines.append(f"- **Target boot (`{tb['index']}`):** started `{tb['first_ts']}` (downtime gap `{tr['downtime_human']}`)")
    if hw["present"]:
        lines.append(f"- **AMD FCH reset register:** `{hw['raw_mask']}` (`{hw['reset_class']}`) — {'; '.join(hw['kernel_descriptions'])}")
    else:
        lines.append("- **Hardware reset register:** not present in target boot kernel log")
    lines.append(f"- **Unclean shutdown:** `{unc['is_unclean']}` (NUL gaps: `{', '.join(unc['nul_gap_files']) or 'none'}`)")
    lines.append(
        f"- **kdump readiness:** `enabled={kdump['enabled']}` (`USE_KDUMP={kdump['use_kdump']}`), `crashkernel_reserved={kdump['crashkernel_reserved']}`"
    )

    lines.append("")
    lines.append("## Evidence & Signatures")
    for f_line in v["proven_facts"]:
        lines.append(f"- **Proven:** {f_line}")
    for c_line in v["circumstantial_signals"]:
        lines.append(f"- **Context:** {c_line}")
    for m_line in v["missing_telemetry"]:
        lines.append(f"- **Blind spot:** {m_line}")
    if sigs["fatal"]:
        for cat, items in sorted(sigs["fatal"].items()):
            lines.append(f"- **Fatal `{cat}`:** `{items[0][:160]}`")

    lines.append("")
    lines.append("## Recommendations")
    for rec in v["recommendations"]:
        lines.append(f"- {rec}")

    if show_steps:
        lines.append("")
        lines.append("## Step-by-Step Manual Commands")
        for st in report["manual_steps"]:
            lines.append(f"### {st['step']}")
            lines.append("```bash")
            lines.append(st["command"])
            lines.append("```")
            lines.append(st["why"])
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sys-crash-triage",
        description="Linux post-crash and unexpected-reboot forensic investigator (stdlib only).",
    )
    p.add_argument(
        "--boot",
        type=int,
        default=0,
        help="Target boot index to inspect (0 = current boot after crash, -1 = previous boot; must be <= 0).",
    )
    p.add_argument(
        "--boots-history",
        type=int,
        default=5,
        help="Number of recent boots to include in timeline (1..50, default: 5).",
    )
    p.add_argument(
        "--tail-lines",
        type=int,
        default=80,
        help="Tail lines to inspect from previous boot end (10..500, default: 80).",
    )
    p.add_argument(
        "--no-journalctl",
        action="store_true",
        help="Skip journalctl subprocesses; inspect /proc, /sys, /var/log/*, pstore, and /var/crash only.",
    )
    p.add_argument(
        "--journal-timeout",
        type=float,
        default=5.0,
        help="Per-call timeout in seconds for journalctl queries (0.5..30.0, default: 5.0).",
    )
    p.add_argument(
        "--show-steps",
        action="store_true",
        help="Include step-by-step manual troubleshooting commands alongside findings.",
    )
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="Emit deterministic JSON report.")
    fmt.add_argument("--markdown", action="store_true", help="Emit Markdown report.")
    p.add_argument(
        "--append-log",
        type=str,
        default=None,
        help="Append one JSONL record to PATH per run.",
    )
    p.add_argument(
        "--exit-on-issue",
        action="store_true",
        help="Exit with code 3 when an unclean crash/reset or fatal fault is detected.",
    )
    p.add_argument("--verbose", action="store_true", help="Print diagnostic warnings to stderr.")
    return p


def validate_args(args: argparse.Namespace) -> str | None:
    if args.boot > 0:
        return f"--boot must be <= 0 (got {args.boot})"
    if not (1 <= args.boots_history <= 50):
        return f"--boots-history must be between 1 and 50 (got {args.boots_history})"
    if not (10 <= args.tail_lines <= 500):
        return f"--tail-lines must be between 10 and 500 (got {args.tail_lines})"
    if not (0.5 <= args.journal_timeout <= 30.0):
        return f"--journal-timeout must be between 0.5 and 30.0 (got {args.journal_timeout})"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    err = validate_args(args)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    try:
        report = investigate_crash(
            boot_index=args.boot,
            boots_history=args.boots_history,
            tail_lines=args.tail_lines,
            use_journalctl=not args.no_journalctl,
            journal_timeout=args.journal_timeout,
            verbose=args.verbose,
        )
        if args.append_log:
            append_jsonl_log(args.append_log, report)

        if args.json:
            sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        elif args.markdown:
            sys.stdout.write(render_markdown(report, show_steps=args.show_steps))
        else:
            sys.stdout.write(render_text(report, show_steps=args.show_steps))
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.exit_on_issue and report["verdict"]["is_issue"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
