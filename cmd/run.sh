#!/bin/bash
set +e
cd /opt/trading-bot

echo "=== positions_state.json — does it exist? ==="
docker exec trading-bot-trading-bot-1 ls -la /app/data/positions_state.json 2>&1
echo
echo "=== contents (truncated) ==="
docker exec trading-bot-trading-bot-1 head -c 3000 /app/data/positions_state.json 2>&1
echo
echo "=== all files in /app/data/ ==="
docker exec trading-bot-trading-bot-1 ls -la /app/data/ 2>&1 | head -30
echo
echo "=== check if data/ is bind-mounted (survives container restart)? ==="
docker inspect trading-bot-trading-bot-1 --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}' 2>&1
echo
echo "=== boot log: did the engine try to load persisted positions? ==="
docker logs --tail 500 trading-bot-trading-bot-1 2>&1 | grep -iE "Load persisted|persisted position|loaded.*positions|Synced.*positions|positions_state|state load" | head -10
echo
echo "=== current bot positions in memory ==="
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"
curl -s -m 8 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/positions" | python3 -m json.tool 2>&1 | head -20
