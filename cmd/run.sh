#!/bin/bash
set -e
echo "=== pull #188 + stash auto-tuner edits ==="
cd /opt/trading-bot
git stash push -m "auto-tuner-pre-188-$(date +%s)" -- config/ 2>&1 | tail -2 || true
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
echo "=== confirm fix in running code ==="
grep -A1 "is_low_float_signal" /opt/trading-bot/bot/engine.py | head -4

echo ""
echo "=== current state ==="
python3 -c "
import json
with open('data/positions_state.json') as f:
    pos = json.load(f)
print(f'positions: {list(pos.keys())}')
"
