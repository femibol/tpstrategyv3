"""Server-side bracket default for equity entries — engine.py:6281 path.

Live incident 2026-05-30 (DELL): the bot's in-memory stop on a momentum
equity entry was the only line of defense. When the IBKR worker wedged
(180s timeouts on every call after an unrelated session disruption), the
bot could NOT place a broker-side stop via `_update_broker_stop` retry
loop, AND could not execute a manual close via the dashboard API —
both went through the same wedged worker. Position sat uncovered for
hours.

Fix: default `use_server_bracket: True` for any equity signal that has
the required fields (price + stop + target). The engine routes those as
LIMIT @ price×1.02, IBKR attaches a bracket child order pair, and the
stop+TP live on IBKR's side from the moment of entry. Even if the bot
or gateway dies entirely, the stop fires on the broker. Master toggle:
`risk.use_server_bracket_equity_default` in settings.yaml (default true).

These tests pin five guarantees:
  1. Shipped default is true (config-level)
  2. Equity signal w/o explicit field → bracket enabled
  3. Crypto signal w/o explicit field → bracket disabled (TradersPost path)
  4. Explicit override at signal level beats the default (either direction)
  5. Master flag false → default reverts to old behavior for equity too
"""
from __future__ import annotations

from types import SimpleNamespace

import yaml


SETTINGS_PATH = "config/settings.yaml"


def _resolve_bracket_default(symbol, signal, *, is_crypto_fn,
                              risk_config, current_price, stop_loss_price,
                              take_profit_price):
    """Mirror of the inline logic at engine.py:~6281 — kept identical here
    so a regression in either trips the tests."""
    bracket_default_enabled = risk_config.get(
        "use_server_bracket_equity_default", True
    )
    if bracket_default_enabled and not is_crypto_fn(symbol):
        default_bracket = True
    else:
        default_bracket = False
    return bool(
        signal.get("use_server_bracket", default_bracket)
        and current_price and current_price > 0
        and stop_loss_price and take_profit_price
    )


def _eq(sym): return not any(sym.endswith(s) for s in ("-USD", "-USDT", "-BTC", "-ETH"))
def _crypto(sym): return not _eq(sym)


def test_shipped_default_in_settings_is_true():
    with open(SETTINGS_PATH) as f:
        cfg = yaml.safe_load(f)
    val = cfg.get("risk", {}).get("use_server_bracket_equity_default")
    assert val is True, (
        f"Shipped default must be True (see module docstring — DELL incident). "
        f"Got: {val!r}. If you flip it, also reverse the test."
    )


def test_equity_signal_defaults_to_bracket():
    out = _resolve_bracket_default(
        "DELL", signal={},
        is_crypto_fn=_crypto,
        risk_config={"use_server_bracket_equity_default": True},
        current_price=420.0, stop_loss_price=416.0, take_profit_price=435.0,
    )
    assert out is True


def test_crypto_signal_defaults_to_no_bracket():
    out = _resolve_bracket_default(
        "BTC-USD", signal={},
        is_crypto_fn=_crypto,
        risk_config={"use_server_bracket_equity_default": True},
        current_price=70000, stop_loss_price=68000, take_profit_price=74000,
    )
    assert out is False


def test_equity_signal_can_override_false():
    out = _resolve_bracket_default(
        "DELL", signal={"use_server_bracket": False},
        is_crypto_fn=_crypto,
        risk_config={"use_server_bracket_equity_default": True},
        current_price=420.0, stop_loss_price=416.0, take_profit_price=435.0,
    )
    assert out is False


def test_crypto_signal_can_override_true():
    out = _resolve_bracket_default(
        "ETH-USD", signal={"use_server_bracket": True},
        is_crypto_fn=_crypto,
        risk_config={"use_server_bracket_equity_default": True},
        current_price=2500, stop_loss_price=2400, take_profit_price=2700,
    )
    assert out is True


def test_master_flag_false_disables_default_for_equity():
    out = _resolve_bracket_default(
        "DELL", signal={},
        is_crypto_fn=_crypto,
        risk_config={"use_server_bracket_equity_default": False},
        current_price=420.0, stop_loss_price=416.0, take_profit_price=435.0,
    )
    assert out is False


def test_missing_price_or_stop_disables_bracket_even_when_default_true():
    """A bracket requires LIMIT entry + stop + target; if any are missing
    the engine must NOT try to attach a bracket (would error at IBKR)."""
    base = {
        "is_crypto_fn": _crypto,
        "risk_config": {"use_server_bracket_equity_default": True},
        "current_price": 420.0,
    }
    # No stop loss
    assert _resolve_bracket_default("DELL", {}, stop_loss_price=0, take_profit_price=435.0, **base) is False
    # No take profit
    assert _resolve_bracket_default("DELL", {}, stop_loss_price=416.0, take_profit_price=0, **base) is False
    # Zero price
    assert _resolve_bracket_default(
        "DELL", {}, is_crypto_fn=_crypto,
        risk_config={"use_server_bracket_equity_default": True},
        current_price=0, stop_loss_price=416.0, take_profit_price=435.0,
    ) is False


def test_engine_inline_code_matches_helper():
    """Lock the test helper to the engine code so a refactor doesn't drift."""
    with open("bot/engine.py") as f:
        src = f.read()
    # Required substrings — if these regress the test logic and engine code
    # have diverged and we need to update both in lockstep.
    assert "use_server_bracket_equity_default" in src
    assert 'signal.get("use_server_bracket", default_bracket)' in src
    assert "_is_crypto_symbol(symbol)" in src
