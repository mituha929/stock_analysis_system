import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.schemas.alerts import AlertCreate, AlertEnabled

router = APIRouter(prefix="/api/v1/alerts", tags=["V3 提醒引擎"])


def service(request: Request):
    instance = getattr(request.app.state, "alert_service", None)
    if instance is None:
        raise HTTPException(503, "提醒引擎未啟動")
    return instance


def call(action, *args, **kwargs):
    try:
        return action(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, "找不到提醒") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except sqlite3.Error as exc:
        raise HTTPException(503, "提醒儲存暫時無法使用") from exc


@router.get("", summary="列出提醒設定與狀態")
def list_alerts(alerts=Depends(service)):
    return call(alerts.list)


@router.post("", status_code=201, summary="建立持續監控提醒", description="首次檢查只建立基準；之後 False→True 才觸發。quote 模式需先登入並訂閱自選股。")
def create_alert(payload: AlertCreate, alerts=Depends(service)):
    return call(alerts.create, payload)


@router.get("/events", summary="查詢觸發歷史（通知已讀狀態請用 notifications API）")
def events(alert_id: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100, alerts=Depends(service)):
    return call(alerts.events, alert_id, limit)


@router.get("/{alert_id}", summary="查看提醒詳細狀態")
def get_alert(alert_id: str, alerts=Depends(service)):
    return call(alerts.get, alert_id)


@router.put("/{alert_id}", summary="更新完整提醒設定並重建基準")
def update_alert(alert_id: str, payload: AlertCreate, alerts=Depends(service)):
    return call(alerts.change, alert_id, payload=payload)


@router.patch("/{alert_id}/enabled", summary="啟用或停用提醒")
def enable_alert(alert_id: str, payload: AlertEnabled, alerts=Depends(service)):
    return call(alerts.change, alert_id, enabled=payload.enabled)


@router.post("/{alert_id}/reset", summary="清除判斷基準（保留冷卻期限及歷史）")
def reset_alert(alert_id: str, alerts=Depends(service)):
    return call(alerts.change, alert_id, reset=True)


@router.post("/{alert_id}/check", summary="立即檢查一次並更新狀態", description="會保存狀態及可能的觸發紀錄；不同於唯讀 rules/test。不略過停用或冷卻限制。")
def check_alert(alert_id: str, alerts=Depends(service)):
    return call(alerts.check, alert_id, force=True)


@router.delete("/{alert_id}", status_code=204, summary="刪除提醒（保留觸發歷史）")
def delete_alert(alert_id: str, alerts=Depends(service)):
    call(alerts.delete, alert_id)
    return Response(status_code=204)
