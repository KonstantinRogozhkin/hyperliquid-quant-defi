#!/usr/bin/env zsh
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: install_local_hl_frontier_cron.sh

Installs one managed local crontab block for the read-only 4-hour Frontier Persistence
tracker (HIP-3 Builder-Perps + Pendle Boros + HyperEVM Yield Stack).
Default schedule: "20 */4 * * *"
EOF
  exit 0
fi

SCRIPT_PATH="${0:A}"
ROOT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")/../.." && pwd)"
RUN_DATE="$(date +%F)"
RUN_TIME="$(date +%H%M)"
TEMP_DIR="${ROOT_DIR}/.neira/temp"
mkdir -p "${TEMP_DIR}"
BACKUP_PATH="${TEMP_DIR}/hl_frontier_crontab_backup_${RUN_DATE}_${RUN_TIME}.txt"
CRON_PATH="${TEMP_DIR}/hl_frontier_crontab_install_${RUN_DATE}_${RUN_TIME}.txt"
LEGACY_BEGIN_MARKER="# BEGIN Spy2Money BackTester hl frontier persistence schedule"
LEGACY_END_MARKER="# END Spy2Money BackTester hl frontier persistence schedule"
BEGIN_MARKER="# BEGIN hyperliquid-quant-defi frontier persistence schedule"
END_MARKER="# END hyperliquid-quant-defi frontier persistence schedule"
TICK_PATH="${ROOT_DIR}/scripts/ops/run_hl_frontier_persistence_tick.sh"
HL_FRONTIER_CRON_SCHEDULE="${HL_FRONTIER_CRON_SCHEDULE:-20 */4 * * *}"

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
  echo "${HL_FRONTIER_CRON_SCHEDULE} zsh \"${TICK_PATH}\""
  echo "${END_MARKER}"
} > "${CRON_PATH}"

TMP_CRON="$(mktemp /tmp/hl_cron.XXXXXX)"
trap 'rm -f "${TMP_CRON}"' EXIT
cp "${CRON_PATH}" "${TMP_CRON}"
crontab "${TMP_CRON}"
echo "installed_crontab=${CRON_PATH}"
echo "backup_crontab=${BACKUP_PATH}"
