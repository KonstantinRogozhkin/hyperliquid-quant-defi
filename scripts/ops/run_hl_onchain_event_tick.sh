#!/usr/bin/env zsh
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: run_hl_onchain_event_tick.sh

Runs one lock-protected 100% read-only On-Chain Event Alpha tick (H2 Whale Liq Walls,
H4 HyperEVM <-> HyperCore Dislocations, H5 HLP Inventory Skew), updates forward return
outcomes (+15m/+1h/+4h) on prior events, and alerts Telegram only on threshold triggers.
EOF
  exit 0
fi

SCRIPT_PATH="${0:A}"
ROOT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
LOCK_FILE="${HL_LOCK_FILE:-${ROOT_DIR}/.neira/state/locks/hl_onchain_event_tick.flock}"

if [[ "${HL_OPS_LOCK_HELD:-0}" != "1" ]]; then
  cd "${ROOT_DIR}"
  HL_OPS_LOCK_HELD=1 exec "${PYTHON_BIN}" -m hl_quant.ops_lock "${LOCK_FILE}" -- zsh "${SCRIPT_PATH}" "$@"
fi

RUN_DATE="$(date -u +%F)"
RUN_TIME="$(date -u +%H%M)"
LOG_DIR="${BACKTESTER_LOG_DIR:-${ROOT_DIR}/.neira/logs/runs/${RUN_DATE}/${RUN_TIME}/hl_onchain_events}"
mkdir -p "${LOG_DIR}"

CMD=("${PYTHON_BIN}" "${ROOT_DIR}/scripts/analytics/run_hl_onchain_event_tick.py")
if [[ "${HL_ONCHAIN_TELEGRAM:-1}" == "1" ]]; then
  CMD+=(--telegram-on-alert)
fi
CMD+=("$@")

"${CMD[@]}" | tee -a "${LOG_DIR}/run_hl_onchain_event_tick.log"
