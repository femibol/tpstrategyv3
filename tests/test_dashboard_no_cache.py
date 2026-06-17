"""Dashboard HTML route must not be cached by the browser.

2026-06-17 incident: user kept seeing "No data" on the dashboard for
multiple PRs in a row despite backend APIs returning fresh data. Root
cause was iOS Safari serving the pre-bugfix HTML + inline JS from disk
cache. Even pull-to-refresh wasn't always enough — the user had to
clear Safari website data manually.

Fix: serve the dashboard HTML with Cache-Control: no-store. JSON
endpoints don't need this (Authorization-bearing fetches bypass the
HTTP cache by default), so only the `/` route is changed.

These tests pin the headers at the route handler level via Flask's
test client, so we exercise the actual response Flask produces rather
than just the source string.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_test_client():
    """Build a Dashboard with a fake engine + config + an empty test
    template so we can hit `/` via Flask's test client. Sets
    `DASHBOARD_SECRET_KEY` so the constructor's fail-closed check
    passes."""
    os.environ.setdefault("DASHBOARD_SECRET_KEY", "test-key-for-cache-headers")
    from bot.dashboard.app import Dashboard

    # The Dashboard constructor needs an engine + config. Stub minimally.
    engine = MagicMock()
    engine.running = True
    config = SimpleNamespace(mode="paper")
    dash = Dashboard(engine, config)

    # Override template rendering so we don't depend on the real
    # 130KB template file. The headers are what we're testing, not the
    # body. Flask's render_template is called inside the route — we
    # patch the underlying loader.
    @dash.app.route("/__cache_test_index", endpoint="cache_test_index")
    def _stub():
        from flask import make_response
        resp = make_response("<html></html>")
        # Don't set headers here — we want to ONLY test the production
        # `/` route headers.
        return resp

    return dash.app.test_client(), dash._secret


def test_dashboard_html_route_has_no_store():
    """The `/` route must send Cache-Control: no-store so the browser
    refetches the HTML+inline JS on every visit."""
    client, secret = _make_test_client()
    # Authenticated request (Basic auth via before_request hook)
    import base64
    auth = "Basic " + base64.b64encode(f"admin:{secret}".encode()).decode()
    resp = client.get("/", headers={"Authorization": auth})
    assert resp.status_code == 200
    cc = resp.headers.get("Cache-Control", "")
    assert "no-store" in cc, (
        f"Cache-Control missing 'no-store': {cc!r}. iOS Safari will keep "
        "serving the stale dashboard JS — that's exactly the bug class "
        "this header prevents."
    )


def test_dashboard_html_route_also_sets_pragma_and_expires():
    """Belt-and-suspenders for HTTP/1.0 caches + older browsers."""
    client, secret = _make_test_client()
    import base64
    auth = "Basic " + base64.b64encode(f"admin:{secret}".encode()).decode()
    resp = client.get("/", headers={"Authorization": auth})
    assert resp.headers.get("Pragma") == "no-cache"
    assert resp.headers.get("Expires") == "0"


def test_dashboard_html_no_cache_headers_in_source():
    """Anti-regression at source level — make sure the headers don't
    get removed by a future refactor that simplifies the route."""
    src = (Path(__file__).parent.parent / "bot" / "dashboard" / "app.py").read_text()
    # Locate the `/` route
    idx = src.find('@self.app.route("/")')
    assert idx > 0
    block = src[idx:idx + 1200]
    assert "no-store" in block, "/ route source missing no-store header"
    assert "make_response" in block, "/ route should use make_response to attach headers"


def test_make_response_is_imported():
    """Without make_response we can't set headers on the / route."""
    src = (Path(__file__).parent.parent / "bot" / "dashboard" / "app.py").read_text()
    imports = "\n".join(src.split("\n")[:30])
    assert "make_response" in imports, (
        "bot/dashboard/app.py must `from flask import ..., make_response` "
        "for the / route to attach Cache-Control headers"
    )
