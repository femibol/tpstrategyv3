"""
Weekly Trade Review — deep Claude-powered analysis posted to Discord.

Runs every Saturday 10am ET. Reads the last week of closed trades, crunches
aggregates (strategy breakdown, regime perf, exit-reason distribution, best/
worst trades), hands the summary to Claude for pattern recognition, and
posts a rich Discord embed with the numbers + Claude's read.

Separate from per-trade insights (fire-and-forget, small prompts) because
a weekly digest wants the bigger picture: which strategies to kill, which
hours to favor, whether exits are leaving money on the table, etc.
"""
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from bot.utils.logger import get_logger

log = get_logger("learning.weekly_review")


class WeeklyReview:
    """Aggregate + Claude-analyze + Discord-post a weekly trade digest."""

    def __init__(self, config, ai_insights, notifier, data_dir=None):
        self.config = config
        self.ai_insights = ai_insights  # reuse existing Claude client
        self.notifier = notifier
        self.data_dir = Path(data_dir) if data_dir else Path(config.base_dir) / "data"

    def is_available(self):
        """Only run if Claude AND Discord are both wired up — otherwise the
        review would either miss the AI analysis or have nowhere to go."""
        return (
            self.ai_insights
            and self.ai_insights.is_available()
            and getattr(self.notifier, "discord_url", None)
        )

    # =========================================================================
    # Main entry point (called from scheduler)
    # =========================================================================

    def run(self, trade_history):
        """Generate and post the weekly review.

        trade_history: list of trade dicts (same shape as engine.trade_history)
        """
        if not self.is_available():
            log.info("Weekly review skipped — Claude or Discord not configured")
            return

        try:
            week_trades = self._filter_last_week(trade_history)
            if not week_trades:
                log.info("Weekly review: no trades in the last 7 days — skipping post")
                return

            stats = self._compute_stats(week_trades)
            claude_analysis = self._get_claude_analysis(stats, week_trades)
            self._post_to_discord(stats, claude_analysis)
            log.info(
                f"Weekly review posted: {stats['total_trades']} trades, "
                f"${stats['total_pnl']:+.2f} P&L"
            )
        except Exception as e:
            log.error(f"Weekly review failed: {e}", exc_info=True)

    # =========================================================================
    # Aggregation
    # =========================================================================

    def _filter_last_week(self, trade_history):
        """Return trades closed in the last 7 days."""
        cutoff = datetime.now() - timedelta(days=7)
        out = []
        for t in trade_history:
            exit_time = t.get("exit_time")
            if not exit_time:
                continue
            try:
                if isinstance(exit_time, str):
                    # Tolerate both naive and tz-aware ISO strings
                    exit_dt = datetime.fromisoformat(exit_time.replace("Z", "+00:00"))
                    # Compare naive-to-naive by stripping tz if present
                    if exit_dt.tzinfo is not None:
                        exit_dt = exit_dt.replace(tzinfo=None)
                else:
                    exit_dt = exit_time
                if exit_dt >= cutoff:
                    out.append(t)
            except Exception:
                continue  # Skip malformed timestamps, don't fail the whole run
        return out

    def _compute_stats(self, trades):
        """Crunch the numbers Claude + Discord both need."""
        wins = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) < 0]
        flats = [t for t in trades if t.get("pnl", 0) == 0]

        total_pnl = sum(t.get("pnl", 0) for t in trades)
        win_pnl = sum(t.get("pnl", 0) for t in wins)
        loss_pnl = sum(t.get("pnl", 0) for t in losses)

        # Strategy breakdown
        by_strategy = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
        for t in trades:
            s = t.get("strategy", "unknown")
            by_strategy[s]["trades"] += 1
            by_strategy[s]["pnl"] += t.get("pnl", 0)
            if t.get("pnl", 0) > 0:
                by_strategy[s]["wins"] += 1

        # Exit-reason breakdown (why trades closed)
        exit_reasons = Counter(t.get("reason", "unknown") for t in trades)

        # Best / worst
        best = max(trades, key=lambda t: t.get("pnl", 0))
        worst = min(trades, key=lambda t: t.get("pnl", 0))

        # Regime performance
        by_regime = defaultdict(lambda: {"trades": 0, "pnl": 0.0})
        for t in trades:
            r = t.get("regime", "unknown")
            by_regime[r]["trades"] += 1
            by_regime[r]["pnl"] += t.get("pnl", 0)

        # Hold time — median-ish via average of middle quartile
        hold_times = [t.get("hold_time_mins", 0) for t in trades if t.get("hold_time_mins")]
        avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "flats": len(flats),
            "win_rate": (len(wins) / len(trades) * 100) if trades else 0,
            "total_pnl": total_pnl,
            "avg_winner": (win_pnl / len(wins)) if wins else 0,
            "avg_loser": (loss_pnl / len(losses)) if losses else 0,
            "profit_factor": abs(win_pnl / loss_pnl) if loss_pnl else float("inf"),
            "by_strategy": dict(by_strategy),
            "exit_reasons": dict(exit_reasons),
            "by_regime": dict(by_regime),
            "best_trade": {
                "symbol": best.get("symbol", "?"),
                "pnl": best.get("pnl", 0),
                "strategy": best.get("strategy", "?"),
            },
            "worst_trade": {
                "symbol": worst.get("symbol", "?"),
                "pnl": worst.get("pnl", 0),
                "strategy": worst.get("strategy", "?"),
            },
            "avg_hold_mins": avg_hold,
        }

    # =========================================================================
    # Claude analysis
    # =========================================================================

    def _get_claude_analysis(self, stats, trades):
        """Hand aggregates + recent trade tape to Claude for a pattern read."""
        # Compact trade tape: just the fields Claude needs, capped at 40 trades
        # to keep the prompt small. Claude has the aggregates separately.
        tape = []
        for t in trades[-40:]:
            tape.append({
                "symbol": t.get("symbol"),
                "pnl": round(t.get("pnl", 0), 2),
                "strategy": t.get("strategy"),
                "reason": t.get("reason"),
                "regime": t.get("regime"),
                "hold_min": t.get("hold_time_mins"),
            })

        # Strategy summary — sorted worst to best P&L
        strat_summary = sorted(
            stats["by_strategy"].items(),
            key=lambda kv: kv[1]["pnl"]
        )
        strat_lines = [
            f"  {name}: {data['trades']}t, {data['wins']}W, ${data['pnl']:+.2f}"
            for name, data in strat_summary
        ]

        prompt = (
            f"Weekly trading review. Last 7 days on an IBKR-executed long-only "
            f"momentum bot.\n\n"
            f"AGGREGATES:\n"
            f"  Trades: {stats['total_trades']} "
            f"({stats['wins']}W / {stats['losses']}L / {stats['flats']}F)\n"
            f"  Win rate: {stats['win_rate']:.1f}%\n"
            f"  Total P&L: ${stats['total_pnl']:+.2f}\n"
            f"  Avg winner: ${stats['avg_winner']:+.2f} | "
            f"Avg loser: ${stats['avg_loser']:+.2f}\n"
            f"  Profit factor: {stats['profit_factor']:.2f}\n"
            f"  Best: {stats['best_trade']['symbol']} "
            f"${stats['best_trade']['pnl']:+.2f} "
            f"({stats['best_trade']['strategy']})\n"
            f"  Worst: {stats['worst_trade']['symbol']} "
            f"${stats['worst_trade']['pnl']:+.2f} "
            f"({stats['worst_trade']['strategy']})\n"
            f"  Avg hold: {stats['avg_hold_mins']:.0f} minutes\n\n"
            f"STRATEGY BREAKDOWN (worst first):\n"
            + "\n".join(strat_lines) + "\n\n"
            f"EXIT REASONS: {stats['exit_reasons']}\n\n"
            f"RECENT TAPE (last {len(tape)}):\n{json.dumps(tape, default=str)}\n\n"
            f"Give me THREE things, each under 400 chars, plain prose (no "
            f"markdown headers — Discord will render it):\n"
            f"1. WORKING: what's actually making money this week. Name names.\n"
            f"2. BROKEN: strategies/patterns losing money. Be specific about why.\n"
            f"3. ACTION: the ONE highest-leverage parameter change or filter "
            f"to try next week.\n\n"
            f"Format: prefix each with '1. WORKING:', '2. BROKEN:', '3. ACTION:'."
        )

        response = self.ai_insights._call_claude(prompt)
        if not response:
            return None
        return self._split_sections(response)

    def _split_sections(self, response):
        """Pull the three labelled sections out of Claude's response."""
        sections = {"WORKING": "", "BROKEN": "", "ACTION": ""}
        current = None
        for line in response.split("\n"):
            line = line.strip()
            if not line:
                continue
            for key in sections:
                # Match "1. WORKING:", "WORKING:", "**1. WORKING:**", etc.
                marker = f"{key}:"
                if marker in line.upper():
                    current = key
                    # Strip the marker + any numbering
                    idx = line.upper().index(marker) + len(marker)
                    line = line[idx:].strip().lstrip("*").strip()
                    sections[current] = line
                    break
            else:
                if current:
                    sections[current] += " " + line
        return sections

    # =========================================================================
    # Discord
    # =========================================================================

    def _post_to_discord(self, stats, claude):
        """Rich embed with the numbers + Claude's three-section read."""
        pnl = stats["total_pnl"]
        color = 0x3FB950 if pnl > 0 else 0xF85149 if pnl < 0 else 0x8B949E
        icon = "📊"

        # Top 3 and bottom 1 strategies by P&L for the embed
        sorted_strats = sorted(
            stats["by_strategy"].items(),
            key=lambda kv: kv[1]["pnl"],
            reverse=True,
        )
        top3 = sorted_strats[:3]
        bottom1 = sorted_strats[-1] if len(sorted_strats) > 3 else None

        strat_field_lines = []
        for name, data in top3:
            wr = (data["wins"] / data["trades"] * 100) if data["trades"] else 0
            strat_field_lines.append(
                f"✅ {name}: ${data['pnl']:+.0f} ({data['trades']}t, {wr:.0f}%)"
            )
        if bottom1 and bottom1[1]["pnl"] < 0:
            name, data = bottom1
            wr = (data["wins"] / data["trades"] * 100) if data["trades"] else 0
            strat_field_lines.append(
                f"❌ {name}: ${data['pnl']:+.0f} ({data['trades']}t, {wr:.0f}%)"
            )
        strat_value = "\n".join(strat_field_lines) or "—"

        fields = [
            {"name": "P&L", "value": f"${pnl:+.2f}", "inline": True},
            {
                "name": "Trades",
                "value": f"{stats['total_trades']} ({stats['wins']}W/{stats['losses']}L)",
                "inline": True,
            },
            {"name": "Win Rate", "value": f"{stats['win_rate']:.0f}%", "inline": True},
            {
                "name": "Avg Winner / Loser",
                "value": f"${stats['avg_winner']:+.0f} / ${stats['avg_loser']:+.0f}",
                "inline": True,
            },
            {
                "name": "Profit Factor",
                "value": f"{stats['profit_factor']:.2f}"
                         if stats["profit_factor"] != float("inf") else "∞",
                "inline": True,
            },
            {
                "name": "Avg Hold",
                "value": f"{stats['avg_hold_mins']:.0f}m",
                "inline": True,
            },
            {
                "name": "Best Trade",
                "value": f"{stats['best_trade']['symbol']} "
                         f"${stats['best_trade']['pnl']:+.2f} "
                         f"({stats['best_trade']['strategy']})",
                "inline": True,
            },
            {
                "name": "Worst Trade",
                "value": f"{stats['worst_trade']['symbol']} "
                         f"${stats['worst_trade']['pnl']:+.2f} "
                         f"({stats['worst_trade']['strategy']})",
                "inline": True,
            },
            {"name": "Strategies", "value": strat_value, "inline": False},
        ]

        if claude:
            # Discord embed field values are capped at 1024 chars; Claude
            # responses are bounded at ~400 each by the prompt.
            if claude.get("WORKING"):
                fields.append({
                    "name": "✅ Working",
                    "value": claude["WORKING"][:1020],
                    "inline": False,
                })
            if claude.get("BROKEN"):
                fields.append({
                    "name": "⚠️ Broken",
                    "value": claude["BROKEN"][:1020],
                    "inline": False,
                })
            if claude.get("ACTION"):
                fields.append({
                    "name": "🎯 Next Week's Action",
                    "value": claude["ACTION"][:1020],
                    "inline": False,
                })

        self.notifier._send_discord_embed(
            title=f"{icon} Weekly Review — {datetime.now().strftime('%Y-%m-%d')}",
            color=color,
            fields=fields,
            footer="AlgoBot weekly digest",
        )
