"""Whole-period OHLCV rebuilt from freshly queried, closed daily candles.

Daily refreshes may include the current (unfinished) week/month. These rows
keep the ordinary period-start key, but are explicitly marked partial so time
passing alone can never promote them to completed analysis inputs.
"""
from datetime import datetime, time, timedelta, timezone
import math

from app.schemas.models import Candle
from app.services.candle_validation import invalid_volume, validate_candle_volumes
from app.services.backfill_policy import complete_aggregate, period_start
from app.services.taiwan_trading_calendar import latest_trading_day_on_or_before, trading_day_status


TAIPEI = timezone(timedelta(hours=8))
PARTIAL_SOURCE = "capital_day_partial"
COMPLETE_SOURCE = "capital_day_aggregate"
MAX_VOLUME = 2**64 - 1


class DailyAggregatePause(Exception):
    def __init__(self, kind, reason):
        super().__init__(reason)
        self.kind = kind


class DailyAggregateCandle(Candle):
    source_bars: int


def daily_aggregate_end(day=None, *, now=None):
    """Never use today's unfinished daily bar, even for an explicit date."""
    now = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    available = now.date() - timedelta(days=now.hour < 14)
    return latest_trading_day_on_or_before(min(day or available, available))


def normalized_daily(candles, first, last):
    """One consistent row per trading date; reject ambiguous/bad input."""
    daily = {}
    for bar in candles:
        day = bar.timestamp.date()
        if not first <= day <= last or not trading_day_status(day)[0]:
            continue
        validate_candle_volumes([bar])
        if (not all(math.isfinite(value) for value in (bar.open, bar.high, bar.low, bar.close))
                or bar.high < max(bar.open, bar.low, bar.close)
                or bar.low > min(bar.open, bar.close)
                or not 0 <= bar.volume <= MAX_VOLUME):
            raise ValueError(f"{day} 日 K 的 OHLCV 無效，未覆寫週／月 K")
        row = Candle(timestamp=datetime.combine(day, time.min, tzinfo=TAIPEI),
                     open=bar.open, high=bar.high, low=bar.low, close=bar.close, volume=bar.volume)
        previous = daily.get(day)
        if previous is not None and previous != row:
            raise ValueError(f"{day} 有互相衝突的日 K，未覆寫週／月 K")
        daily[day] = row
    return [daily[day] for day in sorted(daily)]


def aggregate_daily(candles, interval, first, last):
    if interval not in {"week", "month"} or first != period_start(interval, first):
        raise ValueError("日 K 彙整必須從完整週／月的起始日查詢")
    groups = {}
    for bar in normalized_daily(candles, first, last):
        groups.setdefault(period_start(interval, bar.timestamp.date()), []).append(bar)
    result = []
    for label, daily in sorted(groups.items()):
        volume = sum(bar.volume for bar in daily)
        if volume > MAX_VOLUME:
            raise invalid_volume(volume, datetime.combine(label, time.min, tzinfo=TAIPEI))
        result.append(DailyAggregateCandle(
            timestamp=datetime.combine(label, time.min, tzinfo=TAIPEI),
            open=daily[0].open, high=max(bar.high for bar in daily),
            low=min(bar.low for bar in daily), close=daily[-1].close,
            volume=volume, source_bars=len(daily)))
    return result


def aggregate_source(interval, label, as_of, written_at):
    return COMPLETE_SOURCE if complete_aggregate(interval, label, written_at, as_of) else PARTIAL_SOURCE
