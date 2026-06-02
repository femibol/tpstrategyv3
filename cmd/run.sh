#!/bin/bash
echo "=== pull #187 + stash any auto-tuner edits ==="
cd /opt/trading-bot
git stash push -m "auto-tuner-pre-187-$(date +%s)" -- config/ 2>&1 | tail -2 || true
git fetch origin main
git checkout main 2>/dev/null || true
git pull --ff-only origin main 2>&1 | tail -5
git stash drop stash@{0} 2>&1 | tail -2 || true
git log --oneline -3

echo ""
echo "=== restart bot ==="
docker restart trading-bot-trading-bot-1
sleep 15
docker inspect -f 'state: {{.State.Status}}  health: {{.State.Health.Status}}' trading-bot-trading-bot-1

echo ""
echo "=== watch for the cancel firing ==="
sleep 10
docker logs trading-bot-trading-bot-1 --since 30s 2>&1 | grep -iE "STALE PENDING|CANCELLED|DELL|sync" | tail -15

echo ""
echo "=== current positions ==="
python3 -c "
import json
with open('data/positions_state.json') as f:
    pos = json.load(f)
print(f'count: {len(pos)}')
for sym in pos: print(f'  {sym}')
"

echo ""
echo "=== check IBKR for any remaining pending DELL order ==="
SECRET=$(grep -E "^DASHBOARD_SECRET_KEY" .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")
curl -s -m 10 -u admin:"$SECRET" http://localhost:5000/api/positions | head -c 500
echo ""
