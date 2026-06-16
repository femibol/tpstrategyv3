#!/bin/bash
set +e
# Source the env so DASHBOARD_SECRET_KEY is set without any escaping games.
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
echo "key_len: ${#DASHBOARD_SECRET_KEY}"

AUTH=$(printf 'admin:%s' "$DASHBOARD_SECRET_KEY" | base64 -w0)

dump_status() {
  curl -sS -m 12 -H "Authorization: Basic $AUTH" "http://localhost:5000/api/status" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
except Exception as e:
    print("PARSE FAIL:", e); sys.exit(0)
keys=["running","balance","starting_balance","total_return_pct","daily_pnl","daily_trades","peak_balance","drawdown_pct","positions","strategies_active","total_trades","broker_connected","execution_broker"]
for k in keys: print(f"{k}: {d.get(k)}")
pd=d.get("position_details") or {}
print("position_details count:", len(pd))
print("position_details symbols:", list(pd.keys())[:10])
'
}

dump_positions() {
  curl -sS -m 12 -H "Authorization: Basic $AUTH" "http://localhost:5000/api/positions" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception as e: print("PARSE FAIL:",e); sys.exit(0)
print("count:", len(d))
for p in d[:5]: print(" -", p.get("symbol"), p.get("strategy"), "qty=", p.get("quantity"))
'
}

dump_daily() {
  curl -sS -m 12 -H "Authorization: Basic $AUTH" "http://localhost:5000/api/daily" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception as e: print("PARSE FAIL:",e); sys.exit(0)
print("days returned:", len(d))
for x in d[-5:]: print(" -", x)
'
}

echo "=== /api/status ==="; dump_status
echo "=== /api/positions ==="; dump_positions
echo "=== /api/daily ==="; dump_daily
echo "=== /health (sanity) ==="; curl -sS -m 5 http://localhost:5000/health
