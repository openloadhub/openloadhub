import logging
import traceback
import re
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text
from uuid import uuid4
from contextlib import asynccontextmanager
import os

from app.api.v1 import (
    agent,
    audit,
    auth,
    meta,
    notification,
    plan,
    plan_run,
    report,
    run,
    script,
    task_asset,
    task,
    websocket,
)
from app.core.config import settings
from app.core.nacos_client import init_nacos_client
from app.schemas.response import ApiResponse
from common.config.settings import build_runtime_metadata, ensure_host_runtime_safe

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
UPLOAD_SCRIPT_PATH = "/api/v1/scripts/upload"
_DEFAULT_ADMIN_WEAK_PASSWORDS = {
    "admin",
    "admin123",
    "admin12345",
    "password",
    "password123",
    "test1234",
    "testpassword123",
    "12345678",
    "123456789",
}


def _new_trace_id() -> str:
    return str(uuid4())


def _request_trace_id(request: Request | None) -> str:
    if request is None:
        return _new_trace_id()
    trace_id = getattr(request.state, "trace_id", None)
    if trace_id:
        return trace_id
    trace_id = _new_trace_id()
    request.state.trace_id = trace_id
    return trace_id


def _json_error_response(
    *,
    status_code: int,
    code: int,
    message: str,
    data,
    trace_id: str,
) -> JSONResponse:
    body = ApiResponse(code=code, message=message, data=data, trace_id=trace_id)
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(body.model_dump(by_alias=True)),
        headers={"X-Trace-Id": trace_id},
    )


def _iter_exception_frames(exc: Exception) -> list[traceback.FrameSummary]:
    frames: list[traceback.FrameSummary] = []
    current: Exception | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if current.__traceback__ is not None:
            frames.extend(traceback.extract_tb(current.__traceback__))
        current = current.__cause__ or current.__context__
    return frames


def _classify_upload_exception_layer(frames: list[traceback.FrameSummary]) -> str:
    normalized = [frame.filename.replace("\\", "/") for frame in frames]
    if any(
        path.endswith("/fastapi/routing.py") and frame.name == "serialize_response"
        for path, frame in zip(normalized, frames)
    ):
        return "upload_response_model_serialization"
    if any(path.endswith("/app/core/permissions.py") for path in normalized):
        return "upload_permission_dependency"
    if any(path.endswith("/app/api/deps.py") for path in normalized):
        return "upload_actor_principal_dependency"
    if any("formparsers.py" in path or "/multipart/" in path for path in normalized):
        return "upload_multipart_parse"
    if any(path.endswith("/app/api/v1/script.py") for path in normalized):
        return "upload_route_body"
    return "upload_route_outer_layer"


def _relative_frame_path(filename: str) -> str:
    normalized = filename.replace("\\", "/")
    marker = "/backend/ptp-admin/"
    if marker in normalized:
        return normalized.split(marker, 1)[1]
    return normalized


def _build_upload_exception_detail(request: Request, exc: Exception) -> dict:
    frames = _iter_exception_frames(exc)
    app_frames = [
        f"{_relative_frame_path(frame.filename)}:{frame.lineno} in {frame.name}"
        for frame in frames
        if "/backend/ptp-admin/" in frame.filename.replace("\\", "/")
    ]
    detail = {
        "path": request.url.path,
        "method": request.method,
        "endpoint": getattr(request.scope.get("endpoint"), "__name__", None),
        "exception_type": type(exc).__name__,
        "reason": str(exc),
        "layer": _classify_upload_exception_layer(frames),
        "top_frame": (
            f"{_relative_frame_path(frames[-1].filename)}:{frames[-1].lineno} in {frames[-1].name}"
            if frames
            else None
        ),
        "app_frames": app_frames[-6:],
    }
    if isinstance(exc, ResponseValidationError):
        detail["validation_errors"] = exc.errors()
        detail["layer"] = "upload_response_model_serialization"
    return detail


