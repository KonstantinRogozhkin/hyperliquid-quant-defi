#!/usr/bin/env zsh
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: run_hl_cross_dex_paper_tick.sh

Runs one lock-protected read-only Hyperliquid x Lighter Dual-Leg L2 Paper tick
and delivers the Rich HTML summary to Telegram (set HL_CROSS_DEX_TELEGRAM=0 to disable).
EOF
  exit 0
fi

SCRIPT_PATH="${0:A}"
ROOT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
LOCK_FILE="${HL_LOCK_FILE:-${ROOT_DIR}/.neira/state/locks/hl_cross_dex_paper_tick.flock}"

if [[ "${HL_OPS_LOCK_HELD:-0}" != "1" ]]; then
  cd "${ROOT_DIR}"
  HL_OPS_LOCK_HELD=1 exec "${PYTHON_BIN}" -m hl_quant.ops_lock "${LOCK_FILE}" -- zsh "${SCRIPT_PATH}" "$@"
fi

RUN_DATE="$(date -u +%F)"
RUN_TIME="$(date -u +%H%M)"
LOG_DIR="${BACKTESTER_LOG_DIR:-${ROOT_DIR}/.neira/logs/runs/${RUN_DATE}/${RUN_TIME}/hl_cross_dex_paper}"
mkdir -p "${LOG_DIR}"

CMD=("${PYTHON_BIN}" "${ROOT_DIR}/scripts/analytics/run_hl_cross_dex_paper_tick.py")
if [[ "${HL_CROSS_DEX_TELEGRAM:-1}" == "1" ]]; then
  CMD+=(--telegram)
fi
CMD+=("$@")

"${CMD[@]}" | tee -a "${LOG_DIR}/run_hl_cross_dex_paper_tick.log"
