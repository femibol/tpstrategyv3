#!/bin/bash
echo "=== stash local config edits + pull main + pop ==="
cd /opt/trading-bot
git stash push -m "auto-tuner-writes-$(date +%s)" -- config/ 2>&1 | tail -3 || true
git fetch origin main
git checkout main 2>/dev/null || true
git pull --ff-only origin main 2>&1 | tail -5
git stash pop 2>&1 | tail -3 || true
git log --oneline -3

echo ""
echo "=== force-recreate trading-bot (unwedges IBKR worker) ==="
docker compose up -d --force-recreate trading-bot
sleep 15
docker inspect -f 'state: {{.State.Status}}  started: {{.State.StartedAt}}  health: {{.State.Health.Status}}' trading-bot-trading-bot-1

echo ""
echo "=== DELL state after restart ==="
sleep 10
python3 -c "
import json
with open('data/positions_state.json') as f:
    pos = json.load(f)
d = pos.get('DELL')
if d:
    print(f'  qty={d[\"quantity\"]} entry=\${d[\"entry_price\"]:.2f} current=\${d.get(\"current_price\",0):.2f}')
    print(f'  broker_stop_price=\${d.get(\"broker_stop_price\",0):.2f}  broker_stop_order_id={d.get(\"broker_stop_order_id\")}')
else:
    print('  (no DELL — bot synced to flat from IBKR)')
"

echo ""
echo "=== DELL/bracket activity since restart ==="
docker logs trading-bot-trading-bot-1 --since 20s 2>&1 | grep -iE "DELL|BRACKET|sync.*pos|IBKR.*connect|STOP" | tail -15
