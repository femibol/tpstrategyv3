#!/bin/bash
set +e
cd /opt/trading-bot
bash scripts/deploy-vps.sh main 2>&1 | tail -25
echo
echo "=== positions_state.json after restart ==="
docker exec trading-bot-trading-bot-1 ls -la /app/data/positions_state.json 2>&1
echo
echo "=== boot log: persisted positions loaded? ==="
docker logs --tail 200 trading-bot-trading-bot-1 2>&1 | grep -iE "Load persisted|persisted position|loaded.*positions" | tail -3
echo
echo "=== watch for PERSIST WARNING lines (the new visibility) ==="
docker logs --tail 200 trading-bot-trading-bot-1 2>&1 | grep "PERSIST:" | tail -5
echo "  (if persist is failing for any reason, it'll surface here now instead of vanishing silently)"
echo
echo "=== current positions via API ==="
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"
curl -s -m 8 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/positions" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'positions count: {len(d)}')
for p in d:
    print(f'  {p.get(\"symbol\"):10s}  pnl=\${p.get(\"pnl_dollars\",0):+.2f}  source={p.get(\"price_source\")}')
" 2>&1
