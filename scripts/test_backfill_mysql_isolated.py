"""Integration test in a newly created, disposable database; no Capital DLL.

Never falls back to production tables if CREATE DATABASE is not permitted.
The only DROP target is the random schema created by THIS invocation.
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))

from dotenv import load_dotenv
import mysql.connector
from app.schemas.models import Candle
from app.services.backfill_control import BackfillControl
from app.services.backfill_engine import BackfillRunner
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLSettings,MySQLRepositoryError


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(PROJECT/".env")
    settings=MySQLSettings.from_env()
    schema="capital_backfill_test_"+uuid.uuid4().hex[:12]
    if not re.fullmatch(r"capital_backfill_test_[0-9a-f]{12}",schema):
        raise RuntimeError("Invalid isolated test schema")
    admin=mysql.connector.connect(host=settings.host,port=settings.port,user=settings.user,
        password=settings.password,connection_timeout=10)
    created=False
    checks=[]
    try:
        cursor=admin.cursor()
        cursor.execute(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        admin.commit(); created=True; cursor.close()
        repo=MySQLCandleRepository(replace(settings,database=schema))
        repo.ensure_incremental_storage("candles","candle_backfill_jobs")
        with repo.transaction() as cursor:
            cursor.execute("CREATE TABLE instruments(market_no SMALLINT NOT NULL,symbol VARCHAR(32) NOT NULL,category_no INT NOT NULL,is_active BOOLEAN NOT NULL,PRIMARY KEY(market_no,symbol))")
            cursor.executemany("INSERT INTO instruments VALUES(%s,%s,%s,TRUE)",[(0,"2330",1),(0,"2317",1)])
        end=date(2026,10,14)
        def make_plan(interval,end_date,summary):
            return dict(market_no=0,interval=interval,start_date=summary["start_date"],end_date=end_date,
                job_table="candle_backfill_jobs",candle_table="candles",target_bars=1200)
        def query(sql,params=()):
            with repo.transaction() as cursor:
                cursor.execute(sql,params)
                return cursor.fetchall()
        class Vendor:
            cache=None
            def __init__(self): self.calls=[]
            def fetch_historical_candles(self,symbol,interval,start,finish,**_options):
                self.calls.append((symbol,interval,start,finish))
                first=max(start,end-timedelta(days=1199))
                return [Candle(timestamp=datetime.combine(first+timedelta(days=i),datetime.min.time(),tzinfo=timezone.utc),
                    open=10,high=12,low=9,close=11,volume=100) for i in range(max(0,(finish-first).days+1))]
        # Legacy SQL fixtures contain synthetic weekend candles; test their
        # checkpoint contracts separately from the strict calendar audit.
        with TemporaryDirectory(prefix="capital-mysql-test-") as folder, \
             patch.dict('os.environ',{'CAPITAL_KLINE_MAX_DAYS_DAY':'400'}), \
             patch('app.services.backfill_engine.integrity_enabled',return_value=False):
            control=BackfillControl(Path(folder)/"control.sqlite3")
            group=repo.plan_incremental(0,"day",end,1200,"candles","candle_backfill_jobs","instruments")
            assert group["initialization_jobs"]==2
            vendor=Vendor()
            result=BackfillRunner(vendor,repo,control).run([make_plan("day",end,group)])
            assert result["completed"]==2 and result["failed"]==0
            assert query("SELECT COUNT(*) FROM candles")[0][0]==2400
            assert len(vendor.calls)==6
            checks.append("1200-bar reverse initialization stops at target")
            group=repo.plan_incremental(0,"day",end,1200,"candles","candle_backfill_jobs","instruments")
            assert group["queued_jobs"]==0
            checks.append("same successful as-of date is not queued again")
            tomorrow=end+timedelta(days=1)
            group=repo.plan_incremental(0,"day",tomorrow,1200,"candles","candle_backfill_jobs","instruments")
            assert group["start_date"]==end and group["incremental_jobs"]==2
            result=BackfillRunner(vendor,repo,control).run([make_plan("day",tomorrow,group)])
            assert result["completed"]==2
            assert query("SELECT COUNT(*) FROM candles")[0][0]==2402
            checks.append("incremental overlap upserts without duplicate bars")
            with repo.transaction() as cursor:
                cursor.execute("INSERT INTO candle_backfill_jobs(market_no,symbol,interval_code,start_date,end_date,status,updated_at) VALUES(0,'2330','month','1995-09-05','2026-09-22','pending',UTC_TIMESTAMP(6))")
            repo.plan_incremental(0,"month",date(2026,9,30),1200,"candles","candle_backfill_jobs","instruments")
            assert query("SELECT status FROM candle_backfill_jobs WHERE interval_code='month' AND start_date='1995-09-05'")[0][0]=="superseded"
            checks.append("obsolete pending history is retained as superseded")
            group=repo.plan_incremental(0,"1m",tomorrow,1200,"candles","candle_backfill_jobs","instruments",explicit_start=date(2026,10,1),only_symbols=["2317"])
            scope=make_plan("1m",tomorrow,group)
            class PausingVendor(Vendor):
                def fetch_historical_candles(self,*args,**options):
                    rows=super().fetch_historical_candles(*args,**options)
                    control.pause(True)
                    return rows
            result=BackfillRunner(PausingVendor(),repo,control).run([scope])
            assert result["pause_kind"]=="manual"
            checkpoint=query("SELECT p.next_date FROM candle_backfill_progress p JOIN candle_backfill_jobs j ON j.id=p.job_id WHERE j.interval_code='1m'")[0][0]
            assert checkpoint==date(2026,10,8)
            control.pause(False)
            continuation=Vendor()
            result=BackfillRunner(continuation,repo,control).run([scope])
            assert result["completed"]==1 and continuation.calls[0][2]==checkpoint
            checks.append("safe pause commits window and resume skips its saved range")
            group=repo.plan_incremental(0,"5m",date(2026,10,1),1200,"candles","candle_backfill_jobs","instruments",explicit_start=date(2026,10,1),only_symbols=["2317"])
            scope=make_plan("5m",date(2026,10,1),group)
            owned=repo.claim_incremental_job(scope,"owner")
            rows=Vendor().fetch_historical_candles("2317","5m",date(2026,10,1),date(2026,10,1))
            original_write=repo._write_rows
            def fail_after_insert(*args):
                original_write(*args)
                raise RuntimeError("injected after INSERT, before checkpoint")
            with patch.object(repo,"_write_rows",side_effect=fail_after_insert):
                try:
                    repo.commit_window(owned,rows,date(2026,10,2),0)
                except MySQLRepositoryError:
                    pass
                else:
                    raise AssertionError("Expected rollback")
            assert query("SELECT COUNT(*) FROM candles WHERE interval_code='5m'")[0][0]==0
            assert query("SELECT p.next_date FROM candle_backfill_progress p JOIN candle_backfill_jobs j ON j.id=p.job_id WHERE j.interval_code='5m'")[0][0]==date(2026,10,1)
            checks.append("real transaction rolls back candle rows and cursor together")
            repo.fail_incremental_job(owned,"injected stock failure")
            assert repo.failed_symbols(0,"5m","candle_backfill_jobs","instruments")==["2317"]
            group=repo.plan_incremental(0,"5m",date(2026,10,1),1200,"candles","candle_backfill_jobs","instruments",force=True,only_symbols=["2317"])
            assert group["queued_jobs"]==1
            checks.append("retry planning is limited to the failed symbol")
            snapshot=repo.get_backfill_dashboard_status([0],["day"],"candles","candle_backfill_jobs","instruments")
            assert snapshot["items"][(0,"day")]["data_through"]==tomorrow
            checks.append("dashboard date comes from actual candle timestamps")
            timing=snapshot["items"][(0,"day")]
            expected=query("SELECT AVG(TIMESTAMPDIFF(MICROSECOND,started_at,completed_at)/1000000) FROM candle_backfill_jobs WHERE interval_code='day' AND end_date=%s AND status IN ('completed','insufficient_history') AND attempt_count=1",(tomorrow,))[0][0]
            assert timing["average_fetch_sample_count"]==2
            assert abs(timing["average_fetch_seconds"]-float(expected))<=0.001
            checks.append("latest successful first-attempt average matches real MySQL timestamps")
            # V2 batch path: official sources and all Capital calls are fake.
            # All tables below belong to THIS invocation's disposable schema.
            with patch.dict("os.environ",{"CAPITAL_SUSPENSION_SYNC_ENABLED":"false"}):
                repo.ensure_batch_storage("candles","candle_backfill_jobs")
                with repo.transaction() as cursor:
                    cursor.executemany("INSERT INTO instruments VALUES(%s,%s,%s,TRUE)",[(0,"1503",1),(1,"2067",1),(4,"9999",1),(4,"8888",1)])
                    seed=Vendor().fetch_historical_candles("1503","day",end-timedelta(days=1179),end)
                    repo._write_rows(cursor,"candles",dict(symbol="1503",interval_code="day"),seed,datetime.now(timezone.utc).replace(tzinfo=None))
                def batch_scope(market,interval,symbols):
                    return dict(market_no=market,interval=interval,start_date=None,end_date=end,as_of_date=end,
                        batch_v2=True,planned_on=None,job_table="candle_backfill_jobs",candle_table="candles",
                        instrument_table="instruments",target_bars=1200,only_symbols=symbols)
                scope=batch_scope(0,"day",["1503"])
                vendor=Vendor()
                result=BackfillRunner(vendor,repo,control).run([scope])
                assert result["completed"]==1 and result["failed"]==0
                assert len(vendor.calls)==2
                assert vendor.calls[0][2]==end
                assert vendor.calls[1][3]==end-timedelta(days=1180)
                assert query("SELECT COUNT(*) FROM candles WHERE symbol='1503'")[0][0]==1200
                assert query("SELECT verified_through FROM candle_sync_state WHERE symbol='1503' AND interval_code='day'")[0][0]==end
                checks.append("batch latest gap first, then history strictly before earliest stored bar")
                summary=repo.plan_batch(0,"day",end,1200,"candles","candle_backfill_jobs","instruments",only_symbols=["1503"])
                assert summary["queued_jobs"]==0
                checks.append("successful batch check does not repeatedly initialize short history")
                from app.services.trading_suspensions import SuspensionCalendar,Halt
                cal=SuspensionCalendar([Halt(1,"2067",end,end,"official-fixture",date.today())])
                with repo.transaction() as cursor:
                    cursor.execute("INSERT INTO candle_sync_state VALUES('candles','2067','day',TRUE,TRUE,NULL,1200,UTC_TIMESTAMP(6))")
                halted=batch_scope(1,"day",["2067"])
                prepared=repo.plan_batch(1,"day",end,1200,"candles","candle_backfill_jobs","instruments",only_symbols=["2067"],suspensions=cal)
                halted.update(planned_on=date.today().isoformat(),start_date=prepared["start_date"])
                vendor=Vendor(); runner=BackfillRunner(vendor,repo,control)
                runner.suspensions=cal; runner.suspension_day=date.today()
                result=runner.run([halted])
                assert len(vendor.calls)==0 and result["completed"]==1
                assert query("SELECT verified_through FROM candle_sync_state WHERE symbol='2067'")[0][0] is None
                assert query("SELECT checked_through FROM candle_check_state WHERE symbol='2067'")[0][0]==end
                assert query("SELECT source FROM candle_backfill_skips WHERE source='official-fixture'")
                checks.append("official halt recorded separately, never fakes API verification")
                with repo.transaction() as cursor:
                    for symbol,day in (("9999",date(2026,1,1)),("8888",end-timedelta(days=1))):
                        repo._write_rows(cursor,"candles",dict(symbol=symbol,interval_code="1m"),
                            [Candle(timestamp=datetime.combine(day,datetime.min.time(),tzinfo=timezone.utc),open=10,high=12,low=9,close=11,volume=1)],datetime.now(timezone.utc).replace(tzinfo=None))
                        cursor.execute("INSERT INTO candle_sync_state VALUES('candles',%s,'1m',TRUE,TRUE,NULL,1200,UTC_TIMESTAMP(6))",(symbol,))
                class EmptyVendor(Vendor):
                    def fetch_historical_candles(self,symbol,interval,start,finish,**_options):
                        self.calls.append((symbol,interval,start,finish)); return []
                gap=batch_scope(4,"1m",["9999","8888"])
                empty=EmptyVendor(); runner=BackfillRunner(empty,repo,control); runner.job_window_budget=2
                result=runner.run([gap])
                assert result["deferred"]==1 and result["completed"]==1 and result["failed"]==0
                assert control.snapshot()["state"]=="waiting_backfill"
                assert len(empty.calls)==3
                checkpoint=query("SELECT p.next_date FROM candle_backfill_progress p JOIN candle_backfill_jobs j ON p.job_id=j.id WHERE j.symbol='9999'")[0][0]
                assert checkpoint==date(2026,1,15)
                assert query("SELECT checked_through FROM candle_check_state WHERE symbol='8888'")[0][0]==end
                checks.append("unknown long gap yields to normal stocks and valid empty advances checked cursor")
                with repo.transaction() as cursor:
                    cursor.execute("UPDATE candle_batch_work w JOIN candle_backfill_jobs j ON w.job_id=j.id SET w.not_before=NULL WHERE j.symbol='9999'")
                continued=EmptyVendor(); runner=BackfillRunner(continued,repo,control); runner.job_window_budget=2
                result=runner.run([gap])
                assert continued.calls[0][2]==checkpoint
                assert len(continued.calls)==2 and result["failed"]==0
                checks.append("bounded slow queue resumes from committed cursor, not old last-trade date")
                # An explicit user resume may bypass backoff without resetting
                # its cursor; automatic recovery must still respect backoff.
                blocked=dict(gap,queue_phase="recovery")
                assert repo.claim_incremental_job(blocked,"automatic-round") is None
                overridden=dict(blocked,ignore_backoff=True)
                owned=repo.claim_incremental_job(overridden,"manual-round")
                assert owned and owned["symbol"]=="9999"
                repo.release_incremental_job(owned,"test user resume")
                checks.append("explicit resume bypasses backoff without resetting committed progress")
                # Roll back AFTER candle/check-state writes, before the cursor
                # update: neither rows nor checked/API dates may escape.
                repo.plan_batch(0,"5m",end,1200,"candles","candle_backfill_jobs","instruments",
                    explicit_start=end,only_symbols=["1503"])
                atomic=dict(batch_scope(0,"5m",["1503"]),queue_phase="latest")
                owned=repo.claim_incremental_job(atomic,"atomic-round")
                raw=repo._connect_database(repo._connector())
                class FailingCursor:
                    def __init__(self,cursor): self.cursor=cursor
                    def __getattr__(self,name): return getattr(self.cursor,name)
                    def execute(self,sql,*args,**kwargs):
                        self.cursor.execute(sql,*args,**kwargs)
                        if sql.startswith("UPDATE candle_backfill_progress SET next_date"):
                            raise RuntimeError("injected after rows and coverage, before commit")
                class FailingConnection:
                    def cursor(self,*args,**kwargs): return FailingCursor(raw.cursor(*args,**kwargs))
                    def commit(self): raw.commit()
                    def rollback(self): raw.rollback()
                    def close(self): raw.close()
                rows=Vendor().fetch_historical_candles("1503","5m",end,end)
                with patch.object(repo,"_connect_database",return_value=FailingConnection()):
                    try:
                        repo.commit_window(owned,rows,end+timedelta(days=1),0)
                    except MySQLRepositoryError:
                        pass
                    else: raise AssertionError("Expected v2 atomic rollback")
                assert query("SELECT COUNT(*) FROM candles WHERE symbol='1503' AND interval_code='5m'")[0][0]==0
                assert not query("SELECT checked_through FROM candle_check_state WHERE symbol='1503' AND interval_code='5m'")
                assert not query("SELECT verified_through FROM candle_sync_state WHERE symbol='1503' AND interval_code='5m'")
                assert query("SELECT next_date FROM candle_backfill_progress WHERE job_id=%s AND job_table='candle_backfill_jobs'",(owned["id"],))[0][0]==end
                repo.fail_incremental_job(owned,"test rollback complete")
                checks.append("v2 rows, API verification, checked coverage and cursor roll back together")
                for interval,goal,amount,period_end in (("week",560,600,date(2026,10,8)),("month",100,150,date(2026,9,30))):
                    rows=[]
                    for i in range(amount):
                        if interval=="week":
                            stamp=date(2026,10,5)-timedelta(days=7*i)
                        else:
                            year,month=divmod(2026*12+8-i,12)
                            stamp=date(year,month+1,1)
                        rows.append(Candle(timestamp=datetime.combine(stamp,datetime.min.time(),tzinfo=timezone.utc),open=10,high=12,low=9,close=11,volume=1))
                    with repo.transaction() as cursor:
                        repo._write_rows(cursor,"candles",dict(symbol="1503",interval_code=interval),rows,datetime.now(timezone.utc).replace(tzinfo=None))
                        cursor.execute("INSERT INTO candle_check_state VALUES('candles','1503',%s,%s,UTC_TIMESTAMP(6))",(interval,period_end))
                    old=repo.plan_batch(0,interval,period_end,1200,"candles","candle_backfill_jobs","instruments",only_symbols=["1503"])
                    assert old["initialization_jobs"]==1
                    after=repo.plan_batch(0,interval,period_end,goal,"candles","candle_backfill_jobs","instruments",only_symbols=["1503"])
                    assert after["queued_jobs"]==0
                    assert query("SELECT COUNT(*) FROM candles WHERE symbol='1503' AND interval_code=%s",(interval,))[0][0]==amount
                    states=query("SELECT j.status,p.target_bars,j.started_at FROM candle_backfill_jobs j JOIN candle_backfill_progress p ON p.job_id=j.id AND p.job_table='candle_backfill_jobs' WHERE j.symbol='1503' AND j.interval_code=%s AND j.end_date=%s",(interval,period_end))
                    assert states==[("completed",goal,None)]
                    checks.append(f"{interval} old-1200 pending work meets new {goal} goal without dropping its {amount} stored bars")
                assert query("SELECT COUNT(*) FROM candles WHERE symbol='2330' AND interval_code='day'")[0][0]>1200
                checks.append("latest day bars above 1200 are retained, not trimmed or rejected")
                # Legacy partial aggregate snapshots are retained but cannot
                # make initialization appear complete or enter analysis.
                with repo.transaction() as cursor:
                    for stamp,written in ((date(2026,8,3),datetime(2026,9,1)),
                                          (date(2026,9,1),datetime(2026,9,20)),
                                          (date(2026,10,1),datetime(2026,10,15))):
                        bar=Candle(timestamp=datetime.combine(stamp,datetime.min.time(),tzinfo=timezone.utc),open=10,high=12,low=9,close=11,volume=1)
                        repo._write_rows(cursor,"candles",dict(symbol="2330",interval_code="month"),[bar],written)
                month_job=dict(symbol="2330",interval_code="month",candle_table="candles",end_date=date(2026,9,30))
                assert repo.stored_count(month_job)==1
                with patch("app.services.backfill_policy.completed_end",return_value=date(2026,9,30)):
                    valid,_=repo.get_candles_with_metadata("2330","month")
                assert [c.timestamp.date() for c in valid]==[date(2026,8,3)]
                assert query("SELECT COUNT(*) FROM candles WHERE symbol='2330' AND interval_code='month'")[0][0]==3
                checks.append("partial monthly snapshots stay stored but do not count or enter analysis")
                classified=repo.get_backfill_dashboard_status([4],["1m"],"candles","candle_backfill_jobs","instruments")["items"][(4,"1m")]
                assert classified["pending_phases"]["catchup"]==1
                assert classified["checked_through"] is not None
                checks.append("dashboard pending phases and checked-through dates match persisted batch state")
                # Exercise the button's endpoint implementation against ONLY
                # this disposable schema and its temporary control database.
                from app.api.routes import prepare_board_lot_backfill
                first=end-timedelta(days=3)
                before=query("SELECT COUNT(*) FROM candles WHERE symbol='2330' AND interval_code='day'")[0][0]
                with patch("app.api.routes.MySQLCandleRepository",return_value=repo),patch("app.api.routes.BackfillControl",return_value=control):
                    prepared=prepare_board_lot_backfill("day",1200,first,end,True)
                assert prepared.force and prepared.plan_saved and prepared.created_jobs==6
                assert control.snapshot()["pause_requested"] is True
                assert query("SELECT COUNT(*) FROM candles WHERE symbol='2330' AND interval_code='day'")[0][0]==before
                full=[p for p in control.saved_plans() if p.get("fixed_range")]
                assert len(full)==3 and all(p["explicit_start"]==first for p in full)
                assert not any(p["force"] for p in full),"Preparation already consumed the checkpoint reset"
                control.pause(False)
                requery=Vendor()
                finished=BackfillRunner(requery,repo,control).run(full)
                assert finished["completed"]==6 and finished["failed"]==0
                assert len(requery.calls)==6 and all(call[2]==first and call[3]==end for call in requery.calls)
                assert query("SELECT COUNT(*) FROM candles WHERE symbol='2330' AND interval_code='day'")[0][0]==before
                checks.append("force prepare requeues already-synced symbols, stays paused, then re-fetches full saved range without deleting or duplicating bars")
            with repo.transaction() as cursor:
                cursor.execute("UPDATE instruments SET is_active=FALSE WHERE symbol='2317'")
            # A symbol removed after planning must not be fetched by a queued job.
            scope=make_plan("5m",date(2026,10,1),group)
            assert repo.claim_incremental_job(scope,"inactive-check") is None
            checks.append("inactive symbols are not claimed after planning")
        print(json.dumps({"isolated_mysql_checks":checks,"passed":len(checks),"production_database_modified":False,"capital_dll_used":False},ensure_ascii=False),flush=True)
    finally:
        if created:
            cursor=admin.cursor()
            cursor.execute(f"DROP DATABASE `{schema}`")
            admin.commit(); cursor.close()
            print("已清除本次建立的獨立測試資料庫。",flush=True)
        admin.close()


if __name__=="__main__":
    main()
