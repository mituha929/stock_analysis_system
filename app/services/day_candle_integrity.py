"""Fail-closed daily-bar coverage policy; empty vendor replies prove nothing."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse

from app.services import taiwan_trading_calendar as calendar

ROOT = Path(__file__).resolve().parents[2]
POLICY_VERSION = 4


def integrity_enabled():
    return os.getenv('CAPITAL_DAY_INTEGRITY_ENABLED', 'true').lower() not in {'0', 'false'}


def taipei_today():
    return datetime.now(timezone(timedelta(hours=8))).date()


def integrity_schedule(day=None):
    """Heavy historical checks run on Taiwan market closures, not weekdays.

    No network access: an unverified weekday calendar never grants permission
    to start a potentially expensive holiday audit. Ordinary collection is
    unaffected. ``always`` is an explicit diagnostic/test override.
    """
    if not integrity_enabled():
        return False, '日 K 完整性驗收已停用'
    mode = os.getenv('CAPITAL_DAY_INTEGRITY_SCHEDULE', 'non_trading_days').strip().lower()
    if mode not in {'non_trading_days', 'always'}:
        raise ValueError('CAPITAL_DAY_INTEGRITY_SCHEDULE 僅支援 non_trading_days 或 always')
    if mode == 'always':
        return True, '明確設定每次執行日 K 驗收'
    selected = day or taipei_today()
    is_open, _source = DayCalendar().status(selected)
    if is_open is False:
        return True, '台北時間已確認休市日，執行日 K 完整性驗收'
    return False, ('今日交易日，不執行歷史完整性驗收' if is_open else
                   '今日休市狀態尚未核實，不啟動歷史完整性驗收')


def expanded_day_window(cursor, lower, empty_windows, maximum, *, base=14):
    """Disjoint backward windows, not a replay of already counted refresh bars."""
    steps = (base, 30, 60, 120, 240, 480, 960, 1920)
    width = min(maximum, steps[min(max(int(empty_windows), 0), len(steps)-1)])
    return max(lower, cursor-timedelta(days=width-1)), cursor


def official_reference(value):
    parsed = urlparse(str(value))
    host = (parsed.hostname or '').lower()
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password
            and any(host == domain or host.endswith('.'+domain)
                    for domain in ('twse.com.tw', 'tpex.org.tw')))


class DayEvidence:
    """Explicitly reviewed official evidence, NOT inferred from MIN(candle_time).

    first_trade means the first EVER trading day of this security, not a board
    transfer/listing date. no_trade confirms precisely the stated date range.
    The source URL and reviewed explanation remain available in every report.
    """
    def __init__(self, records=()):
        self.records = {}
        for raw in records:
            market = int(raw['market_no'])
            symbol, kind = str(raw['symbol']), raw['kind']
            if (market not in (0, 1, 4, 5, 6) or (kind == 'first_trade' and market in (5, 6))
                    or not re.fullmatch(r'[A-Z0-9_.-]{1,32}', symbol)
                    or kind not in {'first_trade', 'no_trade'}
                    or not official_reference(raw.get('source'))
                    or len(str(raw.get('source', ''))) > 1024
                    or not str(raw.get('reason', '')).strip()
                    or len(str(raw.get('reason', ''))) > 800
                    or raw.get('reviewed') is not True):
                raise ValueError('日 K 證據須含精確商品、官方 HTTPS 來源、人工核對及原因')
            start, end = date.fromisoformat(raw['start_date']), date.fromisoformat(raw['end_date'])
            reviewed_on = date.fromisoformat(raw['reviewed_on'])
            if start > end or (kind == 'first_trade' and start != end) or reviewed_on > date.today():
                raise ValueError('日 K 證據日期不合法')
            row = dict(raw, start=start, end=end)
            self.records.setdefault((market, symbol), []).append(row)
        for values in self.records.values():
            boundaries = {r['start'] for r in values if r['kind'] == 'first_trade'}
            if len(boundaries) > 1:
                raise ValueError('同一商品的首次交易日證據互相矛盾')

    @classmethod
    def load(cls):
        path = Path(os.getenv('CAPITAL_DAY_EVIDENCE_FILE', str(ROOT/'config/day_candle_evidence.json')))
        if not path.is_absolute():
            path = ROOT/path
        if path.stat().st_size > 2*1024*1024:
            raise ValueError('日 K 證據設定過大')
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('version') != 1 or not isinstance(data.get('records'), list):
            raise ValueError('日 K 證據設定格式錯誤')
        return cls(data['records'])

    def first_trade(self, market, symbol):
        base = {5: 0, 6: 1}.get(market, market)
        return next((r for r in self.records.get((base, symbol), ()) if r['kind'] == 'first_trade'), None)

    def no_trade(self, market, symbol, day):
        return next((r for r in self.records.get((market, symbol), ())
                     if r['kind'] == 'no_trade' and r['start'] <= day <= r['end']), None)


class DayCalendar:
    """Read annual official snapshots once, not once per symbol/day."""
    def __init__(self, payload=None, overrides=None):
        data = calendar._read_cache() if payload is None else payload
        self.source = str(data.get('source', ''))
        self.years = {int(year): {date.fromisoformat(d) for d in days}
                      for year, days in data.get('years', {}).items()}
        self.verified = set(self.years) if official_reference(self.source) else set()
        self.legacy_verified = set(self.verified)
        self.stock_snapshot = calendar.stock_calendar_snapshot() if payload is None else None
        if self.stock_snapshot:
            self.verified.update(self.stock_snapshot[3])
        self.overrides = calendar._configured_holidays() if overrides is None else set(overrides)

    def status(self, day):
        if day in self.overrides:
            return False, 'TWSE_HOLIDAYS:administrator_override'
        known, source = calendar.stock_calendar_status(day, self.stock_snapshot)
        if known is not None:
            if (known and 'scheduled_session' in source and day.year in self.legacy_verified
                    and day in self.years[day.year]):
                return False, self.source
            return known, source
        if day.year >= 2001 and (day.weekday() == 6 or day.weekday() == 5 and day.year >= 2019):
            return False, 'weekend'
        if day.year not in self.legacy_verified:
            return None, 'calendar_unverified'
        if day.weekday() == 5 and day.year < 2019:
            return None, 'historical_saturday_unverified'
        return day not in self.years[day.year], self.source


def assess_days(dates, as_of, target, floor, market, symbol, evidence, sessions, suspensions=None, listing=None, activity=None):
    """Dates are the last target DISTINCT trading dates, oldest first.

    Return all unresolved candidate dates plus evidence-backed no-bar dates.
    A sufficient COUNT never conceals an interior or trailing gap.
    """
    days = sorted({d for d in dates if d <= as_of})[-target:]
    first_evidence = evidence.first_trade(market, symbol)
    listing_boundary = (listing or {}).get('history_start')
    boundary = first_evidence['start'] if first_evidence else listing_boundary
    # Existing history wins over a board listing date, but not over explicit
    # human-reviewed FIRST-EVER-trade evidence (its existing conflict contract).
    if (not first_evidence and boundary and days and days[0] < boundary
            and (listing or {}).get('reason') != 'official_bond_listing'):
        boundary = listing_boundary = None
    conflict = bool(first_evidence and boundary and days and days[0] < boundary)
    first = days[0] if len(days) >= target else min(floor, days[0]) if days else floor
    if boundary and not conflict:
        first = max(first, boundary)
    present = set(days)
    issues, confirmed, unknown_years, calendar_conflicts, closed = [], [], set(), [], []
    # A known lifetime end excludes only missing dates, NEVER stored quotes.
    from app.services.instrument_lifecycle import active_bounds
    valid = active_bounds(listing, first, as_of)
    last_expected = valid[1] if valid else first-timedelta(days=1)
    day = first
    while day <= last_expected:
        is_open, source = sessions.status(day)
        if is_open is None:
            unknown_years.add(day.year)
        if day in present and is_open is False:
            calendar_conflicts.append(day)
        if day not in present:
            if is_open is False:
                # Weekends/holidays are not missing bars and need no fake row.
                closed.append(dict(day=day, source=source))
            else:
                proof = evidence.no_trade(market, symbol, day)
                halt = suspensions.full_window(market, symbol, day, day) if suspensions and is_open else None
                if proof or halt:
                    confirmed.append(dict(day=day, status='confirmed_no_bar', reason='official_no_trade' if proof else 'official_suspension',
                                          source=proof['source'] if proof else halt,
                                          description=(proof['reason']+'; reviewed_on='+proof['reviewed_on']) if proof else None))
                else:
                    trade_source = (activity or {}).get(day)
                    issues.append(dict(day=day, status='unresolved',
                        reason='calendar_unverified' if is_open is None else 'api_missing_confirmed' if trade_source else 'trade_activity_unconfirmed',
                        source=trade_source or source))
        day += timedelta(days=1)
    short = len(days) < target
    available_verified = bool(short and boundary and not conflict and first == boundary and not issues and not unknown_years)
    status = ('evidence_conflict' if conflict else 'calendar_conflict' if calendar_conflicts else 'calendar_unverified' if unknown_years
              else 'unresolved' if issues else 'complete_available_history' if available_verified
              else 'history_shortfall' if short else 'complete')
    if boundary and boundary > as_of:
        status = 'not_listed_as_of'
    return dict(status=status, search_start=first, as_of=as_of, target_bars=target,
                stored_bars=len(days), first_bar=days[0] if days else None,
                last_bar=days[-1] if days else None, unresolved_count=len(issues),
                api_missing_dates=sum(r['reason'] == 'api_missing_confirmed' for r in issues),
                activity_unconfirmed_dates=sum(r['reason'] == 'trade_activity_unconfirmed' for r in issues),
                last_expected_date=last_expected if valid else None,
                calendar_years_unverified=sorted(unknown_years),
                calendar_conflicts=calendar_conflicts,
                first_trade=first_evidence['start'] if first_evidence else None, evidence=first_evidence['source'] if first_evidence else None,
                first_trade_reason=first_evidence['reason'] if first_evidence else None,
                first_trade_reviewed_on=first_evidence['reviewed_on'] if first_evidence else None,
                listing_boundary=listing_boundary, listing_policy=listing,
                history_limit_reason='掛牌歷史不足' if available_verified and not first_evidence and listing_boundary else None,
                issues=issues, confirmed=confirmed, closed=closed)
