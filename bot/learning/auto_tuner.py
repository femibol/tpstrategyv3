"""
Auto-Tuner - Autonomous strategy parameter optimization.

Takes AI insights and statistical learning, then AUTOMATICALLY applies
safe parameter adjustments. No human needed.

Safety guardrails:
- Every parameter has hard min/max bounds (can't go crazy)
- Max adjustment per cycle is capped (gradual, not sudden)
- All changes are logged and reversible
- Requires minimum trade history before tuning
- Notifies on every change made

This is what makes the bot get smarter while you sleep.
"""
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from copy import deepcopy

import requests

from bot.utils.logger import get_logger

log = get_logger("learning.auto_tuner")

# Hard safety bounds for every tunable parameter
# Format: {param: (min, max, max_step_per_cycle)}
PARAM_BOUNDS = {
    # Risk parameters
    "stop_loss_pct": (0.015, 0.08, 0.005),          # 1.5% - 8%, max 0.5% step
    "trailing_stop_pct": (0.008, 0.05, 0.004),       # 0.8% - 5%, max 0.4% step
    "take_profit_pct": (0.03, 0.15, 0.01),            # 3% - 15%, max 1% step
    "risk_per_trade_pct": (0.005, 0.025, 0.003),      # 0.5% - 2.5%, max 0.3% step

    # Mean Reversion
    "mr_entry_zscore": (-3.0, -1.0, 0.3),             # z-score entry threshold
    "mr_rsi_oversold": (20, 40, 3),                    # RSI oversold level
    "mr_rsi_overbought": (60, 80, 3),                  # RSI overbought level
    "mr_bollinger_std": (1.5, 3.0, 0.2),               # Bollinger band width

    # Momentum
    "mom_adx_threshold": (18, 35, 3),                  # ADX trend strength
    "mom_volume_surge": (1.2, 3.0, 0.2),               # Volume multiplier
    "mom_atr_stop_mult": (1.0, 3.5, 0.3),              # ATR stop multiplier
    "mom_atr_target_mult": (2.0, 6.0, 0.5),            # ATR target multiplier

    # RVOL Momentum
    "rvol_min_rvol": (1.5, 4.0, 0.3),                  # Min relative volume
    "rvol_min_score": (40, 80, 5),                      # Min money machine score
    "rvol_atr_stop_mult": (1.0, 3.0, 0.3),
    "rvol_atr_target_mult": (2.0, 5.0, 0.5),

    # SMC Forever
    "smc_risk_reward_min": (1.5, 4.0, 0.3),            # Min R:R ratio
    "smc_fvg_min_size_pct": (0.0005, 0.003, 0.0003),   # FVG size threshold
    "smc_displacement_mult": (1.0, 2.5, 0.2),          # Displacement ATR mult

    # RVOL Scalp
    "rvol_scalp_min_rvol": (1.5, 5.0, 0.3),              # Min relative volume
    "rvol_scalp_min_score": (40, 80, 5),                   # Min money machine score
    "rvol_scalp_atr_stop_mult": (0.5, 2.5, 0.2),          # ATR stop multiplier
    "rvol_scalp_atr_target_mult": (1.0, 4.0, 0.3),        # ATR target multiplier

    # VWAP Scalp
    "vwap_min_distance": (0.001, 0.008, 0.001),        # Min distance from VWAP
    "vwap_max_distance": (0.008, 0.025, 0.002),        # Max distance from VWAP

    # Daily Trend Rider (multi-day swing strategy)
    "tr_min_green_days": (2, 6, 1),                    # Consecutive green daily closes required
    "tr_adx_threshold": (20, 40, 3),                   # Daily ADX minimum
    "tr_atr_stop_mult": (1.0, 2.5, 0.2),               # Daily-ATR stop multiplier
    "tr_max_positions": (1, 5, 1),                     # Concurrent trend riders (overnight bucket)
    "tr_rotation_score_ratio": (1.10, 2.00, 0.10),     # How much better a new candidate must score to rotate
    "tr_max_hold_days": (10, 30, 2),                   # Safety cap before force-exit

    # Strategy allocation (how much capital each strategy gets)
    "alloc_smc_forever": (0.10, 0.40, 0.05),
    "alloc_mean_reversion": (0.05, 0.35, 0.05),
    "alloc_momentum": (0.05, 0.35, 0.05),
    "alloc_rvol_momentum": (0.05, 0.35, 0.05),
    "alloc_vwap_scalp": (0.03, 0.25, 0.03),
    "alloc_rvol_scalp": (0.05, 0.35, 0.05),
    "alloc_pairs_trading": (0.05, 0.30, 0.05),
    "alloc_daily_trend_rider": (0.05, 0.35, 0.05),
}

