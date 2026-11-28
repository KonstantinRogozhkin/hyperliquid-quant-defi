#!/usr/bin/env zsh
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage: install_local_hl_paper_cron.sh

Installs one managed local crontab block for the Hyperliquid Metamodel
L2 Paper / Shadow Tracker with Telegram summary reporting.
Default schedule: every 4 hours at :05 on canonical UTC boundaries (0,4,8,12,16,20 UTC).

Environment:
  HL_PAPER_CRON_SCHEDULE  Optional 5-field cron expression
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
BACKUP_PATH="${TEMP_DIR}/hl_paper_crontab_backup_${RUN_DATE}_${RUN_TIME}.txt"
CRON_PATH="${TEMP_DIR}/hl_paper_crontab_install_${RUN_DATE}_${RUN_TIME}.txt"
LEGACY_BEGIN_MARKER="# BEGIN Spy2Money BackTester hl metamodel paper schedule"
LEGACY_END_MARKER="# END Spy2Money BackTester hl metamodel paper schedule"
BEGIN_MARKER="# BEGIN hyperliquid-quant-defi metamodel paper schedule"
END_MARKER="# END hyperliquid-quant-defi metamodel paper schedule"
TICK_PATH="${ROOT_DIR}/scripts/ops/run_hl_metamodel_paper_tick.sh"

UTC_OFFSET_H="$("${PYTHON_HELPER}" -c 'import datetime as d; print(round((d.datetime.now().astimezone().utcoffset().total_seconds()) / 3600))')"
HL_4H_HOURS="$("${PYTHON_HELPER}" -c 'import sys; off=int(sys.argv[1]); print(",".join(str(x) for x in sorted({(h + off) % 24 for h in (0, 4, 8, 12, 16, 20)})))' "${UTC_OFFSET_H}")"
if [[ "${UTC_OFFSET_H}" == "0" && "$(uname -s)" != "Darwin" ]]; then
  CRON_TZ_HEADER="CRON_TZ=UTC"
else
  CRON_TZ_HEADER="# CRON_TZ=UTC (hours mapped to local UTC offset ${UTC_OFFSET_H}h for BSD/Vixie cron)"
fi
HL_PAPER_CRON_SCHEDULE="${HL_PAPER_CRON_SCHEDULE:-5 ${HL_4H_HOURS} * * *}"

EXISTING_CRONTAB="$(crontab -l 2>/dev/null || true)"
# Refuse to silently re-activate blocks that cutover_local_crons_to_dokploy.py disabled (double-runs vs hl-worker).
if [[ "${HL_FORCE_LOCAL_CRONS:-0}" != "1" ]] && printf "%s\n" "${EXISTING_CRONTAB}" | grep -qF "(cutover to Dokploy worker)"; then
  echo "refusing_install: current crontab has hyperliquid-quant-defi blocks disabled by the Dokploy cutover (\"# Disabled ... (cutover to Dokploy worker)\")." >&2
  echo "Re-installing local crons would double-run jobs alongside the Dokploy hl-worker. Set HL_FORCE_LOCAL_CRONS=1 to override." >&2
  exit 3
fi
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
  echo "${CRON_TZ_HEADER}"
  echo "${HL_PAPER_CRON_SCHEDULE} TZ=UTC zsh \"${TICK_PATH}\""
  echo "${END_MARKER}"
} > "${CRON_PATH}"

TMP_CRON="$(mktemp /tmp/hl_cron.XXXXXX)"
trap 'rm -f "${TMP_CRON}"' EXIT
cp "${CRON_PATH}" "${TMP_CRON}"
crontab "${TMP_CRON}"
echo "installed_crontab=${CRON_PATH}"
echo "backup_crontab=${BACKUP_PATH}"
