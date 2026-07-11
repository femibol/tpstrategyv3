#!/bin/bash
set +e
cd /opt/trading-bot
bash scripts/deploy-vps.sh main 2>&1 | tail -6
echo
echo "=== PR #253 verify ==="
docker exec trading-bot-trading-bot-1 bash -c "cd /app && python3 -c \"
import yaml
c = yaml.safe_load(open('config/settings.yaml'))['crypto']['risk']
st = yaml.safe_load(open('config/strategies.yaml'))
print(f'crypto.capital_base       = {c.get(\\\"capital_base\\\")} (expect 0 until go-live)')
print(f'trend_rider universe size = {len(st[\\\"daily_trend_rider\\\"][\\\"symbols\\\"])} (expect 20)')
\" 2>&1"
docker exec trading-bot-trading-bot-1 grep -c "BRACKET CHILDREN RESIZED" /app/bot/brokers/ibkr.py 2>&1
echo "(expect >= 1 — resize guard present)"
docker exec trading-bot-trading-bot-1 grep -c "crypto_capital_base" /app/bot/risk/position_sizer.py 2>&1
echo "(expect >= 2 — capital base wired)"
echo
echo "=== tonight's fixes in action: floor + runner + trend rider activity (last 8h) ==="
docker logs --since 8h trading-bot-trading-bot-1 2>&1 | grep -E "STRATEGY RISK FLOOR|momentum_runner.*APPROVED|daily_trend_rider.*SIGNAL|TREND RIDER" | tail -8
echo "--- crypto sizing sample (should show ~\$100 risk now) ---"
docker logs --since 12h trading-bot-trading-bot-1 2>&1 | grep "Position size (crypto)" | tail -3
echo
echo "=== containers + disk ==="
docker ps --format '{{.Names}}: {{.Status}}'
df -h / | tail -1
