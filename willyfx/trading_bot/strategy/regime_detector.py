# ============================================================
#  REGIME DETECTOR - Market State Classification
# ============================================================

from monitoring.logger import log_event


class RegimeDetector:
    """
    Classifies current market regime.
    Helps bot avoid trading in wrong market conditions.
    """
    
    REGIME_TREND = "TREND"
    REGIME_RANGE = "RANGE"
    REGIME_TRANSITION = "TRANSITION"
    REGIME_HIGH_VOL = "HIGH_VOL"
    REGIME_DEAD = "DEAD"
    
    def __init__(self):
        self.current_regime = None
        self.last_regime_log = 0
        self.regime_confirmation_count = 0
        self.pending_regime = None
        self.last_processed_candle = None
        self.REGIME_CONFIRMATION_REQUIRED = 2  # Require 2 consecutive candles before switching
    
    def detect_regime(self, df, candle_time=None):
        """
        Classify market regime from price action
        
        Returns:
            dict: regime classification + score
        """
        
        if df is None or len(df) < 50:
            return {"regime": self.REGIME_RANGE, "score": 0.5}
        
        last = df.iloc[-1]
        
        # Get price levels
        ema_fast = df["ema9"].iloc[-1] if "ema9" in df.columns else None
        ema_slow = df["ema50"].iloc[-1] if "ema50" in df.columns else None
        
        # Get volatility
        atr = df["atr"].iloc[-1] if "atr" in df.columns else None
        atr_avg = df["atr"].rolling(20).mean().iloc[-1] if "atr" in df.columns else None
        
        vol_ratio = 1.0
        
        # Check volatility first
        if atr and atr_avg and atr_avg > 0:
            vol_ratio = atr / atr_avg
            
            if vol_ratio > 1.5:
                regime = self.REGIME_HIGH_VOL
                score = vol_ratio
            elif vol_ratio < 0.5:
                regime = self.REGIME_DEAD
                score = vol_ratio
            else:
                # Moderate volatility - check for trend
                if ema_fast and ema_slow:
                    trend_strength = abs(ema_fast - ema_slow)
                    
                    # Softer threshold for trend detection
                    if trend_strength > atr * 0.6:
                        regime = self.REGIME_TREND
                        score = min(1.5, trend_strength / atr) if atr > 0 else 0.8
                    elif trend_strength > atr * 0.2:
                        # Weak trend or choppy - classify as RANGE not TRANSITION
                        regime = self.REGIME_RANGE
                        score = 0.8
                    else:
                        # Very weak trend signal - default to RANGE for scalping
                        regime = self.REGIME_RANGE
                        score = 0.6
                else:
                    regime = self.REGIME_RANGE
                    score = 0.7
        else:
            regime = self.REGIME_RANGE
            score = 0.7
        
        # Only update persistence state once per closed candle.
        should_update = candle_time is None or candle_time != self.last_processed_candle

        if should_update:
            self.last_processed_candle = candle_time if candle_time is not None else self.last_processed_candle

            if regime == self.current_regime:
                self.regime_confirmation_count = 0
                self.pending_regime = None
            elif regime == self.pending_regime:
                self.regime_confirmation_count += 1
                if self.regime_confirmation_count >= self.REGIME_CONFIRMATION_REQUIRED:
                    self.current_regime = regime
                    self.regime_confirmation_count = 0
                    self.pending_regime = None
            else:
                self.pending_regime = regime
                self.regime_confirmation_count = 1
        
        # Use current regime (which requires confirmation to change)
        return {
            "regime": self.current_regime if self.current_regime else regime,
            "score": score,
            "volatility_ratio": vol_ratio,
            "is_confirmed": self.regime_confirmation_count == 0 or self.regime_confirmation_count >= self.REGIME_CONFIRMATION_REQUIRED
        }
    
    def should_trade_in_regime(self, regime, strategy_type="scalp", allow_transition=False):
        """
        Should we trade this regime?
        
        Args:
            regime (str): Current regime
            strategy_type (str): scalp, swing, trend
            allow_transition (bool): Allow trading in TRANSITION regime for scalping
            
        Returns:
            (bool, str): (should_trade, reason)
        """
        
        if strategy_type == "scalp":
            # Scalpers prefer HIGH_VOL and TREND
            if regime in [self.REGIME_HIGH_VOL, self.REGIME_TREND]:
                return True, f"Good scalp conditions: {regime}"
            elif regime == self.REGIME_RANGE:
                return True, f"Acceptable scalp: {regime}"
            elif regime == self.REGIME_TRANSITION:
                if allow_transition:
                    return True, f"Scalp allowed in: {regime}"
                else:
                    return False, f"Uncertain conditions: {regime}"
            elif regime == self.REGIME_DEAD:
                return False, "Market too dead for scalping"
            else:
                return False, f"Unknown regime: {regime}"
        
        elif strategy_type == "swing":
            # Swing traders prefer TREND
            if regime == self.REGIME_TREND:
                return True, f"Good swing conditions: {regime}"
            elif regime == self.REGIME_HIGH_VOL:
                return True, f"Can swing trade: {regime}"
            else:
                return False, f"Not ideal for swing: {regime}"
        
        elif strategy_type == "trend":
            # Trend traders like TREND + HIGH_VOL
            if regime == self.REGIME_TREND:
                return True, f"Perfect trend conditions"
            elif regime == self.REGIME_HIGH_VOL:
                return True, f"High volatility trend"
            else:
                return False, f"Not trending: {regime}"
        
        return False, "Unknown strategy type"
