"""Real SQL queue/status exclusion, only in a new invocation-owned schema."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
import uuid

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from dotenv import load_dotenv
import mysql.connector
from app.schemas.models import Candle, Instrument
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLSettings, MySQLCandleRepository as LegacyRepository


def main():
    load_dotenv(PROJECT/'.env')
    settings=MySQLSettings.from_env()
    schema='capital_symbol_exclusion_test_'+uuid.uuid4().hex[:12]
    if not re.fullmatch(r'capital_symbol_exclusion_test_[0-9a-f]{12}',schema):
        raise RuntimeError('Unexpected test schema')
    admin=mysql.connector.connect(host=settings.host,port=settings.port,user=settings.user,password=settings.password)
    created=False; checks=[]
    try:
        cur=admin.cursor(); cur.execute(f'CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        admin.commit(); cur.close(); created=True
        repo=MySQLCandleRepository(replace(settings,database=schema)); repo.initialize()
        repo.ensure_batch_storage('candles','candle_backfill_jobs')
        def instruments(market):
            return [Instrument(market_no=market,symbol=s,name=s,category_no=99 if s=='TSLQ' else 1,
                               category_name='test',order_symbol=s) for s in ['TSLQ','2330']]
        repo.sync_market(0,instruments(0))
        def query(sql,args=()):
            with repo.transaction() as cursor:
                cursor.execute(sql,args); return cursor.fetchall()
        first,end=date(2026,9,1),date(2026,10,16)
        intervals=('1m','5m','15m','60m','day','week','month')
        for interval in intervals:
            for force in (False,True):
                result=repo.plan_incremental(0,interval,end,1200,'candles','candle_backfill_jobs','instruments',
                                              force=force,explicit_start=first)
                assert result['queued_jobs']==1,result
                result=repo.plan_batch(0,interval,end,1200,'candles','candle_backfill_jobs','instruments',
                                       force=force,explicit_start=first)
                assert result['queued_jobs']==1,result
                result=repo.plan_batch(0,interval,end,1200,'candles','candle_backfill_jobs','instruments',
                                       force=force,refresh_bars=5,aggregate_from_day=interval in {'week','month'})
                assert result['queued_jobs']==1,result
            assert repo.plan_incremental(0,interval,end,1200,'candles','candle_backfill_jobs','instruments',
                                         force=True,only_symbols=['TSLQ'])['queued_jobs']==0
        assert query("SELECT COUNT(*) FROM candle_backfill_jobs WHERE symbol='TSLQ'")[0][0]==0
        repo.create_backfill_jobs([0],'day',first,end,1200,force=True)
        assert query("SELECT COUNT(*) FROM candle_backfill_jobs WHERE symbol='TSLQ'")[0][0]==0
        checks.append('all seven intervals: incremental/full-range/daily/derived/force and explicit symbol cannot queue TSLQ')

        # Retain deliberately seeded old audit jobs. A new policy must stop
        # claiming/retrying them without deleting or rewriting their history.
        now=datetime.now(timezone.utc).replace(tzinfo=None)
        with repo.transaction() as cursor:
            cursor.execute("INSERT INTO candle_backfill_jobs(market_no,symbol,interval_code,start_date,end_date,status,received_count,last_error,updated_at) "
                           "VALUES(0,'TSLQ','day',%s,%s,'failed',337,'retained old error',%s)",(first,end,now))
            job_id=cursor.lastrowid
            cursor.execute("INSERT INTO candle_backfill_progress(job_table,job_id,mode,target_bars,next_date,empty_windows,updated_at) "
                           "VALUES('candle_backfill_jobs',%s,'range',1200,%s,0,%s)",(job_id,first,now))
            cursor.execute("INSERT INTO candle_batch_work(job_table,job_id,phase,needs_history,history_floor,history_next) "
                           "VALUES('candle_backfill_jobs',%s,'latest',FALSE,%s,%s)",(job_id,first,first))
        retained=query('SELECT j.*,p.* FROM candle_backfill_jobs j JOIN candle_backfill_progress p '
                       "ON p.job_table='candle_backfill_jobs' AND p.job_id=j.id WHERE j.id=%s",(job_id,))
        assert repo.failed_symbols(0,'day','candle_backfill_jobs','instruments')==[]
        repo.requeue_latest_failed_backfill_jobs([0],['day'])
        assert query('SELECT j.*,p.* FROM candle_backfill_jobs j JOIN candle_backfill_progress p '
                     "ON p.job_table='candle_backfill_jobs' AND p.job_id=j.id WHERE j.id=%s",(job_id,))==retained
        scope=dict(market_no=0,interval='day',end_date=end,candle_table='candles',job_table='candle_backfill_jobs',
                   instrument_table='instruments',only_symbols=['TSLQ'],batch_v2=True,planned_on='2026-10-20')
        with repo.transaction() as cursor:
            cursor.execute("UPDATE candle_backfill_jobs SET status='pending' WHERE id=%s",(job_id,))
        assert repo.claim_incremental_job(scope,'must-not-own') is None
        claimed=repo.claim_backfill_job('day',first,end,include_failed=True)
        assert claimed is None or claimed['symbol']!='TSLQ',claimed
        if claimed:
            with repo.transaction() as cursor:
                cursor.execute("UPDATE candle_backfill_jobs SET status='pending' WHERE id=%s",(claimed['id'],))
        assert repo.batch_pending([scope])==(0,None)
        groups=repo.get_pending_backfill_groups([0],['day'])
        assert sum(g['queued_jobs'] for g in groups)==query(
            "SELECT COUNT(*) FROM candle_backfill_jobs WHERE market_no=0 AND interval_code='day' AND symbol='2330' AND status='pending'")[0][0]
        with repo.transaction() as cursor:
            cursor.execute("UPDATE candle_backfill_jobs SET status='running' WHERE id=%s",(job_id,))
        repo.recover_incremental_jobs(scope)
        repo.reset_running_backfill_jobs('day',first,end)
        assert query('SELECT status FROM candle_backfill_jobs WHERE id=%s',(job_id,))[0][0]=='running'
        checks.append('old failed/pending/running jobs are retained, not retried, counted as waiting, or reclaimed')

        row=Candle(timestamp=datetime(2026,10,16),open=10,high=12,low=9,close=11,volume=100)
        repo.upsert_candles('TSLQ','day',[row]); repo.upsert_candles('2330','day',[row])
        before=query("SELECT * FROM candles WHERE symbol='TSLQ'")
        repo.sync_market(0,instruments(0))
        assert query("SELECT is_active FROM instruments WHERE symbol='TSLQ'")[0][0]==1
        assert repo.plan_batch(0,'day',end,1200,'candles','candle_backfill_jobs','instruments',
                               force=True,only_symbols=['TSLQ'])['queued_jobs']==0
        assert query("SELECT * FROM candles WHERE symbol='TSLQ'")==before
        assert len(repo.get_candles('TSLQ','day',limit=10))==1
        checks.append('actual product-master resync cannot reinclude TSLQ; stored candles remain readable and unchanged')

        summary=repo.get_candle_storage_summary([0],['day'])
        assert summary['eligible_symbols']==1 and summary['by_interval']['day']['symbol_count']==1,summary
        for repo_type in (MySQLCandleRepository,LegacyRepository):
            dashboard=repo_type(repo.settings).get_backfill_dashboard_status([0],['day'],'candles','candle_backfill_jobs','instruments')
            item=dashboard['items'][(0,'day')]
            assert item['active_instruments']==1 and item['tracked_symbols']==1,item
            assert item['job_counts']['running']==0,item
        checks.append('active/tracked/failed/running/storage summaries exclude TSLQ rather than misreporting it as complete')

        repo.sync_market(5,instruments(5),table_name='odd_lot_instruments')
        result=repo.plan_batch(5,'day',end,1200,'odd_lot_candle','odd_lot_candle_backfill_jobs','odd_lot_instruments',force=True)
        assert result['queued_jobs']==1,result
        assert query("SELECT COUNT(*) FROM odd_lot_candle_backfill_jobs WHERE symbol='TSLQ'")[0][0]==0
        checks.append('same exclusion policy also covers odd-lot queues')
        print(json.dumps(dict(passed=len(checks),checks=checks,capital_dll_used=False,
                              production_database_modified=False),ensure_ascii=False),flush=True)
    finally:
        if created:
            cur=admin.cursor(); cur.execute(f'DROP DATABASE `{schema}`'); admin.commit(); cur.close()
            print('本次商品排除測試資料庫已清除；正式資料庫未修改。',flush=True)
        admin.close()


if __name__=='__main__':
    main()
