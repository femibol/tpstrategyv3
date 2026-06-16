#!/bin/bash
set +e
KEY=""
for f in /opt/trading-bot/.env /opt/trading-bot-cmd/.env; do
  if [ -f "$f" ]; then
    KEY=$(grep -E '^DASHBOARD_SECRET_KEY=' "$f" | head -1 | cut -d= -f2- | tr -d '"'"'"'')
    [ -n "$KEY" ] && { echo "source: $f"; break; }
  fi
done
if [ -z "$KEY" ]; then
  echo "DASHBOARD_SECRET_KEY not found in .env"
  exit 0
fi
LEN=${#KEY}
echo "length: $LEN chars"
echo "starts with: ${KEY:0:6}"
echo "ends with:   ${KEY: -6}"
echo "(middle hidden — masked fingerprint only)"
