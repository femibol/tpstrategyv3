#!/bin/bash
set +e
cd /opt/trading-bot

echo "=== last 30 crypto trades (any -USD/-USDT symbol) ==="
python3 << 'PYEOF'
import json
from datetime import datetime
from zoneinfo import ZoneInfo
et = ZoneInfo('America/New_York')
with open('data/trade_history.json') as f:
    trades = json.load(f)
crypto = []
for t in trades:
    sym = (t.get('symbol') or '').upper()
    if any(sfx in sym for sfx in ('-USD','-USDT','-BTC','-ETH')):
        crypto.append(t)
print(f'total crypto trades in history: {len(crypto)}')
if not crypto:
    print('NONE.'); 
else:
    crypto_sorted = sorted(crypto, key=lambda x: x.get('exit_time') or x.get('entry_time') or '')
    print(f'first crypto: {crypto_sorted[0].get("exit_time","?")[:19]}  {crypto_sorted[0].get("symbol")}')
    print(f'last crypto:  {crypto_sorted[-1].get("exit_time","?")[:19]}  {crypto_sorted[-1].get("symbol")}')
    # Days since last crypto
    try:
        last_dt = datetime.fromisoformat((crypto_sorted[-1].get('exit_time') or crypto_sorted[-1].get('entry_time')).replace('Z','+00:00'))
        now = datetime.now(last_dt.tzinfo)
        delta = now - last_dt
        print(f'DAYS SINCE LAST CRYPTO TRADE: {delta.days} days, {delta.seconds//3600}h')
    except Exception as e:
        print(f'date parse: {e}')
    print()
    print('last 10 crypto trades:')
    for t in crypto_sorted[-10:]:
        ts = (t.get('exit_time') or t.get('entry_time') or '')[:19]
        print(f'  {ts}  {t.get("symbol"):10s}  strat={t.get("strategy"):16s}  pnl=${t.get("pnl",0):+.2f}  reason={t.get("reason")}')
PYEOF

echo
echo "=== current crypto config ==="
docker exec trading-bot-trading-bot-1 bash -c "cd /app && python3 -c \"
import sys; sys.path.insert(0, '/app')
from bot.config import Config
cfg = Config()
crypto = cfg.settings.get('crypto', {})
print(f'crypto.enabled            = {crypto.get(\\\"enabled\\\")}')
print(f'crypto.allowed_strategies = {crypto.get(\\\"allowed_strategies\\\")}')
print(f'crypto.symbols            = {crypto.get(\\\"symbols\\\")}')
print(f'crypto.symbols_suffix     = {crypto.get(\\\"symbols_suffix\\\")}')
strat = cfg.strategies
print(f'mean_reversion.enabled                  = {strat.get(\\\"mean_reversion\\\", {}).get(\\\"enabled\\\")}')
print(f'mean_reversion.crypto_trend_filter_enabled = {strat.get(\\\"mean_reversion\\\", {}).get(\\\"crypto_trend_filter_enabled\\\")}')
print(f'mean_reversion.crypto_trend_lookback_bars = {strat.get(\\\"mean_reversion\\\", {}).get(\\\"crypto_trend_lookback_bars\\\")}')
print(f'mean_reversion.crypto_trend_min_pct       = {strat.get(\\\"mean_reversion\\\", {}).get(\\\"crypto_trend_min_pct\\\")}')
print(f'crypto_runner.enabled    = {strat.get(\\\"crypto_runner\\\", {}).get(\\\"enabled\\\")}')
\" 2>&1"

echo
echo "=== crypto scan activity last 6h (verdict counts) ==="
docker logs --since 6h trading-bot-trading-bot-1 2>&1 | grep -oE "weak [0-9]+d trend|CRYPTO INJECT|BUY signal.*-USD|crypto.*disabled|CRYPTO BLOCKED" | sort | uniq -c | sort -rn | head -10
echo
echo "=== last 5 lines mentioning any crypto symbol ==="
docker logs --since 6h trading-bot-trading-bot-1 2>&1 | grep -E "[A-Z]+-USD" | tail -8

echo
echo "=== crypto symbol injection log ==="
docker logs --since 6h trading-bot-trading-bot-1 2>&1 | grep "CRYPTO INJECT" | head -3
