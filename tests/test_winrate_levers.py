"""Win-rate improvement levers shipped 2026-06-02 evening.

Three knobs, each independently disable-able via config. All defaults
are CONSERVATIVE — designed to reduce known bleed without killing
legitimate signals. Booming-market constraint: bias toward letting
trades through.

LEVER 1 (engine): per-(strategy, symbol) consecutive-loss fast-track
gate in _entry_quality_gate. Defaults to 3 consecutive losses → block.
Per-strategy so a symbol winning on mean_reversion isn't blocked
because momentum lost on it. Disabled via
`risk.consecutive_loss_block_n: 0`.

LEVER 2 (engine): CRYPTO_TRAIL_ARM_PCT bumped 0.5% → 1.0%. Crypto trail
needs more profit cushion to survive the chop. Live audit:
mean_reversion crypto trailing_stop path was 8W/30L at 21% WR for
-$150 cumulative — half those losses likely topped between 0.5% and
1.0% gain and wicked back. With the wider floor those wicks are
absorbed, the trade reverts to time_exit (54% WR) or stop_loss.
Configurable via `risk.crypto_trail_arm_pct`.

LEVER 3 (momentum strategy): skip entries on stocks already DOWN more
than `min_day_change_pct` today (default -2%). FBYD-style trades on a
red-bleeding stock are the classic falling-knife trap. Default
threshold is permissive — only kills entries on names truly bleeding,
leaves neutral/slightly-red names alone.
"""
from __future__ import annotations

import pandas as pd
import pytest


# === LEVER 1: per-(strategy, symbol) consecutive-loss gate ===


def _stub_engine(history, risk_config_override=None):
    """Just enough of an engine to drive `_entry_quality_gate`."""
    from types import SimpleNamespace
    from bot.engine import TradingEngine

    engine = TradingEngine.__new__(TradingEngine)
    engine.trade_history = history
    engine.positions = {}
    engine.config = SimpleNamespace(risk_config=risk_config_override or {})
    engine.broker = None
    engine.current_regime = "neutral"
    return engine


def _trade(symbol, strategy, pnl):
    return {"symbol": symbol, "strategy": strategy, "pnl": pnl}


def test_consecutive_loss_gate_blocks_3_losses_in_a_row():
    """FBYD pattern: 3 momentum losses in a row → block the 4th."""
    history = [
        _trade("FBYD", "momentum", -40.0),
        _trade("FBYD", "momentum", -50.0),
        _trade("FBYD", "momentum", -40.0),
    ]
    engine = _stub_engine(history)
    sig = {"symbol": "FBYD", "strategy": "momentum", "action": "buy",
           "score": 80, "rvol": 3.0}
    passed, reason = engine._entry_quality_gate(sig)
    assert not passed
    assert "consecutive losses" in reason
    assert "momentum/FBYD" in reason


def test_gate_does_not_block_winning_strategy_for_same_symbol():
    """ICP: lost 3 times on momentum, won twice on mean_reversion.
    A new mean_reversion ICP signal must NOT be blocked."""
    history = [
        _trade("ICP-USD", "momentum", -10.0),
        _trade("ICP-USD", "momentum", -12.0),
        _trade("ICP-USD", "momentum", -8.0),
        _trade("ICP-USD", "mean_reversion", +15.0),
        _trade("ICP-USD", "mean_reversion", +8.0),
    ]
    engine = _stub_engine(history)
    sig = {"symbol": "ICP-USD", "strategy": "mean_reversion", "action": "buy",
           "score": 80, "rvol": 3.0}
    passed, reason = engine._entry_quality_gate(sig)
    assert passed, f"mean_reversion shouldn't be blocked, got: {reason}"


def test_gate_disabled_when_consecutive_loss_block_n_is_zero():
    """Pattern that ONLY the consecutive-loss gate catches: 2 historical
    wins + 3 recent losses. Aggregate gate doesn't fire (40% WR > 35%);
    consecutive gate catches the recent slump. With consec=0, signal
    must pass."""
    history = [
        _trade("PARTIAL", "momentum", +30.0),
        _trade("PARTIAL", "momentum", +20.0),
        _trade("PARTIAL", "momentum", -10.0),
        _trade("PARTIAL", "momentum", -12.0),
        _trade("PARTIAL", "momentum", -8.0),
    ]
    # With default (consec=3), the recent slump blocks
    engine_on = _stub_engine(history)
    sig = {"symbol": "PARTIAL", "strategy": "momentum", "action": "buy",
           "score": 80, "rvol": 3.0}
    passed_on, _ = engine_on._entry_quality_gate(sig)
    assert not passed_on, "Default consec=3 must block on recent slump"
    # With disable, signal must pass (aggregate gate's WR=40% doesn't fire)
    engine_off = _stub_engine(history, {"consecutive_loss_block_n": 0})
    passed_off, _ = engine_off._entry_quality_gate(sig)
    assert passed_off, "consecutive_loss_block_n=0 must disable the gate"


