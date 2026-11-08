"""Daily re-query goals are fresh per-run counts, not total stored history."""
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.schemas.models import Candle
from app.services.backfill_engine import BackfillRunner
from app.services.backfill_policy import initial_start, refresh_start
from app.services.backfill_scheduler import DailyBackfillConfig, DailyBackfillScheduler
from app.services.backfill_targets import configured_refresh_targets, configured_targets
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLRepositoryError
from test_batch_candle_planning import BatchFake, batch_job
from test_incremental_backfill import FakeControl, plan


def rows(day, count):
    return [Candle(timestamp=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
                   + timedelta(hours=9, minutes=i), open=10, high=12, low=9, close=11, volume=1)
            for i in range(count)]


class RefreshFake(BatchFake):
    def __init__(self, items):
        super().__init__(items)
        self.saved = []

    def stored_count(self, item):
        return int(item.get("received_count") or 0)

    def commit_window(self, item, candles, next_date, empty, **metadata):
        if self.fail_commit:
            raise MySQLRepositoryError("fake transaction rollback")
        self.saved.extend(c.timestamp for c in candles)
        item.update(next_date=next_date, empty_windows=empty,
                    received_count=self.stored_count(item) + len(candles))
        self.order.append((item["symbol"], item["phase"]))
        return self.stored_count(item)

    def finish_incremental_job(self, item, count, exhausted=False):
        state = "completed" if count >= item["target_bars"] else "insufficient_history"
        item.update(status=state)
        return state


class DailyRefreshConfigTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_refresh_and_initialization_defaults_are_separate(self):
        self.assertEqual(configured_refresh_targets(), {
            "1m":1200,"5m":240,"15m":60,"60m":20,"day":10,"week":5,"month":2,
        })
        self.assertEqual(configured_targets()["day"], 1200)
        self.assertEqual(configured_targets()["week"], 560)
        self.assertEqual(configured_targets()["month"], 100)
        config = DailyBackfillConfig.from_env()
        self.assertEqual(config.refresh_bars_by_interval, configured_refresh_targets())
        snapshot = DailyBackfillScheduler(config, Mock()).snapshot()
        self.assertEqual(snapshot["refresh_bars_by_interval"]["week"], 5)
        self.assertEqual(snapshot["target_bars_by_interval"]["week"], 560)

    @patch.dict(os.environ, {"CAPITAL_DAILY_REFRESH_TARGETS": "day:80,week:12,month:3"})
    def test_configurable_refresh_does_not_change_initialization(self):
        self.assertEqual(configured_refresh_targets()["day"], 80)
        self.assertEqual(configured_targets()["day"], 1200)

    @patch.dict(os.environ, {"CAPITAL_DAILY_REFRESH_TARGETS": "day:80"}, clear=True)
    def test_partial_override_keeps_other_refresh_defaults(self):
        self.assertEqual(configured_refresh_targets(), {
            "1m":1200,"5m":240,"15m":60,"60m":20,"day":80,"week":5,"month":2,
        })

    @patch.dict(os.environ, {}, clear=True)
    def test_new_refresh_goals_use_whole_trading_days_and_periods(self):
        end = date(2026, 10, 16)
        expected = {
            "1m":date(2026, 10, 12), "5m":date(2026, 10, 12),
            "15m":date(2026, 10, 13), "60m":date(2026, 10, 13),
            "day":date(2026, 10, 5), "week":date(2026, 9, 14),
            "month":date(2026, 9, 1),
        }
        trading = lambda day: (day.weekday() < 5, "test")
        with patch("app.services.backfill_policy.trading_day_status", side_effect=trading):
            for interval, bars in configured_refresh_targets().items():
                with self.subTest(interval=interval, bars=bars):
                    self.assertEqual(refresh_start(interval, end, bars), expected[interval])

    def test_bad_refresh_goals_are_rejected(self):
        for raw in ("day:0", "week:10001", "bad:100", "day:abc", "day"):
            with self.subTest(raw=raw), patch.dict(os.environ, {"CAPITAL_DAILY_REFRESH_TARGETS": raw}):
                with self.assertRaises(ValueError):
                    configured_refresh_targets()

    def test_day_estimation_excludes_weekends_and_holidays(self):
        end = date(2026, 10, 16)
        holidays = {date(2026, 10, 9), date(2026, 9, 25)}
        trading = lambda day: (day.weekday() < 5 and day not in holidays, "test")
        with patch("app.services.backfill_policy.trading_day_status", side_effect=trading):
            start = refresh_start("day", end, 100)
        valid = [start + timedelta(days=i) for i in range((end-start).days+1) if trading(start+timedelta(days=i))[0]]
        self.assertEqual(len(valid), 100)
        self.assertEqual(valid[0], start)

    def test_minute_estimation_is_only_an_initial_batch_size(self):
        trading = lambda day: (day.weekday() < 5, "test")
        with patch("app.services.backfill_policy.trading_day_status", side_effect=trading):
            self.assertEqual(refresh_start("1m", date(2026, 10, 16), 100), date(2026, 10, 16))
            self.assertEqual(refresh_start("5m", date(2026, 10, 16), 100), date(2026, 10, 15))

    def test_week_and_month_cover_complete_period_starts(self):
        self.assertEqual(refresh_start("week", date(2026, 10, 16), 10), date(2026, 8, 10))
        self.assertEqual(refresh_start("month", date(2026, 9, 30), 2), date(2026, 8, 1))


