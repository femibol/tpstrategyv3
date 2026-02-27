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
from bot.strategies.rvol_scalp import RvolScalpStrategy
from bot.strategies.prebreakout import PreBreakoutStrategy
from bot.strategies.premarket_gap import PreMarketGapStrategy
from bot.strategies.options_momentum import OptionsMomentumStrategy
from bot.strategies.short_squeeze import ShortSqueezeStrategy
from bot.strategies.pead import PEADStrategy
from bot.strategies.momentum_runner import MomentumRunnerStrategy
from bot.data.polygon_scanner import PolygonScanner
from bot.learning.trade_analyzer import TradeAnalyzer
from bot.learning.ai_insights import AIInsights
from bot.learning.auto_tuner import AutoTuner
from bot.signals.regime_detector import RegimeDetector
from bot.risk.hedging import HedgingManager
from bot.integrations.google_sheets import GoogleSheetsLogger
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
        self.sheets_logger = None
        self.scheduler = None

        # State — protected by _positions_lock for thread safety
        # (BackgroundScheduler, webhook handlers, and main loop all access self.positions)
        self.positions = {}
        self._positions_lock = threading.Lock()
        self._closing_in_progress = set()  # Prevents double-close from concurrent monitors
        self.orders = {}
        self.strategies = {}
        self.daily_trades = []
        self.daily_pnl = 0.0
        self.peak_balance = self.config.starting_balance
        self.current_balance = self.config.starting_balance
        self.start_of_day_balance = self.config.starting_balance

        # Signal deduplication - prevent duplicate entries
        self._signal_cooldowns = {}  # {symbol: last_signal_datetime}
        self._signal_cooldown_secs = 30  # Min seconds between signals for same symbol (was 60, too tight for ~60s scan cycle)
        self._pending_orders = set()  # Symbols with orders currently in-flight

        # Exit cooldown - prevent re-closing recently closed positions
        # Tracks {symbol: close_datetime} to block re-entry via Alpaca sync
        self._recently_closed = {}  # {symbol: datetime when closed}
        self._exit_cooldown_secs = 300  # 5 minutes: don't re-add/re-close within this window

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
        self._in_postmarket = False
        self._equity_market_open = False

    def initialize(self):
        """Initialize all components."""
        log.info("=" * 60)
        log.info(f"ALGOBOT v1.0 - {self.config.mode.upper()} MODE")
        log.info(f"Config Starting Capital: ${self.config.starting_balance:,.2f}")
        log.info("=" * 60)

        # Notifications
        self.notifier = Notifier(self.config)

        # Connect to IBKR (also syncs Alpaca balance + positions on Render)
        self._connect_broker()

        # Log actual balance after broker/Alpaca sync
        log.info(f"Active Capital: ${self.current_balance:,.2f} (after broker sync)")
        self.notifier.system_alert(
            f"Bot starting in {self.config.mode.upper()} mode "
            f"with ${self.current_balance:,.2f}",
            level="success"
        )

        # Initialize risk management
        self.risk_manager = RiskManager(self.config, self.notifier)
        self.position_sizer = PositionSizer(self.config)

        # Apply scaling tier based on actual balance (after risk manager init)
        scaling_tier = self.config.get_scaling_tier(self.current_balance)
        if scaling_tier:
            self.risk_manager.update_tier(scaling_tier)
            self.position_sizer.update_tier(scaling_tier)
            log.info(
                f"Scaling tier active: balance >= ${scaling_tier['min_balance']:,} | "
                f"max_positions={scaling_tier['max_positions']} | "
                f"risk_per_trade={scaling_tier['risk_per_trade']:.1%} | "
                f"max_position_pct={scaling_tier['max_position_pct']:.0%}"
            )

        # Polygon.io — scanning + fallback data source
        self.polygon = PolygonScanner(self.config.polygon_api_key)

        # Market data feed (IBKR primary, Polygon fallback, Yahoo last resort)
        self.market_data = MarketDataFeed(self.config, self.broker, polygon=self.polygon)

        # Start IBKR real-time streaming if connected
        if self.broker and self.broker.is_connected():
            all_symbols = list(set(self.watchlist))
            self.market_data.start_streaming(all_symbols)
            log.info("IBKR real-time streaming initialized for watchlist")

            # Subscribe to real-time account PnL (native IBKR — more accurate than manual calc)
            if hasattr(self.broker, 'subscribe_account_pnl'):
                self.broker.subscribe_account_pnl()

            # Subscribe to tick-by-tick data for held positions (fastest possible —
            # fires on every trade print, not aggregated 5-sec bars)
            scalp_symbols = list(self.positions.keys())[:10]
            if scalp_symbols and hasattr(self.broker, 'subscribe_tick_by_tick'):
                self.broker.subscribe_tick_by_tick(scalp_symbols, self._on_tick)
                # Also keep 5-sec bars as backup for volume analysis
                if hasattr(self.broker, 'subscribe_realtime_bars_with_callback'):
                    self.broker.subscribe_realtime_bars_with_callback(
                        scalp_symbols, self._on_5sec_bar
                    )
            elif scalp_symbols and hasattr(self.broker, 'subscribe_realtime_bars_with_callback'):
                # Fallback: 5-sec bars if tick-by-tick unavailable
                self.broker.subscribe_realtime_bars_with_callback(
                    scalp_symbols, self._on_5sec_bar
                )

        # Load trading universe (scanner-driven — may be empty if no static list)
        self.universe = self.config.get_universe()
        log.info(f"Trading universe loaded: {len(self.universe)} symbols")

        # Load strategies
        self._load_strategies()

        # Inject full universe into RVOL strategies for broad scanning
        self._inject_universe_into_strategies()

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

        # News-driven trading: Polygon (polled) + IBKR (real-time ticks)
        has_polygon = bool(self.config.polygon_api_key)
        has_ibkr = self.broker and self.broker.is_connected()
        if has_polygon or has_ibkr:
            self.news_feed = NewsFeed(
                self.config,
                callback=self._handle_news_signal,
                polygon_api_key=self.config.polygon_api_key,
                broker=self.broker,
            )
            sources = []
            if has_polygon:
                sources.append("Polygon")
            if has_ibkr:
                sources.append("IBKR")
            log.info(f"News catalyst scanner ENABLED ({' + '.join(sources)})")

        # Trade learning system
        self.trade_analyzer = TradeAnalyzer(self.config)
        log.info("Trade learning system enabled")

        # Google Sheets trade logging
        self.sheets_logger = GoogleSheetsLogger(self.config)
        if self.sheets_logger.is_enabled():
            log.info("Google Sheets trade logging ENABLED")

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
        """Connect to IBKR as primary broker/data source."""
        self.broker = IBKRBroker(self.config)

        # Attempt IBKR connection with retry (works locally or with remote Gateway)
        connected = False
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            connected = self.broker.connect()
            if connected:
                break
            if attempt < max_retries:
                wait = 2 ** attempt
                log.warning(f"IBKR connection attempt {attempt}/{max_retries} failed - retrying in {wait}s...")
                import time as _time
                _time.sleep(wait)

        if connected:
            log.info(f"Connected to IBKR ({self.config.mode} mode) - using as primary data source")
            # Sync account state
            account = self.broker.get_account_summary()
            if account:
                self.current_balance = account.get("net_liquidation", self.config.starting_balance)
                self.peak_balance = max(self.peak_balance, self.current_balance)
                log.info(f"Account balance: ${self.current_balance:,.2f}")
            # Sync existing positions
            raw_positions = self.broker.get_positions()
            if raw_positions:
                now = datetime.now(self.tz)
                # Check for pending sell orders at IBKR to avoid syncing
                # positions that are in the process of being closed
                pending_sell_symbols = set()
                try:
                    open_trades = self.broker.ib.openTrades()
                    for t in open_trades:
                        if (t.order.action.upper() == "SELL" and
                                t.orderStatus.status in ("PreSubmitted", "Submitted")):
                            pending_sell_symbols.add(t.contract.symbol)
                    if pending_sell_symbols:
                        log.info(
                            f"IBKR SYNC: Found pending SELL orders for: "
                            f"{', '.join(pending_sell_symbols)} — will skip these"
                        )
                except Exception as e:
                    log.debug(f"Could not check IBKR pending orders: {e}")

                for sym, pos in raw_positions.items():
                    entry = pos.get("entry_price", pos.get("avg_cost", 0))
                    side = pos.get("direction", "long")
                    qty = pos.get("quantity", 0)

                    # Auto-close short positions at IBKR — long-only bot should NEVER have shorts
                    if side == "short":
                        log.error(
                            f"SHORT DETECTED at IBKR: {sym} ({qty} shares). "
                            f"Auto-covering — long-only bot must not hold shorts."
                        )
                        try:
                            self.broker.place_order(sym, "BUY", qty, "MARKET")
                            log.info(f"AUTO-COVERED IBKR short: {sym}")
                        except Exception as e:
                            log.error(f"Failed to auto-cover IBKR short {sym}: {e}")
                        continue

                    # Skip positions with pending sell orders — they're being closed
                    if sym in pending_sell_symbols:
                        log.info(
                            f"IBKR SYNC: Skipping {sym} — has pending SELL order "
                            f"(position is being closed from previous session)"
                        )
                        continue

                    stop_pct = self.config.risk_config.get("stop_loss_pct", 0.03)
                    tp_pct = self.config.risk_config.get("take_profit_pct", 0.20)
                    self.positions[sym] = {
                        **pos,
                        "entry_time": now,
                        "stop_loss": pos.get("stop_loss", entry * (1 - stop_pct)),
                        "take_profit": pos.get("take_profit", entry * (1 + tp_pct)),
                        "trailing_stop_pct": self.config.risk_config.get("trailing_stop_pct", 0.02),
                        "strategy": pos.get("strategy", "synced_from_ibkr"),
                        "executed_via": pos.get("executed_via", "IBKR"),
                        "max_hold_bars": 40,
                        "bar_seconds": 300,
                        "max_hold_days": 5,
                    }
                log.info(f"Synced {len(self.positions)} LONG positions from IBKR")
        else:
            log.warning(
                "IBKR connection failed after %d attempts - falling back to Polygon/Yahoo for data. "
                "Ensure IB Gateway is running and IBKR_HOST/IBKR_PORT are set correctly.",
                max_retries,
            )
            # On Render or when IBKR unavailable, sync positions from Alpaca
            if os.environ.get("RENDER") or self.config.alpaca_api_key:
                self._sync_positions_from_alpaca()

    def _load_strategies(self):
        """Load and initialize all enabled strategies."""
        strat_configs = {
            "mean_reversion": MeanReversionStrategy,
            "momentum": MomentumStrategy,
            "vwap_scalp": VWAPScalpStrategy,
            "pairs_trading": PairsTradingStrategy,
            "smc_forever": SMCForeverStrategy,
            "rvol_momentum": RvolMomentumStrategy,
            "rvol_scalp": RvolScalpStrategy,
            "prebreakout": PreBreakoutStrategy,
            "premarket_gap": PreMarketGapStrategy,
            "options_momentum": OptionsMomentumStrategy,
            "short_squeeze": ShortSqueezeStrategy,
            "pead": PEADStrategy,
            "momentum_runner": MomentumRunnerStrategy,
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

    def _inject_universe_into_strategies(self):
        """Inject the full trading universe into RVOL and momentum strategies.
        This gives them 200+ symbols to scan instead of just 20-30."""
        if not self.universe:
            return

        rvol_strat = self.strategies.get("rvol_momentum")
        scalp_strat = self.strategies.get("rvol_scalp")
        momentum_strat = self.strategies.get("momentum")

        universe_count = len(self.universe)

        if rvol_strat and hasattr(rvol_strat, "add_dynamic_symbols"):
            rvol_strat.add_dynamic_symbols(self.universe)
            log.info(f"Injected {universe_count} universe symbols into RVOL momentum")

        if scalp_strat and hasattr(scalp_strat, "add_dynamic_symbols"):
            scalp_strat.add_dynamic_symbols(self.universe)
            log.info(f"Injected {universe_count} universe symbols into RVOL scalp")

        prebreakout_strat = self.strategies.get("prebreakout")
        if prebreakout_strat and hasattr(prebreakout_strat, "add_dynamic_symbols"):
            prebreakout_strat.add_dynamic_symbols(self.universe)
            log.info(f"Injected {universe_count} universe symbols into pre-breakout")

        gap_strat = self.strategies.get("premarket_gap")
        if gap_strat and hasattr(gap_strat, "add_dynamic_symbols"):
            gap_strat.add_dynamic_symbols(self.universe)
            log.info(f"Injected {universe_count} universe symbols into pre-market gap")

        squeeze_strat = self.strategies.get("short_squeeze")
        if squeeze_strat and hasattr(squeeze_strat, "add_dynamic_symbols"):
            squeeze_strat.add_dynamic_symbols(self.universe)
            log.info(f"Injected {universe_count} universe symbols into short squeeze")

        pead_strat = self.strategies.get("pead")
        if pead_strat and hasattr(pead_strat, "add_dynamic_symbols"):
            pead_strat.add_dynamic_symbols(self.universe)
            log.info(f"Injected {universe_count} universe symbols into PEAD")

        runner_strat = self.strategies.get("momentum_runner")
        if runner_strat and hasattr(runner_strat, "add_dynamic_symbols"):
            runner_strat.add_dynamic_symbols(self.universe)
            log.info(f"Injected {universe_count} universe symbols into momentum runner")

        # Momentum strategy uses static symbols, so we extend the list directly
        if momentum_strat:
            existing = set(s.upper() for s in momentum_strat.symbols)
            new_syms = [s for s in self.universe if s.upper() not in existing]
            momentum_strat.symbols.extend(new_syms)
            log.info(f"Extended momentum strategy to {len(momentum_strat.symbols)} symbols")

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
        try:
            self.initialize()
        except Exception as e:
            log.error(f"INIT FAILED: {e}", exc_info=True)
            raise

        self.running = True
        log.info("Engine state set to RUNNING")

        # Handle graceful shutdown (only works in main thread)
        try:
            signal.signal(signal.SIGINT, self._shutdown)
            signal.signal(signal.SIGTERM, self._shutdown)
        except (ValueError, OSError) as e:
            # Running in a background thread (e.g., Render/gunicorn)
            log.info(f"Signal handlers skipped (background thread): {e}")

        # Start auxiliary services — each wrapped so one failure doesn't kill the engine
        try:
            self.scheduler.start()
            log.info("Scheduler started")
        except Exception as e:
            log.error(f"Scheduler failed to start: {e}", exc_info=True)

        if self.tv_receiver:
            try:
                tv_thread = threading.Thread(
                    target=self.tv_receiver.start,
                    daemon=True
                )
                tv_thread.start()
            except Exception as e:
                log.error(f"TradingView receiver failed: {e}")

        if self.politician_tracker:
            try:
                self.politician_tracker.start()
            except Exception as e:
                log.error(f"Politician tracker failed to start: {e}")

        if self.news_feed:
            try:
                self.news_feed.start()
            except Exception as e:
                log.error(f"News feed failed to start: {e}")

        log.info("Trading engine started - entering main loop")
        try:
            self.notifier.system_alert("Trading engine started", level="success")
        except Exception as e:
            log.error(f"Notification failed: {e}")

        # Run initial scan immediately so dashboard has data on load
        self._run_scanner_cycle()
        log.info("Initial scan complete - main loop starting")

        try:
            self._main_loop()
        except Exception as e:
            log.error(f"MAIN LOOP CRASHED: {e}", exc_info=True)
            try:
                self.notifier.system_alert(f"Engine error: {e}", level="error")
            except Exception:
                pass
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
        """Main trading loop - runs continuously during market hours.

        Two-speed loop:
        - Full cycle every 10s: data update + strategies + signals
        - Fast scalp monitor every 3s: price refresh + position exits
        """
        scan_timer = 0
        scalp_tick = 0  # Sub-loop counter for fast scalp monitoring
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

                # --- FAST SCALP MONITOR (every 3 seconds) ---
                # Refresh prices for open positions and check for quick exits
                scalp_tick += 1
                if self.positions:
                    self._fast_scalp_monitor()

                # --- FULL CYCLE (every ~10 seconds = 3 fast ticks) ---
                if scalp_tick >= 3:
                    scalp_tick = 0

                    # 0. Process startup trim queue (close excess positions from Alpaca sync)
                    if hasattr(self, '_startup_trim_queue') and self._startup_trim_queue:
                        trim_batch = self._startup_trim_queue[:3]  # Close 3 at a time
                        self._startup_trim_queue = self._startup_trim_queue[3:]
                        for sym in trim_batch:
                            if sym in self.positions:
                                pnl_pct = self.positions[sym].get("unrealized_pnl_pct", 0)
                                log.warning(
                                    f"STARTUP TRIM: Closing {sym} (P&L: {pnl_pct:.1%}) "
                                    f"— over position limit. "
                                    f"{len(self._startup_trim_queue)} remaining in queue."
                                )
                                self._close_position(sym, "position_cap",
                                                     f"Over position limit — closing weakest")

                    # 0a. Dynamic discovery: feed top movers into RVOL strategies
                    self._discover_dynamic_symbols()

                    # 0a2. Prune stale dynamic symbols (30 min TTL)
                    # Symbols still actively moving get refreshed each cycle;
                    # dead symbols that stopped appearing as movers get pruned
                    self._prune_stale_dynamic_symbols()

                    # 0b. Update news feed watchlist with held + active symbols
                    if self.news_feed:
                        news_watch = list(set(
                            list(self.positions.keys()) + self.watchlist[:20]
                        ))
                        self.news_feed.update_watchlist(news_watch)

                    # 1. Update market data (standard 5-min + 1-min for scalps)
                    self._update_data()
                    self._update_scalp_data()

                    # 2. Detect market regime (every cycle, uses cached data)
                    regime_result = self.regime_detector.detect(self.market_data)

                    # 3. Monitor existing positions (stops, targets, trailing)
                    self._monitor_positions()

                    # 3b. Portfolio-level risk audit (concentration, exposure, max loss)
                    self._check_portfolio_risk()

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

                    # 6a. Capture rejected signals for momentum rotation
                    rejected_for_rotation = [s for s in signals if s not in approved and s.get("action") == "buy"]
                    if rejected_for_rotation and len(self.positions) >= self.risk_manager.max_positions - 1:
                        self._momentum_rotation_check(rejected_for_rotation)

                    # 7a. Pre-market / Post-market filtering: limit strategies and reduce size
                    if getattr(self, "_in_premarket", False):
                        pm_config = self.config.schedule_config.get("premarket", {})
                        allowed = pm_config.get("allowed_strategies", [])
                        size_mult = pm_config.get("reduce_size_pct", 0.5)
                        if allowed:
                            approved = [s for s in approved if s.get("strategy") in allowed]
                        for sig in approved:
                            if sig.get("quantity"):
                                sig["quantity"] = max(1, int(sig["quantity"] * size_mult))

                    if getattr(self, "_in_postmarket", False):
                        pm_config = self.config.schedule_config.get("postmarket", {})
                        allowed = pm_config.get("allowed_strategies", [])
                        size_mult = pm_config.get("reduce_size_pct", 0.5)
                        if allowed:
                            approved = [s for s in approved if s.get("strategy") in allowed]
                        for sig in approved:
                            if sig.get("quantity"):
                                sig["quantity"] = max(1, int(sig["quantity"] * size_mult))

                    # 7b. POWER HOUR (3:00-4:00 PM ET)
                    now_time = datetime.now(self.tz)
                    if (now_time.hour == 15 and
                            getattr(self, '_equity_market_open', False)):
                        self._in_power_hour = True

                        # --- POWER HOUR PHASE 1: Trim weak positions (3:00-3:30) ---
                        # Close positions with weak bullish scores to free capital
                        # and reduce position count before EOD
                        if now_time.minute < 30 and not getattr(self, '_ph_trimmed', False):
                            self._power_hour_trim()
                            self._ph_trimmed = True

                        # --- POWER HOUR PHASE 2: Moon hour (3:30-3:50) ---
                        # Aggressive entries on late-day runners with surging volume
                        # These are the "moon" plays — stocks ripping into the close
                        if 30 <= now_time.minute <= 50:
                            for sig in approved:
                                if sig.get("action") == "buy":
                                    snap_rvol = sig.get("rvol", 0)
                                    reason = sig.get("reason", "")
                                    # Moon hour: high RVOL late-day runners get boosted
                                    if snap_rvol >= 3.0:
                                        sig["confidence"] = min(1.0, sig.get("confidence", 0.5) + 0.20)
                                        sig["reason"] = reason + " | MOON HOUR RUNNER"
                                        log.info(
                                            f"MOON HOUR BOOST: {sig['symbol']} RVOL={snap_rvol:.1f}x "
                                            f"— confidence boosted to {sig['confidence']:.2f}"
                                        )
                                    elif snap_rvol >= 2.0:
                                        sig["confidence"] = min(1.0, sig.get("confidence", 0.5) + 0.10)
                                        sig["reason"] = reason + " | POWER HOUR"

                        # --- POWER HOUR PHASE 3: Tighten all stops (3:50+) ---
                        # Protect profits before EOD volatility
                        if now_time.minute >= 50 and not getattr(self, '_ph_tightened', False):
                            self._power_hour_tighten_stops()
                            self._ph_tightened = True

                    else:
                        self._in_power_hour = False
                        self._ph_trimmed = False
                        self._ph_tightened = False

                    # 7c. Apply regime-based filtering
                    if regime_result and regime_result.get("regime") == "crisis":
                        # In crisis, only allow hedge signals and exits
                        approved = [s for s in approved if
                                    s.get("source") == "hedging" or
                                    s.get("action") in ("sell", "cover", "close")]

                    # 7d. Off-hours filter: Only allow crypto signals when equity market closed
                    if not getattr(self, '_equity_market_open', True):
                        approved = [s for s in approved if self._is_crypto_symbol(s.get("symbol", ""))]

                    # 7e. Scanner summary notification
                    symbols_scanned = sum(len(s.get_symbols()) for s in self.strategies.values())
                    regime_str = regime_result.get("regime") if regime_result else None
                    spy_change = None
                    spy_data = None
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

                    # 7f. UOA confirmation boost — check unusual options activity
                    # for high-score buy signals to detect smart money alignment
                    if approved and hasattr(self, 'polygon_scanner') and self.polygon_scanner:
                        for sig in approved:
                            if (sig.get("action") == "buy" and
                                    sig.get("score", 0) >= 50 and
                                    not sig.get("uoa_checked")):
                                try:
                                    uoa = self.polygon_scanner.check_unusual_options(sig["symbol"])
                                    if uoa and uoa.get("bullish") and uoa.get("uoa_score", 0) >= 15:
                                        boost = min(15, uoa["uoa_score"])
                                        sig["score"] = sig.get("score", 0) + boost
                                        sig["confidence"] = min(1.0, sig.get("confidence", 0.5) + 0.10)
                                        sig["reason"] = sig.get("reason", "") + f" | UOA BULLISH (sweeps: {uoa.get('large_sweeps', 0)})"
                                        log.info(
                                            f"UOA BOOST: {sig['symbol']} score +{boost} "
                                            f"(calls: {uoa['call_vol']:,}, sweeps: {uoa['large_sweeps']})"
                                        )
                                    sig["uoa_checked"] = True
                                except Exception:
                                    pass

                    # 8. Deduplicate approved signals per-symbol (keep highest score)
                    #    Prevents multiple strategies from placing separate orders
                    #    for the same symbol in a single cycle (e.g. LUNR 200+300+290).
                    best_per_symbol = {}
                    for sig in approved:
                        sym = sig.get("symbol", "")
                        action = sig.get("action", "")
                        key = (sym, action)
                        existing = best_per_symbol.get(key)
                        if existing is None or sig.get("score", 0) > existing.get("score", 0):
                            best_per_symbol[key] = sig
                    deduped = list(best_per_symbol.values())
                    if len(deduped) < len(approved):
                        log.info(
                            f"DEDUP: {len(approved)} signals -> {len(deduped)} "
                            f"(merged duplicate symbols)"
                        )
                    for sig in deduped:
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

                # Sleep 3 seconds (fast scalp tick rate)
                time.sleep(3)

            except Exception as e:
                log.error(f"Main loop error: {e}", exc_info=True)
                time.sleep(30)

    def _is_crypto_symbol(self, symbol):
        """Check if a symbol is a crypto ticker (e.g. BTC-USD, ETH-USDT)."""
        suffixes = self.config.settings.get("crypto", {}).get(
            "symbols_suffix", ["-USD", "-USDT", "-BTC", "-ETH"]
        )
        return any(symbol.upper().endswith(s) for s in suffixes)

    def _is_crypto_enabled(self):
        """Check if crypto trading is enabled in config."""
        return self.config.settings.get("crypto", {}).get("enabled", False)

    def _has_crypto_symbols(self):
        """Check if any watched/traded symbols are crypto (enables 24/7 mode)."""
        all_syms = set(self.watchlist)
        with self._positions_lock:
            all_syms.update(self.positions.keys())
        for s in self.strategies.values():
            try:
                all_syms.update(s.get_symbols())
            except Exception:
                pass
        return any(self._is_crypto_symbol(sym) for sym in all_syms)

    def _is_market_hours(self, now):
        """Check if within trading hours (includes optional premarket + crypto 24/7).
        When crypto symbols exist but market is closed, we still return True
        but set a flag so equity strategies are skipped during off-hours."""
        # Crypto trades 24/7 - but only flag it, don't skip market hour checks
        has_crypto = self._has_crypto_symbols()

        sched = self.config.schedule_config
        day_name = now.strftime("%A")
        trading_days = sched.get("trading_days", [
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
        ])

        if day_name not in trading_days:
            # On weekends, only run if crypto symbols exist (crypto trades 24/7)
            self._in_premarket = False
            self._in_postmarket = False
            self._equity_market_open = False
            return has_crypto

        # Check premarket window
        premarket = sched.get("premarket", {})
        if premarket.get("enabled", False):
            pm_start = premarket.get("start_time", "08:00")
            h_pm, m_pm = map(int, pm_start.split(":"))
            pm_open = now.replace(hour=h_pm, minute=m_pm, second=0)
            regular_open = now.replace(hour=9, minute=30, second=0)
            if pm_open <= now < regular_open:
                self._in_premarket = True
                self._equity_market_open = True
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

        equity_open = actual_open <= now <= actual_close
        self._equity_market_open = equity_open

        # Check postmarket window (after market close)
        postmarket = sched.get("postmarket", {})
        if postmarket.get("enabled", False) and now > market_close:
            pm_end = postmarket.get("end_time", "18:00")
            h_pm, m_pm = map(int, pm_end.split(":"))
            pm_close = now.replace(hour=h_pm, minute=m_pm, second=0)
            if now <= pm_close:
                self._in_postmarket = True
                self._equity_market_open = True
                return True
        self._in_postmarket = False

        # Return True if equity market is open OR if we have crypto (24/7)
        return equity_open or has_crypto

    def _update_data(self):
        """Fetch latest market data for all strategy symbols + watchlist.

        Prioritizes bar fetching for:
        1. Open positions (need real-time monitoring)
        2. Top movers from Polygon (highest change% first — Money Machine priority)
        3. Everything else
        """
        all_symbols = set()
        for strategy in self.strategies.values():
            all_symbols.update(strategy.get_symbols())

        # Also include symbols we have positions in
        all_symbols.update(self.positions.keys())

        # Include watchlist symbols for live prices
        all_symbols.update(self.watchlist)

        # Prioritize: positions first, then top movers by change%, then rest
        priority_symbols = []

        # 1. Open positions (always first — need accurate prices for stops)
        for sym in self.positions.keys():
            if sym in all_symbols:
                priority_symbols.append(sym)

        # 2. Top movers from Polygon (sorted by change% desc — Money Machine priority)
        if self.polygon and self.polygon.enabled:
            top_movers = self.polygon.get_top_movers(limit=50)
            for m in top_movers:
                sym = m.get("symbol", "")
                if sym and sym in all_symbols and sym not in priority_symbols:
                    priority_symbols.append(sym)

        # 3. Everything else
        remaining = [s for s in all_symbols if s not in priority_symbols]
        ordered_symbols = priority_symbols + remaining

        # Prune IBKR streams for symbols no longer being tracked
        # This frees slots for newly discovered movers
        if self.market_data and hasattr(self.market_data, 'prune_stale_streams'):
            self.market_data.prune_stale_streams(all_symbols)

        self.market_data.update(ordered_symbols)

        # Diagnostic: log data coverage every ~5 cycles (avoid log spam)
        if not hasattr(self, '_data_log_counter'):
            self._data_log_counter = 0
        self._data_log_counter += 1
        if self._data_log_counter >= 5:
            self._data_log_counter = 0
            with_data = sum(1 for s in all_symbols if self.market_data.get_data(s) is not None)
            with_price = sum(1 for s in all_symbols if self.market_data._price_cache.get(s))
            blacklisted = len(self.broker._invalid_symbols) if self.broker and hasattr(self.broker, '_invalid_symbols') else 0
            fail_cached = len(self.market_data._bars_fail_cache) if hasattr(self.market_data, '_bars_fail_cache') else 0
            log.info(
                f"DATA COVERAGE: {with_data}/{len(all_symbols)} symbols have bars, "
                f"{with_price}/{len(all_symbols)} have prices"
                f"{f' | IBKR blacklisted: {blacklisted}' if blacklisted else ''}"
                f"{f' | bar-fetch backoff: {fail_cached}' if fail_cached else ''}"
            )
            if with_data == 0:
                log.warning("ZERO DATA: No market data received — strategies cannot generate signals!")

    def _update_scalp_data(self):
        """Fetch 1-minute bars for scalp strategy symbols."""
        scalp_strat = self.strategies.get("rvol_scalp")
        if not scalp_strat:
            return
        scalp_symbols = scalp_strat.get_symbols()
        # Also include open scalp positions
        for sym, pos in self.positions.items():
            if pos.get("strategy") == "rvol_scalp" and sym not in scalp_symbols:
                scalp_symbols.append(sym)
        if scalp_symbols and self.market_data:
            self.market_data.update_1m_bars(scalp_symbols)

    def _fast_scalp_monitor(self):
        """AGGRESSIVE universal position monitor (runs every 3 seconds).

        Refreshes prices for ALL open positions and applies:
        - Intra-candle profit-taking (partial exits) for every position
        - Real-time trailing stop ratcheting (moves stop up on every up-tick)
        - Momentum detection (consecutive up-ticks = sustained move)
        - Stop-loss enforcement with zero delay

        This is the primary exit manager. The slower _monitor_positions()
        handles break-even and max-hold-time only.
        """
        if not self.positions:
            return

        # Snapshot positions for safe iteration (dict may be mutated by sync thread)
        with self._positions_lock:
            positions_snapshot = dict(self.positions)

        # Refresh prices for all open position symbols
        position_symbols = list(positions_snapshot.keys())
        if self.market_data:
            self.market_data.refresh_prices(position_symbols)

        # Load profit taking config
        pt_config = self.config.risk_config.get("profit_taking", {})
        pt_enabled = pt_config.get("enabled", False)
        pt_targets = pt_config.get("targets", [])

        # Check each position for quick exits
        positions_to_close = []
        partial_exits = []

        now_ts = datetime.now(self.tz)

        for symbol, pos in positions_snapshot.items():
            current_price = self.market_data.get_price(symbol) if self.market_data else None
            if current_price is None:
                continue

            # --- ENTRY GRACE PERIOD ---
            # Don't allow exits within 30 seconds of entry. This prevents:
            # 1. Sell-before-fill race conditions (order just placed)
            # 2. Immediate scalp trail triggers from stale prices
            # 3. False stops from bid/ask spread noise right after entry
            entry_time = pos.get("entry_time")
            if entry_time:
                seconds_held = (now_ts - entry_time).total_seconds()
                if seconds_held < 30:
                    continue  # Skip — position too fresh for exit evaluation

            entry_price = pos["entry_price"]
            direction = pos.get("direction", "long")

            # Calculate current P&L (long-only)
            pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0

            pos["unrealized_pnl_pct"] = pnl_pct
            pos["current_price"] = current_price

            # --- MOMENTUM TRACKING (consecutive up-ticks) ---
            prev_price = pos.get("_last_tick_price", entry_price)
            if current_price > prev_price:
                pos["_uptick_count"] = pos.get("_uptick_count", 0) + 1
                pos["_downtick_count"] = 0
            elif current_price < prev_price:
                pos["_downtick_count"] = pos.get("_downtick_count", 0) + 1
                pos["_uptick_count"] = 0
            pos["_last_tick_price"] = current_price

            # Track high water mark for aggressive trailing
            hwm = pos.get("_high_water_mark", entry_price)
            if direction == "long" and current_price > hwm:
                pos["_high_water_mark"] = current_price
            hwm = pos.get("_high_water_mark", entry_price)

            # --- STOP LOSS (instant check) ---
            stop_price = pos.get("stop_loss")
            if stop_price:
                hit = (direction == "long" and current_price <= stop_price) or \
                      (direction == "short" and current_price >= stop_price)
                if hit:
                    positions_to_close.append(
                        (symbol, "stop_loss", f"Stop hit at ${current_price:.2f}")
                    )
                    continue

            # --- INTRA-CANDLE PARTIAL PROFIT TAKING (every 3 seconds) ---
            if pt_enabled and pos["quantity"] > 1:
                targets_hit = pos.get("targets_hit", [])
                for i, target in enumerate(pt_targets):
                    if i in targets_hit:
                        continue
                    target_pct = target.get("pct_from_entry", 0)
                    if pnl_pct >= target_pct:
                        close_pct = target.get("close_pct", 0.25)
                        qty_to_close = max(1, int(pos["quantity"] * close_pct))

                        # Don't close everything via partial - leave at least 1
                        if qty_to_close >= pos["quantity"]:
                            qty_to_close = pos["quantity"] - 1

                        if qty_to_close > 0:
                            partial_exits.append((symbol, qty_to_close, i, target))
                            targets_hit.append(i)
                            pos["targets_hit"] = targets_hit

                            # Move stop to break-even on first target
                            be_buffer = self.config.risk_config.get(
                                "breakeven", {}).get("buffer_pct", 0.002)
                            if target.get("move_stop") == "breakeven" and not pos.get("breakeven_hit"):
                                be_stop = entry_price * (1 + be_buffer) if direction == "long" else entry_price * (1 - be_buffer)
                                pos["stop_loss"] = be_stop
                                pos["breakeven_hit"] = True

                            # Tighten trailing stop if specified
                            if target.get("tighten_trail"):
                                pos["trailing_stop_pct"] = target["tighten_trail"]
                                log.info(
                                    f"TRAIL TIGHTENED: {symbol} now {target['tighten_trail']:.1%} "
                                    f"at +{pnl_pct:.1%}"
                                )

                        break  # Only hit one target per tick

            # --- AGGRESSIVE TRAILING STOP RATCHET (every 3 seconds) ---
            # Moves the trailing stop UP on every price tick, not just every 10 seconds

            # === 4-PHASE ATR-BASED TRAILING for momentum_runner positions ===
            is_momentum_runner = pos.get("momentum_runner", False)
            if is_momentum_runner and direction == "long":
                atr_value = pos.get("atr_value", current_price * 0.03)
                entry_type = pos.get("entry_type", "breakout")

                if pnl_pct >= 0.15:
                    # PHASE 4: Parabolic protection (15%+) — trail 5 EMA equivalent
                    # Use tight % trail as proxy for 5 EMA on 1-min
                    trailing_pct = 0.015
                    pos["_trail_phase"] = 4
                    # Extra exit: if any tick drops >2% from high water mark, exit
                    hwm = pos.get("_high_water_mark", entry_price)
                    if hwm > 0 and (hwm - current_price) / hwm > 0.02:
                        positions_to_close.append(
                            (symbol, "trailing_stop",
                             f"Phase 4 parabolic exit at ${current_price:.2f} | "
                             f"HWM: ${hwm:.2f} | P&L: {pnl_pct:+.1%}")
                        )
                        continue

                elif pnl_pct >= 0.05:
                    # PHASE 3: Let it run (5%+) — trail 5-candle low or 9 EMA (tighter)
                    trailing_pct = 0.025  # ~5-candle low equivalent on 1-min
                    pos["_trail_phase"] = 3

                elif pnl_pct >= 0.02:
                    # PHASE 2: Lock in gains (2-5%) — breakeven + 0.5%, 3-candle trail
                    be_level = entry_price * 1.005
                    if pos.get("stop_loss", 0) < be_level:
                        pos["stop_loss"] = be_level
                    trailing_pct = 0.02  # ~3-candle low equivalent
                    pos["_trail_phase"] = 2

                else:
                    # PHASE 1: Initial protection (0-2%) — hard stop at entry - 1x ATR
                    # Stop already set at signal time; just enforce it
                    trailing_pct = 0  # No trailing yet, use hard stop only
                    pos["_trail_phase"] = 1
                    # If price drops back to entry, exit immediately (failed breakout)
                    if current_price <= entry_price and pnl_pct <= 0:
                        # Only exit on failed breakout if we've been in for >60 seconds
                        entry_time = pos.get("entry_time")
                        if entry_time:
                            from datetime import datetime as dt_cls
                            elapsed = (dt_cls.now(self.tz) - entry_time).total_seconds()
                            if elapsed > 60:
                                positions_to_close.append(
                                    (symbol, "stop_loss",
                                     f"Failed breakout: price back at entry ${current_price:.2f}")
                                )
                                continue

                # Spike entry special handling: take 50% when momentum stalls
                if entry_type == "spike" and pnl_pct >= 0.05:
                    prev_price = pos.get("_last_tick_price", entry_price)
                    if current_price < prev_price and not pos.get("_spike_partial_taken"):
                        # Next tick lower after spike = momentum stalling
                        qty_to_close = max(1, int(pos["quantity"] * 0.5))
                        if qty_to_close < pos["quantity"]:
                            partial_exits.append((symbol, qty_to_close, -1,
                                                  {"pct_from_entry": 0.05, "close_pct": 0.5}))
                            pos["_spike_partial_taken"] = True

                # Apply trailing stop for phases 2-4
                if trailing_pct > 0 and direction == "long":
                    new_trail = current_price * (1 - trailing_pct)
                    if "trailing_stop" not in pos or new_trail > pos.get("trailing_stop", 0):
                        pos["trailing_stop"] = new_trail
                    if current_price <= pos.get("trailing_stop", 0):
                        phase = pos.get("_trail_phase", 0)
                        positions_to_close.append(
                            (symbol, "trailing_stop",
                             f"Phase {phase} trail stop at ${current_price:.2f} | "
                             f"P&L: {pnl_pct:+.1%} | trail: {trailing_pct:.1%}")
                        )
                        continue

            else:
                # === ORIGINAL TRAILING LOGIC for non-momentum-runner positions ===
                base_trail = pos.get("trailing_stop_pct",
                                     self.config.risk_config.get("trailing_stop_pct", 0.02))

                # Momentum-aware trail: consecutive up-ticks = sustained move = give more room
                upticks = pos.get("_uptick_count", 0)
                momentum_buffer = 0.0
                if upticks >= 5:
                    momentum_buffer = 0.005   # Sustained momentum: add 0.5% buffer to not get shaken
                elif upticks >= 3:
                    momentum_buffer = 0.003   # Building momentum: add 0.3% buffer

                # Dynamic trailing based on profit level
                if pnl_pct >= 2.00:
                    trailing_pct = 0.08       # 8% trail at 200%+ — ride the monster
                elif pnl_pct >= 1.00:
                    trailing_pct = 0.06       # 6% trail at 100%+
                elif pnl_pct >= 0.50:
                    trailing_pct = 0.05       # 5% trail at 50%+
                elif pnl_pct >= 0.25:
                    trailing_pct = 0.035      # 3.5% trail at 25%+
                elif pnl_pct >= 0.10:
                    trailing_pct = 0.025      # 2.5% trail at 10%+ — lock it in
                elif pnl_pct >= 0.05:
                    trailing_pct = 0.02       # 2% trail at 5%+
                elif pnl_pct >= 0.02:
                    trailing_pct = 0.015      # 1.5% trail at 2%+ — aggressive protection
                else:
                    trailing_pct = base_trail

                # Add momentum buffer for sustained moves
                trailing_pct += momentum_buffer

                if direction == "long":
                    new_trail = current_price * (1 - trailing_pct)
                    # Only move stop UP, never down
                    if "trailing_stop" not in pos or new_trail > pos.get("trailing_stop", 0):
                        pos["trailing_stop"] = new_trail
                    if current_price <= pos.get("trailing_stop", 0):
                        positions_to_close.append(
                            (symbol, "trailing_stop",
                             f"Trail stop at ${current_price:.2f} | "
                             f"P&L: {pnl_pct:+.1%} | trail: {trailing_pct:.1%}")
                        )
                        continue

            # --- TAKE PROFIT TRAIL TRIGGER ---
            # When TP target hit, activate runner mode with tighter trail
            target_price = pos.get("take_profit")
            if target_price and not pos.get("tp_trail_activated"):
                hit = (direction == "long" and current_price >= target_price) or \
                      (direction == "short" and current_price <= target_price)
                if hit:
                    pos["tp_trail_activated"] = True
                    pos["trailing_stop_pct"] = min(
                        pos.get("trailing_stop_pct", 0.02), 0.015
                    )
                    log.info(
                        f"RUNNER MODE: {symbol} hit TP ${target_price:.2f} at "
                        f"${current_price:.2f} ({pnl_pct:+.1%}) — trailing stop "
                        f"tightened to {pos['trailing_stop_pct']:.1%}, NO hard exit"
                    )

        # Execute exits — track which symbols had partial close attempted
        # so we don't also fire a full close for the same symbol in the same cycle
        partial_attempted = set()
        for symbol, qty, target_idx, target in partial_exits:
            partial_attempted.add(symbol)
            self._partial_close(symbol, qty, target_idx, target)

        for symbol, reason_type, reason_msg in positions_to_close:
            if symbol in partial_attempted:
                log.debug(f"Skipping full close for {symbol} — partial close already attempted this cycle")
                continue
            self._close_position(symbol, reason_type, reason_msg)

    def _on_5sec_bar(self, symbol, bar):
        """
        Callback fired every 5 seconds with real-time bar data from IBKR.
        Updates price cache instantly and triggers immediate exit checks
        for held positions — this is the fastest possible exit path.
        """
        try:
            price = bar.get("close", 0)
            volume = bar.get("volume", 0)

            if price <= 0:
                return

            # Update price cache instantly (faster than polling cycle)
            if self.market_data:
                self.market_data._price_cache[symbol] = price

            # If we hold this position, update P&L and check exits in real-time
            pos = self.positions.get(symbol)
            if pos:
                entry = pos["entry_price"]
                pos["current_price"] = price
                pnl_pct = (price - entry) / entry if entry > 0 else 0
                pos["unrealized_pnl_pct"] = pnl_pct

                # Entry grace period — don't trigger exits within 30s of entry
                entry_time = pos.get("entry_time")
                if entry_time:
                    seconds_held = (datetime.now(self.tz) - entry_time).total_seconds()
                    if seconds_held < 30:
                        return  # Still update price/pnl but don't exit

                # --- INSTANT TRAILING STOP CHECK ---
                # Don't wait for the 3-second poll — exit immediately on IBKR data
                trailing_stop = pos.get("trailing_stop")
                if trailing_stop and pos["direction"] == "long" and price <= trailing_stop:
                    trail_pct = pos.get("trailing_stop_pct", 0.02)
                    self._close_position(
                        symbol, "trailing_stop",
                        f"5-sec trail stop at ${price:.2f} | "
                        f"P&L: {pnl_pct:+.1%} | trail: {trail_pct:.1%}"
                    )
                    return

                # --- INSTANT STOP LOSS CHECK ---
                stop_price = pos.get("stop_loss")
                if stop_price and pos["direction"] == "long" and price <= stop_price:
                    self._close_position(
                        symbol, "stop_loss",
                        f"5-sec stop hit at ${price:.2f}"
                    )
                    return

                # --- RATCHET TRAILING STOP UP ON NEW HIGHS ---
                hwm = pos.get("_high_water_mark", entry)
                if price > hwm:
                    pos["_high_water_mark"] = price
                    # Recalculate trail from new high
                    trail_pct = pos.get("trailing_stop_pct", 0.02)
                    new_trail = price * (1 - trail_pct)
                    if new_trail > pos.get("trailing_stop", 0):
                        pos["trailing_stop"] = new_trail

                # --- MOMENTUM TRACKING ---
                prev = pos.get("_last_tick_price", entry)
                if price > prev:
                    pos["_uptick_count"] = pos.get("_uptick_count", 0) + 1
                    pos["_downtick_count"] = 0
                elif price < prev:
                    pos["_downtick_count"] = pos.get("_downtick_count", 0) + 1
                    pos["_uptick_count"] = 0
                pos["_last_tick_price"] = price

            # Detect sudden volume surge on 5-sec bars (RVOL micro-spike)
            if not hasattr(self, '_5sec_vol_avg'):
                self._5sec_vol_avg = {}

            if volume > 0:
                avg = self._5sec_vol_avg.get(symbol, 0)
                if avg > 0 and volume > avg * 5:
                    log.info(
                        f"5-SEC VOLUME SURGE: {symbol} vol={volume:,} "
                        f"({volume/avg:.1f}x avg) @ ${price:.2f}"
                    )
                # Update rolling average (exponential)
                if avg > 0:
                    self._5sec_vol_avg[symbol] = avg * 0.95 + volume * 0.05
                else:
                    self._5sec_vol_avg[symbol] = volume

        except Exception as e:
            log.debug(f"5-sec bar callback error for {symbol}: {e}")

    def _on_tick(self, symbol, tick):
        """
        Callback fired on EVERY trade print from IBKR tick-by-tick data.
        This is the absolute fastest exit path — fires on each individual
        trade, not aggregated 5-second bars. Sub-100ms latency.
        """
        try:
            price = tick.get("price", 0)
            size = tick.get("size", 0)

            if price <= 0:
                return

            # Update price cache instantly
            if self.market_data:
                self.market_data._price_cache[symbol] = price

            # If we hold this position, check exits on every single trade
            pos = self.positions.get(symbol)
            if not pos:
                return

            entry = pos["entry_price"]
            pos["current_price"] = price
            pnl_pct = (price - entry) / entry if entry > 0 else 0
            pos["unrealized_pnl_pct"] = pnl_pct

            # Entry grace period — don't trigger exits within 30s of entry
            entry_time = pos.get("entry_time")
            if entry_time:
                seconds_held = (datetime.now(self.tz) - entry_time).total_seconds()
                if seconds_held < 30:
                    return  # Still update price/pnl but don't exit

            # --- INSTANT TRAILING STOP CHECK ---
            trailing_stop = pos.get("trailing_stop")
            if trailing_stop and pos["direction"] == "long" and price <= trailing_stop:
                trail_pct = pos.get("trailing_stop_pct", 0.02)
                self._close_position(
                    symbol, "trailing_stop",
                    f"Tick trail stop at ${price:.2f} | "
                    f"P&L: {pnl_pct:+.1%} | trail: {trail_pct:.1%}"
                )
                return

            # --- INSTANT STOP LOSS CHECK ---
            stop_price = pos.get("stop_loss")
            if stop_price and pos["direction"] == "long" and price <= stop_price:
                self._close_position(
                    symbol, "stop_loss",
                    f"Tick stop hit at ${price:.2f}"
                )
                return

            # --- RATCHET TRAILING STOP UP ON NEW HIGHS ---
            hwm = pos.get("_high_water_mark", entry)
            if price > hwm:
                pos["_high_water_mark"] = price
                trail_pct = pos.get("trailing_stop_pct", 0.02)
                new_trail = price * (1 - trail_pct)
                if new_trail > pos.get("trailing_stop", 0):
                    pos["trailing_stop"] = new_trail

            # --- MOMENTUM TRACKING (per-trade resolution) ---
            prev = pos.get("_last_tick_price", entry)
            if price > prev:
                pos["_uptick_count"] = pos.get("_uptick_count", 0) + 1
                pos["_downtick_count"] = 0
            elif price < prev:
                pos["_downtick_count"] = pos.get("_downtick_count", 0) + 1
                pos["_uptick_count"] = 0
            pos["_last_tick_price"] = price

        except Exception as e:
            log.debug(f"Tick callback error for {symbol}: {e}")

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

        # Snapshot for safe iteration
        with self._positions_lock:
            positions_snapshot = dict(self.positions)

        now_ts = datetime.now(self.tz)

        for symbol, pos in positions_snapshot.items():
            current_price = self.market_data.get_price(symbol)
            if current_price is None:
                continue

            # Entry grace period — don't evaluate exits within 30s of entry
            entry_time = pos.get("entry_time")
            if entry_time:
                seconds_held = (now_ts - entry_time).total_seconds()
                if seconds_held < 30:
                    continue

            entry_price = pos["entry_price"]
            direction = pos.get("direction", "long")

            # Calculate unrealized P&L (long-only)
            pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0

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
            # NOTE: Partials are now handled by _fast_scalp_monitor() every 3 seconds
            # for intra-candle execution. This block is a fallback safety net only.
            is_scalp_pos = pos.get("strategy") in ("rvol_scalp", "vwap_scalp")
            if pt_enabled and pos["quantity"] > 1 and not is_scalp_pos:
                targets_hit = pos.get("targets_hit", [])
                for i, target in enumerate(pt_targets):
                    if i in targets_hit:
                        continue
                    target_pct = target.get("pct_from_entry", 0)
                    if pnl_pct >= target_pct:
                        close_pct = target.get("close_pct", 0.25)
                        qty_to_close = max(1, int(pos["quantity"] * close_pct))

                        if qty_to_close >= pos["quantity"]:
                            qty_to_close = pos["quantity"] - 1

                        if qty_to_close > 0:
                            partial_exits.append((symbol, qty_to_close, i, target))
                            targets_hit.append(i)
                            pos["targets_hit"] = targets_hit

                            be_buffer_val = be_config.get("buffer_pct", 0.001)
                            if target.get("move_stop") == "breakeven" and not pos.get("breakeven_hit"):
                                be_stop = entry_price * (1 + be_buffer_val) if direction == "long" else entry_price * (1 - be_buffer_val)
                                pos["stop_loss"] = be_stop
                                pos["breakeven_hit"] = True

                            if target.get("tighten_trail"):
                                pos["trailing_stop_pct"] = target["tighten_trail"]

                        break

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

            # --- NO hard take profit ceiling ---
            # TP target is handled as a trail-tighten trigger in the scalp monitor above.
            # The trailing stop is the ONLY exit mechanism for winners.
            # This lets runners run 50%, 100%, 400%+ with trailing protection.

            # --- Dynamic Trailing Stop (matches aggressive tiers in _fast_scalp_monitor) ---
            base_trail = pos.get("trailing_stop_pct",
                                 self.config.risk_config.get("trailing_stop_pct", 0.015))

            # Aggressive trailing tiers — same as _fast_scalp_monitor for consistency
            # Momentum buffer from uptick tracking
            upticks = pos.get("_uptick_count", 0)
            momentum_buffer = 0.005 if upticks >= 5 else (0.003 if upticks >= 3 else 0.0)

            if pnl_pct >= 2.00:
                trailing_pct = 0.08       # 8% trail at 200%+
            elif pnl_pct >= 1.00:
                trailing_pct = 0.06       # 6% trail at 100%+
            elif pnl_pct >= 0.50:
                trailing_pct = 0.05       # 5% trail at 50%+
            elif pnl_pct >= 0.25:
                trailing_pct = 0.035      # 3.5% trail at 25%+
            elif pnl_pct >= 0.10:
                trailing_pct = 0.025      # 2.5% trail at 10%+
            elif pnl_pct >= 0.05:
                trailing_pct = 0.02       # 2% trail at 5%+
            elif pnl_pct >= 0.02:
                trailing_pct = 0.015      # 1.5% trail at 2%+
            else:
                trailing_pct = base_trail

            trailing_pct += momentum_buffer

            if direction == "long":
                new_trail = current_price * (1 - trailing_pct)
                if "trailing_stop" not in pos or new_trail > pos["trailing_stop"]:
                    pos["trailing_stop"] = new_trail
                if current_price <= pos.get("trailing_stop", 0):
                    positions_to_close.append(
                        (symbol, "trailing_stop",
                         f"Trailing stop at ${current_price:.2f} (trail {trailing_pct:.1%})")
                    )
                    continue
            elif direction == "short":
                new_trail = current_price * (1 + trailing_pct)
                if "trailing_stop" not in pos or new_trail < pos["trailing_stop"]:
                    pos["trailing_stop"] = new_trail
                if current_price >= pos.get("trailing_stop", float("inf")):
                    positions_to_close.append(
                        (symbol, "trailing_stop",
                         f"Trailing stop at ${current_price:.2f} (trail {trailing_pct:.1%})")
                    )
                    continue

            # --- Max Holding Period ---
            if "entry_time" in pos:
                elapsed = (datetime.now(self.tz) - pos["entry_time"]).total_seconds()
                elapsed_days = elapsed / 86400

                # Days-based hold limit (swing/momentum trades)
                max_hold_days = pos.get("max_hold_days", 0)
                if max_hold_days > 0 and elapsed_days > max_hold_days:
                    # Breakout plays in profit: keep riding with trail
                    is_breakout = pos.get("breakout_play") or pos.get("source") == "prebreakout"
                    # If profitable, let it ride with tighter trail instead of hard exit
                    if is_breakout and pnl_pct > 0.01:
                        # Breakout runners get looser trail even at expiry
                        trail = 0.02 if pnl_pct > 0.05 else 0.015
                        pos["trailing_stop_pct"] = min(
                            pos.get("trailing_stop_pct", 0.03), trail
                        )
                        if not pos.get("_breakout_runner_logged"):
                            log.info(
                                f"BREAKOUT RUNNER: {symbol} held {elapsed_days:.1f}d "
                                f"(max {max_hold_days}d) up {pnl_pct:+.1%} - "
                                f"keeping with {trail:.1%} trail"
                            )
                            pos["_breakout_runner_logged"] = True
                    elif pnl_pct > 0.02:
                        pos["trailing_stop_pct"] = min(
                            pos.get("trailing_stop_pct", 0.02), 0.01
                        )
                        log.info(
                            f"HOLD EXPIRING: {symbol} held {elapsed_days:.1f}d "
                            f"(max {max_hold_days}d) but profitable ({pnl_pct:+.1%}) - "
                            f"tightening trail to 1%"
                        )
                    else:
                        positions_to_close.append(
                            (symbol, "time_exit",
                             f"Max hold {max_hold_days}d exceeded ({elapsed_days:.1f}d) "
                             f"| P&L: {pnl_pct:+.1%}")
                        )
                        continue

                # Bars-based hold limit (scalp/intraday trades)
                elif "max_hold_bars" in pos and pos["max_hold_bars"] > 0:
                    bar_seconds = pos.get("bar_seconds", 300)
                    if elapsed > pos["max_hold_bars"] * bar_seconds:
                        # Runner override: if up big, don't force exit - tighten trail instead
                        if pnl_pct >= 0.03 and not pos.get("scalp_mode"):
                            pos["trailing_stop_pct"] = min(
                                pos.get("trailing_stop_pct", 0.02), 0.008
                            )
                            if not pos.get("_runner_mode_logged"):
                                log.info(
                                    f"RUNNER MODE: {symbol} up {pnl_pct:+.1%} at max hold "
                                    f"- keeping with 0.8% trail instead of forced exit"
                                )
                                pos["_runner_mode_logged"] = True
                        else:
                            positions_to_close.append(
                                (symbol, "time_exit", "Max holding period exceeded")
                            )
                            continue

            # --- Stale Position Exit ---
            # If position hasn't moved >0.3% in 30+ minutes, exit to free capital
            # Only for scalp/intraday - swing trades get more room
            # Breakout plays get the most room (they consolidate before moving)
            if "entry_time" in pos and not pos.get("scalp_mode"):
                elapsed_min = (datetime.now(self.tz) - pos["entry_time"]).total_seconds() / 60
                max_hold_days = pos.get("max_hold_days", 0)
                is_breakout = pos.get("breakout_play") or pos.get("source") == "prebreakout"
                if is_breakout:
                    stale_threshold_min = 480  # 8 hours for breakout plays (consolidation is normal)
                    stale_move_pct = 0.01      # 1% threshold (tight ranges expected)
                elif max_hold_days > 0:
                    stale_threshold_min = 180  # 3 hours for swing (was 2h — too aggressive)
                    stale_move_pct = 0.008     # 0.8% for swing (was 0.5% — shaking out winners)
                else:
                    stale_threshold_min = 60   # 1 hour for momentum (was 30min — too tight)
                    stale_move_pct = 0.005     # 0.5% for momentum (was 0.3% — normal consolidation)
                if elapsed_min >= stale_threshold_min and abs(pnl_pct) < stale_move_pct:
                    positions_to_close.append(
                        (symbol, "stale_exit",
                         f"Stale position: {pnl_pct:+.2%} after {elapsed_min:.0f}min")
                    )

        # Execute partial exits first — track symbols to prevent double-send
        partial_attempted = set()
        for symbol, qty, target_idx, target in partial_exits:
            partial_attempted.add(symbol)
            self._partial_close(symbol, qty, target_idx, target)

        # Execute full closes (skip symbols that already had a partial close this cycle)
        for symbol, reason_type, reason_msg in positions_to_close:
            if symbol in partial_attempted:
                log.debug(f"Skipping full close for {symbol} — partial close already attempted this cycle")
                continue
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

        if all_signals:
            log.info(f"SIGNALS GENERATED: {len(all_signals)} signals from strategies")
            for sig in all_signals:
                log.info(
                    f"  -> {sig.get('strategy')}: {sig.get('action')} {sig.get('symbol')} "
                    f"@ ${sig.get('price', 0):.2f} conf={sig.get('confidence', 0):.2f}"
                )

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

    def _check_portfolio_risk(self):
        """
        Portfolio-level risk audit. Runs every cycle.

        Checks all tracked positions (including shorts from broker sync) for:
        - Single-name concentration > 25% of net liquidation
        - Per-position loss > 8% from entry
        - Gross/net exposure breaches

        Force-closes positions that breach critical thresholds.
        Sends alerts for portfolio-level warnings.
        """
        if not self.positions:
            return

        # Get net liquidation from broker or fall back to current_balance
        net_liq = self.current_balance
        if self.broker and self.broker.is_connected():
            try:
                summary = self.broker.get_account_summary()
                if summary and summary.get("net_liquidation"):
                    net_liq = summary["net_liquidation"]
            except Exception:
                pass

        if net_liq <= 0:
            return

        # Price lookup function for the risk manager
        def get_price(symbol):
            if self.market_data:
                return self.market_data.get_price(symbol)
            return None

        # Run portfolio health audit
        with self._positions_lock:
            positions_snapshot = dict(self.positions)

        actions = self.risk_manager.check_portfolio_health(
            positions_snapshot, net_liq, get_price_fn=get_price
        )

        if not actions:
            return

        # Process actions
        force_close_symbols = set()
        for item in actions:
            severity = item.get("severity", "warning")
            reason = item["reason"]

            if item["action"] == "force_close":
                symbol = item["symbol"]
                if symbol not in force_close_symbols:
                    force_close_symbols.add(symbol)
                    log.warning(f"PORTFOLIO RISK: {reason}")
                    self.notifier.risk_alert(reason)
                    self._close_position(symbol, "portfolio_risk", reason)

            elif item["action"] == "alert":
                log.warning(f"PORTFOLIO RISK: {reason}")
                # Rate-limit alerts to avoid spam (once per 5 minutes)
                alert_key = f"portfolio_alert_{item['symbol']}"
                now = datetime.now(self.tz)
                last_alert = getattr(self, '_last_portfolio_alerts', {}).get(alert_key)
                if not last_alert or (now - last_alert).total_seconds() > 300:
                    self.notifier.risk_alert(reason)
                    if not hasattr(self, '_last_portfolio_alerts'):
                        self._last_portfolio_alerts = {}
                    self._last_portfolio_alerts[alert_key] = now

        if force_close_symbols:
            log.warning(
                f"PORTFOLIO RISK: Force-closed {len(force_close_symbols)} positions: "
                f"{', '.join(sorted(force_close_symbols))}"
            )

    def _execute_signal(self, signal):
        """Execute a trading signal through broker chain (IBKR -> TradersPost fallback)."""
        symbol = signal["symbol"]
        action = signal["action"]  # buy, sell, short, cover
        strategy = signal.get("strategy", "unknown")
        now = datetime.now(self.tz)

        # LONG-ONLY MODE: Only BUY entries allowed. Block everything else.
        # This is the last-resort guard — strategies, risk manager, and webhooks
        # should all filter before reaching here, but defense-in-depth matters.
        if action not in ("buy", "sell", "cover", "close", "exit"):
            log.warning(f"LONG-ONLY: Blocking unknown action '{action}' for {symbol}")
            return
        if action == "short":
            log.info(f"LONG-ONLY: Blocking short signal for {symbol}")
            return

        # CRYPTO BLOCK: Reject all crypto signals when crypto is disabled
        if self._is_crypto_symbol(symbol) and not self._is_crypto_enabled():
            log.warning(
                f"CRYPTO BLOCKED: {action.upper()} {symbol} rejected - "
                f"crypto trading is disabled. Strategy: {strategy}"
            )
            return

        # --- SELL/EXIT SIGNALS: Route through _close_position instead ---
        # Webhook-driven exits (TradingView sends "sell") must use the close path,
        # NOT the entry path. If we let them fall through, the position tracking at
        # the bottom creates a "short" entry, overwriting the long — catastrophic.
        if action in ("sell", "cover", "close", "exit"):
            if symbol not in self.positions:
                log.warning(
                    f"BLOCKED: {action} {symbol} - no position held. "
                    f"Preventing phantom exit signal to broker."
                )
                return
            log.info(f"Webhook exit signal: routing {action.upper()} {symbol} through close path")
            self._close_position(symbol, "webhook_exit",
                                 f"External {action} signal from {signal.get('strategy', 'webhook')}")
            return

        # --- LEARNING AVOIDANCE GUARD ---
        # Skip symbols the trade analyzer flagged as consistent losers
        if action == "buy" and self.trade_analyzer:
            if self.trade_analyzer.should_avoid_symbol(symbol):
                log.warning(
                    f"LEARNING BLOCK: {symbol} avoided — consistent loser "
                    f"(score: {self.trade_analyzer.symbol_scores.get(symbol, 0)})"
                )
                return

        # --- DUPLICATE ENTRY GUARD ---
        # Prevent same symbol from being entered twice within cooldown window
        if action == "buy":
            if symbol in self.positions:
                log.info(f"DUPLICATE BLOCKED: {symbol} already in position")
                return

            # Pending order guard: block if an order for this symbol is already in-flight
            if symbol in self._pending_orders:
                log.info(f"PENDING ORDER BLOCKED: {symbol} order already in-flight")
                return

            last_signal = self._signal_cooldowns.get(symbol)
            score = signal.get("score", 0)
            if last_signal and (now - last_signal).total_seconds() < self._signal_cooldown_secs:
                # High-conviction signals (score >= 85) bypass cooldown
                if score >= 85:
                    elapsed = int((now - last_signal).total_seconds())
                    log.info(
                        f"COOLDOWN BYPASSED: {symbol} score {score} overrides "
                        f"cooldown ({elapsed}s ago) — strong signal"
                    )
                else:
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

        # Price floor filter — no sub-$0.50 junk
        min_price = self.config.settings.get("risk", {}).get("min_price", 0.50)
        if action == "buy" and current_price < min_price:
            log.info(f"PRICE FILTER: {symbol} ${current_price:.2f} below ${min_price} floor")
            return

        # Price ceiling filter — only buy stocks in our target range
        # Scanner discovers movers under $100; don't let strategies buy $200+ stocks
        max_buy_price = self.config.settings.get("risk", {}).get("scanner_max_price", 100.0)
        if action == "buy" and current_price > max_buy_price:
            log.info(f"PRICE FILTER: {symbol} ${current_price:.2f} above ${max_buy_price} ceiling — skipping")
            return

        stop_loss_price = signal.get("stop_loss")
        if not stop_loss_price:
            # Use wider stops for crypto (more volatile)
            if self._is_crypto_symbol(symbol):
                crypto_risk = self.config.settings.get("crypto", {}).get("risk", {})
                stop_pct = crypto_risk.get("stop_loss_pct", 0.05)
            else:
                stop_pct = self.config.stop_loss_pct
            stop_loss_price = current_price * (1 - stop_pct)  # Long-only: stop is always below

        # STOP VALIDATION: Reject signals where stop is too close to entry
        # This prevents instant stop triggers from near-zero ATR estimates
        stop_distance_pct = (current_price - stop_loss_price) / current_price if current_price > 0 else 0
        if stop_distance_pct < 0.01:  # Stop must be at least 1% below entry
            log.warning(
                f"STOP TOO CLOSE: {symbol} entry=${current_price:.2f} stop=${stop_loss_price:.2f} "
                f"({stop_distance_pct:.2%} gap). Forcing 2% minimum stop."
            )
            stop_loss_price = current_price * 0.98  # Force 2% stop minimum

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

        # Momentum runner size multiplier (spike entries = 50%, afternoon = 50%)
        size_multiplier = signal.get("size_multiplier", 1.0)
        if size_multiplier < 1.0 and qty > 1:
            old_qty = qty
            qty = max(1, int(qty * size_multiplier))
            log.info(
                f"RUNNER SIZING: {symbol} size_mult={size_multiplier:.0%} — "
                f"reduced from {old_qty} to {qty} shares "
                f"(entry_type={signal.get('entry_type', 'n/a')})"
            )

        # Low-float guard: reduce position size for low-float stocks (< 20M shares)
        # These are more volatile and can gap violently
        if action == "buy" and self.polygon and self.polygon.enabled:
            float_shares = self.polygon.get_float(symbol)
            if float_shares > 0 and float_shares < 20_000_000:
                # Scale down: ultra-low float (<5M) = 40% size, low float (<20M) = 60% size
                if float_shares < 5_000_000:
                    low_float_mult = 0.40
                elif float_shares < 10_000_000:
                    low_float_mult = 0.50
                else:
                    low_float_mult = 0.60
                old_qty = qty
                qty = max(1, int(qty * low_float_mult))
                log.info(
                    f"LOW FLOAT SIZING: {symbol} float={float_shares/1e6:.1f}M — "
                    f"reduced from {old_qty} to {qty} shares ({low_float_mult:.0%})"
                )

        # Calculate take profit
        take_profit_price = signal.get("take_profit")
        if not take_profit_price:
            tp_pct = self.config.take_profit_pct
            take_profit_price = current_price * (1 + tp_pct)  # Long-only: target is always above

        # --- Broker Execution Chain ---
        # Priority: IBKR -> TradersPost -> Alpaca Direct
        order = None
        executed_via = None

        # Mark symbol as pending to block concurrent orders from webhooks/other threads
        if action == "buy":
            self._pending_orders.add(symbol)

        # Outside-RTH flag: allow pre/post market orders
        outside_rth = getattr(self, '_in_premarket', False) or getattr(self, '_in_postmarket', False)

        # 1. Try IBKR (primary broker)
        if self.broker and self.broker.is_connected():
            log.info(f"Executing {symbol} via IBKR{'  [OUTSIDE RTH]' if outside_rth else ''}...")

            # Use MARKET for entries — MIDPRICE sits unfilled on fast-moving
            # momentum stocks, then MKT sells create accidental short positions
            if action == "buy":
                order = self.broker.place_order(
                    symbol=symbol,
                    action="BUY",
                    quantity=qty,
                    order_type="MARKET",
                    outside_rth=outside_rth,
                    stop_loss=stop_loss_price,
                    take_profit=take_profit_price,
                )
            else:
                # This path should never be reached in long-only mode
                # (sell/short blocked above, exits routed to _close_position)
                log.error(f"UNEXPECTED: Non-buy action '{action}' reached IBKR execution for {symbol}")
                self._pending_orders.discard(symbol)
                return

            if order:
                executed_via = "IBKR"
                if order.get("bracket"):
                    log.info(
                        f"BRACKET ORDER active: {symbol} | "
                        f"SL: ${stop_loss_price:.2f} | TP: ${take_profit_price:.2f} "
                        f"(managed by IBKR server-side)"
                    )
                # Mirror to TradersPost for dashboard visibility
                if self.tp_broker and hasattr(self.tp_broker, 'notify_trade'):
                    try:
                        mirror_result = self.tp_broker.notify_trade({
                            "symbol": symbol,
                            "action": action,
                            "quantity": qty,
                            "price": current_price,
                            "strategy": strategy,
                        })
                        if mirror_result and mirror_result.get("success"):
                            log.info(f"TP MIRROR OK: {action.upper()} {symbol} mirrored to TradersPost")
                        else:
                            log.warning(
                                f"TP MIRROR FAILED: {action.upper()} {symbol} — "
                                f"result: {mirror_result}"
                            )
                    except Exception as e:
                        log.warning(f"TP MIRROR EXCEPTION: {action.upper()} {symbol} — {e}")
            else:
                log.warning(f"IBKR order failed for {symbol} - falling through to TradersPost")
        else:
            log.debug(f"IBKR not connected - trying TradersPost for {symbol}")

        # 2. TradersPost webhook (fallback when IBKR unavailable)
        # Alpaca pre-checks only needed for non-IBKR brokers (TradersPost routes to Alpaca)
        if not order and action == "buy" and (self.tp_broker or self.config.alpaca_api_key):
            try:
                broker_positions = self._alpaca_api_call("/v2/positions")
                if isinstance(broker_positions, list):
                    actual_count = len(broker_positions)
                    if actual_count >= self.risk_manager.max_positions:
                        log.error(
                            f"HARD LIMIT: Alpaca has {actual_count} positions "
                            f"(max {self.risk_manager.max_positions}). BLOCKING {symbol}."
                        )
                        self._pending_orders.discard(symbol)
                        return
                    broker_symbols = {p.get("symbol", "").upper() for p in broker_positions}
                    if symbol.upper() in broker_symbols:
                        log.warning(f"DUPLICATE BLOCKED: {symbol} already open at Alpaca.")
                        if symbol not in self.positions:
                            self._sync_positions_from_alpaca()
                        self._pending_orders.discard(symbol)
                        return
                    account = self._alpaca_api_call("/v2/account")
                    if isinstance(account, dict):
                        buying_power = float(account.get("buying_power", 0))
                        order_cost = current_price * qty
                        if order_cost > buying_power:
                            log.error(f"INSUFFICIENT BUYING POWER: {symbol} ${order_cost:,.2f} > ${buying_power:,.2f}. BLOCKING.")
                            self._pending_orders.discard(symbol)
                            return
            except Exception as e:
                log.warning(f"Alpaca pre-check error: {e} — proceeding (risk manager approved)")

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
                    log.warning(
                        f"TradersPost REJECTED {action.upper()} {symbol}: "
                        f"status={tp_result.get('status_code') if tp_result else 'None'} "
                        f"response={tp_result.get('response', 'no response') if tp_result else 'send_signal returned None'}"
                    )
            except Exception as e:
                log.error(f"TradersPost exception for {symbol}: {e}")

        # 3. Alpaca direct order (fallback when TradersPost rejects/unavailable)
        if not order and self.config.alpaca_api_key:
            # LONG-ONLY GUARD: Only buy entries via Alpaca. Never send sell-to-open.
            if action != "buy":
                log.error(
                    f"LONG-ONLY BLOCKED: Refusing to send '{action}' {symbol} "
                    f"to Alpaca as entry order — would create short position"
                )
                self._pending_orders.discard(symbol)
                return
            log.info(f"Trying Alpaca direct order for {action.upper()} {symbol}...")
            alpaca_result = self._place_order_via_alpaca(
                symbol=symbol, qty=qty, side="buy",
                stop_loss=stop_loss_price, take_profit=take_profit_price,
            )
            if alpaca_result and alpaca_result.get("success"):
                order_id = alpaca_result.get("order_id", f"alp_{int(datetime.now(self.tz).timestamp())}")
                # Try to verify fill (best-effort, don't block if can't confirm)
                filled = self._verify_order_fill(order_id, timeout=3)
                order = {
                    "order_id": order_id,
                    "symbol": symbol,
                    "action": action,
                    "quantity": qty,
                    "status": "filled" if filled else "submitted",
                }
                executed_via = "Alpaca-Direct"
                # Use actual fill price if available
                if isinstance(filled, dict) and filled.get("filled_avg_price"):
                    current_price = float(filled["filled_avg_price"])
                log.info(
                    f"Alpaca direct {'FILLED' if filled else 'SUBMITTED'} "
                    f"{action.upper()} {symbol} @ ${current_price:.2f}"
                )
            else:
                log.error(f"Alpaca direct order also failed for {symbol}")

        # 4. If ALL brokers failed, do NOT create a simulated phantom position
        if not order:
            log.error(
                f"ALL BROKERS FAILED for {action.upper()} {symbol} - "
                f"NO position created. "
                f"IBKR={'connected' if self.broker and self.broker.is_connected() else 'disconnected'}, "
                f"TradersPost={'configured' if self.tp_broker else 'NOT configured'}, "
                f"Alpaca={'configured' if self.config.alpaca_api_key else 'NOT configured'}"
            )
            self._pending_orders.discard(symbol)
            return

        # Use actual filled qty and price from broker (prevents partial fill mismatches)
        actual_qty = order.get("quantity", qty)  # Broker returns actual filled qty
        actual_price = order.get("avg_fill_price") or current_price  # Use fill price if available
        if actual_qty != qty:
            log.info(
                f"FILL QTY ADJUSTED: {symbol} requested {qty} but filled {actual_qty} "
                f"(partial fill). Tracking actual qty."
            )
            qty = actual_qty
        if order.get("avg_fill_price") and abs(actual_price - current_price) > 0.01:
            log.info(
                f"FILL PRICE ADJUSTED: {symbol} expected ${current_price:.2f} "
                f"but filled @ ${actual_price:.2f}"
            )
            current_price = actual_price

        risk_amount = abs(current_price - stop_loss_price) * qty
        reward_amount = abs(take_profit_price - current_price) * qty
        total_cost = current_price * qty
        rr = round(reward_amount / risk_amount, 1) if risk_amount > 0 else 0
        log.info(
            f"ORDER {action.upper()} {symbol} via {executed_via} | "
            f"Qty: {qty} | Price: ${current_price:.2f} | "
            f"Cost: ${total_cost:,.2f} | "
            f"Stop: ${stop_loss_price:.2f} (risk ${risk_amount:,.2f}) | "
            f"Target: ${take_profit_price:.2f} (reward ${reward_amount:,.2f}) | "
            f"R:R {rr}:1 | Strategy: {strategy}"
        )

        # Track position (thread-safe)
        with self._positions_lock:
            self.positions[symbol] = {
                "symbol": symbol,
                "direction": "long",  # LONG-ONLY: all positions are long (shorts blocked above)
                "quantity": qty,
                "entry_price": current_price,
                "entry_time": datetime.now(self.tz),
                "stop_loss": stop_loss_price,
                "initial_stop_loss": stop_loss_price,
                "take_profit": take_profit_price,
                "trailing_stop_pct": signal.get(
                    "trailing_stop_pct",
                    self.config.risk_config.get("trailing_stop_pct", 0.02)
                ),
                "confidence": signal.get("confidence", 0),
                "strategy": strategy,
                "order_id": order.get("order_id"),
                "executed_via": executed_via,
                "max_hold_bars": signal.get("max_hold_bars", 40),
                "bar_seconds": signal.get("bar_seconds", 300),
                "max_hold_days": signal.get("max_hold_days", 0),  # 0 = use bar-based, >0 = days limit
                # Scalp-specific metadata
                "scalp_mode": signal.get("scalp_mode", False),
                "same_candle_exit": signal.get("same_candle_exit", False),
                "quick_scalp_pct": signal.get("quick_scalp_pct", 0),
                "runner_pct": signal.get("runner_pct", 0),
                # Breakout play metadata (pre-breakout / rvol breakout signals)
                "source": signal.get("source", ""),
                "breakout_play": signal.get("breakout_play", False),
                # Momentum runner metadata (4-phase trailing stop)
                "momentum_runner": signal.get("momentum_runner", False),
                "entry_type": signal.get("entry_type", ""),
                "atr_value": signal.get("atr_value", 0),
                "size_multiplier": signal.get("size_multiplier", 1.0),
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

        # NOTE: Do NOT forward IBKR orders to TradersPost — that causes
        # duplicate execution. TradersPost is a FALLBACK, not a mirror.

        # Subscribe to tick-by-tick for ALL new positions (fastest exit monitoring —
        # fires on every trade print, not just every 5 seconds)
        if self.broker and self.broker.is_connected():
            if hasattr(self.broker, 'subscribe_tick_by_tick'):
                self.broker.subscribe_tick_by_tick([symbol], self._on_tick)
            # Also keep 5-sec bars for volume surge detection
            if hasattr(self.broker, 'subscribe_realtime_bars_with_callback'):
                self.broker.subscribe_realtime_bars_with_callback([symbol], self._on_5sec_bar)

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

        # Clear pending flag now that position is recorded
        self._pending_orders.discard(symbol)

    def _close_position(self, symbol, reason_type, reason_msg):
        """Close a position through broker chain. Thread-safe with double-close guard."""
        # Exit cooldown: skip if this symbol was recently closed
        # (prevents Alpaca sync re-add → monitor re-close → TradersPost rejection loop)
        if symbol in self._recently_closed:
            elapsed = (datetime.now(self.tz) - self._recently_closed[symbol]).total_seconds()
            if elapsed < self._exit_cooldown_secs:
                log.debug(
                    f"EXIT COOLDOWN: {symbol} closed {elapsed:.0f}s ago "
                    f"(cooldown {self._exit_cooldown_secs}s) — skipping re-close"
                )
                # Also clean up the re-added phantom position
                with self._positions_lock:
                    self.positions.pop(symbol, None)
                return

        # Double-close guard: prevent concurrent close attempts
        if symbol in self._closing_in_progress:
            log.debug(f"Close already in progress for {symbol} — skipping duplicate")
            return
        self._closing_in_progress.add(symbol)

        try:
            self._close_position_inner(symbol, reason_type, reason_msg)
        finally:
            self._closing_in_progress.discard(symbol)

    def _close_position_inner(self, symbol, reason_type, reason_msg):
        """Inner close logic — called by _close_position with double-close guard."""
        pos = self.positions.get(symbol)
        if not pos:
            return

        current_price = self.market_data.get_price(symbol)
        if current_price is None:
            current_price = pos.get("current_price", pos["entry_price"])

        # LONG-ONLY: Only sell to close long positions
        if pos["direction"] != "long":
            log.warning(
                f"CLOSE BLOCKED: {symbol} direction='{pos['direction']}' — "
                f"long-only bot won't manage short positions. Cover manually."
            )
            with self._positions_lock:
                self.positions.pop(symbol, None)
            return
        action = "SELL"
        close_qty = pos["quantity"]
        original_broker = pos.get("executed_via", "Simulated")
        close_broker = None  # Track which broker ACTUALLY closed it (None = nothing worked)

        # Verify actual broker quantity before closing to prevent accidental shorts
        if self.broker and self.broker.is_connected():
            try:
                broker_positions = self.broker.get_positions()
                broker_pos = broker_positions.get(symbol) if broker_positions else None
                if broker_pos:
                    broker_qty = broker_pos.get("quantity", 0)
                    if broker_qty <= 0:
                        log.warning(
                            f"CLOSE BLOCKED: {symbol} not held at broker (qty={broker_qty}). "
                            f"Removing phantom position."
                        )
                        with self._positions_lock:
                            self.positions.pop(symbol, None)
                        return
                    if close_qty > broker_qty:
                        log.warning(
                            f"CLOSE QTY ADJUSTED: {symbol} bot has {close_qty} but broker "
                            f"has {broker_qty}. Using broker qty to prevent short."
                        )
                        close_qty = broker_qty
                elif original_broker == "IBKR":
                    log.warning(
                        f"CLOSE BLOCKED: {symbol} not found at IBKR broker. "
                        f"Removing phantom position."
                    )
                    with self._positions_lock:
                        self.positions.pop(symbol, None)
                    return
            except Exception as e:
                log.warning(f"Could not verify broker position for {symbol}: {e}")

        # Alpaca qty verification (when IBKR is not connected)
        if not (self.broker and self.broker.is_connected()) and self.config.alpaca_api_key:
            try:
                pos_data = self._alpaca_api_call(f"/v2/positions/{symbol}")
                if pos_data and isinstance(pos_data, dict):
                    alpaca_qty = abs(int(float(pos_data.get("qty", 0))))
                    alpaca_side_long = float(pos_data.get("qty", 0)) > 0
                    if not alpaca_side_long:
                        log.error(
                            f"CLOSE BLOCKED: {symbol} is SHORT at Alpaca — "
                            f"long-only bot won't touch. Cover manually."
                        )
                        with self._positions_lock:
                            self.positions.pop(symbol, None)
                        return
                    if alpaca_qty <= 0:
                        log.warning(f"CLOSE BLOCKED: {symbol} 0 shares at Alpaca. Removing phantom.")
                        with self._positions_lock:
                            self.positions.pop(symbol, None)
                        return
                    if close_qty > alpaca_qty:
                        log.warning(
                            f"CLOSE QTY ADJUSTED: {symbol} bot has {close_qty} but Alpaca "
                            f"holds {alpaca_qty}. Capping to prevent short."
                        )
                        close_qty = alpaca_qty
                elif pos_data == self._ALPACA_NOT_FOUND:
                    log.warning(f"CLOSE BLOCKED: {symbol} not found at Alpaca. Removing phantom.")
                    with self._positions_lock:
                        self.positions.pop(symbol, None)
                    return
            except Exception as e:
                log.warning(f"Could not verify Alpaca position for {symbol}: {e}")

        # Try to close via broker chain
        order = None
        outside_rth = getattr(self, '_in_premarket', False) or getattr(self, '_in_postmarket', False)
        if self.broker and self.broker.is_connected():
            order = self.broker.place_order(
                symbol=symbol,
                action=action,
                quantity=close_qty,
                order_type="MARKET",
                outside_rth=outside_rth,
            )
            if order:
                close_broker = "IBKR"
                # Mirror exit to TradersPost for dashboard visibility
                if self.tp_broker and hasattr(self.tp_broker, 'notify_trade'):
                    try:
                        mirror_result = self.tp_broker.notify_trade({
                            "symbol": symbol,
                            "action": "exit",
                            "quantity": close_qty,
                            "price": current_price,
                            "source": "exit",
                        })
                        if mirror_result and mirror_result.get("success"):
                            log.info(f"TP MIRROR OK: EXIT {symbol} mirrored to TradersPost")
                        else:
                            log.warning(f"TP MIRROR FAILED: EXIT {symbol} — result: {mirror_result}")
                    except Exception as e:
                        log.warning(f"TP MIRROR EXCEPTION: EXIT {symbol} — {e}")

        if not order:
            # Always try Alpaca direct close FIRST — Alpaca is the actual broker
            # and source of truth.
            alpaca_closed = False
            alpaca_exists = self._alpaca_position_exists(symbol)  # Single API call

            if alpaca_exists is True:
                log.info(f"Closing {symbol} via Alpaca direct API...")
                alpaca_result = self._close_via_alpaca(symbol)
                if alpaca_result and alpaca_result.get("success"):
                    close_broker = "Alpaca-Direct"
                    alpaca_closed = True
                else:
                    log.warning(
                        f"Alpaca direct close failed for {symbol}: {alpaca_result} "
                        f"— trying TradersPost..."
                    )
            elif alpaca_exists is False:
                # Position confirmed NOT at Alpaca — phantom, clean up internally
                log.warning(
                    f"PHANTOM POSITION detected: {symbol} exists in bot but NOT "
                    f"in broker. Cleaning up internal state."
                )
                close_broker = "Phantom-Cleanup"
                alpaca_closed = True
            else:
                # alpaca_exists is None — API call failed, can't confirm
                log.warning(
                    f"Alpaca position check failed for {symbol} (API error) "
                    f"— trying TradersPost fallback..."
                )

            # If Alpaca direct didn't work, try TradersPost as fallback
            if not alpaca_closed and self.tp_broker:
                log.info(f"Closing {symbol} via TradersPost webhook (fallback)...")
                close_signal = {
                    "symbol": symbol,
                    "action": "exit",
                    "quantity": pos["quantity"],
                    "price": current_price,
                    "source": "exit",
                }
                try:
                    tp_result = self.tp_broker.send_signal(close_signal)
                    if tp_result and tp_result.get("success"):
                        close_broker = "TradersPost"
                except Exception as e:
                    log.error(f"TradersPost fallback close exception for {symbol}: {e}")

            if not close_broker:
                log.error(f"ALL close attempts failed for {symbol}")

            # Best-effort: notify TradersPost so it can sync its state.
            # Only send if the original position was entered via TradersPost
            # (otherwise TradersPost has nothing to close, causing rejections).
            if alpaca_closed and self.tp_broker and close_broker == "Alpaca-Direct":
                if original_broker in ("TradersPost",):
                    try:
                        close_signal = {
                            "symbol": symbol, "action": "exit",
                            "quantity": pos["quantity"], "price": current_price,
                            "source": "exit",
                        }
                        tp_result = self.tp_broker.send_signal(close_signal)
                        if tp_result and tp_result.get("rejected"):
                            log.debug(
                                f"TradersPost sync notification for {symbol} was rejected "
                                f"(position may already be closed at TP) — ignoring"
                            )
                    except Exception:
                        pass  # Best-effort notification
                else:
                    log.debug(
                        f"Skipping TradersPost sync for {symbol} — "
                        f"original broker was {original_broker}, not TradersPost"
                    )

        # Only remove position if a broker ACTUALLY closed it this cycle
        if not close_broker:
            log.error(
                f"CLOSE FAILED for {symbol} — position stays tracked for retry next cycle. "
                f"original_broker={original_broker}"
            )
            return

        executed_via = close_broker

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
            strategy=pos.get("strategy", "unknown"),
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
            "strategy": pos.get("strategy", "unknown"),
            "reason": reason_type,
            "reason_detail": reason_msg,
            "executed_via": executed_via,
            "entry_time": pos["entry_time"].isoformat() if "entry_time" in pos else datetime.now(self.tz).isoformat(),
            "exit_time": datetime.now(self.tz).isoformat(),
            "hold_time_mins": round(hold_time.total_seconds() / 60, 1) if hold_time else None,
            "regime": getattr(self, "current_regime", "unknown"),
            "entry_confidence": pos.get("confidence", 0),
            "initial_stop": pos.get("initial_stop_loss"),
            "final_stop": pos.get("stop_loss"),
            "overnight_hold": pos.get("overnight_hold", False),
            "afterhours_hold": pos.get("afterhours_hold", False),
            "source": pos.get("source", ""),
        })

        # Update win/loss stats
        self._update_performance_stats(pnl)

        # Persist trade to disk (survives restarts for AI learning)
        if self.trade_analyzer:
            self.trade_analyzer.persist_trade(self.trade_history[-1])

        # Auto-trigger Claude AI quick insight every 5 trades
        if self.ai_insights and self.ai_insights.is_available() and len(self.trade_history) % 5 == 0:
            try:
                insight = self.ai_insights.get_quick_insight(
                    self.trade_history, self.performance_stats
                )
                if insight:
                    log.info(f"AI INSIGHT: {insight[:200]}")
                    self.notifier.system_alert(
                        f"AI Quick Insight (after {len(self.trade_history)} trades):\n{insight}",
                        level="info"
                    )
            except Exception as e:
                log.debug(f"AI quick insight error: {e}")

        # Log trade to Google Sheets
        if self.sheets_logger and self.sheets_logger.is_enabled():
            self.sheets_logger.log_trade(self.trade_history[-1])

        # Update watchlist performance tracking
        if symbol in self.watchlist:
            self._update_watchlist_performance(symbol, pnl, pnl_pct)

        with self._positions_lock:
            self.positions.pop(symbol, None)

        # Clean up tick-by-tick subscription for closed position
        if self.broker and hasattr(self.broker, 'unsubscribe_tick_by_tick'):
            try:
                self.broker.unsubscribe_tick_by_tick([symbol])
            except Exception:
                pass

        # Record exit cooldown — prevents Alpaca sync from re-adding this
        # position during settlement delay (causes duplicate exit rejections)
        self._recently_closed[symbol] = datetime.now(self.tz)

    def _partial_close(self, symbol, qty_to_close, target_idx, target):
        """Close part of a position (profit taking)."""
        pos = self.positions.get(symbol)
        if not pos or qty_to_close <= 0:
            return

        # Double-close guard: prevent concurrent close attempts (same guard as _close_position)
        if symbol in self._closing_in_progress:
            log.debug(f"Close already in progress for {symbol} — skipping partial close")
            return
        self._closing_in_progress.add(symbol)

        try:
            self._partial_close_inner(symbol, qty_to_close, target_idx, target, pos)
        finally:
            self._closing_in_progress.discard(symbol)

    def _partial_close_inner(self, symbol, qty_to_close, target_idx, target, pos):
        """Inner partial close logic — called with double-close guard held."""

        current_price = self.market_data.get_price(symbol)
        if current_price is None:
            current_price = pos.get("current_price", pos["entry_price"])

        # LONG-ONLY: Only sell to close long positions
        if pos["direction"] != "long":
            log.warning(f"PARTIAL CLOSE BLOCKED: {symbol} direction='{pos['direction']}' — long-only bot won't manage shorts")
            return
        action = "SELL"
        close_broker = None  # Track which broker ACTUALLY closed it

        # Verify actual broker qty before partial close to prevent overselling
        actual_broker_qty = None
        if self.config.alpaca_api_key and self.config.alpaca_secret_key:
            try:
                pos_data = self._alpaca_api_call(f"/v2/positions/{symbol}")
                if isinstance(pos_data, dict):
                    actual_broker_qty = abs(int(float(pos_data.get("qty", 0))))
                    if actual_broker_qty <= 0:
                        log.warning(f"PARTIAL CLOSE BLOCKED: {symbol} not held at Alpaca (0 shares)")
                        return
                    if qty_to_close > actual_broker_qty:
                        log.warning(
                            f"PARTIAL CLOSE QTY CAPPED: {symbol} requested {qty_to_close} "
                            f"but Alpaca holds {actual_broker_qty}. Capping to prevent short."
                        )
                        qty_to_close = actual_broker_qty
            except Exception as e:
                log.debug(f"Could not verify Alpaca position for partial close {symbol}: {e}")

        # Execute via broker chain
        order = None
        outside_rth = getattr(self, '_in_premarket', False) or getattr(self, '_in_postmarket', False)
        if self.broker and self.broker.is_connected():
            order = self.broker.place_order(
                symbol=symbol, action=action,
                quantity=qty_to_close, order_type="MARKET",
                outside_rth=outside_rth,
            )
            if order:
                close_broker = "IBKR"

        if not order:
            # Alpaca-first: close directly at the broker
            alpaca_exists = self._alpaca_position_exists(symbol)
            if alpaca_exists is True:
                alpaca_result = self._close_via_alpaca(symbol, qty=qty_to_close)
                if alpaca_result and alpaca_result.get("success"):
                    close_broker = "Alpaca-Direct"

            if not close_broker and self.tp_broker:
                close_signal = {
                    "symbol": symbol, "action": "exit",
                    "quantity": qty_to_close, "price": current_price,
                    "source": "exit",
                }
                tp_result = self.tp_broker.send_signal(close_signal)
                if tp_result and tp_result.get("success"):
                    close_broker = "TradersPost"

        # If nothing actually closed, don't update position
        if not close_broker:
            log.error(f"PARTIAL CLOSE FAILED for {symbol} — no broker executed")
            return

        executed_via = close_broker

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
            strategy=pos.get("strategy", "unknown"),
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
            "strategy": pos.get("strategy", "unknown"),
            "reason": f"partial_target_{target_idx + 1}",
            "executed_via": executed_via,
            "entry_time": pos["entry_time"].isoformat() if "entry_time" in pos else datetime.now(self.tz).isoformat(),
            "exit_time": datetime.now(self.tz).isoformat(),
            "partial": True,
        })

        # Persist partial trade
        if self.trade_analyzer:
            self.trade_analyzer.persist_trade(self.trade_history[-1])
        if self.sheets_logger and self.sheets_logger.is_enabled():
            self.sheets_logger.log_trade(self.trade_history[-1])

        # Update performance stats
        self._update_performance_stats(pnl)

        # If position fully closed via partials
        if pos["quantity"] <= 0:
            with self._positions_lock:
                self.positions.pop(symbol, None)

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

        # Cancel all pending IBKR orders first (bracket stop/target orders)
        if self.broker and self.broker.is_connected() and hasattr(self.broker, 'cancel_all_orders'):
            self.broker.cancel_all_orders()

        with self._positions_lock:
            symbols = list(self.positions.keys())
        for symbol in symbols:
            self._close_position(symbol, "emergency", reason)

    def _update_account(self):
        """Update account balance and tracking (works with or without IBKR)."""
        # Try to sync from Alpaca first (source of truth on Render)
        if self.config.alpaca_api_key:
            try:
                account = self._alpaca_api_call("/v2/account")
                if account and isinstance(account, dict):
                    equity = float(account.get("equity", 0))
                    if equity > 0:
                        self.current_balance = equity
            except Exception:
                pass
        elif self.broker and self.broker.is_connected():
            account = self.broker.get_account_summary()
            if account:
                self.current_balance = account.get(
                    "net_liquidation", self.current_balance
                )

            # Use IBKR native real-time PnL if available (more accurate than manual calc)
            if hasattr(self.broker, 'get_realtime_pnl'):
                pnl_data = self.broker.get_realtime_pnl()
                if pnl_data:
                    self.daily_pnl = pnl_data.get("daily", self.daily_pnl)
                    self._ibkr_unrealized_pnl = pnl_data.get("unrealized", 0)
                    self._ibkr_realized_pnl = pnl_data.get("realized", 0)

        self.peak_balance = max(self.peak_balance, self.current_balance)

        # Update scaling tier (both risk manager AND position sizer)
        tier = self.config.get_scaling_tier(self.current_balance)
        if tier:
            self.risk_manager.update_tier(tier)
            self.position_sizer.update_tier(tier)

        # Track equity curve (include unrealized P&L)
        unrealized_pnl = 0
        with self._positions_lock:
            positions_snapshot = dict(self.positions)
        for symbol, pos in positions_snapshot.items():
            price = self.market_data.get_price(symbol) if self.market_data else None
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
            "positions": len(positions_snapshot),
            "daily_pnl": self.daily_pnl,
        })

    # --- Alpaca Helpers ---

    def _verify_order_fill(self, order_id, timeout=5):
        """Check if an Alpaca order has filled within timeout seconds.
        Returns order dict if filled, False if not filled, None on error."""
        if not order_id:
            return False
        import requests as _req
        api_key = self.config.alpaca_api_key
        secret_key = self.config.alpaca_secret_key
        base_url = getattr(self.config, 'alpaca_base_url', 'https://paper-api.alpaca.markets')
        headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
        }
        # Poll order status every 0.5s up to timeout
        checks = int(timeout / 0.5)
        for _ in range(max(checks, 1)):
            try:
                resp = _req.get(
                    f"{base_url}/v2/orders/{order_id}",
                    headers=headers,
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get("status", "")
                    if status == "filled":
                        return data
                    elif status in ("canceled", "expired", "rejected"):
                        log.warning(f"Order {order_id} {status}")
                        return False
                    # Still pending — wait and retry
                time.sleep(0.5)
            except Exception as e:
                log.debug(f"Order fill check error: {e}")
                time.sleep(0.5)
        # Timeout — order may still be pending
        log.warning(f"Order {order_id} not filled within {timeout}s")
        return False

    def _get_time_in_force(self):
        """Return 'day' during regular market hours, 'gtc' otherwise.
        Crypto always uses 'gtc' since it trades 24/7."""
        now = datetime.now(self.tz)
        # Crypto is always GTC
        # During regular hours (9:30-16:00 ET, Mon-Fri), use DAY
        day_name = now.strftime("%A")
        if day_name in ("Saturday", "Sunday"):
            return "gtc"
        regular_open = now.replace(hour=9, minute=30, second=0)
        regular_close = now.replace(hour=16, minute=0, second=0)
        if regular_open <= now <= regular_close:
            return "day"
        return "gtc"

    # --- Alpaca Position Sync (prevents phantom positions) ---

    # Sentinel for 404 responses — distinct from None (error) and [] (empty list)
    _ALPACA_NOT_FOUND = "NOT_FOUND"

    def _alpaca_api_call(self, endpoint, method="GET"):
        """Make a raw HTTP call to Alpaca Trading API.
        No alpaca_trade_api library needed - uses requests directly.
        Returns:
          - Parsed JSON on success (200)
          - _ALPACA_NOT_FOUND sentinel on 404
          - None on any other error (timeout, 500, etc.)
        Callers MUST check for None before using the result."""
        api_key = self.config.alpaca_api_key
        secret_key = self.config.alpaca_secret_key
        if not api_key or not secret_key:
            return None
        base_url = getattr(self.config, 'alpaca_base_url', 'https://paper-api.alpaca.markets')
        try:
            import requests as _req
            resp = _req.request(
                method,
                f"{base_url}{endpoint}",
                headers={
                    "APCA-API-KEY-ID": api_key,
                    "APCA-API-SECRET-KEY": secret_key,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 404:
                return self._ALPACA_NOT_FOUND
            else:
                log.debug(f"Alpaca API {endpoint}: HTTP {resp.status_code}")
                return None
        except Exception as e:
            log.debug(f"Alpaca API {endpoint} error: {e}")
            return None

    def _place_order_via_alpaca(self, symbol, qty, side, limit_price=None,
                                stop_loss=None, take_profit=None):
        """Place an entry order directly via Alpaca API.
        Uses market order by default, limit order if limit_price provided."""
        # LONG-ONLY GUARD: This function is for ENTRIES only.
        # Never allow sell-side entries (would create short positions).
        if side != "buy":
            log.error(
                f"SHORT-SELL BLOCKED: _place_order_via_alpaca called with side='{side}' "
                f"for {symbol}. Long-only bot refuses to create short position."
            )
            return None
        api_key = self.config.alpaca_api_key
        secret_key = self.config.alpaca_secret_key
        if not api_key or not secret_key:
            return None
        try:
            import requests as _req
            base_url = getattr(self.config, 'alpaca_base_url', 'https://paper-api.alpaca.markets')
            headers = {
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": secret_key,
                "Content-Type": "application/json",
            }
            # Simple market order — the bot manages all exits (stops, trailing,
            # profit taking) internally. Don't use bracket orders to avoid
            # dual-control conflicts with the bot's exit monitoring.
            # Use 'gtc' outside regular hours (pre-market/after-hours), 'day' during market
            tif = self._get_time_in_force()
            extended = getattr(self, "_in_premarket", False) or getattr(self, "_in_postmarket", False)
            order_data = {
                "symbol": symbol,
                "qty": str(qty),
                "side": side,
                "type": "market",
                "time_in_force": tif,
            }
            # Extended hours require 'limit' orders on Alpaca, not 'market'
            # Auto-convert: use current price + small buffer to ensure fill
            if extended or (tif == "gtc" and limit_price):
                if not limit_price:
                    # Fetch current price for limit order
                    try:
                        snap = _req.get(
                            f"{base_url}/v2/snapshot?symbols={symbol}",
                            headers=headers, timeout=5,
                        )
                        if snap.status_code == 200:
                            snap_data = snap.json().get(symbol, {})
                            last = float(snap_data.get("latestTrade", {}).get("p", 0))
                            if last > 0:
                                # Add 0.5% buffer for buys, subtract for sells
                                buffer = 0.005
                                limit_price = last * (1 + buffer) if side == "buy" else last * (1 - buffer)
                    except Exception:
                        pass
                if limit_price:
                    order_data["type"] = "limit"
                    order_data["limit_price"] = str(round(limit_price, 2))
                    order_data["extended_hours"] = True
                    order_data["time_in_force"] = "day"  # Alpaca requires 'day' for extended_hours
                    log.info(f"Extended hours limit order: {side} {symbol} @ ${limit_price:.2f}")

            resp = _req.post(
                f"{base_url}/v2/orders",
                headers=headers,
                json=order_data,
                timeout=10,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                log.info(
                    f"ALPACA DIRECT ORDER: {side.upper()} {qty} {symbol} "
                    f"| order_id={data.get('id', 'unknown')} status={data.get('status')}"
                )
                return {
                    "success": True,
                    "order_id": data.get("id"),
                    "status": data.get("status"),
                    "method": "alpaca_direct",
                }
            else:
                log.error(
                    f"ALPACA DIRECT ORDER FAILED: {side.upper()} {symbol} "
                    f"HTTP {resp.status_code} | {resp.text[:200]}"
                )
                return {"success": False, "status_code": resp.status_code}
        except Exception as e:
            log.error(f"ALPACA DIRECT ORDER exception for {symbol}: {e}")
            return None

    def _close_via_alpaca(self, symbol, qty=None, side="sell"):
        """Close a position directly via Alpaca API.
        First cancels any pending orders for the symbol, then closes.
        Full close: DELETE /v2/positions/{symbol}
        Partial close: DELETE /v2/positions/{symbol}?qty={qty}"""
        api_key = self.config.alpaca_api_key
        secret_key = self.config.alpaca_secret_key
        if not api_key or not secret_key:
            return None
        try:
            import requests as _req
            base_url = getattr(self.config, 'alpaca_base_url', 'https://paper-api.alpaca.markets')
            headers = {
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": secret_key,
            }

            # Step 1: Cancel ALL pending orders for this symbol first
            # Alpaca rejects position close if there are pending orders
            try:
                orders_resp = _req.get(
                    f"{base_url}/v2/orders",
                    headers=headers,
                    params={"status": "open", "symbols": symbol},
                    timeout=10,
                )
                if orders_resp.status_code == 200:
                    open_orders = orders_resp.json()
                    for order in open_orders:
                        order_id = order.get("id")
                        if order_id:
                            _req.delete(
                                f"{base_url}/v2/orders/{order_id}",
                                headers=headers,
                                timeout=5,
                            )
                            log.info(f"Cancelled pending order {order_id} for {symbol} before close")
            except Exception as e:
                log.warning(f"Could not cancel orders for {symbol} before close: {e}")

            # Step 2: Try DELETE /v2/positions/{symbol} first
            url = f"{base_url}/v2/positions/{symbol}"
            if qty:
                url += f"?qty={qty}"
            resp = _req.delete(url, headers=headers, timeout=10)
            if resp.status_code in (200, 204):
                close_type = f"partial ({qty} shares)" if qty else "full"
                log.info(f"ALPACA DIRECT CLOSE: {symbol} {close_type} closed (HTTP {resp.status_code})")
                return {"success": True, "method": "alpaca_delete", "status_code": resp.status_code}

            log.warning(
                f"ALPACA DELETE failed for {symbol} (HTTP {resp.status_code}: {resp.text[:100]})"
                f" — trying POST /v2/orders sell fallback..."
            )

            # Step 3: Fallback — submit a market sell order directly
            # DELETE can fail if there are order conflicts; a direct sell always works
            pos_resp = _req.get(
                f"{base_url}/v2/positions/{symbol}",
                headers=headers,
                timeout=5,
            )
            if pos_resp.status_code == 200:
                pos_data = pos_resp.json()
                broker_qty = abs(int(float(pos_data.get("qty", 0))))
                broker_side_long = float(pos_data.get("qty", 0)) > 0

                # LONG-ONLY GUARD: Refuse to close short positions at Alpaca
                # (they shouldn't exist — if they do, manual intervention needed)
                if not broker_side_long:
                    log.error(
                        f"SHORT POSITION DETECTED at Alpaca: {symbol} qty={pos_data.get('qty')}. "
                        f"Long-only bot will NOT touch this. Cover manually."
                    )
                    return {"success": False, "reason": "short_position_at_broker"}

                # Cap requested qty to actual broker position to prevent overselling
                if qty and qty > broker_qty:
                    log.warning(
                        f"SELL QTY CAPPED: {symbol} requested {qty} but Alpaca "
                        f"holds {broker_qty}. Capping to prevent short."
                    )
                    qty = broker_qty
                if broker_qty <= 0:
                    log.warning(f"ALPACA CLOSE: {symbol} has 0 shares at broker — nothing to close")
                    return {"success": False, "reason": "no_position"}

                sell_qty = str(qty) if qty else str(broker_qty)
                sell_side = "sell"
                extended = getattr(self, "_in_premarket", False) or getattr(self, "_in_postmarket", False)
                sell_order = {
                    "symbol": symbol,
                    "qty": sell_qty,
                    "side": sell_side,
                    "type": "market",
                    "time_in_force": self._get_time_in_force(),
                }
                # Extended hours: convert to limit order
                if extended:
                    cur_price = float(pos_data.get("current_price", 0))
                    if cur_price > 0:
                        buffer = 0.005
                        lp = cur_price * (1 - buffer) if sell_side == "sell" else cur_price * (1 + buffer)
                        sell_order["type"] = "limit"
                        sell_order["limit_price"] = str(round(lp, 2))
                        sell_order["extended_hours"] = True
                        sell_order["time_in_force"] = "day"
                sell_resp = _req.post(
                    f"{base_url}/v2/orders",
                    headers={**headers, "Content-Type": "application/json"},
                    json=sell_order,
                    timeout=10,
                )
                if sell_resp.status_code in (200, 201):
                    log.info(
                        f"ALPACA SELL ORDER: {symbol} {sell_qty} shares submitted "
                        f"(HTTP {sell_resp.status_code})"
                    )
                    return {"success": True, "method": "alpaca_sell_order", "status_code": sell_resp.status_code}
                else:
                    log.error(
                        f"ALPACA SELL ORDER ALSO FAILED: {symbol} HTTP {sell_resp.status_code} "
                        f"| {sell_resp.text[:200]}"
                    )

            return {"success": False, "status_code": resp.status_code}
        except Exception as e:
            log.error(f"ALPACA DIRECT CLOSE exception for {symbol}: {e}")
            return None

    def _alpaca_position_exists(self, symbol):
        """Check if a position exists on the Alpaca broker side via raw HTTP.
        Returns True/False, or None if unable to check."""
        api_key = self.config.alpaca_api_key
        secret_key = self.config.alpaca_secret_key
        if not api_key or not secret_key:
            return None
        try:
            import requests as _req
            base_url = getattr(self.config, 'alpaca_base_url', 'https://paper-api.alpaca.markets')
            resp = _req.get(
                f"{base_url}/v2/positions/{symbol}",
                headers={
                    "APCA-API-KEY-ID": api_key,
                    "APCA-API-SECRET-KEY": secret_key,
                },
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                return abs(float(data.get("qty", 0))) > 0
            elif resp.status_code == 404:
                return False
            return None
        except Exception:
            return None

    def _sync_positions_from_alpaca(self):
        """On startup (Render), load actual positions from Alpaca so internal
        state matches reality. Also syncs account balance so position sizing
        is based on real equity, not the static settings.yaml value.
        Uses raw HTTP - no alpaca_trade_api library needed."""
        if not self.config.alpaca_api_key or not self.config.alpaca_secret_key:
            log.info("Alpaca credentials not set - starting with empty positions")
            return

        # --- Sync account balance from Alpaca ---
        try:
            account = self._alpaca_api_call("/v2/account")
            if isinstance(account, dict):
                equity = float(account.get("equity", 0))
                buying_power = float(account.get("buying_power", 0))
                cash = float(account.get("cash", 0))
                if equity > 0:
                    self.current_balance = equity
                    self.peak_balance = max(self.peak_balance, equity)
                    self.start_of_day_balance = equity
                    log.info(
                        f"Alpaca account synced: equity=${equity:,.2f} | "
                        f"cash=${cash:,.2f} | buying_power=${buying_power:,.2f}"
                    )
                else:
                    log.warning("Alpaca returned $0 equity - keeping config starting_balance")
            else:
                log.warning("Alpaca account API returned no data")
        except Exception as e:
            log.warning(f"Alpaca account balance sync failed: {e}")

        try:
            broker_positions = self._alpaca_api_call("/v2/positions")
            # FAIL-SAFE: If API returned None (error) or NOT_FOUND sentinel,
            # do NOT assume 0 positions — skip sync to avoid wiping tracking
            if broker_positions is None or broker_positions == self._ALPACA_NOT_FOUND:
                log.warning("Alpaca startup sync: API returned no data — skipping position sync")
                return
            if not isinstance(broker_positions, list):
                log.warning(f"Alpaca startup sync: unexpected response type {type(broker_positions)} — skipping")
                return

            # Sort by unrealized P&L descending — best performers get tracked first
            # so if we hit the position cap, we keep winners and flag losers
            try:
                broker_positions.sort(
                    key=lambda p: float(p.get("unrealized_plpc", 0) or 0),
                    reverse=True,
                )
            except (ValueError, TypeError):
                pass  # If sort fails, use original order

            max_pos = self.risk_manager.max_positions if hasattr(self, 'risk_manager') else 25
            over_limit_positions = []

            # Pre-fetch pending orders to avoid syncing positions that are being sold
            pending_sell_symbols = set()
            try:
                open_orders = self._alpaca_api_call("/v2/orders?status=open")
                if isinstance(open_orders, list):
                    for o in open_orders:
                        if o.get("side") == "sell" and o.get("status") in ("new", "accepted", "pending_new"):
                            pending_sell_symbols.add(o.get("symbol", "").upper())
                    if pending_sell_symbols:
                        log.info(f"Startup sync: found pending SELL orders for: {', '.join(pending_sell_symbols)}")
            except Exception as e:
                log.debug(f"Could not check pending orders during sync: {e}")

            for p in broker_positions:
                symbol = p.get("symbol", "").upper()
                qty = abs(float(p.get("qty", 0)))
                entry = float(p.get("avg_entry_price", 0))
                side = "long" if float(p.get("qty", 0)) > 0 else "short"

                # AUTO-CLOSE short positions — long-only bot should NEVER have shorts.
                # Instead of just skipping (which leaves the short open), cover it.
                if side == "short":
                    log.error(
                        f"SHORT DETECTED at Alpaca: {symbol} ({int(qty)} shares). "
                        f"Auto-covering — long-only bot must not hold shorts."
                    )
                    try:
                        self._close_via_alpaca(symbol)
                        log.info(f"AUTO-COVERED short: {symbol}")
                        self.notifier.risk_alert(
                            f"AUTO-COVERED short position: {symbol} ({int(qty)} shares) "
                            f"at Alpaca. Long-only bot should never have shorts."
                        )
                    except Exception as e:
                        log.error(f"Failed to auto-cover short {symbol}: {e}")
                    continue

                # Skip positions with pending sell orders — they're being closed
                if symbol in pending_sell_symbols:
                    log.info(
                        f"SYNC: Skipping {symbol} — has pending SELL order "
                        f"(position is being closed)"
                    )
                    continue

                unrealized_pnl_pct = float(p.get("unrealized_plpc", 0) or 0)
                # Use current market price for smarter stop/target
                current_mkt = None
                if self.market_data:
                    try:
                        current_mkt = self.market_data.get_price(symbol)
                    except Exception:
                        pass
                ref_price = current_mkt or entry
                is_crypto = any(symbol.upper().endswith(s) for s in ["-USD", "-USDT"])
                stop_pct = 0.05 if is_crypto else self.config.risk_config.get("stop_loss_pct", 0.03)
                tp_pct = 0.08 if is_crypto else self.config.risk_config.get("take_profit_pct", 0.20)
                self.positions[symbol] = {
                    "symbol": symbol,
                    "direction": side,
                    "quantity": int(qty) if qty and qty == qty and qty == int(qty) else (qty if qty and qty == qty else 0),
                    "entry_price": entry,
                    "entry_time": datetime.now(self.tz),
                    "stop_loss": ref_price * (1 - stop_pct) if side == "long" else ref_price * (1 + stop_pct),
                    "take_profit": ref_price * (1 + tp_pct) if side == "long" else ref_price * (1 - tp_pct),
                    "trailing_stop_pct": self.config.risk_config.get("trailing_stop_pct", 0.02),
                    "strategy": "synced_from_alpaca",
                    "executed_via": "Alpaca",
                    "max_hold_bars": 40,
                    "bar_seconds": 300,
                    "max_hold_days": 5,  # Synced positions: 5-day default hold limit
                    "unrealized_pnl_pct": unrealized_pnl_pct,
                }

                # Track positions over the limit for auto-trim
                if len(self.positions) > max_pos:
                    over_limit_positions.append((symbol, unrealized_pnl_pct))

            if broker_positions:
                log.info(f"Synced {len(broker_positions)} positions from Alpaca on startup")
            else:
                log.info("Alpaca reports 0 open positions")

            # --- POSITION CAP ENFORCEMENT ---
            # If broker has more positions than max_positions, flag the weakest
            # for immediate closure. This prevents the 50+ position problem.
            if over_limit_positions:
                over_count = len(self.positions) - max_pos
                log.warning(
                    f"OVER POSITION LIMIT: {len(self.positions)} positions synced "
                    f"(max: {max_pos}). Will close {over_count} weakest on first cycle."
                )
                # Sort over-limit by P&L ascending (worst first)
                over_limit_positions.sort(key=lambda x: x[1])
                symbols_to_close = [s for s, _ in over_limit_positions[:over_count]]
                # Mark for immediate closure on first main loop cycle
                self._startup_trim_queue = symbols_to_close
                self.notifier.risk_alert(
                    f"Position cap exceeded: {len(self.positions)} synced from Alpaca "
                    f"(max: {max_pos}). Queuing {over_count} weakest for closure: "
                    f"{', '.join(symbols_to_close[:10])}"
                    f"{'...' if len(symbols_to_close) > 10 else ''}"
                )
        except Exception as e:
            log.warning(f"Alpaca startup sync failed: {e}")

    def _sync_positions_with_broker(self):
        """Reconcile internal positions with actual Alpaca broker positions.
        Removes phantom positions that exist internally but not at the broker.
        Uses raw HTTP - no alpaca_trade_api library needed.
        CRITICAL: If the API call fails, do NOT touch positions (fail-safe).
        Thread-safe: uses _positions_lock for all position dict mutations."""
        if not self.config.alpaca_api_key or not self.config.alpaca_secret_key:
            return

        try:
            broker_positions = self._alpaca_api_call("/v2/positions")
        except Exception as e:
            log.warning(f"Alpaca position sync failed: {e}")
            return

        # FAIL-SAFE: If API returned None (error) or NOT_FOUND, do NOT wipe positions.
        if broker_positions is None or broker_positions == self._ALPACA_NOT_FOUND:
            log.warning("Alpaca position sync: API returned None/NotFound — skipping sync to avoid wiping tracking")
            return
        if not isinstance(broker_positions, list):
            log.warning(f"Alpaca position sync: unexpected response type {type(broker_positions)} — skipping")
            return

        broker_symbols = {p.get("symbol", "").upper() for p in broker_positions}

        # Clean up expired entries from _recently_closed (older than cooldown window)
        now = datetime.now(self.tz)
        expired = [s for s, t in self._recently_closed.items()
                   if (now - t).total_seconds() > self._exit_cooldown_secs]
        for s in expired:
            del self._recently_closed[s]

        with self._positions_lock:
            max_pos = self.risk_manager.max_positions if hasattr(self, 'risk_manager') else 25

            # Also sync positions that exist at broker but NOT in bot tracking
            for p in broker_positions:
                sym = p.get("symbol", "").upper()
                if sym and sym not in self.positions:
                    # Don't re-add positions if we're already at or over the cap
                    if len(self.positions) >= max_pos:
                        log.debug(
                            f"POSITION SYNC: Skipping {sym} — at position cap "
                            f"({len(self.positions)}/{max_pos})"
                        )
                        continue

                    # Skip symbols that were recently closed (settlement delay
                    # can cause Alpaca to still show positions we already exited,
                    # re-adding them causes duplicate exit signals to TradersPost)
                    if sym in self._recently_closed:
                        elapsed = (now - self._recently_closed[sym]).total_seconds()
                        log.debug(
                            f"POSITION SYNC: Skipping {sym} — closed {elapsed:.0f}s ago "
                            f"(cooldown {self._exit_cooldown_secs}s)"
                        )
                        continue

                    qty = abs(float(p.get("qty", 0)))
                    entry = float(p.get("avg_entry_price", 0))
                    side = "long" if float(p.get("qty", 0)) > 0 else "short"

                    # Auto-close short positions found during continuous sync
                    if side == "short":
                        log.error(
                            f"SHORT DETECTED during sync: {sym} ({int(qty)} shares). "
                            f"Auto-covering immediately."
                        )
                        try:
                            self._close_via_alpaca(sym)
                            log.info(f"AUTO-COVERED short during sync: {sym}")
                        except Exception as e:
                            log.error(f"Failed to auto-cover short {sym}: {e}")
                        continue

                    self.positions[sym] = {
                        "symbol": sym,
                        "direction": side,
                        "quantity": int(qty) if qty and qty == qty and qty == int(qty) else (qty if qty and qty == qty else 0),
                        "entry_price": entry,
                        "entry_time": datetime.now(self.tz),
                        "stop_loss": entry * (1 - self.config.risk_config.get("stop_loss_pct", 0.03)),
                        "take_profit": entry * (1 + self.config.risk_config.get("take_profit_pct", 0.20)),
                        "trailing_stop_pct": self.config.risk_config.get("trailing_stop_pct", 0.02),
                        "strategy": "synced_from_alpaca",
                        "executed_via": "Alpaca",
                        "max_hold_bars": 40,
                        "bar_seconds": 300,
                        "max_hold_days": 5,
                    }
                    log.info(f"POSITION SYNC: Added missing {sym} from Alpaca broker to bot tracking")

            # Find phantom positions (in bot but not at broker)
            phantoms = []
            for symbol in list(self.positions.keys()):
                if symbol.upper() not in broker_symbols:
                    phantoms.append(symbol)

            removed = 0
            for symbol in phantoms:
                pos = self.positions[symbol]
                # Clean up positions that were broker-executed (not simulated)
                if pos.get("executed_via") in ("TradersPost", "Alpaca", "Alpaca-Direct", "Phantom-Cleanup"):
                    log.warning(
                        f"POSITION SYNC: Removing phantom {symbol} "
                        f"(in bot but not at Alpaca broker)"
                    )
                    del self.positions[symbol]
                    removed += 1

        if removed > 0:
            log.info(
                f"Position sync complete: removed {removed} phantom(s), "
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

        # Fill in market price and default stop_loss for buy signals so they
        # pass risk manager checks (news signals don't include price data)
        if signal.get("action") == "buy":
            symbol = signal["symbol"]
            price = self.market_data.get_price(symbol) if self.market_data else None
            if price and price > 0:
                signal["price"] = price
                if not signal.get("stop_loss"):
                    signal["stop_loss"] = price * (1 - self.config.stop_loss_pct)
                if not signal.get("take_profit"):
                    signal["take_profit"] = price * (1 + self.config.take_profit_pct)
            else:
                log.warning(f"No market price for news signal {symbol} — skipping")
                return

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
        if signal["action"] == "buy" and not signal.get("stop_loss"):
            price = signal.get("price", 0)
            if price > 0:
                signal["stop_loss"] = price * 0.97

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
                        "SHOP", "XYZ", "COIN", "MSTR", "SMCI", "ARM", "IONQ",
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
            "symbols": ["GME", "AMC", "DOGE-USD", "SHIB-USD",
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

        # Reset dynamic symbol lists — start fresh each day
        total_cleared = 0
        for name, strategy in self.strategies.items():
            if hasattr(strategy, 'reset_dynamic_symbols'):
                if hasattr(strategy, '_dynamic_symbols'):
                    total_cleared += len(strategy._dynamic_symbols)
                strategy.reset_dynamic_symbols()
        if total_cleared:
            log.info(f"DAILY RESET: Cleared {total_cleared} dynamic symbols across all strategies")

        # Also trim mean_reversion's static symbol list (gets appended to by engine)
        mr_strat = self.strategies.get("mean_reversion")
        if mr_strat and len(mr_strat.symbols) > 10:
            original = mr_strat.symbols[:10] if mr_strat.config.get("symbols") else []
            mr_strat.symbols = original
            log.info(f"DAILY RESET: Trimmed mean_reversion symbols to {len(mr_strat.symbols)}")

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

    def _momentum_rotation_check(self, rejected_signals):
        """Money Machine position rotation: replace weakest position with stronger signal.

        If a new signal scores significantly higher than our weakest held position,
        close the weak one to make room. The new signal gets picked up next cycle.
        """
        if not self.positions or not rejected_signals:
            return

        # Only rotate if we're at or near capacity
        max_pos = self.risk_manager.max_positions
        if len(self.positions) < max_pos - 1:
            return

        # Score all current positions by momentum
        scored_positions = []
        for symbol, pos in self.positions.items():
            score = 0
            pnl_pct = pos.get("unrealized_pnl_pct", 0)

            # P&L component (max 40 pts)
            if pnl_pct >= 0.05:
                score += 40
            elif pnl_pct >= 0.02:
                score += 30
            elif pnl_pct >= 0:
                score += 15
            elif pnl_pct >= -0.02:
                score += 5
            # else: 0 pts (big loser)

            # RVOL component from snapshot (max 30 pts)
            rvol_strat = self.strategies.get("rvol_momentum")
            if rvol_strat and hasattr(rvol_strat, "_snapshot_data"):
                snap = rvol_strat._snapshot_data.get(symbol)
                if snap:
                    rvol = snap.get("rvol", 0)
                    if rvol >= 3.0:
                        score += 30
                    elif rvol >= 2.0:
                        score += 20
                    elif rvol >= 1.5:
                        score += 10

            # Hold time penalty: longer holds = lower score (momentum fading)
            if "entry_time" in pos:
                hold_mins = (datetime.now(self.tz) - pos["entry_time"]).total_seconds() / 60
                if hold_mins > 120:
                    score -= 10  # 2+ hours: momentum likely gone
                elif hold_mins > 60:
                    score -= 5

            scored_positions.append((symbol, score, pnl_pct))

        if not scored_positions:
            return

        # Find weakest position
        scored_positions.sort(key=lambda x: x[1])
        weakest_sym, weakest_score, weakest_pnl = scored_positions[0]

        # Find strongest rejected signal (rejected due to max positions)
        best_rejected = None
        best_rejected_score = 0
        for sig in rejected_signals:
            sig_score = sig.get("confidence", 0) * 100
            sig_rvol = sig.get("rvol", 0)
            if sig_rvol >= 3.0:
                sig_score += 30
            elif sig_rvol >= 2.0:
                sig_score += 20
            if sig_score > best_rejected_score:
                best_rejected_score = sig_score
                best_rejected = sig

        if not best_rejected:
            return

        # Rotate if the new signal is stronger (15+ point gap) — more aggressive rotation
        score_gap = best_rejected_score - weakest_score
        if score_gap >= 15 and weakest_pnl < 0.05:  # Don't rotate out 5%+ winners (was 3%)
            log.info(
                f"MOMENTUM ROTATION: Closing {weakest_sym} (score={weakest_score}, "
                f"P&L={weakest_pnl:.1%}) to make room for {best_rejected['symbol']} "
                f"(score={best_rejected_score:.0f}) | Gap: +{score_gap:.0f} pts"
            )
            self._close_position(weakest_sym, "rotation",
                                f"Momentum rotation: replaced by stronger signal {best_rejected['symbol']}")
            self.notifier.system_alert(
                f"Rotation: closed {weakest_sym} ({weakest_pnl:.1%}) → "
                f"making room for {best_rejected['symbol']} (RVOL {best_rejected.get('rvol', 0):.1f}x)",
                level="info"
            )

    def _power_hour_trim(self):
        """Power hour position trimming (3:00-3:30 PM ET).

        Closes weak positions to:
        1. Free up capital for late-day moon runners
        2. Reduce position count toward max_positions limit
        3. Lock in small profits on positions that aren't going anywhere

        Evaluates each position using _evaluate_bullish_for_afterhours and
        closes those scoring below 30 (clearly bearish / going nowhere).
        """
        if not self.positions:
            return

        max_pos = self.risk_manager.max_positions
        current_count = len(self.positions)
        trim_target = max(max_pos, current_count - 5)  # Close at most 5 weak ones per cycle

        # Score all positions
        scored = []
        for symbol, pos in list(self.positions.items()):
            if self._is_crypto_symbol(symbol):
                continue  # Don't trim crypto
            _, reason, score = self._evaluate_bullish_for_afterhours(symbol, pos)
            scored.append((symbol, pos, score, reason))

        # Sort by bullish score ascending — weakest first
        scored.sort(key=lambda x: x[2])

        trimmed = 0
        for symbol, pos, score, reason in scored:
            if current_count - trimmed <= trim_target and score >= 30:
                break  # Already at target and remaining positions aren't terrible

            pnl_pct = pos.get("unrealized_pnl_pct", 0)

            # Close if: bearish (score < 30) or if we need to reduce count and score < 40
            should_trim = False
            if score < 30:
                should_trim = True  # Clearly weak — close regardless
            elif score < 40 and (current_count - trimmed) > max_pos:
                should_trim = True  # Mediocre and we're over position limit

            if should_trim:
                log.info(
                    f"POWER HOUR TRIM: Closing {symbol} | Score: {score}/100 | "
                    f"P&L: {pnl_pct:.1%} | {reason}"
                )
                self._close_position(symbol, "power_hour_trim",
                                     f"Power hour trim: bullish score {score}/100 too low")
                trimmed += 1

        if trimmed > 0:
            remaining = len(self.positions)
            self.notifier.system_alert(
                f"Power hour: trimmed {trimmed} weak positions | "
                f"{remaining} positions remaining (max: {max_pos})",
                level="info"
            )

    def _power_hour_tighten_stops(self):
        """Tighten stops on all positions at 3:50 PM ET.

        Protects profits before EOD volatility and the close auction.
        Moves stops to at least breakeven for profitable positions.
        """
        if not self.positions:
            return

        tightened = 0
        for symbol, pos in list(self.positions.items()):
            if self._is_crypto_symbol(symbol):
                continue

            current_price = pos.get("current_price", pos["entry_price"])
            entry_price = pos["entry_price"]
            pnl_pct = pos.get("unrealized_pnl_pct", 0)
            old_stop = pos.get("stop_loss", 0)

            if pos["direction"] == "long":
                if pnl_pct >= 0.02:
                    # 2%+ profit: tighten to 1.5% trail from current price
                    new_stop = current_price * 0.985
                elif pnl_pct > 0:
                    # In profit: move stop to breakeven + small buffer
                    new_stop = entry_price * 1.002
                else:
                    # Losing: tighten stop to limit further downside
                    new_stop = current_price * 0.97

                if new_stop > old_stop:
                    pos["stop_loss"] = round(new_stop, 2)
                    tightened += 1
                    log.debug(
                        f"PH TIGHTEN: {symbol} stop ${old_stop:.2f} → ${new_stop:.2f} "
                        f"(P&L: {pnl_pct:.1%})"
                    )

        if tightened > 0:
            log.info(f"Power hour 3:50 PM: tightened stops on {tightened} positions")

    def _evaluate_bullish_for_afterhours(self, symbol, pos):
        """Evaluate if a position is bullish enough for after-hours / overnight hold.

        Returns (should_hold: bool, reason: str, bullish_score: int)

        Checks multiple technical factors:
        - P&L momentum (position profitability)
        - EMA trend (price above 9 & 20 EMA)
        - RSI (not overbought, healthy range)
        - RVOL (still high = still in play)
        - Price action (making higher highs)
        """
        pnl_pct = pos.get("unrealized_pnl_pct", 0)
        current_price = pos.get("current_price", pos["entry_price"])
        entry_price = pos["entry_price"]
        bullish_score = 0
        reasons = []

        # 1. Profitability check (max 30 pts)
        if pnl_pct >= 0.05:
            bullish_score += 30
            reasons.append(f"Strong profit +{pnl_pct:.1%}")
        elif pnl_pct >= 0.03:
            bullish_score += 25
            reasons.append(f"Good profit +{pnl_pct:.1%}")
        elif pnl_pct >= 0.01:
            bullish_score += 15
            reasons.append(f"Modest profit +{pnl_pct:.1%}")
        elif pnl_pct > 0:
            bullish_score += 5
            reasons.append(f"Barely green +{pnl_pct:.1%}")
        else:
            reasons.append(f"In the red {pnl_pct:.1%}")

        # 2. Technical trend check (max 30 pts)
        if self.market_data:
            data = self.market_data.get_data(symbol)
            if data is not None and len(data) >= 20:
                closes = data["close"].values
                ema9 = self.indicators.ema(closes, 9)
                ema20 = self.indicators.ema(closes, 20)

                if ema9 is not None and ema20 is not None:
                    price_above_9 = closes[-1] > ema9[-1]
                    price_above_20 = closes[-1] > ema20[-1]
                    ema9_above_20 = ema9[-1] > ema20[-1]

                    if price_above_9 and price_above_20 and ema9_above_20:
                        bullish_score += 30
                        reasons.append("Strong uptrend (above 9/20 EMA)")
                    elif price_above_20:
                        bullish_score += 15
                        reasons.append("Above 20 EMA")
                    else:
                        reasons.append("Below key EMAs — bearish")

                # RSI check (max 15 pts)
                rsi = self.indicators.rsi(closes, 14)
                if rsi:
                    if 40 <= rsi <= 70:
                        bullish_score += 15
                        reasons.append(f"RSI healthy ({rsi:.0f})")
                    elif rsi > 70:
                        bullish_score += 5
                        reasons.append(f"RSI overbought ({rsi:.0f}) — risky overnight")
                    else:
                        reasons.append(f"RSI weak ({rsi:.0f})")

        # 3. RVOL check from snapshot data (max 15 pts)
        rvol_strat = self.strategies.get("rvol_momentum")
        if rvol_strat and hasattr(rvol_strat, "_snapshot_data"):
            snap = rvol_strat._snapshot_data.get(symbol)
            if snap:
                rvol = snap.get("rvol", 0)
                if rvol >= 3.0:
                    bullish_score += 15
                    reasons.append(f"RVOL still elevated {rvol:.1f}x")
                elif rvol >= 2.0:
                    bullish_score += 10
                    reasons.append(f"RVOL active {rvol:.1f}x")

        # 4. Price action momentum (max 10 pts)
        if current_price > entry_price * 1.03:
            bullish_score += 10
            reasons.append("Price 3%+ above entry — momentum intact")
        elif current_price > entry_price:
            bullish_score += 5
            reasons.append("Price above entry")

        # Verdict: 50+ = bullish enough for after-hours
        should_hold = bullish_score >= 50
        reason_str = " | ".join(reasons[:5])

        log.info(
            f"BULLISH EVAL: {symbol} | Score: {bullish_score}/100 | "
            f"{'HOLD' if should_hold else 'CLOSE'} | {reason_str}"
        )

        return should_hold, reason_str, bullish_score

    def _end_of_day(self):
        """End of day routine with smart position evaluation.

        For each position, evaluates bullish/bearish technical factors to decide:
        - Close at EOD (bearish, scalps, losers)
        - Hold into after-hours with tightened stops (bullish runners)
        - Hold overnight (strong multi-day plays)

        After-hours selling uses Alpaca extended_hours=True with limit orders.
        """
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
            afterhours_holds = []
            positions_to_close = []

            # Sort positions by P&L descending — evaluate best positions first
            # for overnight/AH hold slots
            sorted_positions = sorted(
                list(self.positions.items()),
                key=lambda x: x[1].get("unrealized_pnl_pct", 0),
                reverse=True,
            )

            for symbol, pos in sorted_positions:
                pnl_pct = pos.get("unrealized_pnl_pct", 0)
                in_profit = pnl_pct > 0

                # NEVER hold stock split candidates overnight
                if symbol in split_candidates:
                    log.warning(f"SPLIT BLOCK: Closing {symbol} - split candidate, no overnight hold")
                    positions_to_close.append(symbol)
                    continue

                # NEVER hold through earnings — gap risk is extreme
                if self.polygon and self.polygon.enabled:
                    try:
                        if self.polygon.has_earnings_soon(symbol, days_ahead=1):
                            log.warning(
                                f"EARNINGS BLOCK: Closing {symbol} — earnings imminent, "
                                f"overnight gap risk too high | P&L: {pnl_pct:.1%}"
                            )
                            self.notifier.risk_alert(
                                f"Closing {symbol} before earnings — overnight gap risk"
                            )
                            positions_to_close.append(symbol)
                            continue
                    except Exception as e:
                        log.debug(f"Earnings check failed for {symbol}: {e}")

                # ALWAYS close RVOL scalp (ultra short-term, never hold)
                if pos.get("strategy") == "rvol_scalp":
                    log.info(f"RVOL SCALP EXIT: Closing {symbol} - scalp positions are intraday only")
                    positions_to_close.append(symbol)
                    continue

                # Crypto positions trade 24/7 - skip EOD close entirely
                if self._is_crypto_symbol(symbol):
                    log.info(f"CRYPTO HOLD: {symbol} trades 24/7 - skipping EOD close | P&L: {pnl_pct:.1%}")
                    overnight_holds.append(symbol)
                    continue

                # --- SMART BULLISH EVALUATION ---
                # Evaluate each position technically to decide hold vs close
                total_holds = len(overnight_holds) + len(afterhours_holds)
                if total_holds >= max_overnight:
                    # Already at capacity — close the rest
                    positions_to_close.append(symbol)
                    log.info(
                        f"EOD CLOSE (at capacity): {symbol} | P&L: {pnl_pct:.1%} | "
                        f"Already holding {total_holds} positions"
                    )
                    continue

                should_hold, eval_reason, bullish_score = self._evaluate_bullish_for_afterhours(symbol, pos)

                if should_hold and in_profit:
                    current_price = pos.get("current_price", pos["entry_price"])

                    # Determine: after-hours only vs overnight hold
                    is_breakout = pos.get("breakout_play") or pos.get("source") == "prebreakout"
                    is_momentum = pos.get("strategy") == "rvol_momentum"
                    is_multi_day = pos.get("strategy") in ("momentum", "prebreakout", "smc_forever")

                    if is_multi_day and bullish_score >= 60:
                        # Strong multi-day play — hold overnight
                        tighten = overnight_cfg.get("tighten_stop_pct", 0.025)
                        if pos["direction"] == "long":
                            new_stop = current_price * (1 - tighten)
                            if new_stop > pos.get("stop_loss", 0):
                                pos["stop_loss"] = new_stop
                        pos["overnight_hold"] = True
                        overnight_holds.append(symbol)
                        log.info(
                            f"OVERNIGHT HOLD: {symbol} | P&L: {pnl_pct:.1%} | "
                            f"Bullish: {bullish_score}/100 | Stop: ${pos['stop_loss']:.2f} | "
                            f"{eval_reason}"
                        )
                    elif is_momentum and bullish_score >= 50:
                        # RVOL momentum runner — hold into after-hours with tight stop
                        new_stop = current_price * 0.97  # 3% stop for AH
                        if new_stop > pos.get("stop_loss", 0):
                            pos["stop_loss"] = new_stop
                        pos["afterhours_hold"] = True
                        afterhours_holds.append(symbol)
                        log.info(
                            f"AFTER-HOURS HOLD: {symbol} | P&L: {pnl_pct:.1%} | "
                            f"Bullish: {bullish_score}/100 | AH Stop: ${pos['stop_loss']:.2f} | "
                            f"{eval_reason}"
                        )
                    elif bullish_score >= 50:
                        # Other strategy with decent bullish score
                        tighten = overnight_cfg.get("tighten_stop_pct", 0.025)
                        if pos["direction"] == "long":
                            new_stop = current_price * (1 - tighten)
                            if new_stop > pos.get("stop_loss", 0):
                                pos["stop_loss"] = new_stop
                        pos["overnight_hold"] = True
                        overnight_holds.append(symbol)
                        log.info(
                            f"OVERNIGHT HOLD: {symbol} | P&L: {pnl_pct:.1%} | "
                            f"Bullish: {bullish_score}/100 | Stop: ${pos['stop_loss']:.2f} | "
                            f"{eval_reason}"
                        )
                    else:
                        positions_to_close.append(symbol)
                        log.info(
                            f"EOD CLOSE (not bullish enough): {symbol} | "
                            f"P&L: {pnl_pct:.1%} | Bullish: {bullish_score}/100 | "
                            f"{eval_reason}"
                        )
                elif overnight_cfg.get("close_losers", True) or not overnight_enabled:
                    positions_to_close.append(symbol)
                    log.info(
                        f"EOD CLOSE: {symbol} | P&L: {pnl_pct:.1%} | "
                        f"Bullish: {bullish_score}/100 | {eval_reason}"
                    )
                else:
                    positions_to_close.append(symbol)

            # Close positions not held (uses GTC + limit for after-hours fills)
            for symbol in positions_to_close:
                self._close_position(symbol, "eod_close", "End of day close")

            # Notifications
            if overnight_holds:
                self.notifier.system_alert(
                    f"Holding {len(overnight_holds)} positions overnight: "
                    f"{', '.join(overnight_holds)}",
                    level="info"
                )
            if afterhours_holds:
                self.notifier.system_alert(
                    f"Holding {len(afterhours_holds)} positions into after-hours: "
                    f"{', '.join(afterhours_holds)} "
                    f"(will be re-evaluated for overnight hold at 6 PM)",
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

        # Log daily summary to Google Sheets
        if self.sheets_logger and self.sheets_logger.is_enabled():
            wins_list = [t for t in self.daily_trades if t.get("pnl", 0) > 0]
            losses_list = [t for t in self.daily_trades if t.get("pnl", 0) <= 0]
            best = max((t.get("pnl", 0) for t in self.daily_trades), default=0)
            worst = min((t.get("pnl", 0) for t in self.daily_trades), default=0)
            best_sym = next((t.get("symbol", "") for t in self.daily_trades if t.get("pnl", 0) == best), "") if self.daily_trades else ""
            worst_sym = next((t.get("symbol", "") for t in self.daily_trades if t.get("pnl", 0) == worst), "") if self.daily_trades else ""
            self.sheets_logger.log_daily_summary({
                "date": stats["date"],
                "trades": total_trades,
                "wins": len(wins_list),
                "losses": len(losses_list),
                "pnl": self.daily_pnl,
                "balance": self.current_balance,
                "best_trade": f"{best_sym} ${best:+.2f}" if best_sym else "",
                "worst_trade": f"{worst_sym} ${worst:+.2f}" if worst_sym else "",
                "positions_held": len(self.positions),
            })
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
        try:
            if self.broker and not self.broker.is_connected():
                log.warning("Broker disconnected - attempting reconnect")
                self.broker.reconnect()

            # Trim unbounded lists to prevent memory leaks
            if len(self.analysis_log) > self.max_analysis_log:
                self.analysis_log = self.analysis_log[-self.max_analysis_log:]
            if len(self.equity_curve) > 2000:
                self.equity_curve = self.equity_curve[-1000:]
            if self.tp_broker and len(self.tp_broker.signal_history) > 500:
                self.tp_broker.signal_history = self.tp_broker.signal_history[-250:]
            if self.risk_manager and len(self.risk_manager.rejected_signals) > 500:
                self.risk_manager.rejected_signals = self.risk_manager.rejected_signals[-250:]
            # Trim signal cooldowns older than 5 minutes
            now = datetime.now(self.tz)
            stale_keys = [k for k, v in self._signal_cooldowns.items()
                          if (now - v).total_seconds() > 300]
            for k in stale_keys:
                del self._signal_cooldowns[k]
        except Exception as e:
            log.error(f"Health check error: {e}")

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
            # IBKR enhanced features status
            "ibkr_features": self._get_ibkr_features_status(),
        }
        return status

    def _get_ibkr_features_status(self):
        """Get status of active IBKR features for dashboard."""
        if not self.broker or not self.broker.is_connected():
            return {"active": False}

        pnl = self.broker.get_realtime_pnl() if hasattr(self.broker, 'get_realtime_pnl') else None
        live_bars_count = len(self.broker._live_bars) if hasattr(self.broker, '_live_bars') else 0
        open_orders = len(self.broker.get_open_orders()) if hasattr(self.broker, 'get_open_orders') else 0

        return {
            "active": True,
            "bracket_orders": True,
            "outside_rth": True,
            "realtime_pnl": pnl,
            "five_sec_bars_symbols": live_bars_count,
            "open_orders": open_orders,
            "news_subscription": hasattr(self.broker, '_news_callback') and self.broker._news_callback is not None,
        }

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
        elif self.polygon and self.polygon.enabled:
            source = "Polygon.io"
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
                "take_profit_pct": risk.get("take_profit_pct", 0.20),
                "max_positions": risk.get("max_positions", 12),
                "risk_per_trade_pct": risk.get("risk_per_trade_pct", 0.02),
                "max_position_size_pct": risk.get("max_position_size_pct", 0.15),
            },
            "schedule": {
                "avoid_first_minutes": schedule.get("avoid_first_minutes", 5),
                "avoid_last_minutes": schedule.get("avoid_last_minutes", 10),
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
        Dynamically discover hot stocks from top movers, losers, and trending
        and inject into ALL scanning strategies. Runs every cycle.

        Aggressive discovery: very low thresholds, scan gainers + losers +
        most active + trending. Feed into RVOL (momentum + scalp) and
        mean_reversion (oversold losers are mean reversion candidates).
        """
        rvol_strat = self.strategies.get("rvol_momentum")
        scalp_strat = self.strategies.get("rvol_scalp")
        mr_strat = self.strategies.get("mean_reversion")
        pb_strat = self.strategies.get("prebreakout")
        gap_strat = self.strategies.get("premarket_gap")
        squeeze_strat = self.strategies.get("short_squeeze")
        pead_strat = self.strategies.get("pead")
        runner_strat = self.strategies.get("momentum_runner")
        if not any([rvol_strat, scalp_strat, mr_strat, pb_strat, gap_strat, squeeze_strat, pead_strat, runner_strat]):
            return

        try:
            # --- Polygon.io full-market scan (if configured) ---
            # One call returns ALL ~10,000 stocks — catches everything Alpaca misses
            if self.polygon and self.polygon.enabled:
                poly_movers, poly_runners, poly_gap_ups = self.polygon.scan_full_market()

                if poly_movers:
                    poly_mover_syms = []
                    poly_scalp_syms = []
                    snapshot_entries = []
                    for m in poly_movers:
                        sym = m.get("symbol", "")
                        if not sym:
                            continue
                        if self._is_crypto_symbol(sym) and not self._is_crypto_enabled():
                            continue
                        change_pct = m.get("change_pct", 0)
                        if change_pct >= 2.0:
                            poly_mover_syms.append(sym)
                            # Collect full snapshot data for RVOL fast path
                            snapshot_entries.append(m)
                        if abs(change_pct) >= 1.0:
                            poly_scalp_syms.append(sym)

                    if poly_mover_syms and rvol_strat:
                        rvol_strat.add_dynamic_symbols(poly_mover_syms)
                    if poly_scalp_syms and scalp_strat:
                        scalp_strat.add_dynamic_symbols(poly_scalp_syms)
                    if poly_scalp_syms and pb_strat:
                        pb_strat.add_dynamic_symbols(poly_scalp_syms)

                    # Feed snapshot data to RVOL strategy for fast-path signals
                    # This lets the Money Machine generate signals INSTANTLY for
                    # top gainers without waiting for historical bars
                    if snapshot_entries and rvol_strat and hasattr(rvol_strat, "feed_snapshot_data"):
                        rvol_strat.feed_snapshot_data(snapshot_entries)
                        log.debug(f"Polygon: fed {len(snapshot_entries)} snapshot entries to RVOL fast path")

                    # Feed momentum runner strategy — session-aware candidates + snapshot
                    if runner_strat:
                        # Determine session for session-aware scanning
                        now_et = datetime.now(self.tz)
                        _h, _m = now_et.hour, now_et.minute
                        _time_val = _h * 100 + _m
                        if _time_val < 930:
                            _session = "premarket"
                        elif _time_val < 1600:
                            _session = "regular"
                        else:
                            _session = "postmarket"

                        # Get session-appropriate candidates from scanner
                        session_candidates = self.polygon.get_session_candidates(_session)
                        if session_candidates:
                            runner_syms_session = [c["symbol"] for c in session_candidates if c.get("symbol")]
                            runner_strat.add_dynamic_symbols(runner_syms_session)
                            runner_strat.feed_snapshot_data(session_candidates)

                        # Also feed all movers as snapshot data
                        if snapshot_entries:
                            runner_strat.feed_snapshot_data(snapshot_entries)
                        runner_strat.add_dynamic_symbols(poly_mover_syms)

                        # Feed sector momentum for sympathy play detection
                        sector_momentum = self.polygon.get_sector_momentum()
                        runner_strat.feed_sector_momentum(sector_momentum)

                        log.debug(
                            f"Polygon: fed {len(session_candidates) if session_candidates else 0} "
                            f"session candidates + {len(poly_mover_syms)} movers into momentum_runner"
                        )

                    log.debug(f"Polygon: injected {len(poly_mover_syms)} movers, {len(poly_scalp_syms)} scalp candidates")

                if poly_gap_ups and gap_strat:
                    gap_syms = [g["symbol"] for g in poly_gap_ups if g.get("symbol")]
                    gap_strat.add_dynamic_symbols(gap_syms)
                    log.debug(f"Polygon: injected {len(gap_syms)} gap-ups into pre-market gap")

                # Feed gap-ups into PEAD — earnings gaps are drift candidates
                if poly_gap_ups and pead_strat:
                    gap_syms = [g["symbol"] for g in poly_gap_ups if g.get("symbol")]
                    pead_strat.add_dynamic_symbols(gap_syms)
                    # Auto-feed earnings data for large gaps (PEAD will check if earnings-related)
                    for g in poly_gap_ups:
                        sym = g.get("symbol", "")
                        gap_pct = g.get("gap_pct", 0)
                        rvol = g.get("rvol", 0)
                        price = g.get("price", 0)
                        if sym and gap_pct >= 5.0 and rvol >= 2.0 and price > 0:
                            from datetime import datetime as dt
                            pead_strat.feed_earnings_data(sym, gap_pct, rvol, dt.now().date(), price)
                    log.debug(f"Polygon: fed {len(poly_gap_ups)} gap-ups into PEAD strategy")

                if poly_runners:
                    runner_syms = [r["symbol"] for r in poly_runners if r.get("symbol")]
                    if runner_syms:
                        if rvol_strat:
                            rvol_strat.add_dynamic_symbols(runner_syms)
                            # Also feed runner snapshot data for fast path
                            if hasattr(rvol_strat, "feed_snapshot_data"):
                                rvol_strat.feed_snapshot_data(poly_runners)
                        if scalp_strat:
                            scalp_strat.add_dynamic_symbols(runner_syms)
                        if pb_strat:
                            pb_strat.add_dynamic_symbols(runner_syms)
                        if gap_strat:
                            gap_strat.add_dynamic_symbols(runner_syms)
                        if squeeze_strat:
                            squeeze_strat.add_dynamic_symbols(runner_syms)
                        if pead_strat:
                            pead_strat.add_dynamic_symbols(runner_syms)
                        if runner_strat:
                            runner_strat.add_dynamic_symbols(runner_syms)
                            runner_strat.feed_snapshot_data(poly_runners)
                        log.debug(f"Polygon: injected {len(runner_syms)} runners into all strategies")
            # Get top movers from Polygon (filtered to $0.50-$50 range)
            movers = self.get_top_movers()
            if movers:
                mover_symbols = []
                scalp_symbols = []
                for m in movers:
                    sym = m.get("symbol", "")
                    price = m.get("price", 0)
                    change_pct = m.get("change_pct", 0)
                    rvol = m.get("rvol", 0)

                    if not sym or price < 0.50 or price > 100.0:
                        continue

                    # Skip crypto symbols when crypto is disabled
                    if self._is_crypto_symbol(sym) and not self._is_crypto_enabled():
                        continue

                    # Feed movers with >2% move into momentum RVOL (lowered from 3%)
                    if change_pct >= 2.0:
                        mover_symbols.append(sym)

                    # Feed ANY stock showing unusual activity into scalp
                    # Very low threshold - just needs movement or volume
                    if abs(change_pct) >= 1.0 or rvol >= 1.3:
                        scalp_symbols.append(sym)

                    # Feed big losers into mean reversion (oversold bounces)
                    if change_pct <= -3.0 and mr_strat:
                        if sym not in mr_strat.symbols:
                            mr_strat.symbols.append(sym)

                # Feed gap-ups into pre-market gap scanner (3%+ movers)
                gap_symbols = []
                for m in movers:
                    sym = m.get("symbol", "")
                    change_pct = m.get("change_pct", 0)
                    if sym and change_pct >= 3.0:
                        gap_symbols.append(sym)

                if mover_symbols and rvol_strat:
                    rvol_strat.add_dynamic_symbols(mover_symbols)
                    log.debug(f"Injected {len(mover_symbols)} movers into RVOL momentum")
                if scalp_symbols and scalp_strat:
                    scalp_strat.add_dynamic_symbols(scalp_symbols)
                    log.debug(f"Injected {len(scalp_symbols)} movers into RVOL scalp")

                # Feed ALL unusual activity into pre-breakout scanner
                if pb_strat and scalp_symbols:
                    pb_strat.add_dynamic_symbols(scalp_symbols)
                    log.debug(f"Injected {len(scalp_symbols)} movers into pre-breakout")

                # Feed movers into short squeeze (high-volume movers may be squeezing)
                if squeeze_strat and mover_symbols:
                    squeeze_strat.add_dynamic_symbols(mover_symbols)
                    # Estimate short interest for top movers and feed to squeeze strategy
                    if self.polygon and hasattr(self.polygon, 'estimate_short_interest'):
                        si_data = self.polygon.estimate_short_interest(mover_symbols[:10])
                        if si_data:
                            squeeze_strat.feed_short_interest(si_data)
                            log.debug(f"Fed short interest data for {len(si_data)} symbols to squeeze strategy")
                    log.debug(f"Injected {len(mover_symbols)} movers into short squeeze")

                # Feed gap-up stocks into pre-market gap strategy
                if gap_strat and gap_symbols:
                    gap_strat.add_dynamic_symbols(gap_symbols)
                    log.debug(f"Injected {len(gap_symbols)} gap-ups into pre-market gap")

            # Also get losers from the movers data (fetched via Polygon in get_top_movers)
            # Losers are mean reversion bounce candidates
            if movers:
                loser_syms = []
                for m in movers:
                    sym = m.get("symbol", "")
                    change_pct = m.get("change_pct", 0)
                    price = m.get("price", 0)
                    if change_pct <= -2.0 and price >= 5.0 and sym:
                        loser_syms.append(sym)
                if loser_syms:
                    if mr_strat:
                        existing = set(mr_strat.symbols)
                        new = [s for s in loser_syms if s not in existing]
                        mr_strat.symbols.extend(new)
                    if scalp_strat:
                        scalp_strat.add_dynamic_symbols(loser_syms)
                    # Losers that base out and accumulate can also pre-breakout
                    if pb_strat:
                        pb_strat.add_dynamic_symbols(loser_syms)
                    log.debug(f"Injected {len(loser_syms)} losers into mean reversion + scalp + pre-breakout")

            # Also check for low-float post-split runners
            runners = self.get_low_float_runners()
            if runners:
                runner_symbols = [r["symbol"] for r in runners if r.get("symbol")]
                if runner_symbols:
                    if rvol_strat:
                        rvol_strat.add_dynamic_symbols(runner_symbols)
                    if scalp_strat:
                        scalp_strat.add_dynamic_symbols(runner_symbols)
                    if pb_strat:
                        pb_strat.add_dynamic_symbols(runner_symbols)
                    if gap_strat:
                        gap_strat.add_dynamic_symbols(runner_symbols)
                    if squeeze_strat:
                        squeeze_strat.add_dynamic_symbols(runner_symbols)
                    if pead_strat:
                        pead_strat.add_dynamic_symbols(runner_symbols)
                    log.debug(f"Injected {len(runner_symbols)} runners into all strategies")

        except Exception as e:
            log.debug(f"Dynamic discovery error: {e}")

    def _prune_stale_dynamic_symbols(self):
        """Prune dynamic symbols that haven't been re-discovered in 30 minutes.

        Runs every cycle. Symbols that are still actively moving get their
        timestamps refreshed by _discover_dynamic_symbols(). Dead symbols
        that stopped appearing as movers are removed to prevent unbounded
        accumulation across all strategies.
        """
        # Only prune every 60 seconds to avoid overhead
        if not hasattr(self, '_last_symbol_prune'):
            self._last_symbol_prune = 0
        import time
        now = time.time()
        if now - self._last_symbol_prune < 60:
            return
        self._last_symbol_prune = now

        total_pruned = 0
        for name, strategy in self.strategies.items():
            if hasattr(strategy, 'prune_dynamic_symbols'):
                pruned = strategy.prune_dynamic_symbols(max_age_seconds=1800)
                if pruned:
                    total_pruned += pruned
                    remaining = len(strategy._dynamic_symbols) if hasattr(strategy, '_dynamic_symbols') else 0
                    log.debug(f"Pruned {pruned} stale symbols from {name} ({remaining} remaining)")

        # Also cap mean_reversion's symbol list (it gets appended to directly)
        mr_strat = self.strategies.get("mean_reversion")
        if mr_strat and len(mr_strat.symbols) > 50:
            original_count = len(mr_strat.config.get("symbols", []))
            # Keep config symbols + most recent 40 dynamic ones
            mr_strat.symbols = mr_strat.symbols[:original_count] + mr_strat.symbols[-(50 - original_count):]
            total_pruned += len(mr_strat.symbols) - 50
            log.debug(f"Capped mean_reversion symbols at 50 (was {len(mr_strat.symbols)})")

        if total_pruned:
            log.info(f"SYMBOL PRUNE: removed {total_pruned} stale symbols across strategies")

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
        Scan for explosive movers using Polygon.io full-market data.
        Catches 100%+ runners, squeeze candidates, extreme volume stocks.

        Returns list of runner dicts sorted by change_pct descending.
        """
        import requests as _req

        runners = []
        seen = set()

        # --- 1. Polygon.io runners (10%+ movers from full-market scan) ---
        if self.polygon and self.polygon.enabled:
            poly_runners = self.polygon.get_runners(limit=50)
            for r in poly_runners:
                sym = r.get("symbol", "")
                if not sym:
                    continue
                price = r.get("price", 0)
                change_pct = r.get("change_pct", 0)
                volume = r.get("volume", 0)
                # Enrich with float data from Polygon cache
                float_shares = self.polygon.get_float(sym) if self.polygon else 0
                is_low_float = float_shares > 0 and float_shares < 20_000_000
                runner_type = "LOW FLOAT SQUEEZE" if is_low_float else (
                    "HIGH MOMENTUM" if change_pct < 40 else "DAY RUNNER"
                )
                runners.append({
                    "symbol": sym,
                    "name": sym,
                    "price": round(price, 2),
                    "change_pct": round(change_pct, 2),
                    "volume": volume,
                    "avg_volume": r.get("avg_volume", 0),
                    "rvol": r.get("rvol", 0),
                    "market_cap": 0,
                    "float_shares": float_shares,
                    "float_display": self._format_float(float_shares),
                    "shares_outstanding": 0,
                    "runner_type": runner_type,
                    "is_low_float": is_low_float,
                    "is_post_split": False,
                    "on_watchlist": sym in self.watchlist,
                })
                seen.add(sym)

        # --- 2. Yahoo fallback for float data + small cap scan ---
        if len(runners) < 3:
            try:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
                params = {"scrIds": "small_cap_gainers", "count": 25}
                resp = _req.get(url, params=params, headers=headers, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    results = data.get("finance", {}).get("result", [])
                    if results:
                        for q in results[0].get("quotes", [])[:25]:
                            sym = q.get("symbol", "")
                            if not sym or "." in sym or sym in seen:
                                continue
                            price = q.get("regularMarketPrice", 0)
                            change_pct = q.get("regularMarketChangePercent", 0)
                            volume = q.get("regularMarketVolume", 0)
                            avg_vol = q.get("averageDailyVolume3Month", 1)
                            float_shares = q.get("floatShares", 0)
                            rvol = round(volume / avg_vol, 1) if avg_vol > 0 else 0
                            is_low_float = float_shares > 0 and float_shares < 20_000_000

                            if change_pct >= 15.0 and volume >= 300_000 and price >= 1.0:
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
                                seen.add(sym)
            except Exception as e:
                log.debug(f"Yahoo runner scan fallback error: {e}")

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
                    "current_vol": int(current_vol) if current_vol == current_vol else 0,
                    "avg_vol": int(avg_vol_20) if avg_vol_20 == avg_vol_20 else 0,
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
        Fetch top movers using Polygon.io full-market scan (real-time).
        One API call returns ALL ~10,000 stocks — no narrow top-50 limits.

        Returns list of dicts: [{symbol, name, price, change_pct, volume, ...}]
        """
        movers = []
        seen_symbols = set()

        # --- 1. Polygon.io full-market scan (PRIMARY — real-time) ---
        if self.polygon and self.polygon.enabled:
            # Gainers (2%+ movers)
            poly_movers = self.polygon.get_top_movers(limit=200)
            for m in poly_movers:
                sym = m.get("symbol", "")
                if not sym or sym in seen_symbols:
                    continue
                movers.append({
                    "symbol": sym,
                    "name": m.get("name", sym),
                    "price": m.get("price", 0),
                    "change_pct": m.get("change_pct", 0),
                    "volume": m.get("volume", 0),
                    "avg_volume": m.get("avg_volume", 0),
                    "rvol": m.get("rvol", 0),
                    "market_cap": 0,
                    "on_watchlist": sym in self.watchlist,
                })
                seen_symbols.add(sym)

            # Losers (for mean reversion)
            poly_losers = self.polygon.get_losers(limit=100)
            for m in poly_losers:
                sym = m.get("symbol", "")
                if not sym or sym in seen_symbols:
                    continue
                movers.append({
                    "symbol": sym,
                    "name": sym,
                    "price": m.get("price", 0),
                    "change_pct": m.get("change_pct", 0),
                    "volume": m.get("volume", 0),
                    "avg_volume": 0,
                    "rvol": 0,
                    "market_cap": 0,
                    "on_watchlist": sym in self.watchlist,
                })
                seen_symbols.add(sym)

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

                # Determine trend (use last value of EMA arrays)
                ema50_val = float(ema50[-1]) if ema50 is not None and len(ema50) > 0 else None
                ema200_val = float(ema200[-1]) if ema200 is not None and len(ema200) > 0 else None

                if ema200_val is not None and ema50_val is not None:
                    if current_price > ema50_val > ema200_val:
                        trend = "STRONG UPTREND"
                        trend_score = 3
                    elif current_price > ema200_val:
                        trend = "UPTREND"
                        trend_score = 2
                    elif current_price < ema50_val < ema200_val:
                        trend = "STRONG DOWNTREND"
                        trend_score = -2
                    elif current_price < ema200_val:
                        trend = "DOWNTREND"
                        trend_score = -1
                    else:
                        trend = "SIDEWAYS"
                        trend_score = 0
                elif ema50_val is not None:
                    if current_price > ema50_val:
                        trend = "UPTREND"
                        trend_score = 2
                    elif current_price < ema50_val:
                        trend = "DOWNTREND"
                        trend_score = -1
                    else:
                        trend = "SIDEWAYS"
                        trend_score = 0
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
                if ema200_val is not None and current_price > ema200_val:
                    score += 10
                    reasons.append(f"Above 200 EMA (${ema200_val:.2f}) - long-term bullish")

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
                    "ema50": round(float(ema50_val), 2) if ema50_val is not None else None,
                    "ema200": round(float(ema200_val), 2) if ema200_val is not None else None,
                    "reasons": reasons,
                })

            except Exception as e:
                log.debug(f"Swing scanner error for {symbol}: {e}")

        # Sort by score descending
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:15]
