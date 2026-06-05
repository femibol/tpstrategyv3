docker logs trading-bot-trading-bot-1 --since 60m 2>&1 | grep -E 'JLHL|SMCZ|SOFI|RMSG' | grep -v HEARTBEAT | tail -30
