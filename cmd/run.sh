#!/bin/bash
echo "=== pull #189 + stash auto-tuner config edits ==="
cd /opt/trading-bot
git stash push -m "auto-tuner-pre-189-$(date +%s)" -- config/ 2>&1 | tail -2 || true
git fetch origin main
git checkout main 2>/dev/null || true
git pull --ff-only origin main 2>&1 | tail -5
git stash drop stash@{0} 2>&1 | tail -2 || true
git log --oneline -3

echo ""
echo "=== restart bot ==="
docker restart trading-bot-trading-bot-1
sleep 12
docker inspect -f 'state: {{.State.Status}}  health: {{.State.Health.Status}}' trading-bot-trading-bot-1

echo ""
echo "=== verify the fix in the running code ==="
grep -A1 '"score"' /opt/trading-bot/bot/strategies/low_float_catalyst.py | head -3
echo ""
grep -A1 '"score"' /opt/trading-bot/bot/strategies/crypto_runner.py | head -3
