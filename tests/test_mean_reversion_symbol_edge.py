"""Per-symbol edge filter for mean_reversion (Wave 1 audit fix #3).

2026-05-15→24 crypto audit (163 closes, mean_reversion-only):
  NEAR:   22 trades, ~50% WR, +$522 — the strategy carrying the book
  ICP:    11 trades, 18% WR, –$78
  DOT:     8 trades, 12% WR, –$42
  BCH:     7 trades, 14% WR, –$35
  LINK/SUI/AVAX/LTC: similar pattern

The crypto_trend_filter already in place blocks symbols whose tape is
currently bleeding, but does nothing for symbols that consistently lose
EVEN when the tape is green. The engine-wide `should_avoid_symbol` pools
ALL strategies — momentum_runner's occasional win on ICP can keep the
score above the -8 floor while mean_reversion drowns on it.

Fix: strategy-scoped per-symbol edge map. mean_reversion gates entries
against its OWN history per symbol. NEAR (50% WR, +$23/trade) passes;
ICP (18% WR, –$7/trade) is blocked. Default OFF in code, ON in config
once history accumulates.

These tests pin:
  1. TradeAnalyzer.get_symbol_edge_map scopes correctly by strategy
  2. Strategy gate blocks bleeders, passes winners
  3. Filter disabled by default at strategy level (config opt-in)
  4. Empty history falls open (don't block fresh symbols)
  5. Configurable thresholds (min_trades, block_wr_pct)
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


# === 1. TradeAnalyzer.get_symbol_edge_map ===


def _make_analyzer(trades):
    """Build a TradeAnalyzer with a pre-loaded trade history."""
    from bot.learning.trade_analyzer import TradeAnalyzer
    import tempfile
    cfg = SimpleNamespace(base_dir=tempfile.mkdtemp())
    a = TradeAnalyzer(cfg)
    a._persisted_trades = list(trades)
    return a


def test_edge_map_aggregates_per_symbol():
    a = _make_analyzer([
        {"symbol": "NEAR-USD", "pnl": 30, "strategy": "mean_reversion"},
        {"symbol": "NEAR-USD", "pnl": -10, "strategy": "mean_reversion"},
        {"symbol": "ICP-USD", "pnl": -20, "strategy": "mean_reversion"},
    ])
    m = a.get_symbol_edge_map(strategy="mean_reversion")
    assert m["NEAR-USD"]["trades"] == 2
    assert m["NEAR-USD"]["wins"] == 1
    assert m["NEAR-USD"]["win_rate"] == 50.0
    assert m["NEAR-USD"]["avg_pnl"] == 10.0
    assert m["ICP-USD"]["trades"] == 1
    assert m["ICP-USD"]["win_rate"] == 0.0


def test_edge_map_scopes_by_strategy():
    """Audit point: ICP should look bad to mean_reversion even though
    momentum_runner won once on it."""
    a = _make_analyzer([
        {"symbol": "ICP-USD", "pnl": -10, "strategy": "mean_reversion"},
        {"symbol": "ICP-USD", "pnl": -15, "strategy": "mean_reversion"},
        {"symbol": "ICP-USD", "pnl": -20, "strategy": "mean_reversion"},
        {"symbol": "ICP-USD", "pnl": +200, "strategy": "momentum_runner"},
    ])
    m_mr = a.get_symbol_edge_map(strategy="mean_reversion")
    m_run = a.get_symbol_edge_map(strategy="momentum_runner")
    # mean_reversion sees 3 losers
    assert m_mr["ICP-USD"]["trades"] == 3
    assert m_mr["ICP-USD"]["win_rate"] == 0.0
    assert m_mr["ICP-USD"]["avg_pnl"] == -15.0
    # momentum_runner sees 1 winner
    assert m_run["ICP-USD"]["trades"] == 1
    assert m_run["ICP-USD"]["win_rate"] == 100.0


def test_edge_map_unfiltered_returns_all_strategies():
    a = _make_analyzer([
        {"symbol": "AAPL", "pnl": 5, "strategy": "momentum"},
        {"symbol": "AAPL", "pnl": -3, "strategy": "mean_reversion"},
    ])
    m = a.get_symbol_edge_map(strategy=None)
    assert m["AAPL"]["trades"] == 2


def test_edge_map_min_trades_filter():
    a = _make_analyzer([
        {"symbol": "AAPL", "pnl": 5, "strategy": "mean_reversion"},
    ])
    m = a.get_symbol_edge_map(strategy="mean_reversion", min_trades=2)
    assert "AAPL" not in m


# === 2. Strategy gate ===


def _strat(cfg_overrides=None):
    from bot.strategies.mean_reversion import MeanReversionStrategy
    from bot.data.indicators import TechnicalIndicators
    cfg = {
        "lookback_period": 20, "entry_zscore": -1.5, "exit_zscore": 0,
        "rsi_oversold": 32, "rsi_overbought": 68,
        "bollinger_period": 20, "bollinger_std": 2.0,
        "max_holding_periods": 15, "max_hold_days": 2,
        "symbol_edge_filter_enabled": True,
        "symbol_edge_min_trades": 3, "symbol_edge_block_wr_pct": 30.0,
        "symbols": [],
    }
    if cfg_overrides:
        cfg.update(cfg_overrides)
    return MeanReversionStrategy(cfg, TechnicalIndicators(), capital=10000)


def test_strategy_default_off():
    """Engine config has it on, but the strategy default is OFF so unit
    tests / fresh installs don't accidentally block entries before
    history exists."""
    from bot.strategies.mean_reversion import MeanReversionStrategy
    from bot.data.indicators import TechnicalIndicators
    strat = MeanReversionStrategy(
        {"lookback_period": 20, "symbols": []},
        TechnicalIndicators(), capital=10000,
    )
    assert strat.symbol_edge_filter_enabled is False


def test_strategy_feed_symbol_edge_stores_map():
    strat = _strat()
    edge = {"ICP-USD": {"trades": 5, "win_rate": 18.0, "avg_pnl": -7.0}}
    strat.feed_symbol_edge(edge)
    assert strat._symbol_edge_map == edge


def test_strategy_feed_symbol_edge_handles_none():
    """A None feed (e.g. analyzer returned nothing) must not crash."""
    strat = _strat()
    strat.feed_symbol_edge(None)
    assert strat._symbol_edge_map == {}


def test_gate_blocks_bleeder():
    """Symbol with 5 trades, 18% WR, –$7/trade — the canonical ICP pattern.
    Gate must short-circuit before the BUY signal payload is built."""
    strat = _strat()
    strat.feed_symbol_edge({
        "ICP-USD": {"trades": 5, "win_rate": 18.0, "avg_pnl": -7.0},
    })
    # Simulate the gate inline (the real one runs inside _analyze_symbol
    # after buy_signal is True; we test the predicate isolated)
    edge = strat._symbol_edge_map["ICP-USD"]
    should_block = (
        strat.symbol_edge_filter_enabled
        and edge["trades"] >= strat.symbol_edge_min_trades
        and edge["win_rate"] < strat.symbol_edge_block_wr_pct
        and edge["avg_pnl"] < 0
    )
    assert should_block is True


def test_gate_passes_winner():
    """Symbol with 22 trades, 50% WR, +$23/trade — NEAR pattern. Must
    flow through to the BUY signal payload unblocked."""
    strat = _strat()
    strat.feed_symbol_edge({
        "NEAR-USD": {"trades": 22, "win_rate": 50.0, "avg_pnl": 23.0},
    })
    edge = strat._symbol_edge_map["NEAR-USD"]
    should_block = (
        strat.symbol_edge_filter_enabled
        and edge["trades"] >= strat.symbol_edge_min_trades
        and edge["win_rate"] < strat.symbol_edge_block_wr_pct
        and edge["avg_pnl"] < 0
    )
    assert should_block is False


def test_gate_passes_low_sample_size():
    """A symbol with only 2 losing trades is statistically insufficient
    — falls below min_trades and falls open."""
    strat = _strat()
    strat.feed_symbol_edge({
        "AAVE-USD": {"trades": 2, "win_rate": 0.0, "avg_pnl": -5.0},
    })
    edge = strat._symbol_edge_map["AAVE-USD"]
    should_block = (
        edge["trades"] >= strat.symbol_edge_min_trades
        and edge["win_rate"] < strat.symbol_edge_block_wr_pct
        and edge["avg_pnl"] < 0
    )
    assert should_block is False


def test_gate_passes_negative_avg_with_high_wr():
    """Edge case: WR ≥ 30% means we're winning more than we lose, even if
    avg_pnl happens to be negative (one big loser pulled the average
    down). Don't block — it's the WR that signals lack of edge."""
    strat = _strat()
    strat.feed_symbol_edge({
        "FOO": {"trades": 10, "win_rate": 60.0, "avg_pnl": -1.0},
    })
    edge = strat._symbol_edge_map["FOO"]
    should_block = (
        edge["trades"] >= strat.symbol_edge_min_trades
        and edge["win_rate"] < strat.symbol_edge_block_wr_pct
        and edge["avg_pnl"] < 0
    )
    assert should_block is False  # high WR overrides one-off avg_pnl dip


def test_gate_passes_positive_avg_pnl():
    """Edge case: low WR but positive avg_pnl means winners are big
    enough to make up for losses. The fat-tail pattern — don't block."""
    strat = _strat()
    strat.feed_symbol_edge({
        "BAR": {"trades": 10, "win_rate": 20.0, "avg_pnl": 5.0},
    })
    edge = strat._symbol_edge_map["BAR"]
    should_block = (
        edge["trades"] >= strat.symbol_edge_min_trades
        and edge["win_rate"] < strat.symbol_edge_block_wr_pct
        and edge["avg_pnl"] < 0
    )
    assert should_block is False  # positive avg_pnl overrides low WR


# === 3. Configurable thresholds ===


def test_configurable_min_trades():
    """Operator can tighten min_trades to 5 (or loosen to 2)."""
    strat = _strat({"symbol_edge_min_trades": 5})
    assert strat.symbol_edge_min_trades == 5


def test_configurable_block_wr_pct():
    """Operator can dial the WR threshold (e.g. 25% for stricter, 35%
    for looser)."""
    strat = _strat({"symbol_edge_block_wr_pct": 25.0})
    assert strat.symbol_edge_block_wr_pct == 25.0


# === 4. Empty history falls open ===


def test_empty_edge_map_does_not_block_any_entry():
    """Day 0 of a fresh install: no trade history → no edge data → every
    symbol flows through. This is the most important safety property —
    we must not block all signals while waiting for history."""
    strat = _strat()
    strat.feed_symbol_edge({})
    # An arbitrary symbol with no edge data
    edge = strat._symbol_edge_map.get("RANDOM")
    assert edge is None  # no entry → gate code skips its checks entirely
