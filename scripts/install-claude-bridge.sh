#!/bin/bash
# One-shot installer for the Claude<->VPS bridge.
#
# Sets up two crons that eliminate the copy-paste workflow Claude sessions
# have had to do all along (push trade_history.json, push log tails, paste
# command output back). After running this once on the VPS as root:
#
#   - Every 5 min: state snapshot pushed to origin/claude/live-state
#     (data/*.json + log tail + docker state)
#   - Every 1 min: any command Claude pushes to origin/claude/cmd is
#     executed (90s timeout) and the result pushed back as cmd/result.txt
#
# Run as root from anywhere:
#   sudo /opt/trading-bot/scripts/install-claude-bridge.sh
#
# Idempotent — safe to re-run; just bumps the cron entries.

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Must be run as root (the crons execute as root). Try: sudo $0" >&2
    exit 1
fi

REPO=/opt/trading-bot
SNAP_SCRIPT="$REPO/scripts/claude-snapshot.sh"
CMD_SCRIPT="$REPO/scripts/claude-cmd-runner.sh"

if [ ! -f "$SNAP_SCRIPT" ] || [ ! -f "$CMD_SCRIPT" ]; then
    echo "Bridge scripts missing — pull the latest main into $REPO first." >&2
    exit 1
fi

echo "==> Making scripts executable"
chmod +x "$SNAP_SCRIPT" "$CMD_SCRIPT"

echo "==> Ensuring state dir exists for the cmd-runner SHA marker"
mkdir -p /var/lib
touch /var/lib/claude-cmd-last-sha
chmod 600 /var/lib/claude-cmd-last-sha

echo "==> First-pass snapshot (so origin/claude/live-state exists immediately)"
"$SNAP_SCRIPT" || {
    echo "First snapshot failed — fix the underlying error before installing the cron." >&2
    exit 1
}

CRON_FILE=/etc/cron.d/claude-bridge
echo "==> Writing $CRON_FILE"
cat > "$CRON_FILE" <<EOF
# Claude<->VPS bridge — installed by $REPO/scripts/install-claude-bridge.sh
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# State snapshot to origin/claude/live-state — every 5 min
*/5 * * * * root $SNAP_SCRIPT >> /var/log/claude-snapshot.log 2>&1

# Command runner polling origin/claude/cmd — every 1 min
* * * * * root $CMD_SCRIPT >> /var/log/claude-cmd-runner.log 2>&1
EOF
chmod 644 "$CRON_FILE"

# Make sure cron picks up the new file immediately on systems that need it.
if command -v systemctl >/dev/null 2>&1; then
    systemctl reload cron 2>/dev/null || systemctl reload crond 2>/dev/null || true
fi

echo "==> Touching log files so logrotate / future tailing has them"
touch /var/log/claude-snapshot.log /var/log/claude-cmd-runner.log
chmod 644 /var/log/claude-snapshot.log /var/log/claude-cmd-runner.log

echo "==> Installing logrotate config (2026-07-09 disk-full incident fix)"
# copytruncate so the bot / crons keep their open file handles — no
# service restarts needed on rotation.
cat > /etc/logrotate.d/trading-bot <<'ROTEOF'
/opt/trading-bot/logs/*.log {
    daily
    rotate 5
    maxsize 200M
    compress
    missingok
    notifempty
    copytruncate
}
/var/log/claude-snapshot.log /var/log/claude-cmd-runner.log {
    weekly
    rotate 2
    maxsize 50M
    compress
    missingok
    notifempty
    copytruncate
}
ROTEOF
chmod 644 /etc/logrotate.d/trading-bot

echo ""
echo "✓ Installed."
echo ""
echo "Verify with:"
echo "  cat $CRON_FILE"
echo "  ls -la /opt/trading-bot-snapshot   # snapshot worktree"
echo "  git -C /opt/trading-bot-snapshot log --oneline -3"
echo "  git ls-remote origin claude/live-state   # confirm remote branch exists"
echo ""
echo "Next snapshot in <5 min; first command-runner tick in <1 min."
echo "Tail logs to confirm:"
echo "  tail -f /var/log/claude-snapshot.log /var/log/claude-cmd-runner.log"