@patch.object(BackfillRunner, "memory_check", return_value=False)
class DailyRefreshWorkerTests(unittest.TestCase):
    def execute(self, fetch, item=None, repository=None, control=None):
        item = item or batch_job(interval="1m", start=date(2026, 1, 1), end=date(2026, 10, 16),
            next_date=date(2026, 10, 16), history_floor=date(2026, 1, 1), mode="refresh",
            count=50000, received_count=0, target_bars=100, needs_history=False)
        repository = repository or RefreshFake([item])
        control = control or FakeControl()
        runner = BackfillRunner(SimpleNamespace(fetch_historical_candles=fetch), repository, control)
        scope = dict(plan(item), batch_v2=True, planned_on=date.today().isoformat(), planner_version=3,
                     as_of_date=item["end_date"], instrument_table="instruments", target_bars=1200,
                     refresh_bars=item["target_bars"])
        with patch("app.services.backfill_engine.completed_end", return_value=item["end_date"]):
            result = runner.run([scope])
        return result, repository, control, item

    @patch.dict(os.environ, {"CAPITAL_DAILY_REFRESH_TARGETS": ""})
    def test_new_minute_goal_fetches_multiple_days_and_keeps_excess(self, _memory):
        end = date(2026, 10, 16)
        item = batch_job(interval="1m", start=date(2026, 1, 1), end=end,
            next_date=end, history_floor=date(2026, 1, 1), mode="refresh",
            count=50000, received_count=0, target_bars=configured_refresh_targets()["1m"],
            needs_history=False)
        candles = [bar for day in range(12, 17) for bar in rows(date(2026, 10, day), 270)]
        fetch = Mock(return_value=candles)
        trading = lambda day: (day.weekday() < 5, "test")
        with patch("app.services.backfill_policy.trading_day_status", side_effect=trading):
            result, repo, _control, item = self.execute(fetch, item)
        self.assertEqual(result["completed"], 1)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[2:], (date(2026, 10, 12), end))
        self.assertEqual(item["received_count"], 1350)
        self.assertEqual(len(set(repo.saved)), 1350)

    def test_old_database_bars_do_not_count_as_fresh_query_and_excess_is_kept(self, _memory):
        fetch = Mock(return_value=rows(date(2026, 10, 16), 270))
        result, repo, _control, item = self.execute(fetch)
        self.assertEqual(result["completed"], 1)
        fetch.assert_called_once()
        self.assertEqual(len(repo.saved), 270)
        self.assertEqual(item["received_count"], 270)
        self.assertEqual(fetch.call_args.args[2:], (date(2026, 10, 16), date(2026, 10, 16)))
        self.assertFalse(fetch.call_args.kwargs["cache_result"])

    def test_sparse_response_extends_backwards_until_actual_count_meets_goal(self, _memory):
        fetch = Mock(side_effect=[rows(date(2026, 10, 16), 20), rows(date(2026, 10, 15), 80)])
        result, repo, _control, item = self.execute(fetch)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(item["received_count"], 100)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args_list[1].args[3], date(2026, 10, 15))
        self.assertEqual(len(set(repo.saved)), 100)

    def test_duplicate_response_bars_are_not_double_counted(self, _memory):
        data = rows(date(2026, 10, 16), 100)
        result, repo, _control, item = self.execute(Mock(return_value=data + data))
        self.assertEqual(result["completed"], 1)
        self.assertEqual(item["received_count"], 100)
        self.assertEqual(len(repo.saved), 100)

    def test_empty_responses_defer_instead_of_initializing_or_failing(self, _memory):
        result, _repo, _control, item = self.execute(Mock(return_value=[]))
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["received_count"], 0)
        self.assertEqual(item["empty_windows"], 3)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["failed"], 0)

    def test_pause_then_resume_keeps_fresh_count_and_cursor(self, _memory):
        control = FakeControl()
        def first(*_args, **_kwargs):
            control.pause(True)
            return rows(date(2026, 10, 16), 20)
        result, repo, control, item = self.execute(Mock(side_effect=first), control=control)
        self.assertEqual(result["pause_kind"], "manual")
        self.assertEqual(item["received_count"], 20)
        self.assertEqual(item["next_date"], date(2026, 10, 15))
        control.pause(False)
        fetch = Mock(return_value=rows(date(2026, 10, 15), 80))
        result, repo, _control, item = self.execute(fetch, item, repo, control)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(item["received_count"], 100)
        self.assertEqual(fetch.call_args.args[3], date(2026, 10, 15))

    def test_database_failure_does_not_advance_cursor_or_fresh_count(self, _memory):
        item = batch_job(interval="1m", start=date(2026, 1, 1), end=date(2026, 10, 16),
            next_date=date(2026, 10, 16), history_floor=date(2026, 1, 1), mode="refresh",
            received_count=0, target_bars=100, needs_history=False)
        repo = RefreshFake([item]); repo.fail_commit = True
        result, _repo, _control, item = self.execute(Mock(return_value=rows(item["end_date"], 100)), item, repo)
        self.assertEqual(result["pause_kind"], "database")
        self.assertEqual(item["received_count"], 0)
        self.assertEqual(item["next_date"], date(2026, 10, 16))


