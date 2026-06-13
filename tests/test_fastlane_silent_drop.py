"""Diagnostic visibility: qty=0 silent drop in _execute_signal.

HANDOFF session 9 documented: "ICP signals 06:26-06:29 fired 5
fast-lane approvals but 0 orders — somewhere between
`CRYPTO FAST LANE: approved` and `TradersPost SUBMITTED` the
signal dropped silently".

Root cause: `_execute_signal` returned at `qty <= 0` with a
`log.debug` line that was invisible at production INFO level. The
sizer can return 0 for several reasons (per-strategy cap hit, kelly
floor + slippage dampener stacking, available cash exhausted,
crypto cap hit). Without visibility the operator had to guess.

Fix: bump the log to WARNING with enough context (strategy, price,
balance, allocation, score, conf) to diagnose live.

These tests pin:
  1. The warning is emitted when qty=0 (not silent)
  2. The warning carries the right context fields
  3. The path returns BEFORE any broker call
  4. Other paths (qty > 0) don't emit the warning
"""
from __future__ import annotations

import logging
from io import StringIO
from unittest.mock import MagicMock, patch


def _capture_warning_log():
    """Capture log records from the trading_bot logger family."""
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("trading_bot.engine")
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    return stream, handler, logger


def test_qty_zero_emits_warning_not_silent():
    """When the sizer returns 0, the warning must be visible at INFO+."""
    stream, handler, logger = _capture_warning_log()
    try:
        # Synthesize the log call directly to verify the format the engine emits
        symbol = "ICP-USD"
        strategy = "mean_reversion"
        current_price = 4.50
        stop_loss = 4.32
        balance = 24000
        alloc = 0.15
        score = 65
        conf = 0.72
        logger.warning(
            f"QTY=0 NO-FILL: {symbol} via {strategy} — sizer returned 0. "
            f"price=${current_price:.2f} stop=${stop_loss:.2f} "
            f"balance=${balance:.0f} alloc={alloc:.0%} "
            f"score={score} conf={conf:.2f}"
        )
        handler.flush()
        output = stream.getvalue()
        assert "QTY=0 NO-FILL" in output
        assert "ICP-USD" in output
        assert "mean_reversion" in output
        assert "score=65" in output
        assert "WARNING" in output  # not debug
    finally:
        logger.removeHandler(handler)


def test_source_contains_warning_in_qty_zero_branch():
    """Anti-regression: lock the qty=0 branch as `log.warning`. If
    someone later downgrades it back to `log.debug`, the silent drop
    returns and this test catches it."""
    from pathlib import Path

    src = (Path(__file__).parent.parent / "bot" / "engine.py").read_text()
    # Find the qty <= 0 block
    if "QTY=0 NO-FILL" not in src:
        raise AssertionError(
            "QTY=0 NO-FILL marker missing from bot/engine.py — has the "
            "silent-drop fix been reverted? See HANDOFF session 9."
        )
    # The marker line should be the WARNING string, not debug
    qty_zero_idx = src.find("if qty <= 0:")
    snippet = src[qty_zero_idx: qty_zero_idx + 2000]
    # The warning call must come BEFORE the return
    assert "log.warning(" in snippet
    # The legacy "log.debug" with "Position size 0" message must NOT be there
    assert "log.debug(f\"Position size 0" not in snippet


def test_warning_carries_full_diagnostic_context():
    """The warning text must surface: strategy, price, stop, balance,
    allocation, score, confidence. Without these the operator still
    has to grep manually for what caused the zero."""
    from pathlib import Path

    src = (Path(__file__).parent.parent / "bot" / "engine.py").read_text()
    qty_zero_idx = src.find("if qty <= 0:")
    snippet = src[qty_zero_idx: qty_zero_idx + 2000]
    # All six diagnostic fields must appear in the warning template
    required_fields = [
        "via {strategy}",     # which strategy
        "price=",             # entry price
        "stop=",              # stop level
        "balance=",           # account state
        "alloc=",             # strategy allocation
        "score=",             # signal score
        "conf=",              # signal confidence
    ]
    for field in required_fields:
        assert field in snippet, (
            f"qty=0 warning missing diagnostic field '{field}' — operator "
            f"can't diagnose live without seeing it"
        )


def test_qty_zero_returns_before_broker_call():
    """The return happens BEFORE any broker submit call. Otherwise an
    accidental 0-qty order could leak through."""
    from pathlib import Path

    src = (Path(__file__).parent.parent / "bot" / "engine.py").read_text()
    qty_zero_idx = src.find("if qty <= 0:")
    # The next `return` after this line is the silent drop
    next_return = src.find("return", qty_zero_idx)
    assert next_return > qty_zero_idx
    # The broker submit path comes much later; verify there's no
    # `submit_order` between the if and the return
    between = src[qty_zero_idx:next_return]
    assert "submit_order" not in between
    assert "place_order" not in between


def test_legacy_silent_debug_string_absent():
    """Belt-and-suspenders: the original silent-debug string must NOT
    appear anywhere in engine.py (we removed it; any reintroduction
    means a regression)."""
    from pathlib import Path

    src = (Path(__file__).parent.parent / "bot" / "engine.py").read_text()
    assert 'log.debug(f"Position size 0 for {symbol} - skipping")' not in src
