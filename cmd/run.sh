#!/bin/bash
set -e
echo "=== pull main on host ==="
cd /opt/trading-bot
git fetch origin main
git checkout main 2>/dev/null || true
git pull --ff-only origin main
git log --oneline -3
echo ""
echo "=== run snapshot once with the fixed code ==="
scripts/claude-snapshot.sh && echo "snapshot OK"
echo ""
echo "=== confirm log-tail.log size in worktree post-run ==="
ls -la /opt/trading-bot-snapshot/review/log-tail.log 2>&1
