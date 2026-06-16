"""Dashboard bug fixes (operator-visible review 2026-06-16).

Live review of the bot's mobile dashboard surfaced four bugs:

  1. Win Rate card stuck at "--" despite 297 trades in history.
     Root cause: `engine.performance_stats` is in-memory only, so a
     bot restart drops the entire historical sample. Until the first
     post-restart close, win_rate / profit factor read 0.
     Fix: rebuild performance_stats from `trade_history` on boot.

  2. Today P&L: "$0.00 / 0 trades" while the operator knew 24 trades
     had closed earlier today.
     Root cause: `engine.daily_pnl` / `daily_trades` reset at the
     09:15-ET `_pre_market_scan`, so trades from the pre-market
     overnight window (often the lion's share of activity) silently
     don't count toward "today" once the scheduler tick fires.
     Fix: compute today's stats client-side from the trades list,
     filtered by America/New_York calendar day.

  3. Balance subtitle "-12.88%" red sits next to "Today P&L" and
     reads as today's loss — it's actually all-time vs starting
     balance. Cosmetic but consequential.
     Fix: prefix with "All-time:" so the framing is unambiguous.

  4. Positions card sub-label was a comma-joined list of strategy
     names (mean_reversion, momentum, ...) — reads like a list of
     position symbols.
     Fix: show "N strategies active" with the full list as a tooltip.

These tests pin the engine fix and the source-level presence of the
JS helpers (the actual DOM updates can't be unit-tested without a
browser harness; anti-regression checks lock the template strings).
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace


def _make_engine_with_history(trades):
    """Build a TradingEngine stub just enough to call the rebuild helper."""
    from bot.engine import TradingEngine
    eng = TradingEngine.__new__(TradingEngine)
    eng.trade_history = list(trades)
    eng.performance_stats = {
        "total_trades": 0, "wins": 0, "losses": 0, "breakeven": 0,
        "total_profit": 0.0, "total_loss": 0.0,
        "largest_win": 0.0, "largest_loss": 0.0,
        "current_streak": 0, "best_streak": 0, "worst_streak": 0,
    }
    return eng


# === 1. Engine rebuild ===


def test_rebuild_replays_full_history():
    eng = _make_engine_with_history([
        {"pnl": 10.0}, {"pnl": -5.0}, {"pnl": 20.0},
        {"pnl": -2.0}, {"pnl": 7.0},
    ])
    eng._rebuild_performance_stats_from_history()
    s = eng.performance_stats
    assert s["total_trades"] == 5
    assert s["wins"] == 3
    assert s["losses"] == 2
    assert s["total_profit"] == 37.0
    assert s["total_loss"] == 7.0


def test_rebuild_resets_before_replay():
    """Calling twice must not double-count."""
    eng = _make_engine_with_history([{"pnl": 10.0}, {"pnl": -5.0}])
    eng._rebuild_performance_stats_from_history()
    eng._rebuild_performance_stats_from_history()
    s = eng.performance_stats
    assert s["total_trades"] == 2
    assert s["total_profit"] == 10.0
    assert s["total_loss"] == 5.0


def test_rebuild_handles_empty_history():
    eng = _make_engine_with_history([])
    eng._rebuild_performance_stats_from_history()
    s = eng.performance_stats
    assert s["total_trades"] == 0
    assert s["wins"] == 0
    assert s["losses"] == 0


def test_rebuild_skips_malformed_pnl():
    """A bad pnl value (None, string, missing key) must not crash. Float-
    unconvertible values are skipped entirely (try/except ValueError);
    None / missing parse as 0 via the `or 0` fallback and count as
    breakeven."""
    eng = _make_engine_with_history([
        {"pnl": 10.0}, {"pnl": None}, {"pnl": "garbage"},
        {}, {"pnl": -3.0},
    ])
    eng._rebuild_performance_stats_from_history()
    s = eng.performance_stats
    # 10 → win, None → breakeven (0), garbage → SKIPPED, {} → breakeven, -3 → loss
    assert s["wins"] == 1
    assert s["losses"] == 1
    assert s["total_trades"] == 4  # garbage row skipped


def test_rebuild_largest_win_and_loss_correct():
    eng = _make_engine_with_history([
        {"pnl": 5.0}, {"pnl": 25.0}, {"pnl": 8.0},
        {"pnl": -3.0}, {"pnl": -40.0}, {"pnl": -7.0},
    ])
    eng._rebuild_performance_stats_from_history()
    s = eng.performance_stats
    assert s["largest_win"] == 25.0
    assert s["largest_loss"] == 40.0  # stored as abs


def test_rebuild_called_at_boot_from_persisted_trades():
    """Lock the engine.__init__ wiring — without this call, the win-rate
    card silently regresses to '--' on every bot restart."""
    src = (Path(__file__).parent.parent / "bot" / "engine.py").read_text()
    # The rebuild method must exist
    assert "def _rebuild_performance_stats_from_history" in src
    # It must be called from the trade-history-restore block
    history_block_start = src.find("TRADE HISTORY: Restored")
    next_block = src.find("TRADE HISTORY: No previous trades", history_block_start)
    snippet = src[history_block_start:next_block]
    assert "_rebuild_performance_stats_from_history" in snippet, (
        "engine.py must call _rebuild_performance_stats_from_history() "
        "inside the persisted-history restore branch"
    )


# === 2. Dashboard JS helpers (anti-regression on template strings) ===


DASHBOARD_HTML = Path(__file__).parent.parent / "bot" / "dashboard" / "templates" / "dashboard.html"


def test_today_stats_helper_present():
    html = DASHBOARD_HTML.read_text()
    assert "_todaysStatsFromTrades" in html, (
        "Dashboard missing _todaysStatsFromTrades helper — "
        "Today P&L card will revert to engine.daily_pnl semantics"
    )
    # Must filter by ET (America/New_York), not browser locale
    assert "America/New_York" in html


def test_today_card_uses_helper_not_status_field():
    """Anti-regression: the Today P&L card must read from the helper,
    not from `status.daily_pnl` which has the pre-market-reset bug."""
    html = DASHBOARD_HTML.read_text()
    # Anchor on the getElementById('dailyPnl') update line — the earlier
    # `<div id="dailyPnl">` markup substring would hit before the JS.
    idx = html.find("getElementById('dailyPnl')")
    assert idx > 0, "dailyPnl update line not found"
    block = html[idx:idx + 800]
    assert "today.pnl" in block, "Today P&L card not using _todaysStatsFromTrades output"
    assert "today.count" in block


def test_balance_subtitle_says_all_time():
    """Without the 'All-time:' prefix, the red %-down reads as today's loss."""
    html = DASHBOARD_HTML.read_text()
    idx = html.find("getElementById('totalReturn')")
    assert idx > 0
    block = html[idx:idx + 500]
    assert "All-time" in block


def test_strategies_subtitle_is_count_not_csv_list():
    """The Positions card subtitle was a comma-joined list of strategy
    names that read like a list of positions. Now: 'N strategies active'."""
    html = DASHBOARD_HTML.read_text()
    idx = html.find("getElementById('strategies')")
    assert idx > 0
    block = html[idx:idx + 600]
    assert "strategies active" in block, "strategies subtitle not formatted as count"


def test_win_rate_has_trade_summary_fallback():
    """If perfData.total_trades == 0 (in-memory empty), the card must
    fall back to tradesSummary instead of leaving '--' visible."""
    html = DASHBOARD_HTML.read_text()
    wr_block = html[html.find("Quick win rate"):html.find("Quick win rate") + 1500]
    assert "tradesSummary" in wr_block, "Win Rate card missing tradesSummary fallback"
    assert "wrSource" in wr_block
