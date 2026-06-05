docker logs trading-bot-trading-bot-1 --since 8m 2>&1 | grep -E 'SIGNAL|APPROVED|FILLED|MARKET|Executing|Order update|Cancelled' | grep -v HEARTBEAT | tail -30
