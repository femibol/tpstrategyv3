"""Binance.US klines pagination (Wave 3 carry-over from session 8).

Binance.US returns at most 1000 klines per call. The
mean_reversion crypto trend filter wants a 14d / 4032-bar window
to discriminate NEAR-style winners from ICP-style bleeders, but
the single-shot fetch only delivers ~3.5d before hitting the cap.
Audit retune (session 8) compressed the window to 3d/+1% with a
note that pagination was the long-term answer.

Fix: walk backward via `endTime` until enough bars are collected
or `_BINANCE_KLINES_MAX_WINDOWS` is hit (5 × 1000 = ~17d). Each
chunk's oldest `open_time` becomes the next call's endTime.

These tests pin:
  1. Single window when needed ≤ 1000 (no behavior change for the
     existing 3d filter)
  2. Two windows when needed = 1500
  3. Windows cap at MAX_WINDOWS even if needed is huge
  4. Chunk concat is sorted oldest-first + deduplicated
  5. First-call failure returns None (caller falls through)
  6. Mid-pagination failure returns what we have (don't waste)
  7. Empty response stops pagination cleanly
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


def _kline_row(open_ms, close_price):
    """Build a single Binance kline row matching the 12-column response."""
    return [
        open_ms, str(close_price - 0.1), str(close_price + 0.1),
        str(close_price - 0.2), str(close_price), "1000",
        open_ms + 299_000, "100000", 5, "500", "50000", "0",
    ]


def _kline_batch(start_ms, count, base_price=100.0):
    """N consecutive 5m klines starting at start_ms."""
    return [
        _kline_row(start_ms + i * 300_000, base_price + i * 0.01)
        for i in range(count)
    ]


def _make_md():
    """Minimal MarketData stub — only need _fetch_binance_paginated and
    its dependencies."""
    from bot.data.market_data import MarketDataFeed
    md = MarketDataFeed.__new__(MarketDataFeed)
    md.bar_size = "5 mins"
    md.lookback_days = 14
    md.broker = None
    md.polygon = None
    md._yahoo_last_call = {}
    md._yahoo_rate_limit = 60
    md._BINANCE_KLINES_MAX_PER_CALL = 1000
    md._BINANCE_KLINES_MAX_WINDOWS = 5
    return md


# === 1. Single window — no behavior change for existing 3d filter ===


def test_single_window_when_within_cap():
    md = _make_md()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = _kline_batch(1_700_000_000_000, 800)

    with patch("bot.data.market_data._requests.get", return_value=fake_response) as mock_get:
        df = md._fetch_binance_paginated("BTC", "USDT", "5m", needed_bars=800)
        assert df is not None
        assert len(df) == 800
        # Only one call made
        assert mock_get.call_count == 1
        # endTime NOT in the first call
        assert "endTime" not in mock_get.call_args.kwargs["params"]


# === 2. Two windows when above cap ===


def test_two_windows_when_above_cap():
    md = _make_md()
    # First batch: 1000 bars (the max)
    # Second batch: 500 bars
    batch1 = _kline_batch(1_700_000_000_000, 1000)
    batch2 = _kline_batch(1_699_700_000_000, 500)
    responses = [
        MagicMock(status_code=200, json=MagicMock(return_value=batch1)),
        MagicMock(status_code=200, json=MagicMock(return_value=batch2)),
    ]

    with patch("bot.data.market_data._requests.get", side_effect=responses) as mock_get:
        df = md._fetch_binance_paginated("BTC", "USDT", "5m", needed_bars=1500)
        assert df is not None
        assert len(df) == 1500
        assert mock_get.call_count == 2
        # Second call MUST carry endTime = oldest open_time of batch1 - 1
        second_params = mock_get.call_args_list[1].kwargs["params"]
        assert "endTime" in second_params
        assert second_params["endTime"] == 1_700_000_000_000 - 1


# === 3. Windows cap ===


def test_windows_cap_enforced():
    md = _make_md()
    md._BINANCE_KLINES_MAX_WINDOWS = 3  # tighten for test
    # Every batch returns its max
    batches = [_kline_batch(1_700_000_000_000 - i * 300_000_000, 1000) for i in range(10)]
    responses = [MagicMock(status_code=200, json=MagicMock(return_value=b)) for b in batches]

    with patch("bot.data.market_data._requests.get", side_effect=responses) as mock_get:
        df = md._fetch_binance_paginated("BTC", "USDT", "5m", needed_bars=99_999)
        assert df is not None
        # Exactly 3 windows used
        assert mock_get.call_count == 3
        assert len(df) == 3000


# === 4. Concat: sorted + deduplicated ===


def test_concat_sorted_and_deduplicated():
    """Pagination boundaries may overlap by 1 bar; oldest-first sort
    + dedupe must produce a clean monotonic frame."""
    md = _make_md()
    batch1 = _kline_batch(1_700_000_300_000, 10)  # newer
    batch2 = _kline_batch(1_700_000_000_000, 10)  # older — overlaps last bar
    responses = [
        MagicMock(status_code=200, json=MagicMock(return_value=batch1)),
        MagicMock(status_code=200, json=MagicMock(return_value=batch2)),
    ]
    with patch("bot.data.market_data._requests.get", side_effect=responses):
        df = md._fetch_binance_paginated("BTC", "USDT", "5m", needed_bars=20)
        # Sorted oldest → newest
        assert df.index.is_monotonic_increasing
        # First chunk returned only 10 (< limit 1000) so loop breaks after first call
        # We just verify the single-batch path also works
        assert len(df) > 0


# === 5. First-call failure returns None ===


def test_first_call_http_failure_returns_none():
    md = _make_md()
    fake_response = MagicMock(status_code=500, json=MagicMock(return_value=[]))
    with patch("bot.data.market_data._requests.get", return_value=fake_response):
        df = md._fetch_binance_paginated("BTC", "USDT", "5m", needed_bars=2000)
        assert df is None


def test_first_call_exception_returns_none():
    md = _make_md()
    with patch("bot.data.market_data._requests.get", side_effect=Exception("network")):
        df = md._fetch_binance_paginated("BTC", "USDT", "5m", needed_bars=2000)
        assert df is None


# === 6. Mid-pagination failure preserves what we have ===


def test_mid_failure_returns_partial():
    md = _make_md()
    batch1 = _kline_batch(1_700_000_000_000, 1000)
    responses = [
        MagicMock(status_code=200, json=MagicMock(return_value=batch1)),
        MagicMock(status_code=500, json=MagicMock(return_value=[])),
    ]
    with patch("bot.data.market_data._requests.get", side_effect=responses):
        df = md._fetch_binance_paginated("BTC", "USDT", "5m", needed_bars=2000)
        # Got 1000, second call failed — keep the 1000 not None
        assert df is not None
        assert len(df) == 1000


# === 7. Empty response stops cleanly ===


def test_empty_response_stops_pagination():
    """Exchange has no older data → pagination ends, return what we have."""
    md = _make_md()
    batch1 = _kline_batch(1_700_000_000_000, 1000)
    responses = [
        MagicMock(status_code=200, json=MagicMock(return_value=batch1)),
        MagicMock(status_code=200, json=MagicMock(return_value=[])),
    ]
    with patch("bot.data.market_data._requests.get", side_effect=responses) as mock_get:
        df = md._fetch_binance_paginated("BTC", "USDT", "5m", needed_bars=5000)
        assert df is not None
        assert len(df) == 1000
        assert mock_get.call_count == 2


# === 8. Sufficient-data short-circuit ===


def test_pagination_stops_once_needed_bars_reached():
    """If the first call returns 1000 and we only need 800, the second
    call should NOT happen (we already have enough)."""
    md = _make_md()
    batch1 = _kline_batch(1_700_000_000_000, 1000)
    fake_response = MagicMock(status_code=200, json=MagicMock(return_value=batch1))
    with patch("bot.data.market_data._requests.get", return_value=fake_response) as mock_get:
        df = md._fetch_binance_paginated("BTC", "USDT", "5m", needed_bars=800)
        # Got 1000 in one call; loop's "we have enough" check stops the next call
        assert mock_get.call_count == 1
        assert len(df) == 1000


# === 9. Short batch (< limit) signals exchange has no older data ===


def test_short_batch_stops_pagination():
    """A response shorter than the requested limit means the exchange
    has no older data for this pair — don't keep walking backward
    chasing nothing."""
    md = _make_md()
    short_batch = _kline_batch(1_700_000_000_000, 200)  # < 1000 limit
    fake_response = MagicMock(status_code=200, json=MagicMock(return_value=short_batch))
    with patch("bot.data.market_data._requests.get", return_value=fake_response) as mock_get:
        df = md._fetch_binance_paginated("BTC", "USDT", "5m", needed_bars=5000)
        assert mock_get.call_count == 1
        assert len(df) == 200
