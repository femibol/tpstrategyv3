#!/usr/bin/env python3
"""
Quick launcher for the Algo Trading Bot.

Usage:
    python run.py                    # Start paper trading (default)
    python run.py live               # Start live trading (requires confirmation)
    python run.py backtest           # Backtest mean_reversion on default symbols
    python run.py backtest momentum  # Backtest specific strategy
"""
import sys
import os
import asyncio
import warnings

# Silence noisy deprecation warnings from third-party libs (yfinance, pandas)
warnings.filterwarnings("ignore", category=DeprecationWarning, module="yfinance")
warnings.filterwarnings("ignore", message=".*utcnow.*")

# Python 3.10+ no longer auto-creates an event loop in the main thread.
# ib_insync/eventkit requires one to exist at import time.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.main import main

if __name__ == "__main__":
    # Quick shortcuts
    if len(sys.argv) == 2:
        arg = sys.argv[1].lower()
        if arg == "live":
            sys.argv = [sys.argv[0], "--mode", "live"]
        elif arg == "paper":
            sys.argv = [sys.argv[0], "--mode", "paper"]
        elif arg == "backtest":
            sys.argv = [sys.argv[0], "--backtest"]
        elif arg == "dashboard":
            sys.argv = [sys.argv[0], "--dashboard"]
    elif len(sys.argv) == 3 and sys.argv[1].lower() == "backtest":
        sys.argv = [sys.argv[0], "--backtest", "--strategy", sys.argv[2]]

    main()
