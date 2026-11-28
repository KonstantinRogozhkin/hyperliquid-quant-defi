#!/usr/bin/env zsh
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: run_hl_autopilot_tick.sh

Runs one lock-protected Neutral Multi-Sleeve Autopilot (+ <=5% Crisis-Alpha CTA)
allocation tick.
EOF
  exit 0
fi

SCRIPT_PATH="${0:A}"
REPO_ROOT="$(cd "$(dirname "${SCRIPT_PATH}")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/.venv/bin/python}"
LOCK_FILE="${HL_LOCK_FILE:-${TMPDIR:-/tmp}/hl_autopilot_tick.flock}"

# Atomic kernel flock (released automatically on crash/SIGKILL): no stale-lock reclaim race.
# Overlapping invocations exit 0 without running the tick.
cd "${REPO_ROOT}"
PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}" exec "${PYTHON_BIN}" -m hl_quant.ops_lock "${LOCK_FILE}" -- \
  "${PYTHON_BIN}" "${REPO_ROOT}/scripts/analytics/run_hl_autopilot_tick.py" "$@"
