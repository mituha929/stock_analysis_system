"""Fake vendor + real MySQL, only in a newly owned disposable schema."""
from dataclasses import replace
from datetime import date, timedelta
import json
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
from app.services.backfill_control import BackfillControl
from app.services.backfill_engine import BackfillRunner
from app.services.capital_history_service import HistoryQuoteEvents, request_history_candles
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLSettings, MySQLRepositoryError


def main():
    load_dotenv(PROJECT/'.env')
    settings=MySQLSettings.from_env()
    schema='capital_exclusion_test_'+uuid.uuid4().hex[:12]
    if not re.fullmatch(r'capital_exclusion_test_[0-9a-f]{12}',schema):
        raise RuntimeError('Unexpected test schema')
    admin=mysql.connector.connect(host=settings.host,port=settings.port,user=settings.user,password=settings.password)
    created=False; checks=[]
    try:
        cursor=admin.cursor(); cursor.execute(f'CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        admin.commit(); cursor.close(); created=True
        repo=MySQLCandleRepository(replace(settings,database=schema))
        repo.ensure_batch_storage('candles','candle_backfill_jobs')
        with repo.transaction() as cursor:
            cursor.execute('CREATE TABLE instruments(market_no SMALLINT NOT NULL,symbol VARCHAR(32) NOT NULL,'
                           'category_no INT NOT NULL,is_active BOOLEAN NOT NULL,PRIMARY KEY(market_no,symbol))')
            cursor.executemany('INSERT INTO instruments VALUES(0,%s,1,TRUE)',[('TSLQ',),('2330',)])

        def query(sql,args=()):
            with repo.transaction() as cursor:
                cursor.execute(sql,args); return cursor.fetchall()

        first,bad,last=date(2017,11,27),date(2017,12,4),date(2017,12,17)
        scope=dict(market_no=0,interval='week',start_date=first,end_date=last,
                   candle_table='candles',job_table='candle_backfill_jobs',instrument_table='instruments')
        repo.plan_incremental(0,'week',last,560,'candles','candle_backfill_jobs','instruments',
                              explicit_start=first,only_symbols=['TSLQ','2330'])
        requests=[]
        class Vendor:
            cache=None
            def fetch_historical_candles(self,symbol,interval,start,end,**kwargs):
                events=HistoryQuoteEvents(); events.OnConnection(3003,0)
                class Quote:
                    def SKQuoteLib_RequestKLineAMByDate(self,*args):
                        requests.append(args)
                        for stamp in (first,bad,date(2017,12,11)):
                            # Deliberately spill the excluded row into each SDK
                            # reply, verifying filtering before volume parsing.
                            if args[4]<=stamp.strftime('%Y%m%d')<=args[5] or (symbol=='TSLQ' and stamp==bad):
                                volume=-2070765642 if symbol=='TSLQ' and stamp==bad else 100
                                events.OnNotifyKLineData(symbol,f'{stamp:%Y/%m/%d},10,12,9,11,{volume}')
                        events.OnKLineComplete('##'); return 0
                return request_history_candles(Quote(),None,events,lambda:None,symbol,interval,start,end,1)

        with TemporaryDirectory(prefix='capital-exclusion-mysql-') as folder:
            control=BackfillControl(Path(folder)/'control.sqlite3')
            result=BackfillRunner(Vendor(),repo,control).run([scope])
            assert result['completed']==2 and result['failed']==0,result
        tslq=query("SELECT candle_time,volume FROM candles WHERE symbol='TSLQ' ORDER BY candle_time")
        assert [(r[0].date(),r[1]) for r in tslq]==[(first,100),(date(2017,12,11),100)],tslq
        assert query("SELECT COUNT(*) FROM candles WHERE symbol='2330'")[0][0]==3
        assert [(args[4],args[5]) for args in requests if args[0]=='TSLQ']==[
            ('20171127','20171203'),('20171211','20171217')],requests
        checks.append('SDK query splitting and spillover filtering; valid neighbours saved; other stock untouched')
        row=query("SELECT j.status,j.received_count,p.next_date,s.start_date,s.end_date,s.source "
                  "FROM candle_backfill_jobs j JOIN candle_backfill_progress p ON p.job_table='candle_backfill_jobs' AND p.job_id=j.id "
                  "JOIN candle_backfill_skips s ON s.job_table=p.job_table AND s.job_id=j.id WHERE j.symbol='TSLQ'")[0]
        assert row[:5]==('completed',2,last+timedelta(days=1),bad,date(2017,12,10)),row
        assert row[5].startswith('history_exclusion:TSLQ-week-20171204')
        checks.append('real SQL stores skip reason and checkpoint atomically without counting an excluded candle')

        # A fresh month job tests rollback after the audit INSERT and before
        # checkpoint advancement. No production schema or records are involved.
        month_first,month_last=date(2018,2,1),date(2018,2,28)
        repo.plan_incremental(0,'month',month_last,100,'candles','candle_backfill_jobs','instruments',
                              explicit_start=month_first,only_symbols=['TSLQ'])
        item=repo.claim_incremental_job(dict(scope,interval='month',end_date=month_last,only_symbols=['TSLQ']),'rollback')
        repo.ensure_history_exclusion_storage()
        raw=repo._connect_database(repo._connector())
        class FailingCursor:
            def __init__(self,cursor): self.cursor=cursor
            def __getattr__(self,name): return getattr(self.cursor,name)
            def execute(self,sql,*args,**kwargs):
                self.cursor.execute(sql,*args,**kwargs)
                if sql.startswith('UPDATE candle_backfill_progress SET next_date='):
                    raise RuntimeError('injected failure after exclusion INSERT')
        class FailingConnection:
            def cursor(self,*args,**kwargs): return FailingCursor(raw.cursor(*args,**kwargs))
            def commit(self): raw.commit()
            def rollback(self): raw.rollback()
            def close(self): raw.close()
        try:
            with patch.object(repo,'_connect_database',return_value=FailingConnection()):
                repo.commit_window(item,[],date(2018,3,1),0,window_start=month_first,window_end=month_last)
        except MySQLRepositoryError:
            pass
        else:
            raise AssertionError('Expected rollback')
        assert query('SELECT COUNT(*) FROM candle_backfill_skips WHERE job_id=%s',(item['id'],))[0][0]==0
        assert query('SELECT next_date FROM candle_backfill_progress WHERE job_id=%s',(item['id'],))[0][0]==month_first
        checks.append('rollback leaves neither skip audit nor advanced checkpoint')
        repo.commit_window(item,[],date(2018,3,1),0,window_start=month_first,window_end=month_last)
        assert query('SELECT COUNT(*) FROM candle_backfill_skips WHERE job_id=%s',(item['id'],))[0][0]==1
        assert query('SELECT received_count FROM candle_backfill_jobs WHERE id=%s',(item['id'],))[0][0]==0
        checks.append('excluded-only month advances cursor with audit and zero fabricated candles')
        print(json.dumps(dict(passed=len(checks),checks=checks,capital_dll_used=False,
                              production_database_modified=False),ensure_ascii=False),flush=True)
    finally:
        if created:
            # schema was created here, checked against an invocation-specific
            # strict pattern; never delete any configured/production schema.
            cursor=admin.cursor(); cursor.execute(f'DROP DATABASE `{schema}`'); admin.commit(); cursor.close()
            print('本次排除規則測試資料庫已清除；正式資料庫未修改。',flush=True)
        admin.close()


if __name__=='__main__':
    # Period-only regression deliberately disables the newer whole-symbol
    # policy inside this test process; it never changes the production config.
    with patch('app.services.backfill_eligibility.excluded_backfill_symbols',return_value=frozenset()):
        main()
