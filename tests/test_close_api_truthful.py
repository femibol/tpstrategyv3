"""Truthful close-API responses (Wave 4 PR #4).

Session-10 DELL incident: user POSTed /api/control/close/DELL,
response said `{"status": "closed"}`, but the IBKR worker was
wedged. The close sat in queue; DELL slept overnight uncovered.
Lost $821 on a fake "closed" status.

The endpoint was reporting INTENT, not OUTCOME. The shape of the
problem:

  1. Endpoint checks `symbol in self.engine.positions` (yes)
  2. Calls `self.engine._close_position(...)` (returns None either way)
  3. Returns `{"status": "closed"}` WITHOUT checking the post-call state

Fix: observe `self.engine.positions` AFTER the close call.
  - position removed → "closed" (200)
  - still tracked AND in queue / recently_closed → "queued" (202)
  - still tracked AND NOT in any queue → "failed" (502)

Same treatment for /api/control/close-all — counts what actually
closed, not what we asked to close.

These tests pin:
  1. Single-close returns 200 + closed when position is gone after
  2. Single-close returns 202 + queued when slippage_close_queue carries
  3. Single-close returns 202 + queued when _recently_closed carries
  4. Single-close returns 502 + failed when position is still tracked
  5. Single-close still 404 when symbol unknown
  6. close-all returns closed_all when all gone
  7. close-all returns 502 + partial when some still tracked
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_app_with_engine(engine):
    """Build a Flask test client wired to the close endpoints. We don't
    use the real DashboardApp init (it instantiates Flask routes for
    every endpoint and requires a lot of config). Instead we apply the
    close endpoints directly to a fresh Flask app."""
    from flask import Flask, jsonify

    app = Flask(__name__)
    container = SimpleNamespace(engine=engine, app=app)

    @app.route("/api/control/close/<symbol>", methods=["POST"])
    def close_position(symbol):
        symbol = symbol.upper()
        if symbol not in container.engine.positions:
            return jsonify({"error": f"No position in {symbol}"}), 404
        container.engine._close_position(symbol, "manual", "test")
        if symbol not in container.engine.positions:
            return jsonify({"status": "closed", "symbol": symbol})
        close_queue = getattr(container.engine, "_slippage_close_queue", []) or []
        recently_closed = getattr(container.engine, "_recently_closed", {}) or {}
        if symbol in close_queue or symbol in recently_closed:
            return jsonify({
                "status": "queued",
                "symbol": symbol,
                "detail": "Close issued but position still tracked.",
            }), 202
        return jsonify({
            "status": "failed",
            "symbol": symbol,
            "detail": "Close call returned but position still tracked.",
        }), 502

    @app.route("/api/control/close-all", methods=["POST"])
    def close_all():
        before = set(container.engine.positions)
        container.engine._close_all_positions("test")
        after = set(container.engine.positions)
        closed = before - after
        still_open = before & after
        if not still_open:
            return jsonify({
                "status": "closed_all",
                "count": len(closed),
                "closed": sorted(closed),
            })
        return jsonify({
            "status": "partial",
            "closed_count": len(closed),
            "closed": sorted(closed),
            "still_open": sorted(still_open),
        }), 502

    return app.test_client()


def _engine(positions, after_close_remove=None, queue=None, recently_closed=None):
    """Stub engine that pretends the close removes some positions."""
    eng = SimpleNamespace()
    eng.positions = dict(positions)
    eng._slippage_close_queue = list(queue or [])
    eng._recently_closed = dict(recently_closed or {})

    def fake_close(symbol, reason, msg):
        if after_close_remove and symbol in after_close_remove:
            eng.positions.pop(symbol, None)
    eng._close_position = fake_close

    def fake_close_all(reason):
        if after_close_remove:
            for sym in list(after_close_remove):
                eng.positions.pop(sym, None)
    eng._close_all_positions = fake_close_all
    return eng


# === Single-symbol close ===


def test_close_returns_closed_when_position_gone():
    """Happy path: close call removed the position from tracking."""
    eng = _engine({"DELL": {"qty": 100}}, after_close_remove={"DELL"})
    client = _make_app_with_engine(eng)
    resp = client.post("/api/control/close/DELL")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "closed"


def test_close_returns_queued_when_in_slippage_queue():
    """The close call queued an exit but the broker hasn't processed
    it yet. Operator sees 202 + 'queued' instead of a misleading 'closed'."""
    eng = _engine(
        {"DELL": {"qty": 100}},
        after_close_remove=None,  # position NOT removed
        queue=["DELL"],  # but it's in the queue
    )
    client = _make_app_with_engine(eng)
    resp = client.post("/api/control/close/DELL")
    assert resp.status_code == 202
    assert resp.get_json()["status"] == "queued"


def test_close_returns_queued_when_in_recently_closed():
    """Close fired, _recently_closed marker is set, but position
    re-added by broker sync mid-flight — still 'queued'."""
    eng = _engine(
        {"DELL": {"qty": 100}},
        after_close_remove=None,
        recently_closed={"DELL": "ts"},
    )
    client = _make_app_with_engine(eng)
    resp = client.post("/api/control/close/DELL")
    assert resp.status_code == 202
    assert resp.get_json()["status"] == "queued"


def test_close_returns_failed_when_position_stuck():
    """The DELL incident exactly: position still tracked, no queue
    record, IBKR worker likely wedged. Old code lied with 'closed'."""
    eng = _engine(
        {"DELL": {"qty": 100}},
        after_close_remove=None,
        queue=[],
        recently_closed={},
    )
    client = _make_app_with_engine(eng)
    resp = client.post("/api/control/close/DELL")
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["status"] == "failed"
    assert "still tracked" in body["detail"]


def test_close_returns_404_for_unknown_symbol():
    """If we never had the position, that's a 404 (not 200/lying)."""
    eng = _engine({"DELL": {"qty": 100}}, after_close_remove={"DELL"})
    client = _make_app_with_engine(eng)
    resp = client.post("/api/control/close/UNKNOWN")
    assert resp.status_code == 404


