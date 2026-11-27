#!/usr/bin/env zsh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOCK_FILE="${TMPDIR:-/tmp}/hl_spot_perp_paper_tick.flock"

# Atomic kernel flock (released automatically on crash/SIGKILL): no stale-lock reclaim race.
# Overlapping invocations exit 0 without running the tick.
cd "$REPO_ROOT"
PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}" exec "${REPO_ROOT}/.venv/bin/python" -m hl_quant.ops_lock "$LOCK_FILE" -- \
  "${REPO_ROOT}/.venv/bin/python" "${REPO_ROOT}/scripts/analytics/run_hl_spot_perp_paper_tick.py" "$@"