def _ensure_db_schema(engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    if "olh_task" in tables:
        task_cols = {c["name"] for c in inspector.get_columns("olh_task")}
        missing = {"env", "task_pattern", "protocols"} - task_cols
        if missing:
            raise RuntimeError(
                "数据库表 olh_task 缺少字段："
                + ", ".join(sorted(missing))
                + "；请先执行 Alembic 迁移到 head。"
            )

    required_runtime_tables = {
        "olh_run_baseline": "缺少运行基线表，当前 baseline 接口会直接 500；请先执行 Alembic 迁移到 head。",
    }
    missing_runtime_tables = {
        table_name: detail
        for table_name, detail in required_runtime_tables.items()
        if table_name not in tables
    }
    if missing_runtime_tables:
        detail_text = " / ".join(
            f"{table_name}: {detail}"
            for table_name, detail in sorted(missing_runtime_tables.items())
        )
        raise RuntimeError(
            "数据库缺少运行期必需表："
            + ", ".join(sorted(missing_runtime_tables))
            + f"；{detail_text} 请执行：cd backend/ptp-admin && alembic upgrade head。"
        )

    # 迁移版本必须存在（否则会出现“表已存在但缺列/缺索引”的漂移）
    if "alembic_version" not in tables:
        raise RuntimeError(
            "数据库缺少 alembic_version 表；请先执行迁移（cd backend/ptp-admin && alembic upgrade head），"
            "或按 docs/sql/README.md 使用手工 SQL 脚本初始化。"
        )

    with engine.connect() as conn:
        versions = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).fetchall()
        if not versions:
            # 典型场景：历史上用 create_all 初始化过表，但没有 Alembic 版本记录
            raise RuntimeError(
                "数据库 alembic_version 为空（疑似历史 create_all 初始化导致未纳入迁移跟踪）；"
                "请执行：cd backend/ptp-admin && alembic stamp 000000 && alembic upgrade head。"
            )


def _default_admin_bootstrap_risk_reason(
    *,
    username: str,
    password: str,
    email: str,
) -> str | None:
    normalized_username = username.strip().lower()
    normalized_password = password.strip().lower()
    normalized_email = email.strip().lower()
    email_local = (
        normalized_email.split("@", 1)[0]
        if "@" in normalized_email
        else normalized_email
    )

    if normalized_password in {normalized_username, email_local}:
        return "DEFAULT_ADMIN_PASSWORD 不能与用户名或邮箱前缀相同，已按最小安全口径跳过默认管理员创建。"
    if len(password.encode("utf-8")) < 12:
        return "DEFAULT_ADMIN_PASSWORD 长度不足 12 位，已按最小安全口径跳过默认管理员创建。"
    if normalized_password in _DEFAULT_ADMIN_WEAK_PASSWORDS:
        return "DEFAULT_ADMIN_PASSWORD 命中已知弱口令集合，已按最小安全口径跳过默认管理员创建。"
    if normalized_username in {"admin", "administrator"} and re.fullmatch(
        r"admin\d+", normalized_password
    ):
        return "DEFAULT_ADMIN_PASSWORD 仍是弱默认变体，已按最小安全口径跳过默认管理员创建。"
    return None


def _optional_bootstrap_file_exists(raw_path: str) -> bool:
    bootstrap_path = Path(raw_path)
    if not bootstrap_path.is_absolute():
        bootstrap_path = Path.cwd() / bootstrap_path
    return bootstrap_path.exists()


