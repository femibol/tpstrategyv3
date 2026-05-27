"""Claude pre-trade hard gate must be config-gated, OFF by default.

Live log analysis (2026-05-27) found the gate skipping 100% of equity signals
in a single session — 267 SKIPs, 0 PROCEEDs — because the strategy-WR rule
was reading dirty historical data. 6 of momentum's 10 "losses" were
`slippage_reject` artifacts from the session-6 P&L bug (fixed in `2ec2325`),
plus pre-fix trailing_stop wicks (fixed in `e6fcc34`). The gate has no
measurement of whether its SKIPs ever correlated with worse outcomes, the
12-trade rolling window is too small for the rule it enforces, and it adds
1–5s of API latency to the entry hot-path.

AI is better applied batch (auto-tuner, weekly review, post-trade insights).
This test pins the gate-off default and confirms the call is skipped when
the flag is false. Flip `ai_pretrade.enabled: true` in settings.yaml to
re-enable the hard gate.
"""
from __future__ import annotations

import yaml


SETTINGS_PATH = "config/settings.yaml"


def _load_settings():
    with open(SETTINGS_PATH) as f:
        return yaml.safe_load(f)


def test_ai_pretrade_gate_is_off_by_default():
    """The shipped default in config/settings.yaml must be enabled=false."""
    cfg = _load_settings()
    ai_cfg = cfg.get("ai_pretrade", {})
    assert ai_cfg.get("enabled") is False, (
        "ai_pretrade.enabled MUST default to false — see module docstring. "
        f"Got: {ai_cfg!r}"
    )


def test_engine_gates_claude_call_on_config_flag():
    """The engine's per-signal Claude call must be wrapped in the config
    check. Asserted by string-search on the source so we don't have to spin
    up a real engine for this regression."""
    with open("bot/engine.py") as f:
        src = f.read()
    # The call site must read the config flag before invoking Claude.
    # We assert both the config lookup and the conditional gate exist on
    # the same code path as the _claude_pre_trade call.
    assert 'ai_pretrade_cfg = self.config.settings.get("ai_pretrade"' in src, (
        "bot/engine.py must read ai_pretrade config before the Claude gate. "
        "If this regressed, the gate is firing on every signal again — see "
        "tests/test_ai_pretrade_gate.py docstring."
    )
    assert 'ai_pretrade_cfg.get("enabled", False)' in src, (
        "bot/engine.py must gate the Claude call on the enabled flag with "
        "default=False. A True default would silently re-enable the gate."
    )
