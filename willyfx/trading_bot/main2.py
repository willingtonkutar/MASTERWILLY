# ============================================================
#  MAIN2 - SIGNAL BOT (Telegram Alerts Only)
# ============================================================

import time

from broker.mt5_client import connect_mt5, disconnect_mt5, get_spread
from data.feed import get_market_data
from risk.risk_manager import RiskManager
from state.state_manager import StateManager
from strategy.strategy_engine import InstitutionalStrategyEngine
from strategy.regime_detector import RegimeDetector
from strategy.smc import SMCAnalyzer
from data.validation_layer import DataValidator
from ai.claude import analyze_market_context, refine_signal
from monitoring.logger import log_event
from monitoring.telegram_notifier import escape_telegram_html, send_telegram_signal, telegram_is_configured
from monitoring.startup_reporter import run_startup_analysis
from main import (
    calculate_indicators,
    get_last_candle_key,
    build_structure_key,
    sync_mt5_positions_with_state,
    is_high_potential_signal,
    evaluate_session_policy,
    build_setup_tag,
    get_dxy_trend,
    get_silver_trend,
    apply_dxy_correlation,
    apply_silver_correlation,
    describe_dxy_alignment,
    describe_silver_alignment,
    apply_adaptive_journal_weight,
    get_dynamic_risk_multiplier,
    get_correlation_score_adjustment,
    evaluate_directional_cooldown,
    validate_trade_decision,
    emit_decision_validation_debug,
    register_signal_signature,
    print_claude_session_summary,
)
import config


def format_signal_message(signal, market_context, regime_data, entry_price, sl, tp, spread, risk_multiplier, risk_notes, execution_cooldown_label=None, trade_type="NORMAL", trade_stage="MID TREND", protection_labels=None, execution_block_reason=None):
    symbol_label = "GOLD" if str(config.SYMBOL).upper() == "XAUUSD" else config.SYMBOL
    observations = market_context.get("observations") or []
    observation_text = " ".join(observations).strip() or "No extra Claude observation returned."
    validation_text = signal.get("claude_validation") or "No Claude validation returned."
    breakdown = signal.get("breakdown", {})
    breakdown_text = ", ".join(f"{key}: {value:+}" for key, value in breakdown.items()) if breakdown else "N/A"
    dxy_trend = market_context.get("dxy_trend")
    dxy_alignment = describe_dxy_alignment(signal.get("direction"), dxy_trend)
    silver_alignment = describe_silver_alignment(
        signal.get("direction"),
        market_context.get("silver_trend"),
        market_context.get("silver_momentum_state"),
    )
    yields_trend = str(market_context.get("yields_trend", "Neutral")).replace("_", " ")
    yields_display = yields_trend if bool(getattr(config, "ENABLE_YIELDS_CONTEXT", False)) else "DISABLED"
    correlation_score = get_correlation_score_adjustment(signal)

    if correlation_score >= 2 and abs(float(signal.get("score", 0) or 0)) >= float(config.MIN_SIGNAL_SCORE_TELEGRAM) + 2:
        trade_quality = "STRONG"
    elif correlation_score <= float(config.CORRELATION_REDUCED_RISK_SCORE):
        trade_quality = "WEAK"
    else:
        trade_quality = "MODERATE"

    protection_text = ", ".join(str(item) for item in (protection_labels or [])) if protection_labels else "NONE"

    message = f"""
🔔 NEW {symbol_label} SIGNAL 🔔

📈 Action: {escape_telegram_html(signal.get('direction'))}
📍 Entry: {entry_price:.2f}
🎯 Take Profit: {tp:.2f}
🛑 Stop Loss: {sl:.2f}

🧠 Claude Summary: {escape_telegram_html(market_context.get('bias', 'NEUTRAL'))} | {escape_telegram_html(market_context.get('momentum', 'UNKNOWN'))} | {escape_telegram_html(observation_text)} | Confidence {escape_telegram_html(market_context.get('confidence', 0))}
🧠 Claude Validation: {escape_telegram_html(validation_text)}
📊 Confluence Score: {signal.get('score', 0):.1f}
📌 Breakdown: {escape_telegram_html(breakdown_text)}
🌐 Regime: {escape_telegram_html(regime_data.get('regime'))}
🕒 Session: {escape_telegram_html(signal.get('session', 'UNKNOWN'))}
🧩 Trade Type: {escape_telegram_html(str(trade_type or 'NORMAL'))}
🧭 Trade Stage: {escape_telegram_html(trade_stage)}
🛡️ Protections: {escape_telegram_html(protection_text)}
⚖️ Risk Multiplier: x{risk_multiplier:.2f}
🧩 Risk Notes: {escape_telegram_html('; '.join(risk_notes))}
📏 Spread: {spread}

🔍 CROSS-MARKET CHECK
• DXY (USD): {escape_telegram_html(dxy_alignment)}
• Yields: {escape_telegram_html(yields_display)}
• Silver (XAGUSD): {escape_telegram_html(silver_alignment)}
• Correlation Score: {correlation_score:+.2f}
• Trade Quality: {escape_telegram_html(trade_quality)}
""".strip()

    if execution_cooldown_label:
        message += f"\n• Execution Cooldown: {escape_telegram_html(execution_cooldown_label)}"

    if execution_block_reason:
        message += f"\n• Execution Gate: {escape_telegram_html(execution_block_reason)}"
        if bool(getattr(config, "ENABLE_TELEGRAM_REJECTION_DEBUG", False)):
            message += f"\n• Rejection Debug: {escape_telegram_html(execution_block_reason)}"

    return message


