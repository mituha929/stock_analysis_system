"""Trading-day collection / holiday maintenance, no live SQL or vendor."""
from copy import deepcopy
from contextlib import nullcontext
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pytz

from app.services.backfill_engine import BackfillRunner
from app.services.backfill_control import BackfillRecovery
from app.services.backfill_scheduler import DailyBackfillConfig, DailyBackfillScheduler
from app.services.capital_process_lock import CapitalProcessLockError
from app.services.day_candle_integrity import DayCalendar, DayEvidence, integrity_schedule, taipei_today
from app.services.day_candle_integrity_maintenance import DayMaintenanceControl, run_day_maintenance
from app.services.mysql_instrument_repository import MySQLRepositoryError
from app.services.trading_suspensions import SuspensionCalendar
from test_batch_candle_planning import BatchFake, batch_job
from test_day_candle_integrity import MemoryAuditRepo, sessions
from test_day_first_backfill import scope
from test_incremental_backfill import FakeControl, candle

MARKETS = {0: ('上市', 'instruments', 'candles', 'candle_backfill_jobs')}
FRIDAY = date(2026, 10, 23)
SATURDAY = date(2026, 10, 24)


class IntegrityScheduleTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict('os.environ', {'CAPITAL_DAY_INTEGRITY_ENABLED': 'true',
                                      'CAPITAL_DAY_INTEGRITY_SCHEDULE': 'non_trading_days'})
        env.start(); self.addCleanup(env.stop)

    def test_trading_weekday_skips_full_history_audit(self):
        with patch('app.services.day_candle_integrity.DayCalendar', side_effect=sessions):
            self.assertFalse(integrity_schedule(FRIDAY)[0])

    def test_weekend_uses_local_snapshot_without_network(self):
        # Local lookup now honors historical Saturday trading exceptions.
        with patch('app.services.taiwan_trading_calendar.urlopen', side_effect=AssertionError('no network')):
            for day in (SATURDAY, SATURDAY+timedelta(days=1)):
                self.assertTrue(integrity_schedule(day)[0])

    def test_official_weekday_closure_and_administrator_override_are_eligible(self):
        for payload, overrides in (({'source': 'https://www.twse.com.tw/calendar',
                                     'years': {'2026': [str(FRIDAY)]}}, []),
                                   ({'years': {}}, [FRIDAY])):
            with patch('app.services.day_candle_integrity.DayCalendar',
                       return_value=DayCalendar(payload, overrides=overrides)):
                self.assertTrue(integrity_schedule(FRIDAY)[0])

    def test_unknown_weekday_calendar_does_not_start_an_audit(self):
        with patch('app.services.day_candle_integrity.DayCalendar',
                   return_value=DayCalendar({'years': {}}, overrides=[])):
            allowed, reason = integrity_schedule(FRIDAY)
        self.assertFalse(allowed); self.assertIn('尚未核實', reason)

    def test_explicit_disable_always_override_and_invalid_mode(self):
        with patch.dict('os.environ', {'CAPITAL_DAY_INTEGRITY_SCHEDULE': 'always'}):
            self.assertTrue(integrity_schedule(FRIDAY)[0])
            with patch.dict('os.environ', {'CAPITAL_DAY_INTEGRITY_ENABLED': 'false'}):
                self.assertFalse(integrity_schedule(SATURDAY)[0])
        with patch.dict('os.environ', {'CAPITAL_DAY_INTEGRITY_SCHEDULE': 'typo'}):
            with self.assertRaises(ValueError): integrity_schedule(FRIDAY)

    def test_schedule_clock_uses_taipei_not_host_date_or_saved_asof(self):
        with patch('app.services.day_candle_integrity.datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 24, 0, 5, tzinfo=timezone(timedelta(hours=8)))
            self.assertEqual(taipei_today(), SATURDAY)
            self.assertEqual(clock.now.call_args.args[0].utcoffset(None), timedelta(hours=8))

    @patch.object(BackfillRunner, 'memory_check', return_value=False)
    def test_weekday_skips_stale_failed_audit_and_allows_other_periods(self, _memory):
        class AuditableRepo(BatchFake):
            def ensure_day_integrity_storage(self): raise AssertionError('weekday audit forbidden')
        daily = batch_job('DAY', status='completed')
        minute = batch_job('MIN', interval='1m', id=2, count=0)
        repo = AuditableRepo([daily, minute]); control = FakeControl()
        control.update(day_integrity={'blocked_symbols': 19, 'unresolved_dates': 50})
        vendor = SimpleNamespace(fetch_historical_candles=lambda s,i,f,l,**kw:[candle(f)])
        with patch('app.services.backfill_engine.integrity_schedule', return_value=(False, '交易日')), \
             patch('app.services.day_candle_integrity_runner.DayIntegrityAudit') as audit:
            result = BackfillRunner(vendor, repo, control).run([scope(minute), scope(daily)])
        audit.assert_not_called()
        self.assertEqual(minute['status'], 'completed')
        self.assertFalse(result.get('day_first_waiting'))
        self.assertEqual(control.state['day_integrity']['blocked_symbols'], 19)
        self.assertFalse(control.state['day_integrity_scheduled'])


class HolidaySchedulerTests(unittest.TestCase):
    def scheduler(self, **changes):
        with patch.dict('os.environ', {}, clear=True):
            config = replace(DailyBackfillConfig.from_env(), enabled=True, **changes)
        regular = Mock(return_value=('completed', 'regular'))
        maintenance = Mock(return_value=('completed', 'audit'))
        scheduler = DailyBackfillScheduler(config, regular, maintenance_callback=maintenance)
        return scheduler, regular, maintenance

    def at(self, day, hour=18, minute=30):
        return pytz.timezone('Asia/Taipei').localize(datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute))

    def tick(self, scheduler, now):
        with patch.object(scheduler, '_now', return_value=now), \
             patch('app.services.backfill_scheduler.integrity_schedule', return_value=(True, '休市')):
            scheduler._tick(now)

    def test_holiday_runs_only_audit_after_existing_scheduled_time_once(self):
        scheduler, regular, maintenance = self.scheduler()
        self.tick(scheduler, self.at(SATURDAY, 18, 29))
        maintenance.assert_not_called()
        self.tick(scheduler, self.at(SATURDAY))
        self.tick(scheduler, self.at(SATURDAY, 18, 31))
        regular.assert_not_called(); maintenance.assert_called_once()
        self.assertEqual(scheduler._state, 'completed')

    def test_waiting_gaps_retry_in_900_seconds_not_every_15_seconds(self):
        scheduler, regular, maintenance = self.scheduler()
        maintenance.return_value = ('waiting_backfill', 'gaps')
        start = self.at(SATURDAY)
        with patch.dict('os.environ', {'CAPITAL_DAY_GAP_RETRY_SECONDS': '900'}):
            self.tick(scheduler, start)
            self.tick(scheduler, start+timedelta(seconds=15))
            self.assertEqual(maintenance.call_count, 1)
            with patch.object(scheduler, '_now', return_value=start+timedelta(seconds=15)):
                self.assertEqual(scheduler._next_run_at(), start+timedelta(seconds=900))
            self.tick(scheduler, start+timedelta(seconds=900))
        self.assertEqual(maintenance.call_count, 2); regular.assert_not_called()

    def test_holiday_retry_is_not_carried_into_the_next_trading_day(self):
        scheduler, regular, maintenance = self.scheduler()
        maintenance.return_value = ('waiting_backfill', 'gaps')
        self.tick(scheduler, self.at(SATURDAY+timedelta(days=1), 23, 59))
        # 2026-10-26 is a configured market closure; Tuesday is the next
        # trading day in this fixture's calendar.
        next_trade = SATURDAY+timedelta(days=3)
        self.tick(scheduler, self.at(next_trade, 0, 15))
        regular.assert_not_called(); self.assertEqual(maintenance.call_count, 1)
        self.tick(scheduler, self.at(next_trade))
        regular.assert_called_once(); self.assertEqual(maintenance.call_count, 1)

    def test_trade_day_calls_only_regular_update(self):
        scheduler, regular, maintenance = self.scheduler()
        # 10/23 is now a confirmed typhoon closure, not a regular Friday.
        self.tick(scheduler, self.at(date(2026,10,22)))
        regular.assert_called_once(); maintenance.assert_not_called()

    def test_disabled_or_day_unselected_never_adds_holiday_work(self):
        scheduler, regular, maintenance = self.scheduler()
        scheduler.config = replace(scheduler.config, enabled=False)
        self.tick(scheduler, self.at(SATURDAY))
        regular.assert_not_called(); maintenance.assert_not_called()
        scheduler.config = replace(scheduler.config, enabled=True, intervals=('1m',))
        self.tick(scheduler, self.at(SATURDAY))
        regular.assert_not_called(); maintenance.assert_not_called()

    def test_calendar_not_verified_does_not_turn_a_holiday_skip_into_audit(self):
        scheduler, regular, maintenance = self.scheduler()
        now = self.at(SATURDAY)
        with patch.object(scheduler, '_now', return_value=now), \
             patch('app.services.backfill_scheduler.integrity_schedule', return_value=(False, 'unknown')):
            scheduler._tick(now)
        regular.assert_not_called(); maintenance.assert_not_called()


