#!/usr/bin/env zsh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOCK_DIR="/tmp/hl_spot_perp_paper_tick.lock"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

cd "$REPO_ROOT"
exec "${REPO_ROOT}/.venv/bin/python" "${REPO_ROOT}/scripts/analytics/run_hl_spot_perp_paper_tick.py" "$@"
