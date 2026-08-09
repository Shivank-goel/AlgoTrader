#!/usr/bin/env bash
# Kill any stale dashboard process and start fresh with latest code.
# IMPORTANT: Run this script from Terminal.app, not Cursor's integrated shell.
# Cursor sandboxes Python DNS — the trading engine cannot reach the exchange from there.
set -euo pipefail

PORT="${1:-8081}"
HOST="${2:-127.0.0.1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXCHANGE_URL="${EXCHANGE_URL:-https://cdn-ind.testnet.deltaex.org}"

echo "Checking exchange connectivity (${EXCHANGE_URL})..."
if ! curl -sf --max-time 10 "${EXCHANGE_URL}/v2/products" > /dev/null; then
  echo ""
  echo "ERROR: Cannot reach Delta Exchange at ${EXCHANGE_URL}"
  echo ""
  echo "  - Run this script from Terminal.app (not Cursor's sandbox shell)"
  echo "  - Check your internet connection and DNS"
  echo "  - Verify the testnet URL is correct in config/settings.yaml"
  exit 1
fi
echo "Exchange reachable."

echo "Stopping anything on port ${PORT}..."
PIDS=$(lsof -ti:"${PORT}" 2>/dev/null || true)
if [ -n "${PIDS}" ]; then
  kill -9 ${PIDS} 2>/dev/null || true
  sleep 1
fi

cd "${ROOT}"
source .venv/bin/activate

echo "Starting dashboard at http://${HOST}:${PORT}"
exec python3 main.py trade --host "${HOST}" --port "${PORT}"
