#!/bin/bash
echo "=== /opt/trading-bot git HEAD ==="
cd /opt/trading-bot && git rev-parse --short HEAD && git log --oneline -3
echo ""
echo "=== logs/ dir on host ==="
ls -la /opt/trading-bot/logs/ 2>&1 | head -10 || echo "missing"
echo ""
echo "=== Tail of the snapshot cron's own log ==="
tail -30 /var/log/claude-snapshot.log 2>&1 | tail -30
echo ""
echo "=== Does the new snapshot script include the diagnostic fallback? ==="
grep -A2 "no readable log file" /opt/trading-bot/scripts/claude-snapshot.sh | head -5 || echo "NEW CODE NOT PRESENT"
echo ""
echo "=== Run the snapshot script manually NOW and see what happens ==="
/opt/trading-bot/scripts/claude-snapshot.sh 2>&1 | tail -20
echo ""
echo "=== Snapshot worktree contents post-run ==="
ls -la /opt/trading-bot-snapshot/review/ 2>&1 || echo "review missing"
