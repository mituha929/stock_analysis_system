import unittest
from datetime import date
from unittest.mock import patch,Mock
from types import SimpleNamespace

from app.services.backfill_targets import configured_targets,target_for,refresh_plan_targets,parse_targets
from app.services.backfill_policy import select_range
from app.services.batch_candle_planning import choose_work
from app.services.backfill_scheduler import DailyBackfillConfig
from app.api.routes import _plan_stale_backfill
from scripts.backfill_market_candles import build_parser
import test_batch_candle_planning as batch_fixture
from test_batch_candle_planning import batch_job
from test_incremental_backfill import candle


class TargetPolicyTests(unittest.TestCase):
    @patch.dict("os.environ",{},clear=True)
    def test_defaults_all_entrypoints(self):
        config=DailyBackfillConfig.from_env()
        self.assertEqual(configured_targets(),dict.fromkeys(("1m","5m","15m","60m","day"),1200)|{"week":560,"month":100})
        self.assertEqual(config.target_bars_by_interval,configured_targets())
        self.assertIsNone(build_parser().parse_args([]).target_bars)
        for interval,count in (("week",560),("month",100)):
            self.assertFalse(choose_work(interval,date(2026,9,30),count,date(2020,1,1),date(2026,9,1),{},target_for(interval))["needs_history"])
            self.assertEqual(select_range(interval,date(2026,9,30),count,date(2026,9,1),{})[0],"incremental")

    @patch.dict("os.environ",{},clear=True)
    def test_explicit_overrides_still_possible(self):
        self.assertEqual(target_for("week",1200),1200)
        self.assertEqual(target_for("month",150),150)
        self.assertEqual(build_parser().parse_args(["--target-bars","1200"]).target_bars,1200)

    @patch.dict("os.environ",{"CAPITAL_DAILY_BACKFILL_TARGETS":"week:600,month:120"})
    def test_configured_per_interval_overrides(self):
        self.assertEqual(target_for("week"),600)
        self.assertEqual(target_for("month"),120)
        self.assertEqual(target_for("day"),1200)

    @patch.dict("os.environ",{},clear=True)
    def test_old_blanket_1200_plans_migrate_without_resetting_dates(self):
        plans=[dict(interval=i,target_bars=1200,planned_on="2026-10-15",start_date="2020-01-01",end_date="2026-09-30") for i in ("day","week","month")]
        refresh_plan_targets(plans)
        self.assertEqual([p["target_bars"] for p in plans],[1200,560,100])
        self.assertEqual(plans[0]["planned_on"],"2026-10-15")
        self.assertIsNone(plans[1]["planned_on"])
        self.assertEqual(plans[1]["start_date"],"2020-01-01")

    @patch.dict("os.environ",{},clear=True)
    def test_deliberate_1200_override_is_not_migrated(self):
        plans=[dict(interval="week",target_bars=1200,target_source="override",planned_on="2026-10-15")]
        refresh_plan_targets(plans)
        self.assertEqual(plans[0]["target_bars"],1200)
        self.assertEqual(plans[0]["planned_on"],"2026-10-15")

    @patch.dict("os.environ",{},clear=True)
    @patch("app.api.routes.BackfillControl")
    @patch("app.api.routes.MySQLCandleRepository")
    def test_api_auto_group_targets_are_not_all_1200(self,repo,control):
        control.return_value.worker_active.return_value=False
        plans,work,_,_=_plan_stale_backfill([0],["day","week","month"],None,True,today=date(2026,10,15))
        self.assertEqual([p["target_bars"] for p in work],[1200,560,100])
        self.assertEqual([p.target_bars for p in plans],[1200,560,100])
        self.assertTrue(all(p["target_source"]=="config" for p in work))
        repo.assert_not_called()

    def test_invalid_goals_are_rejected(self):
        for raw in ("week:0","month:10001","bad:100","week:abc","week"):
            with self.assertRaises(ValueError): parse_targets(raw)


class GoalIsNotCapTests(unittest.TestCase):
    @patch.dict("os.environ",{},clear=True)
    def test_latest_rows_above_goal_are_not_dropped(self):
        from app.services.backfill_engine import BackfillRunner
        with patch.object(BackfillRunner,"memory_check",return_value=False):
            for interval,goal in (("day",1200),("week",560),("month",100)):
                item=batch_job(interval=interval,start=date(2026,9,1),end=date(2026,9,30),count=goal,target_bars=goal)
                fixture=batch_fixture.BatchWorkerTests()
                vendor=SimpleNamespace(fetch_historical_candles=Mock(return_value=[candle(date(2026,9,15))]))
                fixture.execute([item],vendor,job_window_budget=10)
                self.assertGreater(item["count"],goal)
                vendor.fetch_historical_candles.assert_called()
