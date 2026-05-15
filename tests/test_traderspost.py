"""TradersPost webhook broker — coverage for the patterns that bit us live.

This file targets the failure modes documented in CLAUDE.md "Common Issues":
- Dual-mode mismatch (TradersPost rejecting exit signals it has no record of)
- Rate limiting (3s global cooldown, 3 per 60s per symbol, exits bypass)
- Empty webhook URL = disabled (must not crash, must not 'send')
- Bullish-only quirk: sell/short entry signals must be blocked; sell exits must
  map to action="exit" with NO sentiment (TradersPost rejects bearish sentiment)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from bot.brokers.traderspost import TradersPostBroker


class _TPConfig:
    """Minimal config surface the broker reads."""

    def __init__(
        self,
        webhook_url="https://tp.test/webhook/primary",
        webhook_url_secondary="",
        webhook_url_crypto="",
        api_key="",
        webhook_password="",
    ):
        self.traderspost_webhook_url = webhook_url
        self.traderspost_webhook_url_secondary = webhook_url_secondary
        self.traderspost_webhook_url_crypto = webhook_url_crypto
        self.traderspost_api_key = api_key
        self.traderspost_webhook_password = webhook_password


def _ok_response(status=200, text="OK"):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.text = text
    return resp


@pytest.fixture(autouse=True)
def _no_persist():
    """Stub out the on-disk signal log so test runs don't pollute
    data/signal_log.json with fake AAPL signals."""
    with patch.object(TradersPostBroker, "_persist_signal", lambda *a, **kw: None):
        yield


@pytest.fixture
def tp_config():
    return _TPConfig()


@pytest.fixture
def buy_signal():
    return {
        "symbol": "AAPL",
        "action": "buy",
        "quantity": 10,
        "price": 150.00,
        "stop_loss": 147.00,
        "take_profit": 156.00,
        "strategy": "momentum",
    }


@pytest.fixture
def exit_signal():
    return {
        "symbol": "AAPL",
        "action": "sell",
        "quantity": 10,
        "price": 152.00,
        "source": "exit",
        "strategy": "momentum",
    }


# ---------------------------------------------------------------------------
# Disabled broker (blank webhook URL)
# ---------------------------------------------------------------------------

def test_blank_webhook_url_is_not_connected(tp_config, buy_signal):
    """Empty TRADERSPOST_WEBHOOK_URL = broker dormant. The IBKR-direct
    architecture relies on this — see CLAUDE.md 'Common Issues'."""
    tp_config.traderspost_webhook_url = ""
    broker = TradersPostBroker(tp_config)
    assert broker.is_connected() is False


def test_send_signal_no_webhook_returns_none(tp_config, buy_signal):
    """No webhook URL = silent no-op (never crash, never pretend to send)."""
    tp_config.traderspost_webhook_url = ""
    broker = TradersPostBroker(tp_config)
    with patch("bot.brokers.traderspost.requests.post") as posted:
        result = broker.send_signal(buy_signal)
    assert result is None
    posted.assert_not_called()


# ---------------------------------------------------------------------------
# Buy entry — payload formation
# ---------------------------------------------------------------------------

def test_buy_signal_posts_bullish_payload(tp_config, buy_signal):
    """Buy entry payload must carry ticker, action=buy, sentiment=bullish,
    quantity, price, stopLoss object, takeProfit object."""
    broker = TradersPostBroker(tp_config)
    with patch("bot.brokers.traderspost.requests.post", return_value=_ok_response()) as posted:
        result = broker.send_signal(buy_signal)

    assert result and result.get("success") is True
    posted.assert_called_once()
    sent_url = posted.call_args.args[0]
    payload = posted.call_args.kwargs["json"]
    assert sent_url == tp_config.traderspost_webhook_url
    assert payload["ticker"] == "AAPL"
    assert payload["action"] == "buy"
    assert payload["sentiment"] == "bullish"
    assert payload["quantity"] == 10
    assert payload["price"] == 150.00
    assert payload["stopLoss"] == {"type": "stop", "stopPrice": 147.00}
    assert payload["takeProfit"] == {"type": "limit", "limitPrice": 156.00}


def test_buy_without_take_profit_defaults_to_2pct(tp_config, buy_signal):
    """TradersPost requires takeProfit on every entry — when the signal omits
    it, the broker injects a 2% default. Critical: webhooks otherwise reject."""
    buy_signal.pop("take_profit")
    broker = TradersPostBroker(tp_config)
    with patch("bot.brokers.traderspost.requests.post", return_value=_ok_response()) as posted:
        broker.send_signal(buy_signal)
    payload = posted.call_args.kwargs["json"]
    # 150 * 1.02 = 153.0
    assert payload["takeProfit"] == {"type": "limit", "limitPrice": 153.00}


def test_api_key_sent_as_bearer_header(tp_config, buy_signal):
    tp_config.traderspost_api_key = "tp_key_abc"
    broker = TradersPostBroker(tp_config)
    with patch("bot.brokers.traderspost.requests.post", return_value=_ok_response()) as posted:
        broker.send_signal(buy_signal)
    headers = posted.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tp_key_abc"


def test_webhook_password_added_to_payload(tp_config, buy_signal):
    """TradersPost "Invalid Password" rejections happen when webhooks are
    password-protected. The broker must inject password into the payload."""
    tp_config.traderspost_webhook_password = "secret123"
    broker = TradersPostBroker(tp_config)
    with patch("bot.brokers.traderspost.requests.post", return_value=_ok_response()) as posted:
        broker.send_signal(buy_signal)
    payload = posted.call_args.kwargs["json"]
    assert payload["password"] == "secret123"


# ---------------------------------------------------------------------------
# Exit / sell handling — TradersPost is bullish-only
# ---------------------------------------------------------------------------

def test_exit_signal_maps_sell_to_exit_with_no_sentiment(tp_config, exit_signal):
    """The "dual-mode mismatch" rejection (CLAUDE.md) traces back to TradersPost
    rejecting "sell" with "bearish" sentiment. ALL exits must use action="exit"
    and OMIT sentiment entirely."""
    broker = TradersPostBroker(tp_config)
    with patch("bot.brokers.traderspost.requests.post", return_value=_ok_response()) as posted:
        broker.send_signal(exit_signal)
    payload = posted.call_args.kwargs["json"]
    assert payload["action"] == "exit"
    assert "sentiment" not in payload  # bearish sentiment = TradersPost reject
    # Exits omit stopLoss/takeProfit
    assert "stopLoss" not in payload
    assert "takeProfit" not in payload


def test_short_entry_signal_blocked(tp_config):
    """Short entries should never reach the webhook — TradersPost strategy is
    bullish-only, accepting them produces a guaranteed rejection."""
    broker = TradersPostBroker(tp_config)
    short_signal = {"symbol": "AAPL", "action": "short", "quantity": 10, "price": 150.0}
    with patch("bot.brokers.traderspost.requests.post") as posted:
        result = broker.send_signal(short_signal)
    posted.assert_not_called()
    assert result == {"success": False, "reason": "long_only", "blocked": True}


def test_cover_action_maps_to_exit(tp_config):
    """Cover (closing a short) maps to exit. Same anti-bearish rule."""
    broker = TradersPostBroker(tp_config)
    sig = {"symbol": "AAPL", "action": "cover", "quantity": 10, "price": 150.0}
    with patch("bot.brokers.traderspost.requests.post", return_value=_ok_response()) as posted:
        broker.send_signal(sig)
    payload = posted.call_args.kwargs["json"]
    assert payload["action"] == "exit"
    assert "sentiment" not in payload


# ---------------------------------------------------------------------------
# Rate limiting — entries blocked, exits ALWAYS through
# ---------------------------------------------------------------------------

def test_global_cooldown_blocks_rapid_entries(tp_config, buy_signal):
    """3s global minimum interval between webhook calls. Second entry inside
    the window must be blocked with 429-like result."""
    broker = TradersPostBroker(tp_config)
    with patch("bot.brokers.traderspost.requests.post", return_value=_ok_response()):
        r1 = broker.send_signal(buy_signal)
        r2 = broker.send_signal({**buy_signal, "symbol": "MSFT"})  # different sym, same global window
    assert r1["success"] is True
    assert r2 == {"success": False, "reason": "rate_limited", "status_code": 429}


def test_per_symbol_rate_limit_blocks_after_three(tp_config, buy_signal):
    """3 signals per 60s per symbol. The 4th in the window must be blocked.
    Pre-populate the per-symbol bucket and the global cooldown timer so we
    can isolate the per-symbol cap from the 3s global gate."""
    import time as real_time
    broker = TradersPostBroker(tp_config)
    now = real_time.time()
    # 3 recent signals on AAPL fill the bucket
    broker._symbol_signals["AAPL"] = [now - 30, now - 20, now - 10]
    # Push the global cooldown into the past so it doesn't trip first
    broker._last_webhook_time = now - 60
    with patch("bot.brokers.traderspost.requests.post", return_value=_ok_response()):
        result = broker.send_signal(buy_signal)
    assert result == {"success": False, "reason": "rate_limited", "status_code": 429}


def test_per_symbol_rate_limit_isolated_per_symbol(tp_config, buy_signal):
    """The per-symbol bucket must not bleed across symbols — three AAPL signals
    don't block an MSFT entry."""
    import time as real_time
    broker = TradersPostBroker(tp_config)
    now = real_time.time()
    broker._symbol_signals["AAPL"] = [now - 30, now - 20, now - 10]
    broker._last_webhook_time = now - 60
    with patch("bot.brokers.traderspost.requests.post", return_value=_ok_response()):
        result = broker.send_signal({**buy_signal, "symbol": "MSFT"})
    assert result["success"] is True