def print_signal_bot_summary():
    print("\n┌──────────────────────────────┐")
    print("│     SIGNAL BOT SUMMARY       │")
    print("├──────────────────────────────┤")
    print(f"│ Symbol      : {config.SYMBOL:<14} │")
    print(f"│ Timeframe   : {config.TIMEFRAME:<14} │")
    print(f"│ Telegram    : {str(telegram_is_configured()):<14} │")
    print(f"│ Claude      : {str(config.ENABLE_CLAUDE):<14} │")
    print("└──────────────────────────────┘\n")


def send_startup_test_message():
    if not telegram_is_configured() or not config.TELEGRAM_SEND_STARTUP_TEST:
        return

    now_local = time.strftime("%Y-%m-%d %H:%M:%S")
    session_policy = evaluate_session_policy()
    session_tag = str(session_policy.get("session_tag", "UNKNOWN")).replace("_", " ")
    test_message = (
        "<b>🚀 INSTITUTIONAL SIGNAL BOT — LIVE</b>\n"
        "<i>Time to make money, Willington.</i>\n\n"
        "<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
        "<b>📡 STATUS</b>\n"
        "• Mode: <b>Signal Alerts Only</b>\n"
        "• Execution: <b>Disabled</b>\n"
        "• Market Scan: <b>Active</b>\n"
        "• Claude Context: <b>Online</b>\n\n"
        "<b>📈 SESSION SNAPSHOT</b>\n"
        f"• Symbol: <b>{escape_telegram_html(config.SYMBOL)}</b>\n"
        f"• Timeframe: <b>{escape_telegram_html(config.TIMEFRAME)}</b>\n"
        f"• Session: <b>{escape_telegram_html(session_tag)}</b>\n"
        f"• Start Time: <code>{escape_telegram_html(now_local)}</code>\n\n"
        "<b>🧠 ANALYSIS STACK</b>\n"
        "• Structure: <b>SMC / BOS / CHOCH</b>\n"
        "• Momentum: <b>Validated</b>\n"
        "• Risk Filter: <b>Armed</b>\n"
        "• Signal Quality: <b>Selective</b>\n\n"
        "<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
        "I will stay silent until a clean setup appears.\n"
        "When it does, you will get entry, SL, TP, and context immediately."
    )

    ok = send_telegram_signal(test_message)
    if ok:
        print("📨 Startup Telegram test message sent")
    else:
        print("⚠️  Startup Telegram test message failed")


