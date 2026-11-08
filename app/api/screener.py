from threading import Lock

from fastapi import APIRouter, HTTPException, Request

from app.schemas.screener import ScreenerRequest
from app.services.screener_repository import ScreenerRepository
from app.services.screener_service import screen
from app.services.mysql_instrument_repository import MySQLRepositoryError

router = APIRouter(prefix="/api/v1/screener", tags=["V3 選股與排序"])
scan_lock = Lock()


@router.post("/run", summary="執行股票範圍預篩、規則判斷與排序", description="唯讀 MySQL，同一 API 行程一次一個掃描。預設上市櫃、日K、最新360根；結果限制在完整排序之後套用。檢查 complete 判斷是否掃描完成。")
def run_screener(payload: ScreenerRequest, request: Request):
    if not scan_lock.acquire(blocking=False):
        raise HTTPException(409, "已有選股工作執行中，請稍後重試")
    try:
        watchlist = None
        if payload.universe.kind == "watchlist":
            capital = getattr(request.app.state, "capital_service", None)
            if capital is None:
                raise HTTPException(503, "自選股服務尚未啟動")
            watchlist = capital.watchlist()
        with ScreenerRepository() as repository:
            return screen(payload, repository, watchlist)
    except MySQLRepositoryError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        scan_lock.release()


@router.get("/categories", summary="查詢已同步的市場／分類選項")
def categories():
    try:
        with ScreenerRepository() as repository:
            rows = repository.instruments([0, 1, 4], [], "candles")
        groups = {}
        for row in rows:
            key = (row["market_no"], row["category_no"], row["category_name"])
            groups[key] = groups.get(key, 0) + 1
        return {"items": [{"market_no": k[0], "category_no": k[1], "category_name": k[2], "instrument_count": count} for k, count in sorted(groups.items())], "note": "群益商品分類，不保證等同產業；此選項清單取自整張商品主檔。"}
    except MySQLRepositoryError as exc:
        raise HTTPException(503, str(exc)) from exc
