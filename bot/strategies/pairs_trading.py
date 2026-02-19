"""
Pairs Trading (Statistical Arbitrage) Strategy
- Market-neutral strategy used by hedge funds
- Trade the spread between correlated stocks
- Buy the underperformer, short the outperformer
- Profit when spread reverts to mean
"""
import numpy as np
from bot.strategies.base import BaseStrategy
from bot.utils.logger import get_logger

log = get_logger("strategy.pairs")


class PairsTradingStrategy(BaseStrategy):
    """
    Statistical Arbitrage - market neutral like Citadel/DE Shaw.

    Logic:
    1. Track price ratio/spread between correlated pairs
    2. Calculate Z-score of current spread
    3. When spread diverges (Z > 2): short outperformer, buy underperformer
    4. When spread converges (Z < 0.5): close both legs
    5. Market neutral = profit regardless of market direction
    """

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)
        self.lookback = config.get("lookback_period", 60)
        self.min_correlation = config.get("min_correlation", 0.80)
        self.entry_zscore = config.get("entry_zscore", 2.0)
        self.exit_zscore = config.get("exit_zscore", 0.5)
        self.stop_zscore = config.get("stop_zscore", 3.5)
        self.max_hold = config.get("max_holding_bars", 30)
        self.pairs = config.get("pairs", [])
        self.active_pairs = {}

        # Override symbols - extract from pairs
        self.symbols = list(set(
            sym for pair in self.pairs for sym in pair
        ))

    def generate_signals(self, market_data):
        signals = []

        for pair in self.pairs:
            try:
                pair_signals = self._analyze_pair(pair[0], pair[1], market_data)
                signals.extend(pair_signals)
            except Exception as e:
                log.debug(f"Error analyzing pair {pair}: {e}")

        return signals

    def _analyze_pair(self, symbol_a, symbol_b, market_data):
        """Analyze a pair for statistical arbitrage opportunity."""
        signals = []
        pair_key = f"{symbol_a}/{symbol_b}"

        bars_a = market_data.get_bars(symbol_a, self.lookback + 10)
        bars_b = market_data.get_bars(symbol_b, self.lookback + 10)

        if bars_a is None or bars_b is None:
            self.scan_results[pair_key] = {"status": "no_data", "verdict": "WAIT"}
            return signals

        if len(bars_a) < self.lookback or len(bars_b) < self.lookback:
            return signals

        closes_a = bars_a["close"].values[-self.lookback:]
        closes_b = bars_b["close"].values[-self.lookback:]

        # Check correlation
        correlation = np.corrcoef(closes_a, closes_b)[0, 1]

        # Calculate spread (ratio method - more stable than difference)
        ratio = closes_a / np.where(closes_b > 0, closes_b, 1)
        mean_ratio = np.mean(ratio)
        std_ratio = np.std(ratio)

        if std_ratio == 0:
            return signals

        current_ratio = ratio[-1]
        zscore = (current_ratio - mean_ratio) / std_ratio

        price_a = closes_a[-1]
        price_b = closes_b[-1]

        # Determine verdict
        is_active = pair_key in self.active_pairs
        if abs(correlation) < self.min_correlation:
            verdict = "LOW CORR"
        elif is_active and abs(zscore) <= self.exit_zscore:
            verdict = "EXIT SIGNAL"
        elif is_active and abs(zscore) >= self.stop_zscore:
            verdict = "STOP SIGNAL"
        elif not is_active and abs(zscore) >= self.entry_zscore:
            verdict = "ENTRY SIGNAL"
        elif abs(zscore) >= self.entry_zscore * 0.7:
            verdict = "WARMING UP"
        else:
            verdict = "NEUTRAL"

        self.scan_results[pair_key] = {
            "symbol_a": symbol_a,
            "symbol_b": symbol_b,
            "price_a": round(price_a, 2),
            "price_b": round(price_b, 2),
            "correlation": round(correlation, 3),
            "zscore": round(zscore, 2),
            "ratio": round(current_ratio, 4),
            "mean_ratio": round(mean_ratio, 4),
            "is_active": is_active,
            "entry_threshold": self.entry_zscore,
            "verdict": verdict,
        }

        if abs(correlation) < self.min_correlation:
            return signals

        # --- ENTRY: Spread diverged ---
        if abs(zscore) >= self.entry_zscore and pair_key not in self.active_pairs:
            if zscore > self.entry_zscore:
                # A is expensive relative to B
                # Short A, Buy B
                signals.append({
                    "symbol": symbol_a,
                    "action": "short",
                    "price": price_a,
                    "stop_loss": price_a * 1.03,
                    "take_profit": price_a * 0.96,
                    "confidence": min(1.0, abs(zscore) / 3.0),
                    "reason": (
                        f"Pairs SHORT {symbol_a}: {pair_key} Z={zscore:.2f}, "
                        f"corr={correlation:.2f}"
                    ),
                    "max_hold_bars": self.max_hold,
                    "bar_seconds": self._timeframe_to_seconds(),
                    "max_hold_days": 5,  # Pairs trades: max 5 days
                    "pair": pair_key,
                    "pair_leg": "short",
                })
                signals.append({
                    "symbol": symbol_b,
                    "action": "buy",
                    "price": price_b,
                    "stop_loss": price_b * 0.97,
                    "take_profit": price_b * 1.04,
                    "confidence": min(1.0, abs(zscore) / 3.0),
                    "reason": (
                        f"Pairs BUY {symbol_b}: {pair_key} Z={zscore:.2f}, "
                        f"corr={correlation:.2f}"
                    ),
                    "max_hold_bars": self.max_hold,
                    "bar_seconds": self._timeframe_to_seconds(),
                    "max_hold_days": 5,  # Pairs trades: max 5 days
                    "pair": pair_key,
                    "pair_leg": "long",
                })
                self.active_pairs[pair_key] = {"zscore_entry": zscore, "direction": "short_a"}
                log.info(
                    f"PAIRS ENTRY: Short {symbol_a}, Buy {symbol_b} | "
                    f"Z={zscore:.2f} | Corr={correlation:.2f}"
                )

            elif zscore < -self.entry_zscore:
                # B is expensive relative to A
                # Buy A, Short B
                signals.append({
                    "symbol": symbol_a,
                    "action": "buy",
                    "price": price_a,
                    "stop_loss": price_a * 0.97,
                    "take_profit": price_a * 1.04,
                    "confidence": min(1.0, abs(zscore) / 3.0),
                    "reason": (
                        f"Pairs BUY {symbol_a}: {pair_key} Z={zscore:.2f}, "
                        f"corr={correlation:.2f}"
                    ),
                    "max_hold_bars": self.max_hold,
                    "bar_seconds": self._timeframe_to_seconds(),
                    "max_hold_days": 5,  # Pairs trades: max 5 days
                    "pair": pair_key,
                    "pair_leg": "long",
                })
                signals.append({
                    "symbol": symbol_b,
                    "action": "short",
                    "price": price_b,
                    "stop_loss": price_b * 1.03,
                    "take_profit": price_b * 0.96,
                    "confidence": min(1.0, abs(zscore) / 3.0),
                    "reason": (
                        f"Pairs SHORT {symbol_b}: {pair_key} Z={zscore:.2f}, "
                        f"corr={correlation:.2f}"
                    ),
                    "max_hold_bars": self.max_hold,
                    "bar_seconds": self._timeframe_to_seconds(),
                    "max_hold_days": 5,  # Pairs trades: max 5 days
                    "pair": pair_key,
                    "pair_leg": "short",
                })
                self.active_pairs[pair_key] = {"zscore_entry": zscore, "direction": "short_b"}
                log.info(
                    f"PAIRS ENTRY: Buy {symbol_a}, Short {symbol_b} | "
                    f"Z={zscore:.2f} | Corr={correlation:.2f}"
                )

        # --- EXIT: Spread converged ---
        elif pair_key in self.active_pairs and abs(zscore) <= self.exit_zscore:
            direction = self.active_pairs[pair_key]["direction"]
            exit_reason = f"Pairs EXIT: {pair_key} converged Z={zscore:.2f}"
            if direction == "short_a":
                # Close short A (buy to cover), close long B (sell)
                signals.append({
                    "symbol": symbol_a, "action": "cover", "price": price_a,
                    "confidence": 0.9, "source": "exit", "reason": exit_reason,
                    "pair": pair_key,
                })
                signals.append({
                    "symbol": symbol_b, "action": "sell", "price": price_b,
                    "confidence": 0.9, "source": "exit", "reason": exit_reason,
                    "pair": pair_key,
                })
            else:
                # Close long A (sell), close short B (buy to cover)
                signals.append({
                    "symbol": symbol_a, "action": "sell", "price": price_a,
                    "confidence": 0.9, "source": "exit", "reason": exit_reason,
                    "pair": pair_key,
                })
                signals.append({
                    "symbol": symbol_b, "action": "cover", "price": price_b,
                    "confidence": 0.9, "source": "exit", "reason": exit_reason,
                    "pair": pair_key,
                })

            del self.active_pairs[pair_key]
            log.info(f"PAIRS EXIT: {pair_key} | Z converged to {zscore:.2f}")

        # --- STOP: Spread widened too much ---
        elif pair_key in self.active_pairs and abs(zscore) >= self.stop_zscore:
            direction = self.active_pairs[pair_key]["direction"]
            stop_reason = f"Pairs STOP: {pair_key} Z={zscore:.2f}"
            if direction == "short_a":
                signals.append({
                    "symbol": symbol_a, "action": "cover", "price": price_a,
                    "confidence": 1.0, "source": "exit", "reason": stop_reason,
                    "pair": pair_key,
                })
                signals.append({
                    "symbol": symbol_b, "action": "sell", "price": price_b,
                    "confidence": 1.0, "source": "exit", "reason": stop_reason,
                    "pair": pair_key,
                })
            else:
                signals.append({
                    "symbol": symbol_a, "action": "sell", "price": price_a,
                    "confidence": 1.0, "source": "exit", "reason": stop_reason,
                    "pair": pair_key,
                })
                signals.append({
                    "symbol": symbol_b, "action": "cover", "price": price_b,
                    "confidence": 1.0, "source": "exit", "reason": stop_reason,
                    "pair": pair_key,
                })

            del self.active_pairs[pair_key]
            log.warning(f"PAIRS STOP: {pair_key} | Z widened to {zscore:.2f}")

        return signals

    def _timeframe_to_seconds(self):
        tf = self.timeframe
        if "m" in tf:
            return int(tf.replace("m", "")) * 60
        elif "h" in tf:
            return int(tf.replace("h", "")) * 3600
        return 900
