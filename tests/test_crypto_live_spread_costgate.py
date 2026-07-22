"""Live crypto spread threaded into the cost-vs-edge gate (item D, 2026-07-22).

mean_reversion crypto fired 293 / filled 3 (1%) on 07-21: the cost gate
(manager Rule 6.5) called `cost_model.passes()` WITHOUT a live spread, so it
always used the flat 10bps `crypto_spread_bps_default` — 20bps of round-trip
spread cost that mostly isn't there on liquid majors (BTC/ETH/SOL real spread
~1-5bps). That over-rejected profitable entries whose edge cleared the true
cost but not the inflated one.

Fix chain: MarketData.get_crypto_spread_bps (live Binance.US book) →
engine._enrich_crypto_spreads attaches `live_spread_bps` to crypto buys →
manager passes it to cost_model. Illiquid alts still carry a genuinely wide
live spread, so they keep getting rejected correctly.
"""
from __future__ import annotations

from types import SimpleNamespace

from bot.risk.cost_model import CostModel
from bot.data.market_data import MarketDataFeed
from bot.engine import TradingEngine


def _cost_model():
    cfg = SimpleNamespace(settings={"cost_model": {
        "enabled": True, "crypto_fee_bps": 30.0, "crypto_spread_bps_default": 10.0,
        "spread_mult": 2.0, "min_edge_cost_ratio": 2.0, "crypto_min_edge_cost_ratio": 1.5,
    }})
    return CostModel(cfg)


# A crypto signal with a ~60bps take-profit edge — a real mean_reversion setup.
_SIGNAL = {"symbol": "XRP-USD", "action": "buy", "price": 1.0000, "take_profit": 1.0060}


def test_default_spread_rejects_but_live_spread_passes():
    cm = _cost_model()
    # Default: cost = 30 + 10×2 = 50, threshold = 1.5×50 = 75 → 60bps edge REJECTED.
    passed_default, _ = cm.passes(_SIGNAL, "crypto")
    assert passed_default is False
    # Live tight spread (2bps one-side, a liquid major): cost = 30 + 2×2 = 34,
    # threshold = 1.5×34 = 51 → 60bps edge now PASSES on true economics.
    passed_live, _ = cm.passes(_SIGNAL, "crypto", live_spread_bps=2.0)
    assert passed_live is True


def test_wide_live_spread_still_rejects():
    """An illiquid alt whose real spread is wide stays rejected — the fix does
    not blanket-loosen the gate."""
    cm = _cost_model()
    # 40bps one-side spread: cost = 30 + 40×2 = 110, threshold = 165 → rejected.
    passed, _ = cm.passes(_SIGNAL, "crypto", live_spread_bps=40.0)
    assert passed is False


def test_get_crypto_spread_bps_math_and_noncrypto(monkeypatch):
    md = MarketDataFeed.__new__(MarketDataFeed)  # skip heavy __init__

    class _Resp:
        status_code = 200
        @staticmethod
        def json():
            # 0.10% full spread → mid 100.05, one-side = half = ~5bps
            return {"bidPrice": "100.00", "askPrice": "100.10"}

    monkeypatch.setattr("bot.data.market_data._requests.get", lambda *a, **k: _Resp())
    bps = md.get_crypto_spread_bps("BTC-USD")
    assert bps is not None and 4.5 < bps < 5.5, f"expected ~5bps one-side, got {bps}"
    # Non-crypto short-circuits to None (never hits the network).
    assert md.get_crypto_spread_bps("AAPL") is None


def test_get_crypto_spread_bps_failsafe_none(monkeypatch):
    md = MarketDataFeed.__new__(MarketDataFeed)

    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr("bot.data.market_data._requests.get", _boom)
    assert md.get_crypto_spread_bps("ETH-USD") is None  # miss → None, gate falls back


def test_enrich_only_touches_crypto_buys():
    calls = []

    def fake_spread(sym):
        calls.append(sym)
        return 3.0 if sym.endswith("-USD") else None

    fake_engine = SimpleNamespace(market_data=SimpleNamespace(get_crypto_spread_bps=fake_spread))
    signals = [
        {"symbol": "SOL-USD", "action": "buy"},   # crypto buy → enriched
        {"symbol": "SOL-USD", "action": "sell"},  # exit → skipped
        {"symbol": "AAPL", "action": "buy"},       # equity → spread None, unset
    ]
    TradingEngine._enrich_crypto_spreads(fake_engine, signals)
    assert signals[0]["live_spread_bps"] == 3.0
    assert "live_spread_bps" not in signals[1]
    assert "live_spread_bps" not in signals[2]
    assert "SOL-USD" in calls and signals[1].get("action") == "sell"
