#!/usr/bin/env python3
"""sys-disk-hogs: fast, zero-dependency workspace/target/cache disk usage analyzer.

Walks directories iteratively via stdlib os.scandir() (no du/find subprocesses),
deduplicates hardlinks, stays on one filesystem per root by default, avoids
blocking statvfs() on non-local/FUSE/network mounts, and detects regenerable
build/cache hogs separately from non-reclaimable VCS metadata (.git).

Examples:
  sys-disk-hogs
  sys-disk-hogs ~ /datanapps --top 20 --depth 3
  sys-disk-hogs . --min-mib 0 --min-file-mib 1 --json
  sys-disk-hogs / --no-statvfs --markdown
  sys-disk-hogs /mnt/data --cross-mounts --verbose
"""

from __future__ import annotations

import argparse
import datetime as dt
import heapq
import json
import math
import os
import signal
import stat
import sys
import time
from pathlib import Path
from typing import Any

MIB = 1024.0 * 1024.0
GIB = 1024.0 * 1024.0 * 1024.0

NON_LOCAL_FSTYPES = frozenset(
    {
        "fuse",
        "fuseblk",
        "fusectl",
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "sshfs",
        "autofs",
        "davfs",
        "glusterfs",
        "ceph",
        "lustre",
        "9p",
        "virtiofs",
        "afs",
        "coda",
        "ncpfs",
        "gfs",
        "gfs2",
        "ocfs2",
    }
)

RUST_TARGET_MARKERS = frozenset({"debug", "release", "deps", ".rustc_info.json"})
PYTHON_VENV_NAMES = frozenset({".venv", "venv", ".tox", ".nox"})
PYTHON_CACHE_NAMES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
BUILD_ARTIFACT_NAMES = frozenset(
    {
        "build",
        "dist",
        ".next",
        ".nuxt",
        ".turbo",
        ".parcel-cache",
        ".gradle",
        "CMakeFiles",
    }
)
GENERAL_CACHE_NAMES = frozenset(
    {
        ".cache",
        ".cargo",
        ".npm",
        ".pnpm-store",
        ".yarn",
        "go-build",
    }
)


