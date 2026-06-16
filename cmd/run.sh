#!/bin/bash
# Pull the new safe deploy script + run it. This deploy will pick up
# everything currently on main (Wave 1-5 already deployed earlier
# today; this brings the tab consolidation + the deploy script itself).
set +e
cd /opt/trading-bot
# We need scripts/deploy-vps.sh to exist locally before we can invoke it.
# Easiest: fetch + extract from origin/main, then exec it.
git fetch origin main --quiet 2>&1 | tail -3
git show origin/main:scripts/deploy-vps.sh > scripts/deploy-vps.sh
chmod +x scripts/deploy-vps.sh
echo "=== using safe deploy script ==="
scripts/deploy-vps.sh
echo "=== exit: $? ==="
echo "=== quick post-deploy verification ==="
docker logs trading-bot-trading-bot-1 --since 60s 2>&1 | grep -E "TRADE HISTORY|PERF STATS" | tail -3
ls -la data/trade_history.json 2>&1 | head -1
