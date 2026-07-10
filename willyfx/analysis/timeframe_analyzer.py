import pandas as pd


class TimeframeAnalyzer:
    """Timeframe analyzer with structure, sweep, and liquidity context.

    The output is intentionally explicit so the Telegram message can explain
    why the 15-minute chart is bullish/bearish and what liquidity it is aiming at.
    """

    def __init__(self):
        pass

    def _to_frame(self, rates):
        if rates is None or len(rates) < 10:
            return None
        df = pd.DataFrame(rates).copy()
        if 'time' in df.columns:
            df['time'] = pd.to_datetime(df['time'], unit='s', errors='coerce')
        return df.dropna(subset=['close', 'high', 'low'])

    @staticmethod
    def _find_swings(df, window=2):
        swing_highs = []
        swing_lows = []
        highs = df['high'].tolist()
        lows = df['low'].tolist()
        for idx in range(window, len(df) - window):
            h = highs[idx]
            l = lows[idx]
            if h == max(highs[idx - window: idx + window + 1]):
                swing_highs.append((idx, float(h)))
            if l == min(lows[idx - window: idx + window + 1]):
                swing_lows.append((idx, float(l)))
        return swing_highs, swing_lows

    @staticmethod
    def _nearest_level(levels, price, above=True):
        candidates = [lvl for lvl in levels if (lvl > price if above else lvl < price)]
        if not candidates:
            return None
        return min(candidates) if above else max(candidates)

    def analyze(self, rates, timeframe=None, liquidity_model=None):
        """Return direction, reasons, confidence, structure and liquidity plan."""
        df = self._to_frame(rates)
        if df is None or df.empty:
            return {
                "direction": "neutral",
                "reasons": ["insufficient data"],
                "confidence": 0,
                "structure": "neutral",
                "structure_reason": "insufficient data",
            }

        close = df['close']
        current = float(close.iloc[-1])
        prev_close = float(close.iloc[-2]) if len(close) > 1 else current

        ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]

        delta = close.diff().fillna(0)
        up = delta.clip(lower=0).rolling(14).mean().iloc[-1]
        down = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
        rsi = float(100 * (up / (up + down)) if (up + down) else 50)

        swing_highs, swing_lows = self._find_swings(df)
        recent_swing_high = swing_highs[-1][1] if swing_highs else float(df['high'].rolling(5).max().iloc[-1])
        recent_swing_low = swing_lows[-1][1] if swing_lows else float(df['low'].rolling(5).min().iloc[-1])
        prior_swing_high = swing_highs[-2][1] if len(swing_highs) > 1 else recent_swing_high
        prior_swing_low = swing_lows[-2][1] if len(swing_lows) > 1 else recent_swing_low

        reasons = []
        score = 0

        if current > ema20 > ema50:
            score += 2
            reasons.append("price above EMA20 and EMA50")
        elif current < ema20 < ema50:
            score -= 2
            reasons.append("price below EMA20 and EMA50")

        if rsi > 65:
            score += 1
            reasons.append(f"RSI high ({rsi:.1f})")
        elif rsi < 35:
            score -= 1
            reasons.append(f"RSI low ({rsi:.1f})")

        swing_sequence = "neutral"
        if len(swing_highs) >= 2 and len(swing_lows) >= 2:
            hh = swing_highs[-1][1] > swing_highs[-2][1]
            hl = swing_lows[-1][1] > swing_lows[-2][1]
            lh = swing_highs[-1][1] < swing_highs[-2][1]
            ll = swing_lows[-1][1] < swing_lows[-2][1]
            if hh and hl:
                swing_sequence = "bullish"
                score += 2
                reasons.append("swing highs and lows are rising")
            elif lh and ll:
                swing_sequence = "bearish"
                score -= 2
                reasons.append("swing highs and lows are falling")

        bullish_bos = current > recent_swing_high and prev_close <= recent_swing_high
        bearish_bos = current < recent_swing_low and prev_close >= recent_swing_low
        bullish_sweep = df['low'].iloc[-1] < recent_swing_low and current > recent_swing_low
        bearish_sweep = df['high'].iloc[-1] > recent_swing_high and current < recent_swing_high

        structure_reason = "balanced"
        structure = swing_sequence
        if bullish_bos:
            structure = "bullish"
            score += 3
            structure_reason = f"bullish BOS above {recent_swing_high:.2f}"
            reasons.append(structure_reason)
        elif bearish_bos:
            structure = "bearish"
            score -= 3
            structure_reason = f"bearish BOS below {recent_swing_low:.2f}"
            reasons.append(structure_reason)
        elif bullish_sweep:
            structure = "bullish"
            score += 2
            structure_reason = f"swept below {recent_swing_low:.2f} and reclaimed it"
            reasons.append(structure_reason)
        elif bearish_sweep:
            structure = "bearish"
            score -= 2
            structure_reason = f"swept above {recent_swing_high:.2f} and rejected back below"
            reasons.append(structure_reason)

        if score >= 3:
            direction = 'bullish'
        elif score <= -3:
            direction = 'bearish'
        elif score > 0:
            direction = 'slight_bullish'
        elif score < 0:
            direction = 'slight_bearish'
        else:
            direction = 'neutral'

        confidence = min(100, max(0, 45 + abs(score) * 12))

        primary_liquidity = None
        reversal_liquidity = None
        execution_guidance = None
        if liquidity_model:
            levels = liquidity_model.get('levels') or []
            level_prices = [float(level.get('price')) for level in levels if level.get('price') is not None]
            if direction == 'bullish':
                target_price = self._nearest_level(level_prices, current, above=True)
                reversal_price = self._nearest_level(level_prices, current, above=False)
                execution_guidance = 'look for buys on the 1-minute timeframe'
            elif direction == 'bearish':
                target_price = self._nearest_level(level_prices, current, above=False)
                reversal_price = self._nearest_level(level_prices, current, above=True)
                execution_guidance = 'look for sells on the 1-minute timeframe'
            else:
                target_price = None
                reversal_price = None

            def _pick_label(price):
                if price is None:
                    return None
                match = next((level for level in levels if abs(float(level.get('price', 0)) - price) < 1e-9), None)
                if match:
                    return {"type": match.get('type'), "price": float(match.get('price'))}
                return {"type": "liquidity_level", "price": float(price)}

            primary_liquidity = _pick_label(target_price)
            reversal_liquidity = _pick_label(reversal_price)

            if primary_liquidity:
                reasons.append(
                    f"15M aims toward {primary_liquidity['type']} at {primary_liquidity['price']:.2f}"
                )
            if reversal_liquidity:
                reasons.append(
                    f"if taken out and reversed, next liquidity is {reversal_liquidity['type']} at {reversal_liquidity['price']:.2f}"
                )

        return {
            "timeframe": timeframe,
            "direction": direction,
            "reasons": reasons,
            "confidence": confidence,
            "rsi": rsi,
            "structure": structure,
            "structure_reason": structure_reason,
            "recent_swing_high": recent_swing_high,
            "recent_swing_low": recent_swing_low,
            "prior_swing_high": prior_swing_high,
            "prior_swing_low": prior_swing_low,
            "primary_liquidity": primary_liquidity,
            "reversal_liquidity": reversal_liquidity,
            "execution_guidance": execution_guidance,
        }
