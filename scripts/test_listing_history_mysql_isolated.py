"""Real MySQL, fake quotes, disposable schema; never writes production tables."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
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
from app.services.day_candle_integrity import DayCalendar, DayEvidence
from app.services.day_candle_integrity_runner import DayIntegrityAudit
from app.services.daily_candle_aggregation import aggregate_daily
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLSettings
from app.services.stock_master_repository import _ensure_storage

LISTED = date(2026,10,19)
END = date(2026,10,20)


def bar(day, price=10):
    return Candle(timestamp=datetime.combine(day,datetime.min.time(),tzinfo=timezone.utc),
                  open=price,high=price+2,low=price-1,close=price+1,volume=100)


def main():
    load_dotenv(PROJECT/'.env', override=False)
    settings = MySQLSettings.from_env()
    schema = 'capital_listing_test_'+uuid.uuid4().hex[:12]
    assert re.fullmatch(r'capital_listing_test_[0-9a-f]{12}',schema)
    assert schema != settings.database
    admin = mysql.connector.connect(host=settings.host,port=settings.port,user=settings.user,password=settings.password)
    created = False
    checks = []
    try:
        q=admin.cursor(); q.execute(f'CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci'); q.close(); created=True
        repo=MySQLCandleRepository(replace(settings,database=schema))
        repo.ensure_batch_storage('candles','candle_backfill_jobs')
        repo.ensure_day_integrity_storage()
        with repo.transaction() as q:
            _ensure_storage(q)
            q.execute('CREATE TABLE instruments(market_no SMALLINT NOT NULL,symbol VARCHAR(32) NOT NULL,category_no INT NOT NULL,is_active BOOLEAN NOT NULL,PRIMARY KEY(market_no,symbol))')
            q.executemany('INSERT INTO instruments VALUES(%s,%s,1,TRUE)',[(1,'9001'),(0,'9002'),(1,'9003'),(1,'9004'),(1,'9005'),(1,'9006'),(0,'9007'),(1,'9008')])
            for code,market,listed,issuer in [('9001','TPEx',LISTED,'10000001'),('9002','TWSE',LISTED,'10000002'),
                       ('9003','TPEx',LISTED,'10000003'),('9004','TPEx',LISTED,'10000004'),
                       ('9005','TPEx',LISTED,'10000005'),('9006','TPEx',LISTED,'10000006'),
                       ('9007','TWSE',LISTED,'10000007'),('9007','TPEx',date(2026,10,15),'10000007'),
                       ('9008','TPEx',LISTED,'10000008')]:
                q.execute("INSERT INTO stock_master(code,name,market,listing_date,updated_at,issuer_id,source_date,source_url,first_seen_at,last_seen_at,presence_status,date_status,last_run_id) VALUES(%s,'fixture',%s,%s,UTC_TIMESTAMP(6),%s,%s,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),'present','provided','fixture')",
                          (code,market,listed,issuer,END,'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O'))
            repo._write_rows(q,'candles',dict(symbol='9003',interval_code='day'),[bar(date(2026,10,16),77)],datetime(2026,10,16))
            repo._write_rows(q,'candles',dict(symbol='9007',interval_code='day'),[bar(date(2026,10,16),88)],datetime(2026,10,16))

        def query(sql, values=()):
            with repo.transaction() as q:
                q.execute(sql,values)
                return q.fetchall()

        with TemporaryDirectory(prefix='listing-mysql-') as folder, \
             patch.dict(os.environ,{'CAPITAL_DAY_INTEGRITY_ENABLED':'false','CAPITAL_SUSPENSION_SYNC_ENABLED':'false'}), \
             patch('app.services.listing_history.fetch_twse_listing_origins',return_value={('9002',LISTED):'transfer'}):
            calls=[]
            class Vendor:
                cache=None
                def fetch_historical_candles(self,symbol,interval,first,last,**kwargs):
                    calls.append((symbol,interval,first,last))
                    if symbol == '9008': return []
                    days=[first+timedelta(days=i) for i in range((last-first).days+1)]
                    return [bar(d,10) for d in days if d.weekday()<5]
                def history_checkpoint(self,committed): pass

            control=BackfillControl(Path(folder)/'control.sqlite3')
            def execute(symbol,market=1,interval='day',refresh=None,derived=False):
                result=repo.plan_batch(market,interval,END,1200,'candles','candle_backfill_jobs','instruments',
                    only_symbols=[symbol],refresh_bars=refresh,aggregate_from_day=derived,force=True)
                scope=dict(market_no=market,interval=interval,end_date=END,as_of_date=END,
                    target_bars=1200,candle_table='candles',job_table='candle_backfill_jobs',instrument_table='instruments',
                    batch_v2=True,planned_on=date.today().isoformat(),planner_version=4 if derived else 3,
                    only_symbols=[symbol],**({'refresh_bars':refresh} if refresh is not None else {}),
                    **({'aggregate_from_day':True} if derived else {}))
                runner=BackfillRunner(Vendor(),repo,control,source='cli')
                runner.stage_check=lambda stage:False
                runner.job_window_budget=20
                runner.recovery_budget=100
                runner.run([scope])
                return result

            result=execute('9001')
            assert result['start_date'] == LISTED
            assert all(first>=LISTED for s,i,first,last in calls if s=='9001')
            state=query("SELECT j.status,j.last_error,p.target_bars,s.initialized FROM candle_backfill_jobs j JOIN candle_backfill_progress p ON p.job_id=j.id AND p.job_table='candle_backfill_jobs' JOIN candle_sync_state s ON s.symbol=j.symbol AND s.interval_code=j.interval_code AND s.candle_table='candles' WHERE j.symbol='9001'")[0]
            assert state[0]=='insufficient_history' and '掛牌歷史不足' in state[1] and state[2:]==(1200,1),state
            assert query("SELECT COUNT(*) FROM candles WHERE symbol='9001' AND interval_code='day'")[0][0]==2
            checks.append('inclusive listing floor; actual 2/1200 bars; explicit shortfall; initialization terminal')

            # A pre-upgrade saved backward checkpoint is below the new boundary.
            with repo.transaction() as q:
                q.execute("UPDATE candle_backfill_jobs SET status='pending' WHERE symbol='9001'")
                q.execute("UPDATE candle_batch_work w JOIN candle_backfill_jobs j ON j.id=w.job_id SET w.phase='history',w.history_floor='2010-01-01' WHERE j.symbol='9001'")
                q.execute("UPDATE candle_backfill_progress p JOIN candle_backfill_jobs j ON j.id=p.job_id SET p.next_date='2026-10-16',p.owner_token=NULL WHERE j.symbol='9001'")
            plan=dict(market_no=1,interval='day',end_date=END,candle_table='candles',job_table='candle_backfill_jobs',instrument_table='instruments',batch_v2=True,queue_phase='history',only_symbols=['9001'])
            item=repo.claim_incremental_job(plan,'old-cursor')
            before=len(calls)
            runner=BackfillRunner(Vendor(),repo,control); runner.stage_check=lambda stage:False
            runner.run_job(item)
            assert len(calls)==before
            assert query("SELECT status FROM candle_backfill_jobs WHERE symbol='9001'")[0][0]=='insufficient_history'
            checks.append('old checkpoint below listing boundary finishes without a vendor request')

            selected=repo.load_listing_policies('candles',0)
            assert selected['9002']['history_start'] is None
            assert selected['9007']['history_start']==date(2026,10,15)
            # Transferred history can be inserted but old values remain exact.
            before=query("SELECT * FROM candles WHERE symbol='9003'")
            old=repo.load_listing_policies('candles',1)['9003']
            assert old['history_start'] is None and old['reason']=='existing_history_precedes_listing'
            with repo.transaction() as q:
                repo._write_rows(q,'candles',dict(symbol='9003',interval_code='day',listing_preserve_before=LISTED),
                    [bar(date(2026,10,16),999),bar(date(2026,10,15),66),bar(LISTED,55)],datetime(2026,10,20))
            after=query("SELECT * FROM candles WHERE symbol='9003' AND candle_time='2026-10-16'")
            assert before==after
            assert query("SELECT close_price FROM candles WHERE symbol='9003' AND candle_time='2026-10-15'")[0][0]==67
            assert query("SELECT close_price FROM candles WHERE symbol='9003' AND candle_time='2026-10-19'")[0][0]==56
            checks.append('transfer fallback and same-issuer predecessor; existing pre-listing rows byte-for-byte unchanged; missing old rows inserted')

            for symbol,interval,label in [('9004','week',date(2026,10,19)),('9005','month',date(2026,10,1))]:
                execute(symbol,interval=interval,refresh=5 if interval=='week' else 2,derived=True)
                assert query('SELECT DATE(candle_time) FROM candles WHERE symbol=%s AND interval_code=%s',(symbol,interval))[0][0]==label
                assert all(first>=LISTED for s,i,first,last in calls if s==symbol)
                assert repo.load_listing_policies('candles',1)[symbol]['history_start']==LISTED
                with repo.transaction() as q:
                    updated=aggregate_daily([bar(LISTED),bar(END,99)],interval,label,END)
                    repo._write_daily_aggregates(q,'candles',dict(symbol=symbol,interval_code=interval,
                        end_date=END,listing_preserve_before=LISTED),updated,datetime.now(timezone.utc).replace(tzinfo=None))
                assert query('SELECT close_price FROM candles WHERE symbol=%s AND interval_code=%s',(symbol,interval))[0][0]==100
            checks.append('weekly/monthly listing-period labels retained; source daily queries never predate listing')

            execute('9006',refresh=10)
            assert query("SELECT status,last_error FROM candle_backfill_jobs WHERE symbol='9006'")[0][0]=='insufficient_history'
            checks.append('daily refresh also stops at listing instead of searching for 10 nonexistent bars')

            # Calendar proof and audits use fake official sessions, no HTTP.
            calendar=lambda:DayCalendar({'source':'https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule','years':{'2026':[]}},overrides=[])
            with patch('app.services.day_candle_integrity_runner.DayEvidence.load',return_value=DayEvidence()), \
                 patch('app.services.day_candle_integrity_runner.DayCalendar',side_effect=calendar):
                with repo.transaction() as q:
                    q.execute("INSERT INTO candle_day_gaps(candle_table,market_no,symbol,candle_date,status,reason,updated_at) VALUES('candles',1,'9001','2026-10-16','unresolved','old audit',UTC_TIMESTAMP(6))")
                runner=BackfillRunner(Vendor(),repo,control); runner.stage_check=lambda stage:False
                audit=DayIntegrityAudit(runner)
                scope=dict(candle_table='candles',market_no=1,end_date=END,target_bars=1200)
                result=audit.assess(scope,'9001')
                assert result['status']=='complete_available_history' and result['history_limit_reason']=='掛牌歷史不足'
                assert result['first_trade'] is None and result['stored_bars']==2
                assert query("SELECT status FROM candle_day_gaps WHERE symbol='9001'")[0][0]=='confirmed_no_bar'
                result=audit.assess(scope,'9008')
                assert result['status']=='unresolved' and result['unresolved_count']==2
                assert query("SELECT MIN(candle_date) FROM candle_day_gaps WHERE symbol='9008'")[0][0]==LISTED
            checks.append('holiday audit excludes listing-before gaps; empty listing-day data remains unresolved, not fabricated complete')

        print(json.dumps(dict(status='passed',checks=checks,temporary_schema=schema),ensure_ascii=False,indent=2))
        return 0
    finally:
        if created:
            assert re.fullmatch(r'capital_listing_test_[0-9a-f]{12}',schema) and schema != settings.database
            q=admin.cursor(); q.execute(f'DROP DATABASE `{schema}`'); q.close()
            print(json.dumps(dict(temporary_schema_removed=schema),ensure_ascii=False))
        admin.close()


if __name__=='__main__':
    raise SystemExit(main())
