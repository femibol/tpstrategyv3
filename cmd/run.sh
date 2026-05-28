#!/bin/bash
set -e
echo "=== pull #182 on host ==="
cd /opt/trading-bot
git fetch origin main
git checkout main 2>/dev/null || true
git pull --ff-only origin main
git log --oneline -3
echo ""
echo "=== restart bot (bind-mount hot reload) ==="
docker restart trading-bot-trading-bot-1
sleep 6
docker inspect -f 'state: {{.State.Status}}  started: {{.State.StartedAt}}  health: {{.State.Health.Status}}' trading-bot-trading-bot-1
echo ""
echo "=== grep new guard in running bot/engine.py ==="
grep -A2 "EQUITY MARKET CLOSED" /opt/trading-bot/bot/engine.py | head -8
