docker logs trading-bot-trading-bot-1 --since 24h 2>&1 | grep -E 'consecutive losses|SSG' | head -30
