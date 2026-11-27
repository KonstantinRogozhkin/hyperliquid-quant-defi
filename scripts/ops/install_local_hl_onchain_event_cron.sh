#!/bin/zsh
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: install_local_hl_onchain_event_cron.sh

Installs one managed local crontab block for the read-only 15-minute On-Chain Event
Alpha scanner (H2 Whale Liq Walls, H4 HyperEVM Dislocations, H5 HLP Inventory Skew).
Default schedule: "*/15 * * * *"
EOF
  exit 0
fi

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
RUN_DATE="$(date +%F)"
RUN_TIME="$(date +%H%M)"
TEMP_DIR="${ROOT_DIR}/.neira/temp"
mkdir -p "${TEMP_DIR}"
BACKUP_PATH="${TEMP_DIR}/hl_onchain_events_crontab_backup_${RUN_DATE}_${RUN_TIME}.txt"
CRON_PATH="${TEMP_DIR}/hl_onchain_events_crontab_install_${RUN_DATE}_${RUN_TIME}.txt"
LEGACY_BEGIN_MARKER="# BEGIN Spy2Money BackTester hl onchain event alpha schedule"
LEGACY_END_MARKER="# END hyperliquid-quant-defi onchain event alpha schedule"
BEGIN_MARKER="# BEGIN hyperliquid-quant-defi onchain event alpha schedule"
END_MARKER="# END hyperliquid-quant-defi onchain event alpha schedule"
TICK_PATH="${ROOT_DIR}/scripts/ops/run_hl_onchain_event_tick.sh"
HL_ONCHAIN_CRON_SCHEDULE="${HL_ONCHAIN_CRON_SCHEDULE:-*/15 * * * *}"

EXISTING_CRONTAB="$(crontab -l 2>/dev/null || true)"
printf "%s\n" "${EXISTING_CRONTAB}" > "${BACKUP_PATH}"

{
  if [[ -n "${EXISTING_CRONTAB}" ]]; then
    printf "%s\n" "${EXISTING_CRONTAB}" | awk -v begin="${BEGIN_MARKER}" -v end="${END_MARKER}" -v lbegin="${LEGACY_BEGIN_MARKER}" -v lend="${LEGACY_END_MARKER}" '
      $0 == begin || $0 == lbegin {skip=1; next}
      $0 == end || $0 == lend {skip=0; next}
      skip == 0 {print}
    '
  fi
  echo "${BEGIN_MARKER}"
  echo "${HL_ONCHAIN_CRON_SCHEDULE} zsh ${TICK_PATH}"
  echo "${END_MARKER}"
} > "${CRON_PATH}"

crontab < "${CRON_PATH}"
echo "installed_crontab=${CRON_PATH}"
echo "backup_crontab=${BACKUP_PATH}"
