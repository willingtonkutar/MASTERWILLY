# ============================================================
#  MAIN - INSTITUTIONAL TRADING BOT (v2 - Production Ready)
# ============================================================

import time
from datetime import datetime, timedelta
import pandas as pd
import MetaTrader5 as mt5
from broker.mt5_client import connect_mt5, disconnect_mt5, get_rates, get_spread, get_account_info
from risk.risk_manager import RiskManager
from state.state_manager import StateManager
from strategy.strategy_engine import InstitutionalStrategyEngine
from strategy.regime_detector import RegimeDetector
from strategy.smc import SMCAnalyzer
from execution.engine import ExecutionEngine
from execution.exit_engine import SmartExitEngine
from execution.validation_engine import ExecutionValidator
from data.feed import get_market_data, calculate_atr, calculate_ema
from data.validation_layer import DataValidator
from ai.claude import analyze_market_context, refine_signal, get_session_usage_summary
from monitoring.logger import log_event
from monitoring.telegram_notifier import send_telegram_signal, escape_telegram_html, telegram_is_configured
from monitoring.startup_reporter import run_startup_analysis
import config


_last_signal_by_symbol = {}


def _parse_iso_to_timestamp(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _get_trade_outcome_cooldown_minutes(pnl, pnl_percent=0.0):
    return float(getattr(config, "SAME_DIRECTION_COOLDOWN_AFTER_CLOSE_MINUTES", 20) or 20)


def log_trade_rejected(reason, **details):
    payload = {"reason": str(reason)}
    payload.update({k: v for k, v in details.items() if v is not None})
    log_event("TRADE_REJECTED", payload)


def _apply_directional_cooldown_condition(regime, correlation_score, confidence):
    corr_min = float(getattr(config, "COOLDOWN_CONDITIONAL_CORRELATION_MIN", 2.0) or 2.0)
    return (
        str(regime or "").upper() != "TREND"
        or float(correlation_score or 0.0) < corr_min
        or str(confidence or "").upper() != "HIGH"
    )


def _is_strong_trend_cooldown_bypass(regime, score, correlation_score, confidence):
    corr_min = float(getattr(config, "COOLDOWN_CONDITIONAL_CORRELATION_MIN", 2.0) or 2.0)
    bypass_min_score = float(getattr(config, "COOLDOWN_BYPASS_MIN_SCORE", 15.0) or 15.0)
    return (
        str(regime or "").upper() == "TREND"
        and float(score or 0.0) >= bypass_min_score
        and float(correlation_score or 0.0) >= corr_min
        and str(confidence or "").upper() == "HIGH"
    )


def _set_directional_cooldown(cooldown_state, direction, minutes, source, now_ts=None):
    if not isinstance(cooldown_state, dict):
        return

    now_ts = float(now_ts or time.time())
    direction_key = str(direction or "").upper()
    if direction_key not in {"BUY", "SELL"}:
        return

    duration_secs = max(float(minutes or 0.0), 0.0) * 60.0
    cooldown_state[direction_key] = {
        "expires_at": now_ts + duration_secs,
        "minutes": float(minutes or 0.0),
        "source": str(source or "entry"),
    }


def _infer_state_directional_cooldown_expiry(state, direction):
    if state is None:
        return None

    direction_key = str(direction or "").upper()
    if direction_key not in {"BUY", "SELL"}:
        return None

    state_data = getattr(state, "state", {}) or {}
    expiry_candidates = []
    default_minutes = float(getattr(config, "DIRECTIONAL_COOLDOWN_DEFAULT_MINUTES", 15) or 15)

    for trade in state_data.get("open_trades", []):
        if str(trade.get("direction", "")).upper() != direction_key:
            continue
        open_ts = _parse_iso_to_timestamp(trade.get("open_time"))
        if open_ts is None:
            continue
        expiry_candidates.append(open_ts + (default_minutes * 60.0))

    for trade in state_data.get("closed_trades", []):
        if str(trade.get("direction", "")).upper() != direction_key:
            continue
        close_ts = _parse_iso_to_timestamp(trade.get("close_time"))
        if close_ts is None:
            continue
        pnl = float(trade.get("pnl", 0.0) or 0.0)
        pnl_percent = abs(pnl_cash_to_percent(pnl))
        minutes = _get_trade_outcome_cooldown_minutes(pnl, pnl_percent=pnl_percent)
        expiry_candidates.append(close_ts + (minutes * 60.0))

    return max(expiry_candidates) if expiry_candidates else None


def evaluate_directional_cooldown(direction, regime, score, correlation_score, confidence, cooldown_state=None, state=None, now_ts=None):
    now_ts = float(now_ts or time.time())
    direction_key = str(direction or "").upper()

    if not getattr(config, "DIRECTIONAL_COOLDOWN_ENABLED", True):
        return {
            "active": False,
            "blocked": False,
            "bypassed": False,
            "remaining_secs": 0.0,
            "expires_at": None,
            "apply_condition": False,
            "bypass_condition": False,
            "direction": direction_key,
        }

    memory_expiry = None
    if isinstance(cooldown_state, dict):
        memory_expiry = float((cooldown_state.get(direction_key) or {}).get("expires_at", 0.0) or 0.0)

    state_expiry = _infer_state_directional_cooldown_expiry(state, direction_key)
    expires_at = max(memory_expiry or 0.0, float(state_expiry or 0.0))
    active = expires_at > now_ts
    remaining_secs = max(0.0, expires_at - now_ts)

    apply_condition = True
    bypass_condition = False
    blocked = active
    bypassed = False

    return {
        "active": active,
        "blocked": blocked,
        "bypassed": bypassed,
        "remaining_secs": remaining_secs,
        "expires_at": expires_at if active else None,
        "apply_condition": apply_condition,
        "bypass_condition": bypass_condition,
        "direction": direction_key,
    }


def _minutes_since_timestamp(value, now_ts=None):
    timestamp = _parse_iso_to_timestamp(value)
    if timestamp is None:
        return None
    return (float(now_ts or time.time()) - timestamp) / 60.0


def _get_min_hold_minutes():
    return float(getattr(config, "MIN_HOLD_MINUTES", 4) or 4)


def _position_hold_minutes(position, now_ts=None):
    if not position:
        return None

    open_time = position.get("open_time")
    if not open_time and isinstance(position.get("entry_conditions"), dict):
        open_time = position.get("entry_conditions", {}).get("entry_timestamp")
    return _minutes_since_timestamp(open_time, now_ts=now_ts)


def _hold_time_reached(position, now_ts=None):
    hold_minutes = _position_hold_minutes(position, now_ts=now_ts)
    if hold_minutes is None:
        return True
    return hold_minutes >= _get_min_hold_minutes()


def _is_pullback_or_structure_reset(signal_direction, df, smc_data):
    if df is None or len(df) == 0:
        return False

    last = df.iloc[-1]
    atr = float(last.get("atr", 0.0) or 0.0)
    ema50 = float(last.get("ema50", 0.0) or 0.0)
    close = float(last.get("close", 0.0) or 0.0)
    tolerance = atr * 0.5 if atr > 0 else 0.0
    direction = str(signal_direction or "").upper()

    near_ema = abs(close - ema50) <= tolerance if ema50 else False
    structure_reset = bool((smc_data or {}).get("bos") or (smc_data or {}).get("choch"))

    if direction == "BUY":
        pullback = close <= ema50 or near_ema
    elif direction == "SELL":
        pullback = close >= ema50 or near_ema
    else:
        pullback = near_ema

    return pullback or structure_reset


def _supporting_correlation_boost(signal_direction, dxy_info=None, silver_info=None):
    direction = str(signal_direction or "").upper()
    dxy_alignment = describe_dxy_alignment(direction, (dxy_info or {}).get("trend") if isinstance(dxy_info, dict) else None)
    silver_alignment = describe_silver_alignment(direction, (silver_info or {}).get("trend") if isinstance(silver_info, dict) else None, (silver_info or {}).get("momentum_state") if isinstance(silver_info, dict) else None)
    return ("supports" in dxy_alignment.lower()) and ("supports" in silver_alignment.lower())


def evaluate_trade_protections(signal, context):
    """Shared fatigue and late-entry protections for execution and signal bots."""

    if not signal or not signal.get("direction"):
        return {
            "allowed": False,
            "reason": "No direction in signal",
            "risk_multiplier_adjustment": 1.0,
            "trade_quality": "WEAK",
            "trade_stage": "MID TREND",
            "labels": [],
        }

    df = context.get("df")
    smc_data = context.get("smc_data") or {}
    state = context.get("state")
    dxy_info = context.get("dxy_info") or {}
    silver_info = context.get("silver_info") or {}
    correlation_score = float(context.get("correlation_score_adjustment", 0.0) or 0.0)
    now_ts = float(context.get("now_ts", time.time()) or time.time())
    direction = str(signal.get("direction") or "").upper()
    score = float(signal.get("score", 0.0) or 0.0)

    labels = []
    risk_multiplier_adjustment = 1.0
    trade_quality = "STRONG"
    trade_stage = "MID TREND"

    if getattr(config, "ENABLE_TREND_FATIGUE_FILTER", True):
        fatigue_state = {}
        if state and hasattr(state, "get_directional_trade_streak"):
            fatigue_state = state.get_directional_trade_streak() or {}

        fatigue_direction = str(fatigue_state.get("direction") or "").upper()
        fatigue_count = int(fatigue_state.get("count", 0) or 0)
        streak_age = _minutes_since_timestamp(fatigue_state.get("updated_at"), now_ts=now_ts)
        if streak_age is not None and streak_age >= _get_min_hold_minutes():
            fatigue_count = 0
        if fatigue_direction and fatigue_direction != direction:
            fatigue_count = 0

        limit = int(getattr(config, "MAX_CONSECUTIVE_TRADES_PER_DIRECTION", 3) or 3)
        pullback_ok = _is_pullback_or_structure_reset(direction, df, smc_data)
        if fatigue_count >= limit:
            labels.append("TREND_FATIGUE")
            if not pullback_ok:
                if getattr(config, "TREND_FATIGUE_SOFT_MODE", False):
                    risk_multiplier_adjustment *= float(getattr(config, "TREND_FATIGUE_REDUCTION_MULTIPLIER", 0.7) or 0.7)
                    trade_quality = "MODERATE"
                    labels.append("TREND_FATIGUE_SOFT")
                else:
                    return {
                        "allowed": False,
                        "reason": "TREND_FATIGUE: max consecutive trades reached",
                        "risk_multiplier_adjustment": 1.0,
                        "trade_quality": "WEAK",
                        "trade_stage": "LATE TREND",
                        "labels": labels,
                        "trend_fatigue_count": fatigue_count,
                    }

    if not getattr(config, "ENABLE_LATE_ENTRY_FILTER", True) or df is None or len(df) < 3:
        return {
            "allowed": True,
            "reason": "OK",
            "risk_multiplier_adjustment": risk_multiplier_adjustment,
            "trade_quality": trade_quality,
            "trade_stage": trade_stage,
            "labels": labels,
            "trend_fatigue_count": int(fatigue_state.get("count", 0) or 0) if "fatigue_state" in locals() else 0,
        }

    last = df.iloc[-1]
    prev = df.iloc[-2]
    atr = float(last.get("atr", 0.0) or 0.0)
    ema50 = float(last.get("ema50", 0.0) or 0.0)
    close = float(last.get("close", 0.0) or 0.0)
    rsi = float(last.get("rsi", 0.0) or 0.0)
    prev_rsi = float(prev.get("rsi", rsi) or rsi)
    macd_hist = float(last.get("macd_hist", 0.0) or 0.0)
    prev_macd_hist = float(prev.get("macd_hist", macd_hist) or macd_hist)
    distance_atr = abs(close - ema50) / atr if atr else 0.0
    max_distance = float(getattr(config, "MAX_DISTANCE_FROM_EMA_ATR", 1.5) or 1.5)
    overbought = rsi >= float(getattr(config, "RSI_OVERBOUGHT_THRESHOLD", 70) or 70)
    oversold = rsi <= float(getattr(config, "RSI_OVERSOLD_THRESHOLD", 30) or 30)
    momentum_weakening = abs(rsi - prev_rsi) <= 1.0 and macd_hist <= prev_macd_hist

    if distance_atr < max_distance * 0.75:
        trade_stage = "EARLY TREND"
    elif distance_atr >= max_distance:
        trade_stage = "LATE TREND"
    else:
        trade_stage = "MID TREND"

    overextended = False
    if direction == "BUY" and overbought and distance_atr >= max_distance:
        overextended = True
    if direction == "SELL" and oversold and distance_atr >= max_distance:
        overextended = True

    if overextended:
        labels.append("OVEREXTENSION_DETECTED")
        trade_quality = "WEAK"
        strong_correlation_support = _supporting_correlation_boost(direction, dxy_info, silver_info)
        if strong_correlation_support and correlation_score >= float(getattr(config, "LATE_ENTRY_CORRELATION_BOOST_THRESHOLD", 2.0) or 2.0):
            risk_multiplier_adjustment *= float(getattr(config, "LATE_ENTRY_RISK_REDUCTION_MULTIPLIER", 0.7) or 0.7)
            labels.append("CORRELATION_SUPPORT")
        else:
            return {
                "allowed": False,
                "reason": "LATE_ENTRY_BLOCKED: overextension detected",
                "risk_multiplier_adjustment": 1.0,
                "trade_quality": "WEAK",
                "trade_stage": "LATE TREND",
                "labels": labels,
                "overextended": True,
                "trend_fatigue_count": int(fatigue_state.get("count", 0) or 0) if "fatigue_state" in locals() else 0,
            }

    if momentum_weakening:
        labels.append("MOMENTUM_WEAKENING")
        if trade_quality == "STRONG":
            trade_quality = "MODERATE"
        risk_multiplier_adjustment *= 0.9

    if trade_quality == "STRONG" and trade_stage == "LATE TREND":
        trade_quality = "MODERATE"

    return {
        "allowed": True,
        "reason": "OK",
        "risk_multiplier_adjustment": risk_multiplier_adjustment,
        "trade_quality": trade_quality,
        "trade_stage": trade_stage,
        "labels": labels,
        "overextended": overextended,
        "momentum_weakening": momentum_weakening,
        "distance_from_ema_atr": distance_atr,
        "trend_fatigue_count": int(fatigue_state.get("count", 0) or 0) if "fatigue_state" in locals() else 0,
    }


def calculate_indicators(rates):
    """Calculate technical indicators from raw rates"""
    
    df = pd.DataFrame(rates)
    
    # EMA
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    
    # RSI
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))
    
    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]
    
    # ATR
    df["atr"] = calculate_atr(rates, period=14)
    
    return df


def get_last_candle_key(rates):
    """Return the most recent candle identity for change detection."""

    if rates is None or len(rates) == 0:
        return None

    last = rates[-1]
    try:
        return last["time"]
    except (KeyError, TypeError, IndexError):
        return None


def build_structure_key(df, smc_data, regime_data):
    """Build a compact signature of the current market structure."""

    if df is None or len(df) == 0:
        return None

    last = df.iloc[-1]
    return (
        f"{regime_data.get('regime')}|"
        f"{smc_data.get('structure')}|"
        f"{smc_data.get('bos')}|"
        f"{smc_data.get('choch')}|"
        f"{round(float(last.get('close', 0)), 2)}|"
        f"{round(float(last.get('ema9', 0)), 2)}|"
        f"{round(float(last.get('ema50', 0)), 2)}"
    )


def sync_mt5_positions_with_state(state):
    """
    CRITICAL: Sync MT5 open positions with bot state file on startup.
    Detects trades that were placed but not registered due to crashes.
    Prevents duplicate trades and lost position management.
    """
    import MetaTrader5 as mt5
    
    # Get all open positions from MT5
    positions = mt5.positions_get()
    
    if positions is None:
        log_event("WARNING", {"message": "Could not retrieve MT5 positions"})
        return
    
    # Filter for our symbol
    our_positions = [p for p in positions if p.symbol == config.SYMBOL]
    
    if len(our_positions) == 0:
        log_event("INFO", {"message": "No open positions found on MT5"})
        return
    
    # Get tickets already in our state
    registered_tickets = {t["ticket"] for t in state.state.get("open_trades", [])}
    
    # Register any unregistered positions
    for pos in our_positions:
        if pos.ticket not in registered_tickets:
            print(f"\n⚠️  ORPHANED TRADE DETECTED - Registering with bot state")
            print(f"   Ticket: {pos.ticket}")
            print(f"   Symbol: {pos.symbol}")
            print(f"   Type: {'BUY' if pos.type == mt5.ORDER_TYPE_BUY else 'SELL'}")
            print(f"   Volume: {pos.volume}")
            print(f"   Entry Price: {pos.price_open}")
            print(f"   SL: {pos.sl if pos.sl != 0 else 'None'}")
            print(f"   TP: {pos.tp if pos.tp != 0 else 'None'}\n")
            
            direction = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
            
            state.register_trade_open(
                pos.ticket,
                pos.symbol,
                direction,
                pos.price_open,
                pos.volume,
                pos.sl if pos.sl != 0 else 0,
                pos.tp if pos.tp != 0 else 0
            )
            
            log_event("ORPHANED_TRADE_RECOVERED", {
                "ticket": pos.ticket,
                "direction": direction,
                "volume": pos.volume,
                "entry_price": pos.price_open
            })


