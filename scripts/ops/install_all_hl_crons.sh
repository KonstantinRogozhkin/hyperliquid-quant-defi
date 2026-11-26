#!/bin/zsh
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: install_all_hl_crons.sh

Atomically migrates and installs all 7 Hyperliquid read-only / Testnet local cron blocks
pointing to THIS repository (replacing any legacy BackTesterv2 Hyperliquid cron entries).
EOF
  exit 0
fi

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
RUN_DATE="$(date +%F)"
RUN_TIME="$(date +%H%M)"
TEMP_DIR="${ROOT_DIR}/.neira/temp"
mkdir -p "${TEMP_DIR}"
BACKUP_PATH="${TEMP_DIR}/hl_all_crons_backup_${RUN_DATE}_${RUN_TIME}.txt"
SHORT_CRON="/tmp/hl_quant_crontab_$$.txt"
trap 'rm -f "${SHORT_CRON}"' EXIT

EXISTING_CRONTAB="$(crontab -l 2>/dev/null || true)"
printf "%s\n" "${EXISTING_CRONTAB}" > "${BACKUP_PATH}"

# Strip all 7 old/current Hyperliquid cron blocks cleanly, preserving unrelated crons
{
  if [[ -n "${EXISTING_CRONTAB}" ]]; then
    printf "%s\n" "${EXISTING_CRONTAB}" | awk '
      /^# BEGIN Spy2Money BackTester (hyperliquid|hl )/ {skip=1; next}
      /^# END Spy2Money BackTester (hyperliquid|hl )/ {skip=0; next}
      /^# BEGIN hyperliquid-quant-defi / {skip=1; next}
      /^# END hyperliquid-quant-defi / {skip=0; next}
      skip == 0 {print}
    '
  fi

  cat <<EOF
# BEGIN hyperliquid-quant-defi snapshot schedule
50 * * * * zsh ${ROOT_DIR}/scripts/ops/run_hyperliquid_snapshot_tick.sh
# END hyperliquid-quant-defi snapshot schedule
# BEGIN hyperliquid-quant-defi metamodel paper schedule
5 */4 * * * zsh ${ROOT_DIR}/scripts/ops/run_hl_metamodel_paper_tick.sh
# END hyperliquid-quant-defi metamodel paper schedule
# BEGIN hyperliquid-quant-defi testnet executor schedule
10 */4 * * * zsh ${ROOT_DIR}/scripts/ops/run_hl_testnet_executor_tick.sh
# END hyperliquid-quant-defi testnet executor schedule
# BEGIN hyperliquid-quant-defi cross-dex paper schedule
15 */4 * * * zsh ${ROOT_DIR}/scripts/ops/run_hl_cross_dex_paper_tick.sh
# END hyperliquid-quant-defi cross-dex paper schedule
# BEGIN hyperliquid-quant-defi frontier persistence schedule
20 */4 * * * zsh ${ROOT_DIR}/scripts/ops/run_hl_frontier_persistence_tick.sh
# END hyperliquid-quant-defi frontier persistence schedule
# BEGIN hyperliquid-quant-defi onchain event alpha schedule
*/15 * * * * zsh ${ROOT_DIR}/scripts/ops/run_hl_onchain_event_tick.sh
# END hyperliquid-quant-defi onchain event alpha schedule
# BEGIN hyperliquid-quant-defi testnet attribution schedule
0 6 * * * zsh ${ROOT_DIR}/scripts/ops/run_hl_testnet_attribution_daily.sh
# END hyperliquid-quant-defi testnet attribution schedule
EOF
} > "${SHORT_CRON}"

crontab "${SHORT_CRON}"
echo "migrated_7_hl_crons_to=${ROOT_DIR}"
echo "backup_crontab=${BACKUP_PATH}"
