"""
Core Trading Engine - The brain of the operation.
Runs the main event loop, coordinates strategies, risk, and execution.
Fully automated, no-touch operation.
"""
import os
import time
import threading
import signal
import sys
from datetime import datetime, timedelta
from collections import defaultdict

import pytz
from apscheduler.schedulers.background import BackgroundScheduler

from bot.config import Config
from bot.risk.manager import RiskManager
from bot.risk.position_sizer import PositionSizer
from bot.data.market_data import MarketDataFeed
from bot.data.indicators import TechnicalIndicators
from bot.brokers.ibkr import IBKRBroker
from bot.brokers.traderspost import TradersPostBroker
from bot.signals.tradingview import TradingViewReceiver
from bot.strategies.mean_reversion import MeanReversionStrategy
from bot.strategies.momentum import MomentumStrategy
from bot.strategies.vwap import VWAPScalpStrategy
from bot.strategies.pairs_trading import PairsTradingStrategy
from bot.strategies.smc_forever import SMCForeverStrategy
from bot.utils.logger import setup_logger, get_logger
from bot.utils.notifications import Notifier

log = get_logger("engine")


class TradingEngine:
    """
    Main trading engine - coordinates everything.

    Flow:
    1. Connect to broker (IBKR)
    2. Load strategies
    3. Start market data feeds
    4. Run strategy loop every bar
    5. Execute signals through risk manager
    6. Monitor positions and manage exits
    7. Send notifications on trades
    8. Daily summary at close
    """

    def __init__(self, config=None):
        self.config = config or Config()
        self.logger = setup_logger()
        self.running = False
        self.paused = False

        # Core components
        self.broker = None
        self.risk_manager = None
        self.position_sizer = None
        self.notifier = None
        self.market_data = None
        self.indicators = TechnicalIndicators()
        self.tv_receiver = None
        self.tp_broker = None
        self.scheduler = None

        # State
        self.positions = {}
        self.orders = {}
        self.strategies = {}
        self.daily_trades = []
        self.daily_pnl = 0.0
        self.peak_balance = self.config.starting_balance
        self.current_balance = self.config.starting_balance
        self.start_of_day_balance = self.config.starting_balance

        # Performance tracking
        self.trade_history = []
        self.equity_curve = []
        self.daily_stats = []

        # Analysis log - records every signal/scan cycle for visibility
        self.analysis_log = []
        self.max_analysis_log = 200

        # Timezone
        self.tz = pytz.timezone(self.config.timezone)

    def initialize(self):
        """Initialize all components."""
        log.info("=" * 60)
        log.info(f"ALGOBOT v1.0 - {self.config.mode.upper()} MODE")
        log.info(f"Starting Capital: ${self.config.starting_balance:,.2f}")
        log.info("=" * 60)

        # Notifications
        self.notifier = Notifier(self.config)
        self.notifier.system_alert(
            f"Bot starting in {self.config.mode.upper()} mode "
            f"with ${self.config.starting_balance:,.2f}",
            level="success"
        )

        # Connect to IBKR
        self._connect_broker()

        # Initialize risk management
        self.risk_manager = RiskManager(self.config, self.notifier)
        self.position_sizer = PositionSizer(self.config)

        # Market data feed
        self.market_data = MarketDataFeed(self.config, self.broker)

        # Load strategies
        self._load_strategies()

        # TradersPost integration
        if self.config.traderspost_webhook_url:
            self.tp_broker = TradersPostBroker(self.config)
            log.info("TradersPost integration enabled")

        # TradingView webhook receiver
        if self.config.tradingview_webhook_secret:
            self.tv_receiver = TradingViewReceiver(
                self.config,
                callback=self._handle_tv_signal
            )
            log.info("TradingView webhook receiver enabled")

        # Scheduler for periodic tasks
        self.scheduler = BackgroundScheduler(timezone=self.tz)
        self._setup_schedule()

        log.info("All components initialized successfully")

    def _connect_broker(self):
        """Connect to IBKR (skipped on Render where no TWS is available)."""
        self.broker = IBKRBroker(self.config)

        # Skip IBKR connection on Render - no TWS/Gateway available
        if os.environ.get("RENDER"):
            log.info("Running on Render - skipping IBKR connection (using yfinance for data)")
            return

        connected = self.broker.connect()
        if connected:
            log.info(f"Connected to IBKR ({self.config.mode} mode)")
            # Sync account state
            account = self.broker.get_account_summary()
            if account:
                self.current_balance = account.get("net_liquidation", self.config.starting_balance)
                self.peak_balance = max(self.peak_balance, self.current_balance)
                log.info(f"Account balance: ${self.current_balance:,.2f}")
            # Sync existing positions
            self.positions = self.broker.get_positions()
            if self.positions:
                log.info(f"Synced {len(self.positions)} existing positions")
        else:
            log.warning("IBKR connection failed - running in data-only mode")

    def _load_strategies(self):
        """Load and initialize all enabled strategies."""
        strat_configs = {
            "mean_reversion": MeanReversionStrategy,
            "momentum": MomentumStrategy,
            "vwap_scalp": VWAPScalpStrategy,
            "pairs_trading": PairsTradingStrategy,
            "smc_forever": SMCForeverStrategy,
        }

        allocation = self.config.strategy_allocation
        for name, StratClass in strat_configs.items():
            strat_config = self.config.get_strategy_config(name)
            if strat_config.get("enabled", False):
                alloc_pct = allocation.get(name, 0.25)
                alloc_capital = self.current_balance * alloc_pct
                strategy = StratClass(
                    config=strat_config,
                    indicators=self.indicators,
                    capital=alloc_capital
                )
                self.strategies[name] = strategy
                log.info(
                    f"Loaded strategy: {name} | "
                    f"Allocation: {alloc_pct:.0%} (${alloc_capital:,.2f})"
                )

        log.info(f"Total strategies loaded: {len(self.strategies)}")

    def _setup_schedule(self):
        """Set up scheduled tasks."""
        sched = self.config.schedule_config

        # Pre-market scan
        pre_market = sched.get("pre_market_scan", "09:15")
        h, m = map(int, pre_market.split(":"))
        self.scheduler.add_job(
            self._pre_market_scan,
            "cron", hour=h, minute=m,
            day_of_week="mon-fri",
            id="pre_market_scan"
        )

        # End of day routine
        close_time = sched.get("market_close", "16:00")
        h, m = map(int, close_time.split(":"))
        self.scheduler.add_job(
            self._end_of_day,
            "cron", hour=h, minute=5,
            day_of_week="mon-fri",
            id="end_of_day"
        )

        # Health check every 5 minutes
        self.scheduler.add_job(
            self._health_check,
            "interval", minutes=5,
            id="health_check"
        )

    def start(self):
        """Start the trading engine main loop."""
        self.initialize()
        self.running = True

        # Handle graceful shutdown (only works in main thread)
        try:
            signal.signal(signal.SIGINT, self._shutdown)
            signal.signal(signal.SIGTERM, self._shutdown)
        except ValueError:
            # Running in a background thread (e.g., Render/gunicorn)
            log.info("Running in background thread - signal handlers skipped")

        # Start scheduler
        self.scheduler.start()

        # Start TradingView webhook server in background
        if self.tv_receiver:
            tv_thread = threading.Thread(
                target=self.tv_receiver.start,
                daemon=True
            )
            tv_thread.start()

        log.info("Trading engine started - entering main loop")
        self.notifier.system_alert("Trading engine started", level="success")

        # Run initial scan immediately so dashboard has data on load
        self._run_scanner_cycle()

        try:
            self._main_loop()
        except Exception as e:
            log.error(f"Engine error: {e}", exc_info=True)
            self.notifier.system_alert(f"Engine error: {e}", level="error")
        finally:
            self.stop()

    def _run_scanner_cycle(self):
        """Run data update + strategy scan (no trading). Populates scanner dashboard."""
        try:
            self._update_data()
            self._run_strategies()
            log.info(f"Scanner cycle complete - {sum(len(s.scan_results) for s in self.strategies.values())} symbols scanned")
        except Exception as e:
            log.error(f"Scanner cycle error: {e}", exc_info=True)

    def _main_loop(self):
        """Main trading loop - runs continuously during market hours."""
        scan_timer = 0
        while self.running:
            try:
                now = datetime.now(self.tz)

                if not self._is_market_hours(now):
                    # Still run scanner so dashboard shows live data
                    scan_timer += 1
                    if scan_timer >= 4:  # Every ~2 minutes (4 x 30s sleep)
                        self._run_scanner_cycle()
                        scan_timer = 0
                    time.sleep(30)
                    continue

                if self.paused:
                    time.sleep(5)
                    continue

                # Check daily loss limit
                if self.risk_manager.is_daily_loss_exceeded(
                    self.current_balance, self.start_of_day_balance
                ):
                    if not self.paused:
                        log.warning("Daily loss limit hit - pausing trading")
                        self.notifier.risk_alert(
                            f"Daily loss limit reached. "
                            f"Day P&L: ${self.daily_pnl:+.2f}"
                        )
                        self.paused = True
                    time.sleep(60)
                    continue

                # Check max drawdown
                if self.risk_manager.is_max_drawdown_exceeded(
                    self.current_balance, self.peak_balance
                ):
                    log.critical("MAX DRAWDOWN EXCEEDED - EMERGENCY STOP")
                    self.notifier.risk_alert(
                        f"MAX DRAWDOWN EXCEEDED! "
                        f"Balance: ${self.current_balance:,.2f} | "
                        f"Peak: ${self.peak_balance:,.2f}"
                    )
                    self._close_all_positions("Max drawdown exceeded")
                    self.running = False
                    break

                # --- Core Loop ---
                # 1. Update market data
                self._update_data()

                # 2. Monitor existing positions (stops, targets, trailing)
                self._monitor_positions()

                # 3. Run strategies and generate signals
                signals = self._run_strategies()

                # 4. Filter signals through risk manager
                approved = self.risk_manager.filter_signals(
                    signals, self.positions, self.current_balance
                )

                # 5. Execute approved signals
                for sig in approved:
                    self._execute_signal(sig)

                # 6. Update account state
                self._update_account()

                # Sleep based on fastest strategy timeframe
                time.sleep(15)

            except Exception as e:
                log.error(f"Main loop error: {e}", exc_info=True)
                time.sleep(30)

    def _is_market_hours(self, now):
        """Check if within trading hours."""
        sched = self.config.schedule_config
        day_name = now.strftime("%A")
        trading_days = sched.get("trading_days", [
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
        ])

        if day_name not in trading_days:
            return False

        open_time = sched.get("market_open", "09:30")
        close_time = sched.get("market_close", "16:00")
        avoid_first = sched.get("avoid_first_minutes", 5)
        avoid_last = sched.get("avoid_last_minutes", 15)

        h_open, m_open = map(int, open_time.split(":"))
        h_close, m_close = map(int, close_time.split(":"))

        market_open = now.replace(hour=h_open, minute=m_open, second=0)
        market_close = now.replace(hour=h_close, minute=m_close, second=0)

        # Apply buffer
        actual_open = market_open + timedelta(minutes=avoid_first)
        actual_close = market_close - timedelta(minutes=avoid_last)

        return actual_open <= now <= actual_close

    def _update_data(self):
        """Fetch latest market data for all strategy symbols."""
        all_symbols = set()
        for strategy in self.strategies.values():
            all_symbols.update(strategy.get_symbols())

        # Also include symbols we have positions in
        all_symbols.update(self.positions.keys())

        self.market_data.update(list(all_symbols))

    def _monitor_positions(self):
        """Check stops, trailing stops, and take profit targets."""
        positions_to_close = []

        for symbol, pos in self.positions.items():
            current_price = self.market_data.get_price(symbol)
            if current_price is None:
                continue

            entry_price = pos["entry_price"]
            direction = pos.get("direction", "long")

            # Calculate unrealized P&L
            if direction == "long":
                pnl_pct = (current_price - entry_price) / entry_price
            else:
                pnl_pct = (entry_price - current_price) / entry_price

            pos["unrealized_pnl_pct"] = pnl_pct
            pos["current_price"] = current_price

            # Stop loss
            stop_price = pos.get("stop_loss")
            if stop_price:
                hit = (direction == "long" and current_price <= stop_price) or \
                      (direction == "short" and current_price >= stop_price)
                if hit:
                    positions_to_close.append(
                        (symbol, "stop_loss", f"Stop hit at ${current_price:.2f}")
                    )
                    continue

            # Take profit
            target_price = pos.get("take_profit")
            if target_price:
                hit = (direction == "long" and current_price >= target_price) or \
                      (direction == "short" and current_price <= target_price)
                if hit:
                    positions_to_close.append(
                        (symbol, "take_profit", f"Target hit at ${current_price:.2f}")
                    )
                    continue

            # Trailing stop update
            trailing_pct = pos.get("trailing_stop_pct",
                                   self.config.risk_config.get("trailing_stop_pct", 0.02))
            if direction == "long":
                new_trail = current_price * (1 - trailing_pct)
                if "trailing_stop" not in pos or new_trail > pos["trailing_stop"]:
                    pos["trailing_stop"] = new_trail
                if current_price <= pos.get("trailing_stop", 0):
                    positions_to_close.append(
                        (symbol, "trailing_stop",
                         f"Trailing stop at ${current_price:.2f}")
                    )

            # Max holding period check
            if "entry_time" in pos and "max_hold_bars" in pos:
                elapsed = (datetime.now(self.tz) - pos["entry_time"]).total_seconds()
                bar_seconds = pos.get("bar_seconds", 300)
                if elapsed > pos["max_hold_bars"] * bar_seconds:
                    positions_to_close.append(
                        (symbol, "time_exit", "Max holding period exceeded")
                    )

        # Execute closes
        for symbol, reason_type, reason_msg in positions_to_close:
            self._close_position(symbol, reason_type, reason_msg)

    def _run_strategies(self):
        """Run all strategies and collect signals."""
        all_signals = []

        for name, strategy in self.strategies.items():
            try:
                signals = strategy.generate_signals(self.market_data)
                for sig in signals:
                    sig["strategy"] = name
                all_signals.extend(signals)
            except Exception as e:
                log.error(f"Strategy {name} error: {e}", exc_info=True)

        # Log analysis cycle
        if all_signals:
            for sig in all_signals:
                entry = {
                    "time": datetime.now(self.tz).isoformat(),
                    "strategy": sig.get("strategy", "unknown"),
                    "symbol": sig.get("symbol"),
                    "action": sig.get("action"),
                    "price": sig.get("price"),
                    "confidence": sig.get("confidence"),
                    "reason": sig.get("reason", ""),
                    "stop_loss": sig.get("stop_loss"),
                    "take_profit": sig.get("take_profit"),
                }
                self.analysis_log.append(entry)

            # Trim log
            if len(self.analysis_log) > self.max_analysis_log:
                self.analysis_log = self.analysis_log[-self.max_analysis_log:]

        return all_signals

    def _execute_signal(self, signal):
        """Execute a trading signal."""
        symbol = signal["symbol"]
        action = signal["action"]  # buy, sell, short, cover
        strategy = signal.get("strategy", "unknown")

        # Skip if we already have a position (for buy signals)
        if action in ("buy", "short") and symbol in self.positions:
            log.debug(f"Skipping {action} {symbol} - already in position")
            return

        # Position sizing
        current_price = self.market_data.get_price(symbol)
        if current_price is None:
            log.warning(f"No price for {symbol} - skipping signal")
            return

        stop_loss_price = signal.get("stop_loss")
        if not stop_loss_price:
            stop_pct = self.config.stop_loss_pct
            stop_loss_price = current_price * (1 - stop_pct) if action == "buy" \
                else current_price * (1 + stop_pct)

        qty = self.position_sizer.calculate(
            balance=self.current_balance,
            price=current_price,
            stop_loss=stop_loss_price,
            strategy_allocation=self.config.strategy_allocation.get(strategy, 0.25)
        )

        if qty <= 0:
            log.debug(f"Position size 0 for {symbol} - skipping")
            return

        # Calculate take profit
        take_profit_price = signal.get("take_profit")
        if not take_profit_price:
            tp_pct = self.config.take_profit_pct
            take_profit_price = current_price * (1 + tp_pct) if action == "buy" \
                else current_price * (1 - tp_pct)

        # Place order via broker
        order = self.broker.place_order(
            symbol=symbol,
            action=action.upper(),
            quantity=qty,
            order_type="LIMIT",
            limit_price=current_price,
        )

        if order:
            log.info(
                f"ORDER {action.upper()} {symbol} | "
                f"Qty: {qty} | Price: ${current_price:.2f} | "
                f"Stop: ${stop_loss_price:.2f} | Target: ${take_profit_price:.2f} | "
                f"Strategy: {strategy}"
            )

            # Track position
            self.positions[symbol] = {
                "symbol": symbol,
                "direction": "long" if action in ("buy",) else "short",
                "quantity": qty,
                "entry_price": current_price,
                "entry_time": datetime.now(self.tz),
                "stop_loss": stop_loss_price,
                "take_profit": take_profit_price,
                "trailing_stop_pct": signal.get(
                    "trailing_stop_pct",
                    self.config.risk_config.get("trailing_stop_pct", 0.02)
                ),
                "strategy": strategy,
                "order_id": order.get("order_id"),
                "max_hold_bars": signal.get("max_hold_bars", 40),
                "bar_seconds": signal.get("bar_seconds", 300),
            }

            # Notify
            self.notifier.trade_alert(
                action=action,
                symbol=symbol,
                qty=qty,
                price=current_price,
                strategy=strategy,
                reason=signal.get("reason", "")
            )

            # Forward to TradersPost if configured
            if self.tp_broker:
                self.tp_broker.send_signal(signal)

            # Track trade
            self.daily_trades.append({
                "time": datetime.now(self.tz).isoformat(),
                "symbol": symbol,
                "action": action,
                "qty": qty,
                "price": current_price,
                "strategy": strategy,
            })

    def _close_position(self, symbol, reason_type, reason_msg):
        """Close a position."""
        pos = self.positions.get(symbol)
        if not pos:
            return

        current_price = self.market_data.get_price(symbol)
        if current_price is None:
            current_price = pos.get("current_price", pos["entry_price"])

        action = "SELL" if pos["direction"] == "long" else "BUY"

        order = self.broker.place_order(
            symbol=symbol,
            action=action,
            quantity=pos["quantity"],
            order_type="MARKET",
        )

        # Calculate P&L
        if pos["direction"] == "long":
            pnl = (current_price - pos["entry_price"]) * pos["quantity"]
        else:
            pnl = (pos["entry_price"] - current_price) * pos["quantity"]

        self.daily_pnl += pnl

        log.info(
            f"CLOSED {symbol} | {reason_type} | "
            f"P&L: ${pnl:+.2f} | {reason_msg}"
        )

        self.notifier.trade_alert(
            action=f"CLOSE ({reason_type})",
            symbol=symbol,
            qty=pos["quantity"],
            price=current_price,
            strategy=pos["strategy"],
            reason=f"{reason_msg} | P&L: ${pnl:+.2f}"
        )

        # Record trade
        self.trade_history.append({
            "symbol": symbol,
            "direction": pos["direction"],
            "entry_price": pos["entry_price"],
            "exit_price": current_price,
            "quantity": pos["quantity"],
            "pnl": pnl,
            "pnl_pct": pnl / (pos["entry_price"] * pos["quantity"]),
            "strategy": pos["strategy"],
            "reason": reason_type,
            "entry_time": pos["entry_time"].isoformat(),
            "exit_time": datetime.now(self.tz).isoformat(),
        })

        del self.positions[symbol]

    def _close_all_positions(self, reason):
        """Emergency close all positions."""
        log.warning(f"Closing all positions: {reason}")
        symbols = list(self.positions.keys())
        for symbol in symbols:
            self._close_position(symbol, "emergency", reason)

    def _update_account(self):
        """Update account balance and tracking."""
        account = self.broker.get_account_summary()
        if account:
            self.current_balance = account.get(
                "net_liquidation", self.current_balance
            )
            self.peak_balance = max(self.peak_balance, self.current_balance)

            # Update scaling tier
            tier = self.config.get_scaling_tier(self.current_balance)
            if tier:
                self.risk_manager.update_tier(tier)

        # Track equity curve
        self.equity_curve.append({
            "time": datetime.now(self.tz).isoformat(),
            "balance": self.current_balance,
            "positions": len(self.positions),
            "daily_pnl": self.daily_pnl,
        })

    def _handle_tv_signal(self, signal):
        """Handle incoming TradingView webhook signal."""
        log.info(f"TradingView signal received: {signal}")

        # Validate signal age
        max_age = self.config.get_strategy_config(
            "tradingview_signals"
        ).get("max_signal_age_seconds", 30)

        signal["strategy"] = "tradingview"
        signal["source"] = "tradingview_webhook"

        # Run through risk manager
        approved = self.risk_manager.filter_signals(
            [signal], self.positions, self.current_balance
        )

        for sig in approved:
            self._execute_signal(sig)

    def _pre_market_scan(self):
        """Pre-market preparation."""
        log.info("=== PRE-MARKET SCAN ===")
        self.start_of_day_balance = self.current_balance
        self.daily_pnl = 0.0
        self.daily_trades = []
        self.paused = False

        # Refresh strategy capital allocations
        for name, strategy in self.strategies.items():
            alloc = self.config.strategy_allocation.get(name, 0.25)
            strategy.update_capital(self.current_balance * alloc)

        self.notifier.system_alert(
            f"Pre-market scan complete. Balance: ${self.current_balance:,.2f}",
            level="info"
        )

    def _end_of_day(self):
        """End of day routine."""
        log.info("=== END OF DAY ===")

        # Calculate daily stats
        wins = [t for t in self.daily_trades if t.get("pnl", 0) > 0]
        total_trades = len(self.daily_trades)
        win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0

        stats = {
            "date": datetime.now(self.tz).strftime("%Y-%m-%d"),
            "pnl": self.daily_pnl,
            "pnl_pct": (self.daily_pnl / self.start_of_day_balance * 100)
                       if self.start_of_day_balance > 0 else 0,
            "trades": total_trades,
            "win_rate": win_rate,
            "balance": self.current_balance,
            "open_positions": len(self.positions),
        }

        self.daily_stats.append(stats)
        self.notifier.daily_summary(stats)
        log.info(f"Day P&L: ${self.daily_pnl:+.2f} | Trades: {total_trades}")

    def _health_check(self):
        """Periodic health check."""
        if not self.broker.is_connected():
            log.warning("Broker disconnected - attempting reconnect")
            self.broker.reconnect()

    def _shutdown(self, signum=None, frame=None):
        """Graceful shutdown."""
        log.info("Shutdown signal received")
        self.running = False

    def stop(self):
        """Stop the engine."""
        log.info("Stopping trading engine...")
        self.running = False

        if self.scheduler:
            self.scheduler.shutdown(wait=False)

        if self.broker:
            self.broker.disconnect()

        self.notifier.system_alert("Trading engine stopped", level="warning")
        log.info("Engine stopped")

    def get_scanner_data(self):
        """Get live scanner data from all strategies for dashboard."""
        scanner = {}
        for name, strategy in self.strategies.items():
            scan = strategy.get_scan_results()
            if scan:
                scanner[name] = scan
        return scanner

    def get_analysis_log(self):
        """Get recent analysis log entries."""
        return self.analysis_log[-100:]

    def get_status(self):
        """Get current engine status for dashboard."""
        return {
            "running": self.running,
            "paused": self.paused,
            "mode": self.config.mode,
            "balance": self.current_balance,
            "starting_balance": self.config.starting_balance,
            "total_return_pct": (
                (self.current_balance - self.config.starting_balance)
                / self.config.starting_balance * 100
            ),
            "daily_pnl": self.daily_pnl,
            "peak_balance": self.peak_balance,
            "drawdown_pct": (
                (self.peak_balance - self.current_balance)
                / self.peak_balance * 100
            ) if self.peak_balance > 0 else 0,
            "positions": len(self.positions),
            "position_details": self.positions,
            "strategies_active": list(self.strategies.keys()),
            "total_trades": len(self.trade_history),
            "daily_trades": len(self.daily_trades),
            "broker_connected": self.broker.is_connected() if self.broker else False,
        }
