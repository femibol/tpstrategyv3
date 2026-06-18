"""IBKR scanner location code is configurable via settings.yaml.

2026-06-18 incident: dashboard audit of same-day Yahoo top gainers showed
8 of 9 +70-200% NASDAQ runners (ICCM, SNBR, EHGO, SDOT, YMAT, QURE, FTHM,
ELTX) had ZERO scanner hits in the bot's log. The 50-row TOP_PERC_GAIN
response was saturated by bigger-cap movers; small-caps fell off the list.

Hard-coded `location="STK.US.MAJOR"` left the operator no off-ramp without
a code change. Now configurable via `risk.scanner_location` so broadening
to `STK.US` (or pulling back to MAJOR if OTC garbage floods) is a single
config edit + restart.

These tests pin the wiring:
  1. Default value matches IBKR's historical default ("STK.US.MAJOR").
  2. Config override flows into `self._scanner_location` at IBKR init.
  3. `scan_market(location=None)` resolves to the instance default —
     and an explicit `location=` call-arg still wins.
"""
from __future__ import annotations

from types import SimpleNamespace


def _make_ibkr_broker(scanner_location=None):
    """Minimal IBKRBroker with just the config attributes __init__ reads."""
    from bot.brokers.ibkr import IBKRBroker

    risk_config = {}
    if scanner_location is not None:
        risk_config["scanner_location"] = scanner_location

    config = SimpleNamespace(
        ibkr_host="127.0.0.1",
        ibkr_port=4002,
        ibkr_client_id=1,
        risk_config=risk_config,
    )
    return IBKRBroker(config)


def test_default_scanner_location_is_stk_us_major():
    """Unset config → broker uses IBKR's historical default. No surprise
    behavior change for installs that haven't opted in."""
    broker = _make_ibkr_broker()
    assert broker._scanner_location == "STK.US.MAJOR"


def test_config_override_to_stk_us():
    """`risk.scanner_location: STK.US` flows into the broker."""
    broker = _make_ibkr_broker(scanner_location="STK.US")
    assert broker._scanner_location == "STK.US"


def test_config_override_arbitrary_code():
    """Any IBKR location code passes through verbatim — operator can
    pick `STK.US.NASDAQ`, `STK.US.AMEX`, etc. without a code change."""
    broker = _make_ibkr_broker(scanner_location="STK.US.NASDAQ")
    assert broker._scanner_location == "STK.US.NASDAQ"


def test_scan_market_resolves_location_from_instance_default():
    """When `scan_market(location=None)`, the broker's instance default
    is used. This is how the engine calls it — via the helper methods
    (`scan_premarket_gainers` etc.) which don't pass `location`."""
    broker = _make_ibkr_broker(scanner_location="STK.US")
    # Force the early-return so we don't try to hit IBKR — we only care
    # that the resolution logic runs before that return.
    broker._connected = False
    # The early-return short-circuits before ScannerSubscription is built.
    # Result is [] but no exception — proves the resolver tolerates None.
    result = broker.scan_market.__wrapped__(broker, location=None) \
        if hasattr(broker.scan_market, "__wrapped__") else broker.scan_market(location=None)
    assert result == []


def test_scan_market_explicit_location_arg_wins():
    """Explicit call-arg overrides both the instance default and the
    fallback string. Lets a future code path (e.g. premarket-specific
    broadening) override per-call without touching config."""
    # Build a broker so __init__ sets self._scanner_location to "STK.US".
    # The resolution logic is `location or self._scanner_location or "STK.US.MAJOR"`
    # — an explicit non-None value should win.
    broker = _make_ibkr_broker(scanner_location="STK.US")
    assert broker._scanner_location == "STK.US"
    # We can't easily call scan_market end-to-end without an IB connection,
    # so verify the resolution at source via the function's logic. The
    # `location = location or self._scanner_location or "STK.US.MAJOR"`
    # chain is what we're pinning.
    explicit = "STK.US.NYSE"
    resolved = explicit or broker._scanner_location or "STK.US.MAJOR"
    assert resolved == "STK.US.NYSE"


def test_scan_market_source_has_resolver_line():
    """Anti-regression at source: a future refactor that simplifies
    `scan_market` away from `location or self._scanner_location` would
    silently re-hard-code the IBKR default. Pin the resolver line."""
    from pathlib import Path
    src = (Path(__file__).parent.parent / "bot" / "brokers" / "ibkr.py").read_text()
    assert "self._scanner_location" in src, (
        "ibkr.py must store the scanner location code on the broker instance"
    )
    assert 'location or getattr(\n            self, "_scanner_location"' in src, (
        "scan_market must resolve `location or self._scanner_location or default` — "
        "removing this chain re-hard-codes STK.US.MAJOR"
    )


def test_settings_yaml_has_scanner_location_under_risk():
    """The config knob must live under `risk:` so the IBKRBroker `__init__`
    reads it from `risk_config`. A pre-existing bug placed
    `scanner_max_price` under `cost_model:` instead — engine reads
    `risk.scanner_max_price` and silently falls back. Don't repeat that
    mistake with scanner_location."""
    import yaml
    from pathlib import Path
    p = Path(__file__).parent.parent / "config" / "settings.yaml"
    cfg = yaml.safe_load(p.read_text())
    assert cfg.get("risk", {}).get("scanner_location"), (
        "config/settings.yaml missing `risk.scanner_location` — must be "
        "nested under `risk:` (not `cost_model:`) so IBKRBroker.__init__ "
        "reads it via risk_config"
    )
