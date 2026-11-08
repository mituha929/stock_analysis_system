"""Real SQL atomicity/strict 1200-date barrier; disposable owned schema only."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
import mysql.connector
from app.schemas.models import Candle
from app.services.backfill_control import BackfillControl
from app.services.backfill_engine import BackfillRunner
from app.services.day_candle_integrity import DayCalendar,DayEvidence,assess_days
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLSettings,MySQLRepositoryError

END=date(2026,10,20)
MISSING=date(2026,10,19)


def bar(day,close=11):
    return Candle(timestamp=datetime.combine(day,datetime.min.time(),tzinfo=timezone.utc),
                  open=10,high=12,low=9,close=close,volume=100)


def test_calendar():
    # Deliberately synthetic calendar, in process only; no production cache.
    return DayCalendar({'source':'https://www.twse.com.tw/test-fixture',
                        'years':{str(y):[] for y in range(2019,2027)}},overrides=[])


def main():
    load_dotenv(ROOT/'.env')
    settings=MySQLSettings.from_env()
    schema='capital_day_integrity_test_'+uuid.uuid4().hex[:12]
    if not re.fullmatch(r'capital_day_integrity_test_[0-9a-f]{12}',schema):
        raise RuntimeError('Not an invocation-owned test schema')
    admin=mysql.connector.connect(host=settings.host,port=settings.port,user=settings.user,password=settings.password)
    created=False; checks=[]
    try:
        q=admin.cursor();q.execute(f'CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        admin.commit();q.close();created=True
        test_settings=replace(settings,database=schema)
        repo=MySQLCandleRepository(test_settings)

        def query(sql,args=()):
            with repo.transaction() as q:
                q.execute(sql,args); return q.fetchall()

        assert repo.day_integrity_report([0])['storage_ready'] is False
        assert query('SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()')[0][0]==0
        checks.append('report is SELECT-only and does not create tables')
        repo.ensure_batch_storage('candles','candle_backfill_jobs')
        repo.ensure_day_integrity_storage()
        with repo.transaction() as q:
            q.execute('CREATE TABLE instruments(market_no SMALLINT NOT NULL,symbol VARCHAR(32) NOT NULL,'
                      'category_no INT NOT NULL,is_active BOOLEAN NOT NULL,PRIMARY KEY(market_no,symbol))')
            q.executemany('INSERT INTO instruments VALUES(%s,%s,1,TRUE)',[(0,'GOOD'),(0,'NEW'),(0,'TSLQ')])
        # There are already 1200 bars, but one INTERIOR trading day is missing.
        days=[]; day=END
        while len(days)<1201:
            if day.weekday()<5: days.append(day)
            day-=timedelta(days=1)
        initial=sorted(d for d in days if d!=MISSING)
        with repo.transaction() as q:
            repo._write_rows(q,'candles',dict(symbol='GOOD',interval_code='day'),[bar(d) for d in initial],datetime.now(timezone.utc).replace(tzinfo=None))
        assert len(repo.day_integrity_dates('candles','GOOD',END,1200))==1200

        def scope(interval):
            return dict(market_no=0,interval=interval,start_date=None,end_date=END,as_of_date=END,
                        target_bars=1200 if interval=='day' else 1,target_source='override',
                        candle_table='candles',job_table='candle_backfill_jobs',instrument_table='instruments',
                        only_symbols=['GOOD'],batch_v2=True,planned_on=date.today().isoformat() if interval=='day' else None,
                        planner_version=3)
        calls=[];acks=[]; repair=False
        class Vendor:
            cache=None
            quote_ready=True
            def fetch_historical_candles(self,symbol,interval,first,last,**options):
                calls.append((symbol,interval,first,last))
                return [bar(MISSING)] if interval=='day' and repair and first<=MISSING<=last else [bar(last)] if interval!='day' else []
            def history_checkpoint(self,committed): acks.append(committed)

        with TemporaryDirectory(prefix='day-integrity-sql-') as folder, \
             patch.dict('os.environ',{'CAPITAL_SUSPENSION_SYNC_ENABLED':'false','CAPITAL_DAY_INTEGRITY_ENABLED':'true',
                                      'CAPITAL_DAY_INTEGRITY_SCHEDULE':'always'}), \
             patch('app.services.day_candle_integrity_runner.DayCalendar',side_effect=test_calendar), \
             patch('app.services.day_candle_integrity_runner.DayEvidence.load',return_value=DayEvidence()):
            control=BackfillControl(Path(folder)/'control.sqlite3')
            plans=[scope('1m'),scope('day')]
            first=BackfillRunner(Vendor(),repo,control).run(plans)
            assert not first['day_first_waiting'] and first['audit_pending_symbols']==1,first
            assert control.snapshot()['day_integrity']['unresolved_dates']==1
            assert query("SELECT COUNT(*) FROM candle_backfill_jobs WHERE interval_code='1m'")[0][0]==1
            report=repo.day_integrity_report([0],limit=1)
            assert report['total']==1 and report['items'][0]['candle_date']==MISSING,report
            assert report['items'][0]['status']=='unresolved' and report['items'][0]['attempt_count']==1
            assert acks==[True,True],acks
            checks.append('minute normal collection precedes daily audit; interior gap remains unresolved rather than blocking other intervals')

            # An acknowledged empty interval and its retry survive reconstruction.
            repo=MySQLCandleRepository(test_settings)
            saved=control.saved_plans()
            assert repo.day_integrity_report([0])['items'][0]['attempt_count']==1
            checks.append('unresolved symbol/date, attempt count and retry survive repository/runner reconstruction')

            # Force a failure AFTER candle INSERT but BEFORE gap status commit.
            query("CREATE TRIGGER force_gap_rollback BEFORE UPDATE ON candle_day_gaps FOR EACH ROW "
                  "BEGIN IF NEW.status='resolved' THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='isolated rollback'; END IF; END")
            owner=query("SELECT owner_token FROM candle_day_integrity WHERE symbol='GOOD'")[0][0]
            try:
                repo.commit_day_gap_window('candles',0,'GOOD',MISSING,MISSING,[bar(MISSING)],owner,900)
                raise AssertionError('Expected SQL rollback')
            except MySQLRepositoryError:
                pass
            assert query("SELECT COUNT(*) FROM candles WHERE symbol='GOOD' AND candle_time=%s",(MISSING,))[0][0]==0
            assert repo.day_integrity_report([0])['items'][0]['status']=='unresolved'
            query('DROP TRIGGER force_gap_rollback')
            checks.append('candle write and resolved status roll back atomically on SQL failure')

            # Retry only this isolated gap; normal production backoff is untouched.
            query("UPDATE candle_day_gaps SET next_retry_at=NULL WHERE symbol='GOOD'")
            repair=True; control.update(resume_pending=True)
            result=BackfillRunner(Vendor(),repo,control).run(saved)
            assert not result.get('deferred') and control.snapshot()['priority_phase']=='finished',result
            assert repo.day_integrity_report([0])['total']==0
            all_rows=repo.day_integrity_report([0],include_resolved=True)
            assert all_rows['items'][0]['status']=='resolved'
            assert query("SELECT COUNT(*) FROM candles WHERE symbol='GOOD' AND interval_code='day'")[0][0]==1201
            assert [c[1] for c in calls]==['1m','day','day'],calls
            assert query("SELECT close_price FROM candles WHERE symbol='GOOD' AND candle_time=%s",(MISSING,))[0][0]==11
            checks.append('targeted day is upserted and last1200 reaudits complete without repeating finished minute collection')

            # Same timestamp is overwritten, not duplicated.
            owner=query("SELECT owner_token FROM candle_day_integrity WHERE symbol='GOOD'")[0][0]
            repo.commit_day_gap_window('candles',0,'GOOD',MISSING,MISSING,[bar(MISSING,close=12)],owner,900)
            assert query("SELECT COUNT(*),MAX(close_price) FROM candles WHERE symbol='GOOD' AND candle_time=%s",(MISSING,))==[(1,12)]
            checks.append('same-date repair overwrites existing candle, never adds a duplicate')

            # The SQL data-error ledger quarantines on failure five; it never
            # calls an empty response proof that the missing date did not trade.
            query("UPDATE candle_day_gaps SET status='unresolved',data_failures=0 WHERE symbol='GOOD'")
            for _ in range(5): repo.record_day_gap_data_error('candles',0,'GOOD',MISSING,MISSING,'negative volume')
            status,count=query("SELECT status,data_failures FROM candle_day_gaps WHERE symbol='GOOD'")[0]
            assert (status,count)==('failed_data',5),(status,count)
            checks.append('same-window data failures persist and quarantine on fifth, not as no-trade')

            # A young security needs official, reviewed FIRST-EVER-trade proof.
            newer=[date(2026,10,19),END]
            no_proof=assess_days(newer,END,1200,date(2026,10,16),0,'NEW',DayEvidence(),test_calendar())
            assert no_proof['status']!='complete_available_history'
            evidence=DayEvidence([dict(market_no=0,symbol='NEW',kind='first_trade',start_date='2026-10-19',
                end_date='2026-10-19',source='https://www.twse.com.tw/test-official-notice',reason='isolated fixture',
                reviewed=True,reviewed_on='2026-10-20')])
            confirmed=assess_days(newer,END,1200,date(2026,10,16),0,'NEW',evidence,test_calendar())
            repo.save_day_assessment('candles',0,'NEW',confirmed,newer,'isolated-owner')
            assert confirmed['status']=='complete_available_history' and confirmed['stored_bars']==2
            assert repo.day_integrity_symbols(dict(market_no=0,instrument_table='instruments',only_symbols=['TSLQ']))==[]
            checks.append('short first-listed history needs evidence; not called 1200 bars, TSLQ still excluded')

            # A formerly unknown calendar date must not remain a phantom
            # missing-bar query after the official calendar explains it.
            unknown_calendar=DayCalendar({'years':{'2026':[]}},overrides=[])
            before_calendar=assess_days([END],END,2,MISSING,0,'NEW',DayEvidence(),unknown_calendar)
            repo.save_day_assessment('candles',0,'NEW',before_calendar,[END],'calendar-owner')
            confirmed_calendar=DayCalendar({'source':'https://www.twse.com.tw/test-fixture',
                                           'years':{'2026':[str(MISSING)]}},overrides=[])
            after_calendar=assess_days([END],END,2,MISSING,0,'NEW',DayEvidence(),confirmed_calendar)
            repo.save_day_assessment('candles',0,'NEW',after_calendar,[END],'calendar-owner')
            closed=query("SELECT status,reason,next_retry_at FROM candle_day_gaps WHERE symbol='NEW' AND candle_date=%s",(MISSING,))[0]
            assert closed==('confirmed_no_bar','market_closed',None),closed
            checks.append('calendar recovery closes only evidence-backed nontrading gaps; no phantom retries or fake candles')

            # A past failed audit must not become a trading-day completion
            # gate. Real ordinary planning still runs without touching it.
            before_audit=query("SELECT status,data_failures FROM candle_day_gaps WHERE symbol='GOOD'")
            with patch('app.services.backfill_engine.integrity_schedule',return_value=(False,'fixture trading day')):
                normal=BackfillRunner(Vendor(),repo,control).run(saved)
            assert not normal.get('day_first_waiting'),normal
            assert query("SELECT status,data_failures FROM candle_day_gaps WHERE symbol='GOOD'")==before_audit
            checks.append('trading-day run skips historical integrity gate without falsely clearing saved gaps')

            # Holiday maintenance uses the real ledger/upserts, but cannot
            # replace ordinary plans, completed counts, timer or checkpoint.
            from app.api.routes import BACKFILL_WORKER_LOCK,MARKET_BACKFILL_SOURCES
            from app.services.backfill_scheduler import DailyBackfillConfig
            from app.services.day_candle_integrity_maintenance import run_day_maintenance
            config=replace(DailyBackfillConfig.from_env(),markets=(0,),intervals=('day',),
                           target_bars_by_interval={'day':3})
            control.update(state='waiting_backfill',processed=5660,next_retry_at='2026-10-24T13:00:00Z',
                           checkpoint='2026-09-03')
            before_control=control.snapshot()
            with BACKFILL_WORKER_LOCK, control.worker(), \
                 patch('app.services.day_candle_integrity_maintenance.latest_trading_day_on_or_before',return_value=END), \
                 patch('app.services.day_candle_integrity_maintenance.load_suspensions',return_value=None):
                status,_message=run_day_maintenance(Vendor(),config,control,lambda:repo,MARKET_BACKFILL_SOURCES)
            assert status=='waiting_backfill',status
            after_control=control.snapshot()
            for key,value in before_control.items():
                if key not in {'updated_at','day_integrity_maintenance'}:
                    assert after_control[key]==value,(key,after_control[key],value)
            assert not after_control['day_integrity_maintenance']['active']
            assert after_control['day_integrity_maintenance']['day_integrity']['checked_symbols']==2
            checks.append('holiday-only real SQL repair preserves ordinary scope, counters, retry timer and cursor')
        print(json.dumps(dict(passed=len(checks),checks=checks,capital_dll_used=False,
                              production_database_modified=False),ensure_ascii=False),flush=True)
    finally:
        if created:
            q=admin.cursor();q.execute(f'DROP DATABASE `{schema}`');admin.commit();q.close()
            print('Only this invocation-owned day integrity test schema was removed.',flush=True)
        admin.close()


if __name__=='__main__': main()
