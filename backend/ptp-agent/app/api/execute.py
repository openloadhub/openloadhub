import asyncio
import base64
from collections import defaultdict
import hashlib
import logging
import os
import re
import uuid
import time
import signal
import math
import socket
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple
import threading
import subprocess
import shutil
from urllib.parse import quote, urlsplit, urlunsplit
from zipfile import BadZipFile, ZipFile

from fastapi import APIRouter, HTTPException
import httpx

from app.schemas.execute_request import (
    ExecuteRequest,
    ExecuteResponse,
    K6ControlRequest,
)
from app.core.runtime_store import (
    RunLog,
    RunState,
    _build_agent_host_label,
    _is_host_runtime_identity,
    store,
    simulate_run,
)
from app.core.jmeter_runner import JMeterRunner
from app.core.k6_runner import K6Runner
from app.core.jtl_parser import parse_jtl, summarize_samples
from common.utils import s3_utils
from common.config.settings import get_run_artifact_prefix, settings

logger = logging.getLogger(__name__)
router = APIRouter()
AGENT_RUN_ROOT = Path("/tmp/agent_runs")
_NUMERIC_DATA_FIELD_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_HEADER_LABEL_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_ ./:-]*$")
_ANY_DIGIT_RE = re.compile(r"\d")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_RUNTIME_DATA_ZIP_EXTENSIONS = {".csv", ".txt", ".json"}
_K6_CONTROL_TIMEOUT_SECONDS = 2.0
_K6_TPS_CONTROLLER_INTERVAL_SECONDS = 5.0
_K6_TPS_CONTROLLER_DEADBAND_RATIO = 0.1
_K6_TPS_CONTROLLER_MAX_STEP_RATIO = 0.35
_K6_TPS_COUNTER_DELTA_MIN_SECONDS = 3.0
_K6_SCENARIO_DIRECT_BACKPRESSURE_WINDOW_SECONDS = 180.0
_K6_SCENARIO_DIRECT_UPSHIFT_BLOCKED_DETAIL = (
    "scenario_direct_upshift_blocked_recent_runtime_backpressure"
)
_K6_SCENARIO_DIRECT_BACKPRESSURE_MARKERS = (
    "Could not get a VU from the buffer",
    "Error while allocating unplanned VU",
)
# public runtime reconciliation：scenario_direct 广播以前只看 PATCH 成功 + config 回显就写 applied，
# 但真实吞吐仍停在旧基线。以下阈值/环境变量控制 applied 前的 runtime post-check：
# 1) 先校验 /v1/config 里每个 scenario 的 rate 和下发目标是否匹配（绝对容差 + 相对容差）；
# 2) 再做一次短窗口 /v1/metrics 观察，拿 counter delta / s 估算真实吞吐。
_K6_APPLIED_POSTCHECK_WINDOW_SECONDS_DEFAULT = 5.0
_K6_APPLIED_POSTCHECK_SAMPLE_INTERVAL_DEFAULT = 1.5
_K6_APPLIED_POSTCHECK_MIN_RATIO_DEFAULT = 0.6
_K6_APPLIED_POSTCHECK_ABSOLUTE_TOLERANCE_DEFAULT = 5.0
_K6_SCENARIO_DIRECT_ADJUSTING_TIMEOUT_SECONDS_DEFAULT = 120.0
_K6_SCENARIO_DIRECT_ADJUSTING_MIN_UPSHIFT_RATIO_DEFAULT = 1.6
_K6_SCENARIO_DIRECT_ADJUSTING_STICKY_CURRENT_RATIO_DEFAULT = 0.45
_K6_SCENARIO_DIRECT_BACKPRESSURE_BLOCK_UNDER_TARGET_RATIO_DEFAULT = 0.9
_K6_APPLIED_POSTCHECK_CONFIG_REL_TOL = 0.05
_K6_APPLIED_POSTCHECK_CONFIG_ABS_TOL = 1.0
_K6_SCENARIO_DIRECT_RUNTIME_NOT_APPLIED_DETAIL = "scenario_direct_runtime_not_applied"
_RUN_SCOPED_POD_METRIC_LABELS = (
    "run_token",
    "run_id",
    "agent_host",
    "instance",
    "name",
    "pod_ip",
    "pod_name",
    "node_name",
    "node_label",
)


def _coerce_positive_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _round_optional_float(value: Optional[float], digits: int = 4) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _coerce_optional_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_k6_time_unit_seconds(value: Any) -> Optional[float]:
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    matched = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", raw)
    if not matched:
        return None
    amount = float(matched.group(1))
    unit = matched.group(2)
    if amount <= 0:
        return None
    if unit == "ms":
        return amount / 1000.0
    if unit == "s":
        return amount
    if unit == "m":
        return amount * 60.0
    if unit == "h":
        return amount * 3600.0
    return None


def _compute_k6_scenario_config_total_tps(
    scenario_configs: list[dict[str, Any]],
) -> Optional[float]:
    total = 0.0
    found = False
    for item in scenario_configs:
        if not isinstance(item, dict):
            continue
        rate = item.get("rate")
        if rate is None:
            continue
        try:
            normalized_rate = float(rate)
        except (TypeError, ValueError):
            continue
        time_unit_seconds = (
            _parse_k6_time_unit_seconds(item.get("time_unit") or item.get("timeUnit"))
            or 1.0
        )
        if time_unit_seconds <= 0:
            continue
        total += normalized_rate / time_unit_seconds
        found = True
    return total if found else None


def _compute_k6_scenario_config_total_max_vus(
    scenario_configs: list[dict[str, Any]],
) -> Optional[int]:
    total = 0
    found = False
    for item in scenario_configs:
        if not isinstance(item, dict):
            continue
        raw_value = item.get("max_vus")
        if raw_value is None:
            raw_value = item.get("maxVUs")
        parsed = _coerce_positive_int(raw_value)
        if parsed is None:
            continue
        total += parsed
        found = True
    return total if found else None


def _detect_recent_k6_runtime_backpressure(
    state: RunState,
    *,
    now: Optional[datetime] = None,
    window_seconds: float = _K6_SCENARIO_DIRECT_BACKPRESSURE_WINDOW_SECONDS,
) -> Optional[str]:
    state.load_logs_from_file()
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(
        seconds=max(window_seconds, 0.0)
    )
    for log in reversed(state.logs):
        if not str(log.source or "").startswith("tool-"):
            continue
        log_ts = log.ts
        if not isinstance(log_ts, datetime):
            continue
        if log_ts.tzinfo is None:
            log_ts = log_ts.replace(tzinfo=timezone.utc)
        else:
            log_ts = log_ts.astimezone(timezone.utc)
        if log_ts < cutoff:
            break
        message = str(log.message or "")
        for marker in _K6_SCENARIO_DIRECT_BACKPRESSURE_MARKERS:
            if marker in message:
                return marker
    return None


def _allocate_loopback_control_address() -> tuple[str, int]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()
    return f"{host}:{port}", int(port)


def _build_k6_control_mode(state: RunState) -> Optional[str]:
    if state.k6_active_control_path:
        return state.k6_active_control_path
    if state.k6_controller_enabled and state.k6_target_tps:
        return "auto_tps_fallback"
    return state.k6_control_mode


def _safe_iso(ts: Optional[datetime]) -> Optional[str]:
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).isoformat()


def _resolve_k6_status_patch_reason(state: RunState) -> str:
    return str(state.k6_status_patch_reason or "k6_status_patch_not_supported")


def _resolve_k6_control_strategy(state: RunState) -> str:
    if state.k6_scenario_patch_supported:
        return "scenario_direct"
    if state.k6_status_patch_supported:
        return "auto_tps_fallback"
    return str(state.k6_active_control_path or "blocked")


def _resolve_k6_control_unavailable_reason(state: RunState) -> str:
    if state.k6_scenario_patch_reason:
        return str(state.k6_scenario_patch_reason)
    return _resolve_k6_status_patch_reason(state)


