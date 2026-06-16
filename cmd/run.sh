#!/bin/bash
set +e
cd /opt/trading-bot
echo "=== data/ on disk ==="
ls -la data/ 2>&1 | head -20
echo "=== trade_history.json (head) ==="
ls -la data/trade_history.json 2>&1
echo "first 200B:"
head -c 200 data/trade_history.json 2>&1
echo
echo "count entries:"
python3 -c "import json; print(len(json.load(open('data/trade_history.json'))))" 2>&1
echo "=== full container log for trade-history line ==="
docker logs trading-bot-trading-bot-1 2>&1 | grep -E "TRADE HISTORY|PERF STATS|persisted_trades|persist_trade|Load.*trade|trade_analyzer" | head -10
echo "=== docker volume / mounts ==="
docker inspect trading-bot-trading-bot-1 --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}' 2>&1
echo "=== in-container path matches host? ==="
docker exec trading-bot-trading-bot-1 sh -c 'ls -la /app/data/trade_history.json 2>&1; python3 -c "import json; print(\"in-container count:\", len(json.load(open(\"/app/data/trade_history.json\"))))" 2>&1'
