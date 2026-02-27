"""
Short Squeeze Detection Strategy

Finds stocks with high short interest that are starting to squeeze:
- Short interest > 15% of float
- Rising volume + positive price action (shorts covering)
- Low float amplifies the squeeze
- Days to cover > 3 (shorts need time to exit)

Uses Polygon short interest data + existing float/volume infrastructure.
"""
import time
from datetime import datetime

from bot.strategies.base import BaseStrategy
from bot.utils.logger import get_logger

log = get_logger("strategy.short_squeeze")


class ShortSqueezeStrategy(BaseStrategy):
    """Detects and trades short squeeze setups."""

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)

        # Config
        self.min_short_pct = config.get("min_short_pct", 15.0)
        self.min_rvol = config.get("min_rvol", 2.0)
        self.min_score = config.get("min_score", 55)
        self.min_price = config.get("min_price", 1.00)
        self.max_price = config.get("max_price", 100.00)
        self.min_volume = config.get("min_volume", 100000)
        self.max_trades_per_day = config.get("max_trades_per_day", 8)
        self.atr_stop_multiplier = config.get("atr_stop_multiplier", 2.0)
        self.atr_target_multiplier = config.get("atr_target_multiplier", 6.0)
        self.max_hold_days = config.get("max_hold_days", 5)

        # State
        self._short_interest_cache = {}  # symbol -> {short_pct, shares_short, days_to_cover, updated}
        self._signals_today = 0
        self._last_reset = None
        self._dynamic_symbols = set()

    def add_dynamic_symbols(self, symbols):
        """Add dynamically discovered symbols from scanner."""
        self._dynamic_symbols.update(symbols)

    def get_symbols(self):
        return list(set(self.symbols) | self._dynamic_symbols)

    def feed_short_interest(self, data):
        """Feed short interest data from Polygon or other source.

        Args:
            data: dict of {symbol: {short_pct, shares_short, days_to_cover, avg_daily_volume}}
        """
        now = time.time()
        for sym, info in data.items():
            self._short_interest_cache[sym] = {
                **info,
                "updated": now,
            }

    def generate_signals(self, market_data):
        """Scan for short squeeze candidates and generate signals."""
        if not self.enabled:
            return []

        # Reset daily counter
        today = datetime.now().date()
        if self._last_reset != today:
            self._signals_today = 0
            self._last_reset = today

        if self._signals_today >= self.max_trades_per_day:
            return []

        signals = []
        all_symbols = self.get_symbols()

        for symbol in all_symbols:
            try:
                result = self._analyze_squeeze(symbol, market_data)
                if result:
                    signals.append(result)
                    self._signals_today += 1
                    if self._signals_today >= self.max_trades_per_day:
                        break
            except Exception as e:
                log.debug(f"Squeeze analysis error for {symbol}: {e}")

        signals.sort(key=lambda x: x.get("score", 0), reverse=True)
        return signals

    def _analyze_squeeze(self, symbol, market_data):
        """Analyze a symbol for short squeeze potential."""
        price = market_data.get_price(symbol) if market_data else None
        if not price or price < self.min_price or price > self.max_price:
            return None

        # Get short interest data
        si = self._short_interest_cache.get(symbol)
        if not si:
            return None

        short_pct = si.get("short_pct", 0)
        days_to_cover = si.get("days_to_cover", 0)

        if short_pct < self.min_short_pct:
            return None

        # Get volume data
        volume = market_data.get_volume(symbol) if market_data else 0
        if not volume or volume < self.min_volume:
            return None

        # Get RVOL from scanner data
        snap = None
        if hasattr(market_data, 'scanner') and market_data.scanner:
            snap = market_data.scanner.get_snapshot(symbol)

        rvol = 0
        change_pct = 0
        float_shares = 0
        if snap:
            avg_vol = snap.get("avg_volume", 1)
            rvol = volume / avg_vol if avg_vol > 0 else 0
            change_pct = snap.get("change_pct", 0)
            float_shares = snap.get("float_shares", 0)
        elif hasattr(market_data, 'scanner') and market_data.scanner:
            float_shares = market_data.scanner.get_float(symbol)

        # Must have rising volume and positive price action
        if rvol < self.min_rvol or change_pct <= 0:
            return None

        # --- Squeeze Score (0-100) ---
        score = 0
        reasons = []

        # Short interest component (max 30 pts)
        if short_pct >= 40:
            score += 30
            reasons.append(f"Extreme SI {short_pct:.0f}%")
        elif short_pct >= 30:
            score += 25
            reasons.append(f"Very high SI {short_pct:.0f}%")
        elif short_pct >= 20:
            score += 20
            reasons.append(f"High SI {short_pct:.0f}%")
        elif short_pct >= 15:
            score += 12
            reasons.append(f"Elevated SI {short_pct:.0f}%")

        # Days to cover (max 20 pts) — higher = more squeeze pressure
        if days_to_cover >= 10:
            score += 20
            reasons.append(f"DTC {days_to_cover:.1f} (extreme)")
        elif days_to_cover >= 5:
            score += 15
            reasons.append(f"DTC {days_to_cover:.1f}")
        elif days_to_cover >= 3:
            score += 10
            reasons.append(f"DTC {days_to_cover:.1f}")

        # Volume surge (max 20 pts) — covering creates volume spikes
        if rvol >= 5.0:
            score += 20
            reasons.append(f"RVOL {rvol:.1f}x (covering)")
        elif rvol >= 3.0:
            score += 15
            reasons.append(f"RVOL {rvol:.1f}x")
        elif rvol >= 2.0:
            score += 10
            reasons.append(f"RVOL {rvol:.1f}x")

        # Price momentum (max 15 pts)
        if change_pct >= 10.0:
            score += 15
            reasons.append(f"Up +{change_pct:.1f}% (squeeze underway)")
        elif change_pct >= 5.0:
            score += 10
            reasons.append(f"Up +{change_pct:.1f}%")
        elif change_pct >= 2.0:
            score += 5
            reasons.append(f"Up +{change_pct:.1f}%")

        # Low float bonus (max 15 pts) — fewer shares = harder squeeze
        if float_shares > 0:
            if float_shares < 5_000_000:
                score += 15
                reasons.append(f"Tiny float {float_shares/1e6:.1f}M")
            elif float_shares < 15_000_000:
                score += 10
                reasons.append(f"Low float {float_shares/1e6:.1f}M")
            elif float_shares < 30_000_000:
                score += 5
                reasons.append(f"Float {float_shares/1e6:.1f}M")

        # Store scan result for dashboard
        self.scan_results[symbol] = {
            "symbol": symbol,
            "price": price,
            "short_pct": short_pct,
            "days_to_cover": days_to_cover,
            "rvol": round(rvol, 1),
            "change_pct": round(change_pct, 1),
            "float_shares": float_shares,
            "score": score,
            "verdict": "SQUEEZE" if score >= self.min_score else "WATCH",
        }

        if score < self.min_score:
            return None

        # Calculate stop and target using ATR if available
        data = market_data.get_data(symbol) if market_data else None
        atr = 0
        if data is not None and len(data) >= 14 and "high" in data.columns:
            try:
                from bot.indicators.technical import TechnicalIndicators
                ti = TechnicalIndicators()
                atr = ti.atr(data, period=14).iloc[-1] if hasattr(ti, 'atr') else 0
            except Exception:
                atr = price * 0.03  # Fallback: 3% of price

        if atr <= 0:
            atr = price * 0.03

        stop_loss = price - (atr * self.atr_stop_multiplier)
        # No hard TP — trailing stop handles exit
        take_profit = price * 999  # Effectively infinite

        reason_str = "Short Squeeze: " + ", ".join(reasons)

        return {
            "symbol": symbol,
            "action": "buy",
            "price": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "confidence": min(0.90, 0.50 + (score / 200)),
            "score": score,
            "reason": reason_str,
            "strategy": "short_squeeze",
            "rvol": round(rvol, 1),
            "max_hold_bars": 40,
            "bar_seconds": 300,
            "max_hold_days": self.max_hold_days,
            "trailing_stop_pct": 0.04,  # 4% trail — squeezes are volatile
            "breakout_play": True,  # Gets wider trail treatment
        }
