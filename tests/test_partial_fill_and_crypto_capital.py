"""Post-audit follow-ups (2026-07-10, round 2): the three remaining
fixable findings from the go-live audit.

1. BRACKET PARTIAL-FILL: the GTC stop/target children were placed at the
   full requested quantity and never resized after a partial fill — a
   triggered SELL stop would close more shares than owned, flipping the
   account SHORT on live money (execution audit warning, ibkr.py).

2. CRYPTO CAPITAL BASE: crypto executes on a separate TradersPost
   account but was sized against the IBKR balance — capital it cannot
   draw on (risk audit blocker). `crypto.risk.capital_base` (0=legacy)
   swaps the sleeve capital into ALL crypto sizing math.

3. TREND RIDER UNIVERSE: daily_trend_rider (15% allocation) scanned an
   empty symbols list + mover-scan injections only — zero trades ever
   (strategy audit). Now has a static liquid watchlist.
"""
from __future__ import annotations
from pathlib import Path
from types import SimpleNamespace
import yaml

ROOT = Path(__file__).parent.parent
IBKR = (ROOT / "bot" / "brokers" / "ibkr.py").read_text()
STRATS = yaml.safe_load((ROOT / "config" / "strategies.yaml").read_text())
SETTINGS = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())


def _make_sizer(capital_base=0.0):
    from bot.risk.position_sizer import PositionSizer
    cfg = SimpleNamespace(
        risk_per_trade=0.01,
        reserve_cash_pct=0.10,
        risk_config={},
        settings={"crypto": {"risk": {
            "max_position_size_pct": 0.15,
            "capital_base": capital_base,
        }}},
    )
    return PositionSizer(cfg)


def test_bracket_children_resized_on_partial_fill_source():
    """Source pin: the resize block must exist between the partial-fill
    recompute and the return, and must log loudly on failure."""
    assert "BRACKET CHILDREN RESIZED" in IBKR
    assert "tp_order.totalQuantity = filled_qty" in IBKR
    assert "sl_order.totalQuantity = filled_qty" in IBKR
    assert "MANUAL CHECK NEEDED" in IBKR, (
        "resize failure must log at ERROR with a manual-action flag — a "
        "silently oversized SELL stop can flip the account short"
    )
    # Ordering: resize happens before the actual_qty return
    i_resize = IBKR.find("BRACKET CHILDREN RESIZED")
    i_actual = IBKR.rfind("actual_qty = filled_qty if filled_qty > 0 else quantity")
    assert 0 < i_resize < i_actual  # resize precedes the BRACKET path's return


def test_crypto_capital_base_swaps_balance():
    """With capital_base=5000, a BTC-USD sizing on a $25K IBKR balance
    must size against $5K: max_position = 5000*0.15 = $750."""
    sizer = _make_sizer(capital_base=5000)
    # BTC at $100, stop $95 → per-unit risk $5. risk = 5000*0.01 = $50 →
    # qty_by_risk = 10 units = $1000 notional; qty_by_max = 750/100 = 7.5
    # → capped by max_position (proves $5K, not $25K, is the base:
    # at $25K base, max_position would be $3,750 and qty_by_risk would win).
    qty = sizer.calculate(balance=25_000, price=100.0, stop_loss=95.0,
                          symbol="BTC-USD")
    assert qty == 7.5, f"expected 7.5 (sleeve-capped), got {qty}"


def test_crypto_capital_base_zero_is_legacy():
    """capital_base=0 → size against the passed (IBKR) balance as before."""
    sizer = _make_sizer(capital_base=0)
    qty = sizer.calculate(balance=25_000, price=100.0, stop_loss=95.0,
                          symbol="BTC-USD")
    # risk = 25000*0.01 = $250 → qty_by_risk = 50; qty_by_max = 3750/100=37.5
    assert qty == 37.5, f"expected legacy 37.5, got {qty}"


def test_equity_never_uses_crypto_capital_base():
    sizer = _make_sizer(capital_base=5000)
    qty_eq = sizer.calculate(balance=25_000, price=100.0, stop_loss=95.0,
                             symbol="NVDA")
    # equity path: risk $250/$5 = 50 by risk, but max_position 25000*0.15
    # = $3,750 → 37 by value; min = 37. The point: with a $5K crypto base
    # leaking in, this would be 5000*0.15/$100 = 7 shares.
    assert qty_eq == 37, f"equity must ignore crypto capital_base, got {qty_eq}"


def test_settings_capital_base_default_zero():
    assert SETTINGS["crypto"]["risk"]["capital_base"] == 0, (
        "capital_base ships at 0 (legacy/paper) — operator sets it to the "
        "real crypto account equity at go-live"
    )


def test_trend_rider_has_universe():
    syms = STRATS["daily_trend_rider"]["symbols"]
    assert len(syms) >= 15, "daily_trend_rider needs a real static universe"
    assert "NVDA" in syms and "PLTR" in syms
