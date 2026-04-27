"""
AI Trade Insights - Uses Claude API to analyze trades and improve the bot.

Sends trade history + performance data to Claude and gets back:
- What's working and what isn't
- Entry/exit timing improvements
- Strategy-specific adjustments
- Pattern recognition humans miss
- Natural language trade journal

This is the "brain upgrade" - Claude reads your trade log and tells you
how to make more money.
"""
import os
import json
import time
from datetime import datetime
from pathlib import Path

import anthropic

from bot.utils.logger import get_logger

log = get_logger("learning.ai_insights")


SYSTEM_PROMPT = """You are a professional trading coach analyzing an algo trading bot's recent performance.

The user message contains a JSON payload with: recent_trades, performance, open_positions, market_regime, strategy_scores, account_balance.

Analyze the data and respond with these sections:

1. **WHAT'S WORKING** - Which strategies/symbols/patterns are producing profits? Be specific.

2. **WHAT'S NOT WORKING** - What's losing money? Which exits are premature? Any repeated mistakes?

3. **ENTRY TIMING** - Are entries too early/late? Any time-of-day patterns? Should the bot wait for better confirmation?

4. **EXIT IMPROVEMENTS** - Are stops too tight/loose? Are profit targets being hit or is the bot leaving money on the table?

5. **STRATEGY ADJUSTMENTS** - Specific parameter changes (stop %, take profit %, position size) with reasoning.

6. **TOP 3 ACTIONS** - The three most impactful changes to make RIGHT NOW, ranked by expected improvement.

7. **CONFIDENCE SCORE** - Rate the bot's overall health 1-10 and explain why.

Be direct, specific, and actionable. Use actual numbers from the data. Don't be generic - reference specific trades and patterns you see."""


