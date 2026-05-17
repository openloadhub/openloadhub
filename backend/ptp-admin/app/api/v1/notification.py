from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import ActorPrincipal, get_db
from app.core.permissions import require_permission
from app.schemas.notification import (
    WebhookConfigCreateRequest,
    WebhookConfigResponse,
    WebhookConfigUpdateRequest,
    WebhookSendRecordResponse,
    WebhookSendRequest,
    WebhookSendResponse,
    WebhookTemplatePreviewRequest,
    WebhookTemplatePreviewResponse,
)
from app.schemas.response import ApiResponse
from app.services.notification_service import NotificationService

router = APIRouter()


@router.post(
    "/notifications/webhook/configs",
    response_model=ApiResponse[WebhookConfigResponse],
    response_model_by_alias=True,
)
def create_webhook_config(
    payload: WebhookConfigCreateRequest,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("notification", "manage")),
):
    """保存 Webhook 配置，供 PlanRun 终态事件自动触发。"""
    return ApiResponse.success(
        NotificationService.create_webhook_config(
            db,
            payload,
            user_id=actor.user_id,
        )
    )


@router.get(
    "/notifications/webhook/configs",
    response_model=ApiResponse[list[WebhookConfigResponse]],
    response_model_by_alias=True,
)
def list_webhook_configs(
    enabled: bool | None = Query(None),
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("notification", "manage")),
):
    """查询 Webhook 配置列表，返回脱敏 URL。"""
    del actor
    return ApiResponse.success(
        NotificationService.list_webhook_configs(db, enabled=enabled)
    )


@router.put(
    "/notifications/webhook/configs/{config_id}",
    response_model=ApiResponse[WebhookConfigResponse],
    response_model_by_alias=True,
)
def update_webhook_config(
    config_id: int,
    payload: WebhookConfigUpdateRequest,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("notification", "manage")),
):
    """更新 Webhook 配置。"""
    del actor
    config = NotificationService.update_webhook_config(db, config_id, payload)
    if config is None:
        raise HTTPException(status_code=404, detail="Webhook config not found")
    return ApiResponse.success(config)


@router.post(
    "/notifications/webhook/preview",
    response_model=ApiResponse[WebhookTemplatePreviewResponse],
    response_model_by_alias=True,
)
def preview_webhook_notification(
    payload: WebhookTemplatePreviewRequest,
    actor: ActorPrincipal = Depends(require_permission("notification", "manage")),
):
    """预览 Webhook 通知模板与目标平台 payload，不发送外部请求。"""
    del actor
    return ApiResponse.success(NotificationService.preview_webhook_template(payload))


@router.post(
    "/notifications/webhook/send",
    response_model=ApiResponse[WebhookSendResponse],
    response_model_by_alias=True,
)
def send_webhook_notification(
    payload: WebhookSendRequest,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("notification", "manage")),
):
    """发送 Webhook 通知并记录结果。"""
    return ApiResponse.success(
        NotificationService.send_webhook(db, payload, user_id=actor.user_id)
    )


@router.get(
    "/notifications/webhook/records",
    response_model=ApiResponse[list[WebhookSendRecordResponse]],
    response_model_by_alias=True,
)
def list_webhook_notification_records(
    limit: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("notification", "manage")),
):
    """查询 Webhook 发送记录。"""
    del actor
    return ApiResponse.success(
        NotificationService.list_webhook_records(db, limit=limit, status=status)
    )
