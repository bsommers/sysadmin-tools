#!/usr/bin/env python3
"""Deterministic SVG + PNG brand, icon, and showcase graphic generator for sysadmin-tools.

Stdlib-only. Generates:
  - docs/assets/logo.svg + docs/assets/logo.png (1200x360 wide hero banner)
  - assets/logo.svg + assets/logo.png (512x512 square repo/app icon)
  - docs/assets/icon.svg (exact copy of assets/logo.svg)
  - docs/assets/icons/icon-{investigate,io-guard,mem-watch,disk-hogs,crash-triage,net-watch,systemd-watch}.svg (64x64)
  - docs/assets/examples/architecture-overview.svg (1120x520)
  - docs/assets/examples/incident-triage-workflow.svg (1120x440)
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Theme palette: Futuristic Obsidian-Black & Light-Blue Neon Grid
BG_DEEP = "#04070f"
BG_MID = "#081022"
BG_DARK = "#040814"
PANEL_FILL = "#0b1428"
TERM_FILL = "#050b18"
ICON_BG = "#070d1c"

CYAN = "#00e5ff"
SKY = "#38bdf8"
PLASMA = "#0284c7"
EMERALD = "#00ff9d"
AMBER = "#ffb800"
MAGENTA = "#ff2a85"

TEXT_HEAD = "#f0f6fc"
TEXT_BODY = "#cbd5e1"
TEXT_MUTED = "#94a3b8"
TEXT_MONO = "#5eead4"

FONT_SANS = "system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
FONT_MONO = "'Cascadia Code', 'Fira Code', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"


def esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def hex_vertices(cx: float, cy: float, r: float, rotate_deg: float = -90.0) -> list[tuple[float, float]]:
    return [
        (
            cx + r * math.cos(math.radians(rotate_deg + i * 60.0)),
            cy + r * math.sin(math.radians(rotate_deg + i * 60.0)),
        )
        for i in range(6)
    ]


def pts_str(pts: list[tuple[float, float]]) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def neon_defs(prefix: str, glow_cx: str = "50%", glow_cy: str = "50%", glow_r: str = "60%") -> str:
    return f"""  <defs>
    <linearGradient id="{prefix}-bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="{BG_DEEP}"/>
      <stop offset="52%" stop-color="{BG_MID}"/>
      <stop offset="100%" stop-color="{BG_DARK}"/>
    </linearGradient>
    <radialGradient id="{prefix}-core-glow" cx="{glow_cx}" cy="{glow_cy}" r="{glow_r}">
      <stop offset="0%" stop-color="{CYAN}" stop-opacity="0.16"/>
      <stop offset="55%" stop-color="{PLASMA}" stop-opacity="0.06"/>
      <stop offset="100%" stop-color="{BG_DEEP}" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="{prefix}-emblem-glow" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="{EMERALD}" stop-opacity="0.28"/>
      <stop offset="55%" stop-color="{CYAN}" stop-opacity="0.12"/>
      <stop offset="100%" stop-color="{CYAN}" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="{prefix}-accent-line" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="{EMERALD}" stop-opacity="0.95"/>
      <stop offset="28%" stop-color="{CYAN}" stop-opacity="0.95"/>
      <stop offset="80%" stop-color="{SKY}" stop-opacity="0.35"/>
      <stop offset="100%" stop-color="{SKY}" stop-opacity="0"/>
    </linearGradient>
    <pattern id="{prefix}-grid-minor" width="24" height="24" patternUnits="userSpaceOnUse">
      <path d="M 24 0 L 0 0 0 24" fill="none" stroke="{SKY}" stroke-width="0.75" stroke-opacity="0.08"/>
    </pattern>
    <pattern id="{prefix}-grid-major" width="96" height="96" patternUnits="userSpaceOnUse">
      <rect width="96" height="96" fill="url(#{prefix}-grid-minor)"/>
      <path d="M 48 0 L 48 96 M 0 48 L 96 48" fill="none" stroke="{CYAN}" stroke-width="1" stroke-opacity="0.16"/>
      <circle cx="48" cy="48" r="1.8" fill="{CYAN}" fill-opacity="0.38"/>
    </pattern>
    <filter id="{prefix}-glow-cyan" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="3.2" result="blur"/>
      <feMerge>
        <feMergeNode in="blur"/>
        <feMergeNode in="SourceGraphic"/>
      </feMerge>
    </filter>
    <filter id="{prefix}-glow-soft" x="-25%" y="-25%" width="150%" height="150%">
      <feGaussianBlur stdDeviation="2.0" result="blur"/>
      <feMerge>
        <feMergeNode in="blur"/>
        <feMergeNode in="SourceGraphic"/>
      </feMerge>
    </filter>
  </defs>"""


def hud_corners(x: float, y: float, w: float, h: float, arm: float = 16.0, color: str = CYAN, sw: float = 2.0) -> str:
    x2, y2 = x + w, y + h
    return (
        f'  <g fill="none" stroke="{color}" stroke-width="{sw}" stroke-linecap="square">\n'
        f'    <path d="M {x:.1f} {y + arm:.1f} L {x:.1f} {y:.1f} L {x + arm:.1f} {y:.1f}"/>\n'
        f'    <path d="M {x2 - arm:.1f} {y:.1f} L {x2:.1f} {y:.1f} L {x2:.1f} {y + arm:.1f}"/>\n'
        f'    <path d="M {x:.1f} {y2 - arm:.1f} L {x:.1f} {y2:.1f} L {x + arm:.1f} {y2:.1f}"/>\n'
        f'    <path d="M {x2 - arm:.1f} {y2:.1f} L {x2:.1f} {y2:.1f} L {x2:.1f} {y2 - arm:.1f}"/>\n'
        f"  </g>"
    )


def render_square_logo() -> str:
    """512x512 square repo/app icon. No tiny text; all emblem strokes >= 12px."""
    cx, cy = 256.0, 256.0
    outer_pts = hex_vertices(cx, cy, 182.0, -90.0)
    inner_pts = hex_vertices(cx, cy, 122.0, -90.0)
    spoke_outer = hex_vertices(cx, cy, 174.0, -90.0)
    spoke_inner = hex_vertices(cx, cy, 126.0, -90.0)

    spokes = "\n".join(
        f'    <line x1="{ox:.1f}" y1="{oy:.1f}" x2="{ix:.1f}" y2="{iy:.1f}" '
        f'stroke="{SKY}" stroke-width="12" stroke-linecap="round" stroke-opacity="0.85"/>'
        for (ox, oy), (ix, iy) in zip(spoke_outer, spoke_inner)
    )
    vertex_nodes = "\n".join(
        f'    <circle cx="{vx:.1f}" cy="{vy:.1f}" r="13" fill="{BG_DEEP}" stroke="{CYAN}" stroke-width="12"/>'
        for vx, vy in outer_pts
    )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512" role="img" aria-label="sysadmin-tools square icon">
{neon_defs("sq", glow_cx="50%", glow_cy="50%", glow_r="58%")}
  <defs>
    <clipPath id="sq-clip">
      <rect x="20" y="20" width="472" height="472" rx="92"/>
    </clipPath>
  </defs>
  <g clip-path="url(#sq-clip)">
    <rect width="512" height="512" fill="url(#sq-bg)"/>
    <rect width="512" height="512" fill="url(#sq-grid-major)"/>
    <rect width="512" height="512" fill="url(#sq-core-glow)"/>
  </g>
  <rect x="20" y="20" width="472" height="472" rx="92" fill="none" stroke="{CYAN}" stroke-width="12" stroke-opacity="0.88" filter="url(#sq-glow-soft)"/>
  <circle cx="256" cy="256" r="142" fill="url(#sq-emblem-glow)"/>
  <g>
    <polygon points="{pts_str(outer_pts)}" fill="#071124" fill-opacity="0.92" stroke="{CYAN}" stroke-width="16" stroke-linejoin="round" filter="url(#sq-glow-cyan)"/>
{spokes}
    <polygon points="{pts_str(inner_pts)}" fill="{PANEL_FILL}" fill-opacity="0.95" stroke="{SKY}" stroke-width="12" stroke-linejoin="round"/>
{vertex_nodes}
    <polyline points="154,256 196,256 224,196 256,316 288,216 312,256 358,256"
              fill="none" stroke="{EMERALD}" stroke-width="16" stroke-linecap="round" stroke-linejoin="round" filter="url(#sq-glow-cyan)"/>
    <circle cx="224" cy="196" r="10" fill="{TEXT_HEAD}"/>
    <circle cx="288" cy="216" r="10" fill="{CYAN}"/>
  </g>
</svg>
"""