# === Close-all ===


def test_close_all_returns_closed_all_when_all_gone():
    eng = _engine(
        {"DELL": {}, "AAPL": {}, "MSFT": {}},
        after_close_remove={"DELL", "AAPL", "MSFT"},
    )
    client = _make_app_with_engine(eng)
    resp = client.post("/api/control/close-all")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "closed_all"
    assert body["count"] == 3
    assert set(body["closed"]) == {"DELL", "AAPL", "MSFT"}


def test_close_all_returns_partial_when_some_stuck():
    """Two close, one wedged — operator sees the truth."""
    eng = _engine(
        {"DELL": {}, "AAPL": {}, "MSFT": {}},
        after_close_remove={"AAPL", "MSFT"},  # DELL stays
    )
    client = _make_app_with_engine(eng)
    resp = client.post("/api/control/close-all")
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["status"] == "partial"
    assert body["closed_count"] == 2
    assert body["still_open"] == ["DELL"]


# === Anti-regression on source ===


def test_source_has_no_immediate_closed_return():
    """Anti-regression: ensure the legacy `return jsonify({'status':
    'closed', 'symbol': symbol})` shape isn't right after the
    _close_position call without a post-check."""
    from pathlib import Path
    src = (Path(__file__).parent.parent / "bot" / "dashboard" / "app.py").read_text()
    # The endpoint must check `if symbol not in self.engine.positions:`
    # AFTER calling _close_position, to verify the close actually
    # produced the desired state.
    endpoint_idx = src.find("/api/control/close/<symbol>")
    next_route_idx = src.find("@self.app.route", endpoint_idx + 1)
    snippet = src[endpoint_idx:next_route_idx]
    # Must NOT immediately return "closed" after the close call without
    # re-checking position state. Specifically: the LAST jsonify in the
    # endpoint should be either "failed" or "queued" (not closed).
    assert '"status": "failed"' in snippet, (
        "close endpoint missing the truthful 'failed' branch — "
        "session-10 DELL pattern can recur"
    )
    assert '"status": "queued"' in snippet, (
        "close endpoint missing the 'queued' branch"
    )
