docker logs --tail 60 trading-bot-trading-bot-1 2>&1 | grep -v 'GET /health' | grep -v HEARTBEAT
