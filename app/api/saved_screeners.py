import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.api.screener import run_screener
from app.schemas.saved_screener import SavedScreenerCreate, SavedScreenerUpdate
from app.schemas.screener import ScreenerRequest
from app.services.saved_screener_store import InvalidStoredStrategy, StrategyConflict

router = APIRouter(prefix="/api/v1/screeners", tags=["V3 儲存選股策略"])


def store(request: Request):
    instance = getattr(request.app.state, "saved_screener_store", None)
    if instance is None:
        raise HTTPException(503, "策略儲存服務尚未啟動，請重新啟動 API")
    return instance


def call(action, *args):
    try:
        return action(*args)
    except KeyError as exc:
        raise HTTPException(404, "找不到選股策略") from exc
    except StrategyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except InvalidStoredStrategy as exc:
        raise HTTPException(503, str(exc)) from exc
    except sqlite3.Error as exc:
        raise HTTPException(503, "策略儲存暫時無法使用") from exc


@router.post("", status_code=201, summary="儲存選股策略（不執行選股）")
def create_strategy(payload: SavedScreenerCreate, repository=Depends(store)):
    return call(repository.create, payload)


@router.get("", summary="列出已儲存策略")
def list_strategies(limit: Annotated[int, Query(ge=1, le=100)] = 50, offset: Annotated[int, Query(ge=0)] = 0, repository=Depends(store)):
    return call(repository.list, limit, offset)


@router.get("/{strategy_id}", summary="查看完整策略與版本")
def get_strategy(strategy_id: str, repository=Depends(store)):
    return call(repository.get, strategy_id)


@router.put("/{strategy_id}", summary="更新完整策略（需目前 revision）")
def update_strategy(strategy_id: str, payload: SavedScreenerUpdate, repository=Depends(store)):
    return call(repository.update, strategy_id, payload)


@router.delete("/{strategy_id}", status_code=204, summary="刪除已儲存策略")
def delete_strategy(strategy_id: str, repository=Depends(store)):
    call(repository.delete, strategy_id)
    return Response(status_code=204)


@router.post("/{strategy_id}/run", summary="以已儲存策略執行選股", description="不需 body，使用執行開始時的策略版本與當前 MySQL 資料；自選股策略使用當前自選股。結果回傳策略快照。")
def run_strategy(strategy_id: str, request: Request, repository=Depends(store)):
    snapshot = call(repository.get, strategy_id)
    result = run_screener(ScreenerRequest.model_validate(snapshot["configuration"]), request)
    return {"saved_screener": snapshot, **result}
