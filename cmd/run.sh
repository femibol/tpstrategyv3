#!/bin/bash
set +e
cd /opt/trading-bot

echo "=== runtime blocked_symbols (what the BOT actually sees) ==="
docker exec trading-bot-trading-bot-1 python3 << 'PYEOF'
# Load config the same way the engine does — through bot.config.Config
import os, sys
sys.path.insert(0, '.')
try:
    from bot.config import Config
    cfg = Config()
    blocked = cfg.risk_config.get('blocked_symbols', [])
    blocked_upper = {s.upper() for s in blocked}
    print(f'blocked_symbols count: {len(blocked)}')
    print(f'SOXL in blocked_symbols: {"SOXL" in blocked_upper}')
    print(f'TQQQ in blocked_symbols: {"TQQQ" in blocked_upper}')
    print(f'first 10 entries: {list(blocked)[:10]}')
    # Also check the auto-tuner overlay
    import json
    for path in ['data/auto-tuner-overrides.yaml', 'data/strategy-tuner-overrides.yaml']:
        if os.path.exists(path):
            with open(path) as f:
                text = f.read()
            print(f'\\n{path} contents ({len(text)} bytes):')
            print(text[:800])
            if 'blocked_symbols' in text:
                print(f'  *** {path} touches blocked_symbols — possible OVERRIDE ***')
            if 'SOXL' in text:
                print(f'  *** {path} mentions SOXL ***')
        else:
            print(f'\\n{path}: not present')
except Exception as e:
    import traceback
    traceback.print_exc()
PYEOF

echo
echo "=== did 'BLOCKED SYMBOL' guard fire today at all? (last 24h) ==="
docker logs trading-bot-trading-bot-1 --since 24h 2>&1 | grep -E "BLOCKED SYMBOL|BLOCKED.*on the exclusion list" | head -20
echo
echo "=== SAFETY GATE BLOCK lines today ==="
docker logs trading-bot-trading-bot-1 --since 24h 2>&1 | grep "SAFETY GATE BLOCK" | tail -10
echo
echo "=== Did the bot detect SOXL as crypto by mistake? ==="
docker logs trading-bot-trading-bot-1 --since 24h 2>&1 | grep "CRYPTO BLOCKED.*SOXL" | head -3
echo
echo "=== Other leveraged-ETF tickers in trade_history (ever)? ==="
python3 << 'PYEOF'
import json
with open('data/trade_history.json') as f:
    trades = json.load(f)
blocked_check = ['SOXL', 'TQQQ', 'SOXS', 'SQQQ', 'TZA', 'TNA', 'HIBS', 'HIBL', 'TSLL',
                 'NVDL', 'FNGU', 'FNGD', 'YANG', 'YINN', 'EDC', 'EDZ', 'DRN', 'DRV',
                 'NUGT', 'JNUG', 'ERX', 'ERY', 'GUSH', 'DRIP', 'BNKD', 'BNKU',
                 'TECS', 'TECL', 'LABU', 'LABD', 'FAS', 'FAZ', 'UVXY', 'UVIX', 'VIXY']
hits = {}
for t in trades:
    sym = (t.get('symbol') or '').upper()
    if sym in blocked_check:
        hits.setdefault(sym, []).append(t)
print(f'leveraged-ETF symbols in trade_history:')
for sym, ts in sorted(hits.items()):
    pnl = sum((t.get('pnl') or 0) for t in ts)
    last = max(t.get('exit_time') or t.get('entry_time') or '' for t in ts)
    print(f'  {sym:6s}  {len(ts):>2}t  ${pnl:+8.2f}  last={last[:10]}')
PYEOF
