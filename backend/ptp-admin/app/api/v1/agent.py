"""
Agent 管理 API

提供当前已发现的 agent 列表及其健康状态
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.api.deps import ActorPrincipal, get_db
from app.core.agent_orchestrator import AgentOrchestrator
from app.core.permissions import require_permission
from app.models.report import Report
from app.core.nacos_client import get_nacos_client
from app.models.run import Run
from app.schemas.response import ApiResponse
from app.services.run_service import RunService
from common.models.enums import ReportStatus, RunStatus

logger = logging.getLogger(__name__)
router = APIRouter()

_orchestrator = AgentOrchestrator()


class AgentInfo(BaseModel):
    host: str
    ip: Optional[str] = None
    port: Optional[int] = None
    healthy: bool = False
    version: Optional[str] = None
    service: Optional[str] = None
    latency_ms: Optional[float] = None
    availability_state: str = "unhealthy"
    current_run_total: int = 0
    max_concurrency: Optional[int] = None
    current_load_ratio: Optional[float] = None
    runtime_kind: Optional[str] = None
    last_error: Optional[str] = None
    metadata: Dict[str, str] = Field(default_factory=dict, exclude=True)
    probe_error: Optional[str] = Field(default=None, exclude=True)


AgentListResponse = ApiResponse[List[AgentInfo]]


class AgentOpsAlertItem(BaseModel):
    key: str
    title: str
    severity: str = "ok"
    count: int = 0
    summary: str
    detail: Optional[str] = None
    sample_run_ids: List[int] = Field(default_factory=list)
    sample_report_ids: List[int] = Field(default_factory=list)
    sample_hosts: List[str] = Field(default_factory=list)


class AgentOpsSummary(BaseModel):
    has_alerts: bool = False
    critical_total: int = 0
    warning_total: int = 0
    items: List[AgentOpsAlertItem] = Field(default_factory=list)


AgentOpsSummaryResponse = ApiResponse[AgentOpsSummary]


class AgentBulkStopRequest(BaseModel):
    envs: Optional[List[str]] = None
    reason: Optional[str] = "agent_admin_bulk_stop"


class AgentBulkStopResponse(BaseModel):
    scope: str
    target_envs: List[str] = Field(default_factory=list)
    matched_run_total: int = 0
    stopped_run_ids: List[int] = Field(default_factory=list)
    remote_stop_summary: Dict[str, Any] = Field(default_factory=dict)


async def _probe_health(host: str) -> AgentInfo:
    """探测单个 agent 的健康状态"""
    try:
        parts = host.split(":")
        ip = parts[0] if parts else host
        port = int(parts[1]) if len(parts) > 1 else 9096
    except (ValueError, IndexError):
        logger.debug("Malformed agent host: %s", host)
        return AgentInfo(host=host, healthy=False)

    url = f"http://{host}/health"
    info = AgentInfo(host=host, ip=ip, port=port)
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            info.healthy = data.get("status") == "ok"
            info.version = data.get("version")
            info.service = data.get("service")
            info.latency_ms = round(resp.elapsed.total_seconds() * 1000, 1)
            payload_metadata = data.get("metadata")
            if isinstance(payload_metadata, dict):
                info.metadata = {
                    str(key): str(value)
                    for key, value in payload_metadata.items()
                    if value not in (None, "")
                }
            runtime_kind = data.get("runtime_kind")
            if isinstance(runtime_kind, str) and runtime_kind.strip():
                info.runtime_kind = runtime_kind.strip()
                info.metadata.setdefault("runtime_kind", info.runtime_kind)
    except Exception as exc:
        logger.debug("Agent health probe failed for %s: %s", host, exc)
        info.healthy = False
        info.probe_error = str(exc)
    return info


def _fallback_hosts() -> list[str]:
    static_hosts = os.getenv("AGENT_HOSTS", "")
    hosts = [host.strip() for host in static_hosts.split(",") if host.strip()]
    if hosts:
        return list(dict.fromkeys(hosts))
    return []


def _safe_get_nacos_client():
    try:
        return get_nacos_client()
    except Exception as exc:  # pragma: no cover - 容错
        logger.warning("agent list failed to init nacos client: %s", exc)
        return None


def _normalize_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_positive_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        normalized = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _iter_run_agent_aliases(params: Any) -> list[str]:
    if not isinstance(params, dict):
        return []

    aliases: list[str] = []
    seen: set[str] = set()

    def register(raw_value: Any) -> None:
        normalized = _normalize_text(raw_value)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        aliases.append(normalized)

    agent_runs = params.get("agent_runs")
    if isinstance(agent_runs, list):
        for item in agent_runs:
            if not isinstance(item, dict):
                continue
            register(item.get("agent_host"))
            register(item.get("agent_ip"))

    register(params.get("agent_host"))
    register(params.get("agent_ip"))
    return aliases


_TERMINAL_RUN_STATUSES = (
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.STOPPED,
)
_ACTIVE_RUN_STATUSES = (
    RunStatus.PREPARING,
    RunStatus.RUNNING,
)
_AGENT_LIVE_BUSY_STATUSES = {"running"}
_AGENT_LIVE_TERMINAL_STATUSES = {
    "succeeded",
    "failed",
    "stopped",
    "terminated",
    "completed",
}


def _agent_list_recent_terminal_window_seconds() -> int:
    """agent 列表 recent-terminal 窗口与 resource_pool 维持同一 30 分钟基线。"""
    baseline = _running_record_freshness_seconds()
    raw = os.getenv("TASK_RESOURCE_POOL_RECENT_TERMINAL_SECONDS")
    if raw is None or not str(raw).strip():
        return baseline
    try:
        configured = int(raw)
    except (TypeError, ValueError):
        return baseline
    return max(baseline, configured)


def _false_failed_live_lookback_seconds() -> int:
    raw = os.getenv("TASK_RESOURCE_POOL_FALSE_FAILED_LIVE_LOOKBACK_SECONDS", "21600")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 21600


def _running_record_freshness_seconds() -> int:
    raw = os.getenv("TASK_RESOURCE_POOL_RUNNING_FRESHNESS_SECONDS", "1800")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 1800


def _recent_terminal_reference_at_or_after(since: datetime):
    return or_(
        Run.updated_at >= since,
        and_(
            Run.updated_at.is_(None),
            func.coalesce(Run.ended_at, Run.started_at, Run.created_at) >= since,
        ),
    )


def _iter_agent_run_contexts(params: Any) -> list[tuple[str, str]]:
    """从 run.params 抽出 (agent_host, run_token) 二元组用于 live tie-break。

    token 支持多个历史别名：`agent_run_token`(主流 / poll_run_status 写入) /
    `agent_token` / `agent_session` / `run_token`，以便与 TaskService 的
    `_extract_agent_run_contexts` 保持一致。
    """
    if not isinstance(params, dict):
        return []
    contexts: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _extract_token(source: dict) -> Any:
        return (
            source.get("agent_run_token")
            or source.get("agent_token")
            or source.get("agent_session")
            or source.get("run_token")
        )

    def register(host_raw: Any, token_raw: Any) -> None:
        host = _normalize_text(host_raw)
        token = _normalize_text(token_raw)
        if not host or not token:
            return
        key = (host, token)
        if key in seen:
            return
        seen.add(key)
        contexts.append(key)

    agent_runs = params.get("agent_runs")
    if isinstance(agent_runs, list):
        for item in agent_runs:
            if not isinstance(item, dict):
                continue
            register(
                item.get("agent_host") or item.get("agent_ip"), _extract_token(item)
            )

    register(params.get("agent_host") or params.get("agent_ip"), _extract_token(params))
    return contexts


def _fetch_agent_run_status_sync(host: str, token: str) -> Optional[dict]:
    url = f"http://{host}/agent/runs/{token}/status"
    try:
        response = httpx.get(url, timeout=3.0, trust_env=False)
        response.raise_for_status()
    except Exception as exc:
        logger.debug(
            "agent list live tie-break fetch failed host=%s token=%s err=%s",
            host,
            token,
            exc,
        )
        return None
    try:
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _live_run_busy_verdict(
    run: Run,
    *,
    status_cache: dict[tuple[str, str], Optional[dict]],
) -> Optional[bool]:
    """按 agent /status 判断 run 是否仍占用 agent。

    返回:
    - True: 至少一个 agent context 明确仍在 busy
    - False: 所有 agent context 都已明确 terminal
    - None: 没拿到确定证据
    """
    contexts = _iter_agent_run_contexts(run.params)
    if not contexts:
        return None
    saw_busy = False
    saw_unknown = False
    for host, token in contexts:
        key = (host, token)
        if key not in status_cache:
            status_cache[key] = _fetch_agent_run_status_sync(host, token)
        payload = status_cache[key]
        if not isinstance(payload, dict):
            saw_unknown = True
            continue
        status = str(payload.get("status") or "").strip().lower()
        if status in _AGENT_LIVE_BUSY_STATUSES:
            saw_busy = True
            continue
        if status not in _AGENT_LIVE_TERMINAL_STATUSES:
            saw_unknown = True
    if saw_busy:
        return True
    if saw_unknown:
        return None
    return False


def _is_run_record_fresh(run: Run) -> bool:
    freshness_window = _running_record_freshness_seconds()
    if freshness_window <= 0:
        return True
    reference = run.updated_at or run.started_at or run.created_at
    if reference is None:
        return False
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - reference <= timedelta(seconds=freshness_window)


def _collect_agent_run_stats(
    db: Session, agent_infos: list[AgentInfo]
) -> dict[str, dict[str, Any]]:
    if not agent_infos:
        return {}

    alias_to_host: dict[str, str] = {}
    snapshots: dict[str, dict[str, Any]] = {
        info.host: {"current_run_total": 0, "recent_error": None}
        for info in agent_infos
    }
    for info in agent_infos:
        alias_to_host[info.host] = info.host
        if info.ip:
            alias_to_host[info.ip] = info.host

    def resolve_alias(raw_value: str) -> Optional[str]:
        normalized = _normalize_text(raw_value)
        if not normalized:
            return None
        if normalized in alias_to_host:
            return alias_to_host[normalized]
        host_part = normalized.split(":", 1)[0].strip()
        return alias_to_host.get(host_part)

    status_cache: dict[tuple[str, str], Optional[dict]] = {}
    active_runs = db.query(Run).filter(Run.run_status.in_(_ACTIVE_RUN_STATUSES)).all()
    for run in active_runs:
        mapped_hosts = {
            resolved
            for alias in _iter_run_agent_aliases(run.params)
            if (resolved := resolve_alias(alias)) is not None
        }
        if not mapped_hosts:
            continue
        live_decision = _live_run_busy_verdict(run, status_cache=status_cache)
        if live_decision is False:
            continue
        if live_decision is None and not _is_run_record_fresh(run):
            continue
        for host in mapped_hosts:
            snapshots[host]["current_run_total"] += 1

    # terminal-state reconciliation：近期刚进入 terminal 且 agent /status 仍报 running 的 run 也要计入占用，
    # 避免 agent 列表把仍在打流量的机器显示成 idle，让下一个 prepare 继续派到同一台 agent。
    recent_window = _agent_list_recent_terminal_window_seconds()
    terminal_candidates: list[Run] = []
    if recent_window > 0:
        now = datetime.now(timezone.utc)
        since = now - timedelta(seconds=recent_window)
        terminal_candidates.extend(
            db.query(Run)
            .filter(Run.run_status.in_(_TERMINAL_RUN_STATUSES))
            .filter(_recent_terminal_reference_at_or_after(since))
            .all()
        )
    false_failed_lookback = _false_failed_live_lookback_seconds()
    if false_failed_lookback > 0:
        since_false_failed = datetime.now(timezone.utc) - timedelta(
            seconds=false_failed_lookback
        )
        terminal_candidates.extend(
            db.query(Run)
            .filter(Run.run_status.in_(_TERMINAL_RUN_STATUSES))
            .filter(Run.stop_reason.like("poll_run_status_error:%"))
            .filter(
                func.coalesce(Run.created_at, Run.started_at, Run.ended_at)
                >= since_false_failed
            )
            .all()
        )

    seen_terminal_run_ids: set[int] = set()
    for run in terminal_candidates:
        run_id = getattr(run, "run_id", None)
        if not isinstance(run_id, int) or run_id in seen_terminal_run_ids:
            continue
        seen_terminal_run_ids.add(run_id)
        mapped_hosts = {
            resolved
            for alias in _iter_run_agent_aliases(run.params)
            if (resolved := resolve_alias(alias)) is not None
        }
        if not mapped_hosts:
            continue
        if _live_run_busy_verdict(run, status_cache=status_cache) is True:
            for host in mapped_hosts:
                snapshots[host]["current_run_total"] += 1

    recent_terminal_runs = (
        db.query(Run)
        .filter(Run.run_status.in_(_TERMINAL_RUN_STATUSES))
        .order_by(Run.run_id.desc())
        .limit(500)
        .all()
    )
    latest_terminal_seen_hosts: set[str] = set()
    for run in recent_terminal_runs:
        mapped_hosts = {
            resolved
            for alias in _iter_run_agent_aliases(run.params)
            if (resolved := resolve_alias(alias)) is not None
        }
        if not mapped_hosts:
            continue
        recent_error = None
        if run.run_status == RunStatus.FAILED:
            recent_error = _normalize_text(run.stop_reason) or _normalize_text(
                run.run_status_detail
            )
        for host in mapped_hosts:
            if host in latest_terminal_seen_hosts:
                continue
            latest_terminal_seen_hosts.add(host)
            if recent_error and snapshots[host]["recent_error"] is None:
                snapshots[host]["recent_error"] = recent_error

    return snapshots


def _build_agent_alias_index(
    agent_infos: list[AgentInfo],
) -> tuple[dict[str, str], dict[str, AgentInfo]]:
    alias_to_host: dict[str, str] = {}
    host_to_info: dict[str, AgentInfo] = {}
    for info in agent_infos:
        host_to_info[info.host] = info
        alias_to_host[info.host] = info.host
        if info.ip:
            alias_to_host[info.ip] = info.host
    return alias_to_host, host_to_info


def _resolve_run_agent_hosts(run: Run, alias_to_host: dict[str, str]) -> set[str]:
    resolved_hosts: set[str] = set()
    for alias in _iter_run_agent_aliases(run.params):
        normalized = _normalize_text(alias)
        if not normalized:
            continue
        resolved = alias_to_host.get(normalized)
        if resolved is None:
            host_part = normalized.split(":", 1)[0].strip()
            resolved = alias_to_host.get(host_part)
        if resolved:
            resolved_hosts.add(resolved)
    return resolved_hosts


def _all_resolved_hosts_idle(
    mapped_hosts: set[str], host_to_info: dict[str, AgentInfo]
) -> bool:
    if not mapped_hosts:
        return False
    resolved_infos = [host_to_info.get(host) for host in mapped_hosts]
    if any(info is None for info in resolved_infos):
        return False
    return all(
        info is not None
        and info.availability_state == "idle"
        and info.current_run_total <= 0
        for info in resolved_infos
    )


def _is_live_stuck_run(
    run: Run,
    *,
    alias_to_host: dict[str, str],
    host_to_info: dict[str, AgentInfo],
    status_cache: dict[tuple[str, str], Optional[dict]],
) -> bool:
    live_decision = _live_run_busy_verdict(run, status_cache=status_cache)
    if live_decision is True:
        return True
    if live_decision is False:
        return False
    if _is_run_record_fresh(run):
        return True

    mapped_hosts = _resolve_run_agent_hosts(run, alias_to_host)
    if not _iter_agent_run_contexts(run.params):
        return False
    if _all_resolved_hosts_idle(mapped_hosts, host_to_info):
        return False
    return bool(mapped_hosts)


def _derive_availability_state(info: AgentInfo) -> str:
    metadata = info.metadata or {}
    explicit_state = _normalize_text(
        metadata.get("availability_state") or metadata.get("state")
    )
    if explicit_state in {"idle", "busy", "draining", "disabled", "unhealthy"}:
        return explicit_state
    if _normalize_bool(metadata.get("disabled")):
        return "disabled"
    if _normalize_bool(metadata.get("draining")):
        return "draining"
    if not info.healthy:
        return "unhealthy"
    if info.current_run_total > 0:
        return "busy"
    return "idle"


def _enrich_agent_info(info: AgentInfo, snapshot: dict[str, Any]) -> AgentInfo:
    info.current_run_total = int(snapshot.get("current_run_total") or 0)
    info.max_concurrency = _normalize_positive_int(
        (info.metadata or {}).get("max_concurrency")
        or (info.metadata or {}).get("agent_max_concurrency")
    )
    if info.max_concurrency:
        info.current_load_ratio = round(
            info.current_run_total / info.max_concurrency, 4
        )
    else:
        info.current_load_ratio = None
    if not info.runtime_kind:
        info.runtime_kind = _normalize_text((info.metadata or {}).get("runtime_kind"))
    info.availability_state = _derive_availability_state(info)
    info.last_error = info.probe_error or _normalize_text(snapshot.get("recent_error"))
    return info


async def _load_agent_infos(db: Session) -> list[AgentInfo]:
    agents = await _orchestrator.discover_agents()
    hosts = list({a.host for a in agents})
    nacos_client = _safe_get_nacos_client()
    has_real_discovery = bool(
        nacos_client and getattr(nacos_client, "client", None) is not None
    )
    if not hosts and not has_real_discovery:
        hosts = _fallback_hosts()
    results = await asyncio.gather(*[_probe_health(h) for h in hosts])
    snapshots = _collect_agent_run_stats(db, results)
    results = [
        _enrich_agent_info(item, snapshots.get(item.host, {})) for item in results
    ]
    state_order = {"busy": 0, "idle": 1, "draining": 2, "disabled": 3, "unhealthy": 4}
    return sorted(
        results,
        key=lambda a: (
            not a.healthy,
            state_order.get(a.availability_state, 99),
            a.host,
        ),
    )


def _as_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _build_agent_ops_summary(
    db: Session, agent_infos: list[AgentInfo]
) -> AgentOpsSummary:
    now = datetime.now(timezone.utc)
    try:
        run_stuck_seconds = max(
            60,
            int(str(os.getenv("AGENT_OPS_RUN_STUCK_SECONDS", "900")).strip()),
        )
    except (TypeError, ValueError):
        run_stuck_seconds = 900

    active_runs = (
        db.query(Run)
        .filter(Run.run_status.in_([RunStatus.PREPARING, RunStatus.RUNNING]))
        .all()
    )
    alias_to_host, host_to_info = _build_agent_alias_index(agent_infos)
    status_cache: dict[tuple[str, str], Optional[dict]] = {}
    stuck_runs = []
    for run in active_runs:
        started_at = _as_utc(run.started_at) or _as_utc(run.created_at)
        if started_at is None:
            continue
        if (now - started_at).total_seconds() >= run_stuck_seconds:
            if _is_live_stuck_run(
                run,
                alias_to_host=alias_to_host,
                host_to_info=host_to_info,
                status_cache=status_cache,
            ):
                stuck_runs.append(run)

    unhealthy_agents = [
        item
        for item in agent_infos
        if not item.healthy or item.availability_state == "unhealthy"
    ]

    failed_reports = (
        db.query(Report)
        .filter(Report.status == ReportStatus.FAILED)
        .order_by(Report.id.desc())
        .limit(50)
        .all()
    )

    items = [
        AgentOpsAlertItem(
            key="run_stuck",
            title="卡住的 Run",
            severity="critical" if stuck_runs else "ok",
            count=len(stuck_runs),
            summary=(
                f"{len(stuck_runs)} 条运行中的 Run 已超过 {run_stuck_seconds}s"
                if stuck_runs
                else "当前没有超过阈值的 preparing/running Run"
            ),
            detail=(
                " / ".join(
                    f"run #{item.run_id} ({item.run_status})" for item in stuck_runs[:3]
                )
                if stuck_runs
                else None
            ),
            sample_run_ids=[int(item.run_id) for item in stuck_runs[:3]],
        ),
        AgentOpsAlertItem(
            key="agent_unhealthy",
            title="异常 Agent",
            severity="critical" if unhealthy_agents else "ok",
            count=len(unhealthy_agents),
            summary=(
                f"{len(unhealthy_agents)} 台 Agent 当前不可健康接单"
                if unhealthy_agents
                else "当前没有 unhealthy Agent"
            ),
            detail=(
                " / ".join(item.host for item in unhealthy_agents[:3])
                if unhealthy_agents
                else None
            ),
            sample_hosts=[item.host for item in unhealthy_agents[:3]],
        ),
        AgentOpsAlertItem(
            key="report_generation_failed",
            title="失败报告",
            severity="warning" if failed_reports else "ok",
            count=len(failed_reports),
            summary=(
                f"{len(failed_reports)} 条报告生成失败记录待复核"
                if failed_reports
                else "当前没有 FAILED 状态的报告"
            ),
            detail=(
                " / ".join(
                    f"report #{item.id} (run #{item.run_id or '-'})"
                    for item in failed_reports[:3]
                )
                if failed_reports
                else None
            ),
            sample_report_ids=[int(item.id) for item in failed_reports[:3]],
        ),
    ]

    return AgentOpsSummary(
        has_alerts=any(item.severity != "ok" for item in items),
        critical_total=sum(1 for item in items if item.severity == "critical"),
        warning_total=sum(1 for item in items if item.severity == "warning"),
        items=items,
    )


@router.get(
    "/agents",
    response_model=AgentListResponse,
    response_model_by_alias=True,
    summary="获取已发现的 agent 列表及健康状态",
)
async def list_agents(
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("agent", "view")),
):
    """发现所有 agent 并并发探测其 /health 接口"""
    del actor
    return ApiResponse.success(await _load_agent_infos(db))


@router.get(
    "/agents/ops-summary",
    response_model=AgentOpsSummaryResponse,
    response_model_by_alias=True,
    summary="获取 Agent 管理台最小值守告警摘要",
)
async def get_agent_ops_summary(
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("agent", "view")),
):
    del actor
    agent_infos = await _load_agent_infos(db)
    return ApiResponse.success(_build_agent_ops_summary(db, agent_infos))


@router.post(
    "/agents/stop-active-runs",
    response_model=ApiResponse[AgentBulkStopResponse],
    response_model_by_alias=True,
    summary="停止全部或指定环境的 active runs",
)
def stop_active_runs(
    body: AgentBulkStopRequest,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("agent", "stop")),
):
    service = RunService(db)
    target_envs = RunService._normalize_env_filters(body.envs)
    stop_result = service.stop_active_runs_bulk(
        reason=body.reason,
        envs=target_envs,
        user_id=actor.user_id,
    )
    stopped_runs = stop_result.stopped_runs
    return ApiResponse.success(
        AgentBulkStopResponse(
            scope="envs" if target_envs else "all",
            target_envs=target_envs or [],
            matched_run_total=len(stopped_runs),
            stopped_run_ids=[int(run.run_id) for run in stopped_runs],
            remote_stop_summary=stop_result.remote_stop_summary,
        )
    )
