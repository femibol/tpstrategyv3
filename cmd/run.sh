#!/bin/bash
set +e
cd /opt/trading-bot
echo "=== bring the full stack up (gateway died with the disk on 06-30) ==="
docker compose up -d 2>&1 | tail -5
echo
echo "=== wait for gateway + bot to come healthy ==="
sleep 45
docker ps --format '{{.Names}}: {{.Status}}'
echo
echo "=== verify PR #248 config live in container ==="
docker exec trading-bot-trading-bot-1 bash -c "cd /app && python3 -c \"
import yaml
d = yaml.safe_load(open('config/settings.yaml'))
r = d['risk']
pt = r['profit_taking']
eq = pt.get('equity_targets') or []
print(f'equity_targets tiers     = {len(eq)} (expect 7)')
print(f'equity first tier        = {eq[0][\\\"pct_from_entry\\\"] if eq else None} (expect 0.025)')
print(f'crypto first tier        = {pt[\\\"targets\\\"][0][\\\"pct_from_entry\\\"]} (expect 0.005)')
print(f'BE equity trigger        = {r[\\\"breakeven\\\"].get(\\\"equity_trigger_pct\\\")} (expect 0.02)')
print(f'mean_reversion cap       = {r[\\\"max_dollar_risk_per_strategy\\\"][\\\"mean_reversion\\\"]} (expect 150)')
\" 2>&1"
docker exec trading-bot-trading-bot-1 grep -c "_pt_targets_for\|_be_trigger_for" /app/bot/engine.py 2>&1
echo "(expect >= 6)"
echo
echo "=== bot boot log tail ==="
sleep 15
docker logs --tail 15 trading-bot-trading-bot-1 2>&1 | tail -12
echo
echo "=== prevention: docker log caps + logrotate + df ==="
grep -n "max-size" docker-compose.yml | head -3 || echo "no docker log caps in compose (todo)"
df -h / | tail -1
