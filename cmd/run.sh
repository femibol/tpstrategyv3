#!/bin/bash
set +e
BASE="https://acquisitions-algebra-favorites-installation.trycloudflare.com"
echo "=== cloudflared status ==="
docker ps --filter name=cloudflared --format '{{.Names}} | {{.Status}}' 2>&1
echo "=== public /health (expect JSON via tunnel) ==="
curl -s -m 15 "$BASE/health" 2>&1; echo
echo "=== public / no-auth (expect 401) ==="
curl -s -m 15 -o /dev/null -w "HTTP %{http_code}\n" "$BASE/" 2>&1
KEY=""
for f in /opt/trading-bot/.env /opt/trading-bot-cmd/.env; do
  [ -f "$f" ] && KEY=$(grep -E '^DASHBOARD_SECRET_KEY=' "$f" | head -1 | cut -d= -f2- | tr -d '"'"'"'' ) && [ -n "$KEY" ] && break
done
if [ -n "$KEY" ]; then
  echo "=== public / WITH auth (expect 200) ==="
  curl -s -m 15 -o /dev/null -w "HTTP %{http_code}\n" -u "admin:$KEY" "$BASE/" 2>&1
  echo "=== public /api/status WITH auth (expect 200) ==="
  curl -s -m 15 -o /dev/null -w "HTTP %{http_code}\n" -u "admin:$KEY" "$BASE/api/status" 2>&1
else
  echo "=== could not read DASHBOARD_SECRET_KEY from .env (auth test skipped) ==="
fi
