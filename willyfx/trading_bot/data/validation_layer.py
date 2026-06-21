# ============================================================
#  DATA VALIDATION LAYER - Safety Before Strategy
# ============================================================

from monitoring.logger import log_event
import config
import time


class DataValidator:
    """
    Validates market data before feeding to strategy.
    Catches:
    - Missing candles
    - Data spikes/anomalies
    - Broker feed freeze
    - Bad OHLC structure
    """
    
    def __init__(self):
        self.last_time = None
        self.last_close = None
        self.last_gap_warning_time = 0.0
    
    def validate_rates(self, rates):
        """
        Validate OHLC data integrity
        
        Returns:
            (bool, str): (is_valid, error_message)
        """
        
        if rates is None or len(rates) < 2:
            return False, "Insufficient candles"
        
        last = rates[-1]
        
        # Check OHLC structure (handle numpy arrays)
        try:
            open_val = last['open']
            high_val = last['high']
            low_val = last['low']
            close_val = last['close']
        except (KeyError, TypeError):
            return False, "Missing OHLC data"
        
        if not (open_val and high_val and low_val and close_val):
            return False, "Missing OHLC data"
        
        # High must be highest, Low must be lowest
        if not (high_val >= open_val and high_val >= close_val and 
                low_val <= open_val and low_val <= close_val):
            return False, "Invalid OHLC structure"
        
        # Check for extreme spikes (volatility anomalies)
        price_range = high_val - low_val
        avg_range = sum([c['high'] - c['low'] for c in rates[-20:]]) / 20
        
        if price_range > avg_range * 5:
            log_event("WARNING", {
                "message": "Extreme volatility spike detected",
                "range": price_range,
                "avg": avg_range
            })
            return False, "Extreme volatility spike"
        
        # Check for data continuity only when a NEW candle appears.
        # Without this guard, same-candle polling can repeatedly trigger warnings.
        try:
            current_time = last['time']
        except (KeyError, TypeError, IndexError):
            current_time = None
        if current_time != self.last_time:
            prev_close = rates[-2]['close']
            if prev_close:
                gap = abs(open_val - prev_close) / prev_close
                if gap > config.DATA_GAP_WARNING_THRESHOLD:
                    now = time.time()
                    if (now - self.last_gap_warning_time) >= config.DATA_GAP_WARNING_COOLDOWN_SECS:
                        log_event("WARNING", {
                            "message": "Price gap detected",
                            "gap_percent": gap * 100
                        })
                        self.last_gap_warning_time = now

            self.last_time = current_time

        self.last_close = close_val
        return True, None
    
    def validate_indicators(self, df):
        """
        Validate calculated indicators
        
        Returns:
            (bool, str): (is_valid, error_message)
        """
        
        if df is None or len(df) == 0:
            return False, "No dataframe"
        
        # Check for NaN in critical columns
        critical = ['close', 'atr', 'rsi', 'ema9', 'ema50']
        
        for col in critical:
            if col in df.columns:
                if df[col].iloc[-1] != df[col].iloc[-1]:  # NaN check
                    return False, f"NaN in {col}"
                if df[col].iloc[-1] == 0:
                    return False, f"Zero value in {col}"
        
        return True, None
    
    def validate_spread(self, spread, max_spread=None):
        """Validate spread is acceptable"""
        max_spread = max_spread or config.MAX_SPREAD
        
        if spread > max_spread:
            return False, f"Spread too high: {spread} > {max_spread}"
        
        return True, None
    
    def validate_signal(self, signal):
        """
        Validate signal structure
        
        Returns:
            (bool, str): (is_valid, error_message)
        """
        
        if not signal:
            return False, "No signal"
        
        required_fields = ['direction', 'score', 'confidence']
        
        for field in required_fields:
            if field not in signal:
                return False, f"Missing {field}"
        
        if signal['direction'] not in ['BUY', 'SELL', None]:
            return False, "Invalid direction"
        
        if not isinstance(signal['score'], (int, float)):
            return False, "Invalid score"
        
        return True, None
