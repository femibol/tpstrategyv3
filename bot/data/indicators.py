"""
Technical Indicators - calculation library used by strategies.
Pure numpy implementations for speed.
"""
import numpy as np
from bot.utils.logger import get_logger

log = get_logger("data.indicators")


class TechnicalIndicators:
    """
    Technical indicator calculations.
    All methods accept numpy arrays and return values.
    """

    @staticmethod
    def sma(data, period):
        """Simple Moving Average."""
        if len(data) < period:
            return None
        return np.mean(data[-period:])

    @staticmethod
    def ema(data, period):
        """Exponential Moving Average - returns full array."""
        if len(data) < period:
            return None

        multiplier = 2 / (period + 1)
        ema_values = np.zeros(len(data))
        ema_values[period - 1] = np.mean(data[:period])

        for i in range(period, len(data)):
            ema_values[i] = (data[i] * multiplier) + (ema_values[i - 1] * (1 - multiplier))

        return ema_values

    @staticmethod
    def rsi(data, period=14):
        """Relative Strength Index."""
        if len(data) < period + 1:
            return 50  # neutral default

        deltas = np.diff(data)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return float(rsi)

    @staticmethod
    def atr(highs, lows, closes, period=14):
        """Average True Range - volatility measure."""
        if len(highs) < period + 1:
            return None

        tr_values = []
        for i in range(1, len(highs)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
            tr_values.append(tr)

        if len(tr_values) < period:
            return None

        return float(np.mean(tr_values[-period:]))

    @staticmethod
    def adx(highs, lows, closes, period=14):
        """Average Directional Index - trend strength."""
        if len(highs) < period * 2:
            return None

        plus_dm = []
        minus_dm = []
        tr_values = []

        for i in range(1, len(highs)):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]

            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)

            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
            tr_values.append(tr)

        if len(tr_values) < period:
            return None

        # Smoothed values
        atr = np.mean(tr_values[-period:])
        plus_di = (np.mean(plus_dm[-period:]) / atr * 100) if atr > 0 else 0
        minus_di = (np.mean(minus_dm[-period:]) / atr * 100) if atr > 0 else 0

        di_sum = plus_di + minus_di
        if di_sum == 0:
            return 0

        dx = abs(plus_di - minus_di) / di_sum * 100
        return float(dx)

    @staticmethod
    def bollinger_bands(data, period=20, std_dev=2.0):
        """Bollinger Bands - returns (upper, middle, lower)."""
        if len(data) < period:
            return None, None, None

        middle = np.mean(data[-period:])
        std = np.std(data[-period:])

        upper = middle + (std_dev * std)
        lower = middle - (std_dev * std)

        return float(upper), float(middle), float(lower)

    @staticmethod
    def vwap(highs, lows, closes, volumes):
        """Volume Weighted Average Price (intraday)."""
        typical_price = (highs + lows + closes) / 3
        cumulative_tp_vol = np.cumsum(typical_price * volumes)
        cumulative_vol = np.cumsum(volumes)

        vwap = cumulative_tp_vol / np.where(cumulative_vol > 0, cumulative_vol, 1)
        return vwap

    @staticmethod
    def macd(data, fast=12, slow=26, signal=9):
        """MACD - returns (macd_line, signal_line, histogram)."""
        if len(data) < slow + signal:
            return None, None, None

        fast_ema = TechnicalIndicators.ema(data, fast)
        slow_ema = TechnicalIndicators.ema(data, slow)

        if fast_ema is None or slow_ema is None:
            return None, None, None

        macd_line = fast_ema - slow_ema
        signal_line = TechnicalIndicators.ema(macd_line[slow - 1:], signal)

        if signal_line is None:
            return None, None, None

        # Pad signal line to match length
        padded_signal = np.zeros(len(macd_line))
        padded_signal[-len(signal_line):] = signal_line[-len(signal_line):]

        histogram = macd_line - padded_signal

        return macd_line, padded_signal, histogram

    @staticmethod
    def zscore(data, period=20):
        """Z-Score of the latest value."""
        if len(data) < period:
            return 0

        window = data[-period:]
        mean = np.mean(window)
        std = np.std(window)

        if std == 0:
            return 0

        return float((data[-1] - mean) / std)

    @staticmethod
    def stochastic(highs, lows, closes, k_period=14, d_period=3):
        """Stochastic oscillator - returns (%K, %D)."""
        if len(closes) < k_period:
            return None, None

        highest = np.max(highs[-k_period:])
        lowest = np.min(lows[-k_period:])

        if highest == lowest:
            return 50.0, 50.0

        k = ((closes[-1] - lowest) / (highest - lowest)) * 100

        # %D is SMA of %K (simplified)
        d = k  # Would need history for proper %D

        return float(k), float(d)

    # =============================================
    # Smart Money Concepts (SMC / ICT) Indicators
    # =============================================

    @staticmethod
    def find_swing_points(highs, lows, lookback=5):
        """
        Detect swing highs and swing lows (liquidity levels).

        A swing high at index i: highs[i] is highest in [i-lookback, i+lookback].
        A swing low at index i: lows[i] is lowest in [i-lookback, i+lookback].

        Returns:
            swing_highs: list of (index, price)
            swing_lows: list of (index, price)
        """
        swing_highs = []
        swing_lows = []

        for i in range(lookback, len(highs) - lookback):
            if highs[i] == np.max(highs[i - lookback:i + lookback + 1]):
                swing_highs.append((i, float(highs[i])))
            if lows[i] == np.min(lows[i - lookback:i + lookback + 1]):
                swing_lows.append((i, float(lows[i])))

        return swing_highs, swing_lows

    @staticmethod
    def detect_liquidity_sweep(highs, lows, closes, swing_highs, swing_lows, buffer_pct=0.001):
        """
        Detect liquidity sweeps (stop hunts).

        Bullish sweep: price wicks BELOW a swing low then closes back above.
        Bearish sweep: price wicks ABOVE a swing high then closes back below.

        Returns:
            sweeps: list of dicts
        """
        sweeps = []
        last_idx = len(closes) - 1

        for bar_idx in range(max(0, last_idx - 4), last_idx + 1):
            for sw_idx, sw_price in swing_lows:
                if sw_idx >= bar_idx:
                    continue
                buffer = sw_price * buffer_pct
                if lows[bar_idx] < sw_price - buffer and closes[bar_idx] > sw_price:
                    sweeps.append({
                        "type": "bullish",
                        "bar_idx": bar_idx,
                        "level": sw_price,
                        "wick_low": float(lows[bar_idx]),
                        "close": float(closes[bar_idx]),
                    })

            for sw_idx, sw_price in swing_highs:
                if sw_idx >= bar_idx:
                    continue
                buffer = sw_price * buffer_pct
                if highs[bar_idx] > sw_price + buffer and closes[bar_idx] < sw_price:
                    sweeps.append({
                        "type": "bearish",
                        "bar_idx": bar_idx,
                        "level": sw_price,
                        "wick_high": float(highs[bar_idx]),
                        "close": float(closes[bar_idx]),
                    })

        return sweeps

    @staticmethod
    def detect_fvg(highs, lows, min_size_pct=0.001):
        """
        Detect Fair Value Gaps (FVGs).

        Bullish FVG: highs[i-2] < lows[i] (gap between candle 1 high and candle 3 low)
        Bearish FVG: lows[i-2] > highs[i] (gap between candle 1 low and candle 3 high)

        Returns:
            fvgs: list of dicts with type, index, top, bottom, mid, size_pct
        """
        fvgs = []

        for i in range(2, len(highs)):
            mid_price = (highs[i] + lows[i]) / 2
            if mid_price == 0:
                continue

            if highs[i - 2] < lows[i]:
                size = (lows[i] - highs[i - 2]) / mid_price
                if size >= min_size_pct:
                    fvgs.append({
                        "type": "bullish",
                        "index": i,
                        "top": float(lows[i]),
                        "bottom": float(highs[i - 2]),
                        "mid": float((lows[i] + highs[i - 2]) / 2),
                        "size_pct": round(size * 100, 3),
                    })

            if lows[i - 2] > highs[i]:
                size = (lows[i - 2] - highs[i]) / mid_price
                if size >= min_size_pct:
                    fvgs.append({
                        "type": "bearish",
                        "index": i,
                        "top": float(lows[i - 2]),
                        "bottom": float(highs[i]),
                        "mid": float((lows[i - 2] + highs[i]) / 2),
                        "size_pct": round(size * 100, 3),
                    })

        return fvgs

    @staticmethod
    def detect_cisd(opens, highs, lows, closes):
        """
        Detect Change in State of Delivery (CISD).

        Bullish CISD: After consecutive bearish candles (close < open),
        current candle closes above the high of the bearish series.
        Bearish CISD: After consecutive bullish candles (close > open),
        current candle closes below the low of the bullish series.

        Returns:
            cisd: dict or None
        """
        if len(closes) < 4:
            return None

        last = len(closes) - 1

        # Bullish CISD: bearish run then bullish break
        bearish_run_high = 0
        bearish_count = 0
        for j in range(last - 1, max(last - 8, 0), -1):
            if closes[j] < opens[j]:
                bearish_count += 1
                bearish_run_high = max(bearish_run_high, highs[j])
            else:
                break

        if bearish_count >= 2 and closes[last] > bearish_run_high:
            return {
                "type": "bullish",
                "index": last,
                "shift_level": float(bearish_run_high),
                "close": float(closes[last]),
                "candles_broken": bearish_count,
            }

        # Bearish CISD: bullish run then bearish break
        bullish_run_low = float("inf")
        bullish_count = 0
        for j in range(last - 1, max(last - 8, 0), -1):
            if closes[j] > opens[j]:
                bullish_count += 1
                bullish_run_low = min(bullish_run_low, lows[j])
            else:
                break

        if bullish_count >= 2 and closes[last] < bullish_run_low:
            return {
                "type": "bearish",
                "index": last,
                "shift_level": float(bullish_run_low),
                "close": float(closes[last]),
                "candles_broken": bullish_count,
            }

        return None

    @staticmethod
    def detect_displacement(opens, closes, atr_val, min_body_atr=1.5):
        """
        Detect displacement candles (strong aggressive institutional moves).

        A displacement candle has a body >= min_body_atr * ATR.
        Usually creates FVGs and signals institutional intent.
        """
        displacements = []
        if atr_val is None or atr_val <= 0:
            return displacements

        for i in range(max(0, len(closes) - 5), len(closes)):
            body = abs(closes[i] - opens[i])
            if body >= min_body_atr * atr_val:
                displacements.append({
                    "index": i,
                    "type": "bullish" if closes[i] > opens[i] else "bearish",
                    "body": round(float(body), 4),
                    "body_atr_ratio": round(body / atr_val, 2),
                })

        return displacements

    @staticmethod
    def detect_smt_divergence(lows_a, highs_a, lows_b, highs_b, lookback=10):
        """
        Detect Smart Money Technique (SMT) Divergence between two correlated markets.

        Bullish SMT: Market A makes a lower low, Market B makes a higher low.
        Bearish SMT: Market A makes a higher high, Market B makes a lower high.
        """
        min_len = min(len(lows_a), len(lows_b), len(highs_a), len(highs_b))
        if min_len < lookback + 2:
            return None

        recent_low_a = np.min(lows_a[-lookback:])
        prev_low_a = np.min(lows_a[-lookback * 2:-lookback]) if min_len >= lookback * 2 else np.min(lows_a[:lookback])
        recent_low_b = np.min(lows_b[-lookback:])
        prev_low_b = np.min(lows_b[-lookback * 2:-lookback]) if min_len >= lookback * 2 else np.min(lows_b[:lookback])

        # Bullish SMT: A makes lower low, B refuses (higher low)
        if recent_low_a < prev_low_a and recent_low_b > prev_low_b:
            return {
                "type": "bullish",
                "desc": "A=lower low, B=higher low",
                "a_low": round(float(recent_low_a), 2),
                "b_low": round(float(recent_low_b), 2),
            }

        recent_high_a = np.max(highs_a[-lookback:])
        prev_high_a = np.max(highs_a[-lookback * 2:-lookback]) if min_len >= lookback * 2 else np.max(highs_a[:lookback])
        recent_high_b = np.max(highs_b[-lookback:])
        prev_high_b = np.max(highs_b[-lookback * 2:-lookback]) if min_len >= lookback * 2 else np.max(highs_b[:lookback])

        # Bearish SMT: A makes higher high, B refuses (lower high)
        if recent_high_a > prev_high_a and recent_high_b < prev_high_b:
            return {
                "type": "bearish",
                "desc": "A=higher high, B=lower high",
                "a_high": round(float(recent_high_a), 2),
                "b_high": round(float(recent_high_b), 2),
            }

        return None
