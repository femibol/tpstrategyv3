"""Repo-level guardrail: every strategy's buy/short signal dict MUST include
a `score` key.

This test is the proactive answer to the 2026-06-02 trading-day-loss bleed.

Three QUALITY GATE bugs ran in parallel that day:
  - low_float_catalyst (caught only after SNBR was killed, fixed PR #189)
  - crypto_runner (defensive fix shipped alongside PR #189)
  - rvol_scalp (caught only after NU/IBIT scalps died all session, fixed PR #191)

A loose `grep '"score"' bot/strategies/*.py` audit gave false negatives —
rvol_scalp had `"score"` in scan_result (for the dashboard) but NOT in the
signal dict returned to the engine. The QUALITY GATE at engine.py:7990 reads
`signal.get("score", 0)`, defaults to 0, and silently skips every entry
post-approval.

A subsequent AST audit found that 10 buy-signal sites across 8 strategies
had the same bug (daily_trend_rider, mean_reversion, pairs_trading [x2],
prebreakout, premarket_gap, rvol_momentum [x2], smc_forever, vwap). All
fixed in the same sweep.

This test pins the invariant at the source level: walk every strategy
module's AST, find every dict literal that has `"action": "buy"` (or
`"short"`), and assert it has a `"score"` key. A new strategy author who
ships a signal without `score` gets a unit-test failure, not a silent
runtime skip days later.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

STRATEGIES_DIR = pathlib.Path(__file__).parent.parent / "bot" / "strategies"
EXCLUDE = {"__init__.py", "base.py"}
ENTRY_ACTIONS = {"buy", "short"}


def _find_signal_dicts(src: str) -> list[tuple[int, list[str], str]]:
    """Return (lineno, keys, action_value) for every dict literal whose
    `action` key is one of ENTRY_ACTIONS."""
    tree = ast.parse(src)
    out: list[tuple[int, list[str], str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
        if "action" not in keys:
            continue
        for k, v in zip(node.keys, node.values):
            if (
                isinstance(k, ast.Constant)
                and k.value == "action"
                and isinstance(v, ast.Constant)
                and v.value in ENTRY_ACTIONS
            ):
                out.append((node.lineno, keys, v.value))
    return out


def _strategy_files() -> list[pathlib.Path]:
    return sorted(
        p for p in STRATEGIES_DIR.glob("*.py") if p.name not in EXCLUDE
    )


def test_every_strategy_signal_dict_includes_score():
    """Walk every strategy's AST, find every entry-action signal dict,
    assert it has a `score` key. Catches the SNBR/NU/IBIT bug class at
    code-review time — no live trading day required to surface it."""
    missing: list[tuple[str, int, str]] = []
    files_with_signals = 0
    for path in _strategy_files():
        src = path.read_text()
        sigs = _find_signal_dicts(src)
        if sigs:
            files_with_signals += 1
        for lineno, keys, action in sigs:
            if "score" not in keys:
                missing.append((path.name, lineno, action))

    assert files_with_signals > 0, (
        "Test couldn't find any strategy signal dicts to inspect — has the "
        "strategies directory moved, or the dict-shape conventions changed? "
        "Either way, this guardrail is silently dead."
    )

    assert not missing, (
        "Signal dicts missing `score` key — every entry signal MUST set score "
        "or the engine's QUALITY GATE (engine.py:7990) defaults to 0 and kills "
        "the entry post-approval. Sites:\n"
        + "\n".join(f"  - {f}:{ln} (action='{act}')" for f, ln, act in missing)
        + "\n\nFix: add `\"score\": <int 0-100>` to the signal dict. Use "
        "`int(score)` if the strategy has its own additive score, "
        "`max(50, int(round(confidence * 100)))` otherwise."
    )


def test_score_value_is_int_or_int_expression():
    """Score must be a numeric type — int(...) or max(...) over ints.
    Defensive against a future commit that sets `"score": "high"` (string)
    or `"score": None`, which would break the engine's `score < min_entry_score`
    comparison."""
    bad: list[tuple[str, int, str]] = []
    for path in _strategy_files():
        src = path.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "action" not in keys or "score" not in keys:
                continue
            # Find the score value node
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "score":
                    # Accept: ast.Call (e.g. int(...), max(...), round(...))
                    # or ast.Constant with int value, or ast.Attribute/Name
                    # (variable reference — we trust runtime). Reject obvious
                    # wrongs: ast.Constant str / None.
                    if isinstance(v, ast.Constant) and not isinstance(
                        v.value, (int, float)
                    ):
                        bad.append((path.name, k.lineno, repr(v.value)))
    assert not bad, (
        "Found `score` values that are not numeric:\n"
        + "\n".join(f"  - {f}:{ln} value={val}" for f, ln, val in bad)
    )
