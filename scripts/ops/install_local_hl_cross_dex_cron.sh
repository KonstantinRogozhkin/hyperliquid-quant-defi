#!/bin/zsh
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: install_local_hl_cross_dex_cron.sh

Installs one managed local crontab block for the read-only Hyperliquid x Lighter
Dual-Leg L2 Cross-DEX Paper Tracker (default: "15 */4 * * *", 5 minutes after
the Testnet ALO executor tick).
EOF
  exit 0
fi

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
RUN_DATE="$(date +%F)"
RUN_TIME="$(date +%H%M)"
TEMP_DIR="${ROOT_DIR}/.neira/temp"
mkdir -p "${TEMP_DIR}"
BACKUP_PATH="${TEMP_DIR}/hl_cross_dex_crontab_backup_${RUN_DATE}_${RUN_TIME}.txt"
CRON_PATH="${TEMP_DIR}/hl_cross_dex_crontab_install_${RUN_DATE}_${RUN_TIME}.txt"
BEGIN_MARKER="# BEGIN Spy2Money BackTester hl cross-dex paper schedule"
END_MARKER="# END Spy2Money BackTester hl cross-dex paper schedule"
TICK_PATH="${ROOT_DIR}/scripts/ops/run_hl_cross_dex_paper_tick.sh"
HL_CROSS_DEX_CRON_SCHEDULE="${HL_CROSS_DEX_CRON_SCHEDULE:-15 */4 * * *}"

EXISTING_CRONTAB="$(crontab -l 2>/dev/null || true)"
printf "%s\n" "${EXISTING_CRONTAB}" > "${BACKUP_PATH}"

{
  if [[ -n "${EXISTING_CRONTAB}" ]]; then
    printf "%s\n" "${EXISTING_CRONTAB}" | awk -v begin="${BEGIN_MARKER}" -v end="${END_MARKER}" '
      $0 == begin {skip=1; next}
      $0 == end {skip=0; next}
      skip == 0 {print}
    '
  fi
  echo "${BEGIN_MARKER}"
  echo "${HL_CROSS_DEX_CRON_SCHEDULE} zsh ${TICK_PATH}"
  echo "${END_MARKER}"
} > "${CRON_PATH}"

crontab < "${CRON_PATH}"
echo "installed_crontab=${CRON_PATH}"
echo "backup_crontab=${BACKUP_PATH}"
