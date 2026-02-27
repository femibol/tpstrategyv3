"""
Aggressive Momentum Runner Strategy — catches runners before & at breakout.

Three entry types:
1. ANTICIPATION: Detect tight consolidation (flag/pennant/ascending triangle),
   enter in the upper 25% of the range when volume surges 1.5x.
2. BREAKOUT: Enter when price breaks above resistance with strong close
   (upper 30% of candle) and volume >2x 20-bar average.
3. SPIKE: Single-candle >2% move on >5x RVOL — enter immediately at 50% size.

Unified 10-point scoring system:
- Relative Volume (0-3 pts): <2x=0, 2-5x=1, 5-10x=2, >10x=3
- Float (0-2 pts): >100M=0, 50-100M=1, <50M=2
- Catalyst (0-2 pts): none=0, sector sympathy=1, direct news=2
- Technical (0-3 pts): no pattern=0, near resistance=1, breakout forming=2,
  confirmed breakout=3

Only trades candidates scoring 6+/10.

Session-aware:
- Pre-market (4:00-9:30 ET): Gap scanners, coiled spring detection
- Regular (9:30-16:00 ET): Top 5 gappers, new intraday highs, halt candidates
- Post-market (16:00-20:00 ET): Earnings movers, after-hours catalysts

Exit via 4-phase adaptive trailing stop (managed by engine):
- Phase 1 (0-2%): Hard stop at entry - 1x ATR(14)
- Phase 2 (2-5%): Breakeven + 0.5%, trail 3-candle low
- Phase 3 (5%+): Trail 5-candle low or 9 EMA (whichever tighter)
- Phase 4 (15%+): Trail 5 EMA, exit if candle closes below on 2x volume
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

log = get_logger("strategy.momentum_runner")


class MomentumRunnerStrategy(BaseStrategy):
    """
    Aggressive momentum runner catcher with 3 entry types and unified scoring.
    """

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)
        self.min_score = config.get("min_score", 6)  # 6/10 minimum
        self.min_price = config.get("min_price", 1.00)
        self.max_price = config.get("max_price", 100.00)
        self.min_volume = config.get("min_volume", 500000)
        self.max_daily_change_pct = config.get("max_daily_change_pct", 30.0)
        self.max_open_positions = config.get("max_open_positions", 5)
        self.max_trades_per_day = config.get("max_trades_per_day", 10)
        self.atr_stop_mult = config.get("atr_stop_multiplier", 1.0)
        self.spike_size_pct = config.get("spike_size_pct", 0.50)  # 50% size for spike entries
        self.open_dead_zone_minutes = config.get("open_dead_zone_minutes", 2)
        self.afternoon_reduction_hour = config.get("afternoon_reduction_hour", 15)  # 3 PM ET
        self.afternoon_size_reduction = config.get("afternoon_size_reduction", 0.50)  # 50%

        self.trades_today = 0
        self.last_trade_date = None
        self._dynamic_symbols = set()
        self._snapshot_data = {}
        self._sector_runners = {}  # sector -> count of running stocks (for sympathy)
        self._catalyst_cache = {}  # symbol -> catalyst info

        # Track which symbols scored 6+ for the dashboard
        self._qualified_candidates = {}

    def add_dynamic_symbols(self, symbols):
        """Add dynamically discovered symbols."""
        now = time.time()
        for sym in symbols:
            if sym and isinstance(sym, str):
                s = sym.upper()
                self._dynamic_symbols.add(s)
                self._dynamic_symbol_timestamps[s] = now

    def feed_snapshot_data(self, snapshot_entries):
        """Feed Polygon snapshot data for fast-path analysis."""
        for entry in snapshot_entries:
            sym = entry.get("symbol", "")
            if sym:
                self._snapshot_data[sym.upper()] = entry

    def feed_sector_momentum(self, sector_counts):
        """Feed sector momentum data: {sector: count_of_runners}.
        Used for sympathy play detection (3+ in same sector = flag laggard)."""
        self._sector_runners = sector_counts or {}

    def feed_catalyst_data(self, catalyst_map):
        """Feed catalyst data: {symbol: {type: 'earnings'|'news'|'upgrade', ...}}."""
        if catalyst_map:
            self._catalyst_cache.update(catalyst_map)

    def get_symbols(self):
        """Return combined static + dynamic symbol list."""
        return list(set(self.symbols) | self._dynamic_symbols)

    def generate_signals(self, market_data):
        """Scan all symbols for momentum runner setups."""
        signals = []

        now = datetime.now(ET) if ET else datetime.now()
        today = now.date()
        if self.last_trade_date != today:
            self.trades_today = 0
            self.last_trade_date = today
            self._qualified_candidates = {}

        if self.trades_today >= self.max_trades_per_day:
            return signals

        # Session detection
        hour = now.hour
        minute = now.minute
        session = self._get_session(hour, minute)

        # Open dead zone: skip first N minutes unless conviction pick
        if hour == 9 and 30 <= minute < 30 + self.open_dead_zone_minutes:
            # Only allow pre-market conviction picks through
            log.debug(
                f"Momentum runner: open dead zone ({self.open_dead_zone_minutes}min), "
                f"only pre-market conviction picks allowed"
            )

        all_symbols = self.get_symbols()
        # Include snapshot-only symbols
        snapshot_only = set(self._snapshot_data.keys()) - set(all_symbols)
        all_symbols.extend(list(snapshot_only))

        for symbol in all_symbols:
            try:
                result = self._analyze_symbol(symbol, market_data, session, now)
                if result:
                    self.scan_results[symbol] = result["scan"]
                    if result.get("signal"):
                        signals.append(result["signal"])
                        self.trades_today += 1
                        if self.trades_today >= self.max_trades_per_day:
                            break
            except Exception as e:
                log.debug(f"Momentum runner error for {symbol}: {e}")

        # Sort by score descending — prioritize best candidates
        signals.sort(key=lambda s: s.get("score", 0), reverse=True)

        return signals

    def _get_session(self, hour, minute):
        """Determine current trading session."""
        time_val = hour * 100 + minute
        if time_val < 930:
            return "premarket"
        elif time_val < 1600:
            return "regular"
        else:
            return "postmarket"

    # =========================================================================
    # UNIFIED 10-POINT SCORING SYSTEM
    # =========================================================================

    def _score_candidate(self, symbol, rvol, float_shares, change_pct, session,
                         has_consolidation, near_resistance, breaking_out,
                         confirmed_breakout):
        """Score a candidate on a 0-10 scale.

        Components:
        - RVOL (0-3): <2x=0, 2-5x=1, 5-10x=2, >10x=3
        - Float (0-2): >100M=0, 50-100M=1, <50M=2
        - Catalyst (0-2): none=0, sympathy=1, direct=2
        - Technical (0-3): none=0, near resistance=1, forming=2, confirmed=3

        Returns (score, breakdown_dict)
        """
        score = 0
        breakdown = {}

        # --- RVOL Score (0-3) ---
        if rvol >= 10.0:
            rvol_pts = 3
        elif rvol >= 5.0:
            rvol_pts = 2
        elif rvol >= 2.0:
            rvol_pts = 1
        else:
            rvol_pts = 0
        score += rvol_pts
        breakdown["rvol"] = rvol_pts

        # --- Float Score (0-2) ---
        if float_shares > 0:
            if float_shares < 50_000_000:
                float_pts = 2
            elif float_shares < 100_000_000:
                float_pts = 1
            else:
                float_pts = 0
        else:
            float_pts = 0  # Unknown float = no bonus
        score += float_pts
        breakdown["float"] = float_pts

        # --- Catalyst Score (0-2) ---
        catalyst = self._catalyst_cache.get(symbol)
        sector = self._snapshot_data.get(symbol, {}).get("sector", "Unknown")

        if catalyst and catalyst.get("type") in ("earnings", "news", "upgrade", "fda"):
            catalyst_pts = 2
        elif sector in self._sector_runners and self._sector_runners.get(sector, 0) >= 3:
            catalyst_pts = 1  # Sympathy play — 3+ in same sector running
        else:
            catalyst_pts = 0
        score += catalyst_pts
        breakdown["catalyst"] = catalyst_pts

        # --- Technical Score (0-3) ---
        if confirmed_breakout:
            tech_pts = 3
        elif breaking_out or has_consolidation:
            tech_pts = 2
        elif near_resistance:
            tech_pts = 1
        else:
            tech_pts = 0
        score += tech_pts
        breakdown["technical"] = tech_pts

        return score, breakdown

    # =========================================================================
    # MAIN ANALYSIS — BARS PATH
    # =========================================================================

    def _analyze_symbol(self, symbol, market_data, session, now):
        """Analyze a symbol for all 3 entry types."""
        bars = market_data.get_bars(symbol, 80) if market_data else None

        if bars is None or len(bars) < 25:
            # Fall back to snapshot fast path
            snap = self._snapshot_data.get(symbol)
            if snap:
                return self._analyze_from_snapshot(symbol, snap, session, now)
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

        # Volume filter (daily avg > 500K)
        avg_vol_20 = float(np.mean(volumes[-21:-1])) if len(volumes) > 21 else float(np.mean(volumes[:-1]))
        daily_avg_vol = avg_vol_20 * 78  # ~78 five-min bars per day
        if daily_avg_vol < self.min_volume:
            return None

        # Daily change
        prev_close = float(closes[-2]) if len(closes) >= 2 else current_price
        change_pct = ((current_price - prev_close) / prev_close * 100) if prev_close > 0 else 0

        # Skip stocks already up >30% unless fresh catalyst
        if abs(change_pct) > self.max_daily_change_pct:
            catalyst = self._catalyst_cache.get(symbol)
            if not catalyst:
                return None

        # --- RVOL ---
        current_vol = float(volumes[-1])
        rvol = round(current_vol / avg_vol_20, 2) if avg_vol_20 > 0 else 0

        # Volume ratio for breakout confirmation
        vol_20_avg = float(np.mean(volumes[-21:-1])) if len(volumes) > 21 else float(np.mean(volumes[:-1]))

        # --- ATR ---
        atr = self.indicators.atr(highs, lows, closes, period=14)
        # Floor: ATR must be at least 2% of price to avoid near-zero stops
        min_atr = current_price * 0.02
        if not atr or atr < min_atr:
            atr = max(atr or 0, min_atr) if atr and atr > 0 else current_price * 0.03

        # --- Float ---
        snap_data = self._snapshot_data.get(symbol, {})
        float_shares = snap_data.get("float_shares", 0)

        # --- TECHNICAL PATTERN DETECTION ---

        # 1. Consolidation detection (flag/pennant/ascending triangle)
        has_consolidation, consol_low, consol_high = self._detect_consolidation(
            highs, lows, closes, volumes
        )

        # 2. Resistance level detection
        near_resistance, resistance_level = self._detect_resistance(
            highs, lows, closes
        )

        # 3. Breakout detection
        breaking_out, confirmed_breakout, breakout_type = self._detect_breakout(
            opens, highs, lows, closes, volumes, resistance_level, vol_20_avg
        )

        # 4. Spike detection (single-candle >2% on >5x RVOL)
        is_spike = self._detect_spike(
            opens, highs, lows, closes, volumes, vol_20_avg
        )

        # --- UNIFIED SCORING ---
        score, breakdown = self._score_candidate(
            symbol, rvol, float_shares, change_pct, session,
            has_consolidation, near_resistance, breaking_out, confirmed_breakout
        )

        # --- DETERMINE ENTRY TYPE ---
        entry_type = None
        size_multiplier = 1.0

        hour = now.hour
        minute = now.minute

        # Afternoon size reduction (after 3 PM ET)
        if hour >= self.afternoon_reduction_hour:
            size_multiplier *= self.afternoon_size_reduction

        # Open dead zone check
        in_dead_zone = (hour == 9 and 30 <= minute < 30 + self.open_dead_zone_minutes)

        if score >= self.min_score:
            if is_spike and not in_dead_zone:
                entry_type = "spike"
                size_multiplier *= self.spike_size_pct  # 50% size for spikes
            elif confirmed_breakout and not in_dead_zone:
                entry_type = "breakout"
            elif has_consolidation and not in_dead_zone:
                # Anticipation: in upper 25% of consolidation + volume surge
                if consol_high > consol_low:
                    range_position = (current_price - consol_low) / (consol_high - consol_low)
                    recent_vol_avg = float(np.mean(volumes[-4:-1])) if len(volumes) > 4 else vol_20_avg
                    vol_surge = current_vol > recent_vol_avg * 1.5
                    if range_position >= 0.75 and vol_surge:
                        entry_type = "anticipation"
            elif in_dead_zone and score >= 8:
                # Only pre-market conviction picks bypass dead zone
                entry_type = "breakout"

        # --- BUILD SCAN RESULT ---
        verdict = "QUIET"
        if entry_type:
            verdict = f"RUNNER {entry_type.upper()}"
        elif score >= self.min_score:
            verdict = "QUALIFIED"
        elif score >= 4:
            verdict = "WARMING"

        # EMA for context
        ema9 = self.indicators.ema(closes, 9)
        ema20 = self.indicators.ema(closes, 20)
        trend = "BULL" if (ema9 is not None and ema20 is not None and
                           ema9[-1] > ema20[-1]) else "BEAR"

        rsi = self.indicators.rsi(closes, 14)

        scan_result = {
            "price": round(current_price, 2),
            "rvol": rvol,
            "change_pct": round(change_pct, 2),
            "volume": int(current_vol),
            "avg_volume": int(avg_vol_20),
            "float_shares": float_shares,
            "trend": trend,
            "rsi": round(rsi, 1) if rsi else 50,
            "atr": round(atr, 4),
            "score": score,
            "score_breakdown": breakdown,
            "entry_type": entry_type,
            "has_consolidation": has_consolidation,
            "near_resistance": near_resistance,
            "breaking_out": breaking_out,
            "confirmed_breakout": confirmed_breakout,
            "is_spike": is_spike,
            "session": session,
            "verdict": verdict,
            "size_multiplier": round(size_multiplier, 2),
        }

        result = {"scan": scan_result, "signal": None}

        if score >= self.min_score:
            self._qualified_candidates[symbol] = scan_result

        # --- GENERATE SIGNAL ---
        if entry_type and score >= self.min_score:
            # Stop loss: entry - 1x ATR(14) for all entry types
            if entry_type == "anticipation" and consol_low > 0:
                stop_loss = consol_low  # Below consolidation low
            else:
                stop_loss = current_price - (self.atr_stop_mult * atr)

            # Targets: adaptive based on ATR
            targets = [
                round(current_price + (1.5 * atr), 2),  # Quick scalp
                round(current_price + (3.0 * atr), 2),  # Main target
                round(current_price + (6.0 * atr), 2),  # Runner target
            ]
            take_profit = targets[1]  # Main target for R:R calc

            risk = current_price - stop_loss
            reward = take_profit - current_price
            rr_ratio = round(reward / risk, 2) if risk > 0 else 0

            if rr_ratio >= 1.5 and risk > 0:
                confidence = min(1.0, score / 10)

                result["signal"] = {
                    "symbol": symbol,
                    "action": "buy",
                    "price": current_price,
                    "stop_loss": round(stop_loss, 2),
                    "take_profit": round(take_profit, 2),
                    "targets": targets,
                    "confidence": round(confidence, 2),
                    "reason": self._build_reason(entry_type, score, breakdown, rvol, change_pct),
                    "max_hold_bars": 0,  # Trailing stop manages exit
                    "max_hold_days": 2,
                    "bar_seconds": 300,
                    "rvol": rvol,
                    "rr_ratio": rr_ratio,
                    "score": score,
                    "source": "momentum_runner",
                    "entry_type": entry_type,
                    "size_multiplier": round(size_multiplier, 2),
                    "atr_value": round(atr, 4),
                    "trailing_stop_pct": 0.015,  # Initial trail, engine will use 4-phase
                    "runner_mode": True,
                    "momentum_runner": True,  # Flag for engine's 4-phase trail
                }

                self.signals_generated += 1
                log.info(
                    f"RUNNER SIGNAL [{entry_type.upper()}]: {symbol} | "
                    f"Score: {score}/10 ({breakdown}) | RVOL: {rvol:.1f}x | "
                    f"Change: {change_pct:+.1f}% | R:R {rr_ratio:.1f} | "
                    f"Size: {size_multiplier:.0%}"
                )

        return result

    # =========================================================================
    # SNAPSHOT FAST PATH
    # =========================================================================

    def _analyze_from_snapshot(self, symbol, snap, session, now):
        """Analyze using Polygon snapshot data (no bars needed)."""
        price = snap.get("price", 0)
        if price <= 0 or price < self.min_price or price > self.max_price:
            return None

        change_pct = snap.get("change_pct", 0)
        volume = snap.get("volume", 0)
        avg_volume = snap.get("avg_volume", 1)
        rvol = snap.get("rvol", 0)
        float_shares = snap.get("float_shares", 0)
        gap_pct = snap.get("gap_pct", 0)

        # Volume filter
        if volume < self.min_volume:
            return None

        # Skip >30% without catalyst
        if abs(change_pct) > self.max_daily_change_pct:
            catalyst = self._catalyst_cache.get(symbol)
            if not catalyst:
                return None

        if rvol <= 0 and avg_volume > 0:
            rvol = round(volume / avg_volume, 1)

        # Determine technical state from snapshot data
        # Can't detect consolidation/resistance from snapshot — use change_pct heuristics
        confirmed_breakout = change_pct >= 5.0 and rvol >= 3.0
        breaking_out = change_pct >= 3.0 and rvol >= 2.0
        near_resistance = change_pct >= 1.0

        score, breakdown = self._score_candidate(
            symbol, rvol, float_shares, change_pct, session,
            has_consolidation=False,
            near_resistance=near_resistance,
            breaking_out=breaking_out,
            confirmed_breakout=confirmed_breakout
        )

        # Snapshot bonus: Polygon flagging it as a top mover IS confirmation
        if change_pct >= 5.0 and rvol >= 2.0 and score < self.min_score:
            # Boost technical score for strong movers the scanner caught
            score = max(score, self.min_score)
            breakdown["snapshot_boost"] = True

        hour = now.hour
        minute = now.minute
        size_multiplier = 1.0
        if hour >= self.afternoon_reduction_hour:
            size_multiplier *= self.afternoon_size_reduction

        in_dead_zone = (hour == 9 and 30 <= minute < 30 + self.open_dead_zone_minutes)

        entry_type = None
        if score >= self.min_score and not in_dead_zone:
            if confirmed_breakout:
                entry_type = "breakout"
            elif breaking_out:
                entry_type = "breakout"
        elif score >= 8 and in_dead_zone:
            entry_type = "breakout"  # Conviction picks only

        verdict = "QUIET"
        if entry_type:
            verdict = f"RUNNER {entry_type.upper()}"
        elif score >= self.min_score:
            verdict = "QUALIFIED"
        elif score >= 4:
            verdict = "WARMING"

        scan_result = {
            "price": round(price, 2),
            "rvol": rvol,
            "change_pct": round(change_pct, 2),
            "volume": int(volume),
            "avg_volume": int(avg_volume),
            "float_shares": float_shares,
            "gap_pct": round(gap_pct, 2),
            "trend": "BULL" if change_pct > 0 else "BEAR",
            "score": score,
            "score_breakdown": breakdown,
            "entry_type": entry_type,
            "session": session,
            "verdict": verdict,
            "size_multiplier": round(size_multiplier, 2),
            "fast_path": True,
        }

        result = {"scan": scan_result, "signal": None}

        if score >= self.min_score:
            self._qualified_candidates[symbol] = scan_result

        if entry_type and score >= self.min_score:
            est_atr = price * abs(change_pct) / 100 * 0.7
            # Floor: ATR must be at least 2% of price to prevent instant stop triggers
            # For a $1.59 stock, minimum ATR = $0.032 → stop at least $0.032 below entry
            min_atr = price * 0.02
            if est_atr < min_atr:
                est_atr = max(est_atr, min_atr) if est_atr > 0 else price * 0.03

            stop_loss = price - (self.atr_stop_mult * est_atr)
            targets = [
                round(price + (1.5 * est_atr), 2),
                round(price + (3.0 * est_atr), 2),
                round(price + (6.0 * est_atr), 2),
            ]
            take_profit = targets[1]

            risk = price - stop_loss
            reward = take_profit - price
            rr_ratio = round(reward / risk, 2) if risk > 0 else 0

            if rr_ratio >= 1.5 and risk > 0:
                confidence = min(1.0, score / 10)

                result["signal"] = {
                    "symbol": symbol,
                    "action": "buy",
                    "price": price,
                    "stop_loss": round(stop_loss, 2),
                    "take_profit": round(take_profit, 2),
                    "targets": targets,
                    "confidence": round(confidence, 2),
                    "reason": self._build_reason(entry_type, score, breakdown, rvol, change_pct),
                    "max_hold_bars": 0,
                    "max_hold_days": 2,
                    "bar_seconds": 300,
                    "rvol": rvol,
                    "rr_ratio": rr_ratio,
                    "score": score,
                    "source": "momentum_runner",
                    "entry_type": entry_type,
                    "size_multiplier": round(size_multiplier, 2),
                    "atr_value": round(est_atr, 4),
                    "trailing_stop_pct": 0.015,
                    "runner_mode": True,
                    "momentum_runner": True,
                    "fast_path": True,
                }

                self.signals_generated += 1
                log.info(
                    f"RUNNER FAST SIGNAL [{entry_type.upper()}]: {symbol} | "
                    f"Score: {score}/10 | RVOL: {rvol:.1f}x | "
                    f"Change: {change_pct:+.1f}% | R:R {rr_ratio:.1f} [SNAPSHOT]"
                )

        return result

    # =========================================================================
    # PATTERN DETECTION
    # =========================================================================

    def _detect_consolidation(self, highs, lows, closes, volumes):
        """Detect tight consolidation (flag/pennant/ascending triangle).

        Looks for narrowing price range over 3+ candles where the range
        contracts each candle (pennant) or stays flat (flag).

        Returns (is_consolidating, low, high)
        """
        if len(closes) < 8:
            return False, 0, 0

        # Look at last 8 candles for consolidation
        recent_highs = highs[-8:]
        recent_lows = lows[-8:]

        # Calculate range for each candle
        ranges = recent_highs - recent_lows
        if len(ranges) < 4:
            return False, 0, 0

        # Check if ranges are narrowing (pennant/triangle)
        narrowing_count = 0
        for i in range(1, len(ranges)):
            if ranges[i] < ranges[i - 1]:
                narrowing_count += 1

        # At least 3 of the last 7 transitions should show narrowing
        is_narrowing = narrowing_count >= 3

        # Also check if overall range is tight (< 5% of price)
        overall_high = float(np.max(recent_highs))
        overall_low = float(np.min(recent_lows))
        current_price = float(closes[-1])
        range_pct = (overall_high - overall_low) / current_price * 100 if current_price > 0 else 100

        is_tight = range_pct < 5.0

        # Check for ascending lows (ascending triangle)
        ascending_lows = 0
        for i in range(-4, 0):
            if recent_lows[i] > recent_lows[i - 1]:
                ascending_lows += 1
        has_ascending = ascending_lows >= 2

        is_consolidating = (is_narrowing and is_tight) or (has_ascending and is_tight)

        return is_consolidating, overall_low, overall_high

    def _detect_resistance(self, highs, lows, closes):
        """Detect nearby resistance levels.

        Checks for:
        - Prior swing high
        - VWAP level
        - Whole/half dollar levels

        Returns (near_resistance, resistance_level)
        """
        if len(closes) < 15:
            return False, 0

        current_price = float(closes[-1])

        # Prior swing high (highest high in last 10-20 bars, excluding last 2)
        if len(highs) >= 12:
            prior_high = float(np.max(highs[-20:-2])) if len(highs) >= 22 else float(np.max(highs[:-2]))
        else:
            return False, 0

        # Whole dollar level near price
        whole_dollar = round(current_price)
        half_dollar = round(current_price * 2) / 2

        # Check if price is within 1% of any resistance
        pct_from_prior = abs(current_price - prior_high) / current_price if current_price > 0 else 1
        pct_from_whole = abs(current_price - whole_dollar) / current_price if current_price > 0 else 1
        pct_from_half = abs(current_price - half_dollar) / current_price if current_price > 0 else 1

        # Near resistance = within 1.5% of a level, approaching from below
        near = False
        level = 0

        if pct_from_prior < 0.015 and current_price <= prior_high:
            near = True
            level = prior_high
        elif pct_from_whole < 0.01 and current_price <= whole_dollar:
            near = True
            level = whole_dollar
        elif pct_from_half < 0.01 and current_price <= half_dollar:
            near = True
            level = half_dollar

        return near, level

    def _detect_breakout(self, opens, highs, lows, closes, volumes,
                         resistance_level, vol_20_avg):
        """Detect breakout entries.

        Breakout = price above resistance + candle closes in upper 30% +
        volume > 2x 20-bar average.

        Returns (breaking_out, confirmed_breakout, breakout_type)
        """
        if len(closes) < 3:
            return False, False, None

        current_price = float(closes[-1])
        current_open = float(opens[-1])
        current_high = float(highs[-1])
        current_low = float(lows[-1])
        current_vol = float(volumes[-1])

        candle_range = current_high - current_low
        if candle_range <= 0:
            return False, False, None

        # Candle close position (0 = low, 1 = high)
        close_position = (current_price - current_low) / candle_range

        # Strong close = upper 30% of candle
        strong_close = close_position >= 0.70

        # Volume confirmation = >2x 20-bar average
        vol_confirmed = current_vol > vol_20_avg * 2.0 if vol_20_avg > 0 else False

        # Breaking above resistance
        above_resistance = False
        if resistance_level > 0:
            above_resistance = current_price > resistance_level

        # Also check: breaking above recent high (last 10 bars)
        if len(highs) >= 12:
            recent_high = float(np.max(highs[-11:-1]))
            above_recent_high = current_price > recent_high
        else:
            above_recent_high = False

        breaking_out = (above_resistance or above_recent_high) and current_price > current_open
        confirmed_breakout = breaking_out and strong_close and vol_confirmed

        breakout_type = None
        if confirmed_breakout:
            breakout_type = "confirmed"
        elif breaking_out:
            breakout_type = "forming"

        return breaking_out, confirmed_breakout, breakout_type

    def _detect_spike(self, opens, highs, lows, closes, volumes, vol_20_avg):
        """Detect single-candle spike (>2% move on >5x RVOL).

        These are the fastest entries — the "1-tick runners".

        Returns True if latest candle qualifies as a spike.
        """
        if len(closes) < 2:
            return False

        current_price = float(closes[-1])
        current_open = float(opens[-1])
        current_vol = float(volumes[-1])

        # Candle move >2%
        if current_open <= 0:
            return False
        candle_move_pct = abs(current_price - current_open) / current_open * 100

        # RVOL on this candle >5x
        candle_rvol = current_vol / vol_20_avg if vol_20_avg > 0 else 0

        # Must be a bullish candle (close > open)
        is_bullish = current_price > current_open

        return is_bullish and candle_move_pct >= 2.0 and candle_rvol >= 5.0

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _build_reason(self, entry_type, score, breakdown, rvol, change_pct):
        """Build human-readable signal reason."""
        parts = [f"[{entry_type.upper()}]"]
        parts.append(f"Score:{score}/10")

        detail_parts = []
        if breakdown.get("rvol", 0) > 0:
            detail_parts.append(f"RVOL:{rvol:.1f}x")
        if breakdown.get("float", 0) > 0:
            detail_parts.append(f"Float:{breakdown['float']}pt")
        if breakdown.get("catalyst", 0) > 0:
            detail_parts.append("Catalyst" if breakdown["catalyst"] == 2 else "Sympathy")
        if breakdown.get("technical", 0) > 0:
            tech_labels = {1: "NearRes", 2: "Forming", 3: "Confirmed"}
            detail_parts.append(tech_labels.get(breakdown["technical"], "Tech"))

        if change_pct != 0:
            detail_parts.append(f"Chg:{change_pct:+.1f}%")

        parts.append(" | ".join(detail_parts))
        return " ".join(parts)

    def get_qualified_candidates(self):
        """Return candidates that scored 6+/10 (for dashboard)."""
        return self._qualified_candidates
