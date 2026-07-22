"""Strategy validator — the honesty instrument (2026-07-22).

Pins the robustness math and the go-live bar so "real edge vs lucky streak"
can never again be judged on net P&L alone. Motivated by crypto mean_reversion
reading +$393 while turning NEGATIVE once its top ~10 trades were removed.
"""
from __future__ import annotations

from bot.learning.strategy_validator import (
    core_metrics, robustness_metrics, evaluate, build_report, is_crypto,
)


def _trades(pnls, strategy="s", symbol="BTC-USD", start_day=1):
    out = []
    for i, p in enumerate(pnls):
        out.append({
            "pnl": p, "strategy": strategy, "symbol": symbol,
            "exit_time": f"2026-07-{start_day + i:02d}T10:00:00",
        })
    return out


def test_core_metrics_basic():
    m = core_metrics(_trades([10, 10, -5, -5]))
    assert m["n"] == 4
    assert m["net"] == 10
    assert m["expectancy"] == 2.5
    assert m["win_rate"] == 50.0
    assert m["profit_factor"] == 2.0  # 20 / 10
    assert m["avg_win"] == 10 and m["avg_loss"] == -5


def test_profit_factor_edges():
    assert core_metrics(_trades([5, 5]))["profit_factor"] == float("inf")  # no losses
    assert core_metrics(_trades([-5, -5]))["profit_factor"] == 0.0          # no wins
    assert core_metrics([])["profit_factor"] == 0.0


def test_robustness_flags_concentration():
    # One huge winner carries 19 small losers: net positive but a mirage.
    trades = _trades([200] + [-5] * 19)
    m = core_metrics(trades)
    r = robustness_metrics(trades, top_pct=0.05)
    assert m["net"] == 105          # looks profitable
    assert r["removed"] == 1        # 5% of 20
    assert r["net_ex_top"] == -95   # collapses without the one winner
    assert r["top_share"] > 1.0     # the top trade is MORE than 100% of net


def test_robustness_recent_third():
    # Positive early, negative late — decay the recency check must catch.
    trades = _trades([10, 10, 10, 10, 10, 10, -20, -20, -20])
    r = robustness_metrics(trades, top_pct=0.05)
    assert r["recent_third_n"] == 3
    assert r["recent_third_net"] == -60  # last three are the -20s


def test_evaluate_graduates_a_real_edge():
    # Broad, consistent, positive-into-recent, survives top removal.
    pnls = ([12, -8] * 90) + [12] * 20   # 200 trades, many small wins
    v = evaluate("good", _trades(pnls), bar={"min_trades": 150})
    assert v.graduated is True, v.failures
    assert v.to_dict()["verdict"] == "GRADUATED"


def test_evaluate_fails_on_concentration_even_if_net_positive():
    trades = _trades([500] + [-2] * 200)  # 201 trades, net positive, one hero
    v = evaluate("mirage", trades, bar={"min_trades": 150})
    assert v.graduated is False
    assert any("collapses without top" in f for f in v.failures)


def test_evaluate_fails_small_sample():
    v = evaluate("thin", _trades([5, 5, 5]))
    assert v.graduated is False
    assert any("sample too small" in f for f in v.failures)


def test_build_report_shape_and_rollups():
    trades = _trades([10, -5], symbol="AAPL") + _trades([8, -3], symbol="ETH-USD")
    rep = build_report(trades)
    assert set(rep) == {"bar", "overall", "crypto", "equity", "strategies"}
    assert rep["crypto"]["n"] == 2 and rep["equity"]["n"] == 2
    assert rep["overall"]["n"] == 4


def test_is_crypto():
    assert is_crypto("BTC-USD") and is_crypto("eth-usdt")
    assert not is_crypto("AAPL") and not is_crypto("TSLA")
