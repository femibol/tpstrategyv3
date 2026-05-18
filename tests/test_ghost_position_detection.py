"""Ghost-position detector — regression for the 2026-05-18 tz bug.

`_signal_log_net_qty` filters signal_log entries by `t >= since_dt`. Both
sides must live in the same timezone for the comparison to be sound.

Bug we ship a regression for: the writer was emitting naive local time
(`datetime.now().isoformat()`), and the reader was labeling those naive
timestamps as UTC. On a bot running EDT (-04:00), a buy logged at local
05:17 was read as 05:17 UTC. The persisted position's `entry_time` was
tz-aware EDT, which became 09:17 UTC. So `t (05:17) < since_dt (09:17)`
filtered the buy out, net came back 0, and three freshly-opened legitimate
positions (ATOM/ICP/RNDR) were flagged GHOST and skipped on restart —
becoming real orphans on TradersPost that the bot would never manage.

The fix has two halves; both are exercised here:
1. New writes use `datetime.now(timezone.utc).isoformat()` (tz-aware).
2. The reader localizes legacy *naive* timestamps with `self.tz` before
   converting to UTC.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytz

from bot.engine import TradingEngine


@pytest.fixture
def engine_stub(tmp_path, monkeypatch):
    """Bare-bones TradingEngine instance with only what the helper needs.

    The helper reads `data/signal_log.json` relative to the engine file,
    so we monkeypatch the path resolution to point at a tmp file.
    """
    import os as _os_real
    _real_join = _os_real.path.join
    eng = TradingEngine.__new__(TradingEngine)
    eng.tz = pytz.timezone("America/New_York")
    signal_file = tmp_path / "signal_log.json"
    monkeypatch.setattr(
        "bot.engine.os.path.join",
        lambda *parts: str(signal_file) if parts and parts[-1] == "signal_log.json"
        else _real_join(*parts),
    )
    return eng, signal_file


def _write_signals(path, signals):
    path.write_text(json.dumps(signals))


def test_legacy_naive_buy_at_entry_time_counts(engine_stub):
    """Reproduces the 2026-05-18 incident exactly.

    Position entry_time: tz-aware EDT 05:17:47.026150-04:00 (= 09:17 UTC).
    Signal log buy: naive '05:17:47.019508' (legacy writer used
    datetime.now() with no tz). The old code mislabeled this naive value
    as UTC, making t < since_dt true and net = 0 → GHOST.
    The fix localizes naive as bot's tz (EDT), giving t = 09:17 UTC,
    which passes t >= since_dt and counts the buy correctly.
    """
    eng, signal_file = engine_stub
    entry_time = datetime.fromisoformat("2026-05-18T05:17:47.026150-04:00")
    _write_signals(signal_file, [{
        "time": "2026-05-18T05:17:47.019508",  # naive — legacy format
        "symbol": "ATOM-USD",
        "success": True,
        "status_code": 200,
        "quantity": 878.89141,
        "tp_action": "buy",
    }])
    net = eng._signal_log_net_qty("ATOM-USD", since_dt=entry_time)
    # The buy at entry_time MUST be counted — otherwise we ghost the position.
    assert net == pytest.approx(878.89141)


def test_tz_aware_utc_buy_at_entry_time_counts(engine_stub):
    """New writer format (post-fix) is tz-aware UTC. Verify the reader
    still counts the buy when entry_time and signal time match."""
    eng, signal_file = engine_stub
    entry_time = datetime.fromisoformat("2026-05-18T05:17:47.026150-04:00")
    # Same instant, but expressed as tz-aware UTC like the new writer emits.
    signal_time = entry_time.astimezone(timezone.utc).isoformat()
    _write_signals(signal_file, [{
        "time": signal_time,
        "symbol": "ATOM-USD",
        "success": True,
        "status_code": 200,
        "quantity": 100.0,
        "tp_action": "buy",
    }])
    net = eng._signal_log_net_qty("ATOM-USD", since_dt=entry_time)
    assert net == pytest.approx(100.0)


def test_buy_and_exit_net_to_zero_is_real_ghost(engine_stub):
    """When the position was truly closed, net should be 0 → ghost flag
    is correct. Buy + matching exit, both after entry_time."""
    eng, signal_file = engine_stub
    entry_time = datetime.fromisoformat("2026-05-18T05:00:00-04:00")
    _write_signals(signal_file, [
        {
            "time": "2026-05-18T05:10:00",  # naive local, after entry
            "symbol": "SUI-USD",
            "success": True, "status_code": 200,
            "quantity": 1000.0, "tp_action": "buy",
        },
        {
            "time": "2026-05-18T05:20:00",  # naive local, after entry
            "symbol": "SUI-USD",
            "success": True, "status_code": 200,
            "quantity": 1000.0, "tp_action": "exit",
        },
    ])
    net = eng._signal_log_net_qty("SUI-USD", since_dt=entry_time)
    assert net == 0.0


def test_signal_before_entry_time_is_excluded(engine_stub):
    """A buy that happened BEFORE entry_time (e.g. a prior round-trip on
    the same symbol) must not be counted toward the current position.

    The 5-second grace window is small enough to exclude a buy from hours
    earlier."""
    eng, signal_file = engine_stub
    entry_time = datetime.fromisoformat("2026-05-18T05:17:47-04:00")
    _write_signals(signal_file, [
        {
            "time": "2026-05-18T03:00:00",  # naive local, BEFORE entry
            "symbol": "ICP-USD",
            "success": True, "status_code": 200,
            "quantity": 500.0, "tp_action": "buy",
        },
        {
            "time": "2026-05-18T05:17:47.019508",  # at entry (~ms before)
            "symbol": "ICP-USD",
            "success": True, "status_code": 200,
            "quantity": 718.69353, "tp_action": "buy",
        },
    ])
    net = eng._signal_log_net_qty("ICP-USD", since_dt=entry_time)
    # Only the at-entry buy counts; the earlier round-trip is excluded.
    assert net == pytest.approx(718.69353)


def test_grace_window_catches_signal_logged_just_before_entry_time(engine_stub):
    """Real failure mode behind the 2026-05-18 incident: signal_log time
    is captured at HTTP-200 receipt; engine sets entry_time a few ms later.
    Without the grace, the at-entry buy is filtered out and the position
    is misflagged as a ghost on next restart."""
    eng, signal_file = engine_stub
    # Position entry_time set ~7ms after the signal was logged.
    entry_time = datetime.fromisoformat("2026-05-18T05:17:47.026150-04:00")
    _write_signals(signal_file, [{
        "time": "2026-05-18T05:17:47.019508",  # 6.6ms before entry_time
        "symbol": "ATOM-USD",
        "success": True, "status_code": 200,
        "quantity": 878.89141,
        "tp_action": "buy",
    }])
    net = eng._signal_log_net_qty("ATOM-USD", since_dt=entry_time)
    assert net == pytest.approx(878.89141)


def test_rejected_signals_ignored(engine_stub):
    """Rejected (success=False) and 4xx signals must NOT count toward net."""
    eng, signal_file = engine_stub
    entry_time = datetime.fromisoformat("2026-05-18T05:00:00-04:00")
    _write_signals(signal_file, [
        {
            "time": "2026-05-18T05:10:00",
            "symbol": "BTC-USD",
            "success": False, "status_code": 400,  # rejected
            "quantity": 0.5, "tp_action": "buy",
        },
        {
            "time": "2026-05-18T05:15:00",
            "symbol": "BTC-USD",
            "success": True, "status_code": 500,  # 5xx == bad
            "quantity": 0.5, "tp_action": "buy",
        },
    ])
    net = eng._signal_log_net_qty("BTC-USD", since_dt=entry_time)
    assert net == 0.0
