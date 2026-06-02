"""Crypto Runner Strategy — momentum/breakout lane for live crypto pumps.

Tonight's review (2026-05-27) exposed a gap: the bot can grind small wins
on slow crypto uptrends via `mean_reversion`, but it has no entry path for
a name pumping +20% intraday. `momentum` was disabled on crypto in session
5(5) because its default equity-tuned params lost on crypto's chop. This
strategy is the missing lane — explicitly designed for the WLD / DRIFT /
JTO / ENA-style 1-12h pumps.

Mirrors the `low_float_catalyst` shape (hard stop, hard target, time stop,
no trailing) — the same "asymmetric upside, accept higher per-trade loss
probability" bet pattern, on a different asset class. Per-trade max loss
capped by sizing + the -6% server-side stop.

Entry filters:
  - Crypto-only (any `-USD`/`-USDT`/`-BTC`/`-ETH` suffix). Equity skipped.
  - 1h change ≥ +5% (catalyst already in motion, not pre-spec). Computed
    from the last 12 × 5-min bars — same bar source mean_reversion uses.
  - RVOL ≥ 3x on the last 5-min bar vs the prior 10 bars. Lower than the
    equity 5x floor because crypto volume is naturally noisier — bumping
    it tighter would kill the signal on every name except BTC/ETH.
  - Bonus path: if the symbol was injected by the CoinGecko scanner's
    `new_entrants()` (i.e. it just appeared in top-50 by volume vs. the
    previous snapshot), the 1h-change floor halves. New entrants are the
    earliest scanner-detectable signal of fresh attention — relax the
    confirmation threshold so we don't miss the first 30 min of a run.

Exit machinery (set on the signal — engine wires the bracket):
  - hard_stop_pct: -6% from entry (server-side, wider than equity to
    accommodate crypto whippy intraday moves)
  - hard_target_pct: +15% (crypto runners often go +40-100%, but +15%
    gives a clean asymmetric ratio — TP/stop = 2.5x — and is hit by the
    typical 4h pump)
  - max_hold_minutes: 240 (4h). A pump that hasn't worked in 4 hours is
    dead; the next move is the reversal, not the continuation.
  - No trailing — same playbook as low_float_catalyst. Trailing on a
    crypto pump's pullbacks is a guaranteed nick (see PR #172 for the
    trail-arm rationale).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from bot.strategies.base import BaseStrategy
from bot.utils.logger import get_logger

log = get_logger("strategy.crypto_runner")
ET = ZoneInfo("America/New_York")

_CRYPTO_SUFFIXES = ("-USD", "-USDT", "-BTC", "-ETH")


class CryptoRunnerStrategy(BaseStrategy):
    """Live-pump catcher on crypto. See module docstring for design."""

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)
        self.min_1h_change_pct = float(config.get("min_1h_change_pct", 5.0))
        self.min_rvol = float(config.get("min_rvol", 3.0))
        self.hard_stop_pct = float(config.get("hard_stop_pct", 0.06))
        self.hard_target_pct = float(config.get("hard_target_pct", 0.15))
        self.max_hold_minutes = int(config.get("max_hold_minutes", 240))
        self.max_trades_per_day = int(config.get("max_trades_per_day", 8))
        self.max_position_pct = float(config.get("max_position_pct", 0.04))
        # New-entrant boost: when a symbol is in the scanner's new_entrants
        # list, the 1h-change floor multiplier scales it down. 0.5 means we
        # accept names with half the normal 1h move (e.g. +2.5% instead of
        # +5%) — the lift from "just appeared in top-50 by volume" is the
        # confirmation we'd otherwise demand from price.
        self.new_entrant_threshold_mult = float(
            config.get("new_entrant_threshold_mult", 0.5)
        )
        # If True, only enter on names that appear in scanner.new_entrants()
        # OR pass the full normal threshold. False (default) allows the full
        # static universe through the normal-threshold path too.
        self.require_new_entrant = bool(config.get("require_new_entrant", False))

        self._dynamic_symbols: set[str] = set()

    def add_dynamic_symbols(self, symbols):
        """Engine injects the crypto universe here every cycle. Equity gets
        filtered out at entry, but rejecting them here saves a per-symbol
        scan_results write."""
        import time

        now = time.time()
        for sym in symbols or []:
            if not isinstance(sym, str):
                continue
            s = sym.upper()
            if not any(s.endswith(suf) for suf in _CRYPTO_SUFFIXES):
                continue
            self._dynamic_symbols.add(s)
            self._dynamic_symbol_timestamps[s] = now

    def get_symbols(self):
        return list(set(self.symbols) | self._dynamic_symbols)

    def _new_entrants(self) -> set[str]:
        """Pull the scanner's new-entrants list. Import inside the method so
        the strategy import doesn't pull in CoinGecko module at boot when
        the scanner is disabled."""
        try:
            from bot.data.crypto_scanner import new_entrants

            return set(new_entrants(limit=50))
        except Exception as e:
            log.debug(f"crypto_scanner.new_entrants() unavailable: {e}")
            return set()

    def generate_signals(self, market_data):
        signals: list[dict] = []
        today = datetime.now(ET).date()
        if self.last_trade_date != today:
            self.trades_today = 0
            self.last_trade_date = today
        if self.trades_today >= self.max_trades_per_day:
            return signals

        new_entrants = self._new_entrants()

        for symbol in self.get_symbols():
            try:
                sig = self._evaluate(symbol, market_data, new_entrants)
                if sig is None:
                    continue
                signals.append(sig)
                if self.trades_today + len(signals) >= self.max_trades_per_day:
                    break
            except Exception as e:
                log.debug(f"crypto_runner error for {symbol}: {e}")
        return signals

    def _evaluate(self, symbol, market_data, new_entrants):
        sym = symbol.upper()
        if not any(sym.endswith(suf) for suf in _CRYPTO_SUFFIXES):
            return None

        if self._held_symbols is not None and sym in self._held_symbols:
            return None

        is_new_entrant = sym in new_entrants
        if self.require_new_entrant and not is_new_entrant:
            return None

        # 5-min bars × 12 = last 60 min. Pull a bit extra so we have at
        # least 11 prior bars for RVOL avg even if the last bar is fresh.
        bars = market_data.get_bars(symbol, 25) if market_data else None
        if bars is None or len(bars) < 13:
            return None

        closes = bars["close"].values
        volumes = bars["volume"].values
        # Last fully-formed bar is closes[-1]; the bar 12 back is closes[-13].
        # (If bars are length N, indices -1..-13 span the last 12 closes plus
        # the anchor 60min ago.)
        price = float(closes[-1])
        anchor_price = float(closes[-13])
        if anchor_price <= 0 or price <= 0:
            return None
        change_1h_pct = (price - anchor_price) / anchor_price * 100.0

        threshold = self.min_1h_change_pct
        if is_new_entrant:
            threshold *= self.new_entrant_threshold_mult
        if change_1h_pct < threshold:
            self.scan_results[sym] = {
                "status": "wait_1h_change",
                "change_1h_pct": round(change_1h_pct, 2),
                "threshold_pct": round(threshold, 2),
                "new_entrant": is_new_entrant,
            }
            return None

        avg_vol = float(np.mean(volumes[-11:-1])) if len(volumes) > 11 else 0.0
        current_vol = float(volumes[-1])
        rvol = current_vol / avg_vol if avg_vol > 0 else 0.0
        if rvol < self.min_rvol:
            self.scan_results[sym] = {
                "status": "wait_rvol",
                "rvol": round(rvol, 2),
                "change_1h_pct": round(change_1h_pct, 2),
                "new_entrant": is_new_entrant,
            }
            return None

        stop_loss = round(price * (1 - self.hard_stop_pct), 6)
        take_profit = round(price * (1 + self.hard_target_pct), 6)

        # Confidence: ramps with both the 1h move and RVOL, plus a fixed
        # bump for new entrants (cheapest signal of fresh attention).
        change_norm = min(change_1h_pct / 20.0, 1.0)
        rvol_norm = min(rvol / 10.0, 1.0)
        confidence = round(
            0.5 + 0.2 * change_norm + 0.2 * rvol_norm + (0.1 if is_new_entrant else 0),
            2,
        )

        reasons = [
            f"1h +{change_1h_pct:.1f}%",
            f"RVOL {rvol:.1f}x",
        ]
        if is_new_entrant:
            reasons.append("new entrant in top-50")

        self.scan_results[sym] = {
            "status": "SIGNAL",
            "change_1h_pct": round(change_1h_pct, 2),
            "rvol": round(rvol, 2),
            "price": price,
            "new_entrant": is_new_entrant,
            "confidence": confidence,
        }
        self.signals_generated += 1
        log.info(
            f"CRYPTO-RUNNER SIGNAL: {sym} @ ${price:.6g} | "
            f"1h +{change_1h_pct:.1f}% | RVOL {rvol:.1f}x | "
            f"{'NEW entrant' if is_new_entrant else 'static univ'} | "
            f"conf {confidence:.2f}"
        )

        return {
            "symbol": sym,
            "action": "buy",
            "price": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "confidence": confidence,
            # Score for the engine's QUALITY GATE (engine.py:7990). Crypto
            # signals normally bypass via CRYPTO FAST LANE so this is
            # defensive, but missing scores have already cost us SNBR
            # (see low_float_catalyst comment). Don't repeat the bug here.
            "score": int(round(confidence * 100)),
            "reason": " | ".join(reasons),
            "max_hold_bars": self.max_hold_minutes,
            "bar_seconds": 60,
            "rvol": round(rvol, 2),
            "source": "crypto_runner",
            "strategy": "crypto_runner",
            # No server bracket on crypto — execution path is TradersPost
            # webhook (Alpaca crypto), which doesn't accept bracket orders
            # the same way IBKR does. Engine manages the stop/TP in-memory.
            "use_server_bracket": False,
            # No trailing — pumps whipsaw too violently for a trail to do
            # anything but get nicked. Same playbook as low_float_catalyst.
            "trailing_stop_pct": 0,
        }
