docker logs trading-bot-trading-bot-1 --since 5m 2>&1 | grep -v 'GET /health' | grep -v HEARTBEAT | tail -25