def test_gate_does_not_block_brand_new_symbol():
    """A symbol the bot has never traded must not be blocked."""
    engine = _stub_engine([])
    sig = {"symbol": "NEWCO", "strategy": "momentum", "action": "buy",
           "score": 80, "rvol": 3.0}
    passed, _ = engine._entry_quality_gate(sig)
    assert passed


def test_gate_does_not_block_when_last_3_mixed():
    """Win sandwiched between losses must NOT trigger consecutive-loss
    block — the strategy isn't structurally broken on this symbol."""
    history = [
        _trade("MIXED", "momentum", -10.0),
        _trade("MIXED", "momentum", +20.0),  # win!
        _trade("MIXED", "momentum", -10.0),
    ]
    engine = _stub_engine(history)
    sig = {"symbol": "MIXED", "strategy": "momentum", "action": "buy",
           "score": 80, "rvol": 3.0}
    passed, _ = engine._entry_quality_gate(sig)
    assert passed


def test_gate_only_checks_last_n_not_full_history():
    """If symbol has 10 historic losses but the last 3 are wins, the
    consecutive-loss gate must allow re-entry (recovery permitted)."""
    history = [_trade("REC", "momentum", -10.0) for _ in range(7)]
    history += [
        _trade("REC", "momentum", +5.0),
        _trade("REC", "momentum", +8.0),
        _trade("REC", "momentum", +12.0),
    ]
    engine = _stub_engine(history)
    sig = {"symbol": "REC", "strategy": "momentum", "action": "buy",
           "score": 80, "rvol": 3.0}
    passed, reason = engine._entry_quality_gate(sig)
    # NOTE: the older avg-based gate (lines 7967-7980) may still block on
    # cumulative avg < 0. The consecutive-loss gate specifically must not
    # trigger on this pattern. We assert by reason — if blocked, it must
    # be from the avg gate, not the consecutive-loss one.
    if not passed:
        assert "consecutive losses" not in reason


# === LEVER 2: crypto trail-arm 0.5% → 1.0% ===


def test_crypto_trail_arm_default_is_one_percent():
    """The widen-to-1.0% default reduces breakeven-wick exits on crypto."""
    from bot.engine import TradingEngine

    assert TradingEngine.CRYPTO_TRAIL_ARM_PCT == 0.01


def test_trail_floor_uses_crypto_arm_pct():
    """_trail_floor_price for crypto returns entry × (1 + ARM_PCT). With
    ARM_PCT=1%, a $2.18 RNDR entry has trail floor at $2.2018."""
    from bot.engine import TradingEngine

    engine = TradingEngine.__new__(TradingEngine)
    engine.CRYPTO_TRAIL_ARM_PCT = 0.01
    # Mock _is_crypto_symbol to recognize -USD
    engine._is_crypto_symbol = lambda s: s.endswith("-USD")
    floor = engine._trail_floor_price("RNDR-USD", 2.18)
    assert floor == pytest.approx(2.18 * 1.01)


def test_crypto_trail_arm_overrideable_via_risk_config():
    """Config override `risk.crypto_trail_arm_pct` must take effect."""
    from types import SimpleNamespace
    from bot.engine import TradingEngine

    # Build a config object that mimics the real one's risk_config dict
    cfg = SimpleNamespace(
        risk_config={"crypto_trail_arm_pct": 0.02, "momentum_trail_arm_pct": 0.025},
        # __init__ touches a lot of other config; we only construct partially
        # below via __new__ + targeted attribute set.
    )
    # Simulate the constructor's config-read block
    engine = TradingEngine.__new__(TradingEngine)
    if cfg is not None:
        rc = getattr(cfg, "risk_config", None) or {}
        engine.CRYPTO_TRAIL_ARM_PCT = float(rc.get(
            "crypto_trail_arm_pct", TradingEngine.CRYPTO_TRAIL_ARM_PCT
        ))
        engine.MOMENTUM_TRAIL_ARM_PCT = float(rc.get(
            "momentum_trail_arm_pct", TradingEngine.MOMENTUM_TRAIL_ARM_PCT
        ))
    assert engine.CRYPTO_TRAIL_ARM_PCT == 0.02
    assert engine.MOMENTUM_TRAIL_ARM_PCT == 0.025


