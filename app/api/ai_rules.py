from threading import Lock

from fastapi import APIRouter, HTTPException, Request

from app.ai.ollama_client import AIError, OllamaClient, OllamaConfig
from app.ai.rule_parser import parse_rule
from app.ai.schemas import ParseRequest, ParseResponse, ValidateRequest, ValidateResponse
from app.ai.validation import validated_rule
from app.ai.preview_schemas import PreviewRequest, ReviseRequest, ExecuteRequest, InvalidateRequest
from app.ai.preview_store import preview_store
from app.ai.preview_service import make_preview, revise_preview, confirmed_data
from app.ai.single_stock import STOCK_INTENTS, make_entry_preview, analyze_entry
from app.ai.rule_preview import input_issues
from app.api.screener import scan_lock
from app.services.screener_repository import ScreenerRepository
from app.services.screener_service import screen
from app.services.screener_quality import ScreenerQuality
from app.services.mysql_instrument_repository import MySQLRepositoryError

router = APIRouter(prefix="/api/v1/ai/rules")
parse_lock = Lock()


@router.post("/parse", response_model=ParseResponse, tags=["V3.8 自然語言規則解析"], summary="將自然語言解析為待確認的規則草稿",
             description="僅呼叫設定的 Ollama，驗證現有欄位、型態及規則深度。不執行選股、不讀寫資料庫、不呼叫群益。語意仍須人工確認；未解決條件回傳 rule=null。")
def parse_rules(payload: ParseRequest):
    if not parse_lock.acquire(blocking=False):
        raise HTTPException(409, {"code": "ai_busy", "message": "已有條件解析正在進行，請稍後重試。"})
    try:
        return parse_rule(payload, OllamaClient(OllamaConfig.from_env()))
    except AIError as exc:
        raise HTTPException(exc.status, exc.detail()) from exc
    finally:
        parse_lock.release()


@router.post("/validate", response_model=ValidateResponse, tags=["V3.9 規則驗證"],
             summary="驗證規則草稿，不呼叫模型或執行選股",
             description="檢查欄位、運算子、型態、數值範圍與 AND/OR 結構。錯誤以 errors[].path 的 JSON Pointer 定位；通過不代表語意正確，仍須人工確認。")
def validate_rules(payload: ValidateRequest):
    try:
        return ValidateResponse(rule=validated_rule(payload.rule))
    except AIError as exc:
        raise HTTPException(exc.status, exc.detail()) from exc


@router.post("/preview", tags=["V3.10 AI 條件預覽"],summary="產生白話規則及待確認版本，不執行選股")
def preview_rules(payload: PreviewRequest, request: Request):
    if payload.replaces: preview_store.invalidate(payload.replaces)
    entry=make_entry_preview(payload)
    if entry is not None:
        return entry
    issues=input_issues(payload.text,payload.settings)
    watchlist=None
    if payload.settings.universe.kind=="watchlist":
        capital=getattr(request.app.state,"capital_service",None)
        if capital is None:
            issues.append("自選股服務尚未啟動；請選市場或指定股票範圍。")
        else:
            # Local list only. This does not login/subscribe or invoke the DLL.
            watchlist=capital.watchlist()
    parsed=None if issues or watchlist==[] else parse_rules(ParseRequest(text=payload.text,history_interval=payload.history_interval))
    try:
        return make_preview(payload.text,payload.settings,parsed,issues,watchlist)
    except AIError as exc:
        raise HTTPException(exc.status,exc.detail()) from exc


@router.post("/preview/revise",tags=["V3.10 AI 條件預覽"],summary="修改已有草稿，產生新預覽並使舊版本失效")
def revise_rules(payload: ReviseRequest):
    try: return revise_preview(payload)
    except AIError as exc: raise HTTPException(exc.status,exc.detail()) from exc


@router.post("/preview/invalidate",tags=["V3.10 AI 條件預覽"],summary="修改輸入後使舊預覽失效")
def invalidate_rules(payload: InvalidateRequest):
    if payload.editable: preview_store.revoke_for_edit(payload.preview_id)
    else: preview_store.invalidate(payload.preview_id)
    return {"invalidated":True}


@router.post("/execute",tags=["V3.11 確認選股"],summary="執行已確認規則或單股分析，唯讀 MySQL")
def execute_rules(payload: ExecuteRequest):
    if not scan_lock.acquire(blocking=False):
        raise HTTPException(409,{"code":"scan_busy","message":"已有選股執行中，請稍後再試；預覽尚未消耗。"})
    owns_parse=False
    try:
        preview=preview_store.read(payload.preview_id,payload.revision)
        if preview.get("intent") in STOCK_INTENTS:
            if not parse_lock.acquire(blocking=False):
                raise AIError("scan_busy","已有 AI 解析進行中，請稍後再試；預覽尚未消耗。",409)
            owns_parse=True
            return analyze_entry(payload,OllamaClient(OllamaConfig.from_env()),ScreenerRepository)
        data,config=confirmed_data(payload)
        policy=ScreenerQuality(config.history_interval)
        with ScreenerRepository() as repository:
            result=screen(config,repository,data["resolved_watchlist"],quality_policy=policy)
        return {**result,"confirmed_preview":dict(revision=payload.revision,
            original_text=data["original_text"],configuration=config.model_dump(mode="json"),
            resolved_watchlist=data["resolved_watchlist"],assumptions=data["assumptions"])}
    except AIError as exc:
        raise HTTPException(exc.status,exc.detail()) from exc
    except MySQLRepositoryError as exc:
        raise HTTPException(503,{"code":"database_unavailable","message":str(exc)+"；本次預覽已消耗，重試前請重新預覽及確認。"}) from exc
    except ValueError as exc:
        raise HTTPException(422,{"code":"invalid_execution","message":str(exc)}) from exc
    finally:
        if owns_parse: parse_lock.release()
        scan_lock.release()
