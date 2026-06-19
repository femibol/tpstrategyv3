#!/bin/bash
set +e
cd /opt/trading-bot
bash scripts/deploy-vps.sh main 2>&1 | tail -25
echo
echo "=== verify watchlist guard is live ==="
docker exec trading-bot-trading-bot-1 bash -c "grep -c 'WATCHLIST BLOCKED' /app/bot/engine.py 2>&1"
echo "(expect 1 = the log.warning line is present)"
echo
echo "=== preset cleansed of leveraged ETFs ==="
docker exec trading-bot-trading-bot-1 bash -c "cd /app && python3 -c \"
import sys; sys.path.insert(0, '/app')
from bot.engine import TradingEngine
preset = TradingEngine.WATCHLIST_PRESETS.get('sp500_etfs', {})
syms = preset.get('symbols', [])
banned = {'SOXL','TQQQ','SOXS','SQQQ','SPXU','SPXS','UVXY'}
leaked = [s for s in syms if s in banned]
print(f'preset symbols: {syms}')
print(f'leveraged in preset: {leaked} (expect [])')
\" 2>&1"
echo
echo "=== bot log: WATCHLIST BLOCKED warnings since restart? ==="
docker logs --tail 300 trading-bot-trading-bot-1 2>&1 | grep "WATCHLIST BLOCKED" | tail -5