# Map from AI response keys to config paths
PARAM_TO_CONFIG = {
    "stop_loss_pct": ("settings", "risk", "stop_loss_pct"),
    "trailing_stop_pct": ("settings", "risk", "trailing_stop_pct"),
    "take_profit_pct": ("settings", "risk", "take_profit_pct"),
    "risk_per_trade_pct": ("settings", "risk", "risk_per_trade_pct"),
    "mr_entry_zscore": ("strategies", "mean_reversion", "entry_zscore"),
    "mr_rsi_oversold": ("strategies", "mean_reversion", "rsi_oversold"),
    "mr_rsi_overbought": ("strategies", "mean_reversion", "rsi_overbought"),
    "mr_bollinger_std": ("strategies", "mean_reversion", "bollinger_std"),
    "mom_adx_threshold": ("strategies", "momentum", "adx_threshold"),
    "mom_volume_surge": ("strategies", "momentum", "volume_surge_multiplier"),
    "mom_atr_stop_mult": ("strategies", "momentum", "atr_stop_multiplier"),
    "mom_atr_target_mult": ("strategies", "momentum", "atr_target_multiplier"),
    "rvol_min_rvol": ("strategies", "rvol_momentum", "min_rvol"),
    "rvol_min_score": ("strategies", "rvol_momentum", "min_score"),
    "rvol_atr_stop_mult": ("strategies", "rvol_momentum", "atr_stop_multiplier"),
    "rvol_atr_target_mult": ("strategies", "rvol_momentum", "atr_target_multiplier"),
    "smc_risk_reward_min": ("strategies", "smc_forever", "risk_reward_min"),
    "smc_fvg_min_size_pct": ("strategies", "smc_forever", "fvg_min_size_pct"),
    "smc_displacement_mult": ("strategies", "smc_forever", "displacement_atr_mult"),
    "rvol_scalp_min_rvol": ("strategies", "rvol_scalp", "min_rvol"),
    "rvol_scalp_min_score": ("strategies", "rvol_scalp", "min_score"),
    "rvol_scalp_atr_stop_mult": ("strategies", "rvol_scalp", "atr_stop_multiplier"),
    "rvol_scalp_atr_target_mult": ("strategies", "rvol_scalp", "atr_target_multiplier"),
    "vwap_min_distance": ("strategies", "vwap_scalp", "min_distance_from_vwap"),
    "vwap_max_distance": ("strategies", "vwap_scalp", "max_distance_from_vwap"),
    "tr_min_green_days": ("strategies", "daily_trend_rider", "min_green_days"),
    "tr_adx_threshold": ("strategies", "daily_trend_rider", "adx_threshold"),
    "tr_atr_stop_mult": ("strategies", "daily_trend_rider", "atr_stop_multiplier"),
    "tr_max_positions": ("strategies", "daily_trend_rider", "max_positions"),
    "tr_rotation_score_ratio": ("strategies", "daily_trend_rider", "rotation_score_ratio"),
    "tr_max_hold_days": ("strategies", "daily_trend_rider", "max_hold_days"),
    "alloc_smc_forever": ("strategies", "allocation", "smc_forever"),
    "alloc_mean_reversion": ("strategies", "allocation", "mean_reversion"),
    "alloc_momentum": ("strategies", "allocation", "momentum"),
    "alloc_rvol_momentum": ("strategies", "allocation", "rvol_momentum"),
    "alloc_vwap_scalp": ("strategies", "allocation", "vwap_scalp"),
    "alloc_rvol_scalp": ("strategies", "allocation", "rvol_scalp"),
    "alloc_pairs_trading": ("strategies", "allocation", "pairs_trading"),
    "alloc_daily_trend_rider": ("strategies", "allocation", "daily_trend_rider"),
}


