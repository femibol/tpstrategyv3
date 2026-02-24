"""
Pre-Market Gap Scanner Strategy
Based on the proven approach: find top pre-market gainers with extreme volume,
wait for the first pullback, then enter on the break of structure.

Core Logic:
1. Scan for stocks gapping up 5%+ pre-market
2. Filter for extreme relative volume (10x+ vs 90-day avg)
3. Wait for first pullback after initial gap move
4. Enter when price reclaims the pullback high (break of structure)
5. Tight stop below pullback low, target at pre-market high + extension

Best window: 8:00-9:30 AM ET (pre-market momentum peaks here)
Works on: Low-to-mid float stocks with catalyst (earnings, news, upgrades)
"""
import numpy as np
from datetime import datetime
from bot.strategies.base import BaseStrategy
from bot.utils.logger import get_logger

log = get_logger("strategy.premarket_gap")


class PreMarketGapStrategy(BaseStrategy):
    """
    Pre-market gap scanner: finds extreme gap-ups with massive volume,
    waits for pullback, enters on structure break.
    """

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)
        self.min_gap_pct = config.get("min_gap_pct", 0.05)         # 5% minimum gap
        self.min_rvol = config.get("min_rvol", 10.0)               # 10x relative volume
        self.min_price = config.get("min_price", 1.00)
        self.max_price = config.get("max_price", 50.00)            # Focus on cheaper stocks (big % moves)
        self.min_volume = config.get("min_volume", 50000)          # Pre-market vol filter
        self.pullback_min_pct = config.get("pullback_min_pct", 0.02)  # Min 2% pullback
        self.pullback_max_pct = config.get("pullback_max_pct", 0.50)  # Max 50% retracement
        self.atr_stop_mult = config.get("atr_stop_multiplier", 1.2)   # Tight stop
        self.atr_target_mult = config.get("atr_target_multiplier", 3.0)
        self.runner_atr_mult = config.get("runner_atr_multiplier", 8.0)
        self.max_hold_minutes = config.get("max_hold_minutes", 60)    # 1 hour max
        self.max_trades_per_day = config.get("max_trades_per_day", 6)
        self.max_candidates = config.get("max_candidates", 3)        # Top 3 only
        self.start_hour = config.get("start_hour", 6)               # 6 AM ET
        self.end_hour = config.get("end_hour", 10)                  # 10 AM ET (catch open push)
        self.trailing_stop_pct = config.get("trailing_stop_pct", 0.025)  # 2.5% trail
        # Post-open dead zone: avoid the first N minutes after market open (9:30)
        # Gap stocks whipsaw violently in the first few minutes — wait for direction
        self.open_dead_zone_minutes = config.get("open_dead_zone_minutes", 5)

        self.trades_today = 0
        self.last_trade_date = None
        self._dynamic_symbols = set()
        self._gap_candidates = {}  # symbol -> gap data for the day

    def add_dynamic_symbols(self, symbols):
        """Add dynamically discovered symbols from universe scanner."""
        for sym in symbols:
            if sym and isinstance(sym, str):
                self._dynamic_symbols.add(sym.upper())

    def get_symbols(self):
        """Return combined static + dynamic symbol list."""
        return list(set(self.symbols) | self._dynamic_symbols)

    def generate_signals(self, market_data):
        """
        Scan for pre-market gap plays and generate entry signals.
        Two phases:
        1. Discovery: Find gap-up stocks with extreme volume
        2. Entry: Wait for pullback then enter on structure break
        """
        signals = []

        today = datetime.now().date()
        if self.last_trade_date != today:
            self.trades_today = 0
            self.last_trade_date = today
            self._gap_candidates = {}  # Reset candidates daily

        if self.trades_today >= self.max_trades_per_day:
            return signals

        # Time-of-day filter: only trade gap plays during the morning window
        now = datetime.now()
        current_hour = now.hour
        current_minute = now.minute
        if current_hour < self.start_hour or current_hour >= self.end_hour:
            return signals  # Outside gap trading window

        # Post-open dead zone: 9:30-9:35 (configurable) — gap stocks whipsaw hard
        # Wait for the initial volatility to settle before entering
        if current_hour == 9 and 30 <= current_minute < 30 + self.open_dead_zone_minutes:
            log.debug(f"Pre-market gap: in open dead zone (first {self.open_dead_zone_minutes} min), waiting...")
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
                log.debug(f"Pre-market gap analysis error for {symbol}: {e}")

        return signals

    def _analyze_symbol(self, symbol, market_data):
        """Analyze a single symbol for pre-market gap setup."""
        bars = market_data.get_bars(symbol, 100) if market_data else None
        if bars is None or len(bars) < 30:
            return None

        closes = bars["close"].values
        volumes = bars["volume"].values
        highs = bars["high"].values
        lows = bars["low"].values
        opens = bars["open"].values

        current_price = float(closes[-1])
        if current_price <= 0 or current_price < self.min_price or current_price > self.max_price:
            return None

        # --- PHASE 1: GAP DETECTION ---
        # Use the open of the most recent session vs previous session close
        # On 5-min bars: 78 bars/day (390 min / 5 min). The gap is today's open vs yesterday's close.
        # Find the previous day boundary by looking for a significant time gap between bars
        prev_close = float(closes[0])  # fallback: earliest bar we have
        if len(closes) >= 78:
            # Approximate: bars 78+ ago are from the previous session
            prev_close = float(closes[-78])
        elif len(closes) >= 40:
            prev_close = float(closes[-40])  # Half-day fallback

        # Also use the Polygon snapshot gap_pct if available (passed from scanner)
        gap_pct = (current_price - prev_close) / prev_close if prev_close > 0 else 0

        # --- RELATIVE VOLUME ---
        # Sum recent 5-min volume bars to approximate daily volume
        # Compare today's total volume so far to the average day's volume
        today_bars = min(78, len(volumes))
        today_total_vol = float(np.sum(volumes[-today_bars:]))
        # Use bars before today for the average
        hist_bars = len(volumes) - today_bars
        if hist_bars >= 78:
            prev_day_vol = float(np.sum(volumes[-today_bars - 78:-today_bars]))
            avg_vol = max(prev_day_vol, 1)
        else:
            avg_vol = float(np.mean(volumes[:-1])) * today_bars if len(volumes) > 1 else 1
        current_vol = float(volumes[-1])
        rvol = round(today_total_vol / avg_vol, 1) if avg_vol > 0 else 0

        # --- PRICE ACTION ANALYSIS ---
        # Find the high of the move (leg 1 high)
        recent_bars = 10  # Look at last 10 bars for structure
        recent_highs = highs[-recent_bars:]
        recent_lows = lows[-recent_bars:]
        recent_closes = closes[-recent_bars:]

        leg1_high = float(np.max(recent_highs))
        leg1_high_idx = int(np.argmax(recent_highs))
        pullback_low = float(np.min(recent_lows[leg1_high_idx:])) if leg1_high_idx < recent_bars - 1 else current_price

        # Pullback depth: how much did it retrace from the high?
        move_size = leg1_high - prev_close if prev_close > 0 else 0
        pullback_depth = (leg1_high - pullback_low) / move_size if move_size > 0 else 0

        # Is price reclaiming? (break of structure)
        reclaiming = current_price > pullback_low and leg1_high_idx < recent_bars - 2
        above_pullback_high = False
        if leg1_high_idx < recent_bars - 2:
            # Find the high after the pullback low
            post_pullback_bars = recent_highs[leg1_high_idx + 1:]
            if len(post_pullback_bars) >= 2:
                # Current bar is higher than the bar before it = structure breaking up
                above_pullback_high = float(closes[-1]) > float(closes[-2]) and float(closes[-2]) > float(closes[-3]) if len(closes) >= 3 else False

        # --- ATR for stops/targets ---
        atr_val = self.indicators.atr(highs, lows, closes, period=14)
        atr = float(atr_val) if atr_val is not None and atr_val > 0 else current_price * 0.02

        # --- RSI check ---
        rsi_val = self.indicators.rsi(closes, 14)
        rsi = float(rsi_val) if rsi_val is not None else 50

        # --- COMPOSITE SCORING ---
        score = 0
        reasons = []

        # Gap size (max 25 pts)
        if gap_pct >= 0.20:
            score += 25
            reasons.append(f"HUGE gap +{gap_pct:.0%}")
        elif gap_pct >= 0.10:
            score += 20
            reasons.append(f"Strong gap +{gap_pct:.0%}")
        elif gap_pct >= self.min_gap_pct:
            score += 15
            reasons.append(f"Gap +{gap_pct:.0%}")

        # Relative volume (max 25 pts)
        if rvol >= 20:
            score += 25
            reasons.append(f"EXTREME vol {rvol:.0f}x")
        elif rvol >= self.min_rvol:
            score += 20
            reasons.append(f"High vol {rvol:.0f}x")
        elif rvol >= 5:
            score += 10
            reasons.append(f"Elevated vol {rvol:.0f}x")

        # Pullback quality (max 20 pts)
        if self.pullback_min_pct <= pullback_depth <= self.pullback_max_pct:
            score += 20
            reasons.append(f"Clean pullback {pullback_depth:.0%}")
        elif pullback_depth > 0 and pullback_depth < self.pullback_min_pct:
            score += 5
            reasons.append("Shallow pullback")

        # Structure break (max 20 pts)
        if above_pullback_high and reclaiming:
            score += 20
            reasons.append("Structure breaking UP")
        elif reclaiming:
            score += 10
            reasons.append("Reclaiming from pullback")

        # RSI confirmation (max 10 pts)
        if 40 <= rsi <= 70:
            score += 10
            reasons.append(f"RSI healthy ({rsi:.0f})")
        elif rsi < 40:
            score += 5
            reasons.append(f"RSI low ({rsi:.0f})")

        # Determine verdict
        is_qualified_gap = gap_pct >= self.min_gap_pct and rvol >= self.min_rvol
        has_pullback = self.pullback_min_pct <= pullback_depth <= self.pullback_max_pct
        has_structure_break = above_pullback_high and reclaiming

        # Higher score requirement in the first 15 min after open (fakeout zone)
        now_h = datetime.now().hour
        now_m = datetime.now().minute
        in_fakeout_zone = (now_h == 9 and 30 + self.open_dead_zone_minutes <= now_m <= 45)
        min_score_for_signal = 70 if in_fakeout_zone else 60

        if is_qualified_gap and has_pullback and has_structure_break and score >= min_score_for_signal:
            verdict = "BUY SIGNAL"
        elif is_qualified_gap and has_pullback and score >= 45:
            verdict = "SETTING UP"
        elif is_qualified_gap:
            verdict = "GAP QUALIFIED"
        elif gap_pct >= self.min_gap_pct:
            verdict = "LOW VOL GAP"
        else:
            verdict = "NO GAP"

        # Scan result for dashboard
        scan_result = {
            "price": round(current_price, 2),
            "prev_close": round(prev_close, 2),
            "gap_pct": round(gap_pct * 100, 1),
            "rvol": rvol,
            "leg1_high": round(leg1_high, 2),
            "pullback_low": round(pullback_low, 2),
            "pullback_depth": round(pullback_depth * 100, 1),
            "rsi": round(rsi, 1),
            "score": score,
            "verdict": verdict,
            "reasons": reasons,
        }

        result = {"scan": scan_result, "signal": None}

        # --- GENERATE SIGNAL ---
        if verdict == "BUY SIGNAL":
            stop_loss = pullback_low - (self.atr_stop_mult * atr)
            take_profit = current_price + (self.atr_target_mult * atr)
            runner_target = current_price + (self.runner_atr_mult * atr)

            risk = current_price - stop_loss
            reward = take_profit - current_price
            rr_ratio = round(reward / risk, 2) if risk > 0 else 0

            if rr_ratio >= 1.5 and risk > 0:
                confidence = min(1.0, score / 100)

                result["signal"] = {
                    "symbol": symbol,
                    "action": "buy",
                    "price": current_price,
                    "stop_loss": round(stop_loss, 2),
                    "take_profit": round(take_profit, 2),
                    "targets": [
                        round(current_price + (1.5 * atr), 2),
                        round(take_profit, 2),
                        round(runner_target, 2),
                    ],
                    "confidence": round(confidence, 2),
                    "reason": " | ".join(reasons[:4]),
                    "max_hold_bars": int(self.max_hold_minutes / 5),
                    "max_hold_days": 1,
                    "bar_seconds": 300,
                    "rr_ratio": rr_ratio,
                    "source": "premarket_gap",
                    "trailing_stop_pct": self.trailing_stop_pct,
                    "breakout_play": True,
                }

                self.signals_generated += 1
                log.info(
                    f"PRE-MARKET GAP SIGNAL: {symbol} @ ${current_price:.2f} | "
                    f"Gap: +{gap_pct:.0%} | RVOL: {rvol:.0f}x | "
                    f"Score: {score} | R:R {rr_ratio:.1f} | "
                    f"{' | '.join(reasons[:3])}"
                )

        return result
