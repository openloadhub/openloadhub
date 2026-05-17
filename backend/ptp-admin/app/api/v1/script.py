"""
脚本管理 API 路由

提供脚本的 CRUD 操作
"""

import logging
import io
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import File, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from typing import Optional

from pathlib import Path
from uuid import uuid4
import hashlib

from app.api.deps import (
    ActorPrincipal,
    get_actor_principal,
    get_db,
)
from app.core.permissions import require_permission
from app.models.script import ScriptType, ScriptStatus
from app.schemas.response import ApiResponse, PageResult
from app.schemas.script import (
    CurlToK6ScriptCreate,
    CurlToK6PreviewResponse,
    CurlToK6ScriptCreateResponse,
    HarToK6PreviewResponse,
    HarToK6ScriptCreate,
    HarToK6ScriptCreateResponse,
    HarToK6SpecParseRequest,
    HarToK6SpecParseResponse,
    OpenApiToK6PreviewResponse,
    OpenApiToK6ScriptCreate,
    OpenApiToK6ScriptCreateResponse,
    OpenApiToK6SpecParseRequest,
    OpenApiToK6SpecParseResponse,
    ScriptContentUpdate,
    ScriptCreate,
    ScriptUpdate,
    ScriptResponse,
)
from app.services.script_service import ScriptService
from common.config.settings import settings
from common.utils.time import to_rfc3339_z
from common.utils import s3_utils

router = APIRouter()
logger = logging.getLogger(__name__)


def _scripts_dir() -> Path:
    scripts_dir = ScriptService.default_local_scripts_dir()
    scripts_dir.mkdir(parents=True, exist_ok=True)
    return scripts_dir


def _request_trace_id(request: Request | None) -> str | None:
    if request is None:
        return None
    return getattr(request.state, "trace_id", None)


def _upload_error_response(
    message: str, detail: dict, *, trace_id: str | None = None
) -> JSONResponse:
    body = ApiResponse(
        code=500001, message=message, data=detail, trace_id=trace_id or uuid4().hex
    )
    return JSONResponse(
        status_code=500,
        content=jsonable_encoder(body.model_dump(by_alias=True)),
        headers={"X-Trace-Id": body.trace_id},
    )


def _cleanup_uploaded_artifact(
    *, use_s3: bool, file_path: Optional[str], bucket: Optional[str], key: Optional[str]
) -> None:
    if use_s3 and bucket and key:
        try:
            s3_utils.delete_object(bucket, key)
        except Exception as exc:  # pragma: no cover - best effort cleanup
            logger.warning(
                "Failed to cleanup uploaded S3 artifact %s/%s: %s", bucket, key, exc
            )
        return

    if file_path:
        try:
            Path(file_path).unlink(missing_ok=True)
        except Exception as exc:  # pragma: no cover - best effort cleanup
            logger.warning(
                "Failed to cleanup uploaded local artifact %s: %s", file_path, exc
            )


def _persist_script_content(
    *, content: bytes, unique_name: str, suffix: str, content_type: Optional[str]
) -> tuple[str, int, Optional[str], Optional[str], bool]:
    file_path: str
    file_size: int
    bucket: Optional[str] = None
    key: Optional[str] = None

    use_s3 = (
        settings.USE_S3
        and settings.AWS_ACCESS_KEY_ID
        and settings.AWS_SECRET_ACCESS_KEY
    )
    if use_s3:
        bucket = settings.S3_BUCKET
        key = f"scripts/{unique_name}{suffix}"
        s3_utils.upload_bytes(bucket, key, content, content_type=content_type)
        file_path = f"s3://{bucket}/{key}"
        file_size = len(content)
    else:
        scripts_dir = _scripts_dir()
        stored_path = scripts_dir / f"{unique_name}{suffix}"
        stored_path.write_bytes(content)
        file_size = stored_path.stat().st_size
        file_path = str(stored_path)
    return file_path, file_size, bucket, key, bool(use_s3)


