"""CoinGecko top-volume crypto scanner with 24h JSON cache.

Returns the top-N crypto symbols by 24h trading volume, formatted as
``TICKER-USD`` so the engine can drop them straight into the existing
crypto universe without further translation.

The static ``crypto.symbols`` list in ``config/settings.yaml`` is hand
maintained and rotated by trade-review every few weeks — by definition
it misses any name that breaks out *after* the last review (WLD, DRIFT,
JTO, ONDO, ENA, W, …). This scanner replaces that list dynamically when
``crypto.dynamic_universe.enabled`` is true; the static list stays as
the fallback if the API call fails so a CoinGecko outage never disarms
the crypto sleeve.

API: ``https://api.coingecko.com/api/v3/coins/markets`` — free public,
no auth, ~30 req/min rate limit, well inside our once-a-day call.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from threading import Lock

from bot.utils.logger import get_logger

log = get_logger("data.crypto_scanner")

_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "crypto_scanner_cache.json"
)
_CACHE_TTL_SEC = 24 * 3600
_REQ_TIMEOUT = 10
_UA = "Mozilla/5.0 (compatible; trading-bot/1.0)"
_API_URL = "https://api.coingecko.com/api/v3/coins/markets"

# Stablecoins and wrapped/staked derivatives — never run on their own, get
# excluded from the universe regardless of where they rank by volume.
# Stablecoins lead 24h volume by a huge margin (USDT alone > BTC most days)
# so without this filter the top-N is half stablecoins.
_BLACKLIST = {
    # Stablecoins
    "USDT", "USDC", "DAI", "FDUSD", "USDE", "FRAX", "TUSD", "PYUSD",
    "USDD", "USDS", "EURC", "EURT", "BUSD", "GUSD", "LUSD", "USDP",
    "USD1", "USDF", "RLUSD", "FUSD",
    # Wrapped / liquid-staked derivatives — track the underlying, no edge
    "WBTC", "CBBTC", "TBTC", "SBTC", "LBTC",
    "WETH", "STETH", "WSTETH", "WEETH", "RETH", "CBETH", "EZETH",
    "WSOL", "JITOSOL", "JUPSOL", "BNSOL", "MSOL",
    # Native non-tradeable wrappers
    "WBNB", "WMATIC", "WAVAX", "WTRX",
}

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
    os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
    tmp = _CACHE_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(_mem_cache, f)
        os.replace(tmp, _CACHE_PATH)
    except OSError as e:
        log.debug(f"crypto scanner cache write failed: {e}")


def _fetch_from_coingecko(limit: int) -> list[dict] | None:
    """Hit CoinGecko's free /coins/markets endpoint, ordered by 24h volume.

    Returns the raw JSON list on success, None on any network / parse
    failure. Caller falls back to cache or static list.
    """
    # per_page caps at 250; bump a bit above the requested limit so the
    # blacklist filter doesn't starve us below the target.
    per_page = min(250, max(limit * 2, 50))
    url = f"{_API_URL}?vs_currency=usd&order=volume_desc&per_page={per_page}&page=1"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_REQ_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, ConnectionError,
            json.JSONDecodeError) as e:
        log.warning(f"CoinGecko fetch failed: {e}")
        return None
    if not isinstance(data, list):
        log.warning(f"CoinGecko returned non-list response: {type(data).__name__}")
        return None
    return data


def _filter_and_format(rows: list[dict], limit: int) -> list[str]:
    """Convert CoinGecko rows → unique ``TICKER-USD`` list, blacklist-filtered."""
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        sym_raw = row.get("symbol")
        if not isinstance(sym_raw, str):
            continue
        sym = sym_raw.strip().upper()
        if not sym or sym in _BLACKLIST:
            continue
        ticker = f"{sym}-USD"
        if ticker in seen:
            continue
        seen.add(ticker)
        out.append(ticker)
        if len(out) >= limit:
            break
    return out


def top_volume_symbols(limit: int = 50, force_refresh: bool = False) -> list[str]:
    """Return the top ``limit`` crypto symbols by 24h volume.

    Cached for 24h on disk. Returns an empty list (not None) on total
    failure so callers can ``len(...) == 0`` check and fall back to the
    static universe.
    """
    if limit <= 0:
        return []
    now = time.time()

    with _lock:
        cache = _load_cache()
        entry = cache.get("top_volume")
        if (not force_refresh and entry
                and (now - entry.get("ts", 0)) < _CACHE_TTL_SEC
                and isinstance(entry.get("symbols"), list)):
            return list(entry["symbols"])[:limit]

        rows = _fetch_from_coingecko(limit)
        if rows is None:
            # Network failure — return stale cache if present, else empty.
            if entry and isinstance(entry.get("symbols"), list):
                log.info("CoinGecko fetch failed — returning stale cache")
                return list(entry["symbols"])[:limit]
            return []

        symbols = _filter_and_format(rows, limit)
        if not symbols:
            log.warning("CoinGecko returned no usable symbols after filtering")
            if entry and isinstance(entry.get("symbols"), list):
                return list(entry["symbols"])[:limit]
            return []

        cache["top_volume"] = {"symbols": symbols, "ts": now}
        _save_cache()
        log.info(
            f"CoinGecko: refreshed top-{len(symbols)} crypto universe "
            f"({', '.join(symbols[:5])}, …)"
        )
        return symbols
