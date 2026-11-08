from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.services.mysql_instrument_repository import MySQLRepositoryError
from app.services.stock_master_repository import AmbiguousStockProfile, StockMasterRepository
from app.services.stock_master_scheduler import StockMasterSchedule
from app.services.stock_master_sources import CODE_PATTERN

router = APIRouter(prefix="/stocks", tags=["官方股票基本資料"])
StockCode = Annotated[str, Path(pattern="^" + CODE_PATTERN + "$")]


def repository():
    try:
        return StockMasterRepository()
    except MySQLRepositoryError as error:
        raise HTTPException(503, "請設定 MySQL 並先執行官方基本資料更新") from error


def call(action, *args):
    try:
        return action(*args)
    except KeyError as error:
        raise HTTPException(404, "查無已掛牌基本資料；預定掛牌請查 listing-plans") from error
    except AmbiguousStockProfile as error:
        raise HTTPException(409, str(error)) from error
    except MySQLRepositoryError as error:
        raise HTTPException(503, "基本資料尚未初始化或 MySQL 暫時無法讀取") from error


@router.get("/sync-runs", summary="唯讀查看基本資料更新紀錄與週排程設定")
def sync_runs(limit: Annotated[int, Query(ge=1, le=100)] = 10, repo=Depends(repository)):
    config = StockMasterSchedule.from_env()
    return {**call(repo.sync_runs, limit), "schedule": {"enabled": config.enabled, "weekday": config.weekday,
            "time": config.local_time.isoformat(timespec="minutes"), "timezone": "Asia/Taipei", "retry_seconds": config.retry_seconds}}


@router.get("/{code}/profile", summary="查詢官方上市／上櫃日期（不讀取群益 DLL）")
def profile(code: StockCode, market: Literal["TWSE", "TPEx"] | None = None, repo=Depends(repository)):
    return call(repo.profile, code, market)


@router.get("/{code}/history", summary="查看基本資料／市場日期變更歷史")
def history(code: StockCode, limit: Annotated[int, Query(ge=1, le=100)] = 50,
            offset: Annotated[int, Query(ge=0)] = 0, repo=Depends(repository)):
    return call(repo.history, code, limit, offset)


@router.get("/{code}/listing-plans", summary="查看預定掛牌，不當作已掛牌資料")
def listing_plans(code: StockCode, repo=Depends(repository)):
    return call(repo.listing_plans, code)
