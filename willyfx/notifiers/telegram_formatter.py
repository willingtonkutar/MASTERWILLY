from monitoring.telegram_notifier import send_telegram_signal, telegram_is_configured


def _direction_icon(side):
    raw = str(side or 'neutral').lower()
    if raw == 'bullish':
        return '🟢'
    if raw == 'bearish':
        return '🔴'
    return '⚪'


def _direction_name(side):
    raw = str(side or 'neutral').lower()
    if raw == 'bullish':
        return 'Buyers'
    if raw == 'bearish':
        return 'Sellers'
    return 'Neutral'


def _format_range(level):
    if not level:
        return None
    low = level.get('low')
    high = level.get('high')
    if low is None or high is None:
        return None
    return f'{float(low):.2f} ─── {float(high):.2f}'


def _format_target(level):
    if not level or level.get('price') is None:
        return 'No objective yet'
    name = str(level.get('type') or 'internal_liquidity').replace('_', ' ').title()
    return f"{name} @ {float(level.get('price')):.2f}"


def _format_confidence(value):
    return f'{int(round(float(value or 0)))}%'


def _wait_lines(wait_steps):
    steps = wait_steps or []
    return [f'{index + 1}. {step}' for index, step in enumerate(steps)]


def format_analysis_message(symbol, bundle, timestamp=None):
    market_control = bundle.get('market_control') or {}
    recommendation = str(bundle.get('recommendation') or 'NO TRADES').upper()
    confidence = bundle.get('confidence', 0)
    liquidity = bundle.get('liquidity_model', {})
    draw = bundle.get('draw_on_liquidity', {})
    objective_text = bundle.get('objective_text') or 'No directional edge yet.'
    invalidation_text = bundle.get('invalidation_text') or 'Not defined yet.'
    wait_steps = bundle.get('wait_steps') or []
    market_shift = bundle.get('market_shift') or {}

    daily = bundle.get('higher_timeframes', {}).get('daily', {})
    h4 = bundle.get('higher_timeframes', {}).get('4h', {})
    m15 = bundle.get('focus_15m', {})

    lines = [f'<b>🤖 Trade with kutar | {symbol} MTF BRIEFING</b>']
    if timestamp:
        lines.append(f'Time: {timestamp}')
    lines.append('')
    lines.append('<b>========================</b>')
    lines.append('<b>MARKET DECISION</b>')
    lines.append('<b>========================</b>')
    lines.append('')
    lines.append('Action:')
    action_icon = '🟢' if recommendation == 'BUY ONLY' else '🔴' if recommendation == 'SELL ONLY' else '⚪'
    lines.append(f'{action_icon} {recommendation}')
    lines.append('')
    lines.append('Confidence:')
    lines.append(_format_confidence(confidence))

    lines.append('')
    lines.append('<b>========================</b>')
    lines.append('<b>WHY</b>')
    lines.append('<b>========================</b>')
    lines.append('')
    lines.append(f'✓ Daily : {_direction_icon(daily.get("direction"))} {_direction_name(daily.get("direction"))} {market_control.get("daily", {}).get("phrase", "")}')
    lines.append(f'✓ 4H    : {_direction_icon(h4.get("direction"))} {_direction_name(h4.get("direction"))} {market_control.get("4h", {}).get("phrase", "")}')
    lines.append(f'✓ 15M   : {_direction_icon(m15.get("direction"))} {_direction_name(m15.get("direction"))} {market_control.get("15m", {}).get("phrase", "")}')
    lines.append(f'✓ {market_control.get("overall", "Higher timeframe conflict. Trade smaller.")}')

    if market_control.get('daily', {}).get('reason'):
        lines.append(f'  Daily reason: {market_control["daily"]["reason"]}')
    if market_control.get('4h', {}).get('reason'):
        lines.append(f'  4H reason: {market_control["4h"]["reason"]}')
    if market_control.get('15m', {}).get('reason'):
        lines.append(f'  15M reason: {market_control["15m"]["reason"]}')

    lines.append('')
    lines.append('<b>========================</b>')
    lines.append('<b>TARGET</b>')
    lines.append('<b>========================</b>')
    lines.append('')
    if recommendation == 'BUY ONLY':
        lines.append(f'➡ {_format_target(draw.get("bullish_target"))}')
    elif recommendation == 'SELL ONLY':
        lines.append(f'➡ {_format_target(draw.get("bearish_target"))}')
    else:
        lines.append(objective_text)

    lines.append('')
    lines.append('<b>========================</b>')
    lines.append('<b>INVALIDATION</b>')
    lines.append('<b>========================</b>')
    lines.append('')
    lines.append(invalidation_text)

    lines.append('')
    lines.append('<b>========================</b>')
    lines.append('<b>WAIT FOR ON 1M</b>')
    lines.append('<b>========================</b>')
    lines.append('')
    for line in _wait_lines(wait_steps):
        lines.append(line)
    lines.append('')
    lines.append('ONLY after London or NY liquidity.')
    if recommendation == 'NO TRADES':
        lines.append('No directional edge yet.')

    if liquidity:
        lines.append('')
        lines.append('<b>========================</b>')
        lines.append('<b>LIQUIDITY MAP</b>')
        lines.append('<b>========================</b>')
        lines.append('')
        session_ranges = liquidity.get('session_ranges') or {}
        ordered = [
            ('NY Session', session_ranges.get('NEW_YORK')),
            ('Prev Day', liquidity.get('previous_day')),
            ('Asian Range', session_ranges.get('ASIAN')),
            ('Current Day', liquidity.get('current_day')),
        ]
        for label, level in ordered:
            rng = _format_range(level)
            if rng:
                lines.append(f'• [{label}]: {rng}')

    if market_shift:
        lines.append('')
        lines.append('<b>========================</b>')
        lines.append('<b>MARKET STRUCTURE SHIFT (MSS) DETECTED</b>')
        lines.append('<b>========================</b>')
        lines.append('')
        prev_overall = market_shift.get('previous_overall') or 'Unknown'
        curr_overall = market_shift.get('current_overall') or 'Unknown'
        prev_dir = market_shift.get('previous_direction') or 'Unknown'
        curr_dir = market_shift.get('current_direction') or 'Unknown'
        lines.append(f'• Overall Bias: {prev_overall} ➔ {curr_overall}')
        lines.append(f'• 15M Direction: {prev_dir} ➔ {curr_dir}')
        if market_shift.get('new_target'):
            lines.append(f'• Target Reset: {_format_target(market_shift.get("new_target"))}')
        if market_shift.get('previous_target_hit') is False and market_shift.get('previous_target'):
            lines.append(f'• Previous target not hit: {_format_target(market_shift.get("previous_target"))}')
        lines.append('• Notes: 15-minute structure shifted; target map reset.')

    return '\n'.join(lines)


async def send_analysis_telegram(symbol, bundle, timestamp=None):
    msg = format_analysis_message(symbol, bundle, timestamp=timestamp)
    if telegram_is_configured():
        send_telegram_signal(msg)
    return msg


def format_structure_shift_message(symbol, bundle, timestamp=None):
    lines = [f'<b>STRUCTURE SHIFT</b> {symbol}']
    if timestamp:
        lines.append(f'Time: {timestamp}')
    lines.append('')
    lines.append(bundle.get('message', 'Structure shifted'))
    for item in bundle.get('changes', []):
        lines.append(f'• {item}')
    if bundle.get('new_target'):
        target = bundle['new_target']
        lines.append(f'• New target: {target.get("type")} @ {target.get("price"):.2f}')
    return '\n'.join(lines)


async def send_structure_shift_telegram(symbol, bundle, timestamp=None):
    msg = format_structure_shift_message(symbol, bundle, timestamp=timestamp)
    if telegram_is_configured():
        send_telegram_signal(msg)
    return msg
