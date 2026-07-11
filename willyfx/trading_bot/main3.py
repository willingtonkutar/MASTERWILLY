"""main3.py

Multi-timeframe market brief that focuses on daily, 1H, and 15M context,
session-specific liquidity, and duplicate-suppressed Telegram updates.
"""

import asyncio
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
AI_ROOT = PROJECT_ROOT / 'ai'
if str(AI_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ROOT))

import config
from broker.mt5_client import connect_mt5, disconnect_mt5
from data.feed import get_market_data
from analysis import TimeframeAnalyzer, LiquidityMapper, TargetCalculator
from analysis.state_store import load_state, save_state
from claude_adapter import ClaudeAdapter
from notifiers.telegram_formatter import send_analysis_telegram


def _stable_signature(payload):
    encoded = json.dumps(payload, sort_keys=True, default=str).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _pick_guidance(direction):
    if direction == 'bullish':
        return 'look for buys on the 1-minute timeframe'
    if direction == 'bearish':
        return 'look for sells on the 1-minute timeframe'
    return 'wait for clear 15-minute structure before taking 1-minute entries'


def _rates_to_frame(rates):
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates).copy()
    if 'time' in df.columns:
        df['time'] = pd.to_datetime(df['time'], unit='s', errors='coerce', utc=True)
    return df.dropna(subset=['open', 'high', 'low', 'close'])


def _closed_rates(rates):
    if rates is None or len(rates) <= 1:
        return rates
    return rates[:-1]


def _latest_candle_key(rates):
    if rates is None or len(rates) == 0:
        return None
    latest = rates[-1]
    try:
        return str(int(latest['time']))
    except Exception:
        try:
            return str(latest['time'])
        except Exception:
            return None


def _normalize_direction(direction):
    raw = str(direction or 'neutral').lower()
    if raw.startswith('slight_'):
        return raw.replace('slight_', '', 1)
    return raw


def _level_name(level):
    if not level:
        return None
    return str(level.get('type') or level.get('name') or 'liquidity_level')


def _detect_sweeps(df, liquidity_model):
    if df is None or df.empty:
        return []

    last = df.iloc[-1]
    high = float(last['high'])
    low = float(last['low'])
    close = float(last['close'])

    sweep_points = []
    named_levels = []

    if liquidity_model:
        current_session = liquidity_model.get('current_session') or {}
        previous_session = liquidity_model.get('previous_session') or {}
        current_day = liquidity_model.get('current_day') or {}
        previous_day = liquidity_model.get('previous_day') or {}
        session_ranges = liquidity_model.get('session_ranges') or {}

        named_levels.extend([
            ('Current Session High', current_session.get('high')),
            ('Current Session Low', current_session.get('low')),
            ('Previous Session High', previous_session.get('high')),
            ('Previous Session Low', previous_session.get('low')),
            ('Current Day High', current_day.get('high')),
            ('Current Day Low', current_day.get('low')),
            ('Previous Day High', previous_day.get('high')),
            ('Previous Day Low', previous_day.get('low')),
        ])

        asian_range = session_ranges.get('ASIAN') or {}
        london_range = session_ranges.get('LONDON') or {}
        ny_range = session_ranges.get('NEW_YORK') or {}
        overlap_range = session_ranges.get('LONDON_NY_OVERLAP') or {}
        for label, rng in (
            ('Asian High', asian_range.get('high')),
            ('Asian Low', asian_range.get('low')),
            ('London High', london_range.get('high')),
            ('London Low', london_range.get('low')),
            ('NY High', ny_range.get('high')),
            ('NY Low', ny_range.get('low')),
            ('LNY Overlap High', overlap_range.get('high')),
            ('LNY Overlap Low', overlap_range.get('low')),
        ):
            named_levels.append((label, rng))

    seen = set()
    for label, price in named_levels:
        if price is None:
            continue
        price_value = float(price)
        if high > price_value >= close and label not in seen:
            sweep_points.append({'label': label, 'side': 'bearish', 'price': price_value, 'message': f'⚠️ {label} swept at {price_value:.2f}'})
            seen.add(label)
        elif low < price_value <= close and label not in seen:
            sweep_points.append({'label': label, 'side': 'bullish', 'price': price_value, 'message': f'⚠️ {label} swept at {price_value:.2f}'})
            seen.add(label)

    return sweep_points


