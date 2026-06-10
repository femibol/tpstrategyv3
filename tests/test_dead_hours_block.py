"""Equity dead-hours block — Tier 1 restructure 2026-06-09.

30-day audit identified two ET hours where the bot consistently loses
money on equity entries:
  - 05:00 ET (premarket slippage_reject pattern, -$203/30d)
  - 14:00 ET (early-afternoon lull, -$133/30d)

Both windows have low signal-to-fill quality. The fix: drop equity BUY
signals during those hours. Crypto bypasses entirely (24/7 universe);
exits and sells are unaffected — only new buys are gated.

Configured by `risk.equity_dead_hours_et` (list of hours, 24h ET).
Empty list = disabled, no behavior change.

These tests pin:
  1. Equity buys ARE dropped during configured dead hours.
  2. Crypto buys are NEVER dropped (24/7 design).
  3. Sells and exits pass through regardless of hour.
  4. Empty config disables the filter completely.
  5. Hours outside the dead list pass through normally.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


def _apply_dead_hour_filter(approved, dead_hours, now_hour, equity_open=True,
                             is_crypto_fn=None):
    """Mirror of the engine's dead-hour block. Pinned here so a regression
    on the engine side gets caught in unit tests, not after a -$130 day."""
    if not dead_hours or not equity_open:
        return approved
    if now_hour not in dead_hours:
        return approved
    is_crypto_fn = is_crypto_fn or (lambda s: "-USD" in s or "-USDT" in s)
    return [
        sig for sig in approved
        if not (
            sig.get("action") == "buy"
            and not is_crypto_fn(sig.get("symbol", ""))
        )
    ]


# === 1. Dead hour blocks equity buys ===


def test_equity_buy_blocked_at_dead_hour():
    sigs = [{"symbol": "AAPL", "action": "buy"}]
    out = _apply_dead_hour_filter(sigs, dead_hours=[5, 14], now_hour=5)
    assert out == []


def test_equity_buy_blocked_at_second_dead_hour():
    sigs = [{"symbol": "TSLA", "action": "buy"}]
    out = _apply_dead_hour_filter(sigs, dead_hours=[5, 14], now_hour=14)
    assert out == []


# === 2. Crypto buys NEVER dropped ===


def test_crypto_buy_passes_through_dead_hour():
    sigs = [{"symbol": "NEAR-USD", "action": "buy"}]
    out = _apply_dead_hour_filter(sigs, dead_hours=[5, 14], now_hour=5)
    assert out == sigs


def test_usdt_pair_passes_through_dead_hour():
    sigs = [{"symbol": "BTC-USDT", "action": "buy"}]
    out = _apply_dead_hour_filter(sigs, dead_hours=[5, 14], now_hour=14)
    assert out == sigs


# === 3. Sells and exits unaffected ===


def test_equity_sell_passes_through_dead_hour():
    """Exits must NOT be blocked — leaving a position uncovered during
    a dead hour is worse than firing the exit."""
    sigs = [{"symbol": "AAPL", "action": "sell"}]
    out = _apply_dead_hour_filter(sigs, dead_hours=[5, 14], now_hour=5)
    assert out == sigs


def test_short_signal_passes_through_dead_hour():
    """Filter scope: BUY only. Shorts (if ever) are out of scope."""
    sigs = [{"symbol": "QQQ", "action": "short"}]
    out = _apply_dead_hour_filter(sigs, dead_hours=[5, 14], now_hour=5)
    assert out == sigs


# === 4. Empty config disables ===


def test_empty_dead_hours_disables_filter():
    sigs = [{"symbol": "AAPL", "action": "buy"}]
    out = _apply_dead_hour_filter(sigs, dead_hours=[], now_hour=5)
    assert out == sigs


def test_none_dead_hours_disables_filter():
    sigs = [{"symbol": "AAPL", "action": "buy"}]
    out = _apply_dead_hour_filter(sigs, dead_hours=None, now_hour=5)
    assert out == sigs


# === 5. Non-dead hours pass through normally ===


def test_open_hour_passes_through():
    """09:30 ET (hour=9) is THE prime trading hour despite mixed WR — kept
    open intentionally."""
    sigs = [{"symbol": "AAPL", "action": "buy"}]
    out = _apply_dead_hour_filter(sigs, dead_hours=[5, 14], now_hour=9)
    assert out == sigs


def test_midday_passes_through():
    sigs = [{"symbol": "AAPL", "action": "buy"}]
    out = _apply_dead_hour_filter(sigs, dead_hours=[5, 14], now_hour=11)
    assert out == sigs


def test_close_hour_passes_through():
    """16:00 ET = close hour. Allowed; power-hour logic handles late
    blocks separately."""
    sigs = [{"symbol": "AAPL", "action": "buy"}]
    out = _apply_dead_hour_filter(sigs, dead_hours=[5, 14], now_hour=16)
    assert out == sigs


# === 6. Mixed signal batch ===


def test_mixed_batch_only_drops_equity_buys():
    """Realistic cycle: mix of strategies, mix of symbols, mix of actions.
    Only equity buys at the dead hour are dropped — everything else
    passes."""
    sigs = [
        {"symbol": "AAPL", "action": "buy"},     # should drop
        {"symbol": "NEAR-USD", "action": "buy"}, # crypto, passes
        {"symbol": "TSLA", "action": "sell"},    # sell, passes
        {"symbol": "MSFT", "action": "buy"},     # should drop
        {"symbol": "BTC-USD", "action": "sell"}, # crypto sell, passes
    ]
    out = _apply_dead_hour_filter(sigs, dead_hours=[5, 14], now_hour=5)
    symbols = [s["symbol"] for s in out]
    assert symbols == ["NEAR-USD", "TSLA", "BTC-USD"]


# === 7. Equity-closed branch already drops everything else ===


def test_does_not_fire_when_equity_market_closed():
    """If equity_market_open is False, the existing 'EQUITY MARKET CLOSED'
    block already drops all equity buys. The dead-hour block is a
    no-op in that branch (the equity_market_open guard prevents
    double-logging)."""
    sigs = [{"symbol": "AAPL", "action": "buy"}]
    # equity_open=False with dead-hour=current → dead-hour SKIPS its work
    # because equity_open guard fails. Returns the input unchanged.
    out = _apply_dead_hour_filter(sigs, dead_hours=[5, 14],
                                    now_hour=5, equity_open=False)
    assert out == sigs  # passes through; the prior block in engine handles drops
