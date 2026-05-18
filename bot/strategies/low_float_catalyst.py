"""Low-Float Catalyst Strategy — micro-cap momentum on news/runner days.

The asymmetric-bet lane the bot was missing on 2026-05-18: GOVX (+163%, $1→$4),
VRAX, SBFM, WGRX all ran multi-hundred-percent intraday on tiny floats. The
existing momentum / rvol_scalp filter chain killed every one of them on either
the $2-5 price floor or a missing-news gate.

This strategy explicitly accepts higher per-trade loss probability in exchange
for asymmetric upside: small fixed-risk position, server-side hard stop, hard
target, time-stop — no trailing, no partial targets. The bracket is placed at
entry and forgotten. Per-trade max loss capped at ~$20-30 by sizing.

Entry filters:
  - Price $0.50-$10
  - Float ≤ 75M shares (Finviz cached lookup)
  - RVOL ≥ 5x (massive volume vs. average — the catalyst signal)
  - Spread ≤ 3% of mid (rejects $0.09-stock chop where spread eats edge)
  - Day change ≥ 15% (today's catalyst already in motion, not pre-spec)
  - Time: allow 04:00-09:25 ET (premarket), 09:35-15:00 ET (RTH ex-open chop)
    — skip the 9:25-9:35 open-dip window; engine-level pre-open exit flushes
    any premarket holds at 09:25 and the next entry can fire at 09:35+ if
    RVOL/trend still hold.

Exit machinery (set on the signal — engine wires brackets at IBKR):
  - hard_stop_pct: -8% from entry (server-side, won't fail if bot dies)
  - hard_target_pct: +20%
  - max_hold_minutes: 30
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from bot.data.finviz_float import get_float
from bot.strategies.base import BaseStrategy
from bot.utils.logger import get_logger

log = get_logger("strategy.low_float_catalyst")
ET = ZoneInfo("America/New_York")


class LowFloatCatalystStrategy(BaseStrategy):
    """Micro-cap catalyst runner. See module docstring for design."""

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)
        self.min_price = config.get("min_price", 0.50)
        self.max_price = config.get("max_price", 10.00)
        self.max_float_m = config.get("max_float_m", 75.0)
        self.min_rvol = config.get("min_rvol", 5.0)
        self.max_spread_pct = config.get("max_spread_pct", 0.03)
        self.min_day_change_pct = config.get("min_day_change_pct", 15.0)
        self.hard_stop_pct = config.get("hard_stop_pct", 0.08)
        self.hard_target_pct = config.get("hard_target_pct", 0.20)
        self.max_hold_minutes = config.get("max_hold_minutes", 30)
        self.max_trades_per_day = config.get("max_trades_per_day", 8)
        # Block this many minutes either side of the 9:30 open (where these
        # names whipsaw). Engine also flushes any open positions at 9:25.
        self.open_dead_zone_start_min = config.get("open_dead_zone_start_min", 25)
        self.open_dead_zone_end_min = config.get("open_dead_zone_end_min", 35)
        # Optional float-required gate. If False, symbols with unknown float
        # (Finviz failure) are allowed through — useful in case Finviz rate
        # limits during a high-volume session.
        self.require_known_float = config.get("require_known_float", True)
        # Cap fraction of allocated capital sized into any single entry,
        # belt-and-suspenders with the risk_manager's per-position cap.
        self.max_position_pct = config.get("max_position_pct", 0.05)

        self._dynamic_symbols: set[str] = set()

    def add_dynamic_symbols(self, symbols):
        """Engine injects scanner runners here every cycle."""
        now = time.time()
        for sym in symbols or []:
            if sym and isinstance(sym, str):
                s = sym.upper()
                self._dynamic_symbols.add(s)
                self._dynamic_symbol_timestamps[s] = now

    def get_symbols(self):
        return list(set(self.symbols) | self._dynamic_symbols)

    def _in_allowed_window(self, now: datetime) -> bool:
        """True if we are NOT in the 9:25-9:35 ET open-chop dead zone.
        Crypto markets are 24/7 but this strategy is equity-only by design."""
        if now.weekday() >= 5:  # Sat/Sun
            return False
        minutes = now.hour * 60 + now.minute
        open_minutes = 9 * 60 + 30
        dead_start = open_minutes - self.open_dead_zone_start_min
        dead_end = open_minutes + self.open_dead_zone_end_min
        return not (dead_start <= minutes < dead_end)

    def generate_signals(self, market_data):
        signals: list[dict] = []

        today = datetime.now(ET).date()
        if self.last_trade_date != today:
            self.trades_today = 0
            self.last_trade_date = today

        if self.trades_today >= self.max_trades_per_day:
            return signals

        now = datetime.now(ET)
        if not self._in_allowed_window(now):
            return signals

        for symbol in self.get_symbols():
            try:
                sig = self._evaluate(symbol, market_data, now)
                if sig is None:
                    continue
                signals.append(sig)
                if self.trades_today + len(signals) >= self.max_trades_per_day:
                    break
            except Exception as e:
                log.debug(f"low_float_catalyst error for {symbol}: {e}")
        return signals

    def _evaluate(self, symbol, market_data, now):
        """Score one symbol; return signal dict or None."""
        # Equity-only.
        if any(symbol.upper().endswith(s) for s in ("-USD", "-USDT")):
            return None

        # Already held — don't double-up.
        if self._held_symbols is not None and symbol in self._held_symbols:
            return None

        quote = market_data.get_quote(symbol) if market_data else None
        if not quote or not quote.get("price"):
            return None

        price = float(quote["price"])
        if price < self.min_price or price > self.max_price:
            return None

        day_change = float(quote.get("change_pct") or 0)
        if day_change < self.min_day_change_pct:
            return None

        # Spread gate — bid/ask required.
        bid = quote.get("bid")
        ask = quote.get("ask")
        if bid and ask and bid > 0 and ask > bid:
            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid if mid > 0 else 1.0
            if spread_pct > self.max_spread_pct:
                self.scan_results[symbol] = {
                    "status": "wide_spread", "spread_pct": round(spread_pct, 4),
                    "price": price, "change_pct": day_change,
                }
                return None
        else:
            # No bid/ask = no liquidity confidence; reject.
            return None

        # RVOL from bars.
        bars = market_data.get_bars(symbol, 30) if market_data else None
        if bars is None or len(bars) < 10:
            return None
        volumes = bars["volume"].values
        avg_vol = float(np.mean(volumes[-11:-1])) if len(volumes) > 11 else float(np.mean(volumes[:-1]))
        current_vol = float(volumes[-1])
        rvol = current_vol / avg_vol if avg_vol > 0 else 0
        if rvol < self.min_rvol:
            self.scan_results[symbol] = {
                "status": "low_rvol", "rvol": round(rvol, 2),
                "price": price, "change_pct": day_change,
            }
            return None

        # Float gate — last because it's the slowest (network).
        float_m = get_float(symbol)
        if float_m is None:
            if self.require_known_float:
                return None
        elif float_m > self.max_float_m:
            self.scan_results[symbol] = {
                "status": "float_too_large", "float_m": float_m,
                "price": price, "change_pct": day_change,
            }
            return None

        # All filters passed. Build bracket signal.
        stop_loss = round(price * (1 - self.hard_stop_pct), 2)
        take_profit = round(price * (1 + self.hard_target_pct), 2)

        # Confidence scales with RVOL strength and day-change magnitude.
        rvol_norm = min(rvol / 10.0, 1.0)
        change_norm = min(day_change / 50.0, 1.0)
        confidence = round(0.5 + 0.25 * rvol_norm + 0.25 * change_norm, 2)

        reasons = [
            f"RVOL {rvol:.1f}x",
            f"change +{day_change:.1f}%",
            f"float {float_m:.1f}M" if float_m is not None else "float unknown",
            f"spread {spread_pct*100:.2f}%",
        ]

        self.scan_results[symbol] = {
            "status": "SIGNAL", "rvol": round(rvol, 2),
            "price": price, "change_pct": day_change,
            "float_m": float_m, "spread_pct": round(spread_pct, 4),
            "confidence": confidence,
        }

        self.signals_generated += 1
        log.info(
            f"LOW-FLOAT SIGNAL: {symbol} @ ${price:.2f} | "
            f"RVOL {rvol:.1f}x | +{day_change:.1f}% | "
            f"float {float_m if float_m else '?'}M | conf {confidence:.2f}"
        )

        return {
            "symbol": symbol,
            "action": "buy",
            "price": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "confidence": confidence,
            "reason": " | ".join(reasons),
            # The engine reads these to size the position and configure exit
            # machinery. max_hold_bars × bar_seconds = hard time-stop.
            "max_hold_bars": self.max_hold_minutes,
            "bar_seconds": 60,
            "rvol": round(rvol, 2),
            "source": "low_float_catalyst",
            "strategy": "low_float_catalyst",
            # Tell engine to use a real IBKR bracket (server-side stop+TP).
            # Survives bot crashes — the lesson from the 2026-05-15 SHOP -$821
            # event where the bot's in-memory stop was fiction.
            "use_server_bracket": True,
            # No trailing — these names whipsaw too violently for a trail to
            # do anything but get nicked.
            "trailing_stop_pct": 0,
        }
