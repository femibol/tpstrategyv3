#!/usr/bin/env bash
# ============================================================
# AlgoBot - VM Setup Script
# Run this ON THE VM after cloning the repo
#
# Usage:
#   cd ~/tpstrategyv3
#   chmod +x deploy/gcp-setup.sh
#   ./deploy/gcp-setup.sh
# ============================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$REPO_DIR/venv"
USER_NAME="$(whoami)"
SERVICE_DIR="/etc/systemd/system"

echo "============================================"
echo "  AlgoBot - Server Setup"
echo "============================================"
echo "  Repo:    $REPO_DIR"
echo "  User:    $USER_NAME"
echo "  Python:  $(python3 --version 2>/dev/null || echo 'not installed')"
echo "============================================"
echo ""

# ---- 1. System packages ----
echo "[1/6] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3-pip \
    python3-venv \
    python3-dev \
    git \
    xvfb \
    curl \
    unzip \
    > /dev/null 2>&1
echo "  Done."

# ---- 2. Python venv ----
echo "[2/6] Setting up Python virtual environment..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q
pip install -r "$REPO_DIR/requirements.txt" -q
echo "  Done. Installed $(pip list 2>/dev/null | wc -l) packages."

# ---- 3. .env file ----
echo "[3/6] Setting up environment file..."
if [ ! -f "$REPO_DIR/.env" ]; then
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
    echo "  Created .env from template. YOU MUST EDIT IT:"
    echo "    nano $REPO_DIR/.env"
else
    echo "  .env already exists - skipping."
fi

# ---- 4. Data directory + swap ----
echo "[4/7] Creating data directory and swap..."
mkdir -p "$REPO_DIR/data"
mkdir -p "$REPO_DIR/logs"

# Add 1GB swap if not already present (e2-micro only has 1GB RAM)
if [ ! -f /swapfile ]; then
    echo "  Creating 1GB swap file (e2-micro has only 1GB RAM)..."
    sudo fallocate -l 1G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile > /dev/null
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab > /dev/null
    echo "  Swap enabled."
else
    echo "  Swap already exists - skipping."
fi
echo "  Done."

# ---- 5. Install systemd services ----
echo "[5/7] Installing systemd services..."

# Generate algobot.service with correct paths
sudo tee "$SERVICE_DIR/algobot.service" > /dev/null << EOF
[Unit]
Description=AlgoBot Trading System
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$REPO_DIR
ExecStart=$VENV_DIR/bin/python run.py paper
Restart=on-failure
RestartSec=30
StartLimitIntervalSec=300
StartLimitBurst=5
EnvironmentFile=$REPO_DIR/.env

# Give engine time to close positions gracefully on stop/restart
TimeoutStopSec=60
KillSignal=SIGTERM

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=algobot

# Safety
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$REPO_DIR

[Install]
WantedBy=multi-user.target
EOF

# Generate ibgateway.service
sudo tee "$SERVICE_DIR/ibgateway.service" > /dev/null << EOF
[Unit]
Description=Interactive Brokers Gateway (headless)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
ExecStart=/usr/bin/xvfb-run -a /opt/ibgateway/ibgateway
Restart=on-failure
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=5

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ibgateway

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable algobot
echo "  Installed: algobot.service, ibgateway.service"

# ---- 6. Shell aliases ----
echo "[6/7] Adding convenience aliases..."
if ! grep -q "bot-status" "$HOME/.bashrc" 2>/dev/null; then
    cat >> "$HOME/.bashrc" << 'ALIASES'

# --- AlgoBot shortcuts ---
alias bot-start='sudo systemctl start algobot'
alias bot-stop='sudo systemctl stop algobot'
alias bot-restart='sudo systemctl restart algobot'
alias bot-status='sudo systemctl status algobot'
alias bot-logs='journalctl -u algobot -f --no-pager -n 100'
alias bot-update='cd ~/tpstrategyv3 && git pull && source venv/bin/activate && pip install -r requirements.txt -q && sudo systemctl restart algobot'
ALIASES
    echo "  Added aliases: bot-start, bot-stop, bot-restart, bot-status, bot-logs, bot-update"
else
    echo "  Aliases already exist - skipping."
fi

# ---- 7. Done ----
echo ""
echo "[7/7] Setup complete!"
echo ""
echo "============================================"
echo "  NEXT STEPS"
echo "============================================"
echo ""
echo "  1. Edit your .env with API keys:"
echo "     nano $REPO_DIR/.env"
echo ""
echo "  2. (Optional) Install IB Gateway for IBKR trading:"
echo "     - Download from: https://www.interactivebrokers.com/en/trading/ibgateway-stable.php"
echo "     - Upload to VM:  gcloud compute scp ibgateway-stable-standalone-linux-x64.sh algobot:~/"
echo "     - Install:       chmod +x ~/ibgateway-stable-standalone-linux-x64.sh && sudo ~/ibgateway-stable-standalone-linux-x64.sh"
echo "     - Start:         sudo systemctl start ibgateway"
echo ""
echo "  3. Start the bot:"
echo "     sudo systemctl start algobot"
echo ""
echo "  4. Check status:"
echo "     sudo systemctl status algobot"
echo "     sudo journalctl -u algobot -f    # live logs"
echo ""
echo "  5. Dashboard:"
echo "     http://$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_VM_IP'):5000"
echo ""
echo "============================================"
echo "  USEFUL COMMANDS"
echo "============================================"
echo "  sudo systemctl start algobot     # Start bot"
echo "  sudo systemctl stop algobot      # Stop bot"
echo "  sudo systemctl restart algobot   # Restart bot"
echo "  sudo systemctl status algobot    # Check status"
echo "  sudo journalctl -u algobot -f    # Live logs"
echo "  sudo journalctl -u algobot --since '1 hour ago'  # Recent logs"
echo "============================================"
