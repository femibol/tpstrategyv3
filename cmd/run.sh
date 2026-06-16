#!/bin/bash
set +e
cd /opt/trading-bot

echo "=== BEFORE ==="
git rev-parse --short HEAD
git status --porcelain | head -10

echo "=== fetch origin/main ==="
git fetch origin main 2>&1 | tail -3

# Stash any in-flight auto-tuner edits (config/*) so the merge can't conflict.
# These get re-applied on the next tuner cycle anyway. PR #212 (overlay
# pattern) makes this cleaner going forward, but the bot is currently on
# pre-#212 code so the legacy stash-or-conflict is still the path.
if ! git diff --quiet -- config/; then
  echo "=== stashing local config edits ==="
  git stash push -u -m "pre-deploy auto-stash $(date -u +%FT%TZ)" -- config/ 2>&1 | tail -2
fi

echo "=== checkout + reset to origin/main ==="
git checkout main 2>&1 | tail -2
git reset --hard origin/main 2>&1 | tail -2

echo "=== AFTER ==="
git rev-parse --short HEAD

echo "=== restart trading-bot container ==="
docker restart trading-bot-trading-bot-1 2>&1 | tail -2
sleep 4

echo "=== wait for /health (up to 60s) ==="
ok=""
for i in $(seq 1 20); do
  resp=$(curl -s -m 3 http://localhost:5000/health 2>/dev/null)
  if echo "$resp" | grep -q '"status":"ok"'; then
    echo "ready after $((i*3))s -- $resp"; ok=1; break
  fi
  sleep 3
done
[ -z "$ok" ] && { echo "DASH DID NOT BECOME READY"; docker logs --tail 25 trading-bot-trading-bot-1 2>&1 | tail -25; exit 1; }

echo "=== container status ==="
docker ps --filter name=trading-bot-trading-bot --format '{{.Status}}'

echo "=== first lines of startup log ==="
docker logs --tail 30 trading-bot-trading-bot-1 2>&1 | grep -E "TRADE HISTORY|PERF STATS|Loaded strategy|IBKR connected|Dashboard|NEWS FEED" | head -20
echo "=== done ==="