def test_exit_bypasses_rate_limit(tp_config, buy_signal, exit_signal):
    """Exits must NEVER be rate limited — being unable to close a position
    is far worse than spamming the webhook."""
    broker = TradersPostBroker(tp_config)
    with patch("bot.brokers.traderspost.requests.post", return_value=_ok_response()) as posted:
        broker.send_signal(buy_signal)
        # Immediate exit, well inside the 3s global cooldown — must go through
        exit_result = broker.send_signal(exit_signal)
    assert exit_result["success"] is True
    assert posted.call_count == 2


# ---------------------------------------------------------------------------
# Dual mode + crypto routing + mirror mode
# ---------------------------------------------------------------------------

def test_dual_mode_posts_to_both_webhooks(tp_config, buy_signal):
    """Primary + secondary = dual mode. Every signal sent to BOTH URLs."""
    tp_config.traderspost_webhook_url_secondary = "https://tp.test/webhook/paper"
    broker = TradersPostBroker(tp_config)
    assert broker.dual_mode is True
    with patch("bot.brokers.traderspost.requests.post", return_value=_ok_response()) as posted:
        broker.send_signal(buy_signal)
    # Primary + secondary = 2 calls
    assert posted.call_count == 2
    urls = [c.args[0] for c in posted.call_args_list]
    assert tp_config.traderspost_webhook_url in urls
    assert tp_config.traderspost_webhook_url_secondary in urls


