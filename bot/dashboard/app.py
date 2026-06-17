"""
Performance Dashboard - Web-based monitoring UI.
Shows live P&L, positions, equity curve, and system status.
Mobile-responsive with touch controls for phone access via Render.
"""
import hmac
import os
import io
import csv
import json
import threading
import time
import urllib.request
from datetime import datetime, timedelta

from flask import Flask, render_template, jsonify, request, make_response
from flask_cors import CORS

from bot.utils.logger import get_logger

log = get_logger("dashboard")


# TradersPost's paper crypto feed shows $0 / -100% on positions while the
# live market is fine (recurring incident, documented in user memory).
# Pull spot prices straight from Coinbase so the dashboard has a trusted
# view that doesn't depend on the broker UI or the bot's own market_data
# feed (which can be stale on a just-entered symbol). Cached 20s to keep
# the per-position fanout cheap; Coinbase rate-limits unauth'd at ~10rps.
_COINBASE_PRICE_CACHE: dict = {}  # symbol -> (price_float, fetched_ts)
_COINBASE_PRICE_TTL = 20.0
_COINBASE_PRICE_LOCK = threading.Lock()


def _coinbase_spot_price(symbol: str):
    """Return spot USD price for a crypto symbol like 'ICP-USD', or None on
    failure. Failures are logged once per minute per symbol — a flaky
    upstream shouldn't spam the dashboard log on every page render."""
    now = time.time()
    with _COINBASE_PRICE_LOCK:
        cached = _COINBASE_PRICE_CACHE.get(symbol)
        if cached and (now - cached[1]) < _COINBASE_PRICE_TTL:
            return cached[0]
    url = f"https://api.coinbase.com/v2/prices/{symbol}/spot"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as r:
            data = json.loads(r.read().decode("utf-8"))
        price = float(data["data"]["amount"])
    except Exception as e:
        with _COINBASE_PRICE_LOCK:
            last_err = _COINBASE_PRICE_CACHE.get(f"__err_{symbol}", (0, 0))[1]
            if (now - last_err) > 60:
                log.warning(f"Coinbase spot fetch failed for {symbol}: {e}")
                _COINBASE_PRICE_CACHE[f"__err_{symbol}"] = (0, now)
        return None
    with _COINBASE_PRICE_LOCK:
        _COINBASE_PRICE_CACHE[symbol] = (price, now)
    return price


