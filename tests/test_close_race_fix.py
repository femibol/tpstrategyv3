"""Source-level fix for the close-race that produced FBYD triple-records.

PR #216 patched the symptom at the persist layer (dedup on
write + boot cleanup). This PR fixes the actual race in
`_close_position` / `_partial_close`.

Race pattern (check-then-act not atomic under GIL despite single-
statement reads):

  Thread A: `symbol in self._closing_in_progress` → False
  Thread B: `symbol in self._closing_in_progress` → False (still!)
  Thread A: `self._closing_in_progress.add(symbol)`
  Thread B: `self._closing_in_progress.add(symbol)` (no-op)
  ...both proceed to _close_position_inner → both append to
  trade_history → FBYD-class duplicate.

Fix: wrap the check-and-add in `self._positions_lock`. The lock
is released BEFORE calling _close_position_inner so the inner
path can re-acquire it safely (no deadlock, _positions_lock is
non-reentrant).

These tests pin:
  1. Source uses the lock around the check+add (anti-regression)
  2. Concurrent calls produce exactly one inner invocation
  3. Lock is released before calling _inner (no deadlock)
  4. The legacy unlocked pattern is absent
"""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock


def test_source_wraps_close_guard_in_lock():
    """Anti-regression: bot/engine.py must hold _positions_lock when
    doing the check-and-add on _closing_in_progress for _close_position."""
    src = (Path(__file__).parent.parent / "bot" / "engine.py").read_text()
    # Find the _close_position method
    method_idx = src.find("def _close_position(self,")
    next_method_idx = src.find("def _close_position_inner", method_idx)
    snippet = src[method_idx:next_method_idx]
    # The check + add must be inside `with self._positions_lock:`
    assert "with self._positions_lock:" in snippet, (
        "_close_position guard isn't holding _positions_lock — "
        "check-then-act race is back"
    )
    # The add must come AFTER the lock acquisition
    lock_idx = snippet.find("with self._positions_lock:")
    add_idx = snippet.find("self._closing_in_progress.add", lock_idx)
    assert add_idx > 0, "lock present but add not inside it"


def test_source_wraps_partial_close_guard_in_lock():
    """Same fix needed in _partial_close — it has the same race shape."""
    src = (Path(__file__).parent.parent / "bot" / "engine.py").read_text()
    method_idx = src.find("def _partial_close(self,")
    next_method_idx = src.find("def _partial_close_inner", method_idx)
    snippet = src[method_idx:next_method_idx]
    assert "with self._positions_lock:" in snippet, (
        "_partial_close guard isn't holding _positions_lock"
    )


def test_concurrent_close_calls_produce_one_inner_invocation():
    """End-to-end behavior: 5 threads simultaneously calling close,
    inner must only run once."""
    from bot.engine import TradingEngine

    eng = TradingEngine.__new__(TradingEngine)
    eng._positions_lock = threading.Lock()
    eng._closing_in_progress = set()
    eng._recently_closed = {}
    eng._exit_cooldown_secs = 5

    inner_calls = []
    def fake_inner(symbol, reason_type, reason_msg):
        # Hold for a moment so concurrent calls have time to race
        threading.Event().wait(0.02)
        inner_calls.append((symbol, reason_type))
    eng._close_position_inner = fake_inner

    # Bypass the cooldown gate (no prior close)
    barrier = threading.Barrier(5)
    def runner():
        barrier.wait()
        eng._close_position("ICP-USD", "stop_loss", "test")
    threads = [threading.Thread(target=runner) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(inner_calls) == 1, (
        f"expected 1 inner call, got {len(inner_calls)} — the check-then-act "
        f"race is back. Calls: {inner_calls}"
    )


def test_lock_released_before_inner_call():
    """The lock must NOT be held when _close_position_inner runs.
    Otherwise _inner's own `with self._positions_lock` (e.g. for
    self.positions.pop) deadlocks. Verify by having the inner check
    that it can re-acquire the lock."""
    from bot.engine import TradingEngine

    eng = TradingEngine.__new__(TradingEngine)
    eng._positions_lock = threading.Lock()
    eng._closing_in_progress = set()
    eng._recently_closed = {}
    eng._exit_cooldown_secs = 5

    lock_was_held_in_inner = []
    def fake_inner(symbol, reason_type, reason_msg):
        # Try to acquire with a timeout — if the outer lock is still
        # held by the same thread, this would block forever (non-reentrant
        # lock). With a timeout, we get False → deadlock detected.
        acquired = eng._positions_lock.acquire(timeout=0.5)
        lock_was_held_in_inner.append(not acquired)
        if acquired:
            eng._positions_lock.release()
    eng._close_position_inner = fake_inner

    eng._close_position("ICP-USD", "stop_loss", "test")
    assert lock_was_held_in_inner == [False], (
        "outer lock leaked into _close_position_inner — _inner can't "
        "re-acquire _positions_lock for its own pop/mutate work"
    )


def test_legacy_unlocked_pattern_absent():
    """Belt-and-suspenders: the old unlocked check-then-add pattern
    must not be present anywhere in the file."""
    src = (Path(__file__).parent.parent / "bot" / "engine.py").read_text()
    # The exact old shape: `if symbol in self._closing_in_progress:` NOT
    # preceded (within 3 lines) by `with self._positions_lock:`
    lines = src.split("\n")
    for i, line in enumerate(lines):
        if "if symbol in self._closing_in_progress:" in line:
            # Look back 5 lines for the lock
            preceding = "\n".join(lines[max(0, i - 5): i])
            assert "with self._positions_lock:" in preceding, (
                f"line {i + 1}: `if symbol in self._closing_in_progress` "
                f"without preceding `with self._positions_lock:` — "
                f"unguarded check-then-act!"
            )
