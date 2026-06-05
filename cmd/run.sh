sleep 60 && echo '---POST-WARMUP ACTIVITY---' && docker logs trading-bot-trading-bot-1 --since 2m 2>&1 | grep -iE 'SIGNAL|APPROVED|BRACKET' | grep -v HEARTBEAT | tail -20
