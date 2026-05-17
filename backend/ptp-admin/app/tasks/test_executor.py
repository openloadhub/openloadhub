"""
测试执行 Celery 任务

职责：
1. 选择可用 agent 并分发执行
2. 异步轮询 agent 状态（避免阻塞 worker）
3. 回写 Run 状态，触发报告生成
"""

import asyncio
import logging
import os
import time
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from celery import current_task
from celery.result import AsyncResult
import httpx

from app.core.agent_orchestrator import orchestrator
from app.core.celery_app import celery_app
from app.core.database import SessionLocal
from app.models.run import Run
from app.models.task import Task
from app.models.script import Script
from app.services.script_service import ScriptService
from app.services.task_asset_service import TaskAssetService
from app.services.task_service import TaskService
from common.models.enums import RunStatus
from common.schemas.run import (
    RunK6ControlRequest,
    RunK6ControlResponse,
    RunK6ControlTaskStatusResponse,
)

logger = logging.getLogger(__name__)

_POLL_RUN_STATUS_FORCE_SYNC_IO = False
_DEFAULT_RUN_ASYNC_BLOCKING = None

# Run 终态集合 / 可分发状态集合（模块级常量，供 except 分支引用）
_TERMINAL = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.STOPPED}
_DISPATCH_ALLOWED = {RunStatus.PREPARING}


