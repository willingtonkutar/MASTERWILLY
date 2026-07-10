import math
import time
import unittest

from analysis.liquidity_mapper import LiquidityMapper
from analysis.timeframe_analyzer import TimeframeAnalyzer
import main3


def build_rates(start_price=100.0, step=0.5, count=80, reverse=False, start_ts=1_700_000_000):
    rates = []
    price = start_price
    for i in range(count):
        price = price - step if reverse else price + step
        high = price + 0.25
        low = price - 0.25
        open_price = price - 0.1 if not reverse else price + 0.1
        close_price = price
        rates.append({
            'time': start_ts + i * 900,
            'open': open_price,
            'high': high,
            'low': low,
            'close': close_price,
            'tick_volume': 100 + i,
        })
    return rates


class Main3ComponentTests(unittest.TestCase):
    def test_timeframe_analyzer_bullish_with_liquidity(self):
        rates = build_rates()
        liquidity_model = {
            'levels': [
                {'type': 'current_day_high', 'price': 140.0, 'role': 'above', 'scope': 'daily'},
                {'type': 'current_day_low', 'price': 90.0, 'role': 'below', 'scope': 'daily'},
                {'type': 'previous_day_high', 'price': 135.0, 'role': 'above', 'scope': 'daily'},
                {'type': 'previous_day_low', 'price': 95.0, 'role': 'below', 'scope': 'daily'},
            ]
        }
        analysis = TimeframeAnalyzer().analyze(rates, timeframe='15m', liquidity_model=liquidity_model)
        self.assertIn('bull', analysis['direction'])
        self.assertTrue(analysis['execution_guidance'])
        self.assertIsNotNone(analysis['primary_liquidity'])
        self.assertIn('liquidity', ' '.join(analysis['reasons']).lower())

    def test_timeframe_analyzer_bearish_reversal_reason(self):
        rates = build_rates(start_price=200.0, step=0.6, reverse=True)
        liquidity_model = {
            'levels': [
                {'type': 'current_day_high', 'price': 240.0, 'role': 'above', 'scope': 'daily'},
                {'type': 'current_day_low', 'price': 150.0, 'role': 'below', 'scope': 'daily'},
            ]
        }
        analysis = TimeframeAnalyzer().analyze(rates, timeframe='15m', liquidity_model=liquidity_model)
        self.assertIn('bear', analysis['direction'])
        self.assertTrue(analysis['execution_guidance'])
        self.assertIsNotNone(analysis['reversal_liquidity'])

    def test_liquidity_mapper_builds_session_map(self):
        rates = []
        base_ts = 1_700_000_000
        for i in range(120):
            ts = base_ts + i * 900
            price = 100 + math.sin(i / 8) * 2 + i * 0.05
            rates.append({
                'time': ts,
                'open': price - 0.1,
                'high': price + 0.3,
                'low': price - 0.3,
                'close': price,
                'tick_volume': 100,
            })

        model = LiquidityMapper().build_liquidity_model(rates)
        self.assertIn('session', model)
        self.assertIn('levels', model)
        self.assertTrue(model['levels'])
        self.assertIn('current_day', model)
        self.assertIn('previous_day', model)

    def test_signature_is_stable(self):
        payload = {'symbol': 'XAUUSD', 'a': 1, 'b': {'c': 2}}
        sig1 = main3._stable_signature(payload)
        sig2 = main3._stable_signature(payload)
        self.assertEqual(sig1, sig2)


if __name__ == '__main__':
    unittest.main()
