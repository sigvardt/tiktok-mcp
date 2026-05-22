#!/usr/bin/env bash
set -euo pipefail
VERSION="${1:?version required}"
SOURCE="${2:-pypi}"  # "testpypi" or "pypi"
URL_BASE="https://pypi.org"
if [ "$SOURCE" = "testpypi" ]; then
  URL_BASE="https://test.pypi.org"
fi
URL="$URL_BASE/pypi/tiktok-mcp/$VERSION/json"
echo "Waiting for $URL (max 5 min)..."
START=$(date +%s)
TIMEOUT=300
while true; do
  if curl -fsSL -o /dev/null -w "%{http_code}" "$URL" | grep -q "^200$"; then
    ELAPSED=$(($(date +%s) - START))
    echo "Available after ${ELAPSED}s"
    exit 0
  fi
  NOW=$(date +%s)
  if [ $((NOW - START)) -ge $TIMEOUT ]; then
    echo "Timeout after ${TIMEOUT}s"
    exit 1
  fi
  sleep 5
done
