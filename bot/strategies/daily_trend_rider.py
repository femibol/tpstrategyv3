"""
Daily Trend Rider — ride multi-day momentum in liquid stocks.

Concept: find stocks that have been going up consistently (consecutive green
daily candles, price above daily 20 EMA, SuperTrend bullish) and hold them
until the daily trend breaks. This is swing trading, not scalping.

Scanner (runs once near market open):
  - Fetches 60 days of daily bars via IBKR
  - Filters for: 3+ consecutive green daily closes, price > 20 EMA on daily,
    ADX(14) > 25, SuperTrend(7,3) bullish
  - Requires liquid mid/large-cap stocks (avg vol > 1M, price $10-$500)

Entry (intraday):
  - Once a stock passes the daily filter, watches for a pullback entry on 5-min:
    dip to VWAP or 9 EMA then bounce (close back above), on decent volume
  - Can also enter on break of yesterday's high with volume confirmation

Exit:
  - Daily close below SuperTrend(7,3) line → close next session
  - Daily close below 20 EMA → close next session
  - First red daily close after 5+ green days → close (momentum exhaustion)
  - Daily-ATR trailing stop: 1.5x ATR(14) from highest daily close
  - Safety cap: max_hold_days (default 20)

Position management:
  - Positions flagged trend_rider=True bypass normal EOD close
  - Own overnight bucket (max_positions independent of intraday overnight cap)
  - Wider stops than intraday (daily ATR, not 5-min ATR)
  - Smaller position size (higher risk per share → fewer shares)
"""
import time
import numpy as np
from datetime import datetime

try:
    import pytz
    ET = pytz.timezone("US/Eastern")
except ImportError:
    ET = None

from bot.strategies.base import BaseStrategy
from bot.utils.logger import get_logger

log = get_logger("strategy.daily_trend_rider")