def reconcile_state_with_mt5(state):
    """Reconcile bot state with MT5 and settle trades that were closed broker-side."""
    import MetaTrader5 as mt5

    positions = mt5.positions_get(symbol=config.SYMBOL)
    if positions is None:
        return

    live_tickets = {p.ticket for p in positions}
    state_open_trades = list(state.get_open_trades())

    for trade in state_open_trades:
        ticket = trade.get("ticket")
        if ticket not in live_tickets:
            open_time = trade.get("open_time")
            date_from = None
            if open_time:
                try:
                    date_from = datetime.fromisoformat(str(open_time))
                except ValueError:
                    date_from = None

            if date_from is None:
                date_from = datetime.now() - timedelta(days=30)

            date_to = datetime.now()
            deals = None
            for lookup in (
                {"position": ticket},
                {"ticket": ticket},
            ):
                try:
                    deals = mt5.history_deals_get(date_from, date_to, **lookup)
                except Exception:
                    deals = None
                if deals:
                    break

            if deals:
                total_profit = 0.0
                for deal in deals:
                    total_profit += float(getattr(deal, "profit", 0.0) or 0.0)
                    total_profit += float(getattr(deal, "swap", 0.0) or 0.0)
                    total_profit += float(getattr(deal, "commission", 0.0) or 0.0)
                    total_profit += float(getattr(deal, "fee", 0.0) or 0.0)
                last_deal = max(deals, key=lambda deal: getattr(deal, "time", 0) or 0)
                close_price = float(getattr(last_deal, "price", trade.get("entry_price", 0.0)) or trade.get("entry_price", 0.0))

                if state.register_trade_close(ticket, close_price, total_profit):
                    log_event("STALE_TRADE_SETTLED", {
                        "ticket": ticket,
                        "profit": total_profit,
                        "reason": "Closed on MT5 (reconciled)",
                    })
                    continue

            state.drop_stale_open_trade(ticket, reason="Closed on MT5 (reconciled)")


def is_high_potential_signal(signal):
    """Return True when signal qualifies for optional 2nd trade."""
    if signal is None:
        return False

    confidence = str(signal.get("confidence", "")).upper()
    score = float(signal.get("score", 0) or 0)

    return (
        confidence == str(config.HIGH_POTENTIAL_CONFIDENCE).upper()
        and score >= float(config.HIGH_POTENTIAL_MIN_SCORE)
    )


def has_open_position_same_direction(direction, symbol=None):
    """Return True when a live MT5 position exists for symbol in same direction."""
    symbol = symbol or config.SYMBOL
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return False

    target_type = mt5.ORDER_TYPE_BUY if str(direction).upper() == "BUY" else mt5.ORDER_TYPE_SELL
    for pos in positions:
        if int(pos.type) == int(target_type):
            return True
    return False


def get_open_trades_same_direction(open_trades, direction, symbol=None):
    """Return open trades matching direction and symbol from local state."""
    symbol = symbol or config.SYMBOL
    direction_key = str(direction or "").upper()
    if direction_key not in {"BUY", "SELL"}:
        return []

    matched = []
    for trade in open_trades or []:
        if str(trade.get("symbol", "")).upper() != str(symbol).upper():
            continue
        if str(trade.get("direction", "")).upper() != direction_key:
            continue
        matched.append(trade)

    matched.sort(key=lambda item: _parse_iso_to_timestamp(item.get("open_time")) or 0.0)
    return matched


def _trade_profit_r(trade, current_price):
    """Return current open-trade profit in R units."""
    entry_price = float(trade.get("entry_price", 0.0) or 0.0)
    reference_sl = float(trade.get("initial_sl", trade.get("sl", entry_price)) or entry_price)
    risk = abs(entry_price - reference_sl)
    if risk <= 0:
        return 0.0

    direction = str(trade.get("direction", "")).upper()
    profit = float(current_price) - entry_price if direction == "BUY" else entry_price - float(current_price)
    return profit / risk


def _get_same_direction_add_on_gate(open_trades, direction, current_price):
    """Return add-on permission and first-trade profit for same-direction stacking."""
    same_direction_trades = get_open_trades_same_direction(open_trades, direction, symbol=config.SYMBOL)
    if not same_direction_trades:
        return {
            "allowed": True,
            "same_direction_trades": [],
            "first_trade_profit_r": None,
            "reason": None,
        }

    scalp_cap = int(getattr(config, "SCALP_MAX_TRADES", 2) or 2)
    if len(same_direction_trades) >= scalp_cap:
        return {
            "allowed": False,
            "same_direction_trades": same_direction_trades,
            "first_trade_profit_r": None,
            "reason": f"Same-direction trade cap reached: {len(same_direction_trades)}",
        }

    first_trade = same_direction_trades[0]
    first_trade_profit_r = _trade_profit_r(first_trade, current_price)
    if first_trade_profit_r < 0.5:
        return {
            "allowed": False,
            "same_direction_trades": same_direction_trades,
            "first_trade_profit_r": first_trade_profit_r,
            "reason": f"Same-direction add-on blocked: first trade only {first_trade_profit_r:.2f}R",
        }

    return {
        "allowed": True,
        "same_direction_trades": same_direction_trades,
        "first_trade_profit_r": first_trade_profit_r,
        "reason": None,
    }


def pnl_cash_to_percent(pnl_cash):
    """Convert account-currency PnL to percent of account balance."""
    account_info = get_account_info()
    balance = getattr(account_info, "balance", 0.0) if account_info else 0.0
    if not balance or balance <= 0:
        return 0.0

    return (float(pnl_cash) / float(balance)) * 100.0


def are_entry_conditions_still_valid(trade, current_signal):
    """
    Check if the original entry conditions for a trade are still valid.
    Returns True if we should KEEP the trade open (conditions still good).
    Returns False if conditions have degraded and exit should be considered.
    
    This prevents exiting profitable trades on bot restart if the original
    entry setup is still intact. Only allows exits when conditions truly degrade.
    """
    if not config.ENABLE_TRADE_ENTRY_MEMORY:
        return True  # Feature disabled
    
    if not trade or not current_signal:
        return True  # Default: keep trade if we can't evaluate
    
    # Get stored entry conditions
    entry_conditions = trade.get("entry_conditions")
    if not entry_conditions:
        return True  # No stored conditions, keep trade by default
    
    original_score = entry_conditions.get("score", 0)
    original_confidence = entry_conditions.get("confidence", "MEDIUM")
    current_score = current_signal.get("score", 0)
    current_confidence = current_signal.get("confidence", "MEDIUM")
    
    # Allow exit if current conditions SIGNIFICANTLY degraded
    # (But give the trade some grace - only exit if quality really dropped)
    
    # Rule 1: If confidence dropped more than tolerance, exit conditions may apply
    confidence_levels = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}
    original_conf_level = confidence_levels.get(str(original_confidence).upper(), 1)
    current_conf_level = confidence_levels.get(str(current_confidence).upper(), 1)
    
    # If confidence fell more than allowed tolerance, conditions degraded
    if current_conf_level < original_conf_level - config.ENTRY_CONDITION_CONFIDENCE_TOLERANCE:
        return False  # Conditions degraded significantly
    
    # Rule 2: Score must not drop below tolerance ratio from original
    if original_score > 0:
        score_ratio = current_score / original_score if current_score > 0 else 0
        if score_ratio < config.ENTRY_CONDITION_SCORE_TOLERANCE:  # Less than allowed percent of original score
            return False  # Quality degraded
    
    # Rule 3: Keep trade alive if conditions maintained or improved
    return True


def should_log_entry_condition_skip(skip_log_state, ticket, exit_type, candle_key):
    """Log skip messages only once per candle for each trade and exit type."""
    key = (ticket, exit_type)
    if skip_log_state.get(key) == candle_key:
        return False

    skip_log_state[key] = candle_key
    return True


def manage_open_trade_exits(open_trades, df, smc_data, current_signal, candle_key, state, engine, exit_engine, risk, directional_cooldown_state, last_skip_log_state, last_sl_update_by_ticket):
    """Handle partial exits, full exits, and SL/TP management for open positions."""

    if not open_trades:
        return

    for trade in list(open_trades):
        ticket = trade.get("ticket")
        if ticket is None:
            continue

        direction = str(trade.get("direction", "")).upper()
        current_sl = trade.get("sl")
        current_tp = trade.get("tp")
        entry_price = float(trade.get("entry_price", 0.0) or 0.0)
        current_price = float(df.iloc[-1]["close"])

        reference_sl = trade.get("initial_sl", current_sl)
        sl_distance = abs(entry_price - float(reference_sl or entry_price))

        current_profit = current_price - entry_price if direction == "BUY" else entry_price - current_price
        current_profit_r = (current_profit / sl_distance) if sl_distance > 0 else 0.0

        if not trade.get("trailing_active"):
            trailing_start_r = float(getattr(config, "TRAILING_START_R", 1.0) or 1.0)
            if current_profit_r >= trailing_start_r:
                trade["trailing_active"] = True
                state._save_state()
                log_event("INFO", {
                    "message": "TRAILING_ACTIVATED",
                    "ticket": ticket,
                    "direction": direction,
                    "profit_r": round(float(current_profit_r), 3),
                    "trigger_r": trailing_start_r,
                })
                print(f"✅ TRAILING ACTIVATED: Ticket {ticket} | profit={current_profit_r:.2f}R")

        momentum_flip = exit_engine.is_momentum_flip(direction, df)
        min_profit_r = float(getattr(config, "MOMENTUM_FLIP_PROTECT_MIN_PROFIT_R", 0.3) or 0.3)
        if momentum_flip and current_profit_r >= min_profit_r:
            protected_sl = exit_engine.get_momentum_flip_protective_sl(trade, sl_distance)
            if protected_sl and exit_engine.should_move_sl(current_sl, protected_sl, direction):
                if engine.modify_sl_tp(ticket, protected_sl, current_tp):
                    trade["sl"] = protected_sl
                    state._save_state()
                    log_event("INFO", {
                        "message": "MOMENTUM_FLIP_PROTECT_SL",
                        "ticket": ticket,
                        "direction": direction,
                        "profit_r": round(float(current_profit_r), 3),
                        "new_sl": float(protected_sl),
                    })
                    print(f"✅ MOMENTUM FLIP PROTECT: Ticket {ticket} | SL -> {float(protected_sl):.2f}")
                    if (
                        bool(getattr(config, "TELEGRAM_EXIT_PROTECTION_ALERTS_ENABLED", True))
                        and bool(getattr(config, "TELEGRAM_EXECUTION_ALERTS_ENABLED", True))
                        and telegram_is_configured()
                    ):
                        protect_msg = (
                            "🛡️ MOMENTUM FLIP PROTECTION\n"
                            f"• Ticket: {escape_telegram_html(ticket)}\n"
                            f"• Direction: {escape_telegram_html(direction)}\n"
                            f"• Profit: {float(current_profit_r):.2f}R\n"
                            f"• New SL: {float(protected_sl):.2f}\n"
                            f"• Rule: lock small profit on momentum flip"
                        )
                        send_telegram_signal(protect_msg)
                    last_sl_update_by_ticket[ticket] = candle_key
                    continue

        should_exit, exit_type, exit_reason, target = exit_engine.check_exit_conditions(
            trade, df, smc_data
        )

        if should_exit and exit_type == exit_engine.EXIT_TYPE_PARTIAL_TP and not trade.get("partial_exit_taken"):
            close_volume = round(float(trade.get("lot", 0.0) or 0.0) * 0.5, 2)
            if close_volume > 0:
                partial_result = engine.close_partial_trade(
                    ticket,
                    close_volume,
                    symbol=config.SYMBOL,
                    comment="Partial TP @ 1R",
                )
                if partial_result.get("success"):
                    state.reduce_trade_volume(ticket, close_volume)
                    state.mark_trade_partial_exit(ticket, "PARTIAL_TP", {
                        "closed_volume": close_volume,
                        "remaining_volume": partial_result.get("remaining_volume"),
                        "reason": exit_reason,
                    })
                    trade["partial_exit_taken"] = True
                    trade["lot"] = partial_result.get("remaining_volume", trade.get("lot", 0.0))
                    log_event("PARTIAL_TP", {
                        "ticket": ticket,
                        "closed_lot": close_volume,
                        "remaining_lot": partial_result.get("remaining_volume", 0.0),
                    })
                    print(f"📤 PARTIAL TP: Ticket {ticket} | closed {close_volume:.2f} | remaining {partial_result.get('remaining_volume', 0.0):.2f}")

            last_sl_update_by_ticket[ticket] = candle_key
            continue

        if should_exit and exit_type and exit_type != exit_engine.EXIT_TYPE_PARTIAL_TP:
            entry_conditions_valid = True
            entry_conditions = trade.get("entry_conditions")

            if entry_conditions and exit_type not in [exit_engine.EXIT_TYPE_PROFIT, exit_engine.EXIT_TYPE_MANUAL, exit_engine.EXIT_TYPE_STOP_LOSS]:
                entry_conditions_valid = are_entry_conditions_still_valid(trade, current_signal)

                if not entry_conditions_valid:
                    print(f"   ⚠️  Entry conditions degraded - permitting {exit_type} exit")
                else:
                    if should_log_entry_condition_skip(last_skip_log_state, ticket, exit_type, candle_key):
                        print(f"   ✅ Entry conditions still valid - skipping {exit_type} exit")
                    should_exit = False

            if should_exit:
                print(f"\n🛑 EXIT SIGNAL: {exit_type}")
                print(f"   Reason: {exit_reason}")

                close_result = engine.close_trade(ticket, symbol=config.SYMBOL)

                if close_result.get("success"):
                    pnl = close_result.get("profit", 0)
                    state.register_trade_close(ticket, target, pnl)

                    pnl_percent = abs(pnl_cash_to_percent(pnl))
                    outcome_minutes = _get_trade_outcome_cooldown_minutes(pnl, pnl_percent=pnl_percent)
                    _set_directional_cooldown(
                        directional_cooldown_state,
                        trade.get("direction"),
                        outcome_minutes,
                        source="exit_outcome",
                        now_ts=time.time(),
                    )

                    if exit_type == exit_engine.EXIT_TYPE_LOSS_CUT:
                        log_event("LOSS_CUT", {"ticket": ticket, "reason": exit_reason})
                    elif exit_type == exit_engine.EXIT_TYPE_TIME:
                        log_event("TIME_EXIT", {"ticket": ticket, "reason": exit_reason})
                    elif exit_type == exit_engine.EXIT_TYPE_EARLY_EXIT:
                        log_event("EARLY_EXIT", {"ticket": ticket, "reason": exit_reason})

                    if pnl > 0:
                        risk.register_win(pnl_percent)
                    else:
                        risk.register_loss(pnl_percent)

                continue

        if last_sl_update_by_ticket.get(ticket) == candle_key:
            continue

        new_tp = exit_engine.extend_trend_tp(trade, df)
        if new_tp and exit_engine.should_move_tp(current_tp, new_tp, direction):
            if engine.modify_sl_tp(ticket, current_sl, new_tp):
                trade["tp"] = new_tp
                trade["tp_extended"] = True
                state._save_state()
                log_event("TREND_EXTENSION", {"ticket": ticket, "new_tp": new_tp})
                print(f"✅ TP EXTENDED: Ticket {ticket} -> {new_tp:.2f}")
                last_sl_update_by_ticket[ticket] = candle_key
                continue

        reference_sl = trade.get("initial_sl", current_sl)
        sl_distance = abs(float(trade.get("entry_price", 0.0) or 0.0) - float(reference_sl or trade.get("entry_price", 0.0) or 0.0))
        new_sl = None
        move_reason = None

        if config.ENABLE_BREAKEVEN:
            proposed_sl = exit_engine.apply_breakeven(trade, trade.get("entry_price"), df.iloc[-1]["close"], sl_distance)
            if proposed_sl and exit_engine.should_move_sl(current_sl, proposed_sl, direction):
                new_sl = proposed_sl
                move_reason = f"Break-even moved to {proposed_sl:.2f}"
                log_event("BE_MOVED", {"ticket": ticket, "new_sl": proposed_sl, "entry_price": trade.get("entry_price")})

        if new_sl is None and config.ENABLE_PROFIT_LOCK:
            proposed_sl = exit_engine.lock_profit(trade, trade.get("entry_price"), df.iloc[-1]["close"], sl_distance)
            if proposed_sl and exit_engine.should_move_sl(current_sl, proposed_sl, direction):
                new_sl = proposed_sl
                move_reason = f"Profit lock @ {proposed_sl:.2f}"

        if new_sl is None and config.ENABLE_TRAILING_STOP and trade.get("trailing_active"):
            proposed_sl = exit_engine.apply_trailing_stop(trade, df, smc_data=smc_data)
            if proposed_sl and exit_engine.should_move_sl(current_sl, proposed_sl, direction):
                new_sl = proposed_sl
                move_reason = f"Trailing stop @ {proposed_sl:.2f}"

        if new_sl and exit_engine.should_move_sl(current_sl, new_sl, direction):
            success = engine.modify_sl_tp(
                ticket,
                new_sl,
                current_tp,
                direction=direction,
                current_price=current_price,
                original_sl=reference_sl,
            )
            if success:
                trade["sl"] = new_sl
                state._save_state()
                print(f"✅ SL MOVED: Ticket {ticket}")
                print(f"   {move_reason}")
                print(f"   Old: {float(current_sl or 0.0):.2f} → New: {float(new_sl):.2f}")
                last_sl_update_by_ticket[ticket] = candle_key


