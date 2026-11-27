#!/usr/bin/env zsh
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
export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
LOCK_FILE="${HL_LOCK_FILE:-${TMPDIR:-/tmp}/hl_testnet_attribution_daily.flock}"

if [[ "${HL_OPS_LOCK_HELD:-0}" != "1" ]]; then
  cd "${ROOT_DIR}"
  HL_OPS_LOCK_HELD=1 exec "${PYTHON_BIN}" -m hl_quant.ops_lock "${LOCK_FILE}" -- zsh "$0" "$@"
fi

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
CMD+=("$@")

"${CMD[@]}" | tee -a "${LOG_DIR}/run_hl_testnet_attribution.log"

# Quietly refresh local offline HTML dashboard under data/paper (never touches git-tracked docs/)
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/analytics/build_hl_portfolio_dashboard.py" \
  --output-html "${ROOT_DIR}/data/paper/hl_desk_dashboard.html" \
  >> "${LOG_DIR}/run_hl_testnet_attribution.log" 2>&1 || true
