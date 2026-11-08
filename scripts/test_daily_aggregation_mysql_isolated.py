"""Real MySQL overwrite/rollback verification; fake daily data, no Capital DLL.

Creates and removes only this invocation's regex-validated test schema. Never
opens a production API/control file or writes to the production database.
"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
import uuid

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))

from dotenv import load_dotenv
import mysql.connector
from app.schemas.models import Candle
from app.services.backfill_control import BackfillControl
from app.services.backfill_engine import BackfillRunner
from app.services.daily_candle_aggregation import aggregate_daily, PARTIAL_SOURCE, COMPLETE_SOURCE, TAIPEI
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLRepositoryError, MySQLSettings
from app.services.taiwan_trading_calendar import trading_day_status


END=date(2026,10,20)


def bar(day,price=10,volume=100):
    return Candle(timestamp=datetime.combine(day,datetime.min.time(),tzinfo=TAIPEI),
                  open=price,high=price+2,low=price-1,close=price+1,volume=volume)


def main():
    load_dotenv(PROJECT/'.env')
    settings=MySQLSettings.from_env()
    schema='capital_day_aggregate_test_'+uuid.uuid4().hex[:12]
    if not re.fullmatch(r'capital_day_aggregate_test_[0-9a-f]{12}',schema):
        raise RuntimeError('Unexpected test database name')
    admin=mysql.connector.connect(host=settings.host,port=settings.port,user=settings.user,password=settings.password)
    created=False
    checks=[]
    try:
        cursor=admin.cursor()
        cursor.execute(f'CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        admin.commit(); cursor.close(); created=True
        repo=MySQLCandleRepository(replace(settings,database=schema))
        repo.ensure_batch_storage('candles','candle_backfill_jobs')
        with repo.transaction() as cursor:
            cursor.execute('CREATE TABLE instruments (market_no SMALLINT NOT NULL,symbol VARCHAR(32) NOT NULL,'
                           'category_no INT NOT NULL,is_active BOOLEAN NOT NULL,PRIMARY KEY(market_no,symbol))')
            cursor.execute("INSERT INTO instruments VALUES(0,'AGG',1,TRUE)")
            # Legacy first-trading-day labels after a Monday holiday, and a
            # bad current-period snapshot, must be replaced rather than added.
            for day in (date(2026,9,29),date(2026,10,19)):
                repo._write_rows(cursor,'candles',dict(symbol='AGG',interval_code='week'),[bar(day,price=900,volume=999)],
                                 datetime(2026,10,19,12))

        def query(sql,values=()):
            with repo.transaction() as cursor:
                cursor.execute(sql,values)
                return cursor.fetchall()

        # Preserve an older native-refresh job even if it has the same end and
        # otherwise colliding unique date key. The new mode must not claim it.
        repo.plan_batch(0,'week',END,560,'candles','candle_backfill_jobs','instruments',
                        only_symbols=['AGG'],refresh_bars=5)
        legacy_before=query("SELECT j.id,j.status,p.mode,p.next_date,j.received_count FROM candle_backfill_jobs j "
                            "JOIN candle_backfill_progress p ON p.job_table='candle_backfill_jobs' AND p.job_id=j.id "
                            "WHERE p.mode='refresh'")

        events=[]
        class Vendor:
            cache=None
            corrected=False
            pending=False
            def fetch_historical_candles(self,symbol,interval,first,last,**kwargs):
                assert symbol=='AGG' and interval=='day' and not self.pending and kwargs['cache_result'] is False
                self.pending=True; events.append('fetch_day')
                return [bar(day,price=20 if self.corrected and day==END else 10,
                            volume=7 if self.corrected and day==END else 100)
                        for day in (first+timedelta(days=i) for i in range((last-first).days+1))
                        if trading_day_status(day)[0]]
            def history_checkpoint(self,committed):
                assert self.pending
                self.pending=False; events.append('commit_ack' if committed else 'rollback_ack')
        vendor=Vendor()

        def scope(interval,force=False):
            initial,goal=(560,5) if interval=='week' else (100,2)
            summary=repo.plan_batch(0,interval,END,initial,'candles','candle_backfill_jobs','instruments',
                                    only_symbols=['AGG'],refresh_bars=goal,aggregate_from_day=True,
                                    recover_running=True,force=force)
            return dict(market_no=0,interval=interval,end_date=END,as_of_date=END,start_date=summary['start_date'],
                        job_table='candle_backfill_jobs',candle_table='candles',instrument_table='instruments',
                        batch_v2=True,planned_on=date.today().isoformat(),planner_version=4,
                        target_bars=initial,refresh_bars=goal,aggregate_from_day=True,only_symbols=['AGG']),summary

        with TemporaryDirectory(prefix='daily-aggregate-mysql-') as folder, \
             patch.dict(os.environ,{'CAPITAL_SUSPENSION_SYNC_ENABLED':'false'}), \
             patch('app.services.backfill_engine.daily_aggregate_end',return_value=END):
            control=BackfillControl(Path(folder)/'control.sqlite3')
            for interval,count in (('week',5),('month',2)):
                work,summary=scope(interval)
                assert summary['queued_jobs']==1
                result=BackfillRunner(vendor,repo,control).run([work])
                assert result['completed']==1 and result['failed']==0,result
                rows=query('SELECT candle_time,source FROM candles WHERE symbol=%s AND interval_code=%s ORDER BY candle_time',('AGG',interval))
                assert len(rows)==count,rows
                assert rows[-1][1]==PARTIAL_SOURCE and all(row[1]==COMPLETE_SOURCE for row in rows[:-1]),rows
                assert len(repo.get_candles('AGG',interval))==count-1
                metadata=query('SELECT as_of_date,is_complete FROM candle_daily_aggregate_state WHERE interval_code=%s ORDER BY candle_time',(interval,))
                assert all(row[0]==END for row in metadata) and metadata[-1][1]==0
                checks.append(f'{interval}: includes current partial period, closed analysis excludes it, target/count retained')
            assert query("SELECT COUNT(*) FROM candles WHERE symbol='AGG' AND interval_code='day'")[0][0]>10
            assert query("SELECT j.id,j.status,p.mode,p.next_date,j.received_count FROM candle_backfill_jobs j "
                         "JOIN candle_backfill_progress p ON p.job_table='candle_backfill_jobs' AND p.job_id=j.id "
                         "WHERE p.mode='refresh'")==legacy_before
            checks.append('legacy native-refresh work/cursor preserved and excluded from new-mode claims/pending counts')
            checks.append('whole source range refreshed, not only the ten-day daily target')
            assert not query("SELECT candle_time FROM candles WHERE interval_code='week' AND candle_time='2026-09-29'")
            backup=query("SELECT replaced_rows FROM candle_daily_aggregate_state WHERE interval_code='week' AND candle_time='2026-09-28'")[0][0]
            assert '2026-09-29' in backup
            checks.append('legacy Tuesday label replaced with one Monday period key; original alias retained in audit JSON')

            current="SELECT open_price,high_price,low_price,close_price,volume,source FROM candles WHERE symbol='AGG' AND interval_code='week' AND candle_time='2026-10-19'"
            assert query(current)[0][4]==200
            with repo.transaction() as cursor:
                repo._write_rows(cursor,'candles',dict(symbol='AGG',interval_code='week'),
                                 [bar(date(2026,9,30),price=800,volume=888)],datetime(2026,10,19,12))
            for corrected in (False,True,True):
                vendor.corrected=corrected
                work,_summary=scope('week',force=True)
                result=BackfillRunner(vendor,repo,control).run([work])
                assert result['completed']==1 and not vendor.pending
                assert query(current)[0][4]==(107 if corrected else 200)
                assert query("SELECT COUNT(*) FROM candles WHERE interval_code='week'")[0][0]==5
            row=query(current)[0]
            assert tuple(float(value) for value in row[:4])==(10,22,9,21),row
            backup=query("SELECT replaced_rows FROM candle_daily_aggregate_state WHERE interval_code='week' AND candle_time='2026-09-28'")[0][0]
            assert '2026-09-29' in backup and '2026-09-30' in backup
            checks.append('same-asof repeats replace all OHLCV; corrected daily volume is 107, never 200+107')

            before=query(current)
            with repo.transaction() as cursor:
                older=aggregate_daily([bar(date(2026,10,19),price=800)],'week',date(2026,10,19),date(2026,10,19))
                repo._write_daily_aggregates(cursor,'candles',dict(symbol='AGG',interval_code='week',end_date=date(2026,10,19)),
                                             older,datetime.now(timezone.utc).replace(tzinfo=None))
            assert query(current)==before
            checks.append('older-asof paused work cannot overwrite a newer snapshot')

            # Roll back after both candles and aggregate metadata were written,
            # immediately before the job cursor/count would be advanced.
            work,_summary=scope('week',force=True)
            owned=repo.claim_incremental_job(dict(work,queue_phase='latest'),'rollback-test')
            metadata_before=query("SELECT * FROM candle_daily_aggregate_state WHERE interval_code='week'")
            checked_before=query("SELECT * FROM candle_check_state")
            verified_before=query("SELECT * FROM candle_sync_state")
            raw=repo._connect_database(repo._connector())
            class FailingCursor:
                def __init__(self,cursor): self.cursor=cursor
                def __getattr__(self,name): return getattr(self.cursor,name)
                def execute(self,sql,*args,**kwargs):
                    self.cursor.execute(sql,*args,**kwargs)
                    if sql.startswith('UPDATE candle_backfill_progress SET next_date'):
                        raise RuntimeError('injected after aggregate metadata, before cursor')
            class FailingConnection:
                def cursor(self,*args,**kwargs): return FailingCursor(raw.cursor(*args,**kwargs))
                def commit(self): raw.commit()
                def rollback(self): raw.rollback()
                def close(self): raw.close()
            changed=aggregate_daily([bar(END,price=700)],'week',date(2026,10,19),END)
            try:
                with patch.object(repo,'_connect_database',return_value=FailingConnection()):
                    repo.commit_window(owned,changed,date(2026,10,18),0,window_start=date(2026,10,19),window_end=END)
            except MySQLRepositoryError:
                pass
            else:
                raise AssertionError('Expected rollback')
            assert query(current)==before
            assert query("SELECT * FROM candle_daily_aggregate_state WHERE interval_code='week'")==metadata_before
            assert query("SELECT * FROM candle_check_state")==checked_before
            assert query("SELECT * FROM candle_sync_state")==verified_before
            state=query("SELECT p.next_date,j.received_count FROM candle_backfill_progress p JOIN candle_backfill_jobs j "
                        "ON p.job_table='candle_backfill_jobs' AND p.job_id=j.id WHERE j.id=%s",(owned['id'],))[0]
            assert state==(END,0),state
            checks.append('OHLCV, metadata, checked/verified dates, count and cursor roll back atomically')

        print(json.dumps(dict(passed=len(checks),checks=checks,capital_dll_used=False,
                              production_database_modified=False),ensure_ascii=False),flush=True)
    finally:
        if created:
            # Name ownership was validated before CREATE; never substitute the
            # .env production database name or reuse a pre-existing schema.
            cursor=admin.cursor(); cursor.execute(f'DROP DATABASE `{schema}`'); admin.commit(); cursor.close()
            print('本次隔離測試資料庫已清除；正式資料庫未修改。',flush=True)
        admin.close()


if __name__=='__main__':
    main()
