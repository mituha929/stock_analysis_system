"""Post-collection daily audit and targeted repair under the worker lease."""
from datetime import date, datetime, timedelta, timezone
import logging
import os

from app.services.backfill_policy import initial_start
from app.services.capital_history_service import max_history_days
from app.services.candle_validation import find_candle_data_error, validate_candle_volumes
from app.services.day_candle_integrity import DayCalendar, DayEvidence, assess_days, expanded_day_window
from app.services.taiwan_trading_calendar import refresh_official_holidays
from app.services.instrument_lifecycle import blocked_reason, active_bounds

logger = logging.getLogger(__name__)


class DayIntegrityAudit:
    def __init__(self, runner, *, allowed=None):
        self.runner, self.repo, self.control = runner, runner.repository, runner.control
        self.allowed = allowed
        self.evidence = DayEvidence.load()
        self.sessions = DayCalendar()
        self.attempted_years = set()
        self.retry_seconds = max(15, int(os.getenv('CAPITAL_DAY_GAP_RETRY_SECONDS', '900')))
        self.budget = max(1, int(os.getenv('CAPITAL_DAY_GAP_WINDOW_BUDGET', '30')))

    def ensure_calendar(self, first, last):
        for year in range(first.year, last.year+1):
            if year not in self.sessions.verified and year not in self.attempted_years:
                self.attempted_years.add(year)
                self.control.update(stage='day_integrity_calendar', calendar_year=year)
                try:
                    refresh_official_holidays(target_year=year, timeout_seconds=5)
                except Exception as error:
                    logger.warning('日 K 驗收休市資料 %s 尚未核實：%s', year, error)
        self.sessions = DayCalendar()

    def assess(self, plan, symbol):
        table, market, end, goal = plan['candle_table'], plan['market_no'], plan['end_date'], plan['target_bars']
        dates = self.repo.day_integrity_dates(table, symbol, end, goal)
        floor = initial_start('day', end, goal)
        previous = self.repo.previous_day_assessment(table, market, symbol)
        listing = self.runner.history_listing_policy(table, market, symbol)
        boundary = (listing or {}).get('history_start')
        # Keep the already-expanded search boundary over restart. When an
        # entire inspected span has no unexplained gaps but too few bars,
        # extend backwards instead of declaring history exhausted.
        if previous and previous['as_of_date'] == end and previous['target_bars'] == goal:
            floor = min(floor, previous['search_start'])
            if previous['status'] == 'history_shortfall':
                floor = max(date(1900, 1, 1), floor-timedelta(days=60))
        if boundary:
            floor = max(floor, boundary)
        self.ensure_calendar(min(floor, dates[0]) if dates else floor, end)
        activity = (self.repo.trade_activity_dates(table, symbol, floor, end)
                    if getattr(type(self.repo), 'trade_activity_dates', None) is not None else {})
        result = assess_days(dates, end, goal, floor, market, symbol, self.evidence, self.sessions,
                             self.runner.suspensions, listing=listing, activity=activity)
        self.repo.save_day_assessment(table, market, symbol, result, dates, self.runner.owner)
        self.control.update(listing_policy=listing,listing_history_shortfall=bool(result['history_limit_reason']),
                            stored_bars=result['stored_bars'],current_target_bars=goal)
        if result['status'] == 'complete_available_history':
            self.repo.finish_verified_short_day_jobs(table, market, symbol, end,
                result['evidence'] or ('掛牌歷史不足；'+listing['source']))
        return result

    def check_stop(self):
        if self.control.snapshot().get('pause_requested'):
            return self.runner.pause('manual', '日 K 缺口與已提交資料保留；已安全暫停')
        if self.runner.stage_check('before_day_integrity'):
            return self.runner.pause('memory', '日 K 驗收觸發記憶體保護，已提交進度保留')
        return None

    def run(self, plans):
        scopes = [p for p in plans if p.get('batch_v2') and not p.get('fixed_range') and not p.get('explicit_start')]
        if self.allowed is not None and not self.allowed():
            return dict(schedule_deferred=True)
        if getattr(type(self.repo), 'refresh_lifecycle', None) is not None:
            self.repo.refresh_lifecycle()
        self.repo.ensure_day_integrity_storage()
        entries = []
        self.control.update(stage='day_integrity_audit', day_integrity_checked=0, day_integrity_active=True,
                            current_refresh=False, current_aggregate_from_day=False, current_requery=False)
        for plan in scopes:
            for symbol in self.repo.day_integrity_symbols(plan):
                if self.allowed is not None and not self.allowed():
                    return dict(schedule_deferred=True)
                stopped = self.check_stop()
                if stopped is not None:
                    return dict(stopped=stopped)
                self.control.update(current_symbol=symbol, current_market=plan['market_no'], current_interval='day',
                                    current_target_bars=plan['target_bars'])
                assessment = self.assess(plan, symbol)
                entries.append(dict(plan=plan, symbol=symbol, status=assessment['status'],
                                    unresolved_count=assessment['unresolved_count']))
                self.control.update(stage='day_integrity_audit', day_integrity_checked=len(entries))
        # One bounded window per symbol per round, so illiquid/new securities
        # cannot monopolize requests. Normal interval collection already ran;
        # gaps remain unresolved without becoming a gate for those intervals.
        attempted_windows = committed_windows = 0
        for entry in entries:
            if attempted_windows >= self.budget or self.runner.recovery_windows >= self.runner.recovery_budget:
                break
            if self.allowed is not None and not self.allowed():
                return dict(schedule_deferred=True)
            stopped = self.check_stop()
            if stopped is not None:
                return dict(stopped=stopped)
            plan, symbol = entry['plan'], entry['symbol']
            table, market = plan['candle_table'], plan['market_no']
            gap = self.repo.due_day_gap(table, market, symbol)
            if gap is None:
                continue
            previous = self.repo.previous_day_assessment(table, market, symbol)
            first, last = expanded_day_window(gap['candle_date'], previous['search_start'],
                                               gap['empty_windows'], max_history_days('day'))
            listing = self.runner.history_listing_policy(table, market, symbol)
            if blocked_reason(listing):
                continue
            boundary = (listing or {}).get('history_start')
            if boundary:
                if last < boundary:
                    continue
                first = max(first, boundary)
            valid = active_bounds(listing, first, last)
            if valid is None:
                continue
            first, last = valid
            self.runner.publish(stage='day_integrity_repair', current_symbol=symbol, current_market=market,
                                current_interval='day', window_start=first, window_end=last,
                                day_gap_date=gap['candle_date'])
            attempted_windows += 1
            self.runner.recovery_windows += 1
            try:
                rows = self.runner.capital.fetch_historical_candles(symbol, 'day', first, last, cache_result=False)
            except Exception as error:
                if getattr(type(self.repo), 'record_empty_query_error', None) is not None:
                    self.repo.record_empty_query_error(dict(candle_table=table,market_no=market,symbol=symbol,
                                                          interval_code='day'),first,last,error)
                data_error = find_candle_data_error(error)
                if data_error is None:
                    raise
                self.repo.record_day_gap_data_error(table, market, symbol, first, last, data_error)
                # Other daily symbols still run; a quarantined data error
                # remains unresolved and blocks a false integrity pass.
                continue
            committed = False
            try:
                stop_after_save = self.runner.stage_check('after_day_gap_before_write')
                rows = [row for row in rows if first <= row.timestamp.date() <= last]
                validate_candle_volumes(rows)
                self.repo.commit_day_gap_window(table, market, symbol, first, last, rows,
                                                self.runner.owner, self.retry_seconds,
                                                **({'listing_policy': listing} if listing else {}))
                committed = True
                committed_windows += 1
                self.runner.windows += 1
            except Exception as error:
                data_error = find_candle_data_error(error)
                if data_error is None:
                    raise
                self.repo.record_day_gap_data_error(table, market, symbol, first, last, data_error)
            finally:
                rows.clear()
                if getattr(type(self.runner.capital), 'history_checkpoint', None) is not None:
                    self.runner.capital.history_checkpoint(committed)
            if stop_after_save or self.runner.stage_check('after_day_gap_write_release'):
                return dict(stopped=self.runner.pause('memory', '日 K 缺口已提交，記憶體保護暫停'))
            self.runner.publish(stage='day_integrity_saved', windows=self.runner.windows)
            if self.allowed is not None and not self.allowed():
                return dict(schedule_deferred=True)
            assessment = self.assess(plan, symbol)
            entry.update(status=assessment['status'], unresolved_count=assessment['unresolved_count'])
        if self.allowed is not None and not self.allowed():
            return dict(schedule_deferred=True)
        reviews = self.runner.review_summary(plans)
        review_only = (self.repo.review_only_day_symbols(scopes)
                       if getattr(type(self.repo), 'review_only_day_symbols', None) is not None else {})
        reviewed_entries = [e for e in entries if e['status'] == 'unresolved' and
                            review_only.get((e['plan']['market_no'],e['symbol'])) == e['unresolved_count']]
        reviewed_keys = {(e['plan']['market_no'],e['symbol']) for e in reviewed_entries}
        blocked = sum(e['status'] not in {'complete', 'complete_available_history'} and
                      (e['plan']['market_no'],e['symbol']) not in reviewed_keys for e in entries)
        summary = dict(checked_symbols=len(entries), blocked_symbols=blocked,
                       unresolved_symbols=len({(e['plan']['market_no'],e['symbol']) for e in entries if e['unresolved_count']}),
                       unresolved_dates=sum(e['unresolved_count'] for e in entries),
                       complete_symbols=sum(e['status'] == 'complete' for e in entries),
                       available_history_symbols=sum(e['status'] == 'complete_available_history' for e in entries),
                       pending_review_symbols=reviews['symbols'],pending_review_dates=reviews['dates'],
                       pending_review_periods=reviews['periods'],
                       query_windows=attempted_windows, repaired_windows=committed_windows)
        self.control.update(day_integrity=summary, stage='day_integrity_checked')
        return dict(blocked=blocked, summary=summary,
                    pending_review=reviews['records'], pending_review_summary=reviews,
                    retry_at=datetime.now(timezone.utc).replace(tzinfo=None)+timedelta(seconds=self.retry_seconds))
