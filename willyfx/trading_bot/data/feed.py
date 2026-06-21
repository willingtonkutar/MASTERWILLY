# ============================================================
#  DATA FEED - Market Data Management
# ============================================================

from broker.mt5_client import get_rates
import config


def get_market_data(symbol=None, timeframe=None, candles=None):
    """
    Retrieve market data
    
    Args:
        symbol (str): Trading symbol
        timeframe (str): Timeframe
        candles (int): Number of candles
        
    Returns:
        list: OHLC data
    """
    
    symbol = symbol or config.SYMBOL
    timeframe = timeframe or config.TIMEFRAME
    candles = candles or config.CANDLES
    
    rates = get_rates(symbol, timeframe, candles)
    return rates


def calculate_atr(rates, period=14):
    """Calculate Average True Range"""
    
    if len(rates) < period:
        return None
    
    tr_list = []
    for i in range(1, len(rates)):
        high = rates[i]['high']
        low = rates[i]['low']
        close = rates[i-1]['close']
        
        tr = max(
            high - low,
            abs(high - close),
            abs(low - close)
        )
        tr_list.append(tr)
    
    atr = sum(tr_list[-period:]) / period
    return atr


def calculate_ema(rates, period=20):
    """Calculate Exponential Moving Average"""
    
    if len(rates) < period:
        return None
    
    multiplier = 2 / (period + 1)
    closes = [r['close'] for r in rates[-period:]]
    
    sma = sum(closes) / period
    ema = sma
    
    for close in closes[1:]:
        ema = close * multiplier + ema * (1 - multiplier)
    
    return ema


def calculate_sma(df, period):
    """Calculate Simple Moving Average as a pandas Series."""
    if len(df) < period:
        return None
    return df['close'].rolling(window=period).mean()


def calculate_vwap(df):
    """Calculate Volume Weighted Average Price."""
    q = df['tick_volume'] * (df['high'] + df['low'] + df['close']) / 3
    vwap = q.cumsum() / df['tick_volume'].cumsum()
    return vwap
