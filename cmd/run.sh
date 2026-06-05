sleep 25 && docker ps --filter name=trading-bot --format '{{.Names}}\t{{.Status}}' && echo '---LOG---' && docker logs --tail 15 trading-bot-trading-bot-1 2>&1 | tail -15