def _get_session_filter_datetime():
    """Return reference datetime for session filters using configurable basis."""
    reference = str(getattr(config, "SESSION_FILTER_TIME_REFERENCE", "LOCAL")).upper()
    if reference == "UTC":
        dt = datetime.utcnow()
    else:
        dt = datetime.now()

    offset_hours = int(getattr(config, "SESSION_FILTER_UTC_OFFSET_HOURS", 0) or 0)
    if offset_hours != 0:
        from datetime import timedelta
        dt = dt + timedelta(hours=offset_hours)

    return dt


def get_session_tag(now_dt=None):
    now_dt = now_dt or datetime.now()
    hour = now_dt.hour

    if config.LONDON_START <= hour < config.LONDON_END:
        return "LONDON"
    if config.NY_START <= hour < config.NY_END:
        return "NEW_YORK"
    if config.TOKYO_START <= hour < config.TOKYO_END:
        return "TOKYO"
    return "OFF_HOURS"


def evaluate_session_policy(now_dt=None):
    """Session filters and risk multipliers used by both trading and signal bots."""
    now_dt = now_dt or _get_session_filter_datetime()
    policy = {
        "skip_signal": False,
        "skip_reason": None,
        "risk_multiplier": 1.0,
        "session_tag": get_session_tag(now_dt)
    }

    if not config.ENABLE_SESSION_FILTERS:
        return policy

    ny_block_start_total = (int(getattr(config, "NY_OPEN_BLOCK_START_HOUR", 15)) * 60) + int(getattr(config, "NY_OPEN_BLOCK_START_MINUTE", 30))
    ny_block_end_total = ny_block_start_total + int(getattr(config, "NY_OPEN_BLOCK_DURATION_MINUTES", 30))
    now_total = (now_dt.hour * 60) + now_dt.minute
    if ny_block_start_total <= now_total < ny_block_end_total:
        policy["skip_signal"] = True
        policy["skip_reason"] = "NY open volatility window."
        return policy

    if config.NY_OPEN_TRAP_ENABLED:
        strict_block_minutes = int(getattr(config, "NY_OPEN_STRICT_BLOCK_MINUTES", config.NY_OPEN_TRAP_WINDOW_MINUTES) or 0)
        if now_dt.hour == config.NY_OPEN_TRAP_START_HOUR and now_dt.minute < strict_block_minutes:
            policy["skip_signal"] = True
            policy["skip_reason"] = "SKIPPED: NY open protection"
            return policy

        reduced_risk_enabled = bool(getattr(config, "NY_OPEN_REDUCED_RISK_ENABLED", True))
        reduced_risk_minutes = int(getattr(config, "NY_OPEN_REDUCED_RISK_MINUTES", 30) or 0)
        reduced_risk_multiplier = float(getattr(config, "NY_OPEN_REDUCED_RISK_MULTIPLIER", 0.5) or 0.5)
        if reduced_risk_enabled and now_dt.hour == config.NY_OPEN_TRAP_START_HOUR:
            start_min = strict_block_minutes
            end_min = strict_block_minutes + reduced_risk_minutes
            if start_min <= now_dt.minute < end_min:
                policy["risk_multiplier"] *= reduced_risk_multiplier

    if config.SESSION_OPEN_CLOSE_FILTER_ENABLED:
        session_windows = [
            (
                "LONDON",
                int(config.LONDON_START),
                int(config.LONDON_END),
                int(config.LONDON_OPEN_SKIP_MINUTES),
                int(config.LONDON_CLOSE_SKIP_MINUTES),
            ),
            (
                "NEW_YORK",
                int(config.NY_START),
                int(config.NY_END),
                int(config.NY_OPEN_SKIP_MINUTES),
                int(config.NY_CLOSE_SKIP_MINUTES),
            ),
            (
                "TOKYO",
                int(config.TOKYO_START),
                int(config.TOKYO_END),
                int(config.TOKYO_OPEN_SKIP_MINUTES),
                int(config.TOKYO_CLOSE_SKIP_MINUTES),
            ),
        ]

        def _in_open_window(current_hour, current_minute, session_start_hour, open_skip_minutes):
            return current_hour == session_start_hour and current_minute < max(0, open_skip_minutes)

        def _in_close_window(current_hour, current_minute, session_end_hour, close_skip_minutes):
            if close_skip_minutes <= 0:
                return False

            minutes_before_close = close_skip_minutes
            close_total = (session_end_hour * 60) % (24 * 60)
            current_total = (current_hour * 60 + current_minute) % (24 * 60)
            start_total = (close_total - minutes_before_close) % (24 * 60)

            if start_total <= close_total:
                return start_total <= current_total < close_total
            return current_total >= start_total or current_total < close_total

        for label, start_h, end_h, open_skip, close_skip in session_windows:
            if _in_open_window(now_dt.hour, now_dt.minute, start_h, open_skip):
                policy["skip_signal"] = True
                policy["skip_reason"] = f"SKIPPED: {label} open protection"
                return policy
            if _in_close_window(now_dt.hour, now_dt.minute, end_h, close_skip):
                policy["skip_signal"] = True
                policy["skip_reason"] = f"SKIPPED: {label} close protection"
                return policy

    if config.DEAD_MARKET_FILTER_ENABLED:
        if now_dt.hour >= config.DEAD_MARKET_START_HOUR or now_dt.hour < config.DEAD_MARKET_END_HOUR:
            policy["skip_signal"] = True
            policy["skip_reason"] = "Dead market window (late NY / early Asia)"
            return policy

    if config.FRIDAY_RISK_REDUCTION_ENABLED and now_dt.weekday() == 4 and now_dt.hour >= config.FRIDAY_RISK_REDUCTION_HOUR:
        policy["risk_multiplier"] *= config.FRIDAY_RISK_MULTIPLIER

    return policy


def _confidence_rank(confidence):
    levels = {
        "LOW": 1,
        "MEDIUM": 2,
        "HIGH": 3,
    }
    return levels.get(str(confidence or "LOW").upper(), 1)


