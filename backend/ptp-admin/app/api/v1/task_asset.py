from __future__ import annotations

import io
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import ActorPrincipal, get_db
from app.core.permissions import require_permission
from app.schemas.response import ApiResponse
from app.schemas.task_asset import (
    TaskAssetBindRequest,
    TaskAssetDirectUploadCreateRequest,
    TaskAssetDirectUploadFinalizeRequest,
    TaskAssetDirectUploadSessionResponse,
    TaskAssetResponse,
)
from app.services.task_asset_service import TaskAssetService

router = APIRouter()


@router.post(
    "/task-assets/upload",
    response_model=ApiResponse[TaskAssetResponse],
    response_model_by_alias=True,
)
def upload_task_asset(
    category: str = Query(..., description="附件分类：proto/data"),
    task_id: int | None = Query(None, ge=1),
    shard_count: int | None = Query(None, ge=1),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("task", "create")),
):
    service = TaskAssetService(db)
    try:
        asset = service.upload_asset(
            file=file,
            category=category,
            user_id=actor.user_id,
            task_id=task_id,
            shard_count=shard_count,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse.success(asset)


@router.post(
    "/task-assets/direct-upload-sessions",
    response_model=ApiResponse[TaskAssetDirectUploadSessionResponse],
    response_model_by_alias=True,
)
def create_task_asset_direct_upload_session(
    payload: TaskAssetDirectUploadCreateRequest,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("task", "create")),
):
    service = TaskAssetService(db)
    try:
        session = service.create_direct_upload_session(
            category=payload.category,
            file_name=payload.file_name,
            file_size=payload.file_size,
            content_hash_sha256=payload.content_hash_sha256,
            content_type=payload.content_type,
            task_id=payload.task_id,
            user_id=actor.user_id,
            shard_count=payload.shard_count,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse.success(session)


@router.post(
    "/task-assets/direct-upload-sessions/{session_id}/finalize",
    response_model=ApiResponse[TaskAssetResponse],
    response_model_by_alias=True,
)
def finalize_task_asset_direct_upload_session(
    session_id: str,
    payload: TaskAssetDirectUploadFinalizeRequest,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("task", "create")),
):
    service = TaskAssetService(db)
    try:
        asset = service.finalize_direct_upload(
            session_id=session_id,
            finalize_token=payload.finalize_token,
            user_id=actor.user_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse.success(asset)


@router.get(
    "/task-assets",
    response_model=ApiResponse[list[TaskAssetResponse]],
    response_model_by_alias=True,
)
def list_task_assets(
    task_id: int = Query(..., ge=1),
    category: str | None = Query(None),
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("task", "view")),
):
    service = TaskAssetService(db)
    try:
        assets = service.list_assets(
            task_id=task_id, category=category, user_id=actor.user_id
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return ApiResponse.success(assets)


@router.post(
    "/task-assets/bind",
    response_model=ApiResponse[list[TaskAssetResponse]],
    response_model_by_alias=True,
)
def bind_task_assets(
    payload: TaskAssetBindRequest,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("task", "create")),
):
    service = TaskAssetService(db)
    try:
        assets = service.bind_assets(
            task_id=payload.task_id, asset_ids=payload.asset_ids, user_id=actor.user_id
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse.success(assets)


@router.get("/task-assets/{asset_id}/download")
def download_task_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("task", "view")),
):
    service = TaskAssetService(db)
    try:
        asset, payload = service.read_asset_bytes(
            asset_id=asset_id, user_id=actor.user_id
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    disposition = f"attachment; filename*=UTF-8''{quote(asset.file_name)}"
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/octet-stream",
        headers={"Content-Disposition": disposition},
    )


@router.delete(
    "/task-assets/{asset_id}",
    response_model=ApiResponse[None],
    response_model_by_alias=True,
)
def delete_task_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("task", "create")),
):
    service = TaskAssetService(db)
    try:
        service.delete_asset(asset_id=asset_id, user_id=actor.user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApiResponse.success(None)
