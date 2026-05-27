"""Trail-arm gate must keep crypto trails un-armed below 0.5% profit.

The session-5(9) "trail can never lock in a loss" fix (commit `18ae5f2`)
floored `new_trail` at `entry_price`, so the trail stops being set BELOW
entry. But it could still be set AT entry the instant `current_price`
ticked one print above — turning the trail into a breakeven stop that
wicked out on the next slippage tick. Live review of 173 crypto trades
(2026-05-17..05-26) caught 12 such exits, all in the -0.04% to -1.29%
band, all with `final_stop` still at the initial 5%-below-entry level.

The fix in `_trail_arm_allowed` requires `pnl_pct >= 0.005` before the
trail may arm on a crypto position. The static `stop_loss` still bounds
the downside while the trail is un-armed.
"""
from __future__ import annotations

from types import SimpleNamespace

from bot.engine import TradingEngine


def _new_engine():
    """Bypass __init__ and inject the only attribute `_trail_arm_allowed` and
    `_is_crypto_symbol` actually read: config.settings (suffix list)."""
    eng = TradingEngine.__new__(TradingEngine)
    eng.config = SimpleNamespace(settings={})
    return eng


def _allow(engine, symbol, pnl_pct, strategy="mean_reversion", runner=False):
    pos = {"strategy": strategy, "momentum_runner": runner}
    return engine._trail_arm_allowed(pos, pnl_pct, symbol)


def test_crypto_trail_blocked_below_arm_threshold():
    eng = _new_engine()
    # _is_crypto_symbol only inspects the suffix, no engine state needed.
    assert _allow(eng, "BTC-USD", 0.000) is False
    assert _allow(eng, "BTC-USD", 0.001) is False
    assert _allow(eng, "BTC-USD", 0.004) is False


def test_crypto_trail_arms_at_threshold():
    eng = _new_engine()
    assert _allow(eng, "ETH-USD", 0.005) is True
    assert _allow(eng, "NEAR-USD", 0.010) is True
    assert _allow(eng, "SOL-USDT", 0.030) is True


def test_crypto_gate_does_not_depend_on_strategy():
    eng = _new_engine()
    # Even momentum-runner crypto (if it ever existed) must clear the
    # crypto floor, since the bug is about wick-back-through-entry, not
    # about the strategy's chosen trail width.
    assert _allow(eng, "BTC-USD", 0.001, strategy="momentum", runner=True) is False
    assert _allow(eng, "BTC-USD", 0.006, strategy="momentum", runner=True) is True


def test_equity_momentum_still_uses_momentum_floor():
    eng = _new_engine()
    # Plain equity momentum: blocked below 2%, allowed above.
    assert _allow(eng, "AAPL", 0.010, strategy="momentum") is False
    assert _allow(eng, "AAPL", 0.020, strategy="momentum") is True
    # momentum_runner bypasses the momentum floor (its own ATR trail handles it).
    assert _allow(eng, "AAPL", 0.001, strategy="momentum", runner=True) is True


def test_equity_non_momentum_always_allowed():
    eng = _new_engine()
    assert _allow(eng, "AAPL", 0.000, strategy="mean_reversion") is True
    assert _allow(eng, "TSLA", -0.005, strategy="smc_forever") is True


def test_call_without_symbol_preserves_legacy_behavior():
    eng = _new_engine()
    # Older callers (none in-tree post-fix) pass symbol=None.
    # Falls through to the momentum-only gate.
    pos = {"strategy": "mean_reversion"}
    assert eng._trail_arm_allowed(pos, -0.10) is True
    assert eng._trail_arm_allowed({"strategy": "momentum"}, 0.01) is False
    assert eng._trail_arm_allowed({"strategy": "momentum"}, 0.03) is True