class HolidayMaintenanceTests(unittest.TestCase):
    def setUp(self):
        patches = [
            patch('app.services.day_candle_integrity_maintenance.integrity_schedule', return_value=(True, '休市')),
            patch('app.services.day_candle_integrity_maintenance.taipei_today', return_value=SATURDAY),
            patch('app.services.day_candle_integrity_maintenance.latest_trading_day_on_or_before', return_value=FRIDAY),
            patch('app.services.day_candle_integrity_maintenance.load_suspensions', return_value=SuspensionCalendar()),
            patch('app.services.day_candle_integrity_runner.DayEvidence.load', return_value=DayEvidence()),
            patch('app.services.day_candle_integrity_runner.DayCalendar', side_effect=sessions),
            patch.object(BackfillRunner, 'memory_check', return_value=False),
        ]
        for item in patches:
            item.start(); self.addCleanup(item.stop)
        with patch.dict('os.environ', {}, clear=True):
            self.config = replace(DailyBackfillConfig.from_env(), markets=(0,),
                                  target_bars_by_interval={'day': 3})
        self.control = FakeControl()
        self.control.update(state='waiting_backfill', source='api', processed=5660, completed=5641,
                            plans=[{'market_no': 0, 'interval': 'day', 'run_id': 123}],
                            next_retry_at='2026-10-24T13:00:00Z', checkpoint='2026-09-03')
        self.repo = MemoryAuditRepo([date(2026,10,20), date(2026,10,21), FRIDAY])
        self.factory = Mock(return_value=self.repo)
        self.vendor = SimpleNamespace(quote_ready=True, cache=None,
                                      fetch_historical_candles=Mock(return_value=[candle(date(2026,10,22))]))

    def run_maintenance(self):
        return run_day_maintenance(self.vendor, self.config, self.control, self.factory, MARKETS)

    def test_gap_repair_preserves_original_plans_counts_and_retry_checkpoint(self):
        original = deepcopy(self.control.snapshot())
        state, _message = self.run_maintenance()
        self.assertEqual(state, 'completed')
        for key, value in original.items(): self.assertEqual(self.control.snapshot()[key], value)
        self.assertIn(date(2026,10,22), self.repo.days)
        self.assertEqual(self.repo.saved['as_of'], FRIDAY)
        self.assertEqual(self.repo.saved['status'], 'complete')
        self.assertFalse(self.control.snapshot()['day_integrity_maintenance']['active'])

    def test_empty_source_remains_pending_not_complete_and_does_not_replan(self):
        self.vendor.fetch_historical_candles.return_value = []
        state, _message = self.run_maintenance()
        self.assertEqual(state, 'waiting_backfill')
        self.assertEqual(self.repo.saved['status'], 'unresolved')
        self.assertEqual(self.control.snapshot()['processed'], 5660)
        self.assertEqual(self.control.snapshot()['plans'][0]['run_id'], 123)

    def test_manual_pause_is_respected_and_never_cleared(self):
        self.control.pause(True)
        state, _message = self.run_maintenance()
        self.assertEqual(state, 'paused'); self.factory.assert_not_called()
        self.vendor.fetch_historical_candles.assert_not_called()
        self.assertTrue(self.control.snapshot()['pause_requested'])

    def test_missing_login_never_connects_or_constructs_repository(self):
        self.vendor.quote_ready = False
        state, _message = self.run_maintenance()
        self.assertEqual(state, 'waiting_connection'); self.factory.assert_not_called()
        self.vendor.fetch_historical_candles.assert_not_called()

    def test_memory_protection_does_not_start_audit(self):
        with patch('app.services.day_candle_integrity_maintenance.memory_resume_ready', return_value=False):
            state, _message = self.run_maintenance()
        self.assertEqual(state, 'waiting_memory'); self.factory.assert_not_called()

    def test_new_user_pause_is_visible_through_progress_namespace(self):
        progress = DayMaintenanceControl(self.control)
        progress.update(pause_requested=False, state='running')
        self.control.pause(True)
        self.assertTrue(progress.snapshot()['pause_requested'])
        self.assertEqual(self.control.snapshot()['state'], 'waiting_backfill')

    def test_crossing_into_trading_day_stops_before_next_query(self):
        with patch('app.services.day_candle_integrity_maintenance.integrity_schedule',
                   side_effect=[(True, '休市'), (False, '交易日')]):
            state, _message = self.run_maintenance()
        self.assertEqual(state, 'skipped')
        self.vendor.fetch_historical_candles.assert_not_called()
        self.assertFalse(self.control.snapshot()['day_integrity_maintenance']['active'])

    def test_window_received_at_boundary_is_saved_before_stopping(self):
        with patch('app.services.day_candle_integrity_maintenance.integrity_schedule',
                   side_effect=[(True, '休市')]*4+[(False, '交易日')]):
            state, _message = self.run_maintenance()
        self.assertEqual(state, 'skipped')
        self.vendor.fetch_historical_candles.assert_called_once()
        self.assertIn(date(2026,10,22), self.repo.days)
        self.assertEqual(len(self.repo.commits), 1)

    def test_startup_clears_only_abandoned_maintenance_indicator(self):
        self.control.update(day_integrity_maintenance={'active': True, 'state': 'running'})
        self.control.worker_active = lambda: False
        with patch('app.services.backfill_control.BackfillControl', return_value=self.control), \
             patch('app.services.backfill_control.threading.Thread'):
            BackfillRecovery(self.vendor, Mock(), Mock()).start()
        self.assertEqual(self.control.snapshot()['day_integrity_maintenance']['state'], 'interrupted')
        self.assertFalse(self.control.snapshot()['day_integrity_maintenance']['active'])
        self.assertEqual(self.control.snapshot()['state'], 'waiting_backfill')
        self.assertEqual(self.control.snapshot()['plans'][0]['run_id'], 123)

    def test_database_error_keeps_ordinary_work_state_and_can_retry(self):
        self.repo.ensure_day_integrity_storage = Mock(side_effect=MySQLRepositoryError('SQL offline'))
        state, _message = self.run_maintenance()
        self.assertEqual(state, 'waiting_database')
        self.assertEqual(self.control.snapshot()['state'], 'waiting_backfill')
        self.assertEqual(self.control.snapshot()['checkpoint'], '2026-09-03')


