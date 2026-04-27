#!/bin/bash
# Installs Python deps so Claude Code on the web can run the bot, lint, and import
# anthropic / pandas / etc. when working with this repo. Local terminal sessions
# already have a working venv and skip this.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

echo "[session-start] installing Python dependencies from requirements.txt..."
python3 -m pip install --quiet -r requirements.txt

echo "[session-start] dependencies ready."
