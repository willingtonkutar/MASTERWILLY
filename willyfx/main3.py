"""main3.py

Multi-timeframe market brief that focuses on daily, 1H, and 15M context,
session-specific liquidity, and duplicate-suppressed Telegram updates.
"""

import asyncio
import hashlib
import json
from datetime import datetime

import config
from data.feed import get_market_data
from analysis import TimeframeAnalyzer, LiquidityMapper, TargetCalculator
from analysis.state_store import load_state, save_state
from ai.claude_adapter import ClaudeAdapter
from notifiers.telegram_formatter import send_analysis_telegram, send_structure_shift_telegram


def _stable_signature(payload):
    encoded = json.dumps(payload, sort_keys=True, default=str).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _pick_guidance(direction):
    if direction == 'bullish':
        return 'look for buys on the 1-minute timeframe'
    if direction == 'bearish':
        return 'look for sells on the 1-minute timeframe'
    return 'wait for clear 15-minute structure before taking 1-minute entries'


def run_once(symbol=None):
    symbol = symbol or config.SYMBOL

    tfs = {
        'daily': 'D1',
        '1h': 'H1',
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
        if not rates:
            continue
        raw_rates[name] = rates

    liquidity_model = lmap.build_liquidity_model(raw_rates.get('15m', []))

    for name in ('daily', '1h', '15m'):
        rates = raw_rates.get(name)
        if not rates:
            continue
        analysis = tfan.analyze(rates, timeframe=name, liquidity_model=liquidity_model if name == '15m' else None)
        if name == '15m':
            analysis['targets'] = tcalc.calculate_targets(rates, analysis.get('direction'))
        analyses[name] = analysis

    overall = 'neutral'
    if analyses.get('daily', {}).get('direction') == analyses.get('1h', {}).get('direction') == analyses.get('15m', {}).get('direction'):
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
        'summary': f"15M is {focus.get('direction', 'neutral')} and is targeting {focus.get('primary_liquidity', {}).get('type') if focus.get('primary_liquidity') else 'the nearest liquidity pool'}.",
        'if_bullish': 'aims toward session highs / previous day highs / swing highs',
        'if_bearish': 'aims toward session lows / previous day lows / swing lows',
    }

    higher_timeframes = {
        'daily': analyses.get('daily', {}),
        '1h': analyses.get('1h', {}),
    }

    bundle = {
        'higher_timeframes': higher_timeframes,
        'focus_15m': focus,
        'liquidity_model': liquidity_model,
        'guidance': guidance,
        'overall': overall,
    }

    timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    signature_payload = {
        'symbol': symbol,
        'overall': overall,
        'daily': analyses.get('daily', {}).get('direction'),
        '1h': analyses.get('1h', {}).get('direction'),
        '15m': analyses.get('15m', {}).get('direction'),
        'focus_target': focus.get('primary_liquidity'),
        'session': liquidity_model.get('session'),
    }
    signature = _stable_signature(signature_payload)

    prev_state = load_state(getattr(config, 'MAIN3_STATE_FILE', None)) or {}
    prev_signature = prev_state.get('last_report_signature')
    prev_focus = prev_state.get('focus_15m', {})
    prev_overall = prev_state.get('overall')

    changes = []
    if prev_overall and prev_overall != overall:
        changes.append(f"Overall bias changed: {prev_overall} -> {overall}")
    if prev_focus.get('direction') and prev_focus.get('direction') != focus.get('direction'):
        changes.append(f"15M direction changed: {prev_focus.get('direction')} -> {focus.get('direction')}")
    if prev_focus.get('primary_liquidity') != focus.get('primary_liquidity'):
        changes.append("15M target liquidity changed")

    if signature != prev_signature:
        asyncio.run(send_analysis_telegram(symbol, bundle, timestamp=timestamp))

    if changes:
        shift_bundle = {
            'message': '15-minute structure shifted and the target map changed.',
            'changes': changes,
            'new_target': focus.get('primary_liquidity'),
        }
        asyncio.run(send_structure_shift_telegram(symbol, shift_bundle, timestamp=timestamp))

    new_state = {
        'last_report_signature': signature,
        'overall': overall,
        'focus_15m': {
            'direction': focus.get('direction'),
            'primary_liquidity': focus.get('primary_liquidity'),
            'reversal_liquidity': focus.get('reversal_liquidity'),
        },
        'per_tf': {k: {'direction': v.get('direction')} for k, v in analyses.items()},
        'timestamp': timestamp,
    }
    save_state(new_state, getattr(config, 'MAIN3_STATE_FILE', None))


if __name__ == '__main__':
    run_once()
