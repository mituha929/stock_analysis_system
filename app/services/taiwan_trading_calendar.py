"""Taiwan stock-market trading-day checks with an official annual cache.

The automatic backfill must be safe to invoke outside trading hours.  It
therefore reads a local cache during normal operation.  The scheduler refreshes
that cache from TWSE once when a new calendar year is first encountered.
``TWSE_HOLIDAYS`` remains an administrator override and offline fallback.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from functools import lru_cache
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)
TWSE_HOLIDAY_URL = "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule"
DEFAULT_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "twse_holiday_cache.json"
_CACHE_LOCK = threading.Lock()
DEFAULT_STOCK_CALENDAR_PATH = DEFAULT_CACHE_PATH.with_name('stock_market_holidays.json')


@lru_cache(maxsize=4)
def _stock_calendar_snapshot(filename, modified, size):
    """Reload on atomic calendar replacement, never download on a hot path."""
    if size > 8 * 1024 * 1024:
        raise TaiwanTradingCalendarError('台股共用休市日快照過大')
    try:
        data = json.loads(Path(filename).read_text(encoding='utf-8-sig'))
        if data['version'] != 1 or set(data['markets']) != {'TWSE', 'TPEx', 'ESB'}:
            raise ValueError('version/markets')
        first, last = date.fromisoformat(data['range_start']), date.fromisoformat(data['range_end'])
        if first > last:
            raise ValueError('range')
        records = {}
        for row in data['records']:
            day = date.fromisoformat(row['date'])
            if type(row['is_closed']) is not bool or not first <= day <= last or day in records:
                raise ValueError('record')
            records[day] = row
        verified = {int(r['year']) for r in data['annual_coverage']
                    if r['status'] in {'api', 'reviewed_archive'}}
        unknown = {date.fromisoformat(r['date']) for r in data.get('review_required_records', [])}
        return first, last, records, verified, unknown
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise TaiwanTradingCalendarError(f'台股共用休市日快照格式錯誤：{filename}') from error


def stock_calendar_snapshot():
    path = Path(os.getenv('CAPITAL_STOCK_HOLIDAY_FILE', str(DEFAULT_STOCK_CALENDAR_PATH)))
    if not path.is_absolute():
        path = DEFAULT_CACHE_PATH.parents[1] / path
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise TaiwanTradingCalendarError(f'無法讀取台股共用休市日快照：{path}') from error
    return _stock_calendar_snapshot(str(path), stat.st_mtime_ns, stat.st_size)


def stock_calendar_status(day, snapshot=None):
    """None means unknown. No city/school closure is inferred as market-wide."""
    if snapshot is None:
        return None, 'calendar_unverified'
    first, last, records, verified, unknown = snapshot
    if not first <= day <= last:
        return None, 'calendar_outside_snapshot'
    row = records.get(day)
    if row is not None:
        sources = [str(e.get('source_url', '')) for e in row.get('evidence', [])]
        reason = 'stock_market_calendar:' + row.get('category', '')
        return not row['is_closed'], reason + ('; ' + ','.join(sources) if sources else '')
    if day.year in verified and day not in unknown:
        # Calendar-open is NOT proof that an individual security traded.
        return True, 'stock_market_calendar:scheduled_session'
    return None, 'calendar_unverified'


def query_trading_bounds(interval, first, last):
    """Trim closed edges; retain whole week/month labels and bulk date queries.

    Interior holidays need no individual requests. Splitting a multi-year day
    range into thousands of sessions would unnecessarily overload the vendor.
    """
    left, right = first, last
    while left <= right and not trading_day_status(left)[0]:
        left += timedelta(days=1)
    while right >= left and not trading_day_status(right)[0]:
        right -= timedelta(days=1)
    if left > right:
        return None
    return (first, last) if interval in {'week', 'month'} else (left, right)


# Weekday closures announced by TWSE for 2026.  Weekend dates are deliberately
# omitted because they are already handled by ``weekday()`` below.
TWSE_HOLIDAYS_2026 = frozenset({
    date(2026, 1, 1),
    date(2026, 2, 12),
    date(2026, 2, 13),
    date(2026, 2, 16),
    date(2026, 2, 17),
    date(2026, 2, 18),
    date(2026, 2, 19),
    date(2026, 2, 20),
    date(2026, 2, 27),
    date(2026, 4, 3),
    date(2026, 4, 6),
    date(2026, 5, 1),
    date(2026, 6, 19),
    date(2026, 9, 25),
    date(2026, 9, 28),
    date(2026, 10, 9),
    date(2026, 10, 26),
    date(2026, 12, 25),
})


class TaiwanTradingCalendarError(ValueError):
    """Raised when the administrator-provided calendar configuration is invalid."""


def _cache_path() -> Path:
    configured = os.getenv("TWSE_HOLIDAY_CACHE_FILE", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_CACHE_PATH


def _read_cache(path: Path | None = None) -> dict[str, object]:
    selected = path or _cache_path()
    if not selected.exists():
        return {"years": {}}
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TaiwanTradingCalendarError(f"無法讀取台股休市日快取：{selected}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("years", {}), dict):
        raise TaiwanTradingCalendarError(f"台股休市日快取格式錯誤：{selected}")
    return payload


def _cached_holidays(year: int, path: Path | None = None) -> set[date]:
    payload = _read_cache(path)
    raw_dates = payload.get("years", {}).get(str(year), [])
    try:
        return {date.fromisoformat(str(item)) for item in raw_dates}
    except (TypeError, ValueError) as error:
        raise TaiwanTradingCalendarError(f"{year} 年台股休市日快取含有無效日期") from error


def _is_closed_row(name: str) -> bool:
    """Exclude informational rows such as first/last trading day."""
    normalized = name.strip()
    return not normalized.endswith("交易日")


def refresh_official_holidays(
    current_day: date | None = None, *, cache_path: Path | None = None,
    target_year: int | None = None, force: bool = False, timeout_seconds: float = 15,
) -> tuple[bool, str]:
    """Download and cache the current year's official TWSE holiday schedule.

    Returns ``(downloaded, message)``.  Existing valid cache data is never
    replaced when the request or response validation fails.
    """
    today = current_day or date.today()
    requested_year = target_year or today.year
    selected_path = cache_path or _cache_path()
    with _CACHE_LOCK:
        existing = _read_cache(selected_path)
        existing_year = existing.get("years", {}).get(str(requested_year), [])
        fetched_by_year = existing.get("fetched_at_by_year", {})
        fetched_at = fetched_by_year.get(str(requested_year)) if isinstance(fetched_by_year, dict) else None
        prefetched_in_prior_year = bool(
            requested_year == today.year
            and fetched_at
            and datetime.fromisoformat(str(fetched_at)).date().year < requested_year
        )
        if existing_year and not force and not prefetched_in_prior_year:
            return False, f"已使用 {requested_year} 年證交所休市日快取"

        request_url = f"{TWSE_HOLIDAY_URL}?date={requested_year}0101&response=json"
        request = Request(
            request_url,
            headers={"Accept": "application/json", "User-Agent": "capital-api/0.1"},
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise TaiwanTradingCalendarError(f"下載證交所 {requested_year} 年休市日失敗：{error}") from error
        if payload.get("stat") != "ok" or not isinstance(payload.get("data"), list):
            raise TaiwanTradingCalendarError("證交所休市日回應格式錯誤")

        holidays: set[date] = set()
        response_years: set[int] = set()
        for row in payload["data"]:
            if not isinstance(row, list) or len(row) < 2:
                continue
            try:
                row_day = date.fromisoformat(str(row[0]))
            except ValueError:
                continue
            response_years.add(row_day.year)
            if row_day.year == requested_year and row_day.weekday() < 5 and _is_closed_row(str(row[1])):
                holidays.add(row_day)
        if requested_year not in response_years:
            raise TaiwanTradingCalendarError(
                f"證交所尚未提供 {requested_year} 年開休市資料，保留既有快取"
            )
        if not holidays:
            raise TaiwanTradingCalendarError(
                f"證交所 {requested_year} 年回應沒有可辨識的平日休市日，保留既有快取"
            )

        years = dict(existing.get("years", {}))
        years[str(requested_year)] = sorted(item.isoformat() for item in holidays)
        fetched_by_year = dict(fetched_by_year) if isinstance(fetched_by_year, dict) else {}
        fetched_at = datetime.now(timezone.utc).isoformat()
        fetched_by_year[str(requested_year)] = fetched_at
        updated = {
            "source": request_url,
            "fetched_at": fetched_at,
            "fetched_at_by_year": fetched_by_year,
            "years": years,
        }
        selected_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = selected_path.with_suffix(selected_path.suffix + ".tmp")
        temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, selected_path)
        return True, f"已更新 {requested_year} 年證交所休市日，共 {len(holidays)} 個平日休市日"


def _configured_holidays(value: str | None = None) -> set[date]:
    """Read comma-separated ``YYYY-MM-DD`` closures from ``TWSE_HOLIDAYS``."""
    raw_value = os.getenv("TWSE_HOLIDAYS", "") if value is None else value
    holidays: set[date] = set()
    for item in raw_value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            holidays.add(date.fromisoformat(item))
        except ValueError as error:
            raise TaiwanTradingCalendarError(
                "TWSE_HOLIDAYS 必須是以逗號分隔的 YYYY-MM-DD 日期"
            ) from error
    return holidays


def trading_day_status(day: date, configured_holidays: str | None = None) -> tuple[bool, str]:
    """Return whether *day* is a Taiwan stock-market trading day and why."""
    if day in _configured_holidays(configured_holidays):
        return False, "台股休市日"
    known, reason = stock_calendar_status(day, stock_calendar_snapshot())
    if known is not None:
        if not known:
            return False, '週末' if 'weekend' in reason else '台股休市日'
        if any(kind in reason for kind in ('special_open','manual_override','trading_marker')):
            return True, '交易日'
        # A scheduled weekday in the older snapshot must not override a
        # newer, explicit closure already stored in the annual runtime cache.
    # Before 2019 some Saturday bank make-up days traded. If the historical
    # calendar is unavailable, do not silently skip an unverified Saturday.
    if day.weekday() == 6 or (day.weekday() == 5 and day.year >= 2019):
        return False, "週末"
    built_in = TWSE_HOLIDAYS_2026 if day.year == 2026 else frozenset()
    cached = _cached_holidays(day.year)
    if day in built_in or day in cached:
        return False, "台股休市日"
    return True, "交易日"


def latest_trading_day_on_or_before(day: date, configured_holidays: str | None = None) -> date:
    """Return the most recent Taiwan trading day, including *day* when open."""
    candidate = day
    while True:
        is_trading_day, _reason = trading_day_status(candidate, configured_holidays)
        if is_trading_day:
            return candidate
        candidate = candidate.fromordinal(candidate.toordinal() - 1)


def next_trading_day_after(day: date, configured_holidays: str | None = None) -> date:
    """Return the first Taiwan trading day strictly after *day*."""
    candidate = day.fromordinal(day.toordinal() + 1)
    while True:
        is_trading_day, _reason = trading_day_status(candidate, configured_holidays)
        if is_trading_day:
            return candidate
        candidate = candidate.fromordinal(candidate.toordinal() + 1)


def is_last_trading_day_of_week(day: date, configured_holidays: str | None = None) -> bool:
    """Return whether *day* is the final trading day of its ISO week."""
    is_trading_day, _reason = trading_day_status(day, configured_holidays)
    if not is_trading_day:
        return False
    next_day = next_trading_day_after(day, configured_holidays)
    return next_day.isocalendar()[:2] != day.isocalendar()[:2]


def is_last_trading_day_of_month(day: date, configured_holidays: str | None = None) -> bool:
    """Return whether *day* is the final trading day of its calendar month."""
    is_trading_day, _reason = trading_day_status(day, configured_holidays)
    if not is_trading_day:
        return False
    next_day = next_trading_day_after(day, configured_holidays)
    return (next_day.year, next_day.month) != (day.year, day.month)


def latest_completed_trading_week(day: date, configured_holidays: str | None = None) -> date:
    """Return the most recent final trading day of a completed ISO week."""
    candidate = day
    for _ in range(15):
        if is_last_trading_day_of_week(candidate, configured_holidays):
            return candidate
        candidate = candidate.fromordinal(candidate.toordinal() - 1)
    raise TaiwanTradingCalendarError(f"無法找出 {day.isoformat()} 之前已完成的交易週")


def latest_completed_trading_month(day: date, configured_holidays: str | None = None) -> date:
    """Return the most recent final trading day of a completed calendar month."""
    candidate = day
    for _ in range(70):
        if is_last_trading_day_of_month(candidate, configured_holidays):
            return candidate
        candidate = candidate.fromordinal(candidate.toordinal() - 1)
    raise TaiwanTradingCalendarError(f"無法找出 {day.isoformat()} 之前已完成的交易月")
