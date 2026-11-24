#!/usr/bin/env bash
# Install sysadmin-tools CLIs as standalone copies (not symlinks to secondary disks)
# so diagnostics remain executable even when secondary storage mounts are stalled.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PREFIX="${HOME}/.local/bin"
DRY_RUN=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [--prefix DIR] [--dry-run]

Installs CLI scripts from ${REPO_ROOT}/src/sysadmin_tools/ into DIR (default: ~/.local/bin).
Uses direct file copies rather than symlinks so tools stay usable if a secondary disk stalls.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)
      PREFIX="${2:?missing value for --prefix}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown option: $1" >&2
      exit 2
      ;;
  esac
done

TOOLS=(
  "sys_investigate.py:sys-investigate"
  "sys_io_guard.py:sys-io-guard"
  "sys_mem_watch.py:sys-mem-watch"
  "sys_disk_hogs.py:sys-disk-hogs"
  "sys_crash_triage.py:sys-crash-triage"
)

for entry in "${TOOLS[@]}"; do
  src_file="${entry%%:*}"
  bin_name="${entry##*:}"
  SRC="${REPO_ROOT}/src/sysadmin_tools/${src_file}"
  DST="${PREFIX}/${bin_name}"
  if [[ ! -f "$SRC" ]]; then
    echo "error: source script not found: $SRC" >&2
    exit 1
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] install -m 0755 '$SRC' '$DST'"
  else
    mkdir -p "$PREFIX"
    install -m 0755 "$SRC" "$DST"
    echo "Installed: $DST"
  fi
done

if [[ "$DRY_RUN" -eq 0 ]]; then
  echo "Next steps:"
  echo "  sys-investigate --help"
  echo "  sys-io-guard --help"
  echo "  sys-mem-watch --help"
  echo "  sys-disk-hogs --help"
  echo "  sys-crash-triage --help"
fi
