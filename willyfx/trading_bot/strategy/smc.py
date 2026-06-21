# ============================================================
#  SMC - Smart Money Concepts Analysis
# ============================================================

from monitoring.logger import log_event


class SMCAnalyzer:
    """Smart Money Concepts trading analysis"""
    
    def __init__(self):
        self.support_levels = []
        self.resistance_levels = []
    
    def identify_liquidity_levels(self, rates):
        """
        Identify liquidity levels where smart money hunts
        
        Args:
            rates: OHLC data
            
        Returns:
            dict: Support and resistance levels
        """
        
        if rates is None or len(rates) < 2:
            return {"support": [], "resistance": []}
        
        # Find local highs and lows (handle numpy arrays)
        try:
            highs = [r['high'] for r in rates]
            lows = [r['low'] for r in rates]
        except (KeyError, TypeError):
            return {"support": [], "resistance": []}
        
        # Placeholder for SMC logic
        resistance = [max(highs)]
        support = [min(lows)]
        
        return {
            "support": support,
            "resistance": resistance
        }
    
    def check_smc_conditions(self, rates, direction):
        """
        Check if trade aligns with SMC liquidity principles
        
        Returns:
            bool: True if conditions are favorable
        """
        
        return True  # Placeholder

    def detect_structure(self, rates):
        """Lightweight structure read used for gating and signal context."""

        if rates is None or len(rates) < 5:
            return {
                "structure": "UNKNOWN",
                "bos": False,
                "choch": False,
                "bull_sweep": False,
                "bear_sweep": False,
                "in_bull_fvg": False,
                "in_bear_fvg": False
            }

        try:
            recent = rates[-20:] if len(rates) >= 20 else rates
            highs = [r['high'] for r in recent]
            lows = [r['low'] for r in recent]
            closes = [r['close'] for r in recent]
        except (KeyError, TypeError):
            return {
                "structure": "UNKNOWN",
                "bos": False,
                "choch": False,
                "bull_sweep": False,
                "bear_sweep": False,
                "in_bull_fvg": False,
                "in_bear_fvg": False
            }

        last_close = closes[-1]
        prev_close = closes[-2]
        recent_high = max(highs[:-1]) if len(highs) > 1 else highs[-1]
        recent_low = min(lows[:-1]) if len(lows) > 1 else lows[-1]

        structure = "RANGE"
        bos = False
        choch = False
        bull_sweep = False
        bear_sweep = False
        in_bull_fvg = False
        in_bear_fvg = False

        if last_close > recent_high:
            structure = "BULLISH"
            bos = True
            if prev_close <= recent_high:
                choch = True
        elif last_close < recent_low:
            structure = "BEARISH"
            bos = True
            if prev_close >= recent_low:
                choch = True
        else:
            if last_close > prev_close:
                structure = "BULLISH"
            elif last_close < prev_close:
                structure = "BEARISH"

        # Simple liquidity sweep approximation.
        if len(highs) >= 2 and last_close < highs[-2] and last_close > prev_close:
            bull_sweep = True
        if len(lows) >= 2 and last_close > lows[-2] and last_close < prev_close:
            bear_sweep = True

        midpoint = (recent_high + recent_low) / 2 if recent_high and recent_low else last_close
        if last_close >= midpoint:
            in_bull_fvg = True
        else:
            in_bear_fvg = True

        return {
            "structure": structure,
            "bos": bos,
            "choch": choch,
            "bull_sweep": bull_sweep,
            "bear_sweep": bear_sweep,
            "in_bull_fvg": in_bull_fvg,
            "in_bear_fvg": in_bear_fvg,
            "recent_high": recent_high,
            "recent_low": recent_low,
            "last_close": last_close
        }
