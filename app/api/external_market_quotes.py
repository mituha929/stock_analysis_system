from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
from app.services.external_market_quote_repository import ExternalMarketQuoteRepository

router = APIRouter(prefix="/api/v1/external-market-quotes", tags=["外部市場報價"])


@router.get("/us-stocks/schedule", summary="查看15檔美股收盤日K更新排程、逐檔結果及下次時間")
def us_stock_schedule(request: Request):
    return request.app.state.us_stock_daily_scheduler.status()


@router.get("/schedule", summary="查看台指期夜盤與美股指數自動更新排程及最近紀錄")
def schedule(request: Request):
    return request.app.state.external_market_quote_scheduler.status()


@router.get("/latest", summary="查看各商品最新快照、最後檢查結果與目前報價新鮮度")
def latest(request: Request):
    scheduler = request.app.state.external_market_quote_scheduler
    try:
        items = ExternalMarketQuoteRepository().latest()
    except Exception as error:
        raise HTTPException(503, "外部市場報價暫時無法讀取，既有資料保留") from error
    now = datetime.now(timezone.utc)
    for item in items:
        snapshot = item["snapshot"]
        try:
            plan = scheduler._plan((item["source"], item["symbol"], item["session"]), now)
            if snapshot and plan:
                quote_at = snapshot["quote_at_utc"]
                item["current_quality"] = scheduler._quality(quote_at.replace(tzinfo=timezone.utc) if quote_at else None, now, plan)
        except (OSError, ValueError, KeyError, TypeError):
            item["current_quality"] = {"freshness": "calendar_unverified", "usable_for_expected_session": False}
        for row in (snapshot, item["last_check"]):
            if row:
                for key, value in row.items():
                    if isinstance(value, datetime):
                        row[key] = value.replace(tzinfo=timezone.utc)
    return dict(generated_at=now, items=items,
                note="檢查時間不是成交時間；時間未知不視為最新行情。這些資料是報價快照，不是30分鐘K線。")
