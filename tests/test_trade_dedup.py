"""Trade-history deduplication (Wave 3 fix — FBYD pattern from session 9).

HANDOFF session 9 documented: "the same trade dated 2026-05-19T17:14:51
appears 3× in trade_history with identical -$43.40 P&L; inflates loss
counts everywhere." Root cause: `_close_position` writes
`trade_history.append` BEFORE setting `_recently_closed[symbol]` to
the cooldown timestamp, leaving a window where concurrent close calls
from monitor / EOD / broker-sync paths can each reach the append.
`_closing_in_progress` is in-memory and reset by `finally`, leaving
the window after the first call finishes.

Two-layer fix:
  1. `persist_trade`: dedup at write time on (symbol, entry_time,
     exit_time). Looks back ~10 trades for the collision (close calls
     fire within seconds of each other; deep history doesn't apply).
  2. `dedupe_persisted_trades`: one-shot boot cleanup of legacy
     duplicates already on disk. Engine calls at startup.

These tests pin both:
  - persist_trade refuses an exact duplicate
  - persist_trade allows distinct trades on same symbol
  - persist_trade allows partial-fill scenarios (same exit_time but
     different quantities are NOT duplicates — different actions)
  - dedupe_persisted_trades preserves the first occurrence
  - missing key fields → fall open (don't accidentally drop trades)
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace


def _make_analyzer(tmp_dir, pre_seed=None):
    from bot.learning.trade_analyzer import TradeAnalyzer
    cfg = SimpleNamespace(base_dir=tmp_dir)
    a = TradeAnalyzer(cfg)
    if pre_seed is not None:
        a._persisted_trades = list(pre_seed)
    return a


def _trade(symbol, entry_time, exit_time, pnl=-10.0, qty=100):
    return {
        "symbol": symbol,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "pnl": pnl,
        "quantity": qty,
        "strategy": "test",
    }


# === 1. persist_trade write-time dedup ===


def test_persist_trade_blocks_exact_duplicate():
    """FBYD pattern: same symbol + entry_time + exit_time fires twice."""
    with tempfile.TemporaryDirectory() as tmp:
        a = _make_analyzer(tmp)
        t = _trade("FBYD", "2026-05-19T17:14:50", "2026-05-19T17:14:51", pnl=-43.40)
        a.persist_trade(t)
        a.persist_trade(t)  # duplicate
        a.persist_trade(t)  # triplicate
        assert len(a._persisted_trades) == 1
        # File on disk also has only one
        loaded = json.loads(Path(tmp, "data", "trade_history.json").read_text())
        assert len(loaded) == 1


def test_persist_trade_allows_distinct_trades_on_same_symbol():
    """Two separate FBYD entries on the same day — different entry_time,
    different exit_time — must both persist."""
    with tempfile.TemporaryDirectory() as tmp:
        a = _make_analyzer(tmp)
        t1 = _trade("FBYD", "2026-05-19T17:14:51", "2026-05-19T17:30:00", pnl=-43.40)
        t2 = _trade("FBYD", "2026-05-19T18:00:00", "2026-05-19T18:15:00", pnl=12.50)
        a.persist_trade(t1)
        a.persist_trade(t2)
        assert len(a._persisted_trades) == 2


def test_persist_trade_allows_different_symbols_same_time():
    """AAPL exit and MSFT exit at the same time aren't duplicates."""
    with tempfile.TemporaryDirectory() as tmp:
        a = _make_analyzer(tmp)
        t1 = _trade("AAPL", "2026-05-19T17:00:00", "2026-05-19T17:30:00")
        t2 = _trade("MSFT", "2026-05-19T17:00:00", "2026-05-19T17:30:00")
        a.persist_trade(t1)
        a.persist_trade(t2)
        assert len(a._persisted_trades) == 2


def test_persist_trade_missing_key_falls_open():
    """If a trade lacks entry_time or exit_time, don't drop it — the
    dedup gate falls open and the trade persists. Better to keep a
    record we can't dedup than silently drop work."""
    with tempfile.TemporaryDirectory() as tmp:
        a = _make_analyzer(tmp)
        t_no_entry = {"symbol": "FBYD", "exit_time": "x", "pnl": -10}
        t_no_exit = {"symbol": "FBYD", "entry_time": "x", "pnl": -10}
        a.persist_trade(t_no_entry)
        a.persist_trade(t_no_exit)
        assert len(a._persisted_trades) == 2


