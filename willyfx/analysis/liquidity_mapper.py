from datetime import datetime, timezone

import pandas as pd


class LiquidityMapper:
    """Session and liquidity model for 15-minute guidance.

    The goal is to expose the liquidity the 15-minute chart is likely targeting,
    plus the opposite liquidity it would move toward if structure reverses.
    """

    def __init__(self):
        pass

    def detect_session(self, ts=None):
        """Return a session tag based on UTC hour."""
        now = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
        h = now.hour
        if 0 <= h < 8:
            return 'ASIAN'
        if 8 <= h < 13:
            return 'LONDON'
        if 13 <= h < 22:
            if 13 <= h < 16:
                return 'LONDON_NY_OVERLAP'
            return 'NEW_YORK'
        return 'CLOSE'

    @staticmethod
    def _to_frame(rates):
        if not rates:
            return pd.DataFrame()
        df = pd.DataFrame(rates).copy()
        if 'time' in df.columns:
            df['time'] = pd.to_datetime(df['time'], unit='s', errors='coerce', utc=True)
        return df.dropna(subset=['high', 'low', 'close'])

    def psychological_levels(self, price):
        """Return nearby round (psychological) levels for a price."""
        if price is None:
            return []
        base = round(price, 0)
        return [base - 2, base - 1, base, base + 1, base + 2]

    def build_liquidity_model(self, rates):
        """Build session, daily and nearby liquidity targets from 15m candles."""
        df = self._to_frame(rates)
        if df.empty:
            return {
                'session': 'UNKNOWN',
                'levels': [],
                'current_session': None,
                'previous_session': None,
                'current_day': None,
                'previous_day': None,
            }

        latest_time = df['time'].iloc[-1]
        current_session = self.detect_session(latest_time.timestamp())
        current_day = latest_time.normalize()
        previous_day = current_day - pd.Timedelta(days=1)

        df['date'] = df['time'].dt.normalize()
        df['session'] = df['time'].apply(lambda ts: self.detect_session(ts.timestamp()) if pd.notna(ts) else 'UNKNOWN')

        current_day_df = df[df['date'] == current_day]
        previous_day_df = df[df['date'] == previous_day]

        session_order = ['ASIAN', 'LONDON', 'LONDON_NY_OVERLAP', 'NEW_YORK', 'CLOSE']
        distinct_sessions = [s for s in df['session'].tolist() if s in session_order]
        distinct_sessions = list(dict.fromkeys(distinct_sessions))
        current_session_df = df[df['session'] == current_session]
        previous_session_df = pd.DataFrame()
        if len(distinct_sessions) >= 2:
            previous_session = distinct_sessions[-2]
            previous_session_df = df[df['session'] == previous_session]
        else:
            previous_session = None

        current_price = float(df['close'].iloc[-1])

        levels = []

        def _append_level(level_type, price, role, scope):
            if price is None:
                return
            levels.append({
                'type': level_type,
                'price': float(price),
                'role': role,
                'scope': scope,
            })

        _append_level('current_day_high', current_day_df['high'].max(), 'above', 'daily')
        _append_level('current_day_low', current_day_df['low'].min(), 'below', 'daily')
        if not previous_day_df.empty:
            _append_level('previous_day_high', previous_day_df['high'].max(), 'above', 'daily')
            _append_level('previous_day_low', previous_day_df['low'].min(), 'below', 'daily')

        if not current_session_df.empty:
            _append_level(f'{current_session.lower()}_high', current_session_df['high'].max(), 'above', 'session')
            _append_level(f'{current_session.lower()}_low', current_session_df['low'].min(), 'below', 'session')

        if previous_session and not previous_session_df.empty:
            _append_level(f'{previous_session.lower()}_high', previous_session_df['high'].max(), 'above', 'session')
            _append_level(f'{previous_session.lower()}_low', previous_session_df['low'].min(), 'below', 'session')

        for level in self.psychological_levels(current_price):
            _append_level('psychological', level, 'both', 'round_number')

        above = sorted([lvl for lvl in levels if lvl['price'] > current_price], key=lambda item: item['price'])
        below = sorted([lvl for lvl in levels if lvl['price'] < current_price], key=lambda item: item['price'], reverse=True)

        return {
            'session': current_session,
            'current_price': current_price,
            'current_day': {
                'high': float(current_day_df['high'].max()) if not current_day_df.empty else None,
                'low': float(current_day_df['low'].min()) if not current_day_df.empty else None,
            },
            'previous_day': {
                'high': float(previous_day_df['high'].max()) if not previous_day_df.empty else None,
                'low': float(previous_day_df['low'].min()) if not previous_day_df.empty else None,
            },
            'current_session': {
                'name': current_session,
                'high': float(current_session_df['high'].max()) if not current_session_df.empty else None,
                'low': float(current_session_df['low'].min()) if not current_session_df.empty else None,
            },
            'previous_session': {
                'name': previous_session,
                'high': float(previous_session_df['high'].max()) if not previous_session_df.empty else None,
                'low': float(previous_session_df['low'].min()) if not previous_session_df.empty else None,
            },
            'levels': levels,
            'nearest_above': above[0] if above else None,
            'nearest_below': below[0] if below else None,
        }
