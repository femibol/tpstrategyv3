"""
RVOL Momentum Strategy - Trade Ideas Money Machine Style

Auto-trades stocks with unusual relative volume (RVOL).
Dynamically discovers runners from top movers, not just a static watchlist.

Key features:
- Scans for RVOL >= 2x (abnormal volume = institutional interest)
- Dynamically adds top movers / runners to scan list
- Money Machine composite score: RVOL + trend + momentum + gap
- Tight stops (momentum plays = fast in, fast out)
- Avoids low-float traps with price/volume filters
- SNAPSHOT FAST PATH: generates signals from Polygon snapshot data when bars
  aren't available (catches today's top gainers INSTANTLY like the real
  Trade Ideas Money Machine — no historical bars needed)
- TIME-OF-DAY RVOL NORMALIZATION: adjusts RVOL for the time of day
  (early morning volume is naturally lower, so raw RVOL is inflated)
- Long-only
"""
import numpy as np
from datetime import datetime
from bot.strategies.base import BaseStrategy
from bot.utils.logger import get_logger

log = get_logger("strategy.rvol_momentum")


class RvolMomentumStrategy(BaseStrategy):
    """
    Money Machine-style RVOL trading.

    Two analysis paths:
    A. FULL PATH (bars available): RSI, EMA, MACD, ATR + bar-level RVOL
    B. SNAPSHOT FAST PATH (no bars): Uses Polygon daily snapshot data
       (price, change_pct, daily RVOL, volume, gap) to generate signals
       for newly-discovered top gainers immediately.

    The fast path is what the real Trade Ideas Money Machine does — it scans
    ALL 8,000+ stocks via real-time snapshot and instantly trades the top
    momentum plays without waiting for historical bar data.
    """

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)
        self.min_rvol = config.get("min_rvol", 2.0)
        self.min_score = config.get("min_score", 60)
        self.min_price = config.get("min_price", 2.00)
        self.max_price = config.get("max_price", 500.00)
        self.min_volume = config.get("min_volume", 200000)
        self.atr_stop_mult = config.get("atr_stop_multiplier", 1.5)
        self.atr_target_mult = config.get("atr_target_multiplier", 3.0)
        self.max_hold_minutes = config.get("max_hold_minutes", 120)
        self.max_trades_per_day = config.get("max_trades_per_day", 12)
        self.max_float = config.get("max_float", 20_000_000)  # 20M max float (low float = explosive)
        self.prefer_low_float = config.get("prefer_low_float", True)
        self.trades_today = 0
        self.last_trade_date = None

        # Time-of-day volume curve: % of daily volume typically traded by each 30-min interval
        # Based on U.S. equity intraday volume distribution (U-shaped curve)
        # Key: hour*100 + minute_bucket → cumulative % of daily volume by that time
        self._tod_volume_curve = {
            930: 0.06, 1000: 0.15, 1030: 0.22, 1100: 0.28,
            1130: 0.33, 1200: 0.38, 1230: 0.43, 1300: 0.48,
            1330: 0.53, 1400: 0.58, 1430: 0.64, 1500: 0.72,
            1530: 0.85, 1600: 1.00,
        }

        # Dynamic symbol list - gets augmented by top movers
        self._dynamic_symbols = set()

        # Snapshot data from Polygon (fed by engine)
        # {symbol: {price, change_pct, volume, avg_volume, rvol, gap_pct, open}}
        self._snapshot_data = {}

        # Track which symbols got fast-path signals (for logging/dashboard)
        self._fast_path_signals = set()

    def add_dynamic_symbols(self, symbols):
        """Add dynamically discovered symbols (from top movers, screeners)."""
        for sym in symbols:
            if sym and isinstance(sym, str):
                self._dynamic_symbols.add(sym.upper())

    def feed_snapshot_data(self, snapshot_entries):
        """Feed Polygon snapshot data for fast-path analysis.

        Called by the engine with top movers/runners from Polygon's
        full-market snapshot. This data includes daily RVOL, change%,
        volume — everything needed for instant signal generation without bars.

        Args:
            snapshot_entries: list of dicts from Polygon scanner, each with:
                symbol, price, change_pct, volume, avg_volume, rvol, gap_pct, open
        """
        for entry in snapshot_entries:
            sym = entry.get("symbol", "")
            if sym:
                self._snapshot_data[sym.upper()] = entry

    def get_symbols(self):
        """Return combined static + dynamic symbol list."""
        return list(set(self.symbols) | self._dynamic_symbols)

    def _normalize_rvol_for_time(self, raw_rvol, volume, avg_volume):
        """Normalize RVOL for time of day.

        Problem: At 10 AM, a stock may have traded 200K shares vs a 1M daily avg.
        Raw RVOL = 200K/1M = 0.2x — looks dead. But by 10 AM, only ~15% of
        daily volume has typically traded. So normalized RVOL = 0.2/0.15 = 1.33x.

        Conversely, at 9:35 AM, a stock with 100K volume vs 500K avg looks like
        0.2x raw, but only 6% of volume should have traded → 0.2/0.06 = 3.3x.
        That's the stock that's really running.

        Returns normalized RVOL that accounts for how much of the day has passed.
        """
        now = datetime.now()
        current_time = now.hour * 100 + now.minute

        # Pre-market: no normalization (different volume profile)
        if current_time < 930:
            return raw_rvol

        # Find the expected cumulative volume fraction for current time
        expected_fraction = 1.0  # default: end of day
        for time_key in sorted(self._tod_volume_curve.keys()):
            if current_time <= time_key:
                expected_fraction = self._tod_volume_curve[time_key]
                break

        if expected_fraction <= 0:
            return raw_rvol

        # Normalized RVOL = (current_volume / avg_daily_volume) / expected_fraction
        if avg_volume > 0:
            normalized = (volume / avg_volume) / expected_fraction
            return round(normalized, 2)

        return raw_rvol

    def generate_signals(self, market_data):
        """Scan all symbols for RVOL setups and generate trading signals.

        Uses two paths:
        1. Full analysis (bars available) — technical indicators + bar RVOL
        2. Snapshot fast path (no bars) — daily RVOL + price action from Polygon
        """
        signals = []

        # Reset daily counter
        today = datetime.now().date()
        if self.last_trade_date != today:
            self.trades_today = 0
            self.last_trade_date = today
            self._fast_path_signals.clear()

        if self.trades_today >= self.max_trades_per_day:
            return signals

        all_symbols = self.get_symbols()

        # Also include any snapshot symbols not yet in the scan list
        snapshot_only = set(self._snapshot_data.keys()) - set(all_symbols)
        all_symbols.extend(list(snapshot_only))

        for symbol in all_symbols:
            try:
                result = self._analyze_symbol(symbol, market_data)
                if result:
                    self.scan_results[symbol] = result["scan"]
                    if result.get("signal"):
                        signals.append(result["signal"])
                        self.trades_today += 1
                        if self.trades_today >= self.max_trades_per_day:
                            break
            except Exception as e:
                log.debug(f"RVOL analysis error for {symbol}: {e}")

        return signals

    def _analyze_symbol(self, symbol, market_data):
        """Analyze a single symbol for RVOL momentum setup.

        Falls back to snapshot fast path when bars aren't available.
        """
        bars = market_data.get_bars(symbol, 60) if market_data else None
        if bars is None or len(bars) < 25:
            # FAST PATH: use Polygon snapshot data if available
            snap = self._snapshot_data.get(symbol)
            if snap:
                return self._analyze_from_snapshot(symbol, snap)
            return None

        closes = bars["close"].values
        volumes = bars["volume"].values
        highs = bars["high"].values
        lows = bars["low"].values
        opens = bars["open"].values

        current_price = float(closes[-1])
        if current_price <= 0:
            return None

        # Price filter
        if current_price < self.min_price or current_price > self.max_price:
            return None

        # --- RVOL (time-of-day normalized) ---
        avg_vol_20 = float(np.mean(volumes[-21:-1])) if len(volumes) > 21 else float(np.mean(volumes[:-1]))
        current_vol = float(volumes[-1])
        raw_rvol = round(current_vol / avg_vol_20, 2) if avg_vol_20 > 0 else 0
        # Normalize bar-level RVOL: at 10 AM, volume is naturally lower so raw RVOL is inflated
        # Use cumulative volume for better normalization
        total_today_vol = float(np.sum(volumes[-min(78, len(volumes)):]))  # Approx today's bars
        total_avg_daily = avg_vol_20 * 78  # Approx full day average
        rvol = self._normalize_rvol_for_time(raw_rvol, total_today_vol, total_avg_daily)

        # Volume filter
        if avg_vol_20 < self.min_volume / 20:  # Per-bar avg volume
            self.scan_results[symbol] = {
                "status": "low_volume", "rvol": rvol, "price": current_price
            }
            return None

        # --- Price Action ---
        prev_close = float(closes[-2])
        gap_pct = round((opens[-1] - prev_close) / prev_close * 100, 2)
        change_pct = round((current_price - prev_close) / prev_close * 100, 2)
        day_range = float(highs[-1] - lows[-1])
        range_pct = round(day_range / current_price * 100, 2)

        # ATR
        atr = self.indicators.atr(highs, lows, closes, period=14)
        atr_pct = round(atr / current_price * 100, 2) if atr else 0

        # RSI
        rsi = self.indicators.rsi(closes, 14)

        # EMA trend
        ema9 = self.indicators.ema(closes, 9)
        ema20 = self.indicators.ema(closes, 20)
        trend = "BULL" if (ema9 is not None and ema20 is not None and ema9[-1] > ema20[-1]) else "BEAR"

        # MACD
        macd_line, signal_line, histogram = self.indicators.macd(closes)
        macd_bullish = histogram is not None and len(histogram) > 0 and histogram[-1] > 0

        # Direction
        if change_pct > 0.3:
            direction = "UP"
        elif change_pct < -0.3:
            direction = "DOWN"
        else:
            direction = "FLAT"

        # --- Money Machine Composite Score ---
        score = 0
        score_reasons = []

        # RVOL component (max 35 pts)
        if rvol >= 5.0:
            score += 35
            score_reasons.append(f"Extreme RVOL {rvol:.1f}x")
        elif rvol >= 3.0:
            score += 30
            score_reasons.append(f"Very high RVOL {rvol:.1f}x")
        elif rvol >= 2.0:
            score += 20
            score_reasons.append(f"High RVOL {rvol:.1f}x")
        elif rvol >= 1.5:
            score += 10
            score_reasons.append(f"Elevated RVOL {rvol:.1f}x")

        # Price direction (max 20 pts)
        if direction == "UP" and change_pct > 3.0:
            score += 20
            score_reasons.append(f"Strong move +{change_pct:.1f}%")
        elif direction == "UP" and change_pct > 1.0:
            score += 15
            score_reasons.append(f"Moving up +{change_pct:.1f}%")
        elif direction == "UP":
            score += 10
            score_reasons.append(f"Trending up +{change_pct:.1f}%")

        # Trend alignment (max 15 pts)
        if trend == "BULL":
            score += 15
            score_reasons.append("EMA trend bullish")

        # MACD momentum (max 10 pts)
        if macd_bullish:
            score += 10
            score_reasons.append("MACD histogram positive")

        # RSI (max 15 pts)
        if 30 < rsi < 65:
            score += 10
            score_reasons.append(f"RSI healthy ({rsi:.0f})")
        elif rsi < 35:
            score += 15
            score_reasons.append(f"RSI oversold bounce ({rsi:.0f})")
        elif rsi > 75:
            score -= 5  # Penalty: overbought

        # Gap (max 10 pts)
        if gap_pct > 3.0:
            score += 10
            score_reasons.append(f"Gap up +{gap_pct:.1f}%")
        elif gap_pct > 1.0:
            score += 5
            score_reasons.append(f"Small gap +{gap_pct:.1f}%")

        # Range expansion bonus
        if range_pct > atr_pct * 1.5 and atr_pct > 0:
            score += 5
            score_reasons.append("Range expansion")

        # Crypto gets lower thresholds (more volatile, volume patterns differ)
        is_crypto = any(symbol.upper().endswith(s) for s in ("-USD", "-USDT"))
        effective_min_rvol = self.min_rvol * 0.7 if is_crypto else self.min_rvol  # 1.26x for crypto vs 1.8x
        effective_min_score = int(self.min_score * 0.8) if is_crypto else self.min_score  # 40 for crypto vs 50

        # Breakout bonus: big intraday move deserves extra score
        if change_pct >= 5.0:
            score += 15
            score_reasons.append(f"Breakout +{change_pct:.1f}%")
        elif change_pct >= 3.0:
            score += 10
            score_reasons.append(f"Strong breakout +{change_pct:.1f}%")

        # Verdict
        if rvol >= effective_min_rvol and score >= effective_min_score:
            verdict = "RVOL BUY SIGNAL"
        elif rvol >= effective_min_rvol and score >= 40:
            verdict = "RVOL ACTIVE"
        elif rvol >= 1.5:
            verdict = "WARMING"
        else:
            verdict = "QUIET"

        scan_result = {
            "price": round(current_price, 2),
            "rvol": rvol,
            "current_vol": int(current_vol),
            "avg_vol": int(avg_vol_20),
            "change_pct": change_pct,
            "gap_pct": gap_pct,
            "range_pct": range_pct,
            "direction": direction,
            "trend": trend,
            "rsi": round(rsi, 1),
            "atr_pct": atr_pct,
            "macd_bullish": macd_bullish,
            "score": score,
            "verdict": verdict,
            "reasons": score_reasons,
        }

        result = {"scan": scan_result, "signal": None}

        # Generate signal if score qualifies
        if verdict == "RVOL BUY SIGNAL" and direction == "UP" and atr and atr > 0:
            stop_loss = current_price - (self.atr_stop_mult * atr)

            # Breakout stocks get wider targets to capture the full move
            is_breakout = change_pct >= 3.0
            target_mult = self.atr_target_mult * 1.5 if is_breakout else self.atr_target_mult
            take_profit = current_price + (target_mult * atr)

            # Multi-target exits for momentum
            targets = [
                current_price + (1.5 * atr),    # Quick scalp target
                current_price + (3.0 * atr),    # Main target
                current_price + (6.0 * atr),    # Runner target (wider for breakouts)
            ]

            risk = current_price - stop_loss
            reward = take_profit - current_price
            rr_ratio = round(reward / risk, 2) if risk > 0 else 0

            if rr_ratio >= 1.5:  # Minimum 1.5:1 R/R
                confidence = min(1.0, score / 100)

                # Breakout stocks get day-based hold instead of bar-based
                hold_days = 2 if is_breakout else 0  # 2 days for breakouts, 0 = use bar limit

                result["signal"] = {
                    "symbol": symbol,
                    "action": "buy",
                    "price": current_price,
                    "stop_loss": round(stop_loss, 2),
                    "take_profit": round(take_profit, 2),
                    "targets": [round(t, 2) for t in targets],
                    "confidence": round(confidence, 2),
                    "reason": " | ".join(score_reasons[:4]),
                    "max_hold_bars": int(self.max_hold_minutes / 5),  # 5-min bars
                    "max_hold_days": hold_days,
                    "bar_seconds": 300,
                    "rvol": rvol,
                    "rr_ratio": rr_ratio,
                    "source": "rvol_momentum",
                }

                self.signals_generated += 1
                log.info(
                    f"RVOL SIGNAL: {symbol} | Score: {score} | RVOL: {rvol:.1f}x | "
                    f"Change: {change_pct:+.1f}% | R:R {rr_ratio:.1f}"
                )

        return result

    # =========================================================================
    # SNAPSHOT FAST PATH — Trade Ideas Money Machine style
    # =========================================================================

    def _analyze_from_snapshot(self, symbol, snap):
        """Analyze a symbol using Polygon daily snapshot data (no bars needed).

        This is the Money Machine fast path — generates signals from the
        full-market snapshot when historical bars aren't cached yet.

        Uses daily RVOL (today's volume / avg volume), price change %,
        gap %, and volume to score and generate instant signals for
        today's top gainers.

        Args:
            symbol: Ticker symbol
            snap: Dict with keys: price, change_pct, volume, avg_volume, rvol,
                  gap_pct, open, prev_close
        """
        price = snap.get("price", 0)
        if price <= 0:
            return None

        # Price filter
        if price < self.min_price or price > self.max_price:
            return None

        change_pct = snap.get("change_pct", 0)
        volume = snap.get("volume", 0)
        avg_volume = snap.get("avg_volume", 1)
        rvol = snap.get("rvol", 0)
        gap_pct = snap.get("gap_pct", 0)

        # Volume filter (daily volume)
        if volume < self.min_volume:
            return None

        # Float filter — low float stocks move harder
        float_shares = snap.get("float_shares", 0)
        vol_to_float = 0
        if float_shares > 0:
            vol_to_float = round(volume / float_shares, 2)
            # Filter out high float stocks (too sluggish for momentum)
            if self.prefer_low_float and float_shares > self.max_float * 5:
                return None  # 100M+ float = too heavy, skip

        # Recalculate RVOL from daily data if not provided
        if rvol <= 0 and avg_volume > 0:
            rvol = round(volume / avg_volume, 1)

        # Normalize RVOL for time of day (snapshot uses daily volume totals)
        rvol = self._normalize_rvol_for_time(rvol, volume, avg_volume)

        # Direction from daily change
        if change_pct > 0.3:
            direction = "UP"
        elif change_pct < -0.3:
            direction = "DOWN"
        else:
            direction = "FLAT"

        # --- Money Machine Composite Score (snapshot version) ---
        score = 0
        score_reasons = []

        # RVOL component (max 35 pts) — using daily RVOL
        if rvol >= 5.0:
            score += 35
            score_reasons.append(f"Extreme RVOL {rvol:.1f}x")
        elif rvol >= 3.0:
            score += 30
            score_reasons.append(f"Very high RVOL {rvol:.1f}x")
        elif rvol >= 2.0:
            score += 20
            score_reasons.append(f"High RVOL {rvol:.1f}x")
        elif rvol >= 1.5:
            score += 10
            score_reasons.append(f"Elevated RVOL {rvol:.1f}x")

        # Price direction (max 20 pts)
        if direction == "UP" and change_pct > 3.0:
            score += 20
            score_reasons.append(f"Strong move +{change_pct:.1f}%")
        elif direction == "UP" and change_pct > 1.0:
            score += 15
            score_reasons.append(f"Moving up +{change_pct:.1f}%")
        elif direction == "UP":
            score += 10
            score_reasons.append(f"Trending up +{change_pct:.1f}%")

        # Gap (max 10 pts)
        if gap_pct > 3.0:
            score += 10
            score_reasons.append(f"Gap up +{gap_pct:.1f}%")
        elif gap_pct > 1.0:
            score += 5
            score_reasons.append(f"Small gap +{gap_pct:.1f}%")

        # Breakout bonus: big intraday move
        if change_pct >= 5.0:
            score += 15
            score_reasons.append(f"Breakout +{change_pct:.1f}%")
        elif change_pct >= 3.0:
            score += 10
            score_reasons.append(f"Strong breakout +{change_pct:.1f}%")

        # Volume surge bonus (high absolute volume = institutional flow)
        if volume >= 1_000_000:
            score += 10
            score_reasons.append(f"High volume {volume/1e6:.1f}M")
        elif volume >= 500_000:
            score += 5
            score_reasons.append(f"Good volume {volume/1e3:.0f}K")

        # Float scoring (max 15 pts) — low float = explosive moves
        if float_shares > 0:
            if float_shares <= 5_000_000:
                score += 15
                score_reasons.append(f"Ultra low float {float_shares/1e6:.1f}M")
            elif float_shares <= 10_000_000:
                score += 12
                score_reasons.append(f"Low float {float_shares/1e6:.1f}M")
            elif float_shares <= 20_000_000:
                score += 8
                score_reasons.append(f"Small float {float_shares/1e6:.1f}M")

            # Volume-to-float ratio bonus (max 10 pts)
            if vol_to_float >= 2.0:
                score += 10
                score_reasons.append(f"Float traded {vol_to_float:.1f}x")
            elif vol_to_float >= 1.0:
                score += 7
                score_reasons.append(f"Float turning over {vol_to_float:.1f}x")
            elif vol_to_float >= 0.5:
                score += 3
                score_reasons.append(f"Active float rotation {vol_to_float:.1f}x")

        # Snapshot doesn't have RSI/EMA/MACD — award moderate points for
        # being a top gainer (the fact that Polygon flagged it as a mover
        # is already strong momentum confirmation)
        if direction == "UP" and rvol >= 2.0:
            score += 10
            score_reasons.append("Snapshot momentum confirmed")

        # Crypto adjustment
        is_crypto = any(symbol.upper().endswith(s) for s in ("-USD", "-USDT"))
        effective_min_rvol = self.min_rvol * 0.7 if is_crypto else self.min_rvol
        effective_min_score = int(self.min_score * 0.8) if is_crypto else self.min_score

        # Verdict
        if rvol >= effective_min_rvol and score >= effective_min_score:
            verdict = "RVOL BUY SIGNAL"
        elif rvol >= effective_min_rvol and score >= 40:
            verdict = "RVOL ACTIVE"
        elif rvol >= 1.5:
            verdict = "WARMING"
        else:
            verdict = "QUIET"

        scan_result = {
            "price": round(price, 2),
            "rvol": rvol,
            "current_vol": int(volume),
            "avg_vol": int(avg_volume),
            "change_pct": round(change_pct, 2),
            "gap_pct": round(gap_pct, 2),
            "range_pct": 0,
            "direction": direction,
            "trend": "BULL" if direction == "UP" else "BEAR",
            "rsi": 50,  # Unknown from snapshot — neutral
            "atr_pct": 0,
            "macd_bullish": direction == "UP",
            "score": score,
            "verdict": verdict,
            "reasons": score_reasons,
            "float_shares": float_shares,
            "vol_to_float": vol_to_float,
            "fast_path": True,  # Flag for dashboard to show this is snapshot-based
        }

        result = {"scan": scan_result, "signal": None}

        # Generate signal if score qualifies
        if verdict == "RVOL BUY SIGNAL" and direction == "UP":
            # Estimate ATR from change_pct (no bars available)
            # For a stock up X%, ATR is roughly price * X/100 * 0.7
            est_atr = price * abs(change_pct) / 100 * 0.7
            if est_atr <= 0:
                est_atr = price * 0.03  # Fallback: 3% of price

            stop_loss = price - (self.atr_stop_mult * est_atr)

            is_breakout = change_pct >= 3.0
            target_mult = self.atr_target_mult * 1.5 if is_breakout else self.atr_target_mult
            take_profit = price + (target_mult * est_atr)

            targets = [
                price + (1.5 * est_atr),
                price + (3.0 * est_atr),
                price + (6.0 * est_atr),
            ]

            risk = price - stop_loss
            reward = take_profit - price
            rr_ratio = round(reward / risk, 2) if risk > 0 else 0

            if rr_ratio >= 1.5:
                confidence = min(1.0, score / 100)
                hold_days = 2 if is_breakout else 0

                result["signal"] = {
                    "symbol": symbol,
                    "action": "buy",
                    "price": price,
                    "stop_loss": round(stop_loss, 2),
                    "take_profit": round(take_profit, 2),
                    "targets": [round(t, 2) for t in targets],
                    "confidence": round(confidence, 2),
                    "reason": "[FAST] " + " | ".join(score_reasons[:4]),
                    "max_hold_bars": int(self.max_hold_minutes / 5),
                    "max_hold_days": hold_days,
                    "bar_seconds": 300,
                    "rvol": rvol,
                    "rr_ratio": rr_ratio,
                    "source": "rvol_momentum",
                    "fast_path": True,
                }

                self._fast_path_signals.add(symbol)
                self.signals_generated += 1
                log.info(
                    f"RVOL FAST SIGNAL: {symbol} | Score: {score} | RVOL: {rvol:.1f}x | "
                    f"Change: {change_pct:+.1f}% | R:R {rr_ratio:.1f} | "
                    f"Vol: {volume/1e3:.0f}K [SNAPSHOT - no bars needed]"
                )

        return result
