"""Shared calendar/range policy for API and command-line candle maintenance."""
from __future__ import annotations

import math
import os
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from app.services.backfill_targets import target_for

from app.services.taiwan_trading_calendar import (
    latest_completed_trading_month, latest_completed_trading_week,
    latest_trading_day_on_or_before,
    trading_day_status,
)

INITIAL_BARS = 1200


def refresh_start(interval: str, end: date, bars: int) -> date:
    """Estimate a whole-date request using the offline trading calendar.

    Intraday density is only an estimate (normal stock session: 270 minutes).
    The worker counts actual returned bars and extends backwards when needed;
    holidays, suspensions and sparse trading never count as candles.
    """
    if interval in {"week", "month"}:
        cursor = period_start(interval, end)
        for _ in range(bars - 1):
            cursor = period_start(interval, cursor - timedelta(days=1))
        return cursor
    days = bars if interval == "day" else math.ceil(bars / math.ceil(270 / int(interval[:-1])))
    cursor = end
    counted = 0
    while cursor >= date(1900, 1, 1):
        if trading_day_status(cursor)[0]:
            counted += 1
            if counted >= days:
                return cursor
        cursor -= timedelta(days=1)
    return date(1900, 1, 1)


def request_interval(value: float | str | None = None) -> float:
    result = float(value if value is not None else os.getenv("CAPITAL_KLINE_REQUEST_INTERVAL_SECONDS", "0.8"))
    if not math.isfinite(result) or result < 0.5:
        raise ValueError("K 線查詢間隔必須是至少 0.5 秒的有限數值（預設 0.8 秒）")
    return result


def completed_end(interval: str, day: date | None = None) -> date:
    # An explicit date is an intentional as-of date. Automatic calls never
    # use today's unfinished daily/minute candle before the market closes.
    if day is None:
        now = datetime.now()
        day = now.date() - timedelta(days=now.hour < 14)
    if interval == "week":
        return latest_completed_trading_week(day)
    if interval == "month":
        return latest_completed_trading_month(day)
    return latest_trading_day_on_or_before(day)


def period_start(interval: str, day: date) -> date:
    if interval == "week":
        return day - timedelta(days=day.weekday())
    if interval == "month":
        return day.replace(day=1)
    return day


def aggregate_period_end(interval: str, label: date) -> date:
    first = period_start(interval,label)
    next_period = first+timedelta(days=7) if interval == "week" else (first.replace(day=28)+timedelta(days=4)).replace(day=1)
    return latest_trading_day_on_or_before(next_period-timedelta(days=1))


def complete_aggregate(interval, label, written_at, as_of, periods=None):
    """A period label alone cannot prove a weekly/monthly snapshot is final.

    Legacy partial snapshots stay stored, but do not count toward initialization
    or enter analysis. fetched_at is UTC; use Taiwan 14:00 (06:00 UTC), the same
    conservative close boundary as automatic completed_end().
    """
    if interval not in {"week","month"}: return True
    periods = periods if periods is not None else {}
    first = period_start(interval,label)
    if first not in periods: periods[first] = aggregate_period_end(interval,label)
    last = periods[first]
    if last > as_of or written_at is None: return False
    written = written_at.replace(tzinfo=timezone.utc) if written_at.tzinfo is None else written_at.astimezone(timezone.utc)
    return written >= datetime(last.year,last.month,last.day,6,tzinfo=timezone.utc)


def initial_start(interval: str, end: date, bars: int | None = None) -> date:
    # Calendar buffers are a bounded search range, not a promise of vendor
    # availability. Initialization searches backwards and stops at its target
    # or a documented consecutive-empty-window boundary.
    bars = target_for(interval,bars)
    ratios = {"1m": 10 / 360, "5m": 16 / 360, "15m": 45 / 360,
              "60m": 160 / 360, "day": 1.55, "week": 10.5, "month": 31.5}
    return period_start(interval, max(date(1900, 1, 1), end - timedelta(days=max(7, math.ceil(bars * ratios[interval])))))


def history_window(interval: str, start: date, end: date, cursor: date,
                   backward: bool, maximum: int) -> tuple[date, date]:
    """Never split a weekly/monthly aggregate across two committed queries."""
    if backward:
        first = cursor - timedelta(days=min(maximum - 1, (cursor - start).days))
        if interval in {"week", "month"}:
            boundary = period_start(interval, first)
            if boundary < first:
                first = (boundary + timedelta(days=7) if interval == "week" else
                         (boundary.replace(day=28) + timedelta(days=4)).replace(day=1))
        return first, cursor
    last = cursor + timedelta(days=min(maximum - 1, (end - cursor).days))
    if interval in {"week", "month"} and last < end:
        boundary = period_start(interval, last)
        following = (boundary + timedelta(days=7) if interval == "week" else
                     (boundary.replace(day=28) + timedelta(days=4)).replace(day=1))
        # A limit of 7/31 can end EXACTLY on the final calendar day.
        # That period is complete; cutting it off would produce end < cursor.
        if last < following - timedelta(days=1):
            last = boundary - timedelta(days=1)
    return cursor, last


def select_range(interval: str, end: date, count: int, latest: date | None,
                 state: dict | None, target: int | None = None,
                 force: bool = False) -> tuple[str, date] | None:
    state = state or {}
    target = target_for(interval,target)
    initialized = bool(state.get("initialized")) or count >= target
    if not initialized:
        return "initialization", initial_start(interval, end, target)
    verified = state.get("verified_through")
    if verified is not None and verified >= end and not force:
        return None
    # Include the latest stored period again: week/month bars can be labelled
    # with their FIRST day and may have been saved while still incomplete.
    start = period_start(interval, min(latest or end, end))
    if verified is not None:
        start = period_start(interval, min(verified, end))
    return "incremental", start


class HistoryRequestLimiter:
    """One shared serialized request slot, including retry requests."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_finished = 0.0
        self.override: float | None = None

    @contextmanager
    def slot(self):
        with self._lock:
            delay = request_interval(self.override)
            remaining = self._last_finished + delay - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            try:
                yield
            finally:
                self._last_finished = time.monotonic()


history_request_limiter = HistoryRequestLimiter()
