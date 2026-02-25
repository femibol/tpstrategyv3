"""
Options Momentum Strategy - Buy calls on strong breakouts, puts on breakdowns.

Uses the existing IBKR options infrastructure to trade options on high-conviction
momentum signals. Only triggers on the strongest setups:
- High RVOL (3x+) breakouts → buy near-ATM calls (2-3 weeks out)
- Strong breakdown with volume → buy near-ATM puts (2-3 weeks out)

Key risk controls:
- Max 2% of account per options trade (options are leveraged)
- Only liquid underlyings (top 50 optionable stocks)
- Near-the-money strikes (delta ~0.50-0.60) for best risk/reward
- 10-20 DTE (days to expiry) — enough time without excessive theta
- Hard stop at 50% loss on the option premium
- Take profit at 100% gain (double your money)
"""
import numpy as np
from datetime import datetime, timedelta
from bot.strategies.base import BaseStrategy
from bot.utils.logger import get_logger

log = get_logger("strategy.options_momentum")

# Highly liquid optionable stocks (tight spreads, high OI)
DEFAULT_OPTIONS_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA",
    "AMD", "NFLX", "SPY", "QQQ", "IWM", "COIN", "SQ", "SHOP",
    "PLTR", "SOFI", "ROKU", "SNAP", "UBER", "ABNB", "DKNG",
    "MARA", "RIOT", "ARM", "SMCI", "MU", "INTC", "BA", "DIS",
]


