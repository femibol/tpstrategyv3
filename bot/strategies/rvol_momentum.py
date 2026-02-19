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

    Logic:
    1. Scan all symbols (static + dynamically discovered) for RVOL >= 2x
    2. Score using composite: RVOL weight + price direction + trend + MACD + RSI + gap
    3. Generate BUY signals for top scorers (score >= 60)
    4. Use ATR-based stops (tight: 1.5x ATR) and targets (3x ATR)
    5. Max holding period: 2 hours (momentum plays don't hold forever)
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
        self.trades_today = 0
        self.last_trade_date = None

        # Dynamic symbol list - gets augmented by top movers
        self._dynamic_symbols = set()

    def add_dynamic_symbols(self, symbols):
        """Add dynamically discovered symbols (from top movers, screeners)."""
        for sym in symbols:
            if sym and isinstance(sym, str):
                self._dynamic_symbols.add(sym.upper())

    def get_symbols(self):
        """Return combined static + dynamic symbol list."""
        return list(set(self.symbols) | self._dynamic_symbols)

    def generate_signals(self, market_data):
        """Scan all symbols for RVOL setups and generate trading signals."""
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
        """Analyze a single symbol for RVOL momentum setup."""
        bars = market_data.get_bars(symbol, 60) if market_data else None
        if bars is None or len(bars) < 25:
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

        # --- RVOL ---
        avg_vol_20 = float(np.mean(volumes[-21:-1])) if len(volumes) > 21 else float(np.mean(volumes[:-1]))
        current_vol = float(volumes[-1])
        rvol = round(current_vol / avg_vol_20, 2) if avg_vol_20 > 0 else 0

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

        # Verdict
        if rvol >= self.min_rvol and score >= self.min_score:
            verdict = "RVOL BUY SIGNAL"
        elif rvol >= self.min_rvol and score >= 40:
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
            take_profit = current_price + (self.atr_target_mult * atr)

            # Multi-target exits for momentum
            targets = [
                current_price + (1.5 * atr),   # Quick scalp target
                current_price + (3.0 * atr),    # Main target
                current_price + (5.0 * atr),    # Runner target (let it ride)
            ]

            risk = current_price - stop_loss
            reward = take_profit - current_price
            rr_ratio = round(reward / risk, 2) if risk > 0 else 0

            if rr_ratio >= 1.5:  # Minimum 1.5:1 R/R
                confidence = min(1.0, score / 100)

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
