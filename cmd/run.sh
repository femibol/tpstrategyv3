cd /opt/trading-bot && git fetch origin main && git reset --hard origin/main 2>&1 | tail -2 && echo '---HEAD---' && git log --oneline -2 && docker compose restart trading-bot 2>&1 | tail -3
