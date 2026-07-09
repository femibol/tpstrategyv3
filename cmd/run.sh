#!/bin/bash
set +e
cd /opt/trading-bot
echo "=== gateway repair log ==="
tail -6 /tmp/repair.log 2>/dev/null
echo
echo "=== containers ==="
docker ps --format '{{.Names}}: {{.Status}}'
if ! docker ps --format '{{.Names}} {{.Status}}' | grep -q 'ib-gateway.*Up'; then
    echo "GATEWAY STILL DOWN — stopping here, see repair log above"
    exit 0
fi
echo
echo "=== deploy latest main (PR #248 + #249) ==="
bash scripts/deploy-vps.sh main 2>&1 | tail -6
echo "=== recreate containers so compose log caps apply ==="
docker compose up -d 2>&1 | tail -4
sleep 20
docker ps --format '{{.Names}}: {{.Status}}'
echo
echo "=== verify PR #248 in container ==="
docker exec trading-bot-trading-bot-1 bash -c "cd /app && python3 -c \"
import yaml
r = yaml.safe_load(open('config/settings.yaml'))['risk']
eq = r['profit_taking'].get('equity_targets') or []
print(f'equity ladder: {len(eq)} tiers, first {eq[0][\\\"pct_from_entry\\\"] if eq else None} (expect 7 / 0.025)')
print(f'BE equity trigger: {r[\\\"breakeven\\\"].get(\\\"equity_trigger_pct\\\")} (expect 0.02)')
print(f'mean_reversion cap: {r[\\\"max_dollar_risk_per_strategy\\\"][\\\"mean_reversion\\\"]} (expect 150)')
\" 2>&1"
docker exec trading-bot-trading-bot-1 grep -c "_pt_targets_for" /app/bot/engine.py 2>&1
echo "(expect >= 3)"
df -h / | tail -1
