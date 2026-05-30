#!/bin/bash
set -e
echo "=== pull #184 ==="
cd /opt/trading-bot
git fetch origin main
git checkout main 2>/dev/null || true
git pull --ff-only origin main
git log --oneline -3
echo ""
echo "=== full container recreate (clears wedged IBKR worker too) ==="
docker compose up -d --force-recreate trading-bot
sleep 12
docker inspect -f 'state: {{.State.Status}}  started: {{.State.StartedAt}}  health: {{.State.Health.Status}}' trading-bot-trading-bot-1
echo ""
echo "=== check DELL state after restart ==="
sleep 8
python3 -c "
import json
with open('data/positions_state.json') as f:
    pos = json.load(f)
d = pos.get('DELL')
if d:
    print(f'  qty={d[\"quantity\"]} entry=\${d[\"entry_price\"]:.2f} current=\${d.get(\"current_price\",0):.2f} broker_stop=\${d.get(\"broker_stop_price\",0):.2f} stop_order_id={d.get(\"broker_stop_order_id\")}')
else:
    print('  (no DELL — already flat)')
"
echo ""
echo "=== bot log first 30s after restart, filtered for DELL + bracket + IBKR ==="
docker logs trading-bot-trading-bot-1 --since 30s 2>&1 | grep -iE "DELL|BRACKET|IBKR.*connected|sync|stop" | tail -20
