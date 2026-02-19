"""
RVOL Scalp Strategy - 1-Minute Ultra-Fast Breakout Scalping

Catches quick relative volume breakouts on 1-minute bars.
Takes profit on same candle if uptick continues. True scalping:
- 1-minute bars for fast detection
- Tighter targets (1-1.5x ATR, or 0.8-2% quick take)
- 15-minute max hold
- Same-candle profit taking when momentum accelerates
- Volume acceleration detection (bar-over-bar increase)

This is the aggressive money-maker: find the RVOL spike, ride the
first 0.8-2% move, get out. Repeat 25x per day.
"""
import numpy as np
from datetime import datetime
from bot.strategies.base import BaseStrategy
from bot.utils.logger import get_logger

log = get_logger("strategy.rvol_scalp")


class RvolScalpStrategy(BaseStrategy):
    """
    1-Minute RVOL Scalp: detect volume spike, enter breakout, quick profit.

    Logic:
    1. Scan all symbols on 1-min bars for RVOL >= 1.8x
    2. Confirm breakout: 2 consecutive up bars with rising volume
    3. Enter on confirmation with tight stops (1x ATR)
    4. Quick scalp target: +0.8% or 1.5x ATR (whichever comes first)
    5. Runner target: +2% (let momentum carry if strong)
    6. Max hold: 15 minutes (if no target hit, exit)
    7. Same-candle exit: if price hits target mid-bar, flag for immediate exit
    """

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)
        self.min_rvol = config.get("min_rvol", 1.8)
        self.min_score = config.get("min_score", 50)
        self.min_price = config.get("min_price", 2.00)
        self.max_price = config.get("max_price", 800.00)
        self.min_volume = config.get("min_volume", 100000)
        self.atr_stop_mult = config.get("atr_stop_multiplier", 1.0)
        self.atr_target_mult = config.get("atr_target_multiplier", 1.5)
        self.quick_scalp_pct = config.get("quick_scalp_target_pct", 0.008)
        self.runner_pct = config.get("runner_target_pct", 0.02)
        self.max_hold_minutes = config.get("max_hold_minutes", 15)
        self.max_trades_per_day = config.get("max_trades_per_day", 25)
        self.confirm_bars = config.get("breakout_confirmation_bars", 2)
        self.momentum_accel = config.get("momentum_acceleration", True)
        self.trades_today = 0
        self.last_trade_date = None

        # Dynamic symbol list from top movers
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
        """Scan all symbols for 1-min RVOL scalp setups."""
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
                result = self._analyze_scalp(symbol, market_data)
                if result:
                    self.scan_results[symbol] = result["scan"]
                    if result.get("signal"):
                        signals.append(result["signal"])
                        self.trades_today += 1
                        if self.trades_today >= self.max_trades_per_day:
                            break
            except Exception as e:
                log.debug(f"RVOL scalp error for {symbol}: {e}")

        return signals

    def _analyze_scalp(self, symbol, market_data):
        """Analyze a single symbol for 1-min RVOL scalp setup."""
        # Try 1-min bars first, fall back to whatever is available
        bars = market_data.get_bars(symbol, 60, bar_size="1 min") if market_data else None
        if bars is None:
            # Fall back to standard bars (5-min) if 1-min not available
            bars = market_data.get_bars(symbol, 30) if market_data else None
        if bars is None or len(bars) < 15:
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

        # --- RVOL on 1-min bars ---
        # Use last 10 bars as "average" (excluding current)
        avg_vol = float(np.mean(volumes[-11:-1])) if len(volumes) > 11 else float(np.mean(volumes[:-1]))
        current_vol = float(volumes[-1])
        rvol = round(current_vol / avg_vol, 2) if avg_vol > 0 else 0

        # Volume filter
        if avg_vol < self.min_volume / 390:  # Per 1-min bar avg volume (390 min/day)
            self.scan_results[symbol] = {
                "status": "low_volume", "rvol": rvol, "price": current_price
            }
            return None

        # --- Price Action ---
        prev_close = float(closes[-2])
        change_pct = round((current_price - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0

        # ATR (shorter period for 1-min bars)
        atr = self.indicators.atr(highs, lows, closes, period=10)
        atr_pct = round(atr / current_price * 100, 2) if atr else 0

        # RSI (shorter period for scalps)
        rsi = self.indicators.rsi(closes, 9)

        # EMA trend (fast)
        ema5 = self.indicators.ema(closes, 5)
        ema13 = self.indicators.ema(closes, 13)
        trend = "BULL" if (ema5 is not None and ema13 is not None and ema5[-1] > ema13[-1]) else "BEAR"

        # Direction
        if change_pct > 0.1:
            direction = "UP"
        elif change_pct < -0.1:
            direction = "DOWN"
        else:
            direction = "FLAT"

        # --- Breakout Confirmation ---
        # Check for consecutive up bars with rising volume
        breakout_confirmed = False
        if len(closes) >= self.confirm_bars + 1:
            confirmed = True
            for i in range(1, self.confirm_bars + 1):
                idx = -i
                # Each bar must close higher than it opened
                if closes[idx] <= opens[idx]:
                    confirmed = False
                    break
                # Each bar must have higher volume than previous
                if self.momentum_accel and i > 1:
                    if volumes[idx] < volumes[idx - 1]:
                        confirmed = False
                        break
            breakout_confirmed = confirmed

        # --- Volume Acceleration ---
        # Bar-over-bar volume increase (institutional buying)
        vol_accelerating = False
        if len(volumes) >= 4:
            vol_accelerating = (
                volumes[-1] > volumes[-2] > volumes[-3]
                and volumes[-1] > avg_vol * 1.5
            )

        # --- Composite Score ---
        score = 0
        score_reasons = []

        # RVOL (max 30 pts)
        if rvol >= 4.0:
            score += 30
            score_reasons.append(f"Extreme RVOL {rvol:.1f}x")
        elif rvol >= 2.5:
            score += 25
            score_reasons.append(f"Very high RVOL {rvol:.1f}x")
        elif rvol >= 1.8:
            score += 15
            score_reasons.append(f"High RVOL {rvol:.1f}x")

        # Price direction (max 15 pts)
        if direction == "UP" and change_pct > 1.0:
            score += 15
            score_reasons.append(f"Strong move +{change_pct:.1f}%")
        elif direction == "UP" and change_pct > 0.3:
            score += 10
            score_reasons.append(f"Moving up +{change_pct:.1f}%")
        elif direction == "UP":
            score += 5

        # Breakout confirmation (max 20 pts)
        if breakout_confirmed:
            score += 20
            score_reasons.append(f"Breakout confirmed ({self.confirm_bars} bars)")

        # Volume acceleration (max 15 pts)
        if vol_accelerating:
            score += 15
            score_reasons.append("Volume accelerating bar-over-bar")

        # Trend (max 10 pts)
        if trend == "BULL":
            score += 10
            score_reasons.append("Short-term trend bullish")

        # RSI momentum (max 10 pts) - not overbought yet
        if 40 < rsi < 70:
            score += 10
            score_reasons.append(f"RSI in sweet spot ({rsi:.0f})")
        elif rsi > 75:
            score -= 5  # Overbought penalty

        # Crypto gets lower thresholds (more volatile, different volume patterns)
        is_crypto = any(symbol.upper().endswith(s) for s in ("-USD", "-USDT"))
        effective_min_rvol = self.min_rvol * 0.7 if is_crypto else self.min_rvol
        effective_min_score = int(self.min_score * 0.8) if is_crypto else self.min_score

        # Verdict
        if rvol >= effective_min_rvol and score >= effective_min_score and direction == "UP":
            verdict = "SCALP SIGNAL"
        elif rvol >= effective_min_rvol and score >= 35:
            verdict = "WARMING"
        elif rvol >= 1.5:
            verdict = "ACTIVE"
        else:
            verdict = "QUIET"

        scan_result = {
            "price": round(current_price, 2),
            "rvol": rvol,
            "current_vol": int(current_vol),
            "avg_vol": int(avg_vol),
            "change_pct": change_pct,
            "direction": direction,
            "trend": trend,
            "rsi": round(rsi, 1) if rsi else 0,
            "atr_pct": atr_pct,
            "breakout_confirmed": breakout_confirmed,
            "vol_accelerating": vol_accelerating,
            "score": score,
            "verdict": verdict,
            "reasons": score_reasons,
        }

        result = {"scan": scan_result, "signal": None}

        # Generate signal
        if verdict == "SCALP SIGNAL" and atr and atr > 0:
            stop_loss = current_price - (self.atr_stop_mult * atr)

            # Quick scalp target: small % move OR ATR-based
            quick_target = current_price * (1 + self.quick_scalp_pct)
            atr_target = current_price + (self.atr_target_mult * atr)
            take_profit = min(quick_target, atr_target)  # Whichever is closer

            # Runner target for strong momentum (wider for crypto)
            runner_mult = self.runner_pct * 2.0 if is_crypto else self.runner_pct  # 3% for crypto, 1.5% for stocks
            runner_target = current_price * (1 + runner_mult)

            targets = [
                round(take_profit, 2),      # Quick scalp exit
                round(runner_target, 2),     # Runner exit (let it go if strong)
            ]

            risk = current_price - stop_loss
            reward = take_profit - current_price
            rr_ratio = round(reward / risk, 2) if risk > 0 else 0

            # Lower R:R requirement for scalps (we win on volume of trades)
            if rr_ratio >= 1.0:
                confidence = min(1.0, score / 100)

                result["signal"] = {
                    "symbol": symbol,
                    "action": "buy",
                    "price": current_price,
                    "stop_loss": round(stop_loss, 2),
                    "take_profit": round(take_profit, 2),
                    "targets": targets,
                    "confidence": round(confidence, 2),
                    "reason": " | ".join(score_reasons[:4]),
                    "max_hold_bars": self.max_hold_minutes,  # 1 bar = 1 min
                    "bar_seconds": 60,  # 1-minute bars
                    "rvol": rvol,
                    "rr_ratio": rr_ratio,
                    "source": "rvol_scalp",
                    "scalp_mode": True,
                    "same_candle_exit": True,
                    "quick_scalp_pct": self.quick_scalp_pct,
                    "runner_pct": self.runner_pct,
                    "trailing_stop_pct": 0.005,  # Very tight 0.5% trail for scalps
                }

                self.signals_generated += 1
                log.info(
                    f"SCALP SIGNAL: {symbol} | Score: {score} | RVOL: {rvol:.1f}x | "
                    f"Change: {change_pct:+.1f}% | R:R {rr_ratio:.1f} | "
                    f"Target: ${take_profit:.2f} (+{self.quick_scalp_pct*100:.1f}%)"
                )

        return result