class DailyTrendRiderStrategy(BaseStrategy):
    """Ride multi-day momentum trends in liquid stocks."""

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)
        self.min_price = config.get("min_price", 10.0)
        self.max_price = config.get("max_price", 500.0)
        self.min_avg_volume = config.get("min_avg_volume", 1_000_000)
        self.min_green_days = config.get("min_green_days", 3)
        self.ema_period = config.get("ema_period", 20)
        self.adx_threshold = config.get("adx_threshold", 25)
        self.supertrend_period = config.get("supertrend_period", 7)
        self.supertrend_mult = config.get("supertrend_multiplier", 3.0)
        self.atr_period = config.get("atr_period", 14)
        self.atr_stop_mult = config.get("atr_stop_multiplier", 1.5)
        self.max_hold_days = config.get("max_hold_days", 20)
        self.max_positions = config.get("max_positions", 3)
        self.entry_pullback_pct = config.get("entry_pullback_pct", 0.02)
        # Rotation: when at capacity, allow swapping the weakest existing
        # trend rider for a clearly superior new candidate.
        self.rotation_score_ratio = config.get("rotation_score_ratio", 1.25)  # New must score ≥1.25× weakest
        self.rotation_min_hold_days = config.get("rotation_min_hold_days", 1)  # Don't rotate same-day positions

        # Daily-bar cache: {symbol: {bars, supertrend, ema20, atr, ...}}
        self._daily_cache = {}
        self._last_daily_scan = 0
        self._daily_scan_interval = 3600  # Re-scan daily bars every hour
        self._qualified = {}  # symbol -> daily analysis dict (candidates)
        self._dynamic_symbols = set()  # Dynamically-injected universe

        # Track active trend rider positions (to enforce max_positions + rotation)
        self._active_count = 0
        self._active_positions = {}  # symbol -> position dict (passed in by engine)

    def add_dynamic_symbols(self, symbols):
        """Inject dynamically discovered symbols from the engine's scanner."""
        now = time.time()
        for sym in symbols:
            if sym and isinstance(sym, str):
                s = sym.upper()
                self._dynamic_symbols.add(s)
                self._dynamic_symbol_timestamps[s] = now

    def get_symbols(self):
        """Return combined static + dynamic symbol list."""
        return list(set(self.symbols) | self._dynamic_symbols)

    def set_active_count(self, count):
        """Called by engine to tell us how many trend rider positions are open."""
        self._active_count = count

    def set_active_positions(self, positions):
        """Called by engine each cycle with the current trend rider positions.
        positions: dict {symbol: position_dict}. Used for rotation scoring."""
        self._active_positions = positions or {}
        self._active_count = len(self._active_positions)

    # =========================================================================
    # Main signal generation (called every cycle by engine)
    # =========================================================================

    def generate_signals(self, market_data):
        """Two-phase: daily scan for candidates, then intraday entry triggers.

        When at capacity, the candidate competes against the weakest existing
        position. If it scores ≥ rotation_score_ratio× weaker, the signal is
        emitted with rotation_target_symbol set so the engine swaps them.
        """
        signals = []
        now = time.time()

        # Phase 1: Refresh daily bar analysis periodically
        if now - self._last_daily_scan > self._daily_scan_interval:
            self._scan_daily_bars(market_data)
            self._last_daily_scan = now

        # Phase 2: Check for intraday entry triggers on qualified candidates.
        # Don't break early when at capacity — let candidates compete via rotation.
        slots_used_this_cycle = 0  # Counts new entries (open slots + rotations)
        rotated_symbols = set()    # Don't rotate the same position twice in one cycle

        for symbol, daily in self._qualified.items():
            # Skip candidates we already hold
            if symbol in self._active_positions:
                continue

            try:
                sig = self._check_intraday_entry(symbol, daily, market_data)
                if not sig:
                    continue

                effective_active = self._active_count + slots_used_this_cycle - len(rotated_symbols)
                if effective_active < self.max_positions:
                    # Open slot — straight entry
                    signals.append(sig)
                    slots_used_this_cycle += 1
                else:
                    # At capacity — try rotation against weakest existing
                    target = self._consider_rotation(sig, daily, market_data, rotated_symbols)
                    if target:
                        sig["rotation_target_symbol"] = target["symbol"]
                        sig["reason"] = (
                            f"{sig['reason']} | ROTATION: replacing {target['symbol']} "
                            f"(score {sig['_rider_score']:.0f} vs {target['score']:.0f})"
                        )
                        signals.append(sig)
                        rotated_symbols.add(target["symbol"])
                        slots_used_this_cycle += 1
                        log.info(
                            f"TREND RIDER ROTATION: in {symbol} ({sig['_rider_score']:.0f}) "
                            f"out {target['symbol']} ({target['score']:.0f}) — "
                            f"ratio {sig['_rider_score'] / max(target['score'], 1):.2f}x"
                        )
            except Exception as e:
                log.debug(f"Trend rider entry check failed for {symbol}: {e}")

        return signals

    # =========================================================================
    # Rotation scoring
    # =========================================================================

    @staticmethod
    def _score_setup(daily_data):
        """Score a trend setup using its daily metrics. Higher = stronger trend.

        Components (typical ranges):
          - Green-day streak  (10 pts/day, capped at 80)
          - ADX strength      (0-100 → use as-is, capped at 60)
          - Weekly change %   (capped at 30)
          - Distance above SuperTrend (% × 100, capped at 30)

        Total range: ~30 (just qualified) to ~200 (monster trend).
        """
        green = min(daily_data.get("green_days", 0), 8) * 10
        adx = min(daily_data.get("adx", 0), 60)
        weekly = min(max(daily_data.get("weekly_change_pct", 0), 0), 30)
        price = daily_data.get("price", 0)
        st = daily_data.get("supertrend", 0)
        st_dist = 0.0
        if price > 0 and st > 0:
            st_dist = min((price - st) / price * 100, 30)
            st_dist = max(st_dist, 0)
        return green + adx + weekly + st_dist

    def _consider_rotation(self, new_signal, new_daily, market_data, already_rotated):
        """Score the new candidate vs weakest existing position. Return the
        weakest if it should be swapped, else None.

        Returns dict {symbol, score} of the position to close, or None.
        """
        if not self._active_positions:
            return None

        new_score = self._score_setup(new_daily)
        new_signal["_rider_score"] = new_score  # Store for the rotation log

        broker = getattr(market_data, "broker", None)
        if not broker or not broker.is_connected():
            return None

        # Score every existing trend rider position with FRESH daily data.
        # Stale entry-time scores would unfairly favor swapping out positions
        # that are still strong but were entered when momentum was lower.
        weakest = None
        from datetime import datetime as _dt
        now_ts = _dt.now()

        for sym, pos in self._active_positions.items():
            if sym in already_rotated:
                continue

            # Honor minimum hold — don't rotate a position entered today
            entry_time = pos.get("entry_time")
            if entry_time:
                try:
                    if isinstance(entry_time, str):
                        et = _dt.fromisoformat(entry_time.replace("Z", "+00:00"))
                        if et.tzinfo is not None:
                            et = et.replace(tzinfo=None)
                    else:
                        et = entry_time.replace(tzinfo=None) if hasattr(entry_time, 'tzinfo') and entry_time.tzinfo else entry_time
                    held_days = (now_ts - et).total_seconds() / 86400
                    if held_days < self.rotation_min_hold_days:
                        continue
                except Exception:
                    pass  # If we can't parse, allow rotation

            try:
                fresh = self._analyze_daily(sym, broker)
                if not fresh:
                    # Position no longer qualifies on its own metrics — easy swap
                    score = 0
                else:
                    score = self._score_setup(fresh)

                if weakest is None or score < weakest["score"]:
                    weakest = {"symbol": sym, "score": score}
            except Exception as e:
                log.debug(f"Rotation rescore failed for {sym}: {e}")

        if not weakest:
            return None

        # New candidate must be meaningfully stronger
        if new_score >= weakest["score"] * self.rotation_score_ratio:
            return weakest
        return None

    # =========================================================================
    # Phase 1: Daily bar scanning
    # =========================================================================

    def _scan_daily_bars(self, market_data):
        """Fetch daily bars and filter for multi-day momentum candidates."""
        broker = getattr(market_data, "broker", None)
        if not broker or not broker.is_connected():
            log.debug("Trend rider: IBKR not connected, skipping daily scan")
            return

        symbols_to_scan = self._get_scan_universe(market_data)
        self._qualified.clear()

        for symbol in symbols_to_scan:
            try:
                analysis = self._analyze_daily(symbol, broker)
                if analysis and analysis.get("qualified"):
                    self._qualified[symbol] = analysis
                    self.scan_results[symbol] = {
                        **analysis,
                        "verdict": "TREND RIDER CANDIDATE",
                    }
            except Exception as e:
                log.debug(f"Daily analysis failed for {symbol}: {e}")

        if self._qualified:
            names = ", ".join(list(self._qualified.keys())[:10])
            log.info(
                f"TREND RIDER: {len(self._qualified)} candidates from "
                f"{len(symbols_to_scan)} scanned — {names}"
            )

    def _get_scan_universe(self, market_data):
        """Build a universe of liquid stocks to scan daily bars for.
        Uses dynamic symbols (from IBKR scanner) + strategy's configured symbols."""
        universe = set(self.symbols)
        if hasattr(self, "_dynamic_symbols"):
            universe |= self._dynamic_symbols
        return list(universe)

    def _analyze_daily(self, symbol, broker):
        """Analyze daily bars for a single symbol. Returns analysis dict or None."""
        bars = broker.get_historical_bars(symbol, duration="120 D", bar_size="1 day")
        if bars is None or len(bars) < 40:
            return None

        closes = bars["close"].values.astype(float)
        highs = bars["high"].values.astype(float)
        lows = bars["low"].values.astype(float)
        volumes = bars["volume"].values.astype(float)

        current_price = closes[-1]

        # Price filter
        if current_price < self.min_price or current_price > self.max_price:
            return None

        # Volume filter (20-day average)
        avg_vol = np.mean(volumes[-20:])
        if avg_vol < self.min_avg_volume:
            return None

        # --- Consecutive green days ---
        green_days = 0
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] > closes[i - 1]:
                green_days += 1
            else:
                break

        if green_days < self.min_green_days:
            return None

        # --- 20 EMA ---
        ema20 = self._ema(closes, self.ema_period)
        if ema20 is None or current_price <= ema20[-1]:
            return None

        # --- ADX ---
        adx = self._adx(highs, lows, closes, period=14)
        if adx is None or adx < self.adx_threshold:
            return None

        # --- SuperTrend(7,3) ---
        st_line, st_direction = self._supertrend(
            highs, lows, closes,
            period=self.supertrend_period,
            multiplier=self.supertrend_mult,
        )
        if st_direction is None or st_direction[-1] != 1:
            return None  # Not bullish

        # --- ATR for stop calculation ---
        atr = self._atr(highs, lows, closes, self.atr_period)
        if atr is None or atr <= 0:
            return None

        # Yesterday's high (for breakout entry)
        yesterday_high = highs[-2] if len(highs) >= 2 else current_price

        # Weekly change (last 5 trading days)
        weekly_change = (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) >= 6 else 0

        return {
            "qualified": True,
            "price": round(current_price, 2),
            "green_days": green_days,
            "ema20": round(ema20[-1], 2),
            "adx": round(adx, 1),
            "supertrend": round(st_line[-1], 2),
            "supertrend_bullish": True,
            "atr": round(atr, 2),
            "atr_pct": round(atr / current_price * 100, 2),
            "avg_volume": int(avg_vol),
            "yesterday_high": round(yesterday_high, 2),
            "weekly_change_pct": round(weekly_change, 2),
            "stop_distance": round(atr * self.atr_stop_mult, 2),
        }

    # =========================================================================
    # Phase 2: Intraday entry triggers
    # =========================================================================

    def _check_intraday_entry(self, symbol, daily, market_data):
        """Check for an intraday entry trigger on a daily-qualified stock."""
        bars_5m = market_data.get_bars(symbol, 30)
        if bars_5m is None or len(bars_5m) < 15:
            return None

        closes_5m = bars_5m["close"].values.astype(float)
        volumes_5m = bars_5m["volume"].values.astype(float)
        current_price = closes_5m[-1]

        # Update price from live data if available
        live_price = market_data.get_price(symbol)
        if live_price and live_price > 0:
            current_price = live_price

        atr = daily["atr"]
        yesterday_high = daily["yesterday_high"]
        ema20_daily = daily["ema20"]
        supertrend = daily["supertrend"]

        # Compute 5-min 9 EMA for pullback detection
        ema9_5m = self._ema(closes_5m, 9)
        if ema9_5m is None:
            return None

        # Volume check (current bar vs 20-bar avg on 5-min)
        avg_vol_5m = np.mean(volumes_5m[-20:]) if len(volumes_5m) >= 20 else np.mean(volumes_5m)
        vol_ratio = volumes_5m[-1] / avg_vol_5m if avg_vol_5m > 0 else 0

        # --- Entry Type 1: Pullback to 9 EMA then bounce ---
        # Price dipped near 9 EMA (within 0.3%) and is now bouncing back above
        pullback_entry = False
        if len(closes_5m) >= 3:
            # Recent bar touched near 9 EMA
            dipped = any(
                abs(closes_5m[i] - ema9_5m[i]) / ema9_5m[i] < 0.003
                or closes_5m[i] < ema9_5m[i]
                for i in range(-3, -1)
            )
            # Current bar closing back above 9 EMA with some strength
            bouncing = closes_5m[-1] > ema9_5m[-1] and closes_5m[-1] > closes_5m[-2]
            pullback_entry = dipped and bouncing and vol_ratio >= 1.0

        # --- Entry Type 2: Break of yesterday's high ---
        breakout_entry = False
        if current_price > yesterday_high and vol_ratio >= 1.5:
            # Confirm: not too extended (within 1.5% of yesterday high)
            extension = (current_price - yesterday_high) / yesterday_high
            if extension < 0.015:
                breakout_entry = True

        if not pullback_entry and not breakout_entry:
            return None

        entry_type = "pullback_9ema" if pullback_entry else "breakout_yest_high"

        # --- Stop & Target ---
        stop_loss = round(current_price - (atr * self.atr_stop_mult), 2)
        # Don't let stop be above SuperTrend — that's our daily floor
        stop_loss = min(stop_loss, round(supertrend - 0.01, 2))
        # Don't let stop be above daily 20 EMA
        stop_loss = min(stop_loss, round(ema20_daily - 0.01, 2))

        # Target: 3x ATR from entry (wide — we're holding days)
        take_profit = round(current_price + (atr * 3.0), 2)

        risk_pct = (current_price - stop_loss) / current_price
        if risk_pct > 0.06:
            # Stop is too wide (>6% from entry) — skip, wait for better entry
            return None
        if risk_pct < 0.005:
            # Stop is unrealistically tight — data issue
            return None

        # Confidence scoring
        confidence = 0.5
        if daily["green_days"] >= 5:
            confidence += 0.1
        if daily["adx"] >= 35:
            confidence += 0.1
        if breakout_entry:
            confidence += 0.1
        if vol_ratio >= 2.0:
            confidence += 0.1
        confidence = min(confidence, 0.95)

        log.info(
            f"TREND RIDER SIGNAL: {entry_type.upper()} {symbol} @ ${current_price:.2f} | "
            f"{daily['green_days']} green days | ADX {daily['adx']} | "
            f"ST ${daily['supertrend']:.2f} | Stop ${stop_loss:.2f} | "
            f"Vol {vol_ratio:.1f}x"
        )

        return {
            "symbol": symbol,
            "action": "buy",
            "price": current_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "confidence": confidence,
            "strategy": "daily_trend_rider",
            "reason": (
                f"Trend rider {entry_type}: {daily['green_days']} green days, "
                f"ADX {daily['adx']}, SuperTrend ${daily['supertrend']:.2f}, "
                f"weekly +{daily['weekly_change_pct']:.1f}%"
            ),
            "max_hold_bars": 0,  # Not bar-based — uses max_hold_days
            "max_hold_days": self.max_hold_days,
            "bar_seconds": 86400,  # Daily
            "trailing_stop_pct": round(risk_pct, 4),  # Start with entry risk as trail
            "trend_rider": True,  # Flag for engine EOD exemption
            "entry_type": entry_type,
            "momentum_runner": False,
            "scalp_mode": False,
            "generated_at": datetime.now().isoformat(),
            # Daily context for the position (used by engine for daily-close exits)
            "_daily_supertrend": daily["supertrend"],
            "_daily_ema20": daily["ema20"],
            "_daily_atr": daily["atr"],
            "_daily_green_days": daily["green_days"],
        }

    # =========================================================================
    # Technical indicators (self-contained — no dependency on engine indicators
    # for daily-bar calculations since those use a different timeframe)
    # =========================================================================

    @staticmethod
    def _ema(data, period):
        """Exponential Moving Average."""
        if len(data) < period:
            return None
        ema = np.zeros_like(data, dtype=float)
        ema[:period] = np.mean(data[:period])
        mult = 2.0 / (period + 1)
        for i in range(period, len(data)):
            ema[i] = (data[i] - ema[i - 1]) * mult + ema[i - 1]
        return ema

    @staticmethod
    def _atr(highs, lows, closes, period=14):
        """Average True Range (returns latest value)."""
        if len(closes) < period + 1:
            return None
        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]),
            ),
        )
        if len(tr) < period:
            return None
        atr = np.mean(tr[-period:])
        return float(atr)

    @staticmethod
    def _adx(highs, lows, closes, period=14):
        """Average Directional Index (returns latest value)."""
        if len(closes) < period * 2 + 1:
            return None

        plus_dm = np.zeros(len(highs))
        minus_dm = np.zeros(len(lows))

        for i in range(1, len(highs)):
            up = highs[i] - highs[i - 1]
            down = lows[i - 1] - lows[i]
            plus_dm[i] = up if up > down and up > 0 else 0
            minus_dm[i] = down if down > up and down > 0 else 0

        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]),
            ),
        )
        # Pad TR to match length
        tr_full = np.concatenate([[0], tr])

        # Smoothed averages (Wilder's smoothing)
        def wilder_smooth(data, n):
            out = np.zeros_like(data)
            out[n] = np.sum(data[1 : n + 1])
            for i in range(n + 1, len(data)):
                out[i] = out[i - 1] - (out[i - 1] / n) + data[i]
            return out

        atr_smooth = wilder_smooth(tr_full, period)
        plus_di_smooth = wilder_smooth(plus_dm, period)
        minus_di_smooth = wilder_smooth(minus_dm, period)

        # Avoid division by zero
        plus_di = np.where(atr_smooth > 0, 100 * plus_di_smooth / atr_smooth, 0)
        minus_di = np.where(atr_smooth > 0, 100 * minus_di_smooth / atr_smooth, 0)

        dx_sum = plus_di + minus_di
        dx = np.where(dx_sum > 0, 100 * np.abs(plus_di - minus_di) / dx_sum, 0)

        # ADX = Wilder-smoothed DX
        adx_vals = wilder_smooth(dx, period)

        # Return latest non-zero ADX
        for val in reversed(adx_vals):
            if val > 0:
                return float(val)
        return None

    @staticmethod
    def _supertrend(highs, lows, closes, period=7, multiplier=3.0):
        """SuperTrend indicator.

        Returns:
            (st_line, direction) — st_line is the SuperTrend value array,
            direction is +1 (bullish) or -1 (bearish) per bar.
        """
        n = len(closes)
        if n < period + 1:
            return None, None

        # ATR
        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]),
            ),
        )
        # Pad
        tr = np.concatenate([[tr[0]], tr])

        atr = np.zeros(n)
        atr[period - 1] = np.mean(tr[:period])
        for i in range(period, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

        # Mid price
        hl2 = (highs + lows) / 2.0

        # Upper and lower bands
        upper_basic = hl2 + multiplier * atr
        lower_basic = hl2 - multiplier * atr

        upper_band = np.zeros(n)
        lower_band = np.zeros(n)
        supertrend = np.zeros(n)
        direction = np.zeros(n, dtype=int)

        upper_band[0] = upper_basic[0]
        lower_band[0] = lower_basic[0]
        supertrend[0] = upper_basic[0]
        direction[0] = -1

        for i in range(1, n):
            # Lower band: only move up, never down
            if lower_basic[i] > lower_band[i - 1] or closes[i - 1] < lower_band[i - 1]:
                lower_band[i] = lower_basic[i]
            else:
                lower_band[i] = lower_band[i - 1]

            # Upper band: only move down, never up
            if upper_basic[i] < upper_band[i - 1] or closes[i - 1] > upper_band[i - 1]:
                upper_band[i] = upper_basic[i]
            else:
                upper_band[i] = upper_band[i - 1]

            # Direction
            if supertrend[i - 1] == upper_band[i - 1]:
                # Was bearish
                if closes[i] > upper_band[i]:
                    direction[i] = 1  # Flip to bullish
                    supertrend[i] = lower_band[i]
                else:
                    direction[i] = -1
                    supertrend[i] = upper_band[i]
            else:
                # Was bullish
                if closes[i] < lower_band[i]:
                    direction[i] = -1  # Flip to bearish
                    supertrend[i] = upper_band[i]
                else:
                    direction[i] = 1
                    supertrend[i] = lower_band[i]

        return supertrend, direction
