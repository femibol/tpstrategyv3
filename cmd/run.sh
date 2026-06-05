docker logs trading-bot-trading-bot-1 --since 4m 2>&1 | grep -iE 'IBKR Error|order|reject|warning' | grep -v 'GET /health' | grep -v HEARTBEAT | tail -30