def _summarize_levels(direction, liquidity_model, analysis_focus):
    if not liquidity_model:
        return None

    direction_key = _normalize_direction(direction)
    primary = analysis_focus.get('primary_liquidity') or {}
    invalidation = analysis_focus.get('invalidation_level') or {}
    reversal = analysis_focus.get('reversal_liquidity') or {}
    session_ranges = liquidity_model.get('session_ranges') or {}

    def _session_target(name):
        level = session_ranges.get(name) or {}
        if not level:
            return None
        return {'type': f'{name.title()} Range', 'price': float(level.get('high') if direction_key == 'bullish' else level.get('low'))}

    if direction_key == 'bullish' and not primary:
        primary = _session_target('ASIAN') or _session_target('NEW_YORK') or _session_target('LONDON') or {}
    if direction_key == 'bearish' and not primary:
        primary = _session_target('ASIAN') or _session_target('NEW_YORK') or _session_target('LONDON') or {}

    return {
        'bullish_target': primary if direction_key == 'bullish' else reversal,
        'bearish_target': primary if direction_key == 'bearish' else reversal,
        'invalidation': invalidation,
    }


def _normalize_trade_side(direction):
    raw = _normalize_direction(direction)
    if raw in {'bullish', 'bearish'}:
        return raw
    return 'neutral'


def _control_word(direction):
    side = _normalize_trade_side(direction)
    if side == 'bullish':
        return 'Buyers'
    if side == 'bearish':
        return 'Sellers'
    return 'Neutral'


def _control_phrase(direction, reference_direction=None):
    side = _normalize_trade_side(direction)
    if side == 'bullish':
        if _normalize_trade_side(reference_direction) == 'bearish':
            return 'Buyers correcting'
        return 'Buyers in control'
    if side == 'bearish':
        if _normalize_trade_side(reference_direction) == 'bullish':
            return 'Sellers correcting'
        return 'Sellers in control'
    return 'Neutral'


def _overall_market_message(daily_side, h4_side, m15_side):
    aligned = [side for side in (daily_side, h4_side, m15_side) if side in {'bullish', 'bearish'}]
    bull_count = sum(1 for side in aligned if side == 'bullish')
    bear_count = sum(1 for side in aligned if side == 'bearish')
    if bull_count >= 2 and bull_count > bear_count:
        return 'Only look for buys.'
    if bear_count >= 2 and bear_count > bull_count:
        return 'Only look for sells.'
    return 'Higher timeframe conflict. Trade smaller.'


def _recommendation(daily_side, h4_side, m15_side):
    aligned = [side for side in (daily_side, h4_side, m15_side) if side in {'bullish', 'bearish'}]
    bull_count = sum(1 for side in aligned if side == 'bullish')
    bear_count = sum(1 for side in aligned if side == 'bearish')
    if bull_count >= 2 and m15_side == 'bullish':
        return 'BUY ONLY'
    if bear_count >= 2 and m15_side == 'bearish':
        return 'SELL ONLY'
    return 'NO TRADES'


def _confidence_score(daily_conf, h4_conf, m15_conf, recommendation, sweeps_count=0):
    base = (float(daily_conf or 0) * 0.3) + (float(h4_conf or 0) * 0.35) + (float(m15_conf or 0) * 0.35)
    if recommendation in {'BUY ONLY', 'SELL ONLY'}:
        base += 8
    else:
        base -= 12
    if sweeps_count:
        base += min(8, sweeps_count * 2)
    return max(0, min(100, int(round(base))))


def _friendly_target_name(level):
    if not level:
        return 'No objective yet'
    level_type = str(level.get('type') or 'internal_liquidity').lower()
    mapping = {
        'current_day_high': 'Current Day High',
        'current_day_low': 'Current Day Low',
        'previous_day_high': 'Previous Day High',
        'previous_day_low': 'Previous Day Low',
        'current_session_high': 'Current Session High',
        'current_session_low': 'Current Session Low',
        'previous_session_high': 'Previous Session High',
        'previous_session_low': 'Previous Session Low',
        'asian_high': 'Asian High',
        'asian_low': 'Asian Low',
        'london_high': 'London High',
        'london_low': 'London Low',
        'new_york_high': 'NY High',
        'new_york_low': 'NY Low',
        'london_ny_overlap_high': 'London/NY Overlap High',
        'london_ny_overlap_low': 'London/NY Overlap Low',
        'psychological': 'Psychological Level',
        'structural_swing_low': 'Structural Swing Low',
        'structural_swing_high': 'Structural Swing High',
        'liquidity_level': 'Internal Liquidity',
        'internal_liquidity': 'Internal Liquidity',
    }
    return mapping.get(level_type, level_type.replace('_', ' ').title())