def _read_text(path: str | Path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _unescape_mount_path(raw: str) -> str:
    # /proc/mounts escapes space, tab, newline, and backslash as octal sequences.
    return (
        raw.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def is_safe_statvfs_fstype(fstype: str) -> bool:
    """Return False for network, FUSE, or automount filesystems where statvfs can hang."""
    ft = fstype.lower()
    if ft in NON_LOCAL_FSTYPES or ft.startswith("fuse."):
        return False
    return True


def parse_proc_mounts(mounts_txt: str | None = None) -> list[tuple[str, str, str]]:
    """Parse /proc/mounts into (device, mount_point, fstype) tuples."""
    txt = _read_text("/proc/mounts") or "" if mounts_txt is None else mounts_txt
    entries: list[tuple[str, str, str]] = []
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        entries.append((parts[0], _unescape_mount_path(parts[1]), parts[2]))
    return entries


def resolve_mount_info(
    path: str | Path,
    mounts: list[tuple[str, str, str]],
) -> dict[str, str] | None:
    """Find the deepest active mountpoint matching path."""
    try:
        resolved = os.path.realpath(str(path))
    except OSError:
        resolved = os.path.abspath(str(path))

    best: dict[str, str] | None = None
    best_len = -1
    for dev, mnt, fstype in mounts:
        if mnt == "/":
            matched = resolved.startswith("/")
        else:
            norm = mnt.rstrip("/")
            matched = resolved == norm or resolved.startswith(norm + "/")
        # Use >= so later mounts at the exact same path override earlier entries (e.g. rootfs).
        if matched and len(mnt) >= best_len:
            best_len = len(mnt)
            best = {"device": dev, "mount": mnt, "fstype": fstype}
    return best


def safe_statvfs_for_path(
    path: str | Path,
    allow_statvfs: bool = True,
    mounts_txt: str | None = None,
    mounts: list[tuple[str, str, str]] | None = None,
) -> dict[str, Any]:
    """Safely inspect filesystem fill % for path, skipping non-local/FUSE mounts."""
    parsed_mounts = mounts if mounts is not None else parse_proc_mounts(mounts_txt=mounts_txt)
    mnt_info = resolve_mount_info(path, parsed_mounts)
    mount_point = mnt_info["mount"] if mnt_info else None
    fstype = mnt_info["fstype"] if mnt_info else None

    result: dict[str, Any] = {
        "mount": mount_point,
        "fstype": fstype,
        "fs_used_pct": None,
        "fs_total_gib": None,
        "fs_avail_gib": None,
    }
    if not allow_statvfs:
        return result
    if fstype is not None and not is_safe_statvfs_fstype(fstype):
        return result

    try:
        st = os.statvfs(str(path))
    except OSError:
        return result

    if st.f_blocks > 0:
        frsize = st.f_frsize or st.f_bsize or 4096
        total_bytes = st.f_blocks * frsize
        avail_bytes = st.f_bavail * frsize
        result["fs_used_pct"] = round(100.0 * (st.f_blocks - st.f_bfree) / st.f_blocks, 1)
        result["fs_total_gib"] = round(total_bytes / GIB, 2)
        result["fs_avail_gib"] = round(avail_bytes / GIB, 2)
    return result


def classify_hog_dir(
    dir_path: str | Path,
    dir_name: str,
    child_names: set[str] | frozenset[str] | list[str] | None = None,
) -> tuple[str, bool] | None:
    """Classify a directory into (category, reclaimable) or None.

    Returns:
      ("rust-target", True)     - Rust target/ with Cargo.toml in parent or rust markers inside, or cargo-target
      ("node-modules", True)    - node_modules
      ("python-venv", True)     - .venv, venv, .tox, .nox
      ("python-cache", True)    - __pycache__, .pytest_cache, .mypy_cache, .ruff_cache
      ("build-artifacts", True) - build, dist, .next, .nuxt, .turbo, .parcel-cache, .gradle, CMakeFiles
      ("general-cache", True)   - .cache, .cargo, .npm, .pnpm-store, .yarn, go-build
      ("vcs-metadata", False)   - .git (non-reclaimable; tracked separately)
      None                      - regular directory
    """
    if dir_name == "cargo-target":
        return ("rust-target", True)
    if dir_name == "target":
        path_str = str(dir_path).rstrip(os.sep) or os.sep
        parent_dir = os.path.dirname(path_str)
        try:
            if parent_dir and os.path.isfile(os.path.join(parent_dir, "Cargo.toml")):
                return ("rust-target", True)
        except OSError:
            pass
        if child_names is not None:
            if any(marker in child_names for marker in RUST_TARGET_MARKERS):
                return ("rust-target", True)
        else:
            for marker in RUST_TARGET_MARKERS:
                try:
                    if os.path.exists(os.path.join(path_str, marker)):
                        return ("rust-target", True)
                except OSError:
                    continue
        return None
    if dir_name == "node_modules":
        return ("node-modules", True)
    if dir_name in PYTHON_VENV_NAMES:
        return ("python-venv", True)
    if dir_name in PYTHON_CACHE_NAMES:
        return ("python-cache", True)
    if dir_name in BUILD_ARTIFACT_NAMES:
        return ("build-artifacts", True)
    if dir_name in GENERAL_CACHE_NAMES:
        return ("general-cache", True)
    if dir_name == ".git":
        return ("vcs-metadata", False)
    return None


class _DirNode:
    __slots__ = (
        "path",
        "name",
        "depth",
        "parent",
        "total_bytes",
        "total_files",
        "hog_category",
        "hog_reclaimable",
        "in_hog",
    )

    def __init__(
        self,
        path: str,
        name: str,
        depth: int,
        parent: _DirNode | None,
        in_hog: bool,
    ) -> None:
        self.path = path
        self.name = name
        self.depth = depth
        self.parent = parent
        self.total_bytes = 0
        self.total_files = 0
        self.hog_category: str | None = None
        self.hog_reclaimable = False
        self.in_hog = in_hog


def _bytes_to_mib(nbytes: int) -> float:
    return round(nbytes / MIB, 2)


def _bytes_to_gib(nbytes: int) -> float:
    return round(nbytes / GIB, 3)


def _push_bounded_file(
    heap: list[tuple[int, str]],
    top_n: int,
    size_bytes: int,
    path: str,
) -> None:
    if top_n <= 0:
        return
    item = (size_bytes, path)
    if len(heap) < top_n:
        heapq.heappush(heap, item)
    elif item > heap[0]:
        heapq.heapreplace(heap, item)


def scan_paths(
    paths: list[str],
    *,
    top_n: int = 15,
    max_depth: int = 2,
    min_mib: float = 1.0,
    min_file_mib: float = 10.0,
    one_file_system: bool = True,
    no_statvfs: bool = False,
    mounts_txt: str | None = None,
) -> dict[str, Any]:
    """Iteratively scan one or more paths and produce a structured disk usage report."""
    t_start = time.monotonic()
    min_dir_bytes = int(min_mib * MIB)
    min_file_bytes = int(min_file_mib * MIB)

    parsed_mounts = parse_proc_mounts(mounts_txt=mounts_txt)

    # Validate every requested root upfront so missing paths fail fast and cleanly.
    resolved_roots: list[tuple[str, os.stat_result]] = []
    for raw_path in paths:
        expanded = os.path.abspath(os.path.expanduser(raw_path))
        try:
            st = os.stat(expanded)
        except OSError as exc:
            raise OSError(f"cannot access '{raw_path}': {exc.strerror or exc}") from exc
        resolved_roots.append((expanded, st))

    seen_inodes: set[tuple[int, int]] = set()
    largest_files_heap: list[tuple[int, str]] = []
    top_dir_candidates: list[dict[str, Any]] = []
    reclaimable_hog_candidates: list[dict[str, Any]] = []
    vcs_metadata_candidates: list[dict[str, Any]] = []

    root_summaries: list[dict[str, Any]] = []
    total_bytes = 0
    total_files = 0
    total_dirs = 0
    skipped_entries = 0
    deduped_hardlinks = 0
    reclaimable_bytes = 0
    reclaimable_hog_count = 0
    vcs_metadata_bytes = 0

    for root_path, root_st in resolved_roots:
        fs_info = safe_statvfs_for_path(
            root_path,
            allow_statvfs=not no_statvfs,
            mounts=parsed_mounts,
        )

        # Support passing an individual file as a root path.
        if not stat.S_ISDIR(root_st.st_mode):
            file_bytes = 0
            file_count = 0
            if stat.S_ISREG(root_st.st_mode):
                counted = True
                if root_st.st_nlink > 1:
                    inode_key = (root_st.st_dev, root_st.st_ino)
                    if inode_key in seen_inodes:
                        deduped_hardlinks += 1
                        counted = False
                    else:
                        seen_inodes.add(inode_key)
                if counted:
                    file_bytes = root_st.st_size
                    file_count = 1
                    if file_bytes >= min_file_bytes:
                        _push_bounded_file(largest_files_heap, top_n, file_bytes, root_path)

            total_bytes += file_bytes
            total_files += file_count
            root_summaries.append(
                {
                    "path": root_path,
                    "size_bytes": file_bytes,
                    "size_mib": _bytes_to_mib(file_bytes),
                    "size_gib": _bytes_to_gib(file_bytes),
                    "files": file_count,
                    "dirs": 0,
                    **fs_info,
                }
            )
            continue

        root_dev = root_st.st_dev
        root_name = os.path.basename(root_path.rstrip(os.sep)) or root_path
        root_node = _DirNode(root_path, root_name, depth=0, parent=None, in_hog=False)
        root_dirs = 0

        # Explicit iterative post-order DFS stack: (node, expanded).
        stack: list[tuple[_DirNode, bool]] = [(root_node, False)]

        while stack:
            node, expanded = stack.pop()
            if not expanded:
                root_dirs += 1
                child_dirs: list[tuple[str, str]] = []
                need_child_names = (not node.in_hog) and (node.name == "target")
                child_names: set[str] | None = set() if need_child_names else None

                try:
                    with os.scandir(node.path) as it:
                        for entry in it:
                            if child_names is not None:
                                child_names.add(entry.name)
                            try:
                                st = entry.stat(follow_symlinks=False)
                            except OSError:
                                skipped_entries += 1
                                continue

                            mode = st.st_mode
                            if stat.S_ISLNK(mode):
                                continue
                            if stat.S_ISDIR(mode):
                                if one_file_system and st.st_dev != root_dev:
                                    continue
                                child_dirs.append((entry.path, entry.name))
                            elif stat.S_ISREG(mode):
                                if one_file_system and st.st_dev != root_dev:
                                    continue
                                if st.st_nlink > 1:
                                    inode_key = (st.st_dev, st.st_ino)
                                    if inode_key in seen_inodes:
                                        deduped_hardlinks += 1
                                        continue
                                    seen_inodes.add(inode_key)
                                fsize = st.st_size
                                node.total_bytes += fsize
                                node.total_files += 1
                                if fsize >= min_file_bytes:
                                    _push_bounded_file(largest_files_heap, top_n, fsize, entry.path)
                except OSError:
                    skipped_entries += 1

                child_in_hog = node.in_hog
                if not node.in_hog:
                    hog = classify_hog_dir(node.path, node.name, child_names=child_names)
                    if hog is not None:
                        node.hog_category, node.hog_reclaimable = hog
                        child_in_hog = True

                stack.append((node, True))
                # Sort and push children in reverse so traversal order is deterministic.
                child_dirs.sort()
                for child_path, child_name in reversed(child_dirs):
                    stack.append(
                        (
                            _DirNode(
                                path=child_path,
                                name=child_name,
                                depth=node.depth + 1,
                                parent=node,
                                in_hog=child_in_hog,
                            ),
                            False,
                        )
                    )
            else:
                if node.parent is not None:
                    node.parent.total_bytes += node.total_bytes
                    node.parent.total_files += node.total_files

                if node.hog_category is not None:
                    hog_entry = {
                        "category": node.hog_category,
                        "reclaimable": node.hog_reclaimable,
                        "size_bytes": node.total_bytes,
                        "size_mib": _bytes_to_mib(node.total_bytes),
                        "size_gib": _bytes_to_gib(node.total_bytes),
                        "files": node.total_files,
                        "path": node.path,
                    }
                    if node.hog_reclaimable:
                        reclaimable_bytes += node.total_bytes
                        reclaimable_hog_count += 1
                        reclaimable_hog_candidates.append(hog_entry)
                    else:
                        vcs_metadata_bytes += node.total_bytes
                        vcs_metadata_candidates.append(hog_entry)

                if 1 <= node.depth <= max_depth and node.total_bytes >= min_dir_bytes:
                    top_dir_candidates.append(
                        {
                            "path": node.path,
                            "depth": node.depth,
                            "size_bytes": node.total_bytes,
                            "size_mib": _bytes_to_mib(node.total_bytes),
                            "size_gib": _bytes_to_gib(node.total_bytes),
                            "files": node.total_files,
                        }
                    )

        total_bytes += root_node.total_bytes
        total_files += root_node.total_files
        total_dirs += root_dirs
        root_summaries.append(
            {
                "path": root_path,
                "size_bytes": root_node.total_bytes,
                "size_mib": _bytes_to_mib(root_node.total_bytes),
                "size_gib": _bytes_to_gib(root_node.total_bytes),
                "files": root_node.total_files,
                "dirs": root_dirs,
                **fs_info,
            }
        )

    top_dirs = sorted(
        top_dir_candidates,
        key=lambda x: (-x["size_bytes"], x["depth"], x["path"]),
    )[:top_n]
    reclaimable_hogs = sorted(
        reclaimable_hog_candidates,
        key=lambda x: (-x["size_bytes"], x["category"], x["path"]),
    )[:top_n]
    vcs_metadata = sorted(
        vcs_metadata_candidates,
        key=lambda x: (-x["size_bytes"], x["path"]),
    )[:top_n]
    largest_files = [
        {
            "size_bytes": size_bytes,
            "size_mib": _bytes_to_mib(size_bytes),
            "size_gib": _bytes_to_gib(size_bytes),
            "path": fpath,
        }
        for size_bytes, fpath in sorted(largest_files_heap, key=lambda x: (-x[0], x[1]))
    ]

    elapsed_sec = round(max(0.0, time.monotonic() - t_start), 3)
    return {
        "timestamp": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "options": {
            "top": top_n,
            "depth": max_depth,
            "min_mib": min_mib,
            "min_file_mib": min_file_mib,
            "one_file_system": one_file_system,
            "no_statvfs": no_statvfs,
        },
        "summary": {
            "roots": root_summaries,
            "total_bytes": total_bytes,
            "total_mib": _bytes_to_mib(total_bytes),
            "total_gib": _bytes_to_gib(total_bytes),
            "files": total_files,
            "dirs": total_dirs,
            "skipped_entries": skipped_entries,
            "deduped_hardlinks": deduped_hardlinks,
            "reclaimable_bytes": reclaimable_bytes,
            "reclaimable_mib": _bytes_to_mib(reclaimable_bytes),
            "reclaimable_gib": _bytes_to_gib(reclaimable_bytes),
            "reclaimable_hog_count": reclaimable_hog_count,
            "vcs_metadata_bytes": vcs_metadata_bytes,
            "vcs_metadata_mib": _bytes_to_mib(vcs_metadata_bytes),
            "vcs_metadata_gib": _bytes_to_gib(vcs_metadata_bytes),
            "elapsed_sec": elapsed_sec,
            "one_file_system": one_file_system,
        },
        "top_dirs": top_dirs,
        "reclaimable_hogs": reclaimable_hogs,
        "vcs_metadata": vcs_metadata,
        "largest_files": largest_files,
    }


def render_text(rep: dict[str, Any], markdown: bool = False) -> str:
    """Render report as plain ASCII table output or GitHub-flavored Markdown."""
    s = rep["summary"]
    opts = rep.get("options", {})
    h = (lambda title: f"## {title}") if markdown else (lambda title: f"=== {title} ===")

    lines: list[str] = []
    lines.append(
        f"{'# ' if markdown else ''}Disk Hogs Analysis @ {rep['timestamp']} (elapsed={s['elapsed_sec']:.3f}s)"
    )
    lines.append("")

    lines.append(h("Summary"))
    lines.append(
        f"Total       : {s['total_gib']:.3f} GiB ({s['total_mib']:.2f} MiB) | "
        f"files={s['files']} dirs={s['dirs']} skipped={s['skipped_entries']} "
        f"deduped_hardlinks={s['deduped_hardlinks']}"
    )
    lines.append(
        f"Reclaimable : {s['reclaimable_gib']:.3f} GiB ({s['reclaimable_mib']:.2f} MiB) "
        f"across {s['reclaimable_hog_count']} build/cache dirs"
    )
    if s["vcs_metadata_bytes"] > 0:
        lines.append(
            f"VCS (.git)  : {s['vcs_metadata_gib']:.3f} GiB ({s['vcs_metadata_mib']:.2f} MiB) "
            "(non-reclaimable)"
        )
    lines.append(f"Scope       : one_file_system={s['one_file_system']}")
    for r in s["roots"]:
        fs_part = ""
        if r["fs_used_pct"] is not None:
            mnt = r["mount"] or "?"
            fst = r["fstype"] or "?"
            fs_part = (
                f" | fs {mnt} ({fst}): {r['fs_used_pct']:.1f}% used "
                f"({r['fs_avail_gib']:.2f}/{r['fs_total_gib']:.2f} GiB avail)"
            )
        elif r["mount"] is not None:
            fs_part = f" | fs {r['mount']} ({r['fstype'] or '?'}): statvfs skipped"
        lines.append(
            f"Root        : {r['path']} -> {r['size_gib']:.3f} GiB ({r['size_mib']:.2f} MiB, "
            f"{r['files']} files, {r['dirs']} dirs){fs_part}"
        )
    lines.append("")

    depth_lbl = opts.get("depth", 2)
    min_mib_lbl = opts.get("min_mib", 1.0)
    lines.append(h(f"Top Subdirectories (depth <= {depth_lbl}, >= {min_mib_lbl} MiB)"))
    if rep["top_dirs"]:
        lines.append(f"{'SIZE_MIB':>10} {'SIZE_GIB':>9} {'FILES':>8} {'DEPTH':>5}  PATH")
        for d in rep["top_dirs"]:
            lines.append(
                f"{d['size_mib']:>10.2f} {d['size_gib']:>9.3f} {d['files']:>8} {d['depth']:>5}  {d['path']}"
            )
    else:
        lines.append("(none above threshold)")
    lines.append("")

    lines.append(h("Reclaimable Build/Cache Hogs"))
    if rep["reclaimable_hogs"]:
        lines.append(f"{'SIZE_MIB':>10} {'SIZE_GIB':>9} {'FILES':>8} {'CATEGORY':<16}  PATH")
        for hog in rep["reclaimable_hogs"]:
            lines.append(
                f"{hog['size_mib']:>10.2f} {hog['size_gib']:>9.3f} {hog['files']:>8} "
                f"{hog['category']:<16}  {hog['path']}"
            )
    else:
        lines.append("(none detected)")
    lines.append("")

    if rep["vcs_metadata"]:
        lines.append(h("VCS Metadata (non-reclaimable)"))
        lines.append(f"{'SIZE_MIB':>10} {'SIZE_GIB':>9} {'FILES':>8} {'CATEGORY':<16}  PATH")
        for v in rep["vcs_metadata"]:
            lines.append(
                f"{v['size_mib']:>10.2f} {v['size_gib']:>9.3f} {v['files']:>8} "
                f"{v['category']:<16}  {v['path']}"
            )
        lines.append("")

    min_file_lbl = opts.get("min_file_mib", 10.0)
    lines.append(h(f"Largest Files (>= {min_file_lbl} MiB)"))
    if rep["largest_files"]:
        lines.append(f"{'SIZE_MIB':>10} {'SIZE_GIB':>9}  PATH")
        for f in rep["largest_files"]:
            lines.append(f"{f['size_mib']:>10.2f} {f['size_gib']:>9.3f}  {f['path']}")
    else:
        lines.append("(none above threshold)")

    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sys-disk-hogs",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Root paths to analyze (default: current directory '.')",
    )
    p.add_argument(
        "--top",
        type=int,
        default=15,
        help="Max rows per section (1..100, default: 15)",
    )
    p.add_argument(
        "--depth",
        type=int,
        default=2,
        help="Max subdirectory depth for top_dirs (1..10, default: 2)",
    )
    p.add_argument(
        "--min-mib",
        type=float,
        default=1.0,
        help="Minimum subdirectory size in MiB for top_dirs (>= 0, default: 1.0)",
    )
    p.add_argument(
        "--min-file-mib",
        type=float,
        default=10.0,
        help="Minimum file size in MiB for largest_files (>= 0, default: 10.0)",
    )
    fs_group = p.add_mutually_exclusive_group()
    fs_group.add_argument(
        "--one-file-system",
        dest="one_file_system",
        action="store_true",
        default=True,
        help="Stay on each root's filesystem by comparing st_dev (default: enabled)",
    )
    fs_group.add_argument(
        "--cross-mounts",
        dest="one_file_system",
        action="store_false",
        help="Allow traversal across mount boundaries",
    )
    p.add_argument(
        "--no-statvfs",
        action="store_true",
        help="Skip os.statvfs() filesystem fill checks",
    )
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="Emit deterministic JSON report to stdout")
    fmt.add_argument("--markdown", action="store_true", help="Emit Markdown report to stdout")
    p.add_argument("--verbose", action="store_true", help="Print effective configuration to stderr")
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    parser = build_parser()
    args = parser.parse_args(argv)

    if not (1 <= args.top <= 100):
        print("error: --top must be between 1 and 100", file=sys.stderr)
        return 1
    if not (1 <= args.depth <= 10):
        print("error: --depth must be between 1 and 10", file=sys.stderr)
        return 1
    if not math.isfinite(args.min_mib) or args.min_mib < 0.0:
        print("error: --min-mib must be a finite number >= 0", file=sys.stderr)
        return 1
    if not math.isfinite(args.min_file_mib) or args.min_file_mib < 0.0:
        print("error: --min-file-mib must be a finite number >= 0", file=sys.stderr)
        return 1

    paths = args.paths if args.paths else ["."]

    if args.verbose:
        print(
            f"config: paths={paths} top={args.top} depth={args.depth} "
            f"min_mib={args.min_mib} min_file_mib={args.min_file_mib} "
            f"one_file_system={args.one_file_system} no_statvfs={args.no_statvfs} "
            f"format={'json' if args.json else 'markdown' if args.markdown else 'text'}",
            file=sys.stderr,
        )

    try:
        rep = scan_paths(
            paths,
            top_n=args.top,
            max_depth=args.depth,
            min_mib=args.min_mib,
            min_file_mib=args.min_file_mib,
            one_file_system=args.one_file_system,
            no_statvfs=args.no_statvfs,
        )
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True), flush=True)
    else:
        print(render_text(rep, markdown=args.markdown), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
