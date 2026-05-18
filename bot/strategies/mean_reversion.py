"""
Mean Reversion Strategy
- Buy when price drops significantly below its mean (oversold)
- Sell when price returns to mean
- Uses RSI, Bollinger Bands, and Z-Score
- Best for range-bound markets with liquid stocks
"""
import time
import numpy as np
from bot.strategies.base import BaseStrategy
from bot.utils.logger import get_logger

log = get_logger("strategy.mean_reversion")


class MeanReversionStrategy(BaseStrategy):
    """
    Mean Reversion - the bread and butter for small accounts.

    Logic:
    1. Calculate Z-score of price relative to moving average
    2. Check RSI for oversold/overbought
    3. Check if price is at Bollinger Band extremes
    4. Enter when multiple indicators confirm oversold
    5. Exit when price reverts to mean
    """

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)
        self.lookback = config.get("lookback_period", 20)
        self.entry_zscore = config.get("entry_zscore", -2.0)
        self.exit_zscore = config.get("exit_zscore", 0.0)
        self.rsi_oversold = config.get("rsi_oversold", 30)
        self.rsi_overbought = config.get("rsi_overbought", 70)
        # Crypto-specific (looser) thresholds. Crypto's volatility profile and
        # 24/7 trading mean its RSI rarely dips below 30 the way thin-float
        # equities do (only in panic crashes). Z-scores also compress because
        # the 5-min mean shifts with the trend. Looser defaults: Z≤-1.2 + RSI<45.
        # Applied per-symbol via _is_crypto check in _analyze_symbol.
        self.entry_zscore_crypto = config.get("entry_zscore_crypto", -1.2)
        self.rsi_oversold_crypto = config.get("rsi_oversold_crypto", 45)
        self.rsi_overbought_crypto = config.get("rsi_overbought_crypto", 55)
        self.bb_period = config.get("bollinger_period", 20)
        self.bb_std = config.get("bollinger_std", 2.0)
        self.max_hold = config.get("max_holding_periods", 20)

        # Dynamic symbols from IBKR scanner (losers + active). Mean reversion
        # on a static 13-symbol list rarely fires; live-discovered pullback
        # candidates are where this strategy actually earns.
        self._dynamic_symbols = set()
        self._dynamic_symbol_timestamps = {}
        self._max_dynamic_symbols = 50

    def add_dynamic_symbols(self, symbols):
        now = time.time()
        for sym in symbols:
            if sym and isinstance(sym, str):
                s = sym.upper()
                self._dynamic_symbols.add(s)
                self._dynamic_symbol_timestamps[s] = now
        if len(self._dynamic_symbols) > self._max_dynamic_symbols:
            # Crypto symbols are pinned: equity discovery runs after crypto
            # injection in the same cycle, so equity timestamps are always
            # newer than crypto, and a plain newest-wins eviction silently
            # drops the entire crypto universe whenever it can. Keep crypto
            # unconditionally; the cap applies to equity only.
            crypto_suffixes = ("-USD", "-USDT", "-BTC", "-ETH")
            crypto_pinned = {
                s for s in self._dynamic_symbols
                if any(s.endswith(suf) for suf in crypto_suffixes)
            }
            equity_cap = max(0, self._max_dynamic_symbols - len(crypto_pinned))
            equity_sorted = sorted(
                (
                    (s, t) for s, t in self._dynamic_symbol_timestamps.items()
                    if s not in crypto_pinned
                ),
                key=lambda x: -x[1],
            )
            keep = crypto_pinned | {s for s, _ in equity_sorted[:equity_cap]}
            self._dynamic_symbols = keep
            self._dynamic_symbol_timestamps = {
                s: t for s, t in self._dynamic_symbol_timestamps.items() if s in keep
            }

    def get_symbols(self):
        return list(set(self.symbols) | self._dynamic_symbols)

    def generate_signals(self, market_data):
        signals = []

        for symbol in self.get_symbols():
            try:
                sig = self._analyze_symbol(symbol, market_data)
                if sig:
                    signals.append(sig)
            except Exception as e:
                log.debug(f"Error analyzing {symbol}: {e}")

        return signals

    def _analyze_symbol(self, symbol, market_data):
        """Analyze a single symbol for mean reversion entry/exit."""
        bars = market_data.get_bars(symbol, self.lookback + 10)
        if bars is None or len(bars) < self.lookback:
            self.scan_results[symbol] = {"status": "no_data", "verdict": "WAIT"}
            return None

        # Pick the right threshold set: crypto gets the looser one (its
        # volatility/RSI profile makes the equity thresholds basically
        # never fire).
        _is_crypto = any(
            symbol.upper().endswith(s) for s in ("-USD", "-USDT", "-BTC", "-ETH")
        )
        entry_zscore = self.entry_zscore_crypto if _is_crypto else self.entry_zscore
        rsi_oversold = self.rsi_oversold_crypto if _is_crypto else self.rsi_oversold
        rsi_overbought = self.rsi_overbought_crypto if _is_crypto else self.rsi_overbought

        closes = bars["close"].values
        volumes = bars["volume"].values
        current_price = closes[-1]

        # Calculate indicators
        sma = np.mean(closes[-self.lookback:])
        std = np.std(closes[-self.lookback:])

        if std == 0:
            return None

        # Z-Score: how many std devs from mean
        zscore = (current_price - sma) / std

        # RSI
        rsi = self.indicators.rsi(closes, period=14)

        # Bollinger Bands
        bb_upper = sma + (self.bb_std * std)
        bb_lower = sma - (self.bb_std * std)

        # Volume check
        avg_vol = np.mean(volumes[-self.lookback:])
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0

        # Determine where price is relative to bands
        if current_price <= bb_lower:
            bb_zone = "LOWER"
        elif current_price >= bb_upper:
            bb_zone = "UPPER"
        else:
            bb_zone = "MIDDLE"

        # Build verdict
        checks = {
            "zscore_ok": zscore <= entry_zscore,
            "rsi_oversold": rsi < rsi_oversold,
            "rsi_overbought": rsi > rsi_overbought,
            "at_lower_bb": current_price <= bb_lower,
            "vol_surge": vol_ratio > 1.3,
        }
        passed = sum(1 for v in [checks["zscore_ok"], checks["rsi_oversold"], checks["at_lower_bb"]] if v)

        # Mirror the actual entry-path reversal_candle gate so the verdict
        # tells the truth. The old verdict said "BUY SIGNAL" the moment any 2
        # of {zscore, rsi, BB} passed — but the real signal also needs a
        # green/doji last bar (and for some paths a volume surge), so the
        # heartbeat regularly said BUY SIGNAL for 30+ minutes without firing.
        if _is_crypto and len(closes) >= 2:
            reversal_candle = closes[-2] >= bars["open"].values[-2]
        else:
            reversal_candle = closes[-1] > bars["open"].values[-1]
        # Volume floor: path 1 (z+rsi+reversal) was the only path without a
        # volume gate. Trade review found low-volume entries on this path
        # tended to chop back to entry. 1.1x is a soft floor — well below
        # paths 2/3 (1.3/1.5) but blocks the "z=-2 on thin volume" chop.
        # Applies equally to crypto and equity (single strategy, both venues).
        entry_ready = (
            (checks["zscore_ok"] and checks["rsi_oversold"] and reversal_candle and vol_ratio >= 1.1)
            or (checks["zscore_ok"] and checks["at_lower_bb"] and reversal_candle and vol_ratio > 1.3)
            or (checks["rsi_oversold"] and checks["at_lower_bb"] and vol_ratio > 1.5 and reversal_candle)
        )

        if entry_ready:
            verdict = "BUY SIGNAL"
        elif passed >= 2 and not reversal_candle:
            verdict = "WAIT: needs green bar"
        elif passed >= 2 and checks["at_lower_bb"] and vol_ratio <= 1.3:
            verdict = "WAIT: needs vol>1.3x"
        elif checks["zscore_ok"] and checks["rsi_oversold"] and reversal_candle and vol_ratio < 1.1:
            verdict = "WAIT: needs vol>=1.1x"
        elif passed >= 2:
            verdict = "WAIT: combo mismatch"
        elif checks["rsi_overbought"] and zscore >= abs(entry_zscore):
            verdict = "SELL SIGNAL"
        elif passed == 1:
            verdict = "WARMING UP"
        else:
            verdict = "NEUTRAL"

        # Store scan result for dashboard
        self.scan_results[symbol] = {
            "price": round(current_price, 2),
            "sma": round(sma, 2),
            "zscore": round(zscore, 2),
            "rsi": round(rsi, 1),
            "bb_upper": round(bb_upper, 2),
            "bb_lower": round(bb_lower, 2),
            "bb_zone": bb_zone,
            "vol_ratio": round(vol_ratio, 1),
            "checks": checks,
            "checks_passed": passed,
            "verdict": verdict,
        }

        # --- BUY Signal (Oversold) ---
        # Multi-confirmation: need z-score OR (RSI oversold + at lower BB).
        # The reversal_candle gate is identical to the one computed above for
        # the verdict — single source of truth, so heartbeat can't lie.
        zscore_trigger = checks["zscore_ok"]
        rsi_trigger = checks["rsi_oversold"]
        at_lower_bb = checks["at_lower_bb"]
        near_lower_bb = current_price <= bb_lower * 1.005  # Within 0.5% of lower BB
        buy_signal = entry_ready

        if buy_signal:
            confidence = min(1.0, abs(zscore) / 3.0 * 0.5 + (1 - rsi / 100) * 0.5)

            # Stronger signal if at lower Bollinger Band
            if at_lower_bb:
                confidence = min(1.0, confidence + 0.15)

            # Volume confirmation boosts confidence
            if vol_ratio > 1.3:
                confidence = min(1.0, confidence + 0.1)
            elif vol_ratio > 1.0:
                confidence = min(1.0, confidence + 0.05)

            # Reversal candle pattern: current bar closing above open (buying pressure)
            if len(bars) >= 1:
                current_open = bars["open"].values[-1]
                if current_price > current_open:
                    confidence = min(1.0, confidence + 0.05)

            # ATR-based stop loss (smarter than flat 3%)
            atr = self.indicators.atr(
                bars["high"].values, bars["low"].values, closes, period=14
            )
            if atr and atr > 0:
                stop_loss = current_price - (2.0 * atr)
                # Target: distance to mean, but at least 2x risk
                distance_to_mean = sma - current_price
                min_target = current_price + 2 * (current_price - stop_loss)
                take_profit = max(sma, min_target)
            else:
                stop_loss = current_price * 0.97  # Fallback 3%
                take_profit = sma

            signal = {
                "symbol": symbol,
                "action": "buy",
                "price": current_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "confidence": confidence,
                "reason": (
                    f"Mean reversion BUY: Z={zscore:.2f}, "
                    f"RSI={rsi:.0f}, BB={'lower' if at_lower_bb else 'near'}, "
                    f"Vol={vol_ratio:.1f}x"
                ),
                "max_hold_bars": self.max_hold,
                "bar_seconds": self._timeframe_to_seconds(),
                "max_hold_days": self.config.get("max_hold_days", 3),  # Mean reversion: max 3 days
            }

            log.info(f"SIGNAL: {signal['reason']} | {symbol} @ ${current_price:.2f}")
            self.signals_generated += 1
            return signal

        # --- SELL Signal (Overbought - for existing positions) ---
        # Crypto sell threshold is tighter than the entry threshold: requires
        # z >= 1.5 (not just z >= abs(entry_zscore)=1.0) AND respects a 5-min
        # minimum hold from entry. Trade review 2026-05-18: 9/18 mean_reversion
        # crypto closes were near-instant `webhook_exit` after entry — the
        # symbol z-score swung from oversold to slightly-overbought within
        # 2-3 minutes and the bot exited at near-break-even, paying the spread
        # on each ping-pong. min-hold + tighter threshold filter those.
        sell_zscore_threshold = 1.5 if _is_crypto else abs(entry_zscore)
        if zscore >= sell_zscore_threshold and rsi > rsi_overbought:
            # Only fire exits for symbols the bot actually holds. Without this
            # gate, scanner-discovered overbought stocks generate sells that the
            # risk manager rejects ("No position to exit") — burning the
            # signal slot and crowding the rejection log.
            if self._held_symbols is not None and symbol not in self._held_symbols:
                return None

            # Crypto min-hold time: block exit if entry was < 5 min ago. Stop-
            # loss + take-profit still work normally (they live in the engine);
            # this only blocks the strategy's own reverse-direction exit signal.
            if _is_crypto:
                from datetime import datetime as _dt
                entry_time = self._held_entry_times.get(symbol)
                if entry_time is not None:
                    try:
                        elapsed = (_dt.now(entry_time.tzinfo) - entry_time).total_seconds()
                        if elapsed < 300:
                            return None
                    except Exception:
                        pass

            signal = {
                "symbol": symbol,
                "action": "sell",
                "price": current_price,
                "confidence": min(1.0, zscore / 3.0),
                "reason": f"Mean reversion SELL: Z={zscore:.2f}, RSI={rsi:.0f}",
                "max_hold_bars": self.max_hold,
                "bar_seconds": self._timeframe_to_seconds(),
            }
            log.info(f"SIGNAL: {signal['reason']} | {symbol} @ ${current_price:.2f}")
            return signal

        return None

    def _timeframe_to_seconds(self):
        tf = self.timeframe
        if "m" in tf:
            return int(tf.replace("m", "")) * 60
        elif "h" in tf:
            return int(tf.replace("h", "")) * 3600
        return 300