def run_signal_bot():
    """Main loop for signal-only Telegram alerts."""

    print("=" * 60)
    print("📣 INSTITUTIONAL SIGNAL BOT v1.0")
    print("=" * 60)
    print(f"Symbol: {config.SYMBOL}")
    print(f"Timeframe: {config.TIMEFRAME}")
    print(f"Telegram Enabled: {telegram_is_configured()}")
    print(f"Claude Enabled: {config.ENABLE_CLAUDE}")
    print("=" * 60)
    print_signal_bot_summary()

    log_event("SIGNAL_BOT_START", {
        "symbol": config.SYMBOL,
        "strategy": config.STRATEGY_TYPE,
        "telegram_enabled": telegram_is_configured(),
        "autotrade": False
    })

    if not connect_mt5():
        print("❌ Failed to connect to MT5")
        log_event("ERROR", {"message": "MT5 connection failed"})
        return

    if not telegram_is_configured():
        print("⚠️  Telegram is not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")

    print("✅ Connected to MT5\n")
    try:
        run_startup_analysis(send_telegram=True)
    except Exception as exc:
        log_event("STARTUP_ANALYSIS_ERROR", {"error": str(exc)})
    send_startup_test_message()

    last_news_report_ts = time.time()

    risk = RiskManager()
    state = StateManager()
    strategy = InstitutionalStrategyEngine()
    regime = RegimeDetector()
    smc = SMCAnalyzer()
    data_validator = DataValidator()

    sync_mt5_positions_with_state(state)

    iteration = 0
    last_status_print = 0.0
    last_candle_key = None
    last_structure_key = None
    last_regime_state = None
    last_status_snapshot = None
    last_block_reason = None
    last_block_log_time = 0.0
    last_telegram_signature = None
    recent_sweep_history = []
    regime_candle_history = []

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

            state.reset_daily_stats()
            risk.open_trades = len(state.get_open_trades())
            can_trade, risk_reason = risk.can_trade(include_open_limit=False)
            session_policy = evaluate_session_policy()

            if not can_trade:
                now = time.time()
                if risk_reason != last_block_reason or (now - last_block_log_time) >= config.BLOCKED_LOG_COOLDOWN_SECS:
                    print(f"⛔ [{iteration}] Blocked: {risk_reason}")
                    last_block_reason = risk_reason
                    last_block_log_time = now

            if config.ENABLE_DATA_VALIDATION:
                rates = get_market_data()
                is_valid, error = data_validator.validate_rates(rates)
                if not is_valid:
                    print(f"⚠️  Data invalid: {error}")
                    log_event("DATA_ERROR", {"error": error})
                    time.sleep(config.MONITOR_INTERVAL)
                    continue
            else:
                rates = get_market_data()

            if rates is None or len(rates) < 50:
                print(f"⚠️  [{iteration}] Insufficient data")
                time.sleep(config.MONITOR_INTERVAL)
                continue

            candle_key = get_last_candle_key(rates)

            df = calculate_indicators(rates)
            is_valid, error = data_validator.validate_indicators(df)
            if not is_valid:
                print(f"⚠️  Indicators invalid: {error}")
                time.sleep(config.MONITOR_INTERVAL)
                continue

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
                if time.time() - regime.last_regime_log > config.REGIME_LOG_COOLDOWN:
                    print(f"⚠️  Regime unfavorable: {regime_reason}")
                    regime.last_regime_log = time.time()
                time.sleep(config.MONITOR_INTERVAL)
                continue

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
            if len(regime_candle_history) >= 3 and regime_candle_history[-2] != regime_candle_history[-3]:
                regime_recently_changed = True

            if new_candle_closed:
                if smc_data.get("bull_sweep"):
                    recent_sweep_history.append({"candle_time": candle_key, "direction": "BUY"})
                elif smc_data.get("bear_sweep"):
                    recent_sweep_history.append({"candle_time": candle_key, "direction": "SELL"})
                else:
                    recent_sweep_history.append({"candle_time": candle_key, "direction": None})

                recent_sweep_history = recent_sweep_history[-3:]

            if not new_candle_closed and not structure_changed:
                last_candle_key = candle_key
                last_structure_key = structure_key
                time.sleep(config.MONITOR_INTERVAL)
                continue

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
                    time.sleep(config.MONITOR_INTERVAL)
                    continue

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
                        "mode": "signal",
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
                emit_decision_validation_debug(iteration, "signal", signal, decision_validation)
                if not decision_validation.get("allowed", False):
                    print(f"⚠️  [{iteration}] {decision_validation.get('reason')}")
                    log_event("SIGNAL_SKIPPED", {
                        "reason": decision_validation.get("reason"),
                        "mode": "signal",
                        "direction": signal.get("direction"),
                        "score": signal.get("score"),
                        "confidence": signal.get("confidence"),
                    })
                    if bool(getattr(config, "ENABLE_TELEGRAM_REJECTION_DEBUG", False)) and telegram_is_configured():
                        debug_message = (
                            "⚠️ SIGNAL REJECTED\n"
                            f"• Reason: {escape_telegram_html(str(decision_validation.get('reason', 'Unknown')))}\n"
                            f"• Direction: {escape_telegram_html(str(signal.get('direction', 'N/A')))}\n"
                            f"• Score: {float(signal.get('score', 0.0) or 0.0):.1f}\n"
                            f"• Confidence: {escape_telegram_html(str(signal.get('confidence', 'N/A')))}"
                        )
                        send_telegram_signal(debug_message)
                    time.sleep(config.MONITOR_INTERVAL)
                    continue

                should_enter, enter_reason = strategy.should_enter_trade(signal)

                if should_enter:
                    if not can_trade:
                        now = time.time()
                        gate_reason = risk_reason if risk_reason else "Risk gate active"
                        if gate_reason != last_block_reason or (now - last_block_log_time) >= config.BLOCKED_LOG_COOLDOWN_SECS:
                            print(f"⛔ [{iteration}] Signal blocked: {gate_reason}")
                            last_block_reason = gate_reason
                            last_block_log_time = now
                        time.sleep(config.MONITOR_INTERVAL)
                        continue

                    open_trades = state.get_open_trades()
                    open_count = len(open_trades)
                    high_potential = is_high_potential_signal(signal)
                    allowed_open_trades = config.MAX_OPEN_TRADES

                    if config.ENABLE_HIGH_POTENTIAL_SECOND_TRADE and high_potential:
                        allowed_open_trades = max(
                            config.MAX_OPEN_TRADES,
                            config.MAX_OPEN_TRADES_HIGH_POTENTIAL
                        )

                    if open_count >= allowed_open_trades:
                        now = time.time()
                        block_reason = f"Max open trades reached: {open_count}"
                        if block_reason != last_block_reason or (now - last_block_log_time) >= config.BLOCKED_LOG_COOLDOWN_SECS:
                            print(f"⚠️  [{iteration}] Signal generated but already have {open_count} open trade(s)")
                            print(f"   Allowed now: {allowed_open_trades} | High potential: {high_potential}")
                            last_block_reason = block_reason
                            last_block_log_time = now
                        time.sleep(config.MONITOR_INTERVAL)
                        continue

                    spread = get_spread(config.SYMBOL)
                    is_valid_spread, spread_error = data_validator.validate_spread(spread)
                    if not is_valid_spread:
                        print(f"❌ {spread_error}")
                        time.sleep(config.MONITOR_INTERVAL)
                        continue

                    atr = df["atr"].iloc[-1]
                    entry_price = df.iloc[-1]["close"]

                    risk_multiplier, risk_notes = get_dynamic_risk_multiplier(
                        signal,
                        daily_stats=state.get_daily_stats(),
                        session_risk_multiplier=session_policy.get("risk_multiplier", 1.0),
                        correlation_score_adjustment=correlation_score_adjustment,
                    )
                    validation_risk_adj = float(decision_validation.get("risk_multiplier_adjustment", 1.0) or 1.0)
                    if validation_risk_adj != 1.0:
                        risk_multiplier *= validation_risk_adj
                        risk_notes.append(f"Decision validation multiplier x{validation_risk_adj:.2f}")

                    if bool(decision_validation.get("early_exit_enabled", False)):
                        signal["early_exit_enabled"] = True

                    trade_stage = decision_validation.get("trade_stage", "MID TREND")
                    trade_type = decision_validation.get("trade_type", "NORMAL")
                    protection_labels = decision_validation.get("labels", [])
                    execution_block_reason = decision_validation.get("execution_block_reason")

                    if risk_multiplier <= 0:
                        print(f"⚠️  [{iteration}] Dynamic risk model skipped signal: {', '.join(risk_notes)}")
                        time.sleep(config.MONITOR_INTERVAL)
                        continue

                    tp_rr = float(config.TP_RR_HIGH)
                    if str(getattr(config, "STRATEGY_TYPE", "")).lower() == "scalp":
                        tp_rr = max(1.2, min(1.8, tp_rr))

                    validation_tp_adj = float(decision_validation.get("tp_multiplier_adjustment", 1.0) or 1.0)
                    if validation_tp_adj != 1.0:
                        tp_rr *= validation_tp_adj
                        risk_notes.append(f"HTF conflict TP multiplier x{validation_tp_adj:.2f}")

                    if signal["direction"] == "BUY":
                        sl = entry_price - (atr * config.SL_ATR_MULTIPLIER)
                        tp = entry_price + (atr * config.SL_ATR_MULTIPLIER * tp_rr)
                    else:
                        sl = entry_price + (atr * config.SL_ATR_MULTIPLIER)
                        tp = entry_price - (atr * config.SL_ATR_MULTIPLIER * tp_rr)

                    signal_signature = (
                        candle_key,
                        signal.get("direction"),
                        round(float(entry_price), 2),
                        round(float(sl), 2),
                        round(float(tp), 2)
                    )

                    execution_cooldown_label = None
                    cooldown_preview = evaluate_directional_cooldown(
                        direction=signal.get("direction"),
                        regime=regime_data.get("regime"),
                        score=signal.get("score", 0.0),
                        correlation_score=correlation_score_adjustment,
                        confidence=signal.get("confidence", "LOW"),
                        state=state,
                        now_ts=time.time(),
                    )
                    if cooldown_preview.get("blocked"):
                        remaining_min = float(cooldown_preview.get("remaining_secs", 0.0)) / 60.0
                        execution_cooldown_label = f"Would block execution ({remaining_min:.1f}m left)"

                    if signal_signature != last_telegram_signature:
                        message = format_signal_message(
                            signal,
                            claude_context,
                            regime_data,
                            entry_price,
                            sl,
                            tp,
                            spread,
                            risk_multiplier,
                            risk_notes,
                            execution_cooldown_label,
                            trade_type,
                            trade_stage,
                            protection_labels,
                            execution_block_reason,
                        )

                        if send_telegram_signal(message):
                            last_telegram_signature = signal_signature
                            state.register_signal(signal)
                            register_signal_signature(
                                config.SYMBOL,
                                decision_validation.get("signature"),
                                now_ts=time.time(),
                            )
                            print(f"\n📣 [{iteration}] Telegram signal sent")
                            print(f"   Direction: {signal['direction']}")
                            print(f"   Score: {signal['score']:.1f}")
                            print(f"   Confidence: {signal['confidence']}")
                            print(f"   Regime: {regime_data['regime']}")
                        else:
                            print(f"❌ Failed to send Telegram signal")
                    else:
                        print(f"ℹ️  Duplicate signal skipped for candle {candle_key}")

            stats = state.get_daily_stats()
            status_snapshot = (
                stats["daily_pnl"],
                stats["daily_wins"],
                stats["daily_losses"],
                stats["max_drawdown"],
                regime_data.get("regime"),
                len(state.get_open_trades())
            )

            if status_snapshot != last_status_snapshot and time.time() - last_status_print >= config.STATUS_PRINT_INTERVAL_SECS:
                print(f"\n📊 Status | PnL: {stats['daily_pnl']:+.2f} | Wins: {stats['daily_wins']} | Losses: {stats['daily_losses']} | DD: {stats['max_drawdown']:.2f}")
                print(f"   Regime: {regime_data['regime']} | Open: {len(state.get_open_trades())}\n")
                last_status_print = time.time()
                last_status_snapshot = status_snapshot

            last_candle_key = candle_key
            last_structure_key = structure_key
            time.sleep(config.MONITOR_INTERVAL)

    except KeyboardInterrupt:
        print("\n\n⏹️  Signal bot stopped by user")
        log_event("SIGNAL_BOT_STOPPED", {"reason": "User interrupt", "iterations": iteration})

    except Exception as exc:
        print(f"\n❌ Error: {str(exc)}")
        log_event("SIGNAL_BOT_ERROR", {"error": str(exc), "iteration": iteration})
        import traceback
        traceback.print_exc()

    finally:
        print_claude_session_summary()
        disconnect_mt5()
        print("✅ MT5 disconnected")
        print("=" * 60)
        print("🛑 SIGNAL BOT SHUTDOWN")
        print("=" * 60)


if __name__ == "__main__":
    run_signal_bot()