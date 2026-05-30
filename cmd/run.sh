#!/bin/bash
echo "=== container health ==="
docker compose -f /opt/trading-bot/docker-compose.yml ps --format json 2>/dev/null | python3 -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        print(f\"  {d.get('Name','?'):<35s} state={d.get('State','?'):<10s} health={d.get('Health','?'):<10s} {d.get('Status','?')}\")
    except: pass
"
echo ""
echo "=== dashboard port listening on host ==="
ss -tlnp 2>/dev/null | grep -E ":5000" || echo "NO LISTEN on :5000"
echo ""
echo "=== curl dashboard from inside the host ==="
curl -s -m 5 -o /dev/null -w "HTTP %{http_code} time=%{time_total}s\n" http://localhost:5000/health
echo ""
echo "=== curl dashboard via the public IP ==="
PUBLIC=$(curl -s -m 3 ifconfig.me 2>/dev/null || echo "?")
echo "public IP: $PUBLIC"
curl -s -m 5 -o /dev/null -w "HTTP %{http_code} time=%{time_total}s\n" http://$PUBLIC:5000/health
echo ""
echo "=== bot container recent flask errors ==="
docker logs trading-bot-trading-bot-1 --since 30m 2>&1 | grep -iE "flask|werkzeug|dashboard|HTTP|5000|exception|traceback" | tail -15
echo ""
echo "=== bot container last 15 log lines ==="
docker logs trading-bot-trading-bot-1 --tail 15 2>&1
