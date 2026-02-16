"""
Main entry point for the Algo Trading Bot.

Usage:
    python -m bot.main              # Start the live/paper trading bot
    python -m bot.main --backtest   # Run backtest
    python -m bot.main --dashboard  # Start dashboard only
"""
import argparse
import sys
import threading

from bot.config import Config
from bot.engine import TradingEngine
from bot.backtest.engine import BacktestEngine
from bot.dashboard.app import Dashboard
from bot.utils.logger import setup_logger


def main():
    parser = argparse.ArgumentParser(description="Algo Trading Bot")
    parser.add_argument("--mode", choices=["paper", "live"], default=None,
                        help="Trading mode (overrides .env)")
    parser.add_argument("--backtest", action="store_true",
                        help="Run backtest instead of live trading")
    parser.add_argument("--strategy", default="mean_reversion",
                        help="Strategy to backtest")
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="Symbols to trade/backtest")
    parser.add_argument("--start", default=None,
                        help="Backtest start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=None,
                        help="Backtest end date (YYYY-MM-DD)")
    parser.add_argument("--capital", type=float, default=None,
                        help="Starting capital (overrides config)")
    parser.add_argument("--dashboard", action="store_true",
                        help="Start web dashboard")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Disable web dashboard")

    args = parser.parse_args()

    # Load config
    config = Config()
    if args.mode:
        config.mode = args.mode
        config.is_live = args.mode == "live"
        config.is_paper = args.mode == "paper"

    logger = setup_logger()

    # --- Backtest Mode ---
    if args.backtest:
        logger.info("Starting backtest mode...")
        bt = BacktestEngine(config)
        results = bt.run(
            strategy_name=args.strategy,
            symbols=args.symbols,
            start=args.start,
            end=args.end,
            starting_capital=args.capital,
        )
        bt.print_results(results)
        return

    # --- Live/Paper Trading Mode ---
    engine = TradingEngine(config)

    # Start dashboard in background thread
    if not args.no_dashboard:
        dashboard = Dashboard(engine, config)
        dash_thread = threading.Thread(target=dashboard.start, daemon=True)
        dash_thread.start()
        logger.info(f"Dashboard: http://localhost:{config.dashboard_port}")

    # Safety check for live mode
    if config.is_live:
        print("\n" + "=" * 60)
        print("  WARNING: LIVE TRADING MODE")
        print("  This will trade with REAL MONEY!")
        print("  Make sure you have tested with paper trading first.")
        print("=" * 60)
        confirm = input("\nType 'CONFIRM' to proceed with live trading: ")
        if confirm != "CONFIRM":
            print("Aborting. Use --mode paper for paper trading.")
            return

    # Start the engine
    engine.start()


if __name__ == "__main__":
    main()
