"""One-shot day-K assessment ONLY. No vendor login, repairs or queue changes.

Default writes a diagnostic report only. --apply-results additionally upserts
the two existing integrity ledger tables under the shared worker lease. Source
candles use a read-only consistent MySQL snapshot. Normal plans are untouched.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone, timedelta
import gzip
import json
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
from app.services.backfill_control import BackfillControl
from app.services.backfill_policy import initial_start
from app.services.day_candle_integrity import POLICY_VERSION, DayCalendar, DayEvidence, assess_days, taipei_today
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.instrument_lifecycle import fetch_bond_lifecycles, blocked_reason
from app.services.process_memory import current_process_memory
from app.services.taiwan_trading_calendar import latest_trading_day_on_or_before
from app.services.trading_suspensions import load_suspensions

MARKETS={0:('上市','instruments','candles'),1:('上櫃','instruments','candles'),
         4:('興櫃','instruments','candles'),5:('上市零股','odd_lot_instruments','odd_lot_candle'),
         6:('上櫃零股','odd_lot_instruments','odd_lot_candle')}
PROTECTED=('plans','run_id','pause_requested','resume_pending','resume_scope','pending_schedule',
           'normal_round_complete','day_integrity_deferred','processed','completed','failed',
           'insufficient_history','windows','next_retry_at','state','stage')


class ReadSnapshotRepository(MySQLCandleRepository):
    def __init__(self):
        super().__init__()
        self.connection=self._connect_database(self._connector())
        self.connection.start_transaction(isolation_level='REPEATABLE READ',readonly=True,consistent_snapshot=True)

    @contextmanager
    def transaction(self,dictionary=False):
        q=self.connection.cursor(dictionary=dictionary)
        try: yield q
        finally: q.close()

    def close(self):
        self.connection.rollback(); self.connection.close()


def date_ranges(days):
    result=[]
    for day in sorted(set(days)):
        if result and day==result[-1][1]+timedelta(days=1): result[-1][1]=day
        else: result.append([day,day])
    return ', '.join(str(a) if a==b else f'{a}～{b}' for a,b in result)


def load_policies(reader,table,market,bonds):
    # Deliberately bypass load_listing_policies(): the worker version also
    # reconciles old queue waits. An assessment must not change those queues.
    policies=reader._load_stock_listing_policies(table,market)
    if market!=1: return policies
    saved={}
    with reader.transaction(dictionary=True) as q:
        q.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='instrument_lifecycle'")
        if q.fetchone()['n']:
            q.execute('SELECT * FROM instrument_lifecycle WHERE market_no=1')
            saved={str(r['symbol']):r for r in q.fetchall()}
        q.execute('SELECT symbol,name FROM instruments WHERE market_no=1')
        names={str(r['symbol']):r['name'] for r in q.fetchall()}
    for row in bonds:
        code=row['symbol']; old=saved.get(code,{})
        merged=dict(old,**row)
        for key in ('listing_date','maturity_date','termination_date','listing_source_url','maturity_source_url','terminal_source_url','terminal_reason'):
            if merged.get(key) is None: merged[key]=old.get(key)
        if merged.get('trading_status')=='unknown' and old.get('trading_status'):
            merged['trading_status']=old['trading_status']
        saved[code]=merged
    for code,row in saved.items():
        if row['name']!=names.get(code): continue
        policies[code]=dict(row,history_start=row['listing_date'],reason='official_bond_listing',
                            source=row.get('listing_source_url') or row['source_url'],market='TPEx')
    return policies


def require_ledger(writer):
    with writer.transaction() as q:
        q.execute("SELECT TABLE_NAME,ENGINE FROM information_schema.tables WHERE table_schema=DATABASE() "
                  "AND table_name IN('candle_day_integrity','candle_day_gaps')")
        rows=dict(q.fetchall())
        if len(rows)!=2 or any(v!='InnoDB' for v in rows.values()):
            raise RuntimeError('既有兩張 InnoDB 驗收表尚未就緒；本工具不建立或清空資料表')
        q.execute("SELECT TRIGGER_NAME FROM information_schema.triggers WHERE trigger_schema=DATABASE() "
                  "AND event_object_table IN('candle_day_integrity','candle_day_gaps')")
        if q.fetchall(): raise RuntimeError('驗收表有額外觸發器，停止以避免修改其他資料')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply-results',action='store_true')
    parser.add_argument('--markets',default='all')
    parser.add_argument('--as-of',type=date.fromisoformat)
    parser.add_argument('--limit',type=int,default=0,help='每市場上限，0 表示全部；只供小範圍預覽')
    args=parser.parse_args()
    if args.limit<0: parser.error('--limit 不可為負數')
    if args.apply_results and args.limit: parser.error('部分預覽不得寫成完整驗收結果')
    markets=list(MARKETS) if args.markets=='all' else list(dict.fromkeys(int(v) for v in args.markets.split(',')))
    if not markets or any(m not in MARKETS for m in markets): parser.error('不支援的市場')
    load_dotenv(ROOT/'.env',override=False)
    control=BackfillControl(); before=control.snapshot()
    end=args.as_of or latest_trading_day_on_or_before(taipei_today())
    if end>taipei_today(): parser.error('驗收日期不得在未來')
    if before.get('pause_requested') or before.get('preparation_pending') or before.get('day_integrity_active'):
        raise RuntimeError('使用者暫停或工作準備／驗收尚未結束；不解除暫停、不搶占工作')
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
    base=ROOT/'output'/'diagnostics'/f'day-integrity-audit-{stamp}'
    base.parent.mkdir(parents=True,exist_ok=True)
    details=base.with_suffix('.jsonl.gz'); report=base.with_suffix('.md'); summary_path=base.with_suffix('.json')
    if any(p.exists() for p in (details,report,summary_path)): raise RuntimeError('報告檔已存在，不覆蓋')
    started=time.monotonic(); owner=str(uuid.uuid4())
    summaries=[]; items=[]; total=0; reader=None
    with control.worker():
        if args.apply_results: require_ledger(MySQLCandleRepository())
        bonds=fetch_bond_lifecycles(taipei_today().isoformat())
        suspensions=load_suspensions(); evidence=DayEvidence.load(); sessions=DayCalendar()
        print(json.dumps(dict(event='start',as_of=str(end),markets=markets,policy_version=POLICY_VERSION,
              official_bond_records=len(bonds),suspensions=suspensions.status,report=str(report),applied=args.apply_results),
              ensure_ascii=False,default=str),flush=True)
        if args.apply_results:
            control.update(day_integrity_maintenance=dict(active=True,state='running',assessment_only=True,
                day_integrity_active=True,as_of_date=str(end),checked_symbols=0,day_integrity_checked=0))
        try:
            reader=ReadSnapshotRepository(); writer=MySQLCandleRepository()
            with gzip.open(details,'xt',encoding='utf-8') as stream:
                stream.write(json.dumps(dict(type='header',as_of=str(end),target_bars=1200,policy_version=POLICY_VERSION,
                    source_snapshot_started_at=datetime.now(timezone.utc).isoformat(),quotes_requested=0,
                    apply_results=args.apply_results,normal_plans=before.get('plans')),ensure_ascii=False,default=str)+'\n')
                for market in markets:
                    label,instruments,table=MARKETS[market]
                    plan=dict(market_no=market,candle_table=table,instrument_table=instruments,end_date=end,target_bars=1200)
                    symbols=reader.day_integrity_symbols(plan)
                    if args.limit: symbols=symbols[:args.limit]
                    with reader.transaction() as q:
                        q.execute(f'SELECT symbol,name FROM `{instruments}` WHERE market_no=%s',(market,))
                        names={str(s):n for s,n in q.fetchall()}
                    policies=load_policies(reader,table,market,bonds)
                    tally=Counter(); states=Counter(); last_progress=time.monotonic()
                    for index,symbol in enumerate(symbols,1):
                        if control.snapshot().get('pause_requested'): raise RuntimeError('收到安全暫停，已完成的驗收結果保留')
                        memory=current_process_memory()
                        if memory.private_usage_mb and memory.private_usage_mb>=1536:
                            raise RuntimeError('驗收程序超過 1536 MiB 保護門檻，停止並保留已完成結果')
                        dates=reader.day_integrity_dates(table,symbol,end,1200)
                        policy=policies.get(symbol); floor=initial_start('day',end,1200)
                        if (policy or {}).get('history_start'): floor=max(floor,policy['history_start'])
                        result=assess_days(dates,end,1200,floor,market,symbol,evidence,sessions,suspensions,listing=policy)
                        if any(r['reason']=='trade_activity_unconfirmed' for r in result['issues']):
                            first=min(r['day'] for r in result['issues']); last=max(r['day'] for r in result['issues'])
                            activity=reader.trade_activity_dates(table,symbol,first,last)
                            result=assess_days(dates,end,1200,floor,market,symbol,evidence,sessions,suspensions,listing=policy,activity=activity)
                        result.update(assessment_only=True,automatic_collection_stopped=blocked_reason(policy),quotes_requested=0)
                        if args.apply_results: writer.save_day_assessment(table,market,symbol,result,dates,owner)
                        row=dict(market_no=market,market_name=label,symbol=symbol,name=names.get(symbol),status=result['status'],
                            stored_bars=result['stored_bars'],first_bar=result['first_bar'],last_bar=result['last_bar'],
                            listing_boundary=result['listing_boundary'],maturity_date=(policy or {}).get('maturity_date'),
                            termination_date=(policy or {}).get('termination_date'),lifecycle_stop=blocked_reason(policy),
                            unresolved_dates=result['unresolved_count'],api_missing_dates=result['api_missing_dates'],
                            activity_unconfirmed_dates=result['activity_unconfirmed_dates'],
                            unknown_calendar_years=result['calendar_years_unverified'],calendar_conflicts=len(result['calendar_conflicts']),
                            missing_ranges=date_ranges(r['day'] for r in result['issues']))
                        items.append(row); states[result['status']]+=1; tally['symbols']+=1
                        for key in ('unresolved_dates','api_missing_dates','activity_unconfirmed_dates','calendar_conflicts'):
                            tally[key]+=row[key]
                        tally['unresolved_symbols']+=bool(row['unresolved_dates'])
                        tally['api_missing_symbols']+=bool(row['api_missing_dates'])
                        tally['activity_unconfirmed_symbols']+=bool(row['activity_unconfirmed_dates'])
                        tally['has_asof_bar']+=end in dates
                        tally['lifecycle_stopped']+=bool(row['lifecycle_stop'])
                        tally['unknown_listing_boundary']+=not bool(result['listing_boundary'])
                        stream.write(json.dumps(dict(type='assessment',market_no=market,symbol=symbol,name=names.get(symbol),
                            **result),ensure_ascii=False,default=str)+'\n')
                        total+=1
                        if index%25==0 or time.monotonic()-last_progress>=8 or index==len(symbols):
                            print(json.dumps(dict(event='progress',market=label,checked=index,market_total=len(symbols),
                                all_checked=total,unresolved_symbols=tally['unresolved_symbols'],unresolved_dates=tally['unresolved_dates'],
                                private_mb=memory.private_usage_mb,elapsed_seconds=round(time.monotonic()-started,1)),ensure_ascii=False),flush=True)
                            if args.apply_results:
                                control.update(day_integrity_maintenance=dict(active=True,state='running',assessment_only=True,
                                    day_integrity_active=True,as_of_date=str(end),checked_symbols=total,
                                    day_integrity_checked=total,current_market=market,current_symbol=symbol))
                            last_progress=time.monotonic()
                    summaries.append(dict(market_no=market,market_name=label,**tally,statuses=dict(states)))
                stream.write(json.dumps(dict(type='complete',checked_symbols=total,quotes_requested=0),ensure_ascii=False)+'\n')
            after=control.snapshot()
            preserved=all(before.get(k)==after.get(k) for k in PROTECTED)
            payload=dict(status='finished',as_of=str(end),target_bars=1200,policy_version=POLICY_VERSION,
                applied=args.apply_results,assessment_only=True,checked_symbols=total,
                unique_symbols=len({r['symbol'] for r in items}),markets=summaries,items=items,
                quotes_requested=0,repaired_windows=0,normal_control_preserved=preserved,
                elapsed_seconds=round(time.monotonic()-started,1),details=str(details),report=str(report))
            summary_path.write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
            lines=[f'# 日 K 最近 1,200 根完整性驗收（截至 {end}）','',
                   '僅驗收資料庫；群益行情請求 0 次、補抓 0 次，不重排原有工作。',
                   '「完成驗收」不代表每檔資料完整；商品以市場＋代號計數，缺少日期另計。','',
                   '| 市場 | 檢查商品 | 完整 1200 根 | 掛牌可用歷史完整但不足1200 | 待查商品 | 待查日期 | 有成交但缺K日期 | 當日有K |',
                   '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
            for s in summaries:
                lines.append(f"| {s['market_name']} | {s['symbols']} | {s['statuses'].get('complete',0)} | {s['statuses'].get('complete_available_history',0)} | {s['unresolved_symbols']} | {s['unresolved_dates']} | {s['api_missing_dates']} | {s['has_asof_bar']} |")
            lines+=['','## 未解決商品（完整逐日內容另存 JSONL.gz）','',
                    '| 市場 | 代號 | 名稱 | 狀態 | 實存根數 | 最後日K | 待查日期數 | 有成交但缺K | 待查日期範圍 |',
                    '| --- | --- | --- | --- | ---: | --- | ---: | ---: | --- |']
            for r in items:
                if r['status'] not in {'complete','complete_available_history'}:
                    name=str(r['name'] or '').replace('|','／')
                    lines.append(f"| {r['market_name']} | {r['symbol']} | {name} | {r['status']} | {r['stored_bars']} | {r['last_bar'] or '—'} | {r['unresolved_dates']} | {r['api_missing_dates']} | {r['missing_ranges'] or '—'} |")
            lines+=['','未有成交證據的日期不是已確認 API 漏資料，也不是已核實無交易。',
                    '掛牌來源不可靠、轉市場來源未確認或原有更早歷史時，不猜測首次交易日。歷史個股停牌證據並非全市場完整，未知仍待查。',
                    f'政策版本：{POLICY_VERSION}；原回補控制與計畫未改：{preserved}；詳細結果：{details.name}。']
            report.write_text('\n'.join(lines)+'\n',encoding='utf-8')
            if args.apply_results:
                unresolved=sum(s['unresolved_dates'] for s in summaries)
                control.update(day_integrity_maintenance=dict(active=False,state='waiting_review' if unresolved else 'completed',
                    assessment_only=True,day_integrity_active=False,as_of_date=str(end),checked_symbols=total,day_integrity_checked=total,
                    unresolved_symbols=sum(s['unresolved_symbols'] for s in summaries),unresolved_dates=unresolved,
                    day_integrity=dict(checked_symbols=total,complete_symbols=sum(s['statuses'].get('complete',0) for s in summaries),
                        available_history_symbols=sum(s['statuses'].get('complete_available_history',0) for s in summaries),
                        unresolved_symbols=sum(s['unresolved_symbols'] for s in summaries),unresolved_dates=unresolved),
                    query_windows=0,repaired_windows=0,report=str(report)))
            print(json.dumps({k:v for k,v in payload.items() if k!='items'},ensure_ascii=False,default=str),flush=True)
        except BaseException as error:
            if args.apply_results:
                control.update(day_integrity_maintenance=dict(active=False,state='incomplete',assessment_only=True,
                    as_of_date=str(end),checked_symbols=total,error=type(error).__name__,details=str(details)))
            raise
        finally:
            if reader is not None: reader.close()


if __name__=='__main__': main()