@router.post(
    "/scripts/upload",
    response_model=ApiResponse[ScriptResponse],
    response_model_by_alias=True,
)
def upload_script(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("script", "upload")),
):
    """上传脚本文件并创建脚本记录（最小闭环）"""
    trace_id = _request_trace_id(request)
    filename = file.filename or "uploaded"
    ext = filename.split(".")[-1].lower() if "." in filename else ""
    if ext not in {"jmx", "js"}:
        raise HTTPException(status_code=400, detail="Only .jmx/.js are supported")

    script_type = ScriptType.JMETER if ext == "jmx" else ScriptType.K6
    suffix = f".{ext}" if ext else ""
    script_name = Path(filename).stem[:255] or "uploaded"
    unique_name = f"{script_name[:200]}-{uuid4().hex[:8]}"

    try:
        content = file.file.read()
        content_hash = hashlib.sha256(content).hexdigest()
    except Exception as exc:
        logger.exception("Script upload read failed for %s", filename)
        return _upload_error_response(
            "Script upload read failed",
            {
                "filename": filename,
                "script_type": script_type.value,
                "content_type": file.content_type,
                "reason": str(exc),
            },
            trace_id=trace_id,
        )

    use_s3 = bool(
        settings.USE_S3
        and settings.AWS_ACCESS_KEY_ID
        and settings.AWS_SECRET_ACCESS_KEY
    )
    try:
        file_path, file_size, bucket, key, use_s3 = _persist_script_content(
            content=content,
            unique_name=unique_name,
            suffix=suffix,
            content_type=file.content_type,
        )
    except Exception as exc:
        logger.exception("Script upload storage failed for %s", filename)
        return _upload_error_response(
            "Script upload storage failed",
            {
                "filename": filename,
                "script_type": script_type.value,
                "storage_backend": "s3" if use_s3 else "local",
                "s3_bucket": settings.S3_BUCKET if use_s3 else None,
                "s3_endpoint": settings.S3_ENDPOINT if use_s3 else None,
                "reason": str(exc),
            },
            trace_id=trace_id,
        )

    service = ScriptService(db)
    try:
        script = service.create_script(
            ScriptCreate(
                name=script_name,
                description=None,
                script_type=script_type,
                file_path=file_path,
                file_size=file_size,
                content_hash=content_hash,
                version="1.0",
                tags=None,
                parameters=None,
            ),
            user_id=actor.user_id,
        )
    except ValueError as exc:
        _cleanup_uploaded_artifact(
            use_s3=bool(use_s3), file_path=file_path, bucket=bucket, key=key
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        _cleanup_uploaded_artifact(
            use_s3=bool(use_s3), file_path=file_path, bucket=bucket, key=key
        )
        logger.exception("Script upload DB failure for %s", filename)
        return _upload_error_response(
            "Script upload database failed",
            {
                "filename": filename,
                "script_type": script_type.value,
                "storage_backend": "s3" if use_s3 else "local",
                "file_path": file_path,
                "content_hash": content_hash,
                "reason": str(exc),
            },
            trace_id=trace_id,
        )
    except Exception as exc:
        db.rollback()
        _cleanup_uploaded_artifact(
            use_s3=bool(use_s3), file_path=file_path, bucket=bucket, key=key
        )
        logger.exception("Script upload unexpected failure for %s", filename)
        return _upload_error_response(
            "Script upload database failed",
            {
                "filename": filename,
                "script_type": script_type.value,
                "storage_backend": "s3" if use_s3 else "local",
                "file_path": file_path,
                "content_hash": content_hash,
                "reason": str(exc),
            },
            trace_id=trace_id,
        )

    try:
        script_response = ScriptResponse.model_validate(script)
    except ValidationError as exc:
        logger.exception("Script upload response validation failed for %s", filename)
        return _upload_error_response(
            "Script upload response validation failed",
            {
                "filename": filename,
                "script_type": script_type.value,
                "layer": "upload_response_model_serialization",
                "script_id": getattr(script, "id", None),
                "created_persisted": getattr(script, "id", None) is not None,
                "validation_errors": exc.errors(),
            },
            trace_id=trace_id,
        )

    return ApiResponse.success(script_response)


@router.post(
    "/scripts/from-curl",
    response_model=ApiResponse[CurlToK6ScriptCreateResponse],
    response_model_by_alias=True,
)
def create_k6_script_from_curl(
    payload: CurlToK6ScriptCreate,
    request: Request,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("script", "upload")),
):
    """根据单接口 HTTP CURL 生成 K6 脚本并落成脚本资产。"""
    trace_id = _request_trace_id(request)
    service = ScriptService(db)

    try:
        content, script_name, parsed, suggestions, warnings = (
            service.build_k6_script_from_curl(payload)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    content_bytes = content.encode("utf-8")
    content_hash = hashlib.sha256(content_bytes).hexdigest()
    unique_name = f"{Path(script_name).stem[:200]}-{uuid4().hex[:8]}"
    file_path: Optional[str] = None
    bucket: Optional[str] = None
    key: Optional[str] = None
    use_s3 = bool(
        settings.USE_S3
        and settings.AWS_ACCESS_KEY_ID
        and settings.AWS_SECRET_ACCESS_KEY
    )
    try:
        file_path, file_size, bucket, key, use_s3 = _persist_script_content(
            content=content_bytes,
            unique_name=unique_name,
            suffix=".js",
            content_type="text/javascript; charset=utf-8",
        )
    except Exception as exc:
        logger.exception("Script from curl storage failed for %s", script_name)
        return _upload_error_response(
            "Script upload storage failed",
            {
                "script_name": script_name,
                "script_type": ScriptType.K6.value,
                "storage_backend": "s3" if use_s3 else "local",
                "s3_bucket": settings.S3_BUCKET if use_s3 else None,
                "s3_endpoint": settings.S3_ENDPOINT if use_s3 else None,
                "reason": str(exc),
            },
            trace_id=trace_id,
        )

    try:
        script = service.create_script(
            ScriptCreate(
                name=script_name,
                description="Generated from CURL",
                script_type=ScriptType.K6,
                file_path=file_path,
                file_size=file_size,
                content_hash=content_hash,
                version="1.0",
                tags=["generated", "curl", "k6"],
                parameters={
                    "generated_from": "curl",
                    "generated_from_display": "curl_to_k6",
                    "generator": "curl_to_k6_mvp",
                    "generator_version": "v1",
                    "provenance_version": "v1",
                    "provenance_input_contract": "curl_to_k6_v1",
                    "script_author": "ptp_ai_script_author",
                    "llm_generated": False,
                    "llm_review_status": "not_configured",
                    "ai_review_status": "manual_required",
                    "human_confirmation_required": True,
                    "human_confirmation_status": "pending",
                    "final_script_status": "draft_pending_human_review",
                    "source_url": parsed.url,
                    "source_host": urlparse(parsed.url).netloc or None,
                    "source_path": urlparse(parsed.url).path or "/",
                    "http_method": parsed.method,
                    "protocol": parsed.protocol,
                    "connect_timeout_ms": parsed.connect_timeout_ms,
                    "response_timeout_ms": parsed.response_timeout_ms,
                    "body_mode": parsed.body_mode,
                    "query_item_count": len(parsed.query_items),
                    "header_item_count": len(parsed.header_items),
                    "body_item_count": len(parsed.body_items),
                    "body_present": parsed.body_present,
                },
            ),
            user_id=actor.user_id,
        )
    except ValueError as exc:
        _cleanup_uploaded_artifact(
            use_s3=bool(use_s3), file_path=file_path, bucket=bucket, key=key
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        _cleanup_uploaded_artifact(
            use_s3=bool(use_s3), file_path=file_path, bucket=bucket, key=key
        )
        logger.exception("Script from curl DB failure for %s", script_name)
        return _upload_error_response(
            "Script upload database failed",
            {
                "script_name": script_name,
                "script_type": ScriptType.K6.value,
                "storage_backend": "s3" if use_s3 else "local",
                "file_path": file_path,
                "content_hash": content_hash,
                "reason": str(exc),
            },
            trace_id=trace_id,
        )
    except Exception as exc:
        db.rollback()
        _cleanup_uploaded_artifact(
            use_s3=bool(use_s3), file_path=file_path, bucket=bucket, key=key
        )
        logger.exception("Script from curl unexpected DB failure for %s", script_name)
        return _upload_error_response(
            "Script upload database failed",
            {
                "script_name": script_name,
                "script_type": ScriptType.K6.value,
                "storage_backend": "s3" if use_s3 else "local",
                "file_path": file_path,
                "content_hash": content_hash,
                "reason": str(exc),
            },
            trace_id=trace_id,
        )

    try:
        script_response = ScriptResponse.model_validate(script)
    except ValidationError as exc:
        logger.exception(
            "Script from curl response validation failed for %s", script_name
        )
        return _upload_error_response(
            "Script upload response validation failed",
            {
                "script_name": script_name,
                "script_type": ScriptType.K6.value,
                "layer": "curl_generation_response_model_serialization",
                "script_id": getattr(script, "id", None),
                "created_persisted": getattr(script, "id", None) is not None,
                "validation_errors": exc.errors(),
            },
            trace_id=trace_id,
        )

    return ApiResponse.success(
        CurlToK6ScriptCreateResponse(
            script=script_response,
            parsed=parsed,
            suggested_variables=suggestions,
            warnings=warnings,
        )
    )


@router.post(
    "/scripts/preview-curl",
    response_model=ApiResponse[CurlToK6PreviewResponse],
    response_model_by_alias=True,
)
def preview_k6_script_from_curl(
    payload: CurlToK6ScriptCreate,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("script", "upload")),
):
    """解析单接口 HTTP CURL 并返回 K6 脚本预览，不落库。"""
    del actor
    service = ScriptService(db)
    try:
        preview = service.preview_k6_script_from_curl(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse.success(preview)



@router.post(
    "/scripts/parse-openapi",
    response_model=ApiResponse[OpenApiToK6SpecParseResponse],
    response_model_by_alias=True,
)
def parse_openapi_spec(
    payload: OpenApiToK6SpecParseRequest,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("script", "upload")),
):
    """解析 OpenAPI spec，返回可选 endpoint 列表。"""
    del actor
    service = ScriptService(db)
    try:
        parsed = service.parse_openapi_spec(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse.success(parsed)


@router.post(
    "/scripts/preview-openapi",
    response_model=ApiResponse[OpenApiToK6PreviewResponse],
    response_model_by_alias=True,
)
def preview_k6_script_from_openapi(
    payload: OpenApiToK6ScriptCreate,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("script", "upload")),
):
    """根据 OpenAPI 单接口定义预览 K6 脚本，不落库。"""
    del actor
    service = ScriptService(db)
    try:
        preview = service.preview_k6_script_from_openapi(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse.success(preview)


@router.post(
    "/scripts/from-openapi",
    response_model=ApiResponse[OpenApiToK6ScriptCreateResponse],
    response_model_by_alias=True,
)
def create_k6_script_from_openapi(
    payload: OpenApiToK6ScriptCreate,
    request: Request,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("script", "upload")),
):
    """根据 OpenAPI 单接口定义生成 K6 脚本并落成脚本资产。"""
    trace_id = _request_trace_id(request)
    service = ScriptService(db)

    try:
        content, script_name, parsed, suggestions, warnings = (
            service.build_k6_script_from_openapi(payload)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    content_bytes = content.encode("utf-8")
    content_hash = hashlib.sha256(content_bytes).hexdigest()
    unique_name = f"{Path(script_name).stem[:200]}-{uuid4().hex[:8]}"
    file_path: Optional[str] = None
    bucket: Optional[str] = None
    key: Optional[str] = None
    use_s3 = bool(
        settings.USE_S3
        and settings.AWS_ACCESS_KEY_ID
        and settings.AWS_SECRET_ACCESS_KEY
    )
    try:
        file_path, file_size, bucket, key, use_s3 = _persist_script_content(
            content=content_bytes,
            unique_name=unique_name,
            suffix=".js",
            content_type="text/javascript; charset=utf-8",
        )
    except Exception as exc:
        logger.exception("Script from openapi storage failed for %s", script_name)
        return _upload_error_response(
            "Script upload storage failed",
            {
                "script_name": script_name,
                "script_type": ScriptType.K6.value,
                "storage_backend": "s3" if use_s3 else "local",
                "s3_bucket": settings.S3_BUCKET if use_s3 else None,
                "s3_endpoint": settings.S3_ENDPOINT if use_s3 else None,
                "reason": str(exc),
            },
            trace_id=trace_id,
        )

    try:
        script = service.create_script(
            ScriptCreate(
                name=script_name,
                description="Generated from OpenAPI",
                script_type=ScriptType.K6,
                file_path=file_path,
                file_size=file_size,
                content_hash=content_hash,
                version="1.0",
                tags=["generated", "openapi", "k6"],
                parameters={
                    "generated_from": "openapi",
                    "generated_from_display": "openapi_to_k6",
                    "generator": "openapi_to_k6_mvp",
                    "generator_version": "v1",
                    "provenance_version": "v1",
                    "provenance_input_contract": "openapi_to_k6_v1",
                    "script_author": "ptp_ai_script_author",
                    "llm_generated": False,
                    "llm_review_status": "not_configured",
                    "ai_review_status": "manual_required",
                    "human_confirmation_required": True,
                    "human_confirmation_status": "pending",
                    "final_script_status": "draft_pending_human_review",
                    "source_url": parsed.source_url,
                    "source_host": urlparse(parsed.source_url).netloc or None,
                    "source_path": urlparse(parsed.source_url).path or "/",
                    "http_method": parsed.method,
                    "protocol": parsed.protocol,
                    "server_url": parsed.server_url,
                    "operation_id": parsed.operation_id,
                    "summary": parsed.summary,
                    "request_content_type": parsed.request_content_type,
                    "path_item_count": len(parsed.path_items),
                    "query_item_count": len(parsed.query_items),
                    "header_item_count": len(parsed.header_items),
                    "body_item_count": len(parsed.body_items),
                    "body_present": parsed.body_present,
                    "openapi_title": parsed.title,
                    "openapi_version": parsed.version,
                },
            ),
            user_id=actor.user_id,
        )
    except ValueError as exc:
        _cleanup_uploaded_artifact(
            use_s3=bool(use_s3), file_path=file_path, bucket=bucket, key=key
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        _cleanup_uploaded_artifact(
            use_s3=bool(use_s3), file_path=file_path, bucket=bucket, key=key
        )
        logger.exception("Script from openapi DB failure for %s", script_name)
        return _upload_error_response(
            "Script upload database failed",
            {
                "script_name": script_name,
                "script_type": ScriptType.K6.value,
                "storage_backend": "s3" if use_s3 else "local",
                "file_path": file_path,
                "content_hash": content_hash,
                "reason": str(exc),
            },
            trace_id=trace_id,
        )
    except Exception as exc:
        db.rollback()
        _cleanup_uploaded_artifact(
            use_s3=bool(use_s3), file_path=file_path, bucket=bucket, key=key
        )
        logger.exception(
            "Script from openapi unexpected DB failure for %s", script_name
        )
        return _upload_error_response(
            "Script upload database failed",
            {
                "script_name": script_name,
                "script_type": ScriptType.K6.value,
                "storage_backend": "s3" if use_s3 else "local",
                "file_path": file_path,
                "content_hash": content_hash,
                "reason": str(exc),
            },
            trace_id=trace_id,
        )

    try:
        script_response = ScriptResponse.model_validate(script)
    except ValidationError as exc:
        logger.exception(
            "Script from openapi response validation failed for %s", script_name
        )
        return _upload_error_response(
            "Script upload response validation failed",
            {
                "script_name": script_name,
                "script_type": ScriptType.K6.value,
                "layer": "openapi_generation_response_model_serialization",
                "script_id": getattr(script, "id", None),
                "created_persisted": getattr(script, "id", None) is not None,
                "validation_errors": exc.errors(),
            },
            trace_id=trace_id,
        )

    return ApiResponse.success(
        OpenApiToK6ScriptCreateResponse(
            script=script_response,
            parsed=parsed,
            suggested_variables=suggestions,
            warnings=warnings,
        )
    )


@router.post(
    "/scripts/parse-har",
    response_model=ApiResponse[HarToK6SpecParseResponse],
    response_model_by_alias=True,
)
def parse_har_spec(
    payload: HarToK6SpecParseRequest,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("script", "upload")),
):
    """解析 HAR，返回可选 HTTP entry 列表。"""
    del actor
    service = ScriptService(db)
    try:
        parsed = service.parse_har_spec(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse.success(parsed)


@router.post(
    "/scripts/preview-har",
    response_model=ApiResponse[HarToK6PreviewResponse],
    response_model_by_alias=True,
)
def preview_k6_script_from_har(
    payload: HarToK6ScriptCreate,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("script", "upload")),
):
    """根据 HAR 单 entry 预览 K6 脚本，不落库。"""
    del actor
    service = ScriptService(db)
    try:
        preview = service.preview_k6_script_from_har(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse.success(preview)


@router.post(
    "/scripts/from-har",
    response_model=ApiResponse[HarToK6ScriptCreateResponse],
    response_model_by_alias=True,
)
def create_k6_script_from_har(
    payload: HarToK6ScriptCreate,
    request: Request,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("script", "upload")),
):
    """根据 HAR 单 entry 生成 K6 脚本并落成脚本资产。"""
    trace_id = _request_trace_id(request)
    service = ScriptService(db)

    try:
        content, script_name, parsed, suggestions, warnings = (
            service.build_k6_script_from_har(payload)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    content_bytes = content.encode("utf-8")
    content_hash = hashlib.sha256(content_bytes).hexdigest()
    unique_name = f"{Path(script_name).stem[:200]}-{uuid4().hex[:8]}"
    file_path: Optional[str] = None
    bucket: Optional[str] = None
    key: Optional[str] = None
    use_s3 = bool(
        settings.USE_S3
        and settings.AWS_ACCESS_KEY_ID
        and settings.AWS_SECRET_ACCESS_KEY
    )
    try:
        file_path, file_size, bucket, key, use_s3 = _persist_script_content(
            content=content_bytes,
            unique_name=unique_name,
            suffix=".js",
            content_type="text/javascript; charset=utf-8",
        )
    except Exception as exc:
        logger.exception("Script from har storage failed for %s", script_name)
        return _upload_error_response(
            "Script upload storage failed",
            {
                "script_name": script_name,
                "script_type": ScriptType.K6.value,
                "storage_backend": "s3" if use_s3 else "local",
                "s3_bucket": settings.S3_BUCKET if use_s3 else None,
                "s3_endpoint": settings.S3_ENDPOINT if use_s3 else None,
                "reason": str(exc),
            },
            trace_id=trace_id,
        )

    try:
        script = service.create_script(
            ScriptCreate(
                name=script_name,
                description="Generated from HAR",
                script_type=ScriptType.K6,
                file_path=file_path,
                file_size=file_size,
                content_hash=content_hash,
                version="1.0",
                tags=["generated", "har", "k6"],
                parameters={
                    "generated_from": "har",
                    "generated_from_display": "har_to_k6",
                    "generator": "har_to_k6_mvp",
                    "generator_version": "v1",
                    "provenance_version": "v1",
                    "provenance_input_contract": "har_to_k6_v1",
                    "script_author": "ptp_ai_script_author",
                    "llm_generated": False,
                    "llm_review_status": "not_configured",
                    "ai_review_status": "manual_required",
                    "human_confirmation_required": True,
                    "human_confirmation_status": "pending",
                    "final_script_status": "draft_pending_human_review",
                    "entry_index": parsed.entry_index,
                    "source_url": parsed.url,
                    "source_host": urlparse(parsed.url).netloc or None,
                    "source_path": urlparse(parsed.url).path or "/",
                    "http_method": parsed.method,
                    "protocol": parsed.protocol,
                    "status": parsed.status,
                    "mime_type": parsed.mime_type,
                    "query_item_count": len(parsed.query_items),
                    "header_item_count": len(parsed.header_items),
                    "body_item_count": len(parsed.body_items),
                    "body_present": parsed.body_present,
                },
            ),
            user_id=actor.user_id,
        )
    except ValueError as exc:
        _cleanup_uploaded_artifact(
            use_s3=bool(use_s3), file_path=file_path, bucket=bucket, key=key
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        _cleanup_uploaded_artifact(
            use_s3=bool(use_s3), file_path=file_path, bucket=bucket, key=key
        )
        logger.exception("Script from har DB failure for %s", script_name)
        return _upload_error_response(
            "Script upload database failed",
            {
                "script_name": script_name,
                "script_type": ScriptType.K6.value,
                "storage_backend": "s3" if use_s3 else "local",
                "file_path": file_path,
                "content_hash": content_hash,
                "reason": str(exc),
            },
            trace_id=trace_id,
        )
    except Exception as exc:
        db.rollback()
        _cleanup_uploaded_artifact(
            use_s3=bool(use_s3), file_path=file_path, bucket=bucket, key=key
        )
        logger.exception("Script from har unexpected DB failure for %s", script_name)
        return _upload_error_response(
            "Script upload database failed",
            {
                "script_name": script_name,
                "script_type": ScriptType.K6.value,
                "storage_backend": "s3" if use_s3 else "local",
                "file_path": file_path,
                "content_hash": content_hash,
                "reason": str(exc),
            },
            trace_id=trace_id,
        )

    try:
        script_response = ScriptResponse.model_validate(script)
    except ValidationError as exc:
        logger.exception(
            "Script from har response validation failed for %s", script_name
        )
        return _upload_error_response(
            "Script upload response validation failed",
            {
                "script_name": script_name,
                "script_type": ScriptType.K6.value,
                "layer": "har_generation_response_model_serialization",
                "script_id": getattr(script, "id", None),
                "created_persisted": getattr(script, "id", None) is not None,
                "validation_errors": exc.errors(),
            },
            trace_id=trace_id,
        )

    return ApiResponse.success(
        HarToK6ScriptCreateResponse(
            script=script_response,
            parsed=parsed,
            suggested_variables=suggestions,
            warnings=warnings,
        )
    )


@router.get(
    "/scripts/{script_id}/content",
    response_model=ApiResponse[dict],
    response_model_by_alias=True,
)
def get_script_content(script_id: int, db: Session = Depends(get_db)):
    """读取脚本文件内容"""
    service = ScriptService(db)
    script = service.get_script(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    try:
        content = service.load_script_text(script)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Script file not found") from exc
    return ApiResponse.success({"content": content})


@router.get(
    "/scripts/{script_id}/executed-content",
    response_model=ApiResponse[dict],
    response_model_by_alias=True,
)
def get_script_executed_content(script_id: int, db: Session = Depends(get_db)):
    """返回脚本执行前预处理预览，当前主要用于展示 JMeter Influx listener 注入结果。"""
    service = ScriptService(db)
    script = service.get_script(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    try:
        content = service.load_script_text(script)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Script file not found") from exc

    runtime_preview = service.build_runtime_prepared_content(script, content)
    return ApiResponse.success(
        {
            "content": runtime_preview or content,
            "changed": runtime_preview is not None and runtime_preview != content,
            "preview_note": (
                "执行预览会展示平台运行期追加的 InfluxDB BackendListener 模板；真实 runId、token 等参数仍在执行时注入。"
                if runtime_preview is not None
                else None
            ),
        }
    )


@router.get("/scripts/{script_id}/download")
def download_script(script_id: int, db: Session = Depends(get_db)):
    """下载脚本文件"""
    service = ScriptService(db)
    script = service.get_script(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    try:
        payload = service.load_script_bytes(script)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Script file not found") from exc
    # Use script name as filename, fallback to id if name is empty
    filename = script.name or f"script-{script_id}"
    # Add extension based on script type
    if script.script_type == ScriptType.JMETER and not filename.endswith(".jmx"):
        filename = f"{filename}.jmx"
    elif script.script_type == ScriptType.K6 and not filename.endswith(".js"):
        filename = f"{filename}.js"
    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/octet-stream",
        headers={"Content-Disposition": disposition},
    )


@router.put(
    "/scripts/{script_id}/content",
    response_model=ApiResponse[ScriptResponse],
    response_model_by_alias=True,
)
def update_script_content(
    script_id: int,
    payload: ScriptContentUpdate,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("script", "upload")),
):
    """更新脚本正文"""
    service = ScriptService(db)
    try:
        script = service.update_script_content(
            script_id, payload, user_id=actor.user_id
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if detail in {"Script not found", "Task not found"} else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    return ApiResponse.success(script)


@router.post(
    "/scripts", response_model=ApiResponse[ScriptResponse], response_model_by_alias=True
)
def create_script(
    script_in: ScriptCreate,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("script", "upload")),
):
    """创建脚本"""
    service = ScriptService(db)
    try:
        return ApiResponse.success(
            service.create_script(script_in, user_id=actor.user_id)
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/scripts",
    response_model=ApiResponse[PageResult[ScriptResponse]],
    response_model_by_alias=True,
)
def list_scripts(
    status: Optional[ScriptStatus] = Query(None, description="脚本状态"),
    script_type: Optional[ScriptType] = Query(None, description="脚本类型"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100, alias="pageSize"),
    db: Session = Depends(get_db),
):
    """查询脚本列表"""
    service = ScriptService(db)
    skip = (page - 1) * page_size
    scripts, total = service.list_scripts(
        status=status,
        script_type=script_type,
        skip=skip,
        limit=page_size,
    )
    return ApiResponse.success(
        PageResult(items=scripts, total=total, page=page, page_size=page_size)
    )


@router.get(
    "/scripts/search",
    response_model=ApiResponse[PageResult[ScriptResponse]],
    response_model_by_alias=True,
)
def search_scripts(
    keyword: str = Query(..., description="搜索关键词"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100, alias="pageSize"),
    db: Session = Depends(get_db),
):
    """搜索脚本"""
    service = ScriptService(db)
    skip = (page - 1) * page_size
    scripts, total = service.search_scripts(keyword=keyword, skip=skip, limit=page_size)
    return ApiResponse.success(
        PageResult(items=scripts, total=total, page=page, page_size=page_size)
    )


@router.get(
    "/scripts/{script_id}",
    response_model=ApiResponse[ScriptResponse],
    response_model_by_alias=True,
)
def get_script(script_id: int, db: Session = Depends(get_db)):
    """获取脚本详情"""
    service = ScriptService(db)
    script = service.get_script(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    return ApiResponse.success(script)


@router.put(
    "/scripts/{script_id}",
    response_model=ApiResponse[ScriptResponse],
    response_model_by_alias=True,
)
def update_script(
    script_id: int,
    script_in: ScriptUpdate,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("script", "upload")),
):
    """更新脚本"""
    service = ScriptService(db)
    try:
        script = service.update_script(script_id, script_in)
        if not script:
            raise HTTPException(status_code=404, detail="Script not found")
        return ApiResponse.success(script)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete(
    "/scripts/{script_id}",
    response_model=ApiResponse[None],
    response_model_by_alias=True,
)
def delete_script(
    script_id: int,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("script", "delete")),
):
    """删除脚本"""
    service = ScriptService(db)
    try:
        service.delete_script(script_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return ApiResponse.success(None)


@router.get("/scripts/statistics", response_model=dict)
def get_script_statistics(db: Session = Depends(get_db)):
    """获取脚本统计信息"""
    service = ScriptService(db)
    return service.get_script_statistics()
