#!/usr/bin/env bash
# Close crypto orphans on TradersPost/Coinbase that the engine isn't tracking.
# Reads the crypto webhook URL from .env, fetches a live price per symbol from
# Coinbase, then posts an exit signal per orphan.
#
# Usage:  ./scripts/close_crypto_orphans.sh
#         ./scripts/close_crypto_orphans.sh --dry-run    (print payloads only)
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found in $(pwd)" >&2
  exit 1
fi

URL="$(grep -E '^TRADERSPOST_WEBHOOK_URL_CRYPTO=' .env | head -1 | cut -d= -f2-)"
if [[ -z "${URL}" ]]; then
  echo "ERROR: TRADERSPOST_WEBHOOK_URL_CRYPTO not set in .env" >&2
  exit 1
fi

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=1; fi

# Orphans identified from signal_log net-qty walk on 2026-05-18:
ORPHANS=(
  "ATOM-USD:878.8914"
  "ICP-USD:718.6935"
  "RNDR-USD:1033.9172"
)

for entry in "${ORPHANS[@]}"; do
  SYM="${entry%%:*}"
  QTY="${entry##*:}"

  PX="$(curl -fsS "https://api.coinbase.com/v2/prices/${SYM}/spot" \
        | python3 -c "import json,sys;print(round(float(json.load(sys.stdin)['data']['amount']),4))" 2>/dev/null || true)"
  if [[ -z "${PX}" ]]; then
    echo "[${SYM}] SKIP — could not fetch live price from Coinbase"
    continue
  fi

  PAYLOAD="$(python3 -c "
import json
print(json.dumps({'ticker':'${SYM}','action':'exit','quantity':float('${QTY}'),'price':float('${PX}')}))
")"

  echo "─────────────────────────────────────────────────"
  echo "[${SYM}] qty=${QTY}  live=\$${PX}  est_value=\$$(python3 -c "print(round(float('${QTY}')*float('${PX}'),2))")"
  echo "payload: ${PAYLOAD}"

  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "DRY-RUN — not sending"
    continue
  fi

  RESP="$(curl -sS -w '\nHTTP %{http_code}' -X POST "${URL}" \
          -H "Content-Type: application/json" -d "${PAYLOAD}" || true)"
  echo "response: ${RESP}"
  sleep 1
done

echo "─────────────────────────────────────────────────"
echo "Done. Verify on Coinbase before restarting the bot."
