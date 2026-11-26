#!/bin/zsh
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: run_hl_cross_dex_paper_tick.sh

Runs one lock-protected read-only Hyperliquid x Lighter Dual-Leg L2 Paper tick
and delivers the Rich HTML summary to Telegram (set HL_CROSS_DEX_TELEGRAM=0 to disable).
EOF
  exit 0
fi

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
export PYTHONPATH="${ROOT_DIR}/src:${PYTHONPATH:-}"

LOCK_DIR="${ROOT_DIR}/.neira/state/hl_cross_dex_paper.lock"
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
LOG_DIR="${BACKTESTER_LOG_DIR:-${ROOT_DIR}/.neira/logs/runs/${RUN_DATE}/${RUN_TIME}/hl_cross_dex_paper}"
mkdir -p "${LOG_DIR}"

CMD=("${PYTHON_BIN}" "${ROOT_DIR}/scripts/analytics/run_hl_cross_dex_paper_tick.py")
if [[ "${HL_CROSS_DEX_TELEGRAM:-1}" == "1" ]]; then
  CMD+=(--telegram)
fi

"${CMD[@]}" | tee -a "${LOG_DIR}/run_hl_cross_dex_paper_tick.log"
