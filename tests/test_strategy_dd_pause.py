"""_gate_strategy_drawdown — pauses a strategy down >threshold on
its allocated capital, resets at EOD via the date-keyed pause flag.

Covers the wiring verified live 2026-05-18:
- threshold from settings.risk.strategy_daily_dd_pause_pct
- alloc % from strategies.allocation[strategy]
- alloc $ = start_of_day_balance × alloc_pct
- sums today's PnL from trade_history filtered by exit_time date
- returns reason string when DD >= threshold; "" otherwise
- writes a date-keyed pause flag so notification fires once per day
- threshold == 0 disables the gate

NOT tested here:
- _entry_safety_gates() composition with other gates (already covered
  by integration via _execute_signal call site)
- The "buy only" check in _execute_signal (visible at engine.py:5007)
  that ensures exits bypass all safety gates.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytz

from bot.engine import TradingEngine


def _make_engine(threshold=0.03, alloc=None, sod_balance=50000,
                  trade_history=None):
    """Bare TradingEngine with only what _gate_strategy_drawdown reads."""
    eng = TradingEngine.__new__(TradingEngine)
    eng.tz = pytz.timezone("America/New_York")
    eng.start_of_day_balance = sod_balance
    eng.trade_history = trade_history or []
    eng.notifier = MagicMock()
    # Minimal config surface
    cfg = SimpleNamespace()
    cfg.risk_config = {"strategy_daily_dd_pause_pct": threshold}
    cfg.strategies = {"allocation": alloc or {}}
    eng.config = cfg
    return eng


def _today_trade(strategy, pnl, hours_ago=2):
    """Build a trade with exit_time today (local NY tz)."""
    now = datetime.now(pytz.timezone("America/New_York"))
    return {
        "strategy": strategy,
        "pnl": pnl,
        "exit_time": (now - timedelta(hours=hours_ago)).isoformat(),
    }


def test_blank_strategy_name_skipped():
    """Defensive: empty strategy name returns "" immediately."""
    eng = _make_engine()
    assert eng._gate_strategy_drawdown("") == ""
    assert eng._gate_strategy_drawdown(None) == ""


def test_threshold_zero_disables_gate():
    """settings.strategy_daily_dd_pause_pct: 0 → gate is off; even a
    catastrophic loss won't trigger it."""
    eng = _make_engine(threshold=0, alloc={"momentum": 0.10},
                       trade_history=[_today_trade("momentum", -1000)])
    assert eng._gate_strategy_drawdown("momentum") == ""


def test_unallocated_strategy_skipped():
    """A strategy with 0% allocation has no capital to draw down on
    — skip the calculation rather than divide by zero."""
    eng = _make_engine(alloc={"momentum": 0.10},
                       trade_history=[_today_trade("vwap_scalp", -500)])
    assert eng._gate_strategy_drawdown("vwap_scalp") == ""


def test_winning_strategy_not_paused():
    """+PnL means dd_pct > 0; threshold check is `dd_pct <= -threshold`
    so positive P&L can never pause."""
    eng = _make_engine(alloc={"momentum": 0.10},
                       trade_history=[_today_trade("momentum", +100)])
    assert eng._gate_strategy_drawdown("momentum") == ""


def test_loss_below_threshold_not_paused():
    """Loss of $50 on $5000 allocated = -1% < threshold 3% → pass."""
    eng = _make_engine(threshold=0.03, alloc={"momentum": 0.10},
                       trade_history=[_today_trade("momentum", -50)])
    assert eng._gate_strategy_drawdown("momentum") == ""


def test_loss_at_threshold_pauses():
    """Loss of $150 on $5000 = -3% exactly hits threshold → pause."""
    eng = _make_engine(threshold=0.03, alloc={"momentum": 0.10},
                       trade_history=[_today_trade("momentum", -150)])
    reason = eng._gate_strategy_drawdown("momentum")
    assert reason
    assert "daily DD limit" in reason
    assert "momentum" in reason


def test_loss_above_threshold_pauses_and_alerts_once():
    """First trip sends notifier alert; subsequent calls same day
    don't re-alert but still return the reason string."""
    eng = _make_engine(threshold=0.03, alloc={"momentum": 0.10},
                       trade_history=[_today_trade("momentum", -300)])
    r1 = eng._gate_strategy_drawdown("momentum")
    r2 = eng._gate_strategy_drawdown("momentum")
    assert r1 and r2  # both blocked
    # Notifier called exactly once (only on first trip)
    assert eng.notifier.risk_alert.call_count == 1


def test_trades_from_other_days_dont_count():
    """Yesterday's losses don't count toward today's DD."""
    yesterday = datetime.now(pytz.timezone("America/New_York")) - timedelta(days=1)
    eng = _make_engine(threshold=0.03, alloc={"momentum": 0.10},
                       trade_history=[
                           {"strategy": "momentum", "pnl": -500,
                            "exit_time": yesterday.isoformat()},
                       ])
    assert eng._gate_strategy_drawdown("momentum") == ""


def test_per_strategy_isolation():
    """Strategy A being down doesn't pause strategy B."""
    eng = _make_engine(threshold=0.03,
                       alloc={"momentum": 0.10, "mean_reversion": 0.10},
                       trade_history=[_today_trade("momentum", -300)])
    assert eng._gate_strategy_drawdown("momentum") != ""
    assert eng._gate_strategy_drawdown("mean_reversion") == ""


def test_multiple_trades_sum_correctly():
    """Per-strategy PnL is summed across all today's trades for the
    strategy. Two -$100 trades on a 10%-allocated strat → -$200 on
    $5000 = -4% → pause."""
    eng = _make_engine(threshold=0.03, alloc={"momentum": 0.10},
                       trade_history=[
                           _today_trade("momentum", -100, hours_ago=4),
                           _today_trade("momentum", -100, hours_ago=2),
                           _today_trade("mean_reversion", +50),  # ignored
                       ])
    reason = eng._gate_strategy_drawdown("momentum")
    assert reason
    assert "-4.00%" in reason
