"""
Performance Dashboard - Web-based monitoring UI.
Shows live P&L, positions, equity curve, and system status.
Mobile-responsive with touch controls for phone access via Render.
"""
import os
import json
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, jsonify, request
from flask_cors import CORS

from bot.utils.logger import get_logger

log = get_logger("dashboard")


class Dashboard:
    """Web dashboard for monitoring the trading bot - mobile ready."""

    def __init__(self, engine, config):
        self.engine = engine
        self.config = config
        self.app = Flask(
            "trading_dashboard",
            template_folder=str(
                __import__("pathlib").Path(__file__).parent / "templates"
            )
        )
        CORS(self.app)
        self._setup_routes()

    def _require_auth(self, f):
        """Simple API key auth for mobile control endpoints."""
        @wraps(f)
        def decorated(*args, **kwargs):
            secret = os.environ.get("DASHBOARD_SECRET_KEY", "")
            if secret:
                provided = request.headers.get("X-API-Key", "") or request.args.get("key", "")
                if provided != secret:
                    return jsonify({"error": "Unauthorized"}), 401
            return f(*args, **kwargs)
        return decorated

    def _setup_routes(self):

        @self.app.route("/")
        def index():
            # Pass auth key to template so dashboard JS can make authenticated calls
            dashboard_key = os.environ.get("DASHBOARD_SECRET_KEY", "")
            return render_template("dashboard.html", dashboard_key=dashboard_key)

        @self.app.route("/health")
        def health():
            return jsonify({
                "status": "ok",
                "mode": self.config.mode,
                "running": self.engine.running,
                "uptime": datetime.now().isoformat(),
            })

        # --- Read-only APIs ---

        @self.app.route("/api/status")
        def status():
            return jsonify(self.engine.get_status())

        @self.app.route("/api/positions")
        def positions():
            positions = self.engine.positions
            result = []
            for symbol, pos in positions.items():
                entry = pos.get("entry_price", 0)
                current = pos.get("current_price", entry)
                qty = pos.get("quantity", 0)
                direction = pos.get("direction", "long")
                if direction == "long":
                    pnl_dollars = (current - entry) * qty
                else:
                    pnl_dollars = (entry - current) * qty
                result.append({
                    "symbol": symbol,
                    "direction": direction,
                    "quantity": qty,
                    "entry_price": entry,
                    "current_price": current,
                    "pnl_pct": pos.get("unrealized_pnl_pct", 0) * 100,
                    "pnl_dollars": round(pnl_dollars, 2),
                    "strategy": pos.get("strategy", "unknown"),
                    "stop_loss": pos.get("stop_loss", 0),
                    "take_profit": pos.get("take_profit", 0),
                    "breakeven_hit": pos.get("breakeven_hit", False),
                    "trailing_stop": pos.get("trailing_stop", 0),
                    "targets_hit": pos.get("targets_hit", []),
                })
            return jsonify(result)

        @self.app.route("/api/trades")
        def trades():
            return jsonify(self.engine.trade_history[-50:])

        @self.app.route("/api/equity")
        def equity():
            return jsonify(self.engine.equity_curve[-500:])

        @self.app.route("/api/daily")
        def daily():
            return jsonify(self.engine.daily_stats[-30:])

        @self.app.route("/api/notifications")
        def notifications():
            if self.engine.notifier:
                return jsonify(self.engine.notifier.history[-20:])
            return jsonify([])

        @self.app.route("/api/scanner")
        def scanner():
            return jsonify(self.engine.get_scanner_data())

        @self.app.route("/api/analysis")
        def analysis():
            return jsonify(self.engine.get_analysis_log())

        # --- Regime, Learning, Hedging APIs ---

        @self.app.route("/api/regime")
        def regime():
            if self.engine.regime_detector:
                return jsonify(self.engine.regime_detector.get_status())
            return jsonify({"current_regime": "unknown"})

        @self.app.route("/api/learning")
        def learning():
            if self.engine.trade_analyzer:
                return jsonify(self.engine.trade_analyzer.get_status())
            return jsonify({})

        @self.app.route("/api/learning/analyze", methods=["POST"])
        @self._require_auth
        def run_analysis():
            """Manually trigger learning analysis."""
            if self.engine.trade_analyzer:
                result = self.engine.trade_analyzer.analyze(
                    self.engine.trade_history,
                    current_regime=self.engine.regime_detector.current_regime if self.engine.regime_detector else None
                )
                return jsonify(result)
            return jsonify({"error": "Trade analyzer not enabled"}), 404

        @self.app.route("/api/hedging")
        def hedging():
            if self.engine.hedging_manager:
                return jsonify(self.engine.hedging_manager.get_status())
            return jsonify({"enabled": False})

        # --- Control APIs (for mobile) ---

        @self.app.route("/api/control/pause", methods=["POST"])
        @self._require_auth
        def pause():
            self.engine.paused = True
            log.info("Bot PAUSED via dashboard")
            if self.engine.notifier:
                self.engine.notifier.system_alert("Bot paused via mobile", level="warning")
            return jsonify({"status": "paused"})

        @self.app.route("/api/control/resume", methods=["POST"])
        @self._require_auth
        def resume():
            self.engine.paused = False
            log.info("Bot RESUMED via dashboard")
            if self.engine.notifier:
                self.engine.notifier.system_alert("Bot resumed via mobile", level="success")
            return jsonify({"status": "running"})

        @self.app.route("/api/control/close/<symbol>", methods=["POST"])
        @self._require_auth
        def close_position(symbol):
            symbol = symbol.upper()
            if symbol in self.engine.positions:
                self.engine._close_position(symbol, "manual", "Closed via mobile dashboard")
                return jsonify({"status": "closed", "symbol": symbol})
            return jsonify({"error": f"No position in {symbol}"}), 404

        @self.app.route("/api/control/close-all", methods=["POST"])
        @self._require_auth
        def close_all():
            count = len(self.engine.positions)
            self.engine._close_all_positions("Manual close-all via mobile dashboard")
            return jsonify({"status": "closed_all", "count": count})

        @self.app.route("/api/control/emergency-stop", methods=["POST"])
        @self._require_auth
        def emergency_stop():
            self.engine._close_all_positions("EMERGENCY STOP via mobile")
            self.engine.running = False
            log.critical("EMERGENCY STOP triggered via mobile dashboard")
            if self.engine.notifier:
                self.engine.notifier.system_alert("EMERGENCY STOP via mobile", level="error")
            return jsonify({"status": "stopped", "positions_closed": True})

        # --- Signal Routing API ---

        @self.app.route("/api/signal", methods=["POST"])
        @self._require_auth
        def submit_signal():
            """
            Submit a manual trading signal.

            POST JSON:
            {
                "symbol": "NVDA",
                "action": "buy",
                "price": 500.00,        (optional - uses market price)
                "quantity": 10,          (optional - auto-sized)
                "stop_loss": 485.00,     (optional - 3% default)
                "take_profit": 530.00,   (optional - 6% default)
                "strategy": "manual",    (optional)
                "asset_type": "stock",   (optional - "stock" or "option")
                "expiry": "20250117",    (optional - for options)
                "strike": 500.0,         (optional - for options)
                "right": "C"             (optional - "C" call or "P" put)
            }
            """
            data = request.get_json()
            if not data:
                return jsonify({"error": "No JSON data"}), 400

            symbol = data.get("symbol", "").upper()
            action = data.get("action", "").lower()

            if not symbol or not action:
                return jsonify({"error": "symbol and action required"}), 400

            if action not in ("buy", "sell", "short", "cover", "close"):
                return jsonify({"error": f"Invalid action: {action}"}), 400

            # Build signal
            signal = {
                "symbol": symbol,
                "action": action,
                "confidence": float(data.get("confidence", 0.7)),
                "source": "manual",
                "strategy": data.get("strategy", "manual"),
                "reason": data.get("reason", f"Manual signal via API"),
            }

            if data.get("price"):
                signal["price"] = float(data["price"])
            else:
                price = self.engine.market_data.get_price(symbol) if self.engine.market_data else None
                if price:
                    signal["price"] = price
                else:
                    return jsonify({"error": f"No price available for {symbol}. Provide 'price' in request."}), 400

            if data.get("quantity"):
                signal["quantity"] = int(data["quantity"])
            if data.get("stop_loss"):
                signal["stop_loss"] = float(data["stop_loss"])
            if data.get("take_profit"):
                signal["take_profit"] = float(data["take_profit"])
            if data.get("asset_type"):
                signal["asset_type"] = data["asset_type"]
            if data.get("expiry"):
                signal["expiry"] = data["expiry"]
            if data.get("strike"):
                signal["strike"] = float(data["strike"])
            if data.get("right"):
                signal["right"] = data["right"]

            results = self.engine.handle_manual_signal(signal)
            log.info(f"Manual signal submitted: {action.upper()} {symbol} | Result: {results}")

            return jsonify({"status": "ok", "results": results})

        # --- Politician Trade APIs ---

        @self.app.route("/api/politicians/status")
        def politician_status():
            if self.engine.politician_tracker:
                return jsonify(self.engine.politician_tracker.get_status())
            return jsonify({"error": "Politician tracker not enabled"}), 404

        @self.app.route("/api/politicians/trades")
        def politician_trades():
            if self.engine.politician_tracker:
                limit = request.args.get("limit", 50, type=int)
                return jsonify(self.engine.politician_tracker.get_recent_disclosures(limit))
            return jsonify([])

        @self.app.route("/api/politicians/signals")
        def politician_signals():
            if self.engine.politician_tracker:
                return jsonify(self.engine.politician_tracker.get_signals())
            return jsonify([])

        @self.app.route("/api/politicians/check", methods=["POST"])
        @self._require_auth
        def politician_check():
            """Manually trigger check for new politician trades."""
            if self.engine.politician_tracker:
                trades = self.engine.politician_tracker.manual_check()
                return jsonify({"status": "checked", "new_trades": len(trades), "trades": trades})
            return jsonify({"error": "Politician tracker not enabled"}), 404

        @self.app.route("/api/politicians/add", methods=["POST"])
        @self._require_auth
        def add_politician():
            """Add a politician to track."""
            data = request.get_json()
            if not data or not data.get("politician_id") or not data.get("name"):
                return jsonify({"error": "politician_id and name required"}), 400

            if self.engine.politician_tracker:
                self.engine.politician_tracker.add_politician(
                    politician_id=data["politician_id"],
                    name=data["name"],
                    chamber=data.get("chamber", "House"),
                    party=data.get("party", ""),
                    priority=data.get("priority", 3),
                    notable=data.get("notable", ""),
                )
                return jsonify({"status": "added", "politician": data["name"]})
            return jsonify({"error": "Politician tracker not enabled"}), 404

        # --- News Feed APIs ---

        @self.app.route("/api/news")
        def news():
            if self.engine.news_feed:
                limit = request.args.get("limit", 20, type=int)
                return jsonify(self.engine.news_feed.get_recent_news(limit))
            return jsonify([])

        @self.app.route("/api/news/signals")
        def news_signals():
            if self.engine.news_feed:
                return jsonify(self.engine.news_feed.get_signals())
            return jsonify([])

        @self.app.route("/api/news/status")
        def news_status():
            if self.engine.news_feed:
                return jsonify(self.engine.news_feed.get_status())
            return jsonify({"running": False, "api_configured": False})

        # --- Watchlist APIs ---

        @self.app.route("/api/watchlist")
        def watchlist():
            return jsonify(self.engine.get_watchlist_data())

        @self.app.route("/api/watchlist/add", methods=["POST"])
        @self._require_auth
        def watchlist_add():
            data = request.get_json()
            if not data or not data.get("symbol"):
                return jsonify({"error": "symbol required"}), 400
            symbols = self.engine.add_to_watchlist(data["symbol"])
            return jsonify({"status": "added", "watchlist": symbols})

        @self.app.route("/api/watchlist/remove", methods=["POST"])
        @self._require_auth
        def watchlist_remove():
            data = request.get_json()
            if not data or not data.get("symbol"):
                return jsonify({"error": "symbol required"}), 400
            symbols = self.engine.remove_from_watchlist(data["symbol"])
            return jsonify({"status": "removed", "watchlist": symbols})

        # --- Performance / Win-Loss Stats ---

        @self.app.route("/api/performance")
        def performance():
            return jsonify(self.engine.get_performance_summary())

        # --- Settings / Config APIs ---

        @self.app.route("/api/settings")
        def get_settings():
            return jsonify(self.engine.get_editable_settings())

        @self.app.route("/api/settings/profile", methods=["POST"])
        @self._require_auth
        def set_profile():
            """Switch trading mode profile (scalp/swing/invest)."""
            data = request.get_json()
            profile = data.get("profile", "") if data else ""
            if self.engine.apply_trading_profile(profile):
                return jsonify({"status": "ok", "profile": profile})
            return jsonify({"error": f"Unknown profile: {profile}"}), 400

        @self.app.route("/api/settings/update", methods=["POST"])
        @self._require_auth
        def update_setting():
            """Update a single config setting."""
            data = request.get_json()
            if not data or "path" not in data or "value" not in data:
                return jsonify({"error": "path and value required"}), 400
            self.engine.update_config_setting(data["path"], data["value"])
            return jsonify({"status": "ok", "path": data["path"], "value": data["value"]})

        @self.app.route("/api/tips")
        def trading_tips():
            """Get context-aware trading tips."""
            tips = self._generate_tips()
            return jsonify(tips)

        # --- Top Movers API ---

        @self.app.route("/api/movers")
        def top_movers():
            """Get top gaining stocks (catches 300% runners)."""
            return jsonify(self.engine.get_top_movers())

        # --- Preset Watchlist Groups ---

        @self.app.route("/api/watchlist/presets")
        def watchlist_presets():
            """Get available preset groups."""
            presets = {}
            for name, p in self.engine.WATCHLIST_PRESETS.items():
                presets[name] = {
                    "label": p["label"],
                    "count": len(p["symbols"]),
                    "symbols": p["symbols"],
                }
            return jsonify(presets)

        @self.app.route("/api/watchlist/preset/<group>", methods=["POST"])
        def add_preset(group):
            """Add a preset group of symbols to watchlist."""
            result = self.engine.add_preset_group(group)
            if "error" in result:
                return jsonify(result), 400
            return jsonify(result)

        # --- RVOL Scanner API (Money Machine style) ---

        @self.app.route("/api/rvol")
        def rvol_scan():
            """Get relative volume scan results."""
            min_rvol = request.args.get("min_rvol", 1.5, type=float)
            return jsonify(self.engine.get_rvol_scan(min_rvol=min_rvol))

        # --- Low Float Runners API ---

        @self.app.route("/api/runners")
        def low_float_runners():
            """Get low-float post-split runner candidates."""
            return jsonify(self.engine.get_low_float_runners())

        # --- Trade Suggestions API ---

        @self.app.route("/api/suggestions")
        def trade_suggestions():
            """Get AI-generated trade suggestions with profit reasoning."""
            max_suggestions = request.args.get("max", 5, type=int)
            return jsonify(self.engine.get_trade_suggestions(max_suggestions=max_suggestions))

        # --- Quick Trade (execute a suggestion) ---

        @self.app.route("/api/suggestions/execute", methods=["POST"])
        @self._require_auth
        def execute_suggestion():
            """Execute a trade suggestion (LONG only)."""
            data = request.get_json()
            if not data or not data.get("symbol"):
                return jsonify({"error": "symbol required"}), 400

            signal = {
                "symbol": data["symbol"].upper(),
                "action": "buy",
                "confidence": float(data.get("confidence", 0.7)),
                "source": "suggestion",
                "strategy": data.get("strategy", "suggestion"),
                "reason": f"Trade suggestion: {data.get('why', 'Manual execution')}",
            }

            if data.get("stop_loss"):
                signal["stop_loss"] = float(data["stop_loss"])
            if data.get("take_profit"):
                signal["take_profit"] = float(data["take_profit"])

            price = self.engine.market_data.get_price(data["symbol"].upper()) if self.engine.market_data else None
            if price:
                signal["price"] = price
            elif data.get("price"):
                signal["price"] = float(data["price"])
            else:
                return jsonify({"error": "No price available"}), 400

            results = self.engine.handle_manual_signal(signal)
            return jsonify({"status": "ok", "results": results})

        # --- Swing Trade Scanner API ---

        @self.app.route("/api/swing-scanner")
        def swing_scanner():
            """Get swing trade opportunities with hold duration & profit targets."""
            return jsonify(self.engine.get_swing_scanner())

        # --- Quote API (real-time price lookup) ---

        @self.app.route("/api/quote/<symbol>")
        def quote(symbol):
            """Get real-time quote for a symbol."""
            symbol = symbol.upper()
            if self.engine.market_data:
                q = self.engine.market_data.get_quote(symbol)
                if q:
                    return jsonify(q)
            return jsonify({"error": f"No quote available for {symbol}"}), 404

        # --- Webhook receiver for TradingView (on same server) ---

        @self.app.route("/webhook/tradingview", methods=["POST"])
        def tradingview_webhook():
            """Receive TradingView webhook on the main dashboard server."""
            if self.engine.tv_receiver:
                # Delegate to TV receiver's route handler
                return self.engine.tv_receiver.app.test_client().post(
                    "/webhook/tradingview",
                    data=request.data,
                    headers=dict(request.headers),
                ).data
            # Handle directly if no separate TV receiver
            data = request.get_json(force=True)
            if not data:
                return jsonify({"error": "No data"}), 400

            signal = {
                "symbol": (data.get("symbol") or data.get("ticker", "")).upper(),
                "action": (data.get("action") or "buy").lower(),
                "price": float(data.get("price", 0)) if data.get("price") else None,
                "confidence": float(data.get("confidence", 0.7)),
                "source": "tradingview_webhook",
                "strategy": "tradingview",
                "reason": f"TradingView alert: {data.get('comment', '')}",
            }
            if data.get("stop_loss"):
                signal["stop_loss"] = float(data["stop_loss"])
            if data.get("take_profit"):
                signal["take_profit"] = float(data["take_profit"])

            self.engine._handle_tv_signal(signal)
            return jsonify({"status": "ok"})

    def _generate_tips(self):
        """Generate context-aware trading tips based on current state."""
        tips = []
        status = self.engine.get_status()
        perf = self.engine.get_performance_summary()

        # Regime-based tips
        regime_data = status.get("regime") or {}
        regime = regime_data.get("current_regime", "sideways")
        regime_tips = {
            "bull_trend": [
                "Bull trend detected - momentum and breakout strategies work best",
                "Let winners run longer with trailing stops, tighten losers quickly",
                "Consider swing trades over scalps in strong uptrends",
            ],
            "bear_trend": [
                "Bear trend active - reduce position sizes and hold more cash",
                "Inverse ETFs (SH, SQQQ) can hedge downside exposure",
                "Mean reversion setups have higher failure rate in bear markets",
            ],
            "sideways": [
                "Range-bound market - mean reversion and VWAP scalp strategies shine",
                "Look for support/resistance bounces rather than breakouts",
                "Tighter stops work better in choppy conditions",
            ],
            "high_vol": [
                "High volatility - reduce position sizes by 50%",
                "Widen stops to avoid getting stopped out by noise",
                "Avoid VWAP scalping - spreads are wider in volatile markets",
            ],
            "low_vol": [
                "Low vol = potential breakout setup brewing",
                "Compression usually precedes big moves - watch for breakouts",
                "Reduce position count but increase size on high-conviction setups",
            ],
            "crisis": [
                "CRISIS MODE - capital preservation is priority #1",
                "Close risky positions and hedge with inverse ETFs",
                "Only trade with 20% or less of normal size",
            ],
        }
        tips.extend(regime_tips.get(regime, []))

        # Performance-based tips
        if perf.get("total_trades", 0) >= 5:
            wr = perf.get("win_rate", 50)
            pf = perf.get("profit_factor", 1.0)
            if wr < 40:
                tips.append("Win rate below 40% - consider tighter entry criteria or switching to higher-probability setups")
            if pf and pf < 1.0:
                tips.append("Profit factor below 1.0 (losing money) - cut losers faster or let winners run longer")
            if perf.get("loss_streak", 0) >= 3:
                tips.append("Consecutive losses detected - consider pausing, reviewing your recent trades, and reducing size")
            avg_win = perf.get("avg_win", 0)
            avg_loss = abs(perf.get("avg_loss", 0))
            if avg_loss > 0 and avg_win / avg_loss < 1.5:
                tips.append("Reward/risk ratio is below 1.5:1 - try wider take profit targets or tighter stops")

        # Position-based tips
        n_pos = len(self.engine.positions)
        max_pos = self.engine.config.max_positions
        if n_pos >= max_pos:
            tips.append(f"Max positions reached ({n_pos}/{max_pos}) - close a position before entering new trades")
        if n_pos == 0:
            tips.append("No open positions - ready for new entries when signals align")

        # General profit tips
        tips.extend([
            "Follow politician trades within 3 days of disclosure for best alpha",
            "LEAPS options (6-12 month expiry, deep ITM) reduce theta decay vs short-dated options",
            "Scale out of winners: sell 33% at +3%, 50% at +6%, let the rest ride",
            "Never risk more than 1-2% of portfolio on a single trade",
        ])

        return tips

    def start(self):
        host = self.config.dashboard_host
        port = self.config.dashboard_port
        log.info(f"Dashboard starting at http://{host}:{port}")
        self.app.run(host=host, port=port, debug=False, use_reloader=False)
