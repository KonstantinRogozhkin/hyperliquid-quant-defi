#!/usr/bin/env zsh
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: run_hl_metamodel_paper_tick.sh

Read-only live Paper / Shadow tick for the Hyperliquid metamodel.
Fetches trailing 1h bars/funding + live HL L2 orderbooks and updates the
append-only paper ledger under $HL_PAPER_STATE_DIR (default: data/paper/hl_metamodel).
Does not touch DuckDB or place orders.
EOF
  exit 0
fi

SCRIPT_PATH="${0:A}"
ROOT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
STATE_DIR="${HL_PAPER_STATE_DIR:-${ROOT_DIR}/data/paper/hl_metamodel}"
LOCK_FILE="${HL_LOCK_FILE:-${ROOT_DIR}/.neira/state/locks/hl_metamodel_paper_tick.flock}"

if [[ "${HL_OPS_LOCK_HELD:-0}" != "1" ]]; then
  cd "${ROOT_DIR}"
  HL_OPS_LOCK_HELD=1 exec "${PYTHON_BIN}" -m hl_quant.ops_lock "${LOCK_FILE}" -- zsh "${SCRIPT_PATH}" "$@"
fi

RUN_DATE="$(date -u +%F)"
RUN_TIME="$(date -u +%H%M)"
LOG_DIR="${BACKTESTER_LOG_DIR:-${ROOT_DIR}/.neira/logs/runs/${RUN_DATE}/${RUN_TIME}/hl_metamodel_paper_tick}"
mkdir -p "${LOG_DIR}"

HL_PAPER_TELEGRAM="${HL_PAPER_TELEGRAM:-1}"
CMD=(
  "${PYTHON_BIN}" "${ROOT_DIR}/scripts/analytics/run_hl_metamodel_paper_tick.py"
  --state-dir "${STATE_DIR}"
  --max-coins "${HL_PAPER_MAX_COINS:-35}"
)
if [[ "${HL_PAPER_TELEGRAM}" == "1" || "${HL_PAPER_TELEGRAM:l}" == "true" ]]; then
  CMD+=(--telegram)
fi
CMD+=("$@")
"${CMD[@]}" | tee -a "${LOG_DIR}/run_hl_metamodel_paper_tick.log"
