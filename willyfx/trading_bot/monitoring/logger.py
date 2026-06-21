# ============================================================
#  LOGGER - Centralized Logging System
# ============================================================

from datetime import datetime
import os
import config


def _format_value(value):
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _format_log_data(data):
    if data is None:
        return ""

    if not isinstance(data, dict):
        return str(data)

    lines = []
    for key, value in data.items():
        if value is None:
            continue
        lines.append(f"   {key:<18}: {_format_value(value)}")

    return "\n".join(lines)


def log_event(event_type, data=None):
    """
    Log an event to both file and console
    
    Args:
        event_type (str): Type of event
        data (dict): Event data/details
    """
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Console output
    formatted = _format_log_data(data)
    if formatted:
        print(f"[{timestamp}] {event_type}")
        print(formatted)
    else:
        print(f"[{timestamp}] {event_type}")
    
    # File output
    if config.LOG_TRADES_TO_FILE:
        os.makedirs(os.path.dirname(config.LOG_FILE) or ".", exist_ok=True)
        
        with open(config.LOG_FILE, "a") as f:
            if formatted:
                f.write(f"[{timestamp}] {event_type}\n{formatted}\n")
            else:
                f.write(f"[{timestamp}] {event_type}\n")


def log_trade(direction, symbol, lot, entry_price, sl, tp):
    """Log trade entry"""
    log_event("TRADE_ENTRY", {
        "direction": direction,
        "symbol": symbol,
        "lot": lot,
        "entry": entry_price,
        "sl": sl,
        "tp": tp
    })


def log_signal(signal_type, confidence, reasoning):
    """Log trading signal"""
    log_event("SIGNAL", {
        "type": signal_type,
        "confidence": confidence,
        "reasoning": reasoning
    })