class RefreshRepositoryTests(unittest.TestCase):
    def test_finishing_refresh_does_not_mark_initialization_complete(self):
        repo = MySQLCandleRepository()
        cursor = Mock(); cursor.fetchone.return_value = ("owner",)
        from contextlib import contextmanager
        @contextmanager
        def transaction(**_kwargs):
            yield cursor
        item = dict(mode="refresh", target_bars=100, id=1, owner_token="owner", job_table="candle_backfill_jobs",
                    candle_table="candles", symbol="2330", interval_code="day", phase="latest")
        with patch.object(repo, "transaction", transaction):
            self.assertEqual(repo.finish_incremental_job(item, 100), "completed")
        self.assertFalse(any("candle_sync_state" in call.args[0] for call in cursor.execute.call_args_list))

    @patch.dict(os.environ, {}, clear=True)
    def test_scheduler_wires_rolling_goals_without_replacing_full_targets(self):
        from dataclasses import replace
        from app.api.routes import run_scheduled_backfill
        control = FakeControl()
        control.worker_active = lambda: False
        control.request_scheduled_priority = lambda settings: (control.snapshot(), False)
        config = replace(DailyBackfillConfig.from_env(), markets=(0,),
                         intervals=("1m", "5m", "15m", "60m", "day", "week", "month"))
        work = [dict(interval=i, target_bars=config.target_bars_by_interval[i]) for i in config.intervals]
        result = dict(processed=7, completed=7, failed=0, insufficient_history=0, deferred=0)
        with patch("app.api.routes.BackfillControl", return_value=control), \
             patch("app.api.routes.MySQLCandleRepository"), \
             patch("app.api.routes.BackfillMemoryGuard"), \
             patch("app.api.routes._sync_instrument_markets_unlocked", return_value=[]), \
             patch("app.api.routes._plan_stale_backfill", return_value=([], work, date(2026, 10, 16), "test")), \
             patch("app.api.routes._run_stale_backfill_unlocked", return_value=result) as runner:
            state, _message = run_scheduled_backfill(SimpleNamespace(quote_ready=True), config)
        self.assertEqual(state, "completed")
        self.assertEqual([p["refresh_bars"] for p in runner.call_args.args[1]], [1200, 240, 60, 20, 10, 5, 2])
        self.assertEqual([p["target_bars"] for p in runner.call_args.args[1]], [1200, 1200, 1200, 1200, 1200, 560, 100])


if __name__ == "__main__":
    unittest.main()
