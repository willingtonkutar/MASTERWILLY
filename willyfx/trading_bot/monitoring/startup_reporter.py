# ============================================================
#  STARTUP REPORTER - MTF Structure + News Context
# ============================================================

import time
from datetime import datetime

import config
from data.feed import get_market_data
from monitoring.logger import log_event
from monitoring.telegram_notifier import send_telegram_signal, telegram_is_configured


def _format_level(level):
    if level is None:
        return "N/A"
    return f"{float(level):.2f}"


def _format_structure_block(item):
    tf = item.get("timeframe") or "?"
    structure = item.get("structure") or "UNKNOWN"
    last_close = _format_level(item.get("last_close"))
    swing_high = _format_level(item.get("swing_high"))
    swing_low = _format_level(item.get("swing_low"))
    confirm_bull = _format_level(item.get("confirm_bull"))
    confirm_bear = _format_level(item.get("confirm_bear"))

    return (
        f"{tf}: {structure}\n"
        f"- Last Close: {last_close}\n"
        f"- Swing High: {swing_high} | Bull confirm: close above {confirm_bull}\n"
        f"- Swing Low: {swing_low} | Bear confirm: close below {confirm_bear}"
    )


def _format_news_block(headlines, calendar_items):
    lines = []
    if headlines:
        lines.append("HEADLINES:")
        for headline in headlines:
            lines.append(f"- {headline}")
    if calendar_items:
        if lines:
            lines.append("")
        lines.append("ECONOMIC CALENDAR:")
        for event in calendar_items:
            lines.append(f"- {event}")
    return "\n".join(lines) if lines else "No news feed items available."


def _format_claude_block(claude_context):
    if not claude_context:
        return "Claude news context unavailable."

    summary = claude_context.get("summary") or "No summary."
    impact = claude_context.get("impact") or "No impact assessment."
    risks = claude_context.get("risks") or "No risk notes."
    watchlist = claude_context.get("watchlist") or "No watchlist."

    return (
        f"SUMMARY: {summary}\n"
        f"IMPACT: {impact}\n"
        f"RISKS: {risks}\n"
        f"WATCHLIST: {watchlist}"
    )


def build_startup_report(strategy_signal):
    """
    Builds the XAUUSD Smart Money Report from the strategy signal.
    """
    if not strategy_signal:
        return "No signal data available to generate a report."

    symbol_label = str(config.SYMBOL).upper()
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    bias = strategy_signal.get("bias") or "NEUTRAL"
    score = float(strategy_signal.get("score", 0) or 0)
    confidence = strategy_signal.get("confidence", "NONE")
    direction = strategy_signal.get("direction") or "WAIT"
    reasons = strategy_signal.get("reasons") or []
    htf_analysis = strategy_signal.get("structure_analysis", {}) or {}
    poi = strategy_signal.get("point_of_interest", {}) or {}

    trend_lines = []
    for timeframe in ("D1", "H4", "H1", "M15", "M5"):
        trend = htf_analysis.get(timeframe, {}).get("trend", "UNKNOWN")
        trend_lines.append(f"{timeframe}: {trend}")

    poi_type = poi.get("type", "None")
    poi_side = str(poi.get("side", "N/A")).upper()
    poi_timeframe = poi.get("timeframe", "N/A")
    poi_top = _format_level(poi.get("top"))
    poi_bottom = _format_level(poi.get("bottom"))
    poi_message = poi.get("message")

    if poi_message:
        poi_text = poi_message
    else:
        poi_text = (
            f"{poi_timeframe} {poi_side} {poi_type}\n"
            f"- Zone: {poi_bottom} - {poi_top}\n"
            f"- FVG overlap: {'YES' if poi.get('has_fvg_overlap') else 'NO'}"
        )

    reasons_text = "\n".join(f"- {reason}" for reason in reasons[:8]) or "- Waiting for confluence"

    recommendation = "Wait for liquidity sweep + LTF confirmation."
    if direction in ("BUY", "SELL"):
        recommendation = f"{direction} setup is valid if risk/session gates pass."

    report = f"""
XAUUSD SMC STARTUP ANALYSIS
Time: {timestamp}

MODE:
Symbol: {symbol_label}
Decision: {direction}
Bias: {bias}
Score: {score:.1f}
Confidence: {confidence}

HIGHER TIMEFRAMES:
{chr(10).join(trend_lines)}

CURRENT POINT OF INTEREST:
{poi_text}

WHAT THE BOT IS CHECKING:
{reasons_text}

NEXT STEP:
{recommendation}
"""
    return report.strip()


def _trim_message(message, limit=3900):
    if message is None:
        return ""
    if len(message) <= limit:
        return message
    return message[:limit].rstrip() + "..."


from strategy.strategy_engine import InstitutionalStrategyEngine
import pandas as pd

def run_startup_analysis(send_telegram=True):
    if not bool(getattr(config, "ENABLE_STARTUP_ANALYSIS", True)):
        return None

    start_ts = time.time()
    
    # We need a primary timeframe for the main dataframe for indicators.
    # The strategy engine will handle multi-timeframe analysis internally.
    primary_timeframe = "H1"
    candles = int(getattr(config, "STARTUP_ANALYSIS_CANDLES", 200) or 200)

    rates = get_market_data(symbol=config.SYMBOL, timeframe=primary_timeframe, candles=candles)
    if rates is None or len(rates) == 0:
        log_event("STARTUP_ANALYSIS_FAILED", {"reason": "Could not fetch market data."})
        return None
        
    df = pd.DataFrame(rates)

    # The strategy engine now contains all the logic.
    strategy_engine = InstitutionalStrategyEngine()
    signal = strategy_engine.calculate_institutional_score(df)

    report = build_startup_report(signal)
    report = _trim_message(report)

    elapsed = round(time.time() - start_ts, 2)
    log_event("STARTUP_ANALYSIS_COMPLETE", {
        "duration_secs": elapsed,
    })

    if send_telegram and telegram_is_configured():
        send_telegram_signal(report)

    return report
