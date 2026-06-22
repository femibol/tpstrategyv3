"""Position persistence must survive container restarts AND surface
failures when it can't.

2026-06-22 incident: bot bought ARB-USD and JUP-USD via TradersPost.
Both positions lived in memory but never reached disk because
`_persist_positions()` swallowed serialization failures via
`log.debug` — invisible in normal INFO-level logs. Next container
restart booted with 0 positions despite having 2 open trades, and the
dashboard correctly showed 0 (the bug was the persistence, not the
display).

These tests pin three guarantees:
  1. A normally-shaped position dict round-trips through persist + load.
  2. A position dict containing a non-JSON-serializable value (the
     pandas Timestamp / numpy float pattern that caused the live
     failure) does NOT block the entire write — other clean positions
     still persist. The bad position's failure logs at WARNING (visible).
  3. A disk-write failure (read-only fs, full disk, etc.) logs at
     WARNING — operator sees it, not just a debug-level whisper.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _make_engine_stub(tmp_path, positions=None):
    """Minimal TradingEngine that exposes just `_persist_positions`,
    `_positions_lock`, and a `self.positions` dict pointing at a tmp
    data dir. Doesn't import IBKR / Polygon / strategies."""
    from bot.engine import TradingEngine
    import threading

    # Build the temp data dir layout TradingEngine._persist_positions expects:
    # `os.path.dirname(os.path.dirname(__file__))/data/positions_state.json`
    # The function builds the path relative to bot/engine.py's location, not
    # our tmp dir — so we patch the os.path.dirname chain at write time.
    stub = SimpleNamespace(
        positions=dict(positions or {}),
        _positions_lock=threading.RLock(),
        _last_persisted_hash=None,
    )
    stub._persist_positions = TradingEngine._persist_positions.__get__(stub)
    stub._load_persisted_positions = TradingEngine._load_persisted_positions.__get__(stub)
    return stub


def _redirect_positions_state(tmp_path, monkeypatch):
    """Make TradingEngine._persist_positions write to tmp_path instead
    of the real /app/data/positions_state.json. The function builds the
    path from __file__ — easiest hack is to patch os.path.join."""
    target = tmp_path / "positions_state.json"
    orig_join = __import__("os").path.join

    def fake_join(*parts):
        # Detect the call we want to redirect:
        # join(dirname(dirname(__file__)), "data", "positions_state.json")
        if parts and parts[-1] == "positions_state.json":
            return str(target)
        return orig_join(*parts)

    monkeypatch.setattr("os.path.join", fake_join)
    return target


def test_clean_position_round_trips(tmp_path, monkeypatch):
    """Sanity: a normally-shaped position writes and reloads cleanly."""
    target = _redirect_positions_state(tmp_path, monkeypatch)

    pos = {
        "symbol": "ARB-USD",
        "direction": "long",
        "quantity": 5000,
        "entry_price": 0.0855,
        "entry_time": datetime(2026, 6, 22, 14, 30, tzinfo=timezone.utc),
        "stop_loss": 0.080,
        "take_profit": 0.090,
        "strategy": "mean_reversion",
    }
    stub = _make_engine_stub(tmp_path, positions={"ARB-USD": pos})
    stub._persist_positions()

    assert target.exists()
    saved = json.loads(target.read_text())
    assert "ARB-USD" in saved
    assert saved["ARB-USD"]["symbol"] == "ARB-USD"
    assert saved["ARB-USD"]["quantity"] == 5000


def test_bad_position_does_not_kill_other_writes(tmp_path, monkeypatch, caplog):
    """The bug. One position with a non-serializable value used to take
    down the entire persist call — including any other clean positions
    in the same dict. Now: bad position logs WARNING, clean positions
    still write."""
    target = _redirect_positions_state(tmp_path, monkeypatch)

    class _Unserializable:
        """Mimics the failure mode of a pandas Timestamp or numpy float
        that doesn't survive JSON serialization with the default coercion."""
        def __str__(self):
            # Make the serializer's default=str fallback also fail
            raise TypeError("cannot stringify")

    clean = {
        "symbol": "ARB-USD",
        "direction": "long",
        "quantity": 5000,
        "entry_price": 0.0855,
        "entry_time": datetime(2026, 6, 22, 14, 30, tzinfo=timezone.utc),
    }
    bad = {
        "symbol": "JUP-USD",
        "direction": "long",
        "quantity": 5000,
        "entry_price": 0.2099,
        "entry_time": datetime(2026, 6, 22, 14, 36, tzinfo=timezone.utc),
        # The poison value:
        "metadata": _Unserializable(),
    }
    stub = _make_engine_stub(
        tmp_path, positions={"ARB-USD": clean, "JUP-USD": bad}
    )

    with caplog.at_level(logging.WARNING, logger="trading_bot.engine"):
        stub._persist_positions()

    # ARB must have made it to disk even though JUP is poison
    assert target.exists()
    saved = json.loads(target.read_text())
    assert "ARB-USD" in saved
    # PERSIST WARNING must surface (the whole point of the change —
    # previously this was a silent log.debug)
    assert any("PERSIST" in rec.message for rec in caplog.records), (
        "PERSIST failure logged at debug only — operator can't see it. "
        "The 2026-06-22 incident root cause."
    )


def test_disk_write_failure_logs_warning(tmp_path, monkeypatch, caplog):
    """Read-only fs, full disk, perm denied — operator MUST see this.
    The legacy log.debug hid the failure entirely; ARB-USD and JUP-USD
    silently never landed for 4 hours on 2026-06-22."""
    target = _redirect_positions_state(tmp_path, monkeypatch)

    pos = {
        "symbol": "ARB-USD",
        "direction": "long",
        "quantity": 5000,
        "entry_price": 0.0855,
        "entry_time": datetime(2026, 6, 22, 14, 30, tzinfo=timezone.utc),
    }
    stub = _make_engine_stub(tmp_path, positions={"ARB-USD": pos})

    # Force os.replace to fail (simulates fs error after temp write)
    with patch("os.replace", side_effect=OSError("read-only file system")):
        with caplog.at_level(logging.WARNING, logger="trading_bot.engine"):
            stub._persist_positions()

    assert any(
        "PERSIST" in rec.message and "failed" in rec.message
        for rec in caplog.records
    ), "fs failure must log at WARNING so operator notices"


def test_anti_regression_no_log_debug_for_persist_errors():
    """Anti-regression: the legacy `log.debug(f'Position persistence
    error: {e}')` line was the hidden trapdoor. Don't let a future
    refactor reintroduce silent-fail behavior."""
    src = (Path(__file__).parent.parent / "bot" / "engine.py").read_text()
    assert "log.debug(f\"Position persistence error" not in src, (
        "bot/engine.py: the silent-fail log.debug line is back. The "
        "2026-06-22 ARB/JUP persistence-loss bug used this to hide "
        "JSON serialization errors. Use log.warning instead."
    )


def test_source_has_warning_level_log_for_persist_failures():
    """Pin the new behavior: persist failures log at WARNING."""
    src = (Path(__file__).parent.parent / "bot" / "engine.py").read_text()
    # Two log.warning sites: per-symbol serialize failure, outer write failure
    persist_block = src[src.find("def _persist_positions"): src.find("def _load_persisted_positions")]
    assert persist_block.count("log.warning") >= 2, (
        "_persist_positions must surface failures at WARNING level — "
        "both per-symbol serialization and outer write."
    )
