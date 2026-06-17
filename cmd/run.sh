#!/bin/bash
set +e
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a

# Hit /api/trades through tailnet (same path the phone uses) and check
# the exact response: headers, content-type, size, JSON validity.
HOST="https://trading-bot-vps.tail5db65d.ts.net"

echo "=== /api/trades — headers + size ==="
curl -s -m 15 -i -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/trades" > /tmp/r.full
head -20 /tmp/r.full
echo "..."
wc -c /tmp/r.full
echo "=== /api/trades body — JSON validity + first/last entry ==="
sed -n '/^\r$/,$p' /tmp/r.full | tail -n +2 > /tmp/r.body
python3 << 'PYEOF'
import json
with open('/tmp/r.body') as f:
    text = f.read()
print(f"body bytes: {len(text)}")
try:
    d = json.loads(text)
    print(f"parsed: list of {len(d)} items")
    print(f"first row sample (truncated):")
    first = d[0]
    for k, v in first.items():
        s = str(v)
        if len(s) > 80: s = s[:80] + "..."
        print(f"  {k}: {s}")
    # Check for NaN / Infinity / non-finite values which choke iOS Safari JSON.parse
    text_lower = text.lower()
    for token in ['nan', 'infinity', '-infinity', 'undefined']:
        c = text_lower.count(token)
        if c: print(f"  *** SUSPICIOUS token '{token}' appears {c} times — iOS Safari JSON.parse hates these")
    if 'null' in text_lower:
        print(f"  null tokens: {text_lower.count('null')} (these are valid JSON)")
except Exception as e:
    print(f"PARSE FAIL: {e}")
    print("first 400B:", text[:400])
PYEOF

echo "=== for comparison — /api/trades/summary headers + size ==="
curl -s -m 15 -i -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/trades/summary" > /tmp/r2.full
wc -c /tmp/r2.full
head -10 /tmp/r2.full | grep -iE "HTTP|content-type|content-length"
