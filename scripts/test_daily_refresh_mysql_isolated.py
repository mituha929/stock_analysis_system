"""Real MySQL refresh checkpoints in a disposable schema; fake Capital only."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from dotenv import load_dotenv
import mysql.connector
from app.schemas.models import Candle
from app.services.backfill_control import BackfillControl
from app.services.backfill_engine import BackfillRunner
from app.services.backfill_policy import period_start
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLSettings, MySQLRepositoryError
from app.services.manual_daily_queue import archive_and_supersede
from app.services.taiwan_trading_calendar import trading_day_status


def bar(day, minute=0):
    return Candle(timestamp=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
                  + timedelta(hours=9, minutes=minute), open=10, high=12, low=9, close=11, volume=1)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(PROJECT / ".env")
    settings = MySQLSettings.from_env()
    schema = "capital_daily_refresh_test_" + uuid.uuid4().hex[:12]
    assert re.fullmatch(r"capital_daily_refresh_test_[0-9a-f]{12}", schema)
    admin = mysql.connector.connect(host=settings.host, port=settings.port, user=settings.user, password=settings.password)
    created = False
    checks = []
    try:
        cursor = admin.cursor()
        cursor.execute(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        admin.commit(); created = True; cursor.close()
        repo = MySQLCandleRepository(replace(settings, database=schema))

        def query(sql, values=()):
            with repo.transaction() as cursor:
                cursor.execute(sql, values)
                return cursor.fetchall()

        repo.ensure_batch_storage("candles", "candle_backfill_jobs")
        repo.ensure_batch_storage("odd_lot_candle", "odd_lot_candle_backfill_jobs")
        with repo.transaction() as cursor:
            for table in ("instruments", "odd_lot_instruments"):
                cursor.execute(f"CREATE TABLE `{table}`(market_no SMALLINT NOT NULL,symbol VARCHAR(32) NOT NULL,name VARCHAR(128) NOT NULL DEFAULT '',category_no INT NOT NULL,is_active BOOLEAN NOT NULL,PRIMARY KEY(market_no,symbol))")
            cursor.executemany("INSERT INTO instruments(market_no,symbol,category_no,is_active) VALUES(%s,%s,1,TRUE)",
                [(0, "RDAY"), (1, "RWEEK"), (4, "RMONTH"), (0, "RRESUME"), (0, "RFAIL"), (0, "RCOLLIDE"), (0, "RSLOW"), (0, "RNORMAL")])
            cursor.execute("INSERT INTO odd_lot_instruments(market_no,symbol,category_no,is_active) VALUES(6,'RODD',1,TRUE)")
            old = [bar(date(2026, 10, 15)-timedelta(days=i)) for i in range(250)]
            repo._write_rows(cursor, "candles", dict(symbol="RDAY", interval_code="day"), old, datetime(2026, 10, 15))
            cursor.execute("INSERT INTO candle_sync_state VALUES('candles','RDAY','day',FALSE,FALSE,NULL,1200,UTC_TIMESTAMP(6))")

        end = date(2026, 10, 16)
        calls = []
        class Vendor:
            cache = None
            def fetch_historical_candles(self, symbol, interval, first, last, **_options):
                calls.append((symbol, interval, first, last))
                if interval.endswith("m"):
                    return [bar(last, i) for i in range(270)]
                output = []
                cursor = period_start(interval, first)
                while cursor <= last:
                    if interval != "day" or trading_day_status(cursor)[0]:
                        output.append(bar(cursor))
                    cursor = (cursor + timedelta(days=7) if interval == "week" else
                              (cursor.replace(day=28)+timedelta(days=4)).replace(day=1) if interval == "month" else
                              cursor + timedelta(days=1))
                return output

        def scope(symbol, interval, initial, goal, market=0, as_of=end, force=False,
                  force_completed_refresh=False, aggregate_from_day=False, fresh_daily=False):
            odd = market in (5, 6)
            tables = ("odd_lot_candle", "odd_lot_candle_backfill_jobs", "odd_lot_instruments") if odd else ("candles", "candle_backfill_jobs", "instruments")
            summary = repo.plan_batch(market, interval, as_of, initial, *tables, only_symbols=[symbol],
                                      refresh_bars=goal, force=force, recover_running=True,
                                      force_completed_refresh=force_completed_refresh,
                                      fresh_daily=fresh_daily,
                                      aggregate_from_day=aggregate_from_day)
            return dict(market_no=market, interval=interval, start_date=summary["start_date"], end_date=as_of,
                as_of_date=as_of, target_bars=initial, refresh_bars=goal, batch_v2=True,
                planned_on=date.today().isoformat(), planner_version=4 if aggregate_from_day else 3,
                aggregate_from_day=aggregate_from_day, only_symbols=[symbol],
                **({"allowed_ids":summary["fresh_job_ids"]} if "fresh_job_ids" in summary else {}),
                candle_table=tables[0], job_table=tables[1], instrument_table=tables[2]), summary

        with TemporaryDirectory(prefix="daily-refresh-mysql-") as folder, \
             patch.dict(os.environ, {"CAPITAL_SUSPENSION_SYNC_ENABLED": "false", "CAPITAL_DAY_INTEGRITY_ENABLED": "false"}), \
             patch("app.services.listing_history.fetch_twse_listing_origins",return_value={}), \
             patch("app.services.listing_history.fetch_esb_listing_profiles",return_value=()), \
             patch("app.services.instrument_lifecycle.fetch_bond_lifecycles",return_value=()):
            control = BackfillControl(Path(folder) / "control.sqlite3")
            for symbol, interval, initial, goal, market, finish in (
                ("RDAY", "day", 1200, 100, 0, end),
                ("RWEEK", "week", 560, 10, 1, end),
                ("RMONTH", "month", 100, 2, 4, date(2026, 9, 30)),
                ("RODD", "1m", 1200, 100, 6, end),
            ):
                work, summary = scope(symbol, interval, initial, goal, market, finish)
                assert summary["queued_jobs"] == 1
                result = BackfillRunner(Vendor(), repo, control).run([work])
                assert result["completed"] == 1 and result["failed"] == 0
                state = query(f"SELECT j.status,j.received_count,p.target_bars,p.mode FROM `{work['job_table']}` j JOIN candle_backfill_progress p ON p.job_id=j.id AND p.job_table=%s WHERE j.symbol=%s", (work["job_table"], symbol))
                assert state[0][0] == "completed" and state[0][1] >= goal and state[0][2:] == (goal, "refresh"), state
                stored = query("SELECT initialized,target_bars FROM candle_sync_state WHERE symbol=%s", (symbol,))
                assert stored == [(False, initial)], stored
                _, repeat = scope(symbol, interval, initial, goal, market, finish)
                assert repeat["queued_jobs"] == 0
                checks.append(f"{interval}: actual per-run goal {goal}, old initialization goal {initial} preserved, same checked period not requeued")
            assert query("SELECT COUNT(*) FROM candles WHERE symbol='RDAY'")[0][0] == 251
            assert query("SELECT COUNT(*) FROM odd_lot_candle WHERE symbol='RODD'")[0][0] == 270
            checks.append("existing 250 bars retained; refresh upserts overlap and adds only the new day; excess 270 minute bars retained")

            # An explicit manual round must really fetch a completed same-day
            # job again, and overwrite the existing candle in the test schema.
            work, summary = scope("RDAY", "day", 1200, 100, force_completed_refresh=True)
            assert summary["queued_jobs"] == 1, summary
            class ChangedDaily(Vendor):
                def fetch_historical_candles(self, symbol, interval, first, last, **options):
                    return [row.model_copy(update={"close":11.5})
                            for row in super().fetch_historical_candles(symbol, interval, first, last, **options)]
            result = BackfillRunner(ChangedDaily(), repo, control).run([work])
            assert result["completed"] == 1, result
            assert float(query("SELECT close_price FROM candles WHERE symbol='RDAY' AND interval_code='day' AND candle_time=%s",
                               (datetime.combine(end, datetime.min.time()) + timedelta(hours=9),))[0][0]) == 11.5
            checks.append("manual same-day refresh re-fetches and overwrites a completed candle")

            # Weekly/monthly refreshes use daily source bars and must also be
            # eligible for an explicit second run on the same completed day.
            work, summary = scope("RWEEK", "week", 560, 5, market=1, aggregate_from_day=True)
            assert summary["queued_jobs"] == 1
            assert BackfillRunner(Vendor(), repo, control).run([work])["completed"] == 1
            work, summary = scope("RWEEK", "week", 560, 5, market=1, aggregate_from_day=True,
                                  force_completed_refresh=True)
            assert summary["queued_jobs"] == 1
            assert BackfillRunner(ChangedDaily(), repo, control).run([work])["completed"] == 1
            weekly = query("SELECT close_price FROM candles WHERE symbol='RWEEK' AND interval_code='week' AND candle_time=%s",
                           (datetime(2026,10,12),))
            assert len(weekly) == 1 and float(weekly[0][0]) == 11.5, weekly
            checks.append("manual same-day weekly refresh recomputes and overwrites the daily-derived aggregate")

            # Pause after a successful short window, then plan again despite
            # checked_through=end: the remaining fresh count must still run.
            work, _ = scope("RRESUME", "1m", 1200, 100)
            class Pausing(Vendor):
                def fetch_historical_candles(self, symbol, interval, first, last, **_options):
                    control.pause(True)
                    return [bar(last, i) for i in range(20)]
            result = BackfillRunner(Pausing(), repo, control).run([work])
            assert result["pause_kind"] == "manual"
            saved = query("SELECT j.received_count,p.next_date FROM candle_backfill_jobs j JOIN candle_backfill_progress p ON p.job_id=j.id WHERE j.symbol='RRESUME'")[0]
            assert saved == (20, end-timedelta(days=1)), saved
            _, manual_pending = scope("RRESUME", "1m", 1200, 100, force_completed_refresh=True)
            assert manual_pending["queued_jobs"] == 1
            assert query("SELECT j.received_count,p.next_date FROM candle_backfill_jobs j JOIN candle_backfill_progress p ON p.job_id=j.id WHERE j.symbol='RRESUME'")[0] == saved
            checks.append("manual refresh preserves a pending cursor and its received count")
            owned = repo.claim_incremental_job(dict(work, queue_phase="latest"), "failed-refresh-test")
            repo.fail_incremental_job(owned, "injected stock error after partial success")
            control.pause(False)
            _, manual_failed = scope("RRESUME", "1m", 1200, 100, force_completed_refresh=True)
            assert manual_failed["queued_jobs"] == 0
            assert query("SELECT j.status,j.received_count,p.next_date FROM candle_backfill_jobs j JOIN candle_backfill_progress p ON p.job_id=j.id WHERE j.symbol='RRESUME'")[0] == ("failed", *saved)
            checks.append("manual refresh does not reset a failed job or its cursor")
            work, _ = scope("RRESUME", "1m", 1200, 100)
            assert query("SELECT received_count FROM candle_backfill_jobs WHERE symbol='RRESUME'")[0][0] == 20
            assert query("SELECT status FROM candle_backfill_jobs WHERE symbol='RRESUME'")[0][0] == "pending"
            checks.append("failed partial refresh requeues without discarding its committed count or cursor")
            class Remaining(Vendor):
                def fetch_historical_candles(self, symbol, interval, first, last, **_options):
                    assert last == end-timedelta(days=1)
                    return [bar(last, i) for i in range(80)]
            result = BackfillRunner(Remaining(), repo, control).run([work])
            assert result["completed"] == 1
            assert query("SELECT received_count FROM candle_backfill_jobs WHERE symbol='RRESUME'")[0][0] == 100
            checks.append("pending refresh resumes its durable 20-bar count and older cursor, not old stored history or a reset counter")

            # Same-key legacy initialization must remain untouched and must
            # neither be claimed nor counted as daily pending work.
            legacy = repo.plan_batch(0, "day", end, 1200, "candles", "candle_backfill_jobs", "instruments", only_symbols=["RCOLLIDE"])
            before = query("SELECT j.id,p.mode,p.target_bars,p.next_date FROM candle_backfill_jobs j JOIN candle_backfill_progress p ON p.job_id=j.id WHERE j.symbol='RCOLLIDE'")[0]
            work, _ = scope("RCOLLIDE", "day", 1200, 100)
            same = query("SELECT j.id,p.mode,p.target_bars,p.next_date FROM candle_backfill_jobs j JOIN candle_backfill_progress p ON p.job_id=j.id WHERE j.id=%s", (before[0],))[0]
            assert same == before
            assert repo.batch_pending([work])[0] == 1
            owned = repo.claim_incremental_job(dict(work, queue_phase="latest"), "collision-test")
            assert owned["id"] != before[0] and owned["mode"] == "refresh"
            repo.release_incremental_job(owned, "test complete")
            checks.append("legacy initialization job/cursor retained and excluded from daily claim and pending count")

            # A same-date slow refresh must not absorb the new latest round.
            scope("RSLOW", "1m", 1200, 100)
            with repo.transaction() as cursor:
                cursor.execute("SELECT id FROM candle_backfill_jobs WHERE symbol='RSLOW'")
                old_id = cursor.fetchone()[0]
                cursor.execute("UPDATE candle_batch_work SET phase='catchup' WHERE job_table='candle_backfill_jobs' AND job_id=%s", (old_id,))
                cursor.execute("UPDATE candle_backfill_progress SET next_date=%s WHERE job_table='candle_backfill_jobs' AND job_id=%s", (end-timedelta(days=4), old_id))
            work, summary = scope("RSLOW", "1m", 1200, 100)
            assert summary["queued_jobs"] == 1
            same = query("SELECT j.status,p.next_date,w.phase FROM candle_backfill_jobs j JOIN candle_backfill_progress p ON p.job_id=j.id AND p.job_table='candle_backfill_jobs' JOIN candle_batch_work w ON w.job_id=j.id AND w.job_table=p.job_table WHERE j.id=%s", (old_id,))
            assert same == [("pending", end-timedelta(days=4), "catchup")], same
            new = repo.claim_incremental_job(dict(work, queue_phase="latest"), "slow-priority-test")
            assert new and new["id"] != old_id and new["phase"] == "latest", new
            repo.release_incremental_job(new, "test complete")
            checks.append("same-date daily latest uses a distinct job and preserves the slow refresh cursor")

            # Preempting an ordinary latest pass also creates a fresh latest
            # job. Restrict this round to its own IDs so the old partial job
            # does not run before the daily refresh or lose its checkpoint.
            scope("RNORMAL", "1m", 1200, 100)
            with repo.transaction() as cursor:
                cursor.execute("SELECT id FROM candle_backfill_jobs WHERE symbol='RNORMAL'")
                old_id = cursor.fetchone()[0]
                cursor.execute("UPDATE candle_backfill_progress SET next_date=%s WHERE job_table='candle_backfill_jobs' AND job_id=%s", (end-timedelta(days=3), old_id))
                cursor.execute("UPDATE candle_backfill_jobs SET received_count=20 WHERE id=%s", (old_id,))
            work, summary = scope("RNORMAL", "1m", 1200, 100, fresh_daily=True, force_completed_refresh=True)
            assert old_id not in work["allowed_ids"] and len(work["allowed_ids"]) == 1
            assert repo.batch_pending([work])[0] == 1
            new = repo.claim_incremental_job(dict(work, queue_phase="latest"), "ordinary-priority-test")
            assert new["id"] != old_id and new["next_date"] == end
            repo.release_incremental_job(new, "verify new round")
            assert BackfillRunner(Vendor(), repo, control).run([work])["completed"] == 1
            assert repo.batch_pending([work])[0] == 0
            old = query("SELECT j.status,j.received_count,p.next_date,w.phase FROM candle_backfill_jobs j JOIN candle_backfill_progress p ON p.job_id=j.id AND p.job_table='candle_backfill_jobs' JOIN candle_batch_work w ON w.job_id=j.id AND w.job_table=p.job_table WHERE j.id=%s", (old_id,))
            assert old == [("pending",20,end-timedelta(days=3),"latest")], old
            checks.append("ordinary collection yields to a separate fresh daily round; its partial count and cursor survive unchanged")

            # Force an exception after data insertion; both data and progress
            # must roll back, including the per-run received_count.
            work, _ = scope("RFAIL", "1m", 1200, 100)
            owned = repo.claim_incremental_job(dict(work, queue_phase="latest"), "rollback-test")
            original = repo._write_rows
            def fail(*args):
                original(*args)
                raise RuntimeError("injected rollback")
            try:
                with patch.object(repo, "_write_rows", side_effect=fail):
                    repo.commit_window(owned, [bar(end, i) for i in range(100)], end-timedelta(days=1), 0, window_start=end, window_end=end)
            except MySQLRepositoryError:
                pass
            else:
                raise AssertionError("expected rollback")
            assert query("SELECT COUNT(*) FROM candles WHERE symbol='RFAIL'")[0][0] == 0
            saved = query("SELECT j.received_count,p.next_date FROM candle_backfill_jobs j JOIN candle_backfill_progress p ON p.job_id=j.id WHERE j.symbol='RFAIL'")[0]
            assert saved == (0, end), saved
            assert not query("SELECT checked_through FROM candle_check_state WHERE symbol='RFAIL'")
            checks.append("data/check state/received count/cursor roll back atomically")

            # Switching daily work archives only pending/running jobs, with
            # durable checkpoints, then removes them from the runnable queue.
            before = query("SELECT j.id,j.status,j.received_count,p.next_date FROM candle_backfill_jobs j "
                           "JOIN candle_backfill_progress p ON p.job_id=j.id AND p.job_table='candle_backfill_jobs' "
                           "WHERE j.symbol='RFAIL'")[0]
            token = uuid.uuid4().hex
            count = archive_and_supersede(repo, [], token)
            assert count > 0
            after = query("SELECT status FROM candle_backfill_jobs WHERE id=%s", (before[0],))[0][0]
            archived = query("SELECT original_status,JSON_UNQUOTE(JSON_EXTRACT(job_snapshot,'$.next_date')) "
                             "FROM manual_daily_displaced_jobs WHERE request_id=%s AND job_table='candle_backfill_jobs' AND job_id=%s",
                             (token,before[0]))[0]
            assert after == 'superseded' and archived == (before[1],str(before[3])), (after,archived,before)
            assert archive_and_supersede(repo, [], token) == count
            _, new = scope("RFAIL", "1m", 1200, 100, force_completed_refresh=True)
            assert new["queued_jobs"] == 1
            assert query("SELECT original_status FROM manual_daily_displaced_jobs WHERE request_id=%s AND job_id=%s",
                         (token,before[0]))[0][0] == before[1]
            checks.append("safe switch archives and removes old pending/running jobs, is idempotent, and permits fresh daily planning")

            # Exercise the actual one-command handoff and daily runner with
            # fake Capital and this same isolated schema, including HTTP task
            # dispatch and persistent control state.
            from fastapi import BackgroundTasks
            from app.api import routes
            from app.services.backfill_scheduler import DailyBackfillConfig
            with patch.dict(os.environ,{"CAPITAL_BACKFILL_CONTROL_DB":str(control.path)}), \
                 patch("app.api.routes.MySQLCandleRepository",return_value=repo), \
                 patch("app.api.routes._sync_instrument_markets_unlocked",return_value=[]):
                old_run_id=repo.start_daily_backfill_run({"kind":"isolated-old-run"})
                control.update(state="paused",pause_requested=True,pause_kind="manual",
                    plans=[dict(market_no=0,interval="1m",job_table="candle_backfill_jobs",run_id=old_run_id)],
                    manual_daily_run=dict(id="stale-paused-request",phase="running",state="paused",
                                          requested_at="2020-01-01T00:00:00+08:00"),
                    pending_schedule=None,resume_pending=False,preparation_complete=True)
                config=replace(DailyBackfillConfig.from_env(),markets=(0,),intervals=("day",),
                               weekly_intervals=(),monthly_intervals=(),
                               refresh_bars_by_interval={"day":10})
                vendor=Vendor(); vendor.quote_ready=True
                request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
                    capital_service=vendor,backfill_scheduler=SimpleNamespace(config=config))))
                tasks=BackgroundTasks()
                accepted=routes.run_daily_backfill_now(tasks,request)
                assert accepted["created"] and accepted["replaces_request_id"] == "stale-paused-request"
                assert accepted["request_id"] != "stale-paused-request"
                assert control.snapshot()["manual_daily_run"]["phase"] == "waiting_stop"
                asyncio.run(tasks())
                finished=control.snapshot()
                assert finished["manual_daily_run"]["phase"] == "done", finished.get("manual_daily_run")
                assert finished["manual_daily_run"]["archived_jobs"] >= 1
                assert finished["manual_daily_run"]["id"] == accepted["request_id"]
                assert finished["state"] == "completed", finished.get("pause_reason")
                assert query("SELECT status,stage FROM daily_backfill_runs WHERE id=%s",(old_run_id,)) == [('superseded','superseded')]
                assert query("SELECT COUNT(*) FROM candles WHERE symbol='RDAY'")[0][0] >= 251
                checks.append("one command safely removes the old queue and completes a new fake-vendor daily run")

        print(json.dumps(dict(passed=len(checks), checks=checks, production_database_modified=False,
                              capital_dll_used=False), ensure_ascii=False), flush=True)
    finally:
        if created:
            cursor = admin.cursor()
            cursor.execute(f"DROP DATABASE `{schema}`")
            admin.commit(); cursor.close()
            print("已清除本次建立的日常回抓隔離測試資料庫。", flush=True)
        admin.close()


if __name__ == "__main__":
    main()