def render_hero_banner() -> str:
    """1200x360 wide hero banner with cyber-telemetry emblem, wordmark, and 4 HUD pills."""
    cx, cy = 192.0, 180.0
    outer_hex = hex_vertices(cx, cy, 112.0, -90.0)
    mid_hex = hex_vertices(cx, cy, 86.0, -60.0)
    inner_hex = hex_vertices(cx, cy, 66.0, -90.0)
    spoke_outer = hex_vertices(cx, cy, 108.0, -90.0)
    spoke_inner = hex_vertices(cx, cy, 68.0, -90.0)

    ticks = []
    for i in range(12):
        ang = math.radians(i * 30.0)
        r1 = 123.0 if i % 2 == 0 else 126.0
        r2 = 132.0
        ticks.append(
            f'    <line x1="{cx + r1 * math.cos(ang):.1f}" y1="{cy + r1 * math.sin(ang):.1f}" '
            f'x2="{cx + r2 * math.cos(ang):.1f}" y2="{cy + r2 * math.sin(ang):.1f}" '
            f'stroke="{SKY}" stroke-width="{2.0 if i % 2 == 0 else 1.2}" stroke-opacity="0.55"/>'
        )

    spokes = "\n".join(
        f'    <line x1="{ox:.1f}" y1="{oy:.1f}" x2="{ix:.1f}" y2="{iy:.1f}" '
        f'stroke="{SKY}" stroke-width="2.2" stroke-opacity="0.72"/>'
        for (ox, oy), (ix, iy) in zip(spoke_outer, spoke_inner)
    )
    outer_nodes = "\n".join(
        f'    <circle cx="{vx:.1f}" cy="{vy:.1f}" r="5.5" fill="{BG_DEEP}" stroke="{CYAN}" stroke-width="2.5"/>\n'
        f'    <circle cx="{vx:.1f}" cy="{vy:.1f}" r="2.0" fill="{TEXT_HEAD}"/>'
        for vx, vy in outer_hex
    )

    pills = [
        (366, 172, "7 STANDALONE CLIs", CYAN),
        (552, 178, "PURE /proc + /sys", SKY),
        (744, 178, "ZERO RUNTIME DEPS", EMERALD),
        (936, 218, "v0.5.0 · 166 TESTS PASS", EMERALD),
    ]
    pill_svg = []
    for px, pw, label, accent in pills:
        pill_svg.append(
            f'  <g>\n'
            f'    <rect x="{px}" y="246" width="{pw}" height="38" rx="7" fill="{PANEL_FILL}" fill-opacity="0.92" stroke="{accent}" stroke-width="1.5"/>\n'
            f'    <circle cx="{px + 15}" cy="265" r="3.5" fill="{accent}" filter="url(#hero-glow-soft)"/>\n'
            f'    <text x="{px + 27}" y="270" fill="{TEXT_HEAD}" font-family="{FONT_MONO}" font-size="12.5" font-weight="600">{esc(label)}</text>\n'
            f'  </g>'
        )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 360" width="1200" height="360" role="img" aria-label="sysadmin-tools hero banner">
{neon_defs("hero", glow_cx="18%", glow_cy="50%", glow_r="48%")}
  <rect width="1200" height="360" fill="url(#hero-bg)"/>
  <rect width="1200" height="360" fill="url(#hero-grid-major)"/>
  <rect width="1200" height="360" fill="url(#hero-core-glow)"/>
  <rect x="14" y="14" width="1172" height="332" rx="14" fill="none" stroke="{CYAN}" stroke-width="1.5" stroke-opacity="0.45"/>
{hud_corners(14, 14, 1172, 332, arm=22, color=CYAN, sw=2.2)}

  <!-- Top telemetry header micro-bar -->
  <text x="1154" y="42" text-anchor="end" fill="{TEXT_MUTED}" font-family="{FONT_MONO}" font-size="11" letter-spacing="1.6">// ROOT-NVME DIAGNOSTICS · LINUX 3.10+ STDLIB</text>

  <!-- Left multi-ring hexagonal cyber-telemetry shield emblem -->
  <g>
    <circle cx="{cx:.1f}" cy="{cy:.1f}" r="134" fill="url(#hero-emblem-glow)"/>
    <circle cx="{cx:.1f}" cy="{cy:.1f}" r="127" fill="none" stroke="{SKY}" stroke-width="1.2" stroke-opacity="0.32" stroke-dasharray="6 6"/>
{chr(10).join(ticks)}
    <polygon points="{pts_str(outer_hex)}" fill="#071124" fill-opacity="0.93" stroke="{CYAN}" stroke-width="4.5" stroke-linejoin="round" filter="url(#hero-glow-cyan)"/>
    <polygon points="{pts_str(mid_hex)}" fill="none" stroke="{SKY}" stroke-width="1.8" stroke-opacity="0.65" stroke-dasharray="10 6"/>
{spokes}
    <polygon points="{pts_str(inner_hex)}" fill="{PANEL_FILL}" fill-opacity="0.96" stroke="{CYAN}" stroke-width="2.5" stroke-linejoin="round"/>
{outer_nodes}
    <circle cx="{cx:.1f}" cy="{cy:.1f}" r="36" fill="url(#hero-emblem-glow)"/>
    <polyline points="{cx - 52:.1f},{cy:.1f} {cx - 28:.1f},{cy:.1f} {cx - 14:.1f},{cy - 32:.1f} {cx + 2:.1f},{cy + 34:.1f} {cx + 18:.1f},{cy - 20:.1f} {cx + 30:.1f},{cy:.1f} {cx + 52:.1f},{cy:.1f}"
              fill="none" stroke="{EMERALD}" stroke-width="4.2" stroke-linecap="round" stroke-linejoin="round" filter="url(#hero-glow-cyan)"/>
    <circle cx="{cx - 14:.1f}" cy="{cy - 32:.1f}" r="3.8" fill="{TEXT_HEAD}"/>
    <circle cx="{cx + 18:.1f}" cy="{cy - 20:.1f}" r="3.5" fill="{CYAN}"/>
  </g>

  <!-- Right wordmark, subtitle, and status pills -->
  <g>
    <circle cx="372" cy="83" r="4" fill="{EMERALD}" filter="url(#hero-glow-soft)"/>
    <text x="386" y="88" fill="{TEXT_MONO}" font-family="{FONT_MONO}" font-size="13" font-weight="600" letter-spacing="2.0">KERNEL TELEMETRY · ZERO-SUBPROCESS HOT PATH</text>

    <text x="366" y="152" fill="{CYAN}" fill-opacity="0.32" font-family="{FONT_MONO}" font-size="56" font-weight="800" letter-spacing="3.5" filter="url(#hero-glow-cyan)">SYSADMIN-TOOLS</text>
    <text x="366" y="152" fill="{TEXT_HEAD}" font-family="{FONT_MONO}" font-size="56" font-weight="800" letter-spacing="3.5">SYSADMIN-TOOLS</text>

    <rect x="366" y="170" width="764" height="2.2" fill="url(#hero-accent-line)"/>
    <text x="366" y="210" fill="{TEXT_BODY}" font-family="{FONT_SANS}" font-size="21" font-weight="500">Zero-Dependency Linux Telemetry, Guard, Network &amp; Forensics</text>
  </g>

