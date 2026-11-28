#!/usr/bin/env zsh
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: run_hl_frontier_persistence_tick.sh

Runs one lock-protected 100% read-only Frontier Persistence tick (HIP-3 Builder-Perps,
Pendle Boros Fixed Funding, HyperEVM Yield Stack) and refreshes local snapshot artifacts.
EOF
  exit 0
fi

SCRIPT_PATH="${0:A}"
ROOT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
LOCK_FILE="${HL_LOCK_FILE:-${TMPDIR:-/tmp}/hl_frontier_persistence_tick.flock}"

if [[ "${HL_OPS_LOCK_HELD:-0}" != "1" ]]; then
  cd "${ROOT_DIR}"
  HL_OPS_LOCK_HELD=1 exec "${PYTHON_BIN}" -m hl_quant.ops_lock "${LOCK_FILE}" -- zsh "${SCRIPT_PATH}" "$@"
fi

RUN_DATE="$(date -u +%F)"
RUN_TIME="$(date -u +%H%M)"
LOG_DIR="${BACKTESTER_LOG_DIR:-${ROOT_DIR}/.neira/logs/runs/${RUN_DATE}/${RUN_TIME}/hl_frontier_persistence}"
mkdir -p "${LOG_DIR}"

"${PYTHON_BIN}" "${ROOT_DIR}/scripts/analytics/run_hl_frontier_persistence_tick.py" "$@" \
  | tee -a "${LOG_DIR}/run_hl_frontier_persistence_tick.log"