class AutoTuner:
    """
    Autonomous parameter tuning engine.

    Flow:
    1. Collects trade history + performance data
    2. Asks Claude for structured JSON parameter recommendations
    3. Validates each recommendation against safety bounds
    4. Applies changes gradually (capped step size per cycle)
    5. Saves a full changelog for transparency
    6. Notifies on every change

    Runs automatically after EOD analysis + once midday.
    """

    def __init__(self, config, data_dir=None):
        self.config = config
        self.data_dir = Path(data_dir) if data_dir else Path(config.base_dir) / "data"
        self.data_dir.mkdir(exist_ok=True)

        import os
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = "claude-sonnet-4-5-20250514"
        self.api_url = "https://api.anthropic.com/v1/messages"

        # Tuning state
        self._changelog_file = self.data_dir / "auto_tune_changelog.json"
        self._changelog = self._load_changelog()
        self._last_tune_time = 0
        self._min_tune_interval = 3600  # Min 1 hour between tunes

        # Minimum trades before we start auto-tuning
        self.min_trades_to_tune = 15

        if self.api_key:
            log.info("Auto-Tuner enabled (Claude API configured)")
        else:
            log.info("Auto-Tuner disabled (set ANTHROPIC_API_KEY)")

    def is_available(self):
        return bool(self.api_key)

    def run_auto_tune(self, trade_history, performance_stats, strategy_scores,
                      regime_data=None, notifier=None):
        """
        Main entry point: analyze trades and auto-apply improvements.

        Returns dict with changes made and reasoning.
        """
        if not self.api_key:
            return {"applied": False, "reason": "No API key"}

        # Rate limit
        now = time.time()
        if (now - self._last_tune_time) < self._min_tune_interval:
            return {"applied": False, "reason": "Too soon since last tune"}

        if len(trade_history) < self.min_trades_to_tune:
            return {"applied": False, "reason": f"Need {self.min_trades_to_tune} trades, have {len(trade_history)}"}

        log.info("=== AUTO-TUNER: Starting autonomous optimization ===")

        # Get current parameter values
        current_params = self._get_current_params()

        # Ask Claude for structured recommendations
        recommendations = self._get_ai_recommendations(
            trade_history, performance_stats, strategy_scores,
            current_params, regime_data
        )

        if not recommendations:
            return {"applied": False, "reason": "No recommendations from AI"}

        # Validate and apply each recommendation
        changes_made = []
        for param_key, new_value in recommendations.items():
            if param_key not in PARAM_BOUNDS:
                continue

            current_value = current_params.get(param_key)
            if current_value is None:
                continue

            # Apply safety bounds
            safe_value = self._apply_bounds(param_key, current_value, new_value)

            if safe_value is not None and abs(safe_value - current_value) > 1e-6:
                # Apply the change
                self._apply_param(param_key, safe_value)
                change = {
                    "param": param_key,
                    "old": round(current_value, 6),
                    "new": round(safe_value, 6),
                    "ai_suggested": round(new_value, 6),
                    "timestamp": datetime.now().isoformat(),
                }
                changes_made.append(change)
                log.info(
                    f"AUTO-TUNE: {param_key}: {current_value} -> {safe_value} "
                    f"(AI suggested {new_value})"
                )

        # Normalize allocations if any were changed
        alloc_changed = any(c["param"].startswith("alloc_") for c in changes_made)
        if alloc_changed:
            self._normalize_allocations()

        # Save changelog
        if changes_made:
            self._save_changelog(changes_made, recommendations)
            self._last_tune_time = now

            # Save updated configs to disk
            self.config.save_settings()
            self._save_strategies_yaml()

            # Notify
            change_summary = "\n".join(
                f"  {c['param']}: {c['old']} -> {c['new']}"
                for c in changes_made
            )
            log.info(f"AUTO-TUNE: Applied {len(changes_made)} parameter changes")

            if notifier:
                notifier.system_alert(
                    f"Auto-Tuner applied {len(changes_made)} improvements:\n{change_summary}",
                    level="info"
                )

            return {
                "applied": True,
                "changes": changes_made,
                "total_changes": len(changes_made),
            }

        log.info("AUTO-TUNE: No changes needed - current params are optimal")
        self._last_tune_time = now
        return {"applied": False, "reason": "Parameters already near optimal"}

    def _get_current_params(self):
        """Read current parameter values from config."""
        params = {}

        # Risk params
        risk = self.config.settings.get("risk", {})
        params["stop_loss_pct"] = risk.get("stop_loss_pct", 0.03)
        params["trailing_stop_pct"] = risk.get("trailing_stop_pct", 0.02)
        params["take_profit_pct"] = risk.get("take_profit_pct", 0.06)
        params["risk_per_trade_pct"] = risk.get("risk_per_trade_pct", 0.01)

        # Mean Reversion
        mr = self.config.strategies.get("mean_reversion", {})
        params["mr_entry_zscore"] = mr.get("entry_zscore", -2.0)
        params["mr_rsi_oversold"] = mr.get("rsi_oversold", 30)
        params["mr_rsi_overbought"] = mr.get("rsi_overbought", 70)
        params["mr_bollinger_std"] = mr.get("bollinger_std", 2.0)

        # Momentum
        mom = self.config.strategies.get("momentum", {})
        params["mom_adx_threshold"] = mom.get("adx_threshold", 25)
        params["mom_volume_surge"] = mom.get("volume_surge_multiplier", 1.5)
        params["mom_atr_stop_mult"] = mom.get("atr_stop_multiplier", 2.0)
        params["mom_atr_target_mult"] = mom.get("atr_target_multiplier", 4.0)

        # RVOL Momentum
        rvol = self.config.strategies.get("rvol_momentum", {})
        params["rvol_min_rvol"] = rvol.get("min_rvol", 2.0)
        params["rvol_min_score"] = rvol.get("min_score", 60)
        params["rvol_atr_stop_mult"] = rvol.get("atr_stop_multiplier", 1.5)
        params["rvol_atr_target_mult"] = rvol.get("atr_target_multiplier", 3.0)

        # SMC Forever
        smc = self.config.strategies.get("smc_forever", {})
        params["smc_risk_reward_min"] = smc.get("risk_reward_min", 2.0)
        params["smc_fvg_min_size_pct"] = smc.get("fvg_min_size_pct", 0.001)
        params["smc_displacement_mult"] = smc.get("displacement_atr_mult", 1.5)

        # RVOL Scalp
        rvol_scalp = self.config.strategies.get("rvol_scalp", {})
        params["rvol_scalp_min_rvol"] = rvol_scalp.get("min_rvol", 2.5)
        params["rvol_scalp_min_score"] = rvol_scalp.get("min_score", 60)
        params["rvol_scalp_atr_stop_mult"] = rvol_scalp.get("atr_stop_multiplier", 1.0)
        params["rvol_scalp_atr_target_mult"] = rvol_scalp.get("atr_target_multiplier", 2.0)

        # VWAP Scalp
        vwap = self.config.strategies.get("vwap_scalp", {})
        params["vwap_min_distance"] = vwap.get("min_distance_from_vwap", 0.003)
        params["vwap_max_distance"] = vwap.get("max_distance_from_vwap", 0.015)

        # Daily Trend Rider (multi-day swing)
        tr = self.config.strategies.get("daily_trend_rider", {})
        params["tr_min_green_days"] = tr.get("min_green_days", 3)
        params["tr_adx_threshold"] = tr.get("adx_threshold", 25)
        params["tr_atr_stop_mult"] = tr.get("atr_stop_multiplier", 1.5)
        params["tr_max_positions"] = tr.get("max_positions", 3)
        params["tr_rotation_score_ratio"] = tr.get("rotation_score_ratio", 1.25)
        params["tr_max_hold_days"] = tr.get("max_hold_days", 20)

        # Allocations
        alloc = self.config.strategies.get("allocation", {})
        params["alloc_smc_forever"] = alloc.get("smc_forever", 0.25)
        params["alloc_mean_reversion"] = alloc.get("mean_reversion", 0.15)
        params["alloc_momentum"] = alloc.get("momentum", 0.15)
        params["alloc_rvol_momentum"] = alloc.get("rvol_momentum", 0.20)
        params["alloc_vwap_scalp"] = alloc.get("vwap_scalp", 0.10)
        params["alloc_rvol_scalp"] = alloc.get("rvol_scalp", 0.15)
        params["alloc_pairs_trading"] = alloc.get("pairs_trading", 0.15)
        params["alloc_daily_trend_rider"] = alloc.get("daily_trend_rider", 0.15)

        return params

    def _get_ai_recommendations(self, trade_history, performance,
                                strategy_scores, current_params, regime_data):
        """Ask Claude for structured parameter recommendations."""
        recent = trade_history[-75:]  # Last 75 trades

        # Build trade summary
        trade_summary = []
        for t in recent:
            trade_summary.append({
                "symbol": t.get("symbol"),
                "pnl": round(t.get("pnl", 0), 2),
                "pnl_pct": round(t.get("pnl_pct", 0) * 100, 2),
                "strategy": t.get("strategy"),
                "exit_reason": t.get("reason"),
                "direction": t.get("direction"),
            })

        # Per-strategy stats
        strat_stats = {}
        from collections import defaultdict
        stats = defaultdict(lambda: {"trades": 0, "wins": 0, "total_pnl": 0.0})
        for t in recent:
            s = t.get("strategy", "unknown")
            stats[s]["trades"] += 1
            stats[s]["total_pnl"] += t.get("pnl", 0)
            if t.get("pnl", 0) > 0:
                stats[s]["wins"] += 1
        for s, d in stats.items():
            d["win_rate"] = round(d["wins"] / d["trades"] * 100, 1) if d["trades"] > 0 else 0
            strat_stats[s] = dict(d)

        # Exit reason analysis
        exit_stats = defaultdict(lambda: {"count": 0, "total_pnl": 0.0, "wins": 0})
        for t in recent:
            reason = t.get("reason", "unknown")
            exit_stats[reason]["count"] += 1
            exit_stats[reason]["total_pnl"] += t.get("pnl", 0)
            if t.get("pnl", 0) > 0:
                exit_stats[reason]["wins"] += 1

        prompt = f"""You are an algorithmic trading system optimizer. Analyze the trading data and return ONLY a JSON object with parameter adjustments.

CURRENT PARAMETERS:
{json.dumps(current_params, indent=2)}

PARAMETER BOUNDS (min, max):
{json.dumps({k: {"min": v[0], "max": v[1]} for k, v in PARAM_BOUNDS.items()}, indent=2)}

RECENT TRADE DATA ({len(recent)} trades):
{json.dumps(trade_summary, indent=2)}

PER-STRATEGY PERFORMANCE:
{json.dumps(strat_stats, indent=2)}

EXIT REASON ANALYSIS:
{json.dumps(dict(exit_stats), indent=2)}

OVERALL PERFORMANCE:
{json.dumps(performance, indent=2)}

STRATEGY SCORES (learning system):
{json.dumps(strategy_scores, indent=2)}

MARKET REGIME: {json.dumps(regime_data) if regime_data else "unknown"}

RULES:
1. Only adjust parameters that will improve profitability based on the data
2. If stops are getting hit too often, widen them slightly
3. If profits are being left on the table (exits too early), increase take profit
4. Increase allocation to strategies with higher win rates and positive P&L
5. Decrease allocation to consistently losing strategies
6. If a strategy has <35% win rate, reduce its allocation significantly
7. If a strategy has >60% win rate and positive P&L, increase its allocation
8. Keep allocations summing to approximately 1.0
9. Only suggest changes you're confident will help based on the data pattern
10. If current parameters look optimal, return empty object

Return ONLY valid JSON (no markdown, no explanation) with parameter keys and new values:
{{"param_key": new_value, ...}}

Example: {{"stop_loss_pct": 0.035, "alloc_momentum": 0.20, "mom_adx_threshold": 22}}"""

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        try:
            response = requests.post(
                self.api_url,
                headers=headers,
                json={
                    "model": self.model,
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )

            if response.status_code == 200:
                data = response.json()
                content = data.get("content", [])
                if content and content[0].get("type") == "text":
                    text = content[0]["text"].strip()
                    # Parse JSON response
                    # Strip markdown code fences if present
                    if text.startswith("```"):
                        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                        if text.endswith("```"):
                            text = text[:-3]
                        text = text.strip()
                    recommendations = json.loads(text)
                    log.info(f"AI recommendations received: {len(recommendations)} params")
                    return recommendations
            else:
                log.error(f"Auto-Tuner API error {response.status_code}: {response.text[:200]}")

        except json.JSONDecodeError as e:
            log.error(f"Auto-Tuner: Could not parse AI response as JSON: {e}")
        except Exception as e:
            log.error(f"Auto-Tuner API call failed: {e}")

        return None

    def _apply_bounds(self, param_key, current_value, suggested_value):
        """Apply safety bounds and max step size to a parameter change."""
        bounds = PARAM_BOUNDS.get(param_key)
        if not bounds:
            return None

        min_val, max_val, max_step = bounds

        # Clamp to bounds
        suggested_value = max(min_val, min(max_val, suggested_value))

        # Limit step size
        delta = suggested_value - current_value
        if abs(delta) > max_step:
            suggested_value = current_value + (max_step if delta > 0 else -max_step)

        # Re-clamp after step limiting
        suggested_value = max(min_val, min(max_val, suggested_value))

        return suggested_value

    def _apply_param(self, param_key, value):
        """Apply a parameter change to the live config."""
        mapping = PARAM_TO_CONFIG.get(param_key)
        if not mapping:
            return

        config_type, section, key = mapping

        if config_type == "settings":
            d = self.config.settings
            d.setdefault(section, {})[key] = value
        elif config_type == "strategies":
            d = self.config.strategies
            d.setdefault(section, {})[key] = value

    def _normalize_allocations(self):
        """Ensure strategy allocations sum to 1.0."""
        alloc = self.config.strategies.get("allocation", {})
        total = sum(alloc.values())
        if total > 0 and abs(total - 1.0) > 0.01:
            for key in alloc:
                alloc[key] = round(alloc[key] / total, 3)
            log.info(f"Allocations normalized: {alloc}")

    def _save_strategies_yaml(self):
        """Save strategies config to disk."""
        import yaml
        filepath = self.config.config_dir / "strategies.yaml"
        try:
            with open(filepath, "w") as f:
                yaml.dump(self.config.strategies, f, default_flow_style=False, sort_keys=False)
            log.info("Strategies config saved to disk")
        except Exception as e:
            log.error(f"Could not save strategies config: {e}")

    def _save_changelog(self, changes, full_recommendations):
        """Save tuning changes to changelog."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "changes": changes,
            "ai_full_recommendations": full_recommendations,
        }
        self._changelog.append(entry)
        # Keep last 100 tune cycles
        self._changelog = self._changelog[-100:]

        try:
            with open(self._changelog_file, "w") as f:
                json.dump(self._changelog, f, indent=2)
        except Exception as e:
            log.debug(f"Could not save changelog: {e}")

    def _load_changelog(self):
        """Load tuning changelog from disk."""
        if self._changelog_file.exists():
            try:
                with open(self._changelog_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def get_status(self):
        """Get auto-tuner status for dashboard."""
        recent_changes = self._changelog[-5:] if self._changelog else []
        total_changes = sum(len(e.get("changes", [])) for e in self._changelog)
        return {
            "enabled": self.is_available(),
            "total_tune_cycles": len(self._changelog),
            "total_param_changes": total_changes,
            "recent_changes": recent_changes,
            "last_tune": self._changelog[-1]["timestamp"] if self._changelog else None,
            "min_trades_required": self.min_trades_to_tune,
        }

    def get_changelog(self):
        """Get full changelog for review."""
        return list(self._changelog)
