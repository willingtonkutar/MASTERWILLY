# ============================================================
#  STATE MANAGER - Persistent Bot Memory
# ============================================================

import json
import os
from datetime import datetime
from monitoring.logger import log_event


class StateManager:
    """
    Persistent state tracking across restarts.
    Survives VS Code crashes, restarts, system reboots.
    """
    
    STATE_FILE = "data/bot_state.json"
    
    def __init__(self):
        self.state = self._load_state()
    
    def _load_state(self):
        """Load persistent state from disk"""
        if os.path.exists(self.STATE_FILE):
            try:
                with open(self.STATE_FILE, "r") as f:
                    state = json.load(f)
                    log_event("STATE_LOADED", {"trades": len(state.get("open_trades", []))})
                    return state
            except Exception as e:
                log_event("ERROR", {"message": f"Failed to load state: {str(e)}"})
        
        # Fresh state
        return {
            "session_start": datetime.now().isoformat(),
            "today_date": datetime.now().strftime("%Y-%m-%d"),
            "open_trades": [],
            "closed_trades": [],
            "rejected_trades": [],  # PHASE 1 SHADOW LOGGING
            "trade_journal": {
                "by_setup": {},
                "by_session": {},
                "by_regime": {}
            },
            "daily_pnl": 0.0,
            "daily_loss": 0.0,
            "daily_wins": 0,
            "daily_losses": 0,
            "max_drawdown": 0.0,
            "last_signal": None,
            "last_entry_time": None,
            "directional_trade_streak": {
                "direction": None,
                "count": 0,
                "updated_at": None
            },
            "consecutive_losses": 0,
            "session_state": "RUNNING"
        }
    
    def _save_state(self):
        """Save state to disk"""
        os.makedirs(os.path.dirname(self.STATE_FILE) or ".", exist_ok=True)
        try:
            with open(self.STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            log_event("ERROR", {"message": f"Failed to save state: {str(e)}"})

    def _ensure_trade_journal(self):
        journal = self.state.setdefault("trade_journal", {})
        journal.setdefault("by_setup", {})
        journal.setdefault("by_session", {})
        journal.setdefault("by_regime", {})
        return journal

    def _update_journal_bucket(self, scope, key, is_win):
        if not key:
            return

        journal = self._ensure_trade_journal()
        scope_bucket = journal.setdefault(scope, {})
        item = scope_bucket.setdefault(key, {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0})
        item["trades"] += 1
        if is_win:
            item["wins"] += 1
        else:
            item["losses"] += 1

        item["win_rate"] = (item["wins"] / item["trades"]) * 100 if item["trades"] > 0 else 0.0

    def _record_trade_journal(self, trade, pnl):
        entry = trade.get("entry_conditions") or {}
        setup_tag = entry.get("setup_tag", "UNKNOWN_SETUP")
        session_tag = entry.get("session", "UNKNOWN_SESSION")
        regime_tag = entry.get("regime", "UNKNOWN_REGIME")
        is_win = pnl > 0

        self._update_journal_bucket("by_setup", setup_tag, is_win)
        self._update_journal_bucket("by_session", session_tag, is_win)
        self._update_journal_bucket("by_regime", regime_tag, is_win)
    
    def register_trade_open(self, ticket, symbol, direction, entry_price, lot, sl, tp, signal_data=None):
        """Register an open trade with entry conditions"""
        trade = {
            "ticket": ticket,
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "lot": lot,
            "sl": sl,
            "initial_sl": sl,
            "tp": tp,
            "open_time": datetime.now().isoformat(),
            "status": "OPEN",
            "trailing_active": False,
        }
        
        # STORE ENTRY CONDITIONS - for restart resilience
        if signal_data:
            trade["entry_conditions"] = {
                "score": signal_data.get("score", 0),
                "confidence": signal_data.get("confidence", "UNKNOWN"),
                "regime": signal_data.get("regime", "UNKNOWN"),
                "session": signal_data.get("session", "UNKNOWN"),
                "setup_tag": signal_data.get("setup_tag", "UNKNOWN_SETUP"),
                "reasons": signal_data.get("reasons", []),  # Why we entered
                "entry_timestamp": datetime.now().isoformat()
            }
        else:
            trade["entry_conditions"] = None
        
        self.state["open_trades"].append(trade)
        self.state["last_entry_time"] = datetime.now().isoformat()
        streak = self.state.setdefault("directional_trade_streak", {"direction": None, "count": 0, "updated_at": None})
        direction_key = str(direction or "").upper()
        if streak.get("direction") == direction_key:
            streak["count"] = int(streak.get("count", 0) or 0) + 1
        else:
            streak["direction"] = direction_key
            streak["count"] = 1
        streak["updated_at"] = datetime.now().isoformat()
        self._save_state()
        log_event("TRADE_REGISTERED", {"ticket": ticket, "direction": direction})
    
    def register_trade_close(self, ticket, close_price, pnl):
        """Register a closed trade"""
        # Find and close the trade
        for trade in self.state["open_trades"]:
            if trade["ticket"] == ticket:
                trade["status"] = "CLOSED"
                trade["close_price"] = close_price
                trade["close_time"] = datetime.now().isoformat()
                trade["pnl"] = pnl
                
                self.state["closed_trades"].append(trade)
                self.state["open_trades"].remove(trade)
                
                # Update daily stats
                self.state["daily_pnl"] += pnl
                if pnl >= 0:  # Breakeven (pnl=0) counts as a WIN
                    self.state["daily_wins"] += 1
                    self.state["consecutive_losses"] = 0
                else:
                    self.state["daily_losses"] += 1
                    self.state["consecutive_losses"] += 1
                    self.state["daily_loss"] += abs(pnl)
                
                # Update drawdown
                if self.state["daily_pnl"] < self.state["max_drawdown"]:
                    self.state["max_drawdown"] = self.state["daily_pnl"]

                self._record_trade_journal(trade, pnl)
                
                self._save_state()
                log_event("TRADE_CLOSED", {
                    "ticket": ticket,
                    "pnl": pnl,
                    "daily_pnl": self.state["daily_pnl"]
                })
                return True
        
        return False

    def reduce_trade_volume(self, ticket, closed_volume):
        """Reduce the tracked volume for a partially closed trade."""
        for trade in self.state["open_trades"]:
            if trade.get("ticket") == ticket:
                current_lot = float(trade.get("lot", 0.0) or 0.0)
                remaining_lot = max(0.0, current_lot - float(closed_volume or 0.0))
                trade["lot"] = remaining_lot
                trade["last_partial_close_time"] = datetime.now().isoformat()
                trade["partial_exit_taken"] = True
                self._save_state()
                return True, remaining_lot

        return False, None

    def mark_trade_partial_exit(self, ticket, label, details=None):
        """Store partial-exit metadata on an open trade."""
        for trade in self.state["open_trades"]:
            if trade.get("ticket") == ticket:
                trade.setdefault("partial_exits", [])
                trade["partial_exits"].append({
                    "label": label,
                    "details": details or {},
                    "time": datetime.now().isoformat(),
                })
                self._save_state()
                return True

        return False
    
    def register_signal(self, signal):
        """Remember last signal (for debugging)"""
        self.state["last_signal"] = {
            "direction": signal.get("direction"),
            "score": signal.get("score"),
            "confidence": signal.get("confidence"),
            "timestamp": datetime.now().isoformat()
        }
        self._save_state()
    
    def get_open_trades(self):
        """Get all open trades"""
        return self.state["open_trades"]
    
    def get_daily_stats(self):
        """Get today's performance"""
        return {
            "daily_pnl": self.state["daily_pnl"],
            "daily_loss": self.state["daily_loss"],
            "daily_wins": self.state["daily_wins"],
            "daily_losses": self.state["daily_losses"],
            "consecutive_losses": self.state["consecutive_losses"],
            "max_drawdown": self.state["max_drawdown"]
        }
    
    def reset_daily_stats(self):
        """Reset stats at midnight"""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self.state["today_date"]:
            self.state["today_date"] = today
            self.state["daily_pnl"] = 0.0
            self.state["daily_loss"] = 0.0
            self.state["daily_wins"] = 0
            self.state["daily_losses"] = 0
            self.state["max_drawdown"] = 0.0
            self.state["consecutive_losses"] = 0
            self._save_state()
            log_event("DAILY_STATS_RESET", {"date": today})
    
    def set_session_state(self, state):
        """Set session state (RUNNING, PAUSED, STOPPED)"""
        self.state["session_state"] = state
        self._save_state()

    def drop_stale_open_trade(self, ticket, reason="Missing on broker"):
        """Remove an open trade that no longer exists on broker side.

        This is a reconciliation cleanup and does not change win/loss stats.
        """
        for trade in list(self.state["open_trades"]):
            if trade.get("ticket") == ticket:
                self.state["open_trades"].remove(trade)
                self._save_state()
                log_event("STALE_TRADE_REMOVED", {
                    "ticket": ticket,
                    "reason": reason
                })
                return True

        return False
    
    def get_trade_entry_conditions(self, ticket):
        """Retrieve stored entry conditions for a trade (for restart validation)"""
        for trade in self.state["open_trades"]:
            if trade["ticket"] == ticket:
                return trade.get("entry_conditions")
        return None

    def get_trade_journal(self):
        """Get full adaptive trade journal stats."""
        return self._ensure_trade_journal()

    def get_directional_trade_streak(self):
        streak = self.state.get("directional_trade_streak", {}) or {}
        return {
            "direction": streak.get("direction"),
            "count": int(streak.get("count", 0) or 0),
            "updated_at": streak.get("updated_at"),
        }

    def set_directional_trade_streak(self, direction, count, updated_at=None):
        self.state["directional_trade_streak"] = {
            "direction": str(direction or "").upper() if direction else None,
            "count": int(count or 0),
            "updated_at": updated_at or datetime.now().isoformat(),
        }
        self._save_state()

    def get_adaptive_score_adjustment(self, setup_tag, session_tag=None, regime_tag=None):
        """Return score adjustment from historical setup/session/regime performance."""
        import config

        if not config.ENABLE_TRADE_JOURNAL:
            return 0.0, []

        journal = self._ensure_trade_journal()
        total_adjustment = 0.0
        reasons = []

        def apply_adjustment(scope_name, key):
            nonlocal total_adjustment
            if not key:
                return

            scope = journal.get(scope_name, {})
            stats = scope.get(key)
            if not stats:
                return

            trades = int(stats.get("trades", 0) or 0)
            win_rate = float(stats.get("win_rate", 0.0) or 0.0)

            if trades < config.JOURNAL_MIN_TRADES_FOR_ADAPTATION:
                return

            if win_rate < config.JOURNAL_LOW_WINRATE_THRESHOLD:
                total_adjustment += float(config.JOURNAL_SCORE_PENALTY)
                reasons.append(f"{scope_name}:{key} winrate {win_rate:.1f}% (penalty)")
            elif win_rate >= config.JOURNAL_HIGH_WINRATE_THRESHOLD:
                total_adjustment += float(config.JOURNAL_SCORE_BONUS)
                reasons.append(f"{scope_name}:{key} winrate {win_rate:.1f}% (bonus)")

        apply_adjustment("by_setup", setup_tag)
        apply_adjustment("by_session", session_tag)
        apply_adjustment("by_regime", regime_tag)

        return total_adjustment, reasons

    # ========================================================================
    # PHASE 1: SHADOW LOGGING FOR REJECTED TRADES
    # ========================================================================
    
    def register_rejected_signal(self, rejection_record):
        """Log a rejected signal for later outcome analysis.
        
        rejection_record should contain:
        - reason: rejection reason
        - direction: BUY/SELL
        - score: final score
        - confidence: LOW/MEDIUM/HIGH
        - setup_tag: setup identifier
        - regime: TREND/RANGE/UNKNOWN
        - session: LONDON/NEW_YORK/TOKYO/UNKNOWN
        - entry_price: price at rejection time
        - spread: current spread in pips
        - atr: current ATR
        """
        rejected = {
            "timestamp": datetime.now().isoformat(),
            "reason": str(rejection_record.get("reason", "UNKNOWN")),
            "direction": str(rejection_record.get("direction", "")).upper() or "UNKNOWN",
            "score": float(rejection_record.get("score", 0.0) or 0.0),
            "confidence": str(rejection_record.get("confidence", "UNKNOWN")).upper(),
            "setup_tag": str(rejection_record.get("setup_tag", "UNKNOWN_SETUP")),
            "regime": str(rejection_record.get("regime", "UNKNOWN")).upper(),
            "session": str(rejection_record.get("session", "UNKNOWN")).upper(),
            "entry_price": float(rejection_record.get("entry_price", 0.0) or 0.0),
            "spread": float(rejection_record.get("spread", 0.0) or 0.0),
            "atr": float(rejection_record.get("atr", 0.0) or 0.0),
            "outcomes": {
                "15m": None,
                "30m": None,
                "1h": None,
            },
            "resolved": False,
        }
        
        self.state.setdefault("rejected_trades", []).append(rejected)
        self._save_state()
        log_event("SHADOW_REJECTION_LOGGED", {
            "reason": rejected["reason"],
            "direction": rejected["direction"],
            "score": round(rejected["score"], 2),
            "timestamp": rejected["timestamp"],
        })
    
    def resolve_rejection_outcomes(self, current_price, current_time_minutes):
        """Update hypothetical outcomes for rejected signals.
        
        This is called periodically (e.g., every 15m) to update outcomes for all
        pending rejections. Tracks max favorable/adverse excursion, TP/SL hits.
        
        Args:
            current_price: current market price
            current_time_minutes: minutes elapsed since signal rejection (dict keyed by signal timestamp)
        """
        rejected_trades = self.state.get("rejected_trades", [])
        
        for rejected in rejected_trades:
            if rejected.get("resolved", False):
                continue
            
            entry = rejected.get("entry_price", 0.0)
            direction = str(rejected.get("direction", "")).upper()
            
            if direction not in {"BUY", "SELL"} or entry <= 0:
                continue
            
            signal_ts = rejected.get("timestamp", "")
            elapsed_minutes = current_time_minutes.get(signal_ts, 0)
            
            # Calculate pip movement
            pip_move = (current_price - entry) if direction == "BUY" else (entry - current_price)
            
            for horizon in ["15m", "30m", "1h"]:
                horizon_minutes = {"15m": 15, "30m": 30, "1h": 60}[horizon]
                
                # Skip if not enough time has passed
                if elapsed_minutes < horizon_minutes:
                    continue
                
                # Only update if not already recorded for this horizon
                if rejected["outcomes"][horizon] is not None:
                    continue
                
                # Record outcome
                rejected["outcomes"][horizon] = {
                    "timestamp": datetime.now().isoformat(),
                    "current_price": float(current_price),
                    "pip_movement": float(pip_move),
                    "r_multiple": float(pip_move / rejected.get("atr", 1.0)) if rejected.get("atr", 0) > 0 else 0.0,
                }
            
            # Mark as resolved if all horizons completed
            if all(rejected["outcomes"].get(h) is not None for h in ["15m", "30m", "1h"]):
                rejected["resolved"] = True
        
        self._save_state()
    
    def get_rejection_summary(self):
        """Get statistics on rejected signals."""
        rejected_trades = self.state.get("rejected_trades", [])
        
        if not rejected_trades:
            return {
                "total_rejected": 0,
                "resolved": 0,
                "pending": 0,
                "by_reason": {},
                "by_direction": {},
                "avg_outcome_15m": None,
                "avg_outcome_30m": None,
                "avg_outcome_1h": None,
            }
        
        resolved = [r for r in rejected_trades if r.get("resolved", False)]
        
        by_reason = {}
        by_direction = {}
        
        for r in rejected_trades:
            reason = str(r.get("reason", "UNKNOWN"))
            direction = str(r.get("direction", "UNKNOWN"))
            
            by_reason[reason] = by_reason.get(reason, 0) + 1
            by_direction[direction] = by_direction.get(direction, 0) + 1
        
        # Calculate average outcomes
        avg_15m = None
        avg_30m = None
        avg_1h = None
        
        outcomes_15m = [r["outcomes"]["15m"]["pip_movement"] for r in resolved if r["outcomes"]["15m"]]
        outcomes_30m = [r["outcomes"]["30m"]["pip_movement"] for r in resolved if r["outcomes"]["30m"]]
        outcomes_1h = [r["outcomes"]["1h"]["pip_movement"] for r in resolved if r["outcomes"]["1h"]]
        
        if outcomes_15m:
            avg_15m = sum(outcomes_15m) / len(outcomes_15m)
        if outcomes_30m:
            avg_30m = sum(outcomes_30m) / len(outcomes_30m)
        if outcomes_1h:
            avg_1h = sum(outcomes_1h) / len(outcomes_1h)
        
        return {
            "total_rejected": len(rejected_trades),
            "resolved": len(resolved),
            "pending": len(rejected_trades) - len(resolved),
            "by_reason": by_reason,
            "by_direction": by_direction,
            "avg_outcome_15m": avg_15m,
            "avg_outcome_30m": avg_30m,
            "avg_outcome_1h": avg_1h,
        }
