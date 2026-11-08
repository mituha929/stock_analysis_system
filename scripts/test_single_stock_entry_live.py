"""Isolated ASGI, real read-only MySQL + local Ollama, no Capital lifespan."""
import argparse
import asyncio
from dataclasses import replace
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
import time
from urllib.request import build_opener,ProxyHandler

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


async def post(app,path,body):
    messages=[]
    async def receive(): return dict(type="http.request",body=json.dumps(body).encode("utf-8"),more_body=False)
    async def send(message): messages.append(message)
    await app(dict(type="http",asgi={"version":"3.0"},http_version="1.1",method="POST",scheme="http",
        path=path,raw_path=path.encode(),query_string=b"",headers=[(b"content-type",b"application/json")],
        client=("127.0.0.1",1),server=("entry-test",80),root_path=""),receive,send)
    return messages[0]["status"],json.loads(b"".join(m.get("body",b"") for m in messages[1:]))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--allow-live-read",action="store_true")
    parser.add_argument("--intent",choices=("buy","sell","add","reduce"),default="buy")
    args=parser.parse_args()
    if not args.allow_live_read:
        parser.error("Use --allow-live-read to confirm local model and read-only market-data testing")
    from dotenv import load_dotenv
    load_dotenv(ROOT/".env")
    from fastapi import FastAPI
    from app.api.ai_rules import router
    from app.ai.ollama_client import OllamaClient,OllamaConfig
    config=OllamaConfig.from_env()
    if config.keep_alive!="0":
        raise RuntimeError("Set OLLAMA_RULE_KEEP_ALIVE=0 before verifying unload behavior")
    if config.base_url not in {"http://127.0.0.1:11434","http://localhost:11434"}:
        raise RuntimeError("This acceptance script only targets a local Ollama service")
    opener=build_opener(ProxyHandler({}))
    def loaded():
        with opener.open(config.base_url+"/api/ps",timeout=10) as response:
            return [dict(name=m["name"],size=m.get("size"),size_vram=m.get("size_vram"))
                    for m in json.load(response).get("models",[]) if m["name"]==config.model]
    # Controlled warmup makes the allocation/unload measurable. No model pull.
    warm=OllamaClient(replace(config,keep_alive="1m"))
    warm.chat([dict(role="user",content="Return JSON with ok=true.")],
        dict(type="object",properties=dict(ok=dict(type="boolean")),required=["ok"],additionalProperties=False))
    before=loaded()
    app=FastAPI();app.include_router(router)
    async def run():
        actions={"buy":("買進","single_stock_entry"),"sell":("賣出","single_stock_exit"),
                 "add":("加碼","single_stock_add"),"reduce":("減碼","single_stock_reduce")}
        word,intent=actions[args.intent]
        text=f"我想要確認2454是否可以{word}"
        status,p=await post(app,"/api/v1/ai/rules/preview",dict(text=text))
        assert status==200 and p["intent"]==intent and p["preview_id"],p
        status,result=await post(app,"/api/v1/ai/rules/execute",dict(preview_id=p["preview_id"],revision=p["revision"],
            text=p["original_text"],settings=p["settings"],confirmed=True,assumptions_confirmed=True))
        assert status==200 and result["symbol"]=="2454" and result["intent"]==intent,result
        return result
    try:
        result=asyncio.run(run())
        after=loaded()
        for _ in range(10):
            if not after: break
            time.sleep(1);after=loaded()
        report=dict(checked_at=datetime.now(timezone.utc).isoformat(),test_only=True,
            result=result,model_before=before,model_after=after,model_unloaded=not after,
            database_read_only=True,capital_used=False,production_api_restarted=False)
        path=ROOT/"output"/"diagnostics"/("single-stock-entry-"+datetime.now().strftime("%Y%m%d-%H%M%S")+".json")
        path.write_text(json.dumps(report,ensure_ascii=False,default=str,indent=2),encoding="utf-8")
        print(json.dumps(dict(report=str(path),symbol=result["symbol"],intent=result["intent"],data_status=result["data_status"],
            ai_status=result["ai"]["status"],model_before=before,model_after=after,model_unloaded=not after,
            database_read_only=True,capital_used=False),ensure_ascii=False,indent=2),flush=True)
        assert result["data_status"]=="available" and result["ai"]["status"]=="explained", "See diagnostic report"
        assert not after,"Model remains loaded; see diagnostic report"
    finally:
        # Also release the deliberate warmup after a failed DB/model test.
        from urllib.request import Request
        try:
            request=Request(config.base_url+"/api/generate",data=json.dumps(dict(model=config.model,keep_alive=0)).encode(),
                            headers={"Content-Type":"application/json"},method="POST")
            with opener.open(request,timeout=15) as response: response.read()
        except Exception:
            pass


if __name__=="__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