{chr(10).join(pill_svg)}

  <text x="366" y="318" fill="{TEXT_MUTED}" font-family="{FONT_MONO}" font-size="10.5" letter-spacing="0.2">sys-investigate  ·  sys-io-guard  ·  sys-mem-watch  ·  sys-disk-hogs  ·  sys-crash-triage  ·  sys-net-watch  ·  sys-systemd-watch</text>
</svg>
"""


def render_tool_icon(name: str, aria_label: str, body: str) -> str:
    pid = f"ic-{name}"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" role="img" aria-label="{esc(aria_label)}">
  <defs>
    <pattern id="{pid}-grid" width="16" height="16" patternUnits="userSpaceOnUse">
      <path d="M 16 0 L 0 0 0 16" fill="none" stroke="{SKY}" stroke-width="0.75" stroke-opacity="0.14"/>
    </pattern>
    <clipPath id="{pid}-clip">
      <rect x="4" y="4" width="56" height="56" rx="12"/>
    </clipPath>
    <filter id="{pid}-glow" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="1.4" result="blur"/>
      <feMerge>
        <feMergeNode in="blur"/>
        <feMergeNode in="SourceGraphic"/>
      </feMerge>
    </filter>
  </defs>
  <rect x="4" y="4" width="56" height="56" rx="12" fill="{ICON_BG}"/>
  <rect x="4" y="4" width="56" height="56" rx="12" fill="url(#{pid}-grid)" clip-path="url(#{pid}-clip)"/>
  <rect x="4" y="4" width="56" height="56" rx="12" fill="none" stroke="{CYAN}" stroke-width="1.75"/>
{body}
</svg>
"""


