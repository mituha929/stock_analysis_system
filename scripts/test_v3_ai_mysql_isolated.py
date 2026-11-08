"""Real MySQL + fake model; all tables live in a newly created disposable schema.

Never start the production lifespan, invoke Capital or change production rows.
The uniquely named test schema is dropped in finally, including on assertion failure.
"""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
import asyncio
import json
import re
import sys
import uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
import mysql.connector
from fastapi import FastAPI
from app.api.ai_rules import router
from app.ai.schemas import ParseResponse
from app.services.mysql_instrument_repository import MySQLSettings
from app.services.screener_repository import ScreenerRepository
from app.services.screener_quality import ScreenerQuality
from app.services.taiwan_trading_calendar import latest_trading_day_on_or_before

NOW=datetime(2026,10,30,7,tzinfo=timezone.utc)


async def post(app,path,body):
    output=[]
    async def receive():
        return dict(type='http.request',body=json.dumps(body).encode(),more_body=False)
    async def send(message): output.append(message)
    await app(dict(type='http',asgi={'version':'3.0'},http_version='1.1',method='POST',scheme='http',
        path=path,raw_path=path.encode(),query_string=b'',headers=[(b'content-type',b'application/json')],
        client=('127.0.0.1',1),server=('isolated',80),root_path=''),receive,send)
    return output[0]['status'],json.loads(b''.join(m.get('body',b'') for m in output[1:]))


