#!/usr/bin/env python3
"""Unit and CLI error-path tests for sysadmin_tools.sys_disk_hogs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
SCRIPT = SRC_DIR / "sysadmin_tools" / "sys_disk_hogs.py"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sysadmin_tools import sys_disk_hogs as dh  # noqa: E402


def _write_sized_file(path: Path, size_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        if size_bytes > 0:
            f.truncate(size_bytes)


class TestClassifyAndStatvfs(unittest.TestCase):
    def test_classify_hog_dir_categories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            # Rust target with Cargo.toml in parent
            rust_proj = root / "rust_proj"
            (rust_proj / "target").mkdir(parents=True)
            (rust_proj / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
            self.assertEqual(
                dh.classify_hog_dir(rust_proj / "target", "target", child_names=set()),
                ("rust-target", True),
            )

            # Rust target without Cargo.toml, detected via child_names or filesystem markers
            standalone_target = root / "standalone" / "target"
            (standalone_target / "debug").mkdir(parents=True)
            self.assertEqual(
                dh.classify_hog_dir(standalone_target, "target", child_names={"debug"}),
                ("rust-target", True),
            )
            self.assertEqual(
                dh.classify_hog_dir(standalone_target, "target", child_names=None),
                ("rust-target", True),
            )
            for marker in ("release", "deps", ".rustc_info.json"):
                self.assertEqual(
                    dh.classify_hog_dir(root / "other" / "target", "target", child_names={marker}),
                    ("rust-target", True),
                )

            # cargo-target directory name
            self.assertEqual(
                dh.classify_hog_dir(root / "cargo-target", "cargo-target"),
                ("rust-target", True),
            )

            # Plain target dir without Cargo.toml and without rust markers -> NOT a hog
            plain_target = root / "docs" / "target"
            plain_target.mkdir(parents=True)
            self.assertIsNone(dh.classify_hog_dir(plain_target, "target", child_names={"notes.txt"}))
            self.assertIsNone(dh.classify_hog_dir(plain_target, "target", child_names=None))

            # Other categories
            self.assertEqual(dh.classify_hog_dir(root / "node_modules", "node_modules"), ("node-modules", True))
            for name in (".venv", "venv", ".tox", ".nox"):
                self.assertEqual(dh.classify_hog_dir(root / name, name), ("python-venv", True))
            for name in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"):
                self.assertEqual(dh.classify_hog_dir(root / name, name), ("python-cache", True))
            for name in ("build", "dist", ".next", ".nuxt", ".turbo", ".parcel-cache", ".gradle", "CMakeFiles"):
                self.assertEqual(dh.classify_hog_dir(root / name, name), ("build-artifacts", True))
            for name in (".cache", ".cargo", ".npm", ".pnpm-store", ".yarn", "go-build"):
                self.assertEqual(dh.classify_hog_dir(root / name, name), ("general-cache", True))

            # VCS metadata: non-reclaimable
            self.assertEqual(dh.classify_hog_dir(root / ".git", ".git"), ("vcs-metadata", False))

            # Ordinary directories
            for name in ("src", "tests", "docs", "assets"):
                self.assertIsNone(dh.classify_hog_dir(root / name, name))

    def test_is_safe_statvfs_fstype(self) -> None:
        for safe in ("ext4", "xfs", "btrfs", "tmpfs", "f2fs", "vfat"):
            self.assertTrue(dh.is_safe_statvfs_fstype(safe), msg=safe)
        for unsafe in ("nfs", "nfs4", "cifs", "sshfs", "fuse", "fuse.rclone", "fuse.sshfs", "fuseblk", "autofs", "9p"):
            self.assertFalse(dh.is_safe_statvfs_fstype(unsafe), msg=unsafe)

    def test_safe_statvfs_for_path_respects_fstype_and_no_statvfs(self) -> None:
        mounts_txt = "\n".join(
            [
                "rootfs / rootfs rw 0 0",
                "/dev/nvme0n1p2 / ext4 rw,relatime 0 0",
                "/dev/sda1 /mnt/rclone fuse.rclone rw 0 0",
                "10.0.0.1:/export /mnt/nfs nfs4 rw 0 0",
            ]
        )
        fake_stat = mock.MagicMock(f_blocks=1000, f_bfree=200, f_bavail=150, f_frsize=1024 * 1024, f_bsize=1024 * 1024)
        with mock.patch("os.statvfs", return_value=fake_stat) as m_statvfs, mock.patch(
            "os.path.realpath", side_effect=lambda p: p
        ):
            local = dh.safe_statvfs_for_path("/home/bill/work", allow_statvfs=True, mounts_txt=mounts_txt)
            self.assertEqual(local["mount"], "/")
            self.assertEqual(local["fstype"], "ext4")
            self.assertEqual(local["fs_used_pct"], 80.0)
            self.assertEqual(m_statvfs.call_count, 1)

            m_statvfs.reset_mock()
            fuse = dh.safe_statvfs_for_path("/mnt/rclone/data", allow_statvfs=True, mounts_txt=mounts_txt)
            self.assertEqual(fuse["mount"], "/mnt/rclone")
            self.assertEqual(fuse["fstype"], "fuse.rclone")
            self.assertIsNone(fuse["fs_used_pct"])
            m_statvfs.assert_not_called()

            nfs = dh.safe_statvfs_for_path("/mnt/nfs/share", allow_statvfs=True, mounts_txt=mounts_txt)
            self.assertEqual(nfs["fstype"], "nfs4")
            self.assertIsNone(nfs["fs_used_pct"])
            m_statvfs.assert_not_called()

            disabled = dh.safe_statvfs_for_path("/home/bill/work", allow_statvfs=False, mounts_txt=mounts_txt)
            self.assertIsNone(disabled["fs_used_pct"])
            m_statvfs.assert_not_called()


class TestSyntheticTreeScan(unittest.TestCase):
    def test_hogs_nested_rollup_and_git_non_reclaimable(self) -> None:
        mib = 1024 * 1024
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            # Rust project with target + nested target/debug/build (build must NOT be double-counted)
            (root / "rustapp").mkdir()
            (root / "rustapp" / "Cargo.toml").write_text("[package]\nname='rustapp'\n", encoding="utf-8")
            _write_sized_file(root / "rustapp" / "target" / "debug" / "app.bin", 4 * mib)
            _write_sized_file(root / "rustapp" / "target" / "debug" / "build" / "gen.o", 2 * mib)

            # JS project with nested node_modules inside node_modules (must roll up into outer only)
            _write_sized_file(root / "webapp" / "node_modules" / "pkg" / "index.js", 3 * mib)
            _write_sized_file(root / "webapp" / "node_modules" / "pkg" / "node_modules" / "sub" / "a.js", 2 * mib)
            _write_sized_file(root / "webapp" / "node_modules" / "pkg" / "dist" / "bundle.js", 1 * mib)

            # Python venv with nested __pycache__ (must roll up into .venv only)
            _write_sized_file(root / "pyapp" / ".venv" / "lib" / "pkg.py", 2 * mib)
            _write_sized_file(root / "pyapp" / ".venv" / "lib" / "__pycache__" / "pkg.pyc", 1 * mib)

            # .git directory (non-reclaimable; must NOT be counted in reclaimable_bytes)
            _write_sized_file(root / ".git" / "objects" / "pack" / "pack-1.pack", 10 * mib)

            # Non-hog regular source file
            _write_sized_file(root / "src" / "main.py", 1 * mib)

            rep = dh.scan_paths([str(root)], top_n=20, max_depth=2, min_mib=1.0, min_file_mib=3.0, no_statvfs=True)

            hogs = rep["reclaimable_hogs"]
            hog_map = {h["path"]: h for h in hogs}
            self.assertEqual(len(hogs), 3)
            self.assertIn(str(root / "rustapp" / "target"), hog_map)
            self.assertEqual(hog_map[str(root / "rustapp" / "target")]["category"], "rust-target")
            self.assertEqual(hog_map[str(root / "rustapp" / "target")]["size_bytes"], 6 * mib)
            self.assertEqual(hog_map[str(root / "rustapp" / "target")]["files"], 2)

            self.assertIn(str(root / "webapp" / "node_modules"), hog_map)
            self.assertEqual(hog_map[str(root / "webapp" / "node_modules")]["category"], "node-modules")
            self.assertEqual(hog_map[str(root / "webapp" / "node_modules")]["size_bytes"], 6 * mib)
            self.assertEqual(hog_map[str(root / "webapp" / "node_modules")]["files"], 3)

            self.assertIn(str(root / "pyapp" / ".venv"), hog_map)
            self.assertEqual(hog_map[str(root / "pyapp" / ".venv")]["category"], "python-venv")
            self.assertEqual(hog_map[str(root / "pyapp" / ".venv")]["size_bytes"], 3 * mib)

            # Nested hogs must NOT appear separately
            self.assertNotIn(str(root / "webapp" / "node_modules" / "pkg" / "node_modules"), hog_map)
            self.assertNotIn(str(root / "pyapp" / ".venv" / "lib" / "__pycache__"), hog_map)
            self.assertNotIn(str(root / ".git"), hog_map)

            # Reclaimable total excludes .git and regular src
            self.assertEqual(rep["summary"]["reclaimable_bytes"], 15 * mib)
            self.assertEqual(rep["summary"]["reclaimable_hog_count"], 3)

            # .git tracked separately with reclaimable=False
            self.assertEqual(rep["summary"]["vcs_metadata_bytes"], 10 * mib)
            self.assertEqual(len(rep["vcs_metadata"]), 1)
            self.assertEqual(rep["vcs_metadata"][0]["category"], "vcs-metadata")
            self.assertFalse(rep["vcs_metadata"][0]["reclaimable"])
            self.assertEqual(rep["vcs_metadata"][0]["size_bytes"], 10 * mib)

            # Largest files >= 3 MiB: pack-1.pack (10), app.bin (4), index.js (3)
            self.assertEqual(len(rep["largest_files"]), 3)
            self.assertEqual(rep["largest_files"][0]["size_bytes"], 10 * mib)
            self.assertEqual(rep["largest_files"][1]["size_bytes"], 4 * mib)
            self.assertEqual(rep["largest_files"][2]["size_bytes"], 3 * mib)

    def test_hardlink_deduplication(self) -> None:
        mib = 1024 * 1024
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f1 = root / "a" / "shared.bin"
            f2 = root / "b" / "shared_link.bin"
            _write_sized_file(f1, 5 * mib)
            f2.parent.mkdir(parents=True, exist_ok=True)
            os.link(f1, f2)

            rep = dh.scan_paths([str(root)], min_mib=0.0, min_file_mib=1.0, no_statvfs=True)
            self.assertEqual(rep["summary"]["total_bytes"], 5 * mib)
            self.assertEqual(rep["summary"]["files"], 1)
            self.assertEqual(rep["summary"]["deduped_hardlinks"], 1)
            self.assertEqual(len(rep["largest_files"]), 1)

    def test_symlinks_not_traversed_and_loops_safe(self) -> None:
        mib = 1024 * 1024
        with tempfile.TemporaryDirectory() as td_outer, tempfile.TemporaryDirectory() as td_scan:
            outer = Path(td_outer)
            scan = Path(td_scan)
            _write_sized_file(outer / "external_huge.bin", 8 * mib)
            _write_sized_file(scan / "real" / "local.bin", 2 * mib)

            # Symlink to external directory, symlink to local file, and self-referencing loop symlink
            os.symlink(outer, scan / "ext_dir_link")
            os.symlink(scan / "real" / "local.bin", scan / "file_link.bin")
            os.symlink(scan, scan / "real" / "loop_back")

            rep = dh.scan_paths([str(scan)], min_mib=0.0, min_file_mib=1.0, no_statvfs=True)
            self.assertEqual(rep["summary"]["total_bytes"], 2 * mib)
            self.assertEqual(rep["summary"]["files"], 1)
            self.assertEqual(len(rep["largest_files"]), 1)
            self.assertEqual(rep["largest_files"][0]["path"], str(scan / "real" / "local.bin"))

    def test_depth_and_min_mib_filtering(self) -> None:
        mib = 1024 * 1024
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_sized_file(root / "d1" / "d2" / "d3" / "deep.bin", 4 * mib)
            _write_sized_file(root / "small" / "tiny.bin", int(0.5 * mib))

            rep_d1 = dh.scan_paths([str(root)], max_depth=1, min_mib=1.0, no_statvfs=True)
            paths_d1 = [d["path"] for d in rep_d1["top_dirs"]]
            self.assertEqual(paths_d1, [str(root / "d1")])

            rep_d2 = dh.scan_paths([str(root)], max_depth=2, min_mib=1.0, no_statvfs=True)
            paths_d2 = {d["path"] for d in rep_d2["top_dirs"]}
            self.assertEqual(paths_d2, {str(root / "d1"), str(root / "d1" / "d2")})

            rep_d3_all = dh.scan_paths([str(root)], max_depth=3, min_mib=0.0, no_statvfs=True)
            paths_d3 = {d["path"] for d in rep_d3_all["top_dirs"]}
            self.assertEqual(
                paths_d3,
                {str(root / "d1"), str(root / "d1" / "d2"), str(root / "d1" / "d2" / "d3"), str(root / "small")},
            )

    def test_one_file_system_skips_different_st_dev(self) -> None:
        mib = 1024 * 1024
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            local_dir = root / "local"
            other_mnt = root / "other_mount"
            _write_sized_file(local_dir / "a.bin", 2 * mib)
            _write_sized_file(other_mnt / "b.bin", 5 * mib)

            real_scandir = os.scandir

            class _FakeEntry:
                def __init__(self, wrapped: os.DirEntry[str], fake_dev: int | None) -> None:
                    self._w = wrapped
                    self.name = wrapped.name
                    self.path = wrapped.path
                    self._fake_dev = fake_dev

                def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
                    st = self._w.stat(follow_symlinks=follow_symlinks)
                    if self._fake_dev is not None:
                        return os.stat_result(
                            (
                                st.st_mode,
                                st.st_ino,
                                self._fake_dev,
                                st.st_nlink,
                                st.st_uid,
                                st.st_gid,
                                st.st_size,
                                st.st_atime,
                                st.st_mtime,
                                st.st_ctime,
                            )
                        )
                    return st

            class _FakeScandirCtx:
                def __init__(self, path: str) -> None:
                    self._ctx = real_scandir(path)

                def __enter__(self) -> list[_FakeEntry]:
                    entries = []
                    for e in self._ctx.__enter__():
                        fake_dev = 99999999 if e.path == str(other_mnt) else None
                        entries.append(_FakeEntry(e, fake_dev))
                    return entries

                def __exit__(self, *exc: object) -> None:
                    self._ctx.__exit__(*exc)

            with mock.patch("os.scandir", side_effect=lambda p: _FakeScandirCtx(p)):
                rep_one = dh.scan_paths([str(root)], one_file_system=True, min_mib=0.0, no_statvfs=True)
                self.assertEqual(rep_one["summary"]["total_bytes"], 2 * mib)

                rep_cross = dh.scan_paths([str(root)], one_file_system=False, min_mib=0.0, no_statvfs=True)
                self.assertEqual(rep_cross["summary"]["total_bytes"], 7 * mib)

    def test_permission_error_counted_as_skipped_entries(self) -> None:
        mib = 1024 * 1024
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ok_dir = root / "ok"
            denied_dir = root / "denied"
            _write_sized_file(ok_dir / "good.bin", 2 * mib)
            denied_dir.mkdir()

            real_scandir = os.scandir

            def _scandir_with_deny(path: str):
                if path == str(denied_dir):
                    raise PermissionError(13, "Permission denied", path)
                return real_scandir(path)

            with mock.patch("os.scandir", side_effect=_scandir_with_deny):
                rep = dh.scan_paths([str(root)], min_mib=0.0, no_statvfs=True)
                self.assertEqual(rep["summary"]["total_bytes"], 2 * mib)
                self.assertEqual(rep["summary"]["skipped_entries"], 1)

    def test_render_text_and_markdown(self) -> None:
        mib = 1024 * 1024
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_sized_file(root / "node_modules" / "a.js", 2 * mib)
            _write_sized_file(root / ".git" / "pack", 1 * mib)
            rep = dh.scan_paths([str(root)], min_mib=1.0, min_file_mib=1.0, no_statvfs=True)

            txt = dh.render_text(rep, markdown=False)
            self.assertIn("=== Summary ===", txt)
            self.assertIn("=== Reclaimable Build/Cache Hogs ===", txt)
            self.assertIn("node-modules", txt)
            self.assertIn("VCS Metadata (non-reclaimable)", txt)

            md = dh.render_text(rep, markdown=True)
            self.assertIn("# Disk Hogs Analysis @", md)
            self.assertIn("## Summary", md)
            self.assertIn("## Largest Files", md)


class TestCLIValidation(unittest.TestCase):
    def test_help(self) -> None:
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0)
        for flag in (
            "--top",
            "--depth",
            "--min-mib",
            "--min-file-mib",
            "--cross-mounts",
            "--no-statvfs",
            "--json",
            "--markdown",
            "--verbose",
        ):
            self.assertIn(flag, r.stdout)

    def test_missing_path_exits_1_cleanly(self) -> None:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "/nonexistent/path/sys-disk-hogs-test"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("error:", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1)

    def test_invalid_bounds_exit_1_cleanly(self) -> None:
        bad_invocations = [
            ["--top", "0"],
            ["--top", "101"],
            ["--depth", "0"],
            ["--depth", "11"],
            ["--min-mib", "-1"],
            ["--min-file-mib", "-0.1"],
        ]
        for flags in bad_invocations:
            r = subprocess.run([sys.executable, str(SCRIPT), *flags], capture_output=True, text=True, check=False)
            self.assertEqual(r.returncode, 1, msg=f"flags={flags} stderr={r.stderr}")
            self.assertIn("error:", r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertEqual(len(r.stderr.strip().splitlines()), 1)

    def test_argparse_error_exits_2(self) -> None:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--json", "--markdown"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 2)

    def test_verbose_emits_config_to_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                [sys.executable, str(SCRIPT), td, "--json", "--no-statvfs", "--verbose"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("config:", r.stderr)
            data = json.loads(r.stdout)
            self.assertEqual(data["summary"]["files"], 0)
            self.assertEqual(data["summary"]["dirs"], 1)


if __name__ == "__main__":
    unittest.main()
