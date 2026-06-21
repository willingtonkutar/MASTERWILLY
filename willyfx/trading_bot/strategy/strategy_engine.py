# ============================================================
#  INSTITUTIONAL STRATEGY ENGINE v2 - Multi-Layer Confluence
# ============================================================

import time
import config
import pandas as pd
from data.feed import get_market_data
from monitoring.logger import log_event


from strategy.structure_analyzer import StructureAnalyzer
from strategy.confluence import ConfluenceConfig, analyze_confluence, find_current_poi

class InstitutionalStrategyEngine:
    """
    Production-grade strategy with 4-layer confluence model:
    
    Layer 1: STRUCTURE ENGINE (trend + regime)
    Layer 2: LIQUIDITY MODEL (SMC core)
    Layer 3: ORDER FLOW MOMENTUM (real confirmation)
    Layer 4: ENTRY TRIGGER (M5 only)
    """
    
    def __init__(self):
        self.last_signal = None
        self.last_signal_signature = None
        self.last_signal_time = 0
        self.last_candle_time = None
        self.signal_cooldown = config.SIGNAL_COOLDOWN_SECS

    def _build_signal_signature(self, signal):
        """Create stable signature for duplicate suppression across noisy recalculations."""
        if not signal:
            return None

        breakdown = signal.get("breakdown", {}) or {}
        return (
            signal.get("direction"),
            round(float(signal.get("score", 0) or 0), 2),
            str(signal.get("confidence", "")).upper(),
            str(signal.get("bias", "")).upper(),
            round(float(breakdown.get("trend_alignment", 0) or 0), 2),
            round(float(breakdown.get("structure", 0) or 0), 2),
            round(float(breakdown.get("liquidity", 0) or 0), 2),
            round(float(breakdown.get("momentum", 0) or 0), 2),
            round(float(breakdown.get("volatility", 0) or 0), 2),
            round(float(breakdown.get("confirmation", 0) or 0), 2),
        )

    def _cap_negative_component(self, value, floor=-2.0):
        """Prevent any single negative component from dragging score too far down."""
        value = float(value or 0.0)
        return max(value, float(floor)) if value < 0 else value

    def _count_recent_sweeps(self, recent_sweeps, direction):
        """Count same-direction sweeps in the most recent candle window."""
        if not recent_sweeps:
            return 0

        direction_key = str(direction or "").upper()
        recent_window = list(recent_sweeps)[-3:]
        return sum(1 for sweep in recent_window if str(sweep.get("direction", "")).upper() == direction_key)

    def _rates_to_df(self, rates_or_df):
        """Normalize MT5 rates into the OHLCV columns used by the SMC modules."""
        if rates_or_df is None:
            return pd.DataFrame()

        df = rates_or_df.copy() if isinstance(rates_or_df, pd.DataFrame) else pd.DataFrame(rates_or_df)
        if df.empty:
            return df

        if "volume" not in df.columns:
            if "tick_volume" in df.columns:
                df["volume"] = df["tick_volume"]
            elif "real_volume" in df.columns:
                df["volume"] = df["real_volume"]
            else:
                df["volume"] = 0

        return df

    def _fetch_timeframe_df(self, timeframe, fallback_df=None, candles=None):
        """Fetch one timeframe, falling back to the current dataframe when appropriate."""
        if timeframe == getattr(config, "TIMEFRAME", "M15") and fallback_df is not None:
            return self._rates_to_df(fallback_df)

        try:
            rates = get_market_data(
                symbol=config.SYMBOL,
                timeframe=timeframe,
                candles=candles or int(getattr(config, "SMC_ANALYSIS_CANDLES", 300) or 300),
            )
            return self._rates_to_df(rates)
        except Exception as exc:
            log_event("SMC_TIMEFRAME_FETCH_FAILED", {"timeframe": timeframe, "error": str(exc)})
            return pd.DataFrame()

    def _build_mtf_context(self, primary_df):
        higher_tfs = ["D1", "H4", "H1", "M15"]
        zone_tfs = ["H1", "M15"]
        entry_tfs = ["M5", "M3", "M1"]

        higher = {tf: self._fetch_timeframe_df(tf, primary_df) for tf in higher_tfs}
        zones = {tf: higher[tf] if tf in higher else self._fetch_timeframe_df(tf, primary_df) for tf in zone_tfs}
        entries = {tf: self._fetch_timeframe_df(tf, primary_df, candles=180) for tf in entry_tfs}
        return higher, zones, entries

    def _build_confluence_config(self):
        return ConfluenceConfig(
            minimum_score=int(getattr(config, "SMC_MIN_CONFLUENCE_SCORE", 8)),
            order_block_range=int(getattr(config, "SMC_ORDER_BLOCK_RANGE", 25)),
            liquidity_length=int(getattr(config, "SMC_LIQUIDITY_LENGTH", 14)),
            fvg_threshold_percent=float(getattr(config, "SMC_FVG_THRESHOLD_PERCENT", 0)),
            volume_lookback=int(getattr(config, "SMC_VOLUME_LOOKBACK", 20)),
            volume_multiplier=float(getattr(config, "SMC_VOLUME_SPIKE_MULTIPLIER", 1.5)),
        )

    def _decision_to_signal(self, decision, higher, zones, entries):
        signed_score = float(decision.score)
        if decision.action == "SELL":
            signed_score = -signed_score

        abs_score = abs(signed_score)
        if abs_score >= config.INSTITUTIONAL_HIGH_CONFIDENCE:
            confidence = "HIGH"
        elif abs_score >= config.INSTITUTIONAL_MEDIUM_CONFIDENCE:
            confidence = "MEDIUM"
        elif decision.action == "NO_TRADE":
            confidence = "NONE"
        else:
            confidence = "LOW"

        direction = None if decision.action == "NO_TRADE" else decision.action
        bias = str(decision.details.get("trend_bias", "neutral")).upper()
        poi = find_current_poi(zones, direction=decision.direction, config=self._build_confluence_config())

        breakdown = {
            "trend_alignment": 2 if decision.details.get("trend_bias") == decision.direction else 0,
            "structure": 4 if decision.order_block and decision.fair_value_gap else 0,
            "liquidity": 3 if decision.liquidity_zone else 0,
            "momentum": 0,
            "volatility": 0,
            "confirmation": 3 if any("confirmed" in reason or "formed" in reason for reason in decision.reasons) else 0,
        }
        if direction == "SELL":
            breakdown = {key: -value for key, value in breakdown.items()}

        return {
            "direction": direction,
            "score": signed_score,
            "confidence": confidence,
            "bias": "BULLISH" if bias == "BULLISH" else "BEARISH" if bias == "BEARISH" else None,
            "breakdown": breakdown,
            "entry_threshold": float(getattr(config, "SMC_MIN_CONFLUENCE_SCORE", 8)),
            "base_score": signed_score,
            "sweep_detected": bool(decision.liquidity_zone),
            "sweep_direction": direction if decision.liquidity_zone else None,
            "recent_sweep_count": 1 if decision.liquidity_zone else 0,
            "reasons": decision.reasons,
            "smc_decision": {
                "action": decision.action,
                "direction": decision.direction,
                "score": decision.score,
                "details": decision.details,
            },
            "point_of_interest": poi,
            "structure_analysis": {
                "D1": {"trend": self._safe_trend_label(higher.get("D1"))},
                "H4": {"trend": self._safe_trend_label(higher.get("H4"))},
                "H1": {"trend": self._safe_trend_label(higher.get("H1"))},
                "M15": {"trend": self._safe_trend_label(higher.get("M15"))},
                "M5": {"trend": self._safe_trend_label(entries.get("M5"))},
            },
        }

    def _safe_trend_label(self, df):
        if df is None or df.empty or len(df) < 2:
            return "UNKNOWN"
        first_close = float(df.iloc[0]["close"])
        last_close = float(df.iloc[-1]["close"])
        if last_close > first_close:
            return "BULLISH"
        if last_close < first_close:
            return "BEARISH"
        return "NEUTRAL"
    
    def get_structure_analysis(self, df, timeframe):
        """
        Analyzes market structure for a given timeframe.
        """
        if df is None or df.empty:
            return None
        
        analyzer = StructureAnalyzer(df, timeframe)
        return analyzer.analyze()

    def calculate_institutional_score(self, df, smc_data=None, regime=None, recent_sweeps=None):
        """
        Calculate trading signal using 4-layer institutional model.
        """

        if df is None or len(df) < 50:
            return self._null_signal("Insufficient data")

        primary_df = self._rates_to_df(df)
        higher, zones, entries = self._build_mtf_context(primary_df)
        decision = analyze_confluence(
            higher_timeframes=higher,
            zone_timeframes=zones,
            entry_timeframes=entries,
            config=self._build_confluence_config(),
        )
        signal = self._decision_to_signal(decision, higher, zones, entries)
        self.last_signal = signal
        log_event("SMC_CONFLUENCE_SIGNAL", {
            "direction": signal.get("direction"),
            "score": signal.get("score"),
            "confidence": signal.get("confidence"),
            "reasons": signal.get("reasons", []),
            "poi": signal.get("point_of_interest", {}),
        })
        return signal

        # --- Structure Analysis ---
        h1_structure = self.get_structure_analysis(df, "H1")
        m15_structure = self.get_structure_analysis(df, "M15")
        m5_structure = self.get_structure_analysis(df, "M5")

        # --- Scoring ---
        score = {
            "trend_alignment": 0,
            "structure": 0,
            "liquidity": 0,
            "momentum": 0,
            "volatility": 0,
            "confirmation": 0,
        }

        base_score = 0.0
        sweep_detected = False
        sweep_direction = None
        direction = None

        ema_fast = df["ema9"].iloc[-1] if "ema9" in df.columns else None
        ema_slow = df["ema50"].iloc[-1] if "ema50" in df.columns else None
        higher_timeframe_trend = None
        if ema_fast is not None and ema_slow is not None:
            if ema_fast > ema_slow:
                higher_timeframe_trend = "BUY"
            elif ema_fast < ema_slow:
                higher_timeframe_trend = "SELL"

        rsi = df["rsi"].iloc[-1] if "rsi" in df.columns else None
        macd = df["macd"].iloc[-1] if "macd" in df.columns else None
        atr = df["atr"].iloc[-1] if "atr" in df.columns else None
        atr_avg = df["atr"].rolling(20).mean().iloc[-1] if "atr" in df.columns else None

        structure_state = None
        if smc_data:
            structure_state = str(smc_data.get("structure", "")).upper()

            if smc_data.get("bull_sweep"):
                direction = "BUY"
                sweep_detected = True
                sweep_direction = "BUY"
                base_score = 3.0
                score["liquidity"] += 3
                log_event("SIGNAL", {"event": "Bull sweep detected"})
            elif smc_data.get("bear_sweep"):
                direction = "SELL"
                sweep_detected = True
                sweep_direction = "SELL"
                base_score = -3.0
                score["liquidity"] -= 3
                log_event("SIGNAL", {"event": "Bear sweep detected"})

            if direction == "BUY":
                if structure_state == "BULLISH":
                    score["structure"] += 3
                elif structure_state == "BEARISH":
                    score["structure"] -= 2

                if smc_data.get("bos"):
                    score["structure"] += 2
                if smc_data.get("choch"):
                    score["structure"] += 2

            elif direction == "SELL":
                if structure_state == "BEARISH":
                    score["structure"] -= 3
                elif structure_state == "BULLISH":
                    score["structure"] -= 2

                if smc_data.get("bos"):
                    score["structure"] -= 2
                if smc_data.get("choch"):
                    score["structure"] -= 2

            if direction and higher_timeframe_trend:
                if direction == higher_timeframe_trend:
                    score["trend_alignment"] += 3
                else:
                    score["trend_alignment"] -= 4

        if rsi is not None and macd is not None and direction:
            bullish_momentum = rsi > 55 and macd > 0
            bearish_momentum = rsi < 45 and macd < 0

            if direction == "BUY":
                if bullish_momentum:
                    score["momentum"] += 2
                elif bearish_momentum:
                    score["momentum"] -= 2
                if rsi > 70 or rsi < 30:
                    score["momentum"] += 1
            elif direction == "SELL":
                if bearish_momentum:
                    score["momentum"] -= 2
                elif bullish_momentum:
                    score["momentum"] += 2
                if rsi > 70 or rsi < 30:
                    score["momentum"] -= 1

        if sweep_detected and sweep_direction and recent_sweeps:
            sweep_count = self._count_recent_sweeps(recent_sweeps, sweep_direction)
            if sweep_count >= 2:
                base_score += 2 if direction == "BUY" else -2 if direction == "SELL" else 0

        if direction == "BUY" and higher_timeframe_trend == "BUY" and rsi is not None and rsi > 55:
            base_score += 2
        elif direction == "SELL" and higher_timeframe_trend == "SELL" and rsi is not None and rsi < 45:
            base_score -= 2

        if atr is not None and atr_avg is not None and direction:
            vol_ratio = atr / atr_avg if atr_avg else 0
            if vol_ratio > 1.2:
                score["volatility"] += 2 if direction == "BUY" else -2
            elif vol_ratio < 0.7:
                score["volatility"] -= 2 if direction == "BUY" else -2

        bullish_confirmation = bool(
            (score["trend_alignment"] > 0 and score["structure"] > 0)
            or (smc_data and smc_data.get("in_bull_fvg") and rsi is not None and rsi > 50)
        )
        bearish_confirmation = bool(
            (score["trend_alignment"] < 0 and score["structure"] < 0)
            or (smc_data and smc_data.get("in_bear_fvg") and rsi is not None and rsi < 50)
        )

        if direction == "BUY" and bullish_confirmation:
            score["confirmation"] += 3
        elif direction == "SELL" and bearish_confirmation:
            score["confirmation"] -= 3

        total_score = sum(score.values()) + base_score
        if regime and regime.get("regime") == "DEAD":
            total_score *= 0.5

        entry_threshold = 5.5
        if not sweep_detected:
            direction = None
        elif direction == "BUY" and total_score < entry_threshold:
            direction = None
        elif direction == "SELL" and total_score > -entry_threshold:
            direction = None

        log_event("DEBUG_DIRECTION", {
            "direction": direction,
            "structure": float(score["structure"]),
            "momentum": float(score["momentum"]),
            "confirmation": float(score["confirmation"]),
            "trend_alignment": float(score["trend_alignment"]),
            "correlation": 0.0,
            "final_score": float(total_score),
        })

        abs_score = abs(total_score)
        if abs_score >= config.INSTITUTIONAL_HIGH_CONFIDENCE:
            confidence = "HIGH"
        elif abs_score >= config.INSTITUTIONAL_MEDIUM_CONFIDENCE:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        signal = {
            "direction": direction,
            "score": total_score,
            "confidence": confidence,
            "bias": "BULLISH" if higher_timeframe_trend == "BUY" else "BEARISH" if higher_timeframe_trend == "SELL" else None,
            "breakdown": score,
            "entry_threshold": entry_threshold,
            "base_score": base_score,
            "sweep_detected": sweep_detected,
            "sweep_direction": sweep_direction,
            "recent_sweep_count": self._count_recent_sweeps(recent_sweeps, sweep_direction) if sweep_detected and sweep_direction else 0,
            "structure_analysis": {
                "H1": h1_structure,
                "M15": m15_structure,
                "M5": m5_structure
            }
        }

        self.last_signal = signal
        return signal

    def detect_signal(self, df, smc_data=None, regime=None, candle_time=None, recent_sweeps=None):
        """
        Generate a signal with duplicate suppression tied to candle time.

        Returns None when the same candle has already been processed or when the
        computed signal repeats within the cooldown window.
        """

        current_time = time.time()

        if candle_time is not None:
            if candle_time == self.last_candle_time:
                return None
            self.last_candle_time = candle_time

        signal = self.calculate_institutional_score(df, smc_data=smc_data, regime=regime, recent_sweeps=recent_sweeps)

        if not signal or not signal.get("direction"):
            return None

        signature = self._build_signal_signature(signal)
        if self.last_signal_signature and signature == self.last_signal_signature and (current_time - self.last_signal_time) < self.signal_cooldown:
            return None

        self.last_signal = signal
        self.last_signal_signature = signature
        self.last_signal_time = current_time
        return signal
    
    def _null_signal(self, reason):
        """Return neutral signal"""
        return {
            "direction": None,
            "score": 0,
            "confidence": "NONE",
            "bias": None,
            "reason": reason
        }
    
    def should_enter_trade(self, signal, additional_checks=True):
        """
        Final entry decision after all checks
        
        Returns:
            (bool, str): (should_enter, reason)
        """
        
        if not signal or not signal.get("direction"):
            return False, "No direction in signal"
        
        if signal.get("forced_entry") or signal.get("entry_override") or signal.get("early_entry"):
            return True, signal.get("forced_entry_reason") or signal.get("entry_label") or "Validated entry"

        if signal["confidence"] == "LOW":
            return False, "Confidence too low"
        
        # Additional risk checks if enabled
        if additional_checks:
            # Check time of day, session, etc.
            pass
        
        return True, "Signal validated"
    
    def determine_trade_strategy_type(self, regime, signal):
        """
        Classify trade as SCALP or INSTITUTIONAL based on regime and signal quality.
        
        Args:
            regime (str): Current market regime (TREND, HIGH_VOL, RANGE, DEAD, TRANSITION)
            signal (dict): Signal object containing score, confidence, direction
            
        Returns:
            str: "SCALP" or "INSTITUTIONAL"
        """
        if not config.ENABLE_TRADE_STRATEGY_LABEL:
            return "INSTITUTIONAL"  # Default to institutional if feature disabled
        
        if not signal or not signal.get("direction"):
            return "INSTITUTIONAL"
        
        try:
            regime = str(regime or "").upper()
            score = float(signal.get("score", 0) or 0)
            confidence = str(signal.get("confidence", "")).upper()
            
            # Scalp conditions: HIGH_VOL or RANGE regime, decent score
            if regime in config.SCALP_REGIME_TYPES:
                if score >= config.SCALP_MIN_SCORE:
                    return "SCALP"
            
            # Institutional conditions: TREND regime, strong score
            if regime in config.INSTITUTIONAL_REGIME_TYPES:
                if score >= config.INSTITUTIONAL_MIN_SCORE and confidence == "HIGH":
                    return "INSTITUTIONAL"
            
            # Default decision based on confidence and score
            if confidence == "HIGH" and score >= config.INSTITUTIONAL_MIN_SCORE:
                return "INSTITUTIONAL"
            elif score >= config.SCALP_MIN_SCORE:
                return "SCALP"
            
            return "INSTITUTIONAL"  # Safe default
        
        except (TypeError, ValueError, AttributeError) as e:
            log_event("WARNING", {
                "message": f"Error determining trade strategy: {str(e)}",
                "regime": regime,
                "signal_score": signal.get("score") if signal else None
            })
            return "INSTITUTIONAL"
