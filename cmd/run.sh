#!/bin/bash
echo "=== pull #186 on host (stash any auto-tuner edits first) ==="
cd /opt/trading-bot
git stash push -m "auto-tuner-pre-186-$(date +%s)" -- config/ 2>&1 | tail -2 || true
git fetch origin main
git checkout main 2>/dev/null || true
git pull --ff-only origin main 2>&1 | tail -5
# Don't pop — let auto-tuner re-derive its edits naturally to avoid YAML conflict
git stash drop stash@{0} 2>&1 | tail -2 || true
git log --oneline -3

echo ""
echo "=== restart trading-bot (bind-mount makes plain restart hot) ==="
docker restart trading-bot-trading-bot-1
sleep 12
docker inspect -f 'state: {{.State.Status}}  health: {{.State.Health.Status}}' trading-bot-trading-bot-1

echo ""
echo "=== confirm new code is loaded ==="
grep -A1 "def _trail_floor_price" /opt/trading-bot/bot/engine.py | head -3

echo ""
echo "=== quick state check ==="
python3 -c "
import json
with open('data/positions_state.json') as f:
    pos = json.load(f)
print(f'positions: {list(pos.keys())}')"
