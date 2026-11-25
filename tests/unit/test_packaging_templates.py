#!/usr/bin/env python3
"""Unit tests for CI workflow and systemd packaging templates."""

from __future__ import annotations

import configparser
import re
import shlex
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sysadmin_tools import sys_investigate  # noqa: E402

CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SYSTEMD_DIR = REPO_ROOT / "packaging" / "systemd"
SERVICE_FILE = SYSTEMD_DIR / "sys-investigate-sampler.service"
TIMER_FILE = SYSTEMD_DIR / "sys-investigate-sampler.timer"
SYSTEMD_README = SYSTEMD_DIR / "README.md"

SHA_PIN_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*[^@\s]+@([0-9a-f]{40})\s*(?:#.*)?$")
ANY_USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)")


def _extract_run_blocks(workflow_text: str) -> list[str]:
    """Return text lines belonging to `run:` steps in a GitHub workflow."""
    lines = workflow_text.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)(?:-\s*)?run:\s*(.*)$", lines[i])
        if not m:
            i += 1
            continue
        base_indent = len(m.group(1))
        inline = m.group(2).strip()
        if inline in ("|", "|-", ">", ">-"):
            buf: list[str] = []
            i += 1
            while i < len(lines):
                line = lines[i]
                if line.strip() and (len(line) - len(line.lstrip(" "))) <= base_indent:
                    break
                buf.append(line)
                i += 1
            blocks.append("\n".join(buf))
        else:
            blocks.append(inline)
            i += 1
    return blocks