@celery_app.task(
    bind=True,
    name="execute_run_k6_control_task",
    max_retries=0,
    track_started=True,
)
def execute_run_k6_control_task(
    self,
    run_id: int,
    request_payload: dict[str, Any],
    user_id: int | None = None,
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        from app.services.run_service import RunService

        service = RunService(db)
        request = RunK6ControlRequest(**request_payload)
        result = service.update_k6_control(run_id, request, user_id=user_id)
        return result.model_dump(mode="json")
    finally:
        db.close()


def build_run_k6_control_task_status(
    *,
    run_id: int,
    task_id: str,
) -> RunK6ControlTaskStatusResponse:
    task_result = AsyncResult(task_id, app=celery_app)
    state = str(task_result.state or "PENDING").strip().lower()
    completed = task_result.ready()
    result_payload = None
    error = None

    if completed:
        if task_result.successful():
            raw_result = task_result.result
            if isinstance(raw_result, RunK6ControlResponse):
                result_payload = raw_result
            elif isinstance(raw_result, dict):
                result_payload = RunK6ControlResponse.model_validate(raw_result)
        else:
            error = str(task_result.result)

    return RunK6ControlTaskStatusResponse(
        run_id=run_id,
        async_task_id=task_id,
        job_status=state,
        completed=completed,
        result=result_payload,
        error=error,
    )


_DEMO_MIXED_K6_RUNTIME_DEFAULTS = {
    "BASE_URL": "http://demo-target:8080",
    "GRPC_HOST": "demo-target:50051",
}
_DEMO_MIXED_K6_HOST_RUNTIME_DEFAULTS = {
    "BASE_URL": "http://127.0.0.1:18080",
    "GRPC_HOST": "127.0.0.1:50051",
}
_DEMO_MIXED_JMETER_RUNTIME_DEFAULTS = {
    "BASE_URL": "http://demo-target:8080",
    "target_host": "demo-target",
    "target_port": 18080,
    "GRPC_HOST": "demo-target",
    "GRPC_PORT": 50051,
}
_DEMO_MIXED_JMETER_HOST_RUNTIME_DEFAULTS = {
    "BASE_URL": "http://127.0.0.1:18080",
    "target_host": "127.0.0.1",
    "target_port": 18080,
    "GRPC_HOST": "127.0.0.1",
    "GRPC_PORT": 50051,
}

_RUN_PARAM_TO_PROPERTY_KEYS = {
    "request_count",
    "iterations",
    "target_tps",
    "fixed_tps",
    "base_url",
    "sleep_ms",
    "sleep_time",
    "scheduler_enabled",
    "scenario_mode",
}

_RUN_PARAM_CONTROL_KEYS = {
    "task_id",
    "script_id",
    "run_id",
    "run_mode",
    "thread_count",
    "num_threads",
    "duration_seconds",
    "duration",
    "ramp_up",
    "protocol",
    "properties",
    "pod_count",
    "pod_num",
    "pod_total",
    "script_path",
    "script_s3",
    "script_content",
    "script_file_name",
    "data_asset_manifest",
    "proto_asset_manifest",
}

_RUN_PARAM_META_KEYS = {
    "summary_metrics",
    "endpoint_trends",
    "checks",
    "k8s_pods",
    "pods",
    "pod_monitor_series",
    "engine_grafana_url",
    "pod_grafana_url",
    "related_monitors",
    "observability_queries",
    "query_templates",
    "logs",
    "metrics_s3",
    "checks_s3",
    "log_s3",
    "k8s_log_s3",
    "k8s_job",
}

_INLINE_POLL_CONTINUE_KEY = "__poll_run_status_continue__"


def _build_inline_poll_continue(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {_INLINE_POLL_CONTINUE_KEY: dict(kwargs)}


def _extract_inline_poll_continue(result: Any) -> Optional[dict[str, Any]]:
    if not isinstance(result, dict):
        return None
    next_kwargs = result.get(_INLINE_POLL_CONTINUE_KEY)
    if not isinstance(next_kwargs, dict):
        return None
    return dict(next_kwargs)


def _apply_task_inline(task, **kwargs):
    """在当前进程内保留 Celery task trace/request 语义地同步执行 task。"""
    parent_task_name = getattr(current_task, "name", None)
    parent_request = getattr(current_task, "request", None)
    logger.info(
        "Applying task inline: task=%s parent_task=%s parent_called_directly=%s",
        getattr(task, "name", repr(task)),
        parent_task_name,
        getattr(parent_request, "called_directly", None),
    )

    current_kwargs = dict(kwargs)
    while True:
        # throw=False 允许 eager 模式沿用 Celery 的 autoretry 流程，
        # 最终只在重试耗尽后返回 FAILURE。
        eager_result = task.apply(kwargs=current_kwargs, throw=False)
        if eager_result.failed():
            result = eager_result.result
            if isinstance(result, BaseException):
                raise result
            raise RuntimeError(
                f"Inline task {getattr(task, 'name', repr(task))} failed: {result!r}"
            )
        result = eager_result.result
        next_kwargs = (
            _extract_inline_poll_continue(result)
            if getattr(task, "name", None) == "poll_run_status"
            else None
        )
        if next_kwargs is None:
            return result
        current_kwargs = next_kwargs


def _run_async_blocking(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


_DEFAULT_RUN_ASYNC_BLOCKING = _run_async_blocking


def _format_sync_poll_exception(exc: Exception) -> str:
    try:
        text = str(exc).strip()
    except Exception:
        text = ""
    if text:
        return f"{type(exc).__name__}: {text}"
    return type(exc).__name__


def _enable_sync_poll_status_io(reason: Exception | str) -> None:
    global _POLL_RUN_STATUS_FORCE_SYNC_IO
    if _POLL_RUN_STATUS_FORCE_SYNC_IO:
        return
    _POLL_RUN_STATUS_FORCE_SYNC_IO = True
    detail = (
        _format_sync_poll_exception(reason)
        if isinstance(reason, Exception)
        else str(reason)
    )
    logger.warning(
        "poll_run_status switching worker process to sync agent I/O after async bridge failure: %s",
        detail,
    )


def _sync_poll_fallback_allowed() -> bool:
    if os.getenv("PTP_POLL_RUN_STATUS_FORCE_SYNC_FALLBACK", "0") == "1":
        return True
    if os.getenv("PTP_POLL_RUN_STATUS_DISABLE_SYNC_FALLBACK", "0") == "1":
        return False
    if _run_async_blocking is not _DEFAULT_RUN_ASYNC_BLOCKING:
        return False
    if os.getenv("TESTING", "0") != "1":
        return True
    return os.getenv("PTP_POLL_RUN_STATUS_ALLOW_SYNC_FALLBACK_IN_TESTING", "0") == "1"


def _prefer_sync_poll_agent_io() -> bool:
    if os.getenv("PTP_POLL_RUN_STATUS_FORCE_ASYNC_IO", "0") == "1":
        return False
    if os.getenv("TESTING", "0") == "1":
        return (
            os.getenv("PTP_POLL_RUN_STATUS_ALLOW_SYNC_FALLBACK_IN_TESTING", "0") == "1"
        )
    return True


def _fetch_run_status_sync_payload(
    agent_host: str, run_token: str
) -> dict[str, Any] | None:
    if os.getenv("TESTING", "0") == "1":
        return {"status": "succeeded", "jtl_summary": None, "k6_summary": None}

    url = f"http://{agent_host}/agent/runs/{run_token}/status"
    max_retries = max(1, int(os.getenv("AGENT_STATUS_FETCH_RETRIES", "3")))
    backoff_seconds = max(
        0.2, float(os.getenv("AGENT_STATUS_FETCH_BACKOFF_SECONDS", "0.5"))
    )

    for attempt in range(1, max_retries + 1):
        try:
            response = httpx.get(url, timeout=30.0, trust_env=False)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "sync fetch_run_status failed host=%s token=%s attempt=%d/%d error=%s",
                agent_host,
                run_token,
                attempt,
                max_retries,
                _format_sync_poll_exception(exc),
            )
            break
        except (httpx.RequestError, httpx.TimeoutException, ValueError) as exc:
            if attempt < max_retries:
                logger.info(
                    "sync fetch_run_status transient host=%s token=%s attempt=%d/%d error=%s",
                    agent_host,
                    run_token,
                    attempt,
                    max_retries,
                    _format_sync_poll_exception(exc),
                )
                time.sleep(backoff_seconds * attempt)
                continue
            logger.warning(
                "sync fetch_run_status failed host=%s token=%s attempt=%d/%d error=%s",
                agent_host,
                run_token,
                attempt,
                max_retries,
                _format_sync_poll_exception(exc),
            )
        except Exception as exc:
            logger.warning(
                "sync fetch_run_status unexpected host=%s token=%s error=%s",
                agent_host,
                run_token,
                _format_sync_poll_exception(exc),
            )
            break
    return None


def _stop_run_sync(agent_host: str, run_token: str) -> dict[str, Any] | None:
    if os.getenv("TESTING", "0") == "1":
        return {"status": "success", "message": "mocked stop success"}

    url = f"http://{agent_host}/agent/runs/{run_token}/stop"
    try:
        response = httpx.post(url, timeout=30.0, trust_env=False)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except Exception as exc:
        logger.warning(
            "sync stop_run failed for host=%s token=%s: %s",
            agent_host,
            run_token,
            _format_sync_poll_exception(exc),
        )
        return None


def _coerce_positive_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _coerce_optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


def _coerce_non_negative_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _coerce_ratio(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed > 1:
        parsed = parsed / 100.0
    return max(0.0, min(1.0, parsed))


def _coerce_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_avg_data_slice_properties(
    task_data: dict[str, Any],
) -> dict[str, Any]:
    properties = dict(task_data.get("properties") or {})
    distribution = str(task_data.get("data_distribution") or "").strip().lower()
    if distribution != "avg":
        return properties

    raw_total = (
        properties.get("pod_count")
        or properties.get("pod_num")
        or task_data.get("pod_count")
        or task_data.get("pod_num")
    )
    slice_total = _coerce_positive_int(raw_total) or 1
    slice_start = (
        _coerce_positive_int(
            properties.get("agent_slice_index") or properties.get("pod_index")
        )
        or 1
    )
    slice_start = min(slice_start, slice_total)

    properties["PTP_DATA_SPLIT_TYPE"] = "line"
    properties["PTP_DATA_SLICE_START"] = slice_start
    properties["PTP_DATA_SLICE_TOTAL"] = slice_total
    return properties


def _resolve_dispatch_agent_count(task_data: dict[str, Any]) -> int:
    raw_total = (
        task_data.get("pod_count")
        or task_data.get("pod_num")
        or (task_data.get("properties") or {}).get("pod_count")
        or (task_data.get("properties") or {}).get("pod_num")
    )
    return _coerce_positive_int(raw_total) or 1


def _resolve_launch_wave_size(task_data: dict[str, Any], agent_total: int) -> int:
    properties = task_data.get("properties") or {}
    raw_value = (
        properties.get("PTP_AGENT_LAUNCH_WAVE_SIZE")
        or properties.get("agent_launch_wave_size")
        or task_data.get("launch_wave_size")
        or os.getenv("PTP_AGENT_LAUNCH_WAVE_SIZE")
        or os.getenv("MIXED_RUN_AGENT_LAUNCH_WAVE_SIZE")
    )
    wave_size = _coerce_positive_int(raw_value) or 25
    return max(1, min(wave_size, max(1, agent_total)))


def _resolve_launch_wave_delay_seconds(task_data: dict[str, Any]) -> float:
    properties = task_data.get("properties") or {}
    raw_value = (
        properties.get("PTP_AGENT_LAUNCH_WAVE_DELAY_SECONDS")
        or properties.get("agent_launch_wave_delay_seconds")
        or task_data.get("launch_wave_delay_seconds")
        or os.getenv("PTP_AGENT_LAUNCH_WAVE_DELAY_SECONDS")
        or os.getenv("MIXED_RUN_AGENT_LAUNCH_WAVE_DELAY_SECONDS")
    )
    delay = _coerce_float(raw_value)
    return max(0.0, delay or 0.0)


def _build_agent_dispatch_payload(
    task_data: dict[str, Any],
    *,
    agent_host: str,
    agent_metadata: Optional[dict[str, Any]],
    agent_index: int,
    agent_total: int,
) -> dict[str, Any]:
    payload = dict(task_data)
    properties = dict(task_data.get("properties") or {})
    properties["agent_slice_index"] = agent_index
    properties["pod_index"] = agent_index
    payload["properties"] = properties
    payload["pod_count"] = agent_total
    payload["pod_num"] = agent_total
    payload["properties"] = _build_avg_data_slice_properties(payload)
    runtime_kind = ""
    if isinstance(agent_metadata, dict):
        runtime_kind = str(agent_metadata.get("runtime_kind") or "").strip().lower()
        if runtime_kind:
            payload["properties"]["agent_runtime_kind"] = runtime_kind
        normalized_metadata = {
            str(key): value
            for key, value in agent_metadata.items()
            if value not in (None, "")
        }
        if normalized_metadata:
            payload["properties"]["agent_metadata"] = normalized_metadata
        compose_service = str(agent_metadata.get("compose_service") or "").strip()
        if compose_service:
            payload["properties"]["pod_grafana_compose_service"] = compose_service
    payload["properties"] = _rewrite_demo_mixed_k6_targets_for_agent_runtime(
        payload["properties"], agent_host=agent_host
    )
    payload["properties"] = _rewrite_demo_mixed_jmeter_targets_for_agent_runtime(
        payload["properties"], agent_host=agent_host
    )
    return payload


def _normalize_agent_dispatch_result(
    agent,
    raw_result,
    *,
    agent_index: int,
    agent_total: int,
    launch_wave_index: int,
    launch_wave_size: int,
    launch_wave_total: int,
) -> dict[str, Any]:
    if isinstance(raw_result, Exception):
        return {
            "status": "error",
            "agent": agent.host,
            "agent_host": agent.host,
            "agent_metadata": dict(getattr(agent, "metadata", {}) or {}),
            "agent_index": agent_index,
            "agent_total": agent_total,
            "launch_wave_index": launch_wave_index,
            "launch_wave_size": launch_wave_size,
            "launch_wave_total": launch_wave_total,
            "error": str(raw_result),
        }
    result = dict(raw_result)
    result.setdefault("agent", agent.host)
    result.setdefault("agent_host", agent.host)
    result["agent_metadata"] = dict(getattr(agent, "metadata", {}) or {})
    result["agent_index"] = agent_index
    result["agent_total"] = agent_total
    result["launch_wave_index"] = launch_wave_index
    result["launch_wave_size"] = launch_wave_size
    result["launch_wave_total"] = launch_wave_total
    return result


def _build_launch_wave_summary(
    normalized_results: list[dict[str, Any]],
    *,
    agent_total: int,
    wave_size: int,
    wave_total: int,
) -> dict[str, Any]:
    launched_results = [
        item
        for item in normalized_results
        if item.get("status") == "success" and item.get("run_token")
    ]
    failed_results = [
        item
        for item in normalized_results
        if item.get("status") != "success" or not item.get("run_token")
    ]
    attempted_waves = sorted(
        {
            int(item["launch_wave_index"])
            for item in normalized_results
            if isinstance(item.get("launch_wave_index"), int)
        }
    )
    failed_waves = sorted(
        {
            int(item["launch_wave_index"])
            for item in failed_results
            if isinstance(item.get("launch_wave_index"), int)
        }
    )
    return {
        "agent_total": agent_total,
        "launch_wave_size": wave_size,
        "launch_wave_total": wave_total,
        "launch_waves_total": wave_total,
        "launch_waves_attempted": len(attempted_waves),
        "launch_waves_succeeded": len(set(attempted_waves) - set(failed_waves)),
        "launch_waves_failed": len(failed_waves),
        "failed_launch_wave_index": failed_waves[0] if failed_waves else None,
        "launched_agent_total": len(launched_results),
        "failed_agent_total": len(failed_results),
    }


async def _dispatch_agents_with_launch_waves(
    task_id: int,
    task_data: dict[str, Any],
    agents,
) -> list[dict[str, Any]]:
    agent_total = len(agents)
    wave_size = _resolve_launch_wave_size(task_data, agent_total)
    wave_delay_seconds = _resolve_launch_wave_delay_seconds(task_data)
    wave_total = math.ceil(agent_total / wave_size) if agent_total > 0 else 0
    indexed_agents = list(enumerate(agents, start=1))
    normalized_results: list[dict[str, Any]] = []

    for wave_start in range(0, agent_total, wave_size):
        wave = indexed_agents[wave_start : wave_start + wave_size]
        launch_wave_index = int(wave_start / wave_size) + 1
        dispatches = []
        for index, agent in wave:
            agent_metadata = dict(getattr(agent, "metadata", {}) or {})
            dispatches.append(
                orchestrator.execute_task(
                    task_id,
                    agent,
                    _build_agent_dispatch_payload(
                        task_data,
                        agent_host=agent.host,
                        agent_metadata=agent_metadata,
                        agent_index=index,
                        agent_total=agent_total,
                    ),
                )
            )
        raw_results = await asyncio.gather(*dispatches, return_exceptions=True)
        wave_results = [
            _normalize_agent_dispatch_result(
                agent,
                raw_result,
                agent_index=index,
                agent_total=agent_total,
                launch_wave_index=launch_wave_index,
                launch_wave_size=wave_size,
                launch_wave_total=wave_total,
            )
            for (index, agent), raw_result in zip(wave, raw_results)
        ]
        normalized_results.extend(wave_results)
        if any(
            item.get("status") != "success" or not item.get("run_token")
            for item in wave_results
        ):
            break
        if wave_delay_seconds > 0 and wave_start + wave_size < agent_total:
            await asyncio.sleep(wave_delay_seconds)

    return normalized_results


def _rewrite_demo_mixed_k6_targets_for_agent_runtime(
    properties: dict[str, Any],
    *,
    agent_host: str,
) -> dict[str, Any]:
    if not isinstance(properties, dict) or not properties:
        return properties
    runtime_kind = str(properties.get("agent_runtime_kind") or "").strip().lower()
    if runtime_kind != "host":
        return properties

    if properties.get("BASE_URL") != _DEMO_MIXED_K6_RUNTIME_DEFAULTS["BASE_URL"]:
        return properties
    if properties.get("GRPC_HOST") != _DEMO_MIXED_K6_RUNTIME_DEFAULTS["GRPC_HOST"]:
        return properties

    rewritten = dict(properties)
    rewritten.update(_DEMO_MIXED_K6_HOST_RUNTIME_DEFAULTS)
    return rewritten


def _rewrite_demo_mixed_jmeter_targets_for_agent_runtime(
    properties: dict[str, Any],
    *,
    agent_host: str,
) -> dict[str, Any]:
    if not isinstance(properties, dict) or not properties:
        return properties
    runtime_kind = str(properties.get("agent_runtime_kind") or "").strip().lower()
    if runtime_kind != "host":
        return properties

    grpc_host = properties.get("GRPC_HOST")
    if grpc_host == "demo-target:50051":
        grpc_host = "demo-target"

    target_port = _coerce_positive_int(properties.get("target_port"))
    grpc_port = _coerce_positive_int(properties.get("GRPC_PORT"))

    if (
        properties.get("target_host")
        != _DEMO_MIXED_JMETER_RUNTIME_DEFAULTS["target_host"]
    ):
        return properties
    if target_port != _DEMO_MIXED_JMETER_RUNTIME_DEFAULTS["target_port"]:
        return properties
    if properties.get("BASE_URL") not in {
        None,
        _DEMO_MIXED_JMETER_RUNTIME_DEFAULTS["BASE_URL"],
    }:
        return properties
    if grpc_host != _DEMO_MIXED_JMETER_RUNTIME_DEFAULTS["GRPC_HOST"]:
        return properties
    if grpc_port != _DEMO_MIXED_JMETER_RUNTIME_DEFAULTS["GRPC_PORT"]:
        return properties

    rewritten = dict(properties)
    rewritten.update(_DEMO_MIXED_JMETER_HOST_RUNTIME_DEFAULTS)
    return rewritten


def _build_agent_run_entry(
    result: dict[str, Any],
    *,
    agent_index: int,
    agent_total: int,
) -> dict[str, Any]:
    entry = {
        "agent_host": result.get("agent_host") or result.get("agent"),
        "agent_run_token": result.get("run_token"),
        "agent_index": agent_index,
        "agent_total": agent_total,
    }
    for key in ("launch_wave_index", "launch_wave_size", "launch_wave_total"):
        value = result.get(key)
        if isinstance(value, int):
            entry[key] = value
    metadata = result.get("agent_metadata")
    if isinstance(metadata, dict) and metadata:
        entry["agent_metadata"] = metadata
        runtime_kind = str(metadata.get("runtime_kind") or "").strip()
        if runtime_kind:
            entry["agent_runtime_kind"] = runtime_kind
        compose_service = str(metadata.get("compose_service") or "").strip()
        if compose_service:
            entry["pod_grafana_compose_service"] = compose_service
    if result.get("k8s_job"):
        entry["k8s_job"] = result.get("k8s_job")
    return entry


def _merge_pod_monitor_series_payloads(
    payloads: list[dict[str, Any]],
) -> Optional[list[dict[str, Any]]]:
    merged: list[dict[str, Any]] = []
    for payload in payloads:
        series = payload.get("pod_monitor_series")
        if not isinstance(series, list):
            continue
        agent_host = payload.get("agent_host")
        for item in series:
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            if agent_host and not normalized.get("agent_host"):
                normalized["agent_host"] = agent_host
            merged.append(normalized)
    return merged or None


def _aggregate_execution_summary_items(
    summary_items: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if not summary_items:
        return None

    total_requests = 0
    successful_requests = 0
    failed_requests = 0
    throughput_total = 0.0
    weighted_avg_total = 0.0
    weighted_avg_weight = 0
    p95_candidates: list[float] = []
    p99_candidates: list[float] = []
    max_candidates: list[float] = []
    min_candidates: list[float] = []

    for item in summary_items:
        total = _coerce_positive_int(item.get("total_requests")) or 0
        succeeded = _coerce_non_negative_int(item.get("successful_requests")) or 0
        failed = _coerce_non_negative_int(item.get("failed_requests")) or 0
        if total == 0 and succeeded + failed > 0:
            total = succeeded + failed

        total_requests += total
        successful_requests += succeeded
        failed_requests += failed

        throughput_value = _coerce_float(item.get("throughput", item.get("http_reqs")))
        if throughput_value is not None:
            throughput_total += throughput_value

        avg_value = _coerce_float(item.get("rt_avg_ms", item.get("avg_response_time")))
        weight = total or succeeded
        if avg_value is not None and weight > 0:
            weighted_avg_total += avg_value * weight
            weighted_avg_weight += weight

        p95_value = _coerce_float(item.get("rt_p95_ms", item.get("p95_response_time")))
        if p95_value is not None:
            p95_candidates.append(p95_value)

        p99_value = _coerce_float(item.get("rt_p99_ms", item.get("p99_response_time")))
        if p99_value is not None:
            p99_candidates.append(p99_value)

        max_value = _coerce_float(item.get("rt_max_ms", item.get("max_response_time")))
        if max_value is not None:
            max_candidates.append(max_value)

        min_value = _coerce_float(item.get("rt_min_ms", item.get("min_response_time")))
        if min_value is not None:
            min_candidates.append(min_value)

    if total_requests == 0 and failed_requests:
        total_requests = successful_requests + failed_requests

    aggregated: dict[str, Any] = {
        "total_requests": total_requests or None,
        "successful_requests": successful_requests,
        "failed_requests": failed_requests,
        "throughput": round(throughput_total, 4) if throughput_total else None,
        "rt_avg_ms": (
            round(weighted_avg_total / weighted_avg_weight, 4)
            if weighted_avg_weight
            else None
        ),
        "rt_p95_ms": max(p95_candidates) if p95_candidates else None,
        "rt_p99_ms": max(p99_candidates) if p99_candidates else None,
        "rt_max_ms": max(max_candidates) if max_candidates else None,
        "rt_min_ms": min(min_candidates) if min_candidates else None,
    }
    if total_requests > 0:
        aggregated["error_rate"] = round(failed_requests / total_requests, 6)
        aggregated["success_rate"] = round(successful_requests / total_requests, 6)
    return aggregated


def _aggregate_execution_summaries(
    status_entries: list[dict[str, Any]],
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    jtl_summaries = [
        entry["jtl_summary"]
        for entry in status_entries
        if isinstance(entry.get("jtl_summary"), dict)
    ]
    k6_summaries = [
        entry["k6_summary"]
        for entry in status_entries
        if isinstance(entry.get("k6_summary"), dict)
    ]
    return (
        _aggregate_execution_summary_items(jtl_summaries),
        _aggregate_execution_summary_items(k6_summaries),
    )


def _merge_summary_metric_rows_from_status_entries(
    status_entries: list[dict[str, Any]],
) -> Optional[list[dict[str, Any]]]:
    merged: dict[str, dict[str, Any]] = {}

    for entry in status_entries:
        status_payload = entry.get("status_payload")
        if not isinstance(status_payload, dict):
            continue
        for summary_key in ("jtl_summary", "k6_summary"):
            summary = status_payload.get(summary_key)
            if not isinstance(summary, dict):
                continue
            rows = summary.get("endpoint_metrics")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                endpoint_name = str(
                    row.get("endpoint_name") or row.get("name") or ""
                ).strip()
                if not endpoint_name:
                    continue
                bucket = merged.setdefault(
                    endpoint_name,
                    {
                        "endpoint_name": endpoint_name,
                        "_avg_weight_total": 0.0,
                        "_avg_weight_count": 0.0,
                    },
                )
                total_requests = _coerce_non_negative_int(row.get("total_requests"))
                throughput = _coerce_float(row.get("throughput"))
                avg_rt_ms = _coerce_float(row.get("avg_rt_ms"))
                p95_rt_ms = _coerce_float(row.get("p95_rt_ms"))
                p99_rt_ms = _coerce_float(row.get("p99_rt_ms"))
                max_rt_ms = _coerce_float(row.get("max_rt_ms"))
                min_rt_ms = _coerce_float(row.get("min_rt_ms"))

                if total_requests is not None:
                    bucket["total_requests"] = (
                        int(bucket.get("total_requests") or 0) + total_requests
                    )
                if throughput is not None:
                    bucket["throughput"] = round(
                        float(bucket.get("throughput") or 0.0) + throughput, 4
                    )
                if avg_rt_ms is not None:
                    weight = float(total_requests or 1)
                    bucket["_avg_weight_total"] += avg_rt_ms * weight
                    bucket["_avg_weight_count"] += weight
                    bucket["avg_rt_ms"] = (
                        bucket["_avg_weight_total"] / bucket["_avg_weight_count"]
                    )
                if p95_rt_ms is not None:
                    bucket["p95_rt_ms"] = max(
                        float(bucket.get("p95_rt_ms") or p95_rt_ms), p95_rt_ms
                    )
                if p99_rt_ms is not None:
                    bucket["p99_rt_ms"] = max(
                        float(bucket.get("p99_rt_ms") or p99_rt_ms), p99_rt_ms
                    )
                if max_rt_ms is not None:
                    bucket["max_rt_ms"] = max(
                        float(bucket.get("max_rt_ms") or max_rt_ms), max_rt_ms
                    )
                if min_rt_ms is not None:
                    current_min = bucket.get("min_rt_ms")
                    bucket["min_rt_ms"] = (
                        min(min_rt_ms, float(current_min))
                        if current_min is not None
                        else min_rt_ms
                    )

    if not merged:
        return None

    rows: list[dict[str, Any]] = []
    for endpoint_name in sorted(merged):
        payload = dict(merged[endpoint_name])
        payload.pop("_avg_weight_total", None)
        payload.pop("_avg_weight_count", None)
        rows.append({key: value for key, value in payload.items() if value is not None})
    return rows or None


def _merge_check_rows_from_status_entries(
    status_entries: list[dict[str, Any]],
) -> Optional[list[dict[str, Any]]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}

    for entry in status_entries:
        status_payload = entry.get("status_payload")
        if not isinstance(status_payload, dict):
            continue
        for summary_key in ("jtl_summary", "k6_summary"):
            summary = status_payload.get(summary_key)
            if not isinstance(summary, dict):
                continue
            rows = summary.get("checks")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                group_name = (
                    str(row.get("group_name") or "default").strip() or "default"
                )
                check_name = str(row.get("check_name") or "").strip()
                success_rate = _coerce_ratio(row.get("success_rate"))
                if not check_name or success_rate is None:
                    continue
                key = (group_name, check_name)
                bucket = merged.setdefault(
                    key,
                    {
                        "group_name": group_name,
                        "check_name": check_name,
                        "_rate_total": 0.0,
                        "_rate_count": 0,
                    },
                )
                bucket["_rate_total"] += success_rate
                bucket["_rate_count"] += 1
                bucket["success_rate"] = bucket["_rate_total"] / bucket["_rate_count"]

    if not merged:
        return None

    rows: list[dict[str, Any]] = []
    for key in sorted(merged):
        payload = dict(merged[key])
        payload.pop("_rate_total", None)
        payload.pop("_rate_count", None)
        rows.append(payload)
    return rows or None


def _merge_endpoint_trend_rows_from_status_entries(
    status_entries: list[dict[str, Any]],
) -> Optional[list[dict[str, Any]]]:
    merged: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    for entry in status_entries:
        status_payload = entry.get("status_payload")
        if not isinstance(status_payload, dict):
            continue
        for summary_key in ("jtl_summary", "k6_summary"):
            summary = status_payload.get(summary_key)
            if not isinstance(summary, dict):
                continue
            rows = summary.get("endpoint_trends")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                endpoint_name = str(row.get("endpoint_name") or "").strip()
                metric = str(row.get("metric") or "").strip()
                unit = str(row.get("unit") or "").strip()
                points = row.get("points")
                if not endpoint_name or not metric or not isinstance(points, list):
                    continue
                key = (endpoint_name, metric, unit)
                bucket = merged.setdefault(key, [])
                for point in points:
                    if isinstance(point, dict) and point.get("ts"):
                        bucket.append(
                            {
                                "ts": point.get("ts"),
                                "value": point.get("value"),
                            }
                        )

    if not merged:
        return None

    rows: list[dict[str, Any]] = []
    for endpoint_name, metric, unit in sorted(merged):
        seen_points: set[tuple[str, str]] = set()
        points: list[dict[str, Any]] = []
        for point in merged[(endpoint_name, metric, unit)]:
            dedupe_key = (str(point.get("ts") or ""), str(point.get("value")))
            if dedupe_key in seen_points:
                continue
            seen_points.add(dedupe_key)
            points.append(point)
        points.sort(key=lambda item: str(item.get("ts") or ""))
        if points:
            rows.append(
                {
                    "endpoint_name": endpoint_name,
                    "metric": metric,
                    "unit": unit,
                    "points": points,
                }
            )
    return rows or None


def _normalize_agent_run_contexts(
    *,
    agent_runs: Optional[list[dict[str, Any]]],
    agent_host: Optional[str],
    run_token: Optional[str],
    fallback_k8s_job: Any = None,
) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    if isinstance(agent_runs, list):
        for item in agent_runs:
            if not isinstance(item, dict):
                continue
            host = item.get("agent_host") or item.get("agent_ip")
            token = (
                item.get("agent_run_token")
                or item.get("agent_token")
                or item.get("agent_session")
                or item.get("run_token")
            )
            if not isinstance(host, str) or not host:
                continue
            if not isinstance(token, str) or not token:
                continue
            ctx_key = (host, token)
            if ctx_key in seen:
                continue
            seen.add(ctx_key)
            normalized = dict(item)
            normalized["agent_host"] = host
            normalized["agent_run_token"] = token
            contexts.append(normalized)

    if (
        isinstance(agent_host, str)
        and agent_host
        and isinstance(run_token, str)
        and run_token
    ):
        ctx_key = (agent_host, run_token)
        if ctx_key not in seen:
            fallback_context: dict[str, Any] = {
                "agent_host": agent_host,
                "agent_run_token": run_token,
            }
            if fallback_k8s_job:
                fallback_context["k8s_job"] = fallback_k8s_job
            contexts.append(fallback_context)
    return contexts


def _cleanup_k8s_jobs(contexts: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for item in contexts:
        meta = item.get("k8s_job")
        if not isinstance(meta, dict):
            continue
        job_name = str(meta.get("job_name") or "")
        namespace = str(meta.get("namespace") or "")
        dedupe_key = f"{namespace}:{job_name}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        orchestrator.cleanup_k8s_job(meta)


def _upsert_context_field(
    merged_params: dict[str, Any],
    key: str,
    status_entries: list[dict[str, Any]],
) -> None:
    for entry in status_entries:
        value = entry.get(key)
        if value:
            merged_params[key] = value
            return


def _merge_raw_observability_from_status_entries(
    status_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for entry in status_entries:
        raw_observability = entry.get("raw_observability")
        if not isinstance(raw_observability, dict):
            continue
        for key, value in raw_observability.items():
            if not isinstance(value, dict) or not value:
                continue
            annotated = dict(value)
            annotated.setdefault("agent_host", entry.get("agent_host"))
            annotated.setdefault("agent_run_token", entry.get("run_token"))
            existing = merged.get(key)
            if not isinstance(existing, dict):
                merged[key] = annotated
                continue
            if (
                existing.get("status") != "failed"
                and annotated.get("status") == "failed"
            ):
                merged[key] = annotated
    return merged


def _merge_run_params_from_status_entries(
    run: Run,
    *,
    status_entries: list[dict[str, Any]],
    fallback_agent_host: str,
    fallback_run_token: str,
) -> tuple[
    dict[str, Any], Optional[dict[str, Any]], Optional[dict[str, Any]], dict[str, Any]
]:
    primary_entry = status_entries[0] if status_entries else {}
    merged_params = dict(run.params or {})
    merged_params["agent_host"] = primary_entry.get("agent_host") or fallback_agent_host
    merged_params["agent_run_token"] = (
        primary_entry.get("run_token") or fallback_run_token
    )

    agent_metadata = primary_entry.get("agent_metadata")
    if isinstance(agent_metadata, dict) and agent_metadata:
        merged_params["agent_metadata"] = agent_metadata

    merged_params["agent_runs"] = [
        {
            key: value
            for key, value in {
                "agent_host": entry.get("agent_host"),
                "agent_run_token": entry.get("run_token"),
                "agent_index": entry.get("agent_index"),
                "agent_total": entry.get("agent_total"),
                "status": entry.get("status_value"),
                "error": entry.get("error_detail"),
                "agent_ip": entry.get("agent_ip"),
                "log_s3": entry.get("log_s3"),
                "metrics_s3": entry.get("metrics_s3"),
                "k8s_job": entry.get("k8s_job"),
                "k8s_log_s3": entry.get("k8s_log_s3"),
                "ended_at": (entry.get("status_payload") or {}).get("ended_at"),
            }.items()
            if value not in (None, "")
        }
        for entry in status_entries
    ]
    _upsert_context_field(merged_params, "agent_ip", status_entries)
    _upsert_context_field(merged_params, "agent_runtime_kind", status_entries)
    _upsert_context_field(merged_params, "pod_grafana_compose_service", status_entries)
    _upsert_context_field(merged_params, "log_s3", status_entries)
    _upsert_context_field(merged_params, "metrics_s3", status_entries)
    _upsert_context_field(merged_params, "k8s_log_tail", status_entries)
    _upsert_context_field(merged_params, "k8s_log_s3", status_entries)
    _upsert_context_field(merged_params, "k8s_events", status_entries)
    raw_observability = _merge_raw_observability_from_status_entries(status_entries)
    if raw_observability:
        merged_params["raw_observability"] = raw_observability

    aggregated_pod_monitor_series = _merge_pod_monitor_series_payloads(status_entries)
    if aggregated_pod_monitor_series:
        merged_params["pod_monitor_series"] = aggregated_pod_monitor_series

    aggregated_jtl_summary, aggregated_k6_summary = _aggregate_execution_summaries(
        status_entries
    )
    if aggregated_jtl_summary:
        merged_params["jtl_summary"] = aggregated_jtl_summary
    if aggregated_k6_summary:
        merged_params["k6_summary"] = aggregated_k6_summary

    merged_summary_metrics = _merge_summary_metric_rows_from_status_entries(
        status_entries
    )
    if merged_summary_metrics:
        merged_params["summary_metrics"] = merged_summary_metrics
    merged_checks = _merge_check_rows_from_status_entries(status_entries)
    if merged_checks:
        merged_params["checks"] = merged_checks
    merged_endpoint_trends = _merge_endpoint_trend_rows_from_status_entries(
        status_entries
    )
    if merged_endpoint_trends:
        merged_params["endpoint_trends"] = merged_endpoint_trends

    return merged_params, aggregated_jtl_summary, aggregated_k6_summary, primary_entry


def _gather_run_status_payloads(contexts: list[dict[str, Any]]) -> list[Any]:
    if not contexts:
        return []

    if _prefer_sync_poll_agent_io() or (
        _POLL_RUN_STATUS_FORCE_SYNC_IO and _sync_poll_fallback_allowed()
    ):
        return [
            _fetch_run_status_sync_payload(item["agent_host"], item["agent_run_token"])
            for item in contexts
        ]

    async def _gather_all() -> list[Any]:
        tasks = [
            orchestrator.fetch_run_status(item["agent_host"], item["agent_run_token"])
            for item in contexts
        ]
        return await asyncio.gather(*tasks, return_exceptions=True)

    try:
        raw_results = _run_async_blocking(_gather_all())
    except Exception as exc:
        if not _sync_poll_fallback_allowed():
            raise
        _enable_sync_poll_status_io(exc)
        return [
            _fetch_run_status_sync_payload(item["agent_host"], item["agent_run_token"])
            for item in contexts
        ]
    if len(contexts) == 1 and isinstance(raw_results, dict):
        return [raw_results]
    if not isinstance(raw_results, list) or len(raw_results) != len(contexts):
        if not _sync_poll_fallback_allowed():
            return [None for _ in contexts]
        _enable_sync_poll_status_io("malformed async poll payload shape")
        return [
            _fetch_run_status_sync_payload(item["agent_host"], item["agent_run_token"])
            for item in contexts
        ]
    return raw_results


def _build_context_status_entries(
    contexts: list[dict[str, Any]],
    *,
    fallback_k8s_job: Any = None,
) -> list[dict[str, Any]]:
    """Fetch agent /status for each dispatched agent context.

    Even for a single context we keep one `asyncio.gather` inside one
    `_run_async_blocking` call so poll ticks never fall back to the old
    `_run_async_blocking(fetch_run_status(...))` bridge that triggered
    the cancel-scope recursion on terminal-state reconciliation.

    Unit tests historically monkeypatch `_run_async_blocking` to return a
    plain dict for single-context probes, so we still normalize that
    compatibility stub shape to a one-item list here.
    """

    entries: list[dict[str, Any]] = []
    if not contexts:
        return entries

    def _build_entry(item: dict[str, Any], payload: Any) -> dict[str, Any]:
        return {
            "agent_host": item["agent_host"],
            "run_token": item["agent_run_token"],
            "status_payload": payload if isinstance(payload, dict) else None,
            "k8s_job": item.get("k8s_job") or fallback_k8s_job,
            "agent_index": item.get("agent_index"),
            "agent_total": item.get("agent_total"),
            "agent_metadata": item.get("agent_metadata"),
            "agent_runtime_kind": item.get("agent_runtime_kind"),
            "pod_grafana_compose_service": item.get("pod_grafana_compose_service"),
        }

    try:
        raw_results = _gather_run_status_payloads(contexts)
    except Exception as exc:
        logger.warning(
            "fetch_run_status gather failed for %d contexts: %s",
            len(contexts),
            exc,
        )
        raise

    for item, raw in zip(contexts, raw_results):
        if isinstance(raw, Exception):
            logger.info(
                "fetch_run_status context failed host=%s token=%s error=%s",
                item.get("agent_host"),
                item.get("agent_run_token"),
                raw,
            )
            entries.append(_build_entry(item, None))
            continue
        entries.append(_build_entry(item, raw))
    return entries


def _aggregate_poll_status(
    status_entries: list[dict[str, Any]],
) -> tuple[Optional[str], Optional[str]]:
    saw_running = False
    saw_unknown = False
    saw_failed = False
    saw_stopped = False
    saw_succeeded = False
    error_detail: Optional[str] = None

    for entry in status_entries:
        status_value = entry.get("status_value")
        if not error_detail and entry.get("error_detail"):
            error_detail = entry["error_detail"]
        if status_value == "failed":
            saw_failed = True
        elif status_value == "stopped":
            saw_stopped = True
        elif status_value == "succeeded":
            saw_succeeded = True
        elif status_value == "running":
            saw_running = True
        else:
            saw_unknown = True

    if saw_failed:
        return "failed", error_detail
    if saw_running or saw_unknown:
        return "running", error_detail
    if saw_stopped:
        return "stopped", error_detail
    if saw_succeeded:
        return "succeeded", error_detail
    return None, error_detail


def _merge_execution_properties(task: Task, run: Run) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if isinstance(task.properties, dict):
        merged.update(task.properties)

    raw_variables = merged.get("variables")
    if isinstance(raw_variables, dict):
        for key, value in raw_variables.items():
            if isinstance(key, str) and key and key not in merged:
                merged[key] = value

    params = run.params if isinstance(run.params, dict) else {}
    explicit_properties = params.get("properties")
    if isinstance(explicit_properties, dict):
        merged.update(explicit_properties)

    for key, value in params.items():
        if not isinstance(key, str):
            continue
        lowered = key.lower()
        if lowered in _RUN_PARAM_TO_PROPERTY_KEYS and key not in {
            "thread_count",
            "num_threads",
            "duration",
            "ramp_up",
        }:
            merged[key] = value
            continue
        if _should_passthrough_runtime_param(key, value):
            merged[key] = value

    return merged


def _should_passthrough_runtime_param(key: str, value: Any) -> bool:
    lowered = key.lower()
    if lowered in _RUN_PARAM_CONTROL_KEYS or lowered in _RUN_PARAM_META_KEYS:
        return False
    if (
        lowered.startswith("seed_")
        or lowered.startswith("agent_")
        or lowered.startswith("k8s_")
    ):
        return False
    if isinstance(value, (str, int, float, bool)):
        return True
    return False


def _normalize_grpc_properties(
    protocol: Any, engine_type: Any, properties: dict[str, Any]
) -> dict[str, Any]:
    normalized_protocol = str(protocol or "").strip().lower()
    if normalized_protocol not in {"grpc", "mixed"}:
        return properties
    normalized_engine = (
        str(getattr(engine_type, "value", engine_type) or "").strip().lower()
    )

    grpc_host = properties.get("GRPC_HOST")
    grpc_port = properties.get("GRPC_PORT")
    if not isinstance(grpc_host, str) or not grpc_host.strip():
        return properties
    if _coerce_positive_int(grpc_port) is None and not (
        isinstance(grpc_port, str) and grpc_port.strip()
    ):
        return properties

    normalized_host = grpc_host.strip()
    port_value = str(grpc_port).strip()

    if normalized_engine == "k6":
        if normalized_host.startswith("["):
            closing_index = normalized_host.find("]")
            has_port = (
                closing_index > 0
                and closing_index + 1 < len(normalized_host)
                and normalized_host[closing_index + 1] == ":"
            )
        else:
            has_port = normalized_host.count(":") > 1 or (
                normalized_host.count(":") == 1
                and normalized_host.rpartition(":")[2].strip() != ""
            )

        if has_port:
            return properties

        normalized = dict(properties)
        normalized["GRPC_HOST"] = f"{normalized_host}:{port_value}"
        return normalized

    if normalized_host.startswith("["):
        closing_index = normalized_host.find("]")
        if (
            closing_index > 0
            and closing_index + 1 < len(normalized_host)
            and normalized_host[closing_index + 1] == ":"
        ):
            normalized_host = normalized_host[: closing_index + 1]
    elif normalized_host.count(":") == 1:
        host_part, _, port_part = normalized_host.rpartition(":")
        if host_part and port_part.strip():
            normalized_host = host_part

    if normalized_host == grpc_host:
        return properties

    normalized = dict(properties)
    normalized["GRPC_HOST"] = normalized_host
    return normalized


def _apply_demo_mixed_k6_runtime_defaults(
    task: Task, script: Script | None, properties: dict[str, Any]
) -> dict[str, Any]:
    normalized_engine = (
        str(getattr(task.engine_type, "value", task.engine_type) or "").strip().lower()
    )
    if normalized_engine != "k6":
        return properties

    protocols = {
        str(item or "").strip().lower()
        for item in (task.protocols or [])
        if str(item or "").strip()
    }
    if protocols != {"http", "grpc"}:
        return properties

    script_name = str(getattr(script, "name", "") or "").strip().lower()
    if not script_name.startswith("demo-mixed-k6-variable-"):
        return properties

    normalized = dict(properties)
    for key, value in _DEMO_MIXED_K6_RUNTIME_DEFAULTS.items():
        normalized.setdefault(key, value)
    return normalized


def _build_task_dispatch_payload(run: Run, task: Task) -> dict[str, Any]:
    params = run.params if isinstance(run.params, dict) else {}
    engine_type_value = (
        str(getattr(task.engine_type, "value", task.engine_type) or "").strip().lower()
    )
    run_mode = str(params.get("run_mode") or "").strip().lower()
    iteration_mode = run_mode == "iterations" or any(
        _coerce_positive_int(params.get(key))
        for key in ("iterations", "request_count", "loops")
    )
    thread_count = (
        _coerce_positive_int(params.get("thread_count"))
        or _coerce_positive_int(params.get("num_threads"))
        or _coerce_positive_int(params.get("vus"))
        or task.thread_count
    )
    duration = (
        _coerce_positive_int(params.get("duration"))
        or _coerce_positive_int(params.get("duration_seconds"))
        or task.duration
    )
    pod_count = (
        _coerce_positive_int(params.get("pod_count"))
        or _coerce_positive_int(params.get("pod_num"))
        or _coerce_positive_int(
            (task.properties or {}).get("pod_count")
            if isinstance(task.properties, dict)
            else None
        )
        or _coerce_positive_int(
            (task.properties or {}).get("pod_num")
            if isinstance(task.properties, dict)
            else None
        )
        or 1
    )
    ramp_up = _coerce_non_negative_int(params.get("ramp_up"))
    if ramp_up is None:
        ramp_up = task.ramp_up or 0

    protocol = params.get("protocol")
    if not isinstance(protocol, str) or not protocol.strip():
        normalized_protocols = [
            str(item or "").strip().lower()
            for item in (task.protocols or [])
            if str(item or "").strip()
        ]
        unique_protocols = set(normalized_protocols)
        if len(unique_protocols) > 1:
            protocol = "mixed"
        else:
            protocol = normalized_protocols[0] if normalized_protocols else None

    dispatch_properties = _normalize_grpc_properties(
        protocol, task.engine_type, _merge_execution_properties(task, run)
    )
    dispatch_properties["thread_count"] = thread_count
    dispatch_properties["ramp_up"] = ramp_up
    if iteration_mode:
        iteration_count = (
            _coerce_positive_int(params.get("iterations"))
            or _coerce_positive_int(params.get("request_count"))
            or _coerce_positive_int(params.get("loops"))
        )
        for key in (
            "duration",
            "duration_seconds",
            "PTP_DURATION_SECONDS",
            "DURATION",
        ):
            dispatch_properties.pop(key, None)
        if iteration_count is not None:
            dispatch_properties.setdefault("iterations", iteration_count)
            dispatch_properties.setdefault("request_count", iteration_count)
            dispatch_properties.setdefault("loops", iteration_count)
        if engine_type_value == "k6":
            for key in (
                "target_tps",
                "TARGET_TPS",
                "fixed_tps",
            ):
                dispatch_properties.pop(key, None)
        elif engine_type_value == "jmeter":
            dispatch_properties["scheduler_enabled"] = False
    else:
        dispatch_properties["duration"] = duration

    payload = {
        "script_id": task.script_id,
        "engine_type": task.engine_type.value,
        "pod_count": pod_count,
        "pod_num": pod_count,
        "thread_count": thread_count,
        "duration": duration,
        "ramp_up": ramp_up,
        "protocol": protocol,
        "properties": dispatch_properties,
        "run_id": run.run_id,
    }
    for scope_key in ("plan_run_id", "mixed_run_id", "execution_session_id"):
        scope_value = params.get(scope_key)
        if scope_value not in (None, ""):
            payload[scope_key] = scope_value
    return payload


def _apply_real_execution_summary(run: Run, jtl_summary: Any, k6_summary: Any) -> None:
    summary = k6_summary if isinstance(k6_summary, dict) else jtl_summary
    if not isinstance(summary, dict):
        return

    total_requests = _coerce_positive_int(summary.get("total_requests"))
    successful_requests = _coerce_non_negative_int(summary.get("successful_requests"))
    failed_requests = _coerce_non_negative_int(summary.get("failed_requests"))
    error_rate = _coerce_ratio(summary.get("error_rate"))
    success_rate = _coerce_ratio(summary.get("success_rate"))

    if total_requests and failed_requests is not None:
        error_rate = max(0.0, min(1.0, failed_requests / total_requests))
    if success_rate is None and total_requests and successful_requests is not None:
        success_rate = max(0.0, min(1.0, successful_requests / total_requests))
    if success_rate is None and error_rate is not None:
        success_rate = max(0.0, min(1.0, 1 - error_rate))
    if error_rate is None and success_rate is not None:
        error_rate = max(0.0, min(1.0, 1 - success_rate))

    run.total_requests = total_requests
    run.success_rate = success_rate
    run.error_rate = error_rate
    run.avg_rt_ms = _coerce_float(
        summary.get("rt_avg_ms", summary.get("avg_response_time"))
    )
    run.p95_rt_ms = _coerce_float(
        summary.get("rt_p95_ms", summary.get("p95_response_time"))
    )
    run.p99_rt_ms = _coerce_float(
        summary.get("rt_p99_ms", summary.get("p99_response_time"))
    )
    run.rps = _coerce_float(summary.get("throughput", summary.get("http_reqs")))


def _overlay_report_summary_fields(
    payload: dict[str, Any],
    *,
    jtl_summary: Optional[dict[str, Any]],
    k6_summary: Optional[dict[str, Any]],
) -> None:
    summary = k6_summary if isinstance(k6_summary, dict) else jtl_summary
    if not isinstance(summary, dict):
        return

    assigned_targets: set[str] = set()
    for source_key, target_key in (
        ("throughput", "rps"),
        ("http_reqs", "rps"),
        ("rt_avg_ms", "rt_avg_ms"),
        ("avg_response_time", "rt_avg_ms"),
        ("rt_p95_ms", "rt_p95_ms"),
        ("p95_response_time", "rt_p95_ms"),
        ("rt_p99_ms", "rt_p99_ms"),
        ("p99_response_time", "rt_p99_ms"),
        ("error_rate", "error_rate"),
    ):
        value = summary.get(source_key)
        if value is None or target_key in assigned_targets:
            continue
        payload[target_key] = value
        assigned_targets.add(target_key)


def _normalize_agent_terminal_status(
    status_payload: dict[str, Any] | None,
    *,
    elapsed_seconds: float,
    missing_pid_grace_seconds: float,
    finalizing_pid_grace_seconds: float,
) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(status_payload, dict):
        return None, None

    status_value = status_payload.get("status")
    error_detail = status_payload.get("error")
    ended_at = status_payload.get("ended_at")
    pid_present = "pid" in status_payload
    pid = status_payload.get("pid")
    has_summary = isinstance(status_payload.get("jtl_summary"), dict) or isinstance(
        status_payload.get("k6_summary"), dict
    )
    pid_missing = pid_present and (pid is None or pid == 0 or pid == "")

    if status_value is None and ended_at:
        if error_detail:
            return "failed", str(error_detail)
        return "succeeded", None

    if status_value is None and has_summary:
        if error_detail:
            return "failed", str(error_detail)
        return "succeeded", None

    if status_value == "running" and ended_at and pid_missing:
        if error_detail:
            return "failed", str(error_detail)
        if has_summary:
            return "succeeded", None
        return "failed", "agent_reported_running_without_pid_after_end"

    if status_value == "running" and pid_missing and not ended_at and not has_summary:
        if error_detail:
            return "failed", str(error_detail)
        fail_after = max(0.0, missing_pid_grace_seconds) + max(
            0.0, finalizing_pid_grace_seconds
        )
        if elapsed_seconds < fail_after:
            return "running", None

    if (
        status_value == "running"
        and pid_missing
        and elapsed_seconds >= max(0.0, missing_pid_grace_seconds)
    ):
        if error_detail:
            return "failed", str(error_detail)
        if has_summary:
            return "succeeded", None
        return "failed", "agent_reported_running_without_pid"

    return status_value, str(error_detail) if error_detail is not None else None


def _parse_agent_ended_at(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _calculate_poll_timeout_seconds(
    task_data: dict[str, Any], task: Task, startup_buffer: int = 0
) -> int:
    duration_seconds = (
        _coerce_positive_int(task_data.get("duration")) or task.duration or 0
    )
    engine_type_value = (
        str(getattr(task.engine_type, "value", task.engine_type) or "").strip().lower()
    )
    base_timeout = max(150, duration_seconds + 30 + max(0, startup_buffer))
    if engine_type_value == "jmeter" and duration_seconds > 0:
        duration_slack_seconds = max(
            30,
            int(os.getenv("JMETER_DURATION_TIMEOUT_SLACK_SECONDS", "240")),
        )
        base_timeout = max(
            base_timeout,
            duration_seconds + duration_slack_seconds + max(0, startup_buffer),
        )

    properties = task_data.get("properties")
    if not isinstance(properties, dict):
        return base_timeout

    request_count = _coerce_positive_int(
        properties.get("iterations")
    ) or _coerce_positive_int(properties.get("request_count"))
    if not request_count:
        return base_timeout

    target_tps = _coerce_positive_int(
        properties.get("target_tps")
    ) or _coerce_positive_int(properties.get("fixed_tps"))
    if target_tps:
        # For JMeter iteration-mode runs, request_count represents logical loop count.
        # In the current JMeter execution contract, request_count/iterations is applied
        # per thread, and each dispatched agent executes its own full thread group. So the
        # overall work is closer to:
        #   request_count * thread_count * agent_total / target_tps
        # rather than plain request_count/target_tps.
        #
        # Fresh remote mixed JMeter runs proved this explicitly:
        # - each agent produced ~5000 requests for request_count=1000, thread_count=5
        # - overall throughput stayed near 5 rps
        # - real wall time therefore landed near 2000s, not a few hundred seconds
        #
        # Using protocol_fanout here double-counted and still missed the real contract,
        # because the loop body itself already expands into the concrete sampler mix.
        effective_request_count = request_count
        if engine_type_value == "jmeter":
            thread_count = (
                _coerce_positive_int(task_data.get("thread_count"))
                or _coerce_positive_int(task_data.get("num_threads"))
                or _coerce_positive_int(properties.get("thread_count"))
                or _coerce_positive_int(properties.get("threads"))
                or task.thread_count
                or 1
            )
            agent_total = _resolve_dispatch_agent_count(task_data)
            effective_request_count *= max(1, thread_count) * max(1, agent_total)
        estimated_seconds = math.ceil(effective_request_count / max(1, target_tps))
        completion_slack_seconds = 120
        if engine_type_value == "jmeter":
            completion_slack_seconds = max(
                completion_slack_seconds,
                int(os.getenv("JMETER_ITERATION_TIMEOUT_SLACK_SECONDS", "240")),
            )
    else:
        parallelism = (
            _coerce_positive_int(task_data.get("thread_count"))
            or _coerce_positive_int(properties.get("threads"))
            or task.thread_count
            or 1
        )
        # 没有 TPS 约束时，用“每并发至少 1 req/s”的保守估算，避免按次数真执行被 150s 误杀。
        estimated_seconds = math.ceil(request_count / max(1, parallelism))
        completion_slack_seconds = 120

    return min(
        3600,
        max(
            base_timeout,
            estimated_seconds + completion_slack_seconds + max(0, startup_buffer),
        ),
    )


def _resolve_run_timeout_seconds(run: Run, task: Task) -> int:
    task_data = _build_task_dispatch_payload(run, task)
    k8s_startup_buffer = (
        int(os.getenv("K8S_STARTUP_BUFFER", "120"))
        if os.getenv("USE_K8S_AGENT") == "1"
        else 0
    )
    return _calculate_poll_timeout_seconds(
        task_data,
        task,
        startup_buffer=k8s_startup_buffer,
    )


def _resolve_timeout_grace_delay_seconds(
    *,
    status_payload: dict[str, Any] | None,
    status_value: str | None,
    error_detail: str | None,
    elapsed_seconds: float,
    timeout_seconds: int,
    interval_seconds: int,
) -> int | None:
    if not isinstance(status_payload, dict):
        return None
    if status_value not in {None, "running"}:
        return None
    if error_detail:
        return None

    grace_seconds = max(
        0.0, float(os.getenv("RUN_POLL_TIMEOUT_FINAL_GRACE_SECONDS", "15"))
    )
    hard_timeout_seconds = timeout_seconds + grace_seconds
    if elapsed_seconds < timeout_seconds or elapsed_seconds >= hard_timeout_seconds:
        return None

    remaining_seconds = max(1.0, hard_timeout_seconds - elapsed_seconds)
    return max(1, min(interval_seconds, math.ceil(remaining_seconds)))


def _extract_execution_summary_counts(
    params: dict[str, Any] | None,
) -> tuple[Optional[int], Optional[int], Optional[int]]:
    if not isinstance(params, dict):
        return None, None, None
    summary = (
        params.get("k6_summary")
        if isinstance(params.get("k6_summary"), dict)
        else (
            params.get("jtl_summary")
            if isinstance(params.get("jtl_summary"), dict)
            else None
        )
    )
    if not isinstance(summary, dict):
        return None, None, None
    return (
        _coerce_non_negative_int(summary.get("total_requests")),
        _coerce_non_negative_int(summary.get("successful_requests")),
        _coerce_non_negative_int(summary.get("failed_requests")),
    )


def _has_execution_summary_progress(
    previous_params: dict[str, Any] | None,
    current_params: dict[str, Any] | None,
) -> bool:
    previous_counts = _extract_execution_summary_counts(previous_params)
    current_counts = _extract_execution_summary_counts(current_params)

    for previous_value, current_value in zip(previous_counts, current_counts):
        if current_value is None:
            continue
        if previous_value is None:
            if current_value > 0:
                return True
            continue
        if current_value > previous_value:
            return True
    return False


def _extract_status_payload_live_rps(
    status_payload: dict[str, Any] | None,
) -> Optional[float]:
    """Fresh `/status` samples expose live throughput even before terminal summary exists."""

    if not isinstance(status_payload, dict):
        return None

    for key in ("rps", "observed_tps"):
        value = _coerce_float(status_payload.get(key))
        if value is not None and value > 0:
            return value
    return None


def _is_fixed_duration_run_params(params: dict[str, Any] | None) -> bool:
    if not isinstance(params, dict):
        return False
    mode = str(params.get("run_mode") or params.get("run_by") or "").strip().lower()
    if mode in {"duration", "time", "timed"}:
        return True
    return _coerce_optional_bool(params.get("scheduler_enabled")) is True


def _resolve_timeout_running_evidence_delay_seconds(
    *,
    status_payload: dict[str, Any] | None,
    status_value: str | None,
    error_detail: str | None,
    elapsed_seconds: float,
    timeout_seconds: int,
    interval_seconds: int,
    previous_params: dict[str, Any] | None,
    current_params: dict[str, Any] | None,
) -> int | None:
    if status_value != "running":
        return None
    if error_detail:
        return None
    if not isinstance(status_payload, dict):
        return None
    pid = status_payload.get("pid")
    if pid in {None, 0, ""}:
        return None
    live_rps = _extract_status_payload_live_rps(status_payload)
    has_summary_progress = _has_execution_summary_progress(
        previous_params, current_params
    )
    if not has_summary_progress and (live_rps is None or live_rps <= 0):
        return None

    if _is_fixed_duration_run_params(current_params) or _is_fixed_duration_run_params(
        previous_params
    ):
        # 固定时长 run 到达 timeout 后，允许 final refresh / grace 先对齐一次 agent
        # 终态，但不能因为 JMeter 进程仍有 pid/live rps 就无限续命。否则 scheduler
        # 失效或 backend listener 卡住时，批次预计结束时间会被破坏。
        return None

    extension_seconds = max(
        0.0,
        float(os.getenv("RUN_POLL_TIMEOUT_RUNNING_EVIDENCE_EXTENSION_SECONDS", "240")),
    )
    if extension_seconds <= 0:
        return None

    hard_timeout_seconds = timeout_seconds + extension_seconds

    # 绝对 wall-clock 上限：即使 summary_counts 还在涨（evidence 看似在前进），
    # 也不允许某个 run 的 elapsed 超出 `timeout_seconds * multiplier`。
    # 用于防御 pathological 场景：被测服务持续 back-pressure / k6 产能远低于目标
    # TPS，但 summary 每次 poll 都微涨几条，导致 hard_timeout 被反复刷新、run
    # 被拖到远超合理时长。默认不启用（等价于旧行为）；op 可通过 env 打开。
    absolute_ceiling_multiplier_raw = os.getenv(
        "RUN_POLL_TIMEOUT_RUNNING_EVIDENCE_ABSOLUTE_CEILING_MULTIPLIER"
    )
    if absolute_ceiling_multiplier_raw:
        try:
            absolute_ceiling_multiplier = float(absolute_ceiling_multiplier_raw)
        except (TypeError, ValueError):
            absolute_ceiling_multiplier = 0.0
        if absolute_ceiling_multiplier > 0:
            absolute_ceiling_seconds = max(
                timeout_seconds, timeout_seconds * absolute_ceiling_multiplier
            )
            if elapsed_seconds >= absolute_ceiling_seconds:
                return None
            hard_timeout_seconds = min(hard_timeout_seconds, absolute_ceiling_seconds)

    if elapsed_seconds >= hard_timeout_seconds:
        # Runtime freeze / host suspend can jump wall-clock elapsed far past the
        # old extension window while the agent still reports fresh positive
        # throughput after resume. Keep polling in that case instead of forcing
        # a false `timeout -> failed` closeout.
        if live_rps is None or live_rps <= 0:
            return None
        return max(1, interval_seconds)

    remaining_seconds = max(1.0, hard_timeout_seconds - elapsed_seconds)
    return max(1, min(interval_seconds, math.ceil(remaining_seconds)))


def _should_refresh_timeout_boundary_status(
    *,
    status_payload: dict[str, Any] | None,
    status_value: str | None,
    error_detail: str | None,
    elapsed_seconds: float,
    timeout_seconds: int,
) -> bool:
    if elapsed_seconds < timeout_seconds:
        return False
    if error_detail:
        return False
    if status_value in {"succeeded", "failed", "stopped"}:
        return False
    return status_payload is None or status_value in {None, "running"}


def _refresh_timeout_boundary_status(
    agent_host: str, run_token: str
) -> dict[str, Any] | None:
    try:
        refreshed = _gather_run_status_payloads(
            [{"agent_host": agent_host, "agent_run_token": run_token}]
        )[0]
    except Exception as exc:  # pragma: no cover - 容错
        logger.warning(
            "poll_run_status final timeout refresh failed for run_token=%s host=%s: %s",
            run_token,
            agent_host,
            exc,
        )
        return None
    if isinstance(refreshed, dict):
        logger.info(
            "poll_run_status final timeout refresh host=%s token=%s status=%s",
            agent_host,
            run_token,
            refreshed.get("status"),
        )
        return refreshed
    return None


def _execute_test_task_impl(run_id: int, *, inline_poll: bool = False):
    """执行 Run 分发；plan 内联路径可选择同步轮询到终态。"""
    db = SessionLocal()
    try:
        run = db.query(Run).filter(Run.run_id == run_id).first()
        all_runs = db.query(Run).all()
        logger.error(
            f"DEBUG: run_id={run_id}, found_run={run is not None}, all_runs_ids={[r.run_id for r in all_runs]}"
        )
        if not run:
            raise ValueError(f"Run {run_id} not found")

        # Atomic conditional UPDATE: PREPARING → RUNNING
        # 仅允许 PREPARING 进入，排除 RUNNING（防重复分发）和终态
        rows_updated = (
            db.query(Run)
            .filter(Run.run_id == run_id, Run.run_status.in_(_DISPATCH_ALLOWED))
            .update(
                {
                    Run.run_status: RunStatus.RUNNING,
                    Run.run_status_detail: "dispatching_data",
                    Run.stop_reason: None,
                    Run.started_at: run.started_at or datetime.now(timezone.utc),
                },
                synchronize_session="fetch",
            )
        )
        db.commit()

        if rows_updated == 0:
            db.refresh(run)
            logger.info(
                "Run %s already in terminal state %s, skipping execution",
                run_id,
                run.run_status.value,
            )
            return {"status": run.run_status.value, "detail": "already_terminal"}

        db.refresh(run)

        task = db.query(Task).filter(Task.id == run.task_id).first()
        if not task:
            raise ValueError(f"Task {run.task_id} not found")

        # 构造任务数据
        task_data = _build_task_dispatch_payload(run, task)
        script: Script | None = None
        # 可选携带脚本路径，便于 agent 真实执行；缺失时由 agent 回退为模拟
        if task.script_id:
            script = db.query(Script).filter(Script.id == task.script_id).first()
            if script and script.file_path:
                if isinstance(script.file_path, str) and script.file_path.startswith(
                    "s3://"
                ):
                    task_data["script_s3"] = script.file_path
                else:
                    task_data["script_path"] = script.file_path
                try:
                    script_bytes = ScriptService(db).load_script_bytes(script)
                    inline_limit = _coerce_positive_int(
                        os.getenv("PTP_INLINE_SCRIPT_DISPATCH_MAX_BYTES")
                    ) or (2 * 1024 * 1024)
                    if len(script_bytes) <= inline_limit:
                        task_data["script_content"] = script_bytes.decode(
                            "utf-8", errors="replace"
                        )
                        task_data["script_file_name"] = Path(
                            str(script.file_path)
                        ).name or str(script.name or f"script-{script.id}")
                    else:
                        logger.info(
                            "Skipping inline script dispatch for run %s: script %s size %s exceeds %s",
                            run_id,
                            script.id,
                            len(script_bytes),
                            inline_limit,
                        )
                except Exception as exc:
                    logger.warning(
                        "Failed to attach inline script content for run %s script %s: %s",
                        run_id,
                        getattr(script, "id", None),
                        exc,
                    )

        task_data["properties"] = _apply_demo_mixed_k6_runtime_defaults(
            task,
            script,
            dict(task_data.get("properties") or {}),
        )

        runtime_assets = TaskAssetService(db).build_runtime_manifest(
            task.id,
            execution_properties=task_data.get("properties"),
            task_pattern=task.task_pattern.value if task.task_pattern else None,
        )
        proto_runtime_assets = TaskAssetService(db).build_proto_runtime_manifest(
            task.id,
            task_pattern=task.task_pattern.value if task.task_pattern else None,
        )
        if runtime_assets.get("data_files"):
            task_data["data_asset_manifest"] = runtime_assets
        if proto_runtime_assets.get("proto_files"):
            task_data["proto_asset_manifest"] = proto_runtime_assets
        if runtime_assets.get("data_distribution"):
            task_data["data_distribution"] = runtime_assets["data_distribution"]

        logger.info(
            "Dispatching run %s to agent with task_data=%s", run.run_id, task_data
        )

        capacity_context = TaskService(db)._build_agent_capacity_context(task.env)
        try:
            execution_coro = _execute_task_async(
                task.id,
                task_data,
                capacity_context=capacity_context,
            )
        except TypeError as exc:
            if "capacity_context" not in str(exc):
                raise
            execution_coro = _execute_task_async(task.id, task_data)
        result = _run_async_blocking(execution_coro)

        if result["status"] != "success":
            # 即使失败也尽量持久化 K8S 元信息，便于后续清理/排障
            merged_params = dict(run.params or {})
            merged_params["agent_host"] = result.get("agent_host") or result.get(
                "agent"
            )
            agent_metadata = result.get("agent_metadata")
            if isinstance(agent_metadata, dict) and agent_metadata:
                merged_params["agent_metadata"] = agent_metadata
                runtime_kind = str(agent_metadata.get("runtime_kind") or "").strip()
                if runtime_kind:
                    merged_params["agent_runtime_kind"] = runtime_kind
                compose_service = str(
                    agent_metadata.get("compose_service") or ""
                ).strip()
                if compose_service:
                    merged_params["pod_grafana_compose_service"] = compose_service
            if result.get("k8s_job"):
                merged_params["k8s_job"] = result.get("k8s_job")
            if isinstance(result.get("agent_runs"), list) and result["agent_runs"]:
                merged_params["agent_runs"] = result["agent_runs"]
            if isinstance(result.get("launch_wave_summary"), dict):
                merged_params["launch_wave_summary"] = result["launch_wave_summary"]
            run.params = merged_params
            db.commit()
            error_message = str(result.get("error") or "").strip()
            if "No healthy agents available" in error_message:
                _mark_failed(
                    db,
                    run,
                    error_message or "No healthy agents available",
                    status_detail="no_healthy_agents",
                )
                logger.warning("Run %s failed due to no healthy agents", run_id)
                return {"status": "failed", "detail": "no_healthy_agents"}
            raise Exception(
                f"Task execution failed: {error_message or 'unknown_error'}"
            )

        logger.info("Run %s executed successfully on %s", run_id, result.get("agent"))

        merged_params = dict(run.params or {})
        merged_params["agent_host"] = result.get("agent_host") or result.get("agent")
        agent_metadata = result.get("agent_metadata")
        if isinstance(agent_metadata, dict) and agent_metadata:
            merged_params["agent_metadata"] = agent_metadata
            runtime_kind = str(agent_metadata.get("runtime_kind") or "").strip()
            if runtime_kind:
                merged_params["agent_runtime_kind"] = runtime_kind
            compose_service = str(agent_metadata.get("compose_service") or "").strip()
            if compose_service:
                merged_params["pod_grafana_compose_service"] = compose_service
        if result.get("run_token"):
            merged_params["agent_run_token"] = result.get("run_token")
        if isinstance(result.get("agent_runs"), list) and result["agent_runs"]:
            merged_params["agent_runs"] = result["agent_runs"]
        if isinstance(result.get("launch_wave_summary"), dict):
            merged_params["launch_wave_summary"] = result["launch_wave_summary"]
        if result.get("k8s_job"):
            merged_params["k8s_job"] = result.get("k8s_job")
        run.params = merged_params
        db.commit()

        # 原子条件更新：仅在未被并发 stop 时保持 RUNNING
        db.query(Run).filter(
            Run.run_id == run_id,
            Run.run_status.notin_(_TERMINAL),
        ).update(
            {Run.run_status_detail: None},
            synchronize_session="fetch",
        )
        db.commit()
        db.refresh(run)

        if run.run_status in _TERMINAL:
            logger.info("Run %s was stopped during dispatch, aborting", run_id)
            return {"status": run.run_status.value, "detail": "stopped_during_dispatch"}

        run_token = result.get("run_token")
        if run_token:
            # K8S_AGENT 模式下额外预留 Pod 冷启动时间（默认 120s），避免 minikube 下过早超时
            k8s_startup_buffer = (
                int(os.getenv("K8S_STARTUP_BUFFER", "120"))
                if os.getenv("USE_K8S_AGENT") == "1"
                else 0
            )
            poll_timeout = _calculate_poll_timeout_seconds(
                task_data,
                task,
                startup_buffer=k8s_startup_buffer,
            )
            poll_kwargs = {
                "run_id": run.run_id,
                "agent_host": result.get("agent_host"),
                "run_token": run_token,
                "timeout_seconds": poll_timeout,
                "interval_seconds": 2,
                "started_at_ts": datetime.now(timezone.utc).timestamp(),
                "agent_runs": result.get("agent_runs"),
            }
            if inline_poll:
                _apply_task_inline(poll_run_status, inline_retry=True, **poll_kwargs)
            elif os.environ.get("TESTING", "0") == "1":
                _apply_task_inline(poll_run_status, **poll_kwargs)
            else:
                poll_run_status.delay(**poll_kwargs)
        else:
            _mark_failed(db, run, "missing_run_token")

        return result
    except Exception as exc:
        logger.error("Run %s failed: %s", run_id, exc)
        if "run" in locals() and run is not None:
            # 仅在 run 未被并发 stop 时才标记为 FAILED
            db.refresh(run)
            if run.run_status not in _TERMINAL:
                _mark_failed(db, run, str(exc))
        raise
    finally:
        db.close()


def execute_test_run_inline(run_id: int):
    """供 plan_executor 直接调用，避免 run dispatch/poll 再次占用额外 worker 槽位。"""
    return _execute_test_task_impl(run_id, inline_poll=True)


@celery_app.task(
    bind=True,
    name="execute_test_task",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 60},
)
def execute_test_task(self, run_id: int):
    """
    执行 Run 的 Celery 任务（分发 + 触发异步轮询）
    """
    return _execute_test_task_impl(run_id, inline_poll=False)


def _mark_failed(db, run: Run, detail: str, status_detail: str = "expand_failed"):
    # 终态守卫：已被并发 stop/succeed 的 run 不覆盖
    if run.run_status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.STOPPED}:
        return
    run.run_status = RunStatus.FAILED
    run.stop_reason = detail
    run.run_status_detail = status_detail
    end_ts = datetime.now(timezone.utc)
    run.ended_at = end_ts
    start_ts = run.started_at
    if start_ts and start_ts.tzinfo is None:
        start_ts = start_ts.replace(tzinfo=timezone.utc)
    if start_ts:
        run.duration_seconds = int((end_ts - start_ts).total_seconds())
    db.commit()
    k8s_meta = (run.params or {}).get("k8s_job")
    if k8s_meta:
        orchestrator.cleanup_k8s_job(k8s_meta)


async def _execute_task_async(
    task_id: int,
    task_data: Dict,
    capacity_context: Optional[dict[str, Any]] = None,
) -> Dict:
    """异步执行任务：选择 agent 并分发"""
    try:
        desired_agent_count = _resolve_dispatch_agent_count(task_data)
        if capacity_context is None:
            agents = await orchestrator.select_agents(task_id, desired_agent_count)
        else:
            agents = await orchestrator.select_agents(
                task_id,
                desired_agent_count,
                capacity_context=capacity_context,
            )
        if len(agents) < desired_agent_count:
            raise ValueError(
                f"Requested {desired_agent_count} healthy agents, only {len(agents)} available"
            )

        normalized_results = await _dispatch_agents_with_launch_waves(
            task_id,
            task_data,
            agents,
        )
        wave_size = _resolve_launch_wave_size(task_data, len(agents))
        launch_wave_summary = _build_launch_wave_summary(
            normalized_results,
            agent_total=len(agents),
            wave_size=wave_size,
            wave_total=math.ceil(len(agents) / wave_size) if agents else 0,
        )

        successful_results = [
            item
            for item in normalized_results
            if item.get("status") == "success" and item.get("run_token")
        ]
        failed_results = [
            item
            for item in normalized_results
            if item.get("status") != "success" or not item.get("run_token")
        ]
        if failed_results:
            await asyncio.gather(
                *[
                    orchestrator.stop_run(item["agent_host"], item["run_token"])
                    for item in successful_results
                    if item.get("agent_host") and item.get("run_token")
                ],
                return_exceptions=True,
            )
            error_messages = [
                str(item.get("error") or "missing_run_token") for item in failed_results
            ]
            return {
                "status": "error",
                "error": "; ".join(error_messages),
                "agent_runs": [
                    _build_agent_run_entry(
                        item,
                        agent_index=item["agent_index"],
                        agent_total=item["agent_total"],
                    )
                    for item in successful_results
                ],
                "launch_wave_summary": launch_wave_summary,
                "results": normalized_results,
            }

        primary_result = successful_results[0]
        return {
            "status": "success",
            "agent": primary_result.get("agent"),
            "agent_host": primary_result.get("agent_host"),
            "agent_metadata": primary_result.get("agent_metadata"),
            "run_token": primary_result.get("run_token"),
            "k8s_job": primary_result.get("k8s_job"),
            "agent_runs": [
                _build_agent_run_entry(
                    item,
                    agent_index=item["agent_index"],
                    agent_total=item["agent_total"],
                )
                for item in successful_results
            ],
            "launch_wave_summary": launch_wave_summary,
            "results": normalized_results,
        }
    except Exception as e:
        logger.error(f"Async execution failed for task {task_id}: {e}")
        return {"status": "error", "error": str(e)}


def _is_retry_exhausted(task) -> bool:
    request = getattr(task, "request", None)
    retries = getattr(request, "retries", 0) if request else 0
    max_retries = getattr(task, "max_retries", 0)
    try:
        retries = int(retries or 0)
    except (TypeError, ValueError):
        retries = 0
    try:
        max_retries = int(max_retries or 0)
    except (TypeError, ValueError):
        max_retries = 0
    return retries >= max_retries


# terminal-state reconciliation：当 poll_run_status 的主轮询链路炸掉时，控制面必须再拿一次终态证据，
# 否则我们会把仍在打流量的 run 误写成 failed，同时 agent 上的 live k6 进程还在继续跑。
# 该计数仅用于保留 exhausted-running 的重排痕迹，不允许再作为写 failed 的依据。
_POLL_EVIDENCE_RETRY_KEY = "_poll_run_status_evidence_retry"


def _fetch_terminal_evidence(
    contexts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """对 exhausted-retry 窗口里的 agent /status 做一次浅栈 refresh。

    返回每个 context 的 `{"status": <normalized>, "error": <detail>, "payload": <raw>}`。
    任何异常都吞掉，用 `None` 代替；exhausted 分支必须能在 control-plane 异常时静默收集到歧义信号。
    """

    if not contexts:
        return []

    try:
        raw_results = _gather_run_status_payloads(contexts)
    except Exception as exc:  # pragma: no cover - 容错
        logger.warning(
            "poll_run_status terminal-evidence refresh failed for %d contexts: %s",
            len(contexts),
            exc,
        )
        raw_results = [None for _ in contexts]

    evidence: list[dict[str, Any]] = []
    for item, raw in zip(contexts, raw_results):
        payload = raw if isinstance(raw, dict) else None
        status_raw = payload.get("status") if isinstance(payload, dict) else None
        status_norm = (
            str(status_raw).strip().lower() if isinstance(status_raw, str) else None
        )
        error_detail = payload.get("error") if isinstance(payload, dict) else None
        evidence.append(
            {
                "agent_host": item.get("agent_host"),
                "run_token": item.get("agent_run_token"),
                "status": status_norm,
                "error": error_detail,
                "payload": payload,
            }
        )
    return evidence


def _summarize_terminal_evidence(
    evidence: list[dict[str, Any]],
) -> tuple[str, Optional[str]]:
    """把多 context 证据合并成顶层 verdict。

    - 任一 context 仍 running -> 'running'（不允许把顶层写 failed）
    - 否则若任一 failed -> 'failed'（附 error）
    - 否则若任一 stopped -> 'stopped'
    - 否则若全部 succeeded -> 'succeeded'
    - 其它 -> 'unknown'
    """

    saw_running = False
    saw_failed = False
    saw_stopped = False
    saw_succeeded = False
    saw_known = False
    failed_error: Optional[str] = None

    for entry in evidence:
        status = entry.get("status")
        if status == "running":
            saw_running = True
            saw_known = True
        elif status == "failed":
            saw_failed = True
            saw_known = True
            if failed_error is None:
                failed_error = entry.get("error")
        elif status == "stopped":
            saw_stopped = True
            saw_known = True
        elif status == "succeeded":
            saw_succeeded = True
            saw_known = True

    if saw_running:
        return "running", None
    if not saw_known:
        return "unknown", None
    if saw_failed:
        return "failed", failed_error
    if saw_stopped:
        return "stopped", None
    if saw_succeeded:
        return "succeeded", None
    return "unknown", None


def _build_status_entries_from_terminal_evidence(
    contexts: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    context_map = {
        (item.get("agent_host"), item.get("agent_run_token")): item for item in contexts
    }
    status_entries: list[dict[str, Any]] = []

    for entry in evidence:
        agent_host = entry.get("agent_host")
        run_token = entry.get("run_token")
        payload = (
            entry.get("payload") if isinstance(entry.get("payload"), dict) else None
        )
        context = context_map.get((agent_host, run_token), {})
        status_entries.append(
            {
                "agent_host": agent_host,
                "run_token": run_token,
                "status_payload": payload,
                "k8s_job": context.get("k8s_job"),
                "agent_index": context.get("agent_index"),
                "agent_total": context.get("agent_total"),
                "agent_metadata": context.get("agent_metadata"),
                "agent_runtime_kind": context.get("agent_runtime_kind"),
                "pod_grafana_compose_service": context.get(
                    "pod_grafana_compose_service"
                ),
                "status_value": entry.get("status"),
                "error_detail": entry.get("error"),
                "agent_ip": (payload or {}).get("agent_ip"),
                "log_s3": (payload or {}).get("log_s3"),
                "metrics_s3": (payload or {}).get("metrics_s3"),
                "jtl_summary": (payload or {}).get("jtl_summary"),
                "k6_summary": (payload or {}).get("k6_summary"),
                "raw_observability": (payload or {}).get("raw_observability"),
                "pod_monitor_series": (payload or {}).get("pod_monitor_series"),
                "k8s_log_tail": None,
                "k8s_log_s3": None,
                "k8s_events": None,
            }
        )

    return status_entries


def _stop_runs_on_agents(contexts: list[dict[str, Any]]) -> None:
    """exhausted 分支兜底杀进程：按 token 调 agent /stop。失败静默。"""
    if not contexts:
        return

    if _prefer_sync_poll_agent_io() or (
        _POLL_RUN_STATUS_FORCE_SYNC_IO and _sync_poll_fallback_allowed()
    ):
        for item in contexts:
            _stop_run_sync(item["agent_host"], item["agent_run_token"])
        return

    async def _gather_all() -> list[Any]:
        tasks = [
            orchestrator.stop_run(item["agent_host"], item["agent_run_token"])
            for item in contexts
        ]
        return await asyncio.gather(*tasks, return_exceptions=True)

    try:
        _run_async_blocking(_gather_all())
    except Exception as exc:  # pragma: no cover - 容错
        if not _sync_poll_fallback_allowed():
            logger.warning(
                "poll_run_status exhausted-retry stop_run failed for %d contexts: %s",
                len(contexts),
                _format_sync_poll_exception(exc),
            )
            return
        _enable_sync_poll_status_io(exc)
        logger.warning(
            "poll_run_status exhausted-retry stop_run failed for %d contexts: %s",
            len(contexts),
            _format_sync_poll_exception(exc),
        )
        for item in contexts:
            _stop_run_sync(item["agent_host"], item["agent_run_token"])


def _apply_terminal_evidence_outcome(
    db,
    run: Run,
    verdict: str,
    terminal_error: Optional[str],
    fallback_stop_reason: str,
    *,
    status_entries: list[dict[str, Any]],
    fallback_agent_host: str,
    fallback_run_token: str,
) -> None:
    """按 evidence verdict 落库；永远不覆盖已是终态的 run。"""
    if run.run_status in _TERMINAL:
        return
    merged_params, aggregated_jtl_summary, aggregated_k6_summary, primary_entry = (
        _merge_run_params_from_status_entries(
            run,
            status_entries=status_entries,
            fallback_agent_host=fallback_agent_host,
            fallback_run_token=fallback_run_token,
        )
    )
    run.params = merged_params

    if verdict == "failed":
        run.run_status = RunStatus.FAILED
        run.run_status_detail = "expand_failed"
        run.stop_reason = terminal_error or fallback_stop_reason
    elif verdict == "succeeded":
        run.run_status = RunStatus.SUCCEEDED
        run.run_status_detail = None
        run.stop_reason = None
    elif verdict == "stopped":
        run.run_status = RunStatus.STOPPED
        run.run_status_detail = None
        run.stop_reason = terminal_error
    else:
        raise RuntimeError(f"unexpected terminal evidence verdict: {verdict}")

    ended_at_ts = _parse_agent_ended_at(
        (primary_entry.get("status_payload") or {}).get("ended_at")
    )
    run.ended_at = ended_at_ts or datetime.now(timezone.utc)
    start_ts = run.started_at
    if start_ts and start_ts.tzinfo is None:
        start_ts = start_ts.replace(tzinfo=timezone.utc)
    if start_ts:
        run.duration_seconds = max(0, int((run.ended_at - start_ts).total_seconds()))
    _apply_real_execution_summary(run, aggregated_jtl_summary, aggregated_k6_summary)
    db.commit()
    return


def _reschedule_after_recursion_evidence(
    *,
    run_id: int,
    agent_host: str,
    run_token: str,
    timeout_seconds: int,
    interval_seconds: int,
    started_at_ts: float,
    inline_retry: bool,
    agent_runs: Optional[list[dict[str, Any]]],
) -> Optional[dict[str, Any]]:
    """exhausted 但 agent 仍 live 时，把本次 poll 重新入队。

    和正常 poll 下一轮 reschedule 不同，这里在 TESTING=1 下也走 apply_async 而不是
    inline，避免 `_run_async_blocking` 被 mock 成永远抛异常时出现无限内联重试；
    测试如需观察 reschedule，monkeypatch `poll_run_status.apply_async` 即可。
    """

    next_kwargs = {
        "run_id": run_id,
        "agent_host": agent_host,
        "run_token": run_token,
        "timeout_seconds": timeout_seconds,
        "interval_seconds": interval_seconds,
        "started_at_ts": started_at_ts,
        "inline_retry": inline_retry,
        "agent_runs": agent_runs,
    }
    if inline_retry:
        time.sleep(interval_seconds)
        return _build_inline_poll_continue(next_kwargs)
    poll_run_status.apply_async(kwargs=next_kwargs, countdown=interval_seconds)
    return None


def _handle_exhausted_retry_evidence(
    db,
    *,
    run_id: int,
    agent_host: str,
    run_token: str,
    timeout_seconds: int,
    interval_seconds: int,
    started_at_ts: float,
    inline_retry: bool,
    agent_runs_override: Optional[list[dict[str, Any]]],
    cause: Exception,
) -> Optional[dict[str, Any]]:
    """poll_run_status 已经耗尽 Celery 重试时的收尾路径。

    只有以下三种情况才允许把 run 写成 failed：
    1. agent 已经自己回 failed/stopped/succeeded（以真实终态为准）；
    2. 我们主动调 `orchestrator.stop_run` 后 agent 回到终态；
    3. 连续 `PTP_POLL_RUN_STATUS_TERMINAL_EVIDENCE_MAX_RETRIES` 轮都拿不到任何终态证据。

    其它所有情况（特别是 agent 仍报 `running`）必须保持 run 仍为 RUNNING 并重新排下一轮 poll，
    避免 `poll_run_status_error:maximum recursion depth exceeded` 把 live k6 进程从顶层视野
    拽掉但 agent 还在打流量。
    """

    failed_run = db.query(Run).filter(Run.run_id == run_id).first()
    if not failed_run or failed_run.run_status in _TERMINAL:
        return

    contexts = _normalize_agent_run_contexts(
        agent_runs=agent_runs_override or (failed_run.params or {}).get("agent_runs"),
        agent_host=agent_host,
        run_token=run_token,
        fallback_k8s_job=(failed_run.params or {}).get("k8s_job"),
    )

    fallback_stop_reason = f"poll_run_status_error:{cause}"

    evidence = _fetch_terminal_evidence(contexts)
    verdict, terminal_error = _summarize_terminal_evidence(evidence)

    if verdict == "running":
        return _advance_or_fail_on_running_evidence(
            db,
            failed_run,
            run_id=run_id,
            agent_host=agent_host,
            run_token=run_token,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            started_at_ts=started_at_ts,
            inline_retry=inline_retry,
        )
        return None

    if verdict in {"failed", "succeeded", "stopped"}:
        status_entries = _build_status_entries_from_terminal_evidence(
            contexts, evidence
        )
        _apply_terminal_evidence_outcome(
            db,
            failed_run,
            verdict,
            terminal_error,
            fallback_stop_reason=fallback_stop_reason,
            status_entries=status_entries,
            fallback_agent_host=agent_host,
            fallback_run_token=run_token,
        )
        _cleanup_k8s_jobs(contexts)
        return None

    # verdict == 'unknown'：先主动 stop，再拿一次证据；只有确认到 terminal 才允许落库。
    _stop_runs_on_agents(contexts)
    evidence_after_stop = _fetch_terminal_evidence(contexts)
    verdict_after_stop, terminal_error_after_stop = _summarize_terminal_evidence(
        evidence_after_stop
    )

    if verdict_after_stop == "running":
        return _advance_or_fail_on_running_evidence(
            db,
            failed_run,
            run_id=run_id,
            agent_host=agent_host,
            run_token=run_token,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            started_at_ts=started_at_ts,
            inline_retry=inline_retry,
        )
        return None

    if verdict_after_stop in {"failed", "succeeded", "stopped"}:
        status_entries = _build_status_entries_from_terminal_evidence(
            contexts,
            evidence_after_stop,
        )
        _apply_terminal_evidence_outcome(
            db,
            failed_run,
            verdict_after_stop,
            terminal_error_after_stop,
            fallback_stop_reason=fallback_stop_reason,
            status_entries=status_entries,
            fallback_agent_host=agent_host,
            fallback_run_token=run_token,
        )
        _cleanup_k8s_jobs(contexts)
        return None

    # stop + 二次 refresh 仍拿不到 terminal 证据：按可观测证据不能直接 failed，
    # 只能保留 RUNNING 并继续等待后续 poll 拿到真实终态。
    return _advance_or_fail_on_running_evidence(
        db,
        failed_run,
        run_id=run_id,
        agent_host=agent_host,
        run_token=run_token,
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
        started_at_ts=started_at_ts,
        inline_retry=inline_retry,
    )


def _advance_or_fail_on_running_evidence(
    db,
    run: Run,
    *,
    run_id: int,
    agent_host: str,
    run_token: str,
    timeout_seconds: int,
    interval_seconds: int,
    started_at_ts: float,
    inline_retry: bool,
) -> Optional[dict[str, Any]]:
    """evidence 证明 agent 仍在 running，但我们已经耗尽 Celery 重试。

    只要 fresh evidence 仍明确是 running，就必须保持顶层 run 继续为 RUNNING 并重排下一轮 poll。
    exhausted handler 不能再因为重排次数达到阈值就把顶层写成 failed。
    """

    params = dict(run.params or {})
    prev_retry = params.get(_POLL_EVIDENCE_RETRY_KEY)
    try:
        prev_retry = int(prev_retry or 0)
    except (TypeError, ValueError):
        prev_retry = 0
    next_retry = prev_retry + 1

    params[_POLL_EVIDENCE_RETRY_KEY] = next_retry
    run.params = params
    db.commit()
    logger.info(
        "poll_run_status keeping run %s as RUNNING after exhausted retry: evidence=running retry=%s",
        run_id,
        next_retry,
    )
    return _reschedule_after_recursion_evidence(
        run_id=run_id,
        agent_host=agent_host,
        run_token=run_token,
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
        started_at_ts=started_at_ts,
        inline_retry=inline_retry,
        agent_runs=params.get("agent_runs"),
    )


@celery_app.task(
    bind=True,
    name="poll_run_status",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 30},
)
def poll_run_status(
    self,
    run_id: int,
    agent_host: str,
    run_token: str,
    timeout_seconds: int,
    interval_seconds: int,
    started_at_ts: float,
    inline_retry: bool = False,
    agent_runs: Optional[list[dict[str, Any]]] = None,
):
    """异步轮询 agent 状态，避免阻塞执行任务的 worker。"""
    db = SessionLocal()
    try:
        run = db.query(Run).filter(Run.run_id == run_id).first()
        if not run:
            logger.warning("poll_run_status: run %s not found", run_id)
            return
        previous_params = dict(run.params or {}) if isinstance(run.params, dict) else {}
        if run.run_status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.STOPPED}:
            contexts = _normalize_agent_run_contexts(
                agent_runs=agent_runs,
                agent_host=agent_host,
                run_token=run_token,
                fallback_k8s_job=(run.params or {}).get("k8s_job"),
            )
            _cleanup_k8s_jobs(contexts)
            return

        contexts = _normalize_agent_run_contexts(
            agent_runs=agent_runs or (run.params or {}).get("agent_runs"),
            agent_host=agent_host,
            run_token=run_token,
            fallback_k8s_job=(run.params or {}).get("k8s_job"),
        )
        status_entries = _build_context_status_entries(
            contexts,
            fallback_k8s_job=(run.params or {}).get("k8s_job"),
        )
        now_ts = datetime.now(timezone.utc)
        elapsed = now_ts.timestamp() - started_at_ts
        task_id = run.task_id
        for entry in status_entries:
            status_payload = entry.get("status_payload")
            status_value, error_detail = _normalize_agent_terminal_status(
                status_payload,
                elapsed_seconds=elapsed,
                missing_pid_grace_seconds=float(
                    os.getenv("AGENT_RUNNING_WITHOUT_PID_GRACE_SECONDS", "20")
                ),
                finalizing_pid_grace_seconds=float(
                    os.getenv(
                        "AGENT_RUNNING_WITHOUT_PID_FINALIZING_GRACE_SECONDS", "15"
                    )
                ),
            )
            entry["status_value"] = status_value
            entry["error_detail"] = error_detail
            entry["agent_ip"] = (status_payload or {}).get("agent_ip")
            entry["log_s3"] = (status_payload or {}).get("log_s3")
            entry["metrics_s3"] = (status_payload or {}).get("metrics_s3")
            entry["jtl_summary"] = (status_payload or {}).get("jtl_summary")
            entry["k6_summary"] = (status_payload or {}).get("k6_summary")
            entry["raw_observability"] = (status_payload or {}).get("raw_observability")
            entry["pod_monitor_series"] = (status_payload or {}).get(
                "pod_monitor_series"
            )
            entry["k8s_log_tail"] = None
            entry["k8s_log_s3"] = None
            entry["k8s_events"] = None
            k8s_meta = entry.get("k8s_job")
            if (
                (not status_value or status_value == "running")
                and k8s_meta
                and os.getenv("USE_K8S_AGENT") == "1"
            ):
                k8s_status = orchestrator.fetch_k8s_job_status(k8s_meta)
                if k8s_status:
                    k8s_phase = k8s_status.get("phase")
                    entry["k8s_log_tail"] = k8s_status.get("log_tail")
                    if k8s_phase == "Succeeded":
                        entry["status_value"] = entry["status_value"] or "succeeded"
                    elif k8s_phase in {"Failed", "Unknown"}:
                        entry["status_value"] = "failed"
                        entry["error_detail"] = (
                            entry["error_detail"]
                            or k8s_status.get("detail")
                            or "k8s_job_failed"
                        )
                    if not entry["error_detail"] and k8s_status.get("detail"):
                        entry["error_detail"] = k8s_status["detail"]
                full_logs = orchestrator.fetch_k8s_logs(k8s_meta, tail=2000)
                if full_logs:
                    entry["k8s_log_s3"] = orchestrator.upload_k8s_logs_to_s3(
                        k8s_meta,
                        full_logs,
                    )
                entry["k8s_events"] = orchestrator.fetch_k8s_events(k8s_meta, limit=50)

        status_value, error_detail = _aggregate_poll_status(status_entries)
        primary_entry = status_entries[0] if status_entries else {}
        status_payload = primary_entry.get("status_payload")

        if _should_refresh_timeout_boundary_status(
            status_payload=status_payload,
            status_value=status_value,
            error_detail=error_detail,
            elapsed_seconds=elapsed,
            timeout_seconds=timeout_seconds,
        ):
            refreshed_entries = []
            for entry in status_entries:
                refreshed_payload = _refresh_timeout_boundary_status(
                    entry["agent_host"],
                    entry["run_token"],
                )
                if refreshed_payload is None:
                    refreshed_entries.append(entry)
                    continue
                refreshed_entries.append(
                    {
                        **entry,
                        "status_payload": refreshed_payload,
                    }
                )
            status_entries = refreshed_entries
            now_ts = datetime.now(timezone.utc)
            elapsed = now_ts.timestamp() - started_at_ts
            for entry in status_entries:
                payload = entry.get("status_payload")
                refreshed_status, refreshed_error = _normalize_agent_terminal_status(
                    payload,
                    elapsed_seconds=elapsed,
                    missing_pid_grace_seconds=float(
                        os.getenv("AGENT_RUNNING_WITHOUT_PID_GRACE_SECONDS", "20")
                    ),
                    finalizing_pid_grace_seconds=float(
                        os.getenv(
                            "AGENT_RUNNING_WITHOUT_PID_FINALIZING_GRACE_SECONDS", "15"
                        )
                    ),
                )
                entry["status_value"] = refreshed_status
                entry["error_detail"] = refreshed_error
                entry["agent_ip"] = (payload or {}).get("agent_ip")
                entry["log_s3"] = (payload or {}).get("log_s3")
                entry["metrics_s3"] = (payload or {}).get("metrics_s3")
                entry["jtl_summary"] = (payload or {}).get("jtl_summary")
                entry["k6_summary"] = (payload or {}).get("k6_summary")
                entry["raw_observability"] = (payload or {}).get("raw_observability")
                entry["pod_monitor_series"] = (payload or {}).get("pod_monitor_series")
            status_value, error_detail = _aggregate_poll_status(status_entries)
            primary_entry = status_entries[0] if status_entries else {}
            status_payload = primary_entry.get("status_payload")

        merged_params, aggregated_jtl_summary, aggregated_k6_summary, primary_entry = (
            _merge_run_params_from_status_entries(
                run,
                status_entries=status_entries,
                fallback_agent_host=agent_host,
                fallback_run_token=run_token,
            )
        )
        run.params = merged_params

        if status_value in {"succeeded", "failed", "stopped"}:
            if status_value == "succeeded":
                run.run_status = RunStatus.SUCCEEDED
                run.run_status_detail = None
                run.stop_reason = None
            elif status_value == "failed":
                run.run_status = RunStatus.FAILED
                run.run_status_detail = "expand_failed"
                if error_detail:
                    run.stop_reason = error_detail
            else:
                run.run_status = RunStatus.STOPPED
                run.run_status_detail = None
                if error_detail:
                    run.stop_reason = error_detail
            ended_at_ts = (
                _parse_agent_ended_at((status_payload or {}).get("ended_at")) or now_ts
            )
            run.ended_at = ended_at_ts
            start_ts = run.started_at
            if start_ts and start_ts.tzinfo is None:
                start_ts = start_ts.replace(tzinfo=timezone.utc)
            if start_ts:
                run.duration_seconds = max(
                    0, int((ended_at_ts - start_ts).total_seconds())
                )
            _apply_real_execution_summary(
                run, aggregated_jtl_summary, aggregated_k6_summary
            )
            db.commit()
            _cleanup_k8s_jobs(contexts)

            # 生成报告（容错，不影响主流程）
            try:
                from app.services.run_service import RunService
                from app.tasks.report_generator import generate_test_summary_report

                service = RunService(db)
                metrics = service.get_metrics(run_id=run.run_id, step_seconds=5)
                summary = {}
                for s in metrics.series:
                    if not s.points:
                        continue
                    last = s.points[-1]
                    summary[s.metric.value] = last.value
                    summary[f"{s.metric.value}_points"] = [
                        (p.ts.isoformat(), p.value) for p in s.points[-20:]
                    ]
                summary.update(
                    {
                        "agent_host": primary_entry.get("agent_host") or agent_host,
                        "agent_ip": merged_params.get("agent_ip"),
                        "log_s3": merged_params.get("log_s3"),
                        "metrics_s3": merged_params.get("metrics_s3"),
                        "run_token": primary_entry.get("run_token") or run_token,
                        "run_status": run.run_status.value,
                        "run_status_detail": run.run_status_detail,
                        "run_id": run.run_id,
                        "task_id": task_id,
                        "jtl_summary": aggregated_jtl_summary,
                        "k6_summary": aggregated_k6_summary,
                    }
                )
                _overlay_report_summary_fields(
                    summary,
                    jtl_summary=aggregated_jtl_summary,
                    k6_summary=aggregated_k6_summary,
                )
                if os.environ.get("TESTING", "0") == "1":
                    generate_test_summary_report(task_id, summary)
                else:
                    generate_test_summary_report.delay(task_id, summary)
            except Exception as exc:  # pragma: no cover - 报告生成失败不阻塞主流程
                logger.warning(
                    "generate summary report failed for run %s: %s", run_id, exc
                )
            return

        db.commit()

        timeout_grace_delay = _resolve_timeout_grace_delay_seconds(
            status_payload=status_payload,
            status_value=status_value,
            error_detail=error_detail,
            elapsed_seconds=elapsed,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
        )
        if timeout_grace_delay is not None:
            next_kwargs = {
                "run_id": run_id,
                "agent_host": agent_host,
                "run_token": run_token,
                "timeout_seconds": timeout_seconds,
                "interval_seconds": interval_seconds,
                "started_at_ts": started_at_ts,
                "inline_retry": inline_retry,
                "agent_runs": merged_params.get("agent_runs"),
            }
            logger.info(
                "poll_run_status entering timeout grace window for run %s: "
                "elapsed=%.2fs timeout=%ss grace_delay=%ss status=%s",
                run_id,
                elapsed,
                timeout_seconds,
                timeout_grace_delay,
                status_value,
            )
            if inline_retry:
                time.sleep(timeout_grace_delay)
                return _build_inline_poll_continue(next_kwargs)
            elif os.environ.get("TESTING", "0") == "1":
                time.sleep(timeout_grace_delay)
                _apply_task_inline(poll_run_status, **next_kwargs)
            else:
                poll_run_status.apply_async(
                    kwargs=next_kwargs, countdown=timeout_grace_delay
                )
            return

        timeout_running_evidence_delay = (
            _resolve_timeout_running_evidence_delay_seconds(
                status_payload=status_payload,
                status_value=status_value,
                error_detail=error_detail,
                elapsed_seconds=elapsed,
                timeout_seconds=timeout_seconds,
                interval_seconds=interval_seconds,
                previous_params=previous_params,
                current_params=merged_params,
            )
        )
        if timeout_running_evidence_delay is not None:
            next_kwargs = {
                "run_id": run_id,
                "agent_host": agent_host,
                "run_token": run_token,
                "timeout_seconds": timeout_seconds,
                "interval_seconds": interval_seconds,
                "started_at_ts": started_at_ts,
                "inline_retry": inline_retry,
                "agent_runs": merged_params.get("agent_runs"),
            }
            logger.info(
                "poll_run_status keeping timed run alive due to running progress evidence: "
                "run=%s elapsed=%.2fs timeout=%ss extension_delay=%ss status=%s",
                run_id,
                elapsed,
                timeout_seconds,
                timeout_running_evidence_delay,
                status_value,
            )
            if inline_retry:
                time.sleep(timeout_running_evidence_delay)
                return _build_inline_poll_continue(next_kwargs)
            elif os.environ.get("TESTING", "0") == "1":
                time.sleep(timeout_running_evidence_delay)
                _apply_task_inline(poll_run_status, **next_kwargs)
            else:
                poll_run_status.apply_async(
                    kwargs=next_kwargs, countdown=timeout_running_evidence_delay
                )
            return

        if elapsed >= timeout_seconds:
            _stop_runs_on_agents(contexts)
            _mark_failed(db, run, "timeout")
            _cleanup_k8s_jobs(contexts)
            return

        next_kwargs = {
            "run_id": run_id,
            "agent_host": agent_host,
            "run_token": run_token,
            "timeout_seconds": timeout_seconds,
            "interval_seconds": interval_seconds,
            "started_at_ts": started_at_ts,
            "inline_retry": inline_retry,
            "agent_runs": merged_params.get("agent_runs"),
        }
        if inline_retry:
            time.sleep(interval_seconds)
            return _build_inline_poll_continue(next_kwargs)
        elif os.environ.get("TESTING", "0") == "1":
            time.sleep(interval_seconds)
            _apply_task_inline(poll_run_status, **next_kwargs)
        else:
            poll_run_status.apply_async(kwargs=next_kwargs, countdown=interval_seconds)
    except Exception as exc:
        logger.error("poll_run_status failed for run %s: %s", run_id, exc)
        if _is_retry_exhausted(self):
            try:
                inline_result = _handle_exhausted_retry_evidence(
                    db,
                    run_id=run_id,
                    agent_host=agent_host,
                    run_token=run_token,
                    timeout_seconds=timeout_seconds,
                    interval_seconds=interval_seconds,
                    started_at_ts=started_at_ts,
                    inline_retry=inline_retry,
                    agent_runs_override=agent_runs,
                    cause=exc,
                )
                if inline_result is not None:
                    return inline_result
            except Exception as mark_exc:  # pragma: no cover - 容错
                logger.error(
                    "poll_run_status exhausted-retries terminal-evidence handler failed for run %s: %s",
                    run_id,
                    mark_exc,
                )
        raise
    finally:
        db.close()
