# ============================================================
#  MT5 BROKER CLIENT - MetaTrader 5 Connection
# ============================================================

import MetaTrader5 as mt5
import config
from monitoring.logger import log_event


def connect_mt5(login=None, password=None, server=None):
    """
    Connect to MetaTrader 5 terminal
    
    Returns:
        bool: True if connected successfully
    """
    
    # Use environment variables or parameters
    if not login or not password or not server:
        log_event("WARNING", {
            "message": "MT5 credentials not fully configured. Proceeding in demo mode."
        })
    
    if not mt5.initialize():
        log_event("ERROR", {
            "message": "Failed to initialize MT5",
            "error": mt5.last_error()
        })
        return False
    
    account_info = mt5.account_info()
    log_event("MT5 Connected", {
        "version": mt5.version(),
        "login": getattr(account_info, "login", None),
        "server": getattr(account_info, "server", None),
        "balance": getattr(account_info, "balance", None),
        "equity": getattr(account_info, "equity", None),
        "leverage": getattr(account_info, "leverage", None),
        "trade_allowed": getattr(account_info, "trade_allowed", None),
        "trade_expert": getattr(account_info, "trade_expert", None)
    })
    
    return True


def get_symbol_info(symbol):
    """Get symbol information"""
    info = mt5.symbol_info(symbol)
    if info is None:
        log_event("ERROR", {"message": f"Symbol not found: {symbol}"})
        return None
    return info


def get_rates(symbol, timeframe, count):
    """
    Get OHLC data for a symbol
    
    Args:
        symbol (str): Symbol name (e.g., "XAUUSD")
        timeframe (str): Timeframe (e.g., "M15")
        count (int): Number of candles
        
    Returns:
        list: Array of OHLC data
    """
    
    # Convert timeframe string to MT5 constant
    tf_map = {
        "M1": mt5.TIMEFRAME_M1,
        "M3": getattr(mt5, "TIMEFRAME_M3", mt5.TIMEFRAME_M5),
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }
    
    tf = tf_map.get(timeframe, mt5.TIMEFRAME_M15)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    
    if rates is None:
        log_event("ERROR", {"message": f"Failed to get rates for {symbol}"})
        return []
    
    return rates


def get_spread(symbol):
    """Get current bid-ask spread"""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    return tick.ask - tick.bid


def get_account_info():
    """Get current MT5 account info object."""
    return mt5.account_info()


def disconnect_mt5():
    """Disconnect from MT5"""
    mt5.shutdown()
    log_event("MT5 Disconnected", {})
