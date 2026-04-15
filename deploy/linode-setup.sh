#!/bin/bash
# ===========================================================
# Trading Bot — Linode/VPS One-Click Setup
# ===========================================================
#
# Provisions a Linode (or any Ubuntu 22.04 VPS) to run:
#   1. IB Gateway (headless IBKR connection)
#   2. Trading Bot (Python, auto-restart, IBKR-only execution)
#   3. Dashboard (Flask, port 5000)
#   4. Auto-deploy (checks GitHub every 5 min, pulls + restarts)
#
# Usage:
#   1. Create a Linode: Ubuntu 22.04, Dedicated 4GB ($36/mo)
#      Region: US-East (Newark) for lowest latency to NYSE
#   2. SSH in: ssh root@YOUR_IP
#   3. Run:
#      git clone https://github.com/femibol/tpstrategyv3.git /opt/trading-bot
#      cd /opt/trading-bot && bash deploy/linode-setup.sh
#   4. Edit .env with your IBKR credentials: nano /opt/trading-bot/.env
#   5. Start the bot:
#      docker compose up -d
#
# ===========================================================

set -euo pipefail

echo "============================================"
echo "  Trading Bot — Linode/VPS Setup"
echo "============================================"

# --- System updates ---
echo "[1/7] Updating system..."
apt-get update -qq && apt-get upgrade -y -qq

# --- Install Docker ---
echo "[2/7] Installing Docker..."
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    echo "Docker installed: $(docker --version)"
else
    echo "Docker already installed: $(docker --version)"
fi

# --- Install Docker Compose plugin ---
echo "[3/7] Installing Docker Compose..."
if ! docker compose version &> /dev/null; then
    apt-get install -y -qq docker-compose-plugin
fi
echo "Docker Compose: $(docker compose version)"

# --- Install git (should already be there) ---
echo "[4/7] Checking git..."
if ! command -v git &> /dev/null; then
    apt-get install -y -qq git
fi

# --- Setup repo directory ---
echo "[5/7] Setting up repository..."
REPO_DIR="/opt/trading-bot"
if [ -f "./docker-compose.yml" ]; then
    REPO_DIR="$(pwd)"
    echo "Using current directory: $REPO_DIR"
fi
cd "$REPO_DIR"

# --- Create .env if needed ---
echo "[6/7] Checking environment..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "================================================"
    echo "  IMPORTANT: Edit .env with your API keys!"
    echo "  nano $REPO_DIR/.env"
    echo ""
    echo "  REQUIRED (IBKR-only execution):"
    echo "    IB_USERNAME=your_ibkr_username"
    echo "    IB_PASSWORD=your_ibkr_password"
    echo "    IB_TRADING_MODE=paper"
    echo ""
    echo "  RECOMMENDED:"
    echo "    ANTHROPIC_API_KEY=your_key     # Claude pre-trade validation"
    echo "    DISCORD_WEBHOOK_URL=your_hook  # notifications"
    echo "================================================"
    echo ""
else
    echo ".env already exists — keeping current configuration"
fi

# --- Firewall setup ---
echo "[7/7] Configuring firewall..."
if command -v ufw &> /dev/null; then
    ufw allow 22/tcp    # SSH
    ufw allow 5000/tcp  # Dashboard
    ufw allow 8080/tcp  # Webhook receiver
    # Do NOT expose 4001/4002 (IBKR API) — internal only
    # Do NOT expose 5900 (VNC) — use SSH tunnel instead
    ufw --force enable
    echo "Firewall configured (SSH, Dashboard, Webhooks open)"
else
    echo "ufw not found — configure firewall manually"
fi

# --- Setup auto-deploy cron ---
echo ""
echo "Setting up auto-deploy (checks GitHub every 5 min)..."
CRON_CMD="*/5 * * * * $REPO_DIR/deploy/auto-deploy.sh >> /var/log/auto-deploy.log 2>&1"
# Add cron job only if not already present
if ! crontab -l 2>/dev/null | grep -q "auto-deploy.sh"; then
    (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
    echo "Auto-deploy cron job added (every 5 min)"
else
    echo "Auto-deploy cron job already exists"
fi

# --- Create log rotation for auto-deploy ---
cat > /etc/logrotate.d/auto-deploy << 'LOGROTATE'
/var/log/auto-deploy.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
}
LOGROTATE

# --- Install systemd unit so the stack starts on boot and stays up ---
echo ""
echo "Installing trading-bot systemd service..."
install -m 644 "$REPO_DIR/deploy/trading-bot.service" /etc/systemd/system/trading-bot.service
systemctl daemon-reload
systemctl enable trading-bot.service
echo "  systemctl status trading-bot    # check status"
echo "  systemctl restart trading-bot   # manual restart"
echo "  journalctl -u trading-bot -f    # follow logs"

# --- Watchdog cron (every 2 min): restarts stack if a container drops or
# the dashboard /health goes bad. Rate-limited to one action per 10 min.
echo ""
echo "Setting up watchdog (checks container + dashboard health every 2 min)..."
chmod +x "$REPO_DIR/deploy/watchdog.sh"
WATCHDOG_CRON="*/2 * * * * $REPO_DIR/deploy/watchdog.sh >> /var/log/trading-bot-watchdog.log 2>&1"
if ! crontab -l 2>/dev/null | grep -q "watchdog.sh"; then
    (crontab -l 2>/dev/null; echo "$WATCHDOG_CRON") | crontab -
    echo "Watchdog cron job added (every 2 min)"
else
    echo "Watchdog cron job already exists"
fi

# --- Log rotation for watchdog ---
cat > /etc/logrotate.d/trading-bot-watchdog << 'LOGROTATE'
/var/log/trading-bot-watchdog.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
}
LOGROTATE

echo ""
echo "============================================"
echo "  Setup Complete!"
echo "============================================"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Edit your .env file:"
echo "     nano $REPO_DIR/.env"
echo ""
echo "  2. Start the bot (systemd-managed — comes back on reboot):"
echo "     systemctl start trading-bot"
echo ""
echo "  3. Check status:"
echo "     systemctl status trading-bot"
echo "     docker compose ps"
echo "     docker compose logs -f trading-bot"
echo ""
echo "  4. Access dashboard:"
echo "     http://YOUR_IP:5000"
echo ""
echo "  5. Auto-deploy is ON:"
echo "     Push to GitHub -> Linode pulls + restarts in ~5 min"
echo "     Manual trigger: $REPO_DIR/deploy/auto-deploy.sh"
echo "     Logs: tail -f /var/log/auto-deploy.log"
echo ""
echo "  6. Watchdog is ON (every 2 min):"
echo "     Restarts stack if containers drop or dashboard /health fails."
echo "     Logs: tail -f /var/log/trading-bot-watchdog.log"
echo ""
echo "  7. VNC into IB Gateway (debugging):"
echo "     ssh -L 5900:localhost:5900 root@YOUR_IP"
echo "     Then connect VNC viewer to localhost:5900"
echo ""
echo "============================================"
