"""Five-failure persistence/reset/rollback in an invocation-owned MySQL schema."""
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
from app.services.candle_validation import CandleDataError,MAX_CANDLE_VOLUME
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLRepositoryError,MySQLSettings

FIRST=date(2026,10,14)
END=date(2026,10,16)


def bar(day,volume=10):
    return Candle(timestamp=datetime.combine(day,datetime.min.time(),tzinfo=timezone.utc),
                  open=10,high=12,low=9,close=11,volume=volume)


def main():
    load_dotenv(PROJECT/'.env')
    settings=MySQLSettings.from_env()
    schema='capital_data_failure_test_'+uuid.uuid4().hex[:12]
    if not re.fullmatch(r'capital_data_failure_test_[0-9a-f]{12}',schema):
        raise RuntimeError('Unexpected test schema')
    admin=mysql.connector.connect(host=settings.host,port=settings.port,user=settings.user,password=settings.password)
    created=False; checks=[]
    try:
        cursor=admin.cursor(); cursor.execute(f'CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        admin.commit(); cursor.close(); created=True
        test_settings=replace(settings,database=schema)
        repo=MySQLCandleRepository(test_settings)
        repo.ensure_batch_storage('candles','candle_backfill_jobs')
        with repo.transaction() as cursor:
            cursor.execute('CREATE TABLE instruments(market_no SMALLINT NOT NULL,symbol VARCHAR(32) NOT NULL,'
                           'category_no INT NOT NULL,is_active BOOLEAN NOT NULL,PRIMARY KEY(market_no,symbol))')
            cursor.executemany('INSERT INTO instruments VALUES(0,%s,1,TRUE)',[('BAD',),('GOOD',),('RESET',)])
            repo._write_rows(cursor,'candles',dict(symbol='BAD',interval_code='day'),[bar(FIRST-timedelta(days=1),337)],datetime.now())

        def query(sql,values=()):
            with repo.transaction() as cursor:
                cursor.execute(sql,values)
                return cursor.fetchall()

        def scope(symbols):
            summary=repo.plan_batch(0,'day',END,1200,'candles','candle_backfill_jobs','instruments',
                                    explicit_start=FIRST,only_symbols=symbols)
            return dict(market_no=0,interval='day',start_date=FIRST,end_date=END,as_of_date=END,
                        candle_table='candles',job_table='candle_backfill_jobs',instrument_table='instruments',
                        only_symbols=symbols,batch_v2=True,planned_on=date.today().isoformat(),planner_version=3,
                        target_bars=1200,fixed_range=True)

        calls=[]
        class Vendor:
            cache=None
            reset_first_valid=False
            def fetch_historical_candles(self,symbol,interval,first,last,**kwargs):
                calls.append((symbol,first,last))
                return [bar(first+timedelta(days=i),-1 if symbol=='BAD' or
                            (symbol=='RESET' and (first+timedelta(days=i)!=FIRST or not self.reset_first_valid)) else 10)
                        for i in range((last-first).days+1)]
        vendor=Vendor()

        with TemporaryDirectory(prefix='candle-failure-mysql-') as folder, \
             patch.dict(os.environ,{'CAPITAL_SUSPENSION_SYNC_ENABLED':'false','CAPITAL_DAY_INTEGRITY_ENABLED':'false'}):
            control=BackfillControl(Path(folder)/'control.sqlite3')
            work=scope(['BAD','GOOD'])
            with repo.transaction() as cursor:
                cursor.execute("UPDATE candle_backfill_jobs SET attempt_count=135 WHERE symbol='BAD'")
            for count in range(1,6):
                # New repository/runner objects emulate restart while the
                # database retains the active counter. Manual resume bypasses
                # the intentional 15-second backoff without resetting it.
                repo=MySQLCandleRepository(test_settings)
                control.update(resume_pending=True)
                result=BackfillRunner(vendor,repo,control).run([work])
                failure=query("SELECT consecutive_failures,details FROM candle_backfill_data_failures f "
                              "JOIN candle_backfill_jobs j ON j.id=f.job_id WHERE j.symbol='BAD'")[0]
                assert failure[0]==count,failure
                assert json.loads(failure[1])['raw_value']=='-1'
                state=query("SELECT j.status,j.attempt_count,j.received_count,p.next_date FROM candle_backfill_jobs j "
                            "JOIN candle_backfill_progress p ON p.job_table='candle_backfill_jobs' AND p.job_id=j.id WHERE j.symbol='BAD'")[0]
                assert state[0]==('failed' if count==5 else 'pending') and state[1]==135+count and state[3]==FIRST,state
                assert query("SELECT COUNT(*) FROM candles WHERE symbol='BAD'")[0][0]==1
                good=query("SELECT status FROM candle_backfill_jobs WHERE symbol='GOOD'")[0][0]
                assert good==('completed' if count==5 else 'pending'),good
                if count<5:
                    assert result['pause_kind']=='data' and result['failed']==0
                else:
                    assert result['failed']==1 and result['completed']==1
            checks.append('exactly failures 1..4 remain pending; failure 5 is failed and GOOD continues; data/cursor retained')
            checks.append('counter survives new repository/runner instances and is unrelated to total attempt_count 135+')

            with patch.dict(os.environ,{'CAPITAL_KLINE_MAX_DAYS_DAY':'1'}):
                reset=scope(['RESET'])
                for count in (1,2):
                    control.update(resume_pending=True)
                    result=BackfillRunner(vendor,repo,control).run([reset])
                    assert result['pause_kind']=='data'
                vendor.reset_first_valid=True
                control.update(resume_pending=True)
                result=BackfillRunner(vendor,repo,control).run([reset])
                state=query("SELECT p.next_date,f.consecutive_failures,j.received_count FROM candle_backfill_jobs j "
                            "JOIN candle_backfill_progress p ON p.job_table='candle_backfill_jobs' AND p.job_id=j.id "
                            "JOIN candle_backfill_data_failures f ON f.job_table=p.job_table AND f.job_id=j.id WHERE j.symbol='RESET'")[0]
                assert state==(FIRST+timedelta(days=1),1,1),state
                checks.append('successful committed window resets its two failures; next bad window starts at one')

                owned=repo.claim_incremental_job(dict(reset,queue_phase='latest',ignore_backoff=True),'rollback-check')
                before=query('SELECT * FROM candle_backfill_data_failures WHERE job_id=%s',(owned['id'],))
                raw=repo._connect_database(repo._connector())
                class FailingCursor:
                    def __init__(self,cursor): self.cursor=cursor
                    def __getattr__(self,name): return getattr(self.cursor,name)
                    def execute(self,sql,*args,**kwargs):
                        self.cursor.execute(sql,*args,**kwargs)
                        if sql.startswith('UPDATE candle_backfill_data_failures SET consecutive_failures=0'):
                            raise RuntimeError('injected after reset, before commit')
                class FailingConnection:
                    def cursor(self,*args,**kwargs): return FailingCursor(raw.cursor(*args,**kwargs))
                    def commit(self): raw.commit()
                    def rollback(self): raw.rollback()
                    def close(self): raw.close()
                try:
                    with patch.object(repo,'_connect_database',return_value=FailingConnection()):
                        repo.commit_window(owned,[bar(owned['next_date'])],END,0)
                except MySQLRepositoryError:
                    pass
                else:
                    raise AssertionError('Expected rollback')
                assert query('SELECT * FROM candle_backfill_data_failures WHERE job_id=%s',(owned['id'],))==before
                assert query("SELECT COUNT(*) FROM candles WHERE symbol='RESET'")[0][0]==1
                assert query('SELECT next_date FROM candle_backfill_progress WHERE job_id=%s',(owned['id'],))[0][0]==FIRST+timedelta(days=1)
                checks.append('rollback preserves previous failure count together with candle rows and cursor')

                error=CandleDataError('invalid',details=dict(field='volume',raw_value='-1'))
                record=repo.record_data_failure(owned,owned['next_date'],owned['next_date'],error)
                assert record['count']==2
                owned=repo.claim_incremental_job(dict(reset,queue_phase='latest',ignore_backoff=True),'changed-window')
                record=repo.record_data_failure(owned,owned['next_date'],END,error)
                assert record['count']==1
                checks.append('changing the requested window starts a separate consecutive count')

            repo.upsert_candles('MAX','day',[bar(FIRST,MAX_CANDLE_VOLUME)])
            assert query("SELECT volume FROM candles WHERE symbol='MAX'")[0][0]==MAX_CANDLE_VOLUME
            checks.append('UINT64_MAX is accepted exactly by real MySQL, without float rounding')

        print(json.dumps(dict(passed=len(checks),checks=checks,capital_dll_used=False,
                              production_database_modified=False),ensure_ascii=False),flush=True)
    finally:
        if created:
            cursor=admin.cursor(); cursor.execute(f'DROP DATABASE `{schema}`'); admin.commit(); cursor.close()
            print('本次資料錯誤隔離測試資料庫已清除；正式資料庫未修改。',flush=True)
        admin.close()


if __name__=='__main__':
    main()