def main():
    load_dotenv(ROOT/'.env',override=False)
    settings=MySQLSettings.from_env()
    schema='capital_ai_preview_test_'+uuid.uuid4().hex[:12]
    pattern=r'capital_ai_preview_test_[0-9a-f]{12}'
    assert re.fullmatch(pattern,schema) and schema!=settings.database
    admin=mysql.connector.connect(host=settings.host,port=settings.port,user=settings.user,
        password=settings.password,connection_timeout=8)
    created=False; checks=[]
    try:
        cur=admin.cursor();cur.execute(f'CREATE DATABASE `{schema}` CHARACTER SET utf8mb4');cur.close();created=True
        db=mysql.connector.connect(host=settings.host,port=settings.port,user=settings.user,
            password=settings.password,database=schema,connection_timeout=8)
        try:
            cur=db.cursor()
            cur.execute('CREATE TABLE instruments(market_no SMALLINT,symbol VARCHAR(32),name VARCHAR(100),category_no INT,category_name VARCHAR(100),is_active BOOLEAN,PRIMARY KEY(market_no,symbol))')
            cur.execute('CREATE TABLE candles(symbol VARCHAR(32),interval_code VARCHAR(10),candle_time DATETIME,open_price DOUBLE,high_price DOUBLE,low_price DOUBLE,close_price DOUBLE,volume BIGINT,fetched_at DATETIME,PRIMARY KEY(symbol,interval_code,candle_time))')
            cur.executemany("INSERT INTO instruments VALUES(0,%s,%s,1,'fixture',TRUE)",[(s,s) for s in ('FRESH','STALE','PARTIAL','MISSING')])
            dates=[];day=NOW.date()
            while len(dates)<65:
                day=latest_trading_day_on_or_before(day);dates.append(day);day-=timedelta(days=1)
            rows=[]
            for symbol in ('FRESH','STALE','PARTIAL'):
                for i,day in enumerate(reversed(dates)):
                    if symbol=='STALE' and day==NOW.date(): continue
                    fetched=datetime.combine(day,datetime.min.time())+timedelta(hours=7)
                    if symbol=='PARTIAL' and day==NOW.date(): fetched=fetched.replace(hour=5)
                    rows.append((symbol,'day',datetime.combine(day,datetime.min.time()),100+i,102+i,99+i,101+i,1000+i,fetched))
            # Current complete month/week, plus snapshots written before period close.
            rows.extend([
                ('FRESH','week',datetime(2026,10,26),100,102,99,101,1000,datetime(2026,10,30,7)),
                ('PARTIAL','week',datetime(2026,10,26),100,102,99,101,1000,datetime(2026,10,29,7)),
                ('FRESH','month',datetime(2026,10,1),100,102,99,101,1000,datetime(2026,10,30,7)),
                ('PARTIAL','month',datetime(2026,10,1),100,102,99,101,1000,datetime(2026,10,29,7))])
            cur.executemany('INSERT INTO candles VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',rows);db.commit();cur.close()
            def snapshot():
                db.rollback();q=db.cursor();q.execute('SELECT * FROM candles ORDER BY symbol,interval_code,candle_time');out=q.fetchall();q.close();return out
            before=snapshot()
            target=replace(settings,database=schema)
            with patch.object(ScreenerQuality,'_halt_evidence',return_value={}):
                with ScreenerRepository(target) as repo:
                    for interval in ('week','month'):
                        policy=ScreenerQuality(interval,NOW)
                        bars,metadata=repo.confirmed_candles('FRESH',interval,'candles',20,policy)
                        assert len(bars)==1 and policy.assess(bars,metadata,'FRESH',[0])[0] is None
                        bars,metadata=repo.confirmed_candles('PARTIAL',interval,'candles',20,policy)
                        assert not bars and policy.assess(bars,metadata,'PARTIAL',[0])[0]=='unverified_period'
                    try:
                        repo.cursor.execute("INSERT INTO instruments VALUES(0,'BLOCKED','BLOCKED',1,'fixture',TRUE)")
                        raise AssertionError('Scan transaction unexpectedly allows writes')
                    except mysql.connector.Error as exc:
                        assert exc.errno==1792,exc
                checks.append('Real read-only snapshot rejects writes; weekly/monthly per-row close evidence verified')

                fake=ParseResponse(rule=dict(field='price',operator='>',value=0),assumptions=[],
                    unresolved_conditions=[],original_text='股價高於0',history_interval='day',
                    model='fake',prompt_version='fixture',status='draft',elapsed_ms=0)
                app=FastAPI();app.include_router(router)
                async def run():
                    status,p=await post(app,'/api/v1/ai/rules/preview',dict(text='股價高於0',history_interval='day',settings=dict(universe=dict(markets=[0]))))
                    assert status==200,p
                    body=dict(preview_id=p['preview_id'],revision=p['revision'],text=p['original_text'],settings=p['settings'],confirmed=True,assumptions_confirmed=True)
                    status,result=await post(app,'/api/v1/ai/rules/execute',body)
                    assert status==200,result
                    assert result['complete'] and result['processed_count']==4 and result['matched_count']==1,result
                    assert result['items'][0]['symbol']=='FRESH'
                    states={v['symbol']:v['status'] for v in result['excluded']}
                    assert states==dict(STALE='stale_data',PARTIAL='stale_data',MISSING='missing_data'),states
                    status,duplicate=await post(app,'/api/v1/ai/rules/execute',body)
                    assert status==409,duplicate
                with patch('app.api.ai_rules.parse_rules',return_value=fake), \
                     patch('app.api.ai_rules.ScreenerRepository',side_effect=lambda:ScreenerRepository(target)), \
                     patch('app.api.ai_rules.ScreenerQuality',side_effect=lambda i:ScreenerQuality(i,NOW)), \
                     patch('app.ai.preview_service.ScreenerQuality',side_effect=lambda i:ScreenerQuality(i,NOW)), \
                     patch('app.services.capital_dll_quote_service.CapitalQuoteService._load_wrapper',side_effect=AssertionError('Capital blocked')):
                    asyncio.run(run())
                checks.append('ASGI preview/confirm -> actual MySQL snapshot -> fresh match, stale/partial/missing exclusions -> repeated token rejected')
            assert before==snapshot(),'Scan modified test candle data'
            checks.append('All test candle rows unchanged by scan; production schema never selected')
        finally: db.close()
        print(json.dumps(dict(status='passed',checks=checks),ensure_ascii=False,indent=2))
    finally:
        if created:
            assert re.fullmatch(pattern,schema) and schema!=settings.database
            cur=admin.cursor();cur.execute(f'DROP DATABASE `{schema}`');cur.close()
            print(json.dumps(dict(temporary_schema_removed=schema),ensure_ascii=False))
        admin.close()


if __name__=='__main__': main()