def render_tool_icons() -> dict[str, str]:
    # 1. icon-investigate: radar reticle + live telemetry pulse waveform (#00e5ff + #00ff9d)
    investigate = f"""  <g fill="none" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="32" cy="32" r="17" stroke="{CYAN}" stroke-opacity="0.9"/>
    <path d="M 32 11 L 32 16 M 32 48 L 32 53 M 11 32 L 16 32 M 48 32 L 53 32" stroke="{CYAN}"/>
    <polyline points="16,33 22,33 26,22 31,42 36,26 40,33 48,33" stroke="{EMERALD}" filter="url(#ic-investigate-glow)"/>
  </g>"""

    # 2. icon-io-guard: hexagonal shield + padlock + disk lane (#00e5ff + #38bdf8)
    shield_pts = pts_str(hex_vertices(32.0, 29.0, 17.5, -90.0))
    io_guard = f"""  <g fill="none" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
    <polygon points="{shield_pts}" fill="{PANEL_FILL}" fill-opacity="0.7" stroke="{CYAN}"/>
    <path d="M 28 27 L 28 23 A 4 4 0 0 1 36 23 L 36 27" stroke="{SKY}"/>
    <rect x="25.5" y="27" width="13" height="10" rx="2" fill="{ICON_BG}" stroke="{SKY}" filter="url(#ic-io-guard-glow)"/>
    <line x1="32" y1="30.5" x2="32" y2="33.5" stroke="{CYAN}"/>
    <path d="M 15 50 L 49 50" stroke="{CYAN}"/>
    <path d="M 21 47 L 21 53 M 43 47 L 43 53" stroke="{SKY}"/>
  </g>"""

    # 3. icon-mem-watch: memory bar stack + rising pressure trendline (#00e5ff + #ffb800)
    mem_watch = f"""  <g fill="none" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
    <rect x="14" y="40" width="6" height="10" rx="1" fill="{CYAN}" fill-opacity="0.2" stroke="{CYAN}"/>
    <rect x="24" y="34" width="6" height="16" rx="1" fill="{CYAN}" fill-opacity="0.25" stroke="{CYAN}"/>
    <rect x="34" y="28" width="6" height="22" rx="1" fill="{CYAN}" fill-opacity="0.3" stroke="{CYAN}"/>
    <rect x="44" y="21" width="6" height="29" rx="1" fill="{AMBER}" fill-opacity="0.25" stroke="{AMBER}"/>
    <polyline points="15,32 26,26 36,21 47,14" stroke="{AMBER}" filter="url(#ic-mem-watch-glow)"/>
    <polyline points="41.5,14 47,14 47,19.5" stroke="{AMBER}"/>
  </g>"""

    # 4. icon-disk-hogs: storage platter rings + highlighted reclaimable sector wedge (#00e5ff + #00ff9d)
    disk_hogs = f"""  <g fill="none" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="32" cy="32" r="18" stroke="{CYAN}" stroke-opacity="0.85"/>
    <circle cx="32" cy="32" r="10.5" stroke="{CYAN}" stroke-opacity="0.55"/>
    <path d="M 32 32 L 32 14 A 18 18 0 0 1 49.4 27.3 Z" fill="{EMERALD}" fill-opacity="0.28" stroke="{EMERALD}" filter="url(#ic-disk-hogs-glow)"/>
    <circle cx="32" cy="32" r="3.5" fill="{ICON_BG}" stroke="{CYAN}"/>
  </g>"""

    # 5. icon-crash-triage: interrupted heartbeat trace + forensic reticle (#00e5ff + #ff2a85)
    crash_triage = f"""  <g fill="none" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
    <path d="M 13 20 L 13 13 L 20 13 M 44 13 L 51 13 L 51 20 M 13 44 L 13 51 L 20 51 M 44 51 L 51 51 L 51 44" stroke="{CYAN}"/>
    <polyline points="14,34 21,34 25,21 29,44 33,31" stroke="{CYAN}"/>
    <path d="M 38 27 L 48 37 M 48 27 L 38 37" stroke="{MAGENTA}" filter="url(#ic-crash-triage-glow)"/>
    <line x1="36" y1="44" x2="50" y2="44" stroke="{MAGENTA}" stroke-opacity="0.85"/>
  </g>"""

    # 6. icon-net-watch: network topology nodes + bidirectional packet arrows + pulse dot (#00e5ff + #38bdf8 + #00ff9d)
    net_watch = f"""  <g fill="none" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
    <line x1="20" y1="20" x2="44" y2="20" stroke="{SKY}" stroke-opacity="0.75"/>
    <line x1="20" y1="44" x2="44" y2="44" stroke="{SKY}" stroke-opacity="0.75"/>
    <line x1="18" y1="22" x2="18" y2="42" stroke="{CYAN}" stroke-opacity="0.7"/>
    <line x1="46" y1="22" x2="46" y2="42" stroke="{CYAN}" stroke-opacity="0.7"/>
    <path d="M 24 28 L 40 28 M 36 24 L 40 28 L 36 32" stroke="{CYAN}" filter="url(#ic-net-watch-glow)"/>
    <path d="M 40 36 L 24 36 M 28 32 L 24 36 L 28 40" stroke="{EMERALD}" filter="url(#ic-net-watch-glow)"/>
    <circle cx="18" cy="20" r="3.5" fill="{ICON_BG}" stroke="{CYAN}"/>
    <circle cx="46" cy="20" r="3.5" fill="{ICON_BG}" stroke="{CYAN}"/>
    <circle cx="18" cy="44" r="3.5" fill="{ICON_BG}" stroke="{SKY}"/>
    <circle cx="46" cy="44" r="3.5" fill="{ICON_BG}" stroke="{SKY}"/>
    <circle cx="32" cy="14" r="2.2" fill="{EMERALD}" stroke="none" filter="url(#ic-net-watch-glow)"/>
  </g>"""

    # 7. icon-systemd-watch: nested cgroup v2 hierarchy nodes + unit pressure bar (#00e5ff + #38bdf8 + #ffb800)
    systemd_watch = f"""  <g fill="none" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
    <rect x="24" y="12" width="16" height="9" rx="2" fill="{PANEL_FILL}" stroke="{CYAN}"/>
    <path d="M 32 21 L 32 27 M 19 27 L 45 27 M 19 27 L 19 32 M 45 27 L 45 32" stroke="{SKY}"/>
    <rect x="12" y="32" width="14" height="9" rx="2" fill="{ICON_BG}" stroke="{CYAN}"/>
    <rect x="38" y="32" width="14" height="9" rx="2" fill="{ICON_BG}" stroke="{AMBER}"/>
    <rect x="13" y="47" width="38" height="5" rx="2" fill="{PANEL_FILL}" stroke="{SKY}" stroke-width="1.5"/>
    <rect x="14.5" y="48.5" width="24" height="2" rx="1" fill="{AMBER}" stroke="none" filter="url(#ic-systemd-watch-glow)"/>
  </g>"""

    return {
        "icon-investigate.svg": render_tool_icon("investigate", "sys-investigate icon", investigate),
        "icon-io-guard.svg": render_tool_icon("io-guard", "sys-io-guard icon", io_guard),
        "icon-mem-watch.svg": render_tool_icon("mem-watch", "sys-mem-watch icon", mem_watch),
        "icon-disk-hogs.svg": render_tool_icon("disk-hogs", "sys-disk-hogs icon", disk_hogs),
        "icon-crash-triage.svg": render_tool_icon("crash-triage", "sys-crash-triage icon", crash_triage),
        "icon-net-watch.svg": render_tool_icon("net-watch", "sys-net-watch icon", net_watch),
        "icon-systemd-watch.svg": render_tool_icon("systemd-watch", "sys-systemd-watch icon", systemd_watch),
    }


