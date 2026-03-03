#!/bin/bash
# ===========================================================
# Trading Bot — Linode/VPS One-Click Setup
# ===========================================================
#
# Provisions a Linode (or any Ubuntu 22.04 VPS) to run:
#   1. IB Gateway (headless IBKR connection)
#   2. Trading Bot (Python, auto-restart)
#   3. Dashboard (Flask, port 5000)
#
# Usage:
#   1. Create a Linode: Ubuntu 22.04, Dedicated 4GB ($36/mo) or Shared 4GB ($24/mo)
#   2. SSH in: ssh root@YOUR_IP
#   3. Run: curl -sSL https://raw.githubusercontent.com/YOUR_REPO/main/deploy/linode-setup.sh | bash
#      OR: git clone YOUR_REPO && cd tpstrategyv3 && bash deploy/linode-setup.sh
#   4. Edit .env with your API keys
#   5. Start: docker compose up -d
#
# Requirements: Ubuntu 22.04+ with at least 4GB RAM
# ===========================================================

set -euo pipefail

echo "============================================"
echo "  Trading Bot — Linode/VPS Setup"
echo "============================================"

# --- System updates ---
echo "[1/6] Updating system..."
apt-get update -qq && apt-get upgrade -y -qq

# --- Install Docker ---
echo "[2/6] Installing Docker..."
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    echo "Docker installed: $(docker --version)"
else
    echo "Docker already installed: $(docker --version)"
fi

# --- Install Docker Compose plugin ---
echo "[3/6] Installing Docker Compose..."
if ! docker compose version &> /dev/null; then
    apt-get install -y -qq docker-compose-plugin
fi
echo "Docker Compose: $(docker compose version)"

# --- Clone repo (if not already in it) ---
echo "[4/6] Setting up repository..."
REPO_DIR="/opt/trading-bot"
if [ -f "./docker-compose.yml" ]; then
    REPO_DIR="$(pwd)"
    echo "Using current directory: $REPO_DIR"
elif [ ! -d "$REPO_DIR" ]; then
    echo "Clone your repo to $REPO_DIR first, or run this script from the repo directory."
    echo "  git clone https://github.com/YOUR_USER/tpstrategyv3.git $REPO_DIR"
    REPO_DIR="$(pwd)"
fi

cd "$REPO_DIR"

# --- Create .env if needed ---
echo "[5/6] Checking environment..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "================================================"
    echo "  IMPORTANT: Edit .env with your API keys!"
    echo "  nano $REPO_DIR/.env"
    echo ""
    echo "  Required for IB Gateway (add these):"
    echo "    IB_USERNAME=your_ibkr_username"
    echo "    IB_PASSWORD=your_ibkr_password"
    echo "    IB_TRADING_MODE=paper"
    echo "    VNC_PASSWORD=your_vnc_password"
    echo "================================================"
    echo ""
fi

# --- Firewall setup ---
echo "[6/6] Configuring firewall..."
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

echo ""
echo "============================================"
echo "  Setup Complete!"
echo "============================================"
echo ""
echo "  Next steps:"
echo "  1. Edit your .env file:"
echo "     nano $REPO_DIR/.env"
echo ""
echo "  2. Add IB Gateway credentials to .env:"
echo "     IB_USERNAME=your_ibkr_username"
echo "     IB_PASSWORD=your_ibkr_password"
echo "     IB_TRADING_MODE=paper"
echo ""
echo "  3. Start everything:"
echo "     cd $REPO_DIR"
echo "     docker compose up -d"
echo ""
echo "  4. Check status:"
echo "     docker compose ps"
echo "     docker compose logs -f trading-bot"
echo ""
echo "  5. Access dashboard:"
echo "     http://YOUR_IP:5000"
echo ""
echo "  6. VNC into IB Gateway (for debugging):"
echo "     ssh -L 5900:localhost:5900 root@YOUR_IP"
echo "     Then connect VNC viewer to localhost:5900"
echo ""
echo "  Monthly cost: ~\$24-36/mo (Linode Shared/Dedicated 4GB)"
echo "============================================"
