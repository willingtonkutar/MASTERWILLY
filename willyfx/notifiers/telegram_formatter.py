from monitoring.telegram_notifier import send_telegram_signal, telegram_is_configured


def format_analysis_message(symbol, bundle, timestamp=None):
    higher = bundle.get('higher_timeframes', {})
    focus = bundle.get('focus_15m', {})
    liquidity = bundle.get('liquidity_model', {})
    guidance = bundle.get('guidance', {})

    lines = [f"<b>{symbol} MTF BRIEFING</b>"]
    if timestamp:
        lines.append(f"Time: {timestamp}")
    lines.append('')
    lines.append('<b>Higher Timeframes</b>')
    for label in ('daily', '1h'):
        tf = higher.get(label, {})
        lines.append(f"• {label.upper()}: {tf.get('direction', 'neutral')} | conf {tf.get('confidence', 0)}")
        if tf.get('reasons'):
            lines.append(f"  Reason: {tf.get('reasons')[0]}")
    lines.append('')
    lines.append('<b>15M Focus</b>')
    lines.append(f"• Direction: {focus.get('direction', 'neutral')}")
    if focus.get('structure_reason'):
        lines.append(f"• Structure reason: {focus.get('structure_reason')}")
    if focus.get('reasons'):
        lines.append(f"• Reasons: {', '.join(focus.get('reasons')[:3])}")
    if focus.get('primary_liquidity'):
        primary = focus['primary_liquidity']
        lines.append(f"• Primary liquidity target: {primary.get('type')} @ {primary.get('price'):.2f}")
    if focus.get('reversal_liquidity'):
        reversal = focus['reversal_liquidity']
        lines.append(f"• If taken and reversed, next liquidity: {reversal.get('type')} @ {reversal.get('price'):.2f}")
    if focus.get('execution_guidance'):
        lines.append(f"• 1M guidance: {focus.get('execution_guidance')}")

    lines.append('')
    lines.append('<b>Liquidity Map</b>')
    current_session = liquidity.get('current_session', {})
    previous_session = liquidity.get('previous_session', {})
    current_day = liquidity.get('current_day', {})
    previous_day = liquidity.get('previous_day', {})
    lines.append(f"• Session: {liquidity.get('session', 'UNKNOWN')}")
    if current_session.get('high') is not None and current_session.get('low') is not None:
        lines.append(f"• Current session range: {current_session.get('low'):.2f} - {current_session.get('high'):.2f}")
    if previous_session.get('high') is not None and previous_session.get('low') is not None:
        lines.append(f"• Previous session range: {previous_session.get('low'):.2f} - {previous_session.get('high'):.2f}")
    if current_day.get('high') is not None and current_day.get('low') is not None:
        lines.append(f"• Current day range: {current_day.get('low'):.2f} - {current_day.get('high'):.2f}")
    if previous_day.get('high') is not None and previous_day.get('low') is not None:
        lines.append(f"• Previous day range: {previous_day.get('low'):.2f} - {previous_day.get('high'):.2f}")

    lines.append('')
    lines.append('<b>Bias Logic</b>')
    if guidance.get('summary'):
        lines.append(f"• {guidance.get('summary')}")
    if guidance.get('if_bullish'):
        lines.append(f"• If bullish: {guidance.get('if_bullish')}")
    if guidance.get('if_bearish'):
        lines.append(f"• If bearish: {guidance.get('if_bearish')}")

    return '\n'.join(lines)


async def send_analysis_telegram(symbol, bundle, timestamp=None):
    msg = format_analysis_message(symbol, bundle, timestamp=timestamp)
    if telegram_is_configured():
        send_telegram_signal(msg)
    else:
        print(msg)


def format_structure_shift_message(symbol, bundle, timestamp=None):
    lines = [f"<b>STRUCTURE SHIFT</b> {symbol}"]
    if timestamp:
        lines.append(f"Time: {timestamp}")
    lines.append('')
    lines.append(bundle.get('message', 'Structure shifted'))
    for item in bundle.get('changes', []):
        lines.append(f"• {item}")
    if bundle.get('new_target'):
        target = bundle['new_target']
        lines.append(f"• New target: {target.get('type')} @ {target.get('price'):.2f}")
    return '\n'.join(lines)


async def send_structure_shift_telegram(symbol, bundle, timestamp=None):
    msg = format_structure_shift_message(symbol, bundle, timestamp=timestamp)
    if telegram_is_configured():
        send_telegram_signal(msg)
    else:
        print(msg)
