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
from bot.signals.politician_tracker import PoliticianTradeTracker
from bot.signals.news_feed import NewsFeed
from bot.strategies.mean_reversion import MeanReversionStrategy
from bot.strategies.momentum import MomentumStrategy
from bot.strategies.vwap import VWAPScalpStrategy
from bot.strategies.pairs_trading import PairsTradingStrategy
from bot.strategies.smc_forever import SMCForeverStrategy
from bot.strategies.rvol_momentum import RvolMomentumStrategy
from bot.learning.trade_analyzer import TradeAnalyzer
from bot.learning.ai_insights import AIInsights
from bot.learning.auto_tuner import AutoTuner
from bot.signals.regime_detector import RegimeDetector
from bot.risk.hedging import HedgingManager
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
        self.politician_tracker = None
        self.news_feed = None
        self.trade_analyzer = None
        self.auto_tuner = None
        self.regime_detector = None
        self.hedging_manager = None
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

        # Signal deduplication - prevent duplicate entries
        self._signal_cooldowns = {}  # {symbol: last_signal_datetime}
        self._signal_cooldown_secs = 120  # Min seconds between signals for same symbol

        # Performance tracking
        self.trade_history = []
        self.equity_curve = []
        self.daily_stats = []

        # Win/Loss tracking
        self.performance_stats = {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "total_profit": 0.0,
            "total_loss": 0.0,
            "largest_win": 0.0,
            "largest_loss": 0.0,
            "win_streak": 0,
            "loss_streak": 0,
            "current_streak": 0,  # positive = wins, negative = losses
            "best_streak": 0,
            "worst_streak": 0,
        }

        # Weekly watchlist
        watchlist_cfg = self.config.settings.get("watchlist", {})
        self.watchlist = list(watchlist_cfg.get("symbols", []))
        self.watchlist_performance = {}  # {symbol: {week_start, pnl, trades, ...}}

        # Analysis log - records every signal/scan cycle for visibility
        self.analysis_log = []
        self.max_analysis_log = 200

        # Timezone
        self.tz = pytz.timezone(self.config.timezone)
        self._in_premarket = False

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

        # Start IBKR real-time streaming if connected
        if self.broker and self.broker.is_connected():
            all_symbols = list(set(self.watchlist))
            self.market_data.start_streaming(all_symbols)
            log.info("IBKR real-time streaming initialized for watchlist")

        # Load strategies
        self._load_strategies()

        # TradersPost integration
        if self.config.traderspost_webhook_url:
            self.tp_broker = TradersPostBroker(self.config)
            log.info(f"TradersPost integration ENABLED - webhook configured")
            log.info(f"TradersPost URL: ...{self.config.traderspost_webhook_url[-20:]}")
        else:
            log.warning(
                "TradersPost NOT configured! Set TRADERSPOST_WEBHOOK_URL env var. "
                "All trades will be SIMULATED until this is set."
            )

        # TradingView webhook receiver
        if self.config.tradingview_webhook_secret:
            self.tv_receiver = TradingViewReceiver(
                self.config,
                callback=self._handle_tv_signal
            )
            log.info("TradingView webhook receiver enabled")

        # Politician trade tracker (always enabled)
        self.politician_tracker = PoliticianTradeTracker(
            self.config,
            callback=self._handle_politician_signal
        )
        log.info("Politician trade tracker enabled")

        # News feed (if API key configured)
        if self.config.news_api_key:
            self.news_feed = NewsFeed(
                self.config,
                callback=self._handle_news_signal
            )
            log.info("News feed enabled")

        # Trade learning system
        self.trade_analyzer = TradeAnalyzer(self.config)
        log.info("Trade learning system enabled")

        # Claude AI Insights (analyzes trades for deeper learning)
        self.ai_insights = AIInsights(self.config)
        if self.ai_insights.is_available():
            log.info("Claude AI Insights ENABLED")

        # Auto-Tuner (autonomously optimizes parameters from AI analysis)
        self.auto_tuner = AutoTuner(self.config)
        if self.auto_tuner.is_available():
            log.info("Auto-Tuner ENABLED - bot will self-optimize parameters")

        # Load persisted trade history from previous sessions
        if self.trade_analyzer:
            persisted = self.trade_analyzer.get_persisted_trades()
            if persisted:
                self.trade_history = list(persisted)
                log.info(f"Restored {len(persisted)} trades from previous sessions")

        # Market regime detector
        self.regime_detector = RegimeDetector(self.indicators)
        log.info("Market regime detector enabled")

        # Hedging manager
        self.hedging_manager = HedgingManager(self.config)
        log.info(f"Hedging system enabled (auto_hedge={self.hedging_manager.auto_hedge})")

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
            # Sync positions from Alpaca on startup (Render uses TradersPost → Alpaca)
            self._sync_positions_from_alpaca()
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
            "rvol_momentum": RvolMomentumStrategy,
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

        # Auto-Tune: Run midday (12:30 PM) and after EOD (4:30 PM)
        self.scheduler.add_job(
            self._run_auto_tune,
            "cron", hour=12, minute=30,
            day_of_week="mon-fri",
            id="auto_tune_midday"
        )
        self.scheduler.add_job(
            self._run_auto_tune,
            "cron", hour=16, minute=30,
            day_of_week="mon-fri",
            id="auto_tune_eod"
        )

        # Alpaca position sync every 2 minutes (prevents phantom positions)
        if self.config.alpaca_api_key and self.config.alpaca_secret_key:
            self.scheduler.add_job(
                self._sync_positions_with_broker,
                "interval", minutes=2,
                id="alpaca_position_sync"
            )
            log.info("Alpaca position sync scheduled (every 2 min)")

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

        # Start politician trade tracker
        if self.politician_tracker:
            self.politician_tracker.start()

        # Start news feed
        if self.news_feed:
            self.news_feed.start()

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
                # 0. Dynamic discovery: feed top movers into RVOL strategy
                self._discover_dynamic_symbols()

                # 1. Update market data
                self._update_data()

                # 2. Detect market regime (every cycle, uses cached data)
                regime_result = self.regime_detector.detect(self.market_data)

                # 3. Monitor existing positions (stops, targets, trailing)
                self._monitor_positions()

                # 4. Run strategies and generate signals
                signals = self._run_strategies()

                # 5. Check hedging needs
                if self.hedging_manager and self.hedging_manager.auto_hedge:
                    hedge_signals = self.hedging_manager.evaluate(
                        self.positions, self.current_balance, regime_result
                    )
                    signals.extend(hedge_signals)

                # 6. Filter signals through risk manager
                approved = self.risk_manager.filter_signals(
                    signals, self.positions, self.current_balance
                )

                # 7a. Pre-market filtering: limit strategies and reduce size
                if getattr(self, "_in_premarket", False):
                    pm_config = self.config.schedule_config.get("premarket", {})
                    allowed = pm_config.get("allowed_strategies", [])
                    size_mult = pm_config.get("reduce_size_pct", 0.5)
                    if allowed:
                        approved = [s for s in approved if s.get("strategy") in allowed]
                    for sig in approved:
                        if sig.get("quantity"):
                            sig["quantity"] = max(1, int(sig["quantity"] * size_mult))

                # 7b. Apply regime-based filtering
                if regime_result and regime_result.get("regime") == "crisis":
                    # In crisis, only allow hedge signals and exits
                    approved = [s for s in approved if
                                s.get("source") == "hedging" or
                                s.get("action") in ("sell", "cover", "close")]

                # 7c. Scanner summary notification
                symbols_scanned = sum(len(s.get_symbols()) for s in self.strategies.values())
                regime_str = regime_result.get("regime") if regime_result else None
                spy_change = None
                if self.market_data:
                    spy_data = self.market_data.get_data("SPY")
                    if spy_data is not None and len(spy_data) >= 2:
                        spy_change = (spy_data["close"].iloc[-1] - spy_data["close"].iloc[-2]) / spy_data["close"].iloc[-2] * 100
                rejected_count = len(signals) - len(approved) if signals else 0
                if signals or approved:
                    self.notifier.scanner_summary(
                        symbols_scanned=symbols_scanned,
                        signals_found=signals,
                        regime=regime_str,
                        spy_change=spy_change,
                        approved=approved,
                        rejected=rejected_count if rejected_count > 0 else None,
                    )

                # 8. Execute approved signals
                for sig in approved:
                    self._execute_signal(sig)
                    # Record regime context for learning
                    if self.trade_analyzer and regime_result:
                        self.trade_analyzer.record_regime_trade(
                            sig.get("strategy", "unknown"),
                            regime_result.get("regime", "unknown"),
                            0  # P&L recorded at close
                        )

                # 9. Update account state
                self._update_account()

                # Sleep based on fastest strategy timeframe
                time.sleep(15)

            except Exception as e:
                log.error(f"Main loop error: {e}", exc_info=True)
                time.sleep(30)

    def _is_crypto_symbol(self, symbol):
        """Check if a symbol is a crypto ticker (e.g. BTC-USD, ETH-USDT)."""
        crypto_cfg = self.config.settings.get("crypto", {})
        if not crypto_cfg.get("enabled", False):
            return False
        suffixes = crypto_cfg.get("symbols_suffix", ["-USD", "-USDT", "-BTC", "-ETH"])
        return any(symbol.upper().endswith(s) for s in suffixes)

    def _has_crypto_symbols(self):
        """Check if any watched/traded symbols are crypto (enables 24/7 mode)."""
        all_syms = set(self.watchlist) | set(self.positions.keys())
        for s in self.strategies.values():
            all_syms.update(s.get_symbols())
        return any(self._is_crypto_symbol(sym) for sym in all_syms)

    def _is_market_hours(self, now):
        """Check if within trading hours (includes optional premarket + crypto 24/7)."""
        # Crypto trades 24/7 - if we have any crypto symbols, always run
        if self._has_crypto_symbols():
            return True

        sched = self.config.schedule_config
        day_name = now.strftime("%A")
        trading_days = sched.get("trading_days", [
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
        ])

        if day_name not in trading_days:
            return False

        # Check premarket window
        premarket = sched.get("premarket", {})
        if premarket.get("enabled", False):
            pm_start = premarket.get("start_time", "08:00")
            h_pm, m_pm = map(int, pm_start.split(":"))
            pm_open = now.replace(hour=h_pm, minute=m_pm, second=0)
            regular_open = now.replace(hour=9, minute=30, second=0)
            if pm_open <= now < regular_open:
                self._in_premarket = True
                return True

        self._in_premarket = False

        open_time = sched.get("market_open", "09:30")
        close_time = sched.get("market_close", "16:00")
        avoid_first = sched.get("avoid_first_minutes", 30)
        avoid_last = sched.get("avoid_last_minutes", 30)

        h_open, m_open = map(int, open_time.split(":"))
        h_close, m_close = map(int, close_time.split(":"))

        market_open = now.replace(hour=h_open, minute=m_open, second=0)
        market_close = now.replace(hour=h_close, minute=m_close, second=0)

        # Apply buffer
        actual_open = market_open + timedelta(minutes=avoid_first)
        actual_close = market_close - timedelta(minutes=avoid_last)

        return actual_open <= now <= actual_close

    def _update_data(self):
        """Fetch latest market data for all strategy symbols + watchlist."""
        all_symbols = set()
        for strategy in self.strategies.values():
            all_symbols.update(strategy.get_symbols())

        # Also include symbols we have positions in
        all_symbols.update(self.positions.keys())

        # Include watchlist symbols for live prices
        all_symbols.update(self.watchlist)

        self.market_data.update(list(all_symbols))

    def _monitor_positions(self):
        """Check stops, trailing stops, take profit, break-even, and partial exits."""
        positions_to_close = []
        partial_exits = []

        # Load profit taking & break-even config
        pt_config = self.config.risk_config.get("profit_taking", {})
        pt_enabled = pt_config.get("enabled", False)
        pt_targets = pt_config.get("targets", [])
        be_config = self.config.risk_config.get("breakeven", {})
        be_enabled = be_config.get("enabled", True)
        be_trigger = be_config.get("trigger_pct", 0.015)
        be_buffer = be_config.get("buffer_pct", 0.002)

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

            # --- Break-Even Stop ---
            if be_enabled and not pos.get("breakeven_hit") and pnl_pct >= be_trigger:
                if direction == "long":
                    new_stop = entry_price * (1 + be_buffer)
                else:
                    new_stop = entry_price * (1 - be_buffer)

                old_stop = pos.get("stop_loss", 0)
                # Only move stop UP for longs (more protective)
                if direction == "long" and new_stop > old_stop:
                    pos["stop_loss"] = new_stop
                    pos["breakeven_hit"] = True
                    log.info(
                        f"BREAK-EVEN: {symbol} stop moved to ${new_stop:.2f} "
                        f"(was ${old_stop:.2f}, P&L: {pnl_pct:.1%})"
                    )
                    self.notifier.position_update(
                        symbol, "breakeven",
                        f"Stop moved to ${new_stop:.2f} (was ${old_stop:.2f}) | P&L: {pnl_pct:.1%}"
                    )
                elif direction == "short" and (old_stop == 0 or new_stop < old_stop):
                    pos["stop_loss"] = new_stop
                    pos["breakeven_hit"] = True
                    log.info(
                        f"BREAK-EVEN: {symbol} stop moved to ${new_stop:.2f} "
                        f"(was ${old_stop:.2f}, P&L: {pnl_pct:.1%})"
                    )
                    self.notifier.position_update(
                        symbol, "breakeven",
                        f"Stop moved to ${new_stop:.2f} (was ${old_stop:.2f}) | P&L: {pnl_pct:.1%}"
                    )

            # --- Partial Profit Taking ---
            if pt_enabled and pos["quantity"] > 1:
                targets_hit = pos.get("targets_hit", [])
                for i, target in enumerate(pt_targets):
                    if i in targets_hit:
                        continue
                    target_pct = target.get("pct_from_entry", 0)
                    if pnl_pct >= target_pct:
                        close_pct = target.get("close_pct", 0.33)
                        qty_to_close = max(1, int(pos["quantity"] * close_pct))

                        # Don't close everything via partial - leave at least 1
                        if qty_to_close >= pos["quantity"]:
                            qty_to_close = pos["quantity"] - 1

                        if qty_to_close > 0:
                            partial_exits.append((symbol, qty_to_close, i, target))
                            targets_hit.append(i)
                            pos["targets_hit"] = targets_hit

                            # Move stop to break-even if specified
                            if target.get("move_stop") == "breakeven" and not pos.get("breakeven_hit"):
                                be_stop = entry_price * (1 + be_buffer) if direction == "long" else entry_price * (1 - be_buffer)
                                pos["stop_loss"] = be_stop
                                pos["breakeven_hit"] = True

                            # Tighten trailing stop if specified
                            if target.get("tighten_trail"):
                                old_trail = pos.get("trailing_stop_pct", 0.02)
                                pos["trailing_stop_pct"] = target["tighten_trail"]
                                log.info(f"TRAIL TIGHTENED: {symbol} trailing stop now {target['tighten_trail']:.1%}")
                                self.notifier.position_update(
                                    symbol, "trailing_tightened",
                                    f"Trailing stop tightened from {old_trail:.1%} to {target['tighten_trail']:.1%}"
                                )

                        break  # Only hit one target per cycle

            # --- Stop Loss ---
            stop_price = pos.get("stop_loss")
            if stop_price:
                hit = (direction == "long" and current_price <= stop_price) or \
                      (direction == "short" and current_price >= stop_price)
                if hit:
                    positions_to_close.append(
                        (symbol, "stop_loss", f"Stop hit at ${current_price:.2f}")
                    )
                    continue

            # --- Take Profit (full exit if no partial taking, or for remaining shares) ---
            target_price = pos.get("take_profit")
            if target_price:
                hit = (direction == "long" and current_price >= target_price) or \
                      (direction == "short" and current_price <= target_price)
                if hit:
                    positions_to_close.append(
                        (symbol, "take_profit", f"Target hit at ${current_price:.2f}")
                    )
                    continue

            # --- Trailing Stop ---
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
            elif direction == "short":
                new_trail = current_price * (1 + trailing_pct)
                if "trailing_stop" not in pos or new_trail < pos["trailing_stop"]:
                    pos["trailing_stop"] = new_trail
                if current_price >= pos.get("trailing_stop", float("inf")):
                    positions_to_close.append(
                        (symbol, "trailing_stop",
                         f"Trailing stop at ${current_price:.2f}")
                    )

            # --- Max Holding Period ---
            if "entry_time" in pos and "max_hold_bars" in pos:
                elapsed = (datetime.now(self.tz) - pos["entry_time"]).total_seconds()
                bar_seconds = pos.get("bar_seconds", 300)
                if elapsed > pos["max_hold_bars"] * bar_seconds:
                    positions_to_close.append(
                        (symbol, "time_exit", "Max holding period exceeded")
                    )

        # Execute partial exits first
        for symbol, qty, target_idx, target in partial_exits:
            self._partial_close(symbol, qty, target_idx, target)

        # Execute full closes
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
        """Execute a trading signal through broker chain (IBKR -> TradersPost fallback)."""
        symbol = signal["symbol"]
        action = signal["action"]  # buy, sell, short, cover
        strategy = signal.get("strategy", "unknown")
        now = datetime.now(self.tz)

        # LONG-ONLY MODE: Block all short signals
        if action in ("short",):
            log.info(f"LONG-ONLY: Blocking short signal for {symbol}")
            return

        # --- DUPLICATE ENTRY GUARD ---
        # Prevent same symbol from being entered twice within cooldown window
        if action in ("buy", "short"):
            if symbol in self.positions:
                log.info(f"DUPLICATE BLOCKED: {symbol} already in position")
                return

            last_signal = self._signal_cooldowns.get(symbol)
            if last_signal and (now - last_signal).total_seconds() < self._signal_cooldown_secs:
                elapsed = int((now - last_signal).total_seconds())
                log.warning(
                    f"COOLDOWN BLOCKED: {symbol} signal rejected - "
                    f"last signal {elapsed}s ago (min {self._signal_cooldown_secs}s)"
                )
                return

            # Record this signal time BEFORE execution (prevents race condition)
            self._signal_cooldowns[symbol] = now

        # Position sizing - use market price if available, or signal's price
        current_price = self.market_data.get_price(symbol) if self.market_data else None
        if current_price is None:
            current_price = signal.get("price")
        if current_price is None:
            log.warning(f"No price for {symbol} - skipping signal")
            return

        stop_loss_price = signal.get("stop_loss")
        if not stop_loss_price:
            # Use wider stops for crypto (more volatile)
            if self._is_crypto_symbol(symbol):
                crypto_risk = self.config.settings.get("crypto", {}).get("risk", {})
                stop_pct = crypto_risk.get("stop_loss_pct", 0.05)
            else:
                stop_pct = self.config.stop_loss_pct
            stop_loss_price = current_price * (1 - stop_pct) if action == "buy" \
                else current_price * (1 + stop_pct)

        qty = signal.get("quantity") or self.position_sizer.calculate(
            balance=self.current_balance,
            price=current_price,
            stop_loss=stop_loss_price,
            strategy_allocation=self.config.strategy_allocation.get(strategy, 0.25),
            symbol=symbol,
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

        # --- Broker Execution Chain ---
        # Priority: IBKR -> TradersPost -> Simulated
        order = None
        executed_via = None

        # 1. Try IBKR (primary broker)
        if self.broker and self.broker.is_connected():
            log.info(f"Executing {symbol} via IBKR...")
            order = self.broker.place_order(
                symbol=symbol,
                action=action.upper(),
                quantity=qty,
                order_type="LIMIT",
                limit_price=current_price,
            )
            if order:
                executed_via = "IBKR"
            else:
                log.warning(f"IBKR order failed for {symbol} - falling through to TradersPost")
        else:
            log.debug(f"IBKR not connected - trying TradersPost for {symbol}")

        # 2. TradersPost webhook (primary on Render where IBKR unavailable)
        if not order and self.tp_broker:
            log.info(f"Sending {action.upper()} {symbol} to TradersPost webhook...")
            tp_signal = {
                **signal,
                "quantity": qty,
                "price": current_price,
                "stop_loss": stop_loss_price,
                "take_profit": take_profit_price,
            }
            try:
                tp_result = self.tp_broker.send_signal(tp_signal)
                if tp_result and tp_result.get("success"):
                    order = {
                        "order_id": f"tp_{int(datetime.now(self.tz).timestamp())}",
                        "symbol": symbol,
                        "action": action,
                        "quantity": qty,
                        "status": "sent_to_traderspost",
                    }
                    executed_via = "TradersPost"
                    log.info(f"TradersPost accepted {action.upper()} {symbol} (status {tp_result.get('status_code')})")
                else:
                    log.error(
                        f"TradersPost REJECTED {symbol}: "
                        f"status={tp_result.get('status_code') if tp_result else 'None'} "
                        f"response={tp_result.get('response', 'no response') if tp_result else 'send_signal returned None'}"
                    )
            except Exception as e:
                log.error(f"TradersPost exception for {symbol}: {e}")
        elif not order and not self.tp_broker:
            log.warning(
                f"TradersPost NOT configured - tp_broker is None. "
                f"Set TRADERSPOST_WEBHOOK_URL env var on Render!"
            )

        # 3. If no broker available, track as simulated
        if not order:
            order = {
                "order_id": f"sim_{int(datetime.now(self.tz).timestamp())}",
                "symbol": symbol,
                "action": action,
                "quantity": qty,
                "status": "simulated",
            }
            executed_via = "Simulated"
            log.warning(
                f"SIMULATED order for {symbol} - no broker executed. "
                f"IBKR={'connected' if self.broker and self.broker.is_connected() else 'disconnected'}, "
                f"TradersPost={'configured' if self.tp_broker else 'NOT configured'}"
            )

        log.info(
            f"ORDER {action.upper()} {symbol} via {executed_via} | "
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
            "executed_via": executed_via,
            "max_hold_bars": signal.get("max_hold_bars", 40),
            "bar_seconds": signal.get("bar_seconds", 300),
        }

        # Rich notification with full trade details
        self.notifier.trade_entry(
            symbol=symbol,
            action=action,
            qty=qty,
            price=current_price,
            stop_loss=stop_loss_price,
            take_profit=take_profit_price,
            strategy=strategy,
            reason=signal.get("reason", ""),
            confidence=signal.get("confidence", 0),
            rr_ratio=signal.get("rr_ratio", 0) or (
                round(abs(take_profit_price - current_price) / abs(current_price - stop_loss_price), 1)
                if abs(current_price - stop_loss_price) > 0 else 0
            ),
            executed_via=executed_via,
            rvol=signal.get("rvol"),
            targets=signal.get("targets"),
        )

        # Also forward to TradersPost if IBKR was the primary executor
        if executed_via == "IBKR" and self.tp_broker:
            self.tp_broker.send_signal(signal)

        # Track trade
        self.daily_trades.append({
            "time": datetime.now(self.tz).isoformat(),
            "symbol": symbol,
            "action": action,
            "qty": qty,
            "price": current_price,
            "strategy": strategy,
            "executed_via": executed_via,
        })

    def _close_position(self, symbol, reason_type, reason_msg):
        """Close a position through broker chain."""
        pos = self.positions.get(symbol)
        if not pos:
            return

        current_price = self.market_data.get_price(symbol)
        if current_price is None:
            current_price = pos.get("current_price", pos["entry_price"])

        action = "SELL" if pos["direction"] == "long" else "BUY"
        executed_via = pos.get("executed_via", "Simulated")

        # Try to close via broker chain
        order = None
        if self.broker and self.broker.is_connected():
            order = self.broker.place_order(
                symbol=symbol,
                action=action,
                quantity=pos["quantity"],
                order_type="MARKET",
            )
            if order:
                executed_via = "IBKR"

        if not order and self.tp_broker:
            log.info(f"Closing {symbol} via TradersPost webhook...")
            close_signal = {
                "symbol": symbol,
                "action": action.lower(),
                "quantity": pos["quantity"],
                "price": current_price,
                "source": "exit",
            }
            try:
                tp_result = self.tp_broker.send_signal(close_signal)
                if tp_result and tp_result.get("success"):
                    executed_via = "TradersPost"
                else:
                    log.error(f"TradersPost close failed for {symbol}: {tp_result}")
                    # If broker has no position, this is a phantom - clean up internally
                    if self._alpaca_position_exists(symbol) is False:
                        log.warning(
                            f"PHANTOM POSITION detected: {symbol} exists in bot but NOT "
                            f"in broker. Cleaning up internal state."
                        )
                        executed_via = "Phantom-Cleanup"
            except Exception as e:
                log.error(f"TradersPost close exception for {symbol}: {e}")
        elif not order and not self.tp_broker:
            log.warning(f"No broker to close {symbol} - TRADERSPOST_WEBHOOK_URL not set")

        # Calculate P&L
        if pos["direction"] == "long":
            pnl = (current_price - pos["entry_price"]) * pos["quantity"]
        else:
            pnl = (pos["entry_price"] - current_price) * pos["quantity"]

        self.daily_pnl += pnl
        # Update internal balance tracking
        self.current_balance += pnl
        self.peak_balance = max(self.peak_balance, self.current_balance)

        log.info(
            f"CLOSED {symbol} via {executed_via} | {reason_type} | "
            f"P&L: ${pnl:+.2f} | {reason_msg}"
        )

        pnl_pct = pnl / (pos["entry_price"] * pos["quantity"]) if pos["entry_price"] * pos["quantity"] > 0 else 0
        hold_time = (datetime.now(self.tz) - pos["entry_time"]) if "entry_time" in pos else None

        # Rich exit notification
        self.notifier.trade_exit(
            symbol=symbol,
            direction=pos["direction"],
            qty=pos["quantity"],
            entry_price=pos["entry_price"],
            exit_price=current_price,
            pnl=pnl,
            pnl_pct=pnl_pct * 100,
            reason_type=reason_type,
            reason_msg=reason_msg,
            strategy=pos["strategy"],
            executed_via=executed_via,
            hold_time=hold_time,
        )
        self.trade_history.append({
            "symbol": symbol,
            "direction": pos["direction"],
            "entry_price": pos["entry_price"],
            "exit_price": current_price,
            "quantity": pos["quantity"],
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "strategy": pos["strategy"],
            "reason": reason_type,
            "executed_via": executed_via,
            "entry_time": pos["entry_time"].isoformat(),
            "exit_time": datetime.now(self.tz).isoformat(),
        })

        # Update win/loss stats
        self._update_performance_stats(pnl)

        # Persist trade to disk (survives restarts for AI learning)
        if self.trade_analyzer:
            self.trade_analyzer.persist_trade(self.trade_history[-1])

        # Update watchlist performance tracking
        if symbol in self.watchlist:
            self._update_watchlist_performance(symbol, pnl, pnl_pct)

        del self.positions[symbol]

    def _partial_close(self, symbol, qty_to_close, target_idx, target):
        """Close part of a position (profit taking)."""
        pos = self.positions.get(symbol)
        if not pos or qty_to_close <= 0:
            return

        current_price = self.market_data.get_price(symbol)
        if current_price is None:
            current_price = pos.get("current_price", pos["entry_price"])

        action = "SELL" if pos["direction"] == "long" else "BUY"
        executed_via = pos.get("executed_via", "Simulated")

        # Execute via broker chain
        order = None
        if self.broker and self.broker.is_connected():
            order = self.broker.place_order(
                symbol=symbol, action=action,
                quantity=qty_to_close, order_type="MARKET",
            )
            if order:
                executed_via = "IBKR"

        if not order and self.tp_broker:
            close_signal = {
                "symbol": symbol, "action": action.lower(),
                "quantity": qty_to_close, "price": current_price,
            }
            tp_result = self.tp_broker.send_signal(close_signal)
            if tp_result and tp_result.get("success"):
                executed_via = "TradersPost"

        # Calculate P&L for partial
        if pos["direction"] == "long":
            pnl = (current_price - pos["entry_price"]) * qty_to_close
        else:
            pnl = (pos["entry_price"] - current_price) * qty_to_close

        self.daily_pnl += pnl
        self.current_balance += pnl
        self.peak_balance = max(self.peak_balance, self.current_balance)

        # Update remaining quantity
        pos["quantity"] -= qty_to_close

        target_pct = target.get("pct_from_entry", 0)
        log.info(
            f"PARTIAL CLOSE {symbol}: {qty_to_close} shares at ${current_price:.2f} "
            f"(target {target_idx + 1}: +{target_pct:.0%}) | P&L: ${pnl:+.2f} | "
            f"Remaining: {pos['quantity']} shares"
        )

        self.notifier.trade_partial(
            symbol=symbol,
            qty_closed=qty_to_close,
            qty_remaining=pos["quantity"],
            price=current_price,
            pnl=pnl,
            target_idx=target_idx,
            target_pct=target_pct,
            strategy=pos["strategy"],
        )

        # Record partial trade in history
        self.trade_history.append({
            "symbol": symbol,
            "direction": pos["direction"],
            "entry_price": pos["entry_price"],
            "exit_price": current_price,
            "quantity": qty_to_close,
            "pnl": pnl,
            "pnl_pct": pnl / (pos["entry_price"] * qty_to_close) if pos["entry_price"] > 0 else 0,
            "strategy": pos["strategy"],
            "reason": f"partial_target_{target_idx + 1}",
            "executed_via": executed_via,
            "entry_time": pos["entry_time"].isoformat(),
            "exit_time": datetime.now(self.tz).isoformat(),
            "partial": True,
        })

        # Update performance stats
        self._update_performance_stats(pnl)

        # If position fully closed via partials
        if pos["quantity"] <= 0:
            del self.positions[symbol]

    def _update_performance_stats(self, pnl):
        """Update win/loss tracking stats after a trade closes."""
        stats = self.performance_stats
        stats["total_trades"] += 1

        if pnl > 0.01:
            stats["wins"] += 1
            stats["total_profit"] += pnl
            stats["largest_win"] = max(stats["largest_win"], pnl)
            if stats["current_streak"] > 0:
                stats["current_streak"] += 1
            else:
                stats["current_streak"] = 1
            stats["best_streak"] = max(stats["best_streak"], stats["current_streak"])
        elif pnl < -0.01:
            stats["losses"] += 1
            stats["total_loss"] += abs(pnl)
            stats["largest_loss"] = max(stats["largest_loss"], abs(pnl))
            if stats["current_streak"] < 0:
                stats["current_streak"] -= 1
            else:
                stats["current_streak"] = -1
            stats["worst_streak"] = min(stats["worst_streak"], stats["current_streak"])
        else:
            stats["breakeven"] += 1

        stats["win_streak"] = stats["best_streak"]
        stats["loss_streak"] = abs(stats["worst_streak"])

    def _close_all_positions(self, reason):
        """Emergency close all positions."""
        log.warning(f"Closing all positions: {reason}")
        symbols = list(self.positions.keys())
        for symbol in symbols:
            self._close_position(symbol, "emergency", reason)

    def _update_account(self):
        """Update account balance and tracking (works with or without IBKR)."""
        # Try to sync from IBKR if connected
        if self.broker and self.broker.is_connected():
            account = self.broker.get_account_summary()
            if account:
                self.current_balance = account.get(
                    "net_liquidation", self.current_balance
                )
        else:
            # Internal balance tracking: base + unrealized P&L
            unrealized = 0
            for symbol, pos in self.positions.items():
                price = self.market_data.get_price(symbol)
                if price is not None:
                    if pos["direction"] == "long":
                        unrealized += (price - pos["entry_price"]) * pos["quantity"]
                    else:
                        unrealized += (pos["entry_price"] - price) * pos["quantity"]
            # current_balance already includes realized P&L from _close_position
            # Just need to track unrealized for equity curve

        self.peak_balance = max(self.peak_balance, self.current_balance)

        # Update scaling tier
        tier = self.config.get_scaling_tier(self.current_balance)
        if tier:
            self.risk_manager.update_tier(tier)

        # Track equity curve (include unrealized P&L)
        unrealized_pnl = 0
        for symbol, pos in self.positions.items():
            price = self.market_data.get_price(symbol)
            if price is not None:
                if pos["direction"] == "long":
                    unrealized_pnl += (price - pos["entry_price"]) * pos["quantity"]
                else:
                    unrealized_pnl += (pos["entry_price"] - price) * pos["quantity"]

        self.equity_curve.append({
            "time": datetime.now(self.tz).isoformat(),
            "balance": self.current_balance,
            "equity": self.current_balance + unrealized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "positions": len(self.positions),
            "daily_pnl": self.daily_pnl,
        })

    # --- Alpaca Position Sync (prevents phantom positions) ---

    def _init_alpaca_client(self):
        """Lazily initialize Alpaca REST client for position verification."""
        if hasattr(self, '_alpaca_client'):
            return self._alpaca_client
        api_key = self.config.alpaca_api_key
        secret_key = self.config.alpaca_secret_key
        base_url = self.config.alpaca_base_url
        if api_key and secret_key:
            try:
                import alpaca_trade_api as tradeapi
                self._alpaca_client = tradeapi.REST(
                    api_key, secret_key, base_url, api_version='v2'
                )
                log.info("Alpaca REST client initialized for position sync")
            except Exception as e:
                log.warning(f"Alpaca client init failed: {e}")
                self._alpaca_client = None
        else:
            self._alpaca_client = None
        return self._alpaca_client

    def _alpaca_position_exists(self, symbol):
        """Check if a position exists on the Alpaca broker side.
        Returns True/False, or None if unable to check."""
        client = self._init_alpaca_client()
        if not client:
            return None  # Can't verify without Alpaca credentials
        try:
            pos = client.get_position(symbol)
            return abs(float(pos.qty)) > 0
        except Exception:
            # 404 = no position exists, which is what we're checking for
            return False

    def _sync_positions_from_alpaca(self):
        """On startup (Render), load actual positions from Alpaca so internal
        state matches reality. Prevents exits for positions that don't exist."""
        client = self._init_alpaca_client()
        if not client:
            log.info("Alpaca credentials not set - starting with empty positions")
            return
        try:
            broker_positions = client.list_positions()
            for p in broker_positions:
                symbol = p.symbol.upper()
                qty = abs(float(p.qty))
                entry = float(p.avg_entry_price)
                side = "long" if float(p.qty) > 0 else "short"
                self.positions[symbol] = {
                    "symbol": symbol,
                    "direction": side,
                    "quantity": int(qty) if qty == int(qty) else qty,
                    "entry_price": entry,
                    "entry_time": datetime.now(self.tz),
                    "stop_loss": entry * 0.97 if side == "long" else entry * 1.03,
                    "take_profit": entry * 1.04 if side == "long" else entry * 0.96,
                    "trailing_stop_pct": self.config.risk_config.get("trailing_stop_pct", 0.02),
                    "strategy": "synced_from_alpaca",
                    "executed_via": "TradersPost",
                    "max_hold_bars": 40,
                    "bar_seconds": 300,
                }
            if broker_positions:
                log.info(f"Synced {len(broker_positions)} positions from Alpaca on startup")
            else:
                log.info("Alpaca reports 0 open positions")
        except Exception as e:
            log.warning(f"Alpaca startup sync failed: {e}")

    def _sync_positions_with_broker(self):
        """Reconcile internal positions with actual Alpaca broker positions.
        Removes phantom positions that exist internally but not at the broker."""
        client = self._init_alpaca_client()
        if not client:
            return

        try:
            broker_positions = client.list_positions()
            broker_symbols = {p.symbol.upper() for p in broker_positions}
        except Exception as e:
            log.warning(f"Alpaca position sync failed: {e}")
            return

        # Find phantom positions (in bot but not at broker)
        phantoms = []
        for symbol in list(self.positions.keys()):
            if symbol.upper() not in broker_symbols:
                phantoms.append(symbol)

        for symbol in phantoms:
            pos = self.positions[symbol]
            # Only clean up positions that were sent to TradersPost (not simulated)
            if pos.get("executed_via") in ("TradersPost", "Phantom-Cleanup"):
                log.warning(
                    f"POSITION SYNC: Removing phantom {symbol} "
                    f"(in bot but not at Alpaca broker)"
                )
                del self.positions[symbol]

        if phantoms:
            log.info(
                f"Position sync complete: removed {len(phantoms)} phantom(s), "
                f"{len(self.positions)} positions remain"
            )

    def _handle_tv_signal(self, signal):
        """Handle incoming TradingView webhook signal."""
        log.info(f"TradingView signal received: {signal}")

        signal["strategy"] = "tradingview"
        signal["source"] = "tradingview_webhook"

        # Run through risk manager
        approved = self.risk_manager.filter_signals(
            [signal], self.positions, self.current_balance
        )

        for sig in approved:
            self._execute_signal(sig)

    def _handle_news_signal(self, signal):
        """Handle incoming news-based signal."""
        log.info(f"News signal: {signal['action'].upper()} {signal['symbol']} | {signal.get('reason', '')[:60]}")

        approved = self.risk_manager.filter_signals(
            [signal], self.positions, self.current_balance
        )

        for sig in approved:
            self._execute_signal(sig)

    def _handle_politician_signal(self, signal):
        """Handle incoming politician trade signal."""
        log.info(
            f"Politician signal: {signal.get('politician', 'Unknown')} "
            f"{signal['action'].upper()} {signal['symbol']}"
        )

        # Run through risk manager
        approved = self.risk_manager.filter_signals(
            [signal], self.positions, self.current_balance
        )

        for sig in approved:
            self._execute_signal(sig)

    def handle_manual_signal(self, signal):
        """Handle manually submitted signal (from API/dashboard)."""
        log.info(f"Manual signal received: {signal}")

        signal["strategy"] = signal.get("strategy", "manual")
        signal["source"] = "manual"

        # Provide default stop loss if missing (3% for buys)
        if signal["action"] in ("buy", "short") and not signal.get("stop_loss"):
            price = signal.get("price", 0)
            if price > 0:
                signal["stop_loss"] = price * 0.97 if signal["action"] == "buy" else price * 1.03

        # Run through risk manager
        approved = self.risk_manager.filter_signals(
            [signal], self.positions, self.current_balance
        )

        results = []
        for sig in approved:
            self._execute_signal(sig)
            results.append({"symbol": sig["symbol"], "action": sig["action"], "status": "executed"})

        return results if results else [{"status": "rejected", "reason": "Failed risk checks"}]

    def _update_watchlist_performance(self, symbol, pnl, pnl_pct):
        """Track per-symbol performance for the watchlist."""
        now = datetime.now(self.tz)
        # Week key = Monday of current week
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")

        key = f"{symbol}_{week_start}"
        if key not in self.watchlist_performance:
            self.watchlist_performance[key] = {
                "symbol": symbol,
                "week_start": week_start,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "total_pnl_pct": 0.0,
            }
        perf = self.watchlist_performance[key]
        perf["trades"] += 1
        perf["total_pnl"] += pnl
        perf["total_pnl_pct"] += pnl_pct * 100
        if pnl > 0:
            perf["wins"] += 1
        elif pnl < 0:
            perf["losses"] += 1

    def add_to_watchlist(self, symbol):
        """Add a symbol to the weekly watchlist AND inject into active strategies."""
        symbol = symbol.upper()
        if symbol not in self.watchlist:
            self.watchlist.append(symbol)
            log.info(f"Added {symbol} to watchlist")

        # Also inject into active strategies so it gets scanned immediately
        self._inject_symbol_into_strategies(symbol)

        # Fetch data immediately so it shows up right away
        if self.market_data:
            try:
                self.market_data.update([symbol])
            except Exception:
                pass

        return self.watchlist

    def remove_from_watchlist(self, symbol):
        """Remove a symbol from the weekly watchlist."""
        symbol = symbol.upper()
        if symbol in self.watchlist:
            self.watchlist.remove(symbol)
            log.info(f"Removed {symbol} from watchlist")
        return self.watchlist

    def _inject_symbol_into_strategies(self, symbol):
        """Inject a symbol into appropriate strategies for live scanning."""
        # Add to momentum and mean_reversion (the broadest strategies)
        for name in ("momentum", "mean_reversion", "smc_forever"):
            strat = self.strategies.get(name)
            if strat and symbol not in strat.symbols:
                strat.symbols.append(symbol)
                log.info(f"Injected {symbol} into {name} strategy")

    # Preset watchlist groups for quick-add
    WATCHLIST_PRESETS = {
        "sp100_top": {
            "label": "S&P 100 Top",
            "symbols": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
                        "BRK-B", "UNH", "JNJ", "V", "XOM", "JPM", "PG", "MA",
                        "HD", "CVX", "MRK", "ABBV", "LLY", "PEP", "KO", "COST",
                        "AVGO", "WMT"],
        },
        "growth_tech": {
            "label": "Growth Tech",
            "symbols": ["NVDA", "AMD", "PLTR", "SNOW", "CRWD", "NET", "DDOG",
                        "SHOP", "SQ", "COIN", "MSTR", "SMCI", "ARM", "IONQ",
                        "RKLB", "SOFI", "HOOD", "AFRM", "U", "SE"],
        },
        "sp500_etfs": {
            "label": "S&P 500 ETFs",
            "symbols": ["SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "XLK",
                        "XLF", "XLE", "XLV", "XLI", "ARKK", "TQQQ", "SOXL"],
        },
        "crypto_major": {
            "label": "Crypto Major",
            "symbols": ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "ADA-USD",
                        "DOGE-USD", "AVAX-USD", "DOT-USD", "LINK-USD", "MATIC-USD"],
        },
        "meme_popular": {
            "label": "Meme / Popular",
            "symbols": ["GME", "AMC", "BBBY", "DOGE-USD", "SHIB-USD",
                        "RIVN", "LCID", "NIO", "PLTR", "SOFI"],
        },
    }

    def add_preset_group(self, group_name):
        """Add all symbols from a preset group to the watchlist."""
        preset = self.WATCHLIST_PRESETS.get(group_name)
        if not preset:
            return {"error": f"Unknown preset: {group_name}"}

        added = []
        for symbol in preset["symbols"]:
            if symbol not in self.watchlist:
                self.add_to_watchlist(symbol)
                added.append(symbol)

        log.info(f"Added preset '{group_name}': {len(added)} new symbols")
        return {"group": group_name, "added": added, "total_watchlist": len(self.watchlist)}

    def get_watchlist_data(self):
        """Get watchlist with live prices and weekly performance."""
        now = datetime.now(self.tz)
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")

        # Auto-add politician picks if enabled
        watchlist_cfg = self.config.settings.get("watchlist", {})
        if watchlist_cfg.get("auto_add_politician_picks") and self.politician_tracker:
            for sig in self.politician_tracker.get_signals():
                sym = sig.get("symbol", "").upper()
                if sym and sym not in self.watchlist:
                    self.watchlist.append(sym)

        result = []
        for symbol in self.watchlist:
            item = {"symbol": symbol, "price": None, "change_pct": None}

            # Get live price
            if self.market_data:
                price = self.market_data.get_price(symbol)
                if price:
                    item["price"] = price
                quote = self.market_data.get_quote(symbol) if hasattr(self.market_data, "get_quote") else None
                if quote:
                    item["change_pct"] = quote.get("change_pct")

            # Check if we have an open position
            if symbol in self.positions:
                pos = self.positions[symbol]
                item["in_position"] = True
                item["direction"] = pos.get("direction", "long")
                item["entry_price"] = pos.get("entry_price", 0)
                item["unrealized_pnl_pct"] = pos.get("unrealized_pnl_pct", 0) * 100
                item["stop_loss"] = pos.get("stop_loss", 0)
                item["take_profit"] = pos.get("take_profit", 0)
                item["breakeven_hit"] = pos.get("breakeven_hit", False)
            else:
                item["in_position"] = False

            # Weekly performance
            key = f"{symbol}_{week_start}"
            if key in self.watchlist_performance:
                perf = self.watchlist_performance[key]
                item["week_trades"] = perf["trades"]
                item["week_pnl"] = round(perf["total_pnl"], 2)
                item["week_wins"] = perf["wins"]
                item["week_losses"] = perf["losses"]
            else:
                item["week_trades"] = 0
                item["week_pnl"] = 0
                item["week_wins"] = 0
                item["week_losses"] = 0

            result.append(item)

        return result

    def get_performance_summary(self):
        """Get comprehensive win/loss performance stats."""
        stats = dict(self.performance_stats)

        # Calculated fields
        total = stats["total_trades"]
        if total > 0:
            stats["win_rate"] = round(stats["wins"] / total * 100, 1)
            stats["loss_rate"] = round(stats["losses"] / total * 100, 1)
        else:
            stats["win_rate"] = 0
            stats["loss_rate"] = 0

        if stats["wins"] > 0:
            stats["avg_win"] = round(stats["total_profit"] / stats["wins"], 2)
        else:
            stats["avg_win"] = 0

        if stats["losses"] > 0:
            stats["avg_loss"] = round(stats["total_loss"] / stats["losses"], 2)
        else:
            stats["avg_loss"] = 0

        # Profit factor
        if stats["total_loss"] > 0:
            stats["profit_factor"] = round(stats["total_profit"] / stats["total_loss"], 2)
        else:
            stats["profit_factor"] = float("inf") if stats["total_profit"] > 0 else 0

        # Net P&L
        stats["net_pnl"] = round(stats["total_profit"] - stats["total_loss"], 2)

        # Expectancy (avg $ per trade)
        if total > 0:
            stats["expectancy"] = round(stats["net_pnl"] / total, 2)
        else:
            stats["expectancy"] = 0

        # Round dollar amounts
        stats["total_profit"] = round(stats["total_profit"], 2)
        stats["total_loss"] = round(stats["total_loss"], 2)
        stats["largest_win"] = round(stats["largest_win"], 2)
        stats["largest_loss"] = round(stats["largest_loss"], 2)

        # Per-strategy breakdown
        strategy_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
        for trade in self.trade_history:
            strat = trade.get("strategy", "unknown")
            strategy_stats[strat]["trades"] += 1
            if trade.get("pnl", 0) > 0:
                strategy_stats[strat]["wins"] += 1
            strategy_stats[strat]["pnl"] += trade.get("pnl", 0)

        for strat in strategy_stats:
            s = strategy_stats[strat]
            s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] > 0 else 0
            s["pnl"] = round(s["pnl"], 2)

        stats["by_strategy"] = dict(strategy_stats)

        return stats

    def _pre_market_scan(self):
        """Pre-market preparation with learning-adjusted allocations."""
        log.info("=== PRE-MARKET SCAN ===")
        self.start_of_day_balance = self.current_balance
        self.daily_pnl = 0.0
        self.daily_trades = []
        self.paused = False

        # Run trade analyzer to get adjusted strategy weights
        base_alloc = self.config.strategy_allocation
        adjusted_alloc = base_alloc

        if self.trade_analyzer and len(self.trade_history) >= 10:
            analysis = self.trade_analyzer.analyze(
                self.trade_history,
                current_regime=self.regime_detector.current_regime if self.regime_detector else None
            )
            adjusted_alloc = self.trade_analyzer.get_strategy_weights(base_alloc)

            # Log adjustments
            for strat in adjusted_alloc:
                old = base_alloc.get(strat, 0)
                new = adjusted_alloc.get(strat, 0)
                if abs(old - new) > 0.01:
                    log.info(f"LEARNING: {strat} allocation {old:.0%} -> {new:.0%}")

        # Apply regime multipliers to allocation
        if self.regime_detector:
            regime = self.regime_detector.current_regime
            multipliers = self.regime_detector.get_status().get("strategy_multipliers", {})
            for strat in adjusted_alloc:
                mult = multipliers.get(strat, 1.0)
                adjusted_alloc[strat] = round(adjusted_alloc[strat] * mult, 3)
            # Re-normalize
            total = sum(adjusted_alloc.values())
            if total > 0:
                adjusted_alloc = {k: round(v / total, 3) for k, v in adjusted_alloc.items()}

        # Reset daily counters on strategies (e.g. VWAP daily trade limit)
        for name, strategy in self.strategies.items():
            if hasattr(strategy, "reset_daily"):
                strategy.reset_daily()

        # Refresh strategy capital allocations
        for name, strategy in self.strategies.items():
            alloc = adjusted_alloc.get(name, 0.25)
            strategy.update_capital(self.current_balance * alloc)

        regime_str = self.regime_detector.current_regime.upper() if self.regime_detector else "N/A"
        self.notifier.system_alert(
            f"Pre-market scan complete. Balance: ${self.current_balance:,.2f} | "
            f"Regime: {regime_str}",
            level="info"
        )

    def _end_of_day(self):
        """End of day routine with overnight hold decisions and learning."""
        log.info("=== END OF DAY ===")

        # --- Check for stock split candidates (NEVER hold these overnight) ---
        split_candidates = self._check_split_candidates()
        if split_candidates:
            log.warning(f"SPLIT CANDIDATES detected - blocking overnight: {split_candidates}")
            self.notifier.risk_alert(
                f"Stock split candidates detected - closing at EOD: "
                f"{', '.join(split_candidates)}. "
                f"Splits cause extreme overnight gaps."
            )

        # --- Overnight Hold Logic ---
        overnight_cfg = self.config.schedule_config.get("overnight", {})
        overnight_enabled = overnight_cfg.get("enabled", False)
        max_overnight = overnight_cfg.get("max_overnight_positions", 3)

        if self.positions:
            overnight_holds = []
            positions_to_close = []

            for symbol, pos in list(self.positions.items()):
                pnl_pct = pos.get("unrealized_pnl_pct", 0)
                in_profit = pnl_pct > 0

                # NEVER hold stock split candidates overnight
                if symbol in split_candidates:
                    log.warning(f"SPLIT BLOCK: Closing {symbol} - split candidate, no overnight hold")
                    positions_to_close.append(symbol)
                    continue

                # NEVER hold RVOL momentum plays overnight (these are intraday only)
                if pos.get("strategy") == "rvol_momentum":
                    log.info(f"RVOL EXIT: Closing {symbol} - RVOL momentum is intraday only")
                    positions_to_close.append(symbol)
                    continue

                should_hold = False
                if overnight_enabled and in_profit and len(overnight_holds) < max_overnight:
                    min_profit = overnight_cfg.get("min_profit_pct", 0.01)
                    if pnl_pct >= min_profit:
                        # Check if in uptrend (price above entry, which we already know if profitable)
                        should_hold = True

                        if overnight_cfg.get("require_uptrend", False) and self.market_data:
                            # Quick trend check: is price above 20-EMA?
                            data = self.market_data.get_data(symbol)
                            if data is not None and len(data) >= 20:
                                closes = data["close"].values
                                ema20 = self.indicators.ema(closes, 20)
                                if ema20 is not None and closes[-1] < ema20[-1]:
                                    should_hold = False  # Not in uptrend

                if should_hold:
                    # Tighten stop for overnight
                    tighten = overnight_cfg.get("tighten_stop_pct", 0.02)
                    current_price = pos.get("current_price", pos["entry_price"])
                    if pos["direction"] == "long":
                        new_stop = current_price * (1 - tighten)
                        if new_stop > pos.get("stop_loss", 0):
                            pos["stop_loss"] = new_stop
                    pos["overnight_hold"] = True
                    overnight_holds.append(symbol)
                    log.info(
                        f"OVERNIGHT HOLD: {symbol} | P&L: {pnl_pct:.1%} | "
                        f"Stop tightened to ${pos['stop_loss']:.2f}"
                    )
                elif overnight_cfg.get("close_losers", True) or not overnight_enabled:
                    positions_to_close.append(symbol)

            # Close positions not held overnight
            for symbol in positions_to_close:
                self._close_position(symbol, "eod_close", "End of day close")

            if overnight_holds:
                self.notifier.system_alert(
                    f"Holding {len(overnight_holds)} positions overnight: "
                    f"{', '.join(overnight_holds)}",
                    level="info"
                )

        # Calculate daily stats
        wins = [t for t in self.daily_trades if t.get("pnl", 0) > 0]
        total_trades = len(self.daily_trades)
        win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0

        regime = self.regime_detector.current_regime if self.regime_detector else "unknown"

        stats = {
            "date": datetime.now(self.tz).strftime("%Y-%m-%d"),
            "pnl": self.daily_pnl,
            "pnl_pct": (self.daily_pnl / self.start_of_day_balance * 100)
                       if self.start_of_day_balance > 0 else 0,
            "trades": total_trades,
            "win_rate": win_rate,
            "balance": self.current_balance,
            "open_positions": len(self.positions),
            "overnight_holds": len([p for p in self.positions.values() if p.get("overnight_hold")]),
            "regime": regime,
        }

        self.daily_stats.append(stats)
        self.notifier.daily_summary(stats)
        log.info(
            f"Day P&L: ${self.daily_pnl:+.2f} | Trades: {total_trades} | "
            f"Regime: {regime} | Overnight: {stats['overnight_holds']}"
        )

        # Run end-of-day learning analysis
        if self.trade_analyzer and len(self.trade_history) >= 5:
            analysis = self.trade_analyzer.analyze(self.trade_history, current_regime=regime)
            avoid = analysis.get("symbols_to_avoid", [])
            if avoid:
                log.info(f"LEARNING: Symbols to avoid: {[s['symbol'] for s in avoid]}")
            weight_adj = analysis.get("strategy_weight_adjustments", {})
            if weight_adj:
                log.info(f"LEARNING: Strategy weight adjustments: {weight_adj}")

        # Run auto-tune after EOD learning (this is where the bot improves itself)
        self._run_auto_tune()

    def _run_auto_tune(self):
        """Run autonomous parameter optimization using AI analysis."""
        if not self.auto_tuner or not self.auto_tuner.is_available():
            return

        log.info("=== AUTO-TUNE CYCLE ===")
        try:
            regime_data = None
            if self.regime_detector:
                regime_data = {
                    "regime": self.regime_detector.current_regime,
                    "confidence": self.regime_detector.regime_confidence,
                }

            strategy_scores = {}
            if self.trade_analyzer:
                strategy_scores = self.trade_analyzer.strategy_scores

            result = self.auto_tuner.run_auto_tune(
                trade_history=self.trade_history,
                performance_stats=self.performance_stats,
                strategy_scores=strategy_scores,
                regime_data=regime_data,
                notifier=self.notifier,
            )

            if result.get("applied"):
                # Reload strategy capital with new allocations
                alloc = self.config.strategy_allocation
                for name, strategy in self.strategies.items():
                    new_alloc = alloc.get(name, 0.25)
                    strategy.update_capital(self.current_balance * new_alloc)

                log.info(f"Auto-Tune applied {result['total_changes']} changes - strategies reloaded")
            else:
                log.info(f"Auto-Tune: {result.get('reason', 'no changes')}")

        except Exception as e:
            log.error(f"Auto-Tune error: {e}", exc_info=True)

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
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:
                pass

        if self.politician_tracker:
            self.politician_tracker.stop()

        if self.news_feed:
            self.news_feed.stop()

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
        status = {
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
            "traderspost_connected": self.tp_broker._connected if self.tp_broker else False,
            "traderspost_configured": self.tp_broker is not None,
            "traderspost_dual_mode": self.tp_broker.dual_mode if self.tp_broker else False,
            "traderspost_signals_sent": len(self.tp_broker.signal_history) if self.tp_broker else 0,
            "traderspost_last_signal": (
                self.tp_broker.signal_history[-1] if self.tp_broker and self.tp_broker.signal_history else None
            ),
            "execution_broker": (
                "IBKR" if (self.broker and self.broker.is_connected())
                else "TradersPost" if self.tp_broker
                else "Simulated"
            ),
            "politician_tracker": self.politician_tracker.get_status() if self.politician_tracker else None,
            "regime": self.regime_detector.get_status() if self.regime_detector else None,
            "hedging": self.hedging_manager.get_status() if self.hedging_manager else None,
            "learning": self.trade_analyzer.get_status() if self.trade_analyzer else None,
            "trading_profile": self.config.trading_profile,
            "data_source": self._get_data_source_info(),
        }
        return status

    def _get_data_source_info(self):
        """Get current data source status for dashboard."""
        streaming = (
            self.market_data and
            hasattr(self.market_data, '_streaming_active') and
            self.market_data._streaming_active
        )
        subscribed = len(self.market_data._subscribed_symbols) if streaming else 0

        if self.broker and self.broker.is_connected() and streaming:
            source = "IBKR Real-Time"
            status = "live"
        elif self.broker and self.broker.is_connected():
            source = "IBKR Historical"
            status = "connected"
        elif self.market_data and self.market_data.alpaca:
            source = "Alpaca"
            status = "connected"
        else:
            source = "Yahoo Finance"
            status = "delayed"

        return {
            "source": source,
            "status": status,
            "streaming": streaming,
            "subscribed_symbols": subscribed,
        }

    def get_editable_settings(self):
        """Return current settings for the config editor."""
        risk = self.config.risk_config
        schedule = self.config.schedule_config
        overnight = schedule.get("overnight", {})
        premarket = schedule.get("premarket", {})
        hedging = self.config.settings.get("hedging", {})
        return {
            "trading_profile": self.config.trading_profile,
            "profiles": {
                k: {"label": v["label"], "description": v["description"]}
                for k, v in self.config.TRADING_PROFILES.items()
            },
            "risk": {
                "stop_loss_pct": risk.get("stop_loss_pct", 0.03),
                "trailing_stop_pct": risk.get("trailing_stop_pct", 0.02),
                "take_profit_pct": risk.get("take_profit_pct", 0.06),
                "max_positions": risk.get("max_positions", 5),
                "risk_per_trade_pct": risk.get("risk_per_trade_pct", 0.01),
                "max_position_size_pct": risk.get("max_position_size_pct", 0.15),
            },
            "schedule": {
                "avoid_first_minutes": schedule.get("avoid_first_minutes", 30),
                "avoid_last_minutes": schedule.get("avoid_last_minutes", 30),
            },
            "overnight": {
                "enabled": overnight.get("enabled", False),
                "min_profit_pct": overnight.get("min_profit_pct", 0.01),
                "require_uptrend": overnight.get("require_uptrend", True),
                "max_overnight_positions": overnight.get("max_overnight_positions", 3),
            },
            "premarket": {
                "enabled": premarket.get("enabled", False),
                "start_time": premarket.get("start_time", "08:00"),
                "reduce_size_pct": premarket.get("reduce_size_pct", 0.5),
            },
            "hedging": {
                "enabled": hedging.get("enabled", True),
                "auto_hedge": hedging.get("auto_hedge", True),
                "max_hedge_pct": hedging.get("max_hedge_pct", 0.30),
            },
            "crypto": {
                "enabled": self.config.settings.get("crypto", {}).get("enabled", False),
                "risk": self.config.settings.get("crypto", {}).get("risk", {
                    "stop_loss_pct": 0.05,
                    "max_position_size_pct": 0.10,
                }),
            },
        }

    def apply_trading_profile(self, profile_name):
        """Apply a trading profile and save."""
        if self.config.apply_profile(profile_name):
            self.config.save_settings()
            log.info(f"Trading profile changed to: {profile_name}")
            return True
        return False

    def update_config_setting(self, path, value):
        """Update a single config setting and save."""
        self.config.update_setting(path, value)
        self.config.save_settings()
        log.info(f"Config updated: {path} = {value}")

    # =========================================================================
    # Dynamic Stock Discovery - Feed top movers into RVOL strategy
    # =========================================================================

    def _discover_dynamic_symbols(self):
        """
        Dynamically discover hot stocks from top movers and inject into
        the RVOL strategy for auto-trading. Runs every cycle.

        This is what makes it a true Money Machine: the bot finds the runners,
        not just your static watchlist.
        """
        rvol_strat = self.strategies.get("rvol_momentum")
        if not rvol_strat:
            return

        try:
            # Get top movers from Yahoo Finance (big gainers, trending)
            movers = self.get_top_movers()
            if movers:
                mover_symbols = []
                for m in movers:
                    sym = m.get("symbol", "")
                    price = m.get("price", 0)
                    change_pct = m.get("change_pct", 0)
                    # Filter: price > $2, big move, not already in watchlist
                    if sym and price >= 2.0 and change_pct >= 5.0:
                        mover_symbols.append(sym)

                if mover_symbols:
                    rvol_strat.add_dynamic_symbols(mover_symbols)
                    log.debug(f"Injected {len(mover_symbols)} movers into RVOL strategy")

            # Also check for low-float post-split runners
            runners = self.get_low_float_runners()
            if runners:
                runner_symbols = [r["symbol"] for r in runners if r.get("symbol")]
                if runner_symbols:
                    rvol_strat.add_dynamic_symbols(runner_symbols)
                    log.debug(f"Injected {len(runner_symbols)} low-float runners into RVOL strategy")

        except Exception as e:
            log.debug(f"Dynamic discovery error: {e}")

    # =========================================================================
    # Stock Split Detection - Avoid overnight holds on split candidates
    # =========================================================================

    def _check_split_candidates(self):
        """
        Check if any held symbols have upcoming stock splits.
        Returns set of symbols that are split candidates.

        Uses yfinance calendar data to detect upcoming splits.
        Split candidates should NOT be held overnight (extreme volatility risk).
        """
        split_candidates = set()

        try:
            import yfinance as yf
            from datetime import timedelta

            for symbol in self.positions:
                try:
                    ticker = yf.Ticker(symbol)
                    cal = getattr(ticker, "calendar", None)
                    if cal is not None and isinstance(cal, dict):
                        # Check for stock split events
                        splits = cal.get("Stock Splits", [])
                        if splits:
                            split_candidates.add(symbol)
                            log.info(f"SPLIT CANDIDATE: {symbol} has upcoming split")
                            continue

                    # Also check recent splits (within 5 days) - still volatile
                    actions = ticker.actions
                    if actions is not None and len(actions) > 0 and "Stock Splits" in actions.columns:
                        recent_splits = actions[actions["Stock Splits"] != 0]
                        if len(recent_splits) > 0:
                            last_split_date = recent_splits.index[-1]
                            if hasattr(last_split_date, "date"):
                                last_split_date = last_split_date.date()
                            days_since = (datetime.now().date() - last_split_date).days
                            if days_since <= 5:
                                split_candidates.add(symbol)
                                log.info(
                                    f"RECENT SPLIT: {symbol} split {days_since} days ago - "
                                    f"still volatile, avoid overnight"
                                )
                except Exception:
                    pass  # Skip if can't check

        except ImportError:
            log.debug("yfinance not available for split detection")

        self._split_candidates = split_candidates
        return split_candidates

    # =========================================================================
    # Low Float Post-Split Runner Scanner
    # =========================================================================

    def get_low_float_runners(self):
        """
        Scan for low-float stocks that recently went through reverse splits
        and are showing explosive volume. These are squeeze candidates.

        Like RIME +222%, JDZG +125% etc - typically low float + post-split
        + high RVOL = potential squeeze.

        Returns list of runner dicts sorted by change_pct descending.
        """
        import requests as _req

        runners = []

        try:
            # Yahoo Finance screener: small cap gainers with high volume
            url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
            params = {"scrIds": "small_cap_gainers", "count": 25}
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            resp = _req.get(url, params=params, headers=headers, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                results = data.get("finance", {}).get("result", [])
                if results:
                    quotes = results[0].get("quotes", [])
                    for q in quotes[:25]:
                        symbol = q.get("symbol", "")
                        if not symbol or "." in symbol:
                            continue

                        price = q.get("regularMarketPrice", 0)
                        change_pct = q.get("regularMarketChangePercent", 0)
                        volume = q.get("regularMarketVolume", 0)
                        avg_vol = q.get("averageDailyVolume3Month", 1)
                        market_cap = q.get("marketCap", 0)
                        shares_outstanding = q.get("sharesOutstanding", 0)
                        float_shares = q.get("floatShares", shares_outstanding)
                        name = q.get("shortName", symbol)

                        rvol = round(volume / avg_vol, 1) if avg_vol > 0 else 0

                        # Low float criteria:
                        # - Float < 20M shares (very low float)
                        # - Or float < 50M with high RVOL (moderately low float)
                        is_low_float = (
                            (float_shares > 0 and float_shares < 20_000_000) or
                            (float_shares > 0 and float_shares < 50_000_000 and rvol >= 3.0)
                        )

                        # Runner criteria: big move + volume
                        is_runner = (
                            change_pct >= 20.0 and  # At least +20% move
                            volume >= 500_000 and   # Decent volume
                            price >= 1.00 and       # Not a sub-penny
                            price <= 50.00           # Typically low-priced
                        )

                        if is_runner:
                            runner_type = "LOW FLOAT SQUEEZE" if is_low_float else "HIGH MOMENTUM"

                            # Check for split indicators
                            # Post-split stocks often have: low price + low float + extreme move
                            is_post_split = (
                                price < 10.0 and
                                float_shares > 0 and float_shares < 10_000_000 and
                                change_pct >= 50.0
                            )
                            if is_post_split:
                                runner_type = "POST-SPLIT SQUEEZE"

                            runners.append({
                                "symbol": symbol,
                                "name": name[:30],
                                "price": round(price, 2),
                                "change_pct": round(change_pct, 2),
                                "volume": volume,
                                "avg_volume": avg_vol,
                                "rvol": rvol,
                                "market_cap": market_cap,
                                "float_shares": float_shares,
                                "float_display": self._format_float(float_shares),
                                "shares_outstanding": shares_outstanding,
                                "runner_type": runner_type,
                                "is_low_float": is_low_float,
                                "is_post_split": is_post_split,
                                "on_watchlist": symbol in self.watchlist,
                            })

        except Exception as e:
            log.debug(f"Low float runner scan error: {e}")

        # Also scan day gainers for extreme movers
        try:
            url2 = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
            params2 = {"scrIds": "day_gainers", "count": 25}
            resp2 = _req.get(url2, params=params2, headers=headers, timeout=10)

            if resp2.status_code == 200:
                data2 = resp2.json()
                results2 = data2.get("finance", {}).get("result", [])
                if results2:
                    for q in results2[0].get("quotes", [])[:25]:
                        sym = q.get("symbol", "")
                        if not sym or "." in sym or any(r["symbol"] == sym for r in runners):
                            continue

                        price = q.get("regularMarketPrice", 0)
                        change_pct = q.get("regularMarketChangePercent", 0)
                        volume = q.get("regularMarketVolume", 0)
                        avg_vol = q.get("averageDailyVolume3Month", 1)
                        float_shares = q.get("floatShares", 0)

                        # Only add extreme movers not already captured
                        if change_pct >= 40.0 and price >= 1.0 and volume >= 300_000:
                            rvol = round(volume / avg_vol, 1) if avg_vol > 0 else 0
                            is_low_float = float_shares > 0 and float_shares < 20_000_000

                            runners.append({
                                "symbol": sym,
                                "name": q.get("shortName", sym)[:30],
                                "price": round(price, 2),
                                "change_pct": round(change_pct, 2),
                                "volume": volume,
                                "avg_volume": avg_vol,
                                "rvol": rvol,
                                "market_cap": q.get("marketCap", 0),
                                "float_shares": float_shares,
                                "float_display": self._format_float(float_shares),
                                "shares_outstanding": q.get("sharesOutstanding", 0),
                                "runner_type": "LOW FLOAT SQUEEZE" if is_low_float else "DAY RUNNER",
                                "is_low_float": is_low_float,
                                "is_post_split": False,
                                "on_watchlist": sym in self.watchlist,
                            })

        except Exception as e:
            log.debug(f"Day gainer runner scan error: {e}")

        runners.sort(key=lambda x: x["change_pct"], reverse=True)
        return runners

    @staticmethod
    def _format_float(float_shares):
        """Format float shares into readable string."""
        if not float_shares or float_shares <= 0:
            return "N/A"
        if float_shares >= 1_000_000_000:
            return f"{float_shares / 1_000_000_000:.1f}B"
        if float_shares >= 1_000_000:
            return f"{float_shares / 1_000_000:.1f}M"
        if float_shares >= 1_000:
            return f"{float_shares / 1_000:.0f}K"
        return str(int(float_shares))

    # =========================================================================
    # RVOL Scanner - Money Machine Style Relative Volume Scanner
    # =========================================================================

    def get_rvol_scan(self, min_rvol=2.0, extra_symbols=None):
        """
        Scan all watchlist symbols for high relative volume (RVOL).

        Inspired by Trade Ideas Money Machine system:
        - RVOL = current bar volume / average bar volume over 20 periods
        - Looks for stocks with unusual volume activity (RVOL >= 2x)
        - Combines with price action (gap %, range %, direction)
        - Sorts by highest RVOL first

        Returns list of dicts sorted by RVOL descending.
        """
        import numpy as np

        symbols_to_scan = list(set(self.watchlist + list(self.positions.keys())))
        if extra_symbols:
            symbols_to_scan = list(set(symbols_to_scan + extra_symbols))

        results = []

        for symbol in symbols_to_scan:
            try:
                bars = self.market_data.get_bars(symbol, 60) if self.market_data else None
                if bars is None or len(bars) < 25:
                    continue

                closes = bars["close"].values
                volumes = bars["volume"].values
                highs = bars["high"].values
                lows = bars["low"].values
                opens = bars["open"].values

                current_price = closes[-1]
                if current_price <= 0:
                    continue

                # --- RVOL Calculation (core of Money Machine) ---
                avg_vol_20 = float(np.mean(volumes[-21:-1])) if len(volumes) > 21 else float(np.mean(volumes[:-1]))
                current_vol = float(volumes[-1])
                rvol = round(current_vol / avg_vol_20, 2) if avg_vol_20 > 0 else 0

                # --- Price Action ---
                prev_close = closes[-2]
                gap_pct = round((opens[-1] - prev_close) / prev_close * 100, 2)
                change_pct = round((current_price - prev_close) / prev_close * 100, 2)
                day_range = highs[-1] - lows[-1]
                range_pct = round(day_range / current_price * 100, 2)

                # ATR for context
                atr = self.indicators.atr(highs, lows, closes, period=14)
                atr_pct = round(atr / current_price * 100, 2) if atr else 0

                # RSI
                rsi = self.indicators.rsi(closes, 14)

                # EMA trend
                ema9 = self.indicators.ema(closes, 9)
                ema20 = self.indicators.ema(closes, 20)
                trend = "BULL" if (ema9 is not None and ema20 is not None and ema9[-1] > ema20[-1]) else "BEAR"

                # MACD momentum
                macd_line, signal_line, histogram = self.indicators.macd(closes)
                macd_bullish = histogram is not None and len(histogram) > 0 and histogram[-1] > 0

                # Direction
                if change_pct > 0.3:
                    direction = "UP"
                elif change_pct < -0.3:
                    direction = "DOWN"
                else:
                    direction = "FLAT"

                # --- Money Machine Score ---
                # Combines RVOL with momentum indicators for a composite score
                score = 0
                if rvol >= 3.0:
                    score += 30
                elif rvol >= 2.0:
                    score += 20
                elif rvol >= 1.5:
                    score += 10

                if direction == "UP" and change_pct > 1.0:
                    score += 20
                elif direction == "UP":
                    score += 10

                if trend == "BULL":
                    score += 15

                if macd_bullish:
                    score += 10

                if 30 < rsi < 70:
                    score += 5  # Not overbought/oversold
                if rsi < 35:
                    score += 15  # Oversold bounce potential

                if gap_pct > 1.0:
                    score += 10  # Gap up = institutional interest

                # Verdict
                if rvol >= min_rvol and score >= 50:
                    verdict = "HOT"
                elif rvol >= min_rvol and score >= 30:
                    verdict = "ACTIVE"
                elif rvol >= 1.5:
                    verdict = "WARMING"
                else:
                    verdict = "QUIET"

                results.append({
                    "symbol": symbol,
                    "price": round(current_price, 2),
                    "rvol": rvol,
                    "current_vol": int(current_vol),
                    "avg_vol": int(avg_vol_20),
                    "change_pct": change_pct,
                    "gap_pct": gap_pct,
                    "range_pct": range_pct,
                    "direction": direction,
                    "trend": trend,
                    "rsi": round(rsi, 1),
                    "atr_pct": atr_pct,
                    "macd_bullish": macd_bullish,
                    "score": score,
                    "verdict": verdict,
                })

            except Exception as e:
                log.debug(f"RVOL scan error for {symbol}: {e}")

        # Sort by RVOL descending (highest volume activity first)
        results.sort(key=lambda x: x["rvol"], reverse=True)
        return results

    # =========================================================================
    # Top Movers Scanner - Find stocks making big moves outside your watchlist
    # =========================================================================

    def get_top_movers(self):
        """
        Fetch top gaining stocks from Yahoo Finance trending/movers.
        Catches 300%+ runners and hot movers you're not watching yet.

        Returns list of dicts: [{symbol, name, price, change_pct, volume, ...}]
        """
        import requests as _req

        movers = []

        # Yahoo Finance screener: day gainers
        try:
            url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
            params = {"scrIds": "day_gainers", "count": 25}
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            resp = _req.get(url, params=params, headers=headers, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                results = data.get("finance", {}).get("result", [])
                if results:
                    quotes = results[0].get("quotes", [])
                    for q in quotes[:20]:
                        symbol = q.get("symbol", "")
                        if not symbol or "." in symbol:  # Skip foreign tickers
                            continue
                        change_pct = q.get("regularMarketChangePercent", 0)
                        price = q.get("regularMarketPrice", 0)
                        volume = q.get("regularMarketVolume", 0)
                        avg_vol = q.get("averageDailyVolume3Month", 1)
                        rvol = round(volume / avg_vol, 1) if avg_vol > 0 else 0
                        name = q.get("shortName", symbol)

                        # Is this already in our watchlist?
                        on_watchlist = symbol in self.watchlist

                        movers.append({
                            "symbol": symbol,
                            "name": name[:30],
                            "price": round(price, 2),
                            "change_pct": round(change_pct, 2),
                            "volume": volume,
                            "avg_volume": avg_vol,
                            "rvol": rvol,
                            "market_cap": q.get("marketCap", 0),
                            "on_watchlist": on_watchlist,
                        })
        except Exception as e:
            log.debug(f"Top movers fetch error: {e}")

        # Also try trending tickers as fallback
        if len(movers) < 5:
            try:
                url2 = "https://query1.finance.yahoo.com/v1/finance/trending/US"
                resp2 = _req.get(url2, headers=headers, timeout=10)
                if resp2.status_code == 200:
                    data2 = resp2.json()
                    trending = data2.get("finance", {}).get("result", [])
                    if trending:
                        for t in trending[0].get("quotes", [])[:15]:
                            sym = t.get("symbol", "")
                            if sym and sym not in [m["symbol"] for m in movers] and "." not in sym:
                                # Get quote for this trending ticker
                                if self.market_data:
                                    quote = self.market_data.get_quote(sym)
                                    if quote and quote.get("price", 0) > 0:
                                        movers.append({
                                            "symbol": sym,
                                            "name": sym,
                                            "price": round(quote["price"], 2),
                                            "change_pct": round(quote.get("change_pct", 0), 2),
                                            "volume": 0,
                                            "avg_volume": 0,
                                            "rvol": 0,
                                            "market_cap": 0,
                                            "on_watchlist": sym in self.watchlist,
                                        })
            except Exception as e:
                log.debug(f"Trending fetch error: {e}")

        # Sort by change % descending
        movers.sort(key=lambda x: x["change_pct"], reverse=True)
        return movers

    # =========================================================================
    # Trade Suggestion Engine - Suggests specific trades with profit reasoning
    # =========================================================================

    def get_trade_suggestions(self, max_suggestions=5):
        """
        Generate actionable LONG trade suggestions with profit reasoning.

        Analyzes all scanner data + RVOL + technical levels to suggest
        the best trades right now. Each suggestion includes:
        - Entry price, stop loss, take profit
        - Expected profit $ and %
        - Risk/reward ratio
        - Confidence score
        - WHY this trade (detailed reasoning)

        Returns list of suggestion dicts.
        """
        import numpy as np

        suggestions = []
        scanner_data = self.get_scanner_data()
        rvol_data = self.get_rvol_scan(min_rvol=1.3)
        rvol_map = {r["symbol"]: r for r in rvol_data}

        # Collect all symbols with scanner verdicts leaning bullish
        candidates = []

        for strat_name, strat_data in scanner_data.items():
            for symbol, data in strat_data.items():
                if data.get("status") in ("no_data", "error", "low_volatility"):
                    continue

                verdict = (data.get("verdict") or "").upper()
                # Only consider bullish setups (LONG-only)
                if verdict in ("BUY SIGNAL", "A+ SETUP", "BUY ZONE", "ENTRY SIGNAL", "WARMING UP", "BUILDING"):
                    candidates.append({
                        "symbol": symbol if "/" not in symbol else symbol.split("/")[0],
                        "strategy": strat_name,
                        "verdict": verdict,
                        "data": data,
                    })

        # Also consider high-RVOL stocks even without scanner signal
        for r in rvol_data:
            if r["rvol"] >= 2.0 and r["direction"] == "UP" and r["trend"] == "BULL":
                already = any(c["symbol"] == r["symbol"] for c in candidates)
                if not already:
                    candidates.append({
                        "symbol": r["symbol"],
                        "strategy": "rvol_momentum",
                        "verdict": "RVOL SURGE",
                        "data": r,
                    })

        # Skip symbols we already hold
        candidates = [c for c in candidates if c["symbol"] not in self.positions]

        # Score and build suggestions
        for cand in candidates:
            symbol = cand["symbol"]
            strat = cand["strategy"]
            data = cand["data"]
            verdict = cand["verdict"]

            try:
                bars = self.market_data.get_bars(symbol, 60) if self.market_data else None
                if bars is None or len(bars) < 20:
                    continue

                closes = bars["close"].values
                highs = bars["high"].values
                lows = bars["low"].values
                current_price = float(closes[-1])

                if current_price <= 0:
                    continue

                # Get RVOL data for this symbol
                rvol_info = rvol_map.get(symbol, {})
                rvol = rvol_info.get("rvol", 1.0)

                # Calculate levels
                atr = self.indicators.atr(highs, lows, closes, period=14)
                if atr is None or atr <= 0:
                    continue

                rsi = self.indicators.rsi(closes, 14)

                # Strategy-specific entry logic
                reasons = []
                confidence = 0.5
                stop_loss = current_price - (2.0 * atr)
                take_profit = current_price + (3.0 * atr)

                if strat == "mean_reversion":
                    checks = data.get("checks", {})
                    if checks.get("zscore_ok"):
                        reasons.append(f"Z-Score oversold ({data.get('zscore', 0):.1f})")
                        confidence += 0.1
                    if checks.get("rsi_oversold"):
                        reasons.append(f"RSI oversold ({data.get('rsi', 50):.0f})")
                        confidence += 0.1
                    if checks.get("at_lower_bb"):
                        reasons.append("At lower Bollinger Band (bounce zone)")
                        confidence += 0.1
                    sma = data.get("sma", current_price)
                    if sma and sma > current_price:
                        take_profit = sma  # Target the mean
                        reasons.append(f"Target: mean reversion to ${sma:.2f}")
                    stop_loss = current_price * 0.97

                elif strat == "momentum":
                    checks = data.get("checks", {})
                    if checks.get("ema_cross"):
                        reasons.append("Fresh EMA crossover (bullish)")
                        confidence += 0.15
                    elif checks.get("ema_bullish"):
                        reasons.append("EMAs aligned bullish")
                        confidence += 0.05
                    if checks.get("strong_trend"):
                        reasons.append(f"Strong trend (ADX={data.get('adx', 0):.0f})")
                        confidence += 0.1
                    if checks.get("breakout"):
                        reasons.append(f"Breakout above ${data.get('breakout_level', 0):.2f}")
                        confidence += 0.15
                    if checks.get("vol_confirmed"):
                        reasons.append(f"Volume confirmed ({data.get('vol_ratio', 0):.1f}x avg)")
                        confidence += 0.05
                    stop_loss = current_price - (2.0 * atr)
                    take_profit = current_price + (4.0 * atr)

                elif strat == "vwap_scalp":
                    zone = data.get("zone", "")
                    vwap = data.get("vwap", current_price)
                    if "BELOW" in zone:
                        reasons.append(f"Below VWAP (${vwap:.2f}) - bounce setup")
                        confidence += 0.1
                    reasons.append(f"VWAP distance: {data.get('vwap_dist_pct', 0):.2f}%")
                    take_profit = vwap if vwap > current_price else current_price + atr
                    stop_loss = current_price - (1.5 * atr)

                elif strat == "smc_forever":
                    checks = data.get("checks", {})
                    if checks.get("sweep"):
                        reasons.append("Liquidity sweep detected (smart money)")
                        confidence += 0.15
                    if checks.get("smt"):
                        reasons.append("SMT divergence confirmed")
                        confidence += 0.1
                    if checks.get("cisd"):
                        reasons.append("Change in delivery (bullish shift)")
                        confidence += 0.1
                    if checks.get("fvg"):
                        reasons.append("Fair Value Gap entry zone")
                        confidence += 0.1
                    if checks.get("displacement"):
                        reasons.append("Institutional displacement candle")
                        confidence += 0.05
                    stop_loss = current_price - (2.5 * atr)
                    take_profit = current_price + (5.0 * atr)

                elif strat == "rvol_momentum":
                    reasons.append(f"RVOL surge: {rvol:.1f}x average volume")
                    reasons.append(f"Price moving {data.get('direction', 'UP')} with momentum")
                    if data.get("macd_bullish"):
                        reasons.append("MACD histogram positive")
                        confidence += 0.1
                    if data.get("gap_pct", 0) > 0.5:
                        reasons.append(f"Gap up +{data.get('gap_pct', 0):.1f}% (institutional interest)")
                        confidence += 0.1
                    stop_loss = current_price - (2.0 * atr)
                    take_profit = current_price + (3.0 * atr)

                # RVOL bonus for any strategy
                if rvol >= 2.0:
                    reasons.append(f"High RVOL ({rvol:.1f}x) - unusual volume activity")
                    confidence += 0.1
                elif rvol >= 1.5:
                    reasons.append(f"Elevated RVOL ({rvol:.1f}x)")
                    confidence += 0.05

                confidence = min(1.0, confidence)

                # Risk/reward calculation
                risk_per_share = current_price - stop_loss
                reward_per_share = take_profit - current_price
                rr_ratio = round(reward_per_share / risk_per_share, 2) if risk_per_share > 0 else 0

                # Position sizing (what we'd actually trade)
                qty = 0
                if self.position_sizer and risk_per_share > 0:
                    alloc = self.config.strategy_allocation.get(strat, 0.2)
                    qty = self.position_sizer.calculate(
                        balance=self.current_balance,
                        price=current_price,
                        stop_loss=stop_loss,
                        strategy_allocation=alloc
                    )

                potential_profit = round(reward_per_share * qty, 2) if qty > 0 else round(reward_per_share, 2)
                potential_loss = round(risk_per_share * qty, 2) if qty > 0 else round(risk_per_share, 2)
                profit_pct = round(reward_per_share / current_price * 100, 2)
                risk_pct = round(risk_per_share / current_price * 100, 2)

                # Build the "why" narrative
                if not reasons:
                    reasons.append(f"{verdict} signal from {strat} strategy")

                suggestion = {
                    "symbol": symbol,
                    "action": "BUY",
                    "strategy": strat,
                    "verdict": verdict,
                    "price": round(current_price, 2),
                    "stop_loss": round(stop_loss, 2),
                    "take_profit": round(take_profit, 2),
                    "quantity": qty,
                    "confidence": round(confidence, 2),
                    "rr_ratio": rr_ratio,
                    "potential_profit": potential_profit,
                    "potential_loss": potential_loss,
                    "profit_pct": profit_pct,
                    "risk_pct": risk_pct,
                    "rvol": rvol,
                    "rsi": round(rsi, 1),
                    "atr": round(atr, 2),
                    "reasons": reasons,
                    "why": " | ".join(reasons),
                }

                suggestions.append(suggestion)

            except Exception as e:
                log.debug(f"Suggestion error for {symbol}: {e}")

        # Sort by confidence * RR ratio (best risk-adjusted opportunities first)
        suggestions.sort(
            key=lambda x: x["confidence"] * max(x["rr_ratio"], 0.1),
            reverse=True
        )

        return suggestions[:max_suggestions]

    def get_swing_scanner(self):
        """
        Scan the market for swing trade opportunities (multi-day holds).

        Analyzes watchlist + top movers for:
        - Weekly/daily trend (50/200 EMA)
        - Support/resistance levels
        - Suggested hold period
        - Profit target with reasoning

        Returns list of swing trade dicts.
        """
        import numpy as np

        results = []
        if not self.market_data:
            return results

        # Scan watchlist + any high-RVOL additions
        symbols = list(set(self.watchlist))

        for symbol in symbols:
            try:
                bars = self.market_data.get_bars(symbol, 200)
                if bars is None or len(bars) < 50:
                    continue

                closes = bars["close"].values.astype(float)
                highs = bars["high"].values.astype(float)
                lows = bars["low"].values.astype(float)
                volumes = bars["volume"].values.astype(float) if "volume" in bars else None

                current_price = float(closes[-1])
                if current_price <= 0:
                    continue

                # --- Trend Analysis ---
                ema20 = self.indicators.ema(closes, 20)
                ema50 = self.indicators.ema(closes, 50)
                ema200 = self.indicators.ema(closes, 200) if len(closes) >= 200 else None

                # Determine trend
                if ema200 is not None:
                    if current_price > ema50 > ema200:
                        trend = "STRONG UPTREND"
                        trend_score = 3
                    elif current_price > ema200:
                        trend = "UPTREND"
                        trend_score = 2
                    elif current_price < ema50 < ema200:
                        trend = "STRONG DOWNTREND"
                        trend_score = -2
                    elif current_price < ema200:
                        trend = "DOWNTREND"
                        trend_score = -1
                    else:
                        trend = "SIDEWAYS"
                        trend_score = 0
                else:
                    if current_price > ema50:
                        trend = "UPTREND"
                        trend_score = 2
                    elif current_price < ema50:
                        trend = "DOWNTREND"
                        trend_score = -1
                    else:
                        trend = "SIDEWAYS"
                        trend_score = 0

                # --- RSI ---
                rsi = self.indicators.rsi(closes, 14)

                # --- ATR for volatility ---
                atr = self.indicators.atr(highs, lows, closes, period=14)
                if atr is None or atr <= 0:
                    continue
                atr_pct = round(atr / current_price * 100, 2)

                # --- Support / Resistance (recent swing highs/lows) ---
                lookback = min(60, len(lows))
                recent_lows = lows[-lookback:]
                recent_highs = highs[-lookback:]
                support = float(np.min(recent_lows))
                resistance = float(np.max(recent_highs))

                # Nearest support: lowest low in last 20 bars
                near_support = float(np.min(lows[-20:]))
                # Nearest resistance: highest high in last 20 bars
                near_resistance = float(np.max(highs[-20:]))

                dist_to_support_pct = round((current_price - near_support) / current_price * 100, 2)
                dist_to_resistance_pct = round((near_resistance - current_price) / current_price * 100, 2)

                # --- Volume trend ---
                vol_rising = False
                if volumes is not None and len(volumes) >= 20:
                    avg_vol_10 = float(np.mean(volumes[-10:]))
                    avg_vol_20 = float(np.mean(volumes[-20:]))
                    vol_rising = avg_vol_10 > avg_vol_20 * 1.2

                # --- Scoring & Recommendation ---
                score = 0
                reasons = []

                # Trend points
                if trend_score >= 2:
                    score += 30
                    reasons.append(f"{trend} - price above key moving averages")
                elif trend_score == -1:
                    reasons.append("Downtrend - wait for reversal confirmation")

                # RSI oversold bounce opportunity
                if rsi < 35:
                    score += 25
                    reasons.append(f"RSI oversold ({rsi:.0f}) - bounce likely")
                elif rsi < 45 and trend_score >= 1:
                    score += 15
                    reasons.append(f"RSI pulling back ({rsi:.0f}) in uptrend - buy the dip")
                elif rsi > 70:
                    score -= 10
                    reasons.append(f"RSI overbought ({rsi:.0f}) - wait for pullback")

                # Near support = good entry
                if dist_to_support_pct < 3:
                    score += 20
                    reasons.append(f"Near support (${near_support:.2f}) - {dist_to_support_pct:.1f}% away")
                elif dist_to_support_pct < 5:
                    score += 10
                    reasons.append(f"Close to support (${near_support:.2f})")

                # Volume rising
                if vol_rising:
                    score += 10
                    reasons.append("Volume increasing - confirms momentum")

                # Above 200 EMA
                if ema200 is not None and current_price > ema200:
                    score += 10
                    reasons.append(f"Above 200 EMA (${ema200:.2f}) - long-term bullish")

                # --- Calculate targets and hold period ---
                # Target: next resistance or 2-3x ATR above
                profit_target = max(near_resistance, current_price + 3 * atr)
                stop_loss = max(near_support - atr * 0.5, current_price - 2.5 * atr)

                profit_pct = round((profit_target - current_price) / current_price * 100, 2)
                risk_pct = round((current_price - stop_loss) / current_price * 100, 2)
                rr_ratio = round(profit_pct / risk_pct, 2) if risk_pct > 0 else 0

                # Estimated hold: distance to target / avg daily move
                avg_daily_move = atr_pct
                if avg_daily_move > 0:
                    est_hold_days = max(2, min(30, round(profit_pct / avg_daily_move)))
                else:
                    est_hold_days = 7

                # Hold period description
                if est_hold_days <= 5:
                    hold_desc = f"{est_hold_days} days (short swing)"
                elif est_hold_days <= 14:
                    hold_desc = f"{est_hold_days} days (swing trade)"
                else:
                    hold_desc = f"{est_hold_days} days (position trade)"

                # Only include if score >= 30 (decent opportunity)
                if score < 30:
                    continue

                # Rating
                if score >= 70:
                    rating = "STRONG BUY"
                elif score >= 50:
                    rating = "BUY"
                elif score >= 30:
                    rating = "WATCH"
                else:
                    continue

                results.append({
                    "symbol": symbol,
                    "price": round(current_price, 2),
                    "rating": rating,
                    "score": score,
                    "trend": trend,
                    "rsi": round(rsi, 1),
                    "atr_pct": atr_pct,
                    "support": round(near_support, 2),
                    "resistance": round(near_resistance, 2),
                    "profit_target": round(profit_target, 2),
                    "stop_loss": round(stop_loss, 2),
                    "profit_pct": profit_pct,
                    "risk_pct": risk_pct,
                    "rr_ratio": rr_ratio,
                    "hold_period": hold_desc,
                    "hold_days": est_hold_days,
                    "vol_rising": vol_rising,
                    "ema50": round(ema50, 2),
                    "ema200": round(ema200, 2) if ema200 is not None else None,
                    "reasons": reasons,
                })

            except Exception as e:
                log.debug(f"Swing scanner error for {symbol}: {e}")

        # Sort by score descending
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:15]
