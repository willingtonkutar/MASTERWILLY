# ============================================================
#  SMART EXIT ENGINE - Liquidity-Based Exit Logic
# ============================================================

from datetime import datetime

from monitoring.logger import log_event
import config


class SmartExitEngine:
    """
    Sophisticated exit logic instead of simple rules.
    
    Exit Types:
    1. PROFIT PROTECTION - partial exit at target
    2. LIQUIDITY EXIT - opposite liquidity taken
    3. STRUCTURE EXIT - CHoCH on entry timeframe
    4. EXHAUSTION EXIT - ATR compression after move
    5. MANUAL EXIT - user requested
    """
    
    EXIT_TYPE_PROFIT = "PROFIT_TARGET"
    EXIT_TYPE_PARTIAL_TP = "PARTIAL_TP"
    EXIT_TYPE_TREND_EXTENSION = "TREND_EXTENSION"
    EXIT_TYPE_EARLY_EXIT = "EARLY_EXIT"
    EXIT_TYPE_TIME = "TIME_EXIT"
    EXIT_TYPE_LOSS_CUT = "LOSS_CUT"
    EXIT_TYPE_LIQUIDITY = "LIQUIDITY_EXIT"
    EXIT_TYPE_STRUCTURE = "STRUCTURE_BREAK"
    EXIT_TYPE_EXHAUSTION = "EXHAUSTION"
    EXIT_TYPE_MANUAL = "MANUAL"
    EXIT_TYPE_STOP_LOSS = "STOP_LOSS"
    
    def __init__(self):
        self.partial_exits_taken = {}
        self._min_hold_block_logged = set()
    
    def check_exit_conditions(self, position, df, smc_data=None):
        """
        Check all exit conditions for a position
        
        Args:
            position: Open position data
            df: OHLC dataframe with indicators
            smc_data: SMC structure data
            
        Returns:
            (should_exit, exit_type, exit_reason, target_price)
        """
        
        if df is None or len(df) < 5:
            return False, None, "No data", None
        
        ticket = position.get("ticket")
        direction = position.get("direction")
        entry_price = position.get("entry_price")
        profit_percent = position.get("profit_percent", 0)
        current_price = float(df.iloc[-1]["close"])
        risk = abs(float(entry_price or 0.0) - float(position.get("sl", entry_price) or entry_price))
        current_profit = (current_price - float(entry_price or 0.0)) if direction == "BUY" else (float(entry_price or 0.0) - current_price)
        current_profit_r = (current_profit / risk) if risk > 0 else 0.0
        hold_minutes = self._hold_time_minutes(position)
        hold_candles = hold_minutes / 15.0 if hold_minutes < 9999 else 9999.0

        if current_profit_r <= -0.7:
            return True, self.EXIT_TYPE_LOSS_CUT, "momentum against trade", current_price

        if hold_candles > 10 and current_profit_r < 0.3:
            return True, self.EXIT_TYPE_TIME, "no movement", current_price

        if hold_minutes < float(getattr(config, "MIN_HOLD_MINUTES", 4) or 4):
            if ticket not in self._min_hold_block_logged:
                log_event("INFO", {
                    "message": "EXIT_BLOCKED: minimum hold time not reached",
                    "ticket": ticket,
                    "direction": direction,
                })
                self._min_hold_block_logged.add(ticket)
            return False, None, None, None
        
        # ========== TYPE 1: PARTIAL TAKE PROFIT ==========
        exit_type, exit_reason, target = self._check_partial_profit_target(
            current_profit_r, direction, df, position
        )
        if exit_type:
            return True, exit_type, exit_reason, target
        
        # ========== TYPE 2: LIQUIDITY EXIT ==========
        if smc_data:
            exit_type, exit_reason, target = self._check_liquidity_exit(
                smc_data, direction, df
            )
            if exit_type:
                return True, exit_type, exit_reason, target
        
        # ========== TYPE 3: SMART EXIT (ANTI-WICK) ==========
        exit_type, exit_reason, target = self._check_smart_exit(
            direction, df, smc_data
        )
        if exit_type:
            return True, exit_type, exit_reason, target

        # ========== TYPE 4: EXHAUSTION ==========
        exit_type, exit_reason, target = self._check_exhaustion_exit(
            direction, entry_price, df
        )
        if exit_type:
            return True, exit_type, exit_reason, target
        
        return False, None, None, None
    
    def _check_partial_profit_target(self, current_profit_r, direction, df, position):
        """
        Partial take profit at 1R.
        
        Strategy:
        - 1R profit: close 50%
        - keep remainder running
        """
        if current_profit_r >= 1.0:
            return (
                self.EXIT_TYPE_PARTIAL_TP,
                "1R target hit: closing 50%",
                float(df.iloc[-1]["close"]),
            )

        return None, None, None

    def _check_smart_exit(self, direction, df, smc_data=None):
        """Exit only when momentum flips and structure breaks against the trade."""

        if df is None or len(df) < 3:
            return None, None, None

        last = df.iloc[-1]
        direction = str(direction or "").upper()
        bearish_momentum = self.is_momentum_flip("BUY", df)
        bullish_momentum = self.is_momentum_flip("SELL", df)

        structure = str((smc_data or {}).get("structure", "")).upper()
        choch = bool((smc_data or {}).get("choch"))
        bos = bool((smc_data or {}).get("bos"))
        bearish_structure_break = structure == "BEARISH" and (choch or bos)
        bullish_structure_break = structure == "BULLISH" and (choch or bos)

        if direction == "BUY" and bearish_momentum and bearish_structure_break:
            return (
                self.EXIT_TYPE_STRUCTURE,
                "smart exit: bearish momentum + bearish structure break",
                float(last["close"]),
            )

        if direction == "SELL" and bullish_momentum and bullish_structure_break:
            return (
                self.EXIT_TYPE_STRUCTURE,
                "smart exit: bullish momentum + bullish structure break",
                float(last["close"]),
            )

        return None, None, None

    def is_momentum_flip(self, direction, df):
        """Return True when short-term momentum flips against the position direction."""
        if df is None or len(df) < 2:
            return False

        last = df.iloc[-1]
        direction = str(direction or "").upper()
        rsi = float(last.get("rsi", 50.0) or 50.0)
        ema9 = float(last.get("ema9", 0.0) or 0.0)
        ema50 = float(last.get("ema50", 0.0) or 0.0)
        macd_hist = float(last.get("macd_hist", 0.0) or 0.0)

        if direction == "BUY":
            return rsi < 48 and ema9 < ema50 and macd_hist <= 0
        if direction == "SELL":
            return rsi > 52 and ema9 > ema50 and macd_hist >= 0
        return False

    def get_momentum_flip_protective_sl(self, position, risk):
        """Lock a small buffer at momentum flip instead of immediately exiting."""
        if risk <= 0:
            return None

        direction = str(position.get("direction") or "").upper()
        entry_price = float(position.get("entry_price", 0.0) or 0.0)
        lock_r = float(getattr(config, "MOMENTUM_FLIP_PROTECT_LOCK_R", 0.1) or 0.1)

        if direction == "BUY":
            return max(entry_price, entry_price + (lock_r * risk))
        if direction == "SELL":
            return min(entry_price, entry_price - (lock_r * risk))
        return None
    
    def _check_liquidity_exit(self, smc_data, direction, df):
        """
        Exit when opposite liquidity is taken
        
        Example:
        - Long entry → opposite (bear) sweep detected → EXIT
        """
        
        if direction == "BUY" and smc_data.get("bear_sweep"):
            return (
                self.EXIT_TYPE_LIQUIDITY,
                "Bear liquidity sweep: closing long",
                df.iloc[-1]["low"]
            )
        
        if direction == "SELL" and smc_data.get("bull_sweep"):
            return (
                self.EXIT_TYPE_LIQUIDITY,
                "Bull liquidity sweep: closing short",
                df.iloc[-1]["high"]
            )
        
        return None, None, None
    
    def _check_structure_exit(self, smc_data, direction, df):
        """
        Exit when structure breaks on entry timeframe
        
        Example:
        - Long from structure → CHoCH to down → EXIT
        """
        
        if smc_data.get("choch"):
            if direction == "BUY" and smc_data.get("structure") == "BEARISH":
                return (
                    self.EXIT_TYPE_STRUCTURE,
                    "Structure broke to bearish: closing long",
                    df.iloc[-1]["low"]
                )
            
            if direction == "SELL" and smc_data.get("structure") == "BULLISH":
                return (
                    self.EXIT_TYPE_STRUCTURE,
                    "Structure broke to bullish: closing short",
                    df.iloc[-1]["high"]
                )
        
        return None, None, None
    
    def _check_exhaustion_exit(self, direction, entry_price, df):
        """
        Exit when momentum exhausts after move
        
        Signs of exhaustion:
        - ATR compression after expansion
        - Volume drop after spike
        - Candle reversal patterns
        """
        
        if len(df) < 10:
            return None, None, None
        
        last = df.iloc[-1]
        atr = df["atr"].iloc[-1]
        atr_avg = df["atr"].rolling(20).mean().iloc[-1]
        
        # Check if ATR is compressing after expansion
        if atr < atr_avg * 0.7:
            # ATR has compressed significantly
            
            # Check for reversal candle
            if direction == "BUY":
                # Bearish reversal pattern
                if last["close"] < df.iloc[-2]["open"]:
                    return (
                        self.EXIT_TYPE_EXHAUSTION,
                        "Momentum exhaustion + reversal candle: closing long",
                        last["close"]
                    )
            else:
                # Bullish reversal pattern
                if last["close"] > df.iloc[-2]["open"]:
                    return (
                        self.EXIT_TYPE_EXHAUSTION,
                        "Momentum exhaustion + reversal candle: closing short",
                        last["close"]
                    )
        
        return None, None, None
    
    def calculate_partial_exit_size(self, total_volume, exit_type):
        """Calculate how much to close on partial exits"""
        
        if exit_type == self.EXIT_TYPE_PROFIT:
            return total_volume * 0.5  # Close 50% at first profit target
        
        elif exit_type == self.EXIT_TYPE_EXHAUSTION:
            return total_volume * 0.3  # Close 30%
        
        else:
            return total_volume  # Full exit
    
    # ================================================================
    # DYNAMIC SL/TP MANAGEMENT
    # ================================================================
    
    def apply_breakeven(self, position, entry_price, current_price, risk):
        """
        Gradual protection:
        - 0.5R: move SL to -0.1R
        - 0.8R: move SL to breakeven
        
        Args:
            position: Trade position dict
            entry_price: Entry price
            current_price: Current market price
            risk: Risk distance (initial SL distance from entry)
            
        Returns:
            new_sl or None
        """
        if self._hold_time_minutes(position) < float(getattr(config, "MIN_HOLD_MINUTES", 4) or 4):
            return None

        direction = position.get("direction")
        current_sl = float(position.get("sl", 0.0) or 0.0)

        if current_sl > 0:
            if direction == "BUY" and current_sl >= entry_price:
                return None
            if direction == "SELL" and current_sl <= entry_price:
                return None

        if risk <= 0:
            return None

        profit = current_price - entry_price if direction == "BUY" else entry_price - current_price
        profit_r = profit / risk

        # Let lock_profit control the 1.2R stage.
        if profit_r >= 1.2:
            return None

        if direction == "BUY":
            if profit_r >= 0.8:
                return entry_price
            if profit_r >= 0.5:
                return entry_price - (0.1 * risk)

        elif direction == "SELL":
            if profit_r >= 0.8:
                return entry_price
            if profit_r >= 0.5:
                return entry_price + (0.1 * risk)
        
        return None
    
    def lock_profit(self, position, entry_price, current_price, risk):
        """
        Lock +0.3R once trade reaches 1.2R.
        
        Args:
            position: Trade position dict
            entry_price: Entry price
            current_price: Current market price
            risk: Risk distance (initial SL distance)
            
        Returns:
            new_sl or None
        """
        if self._hold_time_minutes(position) < float(getattr(config, "MIN_HOLD_MINUTES", 4) or 4):
            return None

        direction = position.get("direction")
        if risk <= 0:
            return None

        if direction == "BUY":
            profit = current_price - entry_price
            if profit >= 1.2 * risk:
                return entry_price + (0.3 * risk)

        elif direction == "SELL":
            profit = entry_price - current_price
            if profit >= 1.2 * risk:
                return entry_price - (0.3 * risk)
        
        return None
    
    def apply_trailing_stop(self, position, df, smc_data=None):
        """
        Apply structure-based trailing stop.

        For BUY: trail below higher lows.
        For SELL: trail above lower highs.
        
        Args:
            position: Trade position dict
            df: DataFrame with OHLC data
            
        Returns:
            new_sl or None
        """
        if df is None or len(df) < 6:
            return None

        if self._hold_time_minutes(position) < float(getattr(config, "MIN_HOLD_MINUTES", 4) or 4):
            return None
        
        direction = position.get("direction")
        if direction == "BUY":
            if not self._trail_activation_reached(position, df):
                return None
            if smc_data and smc_data.get("recent_low"):
                new_sl = float(smc_data.get("recent_low"))
            else:
                new_sl = self._latest_higher_low(df)
                if new_sl is None:
                    return None
            log_event("INFO", {"message": "TRAILING_ACTIVATED", "direction": direction})
            return new_sl
        elif direction == "SELL":
            if not self._trail_activation_reached(position, df):
                return None
            if smc_data and smc_data.get("recent_high"):
                new_sl = float(smc_data.get("recent_high"))
            else:
                new_sl = self._latest_lower_high(df)
                if new_sl is None:
                    return None
            log_event("INFO", {"message": "TRAILING_ACTIVATED", "direction": direction})
            return new_sl
        
        return None
    
    def should_move_sl(self, current_sl, new_sl, direction):
        """
        Check if we should apply the new SL
        Never move SL backwards (closer to entry)
        
        Args:
            current_sl: Current stop loss
            new_sl: Proposed new stop loss
            direction: BUY or SELL
            
        Returns:
            bool: True if should move, False otherwise
        """
        if current_sl is None or new_sl is None:
            return False
        
        if direction == "BUY":
            # For BUY, only move SL UP (higher = better protection)
            return new_sl > current_sl
        elif direction == "SELL":
            # For SELL, only move SL DOWN (lower = better protection)
            return new_sl < current_sl

    def should_move_tp(self, current_tp, new_tp, direction):
        """Only extend TP in the favorable direction."""
        if current_tp is None or new_tp is None:
            return False

        if direction == "BUY":
            return new_tp > current_tp
        if direction == "SELL":
            return new_tp < current_tp
        return False

    def extend_trend_tp(self, position, df):
        """Extend TP during strong trend continuation instead of closing early."""

        if df is None or len(df) < 2:
            return None

        if position.get("tp_extended"):
            return None

        last = df.iloc[-1]
        ema9 = float(last.get("ema9", 0.0) or 0.0)
        ema50 = float(last.get("ema50", 0.0) or 0.0)
        rsi = float(last.get("rsi", 50.0) or 50.0)
        direction = str(position.get("direction") or "").upper()
        current_tp = float(position.get("tp", 0.0) or 0.0)
        entry_price = float(position.get("entry_price", 0.0) or 0.0)
        initial_sl = float(position.get("initial_sl", position.get("sl", entry_price)) or entry_price)
        risk = abs(entry_price - initial_sl)
        if risk <= 0:
            return None

        if direction == "BUY" and ema9 > ema50 and 55 <= rsi <= 70:
            extension = abs(current_tp - entry_price) * 1.5
            proposed_tp = current_tp + extension
            if str(getattr(config, "STRATEGY_TYPE", "")).lower() == "scalp":
                max_tp = entry_price + (risk * 1.8)
                return min(proposed_tp, max_tp)
            return proposed_tp

        if direction == "SELL" and ema9 < ema50 and 30 <= rsi <= 45:
            extension = abs(current_tp - entry_price) * 1.5
            proposed_tp = current_tp - extension
            if str(getattr(config, "STRATEGY_TYPE", "")).lower() == "scalp":
                min_tp = entry_price - (risk * 1.8)
                return max(proposed_tp, min_tp)
            return proposed_tp

        return None

    def _latest_higher_low(self, df, lookback=8):
        lows = [float(v) for v in df["low"].iloc[-lookback:].tolist()]
        if len(lows) < 4:
            return None

        pivots = []
        for i in range(1, len(lows) - 1):
            if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
                pivots.append(lows[i])

        if len(pivots) >= 2 and pivots[-1] > pivots[-2]:
            return float(pivots[-1])
        if pivots:
            return float(pivots[-1])
        return None

    def _latest_lower_high(self, df, lookback=8):
        highs = [float(v) for v in df["high"].iloc[-lookback:].tolist()]
        if len(highs) < 4:
            return None

        pivots = []
        for i in range(1, len(highs) - 1):
            if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
                pivots.append(highs[i])

        if len(pivots) >= 2 and pivots[-1] < pivots[-2]:
            return float(pivots[-1])
        if pivots:
            return float(pivots[-1])
        return None

    def _hold_time_minutes(self, position):
        open_time = position.get("open_time")
        if not open_time and isinstance(position.get("entry_conditions"), dict):
            open_time = position.get("entry_conditions", {}).get("entry_timestamp")

        if not open_time:
            return 9999.0

        try:
            opened_at = datetime.fromisoformat(str(open_time))
        except ValueError:
            return 9999.0

        return (datetime.now() - opened_at).total_seconds() / 60.0

    def _trail_activation_reached(self, position, df):
        entry_price = float(position.get("entry_price", 0.0) or 0.0)
        current_price = float(df.iloc[-1]["close"])
        risk = abs(entry_price - float(position.get("sl", entry_price) or entry_price))
        if risk <= 0:
            return False

        direction = str(position.get("direction") or "").upper()
        profit = current_price - entry_price if direction == "BUY" else entry_price - current_price
        return profit >= float(getattr(config, "TRAILING_START_R", 1.0) or 1.0) * risk
        
        return False
