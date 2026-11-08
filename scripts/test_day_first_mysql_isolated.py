"""Latest-first, recovery-last across runner instances; new test schema only."""
from dataclasses import replace
from datetime import date, datetime, timezone
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
from app.schemas.models import Candle
from app.services.backfill_control import BackfillControl
from app.services.backfill_engine import BackfillRunner
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLSettings


def main():
    load_dotenv(PROJECT/'.env'); settings=MySQLSettings.from_env()
    schema='capital_day_first_test_'+uuid.uuid4().hex[:12]
    if not re.fullmatch(r'capital_day_first_test_[0-9a-f]{12}',schema):
        raise RuntimeError('Unexpected test schema')
    admin=mysql.connector.connect(host=settings.host,port=settings.port,user=settings.user,password=settings.password)
    created=False; checks=[]
    try:
        cur=admin.cursor(); cur.execute(f'CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        admin.commit(); cur.close(); created=True
        test_settings=replace(settings,database=schema); repo=MySQLCandleRepository(test_settings)
        repo.ensure_batch_storage('candles','candle_backfill_jobs')
        with repo.transaction() as cursor:
            cursor.execute('CREATE TABLE instruments(market_no SMALLINT NOT NULL,symbol VARCHAR(32) NOT NULL,'
                           'category_no INT NOT NULL,is_active BOOLEAN NOT NULL,PRIMARY KEY(market_no,symbol))')
            cursor.executemany('INSERT INTO instruments VALUES(%s,%s,1,TRUE)',[(0,'2330'),(1,'6488')])
        end=date(2026,10,15)
        def plan(market,interval,symbol):
            return dict(market_no=market,interval=interval,start_date=None,end_date=end,as_of_date=end,
                        target_bars=2,target_source='override',candle_table='candles',job_table='candle_backfill_jobs',
                        instrument_table='instruments',only_symbols=[symbol],batch_v2=True,planned_on=None)
        # An old/user-supplied order puts minutes before daily. Only runtime
        # order changes; the user scope/targets/date bounds are not expanded.
        plans=[plan(0,'1m','2330'),plan(0,'day','2330'),plan(1,'day','6488')]
        calls=[]
        class Vendor:
            cache=None
            def fetch_historical_candles(self,symbol,interval,first,last,**kwargs):
                calls.append((symbol,interval,first,last))
                return [Candle(timestamp=datetime.combine(last,datetime.min.time(),tzinfo=timezone.utc),
                               open=10,high=12,low=9,close=11,volume=100)]
        def query(sql,args=()):
            with repo.transaction() as cursor:
                cursor.execute(sql,args); return cursor.fetchall()
        with TemporaryDirectory(prefix='capital-day-first-') as folder, \
             patch.dict('os.environ',{'CAPITAL_SUSPENSION_SYNC_ENABLED':'false','CAPITAL_DAY_INTEGRITY_ENABLED':'false'}):
            control=BackfillControl(Path(folder)/'control.sqlite3')
            first_runner=BackfillRunner(Vendor(),repo,control); first_runner.recovery_budget=0
            result=first_runner.run(plans)
            assert result['deferred']==3,result
            assert [c[1] for c in calls]==['day','day','1m'],calls
            assert control.snapshot()['priority_phase']=='recovery' and control.snapshot()['day_pending']==2
            assert control.snapshot()['normal_round_complete']
            assert query("SELECT COUNT(*) FROM candle_backfill_jobs WHERE interval_code='1m'")[0][0]==1
            state=query("SELECT j.market_no,j.status,w.phase,p.next_date FROM candle_backfill_jobs j "
                        "JOIN candle_backfill_progress p ON p.job_table='candle_backfill_jobs' AND p.job_id=j.id "
                        "JOIN candle_batch_work w ON w.job_table=p.job_table AND w.job_id=j.id ORDER BY j.market_no")
            assert len(state)==3 and all(s[1:3]==('pending','history') for s in state),state
            checks.append('daily and minute normal passes finish before recovery; zero recovery budget preserves all three cursors')
            assert query("SELECT COUNT(*) FROM candles WHERE interval_code='day'")[0][0]==2
            saved=control.saved_plans()
            assert [p['interval'] for p in saved]==['day','day','1m']
            control.update(resume_pending=True)
            # Recreate repository and worker, as after a service restart.
            repo=MySQLCandleRepository(test_settings)
            result=BackfillRunner(Vendor(),repo,control).run(saved)
            assert result['completed']==3 and not result.get('deferred'),result
            minute_index=next(i for i,c in enumerate(calls) if c[1]=='1m')
            assert minute_index==2 and all(c[1]=='day' for c in calls[:2]),calls
            assert len(calls)==6,calls  # normal day/day/minute, then day/day/minute recovery
            checks.append('restart resumes retained history cursors without repeating completed normal requests')
            assert query("SELECT symbol,COUNT(*) FROM candles WHERE interval_code='day' GROUP BY symbol ORDER BY symbol")==[('2330',2),('6488',2)]
            assert query("SELECT COUNT(*) FROM candle_backfill_jobs WHERE status='completed'")[0][0]==3
            assert query("SELECT COUNT(*) FROM candle_backfill_progress WHERE owner_token IS NOT NULL")[0][0]==0
            assert control.snapshot()['priority_phase']=='finished'
            checks.append('targets, committed counts, owner release and original three-plan scope remain correct')
        print(json.dumps(dict(passed=len(checks),checks=checks,capital_dll_used=False,
                              production_database_modified=False),ensure_ascii=False),flush=True)
    finally:
        if created:
            cur=admin.cursor(); cur.execute(f'DROP DATABASE `{schema}`'); admin.commit(); cur.close()
            print('本次日 K 優先測試資料庫已清除；正式資料庫未修改。',flush=True)
        admin.close()


if __name__=='__main__':
    main()