class OptionsMomentumStrategy(BaseStrategy):
    """
    Momentum-based options strategy: buy calls on breakouts, puts on breakdowns.

    Logic:
    1. Scan watchlist for high-RVOL momentum (3x+ relative volume)
    2. Confirm direction: 3+ consecutive bars in direction + trend alignment
    3. Buy near-ATM calls (bullish) or puts (bearish) with 10-20 DTE
    4. Risk max 2% of account per trade (options premium)
    5. Stop at -50% premium, target +100% premium
    6. Max 3 options positions at a time
    """

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)
        self.min_rvol = config.get("min_rvol", 3.0)
        self.min_score = config.get("min_score", 70)
        self.max_premium_pct = config.get("max_premium_pct", 0.02)  # 2% of account max
        self.target_dte_min = config.get("target_dte_min", 10)
        self.target_dte_max = config.get("target_dte_max", 21)
        self.stop_loss_pct = config.get("stop_loss_pct", 0.50)  # 50% of premium
        self.take_profit_pct = config.get("take_profit_pct", 1.00)  # 100% gain
        self.max_positions = config.get("max_options_positions", 3)
        self.max_trades_per_day = config.get("max_trades_per_day", 3)
        self.trades_today = 0
        self.last_trade_date = None
        self.active_options = 0

        # Universe of optionable stocks
        self._options_universe = set(
            config.get("options_universe", DEFAULT_OPTIONS_UNIVERSE)
        )

    def add_dynamic_symbols(self, symbols):
        """Add dynamically discovered symbols to options universe."""
        for sym in symbols:
            if sym and isinstance(sym, str) and len(sym) <= 5:
                self._options_universe.add(sym.upper())

    def get_symbols(self):
        """Return combined static + dynamic options universe."""
        return list(set(self.symbols) | self._options_universe)

    def generate_signals(self, market_data):
        """Scan for high-conviction momentum setups to play with options."""
        signals = []

        # Reset daily counter
        today = datetime.now().date()
        if self.last_trade_date != today:
            self.trades_today = 0
            self.last_trade_date = today

        if self.trades_today >= self.max_trades_per_day:
            return signals

        if self.active_options >= self.max_positions:
            return signals

        all_symbols = self.get_symbols()

        for symbol in all_symbols:
            try:
                result = self._analyze_options_setup(symbol, market_data)
                if result:
                    self.scan_results[symbol] = result["scan"]
                    if result.get("signal"):
                        signals.append(result["signal"])
                        self.trades_today += 1
                        if self.trades_today >= self.max_trades_per_day:
                            break
            except Exception as e:
                log.debug(f"Options analysis error for {symbol}: {e}")

        return signals

    def _analyze_options_setup(self, symbol, market_data):
        """Analyze a symbol for options-worthy momentum setup."""
        bars = market_data.get_bars(symbol, 60) if market_data else None
        if bars is None or len(bars) < 20:
            return None

        closes = bars["close"].values
        volumes = bars["volume"].values
        highs = bars["high"].values
        lows = bars["low"].values
        opens = bars["open"].values

        current_price = float(closes[-1])
        if current_price <= 0 or current_price < 5.0:
            return None  # Options on sub-$5 stocks have terrible liquidity

        # --- RVOL ---
        avg_vol = float(np.mean(volumes[-20:-1])) if len(volumes) > 20 else float(np.mean(volumes[:-1]))
        current_vol = float(volumes[-1])
        rvol = round(current_vol / avg_vol, 2) if avg_vol > 0 else 0

        # --- Price Action ---
        prev_close = float(closes[-2])
        change_pct = round((current_price - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0

        # ATR
        atr = self.indicators.atr(highs, lows, closes, period=14)
        atr_pct = round(atr / current_price * 100, 2) if atr else 0

        # RSI
        rsi = self.indicators.rsi(closes, 14)

        # EMA trend
        ema9 = self.indicators.ema(closes, 9)
        ema21 = self.indicators.ema(closes, 21)
        ema50 = self.indicators.ema(closes, 50) if len(closes) >= 50 else None

        bullish_trend = (ema9 is not None and ema21 is not None and
                         ema9[-1] > ema21[-1])
        strong_trend = (bullish_trend and ema50 is not None and
                        ema21[-1] > ema50[-1])

        # --- Consecutive direction bars ---
        consec_up = 0
        consec_down = 0
        for i in range(1, min(6, len(closes))):
            if closes[-i] > opens[-i]:
                consec_up += 1
            else:
                break
        for i in range(1, min(6, len(closes))):
            if closes[-i] < opens[-i]:
                consec_down += 1
            else:
                break

        # --- Volume surge pattern ---
        vol_surge = (len(volumes) >= 3 and
                     volumes[-1] > volumes[-2] > volumes[-3] and
                     volumes[-1] > avg_vol * 2)

        # --- Composite Score ---
        score = 0
        score_reasons = []
        direction = None

        # BULLISH setup
        if change_pct > 0.5 and bullish_trend:
            direction = "CALL"

            if rvol >= 5.0:
                score += 30
                score_reasons.append(f"Extreme RVOL {rvol:.1f}x")
            elif rvol >= 3.0:
                score += 25
                score_reasons.append(f"High RVOL {rvol:.1f}x")
            elif rvol >= 2.0:
                score += 15
                score_reasons.append(f"Elevated RVOL {rvol:.1f}x")

            if consec_up >= 3:
                score += 20
                score_reasons.append(f"{consec_up} consecutive green bars")
            elif consec_up >= 2:
                score += 10

            if strong_trend:
                score += 15
                score_reasons.append("Strong uptrend (EMA 9>21>50)")
            elif bullish_trend:
                score += 10
                score_reasons.append("Bullish EMA alignment")

            if vol_surge:
                score += 15
                score_reasons.append("Volume surge pattern")

            if 40 < rsi < 70:
                score += 10
                score_reasons.append(f"RSI sweet spot ({rsi:.0f})")
            elif rsi > 75:
                score -= 10  # Overbought penalty

            if change_pct > 3.0:
                score += 10
                score_reasons.append(f"Strong move +{change_pct:.1f}%")

        # BEARISH setup
        elif change_pct < -0.5 and not bullish_trend:
            direction = "PUT"

            if rvol >= 5.0:
                score += 30
                score_reasons.append(f"Extreme sell RVOL {rvol:.1f}x")
            elif rvol >= 3.0:
                score += 25
                score_reasons.append(f"High sell RVOL {rvol:.1f}x")

            if consec_down >= 3:
                score += 20
                score_reasons.append(f"{consec_down} consecutive red bars")

            if not bullish_trend and ema50 is not None and ema21[-1] < ema50[-1]:
                score += 15
                score_reasons.append("Strong downtrend")

            if vol_surge:
                score += 15
                score_reasons.append("Selling volume surge")

            if rsi < 35:
                score += 10
                score_reasons.append(f"RSI oversold ({rsi:.0f})")

            if change_pct < -3.0:
                score += 10
                score_reasons.append(f"Strong drop {change_pct:.1f}%")

        scan_result = {
            "price": round(current_price, 2),
            "rvol": rvol,
            "change_pct": change_pct,
            "direction": direction or "NEUTRAL",
            "rsi": round(rsi, 1) if rsi else 0,
            "atr_pct": atr_pct,
            "score": score,
            "verdict": "OPTIONS SIGNAL" if (direction and score >= self.min_score
                                            and rvol >= self.min_rvol) else "WATCHING",
            "reasons": score_reasons,
        }

        result = {"scan": scan_result, "signal": None}

        # Generate options signal
        if direction and score >= self.min_score and rvol >= self.min_rvol:
            # Calculate option parameters
            right = "C" if direction == "CALL" else "P"
            action = "buy"  # Always buying options (defined risk)

            # Strike: near ATM (round to nearest $1 or $5 depending on price)
            if current_price < 50:
                strike_round = 1
            elif current_price < 200:
                strike_round = 5
            else:
                strike_round = 10

            if direction == "CALL":
                # Slightly ITM call for higher delta
                strike = round((current_price - strike_round * 0.5) / strike_round) * strike_round
            else:
                # Slightly ITM put
                strike = round((current_price + strike_round * 0.5) / strike_round) * strike_round

            # Expiry: 2-3 weeks out (format YYYYMMDD)
            target_dte = 14  # 2 weeks default
            expiry_date = datetime.now() + timedelta(days=target_dte)
            # Snap to nearest Friday
            days_to_friday = (4 - expiry_date.weekday()) % 7
            if days_to_friday == 0:
                days_to_friday = 7
            expiry_date += timedelta(days=days_to_friday)
            expiry = expiry_date.strftime("%Y%m%d")

            # Estimate premium (~ATR * 1.5 for rough sizing)
            est_premium = atr * 1.5 if atr else current_price * 0.03
            max_spend = self.allocated_capital * self.max_premium_pct
            qty = max(1, int(max_spend / (est_premium * 100)))  # Each contract = 100 shares

            # Stop and target on the option premium
            stop_loss_premium = est_premium * (1 - self.stop_loss_pct)
            take_profit_premium = est_premium * (1 + self.take_profit_pct)

            confidence = min(1.0, score / 100)

            result["signal"] = {
                "symbol": symbol,
                "action": action,
                "price": current_price,
                "confidence": round(confidence, 2),
                "reason": f"OPTIONS {direction} | " + " | ".join(score_reasons[:3]),
                "source": "options_momentum",
                "strategy": "options_momentum",
                "rvol": rvol,
                # Options-specific fields
                "asset_type": "option",
                "right": right,
                "strike": strike,
                "expiry": expiry,
                "estimated_premium": round(est_premium, 2),
                "quantity": qty,
                # Risk management
                "stop_loss": round(stop_loss_premium, 2),
                "take_profit": round(take_profit_premium, 2),
                "max_hold_days": target_dte,
                "max_hold_bars": target_dte * 78,  # ~78 five-min bars per day
                "bar_seconds": 300,
            }

            self.signals_generated += 1
            self.active_options += 1
            log.info(
                f"OPTIONS SIGNAL: {direction} {symbol} | Strike: ${strike} | "
                f"Expiry: {expiry} | Score: {score} | RVOL: {rvol:.1f}x | "
                f"Est Premium: ${est_premium:.2f} x {qty} contracts"
            )

        return result
