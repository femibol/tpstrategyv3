"""Slippage tracker persistence (Wave 4 follow-up to PR #214).

PR #214 shipped the per-strategy slippage dampener with the tracker
held in-memory only. A daily-restart cadence on the VPS meant the
20-sample rolling buffer rarely accumulated the >5-sample minimum
that activates the dampener — so on a fresh boot, the dampener
silently fell open to 1.0 even when the strategy's recent fills had
been bleeding.

Fix: persist the buffer to `data/slippage_tracker.json` after every
`_record_slippage` call. Load on engine startup. Gitignored.

These tests pin:
  1. _record_slippage writes the file
  2. _load_slippage_state recovers state across instances
  3. Missing file = empty buffer (fresh install)
  4. Corrupt file falls open to empty (don't block startup)
  5. Buffer cap preserved across save/load (20 samples max)
  6. .gitignore covers the slippage file
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace


def _make_engine(tmp_dir):
    """Minimal engine stub with just the slippage methods + cfg."""
    from bot.engine import TradingEngine
    eng = TradingEngine.__new__(TradingEngine)
    eng.config = SimpleNamespace(base_dir=Path(tmp_dir))
    return eng


# === 1. Persistence round-trip ===


def test_record_slippage_writes_file():
    with tempfile.TemporaryDirectory() as tmp:
        eng = _make_engine(tmp)
        eng._record_slippage("momentum", 0.005)
        eng._record_slippage("momentum", 0.004)
        path = Path(tmp) / "data" / "slippage_tracker.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert "momentum" in data
        assert data["momentum"] == [0.005, 0.004]


def test_load_recovers_state_across_engine_instances():
    """Simulates: engine A records, container restarts, engine B reads."""
    with tempfile.TemporaryDirectory() as tmp:
        eng_a = _make_engine(tmp)
        for v in [0.008, 0.007, 0.009, 0.006, 0.007, 0.008]:
            eng_a._record_slippage("momentum", v)
        # Fresh engine, same disk
        eng_b = _make_engine(tmp)
        mult = eng_b._compute_slippage_mult("momentum")
        # 6 samples with avg ~0.0075 → dampener should fire (mult < 1)
        assert mult < 1.0
        # Before this PR, _strategy_slippage would have been empty on
        # eng_b → mult would have been 1.0


def test_multiple_strategies_persisted_independently():
    with tempfile.TemporaryDirectory() as tmp:
        eng = _make_engine(tmp)
        for v in [0.001, 0.001, 0.001, 0.001, 0.001, 0.001]:
            eng._record_slippage("clean_strat", v)
        for v in [0.010, 0.008, 0.009, 0.007, 0.008, 0.010]:
            eng._record_slippage("dirty_strat", v)

        eng2 = _make_engine(tmp)
        # clean_strat sub-threshold avg → no dampening
        assert eng2._compute_slippage_mult("clean_strat") == 1.0
        # dirty_strat above-floor avg → dampener fires
        assert eng2._compute_slippage_mult("dirty_strat") == 0.5


# === 2. Failure modes ===


def test_missing_file_returns_empty_buffer():
    """Day 0 of fresh install: no file, no samples, no crash."""
    with tempfile.TemporaryDirectory() as tmp:
        eng = _make_engine(tmp)
        store = eng._load_slippage_state()
        assert len(store) == 0
        # And the compute helper still works (returns neutral)
        assert eng._compute_slippage_mult("anything") == 1.0


def test_corrupt_file_falls_open_to_empty():
    """Malformed JSON must not block startup. Log + start fresh."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "data").mkdir()
        (Path(tmp) / "data" / "slippage_tracker.json").write_text(
            "not: valid: json: [[[[[]"
        )
        eng = _make_engine(tmp)
        store = eng._load_slippage_state()
        assert len(store) == 0  # falls open


def test_persist_falls_open_on_write_error(monkeypatch):
    """Disk full / read-only fs → tracker keeps running in-memory."""
    with tempfile.TemporaryDirectory() as tmp:
        eng = _make_engine(tmp)
        # Force a write error: monkeypatch the persist method to blow up.
        # _record_slippage must swallow it and keep the in-memory buffer.
        def boom():
            raise OSError("disk full")
        monkeypatch.setattr(eng, "_persist_slippage_state", boom)
        # Should NOT raise
        try:
            eng._record_slippage("momentum", 0.005)
        except OSError:
            # If the engine forwards the raise, that's the bug we're guarding
            # against. Wrap it so the assertion below explains the failure.
            assert False, "_record_slippage must swallow persist errors"
        # In-memory buffer still updated
        assert len(eng._strategy_slippage.get("momentum", [])) == 1


# === 3. Buffer cap ===


def test_buffer_cap_preserved_across_save_load():
    """Rolling deque max=20 must be enforced post-load. A maxlen-less
    deque would silently accumulate forever on a long-running bot."""
    with tempfile.TemporaryDirectory() as tmp:
        eng = _make_engine(tmp)
        for i in range(30):  # 30 samples, deque should keep last 20
            eng._record_slippage("strat", 0.001 + i * 0.0001)
        eng2 = _make_engine(tmp)
        store = eng2._load_slippage_state()
        # Last 20 of the 30 recorded
        assert len(store["strat"]) == 20
        # Add one more — oldest must evict, NOT accumulate to 21
        eng2._record_slippage("strat", 0.005)
        assert len(eng2._strategy_slippage["strat"]) == 20


# === 4. Gitignore coverage ===


def test_gitignore_covers_slippage_file():
    """The persisted state is per-host runtime — don't track it."""
    ignore = (Path(__file__).parent.parent / ".gitignore").read_text()
    assert "data/slippage_tracker.json" in ignore