def _build_runtime_scenario_config_payload(
    scenario_configs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in scenario_configs:
        if not isinstance(item, dict):
            continue
        scenario_name = str(item.get("scenario_name") or "").strip()
        if not scenario_name:
            continue
        next_item: dict[str, Any] = {"scenarioName": scenario_name}
        if item.get("rate") is not None:
            next_item["rate"] = int(item["rate"])
        if item.get("max_vus") is not None:
            next_item["maxVUs"] = int(item["max_vus"])
        if item.get("vus") is not None:
            next_item["vus"] = int(item["vus"])
        if item.get("time_unit"):
            next_item["timeUnit"] = item["time_unit"]
        if item.get("duration"):
            next_item["duration"] = item["duration"]
        if item.get("pre_allocated_vus") is not None:
            next_item["preAllocatedVUs"] = int(item["pre_allocated_vus"])
        if item.get("executor"):
            next_item["executor"] = item["executor"]
        if item.get("exec_name"):
            next_item["exec"] = item["exec_name"]
        payload.append(next_item)
    return payload


def _apply_k6_scenario_config_patch(
    state: RunState, scenario_configs: list[dict[str, Any]]
) -> dict[str, Any]:
    payload = {
        "data": {
            "scenarioConfigs": _build_runtime_scenario_config_payload(scenario_configs)
        }
    }
    response = _k6_control_request(state, "PATCH", "/v1/status", payload)
    state.k6_control_last_synced_at = datetime.now(timezone.utc)
    return response


def _rebuild_k6_standard_scenario_configs(
    state: RunState,
    target_tps: float,
) -> list[dict[str, Any]]:
    if not state.k6_script_path:
        return list(state.k6_scenario_configs or [])
    script_path = Path(state.k6_script_path)
    runtime_properties = dict(state.k6_runtime_properties or {})
    runtime_properties["target_tps"] = str(
        _round_optional_float(target_tps) or target_tps
    )
    family, scenario_configs = K6Runner.describe_standard_scenario_configs(
        script_path=script_path,
        vus=_coerce_positive_int(runtime_properties.get("PTP_THREAD_COUNT")) or 1,
        duration=_coerce_positive_int(runtime_properties.get("PTP_DURATION_SECONDS"))
        or 1,
        envs=runtime_properties,
    )
    if family:
        state.k6_script_family = family
    serialized = K6Runner.serialize_scenario_configs(scenario_configs)
    return _stabilize_runtime_scenario_direct_configs(
        state,
        target_tps=target_tps,
        scenario_configs=serialized,
    )


def _stabilize_runtime_scenario_direct_configs(
    state: RunState,
    *,
    target_tps: float,
    scenario_configs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not scenario_configs or not state.k6_scenario_configs:
        return scenario_configs

    current_time_units = {
        str(item.get("time_unit") or "").strip()
        for item in state.k6_scenario_configs
        if isinstance(item, dict) and str(item.get("time_unit") or "").strip()
    }
    next_time_units = {
        str(item.get("time_unit") or "").strip()
        for item in scenario_configs
        if isinstance(item, dict) and str(item.get("time_unit") or "").strip()
    }
    if current_time_units != {"1s"} or next_time_units == {"1s"}:
        return scenario_configs

    runtime_properties = dict(state.k6_runtime_properties or {})
    agent_total = _coerce_positive_int(
        runtime_properties.get("pod_count")
        or runtime_properties.get("pod_num")
        or runtime_properties.get("POD_COUNT")
    )
    agent_index = _coerce_positive_int(
        runtime_properties.get("agent_slice_index")
        or runtime_properties.get("pod_index")
    )
    if (
        not agent_total
        or agent_total <= 1
        or not agent_index
        or agent_index > agent_total
    ):
        return scenario_configs

    normalized_tps = math.floor(float(target_tps))
    local_scenario_total = len(scenario_configs)
    if normalized_tps <= 0 or local_scenario_total <= 0:
        return scenario_configs

    local_target_tps = normalized_tps // agent_total
    if (agent_index - 1) < (normalized_tps % agent_total):
        local_target_tps += 1
    if local_target_tps < local_scenario_total:
        return scenario_configs

    stabilized_configs: list[dict[str, Any]] = []
    for scenario_index, item in enumerate(scenario_configs):
        per_scenario_rate = local_target_tps // local_scenario_total
        if scenario_index < (local_target_tps % local_scenario_total):
            per_scenario_rate += 1
        next_item = dict(item)
        next_item["rate"] = max(1, int(per_scenario_rate))
        next_item["time_unit"] = "1s"
        stabilized_configs.append(next_item)
    return stabilized_configs


def _k6_control_request_any(
    state: RunState,
    method: str,
    path: str,
    payload: Optional[dict[str, Any]] = None,
) -> Any:
    if not state.k6_control_url:
        raise RuntimeError("k6_control_url_missing")
    url = f"{state.k6_control_url}{path}"
    with httpx.Client(timeout=_K6_CONTROL_TIMEOUT_SECONDS, trust_env=False) as client:
        response = client.request(method, url, json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            response_text = str(getattr(exc.response, "text", "") or "")
            if "externally-controlled executor" in response_text:
                state.k6_status_patch_supported = False
                state.k6_status_patch_reason = "externally_controlled_executor_required"
                state.k6_control_error = state.k6_status_patch_reason
                raise RuntimeError(state.k6_status_patch_reason) from exc
            raise
        return response.json()


def _k6_control_request(
    state: RunState,
    method: str,
    path: str,
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    data = _k6_control_request_any(state, method, path, payload)
    if not isinstance(data, dict):
        raise RuntimeError("k6_control_invalid_response")
    return data


def _extract_k6_status_attributes(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    attributes = data.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def _normalize_k6_runtime_scenario_config(item: Any) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    scenario_name = str(
        item.get("scenario-name") or item.get("scenarioName") or item.get("name") or ""
    ).strip()
    if not scenario_name:
        return None
    return {
        "scenario_name": scenario_name,
        "executor": item.get("executor"),
        "rate": _coerce_positive_int(item.get("rate")),
        "time_unit": item.get("timeUnit"),
        "pre_allocated_vus": _coerce_positive_int(item.get("preAllocatedVUs")),
        "max_vus": _coerce_positive_int(item.get("maxVUs")),
        "vus": _coerce_positive_int(item.get("vus")),
        "duration": item.get("duration"),
        "exec_name": item.get("exec"),
    }


def _fetch_k6_runtime_scenario_configs(state: RunState) -> list[dict[str, Any]]:
    payload = _k6_control_request_any(state, "GET", "/v1/config")
    if not isinstance(payload, list):
        raise RuntimeError("k6_config_invalid_response")
    configs: list[dict[str, Any]] = []
    for item in payload:
        normalized = _normalize_k6_runtime_scenario_config(item)
        if normalized is not None:
            configs.append(normalized)
    return configs


def _derive_display_k6_current_vus(
    current_vus: int,
    runtime_scenario_configs: list[dict[str, Any]],
    fallback_target_vus: Optional[int],
) -> int:
    normalized_current = max(0, int(current_vus or 0))
    if normalized_current > 0:
        return normalized_current

    configured_floor = 0
    for item in runtime_scenario_configs:
        if not isinstance(item, dict):
            continue
        configured_floor += (
            _coerce_positive_int(item.get("pre_allocated_vus"))
            or _coerce_positive_int(item.get("vus"))
            or 0
        )
    if configured_floor > 0:
        return configured_floor

    fallback = _coerce_positive_int(fallback_target_vus)
    return fallback or 0


def _derive_display_k6_scenario_pre_allocated_vus(
    runtime_scenario_configs: list[dict[str, Any]],
    fallback_target_vus: Optional[int],
) -> int:
    configured_floor = 0
    for item in runtime_scenario_configs:
        if not isinstance(item, dict):
            continue
        configured_floor += (
            _coerce_positive_int(item.get("pre_allocated_vus"))
            or _coerce_positive_int(item.get("vus"))
            or 0
        )
    if configured_floor > 0:
        return configured_floor
    return _coerce_positive_int(fallback_target_vus) or 0


def _derive_display_k6_current_max_vus(
    current_max_vus: Optional[int],
    runtime_scenario_configs: list[dict[str, Any]],
    fallback_target_max_vus: Optional[int],
) -> int:
    normalized_current = max(0, int(current_max_vus or 0))

    configured_ceiling = 0
    for item in runtime_scenario_configs:
        if not isinstance(item, dict):
            continue
        configured_ceiling += (
            _coerce_positive_int(item.get("max_vus"))
            or _coerce_positive_int(item.get("vus"))
            or _coerce_positive_int(item.get("pre_allocated_vus"))
            or 0
        )

    fallback = _coerce_positive_int(fallback_target_max_vus) or 0
    return max(normalized_current, configured_ceiling, fallback)


def _extract_k6_metric_sample(
    metrics_payload: dict[str, Any],
    metric_id: str,
    field_names: tuple[str, ...],
) -> Optional[float]:
    raw_items = metrics_payload.get("data")
    if not isinstance(raw_items, list):
        return None
    for item in raw_items:
        if not isinstance(item, dict) or item.get("id") != metric_id:
            continue
        attributes = item.get("attributes")
        if not isinstance(attributes, dict):
            return None
        sample = attributes.get("sample")
        if not isinstance(sample, dict):
            return None
        for field_name in field_names:
            value = sample.get(field_name)
            try:
                if value is None:
                    continue
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _extract_k6_live_metric_family(metrics_payload: dict[str, Any]) -> Optional[str]:
    raw_items = metrics_payload.get("data")
    if not isinstance(raw_items, list):
        return None
    metric_ids = {
        str(item.get("id") or "").strip()
        for item in raw_items
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    has_browser = "browser_http_reqs" in metric_ids
    has_http = "http_reqs" in metric_ids
    has_grpc = "grpc_req_duration" in metric_ids
    has_iterations = "iterations" in metric_ids
    if has_browser:
        return "browser"
    if has_http and has_grpc:
        return "mixed"
    if has_http:
        return "http"
    if has_grpc:
        return "grpc"
    if has_iterations:
        return "iteration"
    return None


def _extract_k6_live_observed_tps(
    metrics_payload: dict[str, Any],
) -> tuple[Optional[float], Optional[str]]:
    metric_family = _extract_k6_live_metric_family(metrics_payload)
    observed_tps: Optional[float] = None
    if metric_family == "mixed":
        values = [
            _extract_k6_metric_sample(metrics_payload, "http_reqs", ("rate",)),
            _extract_k6_metric_sample(metrics_payload, "grpc_req_duration", ("rate",)),
        ]
        summed = sum(value for value in values if value is not None)
        observed_tps = summed if summed > 0 else None
    elif metric_family == "browser":
        observed_tps = _extract_k6_metric_sample(
            metrics_payload, "browser_http_reqs", ("rate",)
        )
    elif metric_family == "http":
        observed_tps = _extract_k6_metric_sample(
            metrics_payload, "http_reqs", ("rate",)
        )
    elif metric_family == "grpc":
        observed_tps = _extract_k6_metric_sample(
            metrics_payload, "grpc_req_duration", ("rate",)
        )
    elif metric_family == "iteration":
        observed_tps = _extract_k6_metric_sample(
            metrics_payload, "iterations", ("rate",)
        )
    return observed_tps, metric_family


def _resolve_k6_tps_counter_metric_ids(metric_family: Optional[str]) -> tuple[str, ...]:
    if metric_family == "mixed":
        return ("http_reqs", "grpc_req_duration")
    if metric_family == "browser":
        return ("browser_http_reqs",)
    if metric_family == "http":
        return ("http_reqs",)
    if metric_family == "grpc":
        return ("grpc_req_duration",)
    if metric_family == "iteration":
        return ("iterations",)
    return ()


def _derive_k6_live_observed_tps_from_counts(
    state: RunState,
    metrics_payload: dict[str, Any],
    metric_family: Optional[str],
    sampled_at: datetime,
) -> Optional[float]:
    metric_ids = _resolve_k6_tps_counter_metric_ids(metric_family)
    if not metric_ids:
        return None

    current_counts: dict[str, float] = {}
    for metric_id in metric_ids:
        count_value = _extract_k6_metric_sample(metrics_payload, metric_id, ("count",))
        if count_value is None:
            return None
        current_counts[metric_id] = float(count_value)

    previous_counts = dict(state.k6_last_metric_counts or {})
    previous_sampled_at = state.k6_last_metric_sampled_at
    state.k6_last_metric_counts = current_counts
    state.k6_last_metric_sampled_at = sampled_at

    if not previous_counts or previous_sampled_at is None:
        return None

    elapsed_seconds = (sampled_at - previous_sampled_at).total_seconds()
    if elapsed_seconds < _K6_TPS_COUNTER_DELTA_MIN_SECONDS:
        return None

    total_delta = 0.0
    for metric_id, current_count in current_counts.items():
        previous_count = previous_counts.get(metric_id)
        if previous_count is None or current_count < previous_count:
            return None
        total_delta += current_count - previous_count

    if total_delta < 0:
        return None
    return total_delta / elapsed_seconds


def _extract_k6_live_iteration_rate(metrics_payload: dict[str, Any]) -> Optional[float]:
    rate = _extract_k6_metric_sample(metrics_payload, "iterations", ("rate",))
    if rate is None or rate <= 0:
        return None
    return float(rate)


def _resolve_k6_live_observed_tps(
    *,
    metrics_payload: dict[str, Any],
    metric_family: Optional[str],
    raw_observed_tps: Optional[float],
) -> Optional[float]:
    iteration_rate = _extract_k6_live_iteration_rate(metrics_payload)
    if metric_family == "mixed":
        http_rate = _extract_k6_metric_sample(metrics_payload, "http_reqs", ("rate",))
        grpc_rate = _extract_k6_metric_sample(
            metrics_payload, "grpc_req_duration", ("rate",)
        )
        if iteration_rate is not None and (
            raw_observed_tps is None
            or raw_observed_tps <= 0
            or (
                (http_rate is None or grpc_rate is None)
                and iteration_rate > raw_observed_tps + 1e-6
            )
        ):
            return iteration_rate
    if (
        metric_family == "grpc"
        and iteration_rate is not None
        and (raw_observed_tps is None or raw_observed_tps <= 0)
    ):
        return iteration_rate
    return raw_observed_tps


def _extract_k6_live_p95_ms(
    metrics_payload: dict[str, Any], metric_family: Optional[str]
) -> Optional[float]:
    if metric_family == "mixed":
        candidates = [
            _extract_k6_metric_sample(metrics_payload, "http_req_duration", ("p(95)",)),
            _extract_k6_metric_sample(metrics_payload, "grpc_req_duration", ("p(95)",)),
        ]
        values = [value for value in candidates if value is not None]
        return max(values) if values else None
    if metric_family == "browser":
        return _extract_k6_metric_sample(
            metrics_payload, "browser_http_req_duration", ("p(95)",)
        )
    if metric_family == "http":
        return _extract_k6_metric_sample(
            metrics_payload, "http_req_duration", ("p(95)",)
        )
    if metric_family == "grpc":
        return _extract_k6_metric_sample(
            metrics_payload, "grpc_req_duration", ("p(95)",)
        )
    if metric_family == "iteration":
        return _extract_k6_metric_sample(
            metrics_payload, "iteration_duration", ("p(95)",)
        )
    return None


def _maybe_append_live_k6_metrics(state: RunState) -> None:
    if state.k6_observed_tps is None:
        return
    now = datetime.now(timezone.utc)
    last = state.metric_history[-1] if state.metric_history else None
    if isinstance(last, dict):
        last_ts = last.get("ts")
        if isinstance(last_ts, datetime) and (now - last_ts).total_seconds() < 1:
            last["rps"] = float(state.k6_observed_tps)
            if state.rt_p95_ms is not None:
                last["rt_p95_ms"] = float(state.rt_p95_ms)
            return
    state.append_metrics(state.k6_observed_tps, state.rt_p95_ms)


def _refresh_k6_live_control_state(
    state: RunState,
    *,
    append_metric_history: bool = False,
) -> dict[str, Any]:
    if state.engine_type != "k6":
        state.k6_control_available = False
        state.k6_control_error = "engine_type_not_k6"
        return {
            "available": False,
            "reason": state.k6_control_error,
            "supports_target_tps": False,
        }

    if not state.k6_control_url:
        state.k6_control_available = False
        state.k6_control_error = "k6_control_not_initialized"
        return {
            "available": False,
            "reason": state.k6_control_error,
            "supports_target_tps": False,
        }

    try:
        status_payload = _k6_control_request(state, "GET", "/v1/status")
        status_attributes = _extract_k6_status_attributes(status_payload)
        metrics_payload = _k6_control_request(state, "GET", "/v1/metrics")
    except Exception as exc:
        state.k6_control_available = False
        state.k6_control_error = f"k6_control_unreachable: {exc}"
        state.k6_control_last_synced_at = datetime.now(timezone.utc)
        return {
            "available": False,
            "reason": state.k6_control_error,
            "supports_target_tps": False,
        }

    raw_current_vus = _coerce_positive_int(status_attributes.get("vus")) or 0
    current_max_vus = _coerce_positive_int(status_attributes.get("vus-max"))
    raw_observed_tps, metric_family = _extract_k6_live_observed_tps(metrics_payload)
    if metric_family is None and state.k6_metric_family:
        metric_family = state.k6_metric_family
    sampled_at = datetime.now(timezone.utc)
    derived_observed_tps = _derive_k6_live_observed_tps_from_counts(
        state,
        metrics_payload,
        metric_family,
        sampled_at,
    )
    observed_tps = (
        derived_observed_tps if derived_observed_tps is not None else raw_observed_tps
    )
    observed_tps = _resolve_k6_live_observed_tps(
        metrics_payload=metrics_payload,
        metric_family=metric_family,
        raw_observed_tps=observed_tps,
    )
    p95_ms = _extract_k6_live_p95_ms(metrics_payload, metric_family)
    control_strategy = _resolve_k6_control_strategy(state)
    runtime_scenario_configs = list(state.k6_scenario_configs or [])
    if state.k6_scenario_patch_supported:
        try:
            runtime_scenario_configs = _fetch_k6_runtime_scenario_configs(state)
            state.k6_scenario_configs = list(runtime_scenario_configs)
        except Exception:
            runtime_scenario_configs = list(state.k6_scenario_configs or [])
    current_vus = _derive_display_k6_current_vus(
        raw_current_vus,
        runtime_scenario_configs,
        state.k6_target_vus,
    )
    scenario_pre_allocated_vus = _derive_display_k6_scenario_pre_allocated_vus(
        runtime_scenario_configs,
        state.k6_target_vus,
    )
    current_max_vus = _derive_display_k6_current_max_vus(
        current_max_vus,
        runtime_scenario_configs,
        state.k6_target_max_vus,
    )
    supports_target_tps = bool(
        metric_family is not None
        and (state.k6_scenario_patch_supported or state.k6_status_patch_supported)
    )
    unavailable_reason = (
        None if supports_target_tps else _resolve_k6_control_unavailable_reason(state)
    )

    state.k6_control_available = supports_target_tps
    state.k6_control_error = unavailable_reason
    state.k6_control_last_synced_at = sampled_at
    state.k6_observed_tps = _round_optional_float(observed_tps)
    state.k6_metric_family = metric_family
    if observed_tps is not None:
        state.rps = float(observed_tps)
    if p95_ms is not None:
        state.rt_p95_ms = float(p95_ms)
    if append_metric_history:
        _maybe_append_live_k6_metrics(state)
    _reconcile_scenario_direct_controller_state(
        state,
        observed_tps=observed_tps,
        runtime_scenario_configs=runtime_scenario_configs,
        sampled_at=sampled_at,
    )
    return {
        "available": supports_target_tps,
        "reason": unavailable_reason,
        "supports_target_tps": supports_target_tps,
        "observed_tps": _round_optional_float(observed_tps),
        "active_vus": raw_current_vus,
        "scenario_pre_allocated_vus": scenario_pre_allocated_vus,
        "scenario_max_vus": current_max_vus,
        "current_vus": current_vus,
        "current_max_vus": current_max_vus,
        "target_tps": _round_optional_float(state.k6_target_tps),
        "controller_enabled": bool(state.k6_target_tps is not None),
        "controller_status": state.k6_controller_status,
        "controller_message": state.k6_controller_message,
        "metric_family": metric_family,
        "last_synced_at": _safe_iso(state.k6_control_last_synced_at),
        "control_strategy": control_strategy,
        "preferred_control_path": state.k6_preferred_control_path,
        "active_control_path": state.k6_active_control_path or control_strategy,
        "scenario_patch_supported": bool(state.k6_scenario_patch_supported),
        "scenario_patch_reason": state.k6_scenario_patch_reason,
        "script_family": state.k6_script_family,
        "scenario_configs": runtime_scenario_configs,
    }


def _compute_next_vus_for_target_tps(
    target_tps: float,
    observed_tps: Optional[float],
    current_vus: int,
    max_vus: int,
) -> tuple[int, Optional[str]]:
    bounded_current_vus = max(1, int(current_vus))
    bounded_max_vus = max(bounded_current_vus, int(max_vus))
    if observed_tps is None or observed_tps <= 0:
        next_vus = min(bounded_max_vus, bounded_current_vus + 1)
        message = f"observed_tps_unavailable current_vus={bounded_current_vus} next_vus={next_vus}"
        return next_vus, message

    ratio_delta = abs(target_tps - observed_tps) / max(target_tps, 1.0)
    if ratio_delta <= _K6_TPS_CONTROLLER_DEADBAND_RATIO:
        return bounded_current_vus, (
            f"within_deadband target_tps={target_tps:.2f} observed_tps={observed_tps:.2f}"
        )

    scaled_vus = max(
        1,
        int(round(bounded_current_vus * (target_tps / max(observed_tps, 0.001)))),
    )
    step_limit = max(
        1, int(math.ceil(bounded_current_vus * _K6_TPS_CONTROLLER_MAX_STEP_RATIO))
    )
    if scaled_vus > bounded_current_vus:
        next_vus = min(
            bounded_max_vus, min(scaled_vus, bounded_current_vus + step_limit)
        )
    else:
        next_vus = max(1, max(scaled_vus, bounded_current_vus - step_limit))

    if (
        next_vus == bounded_current_vus
        and observed_tps < target_tps
        and bounded_current_vus < bounded_max_vus
    ):
        next_vus = min(bounded_max_vus, bounded_current_vus + 1)
    elif (
        next_vus == bounded_current_vus
        and observed_tps > target_tps
        and bounded_current_vus > 1
    ):
        next_vus = max(1, bounded_current_vus - 1)

    message = (
        f"target_tps={target_tps:.2f} observed_tps={observed_tps:.2f} "
        f"current_vus={bounded_current_vus} next_vus={next_vus}"
    )
    return next_vus, message


def _apply_k6_status_patch(state: RunState, attrs: dict[str, Any]) -> dict[str, Any]:
    payload = {"data": {"attributes": attrs}}
    response = _k6_control_request(state, "PATCH", "/v1/status", payload)
    state.k6_control_last_synced_at = datetime.now(timezone.utc)
    return response


def _run_k6_tps_controller(token: str, state: RunState) -> None:
    stop_event = state.k6_controller_stop_event
    if stop_event is None:
        return

    while not stop_event.is_set():
        if state.status != "running":
            break

        try:
            live_state = _refresh_k6_live_control_state(
                state, append_metric_history=True
            )
            if not live_state.get("available"):
                state.k6_controller_status = "error"
                state.k6_controller_message = str(
                    live_state.get("reason") or "control_unavailable"
                )
                stop_event.wait(_K6_TPS_CONTROLLER_INTERVAL_SECONDS)
                continue

            if not state.k6_controller_enabled or not state.k6_target_tps:
                state.k6_controller_status = "idle"
                stop_event.wait(_K6_TPS_CONTROLLER_INTERVAL_SECONDS)
                continue

            current_vus = int(live_state.get("current_vus") or 1)
            current_max_vus = int(
                live_state.get("current_max_vus")
                or state.k6_target_max_vus
                or current_vus
            )
            target_max_vus = int(state.k6_target_max_vus or current_max_vus)
            observed_tps = live_state.get("observed_tps")
            next_vus, message = _compute_next_vus_for_target_tps(
                float(state.k6_target_tps),
                float(observed_tps) if observed_tps is not None else None,
                current_vus,
                target_max_vus,
            )

            desired_max_vus = max(target_max_vus, next_vus)
            patch_attrs: dict[str, Any] = {}
            if desired_max_vus != current_max_vus:
                patch_attrs["vus-max"] = desired_max_vus
            if next_vus != current_vus:
                patch_attrs["vus"] = next_vus

            if patch_attrs:
                _apply_k6_status_patch(state, patch_attrs)
                state.k6_target_vus = int(next_vus)
                state.k6_target_max_vus = int(desired_max_vus)
                state.k6_controller_status = "adjusting"
                state.k6_controller_message = message
                state.append_log(
                    "INFO",
                    (
                        "k6_tps_controller_applied "
                        f"token={token} observed_tps={_round_optional_float(float(observed_tps) if observed_tps is not None else None)} "
                        f"target_tps={_round_optional_float(state.k6_target_tps)} attrs={patch_attrs}"
                    ),
                )
            else:
                saturated = (
                    observed_tps is not None
                    and float(observed_tps) + 1e-6 < float(state.k6_target_tps)
                    and current_vus >= target_max_vus
                )
                state.k6_controller_status = "saturated" if saturated else "stable"
                state.k6_controller_message = message
        except Exception as exc:  # pragma: no cover - best effort loop
            state.k6_controller_status = "error"
            state.k6_controller_message = str(exc)
            state.append_log(
                "WARN", f"k6_tps_controller_failed token={token} err={exc}"
            )

        stop_event.wait(_K6_TPS_CONTROLLER_INTERVAL_SECONDS)


def _ensure_k6_tps_controller_started(token: str, state: RunState) -> None:
    thread = state.k6_controller_thread
    if thread is not None and getattr(thread, "is_alive", lambda: False)():
        return

    stop_event = threading.Event()
    thread = threading.Thread(
        target=_run_k6_tps_controller,
        args=(token, state),
        name=f"ptp-k6-tps-controller-{token}",
        daemon=True,
    )
    state.k6_controller_stop_event = stop_event
    state.k6_controller_thread = thread
    thread.start()


def _stop_k6_tps_controller(state: RunState) -> None:
    stop_event = state.k6_controller_stop_event
    thread = state.k6_controller_thread
    if stop_event is not None:
        stop_event.set()
    if thread is not None and getattr(thread, "is_alive", lambda: False)():
        thread.join(timeout=1.0)
    state.k6_controller_stop_event = None
    state.k6_controller_thread = None


def _build_k6_control_response_payload(token: str, state: RunState) -> dict[str, Any]:
    live_state = _refresh_k6_live_control_state(
        state,
        append_metric_history=state.status == "running",
    )
    return {
        "available": bool(live_state.get("available")),
        "reason": live_state.get("reason"),
        "run_token": token,
        "control_url": state.k6_control_url,
        "control_mode": _build_k6_control_mode(state),
        "supports_target_tps": bool(live_state.get("supports_target_tps")),
        "observed_tps": live_state.get("observed_tps"),
        "active_vus": live_state.get("active_vus"),
        "scenario_pre_allocated_vus": live_state.get("scenario_pre_allocated_vus"),
        "scenario_max_vus": live_state.get("scenario_max_vus"),
        "current_vus": live_state.get("current_vus"),
        "current_max_vus": live_state.get("current_max_vus"),
        "target_tps": live_state.get("target_tps"),
        "controller_enabled": bool(live_state.get("controller_enabled")),
        "controller_status": live_state.get("controller_status"),
        "controller_message": live_state.get("controller_message"),
        "metric_family": live_state.get("metric_family"),
        "last_synced_at": live_state.get("last_synced_at"),
        "control_strategy": live_state.get("control_strategy"),
        "preferred_control_path": live_state.get("preferred_control_path"),
        "active_control_path": live_state.get("active_control_path"),
        "scenario_patch_supported": bool(live_state.get("scenario_patch_supported")),
        "scenario_patch_reason": live_state.get("scenario_patch_reason"),
        "script_family": live_state.get("script_family"),
        "scenario_configs": live_state.get("scenario_configs") or [],
    }


def _refresh_running_k6_runtime_metrics(
    state: RunState,
    *,
    append_metric_history: bool = False,
) -> None:
    if (
        state.status != "running"
        or state.engine_type != "k6"
        or not state.k6_control_url
    ):
        return
    try:
        _refresh_k6_live_control_state(
            state, append_metric_history=append_metric_history
        )
    except Exception as exc:  # pragma: no cover - best effort runtime refresh
        logger.debug("k6 live runtime metrics refresh skipped: %s", exc)


def _resolve_postcheck_env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return default
    return max(0.0, parsed)


def _resolve_scenario_direct_adjusting_timeout_seconds() -> float:
    return _resolve_postcheck_env_float(
        "PTP_K6_SCENARIO_DIRECT_ADJUSTING_TIMEOUT_SECONDS",
        _K6_SCENARIO_DIRECT_ADJUSTING_TIMEOUT_SECONDS_DEFAULT,
    )


def _should_mark_scenario_direct_runtime_adjusting(
    current_target_tps: Optional[float],
    requested_target_tps: Optional[float],
    observed_tps: Optional[float],
) -> bool:
    if (
        current_target_tps is None
        or requested_target_tps is None
        or current_target_tps <= 0
        or requested_target_tps <= current_target_tps + 1e-6
        or observed_tps is None
        or observed_tps <= 0
    ):
        return False
    min_upshift_ratio = _resolve_postcheck_env_float(
        "PTP_K6_SCENARIO_DIRECT_ADJUSTING_MIN_UPSHIFT_RATIO",
        _K6_SCENARIO_DIRECT_ADJUSTING_MIN_UPSHIFT_RATIO_DEFAULT,
    )
    sticky_current_ratio = _resolve_postcheck_env_float(
        "PTP_K6_SCENARIO_DIRECT_ADJUSTING_STICKY_CURRENT_RATIO",
        _K6_SCENARIO_DIRECT_ADJUSTING_STICKY_CURRENT_RATIO_DEFAULT,
    )
    return (
        requested_target_tps / max(current_target_tps, 0.001) >= min_upshift_ratio
        and observed_tps >= current_target_tps * sticky_current_ratio
    )


def _should_block_scenario_direct_upshift_for_backpressure(
    *,
    current_target_tps: Optional[float],
    observed_tps: Optional[float],
) -> tuple[bool, float]:
    threshold_ratio = _resolve_postcheck_env_float(
        "PTP_K6_SCENARIO_DIRECT_BACKPRESSURE_BLOCK_UNDER_TARGET_RATIO",
        _K6_SCENARIO_DIRECT_BACKPRESSURE_BLOCK_UNDER_TARGET_RATIO_DEFAULT,
    )
    threshold_ratio = min(1.0, max(0.0, threshold_ratio))
    if current_target_tps is None or current_target_tps <= 0:
        return True, threshold_ratio
    if observed_tps is None or observed_tps <= 0:
        return True, threshold_ratio
    return observed_tps < current_target_tps * threshold_ratio, threshold_ratio


def _mark_scenario_direct_runtime_adjusting(
    state: RunState,
    *,
    global_target_tps: float,
    local_target_tps: float,
    observed_tps: Optional[float],
) -> None:
    timeout_seconds = _resolve_scenario_direct_adjusting_timeout_seconds()
    deadline_at = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    state.k6_target_tps = global_target_tps
    state.k6_controller_enabled = False
    state.k6_controller_status = "adjusting"
    state.k6_controller_message = (
        "scenario_direct_adjusting:"
        f"observed_tps={_round_optional_float(observed_tps)}"
        f"_target={local_target_tps:.4f}"
        f"_timeout_seconds={timeout_seconds:.1f}"
    )
    state.k6_scenario_direct_adjusting_local_target_tps = local_target_tps
    state.k6_scenario_direct_adjusting_deadline_at = deadline_at


def _compute_scenario_direct_local_target_tps(
    state: RunState, global_target_tps: float
) -> float:
    """按 pod_count / agent_slice_index 推导本 agent 在 scenario_direct 下应承担的 tps 份额。

    多 agent 拆分（例如 `plan_run 3788` 的 150 = 75 + 75）时，单 agent 的本地目标并不等于
    admin 下发的 global target；post-check 必须对齐本地目标，否则会把合法的 75 误判成
    `scenario_direct_runtime_not_applied`。
    """

    runtime_properties = dict(state.k6_runtime_properties or {})
    agent_total = _coerce_positive_int(
        runtime_properties.get("pod_count")
        or runtime_properties.get("pod_num")
        or runtime_properties.get("POD_COUNT")
    )
    agent_index = _coerce_positive_int(
        runtime_properties.get("agent_slice_index")
        or runtime_properties.get("pod_index")
    )
    if (
        not agent_total
        or agent_total <= 1
        or not agent_index
        or agent_index > agent_total
    ):
        return float(global_target_tps)

    normalized = int(math.floor(float(global_target_tps)))
    if normalized <= 0:
        return float(global_target_tps)

    base = normalized // agent_total
    if (agent_index - 1) < (normalized % agent_total):
        base += 1
    return float(base)


def _verify_scenario_direct_config_total_tps(
    scenario_configs: list[dict[str, Any]], target_tps: float
) -> Optional[str]:
    """public runtime reconciliation：校验 k6 /v1/config 回显的 scenarioConfigs 总 TPS 和下发目标匹配。

    返回 None 表示通过；返回字符串表示 reject reason。
    """
    observed_total = _compute_k6_scenario_config_total_tps(scenario_configs)
    if observed_total is None:
        return f"{_K6_SCENARIO_DIRECT_RUNTIME_NOT_APPLIED_DETAIL}:config_total_tps_unavailable"
    expected = float(target_tps)
    abs_tolerance = max(
        _K6_APPLIED_POSTCHECK_CONFIG_ABS_TOL,
        expected * _K6_APPLIED_POSTCHECK_CONFIG_REL_TOL,
    )
    if abs(observed_total - expected) > abs_tolerance:
        return (
            f"{_K6_SCENARIO_DIRECT_RUNTIME_NOT_APPLIED_DETAIL}:"
            f"config_total_tps={observed_total:.4f}"
            f"_target={expected:.4f}"
        )
    return None


def _observe_k6_runtime_tps_after_patch(
    state: RunState, *, target_tps: float
) -> Optional[float]:
    """public runtime reconciliation：PATCH 之后在一个短窗口里观察真实 counter delta / s。

    约定：如果窗口期间拿不到有效 delta 样本（k6 counter 静止、metrics 拉不到等），
    返回 `None`，调用方应视为 runtime 未切档，走 rejected 路径；不允许“拿不到证据 = applied”。
    """
    window = _resolve_postcheck_env_float(
        "PTP_K6_APPLIED_POSTCHECK_WINDOW_SECONDS",
        _K6_APPLIED_POSTCHECK_WINDOW_SECONDS_DEFAULT,
    )
    if window <= 0:
        logger.info(
            "k6 scenario_direct runtime post-check has no sampling window; runtime evidence will be treated as unavailable env=%s",
            os.getenv("PTP_K6_APPLIED_POSTCHECK_WINDOW_SECONDS"),
        )
        return None

    interval = _resolve_postcheck_env_float(
        "PTP_K6_APPLIED_POSTCHECK_SAMPLE_INTERVAL_SECONDS",
        _K6_APPLIED_POSTCHECK_SAMPLE_INTERVAL_DEFAULT,
    )
    if interval <= 0:
        interval = _K6_APPLIED_POSTCHECK_SAMPLE_INTERVAL_DEFAULT

    deadline = time.monotonic() + window
    best_observed: Optional[float] = None
    min_ratio = _resolve_postcheck_env_float(
        "PTP_K6_APPLIED_POSTCHECK_MIN_RATIO",
        _K6_APPLIED_POSTCHECK_MIN_RATIO_DEFAULT,
    )
    sample_attempts = 0
    while time.monotonic() < deadline:
        time.sleep(interval)
        sample_attempts += 1
        try:
            metrics_payload = _k6_control_request(state, "GET", "/v1/metrics")
        except Exception as exc:  # pragma: no cover - best effort sampling
            logger.debug(
                "k6 scenario_direct runtime post-check sample failed attempt=%s err=%s",
                sample_attempts,
                exc,
            )
            continue
        sampled_at = datetime.now(timezone.utc)
        observed = _derive_k6_live_observed_tps_from_counts(
            state,
            metrics_payload,
            state.k6_metric_family,
            sampled_at,
        )
        raw_observed, metric_family = _extract_k6_live_observed_tps(metrics_payload)
        if observed is None:
            observed = _resolve_k6_live_observed_tps(
                metrics_payload=metrics_payload,
                metric_family=metric_family,
                raw_observed_tps=raw_observed,
            )
        if observed is None:
            continue
        if best_observed is None or observed > best_observed:
            best_observed = observed
        # 一旦已经达到 ratio 就早退，不再多睡浪费时间
        if best_observed is not None and best_observed >= target_tps * min_ratio:
            return best_observed
    return best_observed


def _scenario_direct_runtime_rejected_reason(
    observed_tps: Optional[float], target_tps: float
) -> Optional[str]:
    """根据 runtime post-check 观察到的 tps 判断是否需要 reject。"""
    if observed_tps is None:
        return (
            f"{_K6_SCENARIO_DIRECT_RUNTIME_NOT_APPLIED_DETAIL}:no_runtime_delta_samples"
        )
    min_ratio = _resolve_postcheck_env_float(
        "PTP_K6_APPLIED_POSTCHECK_MIN_RATIO",
        _K6_APPLIED_POSTCHECK_MIN_RATIO_DEFAULT,
    )
    if observed_tps >= target_tps * min_ratio:
        return None
    return (
        f"{_K6_SCENARIO_DIRECT_RUNTIME_NOT_APPLIED_DETAIL}:"
        f"observed_tps={observed_tps:.4f}_target={target_tps:.4f}"
    )


def _reconcile_scenario_direct_controller_state(
    state: RunState,
    *,
    observed_tps: Optional[float],
    runtime_scenario_configs: list[dict[str, Any]],
    sampled_at: datetime,
) -> None:
    if state.k6_active_control_path != "scenario_direct" or state.k6_target_tps is None:
        state.k6_scenario_direct_adjusting_local_target_tps = None
        state.k6_scenario_direct_adjusting_deadline_at = None
        return

    local_target_tps = (
        state.k6_scenario_direct_adjusting_local_target_tps
        if state.k6_scenario_direct_adjusting_local_target_tps is not None
        else _compute_scenario_direct_local_target_tps(state, state.k6_target_tps)
    )
    config_reject_reason = _verify_scenario_direct_config_total_tps(
        runtime_scenario_configs, local_target_tps
    )
    if config_reject_reason is not None:
        state.k6_controller_status = "rejected"
        state.k6_controller_message = config_reject_reason
        state.k6_scenario_direct_adjusting_local_target_tps = None
        state.k6_scenario_direct_adjusting_deadline_at = None
        return

    if state.k6_controller_status != "adjusting":
        return

    runtime_reject_reason = _scenario_direct_runtime_rejected_reason(
        observed_tps, local_target_tps
    )
    if runtime_reject_reason is None:
        state.k6_controller_status = "applied"
        state.k6_controller_message = (
            f"scenario_direct target_tps={_round_optional_float(state.k6_target_tps)}"
        )
        state.k6_scenario_direct_adjusting_local_target_tps = None
        state.k6_scenario_direct_adjusting_deadline_at = None
        return

    deadline_at = state.k6_scenario_direct_adjusting_deadline_at
    if deadline_at is not None and sampled_at < deadline_at:
        state.k6_controller_message = (
            "scenario_direct_adjusting:"
            f"observed_tps={_round_optional_float(observed_tps)}"
            f"_target={local_target_tps:.4f}"
            f"_deadline={deadline_at.isoformat()}"
        )
        return

    state.k6_controller_status = "rejected"
    state.k6_controller_message = runtime_reject_reason
    state.k6_scenario_direct_adjusting_local_target_tps = None
    state.k6_scenario_direct_adjusting_deadline_at = None


def _reject_scenario_direct_runtime(state: RunState, reason: str) -> None:
    state.k6_controller_status = "rejected"
    state.k6_controller_message = reason
    state.k6_scenario_direct_adjusting_local_target_tps = None
    state.k6_scenario_direct_adjusting_deadline_at = None
    try:
        state.append_log("WARN", reason)
    except Exception:  # pragma: no cover - 日志失败不阻塞 raise
        pass
    raise HTTPException(status_code=409, detail=reason)


def _apply_k6_control_update(
    token: str, state: RunState, request: K6ControlRequest
) -> dict[str, Any]:
    live_state = _refresh_k6_live_control_state(state, append_metric_history=True)
    if not live_state.get("supports_target_tps"):
        raise HTTPException(
            status_code=409,
            detail=str(
                live_state.get("reason") or "target_tps_not_supported_for_current_run"
            ),
        )

    next_target_tps = float(request.target_tps)
    next_scenario_configs = _rebuild_k6_standard_scenario_configs(
        state, next_target_tps
    )
    next_control_mode = str(
        live_state.get("active_control_path") or _build_k6_control_mode(state)
    )

    control_strategy = str(
        live_state.get("control_strategy") or _resolve_k6_control_strategy(state)
    )
    if control_strategy == "scenario_direct":
        current_scenario_configs = live_state.get("scenario_configs")
        if not isinstance(current_scenario_configs, list):
            current_scenario_configs = list(state.k6_scenario_configs or [])
        current_local_target_tps = _compute_k6_scenario_config_total_tps(
            current_scenario_configs
        )
        next_local_target_tps = _compute_k6_scenario_config_total_tps(
            next_scenario_configs
        )
        current_effective_target_tps: Optional[float] = current_local_target_tps
        if current_effective_target_tps is None:
            try:
                raw_live_target_tps = live_state.get("target_tps")
                current_effective_target_tps = (
                    float(raw_live_target_tps)
                    if raw_live_target_tps is not None
                    else None
                )
            except (TypeError, ValueError):
                current_effective_target_tps = None
        next_effective_target_tps: Optional[float] = next_local_target_tps
        if next_effective_target_tps is None:
            next_effective_target_tps = next_target_tps
        if (
            current_effective_target_tps is not None
            and next_effective_target_tps is not None
            and next_effective_target_tps > current_effective_target_tps + 1e-6
        ):
            recent_backpressure_marker = _detect_recent_k6_runtime_backpressure(state)
            if recent_backpressure_marker:
                observed_tps = _coerce_optional_float(live_state.get("observed_tps"))
                current_total_max_vus = _compute_k6_scenario_config_total_max_vus(
                    current_scenario_configs
                )
                next_total_max_vus = _compute_k6_scenario_config_total_max_vus(
                    next_scenario_configs
                )
                threshold_ratio = _resolve_postcheck_env_float(
                    "PTP_K6_SCENARIO_DIRECT_BACKPRESSURE_BLOCK_UNDER_TARGET_RATIO",
                    _K6_SCENARIO_DIRECT_BACKPRESSURE_BLOCK_UNDER_TARGET_RATIO_DEFAULT,
                )
                state.append_log(
                    "WARN",
                    (
                        "scenario_direct_upshift_backpressure_marker_observed_but_upshift_allowed "
                        f'marker="{recent_backpressure_marker}" '
                        f"observed_tps={_round_optional_float(observed_tps)} "
                        f"current_target_tps={current_effective_target_tps:.4f} "
                        f"current_max_vus={current_total_max_vus} "
                        f"next_max_vus={next_total_max_vus} "
                        f"threshold_ratio={threshold_ratio:.4f}"
                    ),
                )
        _stop_k6_tps_controller(state)
        if next_scenario_configs:
            _apply_k6_scenario_config_patch(state, next_scenario_configs)
            try:
                next_scenario_configs = _fetch_k6_runtime_scenario_configs(state)
            except Exception:
                pass
            local_expected_target_tps = _compute_scenario_direct_local_target_tps(
                state, next_target_tps
            )
            # public runtime reconciliation post-check 1：/v1/config 回显的 rate 必须和本 agent 的目标份额匹配，
            # 否则 k6 runtime 其实没吃到新配置。
            config_reject_reason = _verify_scenario_direct_config_total_tps(
                next_scenario_configs, local_expected_target_tps
            )
            if config_reject_reason is not None:
                _reject_scenario_direct_runtime(state, config_reject_reason)
            # public runtime reconciliation post-check 2：短窗口 counter delta 观察，真实吞吐必须朝目标靠拢。
            # 只要拿不到 runtime 证据，或 runtime 仍停在旧基线，就不能写 applied。
            observed_tps = _observe_k6_runtime_tps_after_patch(
                state, target_tps=local_expected_target_tps
            )
            runtime_reject_reason = _scenario_direct_runtime_rejected_reason(
                observed_tps, local_expected_target_tps
            )
            if runtime_reject_reason is not None:
                if _should_mark_scenario_direct_runtime_adjusting(
                    current_effective_target_tps,
                    next_effective_target_tps,
                    observed_tps,
                ):
                    state.k6_scenario_configs = next_scenario_configs
                    state.k6_control_mode = next_control_mode
                    _mark_scenario_direct_runtime_adjusting(
                        state,
                        global_target_tps=next_target_tps,
                        local_target_tps=local_expected_target_tps,
                        observed_tps=observed_tps,
                    )
                    return _build_k6_control_response_payload(token, state)
                _reject_scenario_direct_runtime(state, runtime_reject_reason)
        state.k6_target_tps = next_target_tps
        state.k6_controller_enabled = False
        state.k6_scenario_configs = next_scenario_configs
        state.k6_control_mode = next_control_mode
        state.k6_controller_status = "applied"
        state.k6_controller_message = (
            f"scenario_direct target_tps={_round_optional_float(state.k6_target_tps)}"
        )
        state.k6_scenario_direct_adjusting_local_target_tps = None
        state.k6_scenario_direct_adjusting_deadline_at = None
    elif control_strategy == "auto_tps_fallback":
        current_vus = int(live_state.get("current_vus") or state.k6_target_vus or 1)
        current_max_vus = int(
            live_state.get("current_max_vus") or state.k6_target_max_vus or current_vus
        )
        state.k6_target_tps = next_target_tps
        state.k6_scenario_configs = next_scenario_configs
        state.k6_control_mode = next_control_mode
        state.k6_target_vus = current_vus
        state.k6_target_max_vus = max(current_vus, current_max_vus)
        state.k6_controller_enabled = True
        state.k6_controller_status = "starting"
        state.k6_controller_message = (
            f"auto_tps_fallback target_tps={_round_optional_float(state.k6_target_tps)}"
        )
        state.k6_scenario_direct_adjusting_local_target_tps = None
        state.k6_scenario_direct_adjusting_deadline_at = None
        _ensure_k6_tps_controller_started(token, state)
    else:
        raise HTTPException(
            status_code=409,
            detail=str(live_state.get("reason") or "control_unavailable"),
        )
    return _build_k6_control_response_payload(token, state)


def _process_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _stop_process_tree(pid: Optional[int], state: RunState) -> None:
    if not pid or pid <= 0:
        return

    def _wait_for_exit(timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while time.monotonic() < deadline:
            if not _process_alive(pid):
                return True
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        return not _process_alive(pid)

    term_grace_seconds = max(
        0.0, float(os.getenv("AGENT_STOP_TERM_GRACE_SECONDS", "3"))
    )

    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        state.append_log("WARN", f"process_not_found pid={pid}")
        state.pid = None
        return
    except Exception as exc:
        state.append_log("WARN", f"process_group_lookup_failed pid={pid} err={exc}")
        pgid = None

    if pgid:
        try:
            os.killpg(pgid, signal.SIGTERM)
            state.append_log("INFO", f"process_group_terminated pid={pid} pgid={pgid}")
            if _wait_for_exit(term_grace_seconds):
                state.pid = None
                return
            os.killpg(pgid, signal.SIGKILL)
            state.append_log("WARN", f"process_group_killed pid={pid} pgid={pgid}")
            if _wait_for_exit(1.0):
                state.pid = None
                return
        except ProcessLookupError:
            state.append_log("WARN", f"process_group_not_found pid={pid} pgid={pgid}")
            state.pid = None
            return
        except Exception as exc:
            state.append_log(
                "WARN",
                f"process_group_terminate_failed pid={pid} pgid={pgid} err={exc}",
            )

    try:
        os.kill(pid, signal.SIGTERM)
        state.append_log("INFO", f"process_terminated pid={pid}")
        if _wait_for_exit(term_grace_seconds):
            state.pid = None
            return
        os.kill(pid, signal.SIGKILL)
        state.append_log("WARN", f"process_killed pid={pid}")
        if _wait_for_exit(1.0):
            state.pid = None
            return
    except ProcessLookupError:
        state.append_log("WARN", f"process_not_found pid={pid}")
        state.pid = None
    except Exception as exc:  # pragma: no cover - 容错
        state.append_log("WARN", f"process_terminate_failed pid={pid} err={exc}")


def _normalize_pod_metric_label(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _derive_node_label(
    pod_name: Optional[str],
    instance: Optional[str],
) -> str:
    compose_service = _normalize_pod_metric_label(os.getenv("COMPOSE_SERVICE"))
    if compose_service:
        return compose_service

    for candidate in (pod_name, instance):
        normalized = _normalize_pod_metric_label(candidate)
        if not normalized:
            continue
        if "ptp-agent-2" in normalized:
            return "ptp-agent-2"
        if "ptp-agent" in normalized:
            return "ptp-agent"
        return normalized

    return "unknown-agent"


def _build_run_scoped_pod_metric_labels(
    token: str,
    run_id: str,
    state: RunState,
) -> dict[str, str]:
    pod_name = _normalize_pod_metric_label(state.pod_name)
    instance = _normalize_pod_metric_label(state.agent_ip) or pod_name
    agent_host = (
        _normalize_pod_metric_label(_build_agent_host_label(state.agent_ip)) or instance
    )
    node_label = _derive_node_label(pod_name, instance)
    name = node_label or pod_name or instance or "unknown-agent"
    return {
        "run_token": token,
        "run_id": run_id,
        "agent_host": agent_host or "",
        "instance": instance or name,
        "name": name,
        "pod_ip": instance or "",
        "pod_name": pod_name or name,
        "node_name": node_label,
        "node_label": node_label,
    }


def _ensure_run_dir(token: str) -> Path:
    run_dir = AGENT_RUN_ROOT / token
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _split_runtime_data_fields(line: str) -> list[str]:
    for delimiter in (",", "\t", ";", "|"):
        if delimiter in line:
            return [part.strip() for part in line.split(delimiter)]
    return [line.strip()]


def _looks_like_header_row(first_line: str, next_line: Optional[str]) -> bool:
    if not next_line:
        return False

    first_fields = [field for field in _split_runtime_data_fields(first_line) if field]
    next_fields = [field for field in _split_runtime_data_fields(next_line) if field]
    if not first_fields or not next_fields or len(first_fields) != len(next_fields):
        return False

    first_has_label = any(
        _HEADER_LABEL_FIELD_RE.fullmatch(field) for field in first_fields
    )
    if not first_has_label:
        return False

    next_has_numeric = any(
        _NUMERIC_DATA_FIELD_RE.fullmatch(field) for field in next_fields
    )
    if next_has_numeric:
        return True
    next_has_mixed_digits = any(
        _ANY_DIGIT_RE.search(field) and not _NUMERIC_DATA_FIELD_RE.fullmatch(field)
        for field in next_fields
    )
    first_has_digits = any(_ANY_DIGIT_RE.search(field) for field in first_fields)
    if next_has_mixed_digits and not first_has_digits:
        return True

    for first_field, next_field in zip(first_fields, next_fields):
        if not _HEADER_LABEL_FIELD_RE.fullmatch(first_field):
            continue
        if not _HEADER_LABEL_FIELD_RE.fullmatch(next_field):
            return True
    return False


def _split_runtime_lines_with_optional_header(
    lines: list[str],
) -> tuple[list[str], list[str]]:
    if not lines:
        return [], []

    next_non_empty_line = next((line for line in lines[1:] if line.strip()), None)
    if not _looks_like_header_row(lines[0], next_non_empty_line):
        return [], lines
    return lines[:1], lines[1:]


def _build_script_file_name(request: ExecuteRequest, source_name: Optional[str]) -> str:
    raw_name = Path(source_name).name if source_name else "script"
    suffix = Path(raw_name).suffix.lower()
    if suffix not in {".jmx", ".js"}:
        suffix = ".js" if request.engine_type.value == "k6" else ".jmx"
    stem = Path(raw_name).stem or "script"
    return f"{stem}{suffix}"


def _build_data_file_name(asset: Any, index: int) -> str:
    candidate = (
        getattr(asset, "file_name", None)
        or getattr(asset, "local_path", None)
        or getattr(asset, "storage_key", None)
        or getattr(asset, "source_uri", None)
    )
    raw_name = Path(str(candidate)).name if candidate else f"data-{index}"
    return raw_name or f"data-{index}"


def _build_proto_file_name(asset: Any, index: int) -> str:
    candidate = (
        getattr(asset, "file_name", None)
        or getattr(asset, "local_path", None)
        or getattr(asset, "storage_key", None)
        or getattr(asset, "source_uri", None)
    )
    raw_name = Path(str(candidate)).name if candidate else f"proto-{index}.proto"
    return raw_name or f"proto-{index}.proto"


def _load_runtime_asset_bytes(asset: Any, *, missing_message: str) -> bytes:
    inline_content = _runtime_asset_inline_bytes(asset)
    if inline_content is not None:
        return inline_content

    local_source = _resolve_runtime_asset_local_source(asset)
    if local_source:
        return local_source.read_bytes()

    s3_source = _resolve_runtime_asset_s3_source(asset)
    if s3_source:
        bucket, key = s3_source
        return s3_utils.download_bytes(bucket, key)

    raise FileNotFoundError(missing_message)


def _runtime_asset_inline_bytes(asset: Any) -> Optional[bytes]:
    raw_content = getattr(asset, "content_base64", None)
    if not isinstance(raw_content, str) or not raw_content.strip():
        return None
    try:
        return base64.b64decode(raw_content.encode("ascii"), validate=True)
    except Exception as exc:
        raise ValueError("runtime asset inline content is invalid base64") from exc


def _resolve_runtime_asset_local_source(asset: Any) -> Optional[Path]:
    local_source = getattr(asset, "local_path", None)
    if not local_source:
        source_uri = getattr(asset, "source_uri", None)
        if (
            isinstance(source_uri, str)
            and source_uri
            and not source_uri.startswith("s3://")
        ):
            local_source = source_uri
    return Path(local_source) if local_source else None


def _resolve_runtime_asset_s3_source(asset: Any) -> Optional[tuple[str, str]]:
    source_uri = getattr(asset, "source_uri", None)
    if isinstance(source_uri, str) and source_uri.startswith("s3://"):
        return s3_utils.parse_s3_uri(source_uri)

    storage_key = getattr(asset, "storage_key", None)
    bucket = settings.S3_BUCKET
    if isinstance(storage_key, str) and storage_key and bucket:
        return bucket, storage_key
    return None


def _runtime_asset_metadata(asset: Any) -> dict[str, Any]:
    metadata = getattr(asset, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _runtime_asset_compression_type(asset: Any) -> Optional[str]:
    value = getattr(asset, "compression_type", None)
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def _runtime_asset_is_shard(asset: Any) -> bool:
    return getattr(asset, "shard_index", None) is not None


def _should_extract_runtime_zip_asset(asset: Any) -> bool:
    metadata = _runtime_asset_metadata(asset)
    source_compression = metadata.get("source_compression_type") or metadata.get(
        "runtime_compression_type"
    )
    if isinstance(source_compression, str) and source_compression.lower() == "zip":
        return True
    if metadata.get("source_is_compressed") is True:
        return True
    return (
        _runtime_asset_is_shard(asset)
        and _runtime_asset_compression_type(asset) == "zip"
    )


def _valid_sha256(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        return None
    return normalized


def _runtime_asset_source_checksum(asset: Any) -> Optional[str]:
    metadata = _runtime_asset_metadata(asset)
    for candidate in (
        getattr(asset, "checksum_sha256", None),
        metadata.get("checksum_sha256"),
        metadata.get("source_checksum_sha256"),
        metadata.get("source_content_hash"),
    ):
        checksum = _valid_sha256(candidate)
        if checksum:
            return checksum
    if _should_extract_runtime_zip_asset(asset):
        return _valid_sha256(metadata.get("zip_content_hash"))
    return _valid_sha256(getattr(asset, "content_hash", None))


def _runtime_asset_target_checksum(asset: Any) -> Optional[str]:
    metadata = _runtime_asset_metadata(asset)
    if _should_extract_runtime_zip_asset(asset):
        for candidate in (
            metadata.get("expanded_checksum_sha256"),
            metadata.get("expanded_content_hash"),
            getattr(asset, "content_hash", None),
        ):
            checksum = _valid_sha256(candidate)
            if checksum:
                return checksum
        return None
    return _runtime_asset_source_checksum(asset) or _valid_sha256(
        getattr(asset, "content_hash", None)
    )


def _hash_file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _validate_runtime_asset_checksum(
    path: Path,
    expected_hash: Optional[str],
    *,
    label: str,
) -> None:
    checksum = _valid_sha256(expected_hash)
    if not checksum:
        return
    actual = _hash_file_sha256(path)
    if actual != checksum:
        raise ValueError(
            f"runtime data asset {label} checksum mismatch: "
            f"expected {checksum}, got {actual}"
        )


def _runtime_asset_cache_dir() -> Path:
    return Path(os.getenv("PTP_AGENT_ASSET_CACHE_DIR", "/tmp/agent_asset_cache"))


def _runtime_asset_cache_path(asset: Any) -> Optional[Path]:
    checksum = _runtime_asset_source_checksum(asset)
    if not checksum:
        return None
    candidate = (
        getattr(asset, "storage_key", None)
        or getattr(asset, "source_uri", None)
        or getattr(asset, "file_name", None)
        or "asset"
    )
    suffix = Path(str(candidate)).suffix
    if _should_extract_runtime_zip_asset(asset) and suffix.lower() != ".zip":
        suffix = ".zip"
    suffix = suffix or ".asset"
    return _runtime_asset_cache_dir() / f"{checksum}{suffix}"


def _store_runtime_asset_cache(source: Path, cache_path: Path) -> None:
    cache_tmp: Optional[Path] = None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_tmp = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
        _copy_local_file_streaming(source, cache_tmp)
        cache_tmp.replace(cache_path)
    except Exception:
        try:
            if cache_tmp is not None:
                cache_tmp.unlink(missing_ok=True)
        except Exception:
            pass
        logger.debug("runtime asset cache write skipped", exc_info=True)


def _download_runtime_s3_asset_to_file(
    asset: Any,
    bucket: str,
    key: str,
    target: Path,
    *,
    materialization_record: Optional[dict[str, Any]] = None,
) -> None:
    source_checksum = _runtime_asset_source_checksum(asset)
    cache_path = _runtime_asset_cache_path(asset)
    if cache_path and cache_path.exists():
        try:
            _validate_runtime_asset_checksum(
                cache_path, source_checksum, label="cache source"
            )
            _copy_local_file_streaming(cache_path, target)
            logger.info("runtime data asset cache hit key=%s", key)
            if materialization_record is not None:
                materialization_record["cache_hit"] = True
                materialization_record["download_bytes"] = 0
            return
        except Exception:
            cache_path.unlink(missing_ok=True)
            logger.warning("runtime data asset cache entry invalid key=%s", key)

    download_tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.download")
    try:
        s3_utils.download_file(bucket, key, download_tmp)
        _validate_runtime_asset_checksum(
            download_tmp, source_checksum, label="download source"
        )
        downloaded_bytes = download_tmp.stat().st_size
        if cache_path:
            _store_runtime_asset_cache(download_tmp, cache_path)
        download_tmp.replace(target)
        logger.info("runtime data asset downloaded key=%s", key)
        if materialization_record is not None:
            materialization_record["cache_hit"] = False
            materialization_record["download_bytes"] = downloaded_bytes
    except Exception:
        download_tmp.unlink(missing_ok=True)
        raise


def _copy_local_file_streaming(source: Path, target: Path) -> None:
    if source.resolve(strict=False) == target.resolve(strict=False):
        return
    with source.open("rb") as src, target.open("wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)


def _copy_runtime_asset_to_file(
    asset: Any,
    target: Path,
    *,
    missing_message: str,
    materialization_record: Optional[dict[str, Any]] = None,
) -> None:
    source_path, cleanup_path = _prepare_runtime_asset_source_file(
        asset,
        target,
        missing_message=missing_message,
        materialization_record=materialization_record,
    )
    try:
        _materialize_prepared_runtime_asset_to_file(asset, source_path, target)
    finally:
        if cleanup_path:
            cleanup_path.unlink(missing_ok=True)


def _prepare_runtime_asset_source_file(
    asset: Any,
    target: Path,
    *,
    missing_message: str,
    materialization_record: Optional[dict[str, Any]] = None,
) -> tuple[Path, Optional[Path]]:
    inline_content = _runtime_asset_inline_bytes(asset)
    if inline_content is not None:
        source_copy = target.with_name(f".{target.name}.{uuid.uuid4().hex}.inline")
        source_copy.write_bytes(inline_content)
        if materialization_record is not None:
            materialization_record.setdefault("cache_hit", None)
            materialization_record.setdefault("download_bytes", 0)
            materialization_record["inline_bytes"] = len(inline_content)
        return source_copy, source_copy

    local_source = _resolve_runtime_asset_local_source(asset)
    if local_source:
        if not local_source.exists():
            raise FileNotFoundError(missing_message)
        if materialization_record is not None:
            materialization_record.setdefault("cache_hit", None)
            materialization_record.setdefault("download_bytes", 0)
        return local_source, None

    s3_source = _resolve_runtime_asset_s3_source(asset)
    if s3_source:
        bucket, key = s3_source
        source_copy = target.with_name(f".{target.name}.source")
        source_copy.unlink(missing_ok=True)
        try:
            _download_runtime_s3_asset_to_file(
                asset,
                bucket,
                key,
                source_copy,
                materialization_record=materialization_record,
            )
        except Exception:
            source_copy.unlink(missing_ok=True)
            raise
        return source_copy, source_copy

    raise FileNotFoundError(missing_message)


def _materialize_prepared_runtime_asset_to_file(
    asset: Any,
    source_path: Path,
    target: Path,
) -> None:
    if _should_extract_runtime_zip_asset(asset):
        _extract_runtime_zip_asset_to_file(asset, source_path, target)
        return
    _copy_local_file_streaming(source_path, target)
    _validate_runtime_asset_checksum(
        target, _runtime_asset_target_checksum(asset), label="target"
    )


def _safe_runtime_zip_member_name(raw_name: str) -> str:
    normalized = raw_name.replace("\\", "/").strip()
    path = Path(normalized)
    if not normalized or normalized.startswith("/") or ".." in path.parts:
        raise ValueError("Unsafe runtime data asset zip member path")
    if any(part == "" for part in normalized.split("/")):
        raise ValueError("Unsafe runtime data asset zip member path")
    if "/" in normalized:
        raise ValueError("Runtime data asset zip must contain a top-level file")
    return normalized


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _extract_runtime_zip_asset_to_file(
    asset: Any,
    source_path: Path,
    target: Path,
) -> None:
    try:
        with ZipFile(source_path) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1:
                raise ValueError("Runtime data asset zip must contain exactly one file")
            member = members[0]
            if member.flag_bits & 0x1:
                raise ValueError("Encrypted runtime data asset zip is not supported")
            inner_name = _safe_runtime_zip_member_name(member.filename)
            if Path(inner_name).suffix.lower() not in _RUNTIME_DATA_ZIP_EXTENSIONS:
                raise ValueError(
                    "Runtime data asset zip must contain csv/txt/json data"
                )
            max_expanded = _env_int(
                "PTP_AGENT_DATA_ZIP_MAX_EXPANDED_BYTES",
                5 * 1024 * 1024 * 1024,
            )
            if member.file_size > max_expanded:
                raise ValueError("Expanded runtime data asset zip is too large")
            ratio_limit = _env_int("PTP_AGENT_DATA_ZIP_MAX_COMPRESSION_RATIO", 100)
            if (
                member.compress_size > 0
                and member.file_size / member.compress_size > ratio_limit
            ):
                raise ValueError("Runtime data asset zip compression ratio is too high")

            temp_target = target.with_name(f".{target.name}.{uuid.uuid4().hex}.unzip")
            try:
                with archive.open(member) as source, temp_target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                _validate_runtime_asset_checksum(
                    temp_target,
                    _runtime_asset_target_checksum(asset),
                    label="expanded target",
                )
                temp_target.replace(target)
            except Exception:
                temp_target.unlink(missing_ok=True)
                raise
    except BadZipFile as exc:
        raise ValueError("Invalid runtime data asset zip") from exc


def _coerce_runtime_shard_index(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _select_runtime_data_asset_source(request: ExecuteRequest, asset: Any) -> Any:
    shards = getattr(asset, "shards", None)
    if not isinstance(shards, list) or not shards:
        return asset
    if request.effective_data_distribution != "avg":
        return asset

    slice_start = request.runtime_data_slice_start or 1
    has_explicit_index = False
    for shard in shards:
        raw_index = getattr(shard, "shard_index", None)
        if raw_index is None:
            continue
        has_explicit_index = True
        shard_index = _coerce_runtime_shard_index(raw_index)
        if shard_index is None:
            raise ValueError("runtime data shard_index must be 1-based")
        if shard_index == slice_start:
            return shard
    if has_explicit_index:
        raise ValueError(
            "runtime data shard manifest does not contain requested "
            f"shard_index={slice_start}"
        )
    if 1 <= slice_start <= len(shards):
        return shards[slice_start - 1]
    raise ValueError(
        "runtime data shard manifest does not contain requested "
        f"shard_index={slice_start}"
    )


def _runtime_asset_materialization_record(
    request: ExecuteRequest,
    asset: Any,
    source_asset: Any,
    target: Path,
) -> dict[str, Any]:
    metadata = _runtime_asset_metadata(source_asset)
    return {
        "category": "data",
        "file_name": getattr(asset, "file_name", None) or target.name,
        "target": str(target),
        "storage_key": getattr(source_asset, "storage_key", None),
        "source_uri": getattr(source_asset, "source_uri", None),
        "data_distribution": request.effective_data_distribution,
        "shard_index": getattr(source_asset, "shard_index", None),
        "line_count": getattr(source_asset, "line_count", None),
        "compression_type": _runtime_asset_compression_type(source_asset),
        "cache_hit": None,
        "download_bytes": 0,
        "expanded_from_zip": _should_extract_runtime_zip_asset(source_asset),
        "source_checksum_sha256": _runtime_asset_source_checksum(source_asset),
        "target_checksum_sha256": _runtime_asset_target_checksum(source_asset),
        "zip_member_name": metadata.get("zip_member_name"),
    }


def _append_runtime_asset_materialization_logs(
    state: RunState,
    records: list[dict[str, Any]],
) -> None:
    for record in records:
        cache_hit = record.get("cache_hit")
        cache_status = (
            "hit" if cache_hit is True else "miss" if cache_hit is False else "n/a"
        )
        state.append_log(
            "INFO",
            "runtime_data_asset_materialized "
            f"file={record.get('file_name') or 'unknown'} "
            f"target={record.get('target') or 'unknown'} "
            f"shard_index={record.get('shard_index') or 'none'} "
            f"inline_bytes={record.get('inline_bytes') or 0} "
            f"download_bytes={record.get('download_bytes') or 0} "
            f"cache={cache_status} "
            f"storage_key={record.get('storage_key') or 'none'} "
            f"zip={str(bool(record.get('expanded_from_zip'))).lower()}",
        )


def _should_slice_data_asset_by_line(request: ExecuteRequest) -> bool:
    if request.effective_data_distribution != "avg":
        return False
    if request.runtime_data_split_type != "line":
        return False
    slice_total = request.runtime_data_slice_total or 1
    return slice_total > 1


def _inspect_runtime_line_slice_source(source: Path) -> Optional[tuple[bool, int]]:
    with source.open("r", encoding="utf-8", newline="") as handle:
        first_line = handle.readline()
        if not first_line:
            return None

        next_non_empty_line: Optional[str] = None
        line_count_after_first = 0
        for line in handle:
            line_count_after_first += 1
            if next_non_empty_line is None and line.strip():
                next_non_empty_line = line

    has_header = _looks_like_header_row(first_line, next_non_empty_line)
    data_count = line_count_after_first if has_header else line_count_after_first + 1
    if data_count <= 0:
        return None
    return has_header, data_count


def _write_runtime_line_slice(
    source: Path,
    target: Path,
    *,
    has_header: bool,
    start: int,
    end: int,
) -> bool:
    temp_target = target.with_name(f".{target.name}.tmp")
    temp_target.unlink(missing_ok=True)
    written_count = 0
    missing_header = False
    try:
        with source.open("r", encoding="utf-8", newline="") as src, temp_target.open(
            "w", encoding="utf-8", newline=""
        ) as dst:
            if has_header:
                header_line = src.readline()
                if not header_line:
                    missing_header = True
                else:
                    dst.write(header_line)

            if not missing_header:
                for data_index, line in enumerate(src):
                    if data_index < start:
                        continue
                    if data_index >= end:
                        break
                    dst.write(line)
                    written_count += 1
        if missing_header or written_count <= 0:
            temp_target.unlink(missing_ok=True)
            return False
        temp_target.replace(target)
        return True
    except Exception:
        temp_target.unlink(missing_ok=True)
        raise


def _materialize_data_asset_to_file(
    request: ExecuteRequest,
    asset: Any,
    target: Path,
    *,
    materialization_record: Optional[dict[str, Any]] = None,
) -> None:
    missing_message = "runtime data asset source is missing"
    source_asset = _select_runtime_data_asset_source(request, asset)
    if source_asset is not asset:
        _copy_runtime_asset_to_file(
            source_asset,
            target,
            missing_message=missing_message,
            materialization_record=materialization_record,
        )
        return
    if not _should_slice_data_asset_by_line(request):
        _copy_runtime_asset_to_file(
            source_asset,
            target,
            missing_message=missing_message,
            materialization_record=materialization_record,
        )
        return

    if _should_extract_runtime_zip_asset(source_asset):
        source_path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.plain")
        source_path.unlink(missing_ok=True)
        _copy_runtime_asset_to_file(
            source_asset,
            source_path,
            missing_message=missing_message,
            materialization_record=materialization_record,
        )
        cleanup_path: Optional[Path] = source_path
    else:
        source_path, cleanup_path = _prepare_runtime_asset_source_file(
            source_asset,
            target,
            missing_message=missing_message,
            materialization_record=materialization_record,
        )
    try:
        try:
            slice_info = _inspect_runtime_line_slice_source(source_path)
        except UnicodeDecodeError:
            logger.warning("avg slicing skipped due to non-utf8 payload")
            _copy_local_file_streaming(source_path, target)
            return
        if not slice_info:
            _copy_local_file_streaming(source_path, target)
            return

        has_header, data_count = slice_info
        slice_start = request.runtime_data_slice_start or 1
        slice_total = request.runtime_data_slice_total or 1
        page_size = max(1, data_count // slice_total)
        start = (slice_start - 1) * page_size
        end = data_count if slice_start >= slice_total else slice_start * page_size
        if start >= data_count:
            _copy_local_file_streaming(source_path, target)
            return

        try:
            sliced = _write_runtime_line_slice(
                source_path,
                target,
                has_header=has_header,
                start=start,
                end=end,
            )
        except UnicodeDecodeError:
            logger.warning("avg slicing skipped due to non-utf8 payload")
            _copy_local_file_streaming(source_path, target)
            return
        if not sliced:
            _copy_local_file_streaming(source_path, target)
    finally:
        if cleanup_path:
            cleanup_path.unlink(missing_ok=True)


def _slice_data_asset_bytes(request: ExecuteRequest, content: bytes) -> bytes:
    if request.effective_data_distribution != "avg":
        return content
    if request.runtime_data_split_type != "line":
        return content

    slice_start = request.runtime_data_slice_start or 1
    slice_total = request.runtime_data_slice_total or 1
    if slice_total <= 1:
        return content

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("avg slicing skipped due to non-utf8 payload")
        return content

    lines = text.splitlines(keepends=True)
    if not lines:
        return content

    header, data_lines = _split_runtime_lines_with_optional_header(lines)
    if not data_lines:
        return content

    data_count = len(data_lines)
    page_size = max(1, data_count // slice_total)
    start = (slice_start - 1) * page_size
    end = data_count if slice_start >= slice_total else slice_start * page_size
    sliced_lines = data_lines[start:end]
    if not sliced_lines:
        return content

    if header and data_lines is not lines:
        sliced_lines = header + sliced_lines
    sliced = "".join(sliced_lines)
    return sliced.encode("utf-8")


def _materialize_data_assets(
    request: ExecuteRequest,
    run_dir: Path,
    *,
    materialization_records: Optional[list[dict[str, Any]]] = None,
) -> list[Path]:
    if not request.runtime_data_assets:
        return []

    data_dir = run_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    materialized_paths: list[Path] = []
    for index, asset in enumerate(request.runtime_data_assets, start=1):
        file_name = _build_data_file_name(asset, index)
        target = data_dir / file_name
        if target.exists():
            target = data_dir / f"{target.stem}-{index}{target.suffix}"
        source_asset = _select_runtime_data_asset_source(request, asset)
        record = _runtime_asset_materialization_record(
            request, asset, source_asset, target
        )
        _materialize_data_asset_to_file(
            request,
            asset,
            target,
            materialization_record=record,
        )
        try:
            record["target_bytes"] = target.stat().st_size
        except OSError:
            record["target_bytes"] = None
        if materialization_records is not None:
            materialization_records.append(record)
        materialized_paths.append(target)
    return materialized_paths


def _materialize_proto_assets(request: ExecuteRequest, run_dir: Path) -> list[Path]:
    if not request.runtime_proto_assets:
        return []

    proto_dir = run_dir / "proto"
    proto_dir.mkdir(parents=True, exist_ok=True)
    materialized_paths: list[Path] = []
    for index, asset in enumerate(request.runtime_proto_assets, start=1):
        file_name = _build_proto_file_name(asset, index)
        target = proto_dir / file_name
        if target.exists():
            target = proto_dir / f"{target.stem}-{index}{target.suffix}"
        target.write_bytes(
            _load_runtime_asset_bytes(
                asset,
                missing_message="runtime proto asset source is missing",
            )
        )
        materialized_paths.append(target)
    return materialized_paths


def _build_runtime_properties(
    request: ExecuteRequest,
    run_dir: Path,
    data_paths: list[Path],
    proto_paths: list[Path],
) -> Optional[dict[str, Any]]:
    runtime_properties: dict[str, Any] = dict(request.properties or {})
    pod_count = _coerce_positive_int(request.pod_count) or _coerce_positive_int(
        request.pod_num
    )
    if pod_count:
        # Top-level dispatch pod_count is the canonical runtime split signal.
        # It must override stale task defaults preserved in request.properties.
        runtime_properties["pod_count"] = str(pod_count)
        runtime_properties["pod_num"] = str(pod_count)
        runtime_properties["POD_COUNT"] = str(pod_count)
    if data_paths:
        runtime_properties.setdefault("PTP_RUN_DIR", str(run_dir))
        runtime_properties.setdefault("PTP_DATA_DIR", str(run_dir / "data"))
        runtime_properties.setdefault(
            "PTP_DATA_FILES",
            ",".join(path.name for path in data_paths),
        )
        if len(data_paths) == 1:
            runtime_properties.setdefault("DATA_FILE", data_paths[0].name)
        if request.effective_data_distribution:
            runtime_properties.setdefault(
                "PTP_DATA_DISTRIBUTION",
                request.effective_data_distribution,
            )
    if proto_paths:
        runtime_properties.setdefault("PTP_RUN_DIR", str(run_dir))
        runtime_properties.setdefault("PTP_PROTO_DIR", str(run_dir / "proto"))
        runtime_properties.setdefault(
            "PTP_PROTO_FILES",
            ",".join(path.name for path in proto_paths),
        )
    return runtime_properties or None


def _enrich_jmeter_influx_properties(
    runtime_properties: Optional[dict[str, Any]],
    *,
    request: ExecuteRequest,
    token: str,
    state: RunState,
) -> dict[str, Any]:
    properties: dict[str, Any] = dict(runtime_properties or {})
    metrics_enabled = str(properties.get("metrics_enabled", "true")).strip().lower()
    if metrics_enabled in {"0", "false", "off", "no"}:
        properties["jmeter_influx_enabled"] = "0"
        state.append_log("INFO", "jmeter_influx_disabled_metrics_switch_off")
        return properties
    if str(os.getenv("JMETER_INFLUX_ENABLED", "1")).strip().lower() in {
        "0",
        "false",
        "off",
        "no",
    }:
        properties["jmeter_influx_enabled"] = "0"
        return properties

    for compatibility_key in (
        "influxDBHttpScheme",
        "influxDBHost",
        "influxDBPort",
        "influxDBURL",
        "influxDBToken",
        "influxDBOrganization",
        "influxDBBucket",
        "influxDBFlushInterval",
        "influxDBMaxBatchSize",
        "samplersList",
        "useRegexForSamplerList",
        "recordSubSamples",
        "saveResponseBodyOfFailures",
        "responseBodyLength",
        "runId",
        "nodeName",
        "testName",
    ):
        properties.pop(compatibility_key, None)

    influx_scheme = (
        str(os.getenv("JMETER_INFLUXDB_HTTP_SCHEME") or "http").strip() or "http"
    )
    influx_host = (
        str(os.getenv("JMETER_INFLUXDB_HOST") or "influxdb").strip() or "influxdb"
    )
    influx_port = str(os.getenv("JMETER_INFLUXDB_PORT") or "8086").strip() or "8086"
    influx_org = (
        str(
            os.getenv("JMETER_INFLUXDB_ORG") or os.getenv("INFLUXDB_ORG") or "ptp"
        ).strip()
        or "ptp"
    )
    influx_bucket = (
        str(
            os.getenv("JMETER_INFLUXDB_BUCKET") or os.getenv("INFLUXDB_BUCKET") or "ptp"
        ).strip()
        or "ptp"
    )
    default_write_url = (
        f"{influx_scheme}://{influx_host}:{influx_port}/api/v2/write"
        f"?org={quote(influx_org, safe='')}&bucket={quote(influx_bucket, safe='')}"
    )
    properties.setdefault(
        "influxdbMetricsSender",
        "org.apache.jmeter.visualizers.backend.influxdb.HttpMetricsSender",
    )
    properties.setdefault(
        "influxdbUrl",
        str(os.getenv("JMETER_INFLUXDB_URL") or default_write_url).strip()
        or default_write_url,
    )

    influx_token = _resolve_jmeter_influx_token(properties)
    if not influx_token:
        properties["jmeter_influx_enabled"] = "0"
        state.append_log("WARN", "jmeter_influx_disabled_missing_valid_token")
        return properties

    properties.setdefault("jmeter_influx_enabled", "1")
    properties["influxdbToken"] = influx_token
    total_target_tps = _coerce_positive_int(
        properties.get("target_tps")
    ) or _coerce_positive_int(properties.get("fixed_tps"))
    pod_count = (
        _coerce_positive_int(properties.get("pod_count"))
        or _coerce_positive_int(properties.get("pod_num"))
        or 1
    )
    if total_target_tps:
        per_agent_tps = max(1, round(float(total_target_tps) / max(1, pod_count), 4))
        properties["target_tps_per_agent"] = str(per_agent_tps)
        properties["target_tps_per_agent_per_minute"] = str(
            max(1, round(float(per_agent_tps) * 60, 4))
        )
    run_identifier = str(request.run_id or token)
    properties.setdefault("application", run_identifier)
    properties.setdefault("measurement", "jmeter")
    properties.setdefault("summaryOnly", "false")
    properties.setdefault("samplersRegex", ".*")
    properties.setdefault("percentiles", "90;95;99")
    properties.setdefault("testTitle", f"OpenLoadHub-Run-{run_identifier}")
    properties.setdefault("eventTags", "")
    properties.setdefault("TAG_runId", run_identifier)
    properties.setdefault("TAG_taskId", str(request.task_id or 0))
    properties.setdefault(
        "TAG_nodeName",
        state.pod_name or state.agent_ip or state.engine_type or "ptp-agent",
    )
    return properties


def _resolve_jmeter_influx_token(properties: dict[str, Any]) -> Optional[str]:
    influx_base_url = _resolve_jmeter_influx_base_url(properties)
    candidate_tokens: list[str] = []
    for raw in (
        properties.get("influxdbToken"),
        properties.get("influxDBToken"),
        os.getenv("JMETER_INFLUXDB_TOKEN"),
        os.getenv("INFLUXDB_TOKEN"),
    ):
        token = str(raw or "").strip()
        if token and token not in candidate_tokens:
            candidate_tokens.append(token)

    for token in candidate_tokens:
        if _validate_influx_token(influx_base_url, token):
            return token

    username = (
        str(os.getenv("JMETER_INFLUXDB_USERNAME") or "").strip()
        or str(os.getenv("INFLUXDB_USERNAME") or "").strip()
    )
    password = (
        str(os.getenv("JMETER_INFLUXDB_PASSWORD") or "").strip()
        or str(os.getenv("INFLUXDB_PASSWORD") or "").strip()
    )
    if not influx_base_url or not username or not password:
        return None
    refreshed_token = _fetch_latest_influx_token(
        influx_base_url,
        username=username,
        password=password,
    )
    if refreshed_token and _validate_influx_token(influx_base_url, refreshed_token):
        return refreshed_token
    return None


def _resolve_jmeter_influx_base_url(properties: dict[str, Any]) -> str:
    for key in ("influxdbUrl", "influxDBURL"):
        raw = str(properties.get(key) or "").strip()
        if not raw:
            continue
        parsed = urlsplit(raw)
        if parsed.scheme and parsed.netloc:
            return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return ""


def _validate_influx_token(influx_base_url: str, token: str) -> bool:
    if not influx_base_url or not token:
        return False
    try:
        response = httpx.get(
            f"{influx_base_url.rstrip('/')}/api/v2/buckets",
            params={"limit": 1},
            headers={"Authorization": f"Token {token}"},
            timeout=5.0,
            follow_redirects=True,
            trust_env=False,
        )
        return response.status_code == 200
    except Exception:
        return False


def _fetch_latest_influx_token(
    influx_base_url: str,
    *,
    username: str,
    password: str,
) -> Optional[str]:
    try:
        with httpx.Client(
            base_url=influx_base_url.rstrip("/"),
            timeout=5.0,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            signin = client.post("/api/v2/signin", auth=(username, password))
            if signin.status_code != 204:
                return None
            authz = client.get("/api/v2/authorizations", params={"limit": 5})
            if authz.status_code != 200:
                return None
            payload = authz.json()
            authorizations = payload.get("authorizations") or []
            for item in authorizations:
                token = str(item.get("token") or "").strip()
                if token:
                    return token
    except Exception:
        return None
    return None


def _reconcile_running_state(state: RunState) -> None:
    if state.status != "running":
        return

    task = state.async_task

    if task and not task.done():
        # Keep the last observed pid while post-process finalization is still running.
        # Admin pollers treat "running + pid missing" as a stale-agent failure signal.
        # Clearing pid before the async finalizer publishes terminal status/summary creates
        # a false-negative window after the load engine already completed successfully.
        return

    if state.pid and not _process_alive(state.pid):
        state.pid = None

    terminal_status: Optional[str] = None
    terminal_error = state.error

    if task and task.done():
        if task.cancelled():
            terminal_status = "stopped"
        else:
            exc = task.exception()
            if exc is not None:
                terminal_status = "failed"
                terminal_error = terminal_error or str(exc)

    has_summary = isinstance(state.jtl_summary, dict) or isinstance(
        state.k6_summary, dict
    )
    if terminal_status is None and (state.ended_at is not None or has_summary):
        terminal_status = "failed" if terminal_error else "succeeded"

    if terminal_status is None and state.pid is None and (task is None or task.done()):
        terminal_status = "failed"
        terminal_error = terminal_error or "process_exited_without_terminal_status"

    if terminal_status is None:
        return

    state.status = terminal_status
    state.error = terminal_error
    if state.ended_at is None:
        state.ended_at = datetime.now(timezone.utc)


def _is_host_process_scope_run(state: RunState) -> bool:
    return _is_host_runtime_identity(state.agent_ip, state.pod_name)


def _prime_process_scope_pod_monitor_snapshot(state: RunState) -> None:
    if state.pid is None or state.pid <= 0:
        return
    snapshot = state.append_pod_monitor_snapshot()
    # The first process-scoped sample is already a valid baseline. Allow the next
    # terminal/live delta to publish CPU instead of requiring an extra hidden sample.
    if snapshot is not None and _is_host_process_scope_run(state):
        state.cpu_usage_percent_warmup_done = True


def _record_terminal_pod_monitor_snapshot_before_pid_release(state: RunState) -> None:
    if state.pod_monitor_terminal_snapshot_recorded:
        return
    if state.pid is None or state.pid <= 0:
        return
    if state.ended_at is None:
        state.ended_at = datetime.now(timezone.utc)
    snapshot = state.append_pod_monitor_snapshot()
    if snapshot is not None or state.pod_monitor_history:
        state.pod_monitor_terminal_snapshot_recorded = True


def _start_live_pod_monitor_sampling_thread(
    *,
    token: str,
    state: RunState,
    pushgateway: Optional[str],
    interval_seconds: float = 5.0,
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def _worker() -> None:
        while not stop_event.is_set() and state.status == "running":
            try:
                snapshot = state.append_pod_monitor_snapshot()
                if pushgateway and (snapshot is not None or state.pod_monitor_history):
                    _push_live_pod_monitor_metrics(pushgateway, token, state)
            except Exception as exc:  # pragma: no cover - best effort live refresh
                logger.debug("live pod monitor refresh skipped for %s: %s", token, exc)
            stop_event.wait(interval_seconds)

    thread = threading.Thread(
        target=_worker,
        name=f"ptp-pod-monitor-{token}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _build_run_summary_metric_items(state: RunState) -> list[dict[str, Any]]:
    summary = (
        state.k6_summary
        if isinstance(state.k6_summary, dict)
        else (state.jtl_summary if isinstance(state.jtl_summary, dict) else {})
    )
    rows = (
        summary.get("endpoint_metrics")
        if isinstance(summary.get("endpoint_metrics"), list)
        else []
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        endpoint_name = str(row.get("endpoint_name") or row.get("name") or "").strip()
        if not endpoint_name:
            continue
        item = {
            "endpoint_name": endpoint_name,
            "avg_rt_ms": row.get("avg_rt_ms"),
            "p95_rt_ms": row.get("p95_rt_ms"),
            "p99_rt_ms": row.get("p99_rt_ms"),
            "max_rt_ms": row.get("max_rt_ms"),
            "min_rt_ms": row.get("min_rt_ms"),
            "total_requests": row.get("total_requests"),
            "throughput": row.get("throughput"),
        }
        items.append({key: value for key, value in item.items() if value is not None})
    if items and state.engine_type == "k6" and state.status == "running":
        live_total_tps = _round_optional_float(
            state.k6_observed_tps if state.k6_observed_tps is not None else state.rps
        )
        if live_total_tps is not None and live_total_tps > 0:
            weighted_items = [
                item
                for item in items
                if isinstance(item.get("throughput"), (int, float))
            ]
            total_weight = sum(
                float(item.get("throughput") or 0.0) for item in weighted_items
            )
            if total_weight > 0:
                remaining = float(live_total_tps)
                for index, item in enumerate(weighted_items):
                    if index == len(weighted_items) - 1:
                        item["throughput"] = round(max(0.0, remaining), 4)
                        break
                    scaled = round(
                        float(live_total_tps)
                        * float(item.get("throughput") or 0.0)
                        / total_weight,
                        4,
                    )
                    item["throughput"] = scaled
                    remaining -= scaled
    if items:
        return items

    overall = {
        "endpoint_name": str(summary.get("overall_endpoint_name") or "overall"),
        "avg_rt_ms": summary.get("rt_avg_ms") or summary.get("avg_response_time"),
        "p95_rt_ms": summary.get("rt_p95_ms") or summary.get("p95_response_time"),
        "p99_rt_ms": summary.get("rt_p99_ms") or summary.get("p99_response_time"),
        "max_rt_ms": summary.get("rt_max_ms") or summary.get("max_response_time"),
        "min_rt_ms": summary.get("rt_min_ms") or summary.get("min_response_time"),
        "total_requests": summary.get("total_requests"),
        "throughput": (
            _round_optional_float(
                state.k6_observed_tps
                if state.k6_observed_tps is not None
                else state.rps
            )
            if state.engine_type == "k6" and state.status == "running"
            else None
        )
        or summary.get("throughput")
        or summary.get("http_reqs"),
    }
    overall = {key: value for key, value in overall.items() if value is not None}
    if len(overall) > 1:
        return [overall]
    return items


def _build_run_endpoint_trend_items(
    state: RunState,
    *,
    metric_filter: Optional[str] = None,
    endpoint_filter: Optional[str] = None,
) -> list[dict[str, Any]]:
    summary = (
        state.k6_summary
        if isinstance(state.k6_summary, dict)
        else (state.jtl_summary if isinstance(state.jtl_summary, dict) else {})
    )
    raw_items = summary.get("endpoint_trends")
    if not isinstance(raw_items, list):
        endpoint_rows = summary.get("endpoint_metrics")
        raw_items = (
            _build_k6_endpoint_trends(state, endpoint_rows)
            if isinstance(endpoint_rows, list)
            else []
        )

    items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        endpoint_name = str(item.get("endpoint_name") or "").strip()
        metric_name = str(item.get("metric") or "").strip()
        if not endpoint_name or not metric_name:
            continue
        if endpoint_filter and endpoint_name != endpoint_filter:
            continue
        if metric_filter and metric_name != metric_filter:
            continue
        points = item.get("points")
        if not isinstance(points, list):
            continue
        items.append(
            {
                "endpoint_name": endpoint_name,
                "metric": metric_name,
                "unit": item.get("unit") or "",
                "points": points,
            }
        )
    return items


def _refresh_jmeter_live_summary_from_jtl(state: RunState) -> list[Any]:
    path = state.jtl_path
    if not path or not path.exists():
        return []
    samples = list(parse_jtl(path))
    summary = summarize_samples(samples)
    if not summary:
        return samples
    check_rows = _build_jmeter_check_items_from_samples(samples)
    if check_rows:
        summary["checks"] = check_rows
    endpoint_rows = _build_jmeter_endpoint_summary_rows(samples)
    if endpoint_rows:
        summary["endpoint_metrics"] = endpoint_rows
    state.jtl_summary = summary
    throughput = summary.get("throughput")
    p95 = summary.get("p95_response_time")
    if throughput is not None:
        state.rps = float(throughput)
    if p95 is not None:
        state.rt_p95_ms = float(p95)
    return samples


def _build_jmeter_endpoint_trends_from_samples(
    samples: list[Any],
    *,
    step_seconds: int,
    metric_filter: Optional[str] = None,
    endpoint_filter: Optional[str] = None,
) -> list[dict[str, Any]]:
    if not samples:
        return []

    resolved_step_seconds = max(1, int(step_seconds or 1))

    def _bucket_ts(ts: datetime) -> datetime:
        bucket_epoch = (
            int(ts.timestamp() // resolved_step_seconds) * resolved_step_seconds
        )
        return datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)

    def _pct(sorted_values: list[int], percentile: int) -> float:
        if not sorted_values:
            return 0.0
        index = int(len(sorted_values) * percentile / 100)
        index = min(max(index, 0), len(sorted_values) - 1)
        return float(sorted_values[index])

    grouped: dict[str, dict[datetime, list[Any]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for sample in samples:
        endpoint_name = str(getattr(sample, "label", "") or "").strip()
        if not endpoint_name:
            continue
        if endpoint_filter and endpoint_name != endpoint_filter:
            continue
        sample_ts = getattr(sample, "ts", None)
        if not isinstance(sample_ts, datetime) or sample_ts.year < 2000:
            continue
        grouped[endpoint_name][_bucket_ts(sample_ts)].append(sample)

    metric_specs = [
        ("throughput", "rps"),
        ("rt_avg_ms", "ms"),
        ("rt_p95_ms", "ms"),
        ("rt_p99_ms", "ms"),
    ]
    items: list[dict[str, Any]] = []
    for endpoint_name in sorted(grouped):
        buckets = grouped[endpoint_name]
        bucket_values: dict[str, list[dict[str, Any]]] = {
            metric_name: [] for metric_name, _ in metric_specs
        }
        for bucket_ts in sorted(buckets):
            bucket_samples = buckets[bucket_ts]
            elapsed_values = sorted(
                int(getattr(sample, "elapsed_ms", 0) or 0) for sample in bucket_samples
            )
            if not elapsed_values:
                continue
            metric_points = {
                "throughput": round(
                    len(bucket_samples) / float(resolved_step_seconds), 6
                ),
                "rt_avg_ms": round(sum(elapsed_values) / len(elapsed_values), 6),
                "rt_p95_ms": round(_pct(elapsed_values, 95), 6),
                "rt_p99_ms": round(_pct(elapsed_values, 99), 6),
            }
            for metric_name, value in metric_points.items():
                bucket_values[metric_name].append(
                    {"ts": bucket_ts.isoformat(), "value": value}
                )
        for metric_name, unit in metric_specs:
            if metric_filter and metric_name != metric_filter:
                continue
            points = bucket_values[metric_name]
            if not points:
                continue
            items.append(
                {
                    "endpoint_name": endpoint_name,
                    "metric": metric_name,
                    "unit": unit,
                    "points": points,
                }
            )
    return items


def _build_jmeter_check_items_from_samples(samples: list[Any]) -> list[dict[str, Any]]:
    grouped_samples = _group_jmeter_samples_by_endpoint(samples)
    items: list[dict[str, Any]] = []
    for endpoint_name, endpoint_samples in sorted(grouped_samples.items()):
        total_requests = len(endpoint_samples)
        if total_requests <= 0:
            continue
        successful_requests = sum(
            1 for sample in endpoint_samples if bool(getattr(sample, "success", False))
        )
        items.append(
            {
                "group_name": endpoint_name,
                "check_name": "success rate",
                "success_rate": successful_requests / total_requests,
            }
        )
    return items


def _build_run_check_items(state: RunState) -> list[dict[str, Any]]:
    if isinstance(state.k6_summary, dict):
        raw_items = state.k6_summary.get("checks")
        if isinstance(raw_items, list):
            items: list[dict[str, Any]] = []
            for row in raw_items:
                if not isinstance(row, dict):
                    continue
                group_name = str(row.get("group_name") or "").strip()
                check_name = str(row.get("check_name") or "").strip()
                if not group_name or not check_name:
                    continue
                item = {
                    "group_name": group_name,
                    "check_name": check_name,
                    "success_rate": row.get("success_rate"),
                }
                items.append(
                    {key: value for key, value in item.items() if value is not None}
                )
            if items:
                return items

    if isinstance(state.jtl_summary, dict):
        raw_items = state.jtl_summary.get("checks")
        if isinstance(raw_items, list):
            items = [row for row in raw_items if isinstance(row, dict)]
            if items:
                return items

    path = state.jtl_path
    if not path or not path.exists():
        return []

    items = _build_jmeter_check_items_from_samples(list(parse_jtl(path)))
    if items and isinstance(state.jtl_summary, dict):
        state.jtl_summary["checks"] = items
    return items


@router.post("/execute", response_model=ExecuteResponse)
async def execute_test(request: ExecuteRequest):
    """
    Agent 执行入口
    - 默认模拟执行；当 `AGENT_EXEC_MODE=real` 且传入 script_path 时尝试真实执行（失败回退模拟）
    """
    try:
        token = str(request.run_id or request.task_id) + "-" + uuid.uuid4().hex[:6]
        log_path = Path("/tmp/agent_runs") / f"{token}.log"
        metrics_path = Path("/tmp/agent_runs") / f"{token}.metrics"
        state = RunState(
            task_id=request.task_id,
            run_id=request.run_id,
            engine_type=request.engine_type.value,
            status="running",
            log_path=log_path,
            metrics_path=metrics_path,
        )
        store.put(token, state)

        exec_mode = os.getenv("AGENT_EXEC_MODE", "mock").lower()
        if exec_mode == "real" and (
            request.script_content is not None
            or request.script_path
            or request.script_s3
        ):
            if request.engine_type.value == "k6":
                state.async_task = asyncio.create_task(run_k6_real(token, request))
            else:
                state.async_task = asyncio.create_task(run_jmeter_real(token, request))
        else:
            state.async_task = asyncio.create_task(
                simulate_run(token, duration=max(3, min(request.resolved_duration, 30)))
            )

        return ExecuteResponse(
            task_id=request.task_id,
            status="started",
            pid=0,
            agent_id="agent-001",
            run_token=token,
        )
    except Exception as exc:
        logger.exception("Failed to execute task %s", request.task_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/runs/{token}/status")
def get_run_status(token: str):
    state = store.get(token)
    if not state:
        raise HTTPException(status_code=404, detail="run not found")
    state.load_logs_from_file()
    state.load_metrics_from_file()
    _reconcile_running_state(state)
    if state.status == "running":
        _refresh_running_k6_runtime_metrics(state)
        state.append_pod_monitor_snapshot()
    if state.status != "running":
        state.ensure_terminal_pod_monitor_snapshot()
    pod_monitor_series = (
        state.build_pod_monitor_series(step_seconds=10)
        if state.status != "running"
        else []
    )
    raw_observability = _build_jmeter_influx_observability(token, state)
    return {
        "status": state.status,
        "task_id": state.task_id,
        "run_id": state.run_id,
        "started_at": state.started_at,
        "ended_at": state.ended_at,
        "rps": state.rps,
        "rt_p95_ms": state.rt_p95_ms,
        "error": state.error,
        "pid": state.pid,
        "agent_ip": state.agent_ip,
        "log_s3": state.s3_log_uri,
        "metrics_s3": state.s3_metrics_uri,
        "jtl_summary": state.jtl_summary,
        "k6_summary": state.k6_summary,
        "pod_monitor_series": pod_monitor_series,
        **({"raw_observability": raw_observability} if raw_observability else {}),
    }


@router.post("/runs/{token}/stop")
def stop_run(token: str):
    state = store.get(token)
    if not state:
        raise HTTPException(status_code=404, detail="run not found")

    if state.status in {"succeeded", "failed", "stopped"}:
        return {"status": state.status, "run_token": token, "pid": state.pid}

    state.append_log("WARN", "stop_requested")

    _stop_process_tree(state.pid, state)
    _stop_k6_tps_controller(state)

    state.status = "stopped"
    state.ended_at = datetime.now(timezone.utc)
    return {"status": state.status, "run_token": token, "pid": state.pid}




@router.get("/runs/{token}/metrics")
def get_run_metrics(token: str):
    state = store.get(token)
    if not state:
        raise HTTPException(status_code=404, detail="run not found")
    state.load_metrics_from_file()
    _reconcile_running_state(state)
    _refresh_running_k6_runtime_metrics(state, append_metric_history=True)
    series = [
        {
            "metric": "rps",
            "unit": "rps",
            "points": [
                {"ts": snap["ts"], "value": snap["rps"]}
                for snap in state.metric_history
            ],
        },
        {
            "metric": "rt_p95_ms",
            "unit": "ms",
            "points": [
                {"ts": snap["ts"], "value": snap["rt_p95_ms"]}
                for snap in state.metric_history
            ],
        },
    ]
    return {
        "step_seconds": 1,
        "series": series,
    }


@router.get("/runs/{token}/summary-metrics")
def get_run_summary_metrics(token: str):
    state = store.get(token)
    if not state:
        raise HTTPException(status_code=404, detail="run not found")

    _reconcile_running_state(state)
    if state.engine_type == "jmeter":
        _refresh_jmeter_live_summary_from_jtl(state)
    return {
        "items": _build_run_summary_metric_items(state),
    }


@router.get("/runs/{token}/checks")
def get_run_checks(token: str):
    state = store.get(token)
    if not state:
        raise HTTPException(status_code=404, detail="run not found")

    _reconcile_running_state(state)
    if state.engine_type == "jmeter":
        _refresh_jmeter_live_summary_from_jtl(state)
    return {
        "items": _build_run_check_items(state),
    }


@router.get("/runs/{token}/endpoint-trends")
def get_run_endpoint_trends(
    token: str,
    metric: Optional[str] = None,
    endpoint_name: Optional[str] = None,
    step_seconds: int = 10,
):
    state = store.get(token)
    if not state:
        raise HTTPException(status_code=404, detail="run not found")

    _reconcile_running_state(state)
    if state.engine_type == "jmeter":
        samples = _refresh_jmeter_live_summary_from_jtl(state)
        items = _build_jmeter_endpoint_trends_from_samples(
            samples,
            step_seconds=step_seconds,
            metric_filter=metric,
            endpoint_filter=endpoint_name,
        )
        if items:
            return {
                "step_seconds": max(1, int(step_seconds or 1)),
                "items": items,
            }
    return {
        "step_seconds": max(1, int(step_seconds or 1)),
        "items": _build_run_endpoint_trend_items(
            state,
            metric_filter=metric,
            endpoint_filter=endpoint_name,
        ),
    }


@router.get("/runs/{token}/pods")
def get_run_pods(token: str):
    state = store.get(token)
    if not state:
        raise HTTPException(status_code=404, detail="run not found")

    _reconcile_running_state(state)
    return {"items": [state.build_pod_status_payload()]}


@router.get("/runs/{token}/pods/monitor")
def get_run_pods_monitor(token: str, step_seconds: int = 10):
    state = store.get(token)
    if not state:
        raise HTTPException(status_code=404, detail="run not found")

    _reconcile_running_state(state)
    if state.status == "running":
        state.append_pod_monitor_snapshot()
    else:
        state.ensure_terminal_pod_monitor_snapshot()
    series = state.build_pod_monitor_series(step_seconds=step_seconds)

    return {
        "step_seconds": step_seconds,
        "series": series,
    }


@router.get("/runs/{token}/logs")
def get_run_logs(
    token: str, cursor: Optional[int] = None, limit: int = 200, order: str = "asc"
):
    state = store.get(token)
    if not state:
        raise HTTPException(status_code=404, detail="run not found")
    state.load_logs_from_file()
    logs = _merge_runtime_and_jmeter_logs(token, state)
    logs.sort(key=lambda l: l.seq, reverse=(order == "desc"))

    def next_cur(seq_val: int) -> int:
        return seq_val

    filtered = []
    for log in logs:
        if cursor:
            if order == "desc" and log.seq >= cursor:
                continue
            if order == "asc" and log.seq <= cursor:
                continue
        filtered.append(log)
    sliced = filtered[: max(1, min(limit, 2000))]
    next_cursor = next_cur(sliced[-1].seq) if sliced else None
    return {
        "items": [
            {
                "seq": log.seq,
                "ts": log.ts,
                "level": log.level,
                "message": log.message,
                "source": log.source,
            }
            for log in sliced
        ],
        "next_cursor": next_cursor,
    }


_JMETER_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"(?P<level>[A-Z]+) "
    r"(?P<logger>[^:]+): "
    r"(?P<message>.*)$"
)
_JMETER_INFLUX_WRITE_FAILURE_RE = re.compile(
    r"Error writing metrics to influxDB",
    re.IGNORECASE,
)
_JMETER_INFLUX_NO_SPACE_RE = re.compile(
    r"(?:no space left on device|ENOSPC)", re.IGNORECASE
)


def _build_jmeter_influx_observability(
    token: str, state: RunState
) -> Optional[dict[str, Any]]:
    if str(state.engine_type or "").lower() != "jmeter":
        return None

    jmeter_log_path = AGENT_RUN_ROOT / token / "jmeter.log"
    if not jmeter_log_path.exists():
        return None

    try:
        raw_lines = jmeter_log_path.read_text(
            encoding="utf-8", errors="ignore"
        ).splitlines()
    except OSError:
        return None

    failures: list[str] = []
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line:
            continue
        if _JMETER_INFLUX_WRITE_FAILURE_RE.search(line) or (
            "influx" in line.lower() and _JMETER_INFLUX_NO_SPACE_RE.search(line)
        ):
            failures.append(line)

    if not failures:
        return None

    latest_failure = failures[-1]
    reason = "influx_write_failed"
    if _JMETER_INFLUX_NO_SPACE_RE.search(latest_failure):
        reason = "influx_write_no_space_left_on_device"

    return {
        "jmeter_influx": {
            "status": "failed",
            "reason": reason,
            "source": "jmeter.log",
            "failure_count": len(failures),
            "sample": latest_failure[:500],
        }
    }


def _merge_runtime_and_jmeter_logs(token: str, state: RunState) -> list[RunLog]:
    logs = list(state.logs)
    if str(state.engine_type or "").lower() != "jmeter":
        return logs

    jmeter_log_path = AGENT_RUN_ROOT / token / "jmeter.log"
    if not jmeter_log_path.exists():
        return logs

    next_seq = max((log.seq for log in logs), default=0)
    try:
        raw_lines = jmeter_log_path.read_text(
            encoding="utf-8", errors="ignore"
        ).splitlines()
    except OSError:
        return logs

    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line:
            continue
        parsed = _JMETER_LOG_LINE_RE.match(line)
        if parsed:
            try:
                ts = datetime.strptime(
                    parsed.group("ts"), "%Y-%m-%d %H:%M:%S,%f"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                ts = datetime.now(timezone.utc)
            level = parsed.group("level")
            message = f'{parsed.group("logger")}: {parsed.group("message")}'
        else:
            ts = datetime.now(timezone.utc)
            level = "INFO"
            message = line
        next_seq += 1
        logs.append(
            RunLog(
                seq=next_seq,
                ts=ts,
                level=level,
                message=message,
                source="tool-stdout",
            )
        )

    if (
        not state.jtl_failure_logs_emitted
        and state.jtl_path
        and state.jtl_path.exists()
    ):
        try:
            failure_logs = _build_jmeter_failure_run_logs(
                list(parse_jtl(state.jtl_path)),
                start_seq=next_seq,
            )
        except Exception:
            failure_logs = []
        logs.extend(failure_logs)
    return logs


def _build_jmeter_failure_run_logs(
    samples: list[Any],
    *,
    start_seq: int = 0,
) -> list[RunLog]:
    max_lines = max(1, int(os.getenv("JMETER_FAILURE_LOG_LIMIT", "50")))
    logs: list[RunLog] = []
    total_failures = 0
    next_seq = start_seq

    for sample in samples:
        if bool(getattr(sample, "success", False)):
            continue
        total_failures += 1
        if len(logs) >= max_lines:
            continue
        label = str(getattr(sample, "label", "") or "").strip() or "unknown"
        response_code = (
            str(getattr(sample, "response_code", "") or "").strip() or "unknown"
        )
        response_message = (
            str(getattr(sample, "response_message", "") or "").strip() or "unknown"
        )
        elapsed_ms = int(getattr(sample, "elapsed_ms", 0) or 0)
        thread_name = str(getattr(sample, "thread_name", "") or "").strip() or "unknown"
        next_seq += 1
        logs.append(
            RunLog(
                seq=next_seq,
                ts=getattr(sample, "ts", None) or datetime.now(timezone.utc),
                level="ERROR",
                message=(
                    "jmeter_sample_failed "
                    f'label="{label}" '
                    f'code="{response_code}" '
                    f'message="{response_message}" '
                    f"elapsed_ms={elapsed_ms} "
                    f'thread="{thread_name}"'
                ),
                source="tool-stderr",
            )
        )

    truncated = total_failures - len(logs)
    if truncated > 0:
        next_seq += 1
        logs.append(
            RunLog(
                seq=next_seq,
                ts=datetime.now(timezone.utc),
                level="WARN",
                message=f"jmeter_sample_failed_truncated omitted={truncated} limit={max_lines}",
                source="tool-stderr",
            )
        )
    return logs


def _materialize_script(
    token: str,
    request: ExecuteRequest,
    *,
    run_dir: Optional[Path] = None,
) -> Path:
    target_dir = run_dir or _ensure_run_dir(token)
    if request.script_content is not None:
        source_name = (
            request.script_file_name
            or (Path(request.script_path).name if request.script_path else None)
            or f"script-{request.script_id}"
        )
        script_path = target_dir / _build_script_file_name(request, source_name)
        script_path.write_text(request.script_content, encoding="utf-8")
    elif request.script_path:
        source_path = Path(request.script_path)
        if not source_path.exists():
            raise FileNotFoundError(f"script_path not found: {source_path}")
        script_path = target_dir / _build_script_file_name(request, source_path.name)
        shutil.copy2(source_path, script_path)
    elif request.script_s3:
        bucket, key = s3_utils.parse_s3_uri(request.script_s3)
        content = s3_utils.download_bytes(bucket, key)
        script_path = target_dir / _build_script_file_name(request, key)
        script_path.write_bytes(content)
    else:
        raise FileNotFoundError(
            "script_content, script_path, or script_s3 required for real exec"
        )
    return script_path


def _materialize_runtime_bundle(
    token: str,
    request: ExecuteRequest,
    *,
    materialization_records: Optional[list[dict[str, Any]]] = None,
) -> Tuple[Path, Path, list[Path], list[Path]]:
    run_dir = _ensure_run_dir(token)
    script_path = _materialize_script(token, request, run_dir=run_dir)
    data_paths = _materialize_data_assets(
        request,
        run_dir,
        materialization_records=materialization_records,
    )
    proto_paths = _materialize_proto_assets(request, run_dir)
    return run_dir, script_path, data_paths, proto_paths


async def run_jmeter_real(token: str, request: ExecuteRequest):
    state = store.get(token)
    if not state:
        return
    state.append_log("INFO", f"run_started token={token} mode=real")
    if not _is_host_process_scope_run(state):
        state.append_pod_monitor_snapshot()
    process_finished = False
    live_pod_monitor_stop: threading.Event | None = None
    live_pod_monitor_thread: threading.Thread | None = None
    push_gateway = os.getenv("PUSHGATEWAY_URL")
    try:
        materialization_records: list[dict[str, Any]] = []
        run_dir, script_path, data_paths, proto_paths = _materialize_runtime_bundle(
            token,
            request,
            materialization_records=materialization_records,
        )
        runtime_properties = _build_runtime_properties(
            request, run_dir, data_paths, proto_paths
        )
        runtime_properties = _enrich_jmeter_influx_properties(
            runtime_properties,
            request=request,
            token=token,
            state=state,
        )
        if data_paths:
            state.append_log(
                "INFO",
                "data_assets_materialized "
                f"count={len(data_paths)} dir={run_dir / 'data'} "
                f"distribution={request.effective_data_distribution or 'unset'}",
            )
            _append_runtime_asset_materialization_logs(state, materialization_records)
        if proto_paths:
            state.append_log(
                "INFO",
                "proto_assets_materialized "
                f"count={len(proto_paths)} dir={run_dir / 'proto'}",
            )

        jmeter_home = Path(os.getenv("JMETER_HOME", "")).resolve()
        if not jmeter_home.exists():
            raise FileNotFoundError(
                "JMETER_HOME not set or not exists; fallback to simulate"
            )

        runner = JMeterRunner(jmeter_home)
        process, result_path = runner.run_test(
            script_path=script_path,
            thread_count=request.thread_count,
            duration=request.resolved_duration,
            ramp_up=request.ramp_up,
            properties=runtime_properties,
            protocol=(
                request.protocol.value
                if hasattr(request.protocol, "value")
                else request.protocol
            ),
        )
        state.pid = process.pid
        _prime_process_scope_pod_monitor_snapshot(state)
        state.jtl_path = result_path
        stop_stream, threads = _start_s3_stream_uploader(token, state)
        threads += _stream_process_output(process, state)
        live_pod_monitor_stop, live_pod_monitor_thread = (
            _start_live_pod_monitor_sampling_thread(
                token=token,
                state=state,
                pushgateway=push_gateway,
            )
        )

        async def _refresh_jtl_while_running() -> None:
            while state.status == "running" and process.poll() is None:
                try:
                    _parse_jtl_and_update_metrics(state)
                except Exception as exc:  # pragma: no cover - best effort refresh
                    logger.debug(
                        "jmeter live jtl refresh skipped for %s: %s", token, exc
                    )
                await asyncio.sleep(2)

        refresh_task = asyncio.create_task(_refresh_jtl_while_running())
        return_code = await asyncio.to_thread(process.wait)
        process_finished = True
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass
        if state.ended_at is None:
            state.ended_at = datetime.now(timezone.utc)
        for t in threads:
            await asyncio.to_thread(t.join, 1)
        stop_stream()

        _parse_jtl_and_update_metrics(state, emit_failure_logs=True)

        _finalize_jmeter_completion(state, return_code)
    except asyncio.CancelledError:
        state.status = "stopped"
        state.append_log("WARN", "run_cancelled")
    except Exception as exc:
        if state.status != "stopped":
            state.status = "failed"
            state.error = str(exc)
            state.append_log("ERROR", f"real exec failed: {exc}")
            state.append_metrics(state.rps, state.rt_p95_ms)
    finally:
        if live_pod_monitor_stop:
            live_pod_monitor_stop.set()
        if live_pod_monitor_thread:
            await asyncio.to_thread(live_pod_monitor_thread.join, 1.0)
        if process_finished:
            _record_terminal_pod_monitor_snapshot_before_pid_release(state)
        if process_finished:
            state.pid = None
        if state.ended_at is None:
            state.ended_at = datetime.now(timezone.utc)
        if push_gateway:
            try:
                _push_final_metrics(push_gateway, token, state)
            except Exception as exc:
                state.append_log("WARN", f"pushgateway failed: {exc}")
        _archive_artifacts(token, state)


async def run_k6_real(token: str, request: ExecuteRequest):
    state = store.get(token)
    if not state:
        return
    state.append_log("INFO", f"run_started token={token} mode=real-k6")
    if not _is_host_process_scope_run(state):
        state.append_pod_monitor_snapshot()
    process_finished = False
    live_pod_monitor_stop: threading.Event | None = None
    live_pod_monitor_thread: threading.Thread | None = None
    push_gateway = os.getenv("PUSHGATEWAY_URL")
    try:
        materialization_records: list[dict[str, Any]] = []
        run_dir, script_path, data_paths, proto_paths = _materialize_runtime_bundle(
            token,
            request,
            materialization_records=materialization_records,
        )
        runtime_properties = _build_runtime_properties(
            request, run_dir, data_paths, proto_paths
        )
        protocol_value = (
            str(getattr(request.protocol, "value", request.protocol) or "")
            .strip()
            .lower()
        )
        state.k6_metric_family = protocol_value or None
        if data_paths:
            state.append_log(
                "INFO",
                "data_assets_materialized "
                f"count={len(data_paths)} dir={run_dir / 'data'} "
                f"distribution={request.effective_data_distribution or 'unset'}",
            )
            _append_runtime_asset_materialization_logs(state, materialization_records)
        if proto_paths:
            state.append_log(
                "INFO",
                "proto_assets_materialized "
                f"count={len(proto_paths)} dir={run_dir / 'proto'}",
            )
        k6_bin_env = os.getenv("K6_BIN") or os.getenv("K6_BINARY")
        k6_path = Path(k6_bin_env) if k6_bin_env else None
        if k6_path and not k6_path.exists():
            k6_path = None
        if k6_path is None:
            which_bin = shutil.which("k6")
            if which_bin:
                k6_path = Path(which_bin)
        if k6_path is None:
            k6_path = Path("/usr/local/bin/k6")
        runner = K6Runner(k6_path)
        summary_path = Path("/tmp") / f"k6_summary_{token}.json"
        control_address, control_port = _allocate_loopback_control_address()
        runtime_control_plan = runner.build_runtime_control_plan(
            script_path=script_path,
            vus=request.thread_count,
            duration=request.resolved_duration,
            envs=runtime_properties,
            protocol=request.protocol,
        )
        state.k6_control_host = "127.0.0.1"
        state.k6_control_port = control_port
        state.k6_control_url = f"http://127.0.0.1:{control_port}"
        state.k6_control_available = False
        state.k6_control_error = runtime_control_plan.reason
        state.k6_status_patch_supported = runtime_control_plan.status_patch_supported
        state.k6_status_patch_reason = runtime_control_plan.reason
        state.k6_status_patch_mode = runtime_control_plan.mode
        state.k6_control_mode = (
            runtime_control_plan.active_control_path or runtime_control_plan.mode
        )
        state.k6_script_family = runtime_control_plan.script_family
        state.k6_preferred_control_path = runtime_control_plan.preferred_control_path
        state.k6_active_control_path = runtime_control_plan.active_control_path
        state.k6_scenario_patch_supported = (
            runtime_control_plan.scenario_patch_supported
        )
        state.k6_scenario_patch_reason = runtime_control_plan.scenario_patch_reason
        state.k6_scenario_configs = K6Runner.serialize_scenario_configs(
            runtime_control_plan.scenario_configs
        )
        state.k6_runtime_properties = dict(runtime_properties or {})
        state.k6_runtime_properties.setdefault(
            "PTP_THREAD_COUNT", str(request.thread_count)
        )
        state.k6_runtime_properties.setdefault(
            "PTP_DURATION_SECONDS", str(request.resolved_duration)
        )
        state.k6_script_path = str(script_path)
        if (
            state.k6_active_control_path == "scenario_direct"
            and state.k6_scenario_configs
            and not state.k6_scenario_patch_supported
        ):
            state.append_log(
                "INFO",
                "k6_scenario_direct_static_ready "
                f"reason={state.k6_scenario_patch_reason or 'runtime_patch_unavailable'}",
            )
        elif state.k6_status_patch_supported or state.k6_scenario_patch_supported:
            state.append_log(
                "INFO",
                "k6_dynamic_control_ready "
                f"mode={state.k6_active_control_path or runtime_control_plan.mode or 'unknown'}",
            )
        else:
            state.append_log(
                "INFO",
                "k6_dynamic_control_unavailable "
                f"reason={state.k6_control_error or runtime_control_plan.reason or 'unknown'}",
            )
        if state.k6_scenario_patch_reason and state.k6_scenario_patch_supported:
            state.append_log(
                "INFO",
                "k6_scenario_direct_blocked "
                f"reason={state.k6_scenario_patch_reason}",
            )

        # Prometheus remote write URL for richer series
        prom_rw_url = os.getenv("PROMETHEUS_RW_URL") or os.getenv("PROMETHEUS_URL")
        if prom_rw_url:
            # Ensure URL ends with /api/v1/write
            if not prom_rw_url.endswith("/api/v1/write"):
                prom_rw_url = prom_rw_url.rstrip("/") + "/api/v1/write"
            state.append_log("INFO", f"k6_prometheus_rw_enabled url={prom_rw_url}")

        process = runner.run_test(
            script_path=script_path,
            vus=request.thread_count,
            duration=request.resolved_duration,
            ramp_up=request.ramp_up,
            iterations=(
                _coerce_positive_int((runtime_properties or {}).get("iterations"))
                or _coerce_positive_int((runtime_properties or {}).get("request_count"))
            ),
            envs=runtime_properties,
            summary_path=summary_path,
            prometheus_rw_url=prom_rw_url,
            run_token=token,
            run_id=request.run_id,
            protocol=request.protocol,
            control_address=control_address,
            runtime_control_plan=runtime_control_plan,
        )
        state.pid = process.pid
        _prime_process_scope_pod_monitor_snapshot(state)
        live_pod_monitor_stop, live_pod_monitor_thread = (
            _start_live_pod_monitor_sampling_thread(
                token=token,
                state=state,
                pushgateway=push_gateway,
            )
        )
        stop_stream, threads = _start_s3_stream_uploader(token, state)
        threads += _stream_process_output(process, state)
        return_code = await asyncio.to_thread(process.wait)
        process_finished = True
        if state.ended_at is None:
            state.ended_at = datetime.now(timezone.utc)
        for t in threads:
            await asyncio.to_thread(t.join, 1)
        stop_stream()
        _parse_k6_summary(state, summary_path)
        _reparse_k6_summary_if_needed(state, summary_path)
        if state.status != "stopped":
            if return_code == 0:
                state.status = "succeeded"
            else:
                state.status = "failed"
                state.error = f"k6 exit_code={return_code}"
            state.append_metrics(state.rps, state.rt_p95_ms)
        else:
            state.append_log("WARN", "run_stopped_before_k6_complete")
    except asyncio.CancelledError:
        state.status = "stopped"
        state.append_log("WARN", "run_cancelled")
    except Exception as exc:
        if state.status != "stopped":
            state.status = "failed"
            state.error = str(exc)
            state.append_log("ERROR", f"k6 exec failed: {exc}")
            state.append_metrics(state.rps, state.rt_p95_ms)
    finally:
        _stop_k6_tps_controller(state)
        if live_pod_monitor_stop:
            live_pod_monitor_stop.set()
        if live_pod_monitor_thread:
            await asyncio.to_thread(live_pod_monitor_thread.join, 1.0)
        if process_finished:
            _record_terminal_pod_monitor_snapshot_before_pid_release(state)
        if process_finished:
            state.pid = None
        if state.ended_at is None:
            state.ended_at = datetime.now(timezone.utc)
        if push_gateway:
            try:
                _push_final_metrics(push_gateway, token, state)
            except Exception as exc:
                state.append_log("WARN", f"pushgateway failed: {exc}")
        _archive_artifacts(token, state)


async def _run_live_pod_monitor_refresh_loop(
    *,
    token: str,
    state: RunState,
    pushgateway: Optional[str],
    interval_seconds: float = 5.0,
) -> None:
    while state.status == "running":
        try:
            snapshot = state.append_pod_monitor_snapshot()
            if pushgateway and (snapshot is not None or state.pod_monitor_history):
                _push_live_pod_monitor_metrics(pushgateway, token, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - best effort live refresh
            logger.debug("live pod monitor refresh skipped for %s: %s", token, exc)
        await asyncio.sleep(interval_seconds)


def _push_run_metrics(
    pushgateway: str,
    token: str,
    state: RunState,
    *,
    include_jmeter_series: bool,
    include_run_overview_metrics: bool,
):
    from prometheus_client import (
        CollectorRegistry,
        Gauge,
        Counter,
        Histogram,
        pushadd_to_gateway,
    )

    registry = CollectorRegistry()
    if include_run_overview_metrics:
        g_rps = Gauge("ptp_run_rps", "final rps", ["run_token"], registry=registry)
        g_rt = Gauge(
            "ptp_run_rt_p95_ms", "final rt p95", ["run_token"], registry=registry
        )
        g_status = Gauge(
            "ptp_run_status",
            "run status (1=succ,0=fail)",
            ["run_token"],
            registry=registry,
        )
        g_rps.labels(run_token=token).set(state.rps or 0.0)
        g_rt.labels(run_token=token).set(state.rt_p95_ms or 0.0)
        g_status.labels(run_token=token).set(1 if state.status == "succeeded" else 0)

    run_id = str(state.run_id) if state.run_id is not None else token
    pod_metric_labels = _build_run_scoped_pod_metric_labels(token, run_id, state)
    memory_current = state.get_pod_monitor_metric_aggregate(
        "memory_usage_percent", "current"
    )
    memory_max = state.get_pod_monitor_metric_aggregate("memory_usage_percent", "max")
    memory_avg = state.get_pod_monitor_metric_aggregate("memory_usage_percent", "avg")
    memory_used_current = state.get_pod_monitor_metric_aggregate(
        "memory_used_bytes", "current"
    )
    cpu_current = state.get_pod_monitor_metric_aggregate("cpu_usage_percent", "current")
    cpu_max = state.get_pod_monitor_metric_aggregate("cpu_usage_percent", "max")
    cpu_avg = state.get_pod_monitor_metric_aggregate("cpu_usage_percent", "avg")
    cpu_load_current = state.get_pod_monitor_metric_aggregate("cpu_load", "current")
    socket_current = state.get_pod_monitor_metric_aggregate("socket_count", "current")
    socket_peak = state.get_pod_monitor_metric_aggregate("socket_count", "max")
    if memory_current is None or socket_current is None:
        state.ensure_terminal_pod_monitor_snapshot()
        cpu_current = state.get_pod_monitor_metric_aggregate(
            "cpu_usage_percent", "current"
        )
        cpu_max = state.get_pod_monitor_metric_aggregate("cpu_usage_percent", "max")
        cpu_avg = state.get_pod_monitor_metric_aggregate("cpu_usage_percent", "avg")
        cpu_load_current = state.get_pod_monitor_metric_aggregate("cpu_load", "current")
        memory_current = state.get_pod_monitor_metric_aggregate(
            "memory_usage_percent", "current"
        )
        memory_max = state.get_pod_monitor_metric_aggregate(
            "memory_usage_percent", "max"
        )
        memory_avg = state.get_pod_monitor_metric_aggregate(
            "memory_usage_percent", "avg"
        )
        memory_used_current = state.get_pod_monitor_metric_aggregate(
            "memory_used_bytes", "current"
        )
        socket_current = state.get_pod_monitor_metric_aggregate(
            "socket_count", "current"
        )
        socket_peak = state.get_pod_monitor_metric_aggregate("socket_count", "max")

    g_cpu_usage_percent_max = Gauge(
        "ptp_run_pod_cpu_usage_percent_max",
        "peak pod cpu usage percent",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_cpu_usage_percent_avg = Gauge(
        "ptp_run_pod_cpu_usage_percent_avg",
        "average pod cpu usage percent",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_cpu_usage_percent_current = Gauge(
        "ptp_run_pod_cpu_usage_percent_current",
        "current pod cpu usage percent",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_cpu_load_current = Gauge(
        "ptp_run_pod_cpu_load_current",
        "current pod cpu load average",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_memory_usage_percent_max = Gauge(
        "ptp_run_pod_memory_usage_percent_max",
        "peak pod memory usage percent",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_memory_usage_percent_avg = Gauge(
        "ptp_run_pod_memory_usage_percent_avg",
        "average pod memory usage percent",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_memory_usage_percent_current = Gauge(
        "ptp_run_pod_memory_usage_percent_current",
        "current pod memory usage percent",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_memory_used_bytes_current = Gauge(
        "ptp_run_pod_memory_used_bytes_current",
        "current pod memory used bytes",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_socket_count_peak = Gauge(
        "ptp_run_pod_socket_count_peak",
        "peak pod socket count",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_socket_count_current = Gauge(
        "ptp_run_pod_socket_count_current",
        "current pod socket count",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )

    def _set_pod_gauge_if_present(gauge: Gauge, value: Optional[float]) -> None:
        if value is None:
            return
        gauge.labels(**pod_metric_labels).set(float(value))

    _set_pod_gauge_if_present(g_cpu_usage_percent_max, cpu_max)
    _set_pod_gauge_if_present(g_cpu_usage_percent_avg, cpu_avg)
    _set_pod_gauge_if_present(g_cpu_usage_percent_current, cpu_current)
    _set_pod_gauge_if_present(g_cpu_load_current, cpu_load_current)
    _set_pod_gauge_if_present(g_memory_usage_percent_max, memory_max)
    _set_pod_gauge_if_present(g_memory_usage_percent_avg, memory_avg)
    _set_pod_gauge_if_present(g_memory_usage_percent_current, memory_current)
    _set_pod_gauge_if_present(g_memory_used_bytes_current, memory_used_current)
    _set_pod_gauge_if_present(g_socket_count_peak, socket_peak)
    _set_pod_gauge_if_present(g_socket_count_current, socket_current)

    effective_end_at = state.ended_at or datetime.now(timezone.utc)
    duration_seconds = (
        max((effective_end_at - state.started_at).total_seconds(), 1.0)
        if effective_end_at and state.started_at
        else 1.0
    )

    def _delta_or_current(metric_name: str) -> Optional[float]:
        current = state.get_pod_monitor_metric_aggregate(metric_name, "current")
        if current is None:
            return None
        if len(state.pod_monitor_history) >= 2:
            baseline = state.pod_monitor_history[0].get(metric_name)
            if isinstance(baseline, (int, float)):
                return max(0.0, float(current) - float(baseline))
        return float(current)

    network_rx_bytes_delta = _delta_or_current("network_rx_bytes")
    network_tx_bytes_delta = _delta_or_current("network_tx_bytes")
    network_rx_packets_delta = _delta_or_current("network_rx_packets")
    network_tx_packets_delta = _delta_or_current("network_tx_packets")
    disk_used_bytes_current = state.get_pod_monitor_metric_aggregate(
        "disk_used_bytes", "current"
    )
    disk_total_bytes_current = state.get_pod_monitor_metric_aggregate(
        "disk_total_bytes", "current"
    )
    disk_usage_percent_current = state.get_pod_monitor_metric_aggregate(
        "disk_usage_percent", "current"
    )
    disk_read_bytes_delta = _delta_or_current("disk_read_bytes")
    disk_write_bytes_delta = _delta_or_current("disk_write_bytes")

    g_network_rx_bps_avg = Gauge(
        "ptp_run_pod_network_rx_bytes_per_sec_avg",
        "average pod network rx bytes per second across the run window",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_network_tx_bps_avg = Gauge(
        "ptp_run_pod_network_tx_bytes_per_sec_avg",
        "average pod network tx bytes per second across the run window",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_network_rx_pps_avg = Gauge(
        "ptp_run_pod_network_rx_packets_per_sec_avg",
        "average pod network rx packets per second across the run window",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_network_tx_pps_avg = Gauge(
        "ptp_run_pod_network_tx_packets_per_sec_avg",
        "average pod network tx packets per second across the run window",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_disk_used_bytes_current = Gauge(
        "ptp_run_pod_disk_used_bytes_current",
        "current pod disk used bytes",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_disk_total_bytes_current = Gauge(
        "ptp_run_pod_disk_total_bytes_current",
        "current pod disk total bytes",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_disk_usage_percent_current = Gauge(
        "ptp_run_pod_disk_usage_percent_current",
        "current pod disk usage percent",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_disk_read_bps_avg = Gauge(
        "ptp_run_pod_disk_read_bytes_per_sec_avg",
        "average pod disk read bytes per second across the run window",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )
    g_disk_write_bps_avg = Gauge(
        "ptp_run_pod_disk_write_bytes_per_sec_avg",
        "average pod disk write bytes per second across the run window",
        _RUN_SCOPED_POD_METRIC_LABELS,
        registry=registry,
    )

    _set_pod_gauge_if_present(
        g_network_rx_bps_avg,
        (
            network_rx_bytes_delta / duration_seconds
            if network_rx_bytes_delta is not None
            else None
        ),
    )
    _set_pod_gauge_if_present(
        g_network_tx_bps_avg,
        (
            network_tx_bytes_delta / duration_seconds
            if network_tx_bytes_delta is not None
            else None
        ),
    )
    _set_pod_gauge_if_present(
        g_network_rx_pps_avg,
        (
            network_rx_packets_delta / duration_seconds
            if network_rx_packets_delta is not None
            else None
        ),
    )
    _set_pod_gauge_if_present(
        g_network_tx_pps_avg,
        (
            network_tx_packets_delta / duration_seconds
            if network_tx_packets_delta is not None
            else None
        ),
    )
    _set_pod_gauge_if_present(g_disk_used_bytes_current, disk_used_bytes_current)
    _set_pod_gauge_if_present(g_disk_total_bytes_current, disk_total_bytes_current)
    _set_pod_gauge_if_present(g_disk_usage_percent_current, disk_usage_percent_current)
    _set_pod_gauge_if_present(
        g_disk_read_bps_avg,
        (
            disk_read_bytes_delta / duration_seconds
            if disk_read_bytes_delta is not None
            else None
        ),
    )
    _set_pod_gauge_if_present(
        g_disk_write_bps_avg,
        (
            disk_write_bytes_delta / duration_seconds
            if disk_write_bytes_delta is not None
            else None
        ),
    )

    # JMeter richer series are final-only. Re-pushing counters/histograms during a live run
    # would blur the meaning of each point in Grafana and make current/final proof harder to read.
    if include_jmeter_series and state.jtl_summary:
        summary = state.jtl_summary
        endpoint_samples = (
            _group_jmeter_samples_by_endpoint(list(parse_jtl(state.jtl_path)))
            if state.jtl_path and state.jtl_path.exists()
            else {}
        )

        # jmeter_samples_total - total request count
        c_samples = Counter(
            "jmeter_samples_total",
            "Total samples",
            ["run_id", "endpoint_name"],
            registry=registry,
        )

        # jmeter_errors_total - failed request count
        c_errors = Counter(
            "jmeter_errors_total",
            "Total errors",
            ["run_id", "endpoint_name"],
            registry=registry,
        )
        c_count = Counter(
            "jmeter_count_total",
            "Total samples (marketplace compatibility)",
            ["run_id", "endpoint_name"],
            registry=registry,
        )
        c_failure = Counter(
            "jmeter_failure_total",
            "Total failures (marketplace compatibility)",
            ["run_id", "endpoint_name"],
            registry=registry,
        )

        # jmeter_bytes_total - received bytes (use total_requests as proxy if not available)
        c_bytes = Counter(
            "jmeter_bytes_total",
            "Total bytes received",
            ["run_id", "endpoint_name"],
            registry=registry,
        )
        g_summary = Gauge(
            "jmeter_summary",
            "JMeter quantiles (marketplace compatibility)",
            ["run_id", "endpoint_name", "quantile"],
            registry=registry,
        )

        # jmeter_response_time - histogram buckets for percentile queries
        # Use predefined buckets matching Grafana dashboard expectations
        buckets = (50, 100, 200, 500, 1000, 2000, 5000, 10000)
        h_rt = Histogram(
            "jmeter_response_time",
            "Response time histogram",
            ["run_id", "endpoint_name"],
            buckets=buckets,
            registry=registry,
        )
        if endpoint_samples:
            for endpoint_name, endpoint_rows in sorted(endpoint_samples.items()):
                endpoint_summary = summarize_samples(endpoint_rows) or {}
                total_requests = int(endpoint_summary.get("total_requests") or 0)
                failed_requests = int(endpoint_summary.get("failed_requests") or 0)
                total_bytes = sum(
                    int(getattr(sample, "bytes", 0) or 0) for sample in endpoint_rows
                )

                c_samples.labels(run_id=run_id, endpoint_name=endpoint_name).inc(
                    total_requests
                )
                c_count.labels(run_id=run_id, endpoint_name=endpoint_name).inc(
                    total_requests
                )
                c_errors.labels(run_id=run_id, endpoint_name=endpoint_name).inc(
                    failed_requests
                )
                c_failure.labels(run_id=run_id, endpoint_name=endpoint_name).inc(
                    failed_requests
                )
                c_bytes.labels(run_id=run_id, endpoint_name=endpoint_name).inc(
                    total_bytes
                )

                for quantile, summary_key in (
                    ("0.5", "p50_response_time"),
                    ("0.95", "p95_response_time"),
                    ("0.99", "p99_response_time"),
                ):
                    value = endpoint_summary.get(summary_key)
                    if value is not None:
                        g_summary.labels(
                            run_id=run_id,
                            endpoint_name=endpoint_name,
                            quantile=quantile,
                        ).set(float(value))

                histogram = h_rt.labels(run_id=run_id, endpoint_name=endpoint_name)
                for sample in endpoint_rows:
                    histogram.observe(int(getattr(sample, "elapsed_ms", 0) or 0))
        else:
            endpoint_name = "overall"
            total_requests = int(summary.get("total_requests") or 0)
            failed_requests = int(summary.get("failed_requests") or 0)
            c_samples.labels(run_id=run_id, endpoint_name=endpoint_name).inc(
                total_requests
            )
            c_count.labels(run_id=run_id, endpoint_name=endpoint_name).inc(
                total_requests
            )
            c_errors.labels(run_id=run_id, endpoint_name=endpoint_name).inc(
                failed_requests
            )
            c_failure.labels(run_id=run_id, endpoint_name=endpoint_name).inc(
                failed_requests
            )
            c_bytes.labels(run_id=run_id, endpoint_name=endpoint_name).inc(0)
            for quantile, summary_key in (
                ("0.5", "p50_response_time"),
                ("0.95", "p95_response_time"),
                ("0.99", "p99_response_time"),
            ):
                value = summary.get(summary_key)
                if value is not None:
                    g_summary.labels(
                        run_id=run_id,
                        endpoint_name=endpoint_name,
                        quantile=quantile,
                    ).set(float(value))
            p95 = summary.get("p95_response_time", 0)
            if total_requests > 0 and p95 > 0:
                h_rt.labels(run_id=run_id, endpoint_name=endpoint_name).observe(
                    float(p95)
                )

    # Dual-agent runs push the same run_token from two nodes. `pushadd` keeps
    # node-scoped pod series from both agents instead of letting the later push
    # replace the earlier group's metrics wholesale.
    pushadd_to_gateway(
        pushgateway,
        job="ptp-agent",
        registry=registry,
        grouping_key={"run_token": token},
    )


def _push_live_pod_monitor_metrics(pushgateway: str, token: str, state: RunState):
    _push_run_metrics(
        pushgateway,
        token,
        state,
        include_jmeter_series=False,
        include_run_overview_metrics=False,
    )


def _push_final_metrics(pushgateway: str, token: str, state: RunState):
    _push_run_metrics(
        pushgateway,
        token,
        state,
        include_jmeter_series=True,
        include_run_overview_metrics=True,
    )


def _pump_stream(stream, level: str, state: RunState, source: str):
    if not stream:
        return
    for raw in iter(stream.readline, b""):
        if not raw:
            break
        try:
            line = raw.decode(errors="ignore").rstrip("\n")
        except Exception:
            continue
        state.append_output_line(line, level=level, source=source)
    try:
        stream.close()
    except Exception:
        pass


def _stream_process_output(process: subprocess.Popen, state: RunState):
    threads = []
    if process.stdout:
        t_out = threading.Thread(
            target=_pump_stream,
            args=(process.stdout, "INFO", state, "tool-stdout"),
            daemon=True,
        )
        threads.append(t_out)
        t_out.start()
    if process.stderr:
        t_err = threading.Thread(
            target=_pump_stream,
            args=(process.stderr, "ERROR", state, "tool-stderr"),
            daemon=True,
        )
        threads.append(t_err)
        t_err.start()
    return threads


def _start_s3_stream_uploader(
    token: str, state: RunState
) -> Tuple[callable, list[threading.Thread]]:
    use_s3 = os.getenv("LOG_ARCHIVE_S3", os.getenv("USE_S3", "0")) == "1"
    bucket = os.getenv("S3_BUCKET") or settings.S3_BUCKET
    if not use_s3 or not bucket:
        return (lambda: None), []
    interval = float(os.getenv("LOG_STREAM_UPLOAD_INTERVAL", "5"))
    prefix = get_run_artifact_prefix()
    stop_event = threading.Event()
    uploads = []

    def _uploader(path: Optional[Path], suffix: str):
        if not path:
            return
        key = f"{prefix}/{token}{suffix}"
        while not stop_event.is_set():
            if path.exists():
                try:
                    data = path.read_bytes()
                    ct = "text/plain"
                    if suffix.endswith(".gz"):
                        import gzip

                        data = gzip.compress(data)
                        ct = "application/gzip"
                    s3_utils.upload_bytes(bucket, key, data, content_type=ct)
                    if suffix == ".log":
                        state.s3_log_uri = f"s3://{bucket}/{key}"
                    else:
                        state.s3_metrics_uri = f"s3://{bucket}/{key}"
                except Exception:
                    pass
            time.sleep(max(1.0, interval))
        # final flush
        if path and path.exists():
            try:
                data = path.read_bytes()
                ct = "text/plain"
                if suffix.endswith(".gz"):
                    import gzip

                    data = gzip.compress(data)
                    ct = "application/gzip"
                s3_utils.upload_bytes(bucket, key, data, content_type=ct)
            except Exception:
                pass

    threads = []
    if state.log_path:
        t = threading.Thread(
            target=_uploader, args=(state.log_path, ".log"), daemon=True
        )
        threads.append(t)
        t.start()
    if state.metrics_path:
        t = threading.Thread(
            target=_uploader, args=(state.metrics_path, ".metrics.gz"), daemon=True
        )
        threads.append(t)
        t.start()

    def _stop():
        stop_event.set()

    return _stop, threads


def _parse_jtl_and_update_metrics(state: RunState, *, emit_failure_logs: bool = False):
    path = state.jtl_path
    if not path or not path.exists():
        return
    samples = list(parse_jtl(path))
    overall_duration_ms = None
    if samples:
        overall_duration_ms = (samples[-1].ts - samples[0].ts).total_seconds() * 1000
    summary = summarize_samples(samples, duration_ms_override=overall_duration_ms)
    if not summary:
        return
    check_rows = _build_jmeter_check_items_from_samples(samples)
    if check_rows:
        summary["checks"] = check_rows
    endpoint_rows = _build_jmeter_endpoint_summary_rows(
        samples,
        overall_duration_ms=overall_duration_ms,
    )
    if endpoint_rows:
        summary["endpoint_metrics"] = endpoint_rows
    endpoint_trends = _build_jmeter_endpoint_trends_from_samples(
        samples,
        step_seconds=5,
    )
    if endpoint_trends:
        summary["endpoint_trends"] = endpoint_trends
    state.jtl_summary = summary
    state.rps = float(summary.get("throughput", state.rps))
    state.rt_p95_ms = float(summary.get("p95_response_time", state.rt_p95_ms))
    if emit_failure_logs:
        _append_jmeter_failure_logs_from_samples(state, samples)
    state.append_metrics(state.rps, state.rt_p95_ms)


def _append_jmeter_failure_logs_from_samples(
    state: RunState, samples: list[Any]
) -> None:
    if state.jtl_failure_logs_emitted:
        return
    state.jtl_failure_logs_emitted = True
    for log in _build_jmeter_failure_run_logs(samples, start_seq=state.last_seq):
        state.append_log(log.level, log.message, source=log.source)


def _build_jmeter_endpoint_summary_rows(
    samples: list[Any],
    *,
    overall_duration_ms: Optional[float] = None,
) -> list[dict[str, object]]:
    grouped_samples: dict[str, list[Any]] = {}
    for sample in samples:
        endpoint_name = str(getattr(sample, "label", "") or "").strip()
        if not endpoint_name:
            continue
        grouped_samples.setdefault(endpoint_name, []).append(sample)

    rows: list[dict[str, object]] = []
    for endpoint_name, endpoint_samples in sorted(grouped_samples.items()):
        summary = summarize_samples(
            endpoint_samples,
            duration_ms_override=overall_duration_ms,
        )
        if not summary:
            continue
        elapsed_values = [
            int(getattr(sample, "elapsed_ms", 0) or 0) for sample in endpoint_samples
        ]
        row: dict[str, object] = {
            "endpoint_name": endpoint_name,
            "avg_rt_ms": summary.get("avg_response_time"),
            "p95_rt_ms": summary.get("p95_response_time"),
            "p99_rt_ms": summary.get("p99_response_time"),
            "total_requests": summary.get("total_requests"),
            "throughput": summary.get("throughput"),
        }
        if elapsed_values:
            row["min_rt_ms"] = float(min(elapsed_values))
            row["max_rt_ms"] = float(max(elapsed_values))
        rows.append({key: value for key, value in row.items() if value is not None})
    return rows


def _group_jmeter_samples_by_endpoint(samples: list[Any]) -> dict[str, list[Any]]:
    grouped_samples: dict[str, list[Any]] = {}
    for sample in samples:
        endpoint_name = str(getattr(sample, "label", "") or "").strip()
        if not endpoint_name:
            continue
        grouped_samples.setdefault(endpoint_name, []).append(sample)
    return grouped_samples


def _build_endpoint_trends_from_rows(
    state: RunState,
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    if not rows:
        return []

    start_ts = state.started_at
    end_ts = state.ended_at or datetime.now(timezone.utc)
    if end_ts <= start_ts:
        end_ts = start_ts

    metric_specs = [
        ("throughput", "throughput", "rps"),
        ("rt_avg_ms", "avg_rt_ms", "ms"),
        ("rt_p95_ms", "p95_rt_ms", "ms"),
        ("rt_p99_ms", "p99_rt_ms", "ms"),
    ]

    trends: list[dict[str, object]] = []
    for row in rows:
        endpoint_name = str(row.get("endpoint_name") or "").strip()
        if not endpoint_name:
            continue
        for metric_name, payload_key, unit in metric_specs:
            try:
                numeric_value = float(row.get(payload_key))
            except (TypeError, ValueError):
                continue
            trends.append(
                {
                    "endpoint_name": endpoint_name,
                    "metric": metric_name,
                    "unit": unit,
                    "points": [
                        {"ts": start_ts.isoformat(), "value": numeric_value},
                        {"ts": end_ts.isoformat(), "value": numeric_value},
                    ],
                }
            )
    return trends


def _extract_k6_metric(
    metrics: dict, metric_names: tuple[str, ...], stat_names: tuple[str, ...]
):
    for metric_name in metric_names:
        metric = metrics.get(metric_name)
        if not isinstance(metric, dict):
            continue
        values = metric.get("values")
        if not isinstance(values, dict):
            values = {}
        for stat_name in stat_names:
            value = metric.get(stat_name)
            if value is None:
                value = values.get(stat_name)
            if value is not None:
                return value
    return None


_K6_SUBMETRIC_PATTERN = re.compile(r"^(?P<metric>[^{]+)\{(?P<tags>.+)\}$")


def _parse_k6_submetric_key(metric_key: str) -> tuple[str, dict[str, str]]:
    if not isinstance(metric_key, str):
        return "", {}
    match = _K6_SUBMETRIC_PATTERN.match(metric_key.strip())
    if not match:
        return metric_key.strip(), {}

    tags: dict[str, str] = {}
    for raw_part in match.group("tags").split(","):
        if ":" not in raw_part:
            continue
        raw_key, raw_value = raw_part.split(":", 1)
        key = raw_key.strip()
        value = raw_value.strip()
        if key and value:
            tags[key] = value
    return match.group("metric").strip(), tags


def _resolve_k6_endpoint_name(tags: dict[str, str]) -> Optional[str]:
    for key in ("name", "endpoint_name"):
        value = str(tags.get(key) or "").strip()
        if value:
            return value

    grpc_service = str(
        tags.get("grpc_service") or tags.get("service") or tags.get("rpc_service") or ""
    ).strip()
    grpc_method = str(tags.get("grpc_method") or tags.get("rpc_method") or "").strip()
    method = str(tags.get("method") or "").strip()
    url = str(tags.get("url") or "").strip()
    if grpc_service and grpc_method:
        return f"{grpc_service}/{grpc_method}"
    if grpc_service and method and not url:
        return f"{grpc_service}/{method}"
    if grpc_method:
        return grpc_method
    if grpc_service:
        return grpc_service
    method = method.upper()
    if method and url:
        return f"{method} {url}"
    if url:
        return url
    return None


def _build_k6_endpoint_summary_rows(
    metrics: dict,
    request_metric_names: tuple[str, ...],
    duration_metric_names: tuple[str, ...],
    failed_metric_names: tuple[str, ...],
    *,
    fallback_total_requests: Optional[int] = None,
    fallback_throughput: Optional[float] = None,
) -> list[dict[str, object]]:
    endpoint_metrics: dict[str, dict[str, dict]] = {}

    for raw_metric_name, metric_payload in metrics.items():
        if not isinstance(metric_payload, dict):
            continue
        base_metric_name, tags = _parse_k6_submetric_key(str(raw_metric_name))
        endpoint_name = _resolve_k6_endpoint_name(tags)
        if not endpoint_name:
            continue
        endpoint_metrics.setdefault(endpoint_name, {})[
            base_metric_name
        ] = metric_payload

    def collect_metric(
        metric_bucket: dict[str, dict],
        metric_names: tuple[str, ...],
        stat_names: tuple[str, ...],
    ) -> Optional[float]:
        for metric_name in metric_names:
            metric_payload = metric_bucket.get(metric_name)
            if not isinstance(metric_payload, dict):
                continue
            nested = metric_payload.get("values")
            if isinstance(nested, dict):
                metric_payload = nested
            for stat_name in stat_names:
                value = metric_payload.get(stat_name)
                if value is None:
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    rows: list[dict[str, object]] = []
    for endpoint_name, metric_bucket in sorted(endpoint_metrics.items()):
        total_requests = collect_metric(metric_bucket, request_metric_names, ("count",))
        throughput = collect_metric(metric_bucket, request_metric_names, ("rate",))
        if total_requests is None and fallback_total_requests is not None:
            total_requests = float(fallback_total_requests)
        if throughput is None and fallback_throughput is not None:
            throughput = fallback_throughput
        avg_rt_ms = collect_metric(metric_bucket, duration_metric_names, ("avg",))
        p95_rt_ms = collect_metric(metric_bucket, duration_metric_names, ("p(95)",))
        p99_rt_ms = collect_metric(metric_bucket, duration_metric_names, ("p(99)",))
        max_rt_ms = collect_metric(metric_bucket, duration_metric_names, ("max",))
        min_rt_ms = collect_metric(metric_bucket, duration_metric_names, ("min",))
        error_rate = collect_metric(metric_bucket, failed_metric_names, ("rate",))

        signal_values = (
            total_requests,
            throughput,
            avg_rt_ms,
            p95_rt_ms,
            p99_rt_ms,
            max_rt_ms,
            min_rt_ms,
            error_rate,
        )
        if not any(value is not None and float(value) > 0 for value in signal_values):
            continue

        row: dict[str, object] = {
            "endpoint_name": endpoint_name,
            "avg_rt_ms": avg_rt_ms,
            "p95_rt_ms": p95_rt_ms,
            "p99_rt_ms": p99_rt_ms,
            "max_rt_ms": max_rt_ms,
            "min_rt_ms": min_rt_ms,
            "throughput": throughput,
        }
        if total_requests is not None:
            row["total_requests"] = int(total_requests)
        if error_rate is not None:
            row["error_rate"] = error_rate
        rows.append({key: value for key, value in row.items() if value is not None})
    return rows


def _merge_k6_endpoint_summary_rows(
    *row_groups: list[dict[str, object]]
) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for rows in row_groups:
        for row in rows:
            if not isinstance(row, dict):
                continue
            endpoint_name = str(row.get("endpoint_name") or "").strip()
            if not endpoint_name:
                continue
            merged[endpoint_name] = {**merged.get(endpoint_name, {}), **row}
    return [merged[key] for key in sorted(merged)]


def _aggregate_k6_endpoint_summary_rows(
    rows: list[dict[str, object]],
) -> dict[str, float | int]:
    total_requests = 0
    throughput = 0.0
    weighted_avg_total = 0.0
    weighted_avg_weight = 0.0
    p95_candidates: list[float] = []
    p99_candidates: list[float] = []
    max_candidates: list[float] = []
    min_candidates: list[float] = []
    error_rate_candidates: list[float] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        total = row.get("total_requests")
        try:
            total_value = int(total) if total is not None else 0
        except (TypeError, ValueError):
            total_value = 0
        total_requests += max(0, total_value)

        try:
            throughput_value = (
                float(row.get("throughput"))
                if row.get("throughput") is not None
                else 0.0
            )
        except (TypeError, ValueError):
            throughput_value = 0.0
        throughput += max(0.0, throughput_value)

        try:
            avg_rt_value = (
                float(row.get("avg_rt_ms"))
                if row.get("avg_rt_ms") is not None
                else None
            )
        except (TypeError, ValueError):
            avg_rt_value = None
        if avg_rt_value is not None:
            weight = float(total_value or 1)
            weighted_avg_total += avg_rt_value * weight
            weighted_avg_weight += weight

        for field_name, collector in (
            ("p95_rt_ms", p95_candidates),
            ("p99_rt_ms", p99_candidates),
            ("max_rt_ms", max_candidates),
        ):
            try:
                value = (
                    float(row.get(field_name))
                    if row.get(field_name) is not None
                    else None
                )
            except (TypeError, ValueError):
                value = None
            if value is not None:
                collector.append(value)

        try:
            min_value = (
                float(row.get("min_rt_ms"))
                if row.get("min_rt_ms") is not None
                else None
            )
        except (TypeError, ValueError):
            min_value = None
        if min_value is not None:
            min_candidates.append(min_value)

        try:
            error_rate = (
                float(row.get("error_rate"))
                if row.get("error_rate") is not None
                else None
            )
        except (TypeError, ValueError):
            error_rate = None
        if error_rate is not None:
            error_rate_candidates.append(error_rate)

    aggregated: dict[str, float | int] = {}
    if total_requests > 0:
        aggregated["total_requests"] = total_requests
    if throughput > 0:
        aggregated["throughput"] = round(throughput, 4)
    if weighted_avg_weight > 0:
        aggregated["rt_avg_ms"] = round(weighted_avg_total / weighted_avg_weight, 4)
    if p95_candidates:
        aggregated["rt_p95_ms"] = max(p95_candidates)
    if p99_candidates:
        aggregated["rt_p99_ms"] = max(p99_candidates)
    if max_candidates:
        aggregated["rt_max_ms"] = max(max_candidates)
    if min_candidates:
        aggregated["rt_min_ms"] = min(min_candidates)
    if error_rate_candidates and total_requests > 0:
        max_error_rate = max(error_rate_candidates)
        aggregated["error_rate"] = max_error_rate
        aggregated["failed_requests"] = round(total_requests * max_error_rate)
        aggregated["successful_requests"] = max(
            0, total_requests - int(aggregated["failed_requests"])
        )
        aggregated["success_rate"] = max(0.0, min(1.0, 1 - max_error_rate))
    return aggregated


def _build_k6_endpoint_trends(
    state: RunState,
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    return _build_endpoint_trends_from_rows(state, rows)


def _detect_fatal_jmeter_output(state: RunState) -> Optional[str]:
    fatal_markers = (
        "Uncaught Exception",
        "ExceptionInInitializerError",
        "NoClassDefFoundError",
        "ClassNotFoundException",
    )
    for log in reversed(state.logs):
        if not str(log.source or "").startswith("tool-stderr"):
            continue
        message = str(log.message or "")
        if any(marker in message for marker in fatal_markers):
            return message
    return None


def _finalize_jmeter_completion(state: RunState, return_code: int) -> None:
    fatal_output = _detect_fatal_jmeter_output(state)
    if fatal_output and not state.error:
        state.error = fatal_output

    if state.status != "stopped":
        if return_code == 0 and not state.error:
            state.status = "succeeded"
        else:
            state.status = "failed"
            if not state.error:
                state.error = f"jmeter exit_code={return_code}"
        state.rps = max(1.0, state.rps)
        state.append_metrics(state.rps, state.rt_p95_ms)
    else:
        state.append_log("WARN", "run_stopped_before_jmeter_complete")


def _k6_summary_requires_retry(summary: Optional[dict[str, Any]]) -> bool:
    if not isinstance(summary, dict):
        return True

    checks = summary.get("checks")
    if not isinstance(checks, list):
        return False

    has_http_checks = any(
        isinstance(item, dict)
        and str(item.get("check_name") or "").strip().startswith("HTTP ")
        for item in checks
    )
    has_grpc_checks = any(
        isinstance(item, dict)
        and str(item.get("check_name") or "").strip().startswith("GRPC ")
        for item in checks
    )
    if not (has_http_checks and has_grpc_checks):
        return False

    endpoint_rows = summary.get("endpoint_metrics")
    if not isinstance(endpoint_rows, list):
        return True

    has_http_rows = any(
        isinstance(item, dict)
        and str(item.get("endpoint_name") or "")
        .strip()
        .startswith(("GET ", "POST ", "PUT ", "DELETE ", "PATCH "))
        for item in endpoint_rows
    )
    has_grpc_rows = any(
        isinstance(item, dict)
        and str(item.get("endpoint_name") or "").strip().startswith("hello.")
        for item in endpoint_rows
    )
    metric_family = str(summary.get("metric_family") or "").strip().lower()
    return metric_family != "mixed" or not (has_http_rows and has_grpc_rows)


def _reparse_k6_summary_if_needed(state: RunState, summary_path: Path) -> None:
    if not summary_path or not summary_path.exists():
        return
    if not _k6_summary_requires_retry(state.k6_summary):
        return

    retry_total = max(0, int(os.getenv("K6_SUMMARY_REPARSE_RETRIES", "3")))
    retry_delay = max(0.0, float(os.getenv("K6_SUMMARY_REPARSE_DELAY_SECONDS", "0.5")))
    if retry_total <= 0:
        return

    for attempt in range(1, retry_total + 1):
        time.sleep(retry_delay)
        candidate = RunState(
            task_id=state.task_id, run_id=state.run_id, engine_type=state.engine_type
        )
        _parse_k6_summary(candidate, summary_path)
        if candidate.k6_summary:
            state.k6_summary = candidate.k6_summary
            state.rps = candidate.rps
            state.rt_p95_ms = candidate.rt_p95_ms
        if not _k6_summary_requires_retry(state.k6_summary):
            state.append_log("INFO", f"k6_summary_reparse_recovered attempt={attempt}")
            return

    state.append_log("WARN", "k6_summary_reparse_incomplete")


def _collect_k6_check_rows(group_payload: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []

    def _visit(group: object, *, fallback_group_name: str) -> None:
        if not isinstance(group, dict):
            return
        group_name = (
            str(group.get("name") or fallback_group_name or "default").strip()
            or "default"
        )
        checks = group.get("checks")
        if isinstance(checks, dict):
            for check_name, payload in checks.items():
                if not isinstance(payload, dict):
                    continue
                passes = payload.get("passes")
                fails = payload.get("fails")
                try:
                    passes_value = float(passes or 0)
                    fails_value = float(fails or 0)
                except (TypeError, ValueError):
                    continue
                total = passes_value + fails_value
                if total <= 0:
                    continue
                rows.append(
                    {
                        "group_name": group_name,
                        "check_name": str(
                            payload.get("name") or check_name or ""
                        ).strip()
                        or str(check_name),
                        "success_rate": passes_value / total,
                    }
                )
        children = group.get("groups")
        if isinstance(children, dict):
            iterable = children.values()
        elif isinstance(children, list):
            iterable = children
        else:
            iterable = []
        for child in iterable:
            _visit(child, fallback_group_name=group_name)

    _visit(group_payload, fallback_group_name="default")
    return rows


def _parse_k6_summary(state: RunState, summary_path: Path):
    if not summary_path or not summary_path.exists():
        state.append_log("WARN", f"k6_summary_missing path={summary_path}")
        return
    try:
        import json

        data = json.loads(summary_path.read_text())
        metrics = data.get("metrics") or {}
        is_browser = any(name.startswith("browser_") for name in metrics)
        has_http_metrics = any(
            name in ("http_reqs", "http_req_duration")
            or str(name).startswith("http_reqs{")
            or str(name).startswith("http_req_duration{")
            for name in metrics
        )
        has_grpc_duration_metrics = any(
            name == "grpc_req_duration" or str(name).startswith("grpc_req_duration{")
            for name in metrics
        )
        has_iteration_metrics = any(
            name in metrics for name in ("iterations", "iteration_duration")
        )
        if is_browser:
            metric_family = "browser"
            request_metric_names = ("browser_http_reqs", "browser_http_req_duration")
            duration_metric_names = ("browser_http_req_duration",)
            failed_metric_names = ("browser_http_req_failed",)
        elif has_http_metrics and has_grpc_duration_metrics:
            metric_family = "mixed"
            request_metric_names = ("http_reqs", "http_req_duration")
            duration_metric_names = ("http_req_duration", "grpc_req_duration")
            failed_metric_names = ("http_req_failed",)
        elif has_http_metrics:
            metric_family = "http"
            request_metric_names = ("http_reqs", "http_req_duration")
            duration_metric_names = ("http_req_duration",)
            failed_metric_names = ("http_req_failed",)
        elif has_iteration_metrics:
            metric_family = "iteration"
            request_metric_names = ("iterations",)
            duration_metric_names = (
                ("grpc_req_duration", "iteration_duration")
                if has_grpc_duration_metrics
                else ("iteration_duration",)
            )
            failed_metric_names = ()
        else:
            metric_family = "http"
            request_metric_names = ("http_reqs", "http_req_duration")
            duration_metric_names = ("http_req_duration",)
            failed_metric_names = ("http_req_failed",)
        rps = _extract_k6_metric(metrics, request_metric_names, ("rate",))
        p50 = _extract_k6_metric(metrics, duration_metric_names, ("p(50)", "med"))
        p90 = _extract_k6_metric(metrics, duration_metric_names, ("p(90)",))
        p95 = _extract_k6_metric(metrics, duration_metric_names, ("p(95)",))
        p99 = _extract_k6_metric(metrics, duration_metric_names, ("p(99)",))
        avg = _extract_k6_metric(metrics, duration_metric_names, ("avg",))
        max_rt_ms = _extract_k6_metric(metrics, duration_metric_names, ("max",))
        min_rt_ms = _extract_k6_metric(metrics, duration_metric_names, ("min",))
        total_requests = (
            _extract_k6_metric(metrics, request_metric_names, ("count",))
            if metric_family != "iteration"
            else None
        )
        if metric_family == "mixed":
            total_requests = (
                sum(
                    value
                    for value in (
                        _extract_k6_metric(metrics, ("http_reqs",), ("count",)),
                        _extract_k6_metric(
                            metrics, ("grpc_reqs", "grpc_req_duration"), ("count",)
                        ),
                    )
                    if value is not None
                )
                or None
            )
            total_throughput = (
                sum(
                    value
                    for value in (
                        _extract_k6_metric(metrics, ("http_reqs",), ("rate",)),
                        _extract_k6_metric(
                            metrics, ("grpc_reqs", "grpc_req_duration"), ("rate",)
                        ),
                    )
                    if value is not None
                )
                or None
            )
            if total_throughput is not None:
                rps = total_throughput
        errors = (
            _extract_k6_metric(metrics, failed_metric_names, ("rate",))
            if failed_metric_names
            else None
        )
        checks_rate = _extract_k6_metric(
            metrics, ("checks", "checks_rate"), ("rate", "value")
        )
        checks_passes = _extract_k6_metric(
            metrics, ("checks", "checks_rate"), ("passes",)
        )
        checks_fails = _extract_k6_metric(
            metrics, ("checks", "checks_rate"), ("fails",)
        )
        iterations = (
            _extract_k6_metric(metrics, ("iterations",), ("count",))
            if metric_family == "iteration"
            else None
        )
        summary = {
            "metric_family": metric_family,
            "http_reqs": rps,
            "throughput": rps,
            "rt_avg_ms": avg,
            "rt_p50_ms": p50,
            "rt_p90_ms": p90,
            "rt_p95_ms": p95,
            "rt_p99_ms": p99,
            "rt_max_ms": max_rt_ms,
            "rt_min_ms": min_rt_ms,
            "checks_rate": checks_rate,
            "error_rate": errors,
            "total_requests": total_requests,
            "avg_response_time": avg,
            "p50_response_time": p50,
            "p90_response_time": p90,
            "p95_response_time": p95,
            "p99_response_time": p99,
            "max_response_time": max_rt_ms,
            "min_response_time": min_rt_ms,
        }
        if iterations is not None:
            summary["iterations"] = iterations
        if total_requests is not None:
            if errors is None:
                errors = 0.0
            failed_requests = round(float(total_requests) * float(errors))
            summary["error_rate"] = float(errors)
            summary["failed_requests"] = failed_requests
            summary["successful_requests"] = max(
                0, int(float(total_requests) - failed_requests)
            )
            summary["success_rate"] = max(0.0, min(1.0, 1 - float(errors)))
        elif checks_rate is not None:
            summary["success_rate"] = max(0.0, min(1.0, float(checks_rate)))
            if checks_passes is not None:
                summary["successful_requests"] = int(float(checks_passes))
            if checks_fails is not None:
                summary["failed_requests"] = int(float(checks_fails))
                summary["error_rate"] = max(0.0, min(1.0, 1 - float(checks_rate)))

        if metric_family == "mixed":
            http_rows = _build_k6_endpoint_summary_rows(
                metrics,
                request_metric_names=("http_reqs", "http_req_duration"),
                duration_metric_names=("http_req_duration",),
                failed_metric_names=("http_req_failed",),
            )
            grpc_rows = _build_k6_endpoint_summary_rows(
                metrics,
                request_metric_names=(
                    "grpc_reqs",
                    "grpc_req_duration",
                    "iteration_duration",
                ),
                duration_metric_names=("grpc_req_duration", "iteration_duration"),
                failed_metric_names=(),
            )
            endpoint_rows = _merge_k6_endpoint_summary_rows(http_rows, grpc_rows)
        else:
            endpoint_rows = _build_k6_endpoint_summary_rows(
                metrics,
                request_metric_names=request_metric_names,
                duration_metric_names=duration_metric_names,
                failed_metric_names=failed_metric_names,
                fallback_total_requests=(
                    int(float(iterations))
                    if metric_family == "iteration" and iterations is not None
                    else None
                ),
                fallback_throughput=(
                    float(rps)
                    if metric_family == "iteration" and rps is not None
                    else None
                ),
            )
        if endpoint_rows:
            summary["endpoint_metrics"] = endpoint_rows
            summary["endpoint_trends"] = _build_k6_endpoint_trends(state, endpoint_rows)
            if metric_family == "mixed":
                aggregated_endpoint_summary = _aggregate_k6_endpoint_summary_rows(
                    endpoint_rows
                )
                for key, value in aggregated_endpoint_summary.items():
                    current_value = summary.get(key)
                    if current_value is None:
                        summary[key] = value
                        continue
                    if key in {
                        "total_requests",
                        "successful_requests",
                        "failed_requests",
                        "throughput",
                    }:
                        try:
                            if float(value) > float(current_value):
                                summary[key] = value
                        except (TypeError, ValueError):
                            continue
            elif metric_family == "iteration":
                endpoint_total_requests = sum(
                    int(row.get("total_requests") or 0)
                    for row in endpoint_rows
                    if isinstance(row, dict)
                )
                if endpoint_total_requests > 0:
                    summary["total_requests"] = endpoint_total_requests
                    resolved_success_rate = (
                        max(0.0, min(1.0, float(checks_rate)))
                        if checks_rate is not None
                        else (
                            max(0.0, min(1.0, 1 - float(errors)))
                            if errors is not None
                            else 1.0
                        )
                    )
                    failed_requests = round(
                        endpoint_total_requests * (1 - resolved_success_rate)
                    )
                    summary["success_rate"] = resolved_success_rate
                    summary["error_rate"] = max(
                        0.0, min(1.0, 1 - resolved_success_rate)
                    )
                    summary["failed_requests"] = failed_requests
                    summary["successful_requests"] = max(
                        0, endpoint_total_requests - failed_requests
                    )

        root_group = data.get("root_group")
        check_rows = (
            _collect_k6_check_rows(root_group) if isinstance(root_group, dict) else []
        )
        if check_rows:
            summary["checks"] = check_rows

        state.k6_summary = {
            key: value for key, value in summary.items() if value is not None
        }
        if not state.k6_summary:
            state.append_log("WARN", f"k6_summary_empty path={summary_path}")
            return
        if rps is not None:
            state.rps = float(rps)
        if p95 is not None:
            state.rt_p95_ms = float(p95)
        state.append_metrics(state.rps, state.rt_p95_ms)
    except Exception as exc:
        state.append_log(
            "WARN", f"k6_summary_parse_failed path={summary_path} err={exc}"
        )
        return


def _archive_artifacts(token: str, state: RunState):
    """可选将日志/指标归档到 S3/MinIO。"""
    use_s3 = os.getenv("LOG_ARCHIVE_S3", os.getenv("USE_S3", "0")) == "1"
    bucket = os.getenv("S3_BUCKET") or settings.S3_BUCKET
    if not use_s3 or not bucket:
        return
    prefix = get_run_artifact_prefix()
    if state.log_path and state.log_path.exists():
        key = f"{prefix}/{token}.log"
        try:
            s3_utils.upload_bytes(
                bucket, key, state.log_path.read_bytes(), content_type="text/plain"
            )
            state.s3_log_uri = f"s3://{bucket}/{key}"
            state.append_log("INFO", f"log archived: {state.s3_log_uri}")
        except Exception as exc:  # pragma: no cover - 容错
            state.append_log("WARN", f"log archive failed: {exc}")
    if state.metrics_path and state.metrics_path.exists():
        key = f"{prefix}/{token}.metrics.gz"
        try:
            import gzip

            data = gzip.compress(state.metrics_path.read_bytes())
            s3_utils.upload_bytes(bucket, key, data, content_type="application/gzip")
            state.s3_metrics_uri = f"s3://{bucket}/{key}"
            state.append_log("INFO", f"metrics archived: {state.s3_metrics_uri}")
        except Exception as exc:  # pragma: no cover - 容错
            state.append_log("WARN", f"metrics archive failed: {exc}")
