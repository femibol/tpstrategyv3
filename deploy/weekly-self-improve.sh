#!/bin/bash
# ===========================================================
# Weekly Self-Improvement — runs Claude Code on trade history
# ===========================================================
#
# Runs every Sunday night at 10 PM ET to analyze the week's trades
# and propose improvements. Claude reviews:
#   - data/trade_history.json (last 500 trades)
#   - data/learning_adjustments.json (current learned params)
#   - logs/trading.log (recent bot behavior)
#
# Outputs:
#   - /opt/trading-bot/data/weekly-reviews/YYYY-MM-DD.md (review)
#   - Optionally: commits parameter changes to config/settings.yaml
#
# Install in cron:
#   crontab -e
#   0 22 * * 0 /opt/trading-bot/deploy/weekly-self-improve.sh > /var/log/bot-self-improve.log 2>&1
#
# ===========================================================

set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/trading-bot}"
REVIEW_DIR="$REPO_DIR/data/weekly-reviews"
DATE=$(date +%Y-%m-%d)
REVIEW_FILE="$REVIEW_DIR/$DATE.md"

mkdir -p "$REVIEW_DIR"

cd "$REPO_DIR"

# Ensure Claude Code has API key
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    if [ -f ".env" ]; then
        export ANTHROPIC_API_KEY=$(grep -E "^ANTHROPIC_API_KEY=" .env | cut -d= -f2-)
    fi
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: ANTHROPIC_API_KEY not set"
    exit 1
fi

echo "============================================"
echo "  Weekly Self-Improvement: $DATE"
echo "============================================"

# Check that required files exist
if [ ! -f "data/trade_history.json" ]; then
    echo "No trade history yet — skipping review"
    exit 0
fi

TRADE_COUNT=$(python3 -c "import json; print(len(json.load(open('data/trade_history.json'))))" 2>/dev/null || echo "0")
if [ "$TRADE_COUNT" -lt 5 ]; then
    echo "Only $TRADE_COUNT trades this week — need more data for meaningful review"
    exit 0
fi

# Run Claude Code in non-interactive mode with a self-improvement prompt
# The -p flag runs a single prompt and exits (batch mode)
echo "Running Claude weekly review..."

claude -p "$(cat << 'PROMPT_EOF'
You are reviewing a week of trading bot performance on the Linode server.

Please do the following:

1. Read data/trade_history.json and analyze the last 50 trades
2. Read data/learning_adjustments.json to see current learned parameters
3. Identify the 3 most important patterns:
   - What's working (winning trades have in common)
   - What's failing (losing trades have in common)
   - What parameters should change

4. Write a markdown review to data/weekly-reviews/$(date +%Y-%m-%d).md with:
   - Summary stats (win rate, avg win, avg loss, R:R, total P&L)
   - Top 3 winning patterns to boost
   - Top 3 losing patterns to avoid
   - Specific config/settings.yaml parameter changes to make
   - Any new strategy logic that would help

5. If parameter changes are safe (adjusting stops, targets, strategy weights
   within reasonable bounds), update config/settings.yaml and commit the changes.
   DO NOT make structural code changes — only tuning parameter values.

6. Commit any changes with a clear message like:
   "Weekly self-improvement: tighten momentum_runner stop to 2%"

Be concise and data-driven. Only suggest changes you can justify with the data.
PROMPT_EOF
)" 2>&1 | tee -a "$REVIEW_FILE"

echo ""
echo "Review saved to $REVIEW_FILE"

# Push any commits Claude made
if [ -n "$(git log origin/main..HEAD --oneline 2>/dev/null)" ]; then
    echo "Claude made commits — pushing to origin/main..."
    git push origin main
    echo "Weekly improvements pushed. Auto-deploy cron will apply them in ~5 min."
fi

echo "============================================"
echo "  Weekly review complete"
echo "============================================"
