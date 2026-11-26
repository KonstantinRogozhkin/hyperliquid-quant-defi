#!/bin/zsh
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

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
export PYTHONPATH="${ROOT_DIR}/src:${PYTHONPATH:-}"
STATE_DIR="${HL_PAPER_STATE_DIR:-${ROOT_DIR}/data/paper/hl_metamodel}"

LOCK_DIR="${ROOT_DIR}/.neira/state/hl_metamodel_paper.lock"
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
"${CMD[@]}" | tee -a "${LOG_DIR}/run_hl_metamodel_paper_tick.log"
