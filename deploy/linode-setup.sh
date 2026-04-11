#!/bin/bash
# ===========================================================
# Trading Bot — Linode/VPS One-Click Setup
# ===========================================================
#
# Provisions a Linode (or any Ubuntu 22.04 VPS) to run:
#   1. Trading Bot (Python, auto-restart, Alpaca + Polygon)
#   2. IB Gateway (optional — adds real-time IBKR tick data)
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
#   4. Edit .env with your API keys: nano /opt/trading-bot/.env
#   5. Start the bot:
#      docker compose up -d              # Bot only (Alpaca + Polygon)
#      docker compose --profile ibkr up -d  # Bot + IB Gateway
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
    echo "  REQUIRED (minimum to run):"
    echo "    ALPACA_API_KEY=your_key"
    echo "    ALPACA_SECRET_KEY=your_secret"
    echo "    POLYGON_API_KEY=your_key"
    echo ""
    echo "  RECOMMENDED (notifications):"
    echo "    DISCORD_WEBHOOK_URL=your_webhook"
    echo ""
    echo "  OPTIONAL (for IBKR real-time data):"
    echo "    IB_USERNAME=your_ibkr_username"
    echo "    IB_PASSWORD=your_ibkr_password"
    echo "    IB_TRADING_MODE=paper"
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
echo "  2. Start the bot (Alpaca + Polygon, no IBKR):"
echo "     cd $REPO_DIR"
echo "     docker compose up -d"
echo ""
echo "  3. OR start with IBKR real-time data:"
echo "     docker compose --profile ibkr up -d"
echo ""
echo "  4. Check status:"
echo "     docker compose ps"
echo "     docker compose logs -f trading-bot"
echo ""
echo "  5. Access dashboard:"
echo "     http://YOUR_IP:5000"
echo ""
echo "  6. Auto-deploy is ON:"
echo "     Push to GitHub -> Linode pulls + restarts in ~5 min"
echo "     Manual trigger: $REPO_DIR/deploy/auto-deploy.sh"
echo "     Logs: tail -f /var/log/auto-deploy.log"
echo ""
echo "  7. VNC into IB Gateway (if using IBKR):"
echo "     ssh -L 5900:localhost:5900 root@YOUR_IP"
echo "     Then connect VNC viewer to localhost:5900"
echo ""
echo "============================================"
