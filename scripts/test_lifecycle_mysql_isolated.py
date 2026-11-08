"""Disposable MySQL lifecycle acceptance; fake quotes, no production writes/login."""
from contextlib import ExitStack
from dataclasses import replace
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from dotenv import load_dotenv
import mysql.connector
from app.schemas.models import Candle
from app.services.backfill_engine import BackfillRunner
from app.services.day_candle_integrity import assess_days, DayCalendar, DayEvidence
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.instrument_lifecycle import ISSUE_FIELDS, parse_bond_issues, parse_bond_delist
from app.services.mysql_instrument_repository import MySQLSettings
from app.services.trade_activity import classify_missing

END=date(2026,10,22)
LISTED=date(2026,10,20)
NOW=date(2026,10,26)


class Control:
    def __init__(self): self.state={}
    def snapshot(self): return dict(self.state)
    def update(self,**values): self.state.update(values)


def bar(day,close=11):
    return Candle(timestamp=datetime.combine(day,datetime.min.time(),tzinfo=timezone.utc),
                  open=10,high=max(12,close),low=9,close=close,volume=100)


def main():
    load_dotenv(PROJECT/'.env',override=False)
    settings=MySQLSettings.from_env()
    schema='capital_lifecycle_test_'+uuid.uuid4().hex[:12]
    assert re.fullmatch(r'capital_lifecycle_test_[0-9a-f]{12}',schema) and schema != settings.database
    admin=mysql.connector.connect(host=settings.host,port=settings.port,user=settings.user,password=settings.password)
    created=False; checks=[]
    try:
        q=admin.cursor(); q.execute(f'CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci'); q.close(); created=True
        repo=MySQLCandleRepository(replace(settings,database=schema))
        repo.ensure_batch_storage('candles','candle_backfill_jobs'); repo.ensure_day_integrity_storage()
        with repo.transaction() as q:
            q.execute('CREATE TABLE instruments(market_no SMALLINT,symbol VARCHAR(32),name VARCHAR(128),category_no INT,is_active BOOLEAN,PRIMARY KEY(market_no,symbol))')
            q.executemany('INSERT INTO instruments VALUES(1,%s,%s,1,TRUE)',
                          [('17272','中華化二'),('90001','到期測試'),('90002','終止測試'),('90003','轉換測試'),('90004','未知日期')])
        def query(sql,params=()):
            with repo.transaction() as q:
                q.execute(sql,params); return q.fetchall()
        def official(code,name,**extra):
            row=dict.fromkeys(ISSUE_FIELDS,'')
            row.update(Date='20261026',BondCode=code,BondType='5',ShortName=name,ListingStatus='2',
                       ListingDate='20261020',MaturityDate='20311020',IssueAmount='1000',OutstandingAmount='1000')
            row.update(extra); return row
        rows=parse_bond_issues([official('17272','中華化二'),
                               official('90001','到期測試',ListingDate='20171020',MaturityDate='20261021'),
                               official('90002','終止測試'),
                               official('90003','轉換測試',OutstandingAmount='0',OutstandingChangeDate='20261021',OutstandingChangeDescription='全數轉換'),
                               official('90004','未知日期',ListingDate='',MaturityDate='')],NOW)
        rows['90002'].update(parse_bond_delist(dict(stat='ok',tables=[dict(fields=['代碼','簡稱','下櫃日期'],
            data=[['90002','終止測試','115/10/21']],totalCount=1)]),NOW)['90002'])
        # Retain known issuance fields alongside the independently announced exit.
        rows['90002'].update(listing_date=LISTED,maturity_date=date(2031,10,20))
        with ExitStack() as stack:
            stack.enter_context(patch('app.services.instrument_lifecycle.fetch_bond_lifecycles',return_value=tuple(rows.values())))
            stack.enter_context(patch('app.services.instrument_lifecycle.today',return_value=NOW))
            stack.enter_context(patch('app.services.listing_history.fetch_twse_listing_origins',return_value={}))
            repo.refresh_lifecycle(); repo.refresh_lifecycle()
            assert query('SELECT COUNT(*) FROM instrument_lifecycle')[0][0]==5
            checks.append('metadata_upsert_idempotent')
            policies=repo.load_listing_policies('candles',1)
            assert policies['17272']['history_start']==LISTED and policies['90002']['termination_date']==date(2026,10,21)
            assert policies['90001']['maturity_date']==date(2026,10,21)
            checks.append('official_nullable_dates_and_independent_termination')

            # Seed an impossible old empty observation, unchanged checkpoint.
            with repo.transaction() as q:
                q.execute("INSERT INTO candle_backfill_jobs(market_no,symbol,interval_code,start_date,end_date,status,received_count,updated_at) VALUES(1,'17272','day','2017-01-01',%s,'waiting_review',0,UTC_TIMESTAMP(6))",(END,))
                old=q.lastrowid
                q.execute("INSERT INTO candle_backfill_progress VALUES('candle_backfill_jobs',%s,'initialization',1200,'2017-01-01',0,NULL,UTC_TIMESTAMP(6))",(old,))
                q.execute("INSERT INTO candle_batch_work VALUES('candle_backfill_jobs',%s,'catchup',TRUE,'2017-01-01','2017-01-01',UTC_TIMESTAMP(6),'old-round')",(old,))
                q.execute("INSERT INTO candle_empty_job_waits VALUES('candle_backfill_jobs',%s,UTC_TIMESTAMP(6))",(old,))
                q.execute("""INSERT INTO candle_empty_history_reviews(candle_table,market_no,symbol,interval_code,window_start,window_end,status,observation_days,
                    first_observed_at,last_observed_at,job_table,job_id,checkpoint,evidence,updated_at)
                    VALUES('candles',1,'17272','day','2017-01-01','2017-01-01','pending_review','[]',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),'candle_backfill_jobs',%s,'2017-01-01','{}',UTC_TIMESTAMP(6))""",(old,))
                repo._write_rows(q,'candles',dict(symbol='17272',interval_code='day'),[bar(date(2017,1,1),77)],datetime(2026,10,20))
            repo._lifecycle_reconciled=set()
            repo.load_listing_policies('candles',1)
            assert query('SELECT status FROM candle_empty_history_reviews')[0][0]=='confirmed_no_bar'
            assert query('SELECT next_date FROM candle_backfill_progress WHERE job_id=%s',(old,))[0][0]==date(2017,1,1)
            assert query('SELECT status FROM candle_backfill_jobs WHERE id=%s',(old,))[0][0]=='pending'
            checks.append('prelisting_wait_excluded_cursor_preserved')

            # Terminal instruments are excluded from new refresh and ordinary plans.
            for refresh in (None,10):
                summary=repo.plan_batch(1,'day',END,1200,'candles','candle_backfill_jobs','instruments',
                                       only_symbols=['90001','90002','90003'],refresh_bars=refresh)
                assert summary['queued_jobs']==0
            checks.append('maturity_early_exit_full_conversion_not_scheduled')

            calls=[]
            def fetch(symbol,interval,first,last,**_kwargs):
                calls.append((interval,first,last))
                assert first>=LISTED
                from app.services.backfill_policy import period_start
                return [bar(period_start(interval,LISTED))]
            vendor=SimpleNamespace(fetch_historical_candles=fetch)
            runner=BackfillRunner(vendor,repo,Control()); runner.stage_check=lambda _:False
            for interval in ('day','week','month'):
                refresh=10 if interval=='day' else None
                repo.plan_batch(1,interval,END,1200,'candles','candle_backfill_jobs','instruments',
                                only_symbols=['17272'],force=True,refresh_bars=refresh)
                item=repo.claim_incremental_job(dict(job_table='candle_backfill_jobs',candle_table='candles',instrument_table='instruments',
                    market_no=1,interval=interval,end_date=END,batch_v2=True,queue_phase='latest',refresh_bars=refresh),'test-owner')
                assert item is not None
                runner.run_job(item)
                assert query('SELECT status FROM candle_backfill_jobs WHERE id=%s',(item['id'],))[0][0]=='insufficient_history'
            assert query("SELECT close_price FROM candles WHERE symbol='17272' AND interval_code='day' AND candle_time='2017-01-01'")[0][0]==77
            assert len(query("SELECT candle_time FROM candles WHERE interval_code IN('week','month')"))==2
            checks.append('day_week_month_inclusive_bounds_keep_labels_and_existing_history')

            # A late-learned terminal status also stops an ALREADY claimed saved job.
            item.update(status='running',owner_token='test-owner')
            with repo.transaction() as q:
                q.execute("UPDATE candle_backfill_jobs SET symbol='90001',status='running' WHERE id=%s",(item['id'],))
                q.execute("UPDATE candle_backfill_progress SET owner_token='test-owner' WHERE job_id=%s",(item['id'],))
            item['symbol']='90001'
            before=len(calls); runner.run_job(item)
            assert len(calls)==before and query('SELECT status FROM candle_backfill_jobs WHERE id=%s',(item['id'],))[0][0]=='superseded'
            checks.append('old_claimed_job_rechecks_lifetime_before_query')

            activity=repo.trade_activity_dates('candles','17272',LISTED,END)
            assert classify_missing(activity,LISTED,LISTED)['trade_confirmed']
            assert not classify_missing(activity,date(2026,10,21),END)['trade_confirmed']
            result=assess_days([],END,1200,date(2017,1,1),1,'17272',DayEvidence(),
                DayCalendar({'source':'https://www.twse.com.tw/official','years':{'2026':[]}},overrides=[]),listing=policies['17272'],activity=activity)
            assert result['api_missing_dates']==1 and result['activity_unconfirmed_dates']==2
            repo.save_day_assessment('candles',1,'17272',result,[],'test-owner')
            assert query('SELECT COUNT(*) FROM candle_day_gaps')[0][0]==3
            checks.append('only_actual_trade_evidence_is_api_missing_no_prelisting_gaps')

            # A second sync with missing fields cannot erase a stored exit/date.
            repo._lifecycle_day=None
            with patch('app.services.instrument_lifecycle.fetch_bond_lifecycles',return_value=tuple(parse_bond_issues([official('17272','中華化二',ListingDate='',MaturityDate='')],NOW).values())):
                repo.refresh_lifecycle()
            assert query("SELECT listing_date,maturity_date FROM instrument_lifecycle WHERE symbol='17272'")[0]==(LISTED,date(2031,10,20))
            assert query("SELECT termination_date FROM instrument_lifecycle WHERE symbol='90002'")[0][0]==date(2026,10,21)
            checks.append('missing_source_rows_and_fields_preserve_lifecycle_evidence')
    finally:
        if created:
            # Exact UUID schema made above; NEVER the configured production DB.
            assert re.fullmatch(r'capital_lifecycle_test_[0-9a-f]{12}',schema) and schema != settings.database
            q=admin.cursor(); q.execute(f'DROP DATABASE `{schema}`')
            q.execute('SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=%s',(schema,))
            assert q.fetchone()[0]==0
            q.close()
        admin.close()
    print(json.dumps(dict(schema=schema,checks=checks,test_database_deleted=created,production_modified=False,capital_login=False),ensure_ascii=False))


if __name__=='__main__': main()
