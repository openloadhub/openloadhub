from fastapi import APIRouter
from app.api.v1 import (
    audit,
    auth,
    notification,
    report,
    script,
    task,
    task_asset,
    websocket,
)
from app.core.config import settings

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(websocket.router, prefix="", tags=["websocket"])
api_router.include_router(task.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(task_asset.router, prefix="", tags=["task-assets"])
api_router.include_router(script.router, prefix="/scripts", tags=["scripts"])
api_router.include_router(report.router, prefix="", tags=["reports"])
if settings.PTP_ENABLE_NOTIFICATIONS:
    api_router.include_router(notification.router, prefix="", tags=["notifications"])
api_router.include_router(audit.router, prefix="", tags=["audit"])
