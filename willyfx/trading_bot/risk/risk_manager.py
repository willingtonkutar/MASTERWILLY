# ============================================================
#  RISK MANAGER - Core Risk Control System
# ============================================================

import config
import MetaTrader5 as mt5
from datetime import datetime
from monitoring.logger import log_event


class RiskManager:
    """Institutional-grade risk management system"""
    
    def __init__(self):
        self.daily_loss = 0.0
        self.consecutive_losses = 0
        self.open_trades = 0
        self.today_date = datetime.now().date()
        self.trade_history = []
        
    def reset_daily_stats(self):
        """Reset daily stats at midnight"""
        current_date = datetime.now().date()
        if current_date > self.today_date:
            self.daily_loss = 0.0
            self.consecutive_losses = 0
            self.today_date = current_date
            log_event("Daily stats reset", {"date": str(current_date)})
    
    def can_trade(self, include_open_limit=True):
        """Check if trading is allowed.

        Args:
            include_open_limit (bool): If False, skip max-open-trades gate.
                Useful when bot should continue managing existing positions.
        """
        self.reset_daily_stats()
        
        # Check daily loss limit
        if self.daily_loss >= config.MAX_DAILY_LOSS_PERCENT:
            return False, f"Daily loss limit reached: {self.daily_loss}%"
        
        # Check open trades
        if include_open_limit and self.open_trades >= config.MAX_OPEN_TRADES:
            return False, f"Max open trades reached: {self.open_trades}"
        
        return True, None
    
    def register_loss(self, loss_percent):
        """Register a losing trade"""
        self.consecutive_losses += 1
        self.daily_loss += loss_percent
        self.open_trades = max(0, self.open_trades - 1)
        
        log_event("Loss registered", {
            "loss_percent": loss_percent,
            "daily_total": self.daily_loss,
            "consecutive": self.consecutive_losses
        })
    
    def register_win(self, profit_percent):
        """Register a winning trade"""
        self.consecutive_losses = 0  # Reset consecutive loss counter
        self.open_trades = max(0, self.open_trades - 1)
        
        log_event("Win registered", {
            "profit_percent": profit_percent,
            "daily_total": self.daily_loss
        })
    
    def register_trade_open(self):
        """Increment open trade counter"""
        self.open_trades += 1
    
    def get_status(self):
        """Get current risk status"""
        return {
            "daily_loss": self.daily_loss,
            "consecutive_losses": self.consecutive_losses,
            "open_trades": self.open_trades,
            "daily_loss_remaining": config.MAX_DAILY_LOSS_PERCENT - self.daily_loss
        }

    @staticmethod
    def _volume_decimals(step):
        text = f"{float(step):.8f}".rstrip("0")
        if "." not in text:
            return 0
        return len(text.split(".")[1])

    def _normalize_volume(self, symbol, requested_volume):
        info = mt5.symbol_info(symbol)
        if info is None:
            return None

        volume_min = float(getattr(info, "volume_min", 0.0) or 0.0)
        volume_max = float(getattr(info, "volume_max", 0.0) or 0.0)
        volume_step = float(getattr(info, "volume_step", 0.0) or 0.0)

        if volume_step <= 0:
            volume_step = 0.01

        normalized = round(float(requested_volume or 0.0) / volume_step) * volume_step
        if volume_min > 0:
            normalized = max(normalized, volume_min)
        if volume_max > 0:
            normalized = min(normalized, volume_max)

        decimals = self._volume_decimals(volume_step)
        normalized = round(normalized, decimals)
        return max(0.0, normalized)

    def calculate_atr_position_size(self, account_balance, entry_price, sl_price, symbol, risk_percent=0.01):
        """Risk-based lot sizing: risk amount / cash loss per lot at SL distance."""
        try:
            account_balance = float(account_balance or 0.0)
            entry_price = float(entry_price or 0.0)
            sl_price = float(sl_price or 0.0)
            risk_percent = float(risk_percent or 0.0)
        except (TypeError, ValueError):
            return None, "Invalid numeric input for ATR position sizing"

        if account_balance <= 0:
            return None, "Account balance must be positive"

        if risk_percent <= 0:
            return None, "Risk percent must be positive"

        sl_distance = abs(entry_price - sl_price)
        if sl_distance <= 0:
            return None, "SL distance must be positive"

        info = mt5.symbol_info(symbol)
        if info is None:
            return None, f"Cannot fetch symbol info for {symbol}"

        contract_size = float(getattr(info, "trade_contract_size", 0.0) or 0.0)
        if contract_size <= 0:
            return None, f"Invalid contract size for {symbol}"

        risk_amount = account_balance * risk_percent
        loss_per_lot_at_sl = sl_distance * contract_size
        if loss_per_lot_at_sl <= 0:
            return None, "Computed loss per lot is invalid"

        raw_lot = risk_amount / loss_per_lot_at_sl
        normalized_lot = self._normalize_volume(symbol, raw_lot)
        if normalized_lot is None or normalized_lot <= 0:
            return None, "Unable to normalize ATR lot size"

        return float(normalized_lot), None
