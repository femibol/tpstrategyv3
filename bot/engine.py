"""
Core Trading Engine - The brain of the operation.
Runs the main event loop, coordinates strategies, risk, and execution.
Fully automated, no-touch operation.
"""
import json
import os
import re
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
from bot.strategies.daily_trend_rider import DailyTrendRiderStrategy
from bot.data.polygon_scanner import PolygonScanner
from bot.learning.trade_analyzer import TradeAnalyzer
from bot.learning.ai_insights import AIInsights
from bot.learning.auto_tuner import AutoTuner
from bot.learning.weekly_review import WeeklyReview
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

        # Diagnostic counters (for periodic visibility logs)
        self._full_cycle_count = 0

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
        # Tracks {symbol: close_datetime} to block re-entry via broker sync
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

        # Overnight hold state persistence — survives bot restarts
        self._overnight_state_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "overnight_holds.json"
        )

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

        # Connect to IBKR (sole broker for data + execution)
        self._connect_broker()

        # Log actual balance after broker sync
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

        # Polygon — REMOVED from execution chain (IBKR is sole data source).
        # Kept as None so legacy code paths that check `if self.polygon and self.polygon.enabled`
        # gracefully no-op. No API calls, no initialization, no credentials needed.
        self.polygon = None
        blocked_symbols = self.config.risk_config.get("blocked_symbols", [])

        # Market data feed (IBKR primary, Yahoo fallback for reference data)
        self.market_data = MarketDataFeed(self.config, self.broker, polygon=None)

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

        # TradersPost integration (optional — IBKR is primary broker)
        if self.config.traderspost_webhook_url:
            self.tp_broker = TradersPostBroker(self.config)
            log.info(f"TradersPost integration ENABLED - webhook configured")
            log.info(f"TradersPost URL: ...{self.config.traderspost_webhook_url[-20:]}")
        else:
            log.info(
                "TradersPost not configured — using IBKR as sole broker. "
                "Set TRADERSPOST_WEBHOOK_URL to also mirror signals there."
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

        # Weekly Review — Saturday 10am ET deep Claude digest to Discord.
        # Needs both Claude AND Discord to be useful; constructor is cheap
        # either way.
        self.weekly_review = WeeklyReview(self.config, self.ai_insights, self.notifier)
        if self.weekly_review.is_available():
            log.info("Weekly Review ENABLED — Saturday 10am ET to Discord")

        # Load persisted trade history from previous sessions
        if self.trade_analyzer:
            persisted = self.trade_analyzer.get_persisted_trades()
            if persisted:
                self.trade_history = list(persisted)
                wins = sum(1 for t in persisted if t.get("pnl", 0) > 0)
                total_pnl = sum(t.get("pnl", 0) for t in persisted)
                strategies_seen = set(t.get("strategy", "?") for t in persisted)
                log.info(
                    f"TRADE HISTORY: Restored {len(persisted)} trades | "
                    f"{wins}W/{len(persisted)-wins}L | "
                    f"Net P&L: ${total_pnl:+,.2f} | "
                    f"Strategies: {', '.join(strategies_seen)}"
                )
            else:
                log.info("TRADE HISTORY: No previous trades found — starting fresh")

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
        """Connect to IBKR as primary broker/data source.

        Retries aggressively at startup. Bot will continue starting even if
        IBKR isn't ready yet — background reconnect loop will keep trying.
        This lets the dashboard start and positions be monitored even if
        IB Gateway is slow to log in.
        """
        self.broker = IBKRBroker(self.config)

        # Attempt IBKR connection with retry (works locally or with remote Gateway)
        connected = False
        max_retries = 5  # Was 3 — give gateway more time on first boot
        for attempt in range(1, max_retries + 1):
            try:
                connected = self.broker.connect()
                if connected:
                    break
            except Exception as e:
                log.warning(f"IBKR connect exception (attempt {attempt}): {e}")
            if attempt < max_retries:
                wait = min(2 ** attempt, 30)  # Cap at 30s
                log.warning(f"IBKR connection attempt {attempt}/{max_retries} failed - retrying in {wait}s...")
                import time as _time
                _time.sleep(wait)

        # If still not connected after all retries, start background reconnect.
        # Don't block startup — dashboard + position monitoring can still run.
        if not connected:
            log.error(
                "IBKR not connected after all retries. Starting background "
                "reconnect loop. Bot is running in DEGRADED mode (no new trades, "
                "existing bracket stops still active at IBKR)."
            )
            self._start_background_reconnect()

        if connected:
            log.info(f"Connected to IBKR ({self.config.mode} mode) - using as primary data source")
            # Sync account state — IBKR is the source of truth for capital.
            # Override settings.yaml starting_balance with actual broker equity
            # so the drawdown breaker doesn't false-trigger when the config is stale.
            account = self.broker.get_account_summary()
            if account:
                broker_equity = account.get("net_liquidation", 0)
                if broker_equity > 0:
                    self.current_balance = broker_equity
                    self.peak_balance = broker_equity
                    self.start_of_day_balance = broker_equity
                    log.info(
                        f"IBKR BALANCE SYNC: ${broker_equity:,.2f} "
                        f"(overrides settings.yaml starting_balance ${self.config.starting_balance:,.2f})"
                    )
                else:
                    log.warning("IBKR returned $0 equity — keeping config starting_balance as fallback")
            # Sync existing positions
            raw_positions = self.broker.get_positions()
            if raw_positions:
                now = datetime.now(self.tz)

                # Load persisted overnight hold state (if any)
                overnight_state = self._load_overnight_state()
                overnight_syms = {}  # {symbol: saved_pos_data}
                if overnight_state:
                    for item in overnight_state.get("overnight_holds", []):
                        overnight_syms[item["symbol"]] = item
                    for item in overnight_state.get("afterhours_holds", []):
                        overnight_syms[item["symbol"]] = item
                    log.info(
                        f"IBKR SYNC: Loaded overnight state — "
                        f"{len(overnight_syms)} held positions: {list(overnight_syms.keys())}"
                    )

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

                    # Restore overnight hold metadata if this symbol was held overnight
                    saved = overnight_syms.get(sym)
                    if saved:
                        use_stop = saved.get("stop_loss") or pos.get("stop_loss", entry * (1 - stop_pct))
                        use_tp = saved.get("take_profit") or pos.get("take_profit", entry * (1 + tp_pct))
                        use_strategy = saved.get("strategy") or pos.get("strategy", "overnight_hold")
                        is_overnight = True
                        log.info(
                            f"IBKR SYNC: {sym} restored from overnight state — "
                            f"strategy={use_strategy} stop=${use_stop:.2f} tp=${use_tp:.2f}"
                        )
                    else:
                        use_stop = pos.get("stop_loss", entry * (1 - stop_pct))
                        use_tp = pos.get("take_profit", entry * (1 + tp_pct))
                        use_strategy = pos.get("strategy", "synced_from_ibkr")
                        is_overnight = False

                    # Validate non-overnight positions against safety guards
                    # (overnight holds were intentionally kept — trust the EOD decision)
                    sync_flagged = ""
                    if not is_overnight:
                        blocked = self.config.risk_config.get("blocked_symbols", [])
                        if sym.upper() in {s.upper() for s in blocked}:
                            sync_flagged = f"blocked symbol ({sym} is on exclusion list)"
                            log.warning(
                                f"IBKR SYNC FLAGGED: {sym} is a blocked symbol — "
                                f"will close on first cycle"
                            )

                    self.positions[sym] = {
                        **pos,
                        "entry_time": now,
                        "stop_loss": use_stop,
                        "take_profit": use_tp,
                        "trailing_stop_pct": self.config.risk_config.get("trailing_stop_pct", 0.02),
                        "strategy": use_strategy,
                        "executed_via": pos.get("executed_via", "IBKR"),
                        "overnight_hold": is_overnight,
                        "sync_flagged": sync_flagged,
                        "max_hold_bars": 40,
                        "bar_seconds": 300,
                        "max_hold_days": 5,
                    }

                # Check: if we have more positions than max_overnight and overnight state exists,
                # flag unexpected positions (synced but NOT in overnight state) for review
                max_overnight = self.config.schedule_config.get("overnight", {}).get("max_overnight_positions", 3)
                if overnight_state and len(self.positions) > max_overnight:
                    unexpected = [s for s in self.positions if s not in overnight_syms]
                    if unexpected:
                        log.warning(
                            f"IBKR SYNC: {len(self.positions)} positions exceed "
                            f"max_overnight_positions={max_overnight}. "
                            f"Unexpected (not in overnight state): {unexpected}"
                        )

                # Queue flagged positions for close on first cycle
                flagged = [s for s, p in self.positions.items() if p.get("sync_flagged")]
                if flagged:
                    if not hasattr(self, '_slippage_close_queue'):
                        self._slippage_close_queue = []
                    self._slippage_close_queue.extend(flagged)
                    details = [f"{s} ({self.positions[s].get('sync_flagged', '')})" for s in flagged]
                    log.warning(
                        f"IBKR SYNC: {len(flagged)} positions queued for close: "
                        f"{', '.join(details)}"
                    )

                # Clean up overnight state file after successful restore
                self._clear_overnight_state()

                # Add signal cooldown for all synced symbols to prevent re-entry
                # if a strategy generates a signal before the next cycle
                for sym in self.positions:
                    self._signal_cooldowns[sym] = now
                log.info(f"Synced {len(self.positions)} LONG positions from IBKR")
        else:
            log.warning(
                "IBKR connection failed after %d attempts - falling back to Polygon/Yahoo for data. "
                "Ensure IB Gateway is running and IBKR_HOST/IBKR_PORT are set correctly.",
                max_retries,
            )

        # RESTORE PERSISTED STATE: Merge saved position data (trailing stops,
        # targets hit, broker order IDs) into broker-synced positions.
        # The broker sync gives us qty + entry price; the persisted state
        # gives us the richer tracking data the bot needs.
        persisted = self._load_persisted_positions()
        if persisted:
            with self._positions_lock:
                for symbol, saved_pos in persisted.items():
                    if symbol in self.positions:
                        # Merge persisted fields into broker-synced position
                        live_pos = self.positions[symbol]
                        for key in ("trailing_stop", "trailing_stop_pct", "targets_hit",
                                    "broker_stop_order_id", "broker_stop_price",
                                    "_high_water_mark", "_trail_phase",
                                    "breakeven_hit", "tp_trail_activated",
                                    "momentum_runner", "entry_type", "atr_value",
                                    "sector_heat", "source", "breakout_play"):
                            if key in saved_pos and key not in live_pos:
                                live_pos[key] = saved_pos[key]
                        log.debug(f"Restored persisted state for {symbol}")
                    elif symbol not in self.positions:
                        # Position in saved state but not at broker — may have been
                        # closed while bot was down. Skip it.
                        log.info(
                            f"Persisted position {symbol} not found at broker — "
                            f"likely closed while bot was offline. Skipping."
                        )

    def _start_background_reconnect(self):
        """Background thread that retries IBKR connection forever.

        Runs every 30 seconds. Once IBKR connects, syncs account/positions
        and exits. Bot's degraded mode ends; full trading resumes.
        """
        if getattr(self, '_reconnect_thread_started', False):
            return
        self._reconnect_thread_started = True

        def reconnect_loop():
            import time as _time
            attempt = 0
            while not self.broker or not self.broker.is_connected():
                attempt += 1
                _time.sleep(30)
                try:
                    log.info(f"BACKGROUND RECONNECT: attempt #{attempt}...")
                    if self.broker:
                        connected = self.broker.connect()
                    else:
                        self.broker = IBKRBroker(self.config)
                        connected = self.broker.connect()
                    if connected:
                        log.info(
                            f"BACKGROUND RECONNECT SUCCESS: IBKR connected after "
                            f"{attempt} retries. Resuming full trading."
                        )
                        try:
                            self._sync_after_reconnect()
                        except Exception as e:
                            log.warning(f"Post-reconnect sync error: {e}")
                        break
                except Exception as e:
                    log.warning(f"Background reconnect attempt #{attempt} error: {e}")

        import threading
        t = threading.Thread(target=reconnect_loop, daemon=True, name="ibkr-reconnect")
        t.start()
        log.info("Background IBKR reconnect thread started (every 30s)")

    def _sync_after_reconnect(self):
        """Sync account and positions after a successful background reconnect."""
        if not self.broker or not self.broker.is_connected():
            return
        account = self.broker.get_account_summary()
        if account:
            self.current_balance = account.get("net_liquidation", self.current_balance)
            log.info(f"Reconnect sync: account balance ${self.current_balance:,.2f}")
        raw_positions = self.broker.get_positions()
        if raw_positions:
            log.info(f"Reconnect sync: {len(raw_positions)} positions found at IBKR")

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
            "daily_trend_rider": DailyTrendRiderStrategy,
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

        trend_rider_strat = self.strategies.get("daily_trend_rider")
        if trend_rider_strat and hasattr(trend_rider_strat, "add_dynamic_symbols"):
            trend_rider_strat.add_dynamic_symbols(self.universe)
            log.info(f"Injected {universe_count} universe symbols into daily trend rider")

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

        # Weekly digest — Saturday 10am ET.
        # Reviews the last 7 days, hands to Claude, posts Discord embed.
        self.scheduler.add_job(
            self._run_weekly_review,
            "cron", day_of_week="sat", hour=10, minute=0,
            id="weekly_review"
        )

        # Pre-market news vigilance: 6 AM ET sweep over overnight holds.
        # If any held position picked up bearish news overnight, flag it for
        # close at the open (instead of holding into a likely gap-down).
        self.scheduler.add_job(
            self._run_premarket_news_check,
            "cron", day_of_week="mon-fri", hour=6, minute=0,
            id="premarket_news_check"
        )

    def _run_weekly_review(self):
        """Scheduler callback — delegate to WeeklyReview if configured."""
        if self.weekly_review and self.weekly_review.is_available():
            self.weekly_review.run(self.trade_history)

    def _run_premarket_news_check(self):
        """6 AM ET sweep: any overnight hold with bearish news drops gets
        queued for close at the open. Reuses the existing has_bearish_news
        scanner — no overlap with entry-time filters or _validate_synced_position."""
        if not self.config.risk_config.get("premarket_news_check", True):
            return
        if not self.news_feed or not hasattr(self.news_feed, "has_bearish_news"):
            return
        if not self.positions:
            return

        flagged = []
        with self._positions_lock:
            holds = [
                (sym, pos) for sym, pos in self.positions.items()
                if pos.get("overnight_hold") or pos.get("trend_rider") or pos.get("strategy") == "daily_trend_rider"
            ]

        for symbol, _pos in holds:
            try:
                # Cover everything since post-close yesterday + overnight (~16 hours).
                bearish, reason = self.news_feed.has_bearish_news(symbol, lookback_minutes=16 * 60)
                if bearish:
                    flagged.append((symbol, reason))
            except Exception as e:
                log.debug(f"premarket news check failed for {symbol}: {e}")

        if not flagged:
            log.info(f"PREMARKET NEWS SWEEP: {len(holds)} overnight holds, all clean")
            return

        # Queue for close at the open — re-use existing slippage queue mechanism
        if not hasattr(self, "_slippage_close_queue"):
            self._slippage_close_queue = []
        for sym, _ in flagged:
            self._slippage_close_queue.append(sym)

        details = "\n".join(f"  • {s}: {r}" for s, r in flagged)
        log.warning(f"PREMARKET NEWS FLAG: {len(flagged)} holds queued for open-close:\n{details}")
        self.notifier.risk_alert(
            f"PREMARKET NEWS: {len(flagged)} overnight position(s) had bearish news drop:\n{details}\n"
            f"Closing at the open to avoid the gap-down."
        )

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
            # Discover symbols even outside market hours so strategies have symbols
            # to scan when premarket opens (otherwise 0 symbols after daily reset)
            self._discover_dynamic_symbols()
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
                    # Monitor overnight/afterhours positions even outside market hours
                    # Prevents gap-down losses from going undetected until next open
                    overnight_positions = {
                        sym: pos for sym, pos in self.positions.items()
                        if pos.get("overnight_hold") or pos.get("afterhours_hold")
                    }
                    if overnight_positions:
                        self._monitor_overnight_stops(overnight_positions)

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
                    # Persist position state after every monitor tick (covers
                    # stop updates, partial exits, stop moves). Atomic write
                    # so crash mid-write won't corrupt the file.
                    self._persist_positions()

                # --- FULL CYCLE (every ~10 seconds = 3 fast ticks) ---
                if scalp_tick >= 3:
                    scalp_tick = 0

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
                    # Feed sector performance data for geopolitical regime detection
                    if getattr(self, "polygon", None) and self.polygon.enabled and self.regime_detector:
                        try:
                            sector_perf = self.polygon.get_sector_performance()
                            if sector_perf:
                                self.regime_detector.feed_sector_data(sector_perf)
                        except Exception as e:
                            log.debug(f"Sector performance feed failed: {e}")
                    regime_result = self.regime_detector.detect(self.market_data)

                    # 2b. Premarket news reversal: exit if bearish news drops on held positions
                    # OLPX pattern: entered on "beats estimates", "guidance concerns" drops later
                    self._check_premarket_news_reversal()

                    # 2c. Opening fade check (9:30-9:40): evaluate premarket positions
                    # Catches "sell the news" fades where gap stocks reverse at open
                    self._check_opening_fade()

                    # 2d. News-aware profit protection: tighten trails on winners with bearish news
                    # GEMI pattern: +7.5% winner but investigation + sector selloff headlines
                    self._check_news_profit_protection()

                    # 2e. EARNINGS VIGILANCE: check if any open position has earnings
                    # announcement in next 48 hours — exit before gap risk materializes
                    self._check_earnings_vigilance()

                    # 3. Monitor existing positions (stops, targets, trailing)
                    self._monitor_positions()

                    # 3a. BROKER STOP WATCHDOG: verify every position has a
                    # live stop order at the broker. Places emergency stops
                    # for any unprotected positions. This is the safety net
                    # that makes the system crash-proof.
                    if self.positions:
                        self._verify_broker_stops()

                    # 3b. Portfolio-level risk audit (concentration, exposure, max loss)
                    self._check_portfolio_risk()

                    # 3c. Safety nets: alert (don't auto-act) on conditions
                    # the watchdog can't see — IBKR disconnect with positions
                    # held, and per-position stale-data detection.
                    self._check_ibkr_disconnect_with_positions()
                    self._check_stuck_positions()

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

                    # 7a. Pre-market / Post-market filtering: limit strategies, reduce size,
                    #     and enforce quality gate (RVOL + score minimums)
                    if getattr(self, "_in_premarket", False):
                        pm_config = self.config.schedule_config.get("premarket", {})
                        allowed = pm_config.get("allowed_strategies", [])
                        size_mult = pm_config.get("reduce_size_pct", 0.5)
                        min_rvol = pm_config.get("min_rvol", 3.0)
                        min_score = pm_config.get("min_score", 60)
                        if allowed:
                            approved = [s for s in approved if s.get("strategy") in allowed]
                        # Quality gate: reject weak signals in thin premarket liquidity
                        pre_filtered = []
                        for sig in approved:
                            if sig.get("action") != "buy":
                                pre_filtered.append(sig)
                                continue
                            sig_rvol = sig.get("rvol", 0)
                            sig_score = sig.get("score", 0)
                            if sig_rvol < min_rvol or sig_score < min_score:
                                log.info(
                                    f"PREMARKET REJECT: {sig['symbol']} RVOL={sig_rvol:.1f}x "
                                    f"score={sig_score} (need RVOL>={min_rvol} score>={min_score})"
                                )
                                continue
                            pre_filtered.append(sig)
                        approved = pre_filtered
                        for sig in approved:
                            if sig.get("quantity"):
                                sig["quantity"] = max(1, int(sig["quantity"] * size_mult))

                    if getattr(self, "_in_postmarket", False):
                        pm_config = self.config.schedule_config.get("postmarket", {})
                        allowed = pm_config.get("allowed_strategies", [])
                        size_mult = pm_config.get("reduce_size_pct", 0.5)
                        min_rvol = pm_config.get("min_rvol", 3.0)
                        min_score = pm_config.get("min_score", 60)
                        if allowed:
                            approved = [s for s in approved if s.get("strategy") in allowed]
                        # Quality gate: reject weak signals in thin postmarket liquidity
                        post_filtered = []
                        for sig in approved:
                            if sig.get("action") != "buy":
                                post_filtered.append(sig)
                                continue
                            sig_rvol = sig.get("rvol", 0)
                            sig_score = sig.get("score", 0)
                            if sig_rvol < min_rvol or sig_score < min_score:
                                log.info(
                                    f"POSTMARKET REJECT: {sig['symbol']} RVOL={sig_rvol:.1f}x "
                                    f"score={sig_score} (need RVOL>={min_rvol} score>={min_score})"
                                )
                                continue
                            post_filtered.append(sig)
                        approved = post_filtered
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

                        # --- POWER HOUR PHASE 2: Block new entries after 3:30 PM ---
                        # Last 30 min should be for EXITS ONLY, not opening new positions.
                        # Buying at 3:31 PM gives only minutes of price history before EOD
                        # evaluation, creating positions with no thesis for overnight holds.
                        if 30 <= now_time.minute <= 59:
                            pre_filter = len(approved)
                            approved = [sig for sig in approved if sig.get("action") != "buy"]
                            blocked = pre_filter - len(approved)
                            if blocked > 0:
                                log.info(
                                    f"LATE-DAY ENTRY BLOCK: Blocked {blocked} buy signals "
                                    f"after 3:30 PM — exits only in final 30 min"
                                )

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

                    # 7e-2. Rich rejection notifications — show exactly WHY each
                    # signal was filtered so the user can verify from Discord.
                    if rejected_count > 0:
                        rejected_signals = [s for s in signals if s not in approved and s.get("action") == "buy"]
                        self._notify_signal_rejections(rejected_signals)

                    # 7e-3. CYCLE HEARTBEAT — one INFO line per ~minute so the
                    # user can see the bot is actively evaluating even when no
                    # signals fire. Diagnoses "why no trades" at a glance.
                    self._full_cycle_count += 1
                    if self._full_cycle_count % 6 == 1:  # every ~1 min (6 × 10s)
                        bars_warm = 0
                        bars_total = 0
                        if self.market_data:
                            try:
                                tracked = list(getattr(self.market_data, "symbols", []) or [])
                                bars_total = len(tracked)
                                for sym in tracked:
                                    df = self.market_data.get_data(sym)
                                    if df is not None and len(df) >= 40:
                                        bars_warm += 1
                            except Exception:
                                pass
                        log.info(
                            f"CYCLE #{self._full_cycle_count}: "
                            f"regime={regime_str or 'n/a'} | "
                            f"signals={len(signals)}->approved={len(approved)} | "
                            f"positions={len(self.positions)}/{self.risk_manager.max_positions} | "
                            f"bars_warm={bars_warm}/{bars_total} | "
                            f"equity_open={getattr(self, '_equity_market_open', False)} | "
                            f"pm={getattr(self, '_in_premarket', False)} "
                            f"pwr_hr={getattr(self, '_in_power_hour', False)}"
                        )
                        if len(signals) == 0 and bars_total > 0 and bars_warm < bars_total * 0.5:
                            log.info(
                                f"  └─ HINT: only {bars_warm}/{bars_total} symbols have 40+ bars. "
                                f"Strategies need warmup (~3h of 5m bars). This is normal after restart."
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
                        # QUALITY GATE: Multi-factor check before committing capital.
                        # Level 2 order book, per-symbol history, market regime.
                        # Fast, runs locally, zero API cost.
                        if sig.get("action") == "buy":
                            passed, gate_reason = self._entry_quality_gate(sig)
                            if not passed:
                                log.info(
                                    f"QUALITY GATE SKIP: {sig['symbol']} — {gate_reason}"
                                )
                                continue

                        # CLAUDE PRE-TRADE VALIDATION: Ask Claude if we should
                        # take this trade based on recent performance and context.
                        if (sig.get("action") == "buy" and
                                self.ai_insights and self.ai_insights.is_available()):
                            try:
                                claude_verdict = self._claude_pre_trade(sig)
                                if claude_verdict and claude_verdict.get("skip"):
                                    log.info(
                                        f"CLAUDE SKIP: {sig['symbol']} — "
                                        f"{claude_verdict.get('reason', 'AI rejected')}"
                                    )
                                    continue
                                if claude_verdict and claude_verdict.get("reduce_size"):
                                    old_qty = sig.get("quantity", 0)
                                    sig["quantity"] = max(1, int(old_qty * 0.5))
                                    log.info(
                                        f"CLAUDE REDUCE: {sig['symbol']} qty "
                                        f"{old_qty} → {sig['quantity']} — "
                                        f"{claude_verdict.get('reason', '')}"
                                    )
                                if claude_verdict and claude_verdict.get("aggressive"):
                                    old_qty = sig.get("quantity", 0)
                                    size_mult = claude_verdict.get("size_mult", 1.5)
                                    sig["quantity"] = int(old_qty * size_mult)
                                    log.info(
                                        f"CLAUDE AGGRESSIVE: {sig['symbol']} qty "
                                        f"{old_qty} → {sig['quantity']} ({size_mult:.1f}x) — "
                                        f"{claude_verdict.get('reason', '')}"
                                    )
                            except Exception as e:
                                log.debug(f"Claude pre-trade error: {e}")

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
        if getattr(self, "polygon", None) and self.polygon.enabled:
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
                # CRITICAL: Can't monitor stops without a price. Log it so we know.
                stale_count = pos.get("_no_price_count", 0) + 1
                pos["_no_price_count"] = stale_count
                if stale_count % 20 == 1:  # Log every ~60 seconds (20 * 3s)
                    log.warning(
                        f"NO PRICE for {symbol} — stops NOT monitored! "
                        f"({stale_count} consecutive misses)"
                    )
                # SAFETY NET: Force-close after 5 minutes of no price data.
                # A "look away" system MUST close positions it cannot monitor.
                # 100 misses × 3 seconds = 5 minutes of blindness.
                if stale_count >= 100:
                    log.error(
                        f"STALE PRICE WATCHDOG: {symbol} has had NO price data for "
                        f"{stale_count * 3}s (~5 min). Force-closing to prevent "
                        f"unmonitored risk."
                    )
                    positions_to_close.append(
                        (symbol, "stale_price_watchdog",
                         f"No price data for {stale_count * 3}s — cannot monitor stops")
                    )
                continue
            pos["_no_price_count"] = 0

            # --- ENTRY GRACE PERIOD ---
            # Don't allow TRAILING STOP exits within 30 seconds of entry. This prevents:
            # 1. Sell-before-fill race conditions (order just placed)
            # 2. Immediate scalp trail triggers from stale prices
            # 3. False stops from bid/ask spread noise right after entry
            # BUT: Hard stop-loss exits ARE allowed during grace period.
            # A stock crashing through the hard stop must be exited immediately.
            entry_time = pos.get("entry_time")
            in_grace_period = False
            if entry_time:
                seconds_held = (now_ts - entry_time).total_seconds()
                if seconds_held < 30:
                    # Check hard stop even during grace period — emergency exit
                    stop_price = pos.get("stop_loss")
                    if stop_price and current_price <= stop_price:
                        log.warning(
                            f"GRACE PERIOD EMERGENCY EXIT: {symbol} hit hard stop "
                            f"${stop_price:.2f} at ${current_price:.2f} within "
                            f"{seconds_held:.0f}s of entry"
                        )
                        positions_to_close.append(
                            (symbol, "stop_loss",
                             f"Hard stop hit during grace period at ${current_price:.2f}")
                        )
                        continue
                    in_grace_period = True
                    continue  # Skip trailing/profit exits — position too fresh

            entry_price = pos["entry_price"]
            direction = pos.get("direction", "long")

            # Calculate current P&L (long-only)
            pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0

            pos["unrealized_pnl_pct"] = pnl_pct
            pos["current_price"] = current_price

            # --- BREAK-EVEN CHECK (every 3 seconds, not just slow monitor) ---
            be_cfg = self.config.risk_config.get("breakeven", {})
            if (be_cfg.get("enabled", True) and
                    not pos.get("breakeven_hit") and
                    pnl_pct >= be_cfg.get("trigger_pct", 0.01)):
                be_buf = be_cfg.get("buffer_pct", 0.001)
                be_stop = entry_price * (1 + be_buf) if direction == "long" else entry_price * (1 - be_buf)
                old_stop = pos.get("stop_loss", 0)
                if direction == "long" and be_stop > old_stop:
                    pos["stop_loss"] = be_stop
                    pos["breakeven_hit"] = True
                    log.info(
                        f"BREAK-EVEN: {symbol} stop → ${be_stop:.2f} "
                        f"(was ${old_stop:.2f}, P&L: {pnl_pct:.1%})"
                    )

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

            # --- VELOCITY-BASED QUICK EXITS ---
            # Detect fast price spikes and reversal momentum to grab intra-candle profits.
            # Momentum stocks spike then reverse fast — catch the spike before reversal.
            vel_cfg = self.config.risk_config.get("velocity_exits", {})
            if vel_cfg.get("enabled", True) and pos["quantity"] > 3 and pnl_pct > 0:
                # Track recent price history for velocity detection
                price_history = pos.get("_price_history", [])
                now_sec = now_ts.timestamp()
                price_history.append((now_sec, current_price))
                # Keep only last 60 seconds of ticks
                cutoff = now_sec - 60
                price_history = [(t, p) for t, p in price_history if t >= cutoff]
                pos["_price_history"] = price_history

                # === FAST SPIKE DETECTION ===
                # If price moved up X% in last Y seconds — hot runner, take partial
                spike_pct = vel_cfg.get("fast_spike_pct", 0.015)
                spike_window = vel_cfg.get("fast_spike_window_sec", 45)
                spike_close_pct = vel_cfg.get("fast_spike_close_pct", 0.25)

                if not pos.get("_spike_partial_taken") and len(price_history) >= 2:
                    window_start = now_sec - spike_window
                    window_prices = [(t, p) for t, p in price_history if t >= window_start]
                    if window_prices:
                        window_start_price = window_prices[0][1]
                        move_pct = (current_price - window_start_price) / window_start_price
                        if move_pct >= spike_pct:
                            qty_to_close = max(1, int(pos["quantity"] * spike_close_pct))
                            if qty_to_close < pos["quantity"] - 1:
                                log.info(
                                    f"VELOCITY SPIKE: {symbol} +{move_pct:.1%} in {spike_window}s — "
                                    f"taking {spike_close_pct:.0%} partial ({qty_to_close} shares)"
                                )
                                partial_exits.append((
                                    symbol, qty_to_close, -1,
                                    {"pct_from_entry": pnl_pct, "close_pct": spike_close_pct,
                                     "reason": "velocity_spike"}
                                ))
                                pos["_spike_partial_taken"] = True

                # === MOMENTUM REVERSAL DETECTION ===
                # If we've given back significant profit from the HWM — reverse, take 40%
                reversal_retrace = vel_cfg.get("reversal_retrace_pct", 0.30)
                reversal_close = vel_cfg.get("reversal_close_pct", 0.40)

                if (not pos.get("_reversal_partial_taken") and
                        hwm > entry_price and pnl_pct >= 0.02):
                    hwm_gain = hwm - entry_price
                    current_gain = current_price - entry_price
                    retrace_from_hwm = (hwm - current_price) / hwm_gain if hwm_gain > 0 else 0

                    if retrace_from_hwm >= reversal_retrace:
                        qty_to_close = max(1, int(pos["quantity"] * reversal_close))
                        if qty_to_close < pos["quantity"] - 1:
                            log.warning(
                                f"REVERSAL DETECTED: {symbol} retraced {retrace_from_hwm:.0%} "
                                f"from HWM ${hwm:.2f} → ${current_price:.2f} — "
                                f"taking {reversal_close:.0%} partial ({qty_to_close} shares)"
                            )
                            partial_exits.append((
                                symbol, qty_to_close, -2,
                                {"pct_from_entry": pnl_pct, "close_pct": reversal_close,
                                 "reason": "momentum_reversal"}
                            ))
                            pos["_reversal_partial_taken"] = True

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
            # Skip partials for tiny positions (≤3 shares) — not enough shares
            # to meaningfully scale out. Let trailing stop manage the full exit.
            if pt_enabled and pos["quantity"] > 3:
                targets_hit = pos.get("targets_hit", [])
                for i, target in enumerate(pt_targets):
                    if i in targets_hit:
                        continue
                    target_pct = target.get("pct_from_entry", 0)
                    if pnl_pct >= target_pct:
                        close_pct = target.get("close_pct", 0.25)
                        qty_to_close = max(1, int(pos["quantity"] * close_pct))

                        # Don't close everything via partial - leave at least 2
                        # so trailing stop can still manage the remainder
                        if qty_to_close >= pos["quantity"] - 1:
                            qty_to_close = pos["quantity"] - 2

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
                        # SYNC TO BROKER: Update broker-side stop for momentum runners
                        broker_stop = max(
                            pos.get("trailing_stop", 0),
                            pos.get("stop_loss", 0)
                        )
                        if broker_stop > 0:
                            self._update_broker_stop(symbol, broker_stop)
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

                # NEWS PROFIT PROTECTION OVERRIDE:
                # If _check_news_profit_protection() flagged this position,
                # use the tighter news trail instead of normal dynamic trail.
                news_trail = pos.get("_news_trail_override", 0)
                if news_trail > 0:
                    trailing_pct = news_trail
                    if direction == "long":
                        new_trail = current_price * (1 - trailing_pct)
                        if "trailing_stop" not in pos or new_trail > pos.get("trailing_stop", 0):
                            pos["trailing_stop"] = new_trail
                        if current_price <= pos.get("trailing_stop", 0):
                            positions_to_close.append(
                                (symbol, "trailing_stop",
                                 f"News-protected trail stop at ${current_price:.2f} | "
                                 f"P&L: {pnl_pct:+.1%} | news trail: {trailing_pct:.1%}")
                            )
                            continue
                    # Skip normal trailing — news override takes priority
                    continue

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
                        # SYNC TO BROKER: Update broker-side stop to match
                        # Uses the higher of trailing_stop and stop_loss
                        broker_stop = max(
                            pos.get("trailing_stop", 0),
                            pos.get("stop_loss", 0)
                        )
                        if broker_stop > 0:
                            self._update_broker_stop(symbol, broker_stop)
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

    def _check_premarket_news_reversal(self):
        """Premarket News Reversal — exit positions when bearish news drops mid-premarket.

        OLPX pattern: Bot enters at 6 AM on "beats estimates" (+0.97 sentiment),
        but at 8:23 AM "guidance concerns" headline drops (-0.98 sentiment).
        Without this check, bot holds through open and eats the -20% fade.

        This method:
        1. Scans all premarket positions for new bearish news (last 60 min)
        2. Compares entry catalyst (bullish) vs new catalyst (bearish)
        3. If strong bearish news contradicts original entry thesis → EXIT
        4. If moderate bearish news → tighten stop to 1% (prepare to exit)

        Only runs during premarket (before 9:30).
        """
        if not getattr(self, "_in_premarket", False):
            return
        if not self.news_feed:
            return
        if not self.positions:
            return

        # Run at most once per 2 minutes
        now_et = datetime.now(self.tz)
        reversal_key = f"reversal_{now_et.strftime('%H%M')}"
        last_check = getattr(self, '_last_reversal_check', '')
        # Only run on even minutes to avoid spamming
        if now_et.minute % 2 != 0 or last_check == reversal_key:
            return
        self._last_reversal_check = reversal_key

        with self._positions_lock:
            positions_snapshot = dict(self.positions)

        for symbol, pos in positions_snapshot.items():
            entry_time = pos.get("entry_time")
            if not entry_time:
                continue

            # Only check premarket positions
            entry_h = entry_time.hour if hasattr(entry_time, 'hour') else 0
            if entry_h >= 10:  # Not a premarket entry
                continue

            try:
                is_bearish, reason = self.news_feed.has_bearish_news(
                    symbol, lookback_minutes=60
                )
            except Exception as e:
                log.debug(f"News reversal check failed for {symbol}: {e}")
                continue

            if not is_bearish:
                continue

            current_price = self.market_data.get_price(symbol) if self.market_data else None
            entry_price = pos.get("entry_price", 0)
            if not current_price or entry_price <= 0:
                continue

            pnl_pct = (current_price - entry_price) / entry_price
            strategy = pos.get("strategy", "unknown")

            # Strong bearish reversal keywords — immediate exit
            reason_lower = reason.lower()
            critical_reversals = [
                "guidance concerns", "guidance disappoints", "lowers outlook",
                "cuts forecast", "weak outlook", "below guidance",
                "investigation", "class action", "securities fraud",
                "slides more than", "plunges", "tumbles",
            ]
            is_critical = any(kw in reason_lower for kw in critical_reversals)

            if is_critical:
                log.warning(
                    f"NEWS REVERSAL EXIT: {symbol} — bearish news contradicts "
                    f"entry thesis | {reason} | P&L: {pnl_pct:.1%} | "
                    f"Strategy: {strategy} | Exiting before open"
                )
                self._close_position(symbol, "news_reversal",
                    f"Premarket news reversal: {reason}")
            else:
                # Moderate bearish — tighten stop to 1% below current
                if symbol in self.positions:
                    tight_stop = current_price * 0.99
                    old_stop = self.positions[symbol].get("stop_loss", 0)
                    if tight_stop > old_stop:
                        self.positions[symbol]["stop_loss"] = tight_stop
                        log.warning(
                            f"NEWS REVERSAL TIGHTEN: {symbol} — bearish news detected | "
                            f"{reason} | Stop ${old_stop:.2f} → ${tight_stop:.2f} | "
                            f"P&L: {pnl_pct:.1%}"
                        )

    def _check_opening_fade(self):
        """Gap Fade Detector — evaluate premarket positions at market open.

        Runs during the 9:30-9:40 transition window. Catches the OLPX pattern:
        stock gaps on catalyst/earnings, bot enters premarket, but price fades
        when regular session opens (sell the news, profit taking, etc.).

        Checks:
        1. Is the stock below its premarket high? (gap giving back)
        2. Is volume surging on the sell side? (distribution, not accumulation)
        3. Has the first opening candle closed red? (bearish confirmation)
        4. Is the stock already below our entry? (underwater immediately)

        Actions:
        - Tighten stop aggressively for fading positions
        - Exit immediately if stock has given back >50% of premarket gap
        - Log fade alerts for positions that are weakening
        """
        now_et = datetime.now(self.tz)
        h, m = now_et.hour, now_et.minute

        # Only run 9:30-9:40 window
        if not (h == 9 and 30 <= m <= 40):
            return

        # Only run once per minute (not every 10s cycle)
        fade_key = f"fade_check_{now_et.strftime('%H%M')}"
        if getattr(self, '_last_fade_check', '') == fade_key:
            return
        self._last_fade_check = fade_key

        if not self.positions:
            return

        with self._positions_lock:
            positions_snapshot = dict(self.positions)

        positions_to_close = []
        positions_to_tighten = []

        for symbol, pos in positions_snapshot.items():
            entry_time = pos.get("entry_time")
            if not entry_time:
                continue

            # Only check positions entered during premarket (before 9:30)
            entry_h = entry_time.hour if hasattr(entry_time, 'hour') else 0
            entry_m = entry_time.minute if hasattr(entry_time, 'minute') else 0
            if entry_h > 9 or (entry_h == 9 and entry_m >= 30):
                continue  # Entered during regular session, not a premarket position

            entry_price = pos.get("entry_price", 0)
            current_price = self.market_data.get_price(symbol) if self.market_data else None
            if not current_price or entry_price <= 0:
                continue

            pnl_pct = (current_price - entry_price) / entry_price
            strategy = pos.get("strategy", "unknown")

            # Get premarket high from cached data (Polygon snapshot or IBKR)
            pm_high = pos.get("premarket_high", 0)
            if pm_high <= 0:
                # Estimate from entry — assume they entered near the move
                pm_high = entry_price * 1.02  # Conservative estimate

            # How much of the premarket gap has been given back?
            prev_close = pos.get("prev_close", 0)
            if prev_close > 0 and pm_high > prev_close:
                gap_size = pm_high - prev_close
                gap_remaining = current_price - prev_close
                gap_retained_pct = gap_remaining / gap_size if gap_size > 0 else 0
            else:
                gap_retained_pct = 1.0  # Can't calculate, assume holding

            # Check volume character — is selling accelerating?
            bars = self.market_data.get_bars(symbol, 5) if self.market_data else None
            opening_red = False
            sell_volume_surge = False
            if bars is not None and len(bars) >= 2:
                last_close = float(bars["close"].iloc[-1])
                last_open = float(bars["open"].iloc[-1])
                opening_red = last_close < last_open

                # Check if volume is spiking on red candles (distribution)
                recent_vol = float(bars["volume"].iloc[-1])
                prev_vol = float(bars["volume"].iloc[-2]) if len(bars) >= 2 else recent_vol
                if opening_red and recent_vol > prev_vol * 2.0:
                    sell_volume_surge = True

            # --- FADE DECISION LOGIC ---

            # CRITICAL FADE: Given back >50% of gap AND underwater
            if gap_retained_pct < 0.50 and pnl_pct < -0.01:
                positions_to_close.append((symbol, "gap_fade_critical",
                    f"GAP FADE: {symbol} gave back {(1 - gap_retained_pct):.0%} of premarket gap | "
                    f"P&L: {pnl_pct:.1%} | Strategy: {strategy}"))
                continue

            # STRONG FADE: Red opening candle + underwater + sell volume surge
            if opening_red and pnl_pct < 0 and sell_volume_surge:
                positions_to_close.append((symbol, "opening_fade",
                    f"OPENING FADE: {symbol} red candle + sell volume surge at open | "
                    f"P&L: {pnl_pct:.1%} | Strategy: {strategy}"))
                continue

            # MODERATE FADE: Below entry after 9:35, tighten stop aggressively
            if m >= 35 and pnl_pct < -0.005:
                # Tighten stop to 1.5% below current price (vs normal 3%)
                tight_stop = current_price * 0.985
                current_stop = pos.get("stop_loss", 0)
                if tight_stop > current_stop:
                    positions_to_tighten.append((symbol, tight_stop, pnl_pct))

            # WEAK FADE: Gap giving back >30%, warn and tighten
            elif gap_retained_pct < 0.70 and pnl_pct < 0.005:
                tight_stop = current_price * 0.98
                current_stop = pos.get("stop_loss", 0)
                if tight_stop > current_stop:
                    positions_to_tighten.append((symbol, tight_stop, pnl_pct))

        # Execute fade exits
        for symbol, reason_code, msg in positions_to_close:
            log.warning(msg)
            self._close_position(symbol, reason_code, msg)

        # Execute stop tightening
        for symbol, new_stop, pnl_pct in positions_to_tighten:
            if symbol in self.positions:
                old_stop = self.positions[symbol].get("stop_loss", 0)
                self.positions[symbol]["stop_loss"] = new_stop
                log.warning(
                    f"FADE TIGHTEN: {symbol} stop ${old_stop:.2f} → ${new_stop:.2f} | "
                    f"P&L: {pnl_pct:.1%} | Opening fade protection"
                )

        if positions_to_close or positions_to_tighten:
            log.info(
                f"Opening fade check: {len(positions_to_close)} exits, "
                f"{len(positions_to_tighten)} stops tightened"
            )

    def _check_earnings_vigilance(self):
        """Check every open position for upcoming earnings — exit before gap risk.

        Earnings announcements cause unpredictable overnight gaps (can be
        20-50%+ either direction). For a momentum bot, holding through
        earnings is a lottery ticket. Better to exit and re-enter post-announcement.

        Checks:
        1. Earnings within next 48 hours → close position (too risky)
        2. Earnings within next 7 days → tighten stop aggressively
        3. Earnings today → exit immediately regardless of P&L

        Uses IBKR fundamental data (has_earnings_soon on polygon fallback).
        Rate-limited: only runs every 30 minutes per symbol to avoid API spam.
        """
        if not self.positions:
            return

        now = datetime.now(self.tz)

        # Throttle: only check every 30 min (earnings dates don't change intraday)
        last_check = getattr(self, '_earnings_last_check', 0)
        if (now.timestamp() - last_check) < 1800:  # 30 minutes
            return
        self._earnings_last_check = now.timestamp()

        with self._positions_lock:
            positions_snapshot = dict(self.positions)

        for symbol, pos in positions_snapshot.items():
            # Skip if already checked recently for THIS symbol
            last_sym_check = pos.get("_earnings_last_check", 0)
            if (now.timestamp() - last_sym_check) < 3600:  # 1 hour per symbol
                continue

            try:
                # Check via polygon scanner (has_earnings_soon detects news mentions)
                earnings_imminent = False
                if getattr(self, 'polygon', None) and self.polygon.enabled:
                    try:
                        earnings_imminent = self.polygon.has_earnings_soon(symbol, days_ahead=2)
                    except Exception:
                        pass

                pos["_earnings_last_check"] = now.timestamp()

                if earnings_imminent:
                    pnl_pct = pos.get("unrealized_pnl_pct", 0)
                    # Exit if earnings within 2 days — don't hold the gap risk
                    log.warning(
                        f"EARNINGS VIGILANCE: {symbol} has earnings within 48h. "
                        f"Closing position (P&L: {pnl_pct:+.1%}) to avoid gap risk."
                    )
                    if self.notifier:
                        self.notifier.risk_alert(
                            f"Closing {symbol} — earnings within 48 hours. "
                            f"Avoiding overnight gap risk. Current P&L: {pnl_pct:+.1%}"
                        )
                    self._close_position(
                        symbol, "earnings_vigilance",
                        f"Earnings within 48h — exiting before gap risk"
                    )

            except Exception as e:
                log.debug(f"Earnings vigilance error for {symbol}: {e}")

    def _check_news_profit_protection(self):
        """News-Aware Position Protection — two modes:

        MODE 1 - Profit Protection (GEMI pattern):
        Stock is +7.5% but has investigation news → tighten trail to 0.8%

        MODE 2 - Dead Money Exit (OLPX pattern):
        Stock is flat (±2%) for 90+ minutes with bearish catalysts → exit to free capital.
        Also exits positions flat (±2%) for 2+ hours even WITHOUT bearish news —
        dead money is dead money regardless of news.
        OLPX: 4,700 shares at breakeven, "slides 20% on guidance concerns" —
        that capital is trapped doing nothing while bearish news hangs over it.

        Runs every 5 minutes on ALL positions.
        """
        if not self.news_feed:
            return
        if not self.positions:
            return

        now_et = datetime.now(self.tz)
        # Run every 5 minutes (at :00, :05, :10, etc.)
        if now_et.minute % 5 != 0:
            return
        protect_key = f"news_protect_{now_et.strftime('%H%M')}"
        if getattr(self, '_last_news_protect', '') == protect_key:
            return
        self._last_news_protect = protect_key

        with self._positions_lock:
            positions_snapshot = dict(self.positions)

        news_exits = []  # (symbol, reason) for dead money exits

        for symbol, pos in positions_snapshot.items():
            entry_price = pos.get("entry_price", 0)
            current_price = self.market_data.get_price(symbol) if self.market_data else None
            if not current_price or entry_price <= 0:
                continue

            pnl_pct = (current_price - entry_price) / entry_price

            # --- Gather bearish news for this symbol ---
            is_bearish = False
            reason = ""
            bearish_severity = 0  # 0=none, 2=moderate, 3=critical

            try:
                is_bearish, reason = self.news_feed.has_bearish_news(
                    symbol, lookback_minutes=120  # 2 hour window — news lingers
                )
                if is_bearish:
                    bearish_severity = 3  # has_bearish_news only fires on strong signals
            except Exception:
                pass

            if not is_bearish:
                # Also check raw recent_news for moderate bearish (score 2+)
                from bot.signals.news_feed import BEARISH_CATALYSTS
                bearish_count = 0
                reason_parts = []
                max_score = 0
                for article in self.news_feed.recent_news[-50:]:
                    tickers = article.get("tickers", [])
                    if symbol.upper() not in [t.upper() for t in tickers]:
                        continue
                    title = (article.get("title") or "").lower()
                    for kw, score in BEARISH_CATALYSTS.items():
                        if kw in title and score >= 2:
                            bearish_count += 1
                            reason_parts.append(kw)
                            max_score = max(max_score, score)
                            break
                if bearish_count > 0:
                    is_bearish = True
                    bearish_severity = max_score
                    reason = f"Bearish signals ({bearish_count}): {', '.join(reason_parts[:3])}"

            if not is_bearish:
                continue

            # ========================================================
            # MODE 2: DEAD MONEY EXIT — flat position + bearish news
            # OLPX pattern: 4,700 shares at 0.0% for hours with
            # "slides 20% on guidance concerns" headlines.
            # Free the capital for better opportunities.
            # ========================================================
            is_flat = abs(pnl_pct) < 0.02  # Within ±2% of entry (widened from ±1%)
            entry_time = pos.get("entry_time")
            held_minutes = 0
            if entry_time:
                held_minutes = (now_et - entry_time).total_seconds() / 60

            if is_flat and held_minutes >= 90 and bearish_severity >= 2:
                # Flat for 90+ min with bearish news → exit (was 120 min)
                strategy = pos.get("strategy", "unknown")
                news_exits.append((symbol,
                    f"NEWS DEAD MONEY: {symbol} flat ({pnl_pct:+.1%}) for "
                    f"{held_minutes:.0f}min with bearish news: {reason[:60]} | "
                    f"Strategy: {strategy} | Freeing capital"))
                continue

            # Also exit slightly underwater positions (< -0.5%) with critical news after 60 min
            if pnl_pct < -0.005 and pnl_pct > -0.03 and held_minutes >= 60 and bearish_severity >= 3:
                strategy = pos.get("strategy", "unknown")
                news_exits.append((symbol,
                    f"NEWS WEAK EXIT: {symbol} underwater ({pnl_pct:+.1%}) for "
                    f"{held_minutes:.0f}min with critical news: {reason[:60]} | "
                    f"Strategy: {strategy} | Cutting before worse"))
                continue

            # MODE 2b: DEAD MONEY (no news) — flat ±2% for 2+ hours
            # Dead money is dead money regardless of news — free the capital
            if is_flat and held_minutes >= 120 and not is_bearish:
                strategy = pos.get("strategy", "unknown")
                # Skip breakout plays — consolidation before breakout is normal
                if not (pos.get("breakout_play") or pos.get("source") == "prebreakout"):
                    news_exits.append((symbol,
                        f"DEAD MONEY EXIT: {symbol} flat ({pnl_pct:+.1%}) for "
                        f"{held_minutes:.0f}min with no catalyst | "
                        f"Strategy: {strategy} | Freeing capital"))
                    continue

            # ========================================================
            # MODE 1: PROFIT PROTECTION — profitable + bearish news
            # GEMI pattern: +7.5% with investigation headlines.
            # Tighten trail to lock in gains.
            # ========================================================
            if pnl_pct < 0.01:
                continue  # Not profitable enough for trail tightening

            reason_lower = reason.lower()
            strategy = pos.get("strategy", "unknown")

            # Determine severity for trail tightening
            critical_keywords = [
                "investigation", "fraud", "sec", "class action",
                "guidance concerns", "cut guidance", "lowers guidance",
                "bankruptcy", "delisted",
            ]
            is_critical = any(kw in reason_lower for kw in critical_keywords)

            if is_critical:
                # Score 3 bearish on a winner — ultra-tight trail (0.8%)
                news_trail_pct = 0.008
                news_stop = current_price * (1 - news_trail_pct)
            else:
                # Score 2 bearish — tight trail (1.2%)
                news_trail_pct = 0.012
                news_stop = current_price * (1 - news_trail_pct)

            # Only tighten, never loosen
            if symbol not in self.positions:
                continue
            current_stop = self.positions[symbol].get("stop_loss", 0)
            current_trail = self.positions[symbol].get("trailing_stop", 0)
            effective_stop = max(current_stop, current_trail)

            if news_stop > effective_stop:
                self.positions[symbol]["stop_loss"] = news_stop
                self.positions[symbol]["trailing_stop"] = news_stop
                # Override the trailing pct so fast_scalp_monitor uses the tight trail
                self.positions[symbol]["_news_trail_override"] = news_trail_pct
                self.positions[symbol]["_news_protect_active"] = True

                locked_pnl = (news_stop - entry_price) / entry_price
                log.warning(
                    f"NEWS PROFIT PROTECT: {symbol} trail tightened to "
                    f"{news_trail_pct:.1%} (was {effective_stop / current_price - 1:+.1%} from price) | "
                    f"Reason: {reason[:80]} | P&L: {pnl_pct:+.1%} → "
                    f"locked: {locked_pnl:+.1%} | Strategy: {strategy}"
                )

        # Execute dead money exits
        for symbol, msg in news_exits:
            log.warning(msg)
            self._close_position(symbol, "news_dead_money", msg)

    def _monitor_overnight_stops(self, overnight_positions):
        """Monitor stop losses for overnight/afterhours positions outside market hours.

        Runs every 30s in the main loop when market is closed. Uses available
        market data (IBKR extended hours, Polygon) to detect stop hits and
        force-close positions that breach their stop or max-loss limit.
        """
        max_loss_pct = self.config.risk_config.get("max_loss_per_position_pct", 0.08)

        for symbol, pos in overnight_positions.items():
            try:
                current_price = self.market_data.get_price(symbol) if self.market_data else None
                if not current_price or current_price <= 0:
                    continue

                entry_price = pos.get("entry_price", 0)
                stop_price = pos.get("stop_loss")
                direction = pos.get("direction", "long")

                # Check stop loss
                if stop_price:
                    hit = (direction == "long" and current_price <= stop_price) or \
                          (direction == "short" and current_price >= stop_price)
                    if hit:
                        log.warning(
                            f"OVERNIGHT STOP HIT: {symbol} price=${current_price:.2f} "
                            f"stop=${stop_price:.2f} — closing position"
                        )
                        self._close_position(symbol, "overnight_stop", "Overnight stop hit")
                        continue

                # Check max loss per position (failsafe)
                if entry_price > 0:
                    pnl_pct = (current_price - entry_price) / entry_price if direction == "long" \
                        else (entry_price - current_price) / entry_price
                    if pnl_pct <= -max_loss_pct:
                        log.critical(
                            f"OVERNIGHT MAX LOSS: {symbol} P&L={pnl_pct:.1%} exceeds "
                            f"max_loss={max_loss_pct:.0%} — force closing"
                        )
                        self._close_position(symbol, "overnight_max_loss", "Max loss exceeded overnight")
                        continue

            except Exception as e:
                log.error(f"Overnight stop check failed for {symbol}: {e}")

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

        # Feed trend rider its active positions so it can:
        #   - enforce max_positions
        #   - run rotation scoring (swap weakest for clearly superior candidates)
        tr_strat = self.strategies.get("daily_trend_rider")
        if tr_strat:
            tr_positions = {
                sym: pos for sym, pos in self.positions.items()
                if pos.get("trend_rider") or pos.get("strategy") == "daily_trend_rider"
            }
            if hasattr(tr_strat, "set_active_positions"):
                tr_strat.set_active_positions(tr_positions)
            elif hasattr(tr_strat, "set_active_count"):
                tr_strat.set_active_count(len(tr_positions))

        for name, strategy in self.strategies.items():
            try:
                signals = strategy.generate_signals(self.market_data)
                for sig in signals:
                    sig["strategy"] = name
                    # Stamp timestamp + live market price so risk manager checks work
                    # (5% price deviation and 60s staleness guards need these fields)
                    sig["timestamp"] = datetime.now(self.tz)
                    sym = sig.get("symbol")
                    if sym and self.market_data:
                        sig["market_price"] = self.market_data.get_price(sym)
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

    def _check_ibkr_disconnect_with_positions(self):
        """Alert (don't auto-act) if IBKR disconnects while positions are open.

        Bracket stops at IBKR still protect downside, but the bot's internal
        trailing stop and exit logic stop updating — that's a real risk
        the user should know about immediately. Discord alert at 5 min,
        re-alert at 30 min if still down.
        """
        try:
            ibkr_connected = bool(self.broker and self.broker.is_connected())
            has_positions = bool(self.positions)

            if not has_positions or ibkr_connected:
                # Reset state on recovery
                if getattr(self, "_ibkr_disconnect_since", None) is not None:
                    if getattr(self, "_ibkr_disconnect_alerted", False):
                        # We did alert; tell the user it's back
                        self.notifier.system_alert(
                            "IBKR connection RESTORED. Bot back to normal monitoring.",
                            level="success",
                        )
                    self._ibkr_disconnect_since = None
                    self._ibkr_disconnect_alerted = False
                    self._ibkr_disconnect_realerted = False
                return

            # IBKR is disconnected AND we have open positions.
            now_ts = datetime.now(self.tz).timestamp()
            if getattr(self, "_ibkr_disconnect_since", None) is None:
                self._ibkr_disconnect_since = now_ts
                return

            duration_secs = now_ts - self._ibkr_disconnect_since

            # First alert at 5 min
            if duration_secs >= 300 and not getattr(self, "_ibkr_disconnect_alerted", False):
                pos_list = ", ".join(sorted(self.positions.keys())[:8])
                more = f" (+{len(self.positions) - 8} more)" if len(self.positions) > 8 else ""
                self.notifier.risk_alert(
                    f"IBKR DISCONNECTED for {duration_secs / 60:.0f} min while holding "
                    f"{len(self.positions)} positions: {pos_list}{more}\n"
                    f"Broker-side stops are still active at IBKR, but the bot's trailing "
                    f"stops and exit logic CANNOT update until IBKR reconnects. "
                    f"Auto-reconnect is retrying — check IB Gateway / 2FA if this persists."
                )
                self._ibkr_disconnect_alerted = True
                log.warning(
                    f"IBKR DISCONNECT ALERT: {len(self.positions)} positions held, "
                    f"{duration_secs:.0f}s with no broker connection"
                )

            # Re-alert at 30 min — escalation if still not back
            elif duration_secs >= 1800 and not getattr(self, "_ibkr_disconnect_realerted", False):
                self.notifier.risk_alert(
                    f"IBKR STILL DISCONNECTED ({duration_secs / 60:.0f} min). "
                    f"Manual intervention may be required. "
                    f"Check the IBKR mobile app for a 2FA prompt."
                )
                self._ibkr_disconnect_realerted = True
        except Exception as e:
            log.debug(f"_check_ibkr_disconnect_with_positions error: {e}")

    def _check_stuck_positions(self):
        """Detect positions whose price feed appears stale.

        If a position's current price hasn't meaningfully moved (±0.2%) for
        30+ minutes WHILE the broader market (SPY) has moved >0.3% in the
        same window, the data feed for this symbol is probably stale —
        worth alerting because the bot's exits won't fire on a price that
        doesn't change.

        Alerts once per position per stuck-streak.
        """
        if not self.positions:
            return
        try:
            now_ts = datetime.now(self.tz).timestamp()
            spy_price = self.market_data.get_price("SPY") if self.market_data else None
            if spy_price is None or spy_price <= 0:
                return  # Can't compare — bail silently

            if not hasattr(self, "_stuck_position_state"):
                self._stuck_position_state = {}  # symbol -> {first_price, first_price_ts, first_spy, alerted}

            for symbol, pos in list(self.positions.items()):
                current_price = pos.get("current_price") or pos.get("entry_price")
                if not current_price or current_price <= 0:
                    continue

                state = self._stuck_position_state.get(symbol)
                if state is None:
                    self._stuck_position_state[symbol] = {
                        "first_price": current_price,
                        "first_price_ts": now_ts,
                        "first_spy": spy_price,
                        "alerted": False,
                    }
                    continue

                # Reset if price moved meaningfully (>0.2% from anchor)
                price_drift = abs(current_price - state["first_price"]) / state["first_price"]
                if price_drift > 0.002:
                    self._stuck_position_state[symbol] = {
                        "first_price": current_price,
                        "first_price_ts": now_ts,
                        "first_spy": spy_price,
                        "alerted": False,
                    }
                    continue

                # Price hasn't moved — check duration
                stuck_secs = now_ts - state["first_price_ts"]
                if stuck_secs < 1800:  # 30 min threshold
                    continue

                # Has SPY moved meaningfully in that window?
                spy_drift = abs(spy_price - state["first_spy"]) / state["first_spy"]
                if spy_drift < 0.003:
                    continue  # Whole market is also flat — not a feed issue

                if state.get("alerted"):
                    continue  # Already alerted on this streak

                self.notifier.risk_alert(
                    f"STALE DATA SUSPECTED: {symbol} price stuck at "
                    f"${current_price:.2f} for {stuck_secs / 60:.0f} min while SPY "
                    f"moved {spy_drift * 100:+.2f}%. The data feed for {symbol} "
                    f"may be broken — bot exits cannot fire on a non-updating price. "
                    f"Consider checking the position manually."
                )
                log.warning(
                    f"STALE DATA: {symbol} stuck @ ${current_price:.2f} for "
                    f"{stuck_secs:.0f}s; SPY moved {spy_drift * 100:+.2f}%"
                )
                state["alerted"] = True

            # Garbage-collect state for closed positions
            for sym in list(self._stuck_position_state.keys()):
                if sym not in self.positions:
                    self._stuck_position_state.pop(sym, None)

        except Exception as e:
            log.debug(f"_check_stuck_positions error: {e}")

    # =====================================================================
    # Signal rejection visibility — posts a detailed breakdown to Discord
    # so the user sees exactly which filter killed each signal.
    # =====================================================================

    def _notify_signal_rejections(self, rejected_signals):
        """Post rich rejection details to Discord for each rejected buy signal.

        The user wants to see: symbol, price, strategy, score, RVOL, which
        specific check failed (max_positions, direction, score too low, etc.).
        Capped at 5 rejections per cycle to avoid flooding Discord.
        """
        if not rejected_signals or not self.notifier.discord_url:
            return

        # Cap to avoid flooding
        to_report = rejected_signals[:5]
        leftover = len(rejected_signals) - 5

        fields = []
        for sig in to_report:
            symbol = sig.get("symbol", "?")
            price = sig.get("price", 0)
            strategy = sig.get("strategy", "?")
            score = sig.get("score", 0)
            confidence = sig.get("confidence", 0)
            rvol = sig.get("rvol", 0)

            # The true rejection reason from the risk manager (stamped onto
            # the signal when filter_signals rejected it). Fall back to a
            # best-effort reconstruction when the tag is missing (e.g. the
            # signal was dropped by a non-risk-manager filter).
            true_reason = sig.get("_rejection_reason")
            checks = []
            if true_reason:
                checks.append(f"❌ {true_reason}")

            # 1. Position cap
            if len(self.positions) >= self.risk_manager.max_positions:
                checks.append("❌ Position cap full")
            else:
                checks.append(f"✅ Positions {len(self.positions)}/{self.risk_manager.max_positions}")

            # 2. Already holding this symbol
            if symbol in self.positions:
                checks.append("❌ Already holding")

            # 3. Duplicate / recently closed
            if symbol in self._recently_closed:
                checks.append("❌ Recently closed (cooldown)")

            # 4. Score
            min_score = 40  # general minimum
            strat_config = self.config.get_strategy_config(strategy)
            if strat_config:
                min_score = strat_config.get("min_score", min_score)
            if score < min_score:
                checks.append(f"❌ Score {score} < min {min_score}")
            else:
                checks.append(f"✅ Score {score}")

            # 5. RVOL
            min_rvol = strat_config.get("min_rvol", 0) if strat_config else 0
            if min_rvol > 0:
                if rvol < min_rvol:
                    checks.append(f"❌ RVOL {rvol:.1f}x < min {min_rvol}x")
                else:
                    checks.append(f"✅ RVOL {rvol:.1f}x")

            # 6. Direction (long-only)
            action = sig.get("action", "?")
            if action in ("short", "sell"):
                checks.append("❌ SHORT signal (long-only bot)")

            # 7. Pending orders
            if symbol in self._pending_orders:
                checks.append("❌ Order already pending")

            # 8. Blocked symbol
            if hasattr(self, '_blocked_symbols') and symbol in self._blocked_symbols:
                checks.append("❌ Blocked symbol")

            detail = "\n".join(checks)
            fields.append({
                "name": f"❌ {symbol} @ ${price:.2f} ({strategy})",
                "value": detail,
                "inline": False,
            })

        footer = ""
        if leftover > 0:
            footer = f"(+{leftover} more rejections not shown)"

        self.notifier._send_discord_embed(
            title="🔍 Signal Rejections This Cycle",
            color=0x8B949E,  # gray — informational, not alarming
            fields=fields,
            footer=f"AlgoBot {footer}" if footer else "AlgoBot",
        )

    # =====================================================================
    # New-entry safety gates (SPY breaker, global trade cap, strategy DD,
    # pre-market news vigilance). Composed by _entry_safety_gates() so
    # _execute_signal stays clean. Each helper is self-contained and stores
    # its own state; nothing here overlaps with risk_manager (which handles
    # per-position risk + max_positions) or auto_tuner (which handles
    # long-term parameter drift).
    # =====================================================================

    def _entry_safety_gates(self, strategy_name):
        """Run all entry gates. Returns "" to allow, or a reason string to block.

        Each gate is independent; first one that fires wins. Order is cheap-to-
        expensive. None of these duplicate risk_manager (per-position risk +
        max_positions) or auto_tuner (long-term drift); they're complementary.
        """
        try:
            reason = self._gate_spy_circuit_breaker()
            if reason:
                return reason
            reason = self._gate_global_daily_trade_cap()
            if reason:
                return reason
            reason = self._gate_strategy_drawdown(strategy_name)
            if reason:
                return reason
        except Exception as e:
            log.debug(f"safety gate error: {e}")
        return ""

    def _gate_spy_circuit_breaker(self):
        """Block new entries if SPY dropped >2% in the last 30 min.

        The bot already has a regime_detector but it's slow-moving. This is
        a fast, hard breaker for sudden risk-off moves where momentum
        strategies would just buy the falling knife.

        Sticky: once tripped, stays tripped until SPY recovers to within 1%
        of the breaker price. Auto-resumes; no manual intervention needed.
        """
        if not self.market_data:
            return ""

        try:
            spy = self.market_data.get_price("SPY")
            if spy is None or spy <= 0:
                return ""

            now_ts = datetime.now(self.tz).timestamp()

            # State: rolling 30-min SPY history
            if not hasattr(self, "_spy_breaker_state"):
                self._spy_breaker_state = {"history": [], "tripped_at_spy": 0.0}

            hist = self._spy_breaker_state["history"]
            hist.append((now_ts, spy))
            # Trim anything older than 30 min
            cutoff = now_ts - 1800
            hist[:] = [(t, p) for (t, p) in hist if t >= cutoff]
            self._spy_breaker_state["history"] = hist

            tripped_price = self._spy_breaker_state["tripped_at_spy"]
            if tripped_price > 0:
                # Already tripped — check for recovery (within 1% of trip price)
                recovery_threshold = tripped_price * 0.99
                if spy >= recovery_threshold:
                    log.info(
                        f"SPY CIRCUIT BREAKER: cleared at SPY=${spy:.2f} "
                        f"(was ${tripped_price:.2f})"
                    )
                    self.notifier.system_alert(
                        f"SPY circuit breaker CLEARED. SPY back to ${spy:.2f}. Resuming entries.",
                        level="success",
                    )
                    self._spy_breaker_state["tripped_at_spy"] = 0.0
                    return ""
                return f"SPY circuit breaker active (SPY ${spy:.2f}, recover at ${recovery_threshold:.2f})"

            # Not tripped — see if we should trip
            if not hist:
                return ""
            highest_recent = max(p for (_t, p) in hist)
            drop_pct = (spy - highest_recent) / highest_recent
            if drop_pct <= -0.02:  # 2% drop
                self._spy_breaker_state["tripped_at_spy"] = highest_recent
                log.warning(
                    f"SPY CIRCUIT BREAKER TRIPPED: SPY ${highest_recent:.2f} → "
                    f"${spy:.2f} ({drop_pct * 100:+.2f}% in 30 min). Pausing new entries."
                )
                self.notifier.risk_alert(
                    f"SPY CIRCUIT BREAKER: SPY dropped {drop_pct * 100:+.2f}% in 30 min "
                    f"(${highest_recent:.2f} → ${spy:.2f}). Pausing all NEW entries until "
                    f"SPY recovers to ${highest_recent * 0.99:.2f}. Existing positions still managed."
                )
                return f"SPY circuit breaker just tripped (${spy:.2f})"
        except Exception as e:
            log.debug(f"SPY breaker error: {e}")
        return ""

    def _gate_global_daily_trade_cap(self):
        """Hard cap on total entries per day across ALL strategies.

        Per-strategy max_trades_per_day exists already, but on wild days
        the bot can stack 50+ entries across strategies. This is the
        portfolio-level governor — independent of per-strategy caps.
        """
        cap = int(self.config.risk_config.get("max_total_trades_per_day", 25))
        if cap <= 0:
            return ""

        # Count today's entries from trade_history (each trade = 1 entry)
        today = datetime.now(self.tz).date()
        entries_today = 0
        try:
            for t in self.trade_history:
                et = t.get("entry_time")
                if not et:
                    continue
                if isinstance(et, str):
                    try:
                        et_dt = datetime.fromisoformat(et.replace("Z", "+00:00"))
                    except Exception:
                        continue
                else:
                    et_dt = et
                if et_dt.date() == today:
                    entries_today += 1
            # Plus currently-open positions opened today
            for p in self.positions.values():
                pet = p.get("entry_time")
                if pet and hasattr(pet, "date") and pet.date() == today:
                    entries_today += 1
        except Exception as e:
            log.debug(f"daily trade cap counting error: {e}")
            return ""

        if entries_today >= cap:
            # Throttle the alert: only post once per day at the cap moment
            cap_state_key = f"_daily_cap_alerted_{today.isoformat()}"
            if not getattr(self, cap_state_key, False):
                self.notifier.risk_alert(
                    f"DAILY TRADE CAP HIT: {entries_today}/{cap} entries today. "
                    f"Blocking further entries until tomorrow."
                )
                setattr(self, cap_state_key, True)
            return f"Global daily trade cap reached ({entries_today}/{cap})"
        return ""

    def _gate_strategy_drawdown(self, strategy_name):
        """Auto-pause a strategy that's down >X% on its allocated capital today.

        Forces a cooling-off so one bad day doesn't compound. Resets at EOD.
        Independent from auto_tuner allocation tweaks (long-term).
        """
        if not strategy_name:
            return ""

        threshold = float(self.config.risk_config.get("strategy_daily_dd_pause_pct", 0.03))
        if threshold <= 0:
            return ""

        # Allocation $ for this strategy
        try:
            alloc_pct = float(self.config.strategies.get("allocation", {}).get(strategy_name, 0))
            if alloc_pct <= 0:
                return ""
            alloc_capital = self.start_of_day_balance * alloc_pct
            if alloc_capital <= 0:
                return ""
        except Exception:
            return ""

        # Sum today's P&L attributed to this strategy
        today = datetime.now(self.tz).date()
        strat_pnl = 0.0
        try:
            for t in self.trade_history:
                if t.get("strategy") != strategy_name:
                    continue
                xt = t.get("exit_time")
                if not xt:
                    continue
                if isinstance(xt, str):
                    try:
                        xt_dt = datetime.fromisoformat(xt.replace("Z", "+00:00"))
                    except Exception:
                        continue
                else:
                    xt_dt = xt
                if xt_dt.date() == today:
                    strat_pnl += float(t.get("pnl", 0) or 0)
        except Exception as e:
            log.debug(f"strategy DD calc error for {strategy_name}: {e}")
            return ""

        dd_pct = strat_pnl / alloc_capital if alloc_capital > 0 else 0
        if dd_pct <= -threshold:
            paused_key = f"_strategy_paused_today_{strategy_name}_{today.isoformat()}"
            if not getattr(self, paused_key, False):
                self.notifier.risk_alert(
                    f"STRATEGY PAUSED: {strategy_name} down ${strat_pnl:+.2f} "
                    f"({dd_pct * 100:+.2f}% on ${alloc_capital:,.0f} allocated). "
                    f"Cooling off for the rest of today. Auto-resumes tomorrow."
                )
                setattr(self, paused_key, True)
            return f"{strategy_name} hit daily DD limit ({dd_pct * 100:+.2f}%)"
        return ""

    def _validate_synced_position(self, symbol):
        """Validate a synced position against safety guards (blocked symbols, falling knife, bearish news).

        Called during IBKR position sync (startup + continuous) to ensure
        positions that exist at the broker but were NOT entered through _execute_signal
        still get checked. Returns (is_valid, reason) tuple.

        Does NOT close invalid positions — callers decide whether to close or just flag.
        """
        # Blocked symbols (inverse/leveraged ETFs etc.)
        blocked = self.config.risk_config.get("blocked_symbols", [])
        if symbol.upper() in {s.upper() for s in blocked}:
            return False, f"blocked symbol ({symbol} is on exclusion list)"

        # Falling knife check
        falling_knife_pct = self.config.settings.get("risk", {}).get("falling_knife_pct", -5.0)
        try:
            quote = self.market_data.get_quote(symbol) if self.market_data else None
            if quote:
                day_change_pct = quote.get("change_pct", 0)
                if day_change_pct <= falling_knife_pct:
                    return False, f"falling knife (down {day_change_pct:.1f}% today)"
        except Exception:
            pass  # Don't block syncs on quote failure — position already exists at broker

        # Bearish news check
        if self.news_feed:
            try:
                is_bearish, bear_reason = self.news_feed.has_bearish_news(symbol, lookback_minutes=240)
                if is_bearish:
                    return False, f"bearish news ({bear_reason})"
            except Exception:
                pass

        return True, ""

    def _execute_signal(self, signal):
        """Execute a trading signal through broker chain (IBKR -> TradersPost fallback)."""
        symbol = signal["symbol"]
        action = signal["action"]  # buy, sell, short, cover
        strategy = signal.get("strategy", "unknown")
        now = datetime.now(self.tz)

        # ROTATION: trend rider can mark a signal with rotation_target_symbol
        # meaning "close that position first, then enter this one."
        # Synchronous close — if it fails, abort the entry to avoid going over
        # max_positions or holding both during a brief overlap.
        rotation_target = signal.get("rotation_target_symbol")
        if rotation_target and rotation_target != symbol:
            if rotation_target not in self.positions:
                # Already gone — fine, proceed with entry as-is
                log.info(f"ROTATION: target {rotation_target} no longer held, entering {symbol} directly")
            else:
                log.info(f"ROTATION CLOSE: {rotation_target} → entering {symbol}")
                try:
                    self._close_position(
                        rotation_target,
                        "rotation",
                        f"Replaced by stronger trend rider candidate {symbol}",
                    )
                except Exception as e:
                    log.error(f"ROTATION FAILED: could not close {rotation_target}: {e} — aborting entry of {symbol}")
                    return
                # Verify the close took effect (don't enter while old position still tracked)
                if rotation_target in self.positions:
                    log.warning(
                        f"ROTATION ABORTED: {rotation_target} still tracked after close call — "
                        f"skipping {symbol} entry to avoid overshoot"
                    )
                    return

        # LONG-ONLY MODE: Only BUY entries allowed. Block everything else.
        # This is the last-resort guard — strategies, risk manager, and webhooks
        # should all filter before reaching here, but defense-in-depth matters.
        if action not in ("buy", "sell", "cover", "close", "exit"):
            log.warning(f"LONG-ONLY: Blocking unknown action '{action}' for {symbol}")
            return
        if action == "short":
            log.info(f"LONG-ONLY: Blocking short signal for {symbol}")
            return

        # SAFETY GATES — only apply to NEW entries (let exits through always).
        if action == "buy":
            block_reason = self._entry_safety_gates(strategy)
            if block_reason:
                log.info(f"SAFETY GATE BLOCK: {symbol} via {strategy} — {block_reason}")
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

        # --- BLOCKED SYMBOL GUARD ---
        # Block inverse/leveraged ETFs and other excluded symbols from all entry sources.
        # Going long SQQQ in a long-only momentum bot is directionally contradictory.
        if action == "buy":
            blocked = self.config.risk_config.get("blocked_symbols", [])
            if symbol.upper() in {s.upper() for s in blocked}:
                log.warning(f"BLOCKED SYMBOL: {symbol} is on the exclusion list — skipping entry")
                return

        # --- FALLING KNIFE GUARD ---
        # Prevent buying stocks that are down significantly on the day.
        # High RVOL on a -5%+ drop is usually bad news, not a dip-buy opportunity.
        # (WAL pattern: stock down 13% on lawsuit news, bot saw volume spike and bought the bounce)
        # FAIL-CLOSED: if we can't get a quote, block the entry rather than letting it through.
        if action == "buy":
            falling_knife_pct = self.config.settings.get("risk", {}).get("falling_knife_pct", -5.0)
            try:
                quote = self.market_data.get_quote(symbol) if self.market_data else None
                if quote:
                    day_change_pct = quote.get("change_pct", 0)
                    if day_change_pct <= falling_knife_pct:
                        log.warning(
                            f"FALLING KNIFE BLOCK: {symbol} down {day_change_pct:.1f}% today — "
                            f"skipping long entry (threshold: {falling_knife_pct}%) | Strategy: {strategy}"
                        )
                        return
                else:
                    log.warning(
                        f"FALLING KNIFE BLOCK (no quote): {symbol} — cannot verify day change, "
                        f"blocking entry as precaution | Strategy: {strategy}"
                    )
                    return
            except Exception as e:
                log.warning(f"FALLING KNIFE BLOCK (error): {symbol} — quote check failed ({e}), blocking entry")
                return

        # --- BEARISH NEWS CIRCUIT BREAKER ---
        # Prevent buying stocks with recent strong negative catalysts
        # (e.g., store closures, impairment charges, SEC investigation, class action lawsuits)
        # High RVOL on BAD news is a trap, not a setup.
        # Uses 4-hour lookback: class action / SEC headlines are often published hours before
        # the bot sees the symbol on the scanner (RGNX pattern: lawsuits at 13:00, entry at 15:42)
        if action == "buy" and self.news_feed:
            try:
                is_bearish, bear_reason = self.news_feed.has_bearish_news(symbol, lookback_minutes=240)
                if is_bearish:
                    log.warning(
                        f"NEWS BLOCK: {symbol} rejected — bearish catalyst detected | "
                        f"{bear_reason} | Strategy: {strategy}"
                    )
                    return
            except Exception as e:
                log.debug(f"News check failed for {symbol}: {e}")

        # --- DUPLICATE ENTRY GUARD ---
        # Prevent same symbol from being entered twice within cooldown window
        if action == "buy":
            if symbol in self.positions:
                log.info(f"DUPLICATE BLOCKED: {symbol} already in position")
                return

            # Broker-level duplicate check — catches cases where bot positions
            # dict is out of sync with actual broker holdings (e.g., after restart)
            if self.broker and self.broker.is_connected():
                try:
                    broker_positions = self.broker.get_positions()
                    if broker_positions and symbol in broker_positions:
                        broker_qty = broker_positions[symbol].get("quantity", 0)
                        if broker_qty > 0:
                            # Validate before syncing — don't track positions that
                            # fail safety guards (blocked symbols, falling knives, bearish news)
                            is_valid, reject_reason = self._validate_synced_position(symbol)
                            if not is_valid:
                                log.warning(
                                    f"BROKER SYNC FLAGGED: {symbol} at IBKR ({broker_qty} shares) "
                                    f"FAILS safety check: {reject_reason}. "
                                    f"Syncing with flag — monitor will close if stop hit."
                                )
                            else:
                                log.warning(
                                    f"BROKER DUPLICATE BLOCKED: {symbol} already held at IBKR "
                                    f"({broker_qty} shares) but not in bot positions. "
                                    f"Re-syncing position."
                                )
                            # Re-sync this position into the bot
                            pos_data = broker_positions[symbol]
                            entry = pos_data.get("entry_price", pos_data.get("avg_cost", 0))
                            stop_pct = self.config.risk_config.get("stop_loss_pct", 0.03)
                            tp_pct = self.config.risk_config.get("take_profit_pct", 0.20)
                            with self._positions_lock:
                                self.positions[symbol] = {
                                    **pos_data,
                                    "entry_time": datetime.now(self.tz),
                                    "stop_loss": entry * (1 - stop_pct),
                                    "take_profit": entry * (1 + tp_pct),
                                    "trailing_stop_pct": self.config.risk_config.get("trailing_stop_pct", 0.02),
                                    "strategy": "synced_from_ibkr",
                                    "executed_via": "IBKR",
                                    "sync_flagged": reject_reason if not is_valid else "",
                                }
                            # If flagged, schedule immediate close
                            if not is_valid:
                                if not hasattr(self, '_slippage_close_queue'):
                                    self._slippage_close_queue = []
                                self._slippage_close_queue.append(symbol)
                                log.warning(
                                    f"SYNC CLOSE QUEUED: {symbol} — will close on next cycle "
                                    f"({reject_reason})"
                                )
                            return
                except Exception as e:
                    log.debug(f"Broker duplicate check failed: {e}")

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

        # STALE SIGNAL AGE GUARD — reject signals older than 60 seconds.
        # In fast-moving momentum stocks, a signal from even 30s ago can be
        # dangerously stale. This prevents entering on outdated analysis.
        signal_generated = signal.get("generated_at") or signal.get("time")
        if action == "buy":
            if signal_generated:
                try:
                    if isinstance(signal_generated, str):
                        from dateutil.parser import parse as parse_dt
                        signal_dt = parse_dt(signal_generated)
                        if signal_dt.tzinfo is None:
                            signal_dt = signal_dt.replace(tzinfo=self.tz)
                    else:
                        signal_dt = signal_generated
                    signal_age_secs = (now - signal_dt).total_seconds()
                    max_signal_age = 60  # seconds
                    if signal_age_secs > max_signal_age:
                        log.warning(
                            f"STALE SIGNAL REJECT: {symbol} signal is {signal_age_secs:.0f}s old "
                            f"(max {max_signal_age}s) — momentum may have reversed"
                        )
                        return
                except Exception as e:
                    log.debug(
                        f"STALE SIGNAL GUARD: couldn't parse timestamp for {symbol} "
                        f"({signal_generated!r}): {e} — guard skipped for this signal"
                    )
            else:
                # Visibility: signals without generated_at/time silently bypass
                # the age guard. If this fires a lot, fix the signal producer.
                log.debug(
                    f"STALE SIGNAL GUARD: no generated_at/time on {symbol} signal "
                    f"(strategy={signal.get('strategy', '?')}) — guard skipped"
                )

        # STALE PRICE GUARD — reject if live price has moved too far from signal price.
        # Prevents entering with outdated stops/targets (e.g. signal at $46 but price now $49).
        signal_price = signal.get("price", 0)
        if signal_price > 0 and action == "buy":
            price_deviation = abs(current_price - signal_price) / signal_price
            max_deviation = self.config.risk_config.get("max_signal_deviation_pct", 0.03)
            if price_deviation > max_deviation:
                log.warning(
                    f"STALE PRICE REJECT: {symbol} signal @ ${signal_price:.2f} but "
                    f"live price ${current_price:.2f} ({price_deviation:.1%} deviation > "
                    f"{max_deviation:.0%} max) — stops/targets would be invalid"
                )
                return
            elif price_deviation > max_deviation * 0.5:
                # Price moved significantly — recalculate stops and targets from live price
                old_stop = signal.get("stop_loss", 0)
                old_target = signal.get("take_profit", 0)
                if old_stop and signal_price > 0:
                    stop_pct = (signal_price - old_stop) / signal_price
                    signal["stop_loss"] = current_price * (1 - stop_pct)
                if old_target and signal_price > 0:
                    target_pct = (old_target - signal_price) / signal_price
                    signal["take_profit"] = current_price * (1 + target_pct)
                log.info(
                    f"PRICE DRIFT: {symbol} signal ${signal_price:.2f} → live ${current_price:.2f} "
                    f"({price_deviation:.1%}) — recalculated stop=${signal.get('stop_loss', 0):.2f} "
                    f"target=${signal.get('take_profit', 0):.2f}"
                )

        # Price floor filter — no sub-$0.50 junk
        min_price = self.config.settings.get("risk", {}).get("min_price", 0.50)
        if action == "buy" and current_price < min_price:
            log.info(f"PRICE FILTER: {symbol} ${current_price:.2f} below ${min_price} floor")
            return

        # Price ceiling filter — safety net for extreme-priced stocks
        # Top gainers scanner has no cap; this is the last safeguard
        max_buy_price = self.config.settings.get("risk", {}).get("scanner_max_price", 500.0)
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

        # Get current hour for session-based sizing
        current_hour = datetime.now(self.tz).hour
        qty = signal.get("quantity") or self.position_sizer.calculate(
            balance=self.current_balance,
            price=current_price,
            stop_loss=stop_loss_price,
            strategy_allocation=self.config.strategy_allocation.get(strategy, 0.25),
            symbol=symbol,
            # Adaptive sizing inputs: Kelly, drawdown, session-based
            trade_history=self.trade_history,
            peak_balance=self.peak_balance,
            session_stats=getattr(self, '_session_stats', None),
            current_hour=current_hour,
        )

        if qty <= 0:
            log.debug(f"Position size 0 for {symbol} - skipping")
            return

        # Enforce tier caps even when quantity comes from external signal
        # Prevents webhook signals from bypassing position sizer limits
        _, tier_max = self.position_sizer._get_tier_limits(current_price)
        if qty > tier_max:
            log.warning(
                f"TIER CAP: {symbol} signal qty={qty} exceeds tier max={tier_max} "
                f"for ${current_price:.2f} stock. Capping to {tier_max}."
            )
            qty = tier_max

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
        if action == "buy" and getattr(self, "polygon", None) and self.polygon.enabled:
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

        # --- Broker Execution ---
        # IBKR is the sole execution broker. No fallback chain.
        order = None
        executed_via = None

        # Mark symbol as pending to block concurrent orders from webhooks/other threads
        if action == "buy":
            self._pending_orders.add(symbol)

        # Outside-RTH flag: allow pre/post market orders
        outside_rth = getattr(self, '_in_premarket', False) or getattr(self, '_in_postmarket', False)

        # PRE-ORDER SLIPPAGE CHECK: reject stale signals BEFORE placing the order.
        # Uses the SAME threshold as post-fill slippage check (max_slippage_pct) to prevent
        # the enter-then-immediately-exit loop: if the price has already moved beyond what
        # we'd accept as fill slippage, don't place the order at all.
        # (Previously used 2x, but that created a gap where 0.8-1.6% deviation would pass
        # pre-check then fail post-fill check, guaranteeing a double-slippage loss.)
        if action == "buy":
            signal_price = signal.get("price", 0)
            max_pre_slippage = self.config.risk_config.get("max_slippage_pct", 0.008)
            if signal_price > 0 and current_price > 0:
                pre_slippage = abs(current_price - signal_price) / signal_price
                if pre_slippage > max_pre_slippage:
                    log.warning(
                        f"PRE-ORDER REJECT: {symbol} live ${current_price:.2f} vs "
                        f"signal ${signal_price:.2f} = {pre_slippage:.1%} slippage "
                        f"(max {max_pre_slippage:.1%}). Skipping order."
                    )
                    self._pending_orders.discard(symbol)
                    return

        # PRE-ORDER SPREAD CHECK: reject illiquid names BEFORE placing the order.
        # Catches wide bid-ask spreads (e.g. $1.40 bid / $1.60 ask = 13% spread)
        # that guarantee slippage on MARKET orders. Prevents the costly
        # enter-then-immediately-exit loop that lost ~$2,500 on 2024-03-04.
        if action == "buy" and self.broker and self.broker.is_connected():
            max_spread_pct = self.config.risk_config.get("max_spread_pct", 0.02)
            quote = self.broker.get_live_price(symbol) if hasattr(self.broker, 'get_live_price') else None
            if quote and quote.get("bid") and quote.get("ask"):
                bid = quote["bid"]
                ask = quote["ask"]
                if bid > 0 and ask > 0 and ask > bid:
                    mid = (bid + ask) / 2
                    spread_pct = (ask - bid) / mid
                    if spread_pct > max_spread_pct:
                        log.warning(
                            f"SPREAD REJECT: {symbol} bid=${bid:.2f} ask=${ask:.2f} "
                            f"spread={spread_pct:.1%} exceeds max {max_spread_pct:.1%}. "
                            f"Skipping order — illiquid."
                        )
                        self._pending_orders.discard(symbol)
                        return

        # === IBKR-ONLY EXECUTION (Professional Architecture) ===
        # Single broker, single source of truth. No fallback chain = no sync bugs.
        # If IBKR is not connected, we DON'T trade. Period.
        # Bracket orders place a server-side stop at IBKR that survives crashes.
        if self.broker and self.broker.is_connected():
            log.info(f"Executing {symbol} via IBKR{'  [OUTSIDE RTH]' if outside_rth else ''}...")

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
                log.error(f"UNEXPECTED: Non-buy action '{action}' reached execution for {symbol}")
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
            else:
                log.error(
                    f"IBKR order FAILED for {symbol} — not falling through to "
                    f"other brokers. Single-broker architecture: if IBKR can't "
                    f"execute, we skip the trade."
                )
        else:
            log.error(
                f"IBKR NOT CONNECTED — cannot execute {action.upper()} {symbol}. "
                f"No fallback brokers in professional architecture. "
                f"Ensure IB Gateway is running."
            )

        # If IBKR failed or not connected, do NOT create phantom positions
        if not order:
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

        # Slippage protection: compare fill price against BOTH live price AND signal price.
        # The old check only compared fill vs live (both ~same), missing the real problem:
        # signal at $46.82 → live at $49.55 → fill at $49.55 = no "slippage" detected.
        # Now we also check fill vs original signal price to catch stale-signal entries.
        if order.get("avg_fill_price") and action == "buy":
            max_slippage = self.config.risk_config.get("max_slippage_pct", 0.008)
            # Check 1: fill vs live price (traditional slippage)
            live_slippage = abs(actual_price - current_price) / current_price if current_price > 0 else 0
            # Check 2: fill vs original signal price (stale signal detection)
            signal_price = signal.get("price", 0)
            signal_slippage = abs(actual_price - signal_price) / signal_price if signal_price > 0 else 0
            worst_slippage = max(live_slippage, signal_slippage)

            if worst_slippage > max_slippage:
                slippage_source = "signal" if signal_slippage > live_slippage else "market"
                reference_price = signal_price if signal_slippage > live_slippage else current_price
                log.warning(
                    f"SLIPPAGE REJECT: {symbol} slippage {worst_slippage:.1%} exceeds "
                    f"max {max_slippage:.1%} — closing position immediately | "
                    f"Signal ${signal_price:.2f} → Live ${current_price:.2f} → Fill ${actual_price:.2f} "
                    f"(worst vs {slippage_source}: {worst_slippage:.1%})"
                )
                self.notifier.risk_alert(
                    f"Slippage reject: {symbol} filled ${actual_price:.2f} "
                    f"(signal ${signal_price:.2f}, slippage {worst_slippage:.1%}). "
                    f"Closing immediately."
                )
                # Schedule immediate close (can't close inline, position not yet tracked)
                if not hasattr(self, '_slippage_close_queue'):
                    self._slippage_close_queue = []
                self._slippage_close_queue.append(symbol)
            elif worst_slippage > max_slippage * 0.5:
                log.warning(
                    f"SLIPPAGE WARNING: {symbol} slippage {worst_slippage:.1%} "
                    f"(threshold {max_slippage:.1%}) | Signal ${signal_price:.2f} → Fill ${actual_price:.2f}"
                )

        # Update current_price to actual fill price for position tracking
        if order.get("avg_fill_price"):
            current_price = actual_price

        # SAFETY NET: Recalculate stops/targets if fill price makes them invalid.
        # e.g. signal at $46.82 → target $47.08, but filled at $49.55 → target is BELOW entry.
        if action == "buy" and take_profit_price <= current_price:
            signal_price = signal.get("price", 0)
            if signal_price > 0:
                target_pct = (take_profit_price - signal_price) / signal_price if signal_price > 0 else 0.03
                take_profit_price = round(current_price * (1 + max(target_pct, 0.015)), 2)
                stop_pct = (signal_price - stop_loss_price) / signal_price if signal_price > 0 else 0.03
                stop_loss_price = round(current_price * (1 - max(stop_pct, 0.01)), 2)
                log.warning(
                    f"RECALCULATED TARGETS: {symbol} fill ${current_price:.2f} made targets invalid — "
                    f"new stop=${stop_loss_price:.2f} target=${take_profit_price:.2f}"
                )

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

        # Enrich signal with prev_close from Polygon cache (for gap fade detection)
        if not signal.get("prev_close") and getattr(self, "polygon", None) and self.polygon.enabled:
            _pc = self.polygon._price_cache.get(symbol, {})
            if _pc.get("prev_close"):
                signal["prev_close"] = _pc["prev_close"]

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
                # Sector heat: macro-driven theme play (multi-day hold, wider stops)
                "sector_heat": signal.get("sector_heat", False),
                # Gap fade detection data (premarket→open transition)
                "prev_close": signal.get("prev_close", 0),
                "premarket_high": signal.get("premarket_high", current_price),
                # BROKER-SIDE STOP TRACKING: Track the actual stop order at the
                # broker so the bot can cancel/replace as trailing moves up.
                # If the IBKR order came back as a bracket, capture the SL leg ID.
                "broker_stop_order_id": (order.get("sl_order_id") if order.get("bracket") else None),
                "broker_stop_price": stop_loss_price if order.get("bracket") else 0,
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

        # Process slippage close queue — close positions where fill slippage
        # exceeded max_slippage_pct (R:R is ruined, better to exit immediately)
        if hasattr(self, '_slippage_close_queue') and self._slippage_close_queue:
            close_syms = list(self._slippage_close_queue)
            self._slippage_close_queue.clear()
            for close_sym in close_syms:
                if close_sym in self.positions:
                    log.warning(f"SLIPPAGE CLOSE: Closing {close_sym} — excessive entry slippage")
                    self._close_position(close_sym, "slippage_reject",
                                         "Excessive slippage on entry — R:R invalid")

    def _close_position(self, symbol, reason_type, reason_msg):
        """Close a position through IBKR. Thread-safe with double-close guard."""
        # Exit cooldown: skip if this symbol was recently closed
        # (prevents broker sync re-add → monitor re-close → rejection loop)
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

        # Cancel any broker-side stop order to avoid orphan orders
        if pos.get("broker_stop_order_id") and self.broker and self.broker.is_connected():
            try:
                self.broker.cancel_order(pos["broker_stop_order_id"])
                log.info(f"Cancelled broker stop order for {symbol}")
            except Exception as e:
                log.debug(f"Could not cancel broker stop for {symbol}: {e}")

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

        # === IBKR-ONLY CLOSE (Professional Architecture) ===
        # Single broker close. If IBKR is connected, close through it.
        # If not connected, the server-side bracket stop is still active.
        order = None
        partial_fill_remaining = 0
        outside_rth = getattr(self, '_in_premarket', False) or getattr(self, '_in_postmarket', False)

        if self.broker and self.broker.is_connected():
            # Cancel any existing broker-side stop (bracket leg) before closing
            # to avoid the stop triggering after we've already sold
            stop_order_id = pos.get("broker_stop_order_id")
            if stop_order_id:
                try:
                    self.broker.cancel_order(stop_order_id)
                except Exception:
                    pass

            order = self.broker.place_order(
                symbol=symbol,
                action=action,
                quantity=close_qty,
                order_type="MARKET",
                outside_rth=outside_rth,
            )
            if order:
                close_broker = "IBKR"
                # Detect partial fill. Don't block the monitor thread retrying
                # here — the remaining qty is handled by leaving the position
                # tracked with a reduced quantity, so the next monitor cycle
                # (3s later) issues a clean retry.
                filled = order.get("quantity", close_qty)
                requested = order.get("requested_quantity", close_qty)
                if filled < requested:
                    partial_fill_remaining = requested - filled
                    log.warning(
                        f"PARTIAL FILL ON CLOSE: {symbol} filled {filled}/{requested} — "
                        f"{partial_fill_remaining} shares still held. Will retry next cycle."
                    )
            else:
                log.error(f"IBKR close order FAILED for {symbol}")
        else:
            # IBKR not connected — but if we have a bracket order, the server-side
            # stop is STILL active at IBKR. The position is protected even now.
            log.error(
                f"IBKR NOT CONNECTED for close of {symbol} — cannot execute exit. "
                f"Server-side bracket stop (if placed) is still active at IBKR."
            )

        # Only remove position if a broker ACTUALLY closed it this cycle
        if not close_broker:
            log.error(
                f"CLOSE FAILED for {symbol} — position stays tracked for retry next cycle. "
                f"original_broker={original_broker}"
            )
            return

        executed_via = close_broker

        # Determine actual closed quantity (may differ from pos["quantity"] on partial fills)
        closed_qty = pos["quantity"]  # Default: full close
        if partial_fill_remaining > 0:
            closed_qty = pos["quantity"] - partial_fill_remaining

        # Calculate P&L only on the shares actually closed
        if pos["direction"] == "long":
            pnl = (current_price - pos["entry_price"]) * closed_qty
        else:
            pnl = (pos["entry_price"] - current_price) * closed_qty

        self.daily_pnl += pnl
        # Update internal balance tracking
        self.current_balance += pnl
        self.peak_balance = max(self.peak_balance, self.current_balance)

        if partial_fill_remaining > 0:
            log.info(
                f"PARTIAL CLOSED {symbol} via {executed_via} | {reason_type} | "
                f"Closed {closed_qty}/{pos['quantity']} shares | "
                f"P&L: ${pnl:+.2f} | {partial_fill_remaining} shares remain | {reason_msg}"
            )
        else:
            log.info(
                f"CLOSED {symbol} via {executed_via} | {reason_type} | "
                f"P&L: ${pnl:+.2f} | {reason_msg}"
            )

        pnl_pct = pnl / (pos["entry_price"] * closed_qty) if pos["entry_price"] * closed_qty > 0 else 0
        hold_time = (datetime.now(self.tz) - pos["entry_time"]) if "entry_time" in pos else None

        # Rich exit notification
        self.notifier.trade_exit(
            symbol=symbol,
            direction=pos["direction"],
            qty=closed_qty,
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
            "quantity": closed_qty,
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

        # SESSION STATS: Track per-hour performance for session-based sizing
        # The bot learns its best trading hours and sizes up during them.
        self._update_session_stats(pnl)

        # PSYCHOLOGY MARKERS: Detect bot "emotional" patterns
        # Revenge trading: taking more trades after a big loss
        # Overconfidence: sizing up after a win streak
        self._check_psychology_markers(pnl)

        # DAILY LOSS SOFT-STOP: if down 2%+ today, enter cautious mode
        # Hard stop at 4% (already handled in main loop)
        self._check_daily_loss_soft_stop()

        # Persist trade to disk (survives restarts for AI learning)
        if self.trade_analyzer:
            self.trade_analyzer.persist_trade(self.trade_history[-1])

        # PER-TRADE LEARNING: After EVERY closed trade, Claude analyzes what
        # happened and feeds insights back into bot parameters. This is the
        # core self-improvement loop — every trade makes the bot smarter.
        if self.ai_insights and self.ai_insights.is_available():
            try:
                self._claude_post_trade_learning(self.trade_history[-1])
            except Exception as e:
                log.debug(f"Post-trade learning error: {e}")

        # Auto-trigger Claude AI quick insight every 5 trades (summary view)
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
            if partial_fill_remaining > 0:
                # Partial fill: keep position with reduced quantity for retry next cycle
                if symbol in self.positions:
                    self.positions[symbol]["quantity"] = partial_fill_remaining
                    self.positions[symbol]["_partial_close_pending"] = True
                    log.warning(
                        f"POSITION KEPT (partial fill): {symbol} reduced to "
                        f"{partial_fill_remaining} shares — will retry close next cycle"
                    )
            else:
                # Full close: remove position entirely
                self.positions.pop(symbol, None)

        # Clean up tick-by-tick subscription for closed position (only on full close)
        if partial_fill_remaining == 0 and self.broker and hasattr(self.broker, 'unsubscribe_tick_by_tick'):
            try:
                self.broker.unsubscribe_tick_by_tick([symbol])
            except Exception:
                pass

        # Record exit cooldown — prevents broker sync from re-adding this
        # position during settlement delay (causes duplicate exit rejections)
        if partial_fill_remaining == 0:
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
        if self.broker and self.broker.is_connected():
            try:
                broker_positions = self.broker.get_positions()
                broker_pos = broker_positions.get(symbol) if broker_positions else None
                if broker_pos:
                    actual_broker_qty = int(broker_pos.get("quantity", 0) or 0)
                    if actual_broker_qty <= 0:
                        log.warning(f"PARTIAL CLOSE BLOCKED: {symbol} not held at broker (0 shares)")
                        return
                    if qty_to_close > actual_broker_qty:
                        log.warning(
                            f"PARTIAL CLOSE QTY CAPPED: {symbol} requested {qty_to_close} "
                            f"but broker holds {actual_broker_qty}. Capping to prevent short."
                        )
                        qty_to_close = actual_broker_qty
            except Exception as e:
                log.debug(f"Could not verify broker position for partial close {symbol}: {e}")

        # Execute via IBKR (sole execution broker)
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

    def _update_session_stats(self, pnl):
        """Track trade outcomes by hour-of-day for session-based edge detection.

        Over time, the bot learns which hours are profitable and which aren't.
        Position sizer uses this to boost size during best hours, reduce in bad hours.
        Data persists in memory; rebuilt from trade_history on restart.
        """
        if not hasattr(self, '_session_stats'):
            self._session_stats = {}

        hour = datetime.now(self.tz).hour
        if hour not in self._session_stats:
            self._session_stats[hour] = {"trades": 0, "wins": 0, "pnl": 0.0}

        self._session_stats[hour]["trades"] += 1
        self._session_stats[hour]["pnl"] += pnl
        if pnl > 0:
            self._session_stats[hour]["wins"] += 1

        # Log findings every 50 trades
        total_trades = sum(h["trades"] for h in self._session_stats.values())
        if total_trades % 50 == 0 and total_trades >= 20:
            best = max(self._session_stats.items(),
                       key=lambda x: x[1]["pnl"] / max(x[1]["trades"], 1))
            worst = min(self._session_stats.items(),
                        key=lambda x: x[1]["pnl"] / max(x[1]["trades"], 1))
            log.info(
                f"SESSION EDGE: Best hour = {best[0]}:00 "
                f"(${best[1]['pnl']:+.0f} over {best[1]['trades']} trades) | "
                f"Worst hour = {worst[0]}:00 "
                f"(${worst[1]['pnl']:+.0f} over {worst[1]['trades']} trades)"
            )

    def _check_psychology_markers(self, pnl):
        """Detect bot 'emotional' patterns and flag them.

        Bots can exhibit human-like mistakes if not careful:
        - Revenge trading: high trade frequency after losses (greedy to recover)
        - Overconfidence: higher sizing after win streaks
        - Style drift: abandoning what works for new shiny things

        This method tracks meta-metrics and logs warnings when patterns detected.
        """
        if not hasattr(self, '_psych_state'):
            self._psych_state = {
                "consecutive_losses": 0,
                "consecutive_wins": 0,
                "trades_in_last_hour": [],
                "last_big_loss_time": None,
            }

        now = datetime.now(self.tz)
        state = self._psych_state

        if pnl > 0:
            state["consecutive_wins"] += 1
            state["consecutive_losses"] = 0
        else:
            state["consecutive_losses"] += 1
            state["consecutive_wins"] = 0
            # Big loss = more than 1% of balance
            if abs(pnl) > self.current_balance * 0.01:
                state["last_big_loss_time"] = now

        # Track trades in last hour
        state["trades_in_last_hour"].append(now)
        one_hour_ago = now - timedelta(hours=1)
        state["trades_in_last_hour"] = [t for t in state["trades_in_last_hour"] if t >= one_hour_ago]

        # FLAG: Revenge trading (>8 trades in 1 hour after big loss)
        if (state["last_big_loss_time"] and
                (now - state["last_big_loss_time"]).total_seconds() < 3600 and
                len(state["trades_in_last_hour"]) >= 8):
            log.warning(
                f"PSYCHOLOGY FLAG: Possible revenge trading — "
                f"{len(state['trades_in_last_hour'])} trades in 1h after big loss. "
                f"Bot will be more cautious for next hour."
            )
            # Make Claude pre-trade more conservative for next hour
            self._revenge_mode_until = now + timedelta(hours=1)

        # FLAG: Win streak (>5 consecutive wins can lead to oversizing)
        if state["consecutive_wins"] >= 5:
            log.info(
                f"PSYCHOLOGY: Win streak of {state['consecutive_wins']} — "
                f"maintain discipline, don't chase"
            )

        # FLAG: Loss streak (>3 consecutive losses = something's wrong)
        if state["consecutive_losses"] >= 3:
            log.warning(
                f"PSYCHOLOGY FLAG: {state['consecutive_losses']} consecutive losses — "
                f"reducing size on next trade"
            )

    def _check_daily_loss_soft_stop(self):
        """Soft-stop at -2% daily loss: cut size in half, no new positions for 1 hour.

        Hard stop at -4% is handled by risk_manager in main loop.
        This adds a softer intermediate pause to prevent bad days from spiraling.
        """
        if self.start_of_day_balance <= 0:
            return
        daily_pnl_pct = (self.current_balance - self.start_of_day_balance) / self.start_of_day_balance

        if daily_pnl_pct <= -0.02 and not getattr(self, '_daily_soft_stop_active', False):
            self._daily_soft_stop_active = True
            self._soft_stop_until = datetime.now(self.tz) + timedelta(hours=1)
            log.warning(
                f"DAILY SOFT STOP: Down {daily_pnl_pct:.1%} today. "
                f"No new entries for 1 hour. Existing positions still monitored."
            )
            if getattr(self, 'notifier', None):
                self.notifier.risk_alert(
                    f"Daily soft-stop triggered: Down {daily_pnl_pct:.1%}. "
                    f"Pausing new entries for 1 hour to prevent revenge trading."
                )

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
        """Update account balance and tracking via IBKR."""
        if self.broker and self.broker.is_connected():
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


    def _update_broker_stop(self, symbol, new_stop_price):
        """Update broker-side stop order at IBKR when trailing stop moves up.

        Every time the bot's internal trailing stop ratchets higher, we cancel
        the old broker stop and place a new one. This ensures IBKR always has
        the current stop price — if the bot crashes, IBKR enforces the latest
        stop, not the original entry stop.
        """
        if not self.broker or not self.broker.is_connected():
            return False

        pos = self.positions.get(symbol)
        if not pos:
            return False

        # Throttle: only update broker stop every 30 seconds per symbol
        # to avoid hammering the API on every 3-second tick
        last_update = pos.get("_last_broker_stop_update", 0)
        now_ts = datetime.now(self.tz).timestamp()
        if now_ts - last_update < 30:
            return False

        # Only update if the new stop is meaningfully higher (>0.2% difference)
        current_broker_stop = pos.get("broker_stop_price", 0)
        if current_broker_stop > 0:
            improvement = (new_stop_price - current_broker_stop) / current_broker_stop
            if improvement < 0.002:  # Less than 0.2% improvement — skip
                return False

        try:
            # Cancel existing stop order if we have one tracked
            old_stop_id = pos.get("broker_stop_order_id")
            if old_stop_id:
                try:
                    self.broker.cancel_order(old_stop_id)
                except Exception:
                    pass  # Order may already be filled/cancelled

            # Place new GTC stop order at IBKR
            qty = pos.get("quantity", 0)
            if qty <= 0:
                return False

            result = self.broker.place_order(
                symbol=symbol,
                action="SELL",
                quantity=qty,
                order_type="STOP",
                stop_price=new_stop_price,
                outside_rth=True,
            )

            if result:
                pos["broker_stop_order_id"] = result.get("order_id")
                pos["broker_stop_price"] = new_stop_price
                pos["_last_broker_stop_update"] = now_ts
                log.info(
                    f"BROKER STOP UPDATED: {symbol} stop → ${new_stop_price:.2f} "
                    f"(order_id={result.get('order_id', 'unknown')})"
                )
                return True
            else:
                log.warning(f"BROKER STOP UPDATE FAILED: {symbol} — IBKR returned None")
                return False

        except Exception as e:
            log.warning(f"BROKER STOP UPDATE exception for {symbol}: {e}")
            return False

    def _verify_broker_stops(self):
        """Watchdog: verify every open position has an active broker-side stop.

        Runs periodically (called from main loop). If a position has no stop
        at the broker (e.g., bracket order failed, or stop was filled without
        closing the position), places one immediately via IBKR.
        """
        if not self.positions or not self.broker or not self.broker.is_connected():
            return

        try:
            # Get all open orders from IBKR
            open_orders = self.broker.get_open_orders() if hasattr(self.broker, 'get_open_orders') else []

            # Build symbol -> total stop qty map from active SELL stops
            stop_qty_by_symbol = {}
            for order in (open_orders or []):
                if (order.get("order_type") in ("STOP", "STP") and
                        order.get("action") == "SELL" and
                        order.get("status") in ("Submitted", "PreSubmitted", "ApiPending")):
                    sym = order.get("symbol")
                    qty = int(order.get("quantity", 0) or 0)
                    if sym:
                        stop_qty_by_symbol[sym] = stop_qty_by_symbol.get(sym, 0) + qty

            # Check each position
            with self._positions_lock:
                positions_snapshot = dict(self.positions)

            for symbol, pos in positions_snapshot.items():
                pos_qty = int(pos.get("quantity", 0) or 0)
                covered_qty = stop_qty_by_symbol.get(symbol, 0)

                # Stop covers full position — good
                if covered_qty >= pos_qty and pos_qty > 0:
                    continue

                # Either no stop, or stop undercovers the position.
                stop_price = pos.get("stop_loss") or pos.get("trailing_stop")
                if not stop_price:
                    entry = pos.get("entry_price", 0)
                    stop_pct = self.config.risk_config.get("stop_loss_pct", 0.03)
                    stop_price = entry * (1 - stop_pct)

                if stop_price and stop_price > 0:
                    if covered_qty == 0:
                        log.warning(
                            f"STOP WATCHDOG: {symbol} has NO broker-side stop! "
                            f"Placing emergency stop @ ${stop_price:.2f} for {pos_qty} shares"
                        )
                    else:
                        log.warning(
                            f"STOP WATCHDOG: {symbol} stop undercovers position "
                            f"({covered_qty}/{pos_qty} shares). Replacing with full-size stop @ ${stop_price:.2f}"
                        )
                    self._update_broker_stop(symbol, stop_price)

        except Exception as e:
            log.debug(f"Broker stop verification error: {e}")

    def _persist_positions(self):
        """Save all position state to disk. Survives bot crashes and restarts.

        PROFESSIONAL ARCHITECTURE: Position state must never exist only in
        memory. This writes the full position dict (including stop prices,
        trailing stops, targets hit, broker order IDs) to a JSON file.
        Called after every position change (entry, exit, stop update).
        """
        try:
            import json
            positions_file = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "data", "positions_state.json"
            )
            state = {}
            with self._positions_lock:
                for symbol, pos in self.positions.items():
                    # Serialize position — convert datetime objects
                    serialized = {}
                    for k, v in pos.items():
                        if isinstance(v, datetime):
                            serialized[k] = v.isoformat()
                        elif k.startswith("_") and k not in (
                            "_high_water_mark", "_trail_phase",
                        ):
                            continue  # Skip internal transient state
                        else:
                            serialized[k] = v
                    state[symbol] = serialized

            # Skip the disk write if nothing changed since last persist.
            # This matters because _persist_positions runs every 3 seconds
            # while positions are open, and most ticks don't mutate state.
            serialized_bytes = json.dumps(state, sort_keys=True, default=str).encode()
            state_hash = hash(serialized_bytes)
            if getattr(self, "_last_persisted_hash", None) == state_hash:
                return

            # Atomic write: write to temp file then rename (prevents corruption)
            tmp_file = positions_file + ".tmp"
            with open(tmp_file, "w") as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(tmp_file, positions_file)
            self._last_persisted_hash = state_hash
        except Exception as e:
            log.debug(f"Position persistence error: {e}")

    def _load_persisted_positions(self):
        """Load position state from disk on startup.

        Merges with broker-synced positions. Persisted state has richer
        data (trailing stops, targets hit, broker order IDs) that sync
        doesn't capture.
        """
        try:
            import json
            positions_file = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "data", "positions_state.json"
            )
            if not os.path.exists(positions_file):
                return {}

            with open(positions_file, "r") as f:
                state = json.load(f)

            # Convert datetime strings back
            from dateutil.parser import parse as parse_dt
            for symbol, pos in state.items():
                if "entry_time" in pos and isinstance(pos["entry_time"], str):
                    try:
                        pos["entry_time"] = parse_dt(pos["entry_time"])
                        if pos["entry_time"].tzinfo is None:
                            pos["entry_time"] = pos["entry_time"].replace(tzinfo=self.tz)
                    except Exception:
                        pass

            # Check staleness — if file is >24 hours old, don't trust it
            file_age = time.time() - os.path.getmtime(positions_file)
            if file_age > 86400:
                log.warning(
                    f"Position state file is {file_age / 3600:.0f}h old — "
                    f"ignoring, will sync from broker instead"
                )
                return {}

            log.info(f"Loaded {len(state)} persisted positions from disk")
            return state

        except Exception as e:
            log.debug(f"Could not load persisted positions: {e}")
            return {}

    def _claude_pre_trade(self, signal):
        """Ask Claude whether to take a trade based on recent performance + learned patterns.

        Uses the learning state built up from post-trade analysis to inform
        decisions. Strategies that keep losing get auto-skipped. Aggressive
        sizing on high-conviction setups.

        Returns dict:
          {"skip": True, "reason": "..."} — don't take the trade
          {"reduce_size": True, "reason": "..."} — take it but smaller
          {"aggressive": True, "size_mult": 1.5, "reason": "..."} — size up
          {} — proceed normally
        """
        if not self.ai_insights or not self.ai_insights.is_available():
            return {}

        symbol = signal.get("symbol", "")
        strategy = signal.get("strategy", "unknown")

        # === LEARNED PATTERN CHECK (fast, no Claude call needed) ===
        # Auto-skip strategies that have been flagged as losing multiple times
        learning = getattr(self, '_learning_adjustments', {})
        avoided = learning.get("avoided_strategies", {})
        if avoided.get(strategy, 0) >= 3:
            return {
                "skip": True,
                "reason": f"AUTO-SKIP: '{strategy}' flagged avoid {avoided[strategy]}x by learning"
            }

        # Context for Claude
        recent_trades = self.trade_history[-15:] if self.trade_history else []
        wins = sum(1 for t in recent_trades if t.get("pnl", 0) > 0)
        losses = len(recent_trades) - wins
        win_rate = (wins / len(recent_trades) * 100) if recent_trades else 0

        strat_trades = [t for t in recent_trades if t.get("strategy") == strategy]
        strat_wins = sum(1 for t in strat_trades if t.get("pnl", 0) > 0)
        strat_wr = (strat_wins / len(strat_trades) * 100) if strat_trades else 0

        open_count = len(self.positions)
        regime = getattr(self, 'current_regime', 'unknown')
        score = signal.get("score", 0)
        rvol = signal.get("rvol", 0)

        # Learning signals
        boosted = learning.get("boosted_strategies", {}).get(strategy, 0)

        # Strategy-specific extras. Trend rider swing trades have different
        # decision context (daily trend, rotation) than intraday scalps.
        is_trend_rider = (
            strategy == "daily_trend_rider" or signal.get("trend_rider")
        )
        trend_rider_block = ""
        if is_trend_rider:
            rotation_target = signal.get("rotation_target_symbol", "")
            trend_rider_block = (
                f"\nTREND RIDER CONTEXT (multi-day swing, holds overnight):\n"
                f"  Green days: {signal.get('_daily_green_days', '?')} consecutive\n"
                f"  Daily SuperTrend: ${signal.get('_daily_supertrend', 0):.2f}\n"
                f"  Daily 20 EMA: ${signal.get('_daily_ema20', 0):.2f}\n"
                f"  Daily ATR: ${signal.get('_daily_atr', 0):.2f}\n"
                f"  Setup score: {signal.get('_rider_score', 0):.0f}\n"
                f"  Entry type: {signal.get('entry_type', '?')}"
                f"{f' | ROTATION: replacing {rotation_target}' if rotation_target else ''}\n"
                f"Additional rules for trend rider:\n"
                f"- SKIP if green_days<3 or ADX too weak (not a real trend)\n"
                f"- SKIP if rotation and existing position was a recent win\n"
                f"- TAKE with normal size — this is an overnight hold, don't oversize\n"
            )

        prompt = (
            f"Trade decision (ONE line, start TAKE, SKIP, REDUCE, or AGGRESSIVE):\n"
            f"BUY {symbol} via {strategy} @ ${signal.get('price', 0):.2f}\n"
            f"Stop: ${signal.get('stop_loss', 0):.2f} | Target: ${signal.get('take_profit', 0):.2f}\n"
            f"Score: {score} | RVOL: {rvol:.1f}x | Confidence: {signal.get('confidence', 0):.2f}\n"
            f"Recent: {wins}W/{losses}L ({win_rate:.0f}%)\n"
            f"Strategy '{strategy}': {strat_wins}W/{len(strat_trades)-strat_wins}L ({strat_wr:.0f}%)"
            f"{f' | LEARNED BOOST x{boosted}' if boosted else ''}\n"
            f"Open positions: {open_count}/10 | Regime: {regime}"
            f"{trend_rider_block}\n\n"
            f"Rules:\n"
            f"- AGGRESSIVE (1.5x size) if: score>=80 AND RVOL>=5 AND win_rate>=60%\n"
            f"- AGGRESSIVE if: strategy has LEARNED BOOST and score>=70\n"
            f"- TAKE if setup is solid\n"
            f"- REDUCE if: >7 open positions or strategy win rate <40%\n"
            f"- SKIP if: strategy win rate <25% on 5+ trades or bad regime"
        )

        try:
            response = self.ai_insights._call_claude(prompt)
            if not response:
                return {}

            response_upper = response.strip().upper()
            if response_upper.startswith("SKIP"):
                return {"skip": True, "reason": response.strip()[:200]}
            elif response_upper.startswith("REDUCE"):
                return {"reduce_size": True, "reason": response.strip()[:200]}
            elif response_upper.startswith("AGGRESSIVE"):
                return {
                    "aggressive": True,
                    "size_mult": 1.5,
                    "reason": response.strip()[:200]
                }
            else:
                return {}
        except Exception:
            return {}

    def _entry_quality_gate(self, signal):
        """Multi-factor quality check before entry. Only TAKE trades that pass
        multiple confirmations. This is the "constant profit" discipline — fewer
        trades but higher quality.

        Returns:
            (True, "") if trade passes quality gate
            (False, reason) if trade should be skipped
        """
        symbol = signal.get("symbol", "")
        score = signal.get("score", 0)

        # === 1. LEVEL 2 ORDER BOOK CHECK (with imbalance EMA) ===
        # Look for buying pressure: bids stacked higher than asks means demand
        # Research: "Order flow imbalance has a near-linear relationship with
        # short-horizon price changes." — we track 3 snapshots and compute EMA.
        if self.broker and self.broker.is_connected() and hasattr(self.broker, 'get_order_book'):
            try:
                # Take 3 rapid snapshots (300ms apart) to detect imbalance trend
                import time as _time
                imbalances = []
                book = None
                for snap_num in range(3):
                    book = self.broker.get_order_book(symbol, num_rows=5, timeout=2)
                    if book:
                        imbalances.append(book.get("imbalance", 0))
                    if snap_num < 2:
                        _time.sleep(0.3)

                if book and imbalances:
                    # Use latest snapshot for spread check
                    spread_pct = book.get("spread_pct", 0)
                    current_imbalance = imbalances[-1]

                    # EMA (weighted toward recent): [0.2, 0.3, 0.5]
                    if len(imbalances) == 3:
                        ema_imbalance = imbalances[0] * 0.2 + imbalances[1] * 0.3 + imbalances[2] * 0.5
                        # Imbalance trend: is buying pressure RAMPING UP?
                        imbalance_ramping = (imbalances[-1] > imbalances[0] + 0.1)
                    else:
                        ema_imbalance = current_imbalance
                        imbalance_ramping = False

                    # Reject wide spreads (>2% = poor liquidity, bad fills likely)
                    if spread_pct > 0.02:
                        return False, f"wide spread {spread_pct*100:.1f}% (>2%)"

                    # Reject consistently bearish EMA imbalance
                    if ema_imbalance < -0.3:
                        return False, f"bearish EMA imbalance {ema_imbalance:+.2f}"

                    # Reject if spot imbalance diverges negative even if EMA ok
                    # (detects flipping pressure)
                    if current_imbalance < -0.4:
                        return False, f"bearish spot imbalance {current_imbalance:+.2f}"

                    # Store order flow metrics on signal for Claude's context
                    signal["_book_imbalance"] = current_imbalance
                    signal["_book_imbalance_ema"] = ema_imbalance
                    signal["_book_spread"] = spread_pct
                    signal["_book_ramping"] = imbalance_ramping

                    # Boost score if imbalance is BULLISH and ramping
                    # Strong buying pressure signal — rare and valuable
                    if ema_imbalance > 0.3 and imbalance_ramping:
                        current_score = signal.get("score", 0)
                        signal["score"] = current_score + 10
                        log.info(
                            f"ORDER FLOW BOOST: {symbol} EMA imbalance "
                            f"{ema_imbalance:+.2f} ramping → score {current_score} → {signal['score']}"
                        )
            except Exception as e:
                log.debug(f"Order book check error for {symbol}: {e}")

        # === 2. PER-SYMBOL HISTORY CHECK ===
        # If we've traded this symbol before, what's the track record?
        symbol_trades = [t for t in self.trade_history if t.get("symbol") == symbol]
        if len(symbol_trades) >= 3:
            wins = sum(1 for t in symbol_trades if t.get("pnl", 0) > 0)
            win_rate = wins / len(symbol_trades)
            avg_pnl = sum(t.get("pnl", 0) for t in symbol_trades) / len(symbol_trades)

            # Auto-skip symbols where we've lost money 3+ times
            if len(symbol_trades) >= 3 and avg_pnl < 0 and win_rate < 0.35:
                return False, (
                    f"symbol history bad: {wins}/{len(symbol_trades)} wins "
                    f"({win_rate*100:.0f}%), avg P&L ${avg_pnl:+.2f}"
                )

        # === 3. MARKET CONTEXT CHECK ===
        # Respect broader market regime — don't chase longs in crisis
        regime = getattr(self, 'current_regime', 'neutral')
        if regime == "crisis":
            return False, f"market regime is {regime} — no new longs"

        # === 4. MINIMUM SCORE GATE ===
        # Even after all strategy approvals, require a minimum conviction
        min_score = self.config.risk_config.get("min_entry_score", 50)
        if score < min_score:
            return False, f"score {score} below min {min_score}"

        # === 5. POSITION CAP CHECK ===
        max_positions = self.config.risk_config.get("max_positions", 10)
        if len(self.positions) >= max_positions:
            return False, f"at max positions ({max_positions})"

        # === 6. DAILY SOFT STOP CHECK ===
        # If bot hit -2% daily loss, no new entries for 1 hour
        if getattr(self, '_daily_soft_stop_active', False):
            now = datetime.now(self.tz)
            soft_stop_until = getattr(self, '_soft_stop_until', now)
            if now < soft_stop_until:
                mins_left = (soft_stop_until - now).total_seconds() / 60
                return False, f"daily soft-stop active ({mins_left:.0f} min left)"
            else:
                # Cooldown expired — resume
                self._daily_soft_stop_active = False

        # === 7. REVENGE TRADING CHECK ===
        # If flagged for revenge trading, skip this hour
        revenge_until = getattr(self, '_revenge_mode_until', None)
        if revenge_until and datetime.now(self.tz) < revenge_until:
            return False, "revenge trading mode — cooling down"

        return True, ""

    def _claude_post_trade_learning(self, trade):
        """After EVERY closed trade, Claude analyzes and updates bot behavior.

        Core self-improvement loop:
        1. Claude reviews the trade: setup, entry, exit, P&L
        2. Extracts patterns: what worked, what didn't
        3. Updates internal learning state that affects future decisions
        4. Adjusts strategy weights, stop distances, timing filters

        This runs AFTER every trade close (not every 5th). Every loss makes
        the bot more careful about that setup pattern. Every win reinforces.
        """
        if not self.ai_insights or not self.ai_insights.is_available():
            return

        symbol = trade.get("symbol", "")
        strategy = trade.get("strategy", "unknown")
        pnl = trade.get("pnl", 0)
        pnl_pct = trade.get("pnl_pct", 0)
        reason = trade.get("reason", "")
        hold_mins = trade.get("hold_time_mins", 0)
        entry_price = trade.get("entry_price", 0)
        exit_price = trade.get("exit_price", 0)

        win = pnl > 0

        # Context: recent performance of this strategy
        recent = [t for t in self.trade_history[-30:] if t.get("strategy") == strategy]
        strat_win_rate = sum(1 for t in recent if t.get("pnl", 0) > 0) / len(recent) * 100 if recent else 0

        # MINIMUM SAMPLE SIZE GUARD: don't make parameter adjustments on too
        # little data. Require at least 5 trades of this strategy to have any
        # statistical validity. 10+ trades before strategy-level adjustments.
        if len(recent) < 5:
            log.debug(
                f"Skipping post-trade learning for '{strategy}' — "
                f"only {len(recent)} trades (need 5+ for significance)"
            )
            return

        prompt = (
            f"Trade just closed. Analyze and give ONE concrete adjustment "
            f"(<200 chars, actionable format: 'TIGHTEN_STOP:1.5%' or 'AVOID:momentum_runner_premarket' "
            f"or 'BOOST:prebreakout' or 'NO_CHANGE').\n\n"
            f"Trade: {'WIN' if win else 'LOSS'} ${pnl:+.2f} ({pnl_pct*100:+.1f}%)\n"
            f"Symbol: {symbol} | Strategy: {strategy}\n"
            f"Entry: ${entry_price:.2f} → Exit: ${exit_price:.2f}\n"
            f"Hold: {hold_mins:.0f} min | Exit reason: {reason}\n"
            f"Strategy recent: {len(recent)} trades, {strat_win_rate:.0f}% win rate\n\n"
            f"Common adjustments:\n"
            f"- TIGHTEN_STOP:X% if premature stop-out happened\n"
            f"- WIDEN_STOP:X% if noise stopped us out before real move\n"
            f"- BOOST:strategy_name if win pattern repeats\n"
            f"- AVOID:strategy_name if loss pattern repeats 3+ times\n"
            f"- NO_CHANGE if this was a one-off."
        )

        try:
            response = self.ai_insights._call_claude(prompt)
            if not response:
                return

            response = response.strip()
            log.info(f"POST-TRADE LEARNING [{symbol}]: {response[:200]}")

            # Parse Claude's recommendation and apply it
            if not hasattr(self, '_learning_adjustments'):
                self._learning_adjustments = {
                    "boosted_strategies": {},  # strategy_name -> boost_count
                    "avoided_strategies": {},  # strategy_name -> avoid_count
                    "stop_adjustments": [],    # list of {strategy, adjustment, timestamp}
                }

            upper = response.upper()

            # BOOST: boost strategy weight (requires 10+ trades for stat significance)
            boost_match = re.search(r'BOOST:(\w+)', upper)
            if boost_match and len(recent) >= 10:
                boosted = boost_match.group(1).lower()
                count = self._learning_adjustments["boosted_strategies"].get(boosted, 0) + 1
                self._learning_adjustments["boosted_strategies"][boosted] = count
                log.info(f"LEARNING: Boosting strategy '{boosted}' (total boosts: {count})")
                if self.trade_analyzer and hasattr(self.trade_analyzer, 'strategy_scores'):
                    current = self.trade_analyzer.strategy_scores.get(boosted, 50)
                    self.trade_analyzer.strategy_scores[boosted] = min(100, current + 5)

            # AVOID: penalize strategy (requires 10+ trades for stat significance)
            avoid_match = re.search(r'AVOID:(\w+)', upper)
            if avoid_match and len(recent) >= 10:
                avoided = avoid_match.group(1).lower()
                count = self._learning_adjustments["avoided_strategies"].get(avoided, 0) + 1
                self._learning_adjustments["avoided_strategies"][avoided] = count
                log.warning(f"LEARNING: Avoiding strategy '{avoided}' (total avoids: {count})")
                if self.trade_analyzer and hasattr(self.trade_analyzer, 'strategy_scores'):
                    current = self.trade_analyzer.strategy_scores.get(avoided, 50)
                    self.trade_analyzer.strategy_scores[avoided] = max(0, current - 10)

            # TIGHTEN_STOP or WIDEN_STOP: adjust default stop distance
            stop_match = re.search(r'(TIGHTEN|WIDEN)_STOP:?(\d+\.?\d*)', upper)
            if stop_match:
                direction = stop_match.group(1)
                pct = float(stop_match.group(2))
                self._learning_adjustments["stop_adjustments"].append({
                    "direction": direction,
                    "pct": pct,
                    "strategy": strategy,
                    "timestamp": datetime.now(self.tz).isoformat(),
                })
                # Actually apply the adjustment to the config (in-memory)
                current_stop = self.config.risk_config.get("stop_loss_pct", 0.03)
                if direction == "TIGHTEN":
                    new_stop = max(0.015, current_stop * 0.9)  # never below 1.5%
                else:
                    new_stop = min(0.05, current_stop * 1.1)  # never above 5%
                self.config.risk_config["stop_loss_pct"] = new_stop
                log.info(
                    f"LEARNING: Stop distance {direction.lower()}ed from "
                    f"{current_stop*100:.1f}% → {new_stop*100:.1f}%"
                )

            # Persist learning state to disk so it survives restarts
            try:
                import json
                learning_file = os.path.join(
                    os.path.dirname(os.path.dirname(__file__)),
                    "data", "learning_adjustments.json"
                )
                with open(learning_file, "w") as f:
                    json.dump(self._learning_adjustments, f, indent=2, default=str)
            except Exception:
                pass

        except Exception as e:
            log.debug(f"Claude post-trade analysis error: {e}")

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

        # --- Pre-Open Futures Gap Risk Check ---
        # If holding overnight positions and SPY futures indicate a gap down,
        # tighten stops on non-favored sectors to protect against opening gap losses
        self._preopen_gap_risk_check()

        regime_str = self.regime_detector.current_regime.upper() if self.regime_detector else "N/A"
        self.notifier.system_alert(
            f"Pre-market scan complete. Balance: ${self.current_balance:,.2f} | "
            f"Regime: {regime_str}",
            level="info"
        )

    def _preopen_gap_risk_check(self):
        """Pre-open risk check: tighten stops on overnight holds when futures signal a gap.

        Runs at pre-market scan time. Checks SPY snapshot for overnight gap direction.
        If SPY is gapping down significantly (>1%), tightens stops on long positions
        UNLESS they're in a favored sector (e.g., energy during geopolitical regime).

        This prevents overnight holds from losing gains to a morning gap-down.
        """
        if not self.positions:
            return

        overnight_positions = [
            (sym, pos) for sym, pos in self.positions.items()
            if pos.get("overnight_hold", False)
        ]
        if not overnight_positions:
            return

        # Get SPY gap from Polygon snapshot
        spy_gap_pct = 0
        if getattr(self, "polygon", None) and self.polygon.enabled:
            spy_snap = self.polygon.get_snapshot("SPY")
            if spy_snap:
                spy_gap_pct = spy_snap.get("change_pct", 0)

        if spy_gap_pct >= -0.5:
            # No significant gap down — no action needed
            log.info(
                f"PRE-OPEN CHECK: SPY gap {spy_gap_pct:+.1f}% — "
                f"no tightening needed for {len(overnight_positions)} overnight positions"
            )
            return

        # Determine favored sectors (from geopolitical regime or sector heat)
        favored_sectors = set()
        if self.regime_detector:
            hedge_rec = self.regime_detector.get_status().get("hedge_recommendation", {})
            if isinstance(hedge_rec, dict):
                hot = hedge_rec.get("hot_sectors", [])
                if hot:
                    favored_sectors = set(hot)

        tightened = 0
        for symbol, pos in overnight_positions:
            entry_price = pos["entry_price"]
            current_price = pos.get("current_price", entry_price)
            old_stop = pos.get("stop_loss", 0)

            # Check if this position is in a favored sector
            sector = "Unknown"
            if self.polygon:
                sector = self.polygon.get_sector(symbol)

            if sector in favored_sectors:
                log.info(
                    f"PRE-OPEN SKIP: {symbol} in hot sector {sector} — keeping wide stop "
                    f"despite SPY gap {spy_gap_pct:+.1f}%"
                )
                continue

            # Tighten stop based on gap severity
            if spy_gap_pct <= -2.0:
                # Severe gap: tighten to breakeven or 1% above entry
                new_stop = entry_price * 1.005
                reason = f"severe gap (SPY {spy_gap_pct:+.1f}%)"
            elif spy_gap_pct <= -1.0:
                # Moderate gap: tighten to 1.5% trail from current price
                new_stop = current_price * 0.985
                reason = f"moderate gap (SPY {spy_gap_pct:+.1f}%)"
            else:
                # Small gap: tighten to 2% trail
                new_stop = current_price * 0.98
                reason = f"mild gap (SPY {spy_gap_pct:+.1f}%)"

            if new_stop > old_stop:
                pos["stop_loss"] = round(new_stop, 2)
                tightened += 1
                log.info(
                    f"PRE-OPEN TIGHTEN: {symbol} ({sector}) stop "
                    f"${old_stop:.2f} -> ${new_stop:.2f} — {reason}"
                )

        if tightened > 0:
            self.notifier.system_alert(
                f"Pre-open gap risk: SPY {spy_gap_pct:+.1f}% — tightened stops on "
                f"{tightened}/{len(overnight_positions)} overnight positions",
                level="warning"
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

    def _check_trend_rider_daily_exit(self, symbol, pos):
        """Check if today's daily close broke the trend for a trend rider position.

        Fetches today's daily bar (or uses last close) and checks:
        1. Close below SuperTrend → trend flipped bearish
        2. Close below 20 EMA → lost moving average support
        3. First red daily close after 5+ green days → momentum exhaustion

        Returns (should_exit: bool, reason: str).
        """
        broker = self.broker if self.broker and self.broker.is_connected() else None
        if not broker:
            return False, ""

        try:
            bars = broker.get_historical_bars(symbol, duration="10 D", bar_size="1 day")
            if bars is None or len(bars) < 5:
                return False, ""

            closes = bars["close"].values.astype(float)
            highs = bars["high"].values.astype(float)
            lows = bars["low"].values.astype(float)
            today_close = closes[-1]

            # SuperTrend check
            st = pos.get("_daily_supertrend", 0)
            if st > 0 and today_close < st:
                return True, f"Daily close ${today_close:.2f} < SuperTrend ${st:.2f}"

            # 20 EMA check (use stored value from entry scan, or recompute)
            ema20 = pos.get("_daily_ema20", 0)
            if ema20 > 0 and today_close < ema20:
                return True, f"Daily close ${today_close:.2f} < 20 EMA ${ema20:.2f}"

            # Momentum exhaustion: first red day after a long green streak
            green_at_entry = pos.get("_daily_green_days", 0)
            if green_at_entry >= 5 and len(closes) >= 2:
                today_red = closes[-1] < closes[-2]
                if today_red:
                    return True, (
                        f"First red daily close after {green_at_entry}+ green days — "
                        f"momentum exhaustion"
                    )

            return False, ""
        except Exception as e:
            log.debug(f"Trend rider daily exit check failed for {symbol}: {e}")
            return False, ""

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

        # 5. Sector heat bonus (max 15 pts)
        # Macro-driven sector plays (e.g., energy during Hormuz crisis) persist
        # for days/weeks — holding overnight is the right move
        if pos.get("sector_heat", False):
            bullish_score += 15
            reasons.append("Sector heat — macro-driven, multi-day theme")

        # 6. Hold duration penalty (up to -20 pts)
        # Positions held < 1 hour have insufficient price history for overnight thesis
        entry_time = pos.get("entry_time")
        if entry_time:
            held_minutes = (datetime.now(self.tz) - entry_time).total_seconds() / 60
            if held_minutes < 30:
                bullish_score -= 20
                reasons.append(f"Held only {held_minutes:.0f}min — no overnight thesis")
            elif held_minutes < 60:
                bullish_score -= 10
                reasons.append(f"Held only {held_minutes:.0f}min — limited price history")

        # 7. Bearish news penalty (up to -15 pts)
        # Holding overnight with active bearish catalysts is risky
        if self.news_feed:
            try:
                has_bearish, news_reason = self.news_feed.has_bearish_news(
                    symbol, lookback_minutes=120
                )
                if has_bearish:
                    bullish_score -= 15
                    reasons.append(f"Bearish news: {news_reason[:40]}")
            except Exception:
                pass

        # Verdict: 60+ = bullish enough for after-hours (raised from 50 — be selective)
        should_hold = bullish_score >= 60
        reason_str = " | ".join(reasons[:5])

        log.info(
            f"BULLISH EVAL: {symbol} | Score: {bullish_score}/100 | "
            f"{'HOLD' if should_hold else 'CLOSE'} | {reason_str}"
        )

        return should_hold, reason_str, bullish_score

    def _save_overnight_state(self, overnight_holds, afterhours_holds):
        """Persist overnight hold decisions to disk so restarts respect the max limit."""
        try:
            state = {
                "date": datetime.now(self.tz).strftime("%Y-%m-%d"),
                "overnight_holds": [],
                "afterhours_holds": [],
            }
            for sym in overnight_holds:
                pos = self.positions.get(sym, {})
                state["overnight_holds"].append({
                    "symbol": sym,
                    "entry_price": pos.get("entry_price", 0),
                    "stop_loss": pos.get("stop_loss", 0),
                    "take_profit": pos.get("take_profit", 0),
                    "strategy": pos.get("strategy", ""),
                    "quantity": pos.get("quantity", 0),
                })
            for sym in afterhours_holds:
                pos = self.positions.get(sym, {})
                state["afterhours_holds"].append({
                    "symbol": sym,
                    "entry_price": pos.get("entry_price", 0),
                    "stop_loss": pos.get("stop_loss", 0),
                    "take_profit": pos.get("take_profit", 0),
                    "strategy": pos.get("strategy", ""),
                    "quantity": pos.get("quantity", 0),
                })
            os.makedirs(os.path.dirname(self._overnight_state_file), exist_ok=True)
            with open(self._overnight_state_file, "w") as f:
                json.dump(state, f, indent=2)
            log.info(f"Saved overnight state: {len(overnight_holds)} overnight, {len(afterhours_holds)} AH")
        except Exception as e:
            log.error(f"Failed to save overnight state: {e}")

    def _load_overnight_state(self):
        """Load overnight hold state from disk. Returns None if no valid state for today."""
        try:
            if not os.path.exists(self._overnight_state_file):
                return None
            with open(self._overnight_state_file, "r") as f:
                state = json.load(f)
            # Only use state from today or yesterday (overnight holds are one-day affairs)
            state_date = state.get("date", "")
            today = datetime.now(self.tz).strftime("%Y-%m-%d")
            yesterday = (datetime.now(self.tz) - timedelta(days=1)).strftime("%Y-%m-%d")
            # Weekend: Friday holds are valid on Monday
            days_back = (datetime.now(self.tz).date() - datetime.strptime(state_date, "%Y-%m-%d").date()).days
            if days_back > 3:  # More than a long weekend — stale
                log.info(f"Overnight state from {state_date} is stale ({days_back} days old) — ignoring")
                return None
            return state
        except Exception as e:
            log.warning(f"Failed to load overnight state: {e}")
            return None

    def _clear_overnight_state(self):
        """Remove overnight state file after positions are synced."""
        try:
            if os.path.exists(self._overnight_state_file):
                os.remove(self._overnight_state_file)
        except Exception:
            pass

    def _end_of_day(self):
        """End of day routine with smart position evaluation.

        For each position, evaluates bullish/bearish technical factors to decide:
        - Close at EOD (bearish, scalps, losers)
        - Hold into after-hours with tightened stops (bullish runners)
        - Hold overnight (strong multi-day plays)

        After-hours selling uses IBKR outside-RTH limit orders.
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
        max_overnight = overnight_cfg.get("max_overnight_positions", 0)

        # Trend rider positions have their own overnight bucket — they're
        # DESIGNED to hold overnight and bypass the intraday overnight cap.
        trend_rider_cfg = self.config.get_strategy_config("daily_trend_rider")
        max_trend_riders = trend_rider_cfg.get("max_positions", 3)

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

            # Count existing trend rider holds separately
            trend_rider_holds = 0

            for symbol, pos in sorted_positions:
                pnl_pct = pos.get("unrealized_pnl_pct", 0)
                in_profit = pnl_pct > 0

                # NEVER hold stock split candidates overnight
                if symbol in split_candidates:
                    log.warning(f"SPLIT BLOCK: Closing {symbol} - split candidate, no overnight hold")
                    positions_to_close.append(symbol)
                    continue

                # NEVER hold through earnings — gap risk is extreme
                if getattr(self, "polygon", None) and self.polygon.enabled:
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

                # TREND RIDER: holds overnight by design (own bucket, not counted
                # against intraday max_overnight). Check daily-bar exit conditions
                # instead of intraday bullish score.
                if pos.get("trend_rider") or pos.get("strategy") == "daily_trend_rider":
                    if trend_rider_holds >= max_trend_riders:
                        positions_to_close.append(symbol)
                        log.info(
                            f"TREND RIDER EOD CLOSE (at capacity): {symbol} | "
                            f"P&L: {pnl_pct:.1%} | {trend_rider_holds}/{max_trend_riders} riders held"
                        )
                        continue

                    # Daily-close exit: check if today's close broke SuperTrend or 20 EMA
                    should_exit, exit_reason = self._check_trend_rider_daily_exit(symbol, pos)
                    if should_exit:
                        positions_to_close.append(symbol)
                        log.info(
                            f"TREND RIDER DAILY EXIT: {symbol} | P&L: {pnl_pct:.1%} | "
                            f"{exit_reason}"
                        )
                        continue

                    # Hold overnight with a daily-ATR trailing stop
                    daily_atr = pos.get("_daily_atr", 0)
                    if daily_atr > 0:
                        current_price = pos.get("current_price", pos["entry_price"])
                        atr_stop = current_price - (daily_atr * 1.5)
                        if atr_stop > pos.get("stop_loss", 0):
                            pos["stop_loss"] = round(atr_stop, 2)
                    pos["overnight_hold"] = True
                    trend_rider_holds += 1
                    overnight_holds.append(symbol)
                    log.info(
                        f"TREND RIDER OVERNIGHT: {symbol} | P&L: {pnl_pct:.1%} | "
                        f"Green days: {pos.get('_daily_green_days', '?')} | "
                        f"Stop: ${pos.get('stop_loss', 0):.2f} | "
                        f"Rider {trend_rider_holds}/{max_trend_riders}"
                    )
                    continue

                # Crypto positions trade 24/7 - skip EOD close entirely
                if self._is_crypto_symbol(symbol):
                    log.info(f"CRYPTO HOLD: {symbol} trades 24/7 - skipping EOD close | P&L: {pnl_pct:.1%}")
                    overnight_holds.append(symbol)
                    continue

                # --- MINIMUM HOLD TIME CHECK ---
                # Never hold overnight a position entered in the last 30 min.
                # These have no price history to evaluate and are pure overnight gap gambles.
                entry_time = pos.get("entry_time")
                if entry_time:
                    held_minutes = (datetime.now(self.tz) - entry_time).total_seconds() / 60
                    if held_minutes < 30:
                        positions_to_close.append(symbol)
                        log.info(
                            f"EOD CLOSE (too new): {symbol} | P&L: {pnl_pct:.1%} | "
                            f"Held only {held_minutes:.0f}min — need 30+ min for overnight"
                        )
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
                    is_sector_heat = pos.get("sector_heat", False)
                    is_multi_day = (
                        pos.get("strategy") in ("momentum", "prebreakout", "smc_forever")
                        or is_sector_heat  # Macro-driven sector plays are multi-day by nature
                    )

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
                    elif is_momentum and bullish_score >= 60:
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
                    elif bullish_score >= 60:
                        # Other strategy with strong bullish score
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

            # Place server-side stop orders at IBKR for overnight holds
            # These protect against gap-downs even if the bot is offline
            all_holds = overnight_holds + afterhours_holds
            if all_holds and self.broker and self.broker.is_connected():
                for symbol in all_holds:
                    pos = self.positions.get(symbol)
                    if not pos or not pos.get("stop_loss"):
                        continue
                    stop_price = pos["stop_loss"]
                    qty = pos.get("quantity", 0)
                    if qty <= 0:
                        continue
                    try:
                        # Cancel any existing orders for this symbol first
                        self.broker.cancel_symbol_orders(symbol, side="SELL")
                        # Place GTC stop order at broker (survives bot restarts)
                        result = self.broker.place_order(
                            symbol=symbol,
                            action="SELL",
                            quantity=qty,
                            order_type="STOP",
                            stop_price=stop_price,
                            outside_rth=True,
                        )
                        if result:
                            pos["broker_stop_order_id"] = result.get("order_id")
                            log.info(
                                f"BROKER STOP PLACED: {symbol} stop=${stop_price:.2f} "
                                f"qty={qty} order_id={result.get('order_id')}"
                            )
                        else:
                            log.warning(f"Failed to place broker stop for {symbol}")
                    except Exception as e:
                        log.error(f"Error placing broker stop for {symbol}: {e}")

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

            # Persist overnight state to disk so restarts respect the hold list
            if overnight_holds or afterhours_holds:
                self._save_overnight_state(overnight_holds, afterhours_holds)

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
                significant_shifts = []
                for name, strategy in self.strategies.items():
                    new_alloc = alloc.get(name, 0.25)
                    old_capital = getattr(strategy, "allocated_capital", 0)
                    new_capital = self.current_balance * new_alloc
                    strategy.update_capital(new_capital)
                    # Detect ≥3% shifts (in $ terms vs old allocation)
                    if old_capital > 0:
                        shift_pct = abs(new_capital - old_capital) / old_capital
                        if shift_pct >= 0.20:  # 20% relative change in capital
                            significant_shifts.append((name, old_capital, new_capital))

                log.info(f"Auto-Tune applied {result['total_changes']} changes - strategies reloaded")

                # Notify user when allocations meaningfully shift — they care
                # about "the bot is putting more money into X" because it's
                # the most impactful kind of change auto-tuner can make.
                if significant_shifts:
                    lines = "\n".join(
                        f"  • {name}: ${old:,.0f} → ${new:,.0f} ({(new-old)/old*100:+.0f}%)"
                        for name, old, new in significant_shifts
                    )
                    self.notifier.system_alert(
                        f"AUTO-TUNE allocation shift:\n{lines}",
                        level="info",
                    )
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
        elif getattr(self, "polygon", None) and self.polygon.enabled:
            source = "Polygon.io"
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
            # --- IBKR SCANNER (Primary — real-time data) ---
            # Professional architecture: IBKR real-time scanners are the sole
            # discovery source. No Polygon (delayed), no Alpaca.
            _ibkr_available = self.broker and hasattr(self.broker, 'scan_market') and self.broker.is_connected()
            _pm_flag = getattr(self, '_in_premarket', False)
            log.debug(f"Discovery scan: ibkr={'connected' if _ibkr_available else 'disconnected'}, premarket={_pm_flag}")

            if _ibkr_available:
                # Rate limit IBKR scanner: once per 30 seconds (scanner is heavier than snapshot)
                _now = time.time()
                _last_ibkr_scan = getattr(self, '_last_ibkr_scan_time', 0)
                if _now - _last_ibkr_scan >= 30:
                    self._last_ibkr_scan_time = _now

                    # Run multiple scan types for comprehensive discovery
                    ibkr_gainers = self.broker.scan_premarket_gainers(num_rows=50)
                    ibkr_active = self.broker.scan_most_active(num_rows=30)
                    ibkr_hot = self.broker.scan_hot_by_volume(num_rows=30)
                    ibkr_gaps = self.broker.scan_high_gap(num_rows=30)
                    ibkr_losers = self.broker.scan_premarket_losers(num_rows=20)

                    # Collect all unique symbols
                    _ibkr_all_syms = set()
                    _ibkr_gainer_syms = []
                    _ibkr_loser_syms = []
                    _ibkr_gap_syms = []

                    for g in ibkr_gainers:
                        sym = g.get("symbol", "")
                        if sym and sym not in _ibkr_all_syms:
                            _ibkr_gainer_syms.append(sym)
                            _ibkr_all_syms.add(sym)

                    for a in ibkr_active + ibkr_hot:
                        sym = a.get("symbol", "")
                        if sym:
                            _ibkr_all_syms.add(sym)

                    for gap in ibkr_gaps:
                        sym = gap.get("symbol", "")
                        if sym and sym not in _ibkr_all_syms:
                            _ibkr_gap_syms.append(sym)
                            _ibkr_all_syms.add(sym)

                    for l in ibkr_losers:
                        sym = l.get("symbol", "")
                        if sym:
                            _ibkr_loser_syms.append(sym)
                            _ibkr_all_syms.add(sym)

                    _ibkr_all_list = list(_ibkr_all_syms)

                    # Price-ceiling filter: drop symbols above scanner_max_price
                    # (default $500) so strategies don't waste cycles on stocks
                    # the bot can't buy anyway. Capital-inefficient for small
                    # accounts. Symbols whose live price isn't known yet are
                    # allowed through — they'll get caught at execute time.
                    _max_px = self.config.settings.get("risk", {}).get("scanner_max_price", 500.0)

                    def _filter_by_price(syms):
                        kept = []
                        dropped = []
                        for s in syms:
                            px = self.market_data.get_price(s) if self.market_data else None
                            if px is not None and px > _max_px:
                                dropped.append((s, px))
                            else:
                                kept.append(s)
                        return kept, dropped

                    _ibkr_gainer_syms, _g_dropped = _filter_by_price(_ibkr_gainer_syms)
                    _ibkr_loser_syms, _l_dropped = _filter_by_price(_ibkr_loser_syms)
                    _ibkr_gap_syms, _gp_dropped = _filter_by_price(_ibkr_gap_syms)
                    _ibkr_all_list, _all_dropped = _filter_by_price(_ibkr_all_list)
                    _all_dropped_symbols = {s for s, _ in (_g_dropped + _l_dropped + _gp_dropped + _all_dropped)}
                    if _all_dropped_symbols:
                        _sample = sorted(
                            {(s, px) for s, px in (_g_dropped + _l_dropped + _gp_dropped + _all_dropped)},
                            key=lambda sp: -sp[1],
                        )[:5]
                        _sample_str = ", ".join(f"{s}=${px:.0f}" for s, px in _sample)
                        log.info(
                            f"PRICE CEILING: dropped {len(_all_dropped_symbols)} scanner hits "
                            f"above ${_max_px:.0f} ({_sample_str})"
                        )

                    # Feed gainers + active into momentum strategies
                    if _ibkr_gainer_syms:
                        if rvol_strat:
                            rvol_strat.add_dynamic_symbols(_ibkr_gainer_syms)
                        if scalp_strat:
                            scalp_strat.add_dynamic_symbols(_ibkr_gainer_syms)
                        if runner_strat:
                            runner_strat.add_dynamic_symbols(_ibkr_gainer_syms)
                        if pb_strat:
                            pb_strat.add_dynamic_symbols(_ibkr_gainer_syms)
                        if squeeze_strat:
                            squeeze_strat.add_dynamic_symbols(_ibkr_gainer_syms)
                        if pead_strat:
                            pead_strat.add_dynamic_symbols(_ibkr_gainer_syms)

                    # Feed gap-ups into gap strategy
                    if _ibkr_gap_syms and gap_strat:
                        gap_strat.add_dynamic_symbols(_ibkr_gap_syms)

                    # Feed losers into mean reversion
                    if _ibkr_loser_syms and mr_strat:
                        existing = set(mr_strat.symbols)
                        new_losers = [s for s in _ibkr_loser_syms if s not in existing]
                        mr_strat.symbols.extend(new_losers)
                        if scalp_strat:
                            scalp_strat.add_dynamic_symbols(_ibkr_loser_syms)

                    # Feed all active symbols into scalp (broad net)
                    if _ibkr_all_list and scalp_strat:
                        scalp_strat.add_dynamic_symbols(_ibkr_all_list)

                    log.info(
                        f"IBKR scanner: {len(_ibkr_all_syms)} unique symbols discovered | "
                        f"{len(_ibkr_gainer_syms)} gainers, {len(_ibkr_gap_syms)} gaps, "
                        f"{len(_ibkr_loser_syms)} losers"
                    )

            # --- Polygon REMOVED (Professional Architecture) ---
            # IBKR scanners above are the sole discovery source.
            # Polygon was delayed data — removed from execution chain.
            if False:  # Polygon code preserved but disabled
                # During premarket, volume is thin (1K-20K typical) — lower threshold
                # so the scanner actually finds movers instead of filtering everything out
                if getattr(self, '_in_premarket', False):
                    poly_movers, poly_runners, poly_gap_ups = self.polygon.scan_full_market(
                        min_volume=5000, min_change_pct=1.5
                    )
                else:
                    poly_movers, poly_runners, poly_gap_ups = self.polygon.scan_full_market()

                if not poly_movers and not poly_runners and not poly_gap_ups:
                    log.info("Polygon scan returned 0 movers, 0 runners, 0 gap-ups — check API tier/key")

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
                        log.info(f"Polygon: fed {len(snapshot_entries)} snapshot entries to RVOL fast path")

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

                        log.info(
                            f"Polygon: fed {len(session_candidates) if session_candidates else 0} "
                            f"session candidates + {len(poly_mover_syms)} movers into momentum_runner"
                        )

                    log.info(f"Polygon: injected {len(poly_mover_syms)} movers, {len(poly_scalp_syms)} scalp candidates")

                if poly_gap_ups and gap_strat:
                    gap_syms = [g["symbol"] for g in poly_gap_ups if g.get("symbol")]
                    gap_strat.add_dynamic_symbols(gap_syms)
                    log.info(f"Polygon: injected {len(gap_syms)} gap-ups into pre-market gap")

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
                    log.info(f"Polygon: fed {len(poly_gap_ups)} gap-ups into PEAD strategy")

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
                        log.info(f"Polygon: injected {len(runner_syms)} runners into all strategies")

                # --- Top Gainers Scanner (no price cap, all sessions) ---
                # Catches big movers that scan_full_market misses due to $100 cap.
                # Uses the already-cached price data — no extra API calls.
                if hasattr(self.polygon, 'scan_top_gainers'):
                    now_et = datetime.now(self.tz)
                    _h2, _m2 = now_et.hour, now_et.minute
                    _tv2 = _h2 * 100 + _m2
                    if _tv2 < 930:
                        _gainer_session = "premarket"
                    elif _tv2 < 1600:
                        _gainer_session = "regular"
                    else:
                        _gainer_session = "postmarket"

                    # Load config overrides from settings.yaml if available
                    _tg_config = None
                    if hasattr(self, 'config') and hasattr(self.config, 'settings'):
                        _tg_config = self.config.settings.get("top_gainers")
                    _tg_limit = _tg_config.get("limit", 50) if _tg_config else 50
                    _tg_enabled = _tg_config.get("enabled", True) if _tg_config else True

                    if not _tg_enabled:
                        top_gainers = []
                    else:
                        top_gainers = self.polygon.scan_top_gainers(
                            session=_gainer_session, limit=_tg_limit, config=_tg_config
                        )
                    if top_gainers:
                        gainer_syms = []
                        gainer_snapshots = []
                        for g in top_gainers:
                            sym = g.get("symbol", "")
                            if not sym:
                                continue
                            if self._is_crypto_symbol(sym) and not self._is_crypto_enabled():
                                continue
                            gainer_syms.append(sym)
                            gainer_snapshots.append(g)

                        if gainer_syms:
                            # Feed into ALL scanning strategies — these are the day's biggest movers
                            if rvol_strat:
                                rvol_strat.add_dynamic_symbols(gainer_syms)
                                if hasattr(rvol_strat, "feed_snapshot_data"):
                                    rvol_strat.feed_snapshot_data(gainer_snapshots)
                            if scalp_strat:
                                scalp_strat.add_dynamic_symbols(gainer_syms)
                            if pb_strat:
                                pb_strat.add_dynamic_symbols(gainer_syms)
                            if gap_strat:
                                gap_strat.add_dynamic_symbols(gainer_syms)
                            if squeeze_strat:
                                squeeze_strat.add_dynamic_symbols(gainer_syms)
                            if pead_strat:
                                pead_strat.add_dynamic_symbols(gainer_syms)
                            if runner_strat:
                                runner_strat.add_dynamic_symbols(gainer_syms)
                                runner_strat.feed_snapshot_data(gainer_snapshots)
                            if mr_strat:
                                # Top gainers that have pulled back could be mean reversion
                                for g in top_gainers:
                                    if g.get("change_pct", 0) <= -3.0:
                                        s = g.get("symbol", "")
                                        if s and s not in mr_strat.symbols:
                                            mr_strat.symbols.append(s)

                            log.info(
                                f"Top gainers: injected {len(gainer_syms)} stocks (no price cap) "
                                f"into all strategies [{_gainer_session}]"
                            )

            # Get top movers from Polygon (filtered to $0.50-$500 range)
            movers = self.get_top_movers()
            if movers:
                mover_symbols = []
                scalp_symbols = []
                for m in movers:
                    sym = m.get("symbol", "")
                    price = m.get("price", 0)
                    change_pct = m.get("change_pct", 0)
                    rvol = m.get("rvol", 0)

                    _mover_max = self.config.settings.get("risk", {}).get("scanner_max_price", 500.0)
                    if not sym or price < 0.50 or price > _mover_max:
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

            # --- EARLY BIRD SCANNER: Catch accumulation BEFORE the breakout ---
            # Detects volume ramping while price stays flat — smart money loading
            # before retail scanners catch the move. Feeds into prebreakout strategy.
            if self.polygon and hasattr(self.polygon, 'scan_early_birds'):
                early_birds = self.polygon.scan_early_birds(limit=15)
                if early_birds:
                    eb_syms = [e["symbol"] for e in early_birds if e.get("symbol")]
                    if eb_syms and pb_strat:
                        pb_strat.add_dynamic_symbols(eb_syms)
                    # Also feed into RVOL and runner — if they break out, these catch it
                    if eb_syms and rvol_strat:
                        rvol_strat.add_dynamic_symbols(eb_syms)
                    if eb_syms and runner_strat:
                        runner_strat.add_dynamic_symbols(eb_syms)
                    eb_top = ", ".join(e["symbol"] + "(" + str(e["score"]) + ")" for e in early_birds[:3])
                    log.info(
                        f"EARLY BIRD: fed {len(eb_syms)} accumulation candidates into "
                        f"prebreakout + rvol + runner | Top: {eb_top}"
                    )

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
                    log.info(f"Injected {len(runner_symbols)} runners into all strategies")

        except Exception as e:
            log.error(f"Dynamic discovery error: {e}", exc_info=True)

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
        if getattr(self, "polygon", None) and self.polygon.enabled:
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
        if getattr(self, "polygon", None) and self.polygon.enabled:
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
