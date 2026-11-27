#!/usr/bin/env zsh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOCK_DIR="${TMPDIR:-/tmp}/hl_spot_perp_paper_tick.lock"

# PID-aware lock: recover from stale locks left by crashes/SIGKILL.
if mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$$" > "$LOCK_DIR/pid"
else
  ACTIVE_PID="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ -n "$ACTIVE_PID" ]] && kill -0 "$ACTIVE_PID" 2>/dev/null; then
    exit 0
  fi
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR"
  echo "$$" > "$LOCK_DIR/pid"
fi
# No 'exec' below: it would replace the shell and skip this EXIT trap, leaking the lock forever.
trap 'rm -rf "$LOCK_DIR"' EXIT

cd "$REPO_ROOT"
"${REPO_ROOT}/.venv/bin/python" "${REPO_ROOT}/scripts/analytics/run_hl_spot_perp_paper_tick.py" "$@"
