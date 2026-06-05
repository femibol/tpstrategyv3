"""Dashboard /api/strategies/activity endpoint — feeds the new live
strategy activity widget on the dashboard (PR #199).

Returns per-strategy fired/filled counts both cumulative-since-boot and
since-midnight-ET, plus an `alert_silent_fill` flag for the FIRED-BUT-
NEVER-FILLED case (signals firing with no conversions = silent failure).

These tests pin three guarantees:
  1. Shape: every strategy in engine.strategies appears in the response
     with all required fields
  2. Today's delta is computed against a per-day baseline (the first
     call on a new day re-baselines from current counters)
  3. alert_silent_fill fires when today's fires >= 3 and today's fills
     = 0 — and only then
"""
from __future__ import annotations

import datetime as dt
import os
from types import SimpleNamespace

# Dashboard refuses to start without DASHBOARD_SECRET_KEY (fail-closed
# auth, security-critical). Set a test value for the duration of these
# tests; the secret value doesn't matter — we hit the dashboard via the
# Flask test client which bypasses the @auth.login_required path.
os.environ.setdefault("DASHBOARD_SECRET_KEY", "test-secret-not-used")


def _auth_headers():
    """HTTP Basic header matching the test secret. Dashboard's
    before_request hook compares against env DASHBOARD_SECRET_KEY."""
    import base64
    creds = base64.b64encode(b"user:test-secret-not-used").decode()
    return {"Authorization": "Basic " + creds}


class _StubStrategy:
    def __init__(self, fired=0, filled=0, enabled=True):
        self.signals_generated = fired
        self.trades_taken = filled
        self.enabled = enabled


def _make_dashboard(strategies):
    """Build a Dashboard instance with a stub engine + Flask test client."""
    from bot.dashboard.app import Dashboard

    engine = SimpleNamespace(
        strategies=strategies,
        running=True,
        positions={},
        trade_history=[],
        equity_curve=[],
        daily_stats=[],
        notifier=None,
        trade_analyzer=None,
        regime_detector=None,
        hedging_manager=None,
        get_status=lambda: {},
        config=None,
    )
    config = SimpleNamespace(
        mode="paper",
        dashboard_host="127.0.0.1",
        dashboard_port=5000,
        dashboard_secret_key=None,
        risk_config={},
        settings={},
    )
    dash = Dashboard(engine, config)
    dash.app.config["TESTING"] = True
    return dash.app.test_client(), engine


def test_endpoint_returns_one_row_per_strategy():
    client, _ = _make_dashboard({
        "momentum": _StubStrategy(fired=10, filled=3),
        "mean_reversion": _StubStrategy(fired=22, filled=8),
        "low_float_catalyst": _StubStrategy(fired=0, filled=0),
    })
    r = client.get("/api/strategies/activity", headers=_auth_headers())
    assert r.status_code == 200
    data = r.get_json()
    assert "strategies" in data
    names = {row["strategy"] for row in data["strategies"]}
    assert names == {"momentum", "mean_reversion", "low_float_catalyst"}
    for row in data["strategies"]:
        for k in ("strategy", "enabled", "fired_total", "filled_total",
                  "fired_today", "filled_today", "conversion_pct",
                  "alert_silent_fill"):
            assert k in row, f"missing field {k} in {row}"


