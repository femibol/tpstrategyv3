#!/bin/bash
cd /opt/trading-bot
echo "=== running code: PR #186 deployed? ==="
echo "host HEAD: $(git rev-parse HEAD)"
echo "host log:"
git log --oneline -5
echo ""
grep -A3 "def _trail_floor_price" bot/engine.py | head -6
echo "callers in engine.py:"
grep -c "self._trail_floor_price(" bot/engine.py
echo ""

echo "=== uptime ==="
docker inspect -f 'started: {{.State.StartedAt}}  health: {{.State.Health.Status}}' trading-bot-trading-bot-1

echo ""
echo "=== confirm running container's engine.py also has the floor helper ==="
docker exec trading-bot-trading-bot-1 grep -c "self._trail_floor_price(" /app/bot/engine.py 2>&1

echo ""
echo "=== trace XLM trailing_stop exit at 03:46 — what's the trail value vs exit price? ==="
docker logs trading-bot-trading-bot-1 --since 30h 2>&1 | grep -B2 -A2 "Trail stop.*XLM\|TRAIL.*XLM\|trailing_stop.*XLM" | tail -20

echo ""
echo "=== Friday close trades — DELL/PLTA/MARA ==="
python3 -c "
import json
with open('data/trade_history.json') as f:
    trades = json.load(f)
fri = [t for t in trades if t.get('exit_time','').startswith('2026-05-30')]
print(f'Friday closed trades: {len(fri)}')
for t in fri:
    print(f\"  {t['exit_time'][:19]}  {t.get('symbol','?'):<10s} {t.get('strategy','?'):<18s} {t.get('reason','?'):<22s} pnl=\${t.get('pnl',0):+8.2f} pct={t.get('pnl_pct',0)*100:+5.2f}% hold={t.get('hold_time_mins',0):.0f}m\")
print()
print(f'Friday net: \${sum(t[\"pnl\"] for t in fri):+.2f}')
"
