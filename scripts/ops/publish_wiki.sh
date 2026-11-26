#!/bin/zsh
set -euo pipefail

# Publishes docs/wiki/*.md to GitHub Wiki after the wiki has been initialized via GitHub UI
# (one click on https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/wiki/_new).
ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
WIKI_URL="https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi.wiki.git"
TMP_DIR="/tmp/hl_quant_wiki_pub_$$"
trap 'rm -rf "${TMP_DIR}"' EXIT

git clone "${WIKI_URL}" "${TMP_DIR}"
cp "${ROOT_DIR}"/docs/wiki/*.md "${TMP_DIR}/"
cd "${TMP_DIR}"
git add -A
if git diff --cached --quiet; then
  echo "Wiki is already up to date."
  exit 0
fi
git commit -m "docs(wiki): sync technical documentation from docs/wiki"
git push origin master
echo "Published wiki pages to ${WIKI_URL}"
