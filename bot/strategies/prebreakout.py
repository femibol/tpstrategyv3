"""
Pre-Breakout Accumulation Strategy - Enter BEFORE the breakout.

Detects the accumulation phase where smart money is quietly buying:
- Bollinger Band squeeze (volatility compression)
- Volume building while price stays flat (accumulation)
- Higher lows forming inside a tight range
- MACD histogram flipping from negative to positive
- First candle breaking above the compression zone = ENTRY

This catches plays like RXT ($0.39 → $1.71, +227%) and GERN ($1.26 → $1.94, +7.8%)
by entering DURING accumulation, before the explosive move happens.

Targets are intentionally wide — these are multi-bagger setups.
"""
import numpy as np
from datetime import datetime
from bot.strategies.base import BaseStrategy
from bot.utils.logger import get_logger

log = get_logger("strategy.prebreakout")


class PreBreakoutStrategy(BaseStrategy):
    """
    Pre-Breakout Accumulation: detect compression + volume build → enter early.

    Detection Logic:
    1. Bollinger Band squeeze: band width at N-period low (volatility compressed)
    2. Volume shelf: recent volume > average volume while price range is tight
    3. Higher lows forming inside the range (accumulation pattern)
    4. MACD histogram crossing from negative to positive (momentum shift)
    5. Price breaks above upper Bollinger Band or recent consolidation high

    Entry: When compression is detected AND first breakout candle fires.
    Stop: Below the consolidation range low (below lower Bollinger Band).
    Targets: Very wide — 5x ATR initial, let runners go with trailing stop.
    """

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)
        self.min_score = config.get("min_score", 50)
        self.min_price = config.get("min_price", 0.50)
        self.max_price = config.get("max_price", 800.00)
        self.min_volume = config.get("min_volume", 100000)
        self.bb_period = config.get("bollinger_period", 20)
        self.bb_std = config.get("bollinger_std", 2.0)
        self.squeeze_lookback = config.get("squeeze_lookback", 50)
        self.squeeze_percentile = config.get("squeeze_percentile", 25)
        self.vol_build_bars = config.get("vol_build_bars", 5)
        self.vol_build_mult = config.get("vol_build_multiplier", 1.3)
        self.atr_stop_mult = config.get("atr_stop_multiplier", 2.0)
        self.atr_target_mult = config.get("atr_target_multiplier", 5.0)
        self.runner_atr_mult = config.get("runner_atr_multiplier", 10.0)
        self.max_hold_days = config.get("max_hold_days", 5)
        self.max_trades_per_day = config.get("max_trades_per_day", 8)
        self.trailing_stop_pct = config.get("trailing_stop_pct", 0.03)
        self.trades_today = 0
        self.last_trade_date = None

        # Dynamic symbols from scanner
        self._dynamic_symbols = set()

    def add_dynamic_symbols(self, symbols):
        """Add dynamically discovered symbols."""
        for sym in symbols:
            if sym and isinstance(sym, str):
                self._dynamic_symbols.add(sym.upper())

    def get_symbols(self):
        """Return combined static + dynamic symbol list."""
        return list(set(self.symbols) | self._dynamic_symbols)

    def generate_signals(self, market_data):
        """Scan all symbols for pre-breakout accumulation setups."""
        signals = []

        # Reset daily counter
        today = datetime.now().date()
        if self.last_trade_date != today:
            self.trades_today = 0
            self.last_trade_date = today

        if self.trades_today >= self.max_trades_per_day:
            return signals

        all_symbols = self.get_symbols()

        for symbol in all_symbols:
            try:
                result = self._analyze_accumulation(symbol, market_data)
                if result:
                    self.scan_results[symbol] = result["scan"]
                    if result.get("signal"):
                        signals.append(result["signal"])
                        self.trades_today += 1
                        if self.trades_today >= self.max_trades_per_day:
                            break
            except Exception as e:
                log.debug(f"Pre-breakout error for {symbol}: {e}")

        return signals

    def _analyze_accumulation(self, symbol, market_data):
        """Analyze a single symbol for pre-breakout accumulation."""
        bars = market_data.get_bars(symbol, 80) if market_data else None
        if bars is None or len(bars) < self.squeeze_lookback:
            return None

        closes = bars["close"].values
        highs = bars["high"].values
        lows = bars["low"].values
        opens = bars["open"].values
        volumes = bars["volume"].values

        current_price = float(closes[-1])
        if current_price <= 0:
            return None

        # Price filter
        if current_price < self.min_price or current_price > self.max_price:
            return None

        # Volume filter
        avg_daily_vol = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else float(np.mean(volumes))
        if avg_daily_vol < self.min_volume / 78:  # Per 5-min bar avg (390min / 5min = 78 bars)
            return None

        # --- BOLLINGER BAND SQUEEZE ---
        upper, middle, lower = self.indicators.bollinger_bands(
            closes, period=self.bb_period, std_dev=self.bb_std
        )
        if upper is None or middle is None or lower is None or middle == 0:
            return None

        band_width = (upper - lower) / middle

        # Calculate historical band widths to detect squeeze
        band_widths = []
        for i in range(self.bb_period, len(closes)):
            u, m, l = self.indicators.bollinger_bands(
                closes[:i + 1], period=self.bb_period, std_dev=self.bb_std
            )
            if u is not None and m is not None and l is not None and m > 0:
                band_widths.append((u - l) / m)

        in_squeeze = False
        squeeze_strength = 0
        if len(band_widths) >= 10:
            # Squeeze = current band width is in the bottom percentile of recent history
            threshold = np.percentile(band_widths[-self.squeeze_lookback:], self.squeeze_percentile)
            in_squeeze = band_width <= threshold
            if in_squeeze and threshold > 0:
                squeeze_strength = round(1 - (band_width / threshold), 2)

        # --- VOLUME ACCUMULATION ---
        # Recent volume should be higher than the lookback average
        # while price hasn't moved much (key insight: volume UP, price FLAT)
        recent_vol_avg = float(np.mean(volumes[-self.vol_build_bars:])) if len(volumes) > self.vol_build_bars else 0
        lookback_vol_avg = float(np.mean(volumes[-20:-self.vol_build_bars])) if len(volumes) > 20 else 0

        vol_building = False
        vol_ratio = 0
        if lookback_vol_avg > 0:
            vol_ratio = round(recent_vol_avg / lookback_vol_avg, 2)
            vol_building = vol_ratio >= self.vol_build_mult

        # Price range during volume buildup (should be tight)
        recent_range = (float(np.max(highs[-self.vol_build_bars:])) -
                        float(np.min(lows[-self.vol_build_bars:])))
        recent_range_pct = (recent_range / current_price * 100) if current_price > 0 else 100

        # Accumulation = volume building BUT price range is tight
        accumulating = vol_building and recent_range_pct < 5.0

        # --- HIGHER LOWS PATTERN ---
        higher_lows = False
        higher_lows_count = 0
        if len(lows) >= 6:
            # Check last 5 bars for progressively higher lows
            for i in range(-4, 0):
                if lows[i] > lows[i - 1]:
                    higher_lows_count += 1
            higher_lows = higher_lows_count >= 3  # At least 3 of 4 bars have higher lows

        # --- MACD MOMENTUM SHIFT ---
        macd_line, signal_line, histogram = self.indicators.macd(closes, fast=12, slow=26, signal=9)
        macd_bullish_cross = False
        macd_turning = False
        if histogram is not None and len(histogram) >= 3:
            # Just crossed above zero
            macd_bullish_cross = histogram[-1] > 0 and histogram[-2] <= 0
            # Turning positive (trending up even if still below zero)
            macd_turning = histogram[-1] > histogram[-2] > histogram[-3]

        # --- RSI ---
        rsi = self.indicators.rsi(closes, 14)

        # --- ATR ---
        atr = self.indicators.atr(highs, lows, closes, period=14)
        atr_pct = round(atr / current_price * 100, 2) if atr and atr > 0 else 0

        # --- EMA TREND ---
        ema8 = self.indicators.ema(closes, 8)
        ema21 = self.indicators.ema(closes, 21)
        ema_bullish = (ema8 is not None and ema21 is not None and
                       ema8[-1] > ema21[-1])
        ema_just_crossed = (ema8 is not None and ema21 is not None and
                            len(ema8) >= 2 and len(ema21) >= 2 and
                            ema8[-1] > ema21[-1] and ema8[-2] <= ema21[-2])

        # --- BREAKOUT CANDLE ---
        # Price breaking above the consolidation zone (upper BB or recent high)
        recent_high = float(np.max(highs[-10:-1])) if len(highs) > 10 else float(np.max(highs[:-1]))
        breaking_out = current_price > upper or current_price > recent_high
        # Strict breakout candle: strong green candle with 1.5x volume surge
        # Body must be at least 60% of total range (not a doji/spinning top)
        candle_body = abs(closes[-1] - opens[-1])
        candle_range = highs[-1] - lows[-1]
        body_ratio = candle_body / candle_range if candle_range > 0 else 0
        breakout_candle = (closes[-1] > opens[-1] and
                           body_ratio >= 0.6 and
                           volumes[-1] > recent_vol_avg * 1.5)

        # --- ADX (trend strength building) ---
        adx = self.indicators.adx(highs, lows, closes, period=14)
        adx_rising = False
        if adx is not None:
            # We want ADX that is still low but starting to rise (trend beginning)
            adx_rising = 15 < adx < 35  # Sweet spot: trend is forming but not exhausted

        # --- PRICE ABOVE VWAP ---
        vwap_vals = self.indicators.vwap(highs, lows, closes, volumes)
        above_vwap = vwap_vals is not None and current_price > vwap_vals[-1]

        # Crypto detection for adjusted thresholds
        is_crypto = any(symbol.upper().endswith(s) for s in ("-USD", "-USDT"))

        # ============ COMPOSITE SCORE ============
        score = 0
        score_reasons = []

        # Bollinger squeeze (max 25 pts) - THE key signal
        if in_squeeze:
            pts = 15 + int(squeeze_strength * 10)
            score += min(pts, 25)
            score_reasons.append(f"BB squeeze ({squeeze_strength:.0%} tight)")

        # Volume accumulation (max 20 pts)
        if accumulating:
            score += 20
            score_reasons.append(f"Volume accumulating {vol_ratio:.1f}x")
        elif vol_building:
            score += 10
            score_reasons.append(f"Volume building {vol_ratio:.1f}x")

        # Higher lows (max 15 pts)
        if higher_lows:
            score += 15
            score_reasons.append(f"Higher lows ({higher_lows_count}/4)")

        # MACD momentum shift (max 15 pts)
        if macd_bullish_cross:
            score += 15
            score_reasons.append("MACD crossed bullish")
        elif macd_turning:
            score += 10
            score_reasons.append("MACD turning up")

        # EMA alignment (max 10 pts)
        if ema_just_crossed:
            score += 10
            score_reasons.append("EMA 8/21 cross UP")
        elif ema_bullish:
            score += 5
            score_reasons.append("EMA trend bullish")

        # Breakout candle (max 10 pts) - confirmation
        if breaking_out and breakout_candle:
            score += 10
            score_reasons.append("Breakout candle!")
        elif breaking_out:
            score += 5
            score_reasons.append("Price at resistance")

        # RSI sweet spot (max 5 pts) — not overbought
        if 40 <= rsi <= 65:
            score += 5
            score_reasons.append(f"RSI {rsi:.0f} (room to run)")
        elif rsi > 75:
            score -= 10  # Overbought penalty
            score_reasons.append(f"RSI {rsi:.0f} OVERBOUGHT")

        # ADX trend forming (max 5 pts)
        if adx_rising:
            score += 5
            score_reasons.append(f"ADX {adx:.0f} (trend forming)")

        # Above VWAP (max 5 pts)
        if above_vwap:
            score += 5
            score_reasons.append("Above VWAP")

        # Crypto adjustment (crypto is more volatile — lower bar)
        effective_min_score = int(self.min_score * 0.8) if is_crypto else self.min_score

        # --- Verdict ---
        has_breakout_trigger = breaking_out and breakout_candle
        has_accumulation = in_squeeze or accumulating or higher_lows

        if score >= effective_min_score and has_breakout_trigger and has_accumulation:
            verdict = "PRE-BREAKOUT ENTRY"
        elif score >= effective_min_score and has_accumulation:
            verdict = "ACCUMULATING"
        elif score >= 35:
            verdict = "BUILDING"
        else:
            verdict = "QUIET"

        scan_result = {
            "price": round(current_price, 2),
            "band_width": round(band_width * 100, 2),
            "in_squeeze": in_squeeze,
            "squeeze_strength": squeeze_strength,
            "vol_ratio": vol_ratio,
            "vol_building": vol_building,
            "accumulating": accumulating,
            "recent_range_pct": round(recent_range_pct, 2),
            "higher_lows": higher_lows,
            "macd_bullish": macd_bullish_cross,
            "macd_turning": macd_turning,
            "ema_bullish": ema_bullish,
            "breaking_out": breaking_out,
            "rsi": round(rsi, 1) if rsi else 0,
            "adx": round(adx, 1) if adx else 0,
            "atr_pct": atr_pct,
            "score": score,
            "verdict": verdict,
            "reasons": score_reasons,
        }

        result = {"scan": scan_result, "signal": None}

        # --- Generate entry signal ---
        if verdict == "PRE-BREAKOUT ENTRY" and atr and atr > 0:
            # Stop below the consolidation range (lower BB or recent swing low)
            consolidation_low = float(np.min(lows[-10:]))
            bb_stop = lower - (0.5 * atr)  # Below lower BB with buffer
            stop_loss = min(consolidation_low, bb_stop)

            # Wide targets — these are runner plays
            take_profit = current_price + (self.atr_target_mult * atr)
            runner_target = current_price + (self.runner_atr_mult * atr)

            # Even wider for crypto
            if is_crypto:
                take_profit = current_price + (self.atr_target_mult * 1.5 * atr)
                runner_target = current_price + (self.runner_atr_mult * 1.5 * atr)

            targets = [
                round(current_price + (2.0 * atr), 2),    # Quick partial at 2x ATR
                round(take_profit, 2),                      # Main target at 5x ATR
                round(runner_target, 2),                    # Runner at 10x ATR
            ]

            risk = current_price - stop_loss
            reward = take_profit - current_price
            rr_ratio = round(reward / risk, 2) if risk > 0 else 0

            if rr_ratio >= 2.0:
                confidence = min(1.0, score / 100)

                # Wider trailing stop for breakout plays
                trail_pct = self.trailing_stop_pct
                if is_crypto:
                    trail_pct = self.trailing_stop_pct * 1.5

                result["signal"] = {
                    "symbol": symbol,
                    "action": "buy",
                    "price": current_price,
                    "stop_loss": round(stop_loss, 2),
                    "take_profit": round(take_profit, 2),
                    "targets": targets,
                    "confidence": round(confidence, 2),
                    "reason": " | ".join(score_reasons[:5]),
                    "max_hold_bars": 0,  # Use day-based hold, not bar-based
                    "max_hold_days": self.max_hold_days,
                    "bar_seconds": 300,  # 5-min bars
                    "rr_ratio": rr_ratio,
                    "source": "prebreakout",
                    "trailing_stop_pct": trail_pct,
                    "breakout_play": True,
                    "runner_mode": True,  # Signal engine to use wide runner management
                }

                self.signals_generated += 1
                log.info(
                    f"PRE-BREAKOUT: {symbol} | Score: {score} | "
                    f"Squeeze: {in_squeeze} | Vol: {vol_ratio:.1f}x | "
                    f"R:R {rr_ratio:.1f} | Target: ${take_profit:.2f}"
                )

        return result
