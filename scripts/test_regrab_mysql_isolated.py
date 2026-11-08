"""Full regrab orchestration in a disposable schema; fake vendor, no DLL.

Never falls back to production if schema creation is denied. Only the random
schema created by this invocation may be removed by the finally block.
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import uuid
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
sys.path.insert(0,str(PROJECT/"tests"))
from dotenv import load_dotenv
import mysql.connector
from fastapi import FastAPI
from app.api.routes import router
from app.schemas.models import Candle
from app.services.backfill_control import BackfillControl
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLSettings
from app.services.trading_suspensions import SuspensionCalendar
from test_backfill_api_control import asgi_request


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(PROJECT/".env")
    settings=MySQLSettings.from_env()
    schema="capital_regrab_test_"+uuid.uuid4().hex[:12]
    assert re.fullmatch(r"capital_regrab_test_[0-9a-f]{12}",schema)
    admin=mysql.connector.connect(host=settings.host,port=settings.port,user=settings.user,
        password=settings.password,connection_timeout=10)
    created=False
    try:
        cur=admin.cursor()
        cur.execute(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        admin.commit(); created=True; cur.close()
        repo=MySQLCandleRepository(replace(settings,database=schema))
        for candles,jobs,instruments,markets in (
            ("candles","candle_backfill_jobs","instruments",[0,1,4]),
            ("odd_lot_candle","odd_lot_candle_backfill_jobs","odd_lot_instruments",[5,6]),
        ):
            repo.ensure_batch_storage(candles,jobs)
            with repo.transaction() as cur:
                cur.execute(f"CREATE TABLE `{instruments}` (market_no SMALLINT NOT NULL,symbol VARCHAR(32) NOT NULL,category_no INT NOT NULL,is_active BOOLEAN NOT NULL,PRIMARY KEY(market_no,symbol)) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                cur.executemany(f"INSERT INTO `{instruments}` VALUES(%s,%s,1,TRUE)",[(m,f"TEST{m}") for m in markets])
        old=Candle(timestamp=datetime(2000,1,3,tzinfo=timezone.utc),open=10,high=12,low=9,close=11,volume=10)
        repo.upsert_candles("TEST0","day",[old],"candles")
        calls=[]
        def fetch(symbol,interval,start,end,**kwargs):
            assert control.worker_active(),"worker lease lost during fetch"
            assert control.snapshot()["preparation_done"]==35,"fetch before full preparation"
            calls.append((symbol,interval,start,end))
            # Valid sparse response; insufficient history is expected, not a
            # simulated promise of the requested 1200/560/100 vendor bars.
            from app.services.backfill_policy import period_start
            label=period_start(interval,end)
            if label < start: return []
            return [Candle(timestamp=datetime.combine(label,datetime.min.time(),tzinfo=timezone.utc),
                open=10,high=12,low=9,close=11,volume=100)]
        app=FastAPI(); app.include_router(router)
        app.state.capital_service=SimpleNamespace(quote_ready=True,cache=None,fetch_historical_candles=fetch)
        with TemporaryDirectory(prefix="regrab-mysql-") as folder,patch.dict(os.environ,{
            "CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3"),
            "CAPITAL_SUSPENSION_SYNC_ENABLED":"false",
            "CAPITAL_DAY_INTEGRITY_ENABLED":"false",
            "CAPITAL_BACKFILL_JOB_WINDOW_BUDGET":"100",
            "CAPITAL_BACKFILL_RECOVERY_WINDOW_BUDGET":"1000",
        }),patch("app.api.routes.MySQLCandleRepository",return_value=repo), \
             patch("app.services.backfill_engine.load_suspensions",return_value=SuspensionCalendar()):
            control=BackfillControl(); control.update(state="paused",pause_requested=True)
            status,body=asyncio.run(asgi_request(app,"POST","/api/v1/backfill/regrab-all"))
            state=control.snapshot()
            assert status==202,body
            assert state["preparation_complete"] and state["preparation_done"]==35,state
            assert state["state"]=="completed",state
            assert state["processed"]==35 and state["failed"]==0,state
            assert len(state["plans"])==35
            from app.services.backfill_targets import configured_targets
            assert {p["interval"]:p["target_bars"] for p in state["plans"]}==configured_targets()
            assert len({(c[0],c[1]) for c in calls})==35
            assert all(c[3]<=datetime.now().date() for c in calls)
            assert not control.worker_active()
            with repo.transaction() as cur:
                cur.execute("SELECT COUNT(*) FROM candles WHERE symbol='TEST0' AND interval_code='day' AND candle_time='2000-01-03'")
                assert cur.fetchone()[0]==1,"pre-existing candle removed"
                for table in ("candles","odd_lot_candle"):
                    cur.execute(f"SELECT COUNT(*) FROM (SELECT symbol,interval_code,candle_time FROM `{table}` GROUP BY symbol,interval_code,candle_time HAVING COUNT(*)>1) d")
                    assert cur.fetchone()[0]==0,"duplicate candles"
            # Also exercise the original response-model/storage-summary path
            # which previously returned 500 on the live server.
            for group in ("board-lot","odd-lot"):
                status,body=asyncio.run(asgi_request(app,"POST",f"/api/v1/{group}/backfill/prepare?interval=1m&force=true"))
                assert status==200 and body["plan_saved"] and body["force"],body
            print(json.dumps(dict(passed=9,groups=35,processed=state["processed"],
                fake_vendor_requests=len(calls),checks=["35 groups prepared before fetch","same OS lease through fetch",
                "weekend manual start","per-period targets retained","existing candle retained",
                "no duplicate keys","lease released","legacy board prepare response","legacy odd-lot prepare response"],production_database_modified=False,capital_dll_used=False),ensure_ascii=False))
    finally:
        if created:
            cur=admin.cursor(); cur.execute(f"DROP DATABASE `{schema}`"); admin.commit(); cur.close()
            print("已清除本次建立的完整重抓隔離測試資料庫。")
        admin.close()


if __name__=="__main__":
    main()
