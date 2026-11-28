#!/usr/bin/env zsh
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: run_hyperliquid_snapshot_tick.sh

Single local cron tick to collect a public Hyperliquid snapshot
(metaAndAssetCtxs, predictedFundings, L2 books) for future funding-arb research.
Read-only public API; does not touch DuckDB or place orders.

Recommended local cron (UTC/local hourly at :50, 10m before HL funding settlement):
  50 * * * * zsh /path/to/scripts/ops/run_hyperliquid_snapshot_tick.sh

Environment:
  HL_SNAPSHOT_COINS       Comma-separated HL perp names
                          Default: BTC,ETH,SOL,HYPE,DOGE,XRP,SUI,ENA,WIF,kPEPE,AVAX,LINK,TAO
  HL_SNAPSHOT_TRADES      1 to also collect recent trades, 0 to skip (default: 0)
  HL_SNAPSHOT_OUTPUT_ROOT Optional override for --output-root (default: $BACKTESTER_RAW_ROOT/hyperliquid)
  PYTHON_BIN              Optional python binary override
  BACKTESTER_LOG_DIR      Optional log directory override
EOF
  exit 0
fi

SCRIPT_PATH="${0:A}"
ROOT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")/../.." && pwd)"
DEFAULT_PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
if [[ -x "${DEFAULT_PYTHON_BIN}" ]]; then
  PYTHON_BIN="${PYTHON_BIN:-${DEFAULT_PYTHON_BIN}}"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
LOCK_FILE="${HL_LOCK_FILE:-${ROOT_DIR}/.neira/state/locks/hyperliquid_snapshot_tick.flock}"

if [[ "${HL_OPS_LOCK_HELD:-0}" != "1" ]]; then
  cd "${ROOT_DIR}"
  HL_OPS_LOCK_HELD=1 exec "${PYTHON_BIN}" -m hl_quant.ops_lock "${LOCK_FILE}" -- zsh "${SCRIPT_PATH}" "$@"
fi

HL_SNAPSHOT_COINS="${HL_SNAPSHOT_COINS:-BTC,ETH,SOL,HYPE,DOGE,XRP,SUI,ENA,WIF,kPEPE,AVAX,LINK,TAO}"
HL_SNAPSHOT_TRADES="${HL_SNAPSHOT_TRADES:-0}"
HL_SNAPSHOT_OUTPUT_ROOT="${HL_SNAPSHOT_OUTPUT_ROOT:-}"

RUN_DATE="$(date -u +%F)"
RUN_TIME="$(date -u +%H%M)"
LOG_DIR="${BACKTESTER_LOG_DIR:-${ROOT_DIR}/.neira/logs/runs/${RUN_DATE}/${RUN_TIME}/hyperliquid_snapshot_tick}"
LOG_FILE="${LOG_DIR}/run_hyperliquid_snapshot_tick.log"
mkdir -p "${LOG_DIR}"

CMD=(
  "${PYTHON_BIN}"
  "${ROOT_DIR}/scripts/analytics/collect_hyperliquid_market_data.py"
  snapshot
  --coins "${HL_SNAPSHOT_COINS}"
)
if [[ "${HL_SNAPSHOT_TRADES}" == "1" || "${HL_SNAPSHOT_TRADES:l}" == "true" ]]; then
  CMD+=(--trades)
else
  CMD+=(--no-trades)
fi
if [[ -n "${HL_SNAPSHOT_OUTPUT_ROOT}" ]]; then
  CMD+=(--output-root "${HL_SNAPSHOT_OUTPUT_ROOT}")
fi
CMD+=("$@")

{
  echo "utc_now=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "coins=${HL_SNAPSHOT_COINS}"
  echo "trades=${HL_SNAPSHOT_TRADES}"
} >> "${LOG_FILE}"

"${CMD[@]}" | tee -a "${LOG_FILE}"
