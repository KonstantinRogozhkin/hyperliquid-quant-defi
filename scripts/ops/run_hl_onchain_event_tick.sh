#!/bin/zsh
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

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
export PYTHONPATH="${ROOT_DIR}/src:${PYTHONPATH:-}"

LOCK_DIR="${ROOT_DIR}/.neira/state/hl_onchain_events.lock"
mkdir -p "${ROOT_DIR}/.neira/state"
if mkdir "${LOCK_DIR}" 2>/dev/null; then
  echo "$$" > "${LOCK_DIR}/pid"
else
  if [[ -f "${LOCK_DIR}/pid" ]]; then
    ACTIVE_PID="$(cat "${LOCK_DIR}/pid" 2>/dev/null || true)"
    if [[ -n "${ACTIVE_PID}" ]] && kill -0 "${ACTIVE_PID}" 2>/dev/null; then
      exit 0
    fi
  fi
  rm -rf "${LOCK_DIR}"
  mkdir "${LOCK_DIR}"
  echo "$$" > "${LOCK_DIR}/pid"
fi
trap 'rm -rf "${LOCK_DIR}"' EXIT

RUN_DATE="$(date -u +%F)"
RUN_TIME="$(date -u +%H%M)"
LOG_DIR="${BACKTESTER_LOG_DIR:-${ROOT_DIR}/.neira/logs/runs/${RUN_DATE}/${RUN_TIME}/hl_onchain_events}"
mkdir -p "${LOG_DIR}"

CMD=("${PYTHON_BIN}" "${ROOT_DIR}/scripts/analytics/run_hl_onchain_event_tick.py")
if [[ "${HL_ONCHAIN_TELEGRAM:-1}" == "1" ]]; then
  CMD+=(--telegram-on-alert)
fi

"${CMD[@]}" | tee -a "${LOG_DIR}/run_hl_onchain_event_tick.log"