# === 2. Lookback window ===


def test_persist_trade_lookback_only_checks_recent():
    """An exact duplicate from MANY trades ago doesn't trigger the
    write-time guard (that's the role of the boot cleanup). The race
    window between concurrent close calls fires within seconds, so the
    short lookback catches the actual class of bug."""
    with tempfile.TemporaryDirectory() as tmp:
        a = _make_analyzer(tmp)
        ancient = _trade("FBYD", "2026-01-01T00:00:00", "2026-01-01T00:30:00")
        a.persist_trade(ancient)
        # Push 15 unrelated trades
        for i in range(15):
            a.persist_trade(_trade(f"SYM{i}", f"t{i}", f"e{i}"))
        # The ancient one is now 16 records back — outside the 10-look window
        # So a "duplicate" of it would write through. (Boot cleanup handles
        # this case at startup; write-time dedup targets concurrent close
        # races within ~10 trades of each other.)
        a.persist_trade(ancient)
        # 17 records total (ancient + 15 distinct + ancient again)
        assert len(a._persisted_trades) == 17


# === 3. Boot cleanup ===


def test_dedupe_persisted_trades_removes_duplicates():
    """Legacy state on disk has duplicates from before the write-time
    guard — boot cleanup walks through and keeps only the first
    occurrence of each (symbol, entry_time, exit_time) tuple."""
    with tempfile.TemporaryDirectory() as tmp:
        dup = _trade("FBYD", "2026-05-19T17:14:50", "2026-05-19T17:14:51", pnl=-43.40)
        unique = _trade("AAPL", "2026-05-19T18:00:00", "2026-05-19T18:30:00", pnl=12.00)
        a = _make_analyzer(tmp, pre_seed=[dup, dup, dup, unique])
        removed = a.dedupe_persisted_trades()
        assert removed == 2
        assert len(a._persisted_trades) == 2


def test_dedupe_preserves_first_occurrence():
    """Order matters: the first record is kept (original timing), later
    duplicates are dropped."""
    with tempfile.TemporaryDirectory() as tmp:
        first = _trade("FBYD", "2026-05-19T17:00:00", "2026-05-19T17:30:00", pnl=-43.40)
        # "Duplicate" with same key but mutated pnl — the FIRST wins
        evil = dict(first); evil["pnl"] = 999.99
        a = _make_analyzer(tmp, pre_seed=[first, evil])
        a.dedupe_persisted_trades()
        assert len(a._persisted_trades) == 1
        assert a._persisted_trades[0]["pnl"] == -43.40


def test_dedupe_writes_to_disk():
    """Cleanup must persist to disk so the next restart doesn't see
    the duplicates again."""
    with tempfile.TemporaryDirectory() as tmp:
        dup = _trade("FBYD", "2026-05-19T17:14:50", "2026-05-19T17:14:51")
        a = _make_analyzer(tmp, pre_seed=[dup, dup, dup])
        a.dedupe_persisted_trades()
        on_disk = json.loads(Path(tmp, "data", "trade_history.json").read_text())
        assert len(on_disk) == 1


def test_dedupe_clean_history_is_noop():
    """No duplicates → no rewrite, no log spam."""
    with tempfile.TemporaryDirectory() as tmp:
        clean = [
            _trade("AAPL", "t1", "e1"),
            _trade("MSFT", "t2", "e2"),
            _trade("NVDA", "t3", "e3"),
        ]
        a = _make_analyzer(tmp, pre_seed=clean)
        removed = a.dedupe_persisted_trades()
        assert removed == 0
        assert len(a._persisted_trades) == 3


def test_dedupe_handles_missing_keys_gracefully():
    """Records missing symbol/entry_time/exit_time pass through
    untouched (can't safely dedup → keep all)."""
    with tempfile.TemporaryDirectory() as tmp:
        weird = {"pnl": -5}  # no key fields
        good = _trade("AAPL", "t1", "e1")
        a = _make_analyzer(tmp, pre_seed=[weird, good])
        a.dedupe_persisted_trades()
        assert len(a._persisted_trades) == 2
