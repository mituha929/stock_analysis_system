"""Positive trade evidence, not an inference from a successful empty response."""
from datetime import timedelta


def classify_missing(activity, first, last):
    matches = sorted(d for d in (activity or {}) if first <= d <= last)
    return dict(classification='api_missing_confirmed' if matches else 'trade_activity_unconfirmed',
                trade_confirmed=bool(matches), trade_dates=[str(d) for d in matches],
                trade_sources=sorted({activity[d] for d in matches}))


class TradeActivityRepository:
    def trade_activity_dates(self, table, symbol, first, last, cursor=None):
        """Same-market stream only. A week/month volume cannot prove ANY one day.

        Positive day/intraday bars establish that at least one trade occurred;
        they do not assert that every minute ought to have a bar. Used only for
        missing *whole-date* responses, never individual missing minute slots.
        """
        table = self._validate_table_name(table)
        def read(q):
            q.execute(f'''SELECT DISTINCT DATE(candle_time) FROM `{table}`
                WHERE symbol=%s AND candle_time>=%s AND candle_time<%s AND volume>0
                AND interval_code IN('day','1m','5m','15m','60m')
                AND source='capital_history' ''', (symbol, first, last+timedelta(days=1)))
            return {r[0]: 'stored_positive_volume:'+table for r in q.fetchall()}
        if cursor is not None:
            return read(cursor)
        with self.transaction() as q:
            return read(q)
