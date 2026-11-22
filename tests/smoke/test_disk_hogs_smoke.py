#!/usr/bin/env python3
"""Live smoke tests executing sys-disk-hogs against the repository directory."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "src" / "sysadmin_tools" / "sys_disk_hogs.py"


class TestDiskHogsSmoke(unittest.TestCase):
    def test_repo_json_output(self) -> None:
        r = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(REPO_ROOT),
                "--top",
                "10",
                "--depth",
                "3",
                "--min-mib",
                "0",
                "--min-file-mib",
                "0",
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        data = json.loads(r.stdout)
        for key in ("timestamp", "summary", "top_dirs", "reclaimable_hogs", "vcs_metadata", "largest_files"):
            self.assertIn(key, data)

        summary = data["summary"]
        self.assertGreater(summary["total_bytes"], 0)
        self.assertGreater(summary["files"], 0)
        self.assertGreater(summary["dirs"], 0)
        self.assertGreaterEqual(summary["elapsed_sec"], 0.0)
        self.assertTrue(summary["one_file_system"])
        self.assertEqual(len(summary["roots"]), 1)

        # Ensure .git (if present) is never counted as reclaimable
        for hog in data["reclaimable_hogs"]:
            self.assertNotEqual(hog["category"], "vcs-metadata")
            self.assertTrue(hog["reclaimable"])
            for field in ("category", "size_mib", "size_gib", "files", "path"):
                self.assertIn(field, hog)

        if (REPO_ROOT / ".git").is_dir():
            self.assertTrue(any(v["path"] == str(REPO_ROOT / ".git") for v in data["vcs_metadata"]))
            for v in data["vcs_metadata"]:
                self.assertFalse(v["reclaimable"])

    def test_repo_markdown_and_no_statvfs(self) -> None:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), str(REPO_ROOT), "--top", "5", "--no-statvfs", "--markdown"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("# Disk Hogs Analysis @", r.stdout)
        self.assertIn("## Summary", r.stdout)
        self.assertIn("## Reclaimable Build/Cache Hogs", r.stdout)
        self.assertIn("## Largest Files", r.stdout)

        rj = subprocess.run(
            [sys.executable, str(SCRIPT), str(REPO_ROOT), "--no-statvfs", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(rj.returncode, 0, msg=rj.stderr)
        data = json.loads(rj.stdout)
        for root_entry in data["summary"]["roots"]:
            self.assertIsNone(root_entry["fs_used_pct"])

    def test_default_cwd_text_output(self) -> None:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--top", "5"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("=== Summary ===", r.stdout)
        self.assertIn("=== Reclaimable Build/Cache Hogs ===", r.stdout)
        self.assertIn("=== Largest Files", r.stdout)


if __name__ == "__main__":
    unittest.main()
