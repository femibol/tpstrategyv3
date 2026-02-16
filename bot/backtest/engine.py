"""
Backtesting Engine
- Test strategies on historical data before risking real money
- Simulates the full trading loop with realistic execution
- Tracks performance metrics (Sharpe, win rate, max drawdown)
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from bot.config import Config
from bot.data.indicators import TechnicalIndicators
from bot.risk.position_sizer import PositionSizer
from bot.strategies.mean_reversion import MeanReversionStrategy
from bot.strategies.momentum import MomentumStrategy
from bot.strategies.vwap import VWAPScalpStrategy
from bot.utils.logger import get_logger

log = get_logger("backtest")

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False


class BacktestMarketData:
    """Simulated market data feed for backtesting."""

    def __init__(self):
        self._bars = {}
        self._current_idx = {}
        self._all_data = {}

    def load(self, symbol, data):
        """Load historical data for a symbol."""
        self._all_data[symbol] = data
        self._current_idx[symbol] = 0

    def advance(self, symbol):
        """Advance to next bar."""
        if symbol in self._current_idx:
            self._current_idx[symbol] += 1

    def get_bars(self, symbol, periods=None):
        """Get bars up to current index."""
        if symbol not in self._all_data:
            return None
        idx = self._current_idx.get(symbol, 0)
        data = self._all_data[symbol].iloc[:idx + 1]
        if periods and len(data) > periods:
            return data.iloc[-periods:]
        return data

    def get_price(self, symbol):
        """Get current price."""
        bars = self.get_bars(symbol, 1)
        if bars is not None and len(bars) > 0:
            return float(bars["close"].iloc[-1])
        return None

    def get_volume(self, symbol):
        """Get current volume."""
        bars = self.get_bars(symbol, 1)
        if bars is not None and len(bars) > 0:
            return float(bars["volume"].iloc[-1])
        return None

    def update(self, symbols):
        """No-op for backtesting."""
        pass


class BacktestEngine:
    """
    Backtesting engine for strategy validation.

    Usage:
        engine = BacktestEngine()
        results = engine.run(
            strategy="mean_reversion",
            symbols=["SPY", "QQQ"],
            start="2024-01-01",
            end="2024-12-31"
        )
        engine.print_results(results)
    """

    def __init__(self, config=None):
        self.config = config or Config()
        self.indicators = TechnicalIndicators()
        self.position_sizer = PositionSizer(self.config)

    def run(self, strategy_name, symbols=None, start=None, end=None,
            starting_capital=None):
        """
        Run a backtest.

        Args:
            strategy_name: "mean_reversion", "momentum", "vwap_scalp"
            symbols: List of symbols to test
            start: Start date string "YYYY-MM-DD"
            end: End date string "YYYY-MM-DD"
            starting_capital: Override starting capital

        Returns:
            dict with performance metrics
        """
        capital = starting_capital or self.config.starting_balance
        strat_config = self.config.get_strategy_config(strategy_name)

        if symbols:
            strat_config["symbols"] = symbols
        symbols = strat_config.get("symbols", ["SPY"])

        # Create strategy
        strategy_map = {
            "mean_reversion": MeanReversionStrategy,
            "momentum": MomentumStrategy,
            "vwap_scalp": VWAPScalpStrategy,
        }

        if strategy_name not in strategy_map:
            log.error(f"Unknown strategy: {strategy_name}")
            return None

        strategy = strategy_map[strategy_name](
            config=strat_config,
            indicators=self.indicators,
            capital=capital
        )

        # Fetch historical data
        log.info(f"Fetching historical data for {symbols}...")
        market_data = BacktestMarketData()

        if not HAS_YF:
            log.error("yfinance required for backtesting. pip install yfinance")
            return None

        interval_map = {
            "1m": "1m", "5m": "5m", "15m": "15m",
            "30m": "30m", "1h": "1h", "1d": "1d"
        }
        interval = interval_map.get(strat_config.get("timeframe", "5m"), "5m")

        for symbol in symbols:
            try:
                ticker = yf.Ticker(symbol)
                if interval in ("1m",):
                    # 1m data limited to 7 days
                    df = ticker.history(period="5d", interval=interval)
                elif interval in ("5m", "15m", "30m"):
                    if start and end:
                        df = ticker.history(start=start, end=end, interval=interval)
                    else:
                        df = ticker.history(period="59d", interval=interval)
                else:
                    if start and end:
                        df = ticker.history(start=start, end=end, interval=interval)
                    else:
                        df = ticker.history(period="1y", interval=interval)

                if df.empty:
                    log.warning(f"No data for {symbol}")
                    continue

                df.columns = [c.lower() for c in df.columns]
                if "adj close" in df.columns:
                    df = df.drop(columns=["adj close"])

                market_data.load(symbol, df)
                log.info(f"Loaded {len(df)} bars for {symbol}")

            except Exception as e:
                log.error(f"Failed to load {symbol}: {e}")

        # Run simulation
        log.info(f"Running backtest: {strategy_name} | Capital: ${capital:,.2f}")

        balance = capital
        peak_balance = capital
        positions = {}
        trades = []
        equity_curve = []

        # Get total bars (use first symbol as reference)
        ref_symbol = symbols[0]
        total_bars = len(market_data._all_data.get(ref_symbol, []))

        if total_bars == 0:
            log.error("No data loaded for backtesting")
            return None

        # Warmup period
        warmup = max(60, strat_config.get("lookback_period", 20) + 10)

        for bar_idx in range(warmup, total_bars):
            # Advance all symbols to current bar
            for symbol in symbols:
                market_data._current_idx[symbol] = bar_idx

            # Monitor positions
            closed = []
            for symbol, pos in list(positions.items()):
                price = market_data.get_price(symbol)
                if price is None:
                    continue

                # Check stop loss
                if pos["direction"] == "long" and price <= pos["stop_loss"]:
                    pnl = (price - pos["entry_price"]) * pos["quantity"]
                    balance += pnl
                    trades.append({**pos, "exit_price": price, "pnl": pnl, "reason": "stop"})
                    closed.append(symbol)
                    continue

                # Check take profit
                if pos.get("take_profit") and pos["direction"] == "long" and price >= pos["take_profit"]:
                    pnl = (price - pos["entry_price"]) * pos["quantity"]
                    balance += pnl
                    trades.append({**pos, "exit_price": price, "pnl": pnl, "reason": "target"})
                    closed.append(symbol)
                    continue

                # Max hold
                if bar_idx - pos.get("entry_bar", 0) > pos.get("max_hold_bars", 40):
                    pnl = (price - pos["entry_price"]) * pos["quantity"]
                    balance += pnl
                    trades.append({**pos, "exit_price": price, "pnl": pnl, "reason": "time"})
                    closed.append(symbol)

            for s in closed:
                del positions[s]

            # Generate signals
            try:
                signals = strategy.generate_signals(market_data)
            except Exception:
                signals = []

            # Execute signals
            for sig in signals:
                symbol = sig["symbol"]
                if sig["action"] == "buy" and symbol not in positions and len(positions) < 5:
                    price = market_data.get_price(symbol)
                    if price is None or price <= 0:
                        continue

                    stop = sig.get("stop_loss", price * 0.97)
                    qty = self.position_sizer.calculate(
                        balance=balance, price=price, stop_loss=stop
                    )
                    if qty <= 0:
                        continue

                    cost = qty * price
                    if cost > balance * 0.8:  # Keep 20% reserve
                        continue

                    positions[symbol] = {
                        "symbol": symbol,
                        "direction": "long",
                        "quantity": qty,
                        "entry_price": price,
                        "stop_loss": stop,
                        "take_profit": sig.get("take_profit"),
                        "strategy": strategy_name,
                        "entry_bar": bar_idx,
                        "max_hold_bars": sig.get("max_hold_bars", 40),
                    }

            # Track equity
            unrealized = sum(
                (market_data.get_price(s) or p["entry_price"]) * p["quantity"] - p["entry_price"] * p["quantity"]
                for s, p in positions.items()
            )
            total_equity = balance + unrealized
            peak_balance = max(peak_balance, total_equity)

            equity_curve.append(total_equity)

        # Close remaining positions at last price
        for symbol, pos in list(positions.items()):
            price = market_data.get_price(symbol) or pos["entry_price"]
            pnl = (price - pos["entry_price"]) * pos["quantity"]
            balance += pnl
            trades.append({**pos, "exit_price": price, "pnl": pnl, "reason": "end"})

        # Calculate metrics
        results = self._calculate_metrics(
            trades, equity_curve, capital, balance, peak_balance
        )
        results["strategy"] = strategy_name
        results["symbols"] = symbols
        results["total_bars"] = total_bars

        return results

    def _calculate_metrics(self, trades, equity_curve, starting, ending, peak):
        """Calculate performance metrics."""
        if not trades:
            return {
                "total_trades": 0,
                "total_return": 0,
                "sharpe_ratio": 0,
                "max_drawdown": 0,
                "win_rate": 0,
            }

        pnls = [t["pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        total_return = (ending - starting) / starting * 100
        win_rate = len(wins) / len(trades) * 100 if trades else 0
        avg_win = np.mean(wins) if wins else 0
        avg_loss = abs(np.mean(losses)) if losses else 0
        profit_factor = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")

        # Max drawdown
        equity = np.array(equity_curve) if equity_curve else np.array([starting])
        running_max = np.maximum.accumulate(equity)
        drawdowns = (running_max - equity) / running_max * 100
        max_drawdown = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0

        # Sharpe ratio (simplified)
        if len(pnls) > 1:
            returns = np.array(pnls) / starting
            sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252) if np.std(returns) > 0 else 0
        else:
            sharpe = 0

        return {
            "starting_capital": starting,
            "ending_capital": ending,
            "total_return_pct": round(total_return, 2),
            "total_return_dollars": round(ending - starting, 2),
            "total_trades": len(trades),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": round(win_rate, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "sharpe_ratio": round(sharpe, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "best_trade": round(max(pnls), 2) if pnls else 0,
            "worst_trade": round(min(pnls), 2) if pnls else 0,
            "trades": trades,
        }

    def print_results(self, results):
        """Print formatted backtest results."""
        if not results:
            print("No results to display")
            return

        print("\n" + "=" * 60)
        print(f"  BACKTEST RESULTS: {results.get('strategy', 'Unknown')}")
        print("=" * 60)
        print(f"  Symbols:          {', '.join(results.get('symbols', []))}")
        print(f"  Starting Capital: ${results['starting_capital']:,.2f}")
        print(f"  Ending Capital:   ${results['ending_capital']:,.2f}")
        print(f"  Total Return:     {results['total_return_pct']:+.2f}% "
              f"(${results['total_return_dollars']:+,.2f})")
        print("-" * 60)
        print(f"  Total Trades:     {results['total_trades']}")
        print(f"  Win Rate:         {results['win_rate']:.1f}%")
        print(f"  Avg Win:          ${results['avg_win']:,.2f}")
        print(f"  Avg Loss:         ${results['avg_loss']:,.2f}")
        print(f"  Profit Factor:    {results['profit_factor']:.2f}")
        print(f"  Sharpe Ratio:     {results['sharpe_ratio']:.2f}")
        print(f"  Max Drawdown:     {results['max_drawdown_pct']:.2f}%")
        print(f"  Best Trade:       ${results['best_trade']:+,.2f}")
        print(f"  Worst Trade:      ${results['worst_trade']:+,.2f}")
        print("=" * 60 + "\n")
