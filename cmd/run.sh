#!/bin/bash
set +e
cd /opt/trading-bot
bash scripts/deploy-vps.sh main 2>&1 | tail -6
echo
echo "=== verify PR #251 risk floor live in container ==="
docker exec trading-bot-trading-bot-1 bash -c "cd /app && python3 -c \"
import sys, yaml; sys.path.insert(0, '/app')
r = yaml.safe_load(open('config/settings.yaml'))['risk']
c = yaml.safe_load(open('config/settings.yaml'))['crypto']['risk']
print(f'min_dollar_risk_per_strategy = {r.get(\\\"min_dollar_risk_per_strategy\\\")} (expect mean_reversion: 100)')
print(f'crypto max_position_size_pct = {c[\\\"max_position_size_pct\\\"]} (expect 0.15)')
\" 2>&1"
docker exec trading-bot-trading-bot-1 grep -c "STRATEGY RISK FLOOR" /app/bot/risk/position_sizer.py 2>&1
echo "(expect >= 1 — floor code present)"
echo
echo "=== containers ==="
docker ps --format '{{.Names}}: {{.Status}}'
echo
echo "=== watch for the first floored crypto sizing (may be empty until next signal) ==="
docker logs --since 10m trading-bot-trading-bot-1 2>&1 | grep -E "STRATEGY RISK FLOOR|Position size \(crypto\)" | tail -4
df -h / | tail -1
