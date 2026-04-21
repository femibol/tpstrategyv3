#!/usr/bin/env bash
# Install weekly disk-cleanup cron so the VPS doesn't run out of space again.
#
# Usage (run once on the VPS, as root):
#   bash /opt/trading-bot/scripts/install-maintenance-cron.sh
#
# What it does:
#   - 03:00 every Sunday: docker system prune -af --volumes (keeps declared volumes)
#   - 03:05 every Sunday: docker builder prune -af (build cache)
#   - Logs output to /var/log/trading-bot-maintenance.log
set -euo pipefail

CRON_FILE="/etc/cron.d/trading-bot-maintenance"
LOG_FILE="/var/log/trading-bot-maintenance.log"

cat > "$CRON_FILE" <<'EOF'
# Trading-bot weekly maintenance — keeps Docker from filling the disk.
# Runs as root, Sunday 03:00 ET (server TZ). Prune order matters:
# system prune first (removes stopped containers + dangling images),
# then builder prune (removes the build cache layers that --no-cache
# rebuilds leave behind).
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
0 3 * * 0 root docker system prune -af >> /var/log/trading-bot-maintenance.log 2>&1
5 3 * * 0 root docker builder prune -af >> /var/log/trading-bot-maintenance.log 2>&1
EOF

touch "$LOG_FILE"
chmod 644 "$CRON_FILE"
chmod 644 "$LOG_FILE"

echo "Installed cron at $CRON_FILE"
echo "Will log to $LOG_FILE"
echo ""
echo "Verify with:"
echo "  cat $CRON_FILE"
echo "  systemctl status cron"
