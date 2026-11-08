"""Local browser QA only: fake Ollama/market rows; never production lifespan.

Stop with Ctrl+C or POST /__test__/shutdown. Listen on loopback only.
"""
import argparse
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from app.api.ai_rules import router
from app.ai.schemas import ParseResponse
from app.rules.fields import field_metadata
from app.services.screener_quality import ScreenerQuality
from app.services.taiwan_trading_calendar import latest_trading_day_on_or_before
from app.schemas.models import Candle


def fake_parse(payload):
    rules=[dict(field="price",operator=">",value={"field":"ma20"}),dict(field="rsi14",operator="<=",value=70)]
    assumptions=[]
    if "比較強" in payload.text:
        assumptions=[dict(source_text="比較強",interpreted_as="技術方向分數至少70（示意）")]
    return ParseResponse(rule=dict(logic="AND",rules=rules),assumptions=assumptions,unresolved_conditions=[],
        original_text=payload.text,history_interval=payload.history_interval,model="FIXTURE_ONLY_NO_OLLAMA",
        prompt_version="fixture",status="draft",elapsed_ms=1)


class FixtureRepository:
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def instruments(self,markets,categories,table,symbols=None):
        return [dict(market_no=markets[0],symbol=s,name=name,category_no=1,category_name="隔離測試")
            for s,name in (("TEST01","示意公司甲"),("TEST02","示意公司乙"),("TEST03","缺資料示意"))
            if symbols is None or s in symbols]
    def confirmed_candles(self,symbol,interval,table,limit,policy):
        if symbol=="TEST03": return [],dict(raw_count=0,latest_written_at=None)
        end=policy.end-timedelta(days=10 if symbol=="TEST02" else 0)
        dates=[]
        while len(dates)<65:
            end=latest_trading_day_on_or_before(end);dates.append(end);end-=timedelta(days=1)
        rows=[Candle(timestamp=datetime.combine(d,datetime.min.time()),open=100+i,high=103+i,low=99+i,
            close=101+i,volume=1000+i*10) for i,d in enumerate(reversed(dates))]
        # Oscillation keeps RSI away from 100 for a matching fixture example.
        rows[-1]=rows[-1].model_copy(update={"close":rows[-2].close-1})
        return rows[-limit:],dict(raw_count=len(rows),latest_written_at=datetime.now(timezone.utc))


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--port",type=int,default=8766);args=parser.parse_args()
    app=FastAPI(title="ISOLATED V3 AI UI FIXTURE")
    app.state.capital_service=SimpleNamespace(watchlist=lambda:["TEST01"])
    app.include_router(router)
    app.mount("/static",StaticFiles(directory=ROOT/"app/static"),name="static")
    @app.get("/v3",response_class=HTMLResponse)
    def page():
        source=(ROOT/"app/static/v3.html").read_text(encoding="utf-8")
        return source.replace('<main>','<main><p role="alert" style="background:#fff0ad;padding:12px">隔離測試：所有模型回答、股票及價格均為模擬，不連線群益或正式資料庫。</p>')
    @app.get("/api/v1/health")
    def health(): return {"status":"ok","fixture":True}
    @app.get("/api/v1/rules/fields")
    def fields(): return {"fields":field_metadata()}
    @app.get("/api/v1/alerts")
    def alerts(): return {"items":[]}
    @app.get("/api/v1/notifications")
    def notifications(): return {"items":[],"unread_count":0,"next_before":None}
    @app.get("/api/v1/screeners")
    def screeners(): return {"items":[],"total":0}
    config=uvicorn.Config(app,host="127.0.0.1",port=args.port,log_level="warning")
    server=uvicorn.Server(config)
    @app.post("/__test__/shutdown")
    def stop(): server.should_exit=True;return {"stopped_fixture":True}
    with patch("app.api.ai_rules.parse_rules",side_effect=fake_parse), \
         patch("app.api.ai_rules.ScreenerRepository",FixtureRepository), \
         patch("app.services.mysql_instrument_repository.MySQLInstrumentRepository._connect_database",side_effect=AssertionError("Fixture blocked real DB")), \
         patch("app.services.capital_dll_quote_service.CapitalQuoteService._load_wrapper",side_effect=AssertionError("Fixture blocked DLL")):
        print(f"ISOLATED fixture: http://127.0.0.1:{args.port}/v3#ai",flush=True)
        server.run()


if __name__=="__main__": main()