def _initialize_default_admin(SessionLocal) -> None:
    admin_username = settings.DEFAULT_ADMIN_USERNAME.strip()
    admin_password = settings.DEFAULT_ADMIN_PASSWORD.strip()

    if not admin_username and not admin_password:
        logger.info(
            "默认管理员初始化未启用；如需首启创建，请在根环境文件配置 DEFAULT_ADMIN_USERNAME/DEFAULT_ADMIN_PASSWORD。"
        )
        return

    if not admin_username or not admin_password:
        logger.warning(
            "默认管理员初始化跳过：DEFAULT_ADMIN_USERNAME 与 DEFAULT_ADMIN_PASSWORD 必须同时配置；"
            "该逻辑只会首启创建，不会重置已有账号密码。"
        )
        return

    admin_email = settings.DEFAULT_ADMIN_EMAIL.strip() or "admin@example.com"
    admin_full_name = settings.DEFAULT_ADMIN_FULL_NAME.strip() or "Administrator"
    bootstrap_risk = _default_admin_bootstrap_risk_reason(
        username=admin_username,
        password=admin_password,
        email=admin_email,
    )
    if bootstrap_risk:
        logger.warning("默认管理员初始化跳过：%s", bootstrap_risk)
        return

    from app.models.user import User, UserRole
    from app.schemas.auth import UserCreate
    from app.services.auth_service import AuthService

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == admin_username).first()
        if existing:
            logger.info(
                "默认管理员已存在，按规范跳过创建且不会重置密码: %s", admin_username
            )
            return

        auth_service = AuthService(db)
        user = auth_service.create_user(
            UserCreate(
                username=admin_username,
                email=admin_email,
                full_name=admin_full_name,
                role=UserRole.ADMIN,
                password=admin_password,
            )
        )
        user.is_superuser = True
        db.commit()
        logger.info("默认管理员创建成功: %s", admin_username)
    except Exception as exc:
        logger.error("创建默认管理员失败: %s", exc)
        db.rollback()
    finally:
        db.close()


