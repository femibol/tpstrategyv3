"""low_float_catalyst signals exempt from the universal $0.50 price floor.

Live case 2026-06-01: TGHL ran $0.30 → $2.48 intraday (+575%) on 133M
volume / $34M market cap. Textbook low-float runner — exactly the regime
low_float_catalyst was built for in HANDOFF session 5(8) (GOVX, VRAX,
SBFM, WGRX all the same pattern). But the bot couldn't take it because
the universal `risk.min_price = $0.50` engine-level floor at
`engine.py:5924` blocked any equity buy below $0.50.

The asymmetric edge of low_float_catalyst lives in the $0.30-$0.50 range
(per strategy config `low_float_catalyst.min_price = 0.20` already admits
this band). Entering at $1.35+ after the move misses 4-5x of the move.
The universal floor is a momentum/rvol_* guard against penny-stock junk;
the low_float strategy has its OWN tighter price band + RVOL/float/spread
gates designed to admit only legitimate setups in this range.

Fix: exempt signals where `strategy == "low_float_catalyst"` from the
universal floor. The strategy's own min_price still applies. The universal
floor still applies to momentum / rvol_momentum / rvol_scalp / etc.
Mirror of the crypto exemption (different asset class, same pattern).

These tests pin three guarantees:
  1. low_float_catalyst signals are NOT blocked by the universal floor
  2. momentum and other equity strategies ARE still blocked by it
  3. The exemption gate is signal-strategy-keyed (not symbol-keyed) so it
     can't be triggered by passing the same symbol through a different
     strategy
"""
from __future__ import annotations


def _engine_inline_price_filter(signal, current_price, min_price=0.50,
                                 is_crypto=False):
    """Mirror of the inline check at engine.py:~5924. Returns True if the
    signal would be BLOCKED by the universal price floor under the new
    rules. False means the signal is allowed through this gate."""
    is_low_float = signal.get("strategy") == "low_float_catalyst"
    if (signal.get("action") == "buy"
            and current_price < min_price
            and not is_crypto
            and not is_low_float):
        return True
    return False


def test_low_float_signal_at_30c_not_blocked():
    """TGHL-style: low_float_catalyst on a $0.30 stock must pass."""
    sig = {"action": "buy", "symbol": "TGHL", "strategy": "low_float_catalyst"}
    blocked = _engine_inline_price_filter(sig, current_price=0.34)
    assert blocked is False


def test_low_float_signal_at_45c_not_blocked():
    """Right at the edge of the old floor — still within asymmetric edge
    band of low_float_catalyst."""
    sig = {"action": "buy", "symbol": "TGHL", "strategy": "low_float_catalyst"}
    blocked = _engine_inline_price_filter(sig, current_price=0.45)
    assert blocked is False


def test_momentum_signal_at_30c_still_blocked():
    """Universal floor must still protect momentum / rvol_* from penny junk.
    They have NO float/RVOL gates as tight as low_float_catalyst and would
    fire on illiquid noise if the floor is lifted globally."""
    sig = {"action": "buy", "symbol": "JUNK", "strategy": "momentum"}
    blocked = _engine_inline_price_filter(sig, current_price=0.34)
    assert blocked is True


def test_rvol_momentum_signal_at_30c_still_blocked():
    sig = {"action": "buy", "symbol": "JUNK", "strategy": "rvol_momentum"}
    blocked = _engine_inline_price_filter(sig, current_price=0.34)
    assert blocked is True


def test_low_float_above_floor_obviously_not_blocked():
    """Sanity: nothing weird happens at normal prices."""
    sig = {"action": "buy", "symbol": "GOVX", "strategy": "low_float_catalyst"}
    blocked = _engine_inline_price_filter(sig, current_price=2.50)
    assert blocked is False


def test_crypto_still_exempt_regardless_of_strategy():
    """Crypto exemption was the original — must not regress."""
    sig = {"action": "buy", "symbol": "BTC-USD", "strategy": "mean_reversion"}
    blocked = _engine_inline_price_filter(sig, current_price=0.0001, is_crypto=True)
    assert blocked is False


def test_sell_action_never_blocked_by_floor():
    """Floor is BUY-only. Exits and other actions must pass regardless."""
    for strat in ("momentum", "rvol_scalp", "low_float_catalyst"):
        for act in ("sell", "exit"):
            sig = {"action": act, "symbol": "JUNK", "strategy": strat}
            assert _engine_inline_price_filter(sig, current_price=0.10) is False


def test_engine_inline_code_has_strategy_check():
    """Lock the engine code to the strategy-keyed exemption. A refactor
    that drops the `signal.get("strategy") == "low_float_catalyst"`
    check silently re-introduces the TGHL miss."""
    with open("bot/engine.py") as f:
        src = f.read()
    assert 'signal.get("strategy") == "low_float_catalyst"' in src \
        or "signal.get('strategy') == 'low_float_catalyst'" in src, (
        "Engine code must keep the strategy-keyed exemption check for "
        "low_float_catalyst — see TGHL incident 2026-06-01."
    )
    assert "is_low_float_signal" in src, (
        "is_low_float_signal flag must appear in the engine sync block "
        "so the gate condition reads naturally."
    )
