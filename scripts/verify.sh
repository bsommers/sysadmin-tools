#!/usr/bin/env bash
# Composable verification runner for sysadmin-tools.
# Emits target/verify/{summary.tsv,report.md,<stage>.log}.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DIR="${REPO_ROOT}/target/verify"

TIER="all"
MATCH=""

usage() {
  cat <<EOF
Usage: $(basename "$0") [--quick | --tier <unit|smoke|all>] [--match PATTERN]

Stages:
  syntax   python3 -m py_compile on src/ and tests/
  unit     tests/unit/
  smoke    tests/smoke/
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quick)
      TIER="unit"
      shift
      ;;
    --tier)
      TIER="${2:?missing tier}"
      shift 2
      ;;
    --match)
      MATCH="${2:?missing pattern}"
      shift 2
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

mkdir -p "$OUT_DIR"
SUMMARY="${OUT_DIR}/summary.tsv"
REPORT="${OUT_DIR}/report.md"
printf "stage\tstatus\tduration_s\tlog\n" > "$SUMMARY"

FAILED=0
run_stage() {
  local name="$1"
  shift
  local log="${OUT_DIR}/${name}.log"
  local t0 t1 dur rc
  t0=$(date +%s)
  set +e
  "$@" >"$log" 2>&1
  rc=$?
  set -e
  t1=$(date +%s)
  dur=$((t1 - t0))
  if [[ "$rc" -eq 0 ]]; then
    printf "%s\tPASS\t%s\t%s\n" "$name" "$dur" "$log" >> "$SUMMARY"
    printf "[PASS] %-10s (%ss)\n" "$name" "$dur"
  else
    printf "%s\tFAIL\t%s\t%s\n" "$name" "$dur" "$log" >> "$SUMMARY"
    printf "[FAIL] %-10s (%ss) -> %s\n" "$name" "$dur" "$log"
    cat "$log" >&2
    FAILED=1
  fi
}

cd "$REPO_ROOT"

shopt -s nullglob
PY_FILES=(
  src/sysadmin_tools/*.py
  tests/unit/test_*.py
  tests/smoke/test_*.py
)
shopt -u nullglob

run_stage syntax python3 -m py_compile "${PY_FILES[@]}"

if [[ "$TIER" == "all" || "$TIER" == "unit" ]]; then
  if [[ -n "$MATCH" ]]; then
    run_stage unit python3 -m unittest discover -s tests/unit -p "test_*.py" -k "$MATCH" -v
  else
    run_stage unit python3 -m unittest discover -s tests/unit -p "test_*.py" -v
  fi
fi

if [[ "$TIER" == "all" || "$TIER" == "smoke" ]]; then
  if [[ -n "$MATCH" ]]; then
    run_stage smoke python3 -m unittest discover -s tests/smoke -p "test_*.py" -k "$MATCH" -v
  else
    run_stage smoke python3 -m unittest discover -s tests/smoke -p "test_*.py" -v
  fi
fi

{
  echo "# Verification Report"
  echo ""
  echo "| Stage | Status | Duration (s) | Log |"
  echo "|---|---:|---:|---|"
  tail -n +2 "$SUMMARY" | awk -F'\t' '{printf "| %s | %s | %s | `%s` |\n", $1, $2, $3, $4}'
} > "$REPORT"

exit "$FAILED"