def render_architecture_overview() -> str:
    """1120x520 3-column HUD architecture diagram."""
    cli_items = [
        ("sys-investigate", "Stall, I/O, PSI, GPU, PID, diff", EMERALD),
        ("sys-io-guard", "Per-disk flock & build job cap", SKY),
        ("sys-mem-watch", "RAM, swap, PSI & OOM pressure", AMBER),
        ("sys-disk-hogs", "One-FS mount & cache scanner", EMERALD),
        ("sys-crash-triage", "Reboot, panic, MCE & GPU logs", MAGENTA),
        ("sys-net-watch", "NIC, TCP/UDP, SNMP & softnet", CYAN),
        ("sys-systemd-watch", "Cgroup v2 pressure & churn", SKY),
    ]
    kernel_items = [
        ("/proc/* + PSI", "stat, diskstats, meminfo, pressure/{io,mem}", CYAN),
        ("/sys/class/{nvme,drm,block}", "NVMe health, GPU busy/temp, schedulers", SKY),
        ("fcntl.flock on tmpfs", "Per-device RAM lock (/run or /tmp)", EMERALD),
        ("os.scandir one-FS", "Same st_dev walk; skips stalled mounts", AMBER),
        ("Bounded journalctl / pstore", "Cold-path kdump & crash logs w/ timeout", MAGENTA),
        ("/proc/net/*", "dev, tcp/6, udp/6, snmp, softnet_stat", CYAN),
        ("/sys/fs/cgroup + journal", "cgroup v2 pressure, churn, bursts", SKY),
    ]
    output_items = [
        ("Text", "Color-free terminal summary", CYAN),
        ("--markdown", "Paste-ready incident report", SKY),
        ("--json", "Stable structured schema", EMERALD),
        ("--append-log", "Append-only JSONL log", TEXT_MONO),
        ("--diff-log", "JSONL trend diff", SKY),
    ]
    exit_items = [
        ("0", "OK", "Healthy / succeeded", EMERALD),
        ("1", "ERR", "Runtime or tool error", MAGENTA),
        ("2", "USAGE", "Invalid flags or path", AMBER),
        ("3", "ISSUE", "Threshold / stall breach", AMBER),
        ("4", "LOCK BUSY", "Flock already contended", SKY),
    ]

    cli_cards = []
    for idx, (cmd, desc, accent) in enumerate(cli_items):
        cy = 134 + idx * 50
        cli_cards.append(
            f'    <rect x="42" y="{cy}" width="276" height="42" rx="7" fill="{TERM_FILL}" stroke="{accent}" stroke-width="1.2" stroke-opacity="0.75"/>\n'
            f'    <circle cx="56" cy="{cy + 14}" r="3.5" fill="{accent}"/>\n'
            f'    <text x="68" y="{cy + 18}" fill="{TEXT_HEAD}" font-family="{FONT_MONO}" font-size="13" font-weight="700">{esc(cmd)}</text>\n'
            f'    <text x="54" y="{cy + 34}" fill="{TEXT_MUTED}" font-family="{FONT_SANS}" font-size="11.2">{esc(desc)}</text>'
        )

    kernel_cards = []
    for idx, (src, desc, accent) in enumerate(kernel_items):
        cy = 134 + idx * 50
        kernel_cards.append(
            f'    <rect x="388" y="{cy}" width="344" height="42" rx="7" fill="{TERM_FILL}" stroke="{accent}" stroke-width="1.2" stroke-opacity="0.75"/>\n'
            f'    <rect x="388" y="{cy + 8}" width="3.5" height="26" rx="1.5" fill="{accent}"/>\n'
            f'    <text x="403" y="{cy + 18}" fill="{TEXT_MONO}" font-family="{FONT_MONO}" font-size="12.5" font-weight="700">{esc(src)}</text>\n'
            f'    <text x="403" y="{cy + 34}" fill="{TEXT_MUTED}" font-family="{FONT_SANS}" font-size="11.2">{esc(desc)}</text>'
        )

    output_rows = []
    for idx, (fmt, desc, accent) in enumerate(output_items):
        ry = 178 + idx * 21
        output_rows.append(
            f'    <text x="814" y="{ry}" fill="{accent}" font-family="{FONT_MONO}" font-size="11.5" font-weight="700">{esc(fmt)}</text>\n'
            f'    <text x="914" y="{ry}" fill="{TEXT_BODY}" font-family="{FONT_SANS}" font-size="11.2">{esc(desc)}</text>'
        )

    exit_rows = []
    for idx, (code, tag, desc, accent) in enumerate(exit_items):
        ry = 330 + idx * 21
        exit_rows.append(
            f'    <rect x="812" y="{ry - 11}" width="18" height="15" rx="3" fill="{accent}" fill-opacity="0.2" stroke="{accent}" stroke-width="1"/>\n'
            f'    <text x="821" y="{ry}" text-anchor="middle" fill="{accent}" font-family="{FONT_MONO}" font-size="10.5" font-weight="700">{code}</text>\n'
            f'    <text x="838" y="{ry}" fill="{TEXT_HEAD}" font-family="{FONT_MONO}" font-size="10.5" font-weight="700">{esc(tag)}</text>\n'
            f'    <text x="914" y="{ry}" fill="{TEXT_MUTED}" font-family="{FONT_SANS}" font-size="10.8">{esc(desc)}</text>'
        )

    arrows_12 = "\n".join(
        f'    <line x1="332" y1="{y}" x2="365" y2="{y}" stroke="{CYAN}" stroke-width="1.8" stroke-opacity="0.85"/>\n'
        f'    <polygon points="365,{y - 4.5} 374,{y} 365,{y + 4.5}" fill="{CYAN}"/>'
        for y in (155 + idx * 50 for idx in range(7))
    )
    arrows_23 = "\n".join(
        f'    <line x1="746" y1="{y}" x2="779" y2="{y}" stroke="{CYAN}" stroke-width="1.8" stroke-opacity="0.85"/>\n'
        f'    <polygon points="779,{y - 4.5} 788,{y} 779,{y + 4.5}" fill="{CYAN}"/>'
        for y in (218, 384)
    )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1120 520" width="1120" height="520" role="img" aria-label="sysadmin-tools architecture overview">
{neon_defs("arch", glow_cx="50%", glow_cy="48%", glow_r="56%")}
  <rect width="1120" height="520" fill="url(#arch-bg)"/>
  <rect width="1120" height="520" fill="url(#arch-grid-major)"/>
  <rect width="1120" height="520" fill="url(#arch-core-glow)"/>
  <rect x="10" y="10" width="1100" height="500" rx="12" fill="none" stroke="{CYAN}" stroke-width="1.5" stroke-opacity="0.45"/>
{hud_corners(10, 10, 1100, 500, arm=18, color=CYAN, sw=2.0)}

  <!-- Header -->
  <circle cx="34" cy="37" r="4.5" fill="{EMERALD}" filter="url(#arch-glow-soft)"/>
  <text x="48" y="42" fill="{TEXT_HEAD}" font-family="{FONT_MONO}" font-size="17" font-weight="800" letter-spacing="1.4">SYSADMIN-TOOLS // ARCHITECTURE OVERVIEW</text>
  <text x="1092" y="41" text-anchor="end" fill="{TEXT_MONO}" font-family="{FONT_MONO}" font-size="11.5" letter-spacing="0.8">PYTHON 3.10+ STDLIB · ZERO THIRD-PARTY DEPS</text>
  <rect x="28" y="58" width="1064" height="1.5" fill="url(#arch-accent-line)"/>

  <!-- Column 1: Root-NVMe CLIs -->
  <g>
    <rect x="28" y="76" width="304" height="420" rx="10" fill="{PANEL_FILL}" fill-opacity="0.92" stroke="{CYAN}" stroke-width="1.5"/>
    <path d="M 28 86 Q 28 76 38 76 L 322 76 Q 332 76 332 86 L 332 108 L 28 108 Z" fill="{PLASMA}" fill-opacity="0.22"/>
    <line x1="28" y1="108" x2="332" y2="108" stroke="{CYAN}" stroke-width="1" stroke-opacity="0.5"/>
    <text x="42" y="96" fill="{CYAN}" font-family="{FONT_MONO}" font-size="12" font-weight="700" letter-spacing="0.8">01 · ROOT-NVME CLIs (~/.local/bin/)</text>
    <text x="42" y="125" fill="{TEXT_MUTED}" font-family="{FONT_SANS}" font-size="11">Direct copies on root NVMe (no secondary symlinks)</text>
{chr(10).join(cli_cards)}
  </g>

  <!-- Column 1 -> 2 connectors -->
  <g>
{arrows_12}
  </g>

  <!-- Column 2: Kernel Boundary -->
  <g>
    <rect x="374" y="76" width="372" height="420" rx="10" fill="{PANEL_FILL}" fill-opacity="0.92" stroke="{CYAN}" stroke-width="1.5"/>
    <path d="M 374 86 Q 374 76 384 76 L 736 76 Q 746 76 746 86 L 746 108 L 374 108 Z" fill="{PLASMA}" fill-opacity="0.22"/>
    <line x1="374" y1="108" x2="746" y2="108" stroke="{CYAN}" stroke-width="1" stroke-opacity="0.5"/>
    <text x="388" y="96" fill="{CYAN}" font-family="{FONT_MONO}" font-size="12" font-weight="700" letter-spacing="0.8">02 · ZERO-SUBPROCESS KERNEL BOUNDARY</text>
    <text x="388" y="125" fill="{TEXT_MUTED}" font-family="{FONT_SANS}" font-size="11">Hot path reads kernel virtual files directly under 99%+ disk load</text>
{chr(10).join(kernel_cards)}
  </g>

  <!-- Column 2 -> 3 connectors -->
  <g>
{arrows_23}
  </g>

  <!-- Column 3: Deterministic Outputs & Exit Codes -->
  <g>
    <rect x="788" y="76" width="304" height="420" rx="10" fill="{PANEL_FILL}" fill-opacity="0.92" stroke="{CYAN}" stroke-width="1.5"/>
    <path d="M 788 86 Q 788 76 798 76 L 1082 76 Q 1092 76 1092 86 L 1092 108 L 788 108 Z" fill="{PLASMA}" fill-opacity="0.22"/>
    <line x1="788" y1="108" x2="1092" y2="108" stroke="{CYAN}" stroke-width="1" stroke-opacity="0.5"/>
    <text x="802" y="96" fill="{CYAN}" font-family="{FONT_MONO}" font-size="12" font-weight="700" letter-spacing="0.8">03 · OUTPUTS &amp; EXIT CONTRACT</text>
    <text x="802" y="125" fill="{TEXT_MUTED}" font-family="{FONT_SANS}" font-size="11">Deterministic formats &amp; automation exit codes</text>

    <!-- Outputs sub-panel -->
    <rect x="802" y="138" width="276" height="140" rx="7" fill="{TERM_FILL}" stroke="{SKY}" stroke-width="1.2" stroke-opacity="0.7"/>
    <text x="814" y="156" fill="{SKY}" font-family="{FONT_MONO}" font-size="11" font-weight="700" letter-spacing="1.0">OUTPUT FORMATS</text>
    <line x1="814" y1="162" x2="1066" y2="162" stroke="{SKY}" stroke-width="0.8" stroke-opacity="0.3"/>
{chr(10).join(output_rows)}

    <!-- Exit codes sub-panel -->
    <rect x="802" y="292" width="276" height="148" rx="7" fill="{TERM_FILL}" stroke="{CYAN}" stroke-width="1.2" stroke-opacity="0.7"/>
    <text x="814" y="310" fill="{CYAN}" font-family="{FONT_MONO}" font-size="11" font-weight="700" letter-spacing="1.0">EXIT CODES</text>
    <line x1="814" y1="316" x2="1066" y2="316" stroke="{CYAN}" stroke-width="0.8" stroke-opacity="0.3"/>
{chr(10).join(exit_rows)}
  </g>
