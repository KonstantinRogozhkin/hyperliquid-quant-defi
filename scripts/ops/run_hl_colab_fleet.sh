#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
WORK_DIR="${WORK_DIR:-/tmp/hl-meta-20261125}"
BUNDLE="${WORK_DIR}/dataset_bundle.parquet"
SHARDS_DIR="${WORK_DIR}/shards"
NUM_SHARDS="${NUM_SHARDS:-3}"

mkdir -p "${SHARDS_DIR}"

# Package minimal Python modules + shard runner for Colab upload
tar -czf "${WORK_DIR}/code_bundle.tar.gz" -C "${ROOT_DIR}" \
  src/overnight/__init__.py \
  src/hl_quant/__init__.py \
  src/hl_quant/funding_arb.py \
  src/hl_quant/alpha_families.py \
  scripts/analytics/run_hl_colab_shard.py

run_worker() {
  local i="$1"
  local sess="hl-meta-w${i}"
  local log="${SHARDS_DIR}/worker_${i}.log"
  trap "colab stop -s '${sess}' >/dev/null 2>&1 || true" EXIT

  {
    echo "[w${i}] creating session ${sess}"
    colab new -s "${sess}"
    echo "[w${i}] uploading bundle + code"
    colab upload -s "${sess}" "${BUNDLE}" /content/dataset_bundle.parquet
    colab upload -s "${sess}" "${WORK_DIR}/code_bundle.tar.gz" /content/code_bundle.tar.gz
    echo "[w${i}] executing shard ${i}/${NUM_SHARDS}"
    local runner="${SHARDS_DIR}/exec_w${i}.py"
    cat > "${runner}" <<EOF
import os, subprocess, sys
subprocess.check_call(["tar", "-xzf", "/content/code_bundle.tar.gz", "-C", "/content"])
env = dict(os.environ, PYTHONPATH="/content/src")
subprocess.check_call([
    sys.executable, "/content/scripts/analytics/run_hl_colab_shard.py",
    "--bundle", "/content/dataset_bundle.parquet",
    "--output-dir", "/content/out",
    "--shard-id", "${i}",
    "--num-shards", "${NUM_SHARDS}",
], env=env)
EOF
    colab exec -s "${sess}" --timeout 300 --file "${runner}"
    echo "[w${i}] downloading artifacts"
    colab download -s "${sess}" "/content/out/shard_${i}_summary.json" "${SHARDS_DIR}/shard_${i}_summary.json"
    colab download -s "${sess}" "/content/out/shard_${i}_oof_returns.parquet" "${SHARDS_DIR}/shard_${i}_oof_returns.parquet"
    colab download -s "${sess}" "/content/out/shard_${i}_stressed_returns.parquet" "${SHARDS_DIR}/shard_${i}_stressed_returns.parquet"
    echo "[w${i}] DONE"
  } > "${log}" 2>&1
}

pids=()
for ((i = 0; i < NUM_SHARDS; i++)); do
  run_worker "${i}" &
  pids+=($!)
done

fail=0
for ((i = 0; i < NUM_SHARDS; i++)); do
  pid="${pids[$((i + 1))]}"
  if ! wait "${pid}"; then
    echo "Worker ${i} FAILED — tail of log:" >&2
    tail -n 30 "${SHARDS_DIR}/worker_${i}.log" >&2 || true
    fail=1
  else
    echo "Worker ${i} OK"
    tail -n 3 "${SHARDS_DIR}/worker_${i}.log"
  fi
done
exit "${fail}"
