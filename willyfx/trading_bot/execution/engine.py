# ============================================================
#  EXECUTION ENGINE - Order Management & Trade Execution
# ============================================================

import MetaTrader5 as mt5
import config
from monitoring.logger import log_event


class ExecutionEngine:
    """Institutional-grade order execution system"""
    
    def __init__(self):
        self.magic_number = 123456
        self.open_positions = {}

    @staticmethod
    def _volume_decimals(step):
        text = f"{float(step):.8f}".rstrip("0")
        if "." not in text:
            return 0
        return len(text.split(".")[1])

    def _normalize_volume(self, symbol, requested_volume):
        info = mt5.symbol_info(symbol)
        if info is None:
            return None, "Cannot fetch symbol info for volume normalization"

        volume_min = float(getattr(info, "volume_min", 0.0) or 0.0)
        volume_max = float(getattr(info, "volume_max", 0.0) or 0.0)
        volume_step = float(getattr(info, "volume_step", 0.0) or 0.0)

        if volume_step <= 0:
            volume_step = 0.01

        requested = float(requested_volume or 0.0)
        normalized = round(requested / volume_step) * volume_step

        if volume_min > 0:
            normalized = max(normalized, volume_min)
        if volume_max > 0:
            normalized = min(normalized, volume_max)

        decimals = self._volume_decimals(volume_step)
        normalized = round(normalized, decimals)

        if normalized <= 0:
            return None, "Normalized volume is zero"

        return normalized, None
    
    def is_valid_sl(self, direction, new_sl, current_price, original_sl):
        """Validate SL updates so risk is never widened and SL stays on correct side."""
        direction = str(direction or "").upper()

        try:
            new_sl = float(new_sl)
            current_price = float(current_price)
            original_sl = float(original_sl)
        except (TypeError, ValueError):
            return False, "Invalid numeric inputs for SL safety validation"

        if direction == "BUY":
            if new_sl >= current_price:
                return False, "BUY SL must remain below current price"
            if new_sl < original_sl:
                return False, "BUY SL widening blocked (new SL below original SL)"
            return True, None

        if direction == "SELL":
            if new_sl <= current_price:
                return False, "SELL SL must remain above current price"
            if new_sl > original_sl:
                return False, "SELL SL widening blocked (new SL above original SL)"
            return True, None

        return False, f"Unsupported direction for SL safety: {direction}"
    
    def open_trade(self, symbol, direction, lot, sl, tp, comment="Institutional Bot"):
        """
        Open a new trade
        
        Args:
            symbol (str): Trading symbol
            direction (str): "BUY" or "SELL"
            lot (float): Trade volume/lot size
            sl (float): Stop Loss price
            tp (float): Take Profit price
            comment (str): Trade comment
            
        Returns:
            dict: Result containing order ID and status
        """
        
        # Get current price
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            log_event("ERROR", {"message": f"Cannot get tick for {symbol}"})
            return {"success": False, "error": "No tick data"}
        
        # Check spread
        spread = tick.ask - tick.bid
        if spread > config.MAX_SPREAD:
            log_event("WARNING", {
                "message": f"Spread too high: {spread}",
                "symbol": symbol
            })
            return {"success": False, "error": f"Spread too high: {spread}"}
        
        # Determine order type and price
        if direction.upper() == "BUY":
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid

        normalized_lot, volume_error = self._normalize_volume(symbol, lot)
        if volume_error:
            log_event("Trade Failed", {
                "symbol": symbol,
                "error": volume_error,
                "requested_lot": lot,
            })
            return {"success": False, "error": volume_error}

        if abs(float(normalized_lot) - float(lot)) > 1e-9:
            log_event("INFO", {
                "message": "LOT_NORMALIZED",
                "symbol": symbol,
                "requested_lot": float(lot),
                "normalized_lot": float(normalized_lot),
            })
        
        # Build order request
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": normalized_lot,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 20,
            "magic": self.magic_number,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        # Send order
        result = mt5.order_send(request)
        
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log_event("Trade Opened", {
                "symbol": symbol,
                "direction": direction,
                "lot": normalized_lot,
                "price": price,
                "sl": sl,
                "tp": tp,
                "ticket": result.order
            })
            return {
                "success": True,
                "order_id": result.order,
                "price": price,
                "mt5_result": result  # Store raw MT5 result for validation
            }
        else:
            log_event("Trade Failed", {
                "symbol": symbol,
                "error": result.comment,
                "retcode": result.retcode
            })
            return {"success": False, "error": result.comment}
    
    def close_trade(self, ticket, symbol=None, comment="Manual Close"):
        """
        Close an open position
        
        Args:
            ticket (int): Position ticket/order ID
            symbol (str): Symbol (optional)
            comment (str): Close comment
            
        Returns:
            dict: Result status
        """
        
        # Get position info
        position = mt5.positions_get(ticket=ticket)
        if position is None or len(position) == 0:
            log_event("ERROR", {"message": f"Position not found: {ticket}"})
            return {"success": False, "error": "Position not found"}
        
        pos = position[0]
        symbol = pos.symbol
        
        # Get current price
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"success": False, "error": "Cannot get tick"}
        
        # Determine close order type (opposite of entry)
        if pos.type == mt5.ORDER_TYPE_BUY:
            close_type = mt5.ORDER_TYPE_SELL
            close_price = tick.bid
        else:
            close_type = mt5.ORDER_TYPE_BUY
            close_price = tick.ask
        
        # Build close request
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": ticket,
            "price": close_price,
            "deviation": 20,
            "magic": self.magic_number,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        # Send close order
        result = mt5.order_send(request)
        
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            profit = pos.profit
            log_event("Trade Closed", {
                "ticket": ticket,
                "symbol": symbol,
                "profit": profit,
                "comment": comment
            })
            return {"success": True, "profit": profit}
        else:
            log_event("Close Failed", {
                "ticket": ticket,
                "error": result.comment
            })
            return {"success": False, "error": result.comment}

    def close_partial_trade(self, ticket, close_volume, symbol=None, comment="Partial Close"):
        """Close part of an open position."""

        position = mt5.positions_get(ticket=ticket)
        if position is None or len(position) == 0:
            log_event("ERROR", {"message": f"Position not found: {ticket}"})
            return {"success": False, "error": "Position not found"}

        pos = position[0]
        symbol = pos.symbol

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"success": False, "error": "Cannot get tick"}

        if pos.type == mt5.ORDER_TYPE_BUY:
            close_type = mt5.ORDER_TYPE_SELL
            close_price = tick.bid
        else:
            close_type = mt5.ORDER_TYPE_BUY
            close_price = tick.ask

        normalized_close_volume, volume_error = self._normalize_volume(symbol, close_volume)
        if volume_error:
            return {"success": False, "error": volume_error}

        normalized_close_volume = min(float(normalized_close_volume), float(pos.volume))
        if normalized_close_volume <= 0:
            return {"success": False, "error": "Invalid partial close volume"}

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(normalized_close_volume),
            "type": close_type,
            "position": ticket,
            "price": close_price,
            "deviation": 20,
            "magic": self.magic_number,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log_event("Partial Trade Closed", {
                "ticket": ticket,
                "symbol": symbol,
                "closed_volume": float(normalized_close_volume),
                "remaining_volume": max(0.0, float(pos.volume) - float(normalized_close_volume)),
                "comment": comment,
            })
            return {
                "success": True,
                "closed_volume": float(normalized_close_volume),
                "remaining_volume": max(0.0, float(pos.volume) - float(normalized_close_volume)),
                "price": close_price,
            }

        log_event("Partial Close Failed", {
            "ticket": ticket,
            "error": result.comment
        })
        return {"success": False, "error": result.comment}
    
    def modify_sl_tp(self, ticket, new_sl, new_tp, direction=None, current_price=None, original_sl=None):
        """Modify stop loss or take profit.

        Optional safety params allow callers to enforce no-widen SL rules.
        """
        if direction and new_sl is not None and current_price is not None and original_sl is not None:
            is_valid, reason = self.is_valid_sl(direction, new_sl, current_price, original_sl)
            if not is_valid:
                log_event("SL Modify Blocked", {
                    "ticket": ticket,
                    "direction": str(direction).upper(),
                    "new_sl": new_sl,
                    "current_price": current_price,
                    "original_sl": original_sl,
                    "reason": reason,
                })
                return False

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl": new_sl,
            "tp": new_tp,
        }
        
        result = mt5.order_send(request)
        
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log_event("SL/TP Modified", {
                "ticket": ticket,
                "new_sl": new_sl,
                "new_tp": new_tp
            })
            return True
        else:
            log_event("Modify Failed", {"ticket": ticket, "error": result.comment})
            return False