def test_first_call_baselines_zero_for_today():
    """On first call, today's counts equal cumulative (no prior baseline
    to subtract). After that, the baseline is recorded for the day so
    subsequent calls show the DELTA."""
    strat = _StubStrategy(fired=10, filled=3)
    client, engine = _make_dashboard({"momentum": strat})

    # First call: baseline set to 10/3. Today's counts are 0/0 (no
    # activity since baseline was just set).
    r1 = client.get("/api/strategies/activity", headers=_auth_headers()).get_json()
    mom = next(s for s in r1["strategies"] if s["strategy"] == "momentum")
    # Baseline is recorded AT this call, so fired_today should be 0
    assert mom["fired_today"] == 0
    assert mom["filled_today"] == 0
    assert mom["fired_total"] == 10
    assert mom["filled_total"] == 3

    # Strategy fires 4 more signals, 1 more fill on the same day
    strat.signals_generated = 14
    strat.trades_taken = 4
    r2 = client.get("/api/strategies/activity", headers=_auth_headers()).get_json()
    mom = next(s for s in r2["strategies"] if s["strategy"] == "momentum")
    # Today's delta = 14-10=4, 4-3=1
    assert mom["fired_today"] == 4
    assert mom["filled_today"] == 1
    assert mom["fired_total"] == 14


def test_alert_silent_fill_fires_when_3plus_signals_no_fills():
    """The whole point of this widget: surface FIRED-BUT-NEVER-FILLED.
    Threshold of 3 avoids flagging noise from a single race-condition
    signal that lost to a faster strategy on the same symbol."""
    strat = _StubStrategy(fired=0, filled=0)
    client, _ = _make_dashboard({"low_float_catalyst": strat})

    # Baseline call
    client.get("/api/strategies/activity", headers=_auth_headers())

    # Strategy fires 5 signals, zero fills (QUALITY GATE pattern)
    strat.signals_generated = 5
    strat.trades_taken = 0
    r = client.get("/api/strategies/activity", headers=_auth_headers()).get_json()
    row = r["strategies"][0]
    assert row["alert_silent_fill"] is True


def test_alert_silent_fill_does_not_fire_below_threshold():
    """One or two signals with no fills is noise — could be race, could
    be a single bad price tick. Don't pull the alarm."""
    strat = _StubStrategy(fired=0, filled=0)
    client, _ = _make_dashboard({"momentum": strat})
    client.get("/api/strategies/activity", headers=_auth_headers())  # baseline

    strat.signals_generated = 2
    strat.trades_taken = 0
    r = client.get("/api/strategies/activity", headers=_auth_headers()).get_json()
    row = r["strategies"][0]
    assert row["alert_silent_fill"] is False


def test_alert_clears_when_at_least_one_fill_happens():
    strat = _StubStrategy(fired=0, filled=0)
    client, _ = _make_dashboard({"momentum": strat})
    client.get("/api/strategies/activity", headers=_auth_headers())  # baseline

    strat.signals_generated = 5
    strat.trades_taken = 1
    r = client.get("/api/strategies/activity", headers=_auth_headers()).get_json()
    row = r["strategies"][0]
    assert row["alert_silent_fill"] is False


def test_rows_sorted_by_fired_today_desc():
    """Noisiest strategy at top — operator's eye lands on the most active
    one (and the most-active-with-zero-fills is exactly what we want
    to spot)."""
    client, engine = _make_dashboard({
        "quiet": _StubStrategy(fired=0, filled=0),
        "noisy": _StubStrategy(fired=0, filled=0),
        "medium": _StubStrategy(fired=0, filled=0),
    })
    client.get("/api/strategies/activity", headers=_auth_headers())  # baseline
    # Generate fresh signals on the same day after baseline
    engine.strategies["noisy"].signals_generated = 20
    engine.strategies["medium"].signals_generated = 7
    engine.strategies["quiet"].signals_generated = 1
    r = client.get("/api/strategies/activity", headers=_auth_headers()).get_json()
    names_in_order = [row["strategy"] for row in r["strategies"]]
    assert names_in_order == ["noisy", "medium", "quiet"]


def test_disabled_strategies_still_included():
    """A disabled strategy must still appear so the operator can see it's
    off (and not silently inactive due to a bug)."""
    client, _ = _make_dashboard({
        "options_momentum": _StubStrategy(fired=0, filled=0, enabled=False),
    })
    r = client.get("/api/strategies/activity", headers=_auth_headers()).get_json()
    row = r["strategies"][0]
    assert row["enabled"] is False