def test_crypto_signal_routes_to_crypto_webhook(tp_config, buy_signal):
    tp_config.traderspost_webhook_url_crypto = "https://tp.test/webhook/crypto"
    broker = TradersPostBroker(tp_config)
    crypto_signal = {**buy_signal, "symbol": "BTC-USD"}
    with patch("bot.brokers.traderspost.requests.post", return_value=_ok_response()) as posted:
        broker.send_signal(crypto_signal)
    sent_url = posted.call_args.args[0]
    assert sent_url == tp_config.traderspost_webhook_url_crypto


def test_mirror_mode_uses_override_url(tp_config, buy_signal):
    """engine.tp_mirror is constructed with webhook_url_override pointing at
    the MIRROR webhook (separate TP account). Must NOT inherit primary URL."""
    tp_config.traderspost_webhook_url = "https://tp.test/webhook/primary"
    mirror = TradersPostBroker(tp_config, webhook_url_override="https://tp.test/webhook/mirror")
    assert mirror.webhook_url == "https://tp.test/webhook/mirror"
    with patch("bot.brokers.traderspost.requests.post", return_value=_ok_response()) as posted:
        mirror.send_signal(buy_signal)
    assert posted.call_args.args[0] == "https://tp.test/webhook/mirror"


# ---------------------------------------------------------------------------
# Response handling — the "HTTP 200 but rejected" pattern
# ---------------------------------------------------------------------------

def test_rejected_in_body_marked_as_not_successful(tp_config, exit_signal):
    """TradersPost returns HTTP 200 even when it rejects the signal (e.g.
    'no open position'). The body carries the truth — must surface as failure."""
    rejection = _ok_response(status=200, text='{"status":"rejected","reason":"no open position"}')
    broker = TradersPostBroker(tp_config)
    with patch("bot.brokers.traderspost.requests.post", return_value=rejection):
        result = broker.send_signal(exit_signal)
    assert result["success"] is False
    assert result["rejected"] is True
    assert result["status_code"] == 200


def test_http_error_marked_as_not_successful(tp_config, buy_signal):
    err = _ok_response(status=500, text="server explosion")
    broker = TradersPostBroker(tp_config)
    with patch("bot.brokers.traderspost.requests.post", return_value=err):
        result = broker.send_signal(buy_signal)
    assert result["success"] is False
    assert result["rejected"] is False
    assert result["status_code"] == 500


def test_request_timeout_returns_none(tp_config, buy_signal):
    """Timeouts must surface as None (caller treats None as 'try fallback')
    rather than as a spurious success."""
    broker = TradersPostBroker(tp_config)
    with patch(
        "bot.brokers.traderspost.requests.post",
        side_effect=requests.exceptions.Timeout(),
    ):
        result = broker.send_signal(buy_signal)
    assert result is None