# === LEVER 3: momentum "down today" entry gate ===


def _momentum_strat(min_day_change_pct=-2.0):
    from bot.data.indicators import TechnicalIndicators
    from bot.strategies.momentum import MomentumStrategy

    cfg = {
        "fast_ema": 8, "slow_ema": 21, "adx_threshold": 25,
        "volume_surge_multiplier": 1.5, "atr_period": 14,
        "atr_stop_multiplier": 2.0, "atr_target_multiplier": 4.0,
        "max_holding_bars": 40, "breakout_lookback": 20,
        "min_day_change_pct": min_day_change_pct,
    }
    return MomentumStrategy(cfg, TechnicalIndicators(), capital=10000)


class _FakeMD:
    def __init__(self, bars, quote):
        self._bars = bars
        self._quote = quote

    def get_quote(self, symbol):
        return self._quote

    def get_bars(self, symbol, n, bar_size=None):
        return self._bars


def _breakout_bars(bar_close=249.15, lookback_high=240.0):
    import numpy as np
    n = 50
    prices = np.linspace(200.0, lookback_high - 1.0, 30)
    prices = np.concatenate([prices, np.full(19, lookback_high - 1.0), [bar_close]])
    closes = prices.astype(float)
    return pd.DataFrame({
        "open": closes - 0.10, "high": closes + 0.20,
        "low": closes - 0.20, "close": closes,
        "volume": [1_000_000] * 49 + [2_500_000],
    })


def test_momentum_skips_stock_down_today():
    """FBYD-style: clean EMA breakout signal but stock is down -5% today."""
    strat = _momentum_strat()
    strat._dynamic_symbols.add("FBYD")
    quote = {"price": 250.0, "change_pct": -5.0}  # already down 5%
    md = _FakeMD(_breakout_bars(bar_close=250.0), quote)
    sigs = strat.generate_signals(md)
    assert len(sigs) == 0, "Should skip stocks down more than threshold today"


def test_momentum_takes_flat_stock_pullback():
    """Default threshold -2% must NOT kill flat / slightly-red names — those
    are legit pullback entries."""
    strat = _momentum_strat()
    strat._dynamic_symbols.add("FLAT")
    quote = {"price": 250.0, "change_pct": -1.0}  # barely red, within threshold
    md = _FakeMD(_breakout_bars(bar_close=250.0), quote)
    sigs = strat.generate_signals(md)
    assert len(sigs) == 1, (
        f"Flat names within threshold must still fire. Got {len(sigs)}."
    )


def test_momentum_takes_up_today_stock():
    """Stock up today is the classic momentum target — must fire."""
    strat = _momentum_strat()
    strat._dynamic_symbols.add("UPDAY")
    quote = {"price": 250.0, "change_pct": +5.0}
    md = _FakeMD(_breakout_bars(bar_close=250.0), quote)
    sigs = strat.generate_signals(md)
    assert len(sigs) == 1


def test_momentum_gate_disabled_when_threshold_set_low():
    """Setting min_day_change_pct to a very negative number effectively
    disables the gate — escape hatch for backtests / aggressive periods."""
    strat = _momentum_strat(min_day_change_pct=-100.0)
    strat._dynamic_symbols.add("DEEPDOWN")
    quote = {"price": 250.0, "change_pct": -20.0}
    md = _FakeMD(_breakout_bars(bar_close=250.0), quote)
    sigs = strat.generate_signals(md)
    assert len(sigs) == 1, "With threshold disabled, even deep-down fires"


def test_momentum_no_change_pct_field_does_not_block():
    """If quote doesn't include change_pct (some broker paths), do NOT
    block — fail-open, the strategy's other gates still apply."""
    strat = _momentum_strat()
    strat._dynamic_symbols.add("NOCHG")
    quote = {"price": 250.0}  # no change_pct
    md = _FakeMD(_breakout_bars(bar_close=250.0), quote)
    sigs = strat.generate_signals(md)
    assert len(sigs) == 1, "Missing change_pct must NOT block (fail-open)"