def _initialize_default_tester(SessionLocal) -> None:
    tester_username = settings.DEFAULT_TESTER_USERNAME.strip()
    tester_password = settings.DEFAULT_TESTER_PASSWORD.strip()

    if not tester_username and not tester_password:
        return

    if not tester_username or not tester_password:
        logger.warning(
            "默认测试用户初始化跳过：DEFAULT_TESTER_USERNAME 与 DEFAULT_TESTER_PASSWORD 必须同时配置；"
            "该逻辑只会首启创建，不会重置已有账号密码。"
        )
        return

    tester_email = settings.DEFAULT_TESTER_EMAIL.strip() or "demo_tester@example.com"
    tester_full_name = settings.DEFAULT_TESTER_FULL_NAME.strip() or "Demo Tester"
    bootstrap_risk = _default_admin_bootstrap_risk_reason(
        username=tester_username,
        password=tester_password,
        email=tester_email,
    )
    if bootstrap_risk:
        logger.warning("默认测试用户初始化跳过：%s", bootstrap_risk)
        return

    from app.models.user import User, UserRole
    from app.schemas.auth import UserCreate
    from app.services.auth_service import AuthService

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == tester_username).first()
        if existing:
            logger.info(
                "默认测试用户已存在，跳过创建且不会重置密码: %s", tester_username
            )
            return

        AuthService(db).create_user(
            UserCreate(
                username=tester_username,
                email=tester_email,
                full_name=tester_full_name,
                role=UserRole.TESTER,
                password=tester_password,
            )
        )
        logger.info("默认测试用户创建成功: %s", tester_username)
    except Exception as exc:
        logger.error("创建默认测试用户失败: %s", exc)
        db.rollback()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("%s v%s starting", settings.APP_NAME, settings.VERSION)
    if not settings.TESTING:
        ensure_host_runtime_safe(service_name="ptp-admin")
        try:
            init_nacos_client(
                server_addresses=settings.NACOS_SERVER,
                namespace=settings.NACOS_NAMESPACE,
                username=settings.NACOS_USERNAME,
                password=settings.NACOS_PASSWORD,
            )
            logger.info("Nacos client initialized for agent discovery")
        except Exception as exc:
            logger.warning("Init Nacos client failed: %s", exc)

        from app.core.database import engine, SessionLocal
        from app.services.notification_service import NotificationService

        _ensure_db_schema(engine)
        _initialize_default_admin(SessionLocal)
        _initialize_default_tester(SessionLocal)
        if settings.PTP_ENABLE_NOTIFICATIONS and settings.PTP_WEBHOOK_BOOTSTRAP_FILE:
            if _optional_bootstrap_file_exists(settings.PTP_WEBHOOK_BOOTSTRAP_FILE):
                db = SessionLocal()
                try:
                    synced = NotificationService.sync_webhook_configs_from_file(
                        db,
                        config_file=settings.PTP_WEBHOOK_BOOTSTRAP_FILE,
                    )
                    logger.info(
                        "Webhook bootstrap synced %d config(s) from %s",
                        len(synced),
                        settings.PTP_WEBHOOK_BOOTSTRAP_FILE,
                    )
                finally:
                    db.close()
            else:
                logger.warning(
                    "Webhook bootstrap file not found, skipping optional sync: %s",
                    settings.PTP_WEBHOOK_BOOTSTRAP_FILE,
                )
    yield
    logger.info("%s shutdown", settings.APP_NAME)


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def attach_trace_id(request: Request, call_next):
    request.state.trace_id = request.headers.get("X-Trace-Id") or _new_trace_id()
    started_at = perf_counter()
    response = await call_next(request)
    duration_ms = (perf_counter() - started_at) * 1000
    response.headers.setdefault("X-Trace-Id", request.state.trace_id)
    response.headers.setdefault("X-OpenLoadHub-Process-Time-Ms", f"{duration_ms:.2f}")
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):  # type: ignore[override]
    trace_id = _request_trace_id(request)
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        return _json_error_response(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code=401001,
            message=exc.detail or "认证失败",
            data=None,
            trace_id=trace_id,
        )

    # 业务错误：统一 200 + body.code
    return _json_error_response(
        status_code=status.HTTP_200_OK,
        code=exc.status_code,
        message=str(exc.detail),
        data=None,
        trace_id=trace_id,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):  # type: ignore[override]
    return _json_error_response(
        status_code=status.HTTP_200_OK,
        code=400001,
        message="参数验证失败",
        data={"detail": exc.errors()},
        trace_id=_request_trace_id(request),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):  # type: ignore[override]
    trace_id = _request_trace_id(request)
    upload_detail = None
    if request.url.path == UPLOAD_SCRIPT_PATH:
        upload_detail = _build_upload_exception_detail(request, exc)
    logger.exception(
        "Unhandled exception trace_id=%s path=%s method=%s layer=%s: %s",
        trace_id,
        request.url.path,
        request.method,
        upload_detail["layer"] if upload_detail else "generic",
        exc,
    )
    if upload_detail is not None:
        return _json_error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=500001,
            message="Script upload unhandled failure",
            data=upload_detail,
            trace_id=trace_id,
        )
    return _json_error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code=500000,
        message="服务器内部错误",
        data=None,
        trace_id=trace_id,
    )


@app.get("/health")
async def health_check():
    metadata = build_runtime_metadata(service_name="ptp-admin")
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.VERSION,
        "metadata": metadata,
    }


app.include_router(task.router, prefix="/api/v1", tags=["tasks"])
app.include_router(script.router, prefix="/api/v1", tags=["scripts"])
app.include_router(task_asset.router, prefix="/api/v1", tags=["task-assets"])
app.include_router(report.router, prefix="/api/v1", tags=["reports"])
app.include_router(auth.router, prefix="/api/v1", tags=["auth"])
app.include_router(websocket.router, prefix="/api/v1", tags=["websocket"])
app.include_router(run.router, prefix="/api/v1", tags=["runs"])
app.include_router(plan.router, prefix="/api/v1", tags=["plans"])
app.include_router(plan_run.router, prefix="/api/v1", tags=["plan-runs"])
if settings.PTP_ENABLE_NOTIFICATIONS:
    app.include_router(notification.router, prefix="/api/v1", tags=["notifications"])
app.include_router(meta.router, prefix="/api/v1", tags=["meta"])
app.include_router(audit.router, prefix="/api/v1", tags=["audit"])
app.include_router(agent.router, prefix="/api/v1", tags=["agents"])

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
