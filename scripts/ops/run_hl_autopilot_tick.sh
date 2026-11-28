#!/usr/bin/env zsh
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: run_hl_autopilot_tick.sh

Runs one lock-protected Neutral Multi-Sleeve Autopilot (+ <=5% Crisis-Alpha CTA)
allocation tick and delivers the Rich HTML summary to Telegram (set HL_AUTOPILOT_TELEGRAM=0 to disable).
EOF
  exit 0
fi

SCRIPT_PATH="${0:A}"
ROOT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
DATA_ROOT="${HL_DATA_ROOT:-${ROOT_DIR}/data/paper}"
LOCK_FILE="${HL_LOCK_FILE:-${ROOT_DIR}/.neira/state/locks/hl_autopilot_tick.flock}"

if [[ "${HL_OPS_LOCK_HELD:-0}" != "1" ]]; then
  cd "${ROOT_DIR}"
  HL_OPS_LOCK_HELD=1 exec "${PYTHON_BIN}" -m hl_quant.ops_lock "${LOCK_FILE}" -- zsh "${SCRIPT_PATH}" "$@"
fi

RUN_DATE="$(date -u +%F)"
RUN_TIME="$(date -u +%H%M)"
LOG_DIR="${BACKTESTER_LOG_DIR:-${ROOT_DIR}/.neira/logs/runs/${RUN_DATE}/${RUN_TIME}/hl_autopilot}"
mkdir -p "${LOG_DIR}"

HL_AUTOPILOT_TELEGRAM="${HL_AUTOPILOT_TELEGRAM:-1}"
CMD=(
  "${PYTHON_BIN}" "${ROOT_DIR}/scripts/analytics/run_hl_autopilot_tick.py"
  --data-root "${DATA_ROOT}"
)
if [[ "${HL_AUTOPILOT_TELEGRAM}" == "1" || "${HL_AUTOPILOT_TELEGRAM:l}" == "true" ]]; then
  CMD+=(--telegram)
fi
CMD+=("$@")

"${CMD[@]}" | tee -a "${LOG_DIR}/run_hl_autopilot_tick.log"
