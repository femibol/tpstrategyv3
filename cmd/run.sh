#!/bin/bash
set -e
echo "=== pull main on host ==="
cd /opt/trading-bot
git fetch origin main
git checkout main 2>/dev/null || true
git pull --ff-only origin main
git log --oneline -3

echo ""
echo "=== restart bot to pick up new code (bind-mount makes this hot) ==="
docker restart trading-bot-trading-bot-1
sleep 6
docker inspect -f 'state: {{.State.Status}}  started: {{.State.StartedAt}}  health: {{.State.Health.Status}}' trading-bot-trading-bot-1

echo ""
echo "=== verify config picked up: grep loaded threshold ==="
sleep 10  # wait for bot to load strategies
docker logs trading-bot-trading-bot-1 2>&1 | grep -iE "crypto_trend_min_pct|min_change.*0.05|loaded.*mean_reversion" | tail -5

echo ""
echo "=== first heartbeat post-restart ==="
sleep 15
docker logs trading-bot-trading-bot-1 --since 30s 2>&1 | grep -E "CRYPTO FAST LANE HEARTBEAT" | tail -1