class AIInsights:
    """
    Claude-powered trade analysis and improvement engine.

    Analyzes:
    - Trade history (wins, losses, patterns)
    - Strategy performance by market regime
    - Entry/exit timing
    - Symbol-specific patterns
    - Risk management effectiveness

    Returns actionable insights in natural language + structured adjustments.
    """

    def __init__(self, config, data_dir=None):
        self.config = config
        self.data_dir = Path(data_dir) if data_dir else Path(config.base_dir) / "data"
        self.data_dir.mkdir(exist_ok=True)

        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = "claude-opus-4-6"  # Opus 4.6 — best reasoning for trade decisions
        self.max_tokens = 2000

        self._client = (
            anthropic.Anthropic(api_key=self.api_key, max_retries=3)
            if self.api_key
            else None
        )

        # Local result cache (don't call Claude on every request)
        self._cached_insights = None
        self._cache_time = 0
        self._cache_ttl = 300  # 5 minute cache

        self._insights_file = self.data_dir / "ai_insights_history.json"
        self._insights_history = self._load_insights_history()

        if self.api_key:
            log.info("Claude AI Insights enabled (API key configured)")
        else:
            log.info("Claude AI Insights disabled (set ANTHROPIC_API_KEY to enable)")

    def is_available(self):
        return bool(self.api_key)

    def analyze_trades(self, trade_history, performance_stats, positions,
                       regime_data=None, strategy_scores=None):
        """
        Send trade data to Claude for analysis.

        Returns dict with insights, recommendations, and structured adjustments.
        """
        if not self._client:
            return {
                "available": False,
                "message": "Set ANTHROPIC_API_KEY environment variable to enable AI insights",
            }

        now = time.time()
        if self._cached_insights and (now - self._cache_time) < self._cache_ttl:
            return self._cached_insights

        user_payload = self._build_user_payload(
            trade_history, performance_stats, positions,
            regime_data, strategy_scores
        )

        try:
            response_text = self._call_claude(user_payload)
            if response_text:
                insights = self._parse_response(response_text)
                insights["generated_at"] = datetime.now().isoformat()
                insights["trades_analyzed"] = len(trade_history)
                insights["available"] = True

                self._cached_insights = insights
                self._cache_time = now
                self._save_insight(insights)

                return insights
            return {"available": True, "error": "No response from Claude API"}

        except anthropic.APIError as e:
            log.error(f"AI Insights API error: {e}")
            return {"available": True, "error": str(e)}
        except Exception as e:
            log.error(f"AI Insights error: {e}")
            return {"available": True, "error": str(e)}

    def _build_user_payload(self, trade_history, performance, positions,
                            regime_data, strategy_scores):
        """Format the trading data as a JSON payload for the user message."""
        recent_trades = trade_history[-50:] if trade_history else []

        trade_summary = [
            {
                "symbol": t.get("symbol"),
                "direction": t.get("direction"),
                "entry": t.get("entry_price"),
                "exit": t.get("exit_price"),
                "pnl": round(t.get("pnl", 0), 2),
                "pnl_pct": round(t.get("pnl_pct", 0) * 100, 2),
                "strategy": t.get("strategy"),
                "exit_reason": t.get("reason"),
                "entry_time": t.get("entry_time", "")[:16],
                "exit_time": t.get("exit_time", "")[:16],
            }
            for t in recent_trades
        ]

        pos_summary = [
            {
                "symbol": sym,
                "direction": p.get("direction"),
                "entry": p.get("entry_price"),
                "qty": p.get("quantity"),
                "strategy": p.get("strategy"),
                "stop": p.get("stop_loss"),
                "target": p.get("take_profit"),
            }
            for sym, p in (positions or {}).items()
        ]

        data = {
            "recent_trades": trade_summary,
            "performance": performance or {},
            "open_positions": pos_summary,
            "market_regime": regime_data or {},
            "strategy_scores": strategy_scores or {},
            "account_balance": self.config.starting_balance,
        }

        return f"TRADING DATA:\n{json.dumps(data, indent=2)}"

    def _call_claude(self, user_message):
        """Call the Claude API via the official SDK."""
        try:
            response = self._client.with_options(timeout=30.0).messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_message}],
            )

            for block in response.content:
                if block.type == "text":
                    return block.text
            return None

        except anthropic.APIStatusError as e:
            log.error(f"Claude API error {e.status_code}: {str(e.message)[:200]}")
            return None
        except anthropic.APIConnectionError as e:
            log.error(f"Claude API connection error: {e}")
            return None

    def _parse_response(self, response_text):
        sections = {
            "whats_working": "",
            "whats_not_working": "",
            "entry_timing": "",
            "exit_improvements": "",
            "strategy_adjustments": "",
            "top_actions": "",
            "confidence_score": "",
        }

        section_map = {
            "working": "whats_working",
            "not working": "whats_not_working",
            "entry": "entry_timing",
            "exit": "exit_improvements",
            "strategy": "strategy_adjustments",
            "top 3": "top_actions",
            "action": "top_actions",
            "confidence": "confidence_score",
        }

        current_section = None
        for line in response_text.split("\n"):
            line_lower = line.lower().strip()
            for keyword, section_key in section_map.items():
                if keyword in line_lower and (line.startswith("#") or line.startswith("**")):
                    current_section = section_key
                    break
            else:
                if current_section:
                    sections[current_section] += line + "\n"

        return {
            "full_analysis": response_text,
            "sections": {k: v.strip() for k, v in sections.items()},
        }

    def get_quick_insight(self, trade_history, performance_stats):
        """Get a quick one-paragraph insight (cheaper, faster)."""
        if not self._client or not trade_history or len(trade_history) < 3:
            return None

        recent = trade_history[-10:]
        wins = sum(1 for t in recent if t.get("pnl", 0) > 0)
        losses = len(recent) - wins
        total_pnl = sum(t.get("pnl", 0) for t in recent)
        strategies = set(t.get("strategy", "?") for t in recent)

        prompt = (
            f"Last {len(recent)} trades: {wins}W/{losses}L, "
            f"net P&L ${total_pnl:.2f}, strategies: {', '.join(strategies)}. "
            f"Win rate: {performance_stats.get('win_rate', 'N/A')}%, "
            f"Profit factor: {performance_stats.get('profit_factor', 'N/A')}. "
            f"In 2-3 sentences, what's the most important thing this trader "
            f"should change right now? Be specific and actionable."
        )

        try:
            response = self._client.with_options(timeout=15.0).messages.create(
                model=self.model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            for block in response.content:
                if block.type == "text":
                    return block.text
        except Exception as e:
            log.debug(f"Quick insight error: {e}")

        return None

    def get_cached_insights(self):
        if self._cached_insights:
            return self._cached_insights
        if self._insights_history:
            return self._insights_history[-1]
        return {"available": self.is_available(), "message": "No insights generated yet. Trigger an analysis first."}

    def _save_insight(self, insight):
        self._insights_history.append({
            "generated_at": insight.get("generated_at"),
            "trades_analyzed": insight.get("trades_analyzed", 0),
            "sections": insight.get("sections", {}),
        })
        self._insights_history = self._insights_history[-20:]

        try:
            with open(self._insights_file, "w") as f:
                json.dump(self._insights_history, f, indent=2)
        except Exception as e:
            log.debug(f"Could not save insights history: {e}")

    def _load_insights_history(self):
        if self._insights_file.exists():
            try:
                with open(self._insights_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return []
