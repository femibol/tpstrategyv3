#!/bin/bash
set +e
cd /opt/trading-bot
echo "=== ALOT log timeline — when was it seen, what was the verdict? ==="
grep "\\bALOT\\b" logs/trading.log 2>/dev/null | head -40
echo "..."
echo "(showing last 20 hits if more)"
grep "\\bALOT\\b" logs/trading.log 2>/dev/null | tail -20
echo
echo "=== verdicts/skip reasons mentioning ALOT ==="
grep -B1 -A1 "\\bALOT\\b" logs/trading.log 2>/dev/null | grep -iE "WAIT|SKIP|BLOCK|REJECT|verdict|score|rvol|float" | head -30
echo
echo "=== scanner result lists today — was ALOT in the discovered set? ==="
grep -E "scanner returned|DYNAMIC|TOP_PERC_GAIN|discovered.*symbols" logs/trading.log 2>/dev/null | grep -i ALOT | head -10
echo
echo "=== what time did ALOT FIRST appear in the log today? ==="
grep "\\bALOT\\b" logs/trading.log 2>/dev/null | head -1
echo "=== first scanner result from today, for reference ==="
grep -E "scanner|TOP_PERC_GAIN" logs/trading.log 2>/dev/null | head -1