def _objective_sentence(side, target):
    if not target:
        return 'No directional edge yet.'
    price = float(target.get('price'))
    name = _friendly_target_name(target)
    if side == 'bullish':
        return f'Attack {name} @ {price:.2f}'
    if side == 'bearish':
        return f'Sweep toward {name} @ {price:.2f}'
    return f'Watch {name} @ {price:.2f}'


def _invalidation_sentence(side, invalidation):
    if not invalidation or invalidation.get('price') is None:
        return 'Not defined yet.'
    price = float(invalidation.get('price'))
    name = _friendly_target_name(invalidation)
    if side == 'bullish':
        return f'Cancel buys if 15M closes below {price:.2f} ({name}).'
    if side == 'bearish':
        return f'Cancel sells if 15M closes above {price:.2f} ({name}).'
    return f'Cancel the idea if price closes through {price:.2f} ({name}).'


def _wait_steps(side):
    if side in {'bullish', 'bearish'}:
        return ['Sweep', 'MSS', 'Retest', 'Entry']
    return ['Wait for 15M to choose direction', 'Wait for sweep', 'Wait for MSS', 'Wait for retest', 'Then enter']


def _build_bundle(symbol):
    symbol = symbol or config.SYMBOL

    tfs = {
        'daily': 'D1',
        '4h': 'H4',
        '15m': 'M15',
    }

    tfan = TimeframeAnalyzer()
    lmap = LiquidityMapper()
    tcalc = TargetCalculator()
    claude = ClaudeAdapter(enabled=getattr(config, 'ENABLE_CLAUDE', False))

    raw_rates = {}
    analyses = {}

    for name, tf in tfs.items():
        rates = get_market_data(symbol=symbol, timeframe=tf, candles=500)
        if rates is None or len(rates) == 0:
            continue
        raw_rates[name] = rates

    if not raw_rates:
        raise RuntimeError(f'No market data returned for {symbol}. Check MT5 connection, symbol availability, and timeframe access.')

    closed_rates = {name: _closed_rates(rates) for name, rates in raw_rates.items()}

    liquidity_model = lmap.build_liquidity_model(closed_rates.get('15m', []))

    for name in ('daily', '4h', '15m'):
        rates = closed_rates.get(name)
        if rates is None or len(rates) == 0:
            continue
        analysis = tfan.analyze(rates, timeframe=name, liquidity_model=liquidity_model if name == '15m' else None)
        if name == '15m':
            analysis['targets'] = tcalc.calculate_targets(rates, analysis.get('direction'))
        analyses[name] = analysis

    if '15m' not in analyses:
        raise RuntimeError(f'No 15m analysis available for {symbol}. The bot needs 15m market data to build its report.')

    overall = 'neutral'
    if analyses.get('daily', {}).get('direction') == analyses.get('4h', {}).get('direction') == analyses.get('15m', {}).get('direction'):
        overall = analyses['15m']['direction']
    else:
        bullish_votes = sum(1 for a in analyses.values() if 'bull' in str(a.get('direction', '')))
        bearish_votes = sum(1 for a in analyses.values() if 'bear' in str(a.get('direction', '')))
        if bullish_votes > bearish_votes:
            overall = 'bullish'
        elif bearish_votes > bullish_votes:
            overall = 'bearish'

    if claude.enabled:
        try:
            enhanced = claude.analyze({'symbol': symbol, 'analyses': analyses, 'liquidity_model': liquidity_model})
            if enhanced and enhanced.get('recommendation'):
                overall = enhanced['recommendation']
        except Exception:
            pass

    focus = analyses.get('15m', {})
    guidance = {
        'summary': f"15M is {focus.get('direction', 'neutral')} and is targeting {focus.get('primary_liquidity', {}).get('type') if focus.get('primary_liquidity') else 'the nearest liquidity pool' }.",
        'if_bullish': 'aims toward session highs / previous day highs / swing highs',
        'if_bearish': 'aims toward session lows / previous day lows / swing lows',
    }

    higher_timeframes = {
        'daily': analyses.get('daily', {}),
        '4h': analyses.get('4h', {}),
    }

    prev_state = load_state(getattr(config, 'MAIN3_STATE_FILE', None)) or {}
    prev_signature = prev_state.get('last_report_signature')
    prev_focus = prev_state.get('focus_15m', {})
    prev_overall = prev_state.get('overall')
    prev_candle_key = prev_state.get('last_15m_candle_key')

    df_15m = _rates_to_frame(closed_rates.get('15m'))
    current_candle_key = _latest_candle_key(closed_rates.get('15m'))
    current_price = float(df_15m['close'].iloc[-1]) if df_15m is not None and not df_15m.empty else None
    sweep_flags = _detect_sweeps(df_15m, liquidity_model)

    focus_display = dict(focus)
    if overall in {'bullish', 'bearish'} and focus_display.get('direction') != overall:
        focus_display['direction'] = overall
        focus_display['structure_reason'] = 'Post-MSS Realignment'
        focus_display['reasons'] = list(dict.fromkeys(['Post-MSS realignment after structure shift'] + list(focus_display.get('reasons', []))))

    timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    signature_payload = {
        'symbol': symbol,
        'overall': overall,
        'daily': analyses.get('daily', {}).get('direction'),
        '4h': analyses.get('4h', {}).get('direction'),
        '15m': focus_display.get('direction'),
        'focus_target': focus_display.get('primary_liquidity'),
        'session': liquidity_model.get('session'),
        'sweeps': sweep_flags,
        'market_shift': prev_overall,
    }
    signature = _stable_signature(signature_payload)

    previous_target = prev_focus.get('primary_liquidity') or {}
    previous_target_hit = None
    if previous_target.get('price') is not None and df_15m is not None and not df_15m.empty:
        previous_price = float(previous_target['price'])
        previous_target_hit = bool(float(df_15m['low'].min()) <= previous_price <= float(df_15m['high'].max()))

    changes = []
    if prev_overall and prev_overall != overall:
        changes.append(f"Overall bias changed: {prev_overall} -> {overall}")
    if prev_focus.get('direction') and prev_focus.get('direction') != focus_display.get('direction'):
        changes.append(f"15M direction changed: {prev_focus.get('direction')} -> {focus_display.get('direction')}")
    if prev_focus.get('primary_liquidity') != focus_display.get('primary_liquidity'):
        changes.append("15M target liquidity changed")

    if prev_focus.get('primary_liquidity') and previous_target_hit is False:
        changes.append(f"Previous target at {float(previous_target.get('price')):.2f} was not hit")

    market_shift = None
    if changes:
        market_shift = {
            'message': '15-minute structure shifted and the target map reset.',
            'changes': changes,
            'previous_target': previous_target or None,
            'previous_target_hit': previous_target_hit,
            'new_target': focus_display.get('primary_liquidity'),
            'previous_overall': prev_overall,
            'current_overall': overall,
            'previous_direction': prev_focus.get('direction'),
            'current_direction': focus_display.get('direction'),
        }

    if market_shift:
        shifted_direction = market_shift.get('current_direction')
        if shifted_direction in {'bullish', 'bearish'}:
            focus_display['direction'] = shifted_direction
            focus_display['structure_reason'] = focus_display.get('structure_reason') or 'Post-MSS Realignment'
            focus_display['reasons'] = list(dict.fromkeys(['Post-MSS realignment after structure shift'] + list(focus_display.get('reasons', []))))

    daily_side = _normalize_trade_side(analyses.get('daily', {}).get('direction'))
    h4_side = _normalize_trade_side(analyses.get('4h', {}).get('direction'))
    m15_side = _normalize_trade_side(focus_display.get('direction'))
    recommendation = _recommendation(daily_side, h4_side, m15_side)
    confidence = _confidence_score(
        analyses.get('daily', {}).get('confidence'),
        analyses.get('4h', {}).get('confidence'),
        focus_display.get('confidence'),
        recommendation,
        sweeps_count=len(sweep_flags),
    )

    market_control = {
        'daily': {
            'side': _control_word(daily_side),
            'phrase': _control_phrase(daily_side),
            'reason': (analyses.get('daily', {}).get('reasons') or [None])[0],
        },
        '4h': {
            'side': _control_word(h4_side),
            'phrase': _control_phrase(h4_side, reference_direction=daily_side),
            'reason': (analyses.get('4h', {}).get('reasons') or [None])[0],
        },
        '15m': {
            'side': _control_word(m15_side),
            'phrase': _control_phrase(m15_side, reference_direction=h4_side),
            'reason': focus_display.get('structure_reason') or (focus_display.get('reasons') or [None])[0],
        },
        'overall': _overall_market_message(daily_side, h4_side, m15_side),
    }

    target_plan = _summarize_levels(overall, liquidity_model, focus_display)
    target_plan['previous_target_hit'] = previous_target_hit
    target_plan['sweeps'] = sweep_flags

    should_send_analysis = signature != prev_signature and current_candle_key != prev_candle_key
    should_send_shift = bool(market_shift) and current_candle_key != prev_candle_key

    new_state = {
        'last_report_signature': signature,
        'last_15m_candle_key': current_candle_key,
        'overall': overall,
        'focus_15m': {
            'direction': focus_display.get('direction'),
            'primary_liquidity': focus_display.get('primary_liquidity'),
            'reversal_liquidity': focus_display.get('reversal_liquidity'),
            'invalidation_level': focus_display.get('invalidation_level'),
        },
        'per_tf': {k: {'direction': v.get('direction')} for k, v in analyses.items()},
        'recommendation': recommendation,
        'confidence': confidence,
        'market_control': market_control,
        'timestamp': timestamp,
    }

    draw_on_liquidity = {
        'bullish_target': target_plan.get('bullish_target'),
        'bearish_target': target_plan.get('bearish_target'),
        'invalidation': target_plan.get('invalidation'),
    }

    objective_target = draw_on_liquidity['bullish_target'] if recommendation == 'BUY ONLY' else draw_on_liquidity['bearish_target'] if recommendation == 'SELL ONLY' else None
    wait_steps = _wait_steps(_normalize_trade_side('bullish' if recommendation == 'BUY ONLY' else 'bearish' if recommendation == 'SELL ONLY' else 'neutral'))

    bundle = {
        'higher_timeframes': higher_timeframes,
        'focus_15m': focus_display,
        'liquidity_model': liquidity_model,
        'guidance': guidance,
        'overall': overall,
        'sweeps': sweep_flags,
        'draw_on_liquidity': draw_on_liquidity,
        'market_shift': market_shift,
        'current_price': current_price,
        'market_control': market_control,
        'recommendation': recommendation,
        'confidence': confidence,
        'objective': objective_target,
        'objective_text': _objective_sentence('bullish' if recommendation == 'BUY ONLY' else 'bearish' if recommendation == 'SELL ONLY' else 'neutral', objective_target),
        'invalidation_text': _invalidation_sentence('bullish' if recommendation == 'BUY ONLY' else 'bearish' if recommendation == 'SELL ONLY' else 'neutral', draw_on_liquidity.get('invalidation')),
        'wait_steps': wait_steps,
    }

    return {
        'symbol': symbol,
        'bundle': bundle,
        'timestamp': timestamp,
        'signature': signature,
        'prev_signature': prev_signature,
        'changes': changes,
        'should_send_analysis': should_send_analysis,
        'should_send_shift': should_send_shift,
        'new_state': new_state,
        'current_candle_key': current_candle_key,
    }


def _send_if_configured(message_fn, *args, **kwargs):
    if not getattr(config, 'TELEGRAM_SIGNAL_BOT_ENABLED', True):
        return False
    return asyncio.run(message_fn(*args, **kwargs))


def run_once(symbol=None, force_send=False):
    result = _build_bundle(symbol or config.SYMBOL)
    should_send_briefing = force_send or result['should_send_analysis'] or result['should_send_shift']

    if should_send_briefing:
        _send_if_configured(send_analysis_telegram, result['symbol'], result['bundle'], timestamp=result['timestamp'])

    save_state(result['new_state'], getattr(config, 'MAIN3_STATE_FILE', None))


def run_loop(symbol=None):
    if not connect_mt5():
        raise RuntimeError('Failed to initialize MT5 before loading market data.')

    symbol = symbol or config.SYMBOL
    interval_secs = max(float(getattr(config, 'ANALYSIS_INTERVAL', 200) or 200), 1.0)

    try:
        first_run = True
        while True:
            run_once(symbol, force_send=first_run)
            first_run = False
            time.sleep(interval_secs)
    finally:
        disconnect_mt5()


if __name__ == '__main__':
    run_loop()