def _score_bucket(score):
    score_abs = abs(float(score or 0.0))
    size = int(getattr(config, "SCORE_BUCKET_SIZE", 2) or 2)
    start = int(score_abs // size) * size
    end = start + size
    return f"{start}-{end}"


def _build_signal_signature(signal, context):
    return {
        "direction": str(signal.get("direction", "")).upper(),
        "entry": round(float(context.get("entry_price", 0.0) or 0.0), 2),
        "score_bucket": _score_bucket(signal.get("score", 0.0)),
        "regime": str(context.get("regime", signal.get("regime", "UNKNOWN"))).upper(),
    }


def _is_duplicate_signal(symbol, signature, now_ts):
    prev = _last_signal_by_symbol.get(symbol)
    if not prev:
        return False

    window_minutes = float(getattr(config, "DUPLICATE_SIGNAL_WINDOW_MINUTES", 15) or 15)
    window_secs = window_minutes * 60.0
    if (now_ts - float(prev.get("timestamp", 0.0))) > window_secs:
        return False

    entry_threshold = float(getattr(config, "DUPLICATE_ENTRY_THRESHOLD", 0.5) or 0.5)
    same_direction = prev.get("direction") == signature.get("direction")
    same_regime = prev.get("regime") == signature.get("regime")
    same_bucket = prev.get("score_bucket") == signature.get("score_bucket")
    similar_entry = abs(float(prev.get("entry", 0.0)) - float(signature.get("entry", 0.0))) <= entry_threshold

    return same_direction and same_regime and same_bucket and similar_entry


def register_signal_signature(symbol, signature, now_ts=None):
    if not isinstance(signature, dict):
        return

    _last_signal_by_symbol[symbol] = {
        **signature,
        "timestamp": float(now_ts or time.time())
    }


def _is_momentum_override_candidate(signal, context, protection=None):
    if not bool(getattr(config, "ENABLE_MOMENTUM_OVERRIDE", True)):
        return False

    score = abs(float(signal.get("score", 0.0) or 0.0))
    min_score = float(getattr(config, "MOMENTUM_OVERRIDE_MIN_SCORE", 8.0) or 8.0)
    if score < min_score:
        return False

    confidence = str(signal.get("confidence", "LOW")).upper()
    if _confidence_rank(confidence) < _confidence_rank("MEDIUM"):
        return False

    trend_context = context.get("trend_context", {}) or {}
    df = trend_context.get("df")
    if df is None or len(df) < 2:
        return False

    direction = str(signal.get("direction", "")).upper()
    last = df.iloc[-1]

    close = float(last.get("close", 0.0) or 0.0)
    ema9 = float(last.get("ema9", 0.0) or 0.0)
    ema50 = float(last.get("ema50", 0.0) or 0.0)
    rsi = float(last.get("rsi", 50.0) or 50.0)
    macd_hist = float(last.get("macd_hist", 0.0) or 0.0)

    if direction == "BUY":
        momentum_ok = close > ema9 > ema50 and macd_hist > 0 and 50 <= rsi <= 78
    elif direction == "SELL":
        momentum_ok = close < ema9 < ema50 and macd_hist < 0 and 22 <= rsi <= 50
    else:
        momentum_ok = False

    if not momentum_ok:
        return False

    reason = str((protection or {}).get("reason", ""))
    if reason and not any(token in reason for token in ["LATE_ENTRY_BLOCKED", "TREND_FATIGUE"]):
        return False

    return True


def validate_trade_decision(signal, context):
    """Shared validation gate for both execution and signal bots."""
    mode = str(context.get("mode", "execution")).lower()
    session_policy = context.get("session_policy", {}) or {}
    trend_context = context.get("trend_context", {}) or {}
    now_ts = float(context.get("now_ts", time.time()) or time.time())

    def _reject(reason, trade_quality="WEAK", signature=None):
        symbol = str(context.get("symbol", config.SYMBOL))
        log_trade_rejected(
            reason,
            mode=mode,
            symbol=symbol,
            direction=str(signal.get("direction", "")).upper(),
            score=float(signal.get("score", 0.0) or 0.0),
            confidence=str(signal.get("confidence", "LOW")).upper(),
        )
        
        # PHASE 1: Log to shadow memory for outcome tracking
        try:
            state = trend_context.get("state")
            if state is not None and hasattr(state, "register_rejected_signal"):
                df = trend_context.get("df")
                smc_data = trend_context.get("smc_data") or {}
                entry_price = float(signal.get("entry_price", 0.0) or 0.0)
                if entry_price <= 0 and df is not None and len(df) > 0:
                    entry_price = float(df.iloc[-1].get("close", 0.0) or 0.0)
                
                atr = float(df.iloc[-1].get("atr", 0.0) or 0.0) if df is not None and len(df) > 0 else 0.0
                dxy_info = trend_context.get("dxy_info") or {}
                silver_info = trend_context.get("silver_info") or {}
                
                rejection_record = {
                    "reason": reason,
                    "direction": str(signal.get("direction", "")).upper(),
                    "score": float(signal.get("score", 0.0) or 0.0),
                    "confidence": str(signal.get("confidence", "LOW")).upper(),
                    "setup_tag": str(signal.get("setup_tag", "UNKNOWN_SETUP")),
                    "regime": str(signal.get("regime", "UNKNOWN")).upper(),
                    "session": str(session_policy.get("session_tag", "UNKNOWN")).upper(),
                    "entry_price": entry_price,
                    "spread": float(dxy_info.get("spread", 0.0) or 0.0),
                    "atr": atr,
                }
                state.register_rejected_signal(rejection_record)
        except Exception as e:
            log_event("ERROR", {"message": f"Failed to log rejection to shadow memory: {str(e)}"})
        
        return {
            "allowed": False,
            "reason": str(reason),
            "risk_multiplier_adjustment": 1.0,
            "tp_multiplier_adjustment": 1.0,
            "early_exit_enabled": False,
            "trade_quality": trade_quality,
            "signature": signature,
            "trade_type": "REJECTED",
            "htf_conflict": False,
            "htf_conflict_overridden": False,
        }

    def _downgrade_confidence(current_confidence):
        confidence_value = str(current_confidence or "LOW").upper()
        if confidence_value == "HIGH":
            return "MEDIUM"
        if confidence_value == "MEDIUM":
            return "LOW"
        return "LOW"

    if session_policy.get("skip_signal"):
        reason = str(session_policy.get("skip_reason") or "SKIPPED: session filter")
        return _reject(reason, trade_quality="WEAK")

    raw_score = float(signal.get("score", 0.0) or 0.0)
    confidence = str(signal.get("confidence", "LOW")).upper()
    direction = str(signal.get("direction", "")).upper()
    adjusted_score = raw_score
    raw_correlation_score = float(context.get("correlation_score_adjustment", 0.0) or 0.0)
    correlation_score = max(-1.0, min(1.0, raw_correlation_score))
    regime_recently_changed = bool(context.get("regime_recently_changed", False))

    smc_data = trend_context.get("smc_data") or {}
    state = trend_context.get("state")
    debug_breakdown = signal.get("breakdown", {}) if isinstance(signal.get("breakdown", {}), dict) else {}
    structure_score = float(debug_breakdown.get("structure", 0.0) or 0.0)
    liquidity_score = float(debug_breakdown.get("liquidity", 0.0) or 0.0)
    confirmation_score = float(debug_breakdown.get("confirmation", 0.0) or 0.0)
    momentum_score = float(debug_breakdown.get("momentum", 0.0) or 0.0)
    setup_strength = structure_score + liquidity_score + confirmation_score
    correlation_momentum_pct = float(context.get("correlation_momentum_pct", 0.0) or 0.0)

    # ========================================================================
    # PHASE 2: SOFT MOMENTUM GATES (instead of hard rejection)
    # ========================================================================
    soft_momentum_penalty_pct = 0.0
    momentum_soft_blocked = False
    
    if direction == "BUY" and momentum_score < 1.0:
        momentum_soft_penalty = float(getattr(config, "MOMENTUM_SOFT_PENALTY", 1.0) or 1.0)
        soft_momentum_penalty_pct += momentum_soft_penalty
        log_event("MOMENTUM_SOFT_PENALTY", {
            "direction": "BUY",
            "momentum_score": round(momentum_score, 2),
            "penalty": momentum_soft_penalty,
        })
    if direction == "SELL" and momentum_score > -1.0:
        momentum_soft_penalty = float(getattr(config, "MOMENTUM_SOFT_PENALTY", 1.0) or 1.0)
        soft_momentum_penalty_pct += momentum_soft_penalty
        log_event("MOMENTUM_SOFT_PENALTY", {
            "direction": "SELL",
            "momentum_score": round(momentum_score, 2),
            "penalty": momentum_soft_penalty,
        })

    # ========================================================================
    # PHASE 4: ADJUSTED CORRELATION MOMENTUM GATE (lowered thresholds)
    # ========================================================================
    correlation_momentum_min_threshold = float(getattr(config, "CORRELATION_MOMENTUM_MIN_THRESHOLD", 0.02) or 0.02)
    correlation_momentum_soft_threshold = float(getattr(config, "CORRELATION_MOMENTUM_SOFT_THRESHOLD", 0.015) or 0.015)
    correlation_momentum_hard_floor = float(getattr(config, "CORRELATION_MOMENTUM_HARD_FLOOR", 0.010) or 0.010)
    
    if correlation_momentum_pct < correlation_momentum_hard_floor:
        # Hard rejection: below absolute floor
        return _reject("SKIPPED: correlation momentum critical low", trade_quality="WEAK")
    elif correlation_momentum_pct < correlation_momentum_soft_threshold:
        # Soft penalty: well below threshold but above hard floor
        corr_momentum_soft_penalty = float(getattr(config, "CORRELATION_MOMENTUM_SOFT_PENALTY", 0.75) or 0.75)
        soft_momentum_penalty_pct += corr_momentum_soft_penalty
        log_event("CORRELATION_MOMENTUM_SOFT_PENALTY", {
            "correlation_momentum_pct": round(correlation_momentum_pct, 4),
            "hard_floor": correlation_momentum_hard_floor,
            "soft_threshold": correlation_momentum_soft_threshold,
            "penalty": corr_momentum_soft_penalty,
        })
    elif correlation_momentum_pct < correlation_momentum_min_threshold:
        # Soft penalty: below soft threshold but above hard floor
        corr_momentum_soft_penalty = float(getattr(config, "CORRELATION_MOMENTUM_SOFT_PENALTY", 0.75) or 0.75) * 0.5
        soft_momentum_penalty_pct += corr_momentum_soft_penalty
        log_event("CORRELATION_MOMENTUM_MINOR_PENALTY", {
            "correlation_momentum_pct": round(correlation_momentum_pct, 4),
            "threshold": correlation_momentum_min_threshold,
            "penalty": corr_momentum_soft_penalty,
        })

    structure_state = str(smc_data.get("structure", "")).upper()
    sweep_detected = bool(
        signal.get("sweep_detected")
        or smc_data.get("bull_sweep")
        or smc_data.get("bear_sweep")
    )
    strongly_opposite = (
        (direction == "BUY" and structure_state == "BEARISH")
        or (direction == "SELL" and structure_state == "BULLISH")
    )

    forced_entry = bool(sweep_detected and correlation_score >= 2.0 and not strongly_opposite)
    if forced_entry:
        signal["forced_entry"] = True
        signal["forced_entry_reason"] = "FORCED ENTRY - CORRELATION + SWEEP"

    higher_timeframe_trend = str(signal.get("bias", "")).upper()
    if higher_timeframe_trend not in {"BULLISH", "BEARISH"}:
        df = trend_context.get("df")
        if df is not None and len(df) > 0:
            last = df.iloc[-1]
            ema9 = float(last.get("ema9", 0.0) or 0.0)
            ema50 = float(last.get("ema50", 0.0) or 0.0)
            if ema9 > ema50:
                higher_timeframe_trend = "BULLISH"
            elif ema9 < ema50:
                higher_timeframe_trend = "BEARISH"

    trend_alignment = None
    htf_conflict = False
    htf_conflict_overridden = False
    htf_score_penalty = float(getattr(config, "HTF_CONFLICT_SCORE_PENALTY", 0.7) or 0.7)
    htf_risk_penalty = float(getattr(config, "HTF_CONFLICT_RISK_PENALTY", 0.5) or 0.5)
    htf_tp_penalty = float(getattr(config, "HTF_CONFLICT_TP_PENALTY", 0.7) or 0.7)
    htf_strong_override_threshold = float(getattr(config, "HTF_CONFLICT_STRONG_OVERRIDE_THRESHOLD", 12.0) or 12.0)
    tp_multiplier_adjustment = 1.0
    early_exit_enabled = False

    if higher_timeframe_trend in {"BULLISH", "BEARISH"} and direction in {"BUY", "SELL"}:
        trend_alignment = (
            (higher_timeframe_trend == "BULLISH" and direction == "BUY")
            or (higher_timeframe_trend == "BEARISH" and direction == "SELL")
        )
        htf_conflict = not trend_alignment
        if htf_conflict:
            if abs(adjusted_score) >= htf_strong_override_threshold:
                htf_conflict_overridden = True
            else:
                adjusted_score *= htf_score_penalty
                confidence = _downgrade_confidence(confidence)
                signal["confidence"] = confidence
                tp_multiplier_adjustment *= htf_tp_penalty
                early_exit_enabled = True

    if correlation_score >= 3.0 and direction in {"BUY", "SELL"}:
        floor_score = 5.0 if direction == "BUY" else -5.0
        if direction == "BUY":
            adjusted_score = max(adjusted_score, floor_score)
        else:
            adjusted_score = min(adjusted_score, floor_score)

    if forced_entry and abs(adjusted_score) < float(config.MIN_SIGNAL_SCORE_EXECUTION if mode == "execution" else config.MIN_SIGNAL_SCORE_TELEGRAM):
        adjusted_score = 5.0 if direction != "SELL" else -5.0

    weak_correlation_conflict = raw_correlation_score < 0.0 and abs(raw_correlation_score) <= 1.5
    if setup_strength >= 8.0 and weak_correlation_conflict:
        adjusted_score -= raw_correlation_score

    adjustment_total = 0.0
    adaptive_adjustments = signal.get("adaptive_adjustments", {}) if isinstance(signal.get("adaptive_adjustments", {}), dict) else {}
    for key in ["journal", "correlation", "dxy_correlation", "silver_correlation"]:
        block = adaptive_adjustments.get(key)
        if isinstance(block, dict):
            adjustment_total += float(block.get("score_adjustment", 0.0) or 0.0)

    # ========================================================================
    # PHASE 2: APPLY SOFT MOMENTUM PENALTY TO SCORE
    # ========================================================================
    soft_momentum_adjustment = -soft_momentum_penalty_pct
    if soft_momentum_adjustment != 0.0:
        adjustment_total += soft_momentum_adjustment
        log_event("SOFT_MOMENTUM_PENALTY_APPLIED", {
            "penalty_amount": round(soft_momentum_adjustment, 2),
            "total_adjustment": round(adjustment_total, 2),
        })

    final_score = float(adjusted_score) + float(adjustment_total)
    signal["final_score"] = final_score
    signal["score"] = final_score

    signed_score = float(final_score)
    log_event("DEBUG_DIRECTION", {
        "direction": direction,
        "momentum": momentum_score,
        "correlation_momentum_pct": correlation_momentum_pct,
        "final_score": final_score,
    })

    buy_threshold = 5.5
    sell_threshold = -5.5
    if direction == "BUY" and signed_score < buy_threshold:
        return _reject(f"SKIPPED: weak conviction (BUY < +{buy_threshold:.1f})", trade_quality="WEAK")
    if direction == "SELL" and signed_score > sell_threshold:
        return _reject(f"SKIPPED: weak conviction (SELL > {sell_threshold:.1f})", trade_quality="WEAK")

    # Correlation limiter: correlation cannot be the sole driver over execution threshold.
    base_score_without_correlation = raw_score - raw_correlation_score
    crossed_threshold = (
        (direction == "BUY" and signed_score >= buy_threshold)
        or (direction == "SELL" and signed_score <= sell_threshold)
    )
    if crossed_threshold and abs(base_score_without_correlation) < buy_threshold:
        return _reject("SKIPPED: correlation-only trigger", trade_quality="WEAK")

    # Execution safety rule.
    if direction == "BUY" and signed_score < buy_threshold:
        return _reject(f"SKIPPED: execution safety gate (BUY < +{buy_threshold:.1f})", trade_quality="WEAK")
    if direction == "SELL" and signed_score > sell_threshold:
        return _reject(f"SKIPPED: execution safety gate (SELL > {sell_threshold:.1f})", trade_quality="WEAK")

    adjusted_abs_score = abs(final_score)
    if momentum_score == 0:
        confidence = "LOW"
        signal["confidence"] = confidence
    elif forced_entry and _confidence_rank(confidence) < _confidence_rank("MEDIUM"):
        confidence = "MEDIUM"
        signal["confidence"] = confidence
    elif adjusted_abs_score >= float(getattr(config, "INSTITUTIONAL_HIGH_CONFIDENCE", 10) or 10):
        confidence = "HIGH"
        signal["confidence"] = confidence
    elif adjusted_abs_score >= float(getattr(config, "INSTITUTIONAL_MEDIUM_CONFIDENCE", 6) or 6):
        confidence = "MEDIUM"
        signal["confidence"] = confidence

    score = adjusted_abs_score

    min_score = float(config.MIN_SIGNAL_SCORE_EXECUTION if mode == "execution" else config.MIN_SIGNAL_SCORE_TELEGRAM)

    risk_multiplier_adjustment = 1.0
    if htf_conflict and not htf_conflict_overridden:
        risk_multiplier_adjustment *= htf_risk_penalty
    if correlation_score >= 2.0:
        risk_multiplier_adjustment *= 1.2
    elif correlation_score <= -2.0:
        risk_multiplier_adjustment *= 0.7

    # ========================================================================
    # PHASE 2: APPLY SOFT MOMENTUM PENALTY TO RISK MULTIPLIER
    # ========================================================================
    if soft_momentum_penalty_pct > 0.0:
        momentum_risk_reduction = float(getattr(config, "MOMENTUM_SOFT_RISK_MULTIPLIER", 0.8) or 0.8)
        risk_multiplier_adjustment *= momentum_risk_reduction
        log_event("SOFT_MOMENTUM_RISK_REDUCTION", {
            "penalty_amount": round(soft_momentum_penalty_pct, 2),
            "risk_multiplier": round(momentum_risk_reduction, 3),
            "adjusted_risk": round(risk_multiplier_adjustment, 3),
        })

    setup_winrate = None
    setup_trades = 0
    if state is not None and hasattr(state, "get_trade_journal"):
        try:
            journal = state.get_trade_journal() or {}
            setup_stats = (journal.get("by_setup") or {}).get(str(signal.get("setup_tag") or ""), {})
            if setup_stats:
                setup_trades = int(setup_stats.get("trades", 0) or 0)
                setup_winrate = float(setup_stats.get("win_rate", 0.0) or 0.0) / 100.0
        except Exception:
            setup_winrate = None

    early_entry = False
    setup_winrate_override = False
    learning_softened = False
    trade_type = "NORMAL"
    if score >= 10:
        trade_type = "HIGH CONFIDENCE"
        risk_multiplier_adjustment *= 1.0
    elif score >= 8:
        trade_type = "MODERATE CONFIDENCE"
        risk_multiplier_adjustment *= 0.7
    elif score >= 6 and structure_score >= 2 and liquidity_score >= 2:
        trade_type = "EARLY ENTRY"
        risk_multiplier_adjustment *= 0.5
        early_entry = True
    elif not forced_entry:
        return _reject("SKIPPED: low score", trade_quality="WEAK")

    min_setup_winrate = float(getattr(config, "SETUP_WINRATE_REJECTION_THRESHOLD", 0.30) or 0.30)
    min_setup_trades = int(getattr(config, "SETUP_WINRATE_MIN_TRADES", 8) or 8)
    strong_override_score = float(getattr(config, "SETUP_WINRATE_STRONG_SCORE_OVERRIDE", 16.0) or 16.0)
    strong_override_momentum = float(getattr(config, "SETUP_WINRATE_STRONG_MOMENTUM_OVERRIDE", 2.5) or 2.5)
    strong_override_corr_momentum = float(getattr(config, "SETUP_WINRATE_OVERRIDE_MIN_CORRELATION_MOMENTUM", 0.25) or 0.25)

    if (
        setup_winrate is not None
        and setup_trades >= min_setup_trades
        and setup_winrate < min_setup_winrate
    ):
        strong_setup = (
            score >= strong_override_score
            and momentum_score >= strong_override_momentum
            and correlation_momentum_pct >= strong_override_corr_momentum
        )
        if strong_setup:
            setup_winrate_override = True
            risk_multiplier_adjustment *= float(getattr(config, "SETUP_WINRATE_OVERRIDE_RISK_MULTIPLIER", 0.75) or 0.75)
            log_event("SETUP_WINRATE_OVERRIDE", {
                "setup_tag": str(signal.get("setup_tag", "UNKNOWN")),
                "setup_winrate": round(setup_winrate, 4),
                "setup_trades": setup_trades,
                "score": round(score, 2),
                "momentum": round(momentum_score, 2),
                "correlation_momentum_pct": round(correlation_momentum_pct, 4),
            })
        else:
            learning_softened = True
            score_penalty = float(getattr(config, "SETUP_WINRATE_SOFT_SCORE_PENALTY", 2.0) or 2.0)
            score = max(score - score_penalty, 0.0)
            confidence = str(getattr(config, "SETUP_WINRATE_SOFT_CONFIDENCE", "MEDIUM") or "MEDIUM").upper()
            signal["confidence"] = confidence
            risk_multiplier_adjustment *= float(getattr(config, "SETUP_WINRATE_SOFT_RISK_MULTIPLIER", 0.5) or 0.5)
            log_event("SETUP_WINRATE_PENALTY", {
                "setup_tag": str(signal.get("setup_tag", "UNKNOWN")),
                "setup_winrate": round(setup_winrate, 4),
                "setup_trades": setup_trades,
                "score_penalty": round(score_penalty, 2),
                "score_after_penalty": round(score, 2),
                "confidence": confidence,
                "correlation_momentum_pct": round(correlation_momentum_pct, 4),
            })

    if setup_winrate_override:
        trade_type = f"{trade_type} + WINRATE_OVERRIDE"

    if regime_recently_changed:
        learning_softened = True
        confidence = str(getattr(config, "REGIME_RECENT_CHANGE_CONFIDENCE", "MEDIUM") or "MEDIUM").upper()
        signal["confidence"] = confidence
        risk_multiplier_adjustment *= float(getattr(config, "REGIME_RECENT_CHANGE_RISK_MULTIPLIER", 0.75) or 0.75)
        log_event("REGIME_RECENT_CHANGE_PENALTY", {
            "regime_recently_changed": True,
            "confidence": confidence,
            "risk_multiplier_adjustment": round(risk_multiplier_adjustment, 4),
        })

    min_conf = str(config.MIN_CONFIDENCE_EXECUTION if mode == "execution" else config.MIN_CONFIDENCE_TELEGRAM).upper()
    
    # ========================================================================
    # PHASE 5: LOW CONFIDENCE SOFT MODE (instead of hard rejection)
    # ========================================================================
    low_confidence_applied = False
    if _confidence_rank(confidence) < _confidence_rank(min_conf) and not forced_entry and not early_entry and not learning_softened:
        # Allow LOW confidence trades if soft mode enabled
        if bool(getattr(config, "ENABLE_LOW_CONFIDENCE_MODE", True)) and str(confidence).upper() == "LOW":
            low_confidence_applied = True
            low_conf_score_penalty = float(getattr(config, "LOW_CONFIDENCE_SCORE_PENALTY", 1.0) or 1.0)
            # Recalculate final score with low confidence penalty
            final_score = float(adjusted_score) + float(adjustment_total) - low_conf_score_penalty
            signal["final_score"] = final_score
            signal["score"] = final_score
            risk_multiplier_adjustment *= float(getattr(config, "LOW_CONFIDENCE_RISK_MULTIPLIER", 0.5) or 0.5)
            log_event("LOW_CONFIDENCE_SOFT_MODE", {
                "confidence": confidence,
                "score_before_penalty": round(float(adjusted_score) + float(adjustment_total), 2),
                "score_after_penalty": round(final_score, 2),
                "score_penalty": round(low_conf_score_penalty, 2),
                "risk_multiplier": round(risk_multiplier_adjustment, 3),
            })
            signed_score = float(final_score)
        else:
            return _reject("SKIPPED: low confidence", trade_quality="WEAK")

    symbol = str(context.get("symbol", config.SYMBOL))
    signature = _build_signal_signature(signal, context)

    if _is_duplicate_signal(symbol, signature, now_ts):
        return _reject("SKIPPED: duplicate signal", trade_quality="WEAK", signature=signature)

    protection = evaluate_trade_protections(signal, {
        "df": trend_context.get("df"),
        "smc_data": trend_context.get("smc_data"),
        "state": trend_context.get("state"),
        "dxy_info": trend_context.get("dxy_info"),
        "silver_info": trend_context.get("silver_info"),
        "correlation_score_adjustment": correlation_score,
        "now_ts": now_ts,
    })

    momentum_override = False

    if not protection.get("allowed", True):
        if _is_momentum_override_candidate(signal, context, protection=protection):
            momentum_override = True
            trade_type = "MOMENTUM_OVERRIDE"
            risk_multiplier_adjustment *= float(getattr(config, "MOMENTUM_OVERRIDE_RISK_MULTIPLIER", 0.7) or 0.7)
        else:
            rejected = _reject(protection.get("reason", "SKIPPED: trade protection"), trade_quality=protection.get("trade_quality", "WEAK"), signature=signature)
            rejected.update({
                "trade_stage": protection.get("trade_stage", "LATE TREND"),
                "labels": protection.get("labels", []),
                "execution_block_reason": protection.get("reason"),
                "trend_fatigue_count": protection.get("trend_fatigue_count", 0),
                "overextended": protection.get("overextended", False),
                "momentum_weakening": protection.get("momentum_weakening", False),
                "tp_multiplier_adjustment": tp_multiplier_adjustment,
                "early_exit_enabled": early_exit_enabled,
                "htf_conflict": htf_conflict,
                "htf_conflict_overridden": htf_conflict_overridden,
            })
            return rejected

    risk_multiplier_adjustment *= float(protection.get("risk_multiplier_adjustment", 1.0) or 1.0)

    moderate_min = float(getattr(config, "MODERATE_TRADE_MIN_SCORE", 8.0) or 8.0)
    moderate_max = float(getattr(config, "MODERATE_TRADE_MAX_SCORE", 10.0) or 10.0)
    if (not momentum_override) and (moderate_min <= score <= moderate_max) and not early_entry:
        trade_type = "MODERATE"
        risk_multiplier_adjustment *= float(getattr(config, "MODERATE_TRADE_RISK_MULTIPLIER", 0.7) or 0.7)

    if forced_entry:
        trade_type = "FORCED_ENTRY"

    trade_quality = "STRONG"
    if score <= 10.0 or correlation_score < 0:
        trade_quality = "MODERATE"
    if score < min_score + 0.5 or correlation_score <= -2.0:
        trade_quality = "WEAK"

    if protection.get("trade_quality") == "MODERATE":
        trade_quality = "MODERATE"
    elif protection.get("trade_quality") == "WEAK":
        trade_quality = "WEAK"

    labels = list(protection.get("labels", []))
    if htf_conflict and htf_conflict_overridden:
        labels.append("HTF_CONFLICT_OVERRIDE")
    elif htf_conflict:
        labels.append("HTF_CONFLICT_SOFT")
    if momentum_override:
        labels.append("MOMENTUM_OVERRIDE")
    if forced_entry:
        labels.append("FORCED ENTRY - CORRELATION + SWEEP")
    if early_entry:
        labels.append("EARLY_ENTRY")
    if early_exit_enabled:
        labels.append("EARLY_EXIT_ENABLED")

    return {
        "allowed": True,
        "reason": "OK",
        "risk_multiplier_adjustment": risk_multiplier_adjustment,
        "tp_multiplier_adjustment": tp_multiplier_adjustment,
        "trade_quality": trade_quality,
        "signature": signature,
        "trade_stage": protection.get("trade_stage", "MID TREND"),
        "labels": labels,
        "trade_type": trade_type,
        "trend_fatigue_count": protection.get("trend_fatigue_count", 0),
        "overextended": protection.get("overextended", False),
        "momentum_weakening": protection.get("momentum_weakening", False),
        "execution_block_reason": protection.get("reason") if momentum_override else None,
        "forced_entry": forced_entry,
        "forced_entry_reason": signal.get("forced_entry_reason") if forced_entry else None,
        "early_entry": early_entry,
        "early_exit_enabled": early_exit_enabled,
        "htf_conflict": htf_conflict,
        "htf_conflict_overridden": htf_conflict_overridden,
    }


def emit_decision_validation_debug(iteration, mode, signal, decision_validation):
    """Emit compact runtime diagnostics for decision validation outputs."""
    if not bool(getattr(config, "ENABLE_DECISION_VALIDATION_DEBUG", True)):
        return

    if not isinstance(decision_validation, dict):
        return

    payload = {
        "mode": str(mode or "unknown"),
        "allowed": bool(decision_validation.get("allowed", False)),
        "reason": str(decision_validation.get("reason", "N/A")),
        "direction": str((signal or {}).get("direction", "")),
        "score": float((signal or {}).get("score", 0.0) or 0.0),
        "confidence": str((signal or {}).get("confidence", "N/A")),
        "risk_multiplier_adjustment": float(decision_validation.get("risk_multiplier_adjustment", 1.0) or 1.0),
        "tp_multiplier_adjustment": float(decision_validation.get("tp_multiplier_adjustment", 1.0) or 1.0),
        "early_exit_enabled": bool(decision_validation.get("early_exit_enabled", False)),
        "htf_conflict": bool(decision_validation.get("htf_conflict", False)),
        "htf_conflict_overridden": bool(decision_validation.get("htf_conflict_overridden", False)),
        "trade_type": str(decision_validation.get("trade_type", "N/A")),
    }
    log_event("DECISION_VALIDATION", payload)
    print(
        f"🧪 [{iteration}] Decision[{payload['mode']}] allowed={payload['allowed']} "
        f"risk=x{payload['risk_multiplier_adjustment']:.2f} tp=x{payload['tp_multiplier_adjustment']:.2f} "
        f"early_exit={payload['early_exit_enabled']} htf_conflict={payload['htf_conflict']} "
        f"override={payload['htf_conflict_overridden']} type={payload['trade_type']}"
    )


def build_setup_tag(signal, smc_data=None):
    smc_data = smc_data or {}
    parts = [
        str(signal.get("direction", "NONE")).upper(),
        str(signal.get("confidence", "UNKNOWN")).upper(),
        "SWEEP" if smc_data.get("bull_sweep") or smc_data.get("bear_sweep") else "NO_SWEEP",
        "BOS" if smc_data.get("bos") else "NO_BOS",
        "CHOCH" if smc_data.get("choch") else "NO_CHOCH",
    ]
    return "|".join(parts)


def apply_adaptive_journal_weight(signal, state, setup_tag, session_tag, regime_tag):
    adjustment, reasons = state.get_adaptive_score_adjustment(setup_tag, session_tag=session_tag, regime_tag=regime_tag)
    if adjustment == 0:
        return signal

    signal.setdefault("adaptive_adjustments", {})
    signal["adaptive_adjustments"]["journal"] = {
        "score_adjustment": adjustment,
        "reasons": reasons,
    }

    return signal


def compute_correlation_score(gold_direction, dxy_trend=None, yields_trend=None, silver_trend=None, retail_long_ratio=None):
    """Soft correlation score with neutral fallback when data is unavailable."""
    if not config.ENABLE_CORRELATION_FRAMEWORK:
        return 0, []

    score = 0
    notes = []

    direction = str(gold_direction or "").upper()
    dxy = str(dxy_trend or "UNKNOWN").lower()
    if dxy in ["bearish", "bullish"]:
        if direction == "SELL":
            if dxy == "bullish":
                score += 1
                notes.append("DXY bullish supports SELL (+1)")
            else:
                score -= 1
                notes.append("DXY bearish conflicts with SELL (-1)")
        elif direction == "BUY":
            if dxy == "bearish":
                score += 1
                notes.append("DXY bearish supports BUY (+1)")
            else:
                score -= 1
                notes.append("DXY bullish conflicts with BUY (-1)")
        elif dxy == "bearish":
            score += 1
            notes.append("DXY bearish (+1)")
        else:
            score -= 1
            notes.append("DXY bullish (-1)")

    if bool(getattr(config, "ENABLE_YIELDS_CORRELATION", False)):
        yields = str(yields_trend or "UNKNOWN").lower()
        if yields in ["falling", "rising"]:
            if yields == "falling":
                score += 1
                notes.append("Yields falling (+1)")
            else:
                score -= 1
                notes.append("Yields rising (-1)")

    silver = str(silver_trend or "UNKNOWN").upper()
    if silver == "BULLISH":
        silver = "BUY"
    elif silver == "BEARISH":
        silver = "SELL"
    if silver in ["BUY", "SELL"] and gold_direction in ["BUY", "SELL"]:
        if silver == gold_direction:
            score += 1
            notes.append("Silver confirms direction (+1)")
        else:
            score -= 1
            notes.append("Silver diverges from gold (-1)")

    ratio = float(retail_long_ratio or 0)
    if ratio > 0 and gold_direction in ["BUY", "SELL"]:
        if ratio >= config.RETAIL_LONG_RATIO_THRESHOLD and gold_direction == "BUY":
            score -= 2
            notes.append("Retail longs crowded against BUY (-2)")
        elif ratio <= (100 - config.RETAIL_LONG_RATIO_THRESHOLD) and gold_direction == "SELL":
            score -= 2
            notes.append("Retail shorts crowded against SELL (-2)")

    score = max(-1.0, min(1.0, float(score)))
    return score, notes


def apply_correlation_weight(signal, market_context):
    correlation_score, notes = compute_correlation_score(
        gold_direction=signal.get("direction"),
        dxy_trend=market_context.get("dxy_trend"),
        yields_trend=market_context.get("yields_trend"),
        silver_trend=market_context.get("silver_trend"),
        retail_long_ratio=market_context.get("retail_long_ratio"),
    )

    if correlation_score == 0:
        return signal

    correlation_score = max(-1.0, min(1.0, float(correlation_score)))

    signal.setdefault("adaptive_adjustments", {})
    signal["adaptive_adjustments"]["correlation"] = {
        "score_adjustment": correlation_score,
        "notes": notes,
    }

    return signal


def get_dxy_trend(timeframe=None):
    """Fetch DXY (UsDollar) via MT5 and derive EMA trend. Returns bullish/bearish/neutral."""
    if not config.ENABLE_DXY_CORRELATION:
        return {"trend": "neutral", "reason": "disabled"}

    symbol = str(config.DXY_SYMBOL)
    tf = timeframe or config.TIMEFRAME

    try:
        info = mt5.symbol_info(symbol)
        if info is None:
            return {"trend": "neutral", "reason": f"symbol_info missing for {symbol}"}

        if not info.visible and not mt5.symbol_select(symbol, True):
            return {"trend": "neutral", "reason": f"symbol_select failed for {symbol}"}

        lookback = max(int(config.DXY_LOOKBACK_CANDLES), int(config.DXY_EMA_SLOW) + 5)
        rates = get_market_data(symbol=symbol, timeframe=tf, candles=lookback)
        if rates is None or len(rates) < int(config.DXY_EMA_SLOW):
            return {"trend": "neutral", "reason": "insufficient rates"}

        closes = pd.Series([float(r["close"]) for r in rates])
        ema_fast = closes.ewm(span=int(config.DXY_EMA_FAST), adjust=False).mean().iloc[-1]
        ema_slow = closes.ewm(span=int(config.DXY_EMA_SLOW), adjust=False).mean().iloc[-1]

        if ema_fast > ema_slow:
            trend = "bullish"
        elif ema_fast < ema_slow:
            trend = "bearish"
        else:
            trend = "neutral"

        return {
            "trend": trend,
            "ema_fast": float(ema_fast),
            "ema_slow": float(ema_slow),
            "symbol": symbol,
        }
    except Exception as exc:
        return {"trend": "neutral", "reason": f"exception: {str(exc)}"}


def get_silver_trend(timeframe=None):
    """Fetch XAGUSD via MT5 and derive EMA trend + momentum strength."""
    if not config.ENABLE_SILVER_CORRELATION:
        return {
            "trend": "neutral",
            "momentum_pct": 0.0,
            "momentum_state": "weak",
            "reason": "disabled"
        }

    symbol = str(config.SILVER_SYMBOL)
    tf = timeframe or config.TIMEFRAME

    try:
        info = mt5.symbol_info(symbol)
        if info is None:
            return {
                "trend": "neutral",
                "momentum_pct": 0.0,
                "momentum_state": "weak",
                "reason": f"symbol_info missing for {symbol}"
            }

        if not info.visible and not mt5.symbol_select(symbol, True):
            return {
                "trend": "neutral",
                "momentum_pct": 0.0,
                "momentum_state": "weak",
                "reason": f"symbol_select failed for {symbol}"
            }

        lookback = max(int(config.SILVER_LOOKBACK_CANDLES), int(config.SILVER_EMA_SLOW) + 5)
        rates = get_market_data(symbol=symbol, timeframe=tf, candles=lookback)
        if rates is None or len(rates) < int(config.SILVER_EMA_SLOW):
            return {
                "trend": "neutral",
                "momentum_pct": 0.0,
                "momentum_state": "weak",
                "reason": "insufficient rates"
            }

        closes = pd.Series([float(r["close"]) for r in rates])
        ema_fast = closes.ewm(span=int(config.SILVER_EMA_FAST), adjust=False).mean().iloc[-1]
        ema_slow = closes.ewm(span=int(config.SILVER_EMA_SLOW), adjust=False).mean().iloc[-1]
        last_close = float(closes.iloc[-1])

        if ema_fast > ema_slow:
            trend = "bullish"
        elif ema_fast < ema_slow:
            trend = "bearish"
        else:
            trend = "neutral"

        momentum_pct = 0.0
        if last_close > 0:
            momentum_pct = abs((float(ema_fast) - float(ema_slow)) / last_close) * 100.0

        momentum_state = "strong" if momentum_pct >= float(config.SILVER_MOMENTUM_THRESHOLD_PCT) else "weak"

        return {
            "trend": trend,
            "momentum_pct": float(momentum_pct),
            "momentum_state": momentum_state,
            "ema_fast": float(ema_fast),
            "ema_slow": float(ema_slow),
            "symbol": symbol,
        }
    except Exception as exc:
        return {
            "trend": "neutral",
            "momentum_pct": 0.0,
            "momentum_state": "weak",
            "reason": f"exception: {str(exc)}"
        }


def apply_dxy_correlation(signal, dxy_trend):
    """Apply direction-aware gold-vs-DXY soft weighting to signal score."""
    if not config.ENABLE_DXY_CORRELATION:
        return signal

    adjustment = 0.0
    trend = str(dxy_trend or "neutral").lower()
    weight = float(config.DXY_WEIGHT)
    direction = str(signal.get("direction") or "").upper()

    if direction == "BUY":
        if trend == "bullish":
            adjustment = -weight
        elif trend == "bearish":
            adjustment = weight
    elif direction == "SELL":
        if trend == "bullish":
            adjustment = weight
        elif trend == "bearish":
            adjustment = -weight

    if adjustment == 0.0:
        return signal

    adjustment *= float(getattr(config, "CORRELATION_WEIGHT", 1.0) or 1.0)
    adjustment = max(-1.0, min(1.0, adjustment))

    signal.setdefault("adaptive_adjustments", {})
    signal["adaptive_adjustments"]["dxy_correlation"] = {
        "direction": direction,
        "trend": trend,
        "score_adjustment": adjustment,
    }

    log_event("CORRELATION", {
        "asset": "DXY",
        "direction": direction,
        "trend": trend,
        "score_adjustment": adjustment,
    })
    return signal


def apply_silver_correlation(signal, silver_info):
    """Apply direction-aware XAGUSD correlation with optional momentum bonus."""
    if not config.ENABLE_SILVER_CORRELATION:
        return signal

    info = silver_info if isinstance(silver_info, dict) else {}
    trend = str(info.get("trend", "neutral")).lower()
    direction = str(signal.get("direction") or "").upper()
    weight = float(config.SILVER_WEIGHT)
    momentum_pct = float(info.get("momentum_pct", 0.0) or 0.0)
    momentum_threshold = float(config.SILVER_MOMENTUM_THRESHOLD_PCT)
    momentum_bonus = float(config.SILVER_MOMENTUM_BONUS)

    adjustment = 0.0
    aligned = False

    if direction == "BUY":
        if trend == "bullish":
            adjustment = weight
            aligned = True
        elif trend == "bearish":
            adjustment = -weight
    elif direction == "SELL":
        if trend == "bearish":
            adjustment = weight
            aligned = True
        elif trend == "bullish":
            adjustment = -weight

    if aligned and momentum_pct >= momentum_threshold:
        adjustment += momentum_bonus

    if adjustment == 0.0:
        return signal

    adjustment *= float(getattr(config, "CORRELATION_WEIGHT", 1.0) or 1.0)
    adjustment = max(-1.0, min(1.0, adjustment))

    signal.setdefault("adaptive_adjustments", {})
    signal["adaptive_adjustments"]["silver_correlation"] = {
        "direction": direction,
        "trend": trend,
        "momentum_pct": momentum_pct,
        "score_adjustment": adjustment,
    }

    log_event("CORRELATION", {
        "asset": "SILVER",
        "symbol": str(info.get("symbol", config.SILVER_SYMBOL)),
        "direction": direction,
        "trend": trend,
        "momentum_pct": momentum_pct,
        "score_adjustment": adjustment,
    })
    return signal


def describe_dxy_alignment(signal_direction, dxy_trend):
    """Return a human-readable DXY alignment label for alerts."""
    direction = str(signal_direction or "").upper()
    trend = str(dxy_trend or "neutral").lower()

    if direction == "BUY":
        if trend == "bearish":
            return "DXY bearish - supports BUY"
        if trend == "bullish":
            return "DXY bullish - conflicts with BUY"
    elif direction == "SELL":
        if trend == "bullish":
            return "DXY bullish - supports SELL"
        if trend == "bearish":
            return "DXY bearish - conflicts with SELL"

    return f"DXY {trend} - neutral"


def describe_silver_alignment(signal_direction, silver_trend, silver_momentum_state=None):
    """Return a human-readable XAGUSD alignment label for alerts."""
    direction = str(signal_direction or "").upper()
    trend = str(silver_trend or "neutral").lower()
    momentum_state = str(silver_momentum_state or "").lower()
    momentum_note = f" ({momentum_state})" if momentum_state in ["strong", "weak"] else ""

    if direction == "BUY":
        if trend == "bullish":
            return f"XAG bullish - supports BUY{momentum_note}"
        if trend == "bearish":
            return f"XAG bearish - conflicts with BUY{momentum_note}"
    elif direction == "SELL":
        if trend == "bearish":
            return f"XAG bearish - supports SELL{momentum_note}"
        if trend == "bullish":
            return f"XAG bullish - conflicts with SELL{momentum_note}"

    return f"XAG {trend} - neutral{momentum_note}"


def get_correlation_score_adjustment(signal):
    """Return total score impact from active correlation adjustments."""
    adjustments = signal.get("adaptive_adjustments", {}) if isinstance(signal, dict) else {}
    total = 0.0

    for key in ["dxy_correlation", "silver_correlation", "correlation"]:
        block = adjustments.get(key)
        if isinstance(block, dict):
            total += float(block.get("score_adjustment", 0.0) or 0.0)

    return float(total)


def get_dynamic_risk_multiplier(signal, daily_stats=None, session_risk_multiplier=1.0, correlation_score_adjustment=0.0):
    """Unified dynamic risk model used for lot sizing and suggested risk in signals."""
    if not config.ENABLE_DYNAMIC_RISK_MODEL:
        return 1.0, ["Dynamic risk disabled"]

    reasons = []
    confidence = str(signal.get("confidence", "LOW")).upper()

    if confidence == "HIGH":
        multiplier = float(config.RISK_MULTIPLIER_HIGH_CONFIDENCE)
    elif confidence == "MEDIUM":
        multiplier = float(config.RISK_MULTIPLIER_MEDIUM_CONFIDENCE)
    else:
        multiplier = float(config.RISK_MULTIPLIER_LOW_CONFIDENCE)

    reasons.append(f"Confidence={confidence} -> x{multiplier:.2f}")

    stats = daily_stats or {}
    drawdown = abs(float(stats.get("max_drawdown", 0.0) or 0.0))
    if drawdown >= float(config.DRAWDOWN_RISK_REDUCTION_THRESHOLD):
        multiplier *= float(config.DRAWDOWN_RISK_REDUCTION_MULTIPLIER)
        reasons.append(
            f"Drawdown {drawdown:.2f}% >= {config.DRAWDOWN_RISK_REDUCTION_THRESHOLD:.2f}% -> "
            f"x{config.DRAWDOWN_RISK_REDUCTION_MULTIPLIER:.2f}"
        )

    multiplier *= float(session_risk_multiplier)
    if session_risk_multiplier != 1.0:
        reasons.append(f"Session multiplier x{session_risk_multiplier:.2f}")

    correlation_score_adjustment = float(correlation_score_adjustment or 0.0)
    if abs(correlation_score_adjustment) >= 2.0:
        reasons.append("Correlation adjustment applied in decision validation")

    multiplier = max(float(config.MIN_RISK_MULTIPLIER), min(float(config.MAX_RISK_MULTIPLIER), multiplier))
    return multiplier, reasons


def print_startup_summary():
    """Print a compact dashboard-style startup summary."""

    print("\n┌──────────────────────────────┐")
    print("│    BOT STARTUP SUMMARY       │")
    print("├──────────────────────────────┤")
    print(f"│ Symbol      : {config.SYMBOL:<14} │")
    print(f"│ Timeframe   : {config.TIMEFRAME:<14} │")
    print(f"│ Strategy    : {config.STRATEGY_TYPE:<14} │")
    print(f"│ Institutional: {str(config.USE_INSTITUTIONAL_STRATEGY):<14} │")
    print("└──────────────────────────────┘\n")


def print_claude_session_summary():
    """Print Claude API usage for the current bot session."""

    usage = get_session_usage_summary()

    print("\n┌──────────────────────────────┐")
    print("│   CLAUDE SESSION SUMMARY     │")
    print("├──────────────────────────────┤")

    if not usage.get("enabled"):
        print("│ Claude      : DISABLED       │")
        print("└──────────────────────────────┘")
        return

    if not usage.get("api_key_configured"):
        print("│ Claude      : NO API KEY     │")
        print("└──────────────────────────────┘")
        return

    print(f"│ API Calls   : {usage.get('total_api_calls', 0):<14} │")
    print(f"│ Success     : {usage.get('successful_calls', 0):<14} │")
    print(f"│ Failed      : {usage.get('failed_calls', 0):<14} │")
    print(f"│ Warmup      : {usage.get('warmup_calls', 0):<14} │")
    print(f"│ In Tokens   : {usage.get('input_tokens', 0):<14} │")
    print(f"│ Out Tokens  : {usage.get('output_tokens', 0):<14} │")
    print(f"│ Total Tokens: {usage.get('total_tokens', 0):<14} │")


def format_execution_trade_message(
    signal,
    claude_context,
    regime_data,
    session_policy,
    dxy_info,
    silver_info,
    correlation_score,
    enter_reason,
    decision_validation,
    entry_price,
    executed_price,
    sl,
    tp,
    lot,
    spread,
    risk_multiplier,
    risk_notes,
    ticket,
    trade_type="NORMAL",
    trade_stage="MID TREND",
    protection_labels=None,
    trade_strategy="INSTITUTIONAL",
):
    symbol_label = "GOLD" if str(config.SYMBOL).upper() == "XAUUSD" else config.SYMBOL
    dxy_trend = dxy_info.get("trend", "neutral") if isinstance(dxy_info, dict) else "neutral"
    silver_trend = silver_info.get("trend", "neutral") if isinstance(silver_info, dict) else "neutral"
    silver_momentum = silver_info.get("momentum_state", "weak") if isinstance(silver_info, dict) else "weak"
    silver_momentum_pct = float(silver_info.get("momentum_pct", 0.0) or 0.0) if isinstance(silver_info, dict) else 0.0
    dxy_alignment = describe_dxy_alignment(signal.get("direction"), dxy_trend)
    silver_alignment = describe_silver_alignment(signal.get("direction"), silver_trend, silver_momentum)

    reasons = signal.get("reasons")
    reasons_text = ", ".join(str(r) for r in reasons) if isinstance(reasons, list) and reasons else str(reasons or "N/A")
    breakdown = signal.get("breakdown", {})
    breakdown_text = ", ".join(f"{k}: {v:+}" for k, v in breakdown.items()) if isinstance(breakdown, dict) and breakdown else "N/A"
    validation_text = signal.get("claude_validation") or claude_context.get("observation") or "No Claude validation returned"
    observations = claude_context.get("observations") if isinstance(claude_context, dict) else []
    observation_text = " ".join(observations).strip() if isinstance(observations, list) else ""
    if not observation_text:
        observation_text = "No extra Claude observation returned"

    risk_note_text = "; ".join(str(note) for note in risk_notes) if risk_notes else "N/A"
    protection_text = ", ".join(str(item) for item in (protection_labels or [])) if protection_labels else "NONE"
    slippage = float(executed_price or 0.0) - float(entry_price or 0.0)
    quality = decision_validation.get("trade_quality", "UNKNOWN") if isinstance(decision_validation, dict) else "UNKNOWN"
    session_tag = str(session_policy.get("session_tag", "UNKNOWN")).replace("_", " ") if isinstance(session_policy, dict) else "UNKNOWN"

    message = f"""
✅ EXECUTED {escape_telegram_html(symbol_label)} TRADE

📈 Direction: {escape_telegram_html(signal.get('direction'))}
🎫 Ticket: {escape_telegram_html(ticket)}
📍 Signal Entry: {entry_price:.2f}
⚡ Filled Price: {executed_price:.2f}
📏 Slippage: {slippage:+.2f}
🧱 Lot Size: {lot:.2f}
🛑 Stop Loss: {sl:.2f}
🎯 Take Profit: {tp:.2f}

🧠 WHY THIS TRADE WAS TAKEN
• Entry Decision: {escape_telegram_html(enter_reason)}
• Confidence: {escape_telegram_html(signal.get('confidence', 'N/A'))}
• Score: {float(signal.get('score', 0) or 0):.1f}
• Trade Type: {escape_telegram_html(str(trade_type or 'NORMAL'))}
• Trade Quality: {escape_telegram_html(quality)}
• Setup Tag: {escape_telegram_html(signal.get('setup_tag', 'N/A'))}
• Trade Strategy: {escape_telegram_html(str(trade_strategy or 'INSTITUTIONAL'))}
• Regime: {escape_telegram_html(regime_data.get('regime', 'UNKNOWN'))}
• Session: {escape_telegram_html(session_tag)}
• Trade Stage: {escape_telegram_html(trade_stage)}
• Protection Labels: {escape_telegram_html(protection_text)}
• Reasons: {escape_telegram_html(reasons_text)}
• Score Breakdown: {escape_telegram_html(breakdown_text)}

🌐 CORRELATION SNAPSHOT
• DXY: {escape_telegram_html(dxy_alignment)}
• Silver: {escape_telegram_html(silver_alignment)}
• Silver Momentum: {silver_momentum_pct:.2f}% ({escape_telegram_html(silver_momentum)})
• Yields Trend: {escape_telegram_html(str(getattr(config, 'CORRELATION_YIELDS_TREND', 'UNKNOWN')) if bool(getattr(config, 'ENABLE_YIELDS_CONTEXT', False)) else 'DISABLED')}
• Correlation Score: {float(correlation_score or 0.0):+.2f}

⚖️ RISK & EXECUTION
• Risk Multiplier: x{float(risk_multiplier or 0.0):.2f}
• Risk Notes: {escape_telegram_html(risk_note_text)}
• Spread: {escape_telegram_html(spread)}

🧪 CLAUDE VALIDATION
• Validation: {escape_telegram_html(validation_text)}
• Observation: {escape_telegram_html(observation_text)}
""".strip()

    return message


def send_execution_trade_telegram_alert(**kwargs):
    if not getattr(config, "TELEGRAM_EXECUTION_ALERTS_ENABLED", True):
        return False
    if not telegram_is_configured():
        return False

    message = format_execution_trade_message(**kwargs)
    return send_telegram_signal(message)
    print(f"│ Est Cost USD: {usage.get('estimated_cost_usd', 0):<14.6f} │")
    print(f"│ Budget Tier : {usage.get('budget_tier', 'GREEN'):<14} │")

    if "token_budget" in usage:
        print(f"│ Budget      : {usage.get('token_budget', 0):<14} │")
        print(f"│ Remaining   : {usage.get('tokens_remaining', 0):<14} │")

    print("└──────────────────────────────┘")

    by_feature = usage.get("by_feature", {})
    if by_feature:
        print("\nFeature Spend:")
        for feature, stats in by_feature.items():
            print(
                f"  - {feature}: calls={stats.get('calls', 0)}, "
                f"tokens={stats.get('total_tokens', 0)}, "
                f"cost=${stats.get('estimated_cost_usd', 0.0):.6f}"
            )

    by_day = usage.get("by_day", {})
    if by_day:
        print("\nDaily Spend:")
        for day, stats in sorted(by_day.items()):
            print(
                f"  - {day}: calls={stats.get('calls', 0)}, "
                f"tokens={stats.get('total_tokens', 0)}, "
                f"cost=${stats.get('estimated_cost_usd', 0.0):.6f}"
            )


def main():
    """Main trading bot loop - Institutional Grade"""
    
    print("=" * 60)
    print("🚀 INSTITUTIONAL TRADING BOT v2.0")
    print("=" * 60)
    print(f"Symbol: {config.SYMBOL}")
    print(f"Timeframe: {config.TIMEFRAME}")
    print(f"Strategy: {config.STRATEGY_TYPE}")
    print(f"Institutional Mode: {config.USE_INSTITUTIONAL_STRATEGY}")
    print("=" * 60)
    print_startup_summary()
    
    log_event("BOT_START", {
        "symbol": config.SYMBOL,
        "strategy": config.STRATEGY_TYPE,
        "institutional": config.USE_INSTITUTIONAL_STRATEGY
    })

    if not connect_mt5():
        print("❌ Failed to connect to MT5")
        log_event("ERROR", {"message": "MT5 connection failed"})
        return
    
    print("✅ Connected to MT5\n")

    try:
        run_startup_analysis(send_telegram=True)
    except Exception as exc:
        log_event("STARTUP_ANALYSIS_ERROR", {"error": str(exc)})

    last_news_report_ts = time.time()
    
    # Core components
    risk = RiskManager()
    state = StateManager()
    engine = ExecutionEngine()
    strategy = InstitutionalStrategyEngine()
    regime = RegimeDetector()
    smc = SMCAnalyzer()
    exit_engine = SmartExitEngine()
    validator = ExecutionValidator()
    data_validator = DataValidator()
    
    # ================================================================
    # CRITICAL: SYNC WITH MT5 ON STARTUP
    # ================================================================
    sync_mt5_positions_with_state(state)
    
    iteration = 0
    last_status_print = 0.0
    last_candle_key = None
    last_structure_key = None
    last_regime_state = None
    last_status_snapshot = None
    last_block_reason = None
    last_block_log_time = 0.0
    last_skip_log_state = {}
    directional_cooldown_state = {}
    last_sl_update_by_ticket = {}
    recent_sweep_history = []
    regime_candle_history = []
    data_backoff_until = 0.0
    data_backoff_reason = None
    data_backoff_logged_until = 0.0
    
    try:
        while True:
            iteration += 1

            news_interval_mins = int(getattr(config, "NEWS_ANALYSIS_INTERVAL_MINS", 30) or 30)
            news_interval_secs = max(news_interval_mins, 0) * 60
            if news_interval_secs and (time.time() - last_news_report_ts) >= news_interval_secs:
                try:
                    run_startup_analysis(send_telegram=True)
                except Exception as exc:
                    log_event("STARTUP_ANALYSIS_ERROR", {"error": str(exc)})
                last_news_report_ts = time.time()
            
            # ================================================================
            # PHASE 1: SAFETY & STATE CHECKS
            # ================================================================

            reconcile_state_with_mt5(state)
            risk.open_trades = len(state.get_open_trades())
            state.reset_daily_stats()
            can_trade, risk_reason = risk.can_trade(include_open_limit=False)
            session_policy = evaluate_session_policy()

            if not can_trade:
                now = time.time()
                if risk_reason != last_block_reason or (now - last_block_log_time) >= config.BLOCKED_LOG_COOLDOWN_SECS:
                    print(f"⛔ [{iteration}] Blocked: {risk_reason}")
                    last_block_reason = risk_reason
                    last_block_log_time = now
            
            # ================================================================
            # PHASE 2: DATA VALIDATION
            # ================================================================

            now = time.time()
            if now < data_backoff_until:
                remaining_secs = max(0.0, data_backoff_until - now)
                if data_backoff_reason and now >= data_backoff_logged_until:
                    print(f"⚠️  Data paused for {remaining_secs / 60.0:.1f}m: {data_backoff_reason}")
                    data_backoff_logged_until = now + float(getattr(config, "BLOCKED_LOG_COOLDOWN_SECS", 30) or 30)
                sleep_secs = min(remaining_secs, max(float(getattr(config, "MONITOR_INTERVAL", 1) or 1), 1.0))
                time.sleep(sleep_secs)
                continue
            
            if config.ENABLE_DATA_VALIDATION:
                rates = get_market_data()
                
                is_valid, error = data_validator.validate_rates(rates)
                if not is_valid:
                    data_backoff_reason = str(error)
                    data_backoff_until = time.time() + float(getattr(config, "DATA_RECOVERY_BACKOFF_SECS", 180) or 180)
                    data_backoff_logged_until = 0.0
                    print(f"⚠️  Data invalid: {error}. Pausing for {int(getattr(config, 'DATA_RECOVERY_BACKOFF_SECS', 180) or 180)}s")
                    log_event("DATA_ERROR", {
                        "error": error,
                        "backoff_seconds": int(getattr(config, "DATA_RECOVERY_BACKOFF_SECS", 180) or 180),
                    })
                    time.sleep(float(getattr(config, "MONITOR_INTERVAL", 1) or 1))
                    continue
            else:
                rates = get_market_data()
            
            if rates is None or len(rates) < 50:
                print(f"⚠️  [{iteration}] Insufficient data")
                time.sleep(config.MONITOR_INTERVAL)
                continue

            candle_key = get_last_candle_key(rates)
            
            # ================================================================
            # PHASE 3: CALCULATE INDICATORS
            # ================================================================
            
            df = calculate_indicators(rates)
            
            is_valid, error = data_validator.validate_indicators(df)
            if not is_valid:
                print(f"⚠️  Indicators invalid: {error}")
                time.sleep(config.MONITOR_INTERVAL)
                continue
            
            # ================================================================
            # PHASE 3.5: OUTCOME RESOLUTION FOR SHADOW-LOGGED REJECTIONS
            # ================================================================
            try:
                current_price = float(df.iloc[-1].get("close", 0.0) or 0.0) if len(df) > 0 else 0.0
                # Build time elapsed map: map each rejection timestamp to minutes since rejection
                rejected_trades = state.state.get("rejected_trades", [])
                current_time_unix = time.time()
                time_elapsed_map = {}
                for rejected in rejected_trades:
                    ts_str = rejected.get("timestamp", "")
                    try:
                        # Parse ISO timestamp and calculate elapsed minutes
                        from datetime import datetime as dt_parse
                        rejection_dt = dt_parse.fromisoformat(ts_str)
                        rejection_unix = rejection_dt.timestamp()
                        elapsed_secs = current_time_unix - rejection_unix
                        elapsed_mins = elapsed_secs / 60.0
                        time_elapsed_map[ts_str] = elapsed_mins
                    except:
                        pass
                
                if time_elapsed_map:
                    state.resolve_rejection_outcomes(current_price, time_elapsed_map)
            except Exception as e:
                log_event("ERROR", {"message": f"Failed to resolve rejection outcomes: {str(e)}"})
            
            # ================================================================
            # PHASE 4: REGIME DETECTION
            # ================================================================
            
            regime_data = regime.detect_regime(df, candle_time=candle_key)
            if regime_data.get("regime") != last_regime_state:
                print(f"🔄 Regime changed → {regime_data.get('regime')}")
                last_regime_state = regime_data.get("regime")

            if config.ENABLE_REGIME_STABILITY_CONFIRMATION and not regime_data.get("is_confirmed", True):
                time.sleep(config.MONITOR_INTERVAL)
                continue

            should_trade_regime, regime_reason = regime.should_trade_in_regime(
                regime_data["regime"],
                strategy_type=config.STRATEGY_TYPE,
                allow_transition=config.ALLOW_TRANSITION_SCALP
            )
            
            if not should_trade_regime and config.ENABLE_REGIME_FILTER:
                # Add cooldown to reduce log spam
                if time.time() - regime.last_regime_log > config.REGIME_LOG_COOLDOWN:
                    print(f"⚠️  Regime unfavorable: {regime_reason}")
                    regime.last_regime_log = time.time()
                time.sleep(config.MONITOR_INTERVAL)
                continue
            
            # ================================================================
            # PHASE 5: SMC ANALYSIS
            # ================================================================
            
            smc_data = smc.identify_liquidity_levels(rates)
            smc_structure = smc.detect_structure(rates)
            smc_data.update(smc_structure)

            structure_key = build_structure_key(df, smc_data, regime_data)
            new_candle_closed = candle_key is not None and candle_key != last_candle_key
            structure_changed = structure_key is not None and structure_key != last_structure_key

            if new_candle_closed:
                regime_candle_history.append(str(regime_data.get("regime", "UNKNOWN")).upper())
                regime_candle_history = regime_candle_history[-3:]

            regime_recently_changed = False
            if len(regime_candle_history) >= 2 and regime_candle_history[-1] != regime_candle_history[-2]:
                regime_recently_changed = True

            if not new_candle_closed and not structure_changed:
                manage_open_trade_exits(
                    state.get_open_trades(),
                    df,
                    smc_data,
                    None,
                    candle_key,
                    state,
                    engine,
                    exit_engine,
                    risk,
                    directional_cooldown_state,
                    last_skip_log_state,
                    last_sl_update_by_ticket,
                )

                stats = state.get_daily_stats()
                status = risk.get_status()
                open_count = len(state.get_open_trades())

                status_snapshot = (
                    stats["daily_pnl"],
                    stats["daily_wins"],
                    stats["daily_losses"],
                    stats["max_drawdown"],
                    regime_data.get("regime"),
                    open_count
                )

                if status_snapshot != last_status_snapshot and time.time() - last_status_print >= config.STATUS_PRINT_INTERVAL_SECS:
                    print(f"\n📊 Status | PnL: {stats['daily_pnl']:+.2f} | Wins: {stats['daily_wins']} | Losses: {stats['daily_losses']} | DD: {stats['max_drawdown']:.2f}")
                    print(f"   Regime: {regime_data['regime']} | Open: {open_count}\n")
                    last_status_print = time.time()
                    last_status_snapshot = status_snapshot

                last_candle_key = candle_key
                last_structure_key = structure_key
                time.sleep(config.MONITOR_INTERVAL)
                continue

            if new_candle_closed:
                if smc_data.get("bull_sweep"):
                    recent_sweep_history.append({"candle_time": candle_key, "direction": "BUY"})
                elif smc_data.get("bear_sweep"):
                    recent_sweep_history.append({"candle_time": candle_key, "direction": "SELL"})
                else:
                    recent_sweep_history.append({"candle_time": candle_key, "direction": None})

                recent_sweep_history = recent_sweep_history[-3:]
            
            # ================================================================
            # PHASE 6: INSTITUTIONAL STRATEGY ENGINE
            # ================================================================
            
            signal = strategy.detect_signal(
                df,
                smc_data=smc_data,
                regime=regime_data,
                candle_time=candle_key,
                recent_sweeps=recent_sweep_history,
            )
            if signal is not None:
                dxy_info = get_dxy_trend(timeframe=config.TIMEFRAME)
                dxy_trend = dxy_info.get("trend", "neutral") if isinstance(dxy_info, dict) else "neutral"
                silver_info = get_silver_trend(timeframe=config.TIMEFRAME)
                silver_trend = silver_info.get("trend", "neutral") if isinstance(silver_info, dict) else "neutral"
                signal = apply_dxy_correlation(signal, dxy_trend)
                signal = apply_silver_correlation(signal, silver_info)

                if session_policy.get("skip_signal"):
                    now = time.time()
                    block_reason = f"Session filter: {session_policy.get('skip_reason')}"
                    if block_reason != last_block_reason or (now - last_block_log_time) >= config.BLOCKED_LOG_COOLDOWN_SECS:
                        print(f"⚠️  [{iteration}] {block_reason}")
                        last_block_reason = block_reason
                        last_block_log_time = now
                    
                    # PHASE 1: Log session policy rejection to shadow memory
                    try:
                        entry_price = float(df.iloc[-1].get("close", 0.0) or 0.0) if len(df) > 0 else 0.0
                        atr = float(df.iloc[-1].get("atr", 0.0) or 0.0) if len(df) > 0 else 0.0
                        rejection_record = {
                            "reason": block_reason,
                            "direction": str(signal.get("direction", "")).upper(),
                            "score": float(signal.get("score", 0.0) or 0.0),
                            "confidence": str(signal.get("confidence", "LOW")).upper(),
                            "setup_tag": str(signal.get("setup_tag", "UNKNOWN_SETUP")),
                            "regime": str(regime_data.get("regime", "UNKNOWN")).upper(),
                            "session": str(session_policy.get("session_tag", "UNKNOWN")).upper(),
                            "entry_price": entry_price,
                            "spread": float(dxy_info.get("spread", 0.0) or 0.0) if isinstance(dxy_info, dict) else 0.0,
                            "atr": atr,
                        }
                        state.register_rejected_signal(rejection_record)
                    except Exception as e:
                        log_event("ERROR", {"message": f"Failed to log session rejection to shadow memory: {str(e)}"})
                    
                    time.sleep(config.MONITOR_INTERVAL)
                    continue

                # ================================================================
                # PHASE 7: CLAUDE CONTEXT ANALYSIS (validation only)
                # ================================================================
                
                market_context = {
                    "price": df.iloc[-1]["close"],
                    "trend": f"EMA9={df.iloc[-1]['ema9']:.2f} vs EMA50={df.iloc[-1]['ema50']:.2f}",
                    "volatility": regime_data.get("regime"),
                    "structure": smc_data.get("structure"),
                    "momentum": f"RSI={df.iloc[-1]['rsi']:.1f}",
                    "session": session_policy.get("session_tag"),
                    "dxy_trend": dxy_trend,
                    "yields_trend": config.CORRELATION_YIELDS_TREND if bool(getattr(config, "ENABLE_YIELDS_CONTEXT", False)) else "DISABLED",
                    "silver_trend": silver_trend,
                    "silver_momentum_state": silver_info.get("momentum_state", "weak") if isinstance(silver_info, dict) else "weak",
                    "retail_long_ratio": config.CORRELATION_RETAIL_LONG_RATIO,
                }
                
                claude_context = analyze_market_context(market_context)
                signal = refine_signal(signal, claude_context)

                setup_tag = build_setup_tag(signal, smc_data)
                signal["setup_tag"] = setup_tag
                signal["session"] = session_policy.get("session_tag")
                signal["regime"] = regime_data.get("regime")
                signal = apply_adaptive_journal_weight(
                    signal,
                    state,
                    setup_tag=setup_tag,
                    session_tag=session_policy.get("session_tag"),
                    regime_tag=regime_data.get("regime")
                )

                validation_entry_price = float(df.iloc[-1]["close"])
                correlation_score_adjustment = get_correlation_score_adjustment(signal)
                decision_validation = validate_trade_decision(
                    signal,
                    {
                        "mode": "execution",
                        "symbol": config.SYMBOL,
                        "entry_price": validation_entry_price,
                        "regime": regime_data.get("regime"),
                        "session_policy": session_policy,
                        "correlation_score_adjustment": correlation_score_adjustment,
                        "correlation_momentum_pct": float(silver_info.get("momentum_pct", 0.0) or 0.0) if isinstance(silver_info, dict) else 0.0,
                        "trend_context": {
                            "df": df,
                            "smc_data": smc_data,
                            "state": state,
                            "dxy_info": dxy_info,
                            "silver_info": silver_info,
                        },
                        "regime_recently_changed": regime_recently_changed,
                        "now_ts": time.time(),
                    }
                )
                emit_decision_validation_debug(iteration, "execution", signal, decision_validation)
                if not decision_validation.get("allowed", False):
                    print(f"⚠️  [{iteration}] {decision_validation.get('reason')}")
                    log_event("SIGNAL_SKIPPED", {
                        "reason": decision_validation.get("reason"),
                        "mode": "execution",
                        "direction": signal.get("direction"),
                        "score": signal.get("score"),
                        "confidence": signal.get("confidence"),
                    })
                    time.sleep(config.MONITOR_INTERVAL)
                    continue
                
                # ================================================================
                # PHASE 8: ENTRY DECISION
                # ================================================================
                
                should_enter, enter_reason = strategy.should_enter_trade(signal)
                
                if should_enter:
                    if not can_trade:
                        # ================================================================
                        # PHASE 6: RISK-LAYER SEPARATION - Account Safety Gate
                        # This is ONLY for account-level safety (daily loss, consecutive
                        # losses, max open trades). NOT for trade quality assessment.
                        # ================================================================
                        now = time.time()
                        gate_reason = risk_reason if risk_reason else "Risk gate active"
                        log_trade_rejected(
                            gate_reason,
                            mode="execution",
                            symbol=config.SYMBOL,
                            direction=str(signal.get("direction", "")).upper(),
                            score=float(signal.get("score", 0.0) or 0.0),
                            confidence=str(signal.get("confidence", "LOW")).upper(),
                        )
                        
                        # PHASE 1: Log risk gate rejection to shadow memory
                        try:
                            entry_price = float(df.iloc[-1].get("close", 0.0) or 0.0) if len(df) > 0 else 0.0
                            atr = float(df.iloc[-1].get("atr", 0.0) or 0.0) if len(df) > 0 else 0.0
                            rejection_record = {
                                "reason": gate_reason,
                                "direction": str(signal.get("direction", "")).upper(),
                                "score": float(signal.get("score", 0.0) or 0.0),
                                "confidence": str(signal.get("confidence", "LOW")).upper(),
                                "setup_tag": str(signal.get("setup_tag", "UNKNOWN_SETUP")),
                                "regime": str(regime_data.get("regime", "UNKNOWN")).upper(),
                                "session": str(session_policy.get("session_tag", "UNKNOWN")).upper(),
                                "entry_price": entry_price,
                                "spread": float(dxy_info.get("spread", 0.0) or 0.0) if isinstance(dxy_info, dict) else 0.0,
                                "atr": atr,
                            }
                            state.register_rejected_signal(rejection_record)
                        except Exception as e:
                            log_event("ERROR", {"message": f"Failed to log risk gate rejection to shadow memory: {str(e)}"})
                        
                        if gate_reason != last_block_reason or (now - last_block_log_time) >= config.BLOCKED_LOG_COOLDOWN_SECS:
                            print(f"⛔ [{iteration}] Entry blocked: {gate_reason}")
                            last_block_reason = gate_reason
                            last_block_log_time = now
                        time.sleep(config.MONITOR_INTERVAL)
                        continue

                    cooldown_decision = evaluate_directional_cooldown(
                        direction=signal.get("direction"),
                        regime=regime_data.get("regime"),
                        score=signal.get("score", 0.0),
                        correlation_score=correlation_score_adjustment,
                        confidence=signal.get("confidence", "LOW"),
                        cooldown_state=directional_cooldown_state,
                        now_ts=time.time(),
                    )

                    if cooldown_decision.get("blocked"):
                        remaining_min = float(cooldown_decision.get("remaining_secs", 0.0)) / 60.0
                        log_trade_rejected(
                            "Cooldown active: skipping trade",
                            mode="execution",
                            symbol=config.SYMBOL,
                            direction=str(signal.get("direction", "")).upper(),
                            score=float(signal.get("score", 0.0) or 0.0),
                            confidence=str(signal.get("confidence", "LOW")).upper(),
                            remaining_minutes=round(remaining_min, 2),
                        )
                        
                        # PHASE 1: Log cooldown rejection to shadow memory
                        try:
                            entry_price = float(df.iloc[-1].get("close", 0.0) or 0.0) if len(df) > 0 else 0.0
                            atr = float(df.iloc[-1].get("atr", 0.0) or 0.0) if len(df) > 0 else 0.0
                            rejection_record = {
                                "reason": f"Cooldown active ({remaining_min:.1f}m remaining)",
                                "direction": str(signal.get("direction", "")).upper(),
                                "score": float(signal.get("score", 0.0) or 0.0),
                                "confidence": str(signal.get("confidence", "LOW")).upper(),
                                "setup_tag": str(signal.get("setup_tag", "UNKNOWN_SETUP")),
                                "regime": str(regime_data.get("regime", "UNKNOWN")).upper(),
                                "session": str(session_policy.get("session_tag", "UNKNOWN")).upper(),
                                "entry_price": entry_price,
                                "spread": float(dxy_info.get("spread", 0.0) or 0.0) if isinstance(dxy_info, dict) else 0.0,
                                "atr": atr,
                            }
                            state.register_rejected_signal(rejection_record)
                        except Exception as e:
                            log_event("ERROR", {"message": f"Failed to log cooldown rejection to shadow memory: {str(e)}"})
                        
                        print(f"⚠️  [{iteration}] Cooldown active: skipping trade")
                        print(f"   Direction: {signal.get('direction')} | Remaining: {remaining_min:.1f}m")
                        log_event("SIGNAL_SKIPPED", {
                            "reason": "Cooldown active: skipping trade",
                            "mode": "execution",
                            "direction": signal.get("direction"),
                            "remaining_minutes": round(remaining_min, 2),
                        })
                        time.sleep(config.MONITOR_INTERVAL)
                        continue

                    if cooldown_decision.get("bypassed"):
                        print(f"✅ [{iteration}] Cooldown bypassed: strong trend conditions")
                        log_event("INFO", {
                            "message": "Cooldown bypassed: strong trend conditions",
                            "mode": "execution",
                            "direction": signal.get("direction"),
                        })

                    # ================================================================
                    # CHECK: Trade capacity + high-potential second-trade allowance
                    # ================================================================
                    open_trades = state.get_open_trades()

                    open_count = len(open_trades)
                    high_potential = is_high_potential_signal(signal)
                    allowed_open_trades = config.MAX_OPEN_TRADES
                    same_direction_gate = _get_same_direction_add_on_gate(
                        open_trades,
                        signal["direction"],
                        df.iloc[-1]["close"],
                    )

                    if config.ENABLE_HIGH_POTENTIAL_SECOND_TRADE and high_potential:
                        allowed_open_trades = max(
                            config.MAX_OPEN_TRADES,
                            config.MAX_OPEN_TRADES_HIGH_POTENTIAL
                        )

                    if str(getattr(config, "STRATEGY_TYPE", "")).lower() == "scalp":
                        allowed_open_trades = max(allowed_open_trades, int(getattr(config, "SCALP_MAX_TRADES", 2) or 2))

                    if open_count >= allowed_open_trades:
                        now = time.time()
                        block_reason = f"Max open trades reached: {open_count}"
                        log_trade_rejected(
                            block_reason,
                            mode="execution",
                            symbol=config.SYMBOL,
                            direction=str(signal.get("direction", "")).upper(),
                            score=float(signal.get("score", 0.0) or 0.0),
                            confidence=str(signal.get("confidence", "LOW")).upper(),
                            open_count=open_count,
                            allowed_open_trades=allowed_open_trades,
                        )
                        if block_reason != last_block_reason or (now - last_block_log_time) >= config.BLOCKED_LOG_COOLDOWN_SECS:
                            print(f"⚠️  [{iteration}] Signal generated but already have {open_count} open trade(s)")
                            print(f"   Allowed now: {allowed_open_trades} | High potential: {high_potential}")
                            print(f"   Existing ticket: {open_trades[0]['ticket']}")
                            last_block_reason = block_reason
                            last_block_log_time = now
                        time.sleep(config.MONITOR_INTERVAL)
                        continue

                    if not same_direction_gate.get("allowed", True):
                        now = time.time()
                        block_reason = str(same_direction_gate.get("reason") or "Same-direction add-on blocked")
                        log_trade_rejected(
                            block_reason,
                            mode="execution",
                            symbol=config.SYMBOL,
                            direction=str(signal.get("direction", "")).upper(),
                            score=float(signal.get("score", 0.0) or 0.0),
                            confidence=str(signal.get("confidence", "LOW")).upper(),
                            same_direction_open=len(same_direction_gate.get("same_direction_trades", [])),
                            first_trade_profit_r=round(float(same_direction_gate.get("first_trade_profit_r", 0.0) or 0.0), 2) if same_direction_gate.get("first_trade_profit_r") is not None else None,
                        )
                        if block_reason != last_block_reason or (now - last_block_log_time) >= config.BLOCKED_LOG_COOLDOWN_SECS:
                            print(f"⚠️  [{iteration}] {block_reason}")
                            if same_direction_gate.get("first_trade_profit_r") is not None:
                                print(f"   First same-direction trade: {same_direction_gate.get('first_trade_profit_r'):.2f}R")
                            last_block_reason = block_reason
                            last_block_log_time = now
                        time.sleep(config.MONITOR_INTERVAL)
                        continue
                    
                    state.register_signal(signal)
                    
                    print(f"\n🎯 [{iteration}] SIGNAL GENERATED")
                    print(f"   Direction: {signal['direction']}")
                    print(f"   Score: {signal['score']:.1f}")
                    print(f"   Confidence: {signal['confidence']}")
                    print(f"   Regime: {regime_data['regime']}")
                    if signal.get("adaptive_adjustments"):
                        print(f"   Adaptive: {signal.get('adaptive_adjustments')}")
                    
                    # ================================================================
                    # PHASE 9: SPREAD CHECK
                    # ================================================================
                    
                    spread = get_spread(config.SYMBOL)
                    is_valid_spread, spread_error = data_validator.validate_spread(spread)
                    
                    if not is_valid_spread:
                        print(f"❌ {spread_error}")
                        time.sleep(config.MONITOR_INTERVAL)
                        continue
                    
                    # ================================================================
                    # PHASE 10: CALCULATE LOT SIZE (confidence-weighted)
                    # ================================================================

                    daily_stats = state.get_daily_stats()
                    risk_multiplier, risk_notes = get_dynamic_risk_multiplier(
                        signal,
                        daily_stats=daily_stats,
                        session_risk_multiplier=session_policy.get("risk_multiplier", 1.0),
                        correlation_score_adjustment=correlation_score_adjustment,
                    )
                    validation_risk_adj = float(decision_validation.get("risk_multiplier_adjustment", 1.0) or 1.0)
                    if validation_risk_adj != 1.0:
                        risk_multiplier *= validation_risk_adj
                        risk_notes.append(f"Decision validation multiplier x{validation_risk_adj:.2f}")

                    if bool(decision_validation.get("early_exit_enabled", False)):
                        signal["early_exit_enabled"] = True

                    if risk_multiplier <= 0:
                        print(f"⚠️  [{iteration}] Dynamic risk model skipped trade: {', '.join(risk_notes)}")
                        time.sleep(config.MONITOR_INTERVAL)
                        continue

                    lot = float(config.BASE_LOT_SIZE) * float(risk_multiplier)
                    lot = min(lot, 0.01)

                    # ================================================================
                    # PHASE 11: CALCULATE SL/TP
                    # ================================================================

                    atr = df["atr"].iloc[-1]
                    entry_price = df.iloc[-1]["close"]
                    tp_rr = float(config.TP_RR_HIGH)
                    if str(getattr(config, "STRATEGY_TYPE", "")).lower() == "scalp":
                        tp_rr = max(1.2, min(1.8, tp_rr))

                    validation_tp_adj = float(decision_validation.get("tp_multiplier_adjustment", 1.0) or 1.0)
                    if validation_tp_adj != 1.0:
                        tp_rr *= validation_tp_adj
                        risk_notes.append(f"HTF conflict TP multiplier x{validation_tp_adj:.2f}")

                    if signal['direction'] == "BUY":
                        sl = entry_price - (atr * config.SL_ATR_MULTIPLIER)
                        tp = entry_price + (atr * config.SL_ATR_MULTIPLIER * tp_rr)
                    else:
                        sl = entry_price + (atr * config.SL_ATR_MULTIPLIER)
                        tp = entry_price - (atr * config.SL_ATR_MULTIPLIER * tp_rr)
                    
                    # ================================================================
                    # PHASE 12: EXECUTE TRADE
                    # ================================================================
                    
                    if config.AUTO_TRADE:
                        if has_open_position_same_direction(signal["direction"], symbol=config.SYMBOL):
                            log_trade_rejected(
                                "SKIPPED: same-direction position already open",
                                mode="execution",
                                symbol=config.SYMBOL,
                                direction=str(signal.get("direction", "")).upper(),
                                score=float(signal.get("score", 0.0) or 0.0),
                                confidence=str(signal.get("confidence", "LOW")).upper(),
                            )
                            print(f"⚠️  [{iteration}] SKIPPED: position already open in same direction")
                            log_event("SIGNAL_SKIPPED", {
                                "reason": "SKIPPED: same-direction position already open",
                                "mode": "execution",
                                "direction": signal.get("direction"),
                            })
                            time.sleep(config.MONITOR_INTERVAL)
                            continue

                        # ================================================================
                        # PHASE 7: RR VALIDATION BEFORE ORDER SEND
                        # ================================================================
                        
                        rr_validation_passed = True
                        if bool(getattr(config, "ENABLE_RR_VALIDATION", True)):
                            risk_distance = abs(entry_price - sl)
                            reward_distance = abs(tp - entry_price)
                            
                            if risk_distance > 1e-10 and reward_distance > 1e-10:  # Avoid division by zero
                                # Phase 7 validates reward-to-risk, not risk-to-reward.
                                actual_rr = reward_distance / risk_distance
                                min_rr = float(getattr(config, "MIN_RR_RATIO", 1.5) or 1.5)
                                
                                if actual_rr < min_rr:
                                    rr_validation_passed = False
                                    
                                    if bool(getattr(config, "ENABLE_AUTO_TP_ADJUSTMENT", False)):
                                        # Auto-adjust TP to meet minimum reward-to-risk ratio.
                                        if signal["direction"] == "BUY":
                                            tp = entry_price + (risk_distance * min_rr)
                                        else:
                                            tp = entry_price - (risk_distance * min_rr)
                                        rr_validation_passed = True
                                        log_event("RR_AUTO_ADJUSTMENT", {
                                            "risk_distance": round(risk_distance, 6),
                                            "original_reward": round(reward_distance, 6),
                                            "new_reward": round(abs(tp - entry_price), 6),
                                            "actual_rr": round(actual_rr, 3),
                                            "min_rr": min_rr,
                                            "new_tp": round(tp, 6),
                                        })
                                        print(f"⚠️  RR too low ({actual_rr:.2f}x reward/risk). Auto-adjusted TP to {tp:.5f}")
                                    else:
                                        log_event("RR_VALIDATION_FAILED", {
                                            "risk_distance": round(risk_distance, 6),
                                            "reward_distance": round(reward_distance, 6),
                                            "actual_rr": round(actual_rr, 3),
                                            "min_rr": min_rr,
                                        })
                                        print(f"⚠️  [{iteration}] SKIPPED: RR too low ({actual_rr:.2f}x reward/risk < {min_rr}x)")
                            else:
                                rr_validation_passed = False
                                log_event("RR_VALIDATION_FAILED", {
                                    "risk_distance": round(risk_distance, 6),
                                    "reward_distance": round(reward_distance, 6),
                                    "actual_rr": None,
                                    "min_rr": float(getattr(config, "MIN_RR_RATIO", 1.5) or 1.5),
                                    "reason": "zero_distance",
                                })
                                print(f"⚠️  [{iteration}] SKIPPED: invalid RR distances (risk={risk_distance:.6f}, reward={reward_distance:.6f})")
                        
                        if not rr_validation_passed:
                            print(f"❌ [{iteration}] Trade rejected: insufficient reward-to-risk ratio")
                            time.sleep(config.MONITOR_INTERVAL)
                            continue

                        result = engine.open_trade(
                            config.SYMBOL,
                            signal["direction"],
                            lot=lot,
                            sl=sl,
                            tp=tp
                        )
                        
                        # ================================================================
                        # PHASE 13: EXECUTION VALIDATION & REGISTRATION
                        # ================================================================
                        
                        if result["success"]:
                            ticket = result.get("order_id")
                            
                            # CRITICAL: Always register successful trades
                            # This prevents losing track of trades on crashes
                            state.register_trade_open(
                                ticket,
                                config.SYMBOL,
                                signal["direction"],
                                result["price"],
                                lot,
                                sl,
                                tp,
                                signal_data=signal  # STORE ENTRY CONDITIONS FOR RESTART RESILIENCE
                            )
                            risk.register_trade_open()
                            
                            # Optional: Validate fill quality
                            validation_status = "OK"
                            if config.ENABLE_EXECUTION_VALIDATION:
                                # Use raw MT5 result if available, otherwise create fallback
                                mt5_result = result.get("mt5_result")
                                if mt5_result is None:
                                    # Fallback: create object from dict
                                    mt5_result = type('obj', (object,), result)()
                                
                                try:
                                    is_filled, fill_details = validator.validate_fill(
                                        mt5_result,
                                        lot
                                    )
                                    if not is_filled:
                                        validation_status = f"⚠️ {fill_details.get('error', 'Unknown')}"
                                except Exception as e:
                                    # Log validation error but trade is already registered
                                    log_event("WARNING", {
                                        "message": f"Validation check failed: {str(e)}",
                                        "error_type": type(e).__name__,
                                        "trade_ticket": ticket,
                                        "note": "Trade already registered - will be managed"
                                    })
                                    validation_status = "⚠️ Check failed"
                            
                            print(f"✅ Trade OPENED & REGISTERED")
                            print(f"   Ticket: {ticket}")
                            print(f"   Price: {result['price']}")
                            print(f"   SL: {sl:.2f} | TP: {tp:.2f}")
                            print(f"   Entry Score: {signal.get('score', 0):.1f} | Confidence: {signal.get('confidence', 'N/A')}")
                            print(f"   Risk Multiplier: x{risk_multiplier:.2f} ({'; '.join(risk_notes)})")
                            print(f"   📝 Entry conditions STORED for restart resilience")
                            print(f"   Validation: {validation_status}")

                            execution_alert_sent = send_execution_trade_telegram_alert(
                                signal=signal,
                                claude_context=claude_context,
                                regime_data=regime_data,
                                session_policy=session_policy,
                                dxy_info=dxy_info,
                                silver_info=silver_info,
                                correlation_score=correlation_score_adjustment,
                                enter_reason=enter_reason,
                                decision_validation=decision_validation,
                                trade_type=decision_validation.get("trade_type", "NORMAL"),
                                trade_stage=decision_validation.get("trade_stage", "MID TREND"),
                                protection_labels=decision_validation.get("labels", []),
                                entry_price=entry_price,
                                executed_price=float(result.get("price", entry_price) or entry_price),
                                sl=sl,
                                tp=tp,
                                lot=lot,
                                spread=spread,
                                risk_multiplier=risk_multiplier,
                                risk_notes=risk_notes,
                                ticket=str(ticket),
                                trade_strategy=strategy.determine_trade_strategy_type(
                                    regime_data.get("regime", "UNKNOWN"),
                                    signal
                                ),
                            )
                            if execution_alert_sent:
                                print("   📣 Execution Telegram alert sent")
                            elif config.TELEGRAM_EXECUTION_ALERTS_ENABLED:
                                print("   ⚠️  Execution Telegram alert not sent")

                            register_signal_signature(
                                config.SYMBOL,
                                decision_validation.get("signature", _build_signal_signature(
                                    signal,
                                    {
                                        "entry_price": validation_entry_price,
                                        "regime": regime_data.get("regime"),
                                    }
                                )),
                                now_ts=time.time(),
                            )
                            _set_directional_cooldown(
                                directional_cooldown_state,
                                signal.get("direction"),
                                float(getattr(config, "DIRECTIONAL_COOLDOWN_DEFAULT_MINUTES", 15) or 15),
                                source="entry",
                                now_ts=time.time(),
                            )
                        else:
                            print(f"❌ Trade failed: {result.get('error')}")

            last_candle_key = candle_key
            last_structure_key = structure_key
            
            # ================================================================
            # PHASE 14: CHECK EXIT CONDITIONS (for open trades)
            # ================================================================
            
            manage_open_trade_exits(
                state.get_open_trades(),
                df,
                smc_data,
                signal,
                candle_key,
                state,
                engine,
                exit_engine,
                risk,
                directional_cooldown_state,
                last_skip_log_state,
                last_sl_update_by_ticket,
            )
            
            # ================================================================
            # PHASE 15: DISPLAY STATUS
            # ================================================================
            
            stats = state.get_daily_stats()
            status = risk.get_status()
            open_count = len(state.get_open_trades())
            status_snapshot = (
                stats["daily_pnl"],
                stats["daily_wins"],
                stats["daily_losses"],
                stats["max_drawdown"],
                regime_data.get("regime"),
                open_count
            )

            if status_snapshot != last_status_snapshot and time.time() - last_status_print >= config.STATUS_PRINT_INTERVAL_SECS:
                print(f"\n📊 Status | PnL: {stats['daily_pnl']:+.2f} | Wins: {stats['daily_wins']} | Losses: {stats['daily_losses']} | DD: {stats['max_drawdown']:.2f}")
                print(f"   Regime: {regime_data['regime']} | Open: {open_count}\n")
                last_status_print = time.time()
                last_status_snapshot = status_snapshot
            
            time.sleep(config.MONITOR_INTERVAL)
    
    except KeyboardInterrupt:
        print("\n\n⏹️  Bot stopped by user")
        log_event("BOT_STOPPED", {"reason": "User interrupt", "iterations": iteration})
    
    except Exception as e:
        print(f"\n❌ Error: {str(e)}")
        log_event("BOT_ERROR", {"error": str(e), "iteration": iteration})
        import traceback
        traceback.print_exc()
    
    finally:
        print_claude_session_summary()
        disconnect_mt5()
        print("✅ MT5 disconnected")
        print("=" * 60)
        print("🛑 BOT SHUTDOWN")
        print("=" * 60)


if __name__ == "__main__":
    main()