class HolidayLeaseTests(unittest.TestCase):
    def test_api_queue_contention_does_not_start_maintenance_or_touch_control(self):
        from app.api.routes import run_scheduled_day_integrity
        queue = Mock(); queue.acquire.return_value = False
        with patch('app.api.routes.BACKFILL_WORKER_LOCK', queue), \
             patch('app.api.routes.BackfillControl') as control, \
             patch('app.services.day_candle_integrity_maintenance.run_day_maintenance') as work:
            state, _message = run_scheduled_day_integrity(Mock(), Mock())
        self.assertEqual(state, 'waiting_backfill')
        work.assert_not_called(); control.assert_not_called(); queue.release.assert_not_called()

    def test_os_lease_contention_does_not_reclaim_cli_work(self):
        from app.api.routes import run_scheduled_day_integrity
        queue = Mock(); queue.acquire.return_value = True
        with patch('app.api.routes.BACKFILL_WORKER_LOCK', queue), \
             patch('app.api.routes.BackfillControl') as control, \
             patch('app.services.day_candle_integrity_maintenance.run_day_maintenance') as work:
            control.return_value.worker.side_effect = CapitalProcessLockError('busy')
            state, _message = run_scheduled_day_integrity(Mock(), Mock())
        self.assertEqual(state, 'waiting_backfill')
        work.assert_not_called(); control.return_value.update.assert_not_called()
        queue.release.assert_called_once()

    def test_callback_holds_both_queues_without_using_normal_master_sync(self):
        from app.api.routes import run_scheduled_day_integrity
        queue = Mock(); queue.acquire.return_value = True
        with patch('app.api.routes.BACKFILL_WORKER_LOCK', queue), \
             patch('app.api.routes.BackfillControl') as control, \
             patch('app.api.routes._sync_instrument_markets_unlocked') as sync, \
             patch('app.services.day_candle_integrity_maintenance.run_day_maintenance',
                   return_value=('completed', 'ok')) as work:
            control.return_value.worker.return_value = nullcontext()
            state, _message = run_scheduled_day_integrity(Mock(), Mock())
        self.assertEqual(state, 'completed')
        work.assert_called_once(); control.return_value.worker.assert_called_once()
        sync.assert_not_called(); queue.release.assert_called_once()


if __name__ == '__main__': unittest.main()
