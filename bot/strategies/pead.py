"""
Post-Earnings Announcement Drift (PEAD) Strategy

One of the most well-documented anomalies in finance:
stocks that beat earnings continue to drift in the direction
of the surprise for 20-60 trading days.

The effect is strongest in:
- Small/mid cap stocks (less analyst coverage = slower info diffusion)
- Large earnings surprises (5%+ beat)
- High volume on announcement day (confirms institutional interest)

This flips the existing "avoid earnings" logic to "ride the drift."
"""
import time
from datetime import datetime, timedelta

from bot.strategies.base import BaseStrategy
from bot.utils.logger import get_logger

log = get_logger("strategy.pead")


class PEADStrategy(BaseStrategy):
    """Trade post-earnings drift on strong earnings beats."""

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)

        # Config
        self.min_gap_pct = config.get("min_gap_pct", 5.0)   # Min earnings day gap
        self.min_rvol = config.get("min_rvol", 2.0)          # Min RVOL on earnings day
        self.min_score = config.get("min_score", 55)
        self.min_price = config.get("min_price", 2.00)
        self.max_price = config.get("max_price", 100.00)
        self.min_volume = config.get("min_volume", 200000)
        self.max_trades_per_day = config.get("max_trades_per_day", 5)
        self.drift_window_days = config.get("drift_window_days", 30)  # How long to ride
        self.pullback_entry_pct = config.get("pullback_entry_pct", 0.03)  # Enter on 3% pullback
        self.atr_stop_multiplier = config.get("atr_stop_multiplier", 2.5)
        self.max_hold_days = config.get("max_hold_days", 20)

        # State
        self._earnings_beats = {}  # symbol -> {gap_pct, rvol, date, price_at_earnings}
        self._signals_today = 0
        self._last_reset = None
        self._dynamic_symbols = set()

    def add_dynamic_symbols(self, symbols):
        """Add dynamically discovered symbols."""
        now = time.time()
        for sym in symbols:
            if sym and isinstance(sym, str):
                s = sym.upper()
                self._dynamic_symbols.add(s)
                self._dynamic_symbol_timestamps[s] = now

    def get_symbols(self):
        return list(set(self.symbols) | self._dynamic_symbols)

    def feed_earnings_data(self, symbol, gap_pct, rvol, earnings_date, price_at_earnings):
        """Record an earnings beat for drift tracking.

        Called by the engine when it detects a post-earnings gap-up.

        Args:
            symbol: Ticker
            gap_pct: Gap % on earnings day (positive = beat)
            rvol: Relative volume on earnings day
            earnings_date: Date of earnings announcement
            price_at_earnings: Close price on earnings day
        """
        if gap_pct >= self.min_gap_pct and rvol >= self.min_rvol:
            self._earnings_beats[symbol] = {
                "gap_pct": gap_pct,
                "rvol": rvol,
                "date": earnings_date,
                "price_at_earnings": price_at_earnings,
            }
            log.info(
                f"PEAD: Tracking {symbol} — earnings gap +{gap_pct:.1f}%, "
                f"RVOL {rvol:.1f}x on {earnings_date}"
            )

    def generate_signals(self, market_data):
        """Scan earnings beats for drift entry opportunities."""
        if not self.enabled:
            return []

        today = datetime.now().date()
        if self._last_reset != today:
            self._signals_today = 0
            self._last_reset = today

        if self._signals_today >= self.max_trades_per_day:
            return []

        # Also scan current movers for post-earnings gaps
        self._detect_earnings_gaps(market_data)

        # Clean up stale entries (past drift window)
        self._cleanup_stale()

        signals = []
        for symbol, beat in self._earnings_beats.items():
            try:
                result = self._analyze_drift(symbol, beat, market_data)
                if result:
                    signals.append(result)
                    self._signals_today += 1
                    if self._signals_today >= self.max_trades_per_day:
                        break
            except Exception as e:
                log.debug(f"PEAD analysis error for {symbol}: {e}")

        signals.sort(key=lambda x: x.get("score", 0), reverse=True)
        return signals

    def _detect_earnings_gaps(self, market_data):
        """Auto-detect post-earnings gaps from scanner data."""
        if not market_data or not hasattr(market_data, 'scanner'):
            return

        scanner = market_data.scanner
        if not scanner:
            return

        # Check today's gap-ups for earnings catalysts
        gap_ups = scanner.get_gap_ups(limit=30)
        for entry in gap_ups:
            sym = entry.get("symbol", "")
            if sym in self._earnings_beats:
                continue

            gap_pct = entry.get("gap_pct", 0)
            rvol = entry.get("rvol", 0)
            price = entry.get("price", 0)

            if gap_pct < self.min_gap_pct or rvol < self.min_rvol:
                continue

            # Check if this gap is earnings-related
            has_earnings = False
            try:
                has_earnings = scanner.has_earnings_soon(sym, days_ahead=2)
            except Exception:
                pass

            # Large gap + high volume strongly suggests earnings/catalyst
            # Even without confirmed earnings, a 10%+ gap with 5x+ RVOL is worth tracking
            if has_earnings or (gap_pct >= 10.0 and rvol >= 5.0):
                self.feed_earnings_data(
                    sym, gap_pct, rvol,
                    datetime.now().date(),
                    price
                )

    def _analyze_drift(self, symbol, beat, market_data):
        """Analyze a confirmed earnings beat for drift entry."""
        price = market_data.get_price(symbol) if market_data else None
        if not price or price < self.min_price or price > self.max_price:
            return None

        earnings_price = beat["price_at_earnings"]
        gap_pct = beat["gap_pct"]
        rvol = beat["rvol"]
        earnings_date = beat["date"]

        # How many days since earnings?
        today = datetime.now().date()
        if isinstance(earnings_date, datetime):
            earnings_date = earnings_date.date()
        days_since = (today - earnings_date).days

        # Must be within drift window
        if days_since > self.drift_window_days:
            return None

        # Price should still be above the earnings-day close (drift continues)
        if price < earnings_price * 0.95:
            return None  # Stock gave back the earnings gap — no drift

        # Check for pullback entry (don't chase at the highs)
        pnl_from_earnings = (price - earnings_price) / earnings_price
        # Best entries: price pulled back 1-5% from recent highs but still above earnings close
        pullback_bonus = 0
        if 0.01 <= pnl_from_earnings <= 0.05:
            pullback_bonus = 10  # Pulled back to good entry zone
        elif pnl_from_earnings > 0.15:
            pullback_bonus = -5  # Too extended, risky chase

        # --- PEAD Score (0-100) ---
        score = 0
        reasons = []

        # Earnings surprise magnitude (max 30 pts)
        if gap_pct >= 15.0:
            score += 30
            reasons.append(f"Massive beat +{gap_pct:.0f}%")
        elif gap_pct >= 10.0:
            score += 25
            reasons.append(f"Strong beat +{gap_pct:.0f}%")
        elif gap_pct >= 7.0:
            score += 18
            reasons.append(f"Beat +{gap_pct:.0f}%")
        elif gap_pct >= 5.0:
            score += 12
            reasons.append(f"Beat +{gap_pct:.0f}%")

        # Volume on earnings day (max 20 pts)
        if rvol >= 8.0:
            score += 20
            reasons.append(f"Inst. volume {rvol:.0f}x")
        elif rvol >= 5.0:
            score += 15
            reasons.append(f"Heavy volume {rvol:.0f}x")
        elif rvol >= 3.0:
            score += 10
            reasons.append(f"High volume {rvol:.0f}x")
        elif rvol >= 2.0:
            score += 5
            reasons.append(f"Volume {rvol:.0f}x")

        # Drift timing (max 20 pts) — earlier in drift window = stronger
        if days_since <= 3:
            score += 20
            reasons.append(f"Day {days_since} post-earnings")
        elif days_since <= 7:
            score += 15
            reasons.append(f"Week 1 drift")
        elif days_since <= 14:
            score += 10
            reasons.append(f"Week 2 drift")
        else:
            score += 5
            reasons.append(f"Late drift day {days_since}")

        # Price holding above earnings close (max 15 pts)
        if pnl_from_earnings >= 0.05:
            score += 15
            reasons.append("Strong drift continuation")
        elif pnl_from_earnings >= 0.02:
            score += 10
            reasons.append("Holding above earnings close")
        elif pnl_from_earnings >= 0:
            score += 5
            reasons.append("At earnings close level")

        # Pullback entry bonus/penalty (max +/-10 pts)
        score += pullback_bonus

        # Current volume check (max 15 pts)
        volume = market_data.get_volume(symbol) if market_data else 0
        if volume and volume >= self.min_volume:
            score += 5
            if volume >= self.min_volume * 3:
                score += 10
                reasons.append("High current volume")

        # Store scan result
        self.scan_results[symbol] = {
            "symbol": symbol,
            "price": price,
            "earnings_gap_pct": gap_pct,
            "earnings_rvol": rvol,
            "days_since_earnings": days_since,
            "drift_pct": round(pnl_from_earnings * 100, 1),
            "score": score,
            "verdict": "DRIFT BUY" if score >= self.min_score else "WATCH",
        }

        if score < self.min_score:
            return None

        # Calculate stop — use ATR if bars available, else % based
        data = market_data.get_data(symbol) if market_data else None
        atr = 0
        if data is not None and len(data) >= 14 and "high" in data.columns:
            try:
                from bot.indicators.technical import TechnicalIndicators
                ti = TechnicalIndicators()
                atr = ti.atr(data, period=14).iloc[-1] if hasattr(ti, 'atr') else 0
            except Exception:
                atr = price * 0.03

        if atr <= 0:
            atr = price * 0.03

        # Stop below earnings-day close (key support) or ATR-based
        atr_stop = price - (atr * self.atr_stop_multiplier)
        earnings_stop = earnings_price * 0.97  # 3% below earnings close
        stop_loss = max(atr_stop, earnings_stop)  # Use the tighter stop

        reason_str = "PEAD Drift: " + ", ".join(reasons)

        return {
            "symbol": symbol,
            "action": "buy",
            "price": price,
            "stop_loss": stop_loss,
            "take_profit": price * 999,  # No cap — trailing stop handles exit
            "confidence": min(0.85, 0.45 + (score / 200)),
            "score": score,
            "reason": reason_str,
            "strategy": "pead",
            "rvol": round(rvol, 1),
            "max_hold_bars": 80,
            "bar_seconds": 300,
            "max_hold_days": self.max_hold_days,
            "trailing_stop_pct": 0.035,  # 3.5% trail — drift is slower, give room
        }

    def _cleanup_stale(self):
        """Remove earnings beats past the drift window."""
        today = datetime.now().date()
        stale = [
            sym for sym, beat in self._earnings_beats.items()
            if (today - (beat["date"].date() if isinstance(beat["date"], datetime) else beat["date"])).days > self.drift_window_days
        ]
        for sym in stale:
            del self._earnings_beats[sym]
