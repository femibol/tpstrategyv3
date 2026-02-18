"""
Trade Analyzer - Learns from past trades to adapt strategy behavior.

Analyzes trade history to:
1. Adjust strategy allocation weights (winning strategies get more capital)
2. Identify which setups work best in current market conditions
3. Track per-symbol performance and avoid repeat losers
4. Adjust risk parameters based on recent performance
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from bot.utils.logger import get_logger

log = get_logger("learning.trade_analyzer")


class TradeAnalyzer:
    """
    Learns from past trades and adjusts engine behavior.

    Not ML - just smart statistical tracking that adapts:
    - Strategy weights (allocate more to what's working)
    - Symbol bias (avoid symbols that keep losing)
    - Time-of-day patterns (when do we win/lose most)
    - Exit effectiveness (which exit type produces best results)
    """

    def __init__(self, config, data_dir=None):
        self.config = config
        self.data_dir = Path(data_dir) if data_dir else Path(config.base_dir) / "data"
        self.data_dir.mkdir(exist_ok=True)

        # Learning state
        self.strategy_scores = {}      # {strategy: score}
        self.symbol_scores = {}        # {symbol: score}
        self.exit_effectiveness = {}   # {exit_type: {wins, losses, avg_pnl}}
        self.hourly_performance = defaultdict(lambda: {"trades": 0, "pnl": 0.0})
        self.regime_performance = defaultdict(lambda: defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0}))

        # Adaptation settings (aggressive - learn fast, adapt fast)
        self.lookback_trades = 100     # How many trades to analyze
        self.min_trades_for_adjustment = 5   # Start adjusting after just 5 trades
        self.weight_adjustment_rate = 0.20   # Max 20% shift per adaptation cycle
        self.cooldown_losses = 2       # 2 consecutive losses triggers weight reduction

        # Trade history persistence
        self._trades_file = self.data_dir / "trade_history.json"
        self._persisted_trades = self._load_trade_history()

        # Load saved learning data
        self._load_state()

    def persist_trade(self, trade):
        """Save a completed trade to persistent storage (survives restarts)."""
        self._persisted_trades.append(trade)
        # Keep last 500 trades
        self._persisted_trades = self._persisted_trades[-500:]
        try:
            with open(self._trades_file, "w") as f:
                json.dump(self._persisted_trades, f, indent=2)
            log.debug(f"Trade persisted: {trade.get('symbol')} {trade.get('pnl', 0):+.2f}")
        except Exception as e:
            log.debug(f"Could not persist trade: {e}")

    def get_persisted_trades(self):
        """Get all persisted trades (for AI analysis across restarts)."""
        return list(self._persisted_trades)

    def _load_trade_history(self):
        """Load trade history from disk."""
        if self._trades_file.exists():
            try:
                with open(self._trades_file, "r") as f:
                    trades = json.load(f)
                log.info(f"Loaded {len(trades)} persisted trades from disk")
                return trades
            except Exception as e:
                log.debug(f"Could not load trade history: {e}")
        return []

    def analyze(self, trade_history, current_regime=None):
        """
        Run full analysis on trade history. Returns adjustment recommendations.

        Args:
            trade_history: List of trade dicts from engine
            current_regime: Current market regime string

        Returns:
            dict with recommended adjustments
        """
        if len(trade_history) < self.min_trades_for_adjustment:
            return {"adjustments": [], "reason": "Not enough trades to analyze"}

        recent = trade_history[-self.lookback_trades:]

        # Analyze by strategy
        strategy_perf = self._analyze_by_strategy(recent)

        # Analyze by symbol
        symbol_perf = self._analyze_by_symbol(recent)

        # Analyze exit types
        exit_perf = self._analyze_exits(recent)

        # Analyze time patterns
        time_perf = self._analyze_time_patterns(recent)

        # Analyze regime-specific performance
        regime_perf = self._analyze_regime_performance(recent)

        # Generate recommendations
        recommendations = self._generate_recommendations(
            strategy_perf, symbol_perf, exit_perf, time_perf, regime_perf, current_regime
        )

        # Save state
        self._save_state()

        return recommendations

    def _analyze_by_strategy(self, trades):
        """Analyze per-strategy performance."""
        stats = defaultdict(lambda: {
            "trades": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "avg_pnl": 0.0,
            "recent_streak": 0, "largest_win": 0.0, "largest_loss": 0.0,
        })

        for trade in trades:
            strat = trade.get("strategy", "unknown")
            s = stats[strat]
            pnl = trade.get("pnl", 0)

            s["trades"] += 1
            s["total_pnl"] += pnl

            if pnl > 0:
                s["wins"] += 1
                s["largest_win"] = max(s["largest_win"], pnl)
                s["recent_streak"] = max(0, s["recent_streak"]) + 1
            elif pnl < 0:
                s["losses"] += 1
                s["largest_loss"] = min(s["largest_loss"], pnl)
                s["recent_streak"] = min(0, s["recent_streak"]) - 1

        for strat, s in stats.items():
            if s["trades"] > 0:
                s["avg_pnl"] = s["total_pnl"] / s["trades"]
                s["win_rate"] = s["wins"] / s["trades"] * 100

            # Calculate score: combines win rate, average P&L, and recency
            score = 0
            if s["trades"] >= 5:
                # Win rate contribution (0-50)
                score += min(50, s.get("win_rate", 0) * 0.5)
                # Profit factor contribution (0-30)
                if s["losses"] > 0 and s["wins"] > 0:
                    pf = (s["total_pnl"] + abs(s["largest_loss"] * s["losses"])) / abs(s["largest_loss"] * s["losses"])
                    score += min(30, pf * 10)
                elif s["wins"] > 0:
                    score += 30
                # Streak bonus/penalty (-20 to +20)
                score += max(-20, min(20, s["recent_streak"] * 5))

            self.strategy_scores[strat] = round(score, 1)

        return dict(stats)

    def _analyze_by_symbol(self, trades):
        """Analyze per-symbol performance."""
        stats = defaultdict(lambda: {"trades": 0, "wins": 0, "total_pnl": 0.0})

        for trade in trades:
            symbol = trade.get("symbol", "")
            pnl = trade.get("pnl", 0)
            stats[symbol]["trades"] += 1
            stats[symbol]["total_pnl"] += pnl
            if pnl > 0:
                stats[symbol]["wins"] += 1

        for symbol, s in stats.items():
            if s["trades"] > 0:
                s["win_rate"] = s["wins"] / s["trades"] * 100
                s["avg_pnl"] = s["total_pnl"] / s["trades"]

            # Score: positive = good symbol, negative = avoid
            score = s.get("avg_pnl", 0) * 10
            if s["trades"] >= 3 and s.get("win_rate", 0) < 30:
                score -= 20  # Penalty for consistently losing symbol
            self.symbol_scores[symbol] = round(score, 1)

        return dict(stats)

    def _analyze_exits(self, trades):
        """Analyze which exit types produce best results."""
        stats = defaultdict(lambda: {"trades": 0, "wins": 0, "total_pnl": 0.0})

        for trade in trades:
            reason = trade.get("reason", "unknown")
            pnl = trade.get("pnl", 0)
            stats[reason]["trades"] += 1
            stats[reason]["total_pnl"] += pnl
            if pnl > 0:
                stats[reason]["wins"] += 1

        for reason, s in stats.items():
            if s["trades"] > 0:
                s["avg_pnl"] = s["total_pnl"] / s["trades"]
                s["win_rate"] = s["wins"] / s["trades"] * 100

        self.exit_effectiveness = dict(stats)
        return dict(stats)

    def _analyze_time_patterns(self, trades):
        """Analyze performance by time of day."""
        for trade in trades:
            entry_time = trade.get("entry_time", "")
            if not entry_time:
                continue
            try:
                dt = datetime.fromisoformat(entry_time)
                hour = dt.hour
                h = self.hourly_performance[hour]
                h["trades"] += 1
                h["pnl"] += trade.get("pnl", 0)
            except (ValueError, TypeError):
                continue

        return dict(self.hourly_performance)

    def _analyze_regime_performance(self, trades):
        """Track how strategies perform in different market regimes."""
        # This gets populated externally via record_regime_trade()
        return dict(self.regime_performance)

    def record_regime_trade(self, strategy, regime, pnl):
        """Record a trade's regime context for learning."""
        r = self.regime_performance[regime][strategy]
        r["trades"] += 1
        r["pnl"] += pnl
        if pnl > 0:
            r["wins"] += 1

    def _generate_recommendations(self, strategy_perf, symbol_perf, exit_perf,
                                   time_perf, regime_perf, current_regime):
        """Generate actionable recommendations based on analysis."""
        recommendations = {
            "strategy_weight_adjustments": {},
            "symbols_to_avoid": [],
            "symbols_performing_well": [],
            "best_exit_types": [],
            "worst_hours": [],
            "best_hours": [],
            "regime_adjustments": {},
        }

        # Strategy weight adjustments (aggressive - reward winners, punish losers fast)
        base_allocation = self.config.strategy_allocation
        if strategy_perf:
            for strat, perf in strategy_perf.items():
                if perf["trades"] < 3:  # Only need 3 trades now
                    continue
                current_weight = base_allocation.get(strat, 0.20)
                win_rate = perf.get("win_rate", 50)

                # Boost winning strategies faster
                if win_rate > 50 and perf["total_pnl"] > 0:
                    adj = min(self.weight_adjustment_rate, (win_rate - 40) / 80)
                    recommendations["strategy_weight_adjustments"][strat] = round(
                        min(0.45, current_weight + adj), 3
                    )
                # Cut losing strategies harder
                elif win_rate < 40 and perf["total_pnl"] < 0:
                    adj = min(self.weight_adjustment_rate, (50 - win_rate) / 60)
                    recommendations["strategy_weight_adjustments"][strat] = round(
                        max(0.03, current_weight - adj), 3
                    )

        # Symbol recommendations (aggressive - avoid losers faster, favor winners)
        for symbol, perf in symbol_perf.items():
            if perf["trades"] >= 2:  # Only 2 trades needed to start learning
                if perf.get("win_rate", 0) < 35 and perf["total_pnl"] < 0:
                    recommendations["symbols_to_avoid"].append({
                        "symbol": symbol,
                        "win_rate": perf.get("win_rate", 0),
                        "total_pnl": round(perf["total_pnl"], 2),
                    })
                elif perf.get("win_rate", 0) > 55 and perf["total_pnl"] > 0:
                    recommendations["symbols_performing_well"].append({
                        "symbol": symbol,
                        "win_rate": perf.get("win_rate", 0),
                        "total_pnl": round(perf["total_pnl"], 2),
                    })

        # Exit type recommendations
        for reason, perf in exit_perf.items():
            if perf["trades"] >= 3:
                avg = perf.get("avg_pnl", 0)
                recommendations["best_exit_types"].append({
                    "type": reason,
                    "avg_pnl": round(avg, 2),
                    "win_rate": perf.get("win_rate", 0),
                    "count": perf["trades"],
                })
        recommendations["best_exit_types"].sort(key=lambda x: x["avg_pnl"], reverse=True)

        # Time patterns
        for hour, perf in time_perf.items():
            if perf["trades"] >= 3:
                avg_pnl = perf["pnl"] / perf["trades"]
                entry = {"hour": hour, "avg_pnl": round(avg_pnl, 2), "trades": perf["trades"]}
                if avg_pnl < 0:
                    recommendations["worst_hours"].append(entry)
                else:
                    recommendations["best_hours"].append(entry)

        # Regime-specific adjustments
        if current_regime and current_regime in self.regime_performance:
            regime_data = self.regime_performance[current_regime]
            for strat, perf in regime_data.items():
                if perf["trades"] >= 5:
                    win_rate = perf["wins"] / perf["trades"] * 100 if perf["trades"] > 0 else 0
                    if win_rate < 30:
                        recommendations["regime_adjustments"][strat] = {
                            "action": "reduce",
                            "reason": f"{strat} has {win_rate:.0f}% win rate in {current_regime} regime",
                        }
                    elif win_rate > 60:
                        recommendations["regime_adjustments"][strat] = {
                            "action": "increase",
                            "reason": f"{strat} has {win_rate:.0f}% win rate in {current_regime} regime",
                        }

        return recommendations

    def get_strategy_weights(self, base_allocation):
        """
        Get adjusted strategy weights based on learning.
        Returns modified allocation dict.
        """
        adjusted = dict(base_allocation)

        if not self.strategy_scores:
            return adjusted

        # Only adjust if we have enough data
        total_score = sum(self.strategy_scores.values())
        if total_score <= 0 or len(self.strategy_scores) < 2:
            return adjusted

        # Normalize scores to weights (aggressive rebalancing)
        for strat, score in self.strategy_scores.items():
            if strat in adjusted:
                base = adjusted[strat]
                # Clamp adjustment to +-20% of base
                score_factor = score / max(total_score, 1)
                adjustment = (score_factor - (1 / len(self.strategy_scores))) * self.weight_adjustment_rate
                adjusted[strat] = round(max(0.03, min(0.45, base + adjustment)), 3)

        # Normalize to sum to ~1.0
        total = sum(adjusted.values())
        if total > 0:
            adjusted = {k: round(v / total, 3) for k, v in adjusted.items()}

        return adjusted

    def should_avoid_symbol(self, symbol):
        """Check if a symbol should be avoided based on learning."""
        return self.symbol_scores.get(symbol, 0) < -8  # More sensitive: avoid losers faster

    def get_status(self):
        """Get learning system status for dashboard."""
        return {
            "strategy_scores": self.strategy_scores,
            "symbol_scores": dict(sorted(self.symbol_scores.items(), key=lambda x: x[1])),
            "exit_effectiveness": self.exit_effectiveness,
            "trades_analyzed": sum(1 for _ in self.strategy_scores.values()),
        }

    def _save_state(self):
        """Persist learning state to disk."""
        state = {
            "strategy_scores": self.strategy_scores,
            "symbol_scores": self.symbol_scores,
            "exit_effectiveness": self.exit_effectiveness,
            "hourly_performance": dict(self.hourly_performance),
            "regime_performance": {
                k: dict(v) for k, v in self.regime_performance.items()
            },
            "updated": datetime.now().isoformat(),
        }
        filepath = self.data_dir / "learning_state.json"
        try:
            with open(filepath, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.debug(f"Could not save learning state: {e}")

    def _load_state(self):
        """Load saved learning state from disk."""
        filepath = self.data_dir / "learning_state.json"
        if not filepath.exists():
            return
        try:
            with open(filepath, "r") as f:
                state = json.load(f)
            self.strategy_scores = state.get("strategy_scores", {})
            self.symbol_scores = state.get("symbol_scores", {})
            self.exit_effectiveness = state.get("exit_effectiveness", {})
            for k, v in state.get("hourly_performance", {}).items():
                self.hourly_performance[int(k)] = v
            for regime, strats in state.get("regime_performance", {}).items():
                for strat, perf in strats.items():
                    self.regime_performance[regime][strat] = perf
            log.info("Loaded learning state from disk")
        except Exception as e:
            log.debug(f"Could not load learning state: {e}")
