#!/bin/zsh
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: run_hl_testnet_attribution_daily.sh

Runs the read-only Hyperliquid TESTNET post-trade PnL attribution, maker fill-rate,
markout & liquidation-risk audit, and delivers the Rich HTML summary to Telegram.
EOF
  exit 0
fi

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
export PYTHONPATH="${ROOT_DIR}/src:${PYTHONPATH:-}"

LOCK_DIR="${ROOT_DIR}/.neira/state/hl_testnet_attribution.lock"
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
LOG_DIR="${BACKTESTER_LOG_DIR:-${ROOT_DIR}/.neira/logs/runs/${RUN_DATE}/${RUN_TIME}/hl_testnet_attribution}"
mkdir -p "${LOG_DIR}"

CMD=(
  "${PYTHON_BIN}" "${ROOT_DIR}/scripts/analytics/run_hl_testnet_attribution.py"
)
if [[ "${HL_ATTRIBUTION_TELEGRAM:-1}" == "1" ]]; then
  CMD+=(--telegram)
fi

"${CMD[@]}" | tee -a "${LOG_DIR}/run_hl_testnet_attribution.log"

# Quietly refresh local offline HTML dashboard under data/paper (never touches git-tracked docs/)
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/analytics/build_hl_portfolio_dashboard.py" \
  --output-html "${ROOT_DIR}/data/paper/hl_desk_dashboard.html" \
  >> "${LOG_DIR}/run_hl_testnet_attribution.log" 2>&1 || true
