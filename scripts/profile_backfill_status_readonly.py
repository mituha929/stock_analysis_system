"""Time the new dashboard SQL against existing data: SELECT only, no DLL."""
import json
from pathlib import Path
import sys
import time
import asyncio
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from dotenv import load_dotenv
load_dotenv(PROJECT/".env")
from app.api.routes import router
from app.services.backfill_status_cache import BackfillStatusCache
from fastapi import FastAPI
from app.services.mysql_instrument_repository import MySQLInstrumentRepository


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    queries=[]
    connections=[]
    original=MySQLInstrumentRepository._connect_database
    class Cursor:
        def __init__(self,cursor): self.cursor=cursor
        def execute(self,sql,*args,**kwargs):
            if not sql.lstrip().upper().startswith(("SELECT ","WITH ")):
                raise AssertionError("Read-only status profiler rejected a non-SELECT query")
            begin=time.monotonic()
            result=self.cursor.execute(sql,*args,**kwargs)
            queries.append(dict(query=sql[:110],seconds=round(time.monotonic()-begin,4)))
            return result
        def __getattr__(self,name): return getattr(self.cursor,name)
    class Connection:
        def __init__(self,connection): self.connection=connection
        def cursor(self,*args,**kwargs): return Cursor(self.connection.cursor(*args,**kwargs))
        def __getattr__(self,name): return getattr(self.connection,name)
    def connect(repository,connector):
        begin=time.monotonic()
        connection=original(repository,connector)
        connections.append(round(time.monotonic()-begin,4))
        return Connection(connection)
    app=FastAPI(); app.include_router(router)
    app.state.backfill_status_cache=BackfillStatusCache()
    async def request():
        messages=[]
        async def receive(): return {"type":"http.request","body":b"","more_body":False}
        async def send(message): messages.append(message)
        scope=dict(type="http",asgi={"version":"3.0"},http_version="1.1",method="GET",
            path="/api/v1/backfill/status",raw_path=b"/api/v1/backfill/status",query_string=b"markets=all&intervals=all",
            headers=[],scheme="http",server=("test",80),client=("test",123),root_path="")
        await app(scope,receive,send)
        body=b"".join(m.get("body",b"") for m in messages if m["type"]=="http.response.body")
        return json.loads(body)
    begin=time.monotonic()
    with patch.object(MySQLInstrumentRepository,"_connect_database",connect),patch("app.api.routes.backfill_control_status",return_value={"control_version":2}):
        first=asyncio.run(request())
        first_seconds=round(time.monotonic()-begin,4)
        print(json.dumps(dict(first_response_seconds=first_seconds,refreshing=first["database_refreshing"],items=len(first["items"])),ensure_ascii=False),flush=True)
        app.state.backfill_status_cache.pending.join()
        ready_begin=time.monotonic()
        ready=asyncio.run(request())
        cached_seconds=round(time.monotonic()-ready_begin,4)
    print(json.dumps(dict(total_seconds=round(time.monotonic()-begin,4),items=len(ready["items"]),logs=len(ready["schedule_logs"]),
        first_response_seconds=first_seconds,cached_response_seconds=cached_seconds,
        snapshot_database_seconds=ready["database_query_seconds"],isolated_asgi_no_server_restart=True,
        query_count=len(queries),connection_count=len(connections),connection_seconds=sum(connections),
        slowest_queries=sorted(queries,key=lambda q:q["seconds"],reverse=True)[:10],
        read_only=True,capital_dll_used=False),ensure_ascii=False,indent=2))


if __name__=="__main__": main()