</svg>
"""


def render_incident_workflow() -> str:
    """1120x440 4-stage incident triage workflow showcase graphic."""
    stages = [
        {
            "num": "STAGE 01",
            "title": "DETECT",
            "accent": EMERALD,
            "tagline": "Live stall & I/O saturation probe",
            "cmds": [
                "$ sys-investigate",
                "  --progress-check",
            ],
            "sec_label": "THRESHOLDS",
            "metrics": [
                ("wa >= 20%", "CPU I/O wait"),
                ("w_await >= 250ms", "Write latency"),
                ("Dirty >= 2 GiB", "Writeback queue"),
            ],
            "footer": "EXIT 3 -> STALL DETECTED",
        },
        {
            "num": "STAGE 02",
            "title": "ISOLATE",
            "accent": AMBER,
            "tagline": "Pinpoint PID, RAM, hog, net, unit",
            "cmds": [
                "$ sys-investigate --diagnose-pid",
                "$ sys-mem-watch | sys-disk-hogs",
                "$ sys-net-watch | sys-systemd-watch",
            ],
            "sec_label": "SIGNALS",
            "metrics": [
                ("PID / mount", "wchan + cgroup"),
                ("RAM / hogs", "OOM + one-FS scan"),
                ("Net / units", "Retrans + cgroup v2"),
            ],
            "footer": "CULPRIT + FAULT ISOLATED",
        },
        {
            "num": "STAGE 03",
            "title": "PROTECT",
            "accent": SKY,
            "tagline": "Serialize heavy disk workloads",
            "cmds": [
                "$ sys-io-guard \\",
                "  --path /datanapps \\",
                "  -- cargo test",
            ],
            "sec_label": "GUARDRAILS",
            "metrics": [
                ("Per-disk flock", "Lock on RAM tmpfs"),
                ("CARGO_BUILD_JOBS=8", "Capped build I/O"),
                ("Non-blocking opt", "Fast fail if busy"),
            ],
            "footer": "EXIT 4 -> LOCK CONTENDED",
        },
        {
            "num": "STAGE 04",
            "title": "FORENSICS",
            "accent": MAGENTA,
            "tagline": "Post-reboot root-cause triage",
            "cmds": [
                "$ sys-crash-triage",
                "  --show-steps",
            ],
            "sec_label": "SIGNATURES",
            "metrics": [
                ("AMD FCH reset", "Hard reset bit decode"),
                ("Panic / MCE / GPU", "Kernel & OOM sigs"),
                ("pstore / kdump", "Crash dump harvest"),
            ],
            "footer": "EVIDENCE + REMEDIATION",
        },
    ]

    card_w = 248
    gap = 27
    start_x = 24
    card_y = 80
    card_h = 294

    stage_blocks = []
    connector_arrows = []
    for idx, st in enumerate(stages):
        x = start_x + idx * (card_w + gap)
        accent = st["accent"]

        # Command lines inside dark terminal box
        cmd_lines = []
        base_cmd_y = card_y + 92 if len(st["cmds"]) == 3 else card_y + 99
        cmd_pitch = 16 if len(st["cmds"]) == 3 else 18
        for c_idx, line in enumerate(st["cmds"]):
            indent = 12 if line.startswith("  ") else 0
            fsize = "9.5" if len(line.lstrip()) > 32 else "10.2"
            cmd_lines.append(
                f'    <text x="{x + 20 + indent}" y="{base_cmd_y + c_idx * cmd_pitch}" fill="{TEXT_MONO}" '
                f'font-family="{FONT_MONO}" font-size="{fsize}" font-weight="600">{esc(line.lstrip())}</text>'
            )

        # Metric / signal rows
        metric_rows = []
        for m_idx, (k, v) in enumerate(st["metrics"]):
            my = card_y + 168 + m_idx * 30
            metric_rows.append(
                f'    <rect x="{x + 12}" y="{my - 14}" width="{card_w - 24}" height="24" rx="5" fill="{TERM_FILL}" stroke="{accent}" stroke-width="0.9" stroke-opacity="0.45"/>\n'
                f'    <text x="{x + 20}" y="{my + 2}" fill="{accent}" font-family="{FONT_MONO}" font-size="10.4" font-weight="700">{esc(k)}</text>\n'
                f'    <text x="{x + card_w - 20}" y="{my + 2}" text-anchor="end" fill="{TEXT_MUTED}" font-family="{FONT_SANS}" font-size="10.2">{esc(v)}</text>'
            )

        stage_blocks.append(
            f'  <g>\n'
            f'    <rect x="{x}" y="{card_y}" width="{card_w}" height="{card_h}" rx="10" fill="{PANEL_FILL}" fill-opacity="0.92" stroke="{accent}" stroke-width="1.5"/>\n'
            f'    <path d="M {x} {card_y + 10} Q {x} {card_y} {x + 10} {card_y} L {x + card_w - 10} {card_y} Q {x + card_w} {card_y} {x + card_w} {card_y + 10} L {x + card_w} {card_y + 42} L {x} {card_y + 42} Z" fill="{accent}" fill-opacity="0.16"/>\n'
            f'    <line x1="{x}" y1="{card_y + 42}" x2="{x + card_w}" y2="{card_y + 42}" stroke="{accent}" stroke-width="1" stroke-opacity="0.55"/>\n'
            f'    <text x="{x + 14}" y="{card_y + 19}" fill="{accent}" font-family="{FONT_MONO}" font-size="10" font-weight="700" letter-spacing="1.2">{st["num"]}</text>\n'
            f'    <text x="{x + 14}" y="{card_y + 36}" fill="{TEXT_HEAD}" font-family="{FONT_MONO}" font-size="16" font-weight="800" letter-spacing="1.2">{st["title"]}</text>\n'
            f'    <circle cx="{x + card_w - 18}" cy="{card_y + 21}" r="5" fill="{accent}" filter="url(#wf-glow-soft)"/>\n'
            f'    <text x="{x + 14}" y="{card_y + 60}" fill="{TEXT_BODY}" font-family="{FONT_SANS}" font-size="11.5">{esc(st["tagline"])}</text>\n'
            f'    <rect x="{x + 12}" y="{card_y + 70}" width="{card_w - 24}" height="62" rx="6" fill="{TERM_FILL}" stroke="{CYAN}" stroke-width="1.0" stroke-opacity="0.5"/>\n'
            f'{chr(10).join(cmd_lines)}\n'
            f'    <text x="{x + 14}" y="{card_y + 148}" fill="{TEXT_MUTED}" font-family="{FONT_MONO}" font-size="10" font-weight="700" letter-spacing="1.2">{st["sec_label"]}</text>\n'
            f'{chr(10).join(metric_rows)}\n'
            f'    <rect x="{x + 12}" y="{card_y + 254}" width="{card_w - 24}" height="26" rx="5" fill="{accent}" fill-opacity="0.14" stroke="{accent}" stroke-width="1.1"/>\n'
            f'    <text x="{x + card_w / 2:.1f}" y="{card_y + 271}" text-anchor="middle" fill="{TEXT_HEAD}" font-family="{FONT_MONO}" font-size="10.5" font-weight="700">{esc(st["footer"])}</text>\n'
            f'  </g>'
        )

        if idx < len(stages) - 1:
            ax1 = x + card_w + 3
            ax2 = x + card_w + gap - 11
            ay = card_y + 147
            connector_arrows.append(
                f'  <g>\n'
                f'    <line x1="{ax1}" y1="{ay}" x2="{ax2}" y2="{ay}" stroke="{CYAN}" stroke-width="2.4" filter="url(#wf-glow-soft)"/>\n'
                f'    <polygon points="{ax2 - 1},{ay - 5.5} {ax2 + 9},{ay} {ax2 - 1},{ay + 5.5}" fill="{CYAN}"/>\n'
                f'  </g>'
            )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1120 440" width="1120" height="440" role="img" aria-label="sysadmin-tools 4-stage incident triage workflow">
{neon_defs("wf", glow_cx="50%", glow_cy="44%", glow_r="60%")}
  <rect width="1120" height="440" fill="url(#wf-bg)"/>
  <rect width="1120" height="440" fill="url(#wf-grid-major)"/>
  <rect width="1120" height="440" fill="url(#wf-core-glow)"/>
  <rect x="10" y="10" width="1100" height="420" rx="12" fill="none" stroke="{CYAN}" stroke-width="1.5" stroke-opacity="0.45"/>
{hud_corners(10, 10, 1100, 420, arm=18, color=CYAN, sw=2.0)}

  <!-- Header -->
  <circle cx="34" cy="37" r="4.5" fill="{CYAN}" filter="url(#wf-glow-soft)"/>
  <text x="48" y="42" fill="{TEXT_HEAD}" font-family="{FONT_MONO}" font-size="17" font-weight="800" letter-spacing="1.3">INCIDENT TRIAGE WORKFLOW // DETECT · ISOLATE · PROTECT · FORENSICS</text>
  <text x="1096" y="41" text-anchor="end" fill="{TEXT_MONO}" font-family="{FONT_MONO}" font-size="11.5">REAL CLI PLAYBOOK</text>
  <rect x="24" y="58" width="1072" height="1.5" fill="url(#wf-accent-line)"/>

  <!-- 4 Stage Cards -->
{chr(10).join(stage_blocks)}

  <!-- Stage Connectors -->
{chr(10).join(connector_arrows)}

  <!-- Bottom telemetry automation strip -->
  <rect x="24" y="388" width="1072" height="30" rx="6" fill="{PANEL_FILL}" fill-opacity="0.92" stroke="{CYAN}" stroke-width="1.1" stroke-opacity="0.6"/>
  <text x="560" y="407" text-anchor="middle" fill="{TEXT_BODY}" font-family="{FONT_MONO}" font-size="10.6">
    <tspan fill="{EMERALD}" font-weight="700">AUTOMATION:</tspan><tspan dx="8">--json alerts · --markdown tickets · --append-log / --diff-log JSONL · systemd user timer</tspan>
  </text>
</svg>
"""


