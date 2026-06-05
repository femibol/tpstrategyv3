docker logs trading-bot-ib-gateway-1 --since 5m 2>&1 | grep -iE 'error|reject|warning|denied|forbidden|order' | tail -30
