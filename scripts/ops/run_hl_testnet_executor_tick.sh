#!/usr/bin/env zsh
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: run_hl_testnet_executor_tick.sh

Runs one Hyperliquid TESTNET (mock USDC) ALO rebalance cycle from the latest
metamodel target weights (data/paper/hl_metamodel/state.json), with kill-switch
protection and Telegram Rich HTML summary. Never touches mainnet.
EOF
  exit 0
fi

SCRIPT_PATH="${0:A}"
ROOT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
LOCK_FILE="${HL_LOCK_FILE:-${TMPDIR:-/tmp}/hl_testnet_executor_tick.flock}"

if [[ "${HL_OPS_LOCK_HELD:-0}" != "1" ]]; then
  cd "${ROOT_DIR}"
  HL_OPS_LOCK_HELD=1 exec "${PYTHON_BIN}" -m hl_quant.ops_lock "${LOCK_FILE}" -- zsh "${SCRIPT_PATH}" "$@"
fi

RUN_DATE="$(date -u +%F)"
RUN_TIME="$(date -u +%H%M)"
LOG_DIR="${BACKTESTER_LOG_DIR:-${ROOT_DIR}/.neira/logs/runs/${RUN_DATE}/${RUN_TIME}/hl_testnet_executor_tick}"
mkdir -p "${LOG_DIR}"

CMD=(
  "${PYTHON_BIN}" "${ROOT_DIR}/scripts/analytics/run_hl_testnet_executor.py"
  --execute-testnet
  --max-child-usd "${HL_TESTNET_MAX_CHILD_USD:-250}"
  --neutralize-net
)
if [[ "${HL_TESTNET_TELEGRAM:-1}" == "1" ]]; then
  CMD+=(--telegram)
fi
CMD+=("$@")

"${CMD[@]}" | tee -a "${LOG_DIR}/run_hl_testnet_executor_tick.log"
