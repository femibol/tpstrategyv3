docker logs trading-bot-trading-bot-1 --since 30m 2>&1 | grep -iE 'SOFI|SIGNAL:|APPROVED|BRACKET (FILLED|ORDER placed|PARENT|NOT FILLED)|QUALITY GATE SKIP' | grep -v HEARTBEAT | tail -40
