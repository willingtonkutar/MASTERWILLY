import pandas as pd

class TargetCalculator:
    """Simple target calculator using ATR and swing points."""

    def __init__(self):
        pass

    def calculate_atr(self, rates, period=14):
        if rates is None or len(rates) < period:
            return None
        df = pd.DataFrame(rates)
        high = df['high']
        low = df['low']
        close = df['close']
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs()
        ], axis=1).max(axis=1)
        atr = tr.rolling(period).mean().iloc[-1]
        return float(atr)

    def swing_high_low(self, rates, lookback=50):
        df = pd.DataFrame(rates)
        highs = df['high'].tolist()
        lows = df['low'].tolist()
        if len(highs) < 3:
            return None, None
        swing_high = max(highs[-lookback:]) if len(highs) >= lookback else max(highs)
        swing_low = min(lows[-lookback:]) if len(lows) >= lookback else min(lows)
        return float(swing_high), float(swing_low)

    def calculate_targets(self, rates, direction):
        current = float(rates[-1]['close'])
        atr = self.calculate_atr(rates)
        swing_high, swing_low = self.swing_high_low(rates)
        targets = []
        if direction and 'bull' in direction:
            if swing_high:
                targets.append(swing_high)
            if atr:
                targets.append(current + atr * 1.5)
        elif direction and 'bear' in direction:
            if swing_low:
                targets.append(swing_low)
            if atr:
                targets.append(current - atr * 1.5)
        return targets
