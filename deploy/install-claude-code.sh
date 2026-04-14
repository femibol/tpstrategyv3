#!/bin/bash
# ===========================================================
# Install Claude Code on Linode — enables autonomous code improvement
# ===========================================================
#
# After running this, you can:
#   1. SSH into Linode and run `claude` to open an interactive Claude Code session
#   2. Run scheduled self-improvement via cron (see weekly-self-improve.sh)
#   3. Ask Claude to analyze trades, improve code, commit, push — all on the server
#
# Prerequisites:
#   - Ubuntu 22.04+ Linode (we have this)
#   - ANTHROPIC_API_KEY set (in .env, we have this)
#
# Usage:
#   ssh root@50.116.54.226
#   cd /opt/trading-bot
#   bash deploy/install-claude-code.sh
#
# ===========================================================

set -euo pipefail

echo "============================================"
echo "  Installing Claude Code on Linode"
echo "============================================"

# --- Install Node.js (required for Claude Code CLI) ---
echo "[1/4] Installing Node.js 20 LTS..."
if ! command -v node &> /dev/null || [ "$(node -v | cut -c2- | cut -d. -f1)" -lt 20 ]; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
    echo "Node version: $(node -v)"
    echo "npm version: $(npm -v)"
else
    echo "Node.js already installed: $(node -v)"
fi

# --- Install Claude Code globally ---
echo "[2/4] Installing Claude Code..."
if ! command -v claude &> /dev/null; then
    npm install -g @anthropic-ai/claude-code
    echo "Claude Code installed: $(claude --version 2>/dev/null || echo 'installed')"
else
    echo "Claude Code already installed, updating..."
    npm update -g @anthropic-ai/claude-code
fi

# --- Configure API key from .env ---
echo "[3/4] Configuring API key..."
REPO_DIR="${REPO_DIR:-/opt/trading-bot}"
if [ -f "$REPO_DIR/.env" ]; then
    # Read ANTHROPIC_API_KEY from .env
    ANTHROPIC_KEY=$(grep -E "^ANTHROPIC_API_KEY=" "$REPO_DIR/.env" | cut -d= -f2-)
    if [ -n "$ANTHROPIC_KEY" ]; then
        # Add to root's .bashrc so every SSH session has it
        if ! grep -q "ANTHROPIC_API_KEY" /root/.bashrc 2>/dev/null; then
            echo "export ANTHROPIC_API_KEY='$ANTHROPIC_KEY'" >> /root/.bashrc
            echo "API key added to /root/.bashrc"
        fi
        export ANTHROPIC_API_KEY="$ANTHROPIC_KEY"
    else
        echo "WARNING: ANTHROPIC_API_KEY not set in .env — set it first"
    fi
else
    echo "WARNING: $REPO_DIR/.env not found"
fi

# --- Create quick-access alias for trading bot dir ---
echo "[4/4] Creating shortcuts..."
if ! grep -q "alias cb=" /root/.bashrc 2>/dev/null; then
    cat >> /root/.bashrc << 'BASHRC_EOF'

# Trading bot shortcuts
alias cb='cd /opt/trading-bot'
alias bot-logs='cd /opt/trading-bot && docker compose logs -f trading-bot'
alias bot-status='cd /opt/trading-bot && docker compose ps'
alias bot-restart='cd /opt/trading-bot && docker compose restart trading-bot'
alias claude-bot='cd /opt/trading-bot && claude'
BASHRC_EOF
    echo "Added shortcuts: cb, bot-logs, bot-status, bot-restart, claude-bot"
fi

echo ""
echo "============================================"
echo "  Claude Code installed successfully!"
echo "============================================"
echo ""
echo "  How to use:"
echo ""
echo "  1. Source the bashrc (or log out and back in):"
echo "     source /root/.bashrc"
echo ""
echo "  2. Start Claude Code interactive session in your bot dir:"
echo "     claude-bot"
echo "     (or: cd /opt/trading-bot && claude)"
echo ""
echo "  3. From your phone (via Termius SSH), you can now chat with Claude"
echo "     directly on the server — it has access to all your code, logs,"
echo "     trade history, and can make changes and commit them."
echo ""
echo "  Example prompts for Claude on Linode:"
echo "     - 'Review today's trades and suggest parameter adjustments'"
echo "     - 'What pattern do my losing trades have in common?'"
echo "     - 'Commit fixes you recommend'"
echo "     - 'Analyze data/trade_history.json and improve my stop logic'"
echo ""
echo "============================================"
