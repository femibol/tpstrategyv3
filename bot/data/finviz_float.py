"""Finviz float lookup with persistent 24h JSON cache.

Used by the low_float_catalyst strategy to gate entries on shares-outstanding.
Finviz exposes float/shs-outstanding on the public fundamentals page; we scrape
the snapshot table once per symbol per day and cache it in
data/finviz_float_cache.json so subsequent calls are free.

Returns None on any failure — callers must treat None as "unknown, fall open"
to avoid blocking entries when Finviz is rate-limiting or rolled out a
markup change.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from threading import Lock

from bot.utils.logger import get_logger

log = get_logger("data.finviz_float")

_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "finviz_float_cache.json"
)
_CACHE_TTL_SEC = 24 * 3600
_REQ_TIMEOUT = 6
_UA = "Mozilla/5.0 (compatible; trading-bot/1.0)"
_lock = Lock()
_mem_cache: dict | None = None


def _load_cache() -> dict:
    global _mem_cache
    if _mem_cache is not None:
        return _mem_cache
    try:
        with open(_CACHE_PATH) as f:
            _mem_cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _mem_cache = {}
    return _mem_cache


def _save_cache():
    if _mem_cache is None:
        return
    tmp = _CACHE_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(_mem_cache, f)
        os.replace(tmp, _CACHE_PATH)
    except OSError as e:
        log.debug(f"finviz cache write failed: {e}")


def _parse_float_str(s: str) -> float | None:
    """'45.20M' → 45.2, '1.20B' → 1200.0, '-' → None."""
    if not s or s == "-":
        return None
    s = s.strip().upper().replace(",", "")
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([KMB]?)$", s)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    return val * {"": 1e-6, "K": 1e-3, "M": 1.0, "B": 1e3}[unit]


def _fetch_from_finviz(symbol: str) -> float | None:
    url = f"https://finviz.com/quote.ashx?t={symbol.upper()}&p=d"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_REQ_TIMEOUT) as r:
            html = r.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        log.debug(f"finviz fetch failed for {symbol}: {e}")
        return None

    # Finviz snapshot markup (2026):
    #   <div class="snapshot-td-label">Shs Float</div></td>
    #   <td ...><div class="snapshot-td-content"><b>2.89M</b></div></td>
    # Prefer "Shs Float" (tradable) over "Shs Outstand" (includes locked).
    pattern = (
        r'snapshot-td-label">{label}</div></td>\s*'
        r'<td[^>]*>\s*<div[^>]*snapshot-td-content[^>]*>\s*<b>([^<]+)</b>'
    )
    m = re.search(pattern.format(label=r"Shs\s*Float"), html, re.IGNORECASE)
    if not m:
        m = re.search(pattern.format(label=r"Shs\s*Outstand"), html, re.IGNORECASE)
    if not m:
        return None
    return _parse_float_str(m.group(1))


def get_float(symbol: str) -> float | None:
    """Return shares-float in millions, or None if unknown.

    Cached for 24h on disk so the same symbol doesn't re-hit Finviz on
    every scan cycle. Concurrent callers are serialised by a module lock
    to avoid duplicate fetches when the strategy scans 100+ symbols.
    """
    if not symbol:
        return None
    sym = symbol.upper().strip()
    now = time.time()

    with _lock:
        cache = _load_cache()
        entry = cache.get(sym)
        if entry and (now - entry.get("ts", 0)) < _CACHE_TTL_SEC:
            return entry.get("float_m")

        value = _fetch_from_finviz(sym)
        cache[sym] = {"float_m": value, "ts": now}
        _save_cache()
        return value
