from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import Field

from app.api.alerts import service, call
from app.rules.schemas import StrictModel
from app.services.notification_service import NotificationService

router = APIRouter(prefix="/api/v1/notifications", tags=["V3 通知系統"])


def notifications(alerts=Depends(service)):
    return NotificationService(alerts)


class ReadRequest(StrictModel):
    read: bool = Field(strict=True)


class ReadAllRequest(StrictModel):
    through_sequence: int = Field(ge=1, strict=True)


class DesktopAck(StrictModel):
    claim_token: str = Field(min_length=1, max_length=64)
    status: Literal["submitted", "failed"]
    error: str | None = Field(default=None, max_length=500)


@router.get("", summary="通知歷史、未讀數與分頁")
def list_notifications(unread_only: bool = False, before: Annotated[int | None, Query(ge=1)] = None, limit: Annotated[int, Query(ge=1, le=100)] = 50, store=Depends(notifications)):
    return call(store.list, unread_only, before, limit)


@router.post("/read-all", summary="將指定序號以前的通知標為已讀")
def read_all(payload: ReadAllRequest, store=Depends(notifications)):
    return call(store.mark_all, payload.through_sequence)


@router.post("/test", status_code=201, summary="建立一則明確標示的測試通知")
def test_notification(store=Depends(notifications)):
    return call(store.create_test)


@router.patch("/{notification_id}/read", summary="切換已讀／未讀")
def mark_read(notification_id: str, payload: ReadRequest, store=Depends(notifications)):
    return call(store.mark, notification_id, payload.read)


@router.post("/desktop/claim", summary="取得一次系統通知派送權", description="僅在瀏覽器已獲授權時呼叫。每則最多領取一次，僅領取五分鐘內未讀通知。")
def claim(store=Depends(notifications)):
    return call(store.claim)


@router.post("/{notification_id}/desktop-ack", summary="回報瀏覽器通知提交結果")
def acknowledge(notification_id: str, payload: DesktopAck, store=Depends(notifications)):
    return call(store.acknowledge, notification_id, payload.claim_token, payload.status, payload.error)