class Dashboard:
    """Web dashboard for monitoring the trading bot - mobile ready."""

    # Routes that bypass auth: container healthcheck only.
    _PUBLIC_ENDPOINTS = frozenset({"health"})

    def __init__(self, engine, config):
        self.engine = engine
        self.config = config

        # Fail-closed: refuse to start the dashboard if no secret is configured.
        # Previously the check lived inside an optional decorator, so missing
        # config silently disabled auth on every endpoint.
        self._secret = os.environ.get("DASHBOARD_SECRET_KEY", "")
        if not self._secret:
            raise RuntimeError(
                "DASHBOARD_SECRET_KEY must be set; refusing to start dashboard "
                "with no auth"
            )

        self.app = Flask(
            "trading_dashboard",
            template_folder=str(
                __import__("pathlib").Path(__file__).parent / "templates"
            )
        )

        # Scope CORS — default to local-only; override with a comma-separated
        # DASHBOARD_ALLOWED_ORIGINS for browser access from another origin.
        origins_env = os.environ.get(
            "DASHBOARD_ALLOWED_ORIGINS", "http://localhost:5000"
        )
        origins = [o.strip() for o in origins_env.split(",") if o.strip()]
        CORS(self.app, origins=origins, supports_credentials=True)

        # Single auth choke point — every request goes through this before
        # hitting any route handler. New routes inherit auth automatically.
        @self.app.before_request
        def _enforce_basic_auth():
            if request.endpoint in self._PUBLIC_ENDPOINTS:
                return None
            auth = request.authorization
            if not auth or not hmac.compare_digest(
                auth.password or "", self._secret
            ):
                return (
                    "",
                    401,
                    {"WWW-Authenticate": 'Basic realm="AlgoBot Dashboard"'},
                )
            return None

        self._setup_routes()

    def _setup_routes(self):

        @self.app.route("/")
        def index():
            # Auth is enforced by the global before_request hook (Basic auth).
            # The browser session carries the credentials on subsequent XHRs,
            # so the template no longer needs the secret rendered into it.
            #
            # Cache-Control: no-store on the dashboard HTML (Wave-6 ops fix).
            # iOS Safari aggressively caches PWA-style single-page dashboards,
            # so users were still seeing the pre-bugfix JS days after we'd
            # shipped fixes — even after pull-to-refresh. Force the browser
            # to refetch the HTML+inline-JS on every visit. The JSON endpoints
            # are already not cached (Authorization-bearing fetches bypass
            # browser HTTP cache by default), so this single header on the
            # HTML route is the operative fix.
            resp = make_response(render_template("dashboard.html", dashboard_key=""))
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            return resp

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
                # For crypto, override the bot's internal current_price with
                # the live Coinbase spot. Two failure modes this fixes:
                # (1) TradersPost paper UI showing $0/-100% — user looks at
                #     the dashboard and sees the same wrong number; (2) the
                #     bot's own current_price stays pinned to entry until
                #     the first tick arrives on a fresh entry (5sec bar
                #     cadence + occasional fast-lane delay). Live Coinbase
                #     is the same venue TradersPost routes to, so it's the
                #     authoritative mark.
                price_source = "engine"
                if symbol.upper().endswith("-USD"):
                    live = _coinbase_spot_price(symbol.upper())
                    if live is not None:
                        current = live
                        price_source = "coinbase_live"
                if direction == "long":
                    pnl_dollars = (current - entry) * qty
                else:
                    pnl_dollars = (entry - current) * qty
                pnl_pct = ((current / entry) - 1.0) * 100 if entry else 0.0
                if direction != "long":
                    pnl_pct = -pnl_pct
                result.append({
                    "symbol": symbol,
                    "direction": direction,
                    "quantity": qty,
                    "entry_price": entry,
                    "current_price": current,
                    "price_source": price_source,
                    "pnl_pct": pnl_pct if price_source == "coinbase_live"
                               else pos.get("unrealized_pnl_pct", 0) * 100,
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
            from flask import request as req
            trades_list = list(self.engine.trade_history)
            # Optional date filtering
            start = req.args.get("start")  # ISO date: 2026-02-21
            end = req.args.get("end")
            strategy = req.args.get("strategy")
            symbol = req.args.get("symbol")
            limit = int(req.args.get("limit", 100))

            if start:
                trades_list = [t for t in trades_list
                               if str(t.get("exit_time", t.get("entry_time", ""))) >= start]
            if end:
                trades_list = [t for t in trades_list
                               if str(t.get("exit_time", t.get("entry_time", ""))) <= end + "T23:59:59"]
            if strategy:
                trades_list = [t for t in trades_list if t.get("strategy") == strategy]
            if symbol:
                trades_list = [t for t in trades_list if t.get("symbol") == symbol.upper()]

            return jsonify(trades_list[-limit:])

        @self.app.route("/api/trades/export")
        def trades_export():
            """Export all trades as CSV for review."""
            from flask import request as req, Response
            trades_list = list(self.engine.trade_history)
            start = req.args.get("start")
            end = req.args.get("end")
            if start:
                trades_list = [t for t in trades_list
                               if str(t.get("exit_time", t.get("entry_time", ""))) >= start]
            if end:
                trades_list = [t for t in trades_list
                               if str(t.get("exit_time", t.get("entry_time", ""))) <= end + "T23:59:59"]

            output = io.StringIO()
            fields = ["symbol", "direction", "strategy", "entry_price", "exit_price",
                       "quantity", "pnl", "pnl_pct", "reason", "executed_via",
                       "entry_time", "exit_time", "hold_time_mins"]
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for t in trades_list:
                writer.writerow(t)
            return Response(output.getvalue(), mimetype="text/csv",
                            headers={"Content-Disposition": "attachment;filename=trades.csv"})

        @self.app.route("/api/trades/summary")
        def trades_summary():
            """Aggregated trade analytics for the history dashboard."""
            from flask import request as req
            from collections import defaultdict
            from datetime import datetime as _dt

            trades_list = list(self.engine.trade_history)

            # Optional filters
            start = req.args.get("start")
            end = req.args.get("end")
            if start:
                trades_list = [t for t in trades_list
                               if str(t.get("exit_time", t.get("entry_time", ""))) >= start]
            if end:
                trades_list = [t for t in trades_list
                               if str(t.get("exit_time", t.get("entry_time", ""))) <= end + "T23:59:59"]

            if not trades_list:
                return jsonify({"total": 0})

            # Overall stats
            wins = [t for t in trades_list if t.get("pnl", 0) > 0]
            losses = [t for t in trades_list if t.get("pnl", 0) < 0]
            total_pnl = sum(t.get("pnl", 0) for t in trades_list)
            total_profit = sum(t.get("pnl", 0) for t in wins)
            total_loss = sum(abs(t.get("pnl", 0)) for t in losses)

            # By strategy
            by_strategy = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0, "symbols": set()})
            for t in trades_list:
                s = by_strategy[t.get("strategy", "unknown")]
                s["trades"] += 1
                s["pnl"] += t.get("pnl", 0)
                s["symbols"].add(t.get("symbol", ""))
                if t.get("pnl", 0) > 0:
                    s["wins"] += 1
            for k in by_strategy:
                s = by_strategy[k]
                s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else 0
                s["pnl"] = round(s["pnl"], 2)
                s["symbols"] = list(s["symbols"])

            # By symbol
            by_symbol = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
            for t in trades_list:
                s = by_symbol[t.get("symbol", "")]
                s["trades"] += 1
                s["pnl"] += t.get("pnl", 0)
                if t.get("pnl", 0) > 0:
                    s["wins"] += 1
            for k in by_symbol:
                s = by_symbol[k]
                s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else 0
                s["pnl"] = round(s["pnl"], 2)

            # By exit reason
            by_reason = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
            for t in trades_list:
                r = by_reason[t.get("reason", "unknown")]
                r["trades"] += 1
                r["pnl"] += t.get("pnl", 0)
                if t.get("pnl", 0) > 0:
                    r["wins"] += 1
            for k in by_reason:
                r = by_reason[k]
                r["win_rate"] = round(r["wins"] / r["trades"] * 100, 1) if r["trades"] else 0
                r["pnl"] = round(r["pnl"], 2)

            # By hour
            by_hour = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
            for t in trades_list:
                try:
                    h = _dt.fromisoformat(t.get("entry_time", "")).hour
                    bh = by_hour[h]
                    bh["trades"] += 1
                    bh["pnl"] += t.get("pnl", 0)
                    if t.get("pnl", 0) > 0:
                        bh["wins"] += 1
                except (ValueError, TypeError):
                    pass
            for k in by_hour:
                bh = by_hour[k]
                bh["win_rate"] = round(bh["wins"] / bh["trades"] * 100, 1) if bh["trades"] else 0
                bh["pnl"] = round(bh["pnl"], 2)

            # Learning data
            learning = {}
            if self.engine.trade_analyzer:
                learning = self.engine.trade_analyzer.get_status()

            return jsonify({
                "total": len(trades_list),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(len(wins) / len(trades_list) * 100, 1),
                "total_pnl": round(total_pnl, 2),
                "total_profit": round(total_profit, 2),
                "total_loss": round(total_loss, 2),
                "profit_factor": round(total_profit / total_loss, 2) if total_loss > 0 else 0,
                "avg_win": round(total_profit / len(wins), 2) if wins else 0,
                "avg_loss": round(total_loss / len(losses), 2) if losses else 0,
                "largest_win": round(max((t.get("pnl", 0) for t in trades_list), default=0), 2),
                "largest_loss": round(min((t.get("pnl", 0) for t in trades_list), default=0), 2),
                "by_strategy": dict(by_strategy),
                "by_symbol": dict(by_symbol),
                "by_reason": dict(by_reason),
                "by_hour": {str(k): v for k, v in sorted(by_hour.items())},
                "learning": learning,
            })

        @self.app.route("/api/equity")
        def equity():
            return jsonify(self.engine.equity_curve[-500:])

        @self.app.route("/api/strategies/activity")
        def strategies_activity():
            """Per-strategy live activity for the dashboard's strategy
            widgets. Returns cumulative-since-boot counts (signals fired,
            trades taken) + today's deltas. Powers the "FIRED-BUT-NEVER-
            FILLED" alert badge that catches QUALITY-GATE-style silent
            failures (see PR #198 for the same data at EOD log)."""
            strategies = getattr(self.engine, "strategies", {}) or {}
            today_baseline_fired = getattr(
                self.engine, "_dashboard_day_baseline_fired", None
            )
            today_baseline_filled = getattr(
                self.engine, "_dashboard_day_baseline_filled", None
            )
            # Initialize baselines on first call. The day-rollover trigger
            # (midnight ET) re-baselines below.
            from datetime import datetime as _dt
            try:
                from zoneinfo import ZoneInfo as _ZI
                today_et = _dt.now(_ZI("America/New_York")).date()
            except Exception:
                today_et = _dt.utcnow().date()
            last_baseline_date = getattr(
                self.engine, "_dashboard_baseline_date", None
            )
            if (
                today_baseline_fired is None
                or last_baseline_date != today_et
            ):
                today_baseline_fired = {
                    n: getattr(s, "signals_generated", 0)
                    for n, s in strategies.items()
                }
                today_baseline_filled = {
                    n: getattr(s, "trades_taken", 0)
                    for n, s in strategies.items()
                }
                self.engine._dashboard_day_baseline_fired = today_baseline_fired
                self.engine._dashboard_day_baseline_filled = today_baseline_filled
                self.engine._dashboard_baseline_date = today_et

            rows = []
            for name, strat in strategies.items():
                cum_fired = getattr(strat, "signals_generated", 0)
                cum_filled = getattr(strat, "trades_taken", 0)
                today_fired = max(0, cum_fired - today_baseline_fired.get(name, 0))
                today_filled = max(0, cum_filled - today_baseline_filled.get(name, 0))
                # Alert: signals firing but nothing filling — the SNBR /
                # LGPS pattern. Threshold of 3 avoids flagging noise on a
                # single signal that lost a race condition.
                alert_silent_fill = today_fired >= 3 and today_filled == 0
                rows.append({
                    "strategy": name,
                    "enabled": getattr(strat, "enabled", True),
                    "fired_total": cum_fired,
                    "filled_total": cum_filled,
                    "fired_today": today_fired,
                    "filled_today": today_filled,
                    "conversion_pct": round(
                        cum_filled / cum_fired * 100, 1
                    ) if cum_fired else 0.0,
                    "alert_silent_fill": alert_silent_fill,
                })
            # Sort by today's activity desc — noisy strategies at top
            rows.sort(key=lambda r: (-r["fired_today"], -r["fired_total"]))
            return jsonify({"strategies": rows, "as_of": _dt.utcnow().isoformat()})

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
        def run_analysis():
            """Manually trigger learning analysis."""
            if self.engine.trade_analyzer:
                result = self.engine.trade_analyzer.analyze(
                    self.engine.trade_history,
                    current_regime=self.engine.regime_detector.current_regime if self.engine.regime_detector else None
                )
                return jsonify(result)
            return jsonify({"error": "Trade analyzer not enabled"}), 404

        # --- AI Insights (Claude-powered trade analysis) ---

        @self.app.route("/api/ai-insights")
        def ai_insights():
            """Get cached AI insights (no API call)."""
            if self.engine.ai_insights:
                return jsonify(self.engine.ai_insights.get_cached_insights())
            return jsonify({"available": False, "message": "AI insights not configured"})

        @self.app.route("/api/ai-insights/analyze", methods=["POST"])
        def run_ai_analysis():
            """Trigger Claude to analyze trades (costs API tokens)."""
            if not self.engine.ai_insights or not self.engine.ai_insights.is_available():
                return jsonify({"error": "Set ANTHROPIC_API_KEY to enable AI insights"}), 400

            # Gather all data for Claude
            trade_history = self.engine.trade_history
            if not trade_history or len(trade_history) < 3:
                return jsonify({"error": "Need at least 3 completed trades for analysis"}), 400

            performance = self.engine.get_performance_summary()
            positions = self.engine.positions
            regime_data = (
                self.engine.regime_detector.get_status()
                if self.engine.regime_detector else None
            )
            strategy_scores = (
                self.engine.trade_analyzer.strategy_scores
                if self.engine.trade_analyzer else None
            )

            result = self.engine.ai_insights.analyze_trades(
                trade_history=trade_history,
                performance_stats=performance,
                positions=positions,
                regime_data=regime_data,
                strategy_scores=strategy_scores,
            )
            return jsonify(result)

        @self.app.route("/api/ai-insights/quick")
        def ai_quick_insight():
            """Get a quick 2-3 sentence insight (lighter API call)."""
            if not self.engine.ai_insights or not self.engine.ai_insights.is_available():
                return jsonify({"insight": None})

            insight = self.engine.ai_insights.get_quick_insight(
                self.engine.trade_history,
                self.engine.get_performance_summary(),
            )
            return jsonify({"insight": insight})

        # --- Auto-Tuner (autonomous parameter optimization) ---

        @self.app.route("/api/auto-tuner")
        def auto_tuner_status():
            """Get auto-tuner status and recent changes."""
            if self.engine.auto_tuner:
                return jsonify(self.engine.auto_tuner.get_status())
            return jsonify({"enabled": False})

        @self.app.route("/api/auto-tuner/changelog")
        def auto_tuner_changelog():
            """Get full auto-tuner changelog."""
            if self.engine.auto_tuner:
                return jsonify(self.engine.auto_tuner.get_changelog())
            return jsonify([])

        @self.app.route("/api/auto-tuner/run", methods=["POST"])
        def run_auto_tune():
            """Manually trigger auto-tune cycle."""
            if not self.engine.auto_tuner or not self.engine.auto_tuner.is_available():
                return jsonify({"error": "Auto-tuner not available (set ANTHROPIC_API_KEY)"}), 400

            self.engine._run_auto_tune()
            return jsonify({"status": "Auto-tune cycle triggered"})

        @self.app.route("/api/preopen-flush/run", methods=["POST"])
        def run_preopen_flush():
            """Manually trigger the low-float pre-open flush. Same code path
            the 09:25 ET cron uses — closes any low_float_catalyst positions."""
            self.engine._flush_low_float_before_open()
            return jsonify({"status": "Pre-open flush triggered"})

        @self.app.route("/api/reconcile/mirror/run", methods=["POST"])
        def run_mirror_reconcile():
            """Run the TradersPost mirror-account orphan reconciliation.

            Walks signal_log, nets equity buys vs exits, and reports any
            positive net the engine isn't tracking. Pass ?close=true to also
            send a mirror EXIT webhook flattening each orphan."""
            close = request.args.get("close", "").lower() in ("1", "true", "yes")
            try:
                orphans = self.engine._reconcile_mirror_orphans(close=close)
            except Exception as e:
                return jsonify({"error": f"reconcile failed: {e}"}), 500
            return jsonify({
                "status": "Mirror reconcile complete",
                "closed": close,
                "orphans": [{"symbol": s, "quantity": q} for s, q in orphans],
            })

        @self.app.route("/api/hedging")
        def hedging():
            if self.engine.hedging_manager:
                return jsonify(self.engine.hedging_manager.get_status())
            return jsonify({"enabled": False})

        # --- Control APIs (for mobile) ---

        @self.app.route("/api/control/pause", methods=["POST"])
        def pause():
            self.engine.paused = True
            log.info("Bot PAUSED via dashboard")
            if self.engine.notifier:
                self.engine.notifier.system_alert("Bot paused via mobile", level="warning")
            return jsonify({"status": "paused"})

        @self.app.route("/api/control/resume", methods=["POST"])
        def resume():
            self.engine.paused = False
            log.info("Bot RESUMED via dashboard")
            if self.engine.notifier:
                self.engine.notifier.system_alert("Bot resumed via mobile", level="success")
            return jsonify({"status": "running"})

        @self.app.route("/api/control/close/<symbol>", methods=["POST"])
        def close_position(symbol):
            """Close a single position and report the TRUTHFUL outcome.

            Old behavior returned {"status": "closed"} regardless — the
            session-10 DELL incident: user called this, response said
            "closed", but the IBKR worker was wedged and the close sat
            in queue → position slept overnight uncovered (lost $821).

            New behavior reports actual state observed after the call:
              - "closed"  → position no longer in self.engine.positions
              - "queued"  → still tracked but pending in
                            _slippage_close_queue / next monitor cycle
              - "failed"  → still tracked, no pending queue entry. Most
                            likely IBKR worker wedge or broker rejection.
                            HTTP 502 so operator sees the real state.
            """
            symbol = symbol.upper()
            if symbol not in self.engine.positions:
                return jsonify({"error": f"No position in {symbol}"}), 404

            self.engine._close_position(symbol, "manual", "Closed via mobile dashboard")

            # Post-call observation
            if symbol not in self.engine.positions:
                return jsonify({"status": "closed", "symbol": symbol})

            close_queue = getattr(self.engine, "_slippage_close_queue", []) or []
            recently_closed = getattr(self.engine, "_recently_closed", {}) or {}
            if symbol in close_queue or symbol in recently_closed:
                return jsonify({
                    "status": "queued",
                    "symbol": symbol,
                    "detail": "Close issued but position still tracked; will retry next monitor cycle.",
                }), 202

            return jsonify({
                "status": "failed",
                "symbol": symbol,
                "detail": "Close call returned but position still tracked. "
                          "Likely IBKR worker wedge or broker rejection — "
                          "inspect logs.",
            }), 502

        @self.app.route("/api/control/close-all", methods=["POST"])
        def close_all():
            """Same truthful-status treatment as the single-close endpoint:
            count what actually closed, not what we asked to close."""
            before = set(self.engine.positions)
            self.engine._close_all_positions("Manual close-all via mobile dashboard")
            after = set(self.engine.positions)
            closed = before - after
            still_open = before & after

            if not still_open:
                return jsonify({
                    "status": "closed_all",
                    "count": len(closed),
                    "closed": sorted(closed),
                })
            return jsonify({
                "status": "partial",
                "closed_count": len(closed),
                "closed": sorted(closed),
                "still_open": sorted(still_open),
                "detail": f"{len(still_open)} position(s) still tracked after "
                          f"close — inspect logs for broker/worker errors.",
            }), 502

        @self.app.route("/api/control/emergency-stop", methods=["POST"])
        def emergency_stop():
            self.engine._close_all_positions("EMERGENCY STOP via mobile")
            self.engine.running = False
            log.critical("EMERGENCY STOP triggered via mobile dashboard")
            if self.engine.notifier:
                self.engine.notifier.system_alert("EMERGENCY STOP via mobile", level="error")
            return jsonify({"status": "stopped", "positions_closed": True})

        @self.app.route("/api/control/reconnect-ibkr", methods=["POST"])
        def reconnect_ibkr():
            """Force IBKR reconnection attempt."""
            if self.engine.broker:
                try:
                    self.engine.broker.disconnect()
                except Exception:
                    pass
                try:
                    connected = self.engine.broker.connect()
                    if connected:
                        if self.engine.notifier:
                            self.engine.notifier.system_alert(
                                "IBKR reconnected via dashboard", level="success"
                            )
                        return jsonify({"status": "connected"})
                    else:
                        return jsonify({"status": "failed", "reason": "connect returned false"})
                except Exception as e:
                    return jsonify({"status": "failed", "reason": str(e)})
            return jsonify({"status": "failed", "reason": "no broker configured"})

        # --- Signal Routing API ---

        @self.app.route("/api/signal", methods=["POST"])
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
                # Try the streaming cache first (free / instant for the ~95 streamed symbols).
                price = self.engine.market_data.get_price(symbol) if self.engine.market_data else None
                # Crypto path: Yahoo direct (~5s age, 24/7). Crypto isn't on the
                # IBKR streaming budget and IBKR's PAXOS crypto coverage is
                # limited/paper-uncertain. Try this before the IBKR snapshot.
                if not price and self.engine.market_data and hasattr(self.engine.market_data, "get_crypto_price"):
                    try:
                        price = self.engine.market_data.get_crypto_price(symbol)
                    except Exception:
                        price = None
                # Equity fallback: one-off broker snapshot for symbols outside the
                # streaming subscription (META, etc.) — one IBKR API call, no
                # streaming line burned.
                if not price and self.engine.broker and hasattr(self.engine.broker, "get_snapshot_price"):
                    try:
                        price = self.engine.broker.get_snapshot_price(symbol)
                    except Exception:
                        price = None
                if price:
                    signal["price"] = price
                else:
                    return jsonify({"error": f"No price available for {symbol}. Provide 'price' in request."}), 400

            if data.get("quantity"):
                # Crypto trades fractionally (0.001 BTC, 0.01 ETH); equities are
                # whole shares. Accept float so /api/signal can submit either.
                # Stocks get an int cast downstream; crypto preserves the float.
                qty = float(data["quantity"])
                signal["quantity"] = qty if self.engine._is_crypto_symbol(symbol) else int(qty)
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
        def politician_check():
            """Manually trigger check for new politician trades."""
            if self.engine.politician_tracker:
                trades = self.engine.politician_tracker.manual_check()
                return jsonify({"status": "checked", "new_trades": len(trades), "trades": trades})
            return jsonify({"error": "Politician tracker not enabled"}), 404

        @self.app.route("/api/politicians/add", methods=["POST"])
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
        def watchlist_add():
            data = request.get_json()
            if not data or not data.get("symbol"):
                return jsonify({"error": "symbol required"}), 400
            symbols = self.engine.add_to_watchlist(data["symbol"])
            return jsonify({"status": "added", "watchlist": symbols})

        @self.app.route("/api/watchlist/remove", methods=["POST"])
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
        def set_profile():
            """Switch trading mode profile (scalp/swing/invest)."""
            data = request.get_json()
            profile = data.get("profile", "") if data else ""
            if self.engine.apply_trading_profile(profile):
                return jsonify({"status": "ok", "profile": profile})
            return jsonify({"error": f"Unknown profile: {profile}"}), 400

        @self.app.route("/api/settings/update", methods=["POST"])
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
