"""CoinGecko top-volume crypto scanner — parse / filter / cache / fallback.

The scanner replaces the hand-maintained ``crypto.symbols`` list when
``crypto.dynamic_universe.enabled`` is true so newly-listed runners
(WLD, DRIFT, JTO, ONDO, …) are picked up automatically. CoinGecko's
``/coins/markets?order=volume_desc`` is fronted by a 24h on-disk cache
so we only hit the API once a day per process.

These tests pin three guarantees:
  1. Stablecoins / wrapped tokens are filtered out (USDT alone outranks
     BTC by volume most days — without this filter the top-N would be
     half stablecoins).
  2. Network failure falls back to stale cache, and to empty list if no
     cache exists — never raises, never returns garbage.
  3. The 24h cache is honored — a fresh-cache call doesn't re-hit the
     network.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from unittest import mock

import pytest

from bot.data import crypto_scanner


# Each test gets a clean module-level cache + tmp cache path so they
# don't leak state into each other or into the real data/ directory.
@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    cache_path = str(tmp_path / "crypto_scanner_cache.json")
    monkeypatch.setattr(crypto_scanner, "_CACHE_PATH", cache_path)
    monkeypatch.setattr(crypto_scanner, "_mem_cache", None)
    yield


def _mk_row(symbol, volume=1e9):
    return {
        "id": symbol.lower(),
        "symbol": symbol.lower(),
        "name": symbol.title(),
        "total_volume": volume,
        "market_cap": volume * 10,
    }


def test_returns_tickers_in_usd_format(monkeypatch):
    rows = [_mk_row("btc"), _mk_row("eth"), _mk_row("near"), _mk_row("wld")]
    monkeypatch.setattr(crypto_scanner, "_fetch_from_coingecko", lambda limit: rows)
    out = crypto_scanner.top_volume_symbols(limit=10)
    assert out == ["BTC-USD", "ETH-USD", "NEAR-USD", "WLD-USD"]


def test_blacklist_filters_stablecoins_and_wrapped(monkeypatch):
    # CoinGecko by volume is typically: USDT > USDC > BTC > ETH > DAI > WBTC > ...
    rows = [
        _mk_row("usdt"), _mk_row("usdc"), _mk_row("btc"),
        _mk_row("eth"), _mk_row("dai"), _mk_row("wbtc"),
        _mk_row("steth"), _mk_row("sol"), _mk_row("weth"),
        _mk_row("near"),
    ]
    monkeypatch.setattr(crypto_scanner, "_fetch_from_coingecko", lambda limit: rows)
    out = crypto_scanner.top_volume_symbols(limit=10)
    # All stables + wrapped derivatives gone; tradeables kept in order.
    assert out == ["BTC-USD", "ETH-USD", "SOL-USD", "NEAR-USD"]


def test_limit_honored_after_filter(monkeypatch):
    rows = [_mk_row("usdt")] + [_mk_row(f"coin{i}") for i in range(20)]
    monkeypatch.setattr(crypto_scanner, "_fetch_from_coingecko", lambda limit: rows)
    out = crypto_scanner.top_volume_symbols(limit=5)
    assert len(out) == 5
    assert "USDT-USD" not in out


def test_dedupes_duplicate_symbols(monkeypatch):
    rows = [_mk_row("btc"), _mk_row("eth"), _mk_row("btc"), _mk_row("sol")]
    monkeypatch.setattr(crypto_scanner, "_fetch_from_coingecko", lambda limit: rows)
    out = crypto_scanner.top_volume_symbols(limit=10)
    assert out == ["BTC-USD", "ETH-USD", "SOL-USD"]


def test_network_failure_with_stale_cache_returns_cache(monkeypatch):
    # Prime the cache.
    rows = [_mk_row("btc"), _mk_row("eth")]
    monkeypatch.setattr(crypto_scanner, "_fetch_from_coingecko", lambda limit: rows)
    crypto_scanner.top_volume_symbols(limit=10)
    # Reset in-memory cache so the next call goes through the disk-cache code path,
    # then make the cache appear stale (older than TTL).
    monkeypatch.setattr(crypto_scanner, "_mem_cache", None)
    cache = crypto_scanner._load_cache()
    cache["top_volume"]["ts"] = time.time() - (crypto_scanner._CACHE_TTL_SEC + 1)
    # Network down now.
    monkeypatch.setattr(crypto_scanner, "_fetch_from_coingecko", lambda limit: None)
    out = crypto_scanner.top_volume_symbols(limit=10)
    # Falls back to stale cached values, doesn't raise, doesn't return [].
    assert out == ["BTC-USD", "ETH-USD"]


def test_network_failure_with_no_cache_returns_empty(monkeypatch):
    monkeypatch.setattr(crypto_scanner, "_fetch_from_coingecko", lambda limit: None)
    out = crypto_scanner.top_volume_symbols(limit=10)
    assert out == []


def test_fresh_cache_skips_network(monkeypatch):
    rows = [_mk_row("btc"), _mk_row("eth")]
    call_count = {"n": 0}

    def fake_fetch(limit):
        call_count["n"] += 1
        return rows

    monkeypatch.setattr(crypto_scanner, "_fetch_from_coingecko", fake_fetch)
    crypto_scanner.top_volume_symbols(limit=10)
    crypto_scanner.top_volume_symbols(limit=10)
    crypto_scanner.top_volume_symbols(limit=10)
    assert call_count["n"] == 1  # only the first call hit the network


def test_force_refresh_bypasses_cache(monkeypatch):
    rows = [_mk_row("btc")]
    call_count = {"n": 0}

    def fake_fetch(limit):
        call_count["n"] += 1
        return rows

    monkeypatch.setattr(crypto_scanner, "_fetch_from_coingecko", fake_fetch)
    crypto_scanner.top_volume_symbols(limit=10)
    crypto_scanner.top_volume_symbols(limit=10, force_refresh=True)
    assert call_count["n"] == 2


def test_zero_limit_returns_empty():
    assert crypto_scanner.top_volume_symbols(limit=0) == []


def test_engine_helper_merges_scanner_with_static(monkeypatch):
    """_get_crypto_universe: scanner output ∪ static list, scanner-first, deduped.

    The static list is the user's hand-curated favorites (NEAR/RNDR/ATOM
    that earn most of the P&L). The scanner adds newly-listed runners on
    top. We never want the scanner to *drop* a favorite just because it
    fell out of the top-N this week.
    """
    from types import SimpleNamespace
    from bot.engine import TradingEngine

    eng = TradingEngine.__new__(TradingEngine)
    eng.config = SimpleNamespace(settings={
        "crypto": {
            "enabled": True,
            "symbols": ["BTC-USD", "ETH-USD", "NEAR-USD", "FAVORITE-USD"],
            "dynamic_universe": {"enabled": True, "limit": 5},
        }
    })

    # Scanner returns runners not in the static list, plus one overlap.
    monkeypatch.setattr(
        "bot.data.crypto_scanner.top_volume_symbols",
        lambda limit=50: ["BTC-USD", "WLD-USD", "DRIFT-USD", "JTO-USD", "ETH-USD"],
    )
    out = eng._get_crypto_universe()
    # Scanner-first order, then static-only names appended, no duplicates.
    assert out == ["BTC-USD", "WLD-USD", "DRIFT-USD", "JTO-USD", "ETH-USD",
                   "NEAR-USD", "FAVORITE-USD"]


def test_engine_helper_dynamic_disabled_returns_static(monkeypatch):
    from types import SimpleNamespace
    from bot.engine import TradingEngine

    eng = TradingEngine.__new__(TradingEngine)
    eng.config = SimpleNamespace(settings={
        "crypto": {
            "enabled": True,
            "symbols": ["BTC-USD", "ETH-USD"],
            "dynamic_universe": {"enabled": False},
        }
    })

    # Scanner should NEVER be called when disabled — assert it isn't.
    monkeypatch.setattr(
        "bot.data.crypto_scanner.top_volume_symbols",
        lambda limit=50: (_ for _ in ()).throw(AssertionError("must not call scanner")),
    )
    assert eng._get_crypto_universe() == ["BTC-USD", "ETH-USD"]


def test_engine_helper_scanner_failure_falls_back_to_static(monkeypatch):
    from types import SimpleNamespace
    from bot.engine import TradingEngine

    eng = TradingEngine.__new__(TradingEngine)
    eng.config = SimpleNamespace(settings={
        "crypto": {
            "enabled": True,
            "symbols": ["BTC-USD", "ETH-USD", "NEAR-USD"],
            "dynamic_universe": {"enabled": True, "limit": 50},
        }
    })

    # Scanner returns empty (network failure with no cache).
    monkeypatch.setattr(
        "bot.data.crypto_scanner.top_volume_symbols",
        lambda limit=50: [],
    )
    assert eng._get_crypto_universe() == ["BTC-USD", "ETH-USD", "NEAR-USD"]


def test_engine_helper_scanner_raises_falls_back_to_static(monkeypatch):
    from types import SimpleNamespace
    from bot.engine import TradingEngine

    eng = TradingEngine.__new__(TradingEngine)
    eng.config = SimpleNamespace(settings={
        "crypto": {
            "enabled": True,
            "symbols": ["BTC-USD"],
            "dynamic_universe": {"enabled": True, "limit": 50},
        }
    })

    def boom(limit=50):
        raise RuntimeError("CoinGecko went sideways")

    monkeypatch.setattr("bot.data.crypto_scanner.top_volume_symbols", boom)
    # Must not propagate the exception — fall back to the static list.
    assert eng._get_crypto_universe() == ["BTC-USD"]


def test_malformed_row_skipped(monkeypatch):
    # Bad rows: missing symbol, non-string symbol, empty symbol.
    rows = [
        {"id": "no-symbol-field"},
        {"symbol": None},
        {"symbol": ""},
        _mk_row("btc"),
        _mk_row("eth"),
    ]
    monkeypatch.setattr(crypto_scanner, "_fetch_from_coingecko", lambda limit: rows)
    out = crypto_scanner.top_volume_symbols(limit=10)
    assert out == ["BTC-USD", "ETH-USD"]
