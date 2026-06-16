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
from pathlib import Path
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
from bot.utils.market_calendar import is_us_market_holiday
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
from bot.strategies.low_float_catalyst import LowFloatCatalystStrategy
from bot.strategies.crypto_runner import CryptoRunnerStrategy
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
        self.tp_mirror = None
        self.politician_tracker = None
        self.news_feed = None
        self.trade_analyzer = None
        self.auto_tuner = None
        self.regime_detector = None
        self.hedging_manager = None
        self.sheets_logger = None
        self.scheduler = None

        # Trail-arm thresholds overridable via risk config so tuning doesn't
        # require a code change. Defaults live on the class constants below.
        if config is not None:
            rc = getattr(config, "risk_config", None) or {}
            self.CRYPTO_TRAIL_ARM_PCT = float(rc.get(
                "crypto_trail_arm_pct", self.__class__.CRYPTO_TRAIL_ARM_PCT
            ))
            self.MOMENTUM_TRAIL_ARM_PCT = float(rc.get(
                "momentum_trail_arm_pct", self.__class__.MOMENTUM_TRAIL_ARM_PCT
            ))

        # Gateway auto-recovery state. After N consecutive reconnect
        # failures we try restarting the ib-gateway container via the
        # Docker socket, bounded by per-day + cooldown caps so a bad
        # credential or IBKR outage doesn't have us restart-looping.
        #
        # CRITICAL: this state must survive bot restarts. Restarting
        # ib-gateway tears down the shared network namespace, which kills
        # the trading-bot container too (network_mode: service:ib-gateway).
        # Docker brings the bot back up — but with in-memory counters the
        # 3/day cap resets and the bot ramps up another restart cycle
        # forever. State is loaded from disk on init and persisted after
        # every change.
        self._auto_recovery_state_file = Path("data/auto_recovery_state.json")
        self._auto_restart_count = 0
        self._auto_restart_day = None   # date() of the day we're counting
        self._last_auto_restart_ts = 0.0
        self._load_auto_recovery_state()

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

        # Gate-hit telemetry: counts per gate, per symbol, per day. Lets the
        # user / Claude see which defenses actually fire and which symbols
        # trigger them most. Reset at _pre_market_scan. Exposed via /api/status.
        from collections import defaultdict
        self._gate_hits = defaultdict(lambda: defaultdict(int))  # {gate_name: {symbol: count}}
        self._gate_hits_total = defaultdict(int)  # {gate_name: total_today}
        self._gate_recent = []  # last 50 (gate, symbol, reason, ts) for the dashboard tail
        self._pending_orders = set()  # Symbols with orders currently in-flight

        # Exit cooldown - prevent re-closing recently closed positions
        # Tracks {symbol: close_datetime} to block re-entry via broker sync
        self._recently_closed = {}  # {symbol: datetime when closed}
        self._exit_cooldown_secs = 300  # 5 minutes: don't re-add/re-close within this window

        # Equity re-entry guard — tracks the LAST close per symbol with its
        # reason + P&L so a momentum BUY can't immediately re-fire a name that
        # just lost money. {symbol: {"time": dt, "reason": str, "pnl": float}}.
        # Cleared each session in _pre_market_scan. See the duplicate-entry
        # guard for how slippage_reject vs ordinary losses are treated.
        self._recent_close_info = {}
        self._equity_loss_cooldown_secs = 1800  # 30 min after an ordinary losing close

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

        # Start IBKR real-time streaming if connected. Held positions go FIRST
        # so they always make the 95-line cap — without this, on a busy
        # restart the scanner trim could leave a held symbol unmonitored
        # (no live price → no trailing stop → no exits).
        if self.broker and self.broker.is_connected():
            held = list(self.positions.keys())
            watch = [s for s in set(self.watchlist) if s not in self.positions]
            all_symbols = held + watch
            self.market_data.start_streaming(all_symbols)
            log.info(
                f"IBKR real-time streaming initialized "
                f"({len(held)} held + {len(watch)} watchlist)"
            )

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

        # TradersPost CRYPTO broker — separate execution path for crypto.
        # IBKR's crypto coverage is limited (PAXOS-routed BTC/ETH/LTC/BCH) and
        # paper-mode crypto isn't reliable, so we keep crypto execution off
        # IBKR and route it via TradersPost's crypto subscription. This is a
        # separate broker instance from `tp_broker` so equities stay
        # IBKR-direct unchanged. Symbol-class routing happens in the engine's
        # execution paths; this broker only ever sees crypto symbols.
        crypto_url = getattr(self.config, 'traderspost_webhook_url_crypto', '') or ''
        self.tp_crypto_broker = None
        if crypto_url:
            # min_interval=0: crypto bursts (3-5 simultaneous approvals from
            # the fast lane) should all fire. The per-symbol cap (3/min)
            # still guards against runaway loops; the 3s global cooldown
            # was a default-safe value for the equity webhook and isn't
            # needed on the dedicated crypto webhook.
            self.tp_crypto_broker = TradersPostBroker(
                self.config,
                webhook_url_override=crypto_url,
                min_interval_override=0,
            )
            log.info(
                f"TradersPost CRYPTO broker enabled — crypto signals route "
                f"to ...{crypto_url[-20:]}"
            )

        # TradersPost MIRROR (visualization only — never executes).
        # Sends a copy of every IBKR fill (entries + closes) to a separate
        # TradersPost webhook so the trades show up in TradersPost's UI.
        # Independent of tp_broker; the mirror endpoint should point at a
        # TradersPost subscription using its built-in Paper Trading broker.
        mirror_url = self.config.traderspost_mirror_webhook_url
        if mirror_url:
            self.tp_mirror = TradersPostBroker(
                self.config, webhook_url_override=mirror_url
            )
            log.info(
                f"TradersPost MIRROR enabled — IBKR fills will be mirrored "
                f"to ...{mirror_url[-20:]}"
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
            # One-shot legacy-dup cleanup. FBYD (session 9) and similar
            # duplicated 2-3× in trade_history due to a close-path race;
            # any leftover dups on disk inflate every downstream stat.
            if hasattr(self.trade_analyzer, "dedupe_persisted_trades"):
                try:
                    self.trade_analyzer.dedupe_persisted_trades()
                except Exception as e:
                    log.debug(f"Boot dup cleanup error: {e}")
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
                # Rebuild in-memory performance_stats from the restored
                # history. Without this, the dashboard's Win Rate / PF
                # cards render "--" until the first post-restart close,
                # because `performance_stats` is only updated by the
                # close path and isn't itself persisted. Reading from
                # trade_history matches the same source the analytics
                # endpoints already use.
                try:
                    self._rebuild_performance_stats_from_history()
                except Exception as e:
                    log.debug(f"perf_stats rebuild failed: {e}")
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

        # Wave 5: register the wedge callback so a hung ib_async worker
        # (DELL incident pattern) triggers the same auto-recovery the
        # reconnect loop uses. is_connected() returns True while the
        # worker is wedged, so the reconnect path never sees the failure;
        # this is the only place that catches it.
        if hasattr(self.broker, "on_wedge"):
            self.broker.on_wedge(self._handle_ibkr_wedge)

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
            # Sync existing positions. IBKR's positions() list is populated
            # asynchronously after connect — a single call right after
            # connection routinely returns empty or partial. On 2026-05-21
            # this left PLTR/RIOT/FBYD as unmanaged orphans: boot logged
            # "Synced 0 LONG positions" while the broker held three longs,
            # and nothing monitored their stops until a fresh signal happened
            # to re-trigger the per-symbol duplicate-block resync.
            # Poll until the list settles (two identical non-empty reads) or
            # the budget runs out. A genuinely flat account simply polls out
            # the full budget once at boot (~10s) and is treated as flat.
            import time as _sync_time
            raw_positions = {}
            _prev_keys = None
            for _attempt in range(1, 7):
                raw_positions = self.broker.get_positions() or {}
                _keys = frozenset(raw_positions)
                if raw_positions and _keys == _prev_keys:
                    log.info(
                        f"IBKR SYNC: position list settled on poll {_attempt} "
                        f"— {len(raw_positions)} position(s): {sorted(_keys)}"
                    )
                    break
                _prev_keys = _keys
                if _attempt < 6:
                    _sync_time.sleep(2)
            else:
                if raw_positions:
                    log.warning(
                        f"IBKR SYNC: position list did not stabilise after 6 "
                        f"polls — proceeding with last read "
                        f"({len(raw_positions)}): {sorted(raw_positions)}"
                    )
                else:
                    log.info(
                        "IBKR SYNC: no open positions at IBKR after 6 polls "
                        "— treating account as flat."
                    )
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
                # positions that are in the process of being closed.
                # Stale orders (pending > N hours) are auto-cancelled here —
                # without that, the DELL incident pattern repeats: a stale
                # bracket SELL leg at an out-of-market limit sits "Submitted"
                # for hours/days while IBKR's price cap keeps it from filling,
                # and the sync silently skips the symbol every restart. By
                # the time the user notices, the position has either ridden
                # a runup (paper-account double-counting) or bled a dump.
                pending_sell_symbols = set()
                stale_pending_max_hours = float(
                    self.config.risk_config.get("stale_pending_sell_max_hours", 2.0)
                )
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    now_utc = _dt.now(_tz.utc)
                    open_trades = self.broker.ib.openTrades()
                    for t in open_trades:
                        if (t.order.action.upper() == "SELL" and
                                t.orderStatus.status in ("PreSubmitted", "Submitted")):
                            sym = t.contract.symbol
                            # Check age — older than threshold = cancel.
                            age_hours = None
                            try:
                                if t.log:
                                    placed_at = t.log[0].time
                                    if placed_at.tzinfo is None:
                                        placed_at = placed_at.replace(tzinfo=_tz.utc)
                                    age_hours = (now_utc - placed_at).total_seconds() / 3600
                            except Exception as e:
                                log.debug(f"Could not read order age for {sym}: {e}")
                            if age_hours is not None and age_hours > stale_pending_max_hours:
                                try:
                                    self.broker.cancel_order(t.order.orderId)
                                    log.warning(
                                        f"STALE PENDING SELL CANCELLED: {sym} order "
                                        f"{t.order.orderId} pending {age_hours:.1f}h "
                                        f"(threshold {stale_pending_max_hours}h) — "
                                        f"sync will resume normal tracking next cycle"
                                    )
                                    # Don't add to skip set — let sync proceed.
                                    continue
                                except Exception as e:
                                    log.error(
                                        f"Failed to cancel stale order for {sym}: {e}"
                                    )
                            pending_sell_symbols.add(sym)
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
                            if self.tp_broker:
                                # TradersPost-primary: IBKR is data-only, the
                                # bot does not place orders at IBKR. Cover manually.
                                log.warning(
                                    f"SHORT {sym} at IBKR NOT auto-covered — "
                                    f"TradersPost-primary, IBKR is data-only. "
                                    f"Cover this position manually."
                                )
                            else:
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

                    # SYNC fake-stop guard: if current price is already at/below
                    # the intended stop, priming stop_loss here fires the exit
                    # on the very next monitor tick and locks in a bigger loss
                    # than stop_pct intended. Use a recovery stop at
                    # current_price*(1-stop_pct) and arm the real stop once
                    # price climbs back above it. Same hazard as the SHOP exit.
                    sync_stop = use_stop
                    sync_stop_armed = True
                    sync_stop_target = use_stop
                    try:
                        cur = self.market_data.get_price(sym) if self.market_data else None
                    except Exception:
                        cur = None
                    if cur is not None and cur <= use_stop and not is_overnight:
                        sync_stop = cur * (1 - stop_pct)
                        sync_stop_armed = False
                        log.warning(
                            f"IBKR SYNC FAKE-STOP GUARD: {sym} entry ${entry:.2f}, "
                            f"current ${cur:.2f} already at/below intended stop "
                            f"${use_stop:.2f}. Using recovery stop ${sync_stop:.2f}; "
                            f"intended stop arms once price climbs above ${use_stop:.2f}."
                        )

                    self.positions[sym] = {
                        **pos,
                        "entry_time": now,
                        "stop_loss": sync_stop,
                        "take_profit": use_tp,
                        "trailing_stop_pct": self.config.risk_config.get("trailing_stop_pct", 0.02),
                        "strategy": use_strategy,
                        "executed_via": pos.get("executed_via", "IBKR"),
                        "overnight_hold": is_overnight,
                        "sync_flagged": sync_flagged,
                        "max_hold_bars": 40,
                        "bar_seconds": 300,
                        "max_hold_days": 5,
                        "_entry_stop_armed": sync_stop_armed,
                        "_entry_stop_target": sync_stop_target,
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
                        # Crypto path: TradersPost has no positions API, so
                        # IBKR's broker-sync never sees crypto holdings. Trust
                        # the persisted state and re-insert. The orphan
                        # reconciliation walk below catches the inverse case
                        # (broker holds it, persisted state doesn't).
                        if self._is_crypto_symbol(symbol):
                            # GHOST CHECK: Verify the position is still open per
                            # signal_log. The persistence path can fail to write
                            # a removal (saw this 2026-05-18: SUI closed +$8.65
                            # at 04:40:59 EDT, restart at 04:45:14 EDT restored
                            # the pre-close state because positions_state.json
                            # hadn't updated yet). Walk signal_log from this
                            # position's entry_time forward; if net qty <= 0,
                            # the position was actually closed — skip restore.
                            entry_time = saved_pos.get("entry_time")
                            if isinstance(entry_time, str):
                                try:
                                    entry_time = datetime.fromisoformat(entry_time)
                                except Exception:
                                    entry_time = None
                            try:
                                signal_net = self._signal_log_net_qty(
                                    symbol, since_dt=entry_time
                                )
                            except Exception as e:
                                log.debug(f"signal_log ghost check failed for {symbol}: {e}")
                                signal_net = None
                            persisted_qty = float(saved_pos.get("quantity", 0) or 0)
                            if (signal_net is not None
                                    and persisted_qty > 0
                                    and signal_net <= 0.01):
                                log.warning(
                                    f"GHOST CRYPTO POSITION SKIPPED: {symbol} "
                                    f"persisted qty={persisted_qty:.4f} but "
                                    f"signal_log net since entry={signal_net:.4f} "
                                    f"— position was closed before restart, not "
                                    f"restoring"
                                )
                                continue
                            self.positions[symbol] = saved_pos
                            log.info(
                                f"Restored persisted CRYPTO position {symbol} "
                                f"qty={saved_pos.get('quantity')} entry="
                                f"${saved_pos.get('entry_price')} (TP has no "
                                f"positions API; trusting persisted state)"
                            )
                        else:
                            # Equity: position in saved state but not at IBKR —
                            # may have been closed while bot was down. Skip.
                            log.info(
                                f"Persisted position {symbol} not found at broker — "
                                f"likely closed while bot was offline. Skipping."
                            )

        # STARTUP TRAIL MIGRATION: positions persisted before 18ae5f2 have
        # `trailing_stop` set below entry by the old code. UNSET the stale
        # trail — raising it to entry_price when current_price < entry_price
        # would instantly trigger the exit gate at the next tick (saw this
        # fire 3 unnecessary exits on 2026-05-18). With trail=0, the new
        # code's natural ratchet will install a proper trail once price
        # moves above entry. Hard stop_loss remains as downside protection.
        with self._positions_lock:
            for symbol, pos in self.positions.items():
                if pos.get("direction", "long") != "long":
                    continue
                entry_price = pos.get("entry_price", 0)
                trail = pos.get("trailing_stop", 0)
                if (entry_price > 0 and trail > 0
                        and trail < entry_price
                        and not pos.get("_trail_migrated")):
                    pos["trailing_stop"] = 0
                    pos["_trail_migrated"] = True
                    log.info(
                        f"TRAIL MIGRATION (startup): {symbol} stale trail "
                        f"${trail:.4f} unset (entry ${entry_price:.4f}, "
                        f"will re-arm on first ratchet above entry)"
                    )

        # Crypto reconciliation: TradersPost crypto subscriptions don't expose
        # positions via API, so the bot can't broker-sync them like it does
        # equity at IBKR. Walk signal_log.json instead — sum buy webhooks vs
        # exit webhooks per crypto symbol over the last 48h to derive what
        # the bot believes is open broker-side. Any symbol with net open qty
        # that isn't in the engine's positions is an orphan from a previous
        # session and gets a WARNING + Discord risk_alert so the operator
        # can decide to manually close.
        try:
            self._reconcile_crypto_orphans()
        except Exception as e:
            log.debug(f"crypto orphan reconciliation failed: {e}")

        # Mirror reconciliation: the TradersPost MIRROR webhook is a one-way
        # copy of every IBKR equity fill with no positions API of its own.
        # When a mirrored exit doesn't actually flatten the connected broker,
        # the mirror account drifts long. Same signal_log walk as crypto, but
        # for equity symbols. Alert-only at boot — closing is opt-in via the
        # /api/reconcile/mirror/run endpoint so an approximate tripwire never
        # fires an unattended exit.
        try:
            self._reconcile_mirror_orphans()
        except Exception as e:
            log.debug(f"mirror orphan reconciliation failed: {e}")

    def _signal_log_net_qty(self, symbol, since_dt=None, lookback_hours=48):
        """Compute net qty (buys - exits) for a crypto symbol from signal_log.

        Used by `_load_persisted_positions` to detect ghost positions (engine
        thinks it owns X but signal_log shows the position was closed). Returns
        a float clamped at 0 (spot can't be short; negative net just means
        signal_log over-records exits when TP returns 200 on rejected closes).

        Args:
            symbol: ticker like "SUI-USD"
            since_dt: datetime to start the walk from. If None, walks the last
                `lookback_hours`. Pass the persisted position's entry_time to
                check if the position has been closed since it opened.
            lookback_hours: max lookback when since_dt is None.

        Returns:
            float: net qty since since_dt (or last lookback_hours), clamped to 0.
        """
        import json as _json
        from datetime import timedelta as _td, timezone as _tz
        signal_file = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "data", "signal_log.json"
        )
        if not os.path.exists(signal_file):
            return 0.0
        try:
            with open(signal_file, "r") as f:
                sigs = _json.load(f)
        except Exception:
            return 0.0
        if since_dt is None:
            since_dt = datetime.now(_tz.utc) - _td(hours=lookback_hours)
        elif since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=_tz.utc)
        else:
            since_dt = since_dt.astimezone(_tz.utc)
        # Grace window for the order-completion race: the signal_log entry
        # is written when TradersPost returns HTTP 200, but the engine
        # records `entry_time` a few ms later when it materializes the
        # position dict. Without this slack, the at-entry buy lands ~7ms
        # before entry_time and gets filtered out — exact failure mode
        # behind the 2026-05-18 ATOM/ICP/RNDR ghost flagging once the tz
        # bug above was fixed.
        since_dt = since_dt - _td(seconds=5)

        net = 0.0
        for s in sigs:
            if s.get("symbol") != symbol:
                continue
            if not s.get("success") or int(s.get("status_code", 0)) >= 300:
                continue
            try:
                t = datetime.fromisoformat(s.get("time", ""))
                if t.tzinfo is None:
                    # Legacy entries (pre-fix) used datetime.now().isoformat()
                    # — naive *local* time, NOT UTC. Mislabeling them as UTC
                    # shifted them by self.tz's offset, which on EDT caused
                    # legitimate just-opened positions to fail the
                    # `t < since_dt` filter and be flagged as ghosts
                    # (observed 2026-05-18 with ATOM/ICP/RNDR).
                    t = self.tz.localize(t).astimezone(_tz.utc)
                else:
                    t = t.astimezone(_tz.utc)
            except Exception:
                continue
            if t < since_dt:
                continue
            qty = float(s.get("quantity", 0) or 0)
            action = (s.get("tp_action", "") or "").lower()
            if action == "buy":
                net += qty
            elif action == "exit":
                net -= qty
        return max(0.0, net)

    def _reconcile_crypto_orphans(self, lookback_hours=48):
        """Detect crypto positions on TradersPost that the engine doesn't track.

        Walks ``data/signal_log.json`` and nets buy-quantity against
        exit-quantity per crypto symbol. Compares against ``self.positions``
        (already populated by ``_load_persisted_positions`` and broker sync)
        and surfaces any non-zero net deltas as orphans.

        Why: TradersPost crypto subscriptions are webhook-only — there is no
        REST endpoint to query open broker positions. When the bot crashes
        between an entry and the next ``_persist_positions`` tick, or when an
        edge case (like the rotation→re-entry loop fixed in dbe19bf) creates
        broker fills the engine never tracked, those orphans accumulate
        silently. Surfacing them at boot turns a slow leak into a loud one.
        """
        import json as _json
        signal_file = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "data", "signal_log.json"
        )
        if not os.path.exists(signal_file):
            return
        try:
            with open(signal_file, "r") as f:
                sigs = _json.load(f)
        except Exception as e:
            log.debug(f"signal_log read failed: {e}")
            return

        from datetime import timedelta as _td, timezone as _tz
        cutoff = datetime.now(_tz.utc) - _td(hours=lookback_hours)
        from collections import defaultdict as _dd
        net_qty = _dd(float)
        for s in sigs:
            try:
                t = datetime.fromisoformat(s.get("time", ""))
                if t.tzinfo is None:
                    # Same fix as _signal_log_net_qty: legacy naive entries
                    # are local time, not UTC. See that helper for the
                    # 2026-05-18 incident detail.
                    t = self.tz.localize(t).astimezone(_tz.utc)
                else:
                    t = t.astimezone(_tz.utc)
            except Exception:
                continue
            if t < cutoff:
                continue
            if not s.get("success") or int(s.get("status_code", 0)) >= 300:
                continue
            sym = s.get("symbol", "")
            if not sym or not self._is_crypto_symbol(sym):
                continue
            qty = float(s.get("quantity", 0) or 0)
            tp_action = (s.get("tp_action", "") or "").lower()
            if tp_action == "buy":
                net_qty[sym] += qty
            elif tp_action == "exit":
                net_qty[sym] -= qty

        orphans = []
        with self._positions_lock:
            for sym, qty in net_qty.items():
                # Negative net = the bot signaled more exits than buys for this
                # symbol over the lookback window. Can't be short on a spot
                # crypto account — this means signal_log over-records exits
                # (TradersPost returns 200 even when the broker rejects the
                # exit for "no position to close"). Treat as zero, not orphan.
                if qty < 0.0001:
                    continue
                if sym in self.positions:
                    continue  # Engine knows about it — not an orphan
                orphans.append((sym, qty))

        if not orphans:
            log.info(
                f"CRYPTO RECONCILE: clean — no broker-side orphans in the "
                f"last {lookback_hours}h"
            )
            return

        # Sort biggest-qty-first so the alert lead with the biggest exposure.
        orphans.sort(key=lambda x: -abs(x[1]))
        orphan_summary = ", ".join(f"{s}={q:.4f}" for s, q in orphans)
        log.warning(
            f"CRYPTO RECONCILE: {len(orphans)} ORPHAN crypto position(s) "
            f"likely open on TradersPost but NOT tracked by engine: "
            f"{orphan_summary}. The bot will not manage stops/exits on these."
        )
        try:
            self.notifier.risk_alert(
                f"⚠️ CRYPTO ORPHAN POSITIONS DETECTED at boot: "
                f"{len(orphans)} symbol(s) on TradersPost without engine "
                f"tracking. {orphan_summary}. Check TradersPost UI and "
                f"close manually if unwanted."
            )
        except Exception as e:
            log.debug(f"orphan notifier alert failed: {e}")

    def _reconcile_mirror_orphans(self, lookback_hours=72, close=False):
        """Detect equity positions drifting on the TradersPost MIRROR account.

        The mirror (``TRADERSPOST_MIRROR_WEBHOOK_URL``) is a one-way copy of
        every IBKR equity fill — it has no positions API, so when a mirrored
        exit doesn't actually flatten the connected broker (TradersPost
        returns HTTP 200 even on a rejected "no position" close) the mirror
        account silently accumulates one-sided longs. IBKR stays the source
        of truth; this walk turns that slow leak into a loud alert.

        Walks ``data/signal_log.json``, nets buy vs exit quantity per EQUITY
        symbol (crypto is handled by :meth:`_reconcile_crypto_orphans`), and
        reports any positive net the engine isn't tracking. With
        ``close=True`` it sends a mirror EXIT webhook to flatten each orphan.

        Returns the list of ``(symbol, qty)`` orphans found.

        Limitation: signal_log records what was SENT, not what the broker
        filled. An orphan whose phantom-success exits net it to zero in the
        log (HTTP 200 on a rejected close) is invisible here — only a real
        positions API on the connected broker could catch that case.

        Detection is a pure signal_log walk and needs no broker object — it
        runs at boot before ``tp_mirror`` is constructed. Only ``close=True``
        needs the mirror webhook.
        """
        import json as _json
        signal_file = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "data", "signal_log.json"
        )
        if not os.path.exists(signal_file):
            return []
        try:
            with open(signal_file, "r") as f:
                sigs = _json.load(f)
        except Exception as e:
            log.debug(f"signal_log read failed: {e}")
            return []

        from datetime import timedelta as _td, timezone as _tz
        cutoff = datetime.now(_tz.utc) - _td(hours=lookback_hours)
        from collections import defaultdict as _dd
        net_qty = _dd(float)
        for s in sigs:
            try:
                t = datetime.fromisoformat(s.get("time", ""))
                if t.tzinfo is None:
                    # Legacy naive entries are local time — see _signal_log_net_qty.
                    t = self.tz.localize(t).astimezone(_tz.utc)
                else:
                    t = t.astimezone(_tz.utc)
            except Exception:
                continue
            if t < cutoff:
                continue
            if not s.get("success") or int(s.get("status_code", 0)) >= 300:
                continue
            sym = s.get("symbol", "")
            # Equity only — crypto is _reconcile_crypto_orphans' job.
            if not sym or self._is_crypto_symbol(sym):
                continue
            qty = float(s.get("quantity", 0) or 0)
            tp_action = (s.get("tp_action", "") or "").lower()
            if tp_action == "buy":
                net_qty[sym] += qty
            elif tp_action == "exit":
                net_qty[sym] -= qty

        orphans = []
        with self._positions_lock:
            for sym, qty in net_qty.items():
                # Negative net = more exits than buys logged — the mirror
                # over-records exits (HTTP 200 on rejected closes). Not an orphan.
                if qty < 0.0001:
                    continue
                # Engine tracks it AND the qty roughly matches → managed, skip.
                if sym in self.positions:
                    continue
                orphans.append((sym, round(qty, 4)))

        if not orphans:
            log.info(
                f"MIRROR RECONCILE: clean — no equity orphans on the mirror "
                f"account in the last {lookback_hours}h"
            )
            return []

        orphans.sort(key=lambda x: -abs(x[1]))
        orphan_summary = ", ".join(f"{s}={q:g}" for s, q in orphans)
        log.warning(
            f"MIRROR RECONCILE: {len(orphans)} ORPHAN equity position(s) "
            f"likely open on the TradersPost mirror account but NOT tracked "
            f"by the engine: {orphan_summary}. IBKR is the source of truth; "
            f"the bot does not manage stops/exits on these."
        )
        try:
            self.notifier.risk_alert(
                f"⚠️ MIRROR ORPHAN POSITIONS: {len(orphans)} equity symbol(s) "
                f"adrift on the TradersPost mirror without engine tracking. "
                f"{orphan_summary}. Check the mirror broker and close manually, "
                f"or POST /api/reconcile/mirror/run?close=true."
            )
        except Exception as e:
            log.debug(f"mirror orphan notifier alert failed: {e}")

        if close and not self.tp_mirror:
            log.warning(
                "MIRROR RECONCILE: close requested but the mirror webhook is "
                "not configured — orphans reported only, not flattened."
            )
        elif close:
            for sym, qty in orphans:
                try:
                    self.tp_mirror.notify_trade({
                        "symbol": sym,
                        "action": "sell",
                        "quantity": qty,
                        "source": "mirror_exit",
                    })
                    log.warning(
                        f"MIRROR RECONCILE: sent flatten EXIT for orphan "
                        f"{sym} qty={qty:g}"
                    )
                except Exception as e:
                    log.error(f"MIRROR RECONCILE: failed to close orphan {sym}: {e}")

        return orphans

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
            # Tiered alert schedule: first three pings then exponential
            # backoff. The earlier "every 10 min forever" cadence pinged
            # the user 220+ times across a 36h outage. Goal: notify
            # promptly, then go quiet until something material changes.
            #
            # Alert at attempts:
            #   10  (~5  min) — first ping, paired with auto-recovery #1
            #   30  (~15 min) — second ping, paired with auto-recovery #2
            #   60  (~30 min) — third ping, paired with auto-recovery #3
            #   180 (~90 min) — backed-off check-in
            #   360 (~3 h)
            #   720 (~6 h)
            #   then every 720 attempts (~6 h) until reconnect.
            ALERT_SCHEDULE = [10, 30, 60, 180, 360, 720]
            ALERT_BACKOFF_INTERVAL = 720  # ~6h cadence after the schedule
            alerted_attempts = set()
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
                        if self.notifier:
                            try:
                                self.notifier.system_alert(
                                    f"IBKR reconnected after {attempt} attempts — trading resumed",
                                    level="success",
                                )
                            except Exception:
                                pass
                        try:
                            self._sync_after_reconnect()
                        except Exception as e:
                            log.warning(f"Post-reconnect sync error: {e}")
                        break
                except Exception as e:
                    log.warning(f"Background reconnect attempt #{attempt} error: {e}")

                # Decide whether this attempt is an alert milestone
                should_alert = False
                if attempt in ALERT_SCHEDULE:
                    should_alert = True
                elif attempt > ALERT_SCHEDULE[-1]:
                    # Past the scheduled milestones — fire every Nth attempt
                    if (attempt - ALERT_SCHEDULE[-1]) % ALERT_BACKOFF_INTERVAL == 0:
                        should_alert = True

                if should_alert and attempt not in alerted_attempts:
                    alerted_attempts.add(attempt)
                    mins = attempt * 30 // 60

                    # Compute when the next reminder will fire so the user
                    # knows they won't be re-pinged immediately.
                    if attempt in ALERT_SCHEDULE:
                        idx = ALERT_SCHEDULE.index(attempt)
                        if idx < len(ALERT_SCHEDULE) - 1:
                            next_delta_attempts = ALERT_SCHEDULE[idx + 1] - attempt
                        else:
                            next_delta_attempts = ALERT_BACKOFF_INTERVAL
                    else:
                        next_delta_attempts = ALERT_BACKOFF_INTERVAL
                    next_min = next_delta_attempts * 30 // 60

                    # Try to self-heal before paging: restart the
                    # gateway container (bounded by 3/day + 10min cooldown).
                    restarted = self._try_auto_recover_gateway()
                    if restarted:
                        # Give the freshly-restarted gateway 120s to
                        # boot before the next reconnect attempt — IBC
                        # cold-boot + IBKR login takes ~60-90s.
                        _time.sleep(120)

                    if restarted:
                        recovery_note = "Auto-restart of ib-gateway was just issued. "
                    else:
                        recovery_note = (
                            "Auto-restart did NOT fire (see logs for "
                            "AUTO-RECOVERY skip reason). "
                        )
                    msg = (
                        f"IBKR reconnect failing: {attempt} consecutive attempts "
                        f"(~{mins} min). {recovery_note}"
                        f"Next reminder in ~{next_min} min — "
                        f"if this keeps firing, VNC in or check .env credentials."
                    )
                    log.critical(msg)
                    if self.notifier:
                        try:
                            # level=error triggers @everyone, so your
                            # phone actually vibrates. After the first 3
                            # pings we back off so you're not tortured.
                            self.notifier.system_alert(msg, level="error")
                        except Exception:
                            pass

        import threading
        t = threading.Thread(target=reconnect_loop, daemon=True, name="ibkr-reconnect")
        t.start()
        log.info("Background IBKR reconnect thread started (every 30s)")

    def _load_auto_recovery_state(self):
        """Read persisted auto-recovery counters from disk so the 3/day cap
        actually holds across bot restarts. Without this, every fresh bot
        process starts with count=0 — and since restarting the gateway kills
        the shared netns (and thus the bot), in-memory state never lasts
        long enough for the cap to fire. Result: ~10-min restart loops
        observed live on 2026-05-15."""
        try:
            if not self._auto_recovery_state_file.exists():
                return
            with open(self._auto_recovery_state_file) as f:
                state = json.load(f)
            saved_day = state.get("day")
            today = datetime.now(self.tz).date().isoformat()
            # Counters only valid for today — yesterday's count starts fresh
            if saved_day == today:
                self._auto_restart_count = int(state.get("count", 0))
                self._auto_restart_day = datetime.now(self.tz).date()
                self._last_auto_restart_ts = float(state.get("last_ts", 0.0))
                log.warning(
                    f"AUTO-RECOVERY state loaded: {self._auto_restart_count}/3 "
                    f"gateway restarts already used today (last at "
                    f"{datetime.fromtimestamp(self._last_auto_restart_ts).isoformat() if self._last_auto_restart_ts else 'never'})"
                )
        except Exception as e:
            log.warning(f"AUTO-RECOVERY state load failed (will treat as fresh): {e}")

    def _persist_auto_recovery_state(self):
        """Save counters to disk so they survive the bot restart that the
        gateway restart triggers via shared-netns teardown."""
        try:
            self._auto_recovery_state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._auto_recovery_state_file.with_suffix(".json.tmp")
            payload = {
                "day": self._auto_restart_day.isoformat() if self._auto_restart_day else None,
                "count": self._auto_restart_count,
                "last_ts": self._last_auto_restart_ts,
            }
            with open(tmp, "w") as f:
                json.dump(payload, f)
            tmp.replace(self._auto_recovery_state_file)
        except Exception as e:
            log.warning(f"AUTO-RECOVERY state persist failed: {e}")

    def _handle_ibkr_wedge(self, consecutive_timeouts):
        """Called by the IBKR broker when N consecutive worker calls have
        timed out. Treats the worker as wedged and triggers the same
        auto-recovery path the reconnect loop uses for lost connection.

        Closes the gap from the DELL incident (session 10): `is_connected()`
        returns True throughout the wedge because TCP is up; the only
        symptom is repeated `IBKR worker call timed out` messages. Without
        this hook, the bot logged the failures but the reconnect loop
        never triggered restart.
        """
        log.critical(
            f"IBKR WORKER WEDGED: {consecutive_timeouts} consecutive worker "
            f"timeouts — attempting ib-gateway auto-recovery (DELL pattern)"
        )
        if self.notifier:
            try:
                self.notifier.system_alert(
                    f"IBKR worker wedged ({consecutive_timeouts} consecutive "
                    f"timeouts on `worker call timed out`). Attempting "
                    f"ib-gateway container restart.",
                    level="error",
                )
            except Exception:
                pass
        try:
            self._try_auto_recover_gateway()
        except Exception as e:
            log.error(f"Wedge auto-recovery raised: {e}", exc_info=True)

    def _try_auto_recover_gateway(self):
        """Restart the ib-gateway container when the API is wedged.

        TCP-only healthcheck on the gateway container can't tell the
        difference between "alive" and "port open, API dead" (which is
        how we lost 22h this week). Instead of trying to make the
        healthcheck smarter (fragile inside the unusualalpha image),
        the bot escalates from its own real liveness probe: if we've
        been failing reconnect for ~5 min, restart the container.

        Safety caps:
          * 3 restarts per calendar day max (prevents restart-loop if
            credentials are wrong or IBKR is out)
          * 10 min cooldown between attempts (gateway boot takes ~90s;
            cooldown avoids bouncing a still-booting container)
          * Requires /var/run/docker.sock mounted + docker SDK installed
            (both in docker-compose + requirements); silently skips if
            either is missing so bot still runs on dev machines

        Returns True if a restart was issued, False otherwise.
        """
        now = time.time()
        today = datetime.now(self.tz).date()

        # Reset daily counter at calendar rollover
        if self._auto_restart_day != today:
            self._auto_restart_day = today
            self._auto_restart_count = 0
            self._persist_auto_recovery_state()

        # Cooldown between attempts
        if now - self._last_auto_restart_ts < 600:
            return False

        # Daily cap
        if self._auto_restart_count >= 3:
            # Only log the cap-reached message once per day
            if self._auto_restart_count == 3:
                log.critical(
                    "AUTO-RECOVERY: daily restart cap (3) reached. "
                    "Not attempting further container restarts today. "
                    "Human intervention required."
                )
                if self.notifier:
                    try:
                        self.notifier.system_alert(
                            "AUTO-RECOVERY halted: 3 ib-gateway restarts today "
                            "all failed. Likely IBKR outage, bad credentials, "
                            "or account lockout. Manual intervention required.",
                            level="error",
                        )
                    except Exception:
                        pass
                self._auto_restart_count = 4  # sentinel: don't log again today
                self._persist_auto_recovery_state()
            return False

        try:
            import docker
        except ImportError:
            # Bumped from DEBUG → WARNING because a silent skip is how
            # this got missed for 36h. Throttled so it logs at most
            # once per hour to avoid filling the log.
            last_warn = getattr(self, "_last_auto_recovery_skip_warn", 0)
            if now - last_warn > 3600:
                log.warning(
                    "AUTO-RECOVERY skipped: docker Python SDK not installed. "
                    "Add `docker>=7.0.0` to requirements.txt and rebuild image."
                )
                self._last_auto_recovery_skip_warn = now
            return False

        try:
            client = docker.from_env()
            # Default container name pattern is <compose-project>-ib-gateway-1.
            # Project name defaults to the directory name ("trading-bot").
            # Fall back to scanning by image if naming differs.
            target = None
            try:
                target = client.containers.get("trading-bot-ib-gateway-1")
            except Exception:
                for c in client.containers.list(all=True):
                    if "ib-gateway" in (c.name or ""):
                        target = c
                        break
            if not target:
                last_warn = getattr(self, "_last_auto_recovery_skip_warn", 0)
                if now - last_warn > 3600:
                    log.warning(
                        "AUTO-RECOVERY skipped: ib-gateway container not "
                        "found in docker daemon. Available containers: "
                        f"{[c.name for c in client.containers.list(all=True)]}"
                    )
                    self._last_auto_recovery_skip_warn = now
                return False

            log.critical(
                f"AUTO-RECOVERY: restarting ib-gateway container "
                f"(attempt {self._auto_restart_count + 1}/3 today)"
            )
            # Persist the bump BEFORE issuing the restart — once
            # target.restart() returns, the shared netns is torn down and the
            # trading-bot container may die before any subsequent code runs.
            # Writing first guarantees the next boot sees the incremented count.
            self._auto_restart_count += 1
            self._last_auto_restart_ts = now
            self._persist_auto_recovery_state()
            target.restart(timeout=15)

            if self.notifier:
                try:
                    self.notifier.system_alert(
                        f"AUTO-RECOVERY: ib-gateway restart issued "
                        f"({self._auto_restart_count}/3 today). Bot should "
                        f"reconnect within ~2 min of gateway boot.",
                        level="error",
                    )
                except Exception:
                    pass
            return True
        except Exception as e:
            # Bumped from "warning, every call" to "warning, once per hour"
            # so we can see WHY recovery isn't firing without flooding the
            # log when it's a persistent missing-mount.
            last_warn = getattr(self, "_last_auto_recovery_skip_warn", 0)
            if now - last_warn > 3600:
                log.warning(
                    f"AUTO-RECOVERY failed: {e}. Likely /var/run/docker.sock "
                    f"not mounted in this container (check docker-compose.yml "
                    f"volumes for trading-bot service)."
                )
                self._last_auto_recovery_skip_warn = now
            return False

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
            "low_float_catalyst": LowFloatCatalystStrategy,
            "crypto_runner": CryptoRunnerStrategy,
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

        # Crypto sleeve: inject the configured crypto symbols into the
        # crypto-eligible strategies so they iterate BTC/ETH/SOL on every
        # cycle alongside equities. Bars for these come from Yahoo (see
        # market_data._fetch_bars crypto short-circuit). Strategies emit
        # signals → engine routes to tp_crypto_broker → TradersPost webhook.
        if self._is_crypto_enabled():
            crypto_cfg = self.config.settings.get("crypto", {})
            crypto_symbols = self._get_crypto_universe()
            allowed = crypto_cfg.get("allowed_strategies", ["mean_reversion", "momentum"])
            if crypto_symbols:
                for strat_name in allowed:
                    strat = self.strategies.get(strat_name)
                    if strat and hasattr(strat, "add_dynamic_symbols"):
                        strat.add_dynamic_symbols(crypto_symbols)
                        log.info(
                            f"Injected {len(crypto_symbols)} crypto symbols "
                            f"({', '.join(crypto_symbols)}) into {strat_name}"
                        )

        runner_strat = self.strategies.get("momentum_runner")
        if runner_strat and hasattr(runner_strat, "add_dynamic_symbols"):
            runner_strat.add_dynamic_symbols(self.universe)
            log.info(f"Injected {universe_count} universe symbols into momentum runner")

        trend_rider_strat = self.strategies.get("daily_trend_rider")
        if trend_rider_strat and hasattr(trend_rider_strat, "add_dynamic_symbols"):
            trend_rider_strat.add_dynamic_symbols(self.universe)
            log.info(f"Injected {universe_count} universe symbols into daily trend rider")

        low_float_strat = self.strategies.get("low_float_catalyst")
        if low_float_strat and hasattr(low_float_strat, "add_dynamic_symbols"):
            low_float_strat.add_dynamic_symbols(self.universe)
            log.info(f"Injected {universe_count} universe symbols into low_float_catalyst")

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

        # Low-float catalyst pre-open flush: 09:25 ET.
        # Premarket micro-cap runners almost always whipsaw on the open as
        # daytraders dump. Close any low_float_catalyst positions before the
        # bell so we don't ride the gap-down. The strategy can re-enter at
        # 09:35+ if RVOL/trend still hold (open_dead_zone in the strategy
        # config blocks 9:25-9:35 entries).
        self.scheduler.add_job(
            self._flush_low_float_before_open,
            "cron", day_of_week="mon-fri", hour=9, minute=25,
            id="low_float_preopen_flush"
        )

        # Trend rider pre-open scan: 9:00 AM ET, 30 min before the bell.
        # Pre-populates `_qualified` candidates so the breakout-of-yesterday's-high
        # entry can fire on the open instead of waiting for the lazy first scan
        # mid-morning. Without this the breakouts the bot most wants to catch are
        # already 1.5%+ extended by the time the strategy is ready.
        self.scheduler.add_job(
            self._run_trend_rider_prescan,
            "cron", day_of_week="mon-fri", hour=9, minute=0,
            id="trend_rider_prescan"
        )

    def _flush_low_float_before_open(self):
        """Close any low_float_catalyst positions at 09:25 ET to dodge the
        open-bell whipsaw. Strategy's own dead-zone gates re-entry until 09:35,
        at which point conditions can be reassessed."""
        to_close = []
        with self._positions_lock:
            for sym, pos in list(self.positions.items()):
                if isinstance(pos, dict) and pos.get("strategy") == "low_float_catalyst":
                    to_close.append(sym)
        if not to_close:
            log.info("LOW-FLOAT PREOPEN FLUSH: no positions to close")
            return
        log.warning(
            f"LOW-FLOAT PREOPEN FLUSH: closing {len(to_close)} positions before "
            f"open whipsaw: {to_close}"
        )
        for sym in to_close:
            try:
                self._close_position(sym, "preopen_flush", "Low-float preopen flush")
            except Exception as e:
                log.error(f"Preopen flush failed for {sym}: {e}")

    def _run_trend_rider_prescan(self):
        """Force the daily-trend-rider daily-bar scan at 9 AM ET so candidates are
        queued before the bell. No-op if the strategy is not loaded."""
        strat = self.strategies.get("daily_trend_rider")
        if not strat:
            return
        if not self.market_data:
            return
        try:
            strat._scan_daily_bars(self.market_data)
            strat._last_daily_scan = time.time()
            log.info(
                f"TREND RIDER PRESCAN: {len(getattr(strat, '_qualified', {}))} candidates "
                f"qualified before market open"
            )
        except Exception as e:
            log.error(f"Trend rider prescan failed: {e}", exc_info=True)

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

                    # Crypto fast lane runs 24/7 — it must fire here too, otherwise
                    # weekends + after-hours = no crypto evaluation at all (which is
                    # exactly the state that produced "no organic crypto trades" for
                    # 14+ hours). Internally guarded on crypto.enabled config.
                    self._quick_scan_crypto()

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

                # --- HOT-MOVER FAST LANE (every 3 seconds) ---
                # Closes the PIII-style gap: a 5-15% gainer that spikes
                # mid-cycle was previously evaluated once per 10s. By the time
                # the next cycle runs the price has drifted past the deviation
                # gate. Fast lane re-evaluates the top 5 movers every 3s so a
                # fresh, current-priced signal can fire and fill.
                self._quick_scan_hot_movers()

                # --- CRYPTO FAST LANE (every 3 seconds) ---
                # Mirror of the hot-mover lane for BTC/ETH/SOL: the slow
                # 132s cycle was ageing out crypto signals before they
                # reached risk_manager. Re-runs mean_reversion + momentum
                # on the 3 crypto symbols every 3s and executes any fresh
                # signal directly.
                self._quick_scan_crypto()

                # --- FULL CYCLE (every ~10 seconds = 3 fast ticks) ---
                if scalp_tick >= 3:
                    scalp_tick = 0

                    # Per-stage timing (kept in process memory; logged at cycle end
                    # if any single stage exceeded 5s — surfaces slow paths without
                    # spamming the log on healthy cycles). The 22-min cycle observed
                    # 2026-05-15 came from _update_data hitting IBKR's historical-bar
                    # pacing limit; without timing visibility the symptom looked like
                    # a hung bot when it was actually a slow-but-progressing loop.
                    import time as _time
                    _t0 = _time.perf_counter()
                    _stage_times = {}
                    def _stage(name):
                        nonlocal _t0
                        _stage_times[name] = _time.perf_counter() - _t0
                        _t0 = _time.perf_counter()

                    # 0a. Dynamic discovery: feed top movers into RVOL strategies
                    self._discover_dynamic_symbols()
                    _stage("discover")

                    # 0a2. Prune stale dynamic symbols (30 min TTL)
                    # Symbols still actively moving get refreshed each cycle;
                    # dead symbols that stopped appearing as movers get pruned
                    self._prune_stale_dynamic_symbols()
                    _stage("prune")

                    # 0b. Update news feed watchlist with held + active symbols
                    if self.news_feed:
                        news_watch = list(set(
                            list(self.positions.keys()) + self.watchlist[:20]
                        ))
                        self.news_feed.update_watchlist(news_watch)
                    _stage("news_watch")

                    # 1. Update market data (standard 5-min + 1-min for scalps)
                    self._update_data()
                    _stage("update_data")
                    self._update_scalp_data()
                    _stage("update_scalp")

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

                    _stage("misc_3")

                    # 3. Monitor existing positions (stops, targets, trailing)
                    self._monitor_positions()
                    _stage("monitor_positions")

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
                    _stage("run_strategies")

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
                    _stage("filter")

                    # 6a. Capture rejected signals for momentum rotation
                    rejected_for_rotation = [s for s in signals if s not in approved and s.get("action") == "buy"]
                    if rejected_for_rotation and len(self.positions) >= self.risk_manager.max_positions - 1:
                        self._momentum_rotation_check(rejected_for_rotation)

                    # 6b. Deep-overnight equity guard. 20:00-04:00 ET (and
                    # weekends / holidays) sits OUTSIDE both premarket and
                    # postmarket windows, so the per-window allowed_strategies
                    # filter below never applies. The strategy loop still runs
                    # in that window because crypto keeps `_should_run` True,
                    # and equity strategies happily generate signals on names
                    # in their dynamic universes — which IBKR's extended-hours
                    # session is happy to fill. Result observed 2026-05-28
                    # 03:02 EDT: RKLB momentum entry, stop_loss -$82.56 in
                    # dead-of-night liquidity. The same loss tripped the
                    # strategy daily DD gate, pausing momentum for the entire
                    # next day.
                    #
                    # Drop equity BUY signals when the equity market is fully
                    # closed. Crypto signals pass through (24/7 by design).
                    # Exits / sells are unaffected — they don't flow through
                    # `approved` here; stops fire from `_check_position_exits`.
                    if not getattr(self, "_equity_market_open", False):
                        pre_filtered = []
                        dropped = []
                        for sig in approved:
                            if (sig.get("action") == "buy"
                                    and not self._is_crypto_symbol(sig.get("symbol", ""))):
                                dropped.append(sig.get("symbol", "?"))
                                continue
                            pre_filtered.append(sig)
                        if dropped:
                            log.info(
                                f"EQUITY MARKET CLOSED: dropped {len(dropped)} "
                                f"buy signal(s) — {', '.join(dropped[:10])}"
                                f"{'...' if len(dropped) > 10 else ''}"
                            )
                        approved = pre_filtered

                    # Equity dead-hours filter (Tier 1 restructure 2026-06-09).
                    # 30-day audit found two ET hours where the bot consistently
                    # loses money on equity entries: 05:00 (premarket
                    # slippage_reject pattern, -$203/30d) and 14:00 (early
                    # afternoon lunch lull, -$133/30d). Block equity BUY
                    # signals there; crypto bypasses (24/7 universe).
                    # Config: risk.equity_dead_hours_et (list of hours).
                    dead_hours = self.config.risk_config.get(
                        "equity_dead_hours_et", []
                    )
                    if dead_hours and getattr(self, "_equity_market_open", False):
                        now_hour = datetime.now(self.tz).hour
                        if now_hour in dead_hours:
                            pre_filtered = []
                            dropped = []
                            for sig in approved:
                                if (sig.get("action") == "buy"
                                        and not self._is_crypto_symbol(sig.get("symbol", ""))):
                                    dropped.append(sig.get("symbol", "?"))
                                    continue
                                pre_filtered.append(sig)
                            if dropped:
                                log.info(
                                    f"DEAD HOUR BLOCK ({now_hour:02d}:00 ET): "
                                    f"dropped {len(dropped)} equity buy "
                                    f"signal(s) — {', '.join(dropped[:10])}"
                                    f"{'...' if len(dropped) > 10 else ''}"
                                )
                            approved = pre_filtered

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
                        # Quality gate: reject weak signals in thin premarket
                        # liquidity. Fall OPEN when rvol/score fields aren't
                        # present — not all strategies stamp those keys on
                        # every emit path, and reading the default 0 was
                        # silently killing every equity entry during premarket
                        # (observed 2026-05-18 ~07:12 EDT: SG/AMZN/NOK/QNCX
                        # all approved by risk_manager, then dropped here
                        # with `score=0` despite the strategy logging score 75-80).
                        pre_filtered = []
                        for sig in approved:
                            if sig.get("action") != "buy":
                                pre_filtered.append(sig)
                                continue
                            sig_rvol = sig.get("rvol")
                            sig_score = sig.get("score")
                            if sig_rvol is not None and sig_rvol < min_rvol:
                                log.info(
                                    f"PREMARKET REJECT: {sig['symbol']} RVOL={sig_rvol:.1f}x "
                                    f"(need RVOL>={min_rvol})"
                                )
                                continue
                            if sig_score is not None and sig_score < min_score:
                                log.info(
                                    f"PREMARKET REJECT: {sig['symbol']} score={sig_score} "
                                    f"(need score>={min_score})"
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
                        # Quality gate: same fall-open-on-missing semantics
                        # as the premarket branch above.
                        post_filtered = []
                        for sig in approved:
                            if sig.get("action") != "buy":
                                post_filtered.append(sig)
                                continue
                            sig_rvol = sig.get("rvol")
                            sig_score = sig.get("score")
                            if sig_rvol is not None and sig_rvol < min_rvol:
                                log.info(
                                    f"POSTMARKET REJECT: {sig['symbol']} RVOL={sig_rvol:.2f}x "
                                    f"(need RVOL>={min_rvol:.2f})"
                                )
                                continue
                            if sig_score is not None and sig_score < min_score:
                                log.info(
                                    f"POSTMARKET REJECT: {sig['symbol']} score={sig_score} "
                                    f"(need score>={min_score})"
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

                    _stage("post_signal")

                    # Surface slow stages so future regressions of the 22-min
                    # cycle bottleneck are obvious. Only logs when a stage
                    # exceeded the threshold — healthy cycles stay quiet.
                    _slow = {k: v for k, v in _stage_times.items() if v > 5.0}
                    if _slow:
                        _total = sum(_stage_times.values())
                        _detail = ", ".join(f"{k}={v:.1f}s" for k, v in sorted(_slow.items(), key=lambda kv: -kv[1]))
                        log.warning(
                            f"SLOW CYCLE ({_total:.1f}s total): {_detail}"
                        )

                    # 7e-3. CYCLE HEARTBEAT — one INFO line per ~minute so the
                    # user can see the bot is actively evaluating even when no
                    # signals fire. Diagnoses "why no trades" at a glance.
                    self._full_cycle_count += 1
                    if self._full_cycle_count % 6 == 1:  # every ~1 min (6 × 10s)
                        bars_warm = 0
                        bars_total = 0
                        if self.market_data:
                            try:
                                # MarketDataFeed doesn't expose a .symbols
                                # attribute — pull tracked symbols from the
                                # actual bar cache (set by update/get_data).
                                tracked = list(
                                    getattr(self.market_data, "_bars_cache", {}).keys()
                                )
                                bars_total = len(tracked)
                                for sym in tracked:
                                    df = self.market_data._bars_cache.get(sym)
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
                        # Claude pre-trade hard gate is OFF by default — flip
                        # `ai_pretrade.enabled: true` to turn it back on. Live
                        # log analysis (2026-05-27) showed it skipping 100% of
                        # equity signals (267 SKIPs, 0 PROCEEDs in one session)
                        # because the WR rule was reading dirty historical data
                        # — 6/10 momentum "losses" were `slippage_reject`
                        # artifacts from the session-6 P&L bug already fixed in
                        # `2ec2325`, plus pre-fix trail-stop wicks (also fixed
                        # by `e6fcc34`). The gate has no measurement of whether
                        # its SKIPs ever correlated with worse outcomes, the
                        # 12-trade rolling window is too small for the rule it
                        # enforces, and adding 1–5s of API latency to the
                        # hot-path compounds with the signal queue's existing
                        # 16–53s dwell (HANDOFF session 6). AI is better
                        # applied batch (AutoTuner crons 12:30/16:30 ET,
                        # WeeklyReview Sat, AIInsights every 5 trades) — those
                        # paths stay on. This one gets gated off.
                        ai_pretrade_cfg = self.config.settings.get("ai_pretrade", {})
                        if (sig.get("action") == "buy" and
                                ai_pretrade_cfg.get("enabled", False) and
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

    def _get_crypto_universe(self):
        """Resolve the crypto universe for this cycle.

        With ``crypto.dynamic_universe.enabled: true``, pull the top-N
        symbols by 24h volume from CoinGecko (cached 24h on disk) and
        union them with the hand-curated ``crypto.symbols`` list so the
        user's must-keep favorites are never dropped by a CoinGecko
        ranking change. If the scanner fails (network down, parse error)
        the static list alone is returned, so a CoinGecko outage never
        disarms the crypto sleeve.

        Returns the merged universe (scanner-ranked first, static names
        appended after) as a deduped list.
        """
        crypto_cfg = self.config.settings.get("crypto", {})
        static_list = list(crypto_cfg.get("symbols", []))
        dyn_cfg = crypto_cfg.get("dynamic_universe", {})
        if not dyn_cfg.get("enabled", False):
            return static_list
        try:
            from bot.data.crypto_scanner import top_volume_symbols
            limit = int(dyn_cfg.get("limit", 50))
            ranked = top_volume_symbols(limit=limit)
        except Exception as e:
            log.warning(f"crypto scanner errored, falling back to static list: {e}")
            return static_list
        if not ranked:
            return static_list
        seen = set()
        merged: list[str] = []
        for sym in ranked + static_list:
            up = sym.upper()
            if up in seen:
                continue
            seen.add(up)
            merged.append(up)
        return merged

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

        # Full-day NYSE closures (Memorial Day, Thanksgiving, etc.) — treat
        # the same as a weekend so the equity sleeve doesn't run scanners
        # against a closed market.
        if is_us_market_holiday(now.date()):
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

        # Prioritize: positions first, then crypto, then top movers, then rest
        priority_symbols = []

        # 1. Open positions (always first — need accurate prices for stops)
        for sym in self.positions.keys():
            if sym in all_symbols:
                priority_symbols.append(sym)

        # 2. Crypto — the crypto fast lane needs bars for the whole crypto
        #    universe, and crypto uses a separate (non-IBKR) data feed, so it
        #    isn't the slow part. Always kept ahead of the universe cap.
        for sym in all_symbols:
            if self._is_crypto_symbol(sym) and sym not in priority_symbols:
                priority_symbols.append(sym)

        # 3. Top movers from Polygon (sorted by change% desc — Money Machine priority)
        if getattr(self, "polygon", None) and self.polygon.enabled:
            top_movers = self.polygon.get_top_movers(limit=50)
            for m in top_movers:
                sym = m.get("symbol", "")
                if sym and sym in all_symbols and sym not in priority_symbols:
                    priority_symbols.append(sym)

        # 4. Everything else
        remaining = [s for s in all_symbols if s not in priority_symbols]
        ordered_symbols = priority_symbols + remaining

        # --- UNIVERSE CAP ---
        # The IBKR scanner injects ~120 symbols/cycle; left uncapped the
        # working universe hit 190 and _update_data's IBKR historical-bar
        # fetch ran ~180s — blocking _fast_scalp_monitor (and therefore
        # position-stop checks) for minutes every cycle. Cap the set that
        # gets a bar refresh: positions + crypto lead the list and are never
        # trimmed; only the low-priority equity-discovery tail is dropped.
        # The bot holds at most a handful of equity positions — it does not
        # need fresh bars for 120 scanner movers.
        max_universe = self.config.risk_config.get("max_universe_symbols", 90)
        if len(ordered_symbols) > max_universe:
            trimmed = len(ordered_symbols) - max_universe
            ordered_symbols = ordered_symbols[:max_universe]
            log.info(
                f"UNIVERSE CAP: working set {len(ordered_symbols)} symbols "
                f"(trimmed {trimmed} low-priority equity names; max {max_universe})"
            )
        # Keep coverage diagnostics + stream pruning consistent with the cap.
        all_symbols = set(ordered_symbols)

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

    def _quick_scan_hot_movers(self):
        """Fast-lane signal generation for top movers (runs every 3 seconds).

        Closes the gap the PIII miss exposed: a stock spiking 5-15% mid-cycle
        was evaluated once per 10s, so by the next cycle the signal price was
        stale and risk_manager.Rule 6 rejected it for excessive drift. Now the
        top 5 movers get re-evaluated every 3s by the strategies designed for
        them, so a fresh current-priced signal can fire and reach execution
        before the price moves another 10%.

        Safe to run frequently: polygon_scanner.get_top_movers reads its 15s
        cached snapshot, so no extra API calls. Strategies are evaluated only
        on the hot-mover subset (not the full universe), keeping the work
        small.
        """
        # Skip outside market hours
        if not getattr(self, "_equity_market_open", False):
            return
        # Need polygon for the mover list
        if not getattr(self, "polygon", None) or not self.polygon.enabled:
            return
        # Skip if the strategy ecosystem isn't ready yet
        if not self.strategies or not self.market_data:
            return

        try:
            movers = self.polygon.get_top_movers(limit=5)
        except Exception as e:
            log.debug(f"FAST LANE: get_top_movers failed: {e}")
            return
        if not movers:
            return

        # Normalize to a symbol set; movers can be dicts or strings depending
        # on the polygon adapter version
        hot_symbols = set()
        for m in movers:
            sym = m.get("symbol") if isinstance(m, dict) else m
            if sym:
                hot_symbols.add(str(sym).upper())
        if not hot_symbols:
            return
        # Strip symbols we already hold — duplicate-entry guard catches them
        # later but skipping early saves work
        hot_symbols -= set(self.positions.keys())
        if not hot_symbols:
            return

        # Only momentum-aware strategies benefit from fast-lane evaluation;
        # mean-reversion / pairs / etc. have no reason to re-fire on hot movers
        fast_lane_strats = (
            "momentum_runner", "premarket_gap", "rvol_momentum", "daily_trend_rider",
            "low_float_catalyst",
        )

        fast_signals = []
        for name in fast_lane_strats:
            strat = self.strategies.get(name)
            if strat is None or not hasattr(strat, "generate_signals"):
                continue
            # Temporarily reduce dynamic universe to JUST the hot movers so
            # generate_signals iterates only those. Static `symbols` stays.
            # Without an explicit _dynamic_symbols attr the strategy can't be
            # subset cleanly, so skip it for this pass.
            original_dyn = getattr(strat, "_dynamic_symbols", None)
            if original_dyn is None:
                continue
            try:
                strat._dynamic_symbols = set(hot_symbols)
                if hasattr(strat, "set_held_symbols"):
                    _entry_times = {
                        sym: pos.get("entry_time")
                        for sym, pos in self.positions.items()
                        if pos.get("entry_time") is not None
                    }
                    strat.set_held_symbols(set(self.positions.keys()), entry_times=_entry_times)
                sigs = strat.generate_signals(self.market_data) or []
                for sig in sigs:
                    sig["strategy"] = name
                    sig["timestamp"] = datetime.now(self.tz)
                    sym = sig.get("symbol")
                    if sym and self.market_data:
                        sig["market_price"] = self.market_data.get_price(sym)
                    sig["_extended_hours"] = bool(
                        getattr(self, "_in_premarket", False)
                        or getattr(self, "_in_postmarket", False)
                    )
                    sig["_fast_lane"] = True
                fast_signals.extend(sigs)
            except Exception as e:
                log.debug(f"FAST LANE: strategy {name} error: {e}")
            finally:
                strat._dynamic_symbols = original_dyn

        if not fast_signals:
            return

        try:
            approved = self.risk_manager.filter_signals(
                fast_signals, self.positions, self.current_balance
            )
        except Exception as e:
            log.error(f"FAST LANE: risk_manager error: {e}", exc_info=True)
            return

        if approved:
            log.info(
                f"FAST LANE: {len(approved)}/{len(fast_signals)} signals approved on "
                f"hot movers {sorted(hot_symbols)[:5]}"
            )
            for sig in approved:
                try:
                    self._execute_signal(sig)
                except Exception as e:
                    log.error(
                        f"FAST LANE: execute failed for {sig.get('symbol')}: {e}",
                        exc_info=True,
                    )

    def _quick_scan_crypto(self):
        """Fast-lane crypto signal generation (runs every 3 seconds).

        Crypto trades 24/7 but the slow ~132s main cycle was ageing out
        crypto signals before risk_manager processed them — only 1
        organic crypto signal fired in 14h post-wires (ETH momentum
        2026-05-16 02:05 ET, queued in the 02:07 batch, overwritten by
        the 02:10 batch). This re-evaluates mean_reversion + momentum on
        BTC/ETH/SOL every 3s and pushes any resulting signal straight to
        risk_manager + execute — same pattern as the equity hot-mover
        fast lane, just with a fixed symbol set and no RTH gate.

        Cheap by construction: bar data is reused from the last slow
        cycle (Yahoo crypto fetches are exempt from the equity budget),
        and strategies iterate over only 3 symbols.
        """
        if not self.strategies or not self.market_data:
            return
        if not self._is_crypto_enabled():
            return

        # Source of truth: config/settings.yaml crypto.symbols. Was
        # hardcoded to ("BTC-USD","ETH-USD","SOL-USD") and silently
        # capped the fast lane at 3 names even after the yaml grew —
        # which would have hidden any moon trade outside that trio.
        crypto_syms = self.config.settings.get("crypto", {}).get("symbols", [])
        available = [s for s in crypto_syms if s not in self.positions]
        if not available:
            return

        # Heartbeat every ~60s so we can SEE the fast lane is alive and what
        # mean_reversion is computing — without this, the lane is invisible
        # until something fires, and we can't tell broken-from-quiet.
        now_ts = datetime.now(self.tz)
        last_hb = getattr(self, "_crypto_fast_lane_hb", None)
        if last_hb is None or (now_ts - last_hb).total_seconds() >= 60:
            self._crypto_fast_lane_hb = now_ts
            try:
                mr = self.strategies.get("mean_reversion")
                # With ~46 crypto symbols, per-symbol rows would make a single
                # 5KB log line every minute. Bucket by verdict and only spell
                # out the interesting ones (BUY SIGNAL + any WAIT:* near-miss);
                # collapse NEUTRAL / WARMING UP / no_data into counts.
                buckets = {"buy": [], "wait_near": [], "neutral": 0, "warming": 0, "no_data": 0}
                for sym in available:
                    sr = getattr(mr, "scan_results", {}).get(sym) if mr else None
                    if not sr:
                        buckets["no_data"] += 1
                        continue
                    verdict = sr.get("verdict") or ""
                    if verdict == "BUY SIGNAL":
                        buckets["buy"].append(
                            f"{sym}(z={sr.get('zscore')} rsi={sr.get('rsi')} bb={sr.get('bb_zone')})"
                        )
                    elif verdict.startswith("WAIT:"):
                        # Near-misses worth seeing — the user wants to know what's
                        # one bar away from firing.
                        short = verdict.replace("WAIT: ", "")
                        buckets["wait_near"].append(f"{sym}({short})")
                    elif verdict == "WAIT":
                        # _analyze_symbol's no-bars early-return sets verdict="WAIT"
                        # without zscore/rsi/etc. — that's "no data", NOT a true
                        # neutral verdict. Bucket it correctly so the heartbeat
                        # doesn't claim 45 symbols are NEUTRAL when really their
                        # bars never loaded.
                        buckets["no_data"] += 1
                    elif verdict == "WARMING UP":
                        buckets["warming"] += 1
                    else:
                        buckets["neutral"] += 1
                parts = [f"universe={len(available)}"]
                if buckets["buy"]:
                    parts.append(f"BUY[{len(buckets['buy'])}]: {', '.join(buckets['buy'])}")
                if buckets["wait_near"]:
                    parts.append(f"WAIT[{len(buckets['wait_near'])}]: {', '.join(buckets['wait_near'])}")
                if buckets["warming"]:
                    parts.append(f"warming={buckets['warming']}")
                if buckets["neutral"]:
                    parts.append(f"neutral={buckets['neutral']}")
                if buckets["no_data"]:
                    parts.append(f"no_data={buckets['no_data']}")
                log.info(
                    "CRYPTO FAST LANE HEARTBEAT (mean_reversion): %s",
                    " | ".join(parts),
                )
            except Exception as _e:
                log.debug(f"CRYPTO FAST LANE heartbeat failed: {_e}")

        fast_lane_strats = ("mean_reversion", "momentum")
        fast_signals = []
        for name in fast_lane_strats:
            strat = self.strategies.get(name)
            if strat is None or not hasattr(strat, "generate_signals"):
                continue
            original_dyn = getattr(strat, "_dynamic_symbols", None)
            if original_dyn is None:
                continue
            try:
                strat._dynamic_symbols = set(available)
                if hasattr(strat, "set_held_symbols"):
                    _entry_times = {
                        sym: pos.get("entry_time")
                        for sym, pos in self.positions.items()
                        if pos.get("entry_time") is not None
                    }
                    strat.set_held_symbols(set(self.positions.keys()), entry_times=_entry_times)
                sigs = strat.generate_signals(self.market_data) or []
                # Hard filter: the fast lane MUST only surface crypto signals.
                # Overriding `_dynamic_symbols` doesn't narrow the strategy's
                # universe — `get_symbols()` is `self.symbols | _dynamic_symbols`,
                # so the strategy still iterates its base equity list and we
                # were silently approving AMZN etc. from the crypto lane.
                available_set = set(available)
                sigs = [s for s in sigs if s.get("symbol") in available_set]
                for sig in sigs:
                    sig["strategy"] = name
                    sig["timestamp"] = datetime.now(self.tz)
                    sym = sig.get("symbol")
                    if sym and self.market_data:
                        sig["market_price"] = self.market_data.get_price(sym)
                    sig["_extended_hours"] = bool(
                        getattr(self, "_in_premarket", False)
                        or getattr(self, "_in_postmarket", False)
                    )
                    sig["_fast_lane"] = True
                    sig["_crypto_fast_lane"] = True
                fast_signals.extend(sigs)
            except Exception as e:
                log.debug(f"CRYPTO FAST LANE: strategy {name} error: {e}")
            finally:
                strat._dynamic_symbols = original_dyn

        if not fast_signals:
            return

        try:
            approved = self.risk_manager.filter_signals(
                fast_signals, self.positions, self.current_balance
            )
        except Exception as e:
            log.error(f"CRYPTO FAST LANE: risk_manager error: {e}", exc_info=True)
            return

        if approved:
            # One-line per-signal detail so we can see WHAT is approved
            # (symbol + action + strategy + confidence) and trace why an
            # apparent "approval" isn't producing a trade.
            for sig in approved:
                log.info(
                    "CRYPTO FAST LANE: approved %s %s from %s conf=%.2f price=%s reason=%s",
                    sig.get("action"),
                    sig.get("symbol"),
                    sig.get("strategy"),
                    sig.get("confidence", 0.0),
                    sig.get("market_price") or sig.get("price"),
                    sig.get("reason"),
                )
                try:
                    self._execute_signal(sig)
                except Exception as e:
                    log.error(
                        f"CRYPTO FAST LANE: execute failed for {sig.get('symbol')}: {e}",
                        exc_info=True,
                    )

    # Profit level (as a P&L fraction) at which a plain `momentum` position's
    # trailing stop is allowed to engage. Momentum is a runner strategy — a
    # noise-width trail that arms at entry strangles the breakout before it
    # develops. 2026-05-21 review: plain momentum trailed from tick #1 and
    # produced 10 trailing_stop exits, 0 wins, all within ±1% of entry.
    # Below this level only the hard stop_loss protects the trade; above it
    # the normal profit-tiered trail takes over. momentum_runner is unaffected
    # — it has its own 4-phase ATR trail.
    MOMENTUM_TRAIL_ARM_PCT = 0.02
    # Crypto-specific arming floor. The 18ae5f2 / session-5(9) fix prevented
    # the trail from being SET below entry but allowed it to be set AT entry
    # the moment price ticked one print above — turning the trail into a
    # breakeven stop that wicked out on the next print. Live review of 173
    # crypto trades (2026-05-17..05-26) found 12 trail exits in the -0.04%
    # to -1.29% band, net -$150, with `final_stop` still pinned at the
    # initial 5%-below-entry stop_loss — meaning the trail had armed at
    # entry and fired on slippage.
    #
    # 2026-06-02 trade audit on 145 mean_reversion trades: trailing_stop
    # path delivered 8W/30L at 21% WR for -$150 cumulative. The 0.5% arm
    # threshold survives most slippage but still fires on the next
    # normal-volatility pullback. Widening to 1.0% gives the trade ~1
    # ATR of breathing room on a typical crypto runner before the trail
    # engages — pulls the median trail-exit further above entry, lets
    # more half-winning trades reach time_exit or partial_target where
    # the strategy's actual edge lives (time_exit: 28W/24L 54% WR).
    CRYPTO_TRAIL_ARM_PCT = 0.01

    def _trail_floor_price(self, symbol, entry_price):
        """Minimum price the trailing-stop may sit at for this symbol.

        For crypto, returns `entry_price × (1 + CRYPTO_TRAIL_ARM_PCT)` so a
        trail exit always locks in at least +0.5% — eliminates the
        breakeven-wick pattern that survived PR #172. Before this fix the
        floor was `entry_price`, so a brief tick above entry armed the
        trail at exactly entry, and the next dip through entry triggered
        a sub-entry exit (5 such trades 2026-05-27..05-29, net -$26.80).

        For non-crypto symbols returns `entry_price` unchanged — equity
        / momentum strategies have their own arming gates and rely on
        the entry-floor behavior. Don't extend this to all assets without
        a parallel trade-review.
        """
        if self._is_crypto_symbol(symbol):
            return entry_price * (1 + self.CRYPTO_TRAIL_ARM_PCT)
        return entry_price

    def _trail_arm_allowed(self, pos, pnl_pct, symbol=None):
        """Whether a position's trailing stop may be ARMED/ratcheted now.

        Returns False for crypto positions below CRYPTO_TRAIL_ARM_PCT and
        plain `momentum` positions below MOMENTUM_TRAIL_ARM_PCT. The exit
        check against an already-set trail is never gated by this (a trail
        armed earlier, after the position ran, is still honored).
        """
        if symbol and self._is_crypto_symbol(symbol):
            return pnl_pct >= self.CRYPTO_TRAIL_ARM_PCT
        if pos.get("strategy") != "momentum" or pos.get("momentum_runner"):
            return True
        return pnl_pct >= self.MOMENTUM_TRAIL_ARM_PCT

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

            # --- SYNC FAKE-STOP ARMING ---
            # Positions synced from IBKR mid-drawdown got a loosened recovery
            # stop (see broker-sync blocks). Once price climbs back above the
            # original entry-based stop, swap in the entry stop so we don't
            # sit with a permanently loose stop on a recovered position.
            if not pos.get("_entry_stop_armed", True):
                target = pos.get("_entry_stop_target")
                if target is not None and current_price > target:
                    pos["stop_loss"] = max(pos.get("stop_loss", 0), target)
                    pos["_entry_stop_armed"] = True
                    log.info(
                        f"SYNC STOP ARMED: {symbol} price ${current_price:.2f} "
                        f"crossed entry-stop ${target:.2f} — stop tightened"
                    )

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
                # Crypto floor: trail can never be tighter than 1.5%, regardless of
                # tighten_trail/HOLD EXPIRING/RUNNER MODE paths that progressively
                # shrink trailing_stop_pct. Pre-fix, BTC and LINK exited at trail=0.1%
                # for -$15.80 combined on tiny pullbacks.
                _is_crypto = self._is_crypto_symbol(symbol)
                if _is_crypto:
                    _trail_floor = 0.015
                    if pos.get("trailing_stop_pct", 0) and pos["trailing_stop_pct"] < _trail_floor:
                        pos["trailing_stop_pct"] = _trail_floor
                base_trail = pos.get("trailing_stop_pct",
                                     self.config.risk_config.get("trailing_stop_pct", 0.02))
                if _is_crypto and base_trail < 0.015:
                    base_trail = 0.015

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
                    # MIGRATION: positions entered before 18ae5f2 have a stale
                    # trail set below entry by the old code. UNSET the stale trail
                    # rather than raise it — raising to entry_price when
                    # current_price < entry_price would instantly trigger the exit
                    # gate (saw this fire 3 unnecessary exits on 2026-05-18).
                    # With trail=0, the new code's natural ratchet will install a
                    # proper trail once price moves above entry. Hard stop_loss
                    # remains in place as the downside protection.
                    if (pos.get("trailing_stop", 0) > 0
                            and pos["trailing_stop"] < entry_price
                            and not pos.get("_trail_migrated")):
                        old_trail = pos["trailing_stop"]
                        pos["trailing_stop"] = 0
                        pos["_trail_migrated"] = True
                        log.info(
                            f"TRAIL MIGRATION: {symbol} stale trail "
                            f"${old_trail:.4f} unset (entry ${entry_price:.4f}, "
                            f"will re-arm on first ratchet above entry)"
                        )
                    # Trail can never lock in a loss: floor at entry_price (equity)
                    # or entry × 1.005 (crypto — _trail_floor_price). Pre-PR-#172,
                    # the trail engaged at tick #1 below entry and exited every
                    # losing crypto trade for -1.5%/-3%. Post-#172 the floor at
                    # entry stopped that but a wick pattern persisted (price
                    # ticks above entry → trail set at entry → wicks back below
                    # entry by slippage → exits at small loss); 5 such crypto
                    # trail exits 2026-05-27..05-29, net -$26.80. The new
                    # crypto floor at entry × 1.005 means trail exits always
                    # lock in ≥0.5% gain (modulo slippage).
                    floor_price = self._trail_floor_price(symbol, entry_price)
                    new_trail = max(current_price * (1 - trailing_pct), floor_price)
                    # Only move stop UP, never down — and only ratchet while in profit
                    # so we don't set the floor at entry on every losing tick.
                    # _trail_arm_allowed keeps a plain-momentum trail un-armed
                    # until the breakout has run past MOMENTUM_TRAIL_ARM_PCT,
                    # and gates crypto trails below CRYPTO_TRAIL_ARM_PCT to
                    # stop breakeven-stop wicks (see helper docstring).
                    if (self._trail_arm_allowed(pos, pnl_pct, symbol)
                            and current_price > entry_price and (
                        "trailing_stop" not in pos
                        or new_trail > pos.get("trailing_stop", 0)
                    )):
                        pos["trailing_stop"] = new_trail
                        # SYNC TO BROKER: Update broker-side stop to match
                        # Uses the higher of trailing_stop and stop_loss
                        broker_stop = max(
                            pos.get("trailing_stop", 0),
                            pos.get("stop_loss", 0)
                        )
                        if broker_stop > 0:
                            self._update_broker_stop(symbol, broker_stop)
                    if pos.get("trailing_stop", 0) > 0 and current_price <= pos["trailing_stop"]:
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
                # Mirror of the crypto trail fix in _fast_scalp_monitor
                # (commit 18ae5f2): only ratchet while in profit and floor
                # at entry_price. Without the floor, a brief wick above
                # entry sets trail just below entry (e.g. entry $4.99, wick
                # $5.00, trail $4.92) and the position exits at -1.5% on
                # the first pullback. Cost TZA -$22 / SCCG -$20 on
                # 2026-05-18 via this path.
                hwm = pos.get("_high_water_mark", entry)
                if price > hwm:
                    pos["_high_water_mark"] = price
                    # _trail_arm_allowed: plain-momentum trail stays un-armed
                    # below MOMENTUM_TRAIL_ARM_PCT so the breakout has room,
                    # crypto trail below CRYPTO_TRAIL_ARM_PCT (see helper).
                    if price > entry and self._trail_arm_allowed(pos, pnl_pct, symbol):
                        trail_pct = pos.get("trailing_stop_pct", 0.02)
                        # See engine.py:~3403 — crypto floor at entry × 1.005.
                        floor_price = self._trail_floor_price(symbol, entry)
                        new_trail = max(price * (1 - trail_pct), floor_price)
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
            # Same floor-at-entry guard as _on_5sec_bar (see commit 18ae5f2
            # for the original crypto fix). Tick path is the most aggressive
            # ratchet — fires on every print — so the unguarded version
            # locked in losses fastest.
            hwm = pos.get("_high_water_mark", entry)
            if price > hwm:
                pos["_high_water_mark"] = price
                # _trail_arm_allowed: plain-momentum trail stays un-armed
                # below MOMENTUM_TRAIL_ARM_PCT so the breakout has room,
                # crypto trail below CRYPTO_TRAIL_ARM_PCT (see helper).
                if price > entry and self._trail_arm_allowed(pos, pnl_pct, symbol):
                    trail_pct = pos.get("trailing_stop_pct", 0.02)
                    # See engine.py:~3403 — crypto floor at entry × 1.005.
                    floor_price = self._trail_floor_price(symbol, entry)
                    new_trail = max(price * (1 - trail_pct), floor_price)
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

            # Trend riders are explicitly "ride till bad news" — they're more
            # sensitive than other strategies. A score-1 bearish whisper (analyst
            # downgrade, soft sector commentary) should be enough to tighten the
            # trail on a profitable swing position, even if it wouldn't faze an
            # intraday momentum trade.
            is_trend_rider = (
                pos.get("trend_rider") or pos.get("strategy") == "daily_trend_rider"
            )
            min_news_score = 1 if is_trend_rider else 2

            if not is_bearish:
                # Also check raw recent_news for moderate bearish
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
                        if kw in title and score >= min_news_score:
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

            # --- Trend Rider Sharp-Drop Exit ---
            # Catches institutional distribution mid-session (3%+ drop in 30 min)
            # before the daily-close exit logic gets to react. No-op for non-trend-
            # rider positions.
            should_drop_exit, drop_reason = self._check_trend_rider_sharp_drop(symbol, pos)
            if should_drop_exit:
                positions_to_close.append((symbol, "trend_rider_sharp_drop", drop_reason))
                continue

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
                # Floor at entry (equity) or entry × 1.005 (crypto). Mirror of
                # the crypto fix in _fast_scalp_monitor (commit 18ae5f2) +
                # the 0.5% lockin from the trail-floor-profit follow-up.
                floor_price = self._trail_floor_price(symbol, entry_price)
                new_trail = max(current_price * (1 - trailing_pct), floor_price)
                # _trail_arm_allowed keeps a plain-momentum trail un-armed until
                # MOMENTUM_TRAIL_ARM_PCT, and crypto trail until
                # CRYPTO_TRAIL_ARM_PCT (see helper).
                if (self._trail_arm_allowed(pos, pnl_pct, symbol)
                        and current_price > entry_price and (
                    "trailing_stop" not in pos or new_trail > pos["trailing_stop"]
                )):
                    pos["trailing_stop"] = new_trail
                if pos.get("trailing_stop", 0) > 0 and current_price <= pos["trailing_stop"]:
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

            # --- ADAPTIVE TIME EXIT (Tier 1 B, 2026-06-09) ---
            # At max_hold / 2 ("half-life"), evaluate the position once:
            #   - FLAT (-0.3% to +0.5%): close NOW — cull deadwood, recycle capital
            #   - WINNING (>+0.5%): tighten trail + extend max_hold by 2x
            #   - LOSING (<-0.3%): leave alone — stop_loss / normal time_exit handle
            #
            # 30d audit of 69 crypto time_exits: 39 closed near zero (deadwood
            # that sat unproductively until force-close), 22 closed in profit
            # mid-run (winners cut short). This rule culls the deadwood early
            # and lets the winners breathe.
            #
            # Fires once per position (guarded by `_half_life_evaluated`).
            # Disabled via `risk.adaptive_time_exit_enabled: false`.
            if (
                "entry_time" in pos
                and not pos.get("_half_life_evaluated")
                and self.config.risk_config.get("adaptive_time_exit_enabled", True)
            ):
                elapsed = (datetime.now(self.tz) - pos["entry_time"]).total_seconds()
                # Compute the position's nominal max-hold in seconds.
                max_hold_secs = 0
                _max_hold_days = pos.get("max_hold_days", 0)
                _max_hold_bars = pos.get("max_hold_bars", 0)
                if _max_hold_days > 0:
                    max_hold_secs = _max_hold_days * 86400
                elif _max_hold_bars > 0:
                    max_hold_secs = _max_hold_bars * pos.get("bar_seconds", 300)

                if max_hold_secs > 0 and elapsed >= max_hold_secs / 2:
                    pos["_half_life_evaluated"] = True
                    flat_band = self.config.risk_config.get(
                        "half_life_flat_band", [-0.003, 0.005]
                    )
                    extend_mult = self.config.risk_config.get(
                        "half_life_extend_multiplier", 2.0
                    )
                    winner_trail = self.config.risk_config.get(
                        "half_life_winner_trail_pct", 0.01
                    )
                    lo, hi = float(flat_band[0]), float(flat_band[1])
                    if pnl_pct > hi:
                        # WINNER: tighten trail, extend max_hold
                        pos["trailing_stop_pct"] = min(
                            pos.get("trailing_stop_pct", 0.02), winner_trail
                        )
                        pos["_half_life_extended"] = True
                        pos["_half_life_extend_mult"] = float(extend_mult)
                        log.info(
                            f"HALF LIFE EXTEND: {symbol} winning {pnl_pct:+.1%} at "
                            f"half-life — trail tightened to {winner_trail:.1%}, "
                            f"max_hold × {extend_mult:.1f}"
                        )
                    elif lo <= pnl_pct <= hi:
                        # DEADWOOD: cull now, recycle capital
                        positions_to_close.append(
                            (symbol, "time_exit_half_life",
                             f"Flat at half-life ({pnl_pct:+.2%}) — recycling capital")
                        )
                        log.info(
                            f"HALF LIFE CULL: {symbol} flat at {pnl_pct:+.2%} after "
                            f"{elapsed/60:.0f}min — closing to recycle capital"
                        )
                        continue
                    else:
                        # LOSER: let stop_loss / normal time_exit handle it
                        log.info(
                            f"HALF LIFE LOSER: {symbol} {pnl_pct:+.1%} at half-life — "
                            f"letting stop_loss handle"
                        )

            # --- Max Holding Period ---
            if "entry_time" in pos:
                elapsed = (datetime.now(self.tz) - pos["entry_time"]).total_seconds()
                elapsed_days = elapsed / 86400

                # Days-based hold limit (swing/momentum trades).
                # Adaptive: if HALF LIFE EXTEND fired earlier (winning at
                # half-life), the effective max-hold doubles so the trade
                # can keep running on the tightened trail.
                _hl_mult = float(pos.get("_half_life_extend_mult", 1.0))
                max_hold_days = pos.get("max_hold_days", 0) * _hl_mult
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

                # Bars-based hold limit (scalp/intraday trades).
                # Adaptive: HALF LIFE EXTEND doubles the bars budget for
                # winners. _hl_mult was computed above (1.0 if not extended).
                elif "max_hold_bars" in pos and pos["max_hold_bars"] > 0:
                    bar_seconds = pos.get("bar_seconds", 300)
                    if elapsed > pos["max_hold_bars"] * bar_seconds * _hl_mult:
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

        # HARD GATE: never generate signals while IBKR is not live.
        # Strategies don't know their input is stale Yahoo fallback data
        # (15-min delayed) and will happily emit BUY on days-old prices,
        # which then dies at the execution gate with "IBKR NOT CONNECTED"
        # — producing the "approved=N, positions=0" pattern that masked
        # a 22h outage. Better to return zero signals loudly.
        if not self.broker or not self.broker.is_connected():
            now = time.time()
            last = getattr(self, "_last_broker_down_warn", 0.0)
            if now - last >= 300:  # Warn every 5 min, not every cycle
                log.warning(
                    "SIGNALS SUPPRESSED: IBKR not live. Skipping strategy "
                    "evaluation until reconnect — refusing to generate buy "
                    "signals on stale fallback data."
                )
                self._last_broker_down_warn = now
            return all_signals

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

        # Stamp held positions on every strategy so exit-signal branches can
        # gate on actual ownership instead of firing sells against unheld
        # scanner-discovered symbols (risk_manager would reject as "No position
        # to exit" — wastes a slot and crowds the rejection log).
        held_symbols = set(self.positions.keys())
        held_entry_times = {
            sym: pos.get("entry_time")
            for sym, pos in self.positions.items()
            if pos.get("entry_time") is not None
        }
        for strategy in self.strategies.values():
            if hasattr(strategy, "set_held_symbols"):
                strategy.set_held_symbols(held_symbols, entry_times=held_entry_times)

        # Per-symbol edge filter: mean_reversion (audit 2026-06-13) gates
        # entries against its own per-symbol P&L history rather than the
        # engine-wide should_avoid_symbol score (which pools across all
        # strategies and so misses strategy-specific bleeders like the
        # ICP/DOT/BCH crypto pattern). Cheap dict build; feed only if
        # both sides exist + the analyzer has > 0 trades to score.
        if self.trade_analyzer and hasattr(self.trade_analyzer, "get_symbol_edge_map"):
            # Wave 5 extends the gate to momentum (historically -$500/30d,
            # 26% WR — same per-strategy bleeder pattern). Each strategy
            # gets its OWN edge map so momentum's losers don't poison
            # mean_reversion's filter and vice versa.
            for strat_name in ("mean_reversion", "momentum"):
                strat = self.strategies.get(strat_name)
                if strat and hasattr(strat, "feed_symbol_edge"):
                    try:
                        strat.feed_symbol_edge(
                            self.trade_analyzer.get_symbol_edge_map(
                                strategy=strat_name,
                                min_trades=1,
                            )
                        )
                    except Exception as e:
                        log.debug(f"Symbol edge feed error ({strat_name}): {e}")

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
                    # Session hint: lets risk_manager widen its slippage + staleness
                    # gates during pre/post market when pre-market gappers can drift
                    # 5-15% between signal generation and execution.
                    sig["_extended_hours"] = bool(
                        getattr(self, "_in_premarket", False)
                        or getattr(self, "_in_postmarket", False)
                    )
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

        # Re-stamp timestamp + market_price for the WHOLE batch so they reflect
        # "the moment this batch arrives at risk_manager", not "the moment each
        # individual strategy emitted." Without this, slow late-loop strategies
        # (rvol_*, momentum_runner, daily_trend_rider can take 30s+ each) make
        # early-loop signals look 100s stale even though they're market-fresh.
        # Observed 2026-05-15: 87 ghost "Stale signal: 103s old" rejections —
        # all from the same 10:17:33 stamp, all rejected at 10:19:15.
        batch_now = datetime.now(self.tz)
        for sig in all_signals:
            sig["timestamp"] = batch_now
            sym = sig.get("symbol")
            if sym and self.market_data:
                sig["market_price"] = self.market_data.get_price(sym)

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

    def _entry_safety_gates(self, strategy_name, symbol=None):
        """Run all entry gates. Returns "" to allow, or a reason string to block.

        Each gate is independent; first one that fires wins. Order is cheap-to-
        expensive. None of these duplicate risk_manager (per-position risk +
        max_positions) or auto_tuner (long-term drift); they're complementary.
        """
        try:
            reason = self._gate_spy_circuit_breaker()
            if reason:
                self._record_gate_hit("spy_circuit_breaker", symbol, reason)
                return reason
            reason = self._gate_global_daily_trade_cap(symbol=symbol)
            if reason:
                self._record_gate_hit("daily_trade_cap", symbol, reason)
                return reason
            reason = self._gate_strategy_drawdown(strategy_name)
            if reason:
                self._record_gate_hit("strategy_drawdown", symbol, reason)
                return reason
            reason = self._gate_daily_drawdown()
            if reason:
                self._record_gate_hit("daily_drawdown", symbol, reason)
                return reason
            if symbol:
                reason = self._gate_crypto_funding_extreme(symbol)
                if reason:
                    self._record_gate_hit("crypto_funding", symbol, reason)
                    return reason
                reason = self._gate_correlation_cluster(symbol)
                if reason:
                    self._record_gate_hit("correlation_cluster", symbol, reason)
                    return reason
        except Exception as e:
            # Fail CLOSED: any gate raising silently used to return "" and
            # let the entry through, bypassing SPY breaker, daily trade cap,
            # drawdown gates, crypto funding, and correlation cluster all
            # at once. A KeyError or one bad network call could nullify
            # every defensive feature shipped this past week.
            log.warning(
                f"SAFETY GATE ERROR — blocking entry to fail closed: {e}"
            )
            return "safety_gate_error"
        return ""

    def _record_gate_hit(self, gate_name, symbol, reason):
        """Increment counters + record recent hit for the gate-hit dashboard.

        Why: without this we can't measure whether the defensive gates
        actually fire and prevent losses, or whether they're choking off
        good trades. Counts reset daily in _pre_market_scan so the dashboard
        shows "today" only; longer-term aggregation is a follow-up.
        """
        try:
            self._gate_hits[gate_name][symbol or "_unknown_"] += 1
            self._gate_hits_total[gate_name] += 1
            self._gate_recent.append({
                "gate": gate_name,
                "symbol": symbol or "",
                "reason": reason,
                "ts": datetime.now(self.tz).isoformat(timespec="seconds"),
            })
            # Cap the tail at 50
            if len(self._gate_recent) > 50:
                self._gate_recent = self._gate_recent[-50:]
        except Exception as e:
            log.debug(f"_record_gate_hit failed: {e}")

    def _gate_correlation_cluster(self, symbol):
        """Cap concurrent positions per correlation cluster.

        Why: a "diversified" book of 7 long crypto positions is really 1
        position on BTC beta — all alts trade ~0.7+ correlated with BTC most
        regimes, so a 3% BTC drop hits all 7 stops simultaneously. Pros size
        by factor exposure, not name count. This is the first-pass version
        using asset-class clustering; future upgrade can compute real
        pairwise correlation over rolling windows.

        Caps (configurable via config/settings.yaml):
          - crypto cluster: 5 concurrent (vs 7 generic max_positions)

        Equity isn't capped here yet because we have only 9 equity rows in
        history — not enough data to calibrate sector clusters. Add later.
        """
        if not self.positions:
            return ""
        if not self._is_crypto_symbol(symbol):
            return ""

        cap = self.config.settings.get("crypto", {}).get("max_concurrent_positions", 5)
        crypto_open = sum(1 for s in self.positions if self._is_crypto_symbol(s))
        if crypto_open >= cap:
            return (
                f"crypto concurrent cap: {crypto_open}/{cap} positions already open "
                f"(all share BTC-beta cluster)"
            )
        return ""

    def _gate_crypto_funding_extreme(self, symbol):
        """Skip crypto entries when perpetual funding rate is extreme.

        Mean reversion breaks down when funding is heavily one-sided — that's
        the market saying "this is a real directional move, not noise" via
        the perp/spot premium. Long shorts in heavy negative funding (or vice
        versa) and you're fighting both price and carry.

        Source: OKX public API (no auth; Bybit is 403 from this VPS,
        Binance.com perp is 451 geo-blocked). Cached 5 min per symbol.

        Threshold: |funding| > 0.05%/8h = ~0.15%/day = ~55%/yr annualized.
        That's already an extreme regime where carry alone dwarfs the
        average mean-reversion target.
        """
        if not self._is_crypto_symbol(symbol):
            return ""

        try:
            funding = self._get_crypto_funding_rate(symbol)
        except Exception as e:
            log.debug(f"funding fetch failed for {symbol}: {e}")
            return ""  # fail-open: don't block on data outage

        if funding is None:
            return ""

        threshold = 0.0005  # 0.05% per 8h
        if abs(funding) > threshold:
            return (
                f"funding rate extreme: {funding*100:+.3f}%/8h "
                f"(threshold ±{threshold*100:.2f}%)"
            )
        return ""

    def _get_crypto_funding_rate(self, symbol):
        """Fetch current funding rate for a crypto symbol from OKX.

        Returns funding rate as a decimal (0.0001 = 0.01% per 8h), or None
        if the symbol isn't listed on OKX perpetuals (e.g., very low-cap
        names). Cached 5 minutes per symbol.
        """
        import requests as _requests
        now_ts = time.time()
        cache = getattr(self, "_funding_cache", None)
        if cache is None:
            cache = {}
            self._funding_cache = cache
        entry = cache.get(symbol)
        if entry and now_ts - entry["ts"] < 300:
            return entry["funding"]

        # Symbol mapping: BTC-USD → BTC-USDT-SWAP (OKX perpetual format)
        base = symbol.upper().split("-")[0]
        # Reuse the Binance alias map for the same rebrands (POL, RENDER)
        aliases = getattr(self.market_data, "_BINANCE_ALIASES", {}) if self.market_data else {}
        base = aliases.get(base, base)
        okx_inst = f"{base}-USDT-SWAP"

        try:
            resp = _requests.get(
                "https://www.okx.com/api/v5/public/funding-rate",
                params={"instId": okx_inst},
                timeout=5,
            )
            if resp.status_code != 200:
                cache[symbol] = {"ts": now_ts, "funding": None}
                return None
            data = resp.json().get("data", [])
            if not data:
                cache[symbol] = {"ts": now_ts, "funding": None}
                return None
            funding = float(data[0].get("fundingRate", 0))
            cache[symbol] = {"ts": now_ts, "funding": funding}
            return funding
        except Exception as e:
            log.debug(f"OKX funding fetch failed for {okx_inst}: {e}")
            cache[symbol] = {"ts": now_ts, "funding": None}
            return None

    def _gate_daily_drawdown(self):
        """Tiered daily drawdown circuit breaker (crypto + equity).

        Pros do this because losing streaks cluster in regime changes — once
        you're down 3% intraday, the same broken regime is still active and
        the next 5 trades have skewed-negative expectancy. Pausing for hours
        is cheaper than fighting it. EOD reset at _end_of_day clears all
        tiers via `self.daily_pnl = 0`, no manual intervention.

        Tiers (against realized intraday P&L, not unrealized):
          -2.0%  → 1h pause (mirrors existing _check_daily_loss_soft_stop)
          -3.5%  → 4h pause
          -5.0%  → halt rest of day

        Independent of `_check_daily_loss_soft_stop` which fires post-trade.
        This runs on every entry signal so it triggers even if losses come
        from unrealized swings recognized later.
        """
        sod_bal = getattr(self, "start_of_day_balance", 0)
        if sod_bal <= 0:
            return ""
        dd_pct = self.daily_pnl / sod_bal
        if dd_pct >= -0.02:
            return ""

        now = datetime.now(self.tz)
        block_until = getattr(self, "_dd_block_until", None)
        if block_until and now < block_until:
            mins = (block_until - now).total_seconds() / 60
            return f"daily drawdown circuit breaker active ({mins:.0f}m left, DD {dd_pct:.1%})"

        if dd_pct <= -0.05:
            # Halt for the rest of the day. EOD reset at 16:30 ET clears it.
            eod = now.replace(hour=23, minute=59, second=59, microsecond=0)
            self._dd_block_until = eod
            log.warning(
                f"DAILY DRAWDOWN HALT: DD {dd_pct:.1%} — blocking all new entries "
                f"until EOD (-{-dd_pct*100:.1f}% vs -5.0% trigger)"
            )
            if getattr(self, "notifier", None):
                self.notifier.risk_alert(
                    f"DAILY DRAWDOWN HALT — Down {dd_pct:.1%} today. "
                    f"All new entries blocked until end of day."
                )
            return f"daily drawdown halt ({dd_pct:.1%})"

        if dd_pct <= -0.035:
            self._dd_block_until = now + timedelta(hours=4)
            log.warning(
                f"DAILY DRAWDOWN PAUSE (4h): DD {dd_pct:.1%} "
                f"— blocking new entries until {self._dd_block_until.strftime('%H:%M')}"
            )
            if getattr(self, "notifier", None):
                self.notifier.risk_alert(
                    f"DAILY DRAWDOWN PAUSE — Down {dd_pct:.1%}. "
                    f"Blocking entries for 4h."
                )
            return f"daily drawdown 4h pause ({dd_pct:.1%})"

        # -2% to -3.5%: 1h pause (covers the gap where _check_daily_loss_soft_stop
        # hasn't fired yet because no trade has closed since hitting the threshold)
        self._dd_block_until = now + timedelta(hours=1)
        log.warning(
            f"DAILY DRAWDOWN PAUSE (1h): DD {dd_pct:.1%} "
            f"— blocking new entries until {self._dd_block_until.strftime('%H:%M')}"
        )
        return f"daily drawdown 1h pause ({dd_pct:.1%})"

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

    def _gate_global_daily_trade_cap(self, symbol=None):
        """Hard cap on total entries per day across ALL strategies.

        Per-strategy max_trades_per_day exists already, but on wild days
        the bot can stack 50+ entries across strategies. This is the
        portfolio-level governor — independent of per-strategy caps.

        Crypto and equity are counted into separate buckets with separate
        caps. Crypto trades 24/7, so an equity-tuned cap (25) gets hit by
        evening and locks crypto out for the rest of the day. Defaults:
        equity 25, crypto 50. Either can be set to 0 to disable.
        """
        equity_cap = int(self.config.risk_config.get("max_total_trades_per_day", 25))
        crypto_cap = int(self.config.risk_config.get("max_total_crypto_trades_per_day", 50))
        is_crypto_signal = bool(symbol and self._is_crypto_symbol(symbol))
        cap = crypto_cap if is_crypto_signal else equity_cap
        bucket_label = "crypto" if is_crypto_signal else "equity"
        if cap <= 0:
            return ""

        # Count today's entries from trade_history (each trade = 1 entry),
        # bucketed by asset class so equity activity doesn't crowd out crypto
        # and vice-versa.
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
                if et_dt.date() != today:
                    continue
                t_is_crypto = self._is_crypto_symbol(t.get("symbol", ""))
                if t_is_crypto == is_crypto_signal:
                    entries_today += 1
            # Plus currently-open positions opened today (same bucket)
            for sym_p, p in self.positions.items():
                pet = p.get("entry_time")
                if pet and hasattr(pet, "date") and pet.date() == today:
                    p_is_crypto = self._is_crypto_symbol(sym_p)
                    if p_is_crypto == is_crypto_signal:
                        entries_today += 1
        except Exception as e:
            log.debug(f"daily trade cap counting error: {e}")
            return ""

        if entries_today >= cap:
            # Throttle the alert: only post once per day per bucket at the
            # cap moment so a chatty cap doesn't spam Discord every cycle.
            cap_state_key = f"_daily_cap_alerted_{bucket_label}_{today.isoformat()}"
            if not getattr(self, cap_state_key, False):
                self.notifier.risk_alert(
                    f"DAILY {bucket_label.upper()} TRADE CAP HIT: {entries_today}/{cap} "
                    f"entries today. Blocking further {bucket_label} entries until tomorrow."
                )
                setattr(self, cap_state_key, True)
            return f"Global daily {bucket_label} trade cap reached ({entries_today}/{cap})"
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
                # slippage_reject trades never held real market exposure —
                # they're opened and closed within ~3s by the post-fill
                # slippage gate, and their recorded P&L is computed off the
                # stale signal price (not the real fill), so it's noise.
                # Counting them would let an entry-time slippage event pause
                # the whole strategy for the day on a loss it never took.
                if (t.get("reason") or t.get("exit_reason")) == "slippage_reject":
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

    # Slippage tracker persistence (Wave 4). PR #214 shipped the dampener
    # in-memory only — a daily-restart cadence on the VPS meant the
    # buffer never accumulated the >5-sample minimum so the dampener
    # rarely fired. Persisted state survives restart and the dampener
    # can act on the first signal of a new session.
    _SLIPPAGE_FILE_NAME = "slippage_tracker.json"
    _SLIPPAGE_BUFFER_MAX = 20

    def _slippage_file_path(self):
        from pathlib import Path
        return Path(self.config.base_dir) / "data" / self._SLIPPAGE_FILE_NAME

    def _load_slippage_state(self):
        """Load persisted slippage buffers from disk. Falls open
        (returns empty defaultdict) on any error — never blocks
        startup on missing/corrupt cache or unset config."""
        from collections import deque, defaultdict
        import json
        store = defaultdict(lambda: deque(maxlen=self._SLIPPAGE_BUFFER_MAX))
        try:
            path = self._slippage_file_path()
            if not path.exists():
                return store
            with open(path, "r") as f:
                raw = json.load(f)
            for strategy, samples in raw.items():
                store[strategy] = deque(
                    (float(s) for s in samples), maxlen=self._SLIPPAGE_BUFFER_MAX,
                )
            log.info(f"Slippage tracker loaded ({sum(len(v) for v in store.values())} samples across {len(store)} strategies)")
        except Exception as e:
            log.warning(f"Slippage tracker load failed ({e}) — starting fresh")
        return store

    def _persist_slippage_state(self):
        """Write the slippage buffers to disk. Called after each record
        so a crash mid-session doesn't lose accumulated drag history.

        Falls open on any error (missing config, read-only fs, disk full)
        — in-memory tracking keeps working even if persistence breaks."""
        import json
        buf = getattr(self, "_strategy_slippage", None)
        if buf is None:
            return
        try:
            path = self._slippage_file_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump({k: list(v) for k, v in buf.items()}, f)
        except Exception as e:
            log.debug(f"slippage tracker persist failed: {e}")

    def _record_slippage(self, strategy, slippage_pct):
        """Record the realized signed slippage on a fill for the given
        strategy. Maintains a rolling deque so `_compute_slippage_mult`
        can dampen the next entry's size when recent fills show drag.

        Negative slippage (we got a discount) is recorded as zero —
        only adverse slippage counts as friction. The slippage_reject
        hard threshold handles the catastrophic case; this catches the
        steady drag that fills the strategy's edge with cost-per-trade.

        Persisted to disk after each record (Wave 4) so the dampener
        accumulates across restarts.
        """
        if not strategy:
            return
        if not hasattr(self, "_strategy_slippage"):
            self._strategy_slippage = self._load_slippage_state()
        adverse = max(0.0, float(slippage_pct or 0))
        self._strategy_slippage[strategy].append(adverse)
        try:
            self._persist_slippage_state()
        except Exception as e:
            # Disk-full / read-only fs / other write failure — keep the
            # in-memory tracker working. Persistence is a nice-to-have;
            # the dampener still functions on the current session.
            log.debug(f"slippage persist swallowed: {e}")

    def _compute_slippage_mult(self, strategy):
        """Return a sizing multiplier in [0.5, 1.0] based on the strategy's
        recent realized slippage. Mirrors `_compute_vol_regime_mult`
        shape so the call site stays parallel.

        Logic:
          fewer than 5 fills tracked → 1.0 (insufficient sample)
          avg ≤ 0.003 (0.3%)         → 1.0 (clean, no dampening)
          avg 0.003–0.006            → linear 1.0 → 0.5
          avg > 0.006 (0.6%)         → 0.5 floor

        Returns 1.0 on any error so a bad lookup doesn't block trading.
        """
        try:
            # Lazy-load persisted state on first access. Without this, a
            # fresh engine boot (Wave 4 persistence) would still see an
            # empty buffer until the first new fill of the session.
            if not hasattr(self, "_strategy_slippage"):
                self._strategy_slippage = self._load_slippage_state()
            buf = self._strategy_slippage.get(strategy)
            if not buf or len(buf) < 5:
                return 1.0
            avg = sum(buf) / len(buf)
            if avg <= 0.003:
                return 1.0
            if avg >= 0.006:
                mult = 0.5
            else:
                mult = 1.0 - ((avg - 0.003) / 0.003) * 0.5
            log.info(
                f"SLIPPAGE DAMPENER: {strategy} avg={avg:.3%} over {len(buf)} fills "
                f"→ sizing mult {mult:.2f}x"
            )
            return mult
        except Exception as e:
            log.debug(f"slippage mult failed for {strategy}: {e}")
            return 1.0

    def _compute_vol_regime_mult(self, symbol):
        """Compare short-window realized vol to a longer baseline. Returns
        a multiplier in [0.4, 1.0] — never sizes UP, only down when vol
        spikes vs the symbol's own baseline.

        Why this is needed even though ATR-based stops already vol-scale:
        ATR is computed over a single window (typically 14 bars). When
        vol regime SHIFTS — flash crash, major news, exchange outage —
        ATR lags by several bars and stop-distance underestimates current
        risk. Dollar risk per trade ends up 1.5-2x intended exactly when
        you can least afford it. This multiplier catches the regime shift
        on a shorter window and dampens sizing before ATR catches up.

        Logic:
          short_vol = std of last 10 5-min log returns (~50 min)
          long_vol  = std of last 60 5-min log returns (~5 hours)
          ratio     = short_vol / long_vol
          ratio < 1.5  → mult = 1.0 (normal)
          ratio 1.5-3  → linear from 1.0 → 0.5
          ratio > 3    → mult = 0.4 (floor)

        Returns 1.0 (neutral) on any data error so a bad fetch doesn't
        block a trade.
        """
        try:
            if not self.market_data:
                return 1.0
            bars = self.market_data.get_data(symbol)
            if bars is None or len(bars) < 60:
                return 1.0
            import numpy as _np
            closes = bars["close"].values[-60:]
            returns = _np.diff(_np.log(closes))
            if len(returns) < 50:
                return 1.0
            short_vol = float(_np.std(returns[-10:]))
            long_vol = float(_np.std(returns))
            if long_vol <= 0:
                return 1.0
            ratio = short_vol / long_vol
            if ratio < 1.5:
                return 1.0
            if ratio >= 3.0:
                mult = 0.4
            else:
                # Linear from 1.0 (ratio=1.5) → 0.5 (ratio=3.0)
                mult = 1.0 - ((ratio - 1.5) / 1.5) * 0.5
            log.info(
                f"VOL REGIME: {symbol} short={short_vol:.4f} long={long_vol:.4f} "
                f"ratio={ratio:.2f}x → sizing mult {mult:.2f}x"
            )
            return mult
        except Exception as e:
            log.debug(f"vol regime mult failed for {symbol}: {e}")
            return 1.0

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
            block_reason = self._entry_safety_gates(strategy, symbol=symbol)
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
        # Fails CLOSED during RTH (block on missing data, safer default), but fails
        # OPEN during pre/post-market for scanner-vetted gap-up sources: the scanner
        # already proved direction, and IBKR's streaming quote takes a few seconds
        # to populate after a new subscription. Blocking those silently kills
        # exactly the pre-market entries the bot is supposed to take.
        if action == "buy":
            falling_knife_pct = self.config.settings.get("risk", {}).get("falling_knife_pct", -5.0)
            in_extended = getattr(self, "_in_premarket", False) or getattr(self, "_in_postmarket", False)
            premarket_sources = {"premarket_gap", "rvol_momentum", "momentum_runner"}
            # Manual signals come from a human via the dashboard — they fail open on
            # the no-quote path because non-streamed symbols (anything outside the
            # 95-line IBKR cap) have no real-time quote, and the user already chose
            # the symbol consciously. The legit "quote present + change ≤ threshold"
            # block below still fires for manual — that protection is preserved.
            is_manual = signal.get("source") == "manual" or strategy == "manual"
            # Crypto symbols (BTC-USD, ETH-USD, ...) have no IBKR streaming
            # quote — Binance.US/Yahoo bars feed strategies directly, but
            # `get_quote()` only knows about equity quote sources, so it
            # always returns None for crypto. Check the symbol directly
            # rather than the fast-lane flag: the slow cycle emits crypto
            # signals too (momentum/mean_reversion's dynamic universes include
            # crypto post the pinning fix), and those signals don't carry
            # `_crypto_fast_lane=True` — without this they fail-close even
            # though the fast lane is firing the same signal seconds later.
            is_crypto = self._is_crypto_symbol(symbol)
            fail_open = (
                in_extended
                or signal.get("source") in premarket_sources
                or strategy in premarket_sources
                or is_manual
                or is_crypto
            )
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
                elif fail_open:
                    # "PASS" not "SKIP" — this branch ALLOWS the trade through.
                    # Previous "FALLING KNIFE SKIP" wording read like the
                    # guard was rejecting the entry, which led to a false-
                    # alarm investigation on the crypto path 2026-05-27.
                    log.info(
                        f"FALLING KNIFE PASS: {symbol} no quote in extended/crypto/manual "
                        f"context — fail-open, scanner/source vetted direction | Strategy: {strategy}"
                    )
                else:
                    log.warning(
                        f"FALLING KNIFE BLOCK (no quote): {symbol} — cannot verify day change, "
                        f"blocking entry as precaution | Strategy: {strategy}"
                    )
                    return
            except Exception as e:
                if fail_open:
                    log.info(f"FALLING KNIFE SKIP (error, extended/momentum): {symbol} — {e}")
                else:
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

            # Crypto re-entry cooldown: mean_reversion fires the same crypto
            # BUY every 3s as long as Z-score / RSI stay in oversold territory.
            # Without a cooldown, any close (stop, trailing, rotation, manual)
            # triggers an immediate re-buy on the same name, which (a) leaves
            # multiple TradersPost orders if the engine's exit-tracking races
            # with the next BUY (observed live for SUI/ICP/LINK 2026-05-17 —
            # 3 entries of each in ~30 min) and (b) chops capital on noise.
            # Equities have their own duplicate-check via broker.get_positions
            # below; this only fires for crypto on the tp_crypto_broker path.
            if self._is_crypto_symbol(symbol) and symbol in self._recently_closed:
                _elapsed = (datetime.now(self.tz) - self._recently_closed[symbol]).total_seconds()
                _crypto_cooldown = 600  # 10 min
                if _elapsed < _crypto_cooldown:
                    log.info(
                        f"CRYPTO RE-ENTRY COOLDOWN: {symbol} closed {_elapsed:.0f}s ago "
                        f"(cooldown {_crypto_cooldown}s) — skipping new entry"
                    )
                    return

            # Equity re-entry guard. A momentum BUY keeps re-firing the same
            # equity name every scan cycle while its EMA/ADX trend persists, so
            # a name that just lost money gets bought right back before the
            # quality gate has enough history to skip it. Two cases:
            #   * slippage_reject — the fill slipped past max_slippage_pct, i.e.
            #     a structural liquidity problem (wide spread on a thin name),
            #     not a timing one. Re-entering only repeats the loss. Block it
            #     for the rest of the session. (FBYD did this 3x on 2026-05-19,
            #     −$43.40 each, before the quality gate engaged.)
            #   * ordinary losing close — short cooldown so the bot stops
            #     chopping the same name on noise.
            # Cleared each session in _pre_market_scan.
            if not self._is_crypto_symbol(symbol) and symbol in self._recent_close_info:
                _info = self._recent_close_info[symbol]
                _elapsed = (datetime.now(self.tz) - _info["time"]).total_seconds()
                if _info.get("reason") == "slippage_reject":
                    log.info(
                        f"SLIPPAGE BLOCK: {symbol} had a slippage_reject close "
                        f"{_elapsed:.0f}s ago — blocked for the rest of the session "
                        f"(structural liquidity problem, not timing)"
                    )
                    return
                if _info.get("pnl", 0) < 0 and _elapsed < self._equity_loss_cooldown_secs:
                    log.info(
                        f"EQUITY RE-ENTRY COOLDOWN: {symbol} closed at a loss "
                        f"(${_info['pnl']:+.2f}) {_elapsed:.0f}s ago "
                        f"(cooldown {self._equity_loss_cooldown_secs}s) — skipping new entry"
                    )
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
                            entry_stop = entry * (1 - stop_pct)
                            # Fake-stop guard: if current price is already at/below
                            # the entry-based stop (sync happened after a drawdown),
                            # priming stop_loss at entry_stop locks in an immediate
                            # loss the next monitor tick. Drop to current*(1-stop_pct)
                            # so the position has room to recover; arm the entry stop
                            # once price climbs back above it. Same hazard as SHOP.
                            cur = None
                            try:
                                cur = self.market_data.get_price(symbol) if self.market_data else None
                            except Exception:
                                cur = None
                            sync_stop = entry_stop
                            sync_stop_armed = True
                            if cur is not None and cur <= entry_stop:
                                sync_stop = cur * (1 - stop_pct)
                                sync_stop_armed = False
                                log.warning(
                                    f"SYNC FAKE-STOP GUARD: {symbol} entry ${entry:.2f}, "
                                    f"current ${cur:.2f} already at/below entry-stop "
                                    f"${entry_stop:.2f}. Using recovery stop ${sync_stop:.2f}; "
                                    f"entry stop arms once price climbs above ${entry_stop:.2f}."
                                )
                            with self._positions_lock:
                                self.positions[symbol] = {
                                    **pos_data,
                                    "entry_time": datetime.now(self.tz),
                                    "stop_loss": sync_stop,
                                    "take_profit": entry * (1 + tp_pct),
                                    "trailing_stop_pct": self.config.risk_config.get("trailing_stop_pct", 0.02),
                                    "strategy": "synced_from_ibkr",
                                    "executed_via": "IBKR",
                                    "sync_flagged": reject_reason if not is_valid else "",
                                    "_entry_stop_armed": sync_stop_armed,
                                    "_entry_stop_target": entry_stop,
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

        # Price floor filter — no sub-$0.50 junk for equity.
        # Crypto exempt: SEI is $0.07, BONK / FLOKI / SHIB / PEPE / WIF are
        # all sub-dollar legitimate pairs. The $0.50 floor is an equity
        # penny-stock guard, not an asset-class assertion. Mirror of the
        # crypto exemption on the price ceiling below.
        # low_float_catalyst exempt: 2026-06-01 TGHL ran $0.30 → $2.48 (+575%)
        # intraday on 133M volume / $34M market cap — textbook low-float setup,
        # exactly the regime low_float_catalyst was built for (HANDOFF session
        # 5(8)). The strategy's own min_price ($0.20) admits TGHL but the
        # universal $0.50 floor blocks the asymmetric-edge entry window
        # ($0.30-$0.50). Strategy-level exemption keeps the universal guard
        # in place for momentum/rvol_*/etc. which still need it.
        min_price = self.config.settings.get("risk", {}).get("min_price", 0.50)
        is_low_float_signal = signal.get("strategy") == "low_float_catalyst"
        if (action == "buy" and current_price < min_price
                and not self._is_crypto_symbol(symbol)
                and not is_low_float_signal):
            log.info(f"PRICE FILTER: {symbol} ${current_price:.2f} below ${min_price} floor")
            return

        # Price ceiling filter — safety net for extreme-priced stocks
        # Top gainers scanner has no cap; this is the last safeguard.
        # Crypto exempt: BTC is $79k, ETH is $2k — the $500 stock ceiling is
        # an asset-class mismatch. Risk for crypto is capped by position
        # sizing ($ value), not unit price.
        max_buy_price = self.config.settings.get("risk", {}).get("scanner_max_price", 500.0)
        if (action == "buy"
                and current_price > max_buy_price
                and not self._is_crypto_symbol(symbol)):
            log.info(f"PRICE FILTER: {symbol} ${current_price:.2f} above ${max_buy_price} ceiling — skipping")
            return

        # Penny-runner pool: entry price falls in the configured band. These
        # are squeeze plays (YMAT-style sub-$1 → $2+, BRAI-style $8 → $15+) so
        # they need a wider stop than normal equity and a much wider trailing
        # to ride spikes without choking on routine pullbacks.
        _penny_min = self.config.risk_config.get("penny_runner_price_min", 0.20)
        _penny_max = self.config.risk_config.get("penny_runner_price_max", 15.00)
        _is_penny_runner = (
            not self._is_crypto_symbol(symbol)
            and _penny_min <= current_price <= _penny_max
            and self.config.risk_config.get("max_penny_runner_positions", 0) > 0
        )

        stop_loss_price = signal.get("stop_loss")
        if not stop_loss_price:
            # Use wider stops for crypto (more volatile)
            if self._is_crypto_symbol(symbol):
                crypto_risk = self.config.settings.get("crypto", {}).get("risk", {})
                stop_pct = crypto_risk.get("stop_loss_pct", 0.05)
            elif _is_penny_runner:
                stop_pct = self.config.risk_config.get("penny_runner_stop_loss_pct", 0.06)
            else:
                stop_pct = self.config.stop_loss_pct
            stop_loss_price = current_price * (1 - stop_pct)  # Long-only: stop is always below

        # STOP VALIDATION: Reject signals where stop is too close to entry.
        # Prevents instant stop triggers from near-zero ATR estimates.
        # Crypto gets a wider floor (5%) and INFO-level log because crypto
        # ATR is tiny relative to price (e.g. MATIC ATR ≈ $0.0001 on a $0.09
        # entry = 0.1% stop), so the near-zero stop is expected, not anomalous.
        stop_distance_pct = (current_price - stop_loss_price) / current_price if current_price > 0 else 0
        _is_crypto_entry = self._is_crypto_symbol(symbol)
        # Stop floor by asset class and price tier. Cheap equities need a
        # wider floor — a $1 stock with a 2% stop sits 2¢ away, which is
        # one bid-ask cross. Conservative tiers below (2026-05-18 review):
        #   crypto:         5% (unchanged — ATR ratios at crypto prices)
        #   equity < $5:    6% — penny scalps survive normal noise
        #   equity $5-$50:  3% (was 2% — mid-caps still get a real stop)
        #   equity > $50:   2% — liquid majors, tight bid-ask
        if _is_crypto_entry:
            _min_stop_pct = 0.05
            _floor_pct = 0.05
        elif current_price < 5.0:
            _min_stop_pct = 0.05
            _floor_pct = 0.06
        elif current_price < 50.0:
            _min_stop_pct = 0.02
            _floor_pct = 0.03
        else:
            _min_stop_pct = 0.01
            _floor_pct = 0.02
        if stop_distance_pct < _min_stop_pct:
            _msg = (
                f"STOP FLOOR APPLIED: {symbol} entry=${current_price:.4f} stop=${stop_loss_price:.4f} "
                f"({stop_distance_pct:.2%} gap). Setting {_floor_pct:.0%} minimum stop."
            )
            if _is_crypto_entry:
                log.info(_msg)
            else:
                log.warning(_msg)
            stop_loss_price = current_price * (1 - _floor_pct)

        # Get current hour for session-based sizing
        current_hour = datetime.now(self.tz).hour

        # Per-signal regime affinity: REGIME_STRATEGY_AFFINITY maps each
        # regime to per-strategy multipliers (the same table EOD uses for
        # allocation). Reading the regime status here lets live conditions
        # shift sizing within the trading day — momentum strategies grow in
        # BULL_TREND, mean-reversion grows in SIDEWAYS, everything shrinks in
        # CRISIS.
        #
        # CONFIDENCE GATE: only apply the multiplier when the detector is
        # confident (> 0.55). The SIDEWAYS default lands at 0.5 confidence and
        # "Insufficient data" at 0.3 — applying multipliers from those states
        # would shrink momentum sizing 30-50% on a stuck-SIDEWAYS bot, which
        # is exactly the wrong direction for a long-only momentum trader. Low
        # confidence → neutral 1.0 multiplier instead.
        regime_mult = 1.0
        if self.regime_detector:
            try:
                status = self.regime_detector.get_status()
                regime_conf = status.get("confidence", 0.0)
                if regime_conf > 0.55:
                    multipliers = status.get("strategy_multipliers", {})
                    regime_mult = multipliers.get(strategy, 1.0)
            except Exception as e:
                log.debug(f"Regime multiplier lookup failed for {strategy}: {e}")

        vol_regime_mult = self._compute_vol_regime_mult(symbol)
        slippage_mult = self._compute_slippage_mult(strategy)
        qty = signal.get("quantity") or self.position_sizer.calculate(
            balance=self.current_balance,
            price=current_price,
            stop_loss=stop_loss_price,
            strategy_allocation=self.config.strategy_allocation.get(strategy, 0.25),
            symbol=symbol,
            # Adaptive sizing inputs: Kelly, drawdown, session-based.
            # strategy → per-strategy Kelly (proven strategies size up on
            # their own edge instead of being dragged down by the blend).
            trade_history=self.trade_history,
            strategy=strategy,
            peak_balance=self.peak_balance,
            session_stats=getattr(self, '_session_stats', None),
            current_hour=current_hour,
            # New: confidence-scaled + regime-aware sizing
            confidence=signal.get("confidence", 0.6),
            regime_multiplier=regime_mult,
            # Vol-regime dampener: cuts size when realized vol spikes
            # vs symbol's own baseline. Protective only (max 1.0x).
            vol_regime_mult=vol_regime_mult,
            # Slippage dampener: cuts size when this strategy's recent
            # fills show steady-drag slippage > 0.3% avg.
            slippage_mult=slippage_mult,
        )

        if qty <= 0:
            # Was log.debug — invisible at production INFO level. The HANDOFF
            # session 9 ICP fast-lane → no-fill gap (5 approvals 06:26-06:29,
            # 0 orders) ended right here: the sizer returned 0, this return
            # fired, no log appeared. Now logged at INFO with enough context
            # to diagnose live: which strategy, what price, what balance,
            # what stop. If qty=0 is the per-strategy cap or dampener
            # squeezing to zero, the operator can SEE it instead of guessing.
            stop_loss = signal.get("stop_loss") or signal.get("stop") or 0
            log.warning(
                f"QTY=0 NO-FILL: {symbol} via {strategy} — sizer returned 0. "
                f"price=${current_price:.2f} stop=${stop_loss:.2f} "
                f"balance=${self.current_balance:.0f} "
                f"alloc={self.config.strategy_allocation.get(strategy, 0):.0%} "
                f"score={signal.get('score', 0)} conf={signal.get('confidence', 0):.2f}"
            )
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

        # MINIMUM R/R ENFORCEMENT (long-only): never execute a trade where the
        # reward is less than 2x the risk. Catches ATR-sized stops with
        # too-close targets (e.g. AAPL entry $273.90, stop $266.97, target
        # $275.42 = R/R 0.2 — terrible asymmetry). If the target is too
        # close, stretch it to 2x risk; don't loosen the stop.
        if action == "buy":
            _risk = current_price - stop_loss_price
            _reward = take_profit_price - current_price
            if _risk > 0 and _reward < 2.0 * _risk:
                new_tp = current_price + 2.0 * _risk
                _msg = (
                    f"R/R STRETCH: {symbol} entry=${current_price:.4f} "
                    f"stop=${stop_loss_price:.4f} (risk=${_risk:.4f}) "
                    f"target=${take_profit_price:.4f} (reward=${_reward:.4f}, "
                    f"R/R={_reward/_risk:.2f}) — stretching target to "
                    f"${new_tp:.4f} for 2:1 minimum."
                )
                # Crypto strategies routinely emit target = mean (small reward
                # vs ATR-sized stop). The stretch is expected, not anomalous —
                # log at INFO so the file isn't full of false WARNINGs.
                if self._is_crypto_symbol(symbol):
                    log.info(_msg)
                else:
                    log.warning(_msg)
                take_profit_price = new_tp

        # --- Broker Execution ---
        # IBKR is the sole execution broker. No fallback chain.
        order = None
        executed_via = None

        # Mark symbol as pending to block concurrent orders from webhooks/other threads
        if action == "buy":
            self._pending_orders.add(symbol)

        # Outside-RTH flag: source of truth is the wall clock, not the bot's
        # _in_premarket / _in_postmarket state flags. Those flags only cover
        # 04:00–20:00 ET (the configured pre/post-market windows) and are False
        # overnight (20:00–04:00) and on weekends — so a manual /api/signal at
        # 23:00 ET would have built a regular MARKET order, which IBKR rejects
        # off-hours. With wall-clock determination, the broker takes the
        # extended-hours path (aggressive LIMIT + outsideRth + DAY) any time
        # we're outside 09:30–16:00 ET on a weekday. Overnight, IBKR holds the
        # order as PreSubmitted; the place_order timeout handler already
        # surfaces that as "deferred", not a failure. Result: manual trades
        # work any time the bot is up.
        now_et = datetime.now(self.tz)
        rth_start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        rth_end = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        in_rth = (now_et.weekday() < 5) and (rth_start <= now_et <= rth_end)
        outside_rth = not in_rth

        # PRE-ORDER SLIPPAGE CHECK: reject stale signals BEFORE placing the order.
        # Directional, mirroring risk_manager.Rule 6 with even fresher data
        # (current_price is just-fetched from broker streaming, not the signal's
        # stamped market_price). Chase UP gets wide cap (trend strengthened);
        # chase DOWN gets tight cap (bullish setup broke). Post-fill
        # max_slippage_pct=0.008 is a separate, untouched check that protects
        # realized R:R on the actual fill.
        if action == "buy":
            signal_price = signal.get("price", 0)
            if signal_price > 0 and current_price > 0:
                signed_drift = (current_price - signal_price) / signal_price
                if outside_rth:
                    max_up = self.config.risk_config.get("max_signal_deviation_pct_extended", 0.12)
                    max_down = 0.05
                else:
                    max_up = self.config.risk_config.get("max_signal_deviation_pct", 0.05)
                    max_down = 0.03
                if signed_drift > max_up:
                    log.warning(
                        f"PRE-ORDER REJECT (chase up): {symbol} signal ${signal_price:.2f} → "
                        f"live ${current_price:.2f} = {signed_drift:+.1%} "
                        f"(max +{max_up:.0%}, {'EXT' if outside_rth else 'RTH'})"
                    )
                    self._pending_orders.discard(symbol)
                    return
                if signed_drift < -max_down:
                    log.warning(
                        f"PRE-ORDER REJECT (setup broke): {symbol} signal ${signal_price:.2f} → "
                        f"live ${current_price:.2f} = {signed_drift:+.1%} "
                        f"(max -{max_down:.0%})"
                    )
                    self._pending_orders.discard(symbol)
                    return

        # PRE-ORDER SPREAD CHECK: reject illiquid names BEFORE placing the order.
        # Catches wide bid-ask spreads (e.g. $1.40 bid / $1.60 ask = 13% spread)
        # that guarantee slippage on MARKET orders. Session- and price-tier-aware:
        # extended hours and sub-$5 names get wider headroom because their normal
        # spreads are structurally wider — a 2% cap rejects the whole low-float runner
        # universe pre-market.
        # Runs for every equity entry regardless of execution routing: the quote
        # comes from the IBKR data broker, which is connected even when a
        # tp_broker mirror handles the fill. Previously gated on `not self.tp_broker`,
        # so a thin name (FBYD, 2026-05-19) skipped the gate, filled MARKET, and
        # tripped the post-fill slippage_reject for a realized −$43 loss instead.
        # Crypto routes through tp_crypto_broker and has no IBKR quote — skip it.
        if (action == "buy" and not self._is_crypto_symbol(symbol)
                and self.broker and self.broker.is_connected()):
            max_spread_pct = self.config.risk_config.get("max_spread_pct", 0.02)
            if outside_rth:
                max_spread_pct *= 2.0      # extended hours: double the spread budget
            if current_price and current_price < 5:
                max_spread_pct *= 1.5      # sub-$5 names: 50% wider allowed
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

        # === PRE-FILL STALENESS GATE (equity buys) ===
        # A momentum signal can sit in the execution queue for 30-60s (serial
        # processing, Claude pre-trade calls) before the order is placed. On a
        # fast runner the price drifts well past the signal price in that
        # window — the MARKET order fills high and the post-fill
        # slippage_reject round-trips the position at a loss (RGTX/RGTU/RL,
        # 2026-05-21: signal $23.40 → order placed 53s later → fill $25.06).
        # Catch it BEFORE crossing the spread: if the live ask has already
        # drifted past the signal price by more than the slippage budget, the
        # entry's R:R is already invalid — skip instead of buy-then-dump.
        if (action == "buy" and not self._is_crypto_symbol(symbol)
                and self.broker and self.broker.is_connected()):
            signal_price = signal.get("price", 0)
            max_drift = self.config.risk_config.get("max_slippage_pct", 0.008)
            if outside_rth:
                max_drift *= 2.0           # extended hours: wider budget
            quote = self.broker.get_live_price(symbol) if hasattr(self.broker, 'get_live_price') else None
            live_ref = None
            if quote:
                if quote.get("ask") and quote["ask"] > 0:
                    live_ref = quote["ask"]
                elif quote.get("bid") and quote.get("ask"):
                    live_ref = (quote["bid"] + quote["ask"]) / 2
            if signal_price > 0 and live_ref and live_ref > 0:
                drift = (live_ref - signal_price) / signal_price
                if drift > max_drift:
                    log.warning(
                        f"STALE SIGNAL SKIP: {symbol} live ask ${live_ref:.2f} "
                        f"has drifted {drift:.1%} above signal ${signal_price:.2f} "
                        f"(max {max_drift:.1%}) — entry R:R already invalid, "
                        f"skipping order."
                    )
                    self._pending_orders.discard(symbol)
                    return

        # === EXECUTION ROUTING: TradersPost-primary ===
        # TradersPost webhook is the PRIMARY execution path. It is a plain
        # HTTPS POST — it never touches ib_async / nest_asyncio, so it is
        # structurally immune to the asyncio contextvars re-entry crash that
        # repeatedly wedged the bot when IBKR placed orders directly.
        # IBKR is data-only here. The direct IBKR order path is kept ONLY as
        # a legacy fallback for setups with no TradersPost webhook configured.
        order = None
        executed_via = None

        # Asset-class routing:
        #   - crypto → tp_crypto_broker (separate TradersPost subscription, crypto venues)
        #   - stock + tp_broker → tp_broker (legacy primary path)
        #   - stock + no tp_broker → IBKR-direct (current default)
        is_crypto = self._is_crypto_symbol(symbol)
        active_tp = self.tp_crypto_broker if (is_crypto and self.tp_crypto_broker) else self.tp_broker

        if active_tp:
            log.info(
                f"Executing {symbol} via TradersPost"
                f"{' (CRYPTO)' if (is_crypto and self.tp_crypto_broker) else ''}"
                f"{'  [OUTSIDE RTH]' if outside_rth else ''}..."
            )
            if action == "buy":
                # Cap the take_profit sent to TradersPost at a sane bracket
                # distance — the bot's settings.yaml take_profit_pct=999.0
                # produces $79M-style numbers because the bot manages exits
                # locally via trailing stops. But TradersPost / its downstream
                # brokers reject OTO (single-leg) orders, so we MUST send a
                # real bracket pair. Use the bot's computed SL as one leg and
                # a 20% TP cap as the other — the trailing stop will exit long
                # before the 20% TP triggers, but the bracket is now valid.
                tp_for_broker = take_profit_price
                if take_profit_price and current_price and take_profit_price > current_price * 1.20:
                    tp_for_broker = round(current_price * 1.20, 2)
                order = active_tp.place_order(
                    symbol=symbol,
                    action="buy",
                    quantity=qty,
                    order_type="MARKET",
                    limit_price=current_price,
                    stop_loss=stop_loss_price,
                    take_profit=tp_for_broker,
                )
            else:
                log.error(f"UNEXPECTED: Non-buy action '{action}' reached execution for {symbol}")
                self._pending_orders.discard(symbol)
                return
            if order:
                executed_via = "TradersPost"
                # No broker-side bracket via webhook — the bot's
                # _monitor_positions loop manages stops/targets locally.
                log.info(
                    f"TradersPost SUBMITTED: {symbol} qty={qty}. "
                    f"SL ${stop_loss_price:.2f} / TP ${take_profit_price:.2f} "
                    f"managed by bot (no broker-side bracket)."
                )
            else:
                log.error(f"TradersPost webhook FAILED for {symbol} — no execution.")

        elif self.broker and self.broker.is_connected():
            # Legacy path: no TradersPost webhook configured. Places directly
            # at IBKR via ib_async — carries the contextvars crash risk.
            # Configure TRADERSPOST_WEBHOOK_URL to avoid it.
            log.info(f"Executing {symbol} via IBKR{'  [OUTSIDE RTH]' if outside_rth else ''}...")
            # MIDPRICE order routing: for strategies that aren't time-critical
            # (swing/accumulation entries, mean-reversion), use IBKR's MIDPRICE
            # algo to fill at the bid-ask midpoint and capture half the spread
            # as price improvement. Capped at +0.5% above live so worst case
            # = same as a tight LIMIT.
            #
            # Speed-critical strategies (momentum runners, gap chases, scalps)
            # stay on MARKET — for them, a missed fill costs more than the
            # half-spread saved. Outside RTH always uses MARKET because
            # MIDPRICE has no midpoint when the book is closed; ibkr.py
            # converts that to an aggressive LIMIT internally.
            midprice_strats = {
                "daily_trend_rider", "mean_reversion", "prebreakout", "smc_forever",
            }
            use_midprice = (
                action == "buy"
                and not outside_rth
                and strategy in midprice_strats
                and current_price and current_price > 0
            )
            # Signals that explicitly request a server-side bracket (low_float_catalyst)
            # need a LIMIT entry — broker's bracket attachment only fires for
            # LIMIT/MIDPRICE in ibkr.py:426. Use a 2% above-market cap so fast
            # micro-cap runners still fill, while keeping fill price bounded.
            #
            # 2026-05-30: default to TRUE for equity signals so the stop+TP
            # live on IBKR's side instead of in bot memory. Survives bot/gateway
            # disruptions — directly addresses the DELL incident where a
            # wedged IBKR worker left the position uncovered for hours despite
            # the bot's in-memory stop. Crypto continues to default FALSE
            # because crypto execution goes through the TradersPost webhook
            # path which doesn't support IBKR-style broker brackets. Strategies
            # can still override explicitly (crypto_runner sets False, an
            # equity strategy could opt out by setting False).
            # Master kill switch: `risk.use_server_bracket_equity_default`
            # in settings.yaml (default true) flips the default back to the
            # old behavior if the bracket lifecycle causes issues.
            bracket_default_enabled = self.config.risk_config.get(
                "use_server_bracket_equity_default", True
            )
            if bracket_default_enabled and not self._is_crypto_symbol(symbol):
                default_bracket = True
            else:
                default_bracket = False
            use_server_bracket = bool(
                signal.get("use_server_bracket", default_bracket)
                and current_price and current_price > 0
                and stop_loss_price and take_profit_price
            )
            # Toggle: force MARKET-parent bracket on equity. Resolves IBKR
            # Error 2152 "PendingSubmit → cancelled" when account doesn't
            # have top-of-book market-data subscriptions for NASDAQ/NYSE/
            # BATS/ARCA. Live observation 2026-06-05: every equity bracket
            # order all morning timed out at 15s in PendingSubmit, blocking
            # every fill on SNBR/BGMS/ETHD-class runners.
            # Trade-off: worse fills (no LIMIT price guarantee), but actually
            # fills. Per-fill slippage is logged at the broker layer so the
            # cost can be measured. Disable via setting to False once IBKR
            # market-data permissions are restored.
            force_market_bracket = self.config.risk_config.get(
                "use_market_orders_on_bracket", False
            )
            if use_server_bracket and not use_midprice:
                if force_market_bracket:
                    entry_order_type = "MARKET"
                else:
                    entry_order_type = "LIMIT"
                entry_limit_price = round(current_price * 1.02, 2)
            else:
                entry_order_type = "MIDPRICE" if use_midprice else "MARKET"
                entry_limit_price = round(current_price * 1.005, 2) if use_midprice else None

            if action == "buy":
                order = self.broker.place_order(
                    symbol=symbol,
                    action="BUY",
                    quantity=qty,
                    order_type=entry_order_type,
                    limit_price=entry_limit_price,
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
                log.warning(f"IBKR order failed for {symbol}.")

        # If nothing executed, do NOT create a phantom position
        if not order:
            log.error(
                f"NO EXECUTION PATH AVAILABLE — cannot execute {action.upper()} "
                f"{symbol}. Set TRADERSPOST_WEBHOOK_URL in .env (primary execution "
                f"path), or ensure IBKR is connected (legacy fallback)."
            )
            self._pending_orders.discard(symbol)
            return

        # DEFERRED: IBKR accepted the order outside RTH but queued it for the next
        # regular session (PreSubmitted / Warning 399). The broker is holding it —
        # don't track a position yet, the fill will arrive via streaming when the
        # venue opens. Without this branch the engine would book a phantom
        # position for the requested quantity even though zero shares filled.
        if order.get("deferred"):
            log.info(
                f"Order DEFERRED at IBKR: {action.upper()} {qty} {symbol} queued for "
                f"next regular session (status={order.get('status', 'PreSubmitted')}). "
                f"Not tracking position; engine will pick up the fill via streaming."
            )
            # Stash on the signal dict so handle_manual_signal can distinguish
            # "queued at broker" from "blocked by gate" in its API response.
            # Signal dicts flow by reference, so the caller sees the mutation.
            signal["_deferred"] = True
            signal["_deferred_order_id"] = order.get("order_id")
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
        # Directional for BUY — only fills ABOVE the reference count as slippage (chasing
        # up). Fills BELOW are a discount, not an offense; the abs() version was
        # auto-closing winning fills (RKLB 2026-05-22: signal $135.73 → fill $134.49,
        # −0.91% treated as +0.91% reject for guaranteed −$7).
        if order.get("avg_fill_price") and action == "buy":
            if outside_rth:
                max_slippage = self.config.risk_config.get("max_slippage_pct_extended", 0.015)
            else:
                max_slippage = self.config.risk_config.get("max_slippage_pct", 0.008)
            signal_price = signal.get("price", 0)
            # Signed drift: positive = fill above reference = adverse for a BUY
            live_drift = ((actual_price - current_price) / current_price) if current_price > 0 else 0
            signal_drift = ((actual_price - signal_price) / signal_price) if signal_price > 0 else 0
            worst_slippage = max(live_drift, signal_drift)
            # Record adverse slippage on every BUY fill (not just rejects)
            # so `_compute_slippage_mult` can dampen the next entry's size
            # when steady-drag builds up. The reject + warn paths below
            # are unchanged.
            self._record_slippage(strategy, worst_slippage)

            if worst_slippage > max_slippage:
                slippage_source = "signal" if signal_drift > live_drift else "market"
                log.warning(
                    f"SLIPPAGE REJECT: {symbol} chased {worst_slippage:+.1%} above "
                    f"{slippage_source} (max {max_slippage:.1%}, {'EXT' if outside_rth else 'RTH'}) — "
                    f"closing position immediately | "
                    f"Signal ${signal_price:.2f} → Live ${current_price:.2f} → Fill ${actual_price:.2f}"
                )
                self.notifier.risk_alert(
                    f"Slippage reject: {symbol} filled ${actual_price:.2f} "
                    f"(signal ${signal_price:.2f}, slippage {worst_slippage:+.1%}). "
                    f"Closing immediately."
                )
                # Schedule immediate close (can't close inline, position not yet tracked)
                if not hasattr(self, '_slippage_close_queue'):
                    self._slippage_close_queue = []
                self._slippage_close_queue.append(symbol)
            elif worst_slippage > max_slippage * 0.5:
                log.warning(
                    f"SLIPPAGE WARNING: {symbol} slippage {worst_slippage:+.1%} "
                    f"(threshold {max_slippage:.1%}, {'EXT' if outside_rth else 'RTH'}) | "
                    f"Signal ${signal_price:.2f} → Fill ${actual_price:.2f}"
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
                    self.config.risk_config.get("penny_runner_trailing_stop_pct", 0.08)
                    if _is_penny_runner
                    else self.config.risk_config.get("trailing_stop_pct", 0.02)
                ),
                "is_penny_runner": _is_penny_runner,
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

        # Persist position immediately after add so a crash/restart before the
        # next 3s scalp-monitor tick doesn't lose this entry. (Observed live
        # 2026-05-17: SUI/ICP entries at 18:09-18:46 EDT never landed in
        # positions_state.json before the 19:40 restart, creating $58K of
        # untracked broker-side orphans.)
        try:
            self._persist_positions()
        except Exception as e:
            log.debug(f"persist-on-entry failed for {symbol}: {e}")

        # Bump the originating strategy's daily-trade counter. Counter only
        # increments AFTER a successful entry — previously strategies bumped
        # this in generate_signals, which burned the day's slots even when
        # risk_manager rejected the signal (PIII pattern: 3 rejected signals
        # silently killed the strategy for the rest of the day).
        strat_obj = self.strategies.get(strategy) if strategy else None
        if strat_obj and hasattr(strat_obj, "record_entry_filled"):
            try:
                strat_obj.record_entry_filled(symbol)
            except Exception as e:
                log.debug(f"record_entry_filled failed for {strategy}/{symbol}: {e}")

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

        # Mirror the entry to TradersPost (visualization only — never an
        # execution path). The mirror instance is wired to a separate
        # webhook (TRADERSPOST_MIRROR_WEBHOOK_URL) whose subscription should
        # use TradersPost's built-in Paper Trading broker, so this can never
        # double-fill on IBKR.
        # Crypto entries already went to tp_crypto_broker (the dedicated
        # crypto subscription) — mirroring them to the IBKR mirror webhook
        # cross-contaminates the IBKR/equity book with crypto positions.
        if self.tp_mirror and not self._is_crypto_symbol(symbol):
            try:
                self.tp_mirror.notify_trade({
                    "symbol": symbol,
                    "action": action,
                    "quantity": qty,
                    "price": current_price,
                    "strategy": strategy,
                    "source": "mirror_entry",
                })
            except Exception as e:
                log.debug(f"TradersPost mirror (entry) failed for {symbol}: {e}")

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
        # exceeded max_slippage_pct (R:R is ruined, better to exit immediately).
        #
        # FLOOR (added 2026-06-05): if the live price is already AT/BELOW the
        # position's stop_loss, the bracket's server-side stop_loss is about
        # to fire anyway. Sending a duplicate MARKET SELL via slippage_reject
        # just races at the same bad price — or worse, fills before the
        # bracket and locks in a price WORSE than the strategy's predefined
        # stop. HIBS on 2026-06-05: entry $25.25, stop_loss $23.12, but
        # slippage_reject MARKET SELL filled at $22.73 (-$0.39 below stop
        # = -$58 extra damage on top of the planned -$317 max). Total loss
        # -$375 instead of the strategy-bounded -$317.
        #
        # Fix: skip the slippage_reject MARKET when price has already crashed
        # past the stop. Let the bracket stop_loss handle it at its predefined
        # trigger price.
        if hasattr(self, '_slippage_close_queue') and self._slippage_close_queue:
            close_syms = list(self._slippage_close_queue)
            self._slippage_close_queue.clear()
            for close_sym in close_syms:
                if close_sym not in self.positions:
                    continue
                pos = self.positions[close_sym]
                stop_loss = float(pos.get("stop_loss", 0) or 0)
                current_price = None
                try:
                    quote = self.market_data.get_quote(close_sym) if self.market_data else None
                    if quote:
                        current_price = quote.get("price") or 0
                except Exception:
                    pass
                if (
                    stop_loss > 0
                    and current_price
                    and float(current_price) <= stop_loss
                ):
                    log.warning(
                        f"SLIPPAGE EXIT FLOOR: {close_sym} live "
                        f"${float(current_price):.2f} already at/below stop_loss "
                        f"${stop_loss:.2f} — skipping slippage_reject MARKET SELL. "
                        f"Bracket stop_loss will handle exit at the predefined level."
                    )
                    continue
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

        # Double-close guard: prevent concurrent close attempts.
        # The check-then-act below MUST be inside the lock — otherwise two
        # threads can both pass `symbol in self._closing_in_progress` (False),
        # then both `self._closing_in_progress.add(symbol)`, then both
        # proceed to `_close_position_inner` and both append to
        # trade_history. That race is the source of the FBYD triple-record
        # pattern (HANDOFF session 9; PR #216 patched at the persist layer,
        # this fixes it at the source).
        with self._positions_lock:
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

        # Cancel any broker-side stop order to avoid orphan orders.
        # Skipped under TradersPost-primary: no IBKR-side stops exist there,
        # and cancel_order is an ib_async coroutine (contextvars risk).
        if not self.tp_broker and pos.get("broker_stop_order_id") and self.broker and self.broker.is_connected():
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

        # Verify actual broker quantity before closing to prevent accidental
        # shorts. Skipped under TradersPost-primary — get_positions() is an
        # ib_async coroutine and must not run on the exit path; TradersPost
        # is the execution broker and the bot's own tracking is the record.
        if not self.tp_broker and self.broker and self.broker.is_connected():
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

        # === CLOSE ROUTING: TradersPost-primary ===
        # Exits go through the TradersPost webhook (plain HTTPS, no ib_async,
        # no contextvars risk). TradersPost bypasses its own rate limits for
        # exits. IBKR direct-close is kept only as a legacy fallback when no
        # webhook is configured.
        order = None
        partial_fill_remaining = 0
        # Exit price for P&L: defaults to the current_price reference, but the
        # IBKR-direct close path below overrides it with the real avg_fill_price.
        exit_price = current_price
        # Wall-clock outside-RTH: same logic as the entry path. Exits must also
        # work overnight (close a position bought in pre-market at 23:00 ET).
        # Without this, a manual SELL after 20:00 builds a plain MARKET order
        # that IBKR rejects off-hours.
        _now_et = datetime.now(self.tz)
        _rth_s = _now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        _rth_e = _now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        outside_rth = not ((_now_et.weekday() < 5) and (_rth_s <= _now_et <= _rth_e))

        # Asset-class routing on exit: crypto → tp_crypto_broker, stock → tp_broker.
        # Symmetric with the entry path so a position opened on the crypto
        # subscription gets closed there too (NOT sent to the equity webhook).
        _is_crypto_exit = self._is_crypto_symbol(symbol)
        _active_tp = self.tp_crypto_broker if (_is_crypto_exit and self.tp_crypto_broker) else self.tp_broker

        if _active_tp:
            order = _active_tp.place_order(
                symbol=symbol,
                action=action,
                quantity=close_qty,
                order_type="MARKET",
                limit_price=current_price,
            )
            if order:
                close_broker = "TradersPost"
            else:
                log.error(
                    f"TradersPost close webhook FAILED for {symbol} — "
                    f"position stays tracked for retry next cycle."
                )

        elif self.broker and self.broker.is_connected():
            # Legacy IBKR-direct close (no TradersPost webhook configured).
            # Cancel any existing broker-side stop (bracket leg) before closing
            # to avoid the stop triggering after we've already sold.
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
                # Use the actual broker fill price for P&L, not the stale
                # current_price reference. On a fast/thin name the market-data
                # quote can be seconds stale (e.g. RGTX 2026-05-21: reference
                # $23.84 vs real fill $25.05 — recorded a fictional −$61 on a
                # round-trip that really cost ~$0.50). avg_fill_price is the
                # cash truth; fall back to current_price only if absent.
                if order.get("avg_fill_price"):
                    exit_price = order["avg_fill_price"]
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
            log.error(
                f"NO EXECUTION PATH for close of {symbol} — TradersPost webhook "
                f"not configured and IBKR not connected. Position stays tracked "
                f"for retry next cycle."
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

        # Calculate P&L only on the shares actually closed, against the real
        # exit fill price (see exit_price assignment in the IBKR close block).
        if pos["direction"] == "long":
            pnl = (exit_price - pos["entry_price"]) * closed_qty
        else:
            pnl = (pos["entry_price"] - exit_price) * closed_qty

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

        # Mirror the close to TradersPost (visualization only).
        # Crypto exits already went through tp_crypto_broker; mirroring them
        # to the IBKR/equity mirror webhook would close a phantom position
        # there or worse, route the exit to the IBKR-attached subscription.
        if self.tp_mirror and not self._is_crypto_symbol(symbol):
            try:
                self.tp_mirror.notify_trade({
                    "symbol": symbol,
                    "action": action,
                    "quantity": closed_qty,
                    "price": exit_price,
                    "source": "mirror_exit",
                })
            except Exception as e:
                log.debug(f"TradersPost mirror (close) failed for {symbol}: {e}")

        pnl_pct = pnl / (pos["entry_price"] * closed_qty) if pos["entry_price"] * closed_qty > 0 else 0
        hold_time = (datetime.now(self.tz) - pos["entry_time"]) if "entry_time" in pos else None

        # Rich exit notification
        self.notifier.trade_exit(
            symbol=symbol,
            direction=pos["direction"],
            qty=closed_qty,
            entry_price=pos["entry_price"],
            exit_price=exit_price,
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
            "exit_price": exit_price,
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

        # Persist immediately after mutating positions. A crash between here
        # and the next 3-second cycle would resurrect the pre-close quantity
        # on restart and re-close already-filled shares (duplicate exit
        # rejections at the broker).
        try:
            self._persist_positions()
        except Exception as e:
            log.debug(f"persist-on-close failed for {symbol}: {e}")

        # Clean up tick-by-tick subscription for closed position (only on full close)
        if partial_fill_remaining == 0 and self.broker and hasattr(self.broker, 'unsubscribe_tick_by_tick'):
            try:
                self.broker.unsubscribe_tick_by_tick([symbol])
            except Exception:
                pass

        # Record exit cooldown — prevents broker sync from re-adding this
        # position during settlement delay (causes duplicate exit rejections)
        if partial_fill_remaining == 0:
            _now = datetime.now(self.tz)
            self._recently_closed[symbol] = _now
            # Record reason + P&L for the equity re-entry guard.
            self._recent_close_info[symbol] = {
                "time": _now,
                "reason": reason_type,
                "pnl": pnl,
            }

    def _partial_close(self, symbol, qty_to_close, target_idx, target):
        """Close part of a position (profit taking)."""
        pos = self.positions.get(symbol)
        if not pos or qty_to_close <= 0:
            return

        # Double-close guard: prevent concurrent close attempts. Same
        # check-then-act atomicity issue as _close_position — must be
        # inside the lock or two threads race the partial through twice.
        with self._positions_lock:
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

        # Verify actual broker qty before partial close to prevent
        # overselling. Skipped under TradersPost-primary — get_positions()
        # is an ib_async coroutine and must not run on the exit path.
        if not self.tp_broker and self.broker and self.broker.is_connected():
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

        # Execute the partial close — TradersPost-primary, IBKR legacy fallback
        order = None
        # Wall-clock outside-RTH (same logic as entry / full-close paths).
        _now_et = datetime.now(self.tz)
        _rth_s = _now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        _rth_e = _now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        outside_rth = not ((_now_et.weekday() < 5) and (_rth_s <= _now_et <= _rth_e))
        # Asset-class routing (crypto → tp_crypto_broker, stock → tp_broker).
        _is_crypto_pc = self._is_crypto_symbol(symbol)
        _active_tp_pc = self.tp_crypto_broker if (_is_crypto_pc and self.tp_crypto_broker) else self.tp_broker
        if _active_tp_pc:
            order = _active_tp_pc.place_order(
                symbol=symbol, action=action,
                quantity=qty_to_close, order_type="MARKET",
                limit_price=current_price,
            )
            if order:
                close_broker = "TradersPost"
        elif self.broker and self.broker.is_connected():
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
            # NOTE: this is an observability flag only — size is not reduced
            # automatically here. Sizing already adapts via the Kelly + drawdown
            # multipliers in position_sizer.py, and the daily soft-stop in
            # _check_daily_loss_soft_stop pauses entries at -2% daily P&L.
            log.warning(
                f"PSYCHOLOGY FLAG: {state['consecutive_losses']} consecutive losses — "
                f"review strategy mix; consider manual pause"
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

    def _rebuild_performance_stats_from_history(self):
        """Replay every closed trade in trade_history through
        `_update_performance_stats` so the in-memory tracker reflects
        the full historical sample, not just trades closed since the
        last bot start. Called once during boot after persisted history
        loads. Cheap: O(n) over a 500-trade ceiling."""
        # Reset to baseline before replay so calling this twice (e.g. test
        # harness) doesn't double-count.
        self.performance_stats = {
            "total_trades": 0, "wins": 0, "losses": 0, "breakeven": 0,
            "total_profit": 0.0, "total_loss": 0.0,
            "largest_win": 0.0, "largest_loss": 0.0,
            "current_streak": 0, "best_streak": 0, "worst_streak": 0,
        }
        for trade in self.trade_history:
            try:
                self._update_performance_stats(float(trade.get("pnl", 0) or 0))
            except (TypeError, ValueError):
                continue
        log.info(
            f"PERF STATS: rebuilt from history — total={self.performance_stats['total_trades']} "
            f"W={self.performance_stats['wins']} L={self.performance_stats['losses']} "
            f"net=${self.performance_stats['total_profit'] - self.performance_stats['total_loss']:+,.2f}"
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

        # Cancel all pending IBKR orders first (bracket stop/target orders).
        # Skipped under TradersPost-primary — there are no IBKR-side orders,
        # and cancel_all_orders is an ib_async coroutine (contextvars risk).
        if not self.tp_broker and self.broker and self.broker.is_connected() and hasattr(self.broker, 'cancel_all_orders'):
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
        # TradersPost-primary: IBKR is data-only — no broker-side stop orders.
        # Both cancel_order and place_order below are ib_async coroutines and
        # must not run on this path. The bot's _monitor_positions loop manages
        # trailing stops locally instead.
        if self.tp_broker:
            return False

        # Crypto symbols don't exist on IBKR — they're traded on the crypto
        # broker and protected by the in-process trailing stop only.
        if self._is_crypto_symbol(symbol):
            return False

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
                # Crypto positions live on the crypto broker, not IBKR, so a
                # broker-side stop here is impossible. The in-process trailing
                # stop in _monitor_positions is the only protection.
                if self._is_crypto_symbol(symbol):
                    continue

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

        # Context for Claude. Two corrections vs. naive accounting:
        # 1. |P&L| < $1 → scratch, not a loss. Crypto wash exits at $0.00 were
        #    otherwise inflating the "loss" count and locking out fresh entries.
        # 2. Compare like-with-like: judge an equity candidate against equity
        #    history, crypto against crypto. Mixing them lets a crypto losing
        #    streak veto every equity entry (and vice versa).
        is_crypto = self._is_crypto_symbol(symbol)
        asset_pool = [
            t for t in self.trade_history
            if self._is_crypto_symbol(t.get("symbol", "")) == is_crypto
        ]
        recent_trades = asset_pool[-15:]

        def _outcome(t):
            pnl = t.get("pnl", 0)
            if abs(pnl) < 1.0:
                return "scratch"
            return "win" if pnl > 0 else "loss"

        outcomes = [_outcome(t) for t in recent_trades]
        wins = outcomes.count("win")
        losses = outcomes.count("loss")
        scratches = outcomes.count("scratch")
        decisive = wins + losses
        win_rate = (wins / decisive * 100) if decisive else 0

        strat_trades = [t for t in recent_trades if t.get("strategy") == strategy]
        strat_outcomes = [_outcome(t) for t in strat_trades]
        strat_wins = strat_outcomes.count("win")
        strat_losses = strat_outcomes.count("loss")
        strat_scratches = strat_outcomes.count("scratch")
        strat_decisive = strat_wins + strat_losses
        strat_wr = (strat_wins / strat_decisive * 100) if strat_decisive else 0

        open_count = len(self.positions)
        regime = getattr(self, 'current_regime', 'unknown')
        score = signal.get("score", 0)
        rvol = signal.get("rvol", 0)

        # --- Extra decision context (cheap to compute, big for Claude's call) ---
        # R:R: most important single number for "is this setup worth taking?"
        price = signal.get("price", 0) or 0
        stop = signal.get("stop_loss", 0) or 0
        tp = signal.get("take_profit", 0) or 0
        risk_pct = ((price - stop) / price) if (price > 0 and stop > 0 and stop < price) else 0
        reward_pct = ((tp - price) / price) if (price > 0 and tp > price) else 0
        rr = (reward_pct / risk_pct) if risk_pct > 0 else 0

        # Spread (bps). Pulled from the IBKR streaming cache — no extra
        # network call. None when streaming hasn't populated bid/ask yet.
        spread_bps = None
        if self.broker and hasattr(self.broker, 'get_live_price'):
            try:
                q = self.broker.get_live_price(symbol)
                if q and q.get("bid") and q.get("ask") and q["ask"] > q["bid"]:
                    mid = (q["bid"] + q["ask"]) / 2
                    if mid > 0:
                        spread_bps = round((q["ask"] - q["bid"]) / mid * 10000)
            except Exception:
                pass

        # Time-into-session
        now_local = datetime.now(self.tz)
        sched = self.config.schedule_config
        try:
            h_open, m_open = map(int, sched.get("market_open", "09:30").split(":"))
            market_open_dt = now_local.replace(hour=h_open, minute=m_open,
                                               second=0, microsecond=0)
            mins_in = int((now_local - market_open_dt).total_seconds() / 60)
        except Exception:
            mins_in = 0
        if mins_in < 0:
            session_loc = f"premarket ({-mins_in}m before open)"
        elif mins_in < 30:
            session_loc = f"opening-30m ({mins_in}m in)"
        elif mins_in < 360:
            session_loc = f"midday ({mins_in}m in)"
        elif mins_in < 390:
            session_loc = f"power-hour ({390 - mins_in}m to close)"
        else:
            session_loc = f"after-hours ({mins_in - 390}m past close)"

        # Day P&L — discourages AGGRESSIVE sizing on losing days (revenge trades)
        day_pnl_abs = float(getattr(self, 'daily_pnl', 0) or 0)
        sod = float(getattr(self, 'start_of_day_balance', 0) or 0)
        day_pnl_pct = (day_pnl_abs / sod * 100) if sod > 0 else 0

        # Per-symbol recent record — symbol-specific behaviour often
        # dominates strategy-wide stats (cf. NEAR-USD vs DOT-USD).
        sym_trades = [t for t in self.trade_history if t.get("symbol") == symbol][-3:]

        def _short_rec(t):
            p = t.get("pnl", 0) or 0
            tag = "W" if p > 0.5 else ("L" if p < -0.5 else "S")
            return f"{tag}{p:+.0f}"
        sym_rec = (", ".join(_short_rec(t) for t in sym_trades)
                   if sym_trades else "no prior trades")

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

        spread_line = f"Spread: {spread_bps}bps" if spread_bps is not None else "Spread: n/a"
        rr_line = (f"R:R = {rr:.2f} (risk {risk_pct*100:.2f}%, reward {reward_pct*100:.2f}%)"
                   if risk_pct > 0 else "R:R = n/a")

        prompt = (
            f"BUY {symbol} via {strategy} @ ${signal.get('price', 0):.2f}\n"
            f"Stop: ${signal.get('stop_loss', 0):.2f} | Target: ${signal.get('take_profit', 0):.2f}\n"
            f"{rr_line} | {spread_line}\n"
            f"Score: {score} | RVOL: {rvol:.1f}x | Confidence: {signal.get('confidence', 0):.2f}\n"
            f"Session: {session_loc} | Day P&L: ${day_pnl_abs:+.0f} ({day_pnl_pct:+.2f}%)\n"
            f"Recent {symbol}: {sym_rec}\n"
            f"Recent ({'crypto' if is_crypto else 'equity'} only): "
            f"{wins}W/{losses}L/{scratches}scratch "
            f"({win_rate:.0f}% on {decisive} decisive)\n"
            f"Strategy '{strategy}': {strat_wins}W/{strat_losses}L/"
            f"{strat_scratches}scratch ({strat_wr:.0f}% on {strat_decisive} decisive)"
            f"{f' | LEARNED BOOST x{boosted}' if boosted else ''}\n"
            f"Open positions: {open_count}/10 | Regime: {regime}"
            f"{trend_rider_block}\n\n"
            f"Rules (apply in order, first match wins):\n"
            f"- SKIP if: strategy win rate <25% on 20+ DECISIVE trades, or bad regime, "
            f"or R:R<1.0, or spread>50bps on equity. Under 20 decisive trades the sample is "
            f"too small to gate — judge the setup on its merits instead of auto-skipping.\n"
            f"- REDUCE if: >7 open positions, or (strategy win rate <40% on 20+ DECISIVE trades), "
            f"or day P&L <-1% (don't double down on a losing day).\n"
            f"- AGGRESSIVE (1.5x size) if: score>=80 AND RVOL>=5 AND win_rate>=60% on 20+ DECISIVE "
            f"trades AND R:R>=2.0 AND day P&L >=-0.5%. (LEARNED BOOST x score>=70 also qualifies.)\n"
            f"- TAKE otherwise if the setup is solid.\n"
            f"- Scratches (|P&L|<$1) are noise, NOT losses — don't count them as evidence of failure.\n"
            f"- 'Recent' and 'Strategy' stats above only cover "
            f"{('crypto' if is_crypto else 'equity')} trades; do not extrapolate from the other asset class.\n\n"
            f"Respond with a brief reason (≤30 words), then END with a line:\n"
            f"DECISION: <one of TAKE, SKIP, REDUCE, AGGRESSIVE>"
        )

        try:
            response = self.ai_insights._call_claude(prompt)
            if not response:
                return {}

            decision = self._parse_claude_decision(response)
            if decision is None:
                log.warning(
                    f"Claude pre-trade: no parseable decision for {symbol} "
                    f"({strategy}); response head: {response[:120]!r}"
                )
                return {}

            reason = response.strip()[:200]
            if decision == "SKIP":
                return {"skip": True, "reason": reason}
            if decision == "REDUCE":
                return {"reduce_size": True, "reason": reason}
            if decision == "AGGRESSIVE":
                return {"aggressive": True, "size_mult": 1.5, "reason": reason}
            return {}
        except Exception as e:
            log.debug(f"Claude pre-trade exception for {symbol}: {e}")
            return {}

    @staticmethod
    def _parse_claude_decision(text):
        """Robust decision extraction. Prefers an explicit `DECISION: X` line
        anywhere in the response; falls back to scanning for the four
        keywords as standalone words with conservative precedence
        (SKIP > REDUCE > AGGRESSIVE > TAKE) so a verbose reasoning prefix
        doesn't fail open the way `startswith` did.
        Returns one of {'TAKE','SKIP','REDUCE','AGGRESSIVE'} or None.
        """
        if not text:
            return None
        m = re.search(r"DECISION\s*:\s*(TAKE|SKIP|REDUCE|AGGRESSIVE)\b",
                      text, flags=re.IGNORECASE)
        if m:
            return m.group(1).upper()
        upper = text.upper()
        for keyword in ("SKIP", "REDUCE", "AGGRESSIVE", "TAKE"):
            if re.search(r"\b" + keyword + r"\b", upper):
                return keyword
        return None

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

                    # Tiered spread allowance + auto size-down for runners.
                    #
                    # The flat 2% rule was tuned for mid-cap equity. On the
                    # micro-cap runners we actually want (LGPS, TSAT, MASK
                    # class) the spread is naturally 3-8% — those names
                    # were blocked for HOURS on 2026-06-04, exactly when
                    # they were running. By price tier:
                    #   - $5+      : strict 2% (mid-cap, tight spreads)
                    #   - $2-$5    : up to 3% (small-cap)
                    #   - sub-$2   : up to 5% (micro-cap runners)
                    # Override via config: risk.max_spread_pct_tiers.
                    price_for_spread = float(signal.get("price", 0) or 0)
                    spread_tiers = self.config.risk_config.get(
                        "max_spread_pct_tiers",
                        {"lt_2": 0.05, "lt_5": 0.03, "default": 0.02},
                    )
                    strict_spread = float(spread_tiers.get("default", 0.02))
                    if price_for_spread > 0 and price_for_spread < 2.0:
                        max_spread = float(spread_tiers.get("lt_2", 0.05))
                    elif price_for_spread > 0 and price_for_spread < 5.0:
                        max_spread = float(spread_tiers.get("lt_5", 0.03))
                    else:
                        max_spread = strict_spread

                    if spread_pct > max_spread:
                        return False, (
                            f"wide spread {spread_pct*100:.1f}% "
                            f"(>{max_spread*100:.0f}% for ${price_for_spread:.2f})"
                        )

                    # Size-down when spread is above strict but within tier.
                    # Linear scale: at strict, mult=1.0; at tier ceiling,
                    # mult=0.5. Caps risk-per-trade on names with wider
                    # spreads so a 5% slippage cost on a low-float runner
                    # doesn't pair with a full-size position.
                    if (
                        spread_pct > strict_spread
                        and max_spread > strict_spread
                    ):
                        over = spread_pct - strict_spread
                        rng = max_spread - strict_spread
                        scale = max(0.5, 1.0 - (over / rng) * 0.5)
                        existing_mult = float(signal.get("size_multiplier", 1.0))
                        signal["size_multiplier"] = existing_mult * scale
                        log.info(
                            f"SPREAD SIZE-DOWN: {symbol} spread "
                            f"{spread_pct*100:.1f}% (price ${price_for_spread:.2f}, "
                            f"tier max {max_spread*100:.0f}%) → "
                            f"size {existing_mult:.2f} × {scale:.2f} = "
                            f"{signal['size_multiplier']:.2f}"
                        )

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

        # === 2. PER-(STRATEGY, SYMBOL) CONSECUTIVE-LOSS FAST-TRACK ===
        # Per-strategy rather than global so a symbol that wins on
        # mean_reversion isn't blocked because momentum bled on it. Fires
        # BEFORE the aggregate per-symbol check below so the user sees
        # the specific (strategy, symbol) reason in the log.
        # 2026-06-02 trade audit: catches FBYD/momentum (0/3 -$130),
        # RKLB/momentum (last 3 were losses), without touching healthy
        # paths like NEAR/mean_reversion (22 trades, 50% WR).
        # Disabled via `risk.consecutive_loss_block_n: 0`.
        strat_name = signal.get("strategy") or signal.get("source", "")
        consec_n = int(self.config.risk_config.get(
            "consecutive_loss_block_n", 3
        ))
        if strat_name and consec_n > 0:
            strat_sym_trades = [
                t for t in self.trade_history
                if t.get("symbol") == symbol and t.get("strategy") == strat_name
            ]
            last_n = strat_sym_trades[-consec_n:]
            if len(last_n) >= consec_n and all(
                t.get("pnl", 0) < 0 for t in last_n
            ):
                cum_pnl = sum(t.get("pnl", 0) for t in last_n)
                return False, (
                    f"{consec_n} consecutive losses on {strat_name}/{symbol}: "
                    f"${cum_pnl:+.2f}"
                )

        # === 2b. AGGREGATE PER-SYMBOL HISTORY CHECK ===
        # If we've traded this symbol before across any strategy, what's
        # the track record? Catches symbols that are structurally bad
        # across the board even when no single (strategy, symbol) hits the
        # consecutive-loss bar.
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

        # Report ACTUAL fill outcome, not just "_execute_signal returned".
        # _execute_signal runs additional gates (falling-knife / bad-news / spread /
        # post-slippage) after risk_manager. Previously we reported "executed" the
        # moment the function returned, masking downstream blocks (META hit this on
        # 2026-05-15 — falling-knife "no quote" block, but the API still said
        # executed). Detect the real outcome via position-state transition.
        results = []
        for sig in approved:
            sym = sig["symbol"]
            action = sig["action"]
            held_before = sym in self.positions
            self._execute_signal(sig)
            held_after = sym in self.positions
            if action in ("buy", "short"):
                filled = (not held_before) and held_after
            elif action in ("sell", "cover", "close"):
                filled = held_before and (not held_after)
            else:
                filled = held_before != held_after
            if filled:
                results.append({"symbol": sym, "action": action, "status": "executed"})
            elif sig.get("_deferred"):
                # Order accepted by IBKR but queued for the next session
                # (typical for overnight / weekend manual signals). The fill
                # arrives via streaming when the venue opens — not a failure.
                results.append({
                    "symbol": sym,
                    "action": action,
                    "status": "deferred",
                    "order_id": sig.get("_deferred_order_id"),
                    "reason": "queued at IBKR for next regular session",
                })
            else:
                results.append({
                    "symbol": sym,
                    "action": action,
                    "status": "blocked",
                    "reason": "downstream gate (falling-knife / news / spread / slippage — see logs)",
                })

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
        # Reset drawdown circuit breaker for the new session
        self._dd_block_until = None
        self._daily_soft_stop_active = False
        # Clear the equity re-entry guard — slippage blocks and loss cooldowns
        # are session-scoped (a thin spread / bad fill is a same-day condition).
        self._recent_close_info = {}
        # Reset gate-hit counters for the new session (recent tail kept)
        from collections import defaultdict
        self._gate_hits = defaultdict(lambda: defaultdict(int))
        self._gate_hits_total = defaultdict(int)

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

        # Score all current positions by momentum. Crypto is excluded because:
        # (a) it routes through tp_crypto_broker, not the equity broker that
        #     `rejected_signals` are aimed at — closing a crypto slot doesn't
        #     free equity capacity in any meaningful sense;
        # (b) mean_reversion keeps re-firing the same crypto signal every 3s,
        #     so a rotate-out is immediately followed by a fast-lane re-entry —
        #     observed live 2026-05-17: SUI/ICP/LINK each entered+rotated 3x in
        #     ~30 min, leaving an orphan on TradersPost when the cycle didn't
        #     close out cleanly.
        scored_positions = []
        for symbol, pos in self.positions.items():
            if self._is_crypto_symbol(symbol):
                continue
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

    def _check_trend_rider_sharp_drop(self, symbol, pos):
        """Intraday sharp-drop check for trend riders.

        Daily-bar exits (SuperTrend / 20 EMA / red-after-green) only fire at the
        close. But institutional distribution shows up intraday: a clean 3%+ drop
        in 30 minutes on no specific news is a tell the trend is breaking *now*,
        not at 4 PM. The trailing stop catches this eventually, but for swing
        positions held days the trail may be loose enough that 4-5% comes off
        the peak before it triggers. Returns (should_exit: bool, reason: str).
        """
        if not (pos.get("trend_rider") or pos.get("strategy") == "daily_trend_rider"):
            return False, ""
        if not self.market_data:
            return False, ""
        try:
            bars = self.market_data.get_bars(symbol, 8)  # last ~40 min on 5m
            if bars is None or len(bars) < 6:
                return False, ""
            recent_high = float(bars["high"].iloc[-6:].max())
            current = float(bars["close"].iloc[-1])
            if recent_high <= 0:
                return False, ""
            drop_pct = (current - recent_high) / recent_high
            if drop_pct <= -0.03:
                return True, (
                    f"Sharp drop: -{abs(drop_pct):.1%} in last 30 min "
                    f"(${recent_high:.2f} → ${current:.2f}) — institutional distribution"
                )
        except Exception as e:
            log.debug(f"Trend rider sharp-drop check failed for {symbol}: {e}")
        return False, ""

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

    def _log_strategy_signal_summary(self):
        """Daily observability: one log line per enabled strategy showing
        signals fired vs filled SINCE THE LAST EOD CALL.

        Catches the "enabled but silent" failure mode. Three multi-day
        bleeds in the 2026-05-27..06-04 window were strategies firing
        signals that never converted to fills (SNBR/low_float_catalyst
        QUALITY GATE score=0, NU/IBIT rvol_scalp same bug, LGPS/TSAT
        wide-spread block). Each was visible in the per-cycle log noise
        but invisible in summary form — operators had to grep to find
        them. This summary makes the pattern impossible to miss: a
        strategy with N fired / 0 filled stands out at a glance.

        The strategies' `signals_generated` and `trades_taken` counters
        are monotonic since boot, so we snapshot them after each EOD log
        and report the DELTA next time. First EOD post-boot reports
        cumulative-since-boot; every subsequent EOD reports since the
        last EOD.
        """
        if not getattr(self, "strategies", None):
            return

        # Snapshots persisted across EOD calls. Initialized on first use.
        last_fired = getattr(self, "_last_eod_signals_fired", {})
        last_filled = getattr(self, "_last_eod_signals_filled", {})

        rows = []
        new_fired = {}
        new_filled = {}
        for name, strat in self.strategies.items():
            cum_fired = getattr(strat, "signals_generated", 0)
            cum_filled = getattr(strat, "trades_taken", 0)
            new_fired[name] = cum_fired
            new_filled[name] = cum_filled
            delta_fired = cum_fired - last_fired.get(name, 0)
            delta_filled = cum_filled - last_filled.get(name, 0)
            rows.append((name, delta_fired, delta_filled))
        # Sort by fired desc so the noisiest strategy is at the top
        rows.sort(key=lambda r: -r[1])

        log.info("=== STRATEGY SIGNAL SUMMARY (since last EOD) ===")
        for name, fired, filled in rows:
            if fired > 0 and filled == 0:
                marker = "  FIRED-BUT-NEVER-FILLED"
            elif fired == 0 and filled == 0:
                marker = "  (silent)"
            else:
                conv = filled / fired * 100 if fired else 0
                marker = f"  ({conv:.0f}% conversion)"
            log.info(f"  {name:<22} fired={fired:>3}  filled={filled:>3}{marker}")

        # Persist new baselines for next EOD's delta
        self._last_eod_signals_fired = new_fired
        self._last_eod_signals_filled = new_filled

    def _end_of_day(self):
        """End of day routine with smart position evaluation.

        For each position, evaluates bullish/bearish technical factors to decide:
        - Close at EOD (bearish, scalps, losers)
        - Hold into after-hours with tightened stops (bullish runners)
        - Hold overnight (strong multi-day plays)

        After-hours selling uses IBKR outside-RTH limit orders.
        """
        log.info("=== END OF DAY ===")

        # --- Per-strategy signal/fill summary ---
        # One log line per enabled strategy with the day's signals_generated
        # vs trades_taken. Catches the "strategy is enabled but never
        # produces fills" pattern that hid the QUALITY GATE score-field
        # bugs for weeks (silent signals == silent failure). Counters are
        # zeroed by each strategy's daily-reset in generate_signals().
        try:
            self._log_strategy_signal_summary()
        except Exception as e:
            log.debug(f"strategy signal summary failed: {e}")

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

            # Place server-side stop orders at IBKR for overnight holds.
            # These protect against gap-downs even if the bot is offline.
            # Skipped under TradersPost-primary — broker-side stops require
            # direct IBKR order placement (ib_async coroutine, contextvars
            # risk); the bot's _monitor_positions loop manages stops locally.
            all_holds = overnight_holds + afterhours_holds
            if all_holds and self.tp_broker:
                log.info(
                    f"TradersPost-primary: {len(all_holds)} overnight hold(s) — "
                    f"stops managed locally by the bot, no IBKR-side GTC stops placed."
                )
            if all_holds and not self.tp_broker and self.broker and self.broker.is_connected():
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
            # Gate-hit telemetry: how often each defensive gate fired today
            # and which symbols/strategies triggered them. Lets the operator
            # measure whether the new defensive layer is working.
            "gate_hits": {
                "totals": dict(self._gate_hits_total),
                "by_symbol": {gate: dict(syms) for gate, syms in self._gate_hits.items()},
                "recent": list(self._gate_recent[-20:]),
            },
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
        """Apply a trading profile and persist via the overlay.

        Writes to `data/auto-tuner-overrides.yaml` (gitignored) instead of
        `config/settings.yaml` so a host `git pull` doesn't fight the
        runtime profile choice. Falls back to the in-memory profile only
        if persistence fails.
        """
        if self.config.apply_profile(profile_name):
            try:
                self.config.save_setting_override("trading_profile", profile_name)
            except Exception as e:
                log.warning(f"Profile overlay write failed: {e}")
            log.info(f"Trading profile changed to: {profile_name}")
            return True
        return False

    def update_config_setting(self, path, value):
        """Update a single config setting via the overlay path."""
        self.config.save_setting_override(path, value)
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
        momentum_strat = self.strategies.get("momentum")

        # Crypto re-injection: keep the configured crypto symbols in the
        # crypto-eligible strategies' dynamic universes on every scanner
        # cycle. add_dynamic_symbols refreshes timestamps for symbols
        # already present, so the 30-min prune (_prune_stale_dynamic_symbols)
        # won't evict crypto between cycles. Without this, BTC/ETH/SOL drop
        # out after 30 minutes since they never appear in the equity scanner.
        if self._is_crypto_enabled():
            crypto_cfg = self.config.settings.get("crypto", {})
            crypto_symbols = self._get_crypto_universe()
            allowed = crypto_cfg.get("allowed_strategies", ["mean_reversion", "momentum"])
            if crypto_symbols:
                _injected_to = []
                for strat_name in allowed:
                    strat = self.strategies.get(strat_name)
                    if strat and hasattr(strat, "add_dynamic_symbols"):
                        strat.add_dynamic_symbols(crypto_symbols)
                        _injected_to.append(
                            f"{strat_name}({len(strat._dynamic_symbols)} dyn)"
                        )
                # Log every ~25 scanner cycles so the entry stays visible
                # in the log without spamming. First cycle always logs.
                if not hasattr(self, "_crypto_inject_count"):
                    self._crypto_inject_count = 0
                if self._crypto_inject_count % 25 == 0:
                    log.info(
                        f"CRYPTO INJECT: {len(crypto_symbols)} symbols "
                        f"({', '.join(crypto_symbols)}) → {', '.join(_injected_to)}"
                    )
                self._crypto_inject_count += 1
        if not any([rvol_strat, scalp_strat, mr_strat, pb_strat, gap_strat, squeeze_strat, pead_strat, runner_strat, momentum_strat]):
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

                    # Feed the FULL discovered universe into every strategy.
                    # Price-ceiling filter above already dropped >$500 names.
                    # Each strategy's own logic (min_price, ADX, RVOL, verdict)
                    # still enforces quality — this just makes sure no strategy
                    # is stuck on a stale hardcoded list while the tape moves.
                    _trend_rider_strat = self.strategies.get("daily_trend_rider")
                    _dynamic_strategies = [
                        rvol_strat, scalp_strat, runner_strat, pb_strat,
                        squeeze_strat, pead_strat, momentum_strat, gap_strat,
                        mr_strat, _trend_rider_strat,
                    ]
                    if _ibkr_all_list:
                        for _s in _dynamic_strategies:
                            if _s and hasattr(_s, "add_dynamic_symbols"):
                                _s.add_dynamic_symbols(_ibkr_all_list)

                    # Targeted feeds on top of the broad net:
                    # gap-ups still flagged specifically for the gap strategy
                    if _ibkr_gap_syms and gap_strat:
                        gap_strat.add_dynamic_symbols(_ibkr_gap_syms)
                    # losers tagged for mean reversion (oversold = reversion candidate)
                    if _ibkr_loser_syms and mr_strat:
                        mr_strat.add_dynamic_symbols(_ibkr_loser_syms)
                        if scalp_strat:
                            scalp_strat.add_dynamic_symbols(_ibkr_loser_syms)

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

                        # Feed catalyst data so the 0-3 catalyst component of
                        # the 10-pt score is no longer always 0. Without this
                        # momentum_runner could only ever reach 6 by perfect
                        # RVOL + Technical (3+0+0+3), which silenced the
                        # strategy entirely for 30d at 30% allocation. Also
                        # unblocks the >30% daily-change wall in
                        # _analyze_symbol: stocks already up >30% require a
                        # catalyst record to be considered at all.
                        if self.news_feed and hasattr(self.news_feed, "get_catalyst_map"):
                            try:
                                catalyst_map = self.news_feed.get_catalyst_map(
                                    lookback_minutes=240, min_score=2,
                                )
                                if catalyst_map:
                                    runner_strat.feed_catalyst_data(catalyst_map)
                                    log.info(
                                        f"News: fed {len(catalyst_map)} catalysts into momentum_runner"
                                    )
                            except Exception as e:
                                log.debug(f"Catalyst feed error: {e}")
                            # Wave 5: surface feed health so a silent Polygon
                            # outage doesn't regress momentum_runner to its
                            # pre-#210 catalyst-blind state without notice.
                            # Throttled to once per hour so a multi-hour
                            # outage doesn't fill the log.
                            try:
                                if hasattr(self.news_feed, "is_healthy"):
                                    healthy, age, reason = self.news_feed.is_healthy()
                                    if not healthy:
                                        last_warn = getattr(self, "_news_unhealthy_last_warn_ts", 0)
                                        now_ts = time.time()
                                        if now_ts - last_warn > 3600:
                                            self._news_unhealthy_last_warn_ts = now_ts
                                            log.warning(
                                                f"NEWS FEED UNHEALTHY: {reason} "
                                                f"(age={age}s) — momentum_runner catalyst "
                                                f"scoring will silently regress to 0-pt until "
                                                f"feed recovers"
                                            )
                                            if self.notifier:
                                                try:
                                                    self.notifier.system_alert(
                                                        f"News feed unhealthy: {reason}. "
                                                        f"momentum_runner catalyst component "
                                                        f"is silently at 0 until feed recovers.",
                                                        level="warning",
                                                    )
                                                except Exception:
                                                    pass
                            except Exception:
                                pass

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
                    lfc_strat = self.strategies.get("low_float_catalyst")
                    if lfc_strat:
                        lfc_strat.add_dynamic_symbols(runner_symbols)
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
