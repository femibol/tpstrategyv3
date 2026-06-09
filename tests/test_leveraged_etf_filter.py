"""Leveraged/inverse ETF CLASS filter — name-based, at scanner discovery.

The ticker blocklist approach kept failing:
  - HIBS traded 2026-06-05 (-$375) while ALREADY ON blocked_symbols
  - TSLL slipped through 2026-06-09 (never listed)
  - New leveraged products launch monthly; a static list can't win

Fix: the IBKR scanner's contractDetails carries each fund's registered
longName ("Direxion Daily TSLA Bull 2X Shares"). Filter the CLASS by
name keywords at discovery — before the symbol ever enters the trading
universe. Zero extra network calls; the data was already in the scanner
response.

Tradeoff accepted: a keyword like " BEAR " could exclude a real company
such as "Bear Creek Mining" from the scan universe. Missing one ordinary
name from a 50-row scan is cheap; letting one 3x ETF through cost -$375
in 5 seconds. The keyword list is config-overridable
(risk.leveraged_etf_name_keywords) if a false positive ever matters.

These tests pin:
  1. Real leveraged/inverse fund names (the ones that actually bled us
     or nearly did) are caught.
  2. Real operating-company names — including the tricky ones (Ultra
     Clean Holdings, Bullfrog AI) — are NOT caught.
  3. Keywords are config-overridable; empty list disables the filter.
  4. Empty/None longName fails open (don't block symbols IBKR didn't
     name — the ticker blocklist still backstops those).
"""
from __future__ import annotations

from types import SimpleNamespace


def _make_broker(keywords=None):
    from bot.brokers.ibkr import IBKRBroker

    risk_config = {}
    if keywords is not None:
        risk_config["leveraged_etf_name_keywords"] = keywords
    config = SimpleNamespace(
        ibkr_host="127.0.0.1",
        ibkr_port=4002,
        ibkr_client_id=1,
        risk_config=risk_config,
    )
    return IBKRBroker(config)


# === 1. Real leveraged/inverse funds — must be caught ===

LEVERAGED_FUNDS = [
    # (ticker, registered longName) — all real products
    ("SOXL", "Direxion Daily Semiconductor Bull 3X Shares"),
    ("SOXS", "Direxion Daily Semiconductor Bear 3X Shares"),
    ("SQQQ", "ProShares UltraPro Short QQQ"),
    ("TQQQ", "ProShares UltraPro QQQ"),
    ("QLD", "ProShares Ultra QQQ"),                       # 2x, no "2X" in name
    ("SDS", "ProShares UltraShort S&P500"),
    ("SH", "ProShares Short S&P500"),
    ("TSLL", "Direxion Daily TSLA Bull 2X Shares"),       # the 2026-06-09 leak
    ("TSLZ", "T-Rex 2X Inverse Tesla Daily Target ETF"),
    ("NVDL", "GraniteShares 2x Long NVDA Daily ETF"),
    ("HIBS", "Direxion Daily S&P 500 High Beta Bear 3X Shares"),  # the -$375
    ("EDZ", "Direxion Daily MSCI Emerging Markets Bear 3X Shares"),
    ("TZA", "Direxion Daily Small Cap Bear 3X Shares"),
    ("UVXY", "ProShares Ultra VIX Short-Term Futures ETF"),
    ("TSLQ", "Tradr 1.5X Short TSLA Daily ETF"),
]


def test_real_leveraged_funds_are_caught():
    broker = _make_broker()
    missed = [
        (sym, name) for sym, name in LEVERAGED_FUNDS
        if not broker._is_leveraged_etf_name(name)
    ]
    assert not missed, (
        "Leveraged funds NOT caught by the class filter (each of these "
        "is a live -$375-class risk):\n"
        + "\n".join(f"  {s}: {n}" for s, n in missed)
    )


# === 2. Real operating companies — must NOT be caught ===

REAL_COMPANIES = [
    ("AAPL", "Apple Inc"),
    ("UCTT", "Ultra Clean Holdings Inc"),       # the "ULTRA " trap
    ("BFRG", "Bullfrog AI Holdings Inc"),       # "BULL" inside a word
    ("DJCO", "Daily Journal Corp"),             # "Daily" alone isn't enough
    ("SNXX", "Snexx Pharma Holdings"),          # today's +$138 winner-class name
    ("MARA", "MARA Holdings Inc"),
    ("COIN", "Coinbase Global Inc"),
    ("NEXR", "NexRev Technologies Inc"),
    ("SOFI", "SoFi Technologies Inc"),
]


def test_real_companies_pass_through():
    broker = _make_broker()
    wrongly_blocked = [
        (sym, name) for sym, name in REAL_COMPANIES
        if broker._is_leveraged_etf_name(name)
    ]
    assert not wrongly_blocked, (
        "Real companies wrongly flagged as leveraged ETFs (these would "
        "silently vanish from the scan universe):\n"
        + "\n".join(f"  {s}: {n}" for s, n in wrongly_blocked)
    )


# === 3. Config override ===


def test_keywords_overridable_via_config():
    broker = _make_broker(keywords=["MOON ROCKET"])
    assert broker._is_leveraged_etf_name("Acme Moon Rocket 5X Fund")
    # Default keywords no longer apply with the override in place
    assert not broker._is_leveraged_etf_name("Direxion Daily TSLA Bull 2X Shares")


def test_empty_keyword_list_disables_filter():
    broker = _make_broker(keywords=[])
    assert not broker._is_leveraged_etf_name("ProShares UltraPro Short QQQ")


# === 4. Missing name fails open ===


def test_empty_or_none_longname_fails_open():
    """No name = no class verdict. The static ticker blocklist still
    backstops known offenders; don't block unnamed symbols blind."""
    broker = _make_broker()
    assert broker._is_leveraged_etf_name("") is False
    assert broker._is_leveraged_etf_name(None) is False


# === 5. Word-boundary behavior at name edges ===


def test_bull_bear_match_at_name_start_and_end():
    """Padded matching: ' BULL ' must hit names that begin or end with
    the word, not only mid-name occurrences."""
    broker = _make_broker()
    assert broker._is_leveraged_etf_name("Bull 3X Whatever Fund")
    assert broker._is_leveraged_etf_name("Some Index Daily Bear")