def write_svg(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Verify XML well-formedness before writing
    ET.fromstring(content)
    path.write_text(content, encoding="utf-8")


def rasterize_svg(svg_path: Path, png_path: Path, width: int, height: int) -> None:
    png_path.parent.mkdir(parents=True, exist_ok=True)
    inkscape = shutil.which("inkscape")
    magick = shutil.which("magick") or shutil.which("convert")

    if inkscape:
        cmd = [
            inkscape,
            str(svg_path),
            "--export-type=png",
            f"--export-filename={png_path}",
            f"--export-width={width}",
            f"--export-height={height}",
        ]
    elif magick:
        cmd = [
            magick,
            "-background",
            "none",
            "-density",
            "192",
            str(svg_path),
            "-resize",
            f"{width}x{height}!",
            str(png_path),
        ]
    else:
        raise RuntimeError("Neither 'inkscape' nor 'magick'/'convert' found in PATH")

    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Rasterization failed ({' '.join(cmd)}): {res.stderr.strip()}")
    data = png_path.read_bytes()
    if len(data) < 64 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError(f"Invalid PNG output at {png_path}")


def main() -> int:
    # 1. Square icon (512x512) + copy to docs/assets/icon.svg + rasterize PNG
    square_svg = render_square_logo()
    assets_logo_svg = ROOT / "assets" / "logo.svg"
    assets_logo_png = ROOT / "assets" / "logo.png"
    docs_icon_svg = ROOT / "docs" / "assets" / "icon.svg"
    write_svg(assets_logo_svg, square_svg)
    write_svg(docs_icon_svg, square_svg)
    rasterize_svg(assets_logo_svg, assets_logo_png, 512, 512)

    # 2. Wide hero banner (1200x360) + rasterize PNG
    hero_svg = render_hero_banner()
    docs_logo_svg = ROOT / "docs" / "assets" / "logo.svg"
    docs_logo_png = ROOT / "docs" / "assets" / "logo.png"
    write_svg(docs_logo_svg, hero_svg)
    rasterize_svg(docs_logo_svg, docs_logo_png, 1200, 360)

    # 3. 7 Cohesive tool icons (64x64)
    icons_dir = ROOT / "docs" / "assets" / "icons"
    for filename, svg_content in render_tool_icons().items():
        write_svg(icons_dir / filename, svg_content)

    # 4. 2 Visual showcase graphics (1120x520 architecture, 1120x440 workflow)
    examples_dir = ROOT / "docs" / "assets" / "examples"
    write_svg(examples_dir / "architecture-overview.svg", render_architecture_overview())
    write_svg(examples_dir / "incident-triage-workflow.svg", render_incident_workflow())

    print("Rendered all SVG + PNG assets successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
