"""Unit tests for bot.risk.manager.RiskManager.

Covers every branch of `_check_all_rules`, the entry/exit fork in
`filter_signals`, the daily-loss / drawdown helpers, the `update_tier`
mutation, and the portfolio-health audit.
"""
from __future__ import annotations

import pytest

from bot.risk.manager import RiskManager


# -----------------------------------------------------------------------------
# filter_signals — entry rejection branches
# -----------------------------------------------------------------------------

def test_max_positions_reached_rejects(config, base_signal, position_factory):
    rm = RiskManager(config)
    positions = {f"SYM{i}": position_factory(symbol=f"SYM{i}") for i in range(5)}
    approved = rm.filter_signals([base_signal], positions, current_balance=100_000)
    assert approved == []
    assert "Max positions" in base_signal["_rejection_reason"]


def test_already_in_position_rejects(config, base_signal, position_factory):
    rm = RiskManager(config)
    positions = {"AAPL": position_factory(symbol="AAPL")}
    approved = rm.filter_signals([base_signal], positions, current_balance=100_000)
    assert approved == []
    assert "Already in position" in base_signal["_rejection_reason"]


def test_stale_signal_rejects(config, base_signal, stale_timestamp):
    rm = RiskManager(config)
    base_signal["timestamp"] = stale_timestamp
    approved = rm.filter_signals([base_signal], {}, current_balance=100_000)
    assert approved == []
    assert "Stale signal" in base_signal["_rejection_reason"]


def test_min_price_floor_rejects(config, base_signal):
    rm = RiskManager(config)
    base_signal["price"] = 0.10  # below default 0.50 floor
    approved = rm.filter_signals([base_signal], {}, current_balance=100_000)
    assert approved == []
    assert "below minimum" in base_signal["_rejection_reason"]


def test_max_price_ceiling_rejects(config_factory, base_signal):
    rc = {"max_price": 100.0, "min_price": 0.50, "max_positions": 5,
          "max_position_size_pct": 0.15, "risk_per_trade_pct": 0.01,
          "long_only": False, "portfolio_limits": {
              "max_single_name_pct": 0.25, "max_gross_exposure_pct": 1.50,
              "max_net_exposure_pct": 1.00, "max_loss_per_position_pct": 0.08}}
    rm = RiskManager(config_factory(risk_config=rc))
    base_signal["price"] = 250.0
    approved = rm.filter_signals([base_signal], {}, current_balance=100_000)
    assert approved == []
    assert "above maximum" in base_signal["_rejection_reason"]


def test_price_drift_over_5pct_rejects(config, base_signal):
    rm = RiskManager(config)
    base_signal["price"] = 150.0
    base_signal["market_price"] = 100.0  # 50% drift
    approved = rm.filter_signals([base_signal], {}, current_balance=100_000)
    assert approved == []
    assert "away from" in base_signal["_rejection_reason"]


def test_position_size_exceeds_max_rejects(config, base_signal):
    rm = RiskManager(config)
    # 15% of $10k = $1500. Order of 100 * $150 = $15000 -> blows the cap.
    base_signal["quantity"] = 100
    approved = rm.filter_signals([base_signal], {}, current_balance=10_000)
    assert approved == []
    assert "exceeds max" in base_signal["_rejection_reason"]


def test_reserve_cash_violated_rejects(config, base_signal, position_factory):
    rm = RiskManager(config)
    # 10k balance, 20% reserve = 2k held back. Existing position eats the rest.
    positions = {"NVDA": position_factory(symbol="NVDA", entry=100.0, qty=80)}
    base_signal["quantity"] = 5
    approved = rm.filter_signals([base_signal], positions, current_balance=10_000)
    assert approved == []
    assert "available capital" in base_signal["_rejection_reason"]


def test_low_confidence_rejects(config, base_signal):
    rm = RiskManager(config)
    base_signal["confidence"] = 0.10  # below 0.35 threshold
    approved = rm.filter_signals([base_signal], {}, current_balance=100_000)
    assert approved == []
    assert "Confidence" in base_signal["_rejection_reason"]


def test_no_stop_loss_rejects(config, base_signal):
    rm = RiskManager(config)
    base_signal.pop("stop_loss")
    approved = rm.filter_signals([base_signal], {}, current_balance=100_000)
    assert approved == []
    assert "stop loss" in base_signal["_rejection_reason"]


def test_gross_exposure_breach_rejects(config, base_signal, position_factory):
    rm = RiskManager(config)
    # Reserve check uses cost basis; gross-exposure uses mark-to-market.
    # Cheap entries that have run hard let us isolate the gross-exposure
    # branch without tripping the reserve rule first.
    positions = {
        f"SYM{i}": position_factory(symbol=f"SYM{i}", entry=10.0, qty=300, current=150.0)
        for i in range(2)
    }
    # Cap max_positions higher so we test the gross-exposure rule, not max-pos.
    rm.max_positions = 99
    approved = rm.filter_signals([base_signal], positions, current_balance=60_000)
    assert approved == []
    assert "exposure" in base_signal["_rejection_reason"].lower()


def test_long_only_blocks_short(config_factory, base_signal):
    rc = {**{"long_only": True, "max_positions": 5,
             "max_position_size_pct": 0.15, "risk_per_trade_pct": 0.01,
             "min_price": 0.50, "max_price": 99999.0,
             "portfolio_limits": {"max_single_name_pct": 0.25,
                                  "max_gross_exposure_pct": 1.50,
                                  "max_net_exposure_pct": 1.00,
                                  "max_loss_per_position_pct": 0.08}}}
    rm = RiskManager(config_factory(risk_config=rc))
    base_signal["action"] = "short"
    approved = rm.filter_signals([base_signal], {}, current_balance=100_000)
    assert approved == []
    assert "long_only" in base_signal["_rejection_reason"]