class TestCIWorkflowTemplate(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(CI_WORKFLOW.is_file(), f"missing {CI_WORKFLOW}")
        self.text = CI_WORKFLOW.read_text(encoding="utf-8")

    def test_required_hardening_keys_and_commands(self) -> None:
        for required in (
            "permissions:",
            "contents: read",
            "concurrency:",
            "cancel-in-progress: true",
            "timeout-minutes:",
            "persist-credentials: false",
            "runs-on: ubuntu-latest",
            "./scripts/verify.sh",
            "./scripts/install.sh --dry-run",
        ):
            self.assertIn(required, self.text)
        self.assertNotIn("pull_request_target", self.text)

    def test_actions_are_sha_pinned(self) -> None:
        uses_lines = [line for line in self.text.splitlines() if ANY_USES_RE.match(line)]
        self.assertGreaterEqual(len(uses_lines), 2)
        for line in uses_lines:
            self.assertRegex(line, SHA_PIN_RE, f"action must be pinned to 40-hex SHA: {line!r}")

    def test_no_expression_interpolation_in_run_blocks(self) -> None:
        blocks = _extract_run_blocks(self.text)
        self.assertGreaterEqual(len(blocks), 2)
        for block in blocks:
            self.assertNotIn("${{", block, f"unsafe expression interpolation in run block: {block!r}")
            self.assertNotIn("github.event.", block)


class TestSystemdPackagingTemplates(unittest.TestCase):
    def _parse_unit(self, path: Path) -> configparser.ConfigParser:
        self.assertTrue(path.is_file(), f"missing {path}")
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn("/home/", raw, f"hardcoded /home/ path found in {path}")
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str  # preserve systemd CamelCase keys
        cp.read_string(raw)
        return cp

    def test_service_unit(self) -> None:
        cp = self._parse_unit(SERVICE_FILE)
        self.assertEqual(cp.sections(), ["Unit", "Service", "Install"])
        svc = cp["Service"]
        self.assertEqual(svc["Type"], "oneshot")
        self.assertEqual(svc["ExecStartPre"], "/usr/bin/mkdir -p %h/.local/state/sysadmin-tools")
        self.assertEqual(svc["Nice"], "10")
        self.assertEqual(svc["IOSchedulingClass"], "best-effort")
        self.assertEqual(svc["TimeoutStartSec"], "30")
        self.assertEqual(svc["MemoryMax"], "128M")

        cmd = shlex.split(svc["ExecStart"])
        self.assertEqual(cmd[0], "%h/.local/bin/sys-investigate")
        args = sys_investigate.build_parser().parse_args(cmd[1:])
        self.assertEqual(args.interval, 1.0)
        self.assertEqual(args.watch, 1)
        self.assertEqual(args.append_log, "%h/.local/state/sysadmin-tools/investigate.jsonl")
        self.assertTrue(args.no_statvfs)

    def test_timer_unit(self) -> None:
        cp = self._parse_unit(TIMER_FILE)
        self.assertEqual(cp.sections(), ["Unit", "Timer", "Install"])
        tmr = cp["Timer"]
        self.assertEqual(tmr["OnBootSec"], "2min")
        self.assertEqual(tmr["OnUnitActiveSec"], "5min")
        self.assertEqual(tmr["RandomizedDelaySec"], "15s")
        self.assertEqual(tmr["Persistent"], "true")
        self.assertEqual(cp["Install"]["WantedBy"], "timers.target")

    def test_readme_exists_and_references_user_systemd_dir(self) -> None:
        self.assertTrue(SYSTEMD_README.is_file())
        readme = SYSTEMD_README.read_text(encoding="utf-8")
        self.assertIn("~/.config/systemd/user", readme)
        self.assertIn("sys-investigate-sampler.timer", readme)


class TestGitHubPagesShowcase(unittest.TestCase):
    PAGES_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pages.yml"
    DOCS_DIR = REPO_ROOT / "docs"
    INDEX_HTML = DOCS_DIR / "index.html"
    NOJEKYLL = DOCS_DIR / ".nojekyll"

    def test_pages_workflow_hardened_and_sha_pinned(self) -> None:
        self.assertTrue(self.PAGES_WORKFLOW.is_file(), f"missing {self.PAGES_WORKFLOW}")
        text = self.PAGES_WORKFLOW.read_text(encoding="utf-8")
        for required in (
            "permissions:",
            "contents: read",
            "pages: write",
            "id-token: write",
            "concurrency:",
            "timeout-minutes:",
            "persist-credentials: false",
            "runs-on: ubuntu-latest",
        ):
            self.assertIn(required, text)
        self.assertNotIn("pull_request_target", text)

        uses_lines = [line for line in text.splitlines() if ANY_USES_RE.match(line)]
        self.assertGreaterEqual(len(uses_lines), 4)
        for line in uses_lines:
            self.assertRegex(line, SHA_PIN_RE, f"action must be pinned to 40-hex SHA: {line!r}")

        for block in _extract_run_blocks(text):
            self.assertNotIn("${{", block, f"unsafe expression interpolation in run block: {block!r}")
            self.assertNotIn("github.event.", block)

    def test_docs_index_html_local_links_and_nojekyll(self) -> None:
        self.assertTrue(self.NOJEKYLL.is_file(), f"missing {self.NOJEKYLL}")
        self.assertTrue(self.INDEX_HTML.is_file(), f"missing {self.INDEX_HTML}")
        html = self.INDEX_HTML.read_text(encoding="utf-8")
        self.assertNotIn("/home/", html)

        # No external script, stylesheet, or webfont CDNs.
        self.assertIsNone(re.search(r"<script[^>]+src=[\"']https?://", html, re.IGNORECASE))
        self.assertIsNone(re.search(r"<link[^>]+rel=[\"']stylesheet[\"'][^>]+href=[\"']https?://", html, re.IGNORECASE))
        self.assertIsNone(re.search(r"@import\s+url\([\"']?https?://", html, re.IGNORECASE))

        expected_assets = (
            "assets/logo.svg",
            "assets/icon.svg",
            "assets/logo.png",
            "assets/icons/icon-investigate.svg",
            "assets/icons/icon-io-guard.svg",
            "assets/icons/icon-mem-watch.svg",
            "assets/icons/icon-disk-hogs.svg",
            "assets/icons/icon-crash-triage.svg",
            "assets/icons/icon-net-watch.svg",
            "assets/icons/icon-systemd-watch.svg",
            "assets/examples/architecture-overview.svg",
            "assets/examples/incident-triage-workflow.svg",
        )
        for asset in expected_assets:
            self.assertIn(asset, html, f"missing asset reference in docs/index.html: {asset}")

        docs_root = self.DOCS_DIR.resolve()
        for attr in re.findall(r'(?:src|href)="([^"#]+)"', html):
            if attr.startswith(("http://", "https://", "mailto:", "javascript:")):
                continue
            target = (self.DOCS_DIR / attr).resolve()
            self.assertTrue(
                target == docs_root or docs_root in target.parents,
                f"relative link escapes docs/: {attr}",
            )
            self.assertTrue(target.exists(), f"broken local link in docs/index.html: {attr} -> {target}")

    def test_svgs_valid_xml_and_no_external_urls(self) -> None:
        svg_files = sorted((REPO_ROOT / "assets").rglob("*.svg")) + sorted(
            (self.DOCS_DIR / "assets").rglob("*.svg")
        )
        self.assertGreaterEqual(len(svg_files), 11)
        for svg in svg_files:
            ET.parse(svg)
            txt = svg.read_text(encoding="utf-8")
            self.assertNotIn("/home/", txt, f"hardcoded /home/ path in {svg}")
            urls = [
                u
                for u in re.findall(r"https?://[^\s\"'<>)]+", txt)
                if u not in ("http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink")
            ]
            self.assertEqual(urls, [], f"unexpected external URLs in {svg}: {urls}")


if __name__ == "__main__":
    unittest.main()
