#!/bin/zsh
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: install_all_hl_crons.sh

Atomically migrates and installs all 7 Hyperliquid read-only / Testnet local cron blocks
pointing to THIS repository (replacing any legacy BackTesterv2 Hyperliquid cron entries),
aligned to canonical UTC 4-hour boundaries (:05, :10, :17, :20) and 09:00 UTC daily attribution.
EOF
  exit 0
fi

SCRIPT_PATH="${0:A}"
ROOT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")/../.." && pwd)"
PYTHON_HELPER="${ROOT_DIR}/.venv/bin/python"
if [[ ! -x "${PYTHON_HELPER}" ]]; then
  PYTHON_HELPER="python3"
fi
RUN_DATE="$(date -u +%F)"
RUN_TIME="$(date -u +%H%M)"
TEMP_DIR="${ROOT_DIR}/.neira/temp"
mkdir -p "${TEMP_DIR}"
BACKUP_PATH="${TEMP_DIR}/hl_all_crons_backup_${RUN_DATE}_${RUN_TIME}.txt"
SHORT_CRON="$(mktemp /tmp/hl_cron.XXXXXX)"
trap 'rm -f "${SHORT_CRON}"' EXIT

UTC_OFFSET_H="$("${PYTHON_HELPER}" -c 'import datetime as d; print(round((d.datetime.now().astimezone().utcoffset().total_seconds()) / 3600))')"
HL_4H_HOURS="$("${PYTHON_HELPER}" -c 'import sys; off=int(sys.argv[1]); print(",".join(str(x) for x in sorted({(h + off) % 24 for h in (0, 4, 8, 12, 16, 20)})))' "${UTC_OFFSET_H}")"
HL_ATTR_HOUR="$("${PYTHON_HELPER}" -c 'import sys; off=int(sys.argv[1]); print((9 + off) % 24)' "${UTC_OFFSET_H}")"
if [[ "${UTC_OFFSET_H}" == "0" && "$(uname -s)" != "Darwin" ]]; then
  CRON_TZ_HEADER="CRON_TZ=UTC"
else
  CRON_TZ_HEADER="# CRON_TZ=UTC (hours mapped to local UTC offset ${UTC_OFFSET_H}h for BSD/Vixie cron)"
fi

EXISTING_CRONTAB="$(crontab -l 2>/dev/null || true)"
# Refuse to silently re-activate blocks that cutover_local_crons_to_dokploy.py disabled (double-runs vs hl-worker).
if [[ "${HL_FORCE_LOCAL_CRONS:-0}" != "1" ]] && printf "%s\n" "${EXISTING_CRONTAB}" | grep -qF "(cutover to Dokploy worker)"; then
  echo "refusing_install: current crontab has hyperliquid-quant-defi blocks disabled by the Dokploy cutover (\"# Disabled ... (cutover to Dokploy worker)\")." >&2
  echo "Re-installing local crons would double-run jobs alongside the Dokploy hl-worker. Set HL_FORCE_LOCAL_CRONS=1 to override." >&2
  exit 3
fi
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
${CRON_TZ_HEADER}
50 * * * * TZ=UTC zsh "${ROOT_DIR}/scripts/ops/run_hyperliquid_snapshot_tick.sh"
# END hyperliquid-quant-defi snapshot schedule
# BEGIN hyperliquid-quant-defi metamodel paper schedule
${CRON_TZ_HEADER}
5 ${HL_4H_HOURS} * * * TZ=UTC zsh "${ROOT_DIR}/scripts/ops/run_hl_metamodel_paper_tick.sh"
# END hyperliquid-quant-defi metamodel paper schedule
# BEGIN hyperliquid-quant-defi testnet executor schedule
${CRON_TZ_HEADER}
10 ${HL_4H_HOURS} * * * TZ=UTC zsh "${ROOT_DIR}/scripts/ops/run_hl_testnet_executor_tick.sh"
# END hyperliquid-quant-defi testnet executor schedule
# BEGIN hyperliquid-quant-defi cross-dex paper schedule
${CRON_TZ_HEADER}
17 ${HL_4H_HOURS} * * * TZ=UTC zsh "${ROOT_DIR}/scripts/ops/run_hl_cross_dex_paper_tick.sh"
# END hyperliquid-quant-defi cross-dex paper schedule
# BEGIN hyperliquid-quant-defi frontier persistence schedule
${CRON_TZ_HEADER}
20 ${HL_4H_HOURS} * * * TZ=UTC zsh "${ROOT_DIR}/scripts/ops/run_hl_frontier_persistence_tick.sh"
# END hyperliquid-quant-defi frontier persistence schedule
# BEGIN hyperliquid-quant-defi onchain event alpha schedule
${CRON_TZ_HEADER}
*/15 * * * * TZ=UTC zsh "${ROOT_DIR}/scripts/ops/run_hl_onchain_event_tick.sh"
# END hyperliquid-quant-defi onchain event alpha schedule
# BEGIN hyperliquid-quant-defi testnet attribution schedule
${CRON_TZ_HEADER}
0 ${HL_ATTR_HOUR} * * * TZ=UTC zsh "${ROOT_DIR}/scripts/ops/run_hl_testnet_attribution_daily.sh"
# END hyperliquid-quant-defi testnet attribution schedule
EOF
} > "${SHORT_CRON}"

crontab "${SHORT_CRON}"
echo "migrated_7_hl_crons_to=${ROOT_DIR}"
echo "backup_crontab=${BACKUP_PATH}"