# -----------------------------------------------------------------------------
# filter_signals — exit branch
# -----------------------------------------------------------------------------

def test_exit_signal_with_position_approves(config, base_signal, position_factory):
    rm = RiskManager(config)
    base_signal["action"] = "sell"
    positions = {"AAPL": position_factory(symbol="AAPL")}
    approved = rm.filter_signals([base_signal], positions, current_balance=100_000)
    assert approved == [base_signal]


def test_close_action_with_position_approves(config, base_signal, position_factory):
    rm = RiskManager(config)
    base_signal["action"] = "close"
    positions = {"AAPL": position_factory(symbol="AAPL")}
    approved = rm.filter_signals([base_signal], positions, current_balance=100_000)
    assert approved == [base_signal]


def test_exit_signal_without_position_rejects(config, base_signal):
    rm = RiskManager(config)
    base_signal["action"] = "sell"
    approved = rm.filter_signals([base_signal], {}, current_balance=100_000)
    assert approved == []
    assert "No position to exit" in base_signal["_rejection_reason"]


# -----------------------------------------------------------------------------
# filter_signals — happy path + side effects
# -----------------------------------------------------------------------------

def test_clean_entry_approves(config, base_signal):
    rm = RiskManager(config)
    approved = rm.filter_signals([base_signal], {}, current_balance=100_000)
    assert approved == [base_signal]
    assert "_rejection_reason" not in base_signal


def test_filter_signals_appends_rejection_log(config, base_signal):
    rm = RiskManager(config)
    base_signal["price"] = 0.10
    rm.filter_signals([base_signal], {}, current_balance=100_000)
    assert len(rm.rejected_signals) == 1
    entry = rm.rejected_signals[0]
    assert entry["signal"] is base_signal
    assert "below minimum" in entry["reason"]


# -----------------------------------------------------------------------------
# Daily-loss + drawdown helpers
# -----------------------------------------------------------------------------

def test_daily_loss_exceeded_at_threshold(config):
    rm = RiskManager(config)
    # default max_daily_loss = 0.02 (2%).
    assert rm.is_daily_loss_exceeded(current_balance=98_000, start_of_day_balance=100_000)


def test_daily_loss_not_exceeded_below_threshold(config):
    rm = RiskManager(config)
    assert not rm.is_daily_loss_exceeded(current_balance=99_000, start_of_day_balance=100_000)


def test_daily_loss_handles_zero_start(config):
    rm = RiskManager(config)
    assert not rm.is_daily_loss_exceeded(current_balance=0, start_of_day_balance=0)


def test_max_drawdown_exceeded(config):
    rm = RiskManager(config)
    # default max_drawdown = 0.10 (10%).
    assert rm.is_max_drawdown_exceeded(current_balance=89_000, peak_balance=100_000)


def test_max_drawdown_not_exceeded(config):
    rm = RiskManager(config)
    assert not rm.is_max_drawdown_exceeded(current_balance=95_000, peak_balance=100_000)


# -----------------------------------------------------------------------------
# update_tier
# -----------------------------------------------------------------------------

def test_update_tier_changes_max_positions(config):
    rm = RiskManager(config)
    rm.update_tier({"max_positions": 12, "risk_per_trade": 0.02, "max_position_pct": 0.20})
    assert rm.max_positions == 12
    assert rm.risk_per_trade == 0.02
    assert rm.max_position_pct == 0.20


def test_update_tier_with_none_is_noop(config):
    rm = RiskManager(config)
    before = (rm.max_positions, rm.risk_per_trade, rm.max_position_pct)
    rm.update_tier(None)
    assert (rm.max_positions, rm.risk_per_trade, rm.max_position_pct) == before


# -----------------------------------------------------------------------------
# check_portfolio_health
# -----------------------------------------------------------------------------

def test_concentration_breach_triggers_force_close(config, position_factory):
    rm = RiskManager(config)
    # 30% of the book in one name (>25% cap) -> force_close
    positions = {"NVDA": position_factory(symbol="NVDA", entry=100.0, qty=300, current=100.0)}
    actions = rm.check_portfolio_health(positions, net_liquidation=100_000)
    closes = [a for a in actions if a["action"] == "force_close"]
    assert any("CONCENTRATION BREACH" in a["reason"] for a in closes)


def test_max_loss_breach_triggers_force_close(config, position_factory):
    rm = RiskManager(config)
    # Long entry @ $100 now @ $90 (10% down, > 8% cap)
    positions = {"AAPL": position_factory(symbol="AAPL", entry=100.0, qty=10, current=90.0)}
    actions = rm.check_portfolio_health(positions, net_liquidation=100_000)
    closes = [a for a in actions if a["action"] == "force_close"]
    assert any("MAX LOSS BREACH" in a["reason"] for a in closes)


def test_check_portfolio_health_empty_returns_empty(config):
    rm = RiskManager(config)
    assert rm.check_portfolio_health({}, net_liquidation=100_000) == []


def test_check_portfolio_health_zero_liq_returns_empty(config, position_factory):
    rm = RiskManager(config)
    positions = {"AAPL": position_factory()}
    assert rm.check_portfolio_health(positions, net_liquidation=0) == []
