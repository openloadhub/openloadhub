#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANDIDATE_ROOT = ROOT / ".tmp" / "opensource-export-candidate"
DEFAULT_JSON = ROOT / ".tmp" / "logs" / "opensource-demo-smoke-summary.json"
DEFAULT_MD = ROOT / ".tmp" / "logs" / "opensource-demo-smoke-summary.md"


SMOKE_ENV = {
    "MYSQL_PORT": "13306",
    "REDIS_PORT": "16379",
    "REDIS_EXPORTER_PORT": "19121",
    "MYSQLD_EXPORTER_PORT": "19104",
    "CADVISOR_PORT": "18098",
    "PUSHGATEWAY_PORT": "19091",
    "PROMETHEUS_PORT": "19090",
    "INFLUXDB_PORT": "18086",
    "GRAFANA_PORT": "13001",
    "PTP_ADMIN_PORT": "18000",
    "PTP_AGENT_PORT": "19096",
    "PTP_AGENT_2_PORT": "19097",
    "PTP_AGENT_3_PORT": "19098",
    "PTP_AGENT_4_PORT": "19099",
    "PTP_FRONTEND_PORT": "13000",
    "DEMO_TARGET_HTTP_PORT": "18088",
    "DEMO_TARGET_GRPC_PORT": "15051",
    "GRAFANA_PUBLIC_BASE_URL": "http://127.0.0.1:13001",
    "VITE_API_BASE": "http://127.0.0.1:18000",
}

DEMO_TASK_EXPECTATIONS = {
    "OpenLoadHub Demo - k6 HTTP+gRPC": "openloadhub-demo-k6-http-grpc",
    "OpenLoadHub Demo - JMeter HTTP+gRPC": "openloadhub-demo-jmeter-http-grpc",
}
DEMO_PLAN_EXPECTATIONS = {
    "OpenLoadHub Demo Plan - JMeter Simple": {
        "slug": "openloadhub-demo-plan-jmeter-simple",
        "task_count": 1,
        "pod_count": 4,
    },
    "OpenLoadHub Demo Plan - k6 + JMeter Advanced": {
        "slug": "openloadhub-demo-plan-k6-jmeter-advanced",
        "task_count": 2,
        "pod_count": 2,
    },
}
DEMO_TASK_DEFAULTS = {
    "duration": 20,
    "pod_count": 1,
}
DEMO_PLAN_DEFAULTS = {
    "pod_count": 4,
    "total_round": 2,
}
DEMO_TASK_THREAD_COUNT_DEFAULTS = {
    "OpenLoadHub Demo - k6 HTTP+gRPC": 10,
    "OpenLoadHub Demo - JMeter HTTP+gRPC": 1,
}
DEMO_TASK_REQUIRED_VARIABLES = {
    "BASE_URL",
    "GRPC_HOST",
    "target_tps",
}


@dataclass(frozen=True)
class Probe:
    name: str
    url: str
    method: str = "GET"
    json_body: dict[str, Any] | None = None
    expected_status: int = 200
    expected_json_status: str | None = None
    expected_json_values: dict[str, str] | None = None
    expected_text: str | None = None


@dataclass(frozen=True)
class SmokeUrls:
    api_base: str
    frontend_base: str
    grafana_base: str
    prometheus_base: str
    demo_target_base: str
    agent_bases: tuple[str, str, str, str]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return ROOT / candidate


def _compose_cmd(candidate_root: Path, project_name: str, *args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-f",
        str(candidate_root / "docker-compose.demo.yml"),
        "--project-directory",
        str(candidate_root),
        "--project-name",
        project_name,
        *args,
    ]


def _build_smoke_env() -> dict[str, str]:
    env = {
        key: os.environ.get(key, default)
        for key, default in SMOKE_ENV.items()
        if key not in {"GRAFANA_PUBLIC_BASE_URL", "VITE_API_BASE"}
    }
    env["GRAFANA_PUBLIC_BASE_URL"] = os.environ.get(
        "GRAFANA_PUBLIC_BASE_URL",
        f"http://127.0.0.1:{env['GRAFANA_PORT']}",
    )
    env["VITE_API_BASE"] = os.environ.get(
        "VITE_API_BASE",
        f"http://127.0.0.1:{env['PTP_ADMIN_PORT']}",
    )
    env["COMPOSE_PARALLEL_LIMIT"] = os.environ.get("COMPOSE_PARALLEL_LIMIT", "1")
    return env


def _local_url(port: str, path: str = "") -> str:
    if path and not path.startswith("/"):
        path = f"/{path}"
    return f"http://127.0.0.1:{port}{path}"


def _build_smoke_urls(env: dict[str, str]) -> SmokeUrls:
    return SmokeUrls(
        api_base=_local_url(env["PTP_ADMIN_PORT"]),
        frontend_base=_local_url(env["PTP_FRONTEND_PORT"]),
        grafana_base=_local_url(env["GRAFANA_PORT"]),
        prometheus_base=_local_url(env["PROMETHEUS_PORT"]),
        demo_target_base=_local_url(env["DEMO_TARGET_HTTP_PORT"]),
        agent_bases=(
            _local_url(env["PTP_AGENT_PORT"]),
            _local_url(env["PTP_AGENT_2_PORT"]),
            _local_url(env["PTP_AGENT_3_PORT"]),
            _local_url(env["PTP_AGENT_4_PORT"]),
        ),
    )


def _run(
    cmd: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        cwd=str(cwd),
        env={**os.environ, **env},
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _probe_once(probe: Probe, timeout_seconds: float) -> tuple[bool, dict[str, Any]]:
    body = None
    headers = {"User-Agent": "ptp-opensource-smoke"}
    if probe.json_body is not None:
        body = json.dumps(probe.json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(probe.url, data=body, headers=headers, method=probe.method)
    started = time.monotonic()
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
            status = response.status
            body = response.read(256 * 1024).decode("utf-8", errors="replace")
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            ok = status == probe.expected_status
            detail: dict[str, Any] = {
                "name": probe.name,
                "url": probe.url,
                "status_code": status,
                "elapsed_ms": elapsed_ms,
                "ok": ok,
            }
            if probe.expected_json_status is not None:
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    detail["error"] = f"json_decode_failed: {exc}"
                    detail["ok"] = False
                    return False, detail
                actual = str(payload.get("status") or "")
                detail["json_status"] = actual
                ok = ok and actual == probe.expected_json_status
                detail["ok"] = ok
            if probe.expected_json_values is not None:
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    detail["error"] = f"json_decode_failed: {exc}"
                    detail["ok"] = False
                    return False, detail
                mismatches = {}
                for path, expected in probe.expected_json_values.items():
                    actual_value: Any = payload
                    for segment in path.split("."):
                        if not isinstance(actual_value, dict):
                            actual_value = None
                            break
                        actual_value = actual_value.get(segment)
                    if str(actual_value) != expected:
                        mismatches[path] = {"expected": expected, "actual": actual_value}
                detail["json_value_mismatches"] = mismatches
                ok = ok and not mismatches
                detail["ok"] = ok
            if probe.expected_text is not None:
                contains = probe.expected_text in body
                detail["contains_expected_text"] = contains
                ok = ok and contains
                detail["ok"] = ok
            return ok, detail
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        return False, {
            "name": probe.name,
            "url": probe.url,
            "ok": False,
            "error": str(exc),
        }


def _wait_for_probe(probe: Probe, *, deadline: float, interval: float) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        ok, detail = _probe_once(probe, timeout_seconds=min(interval, 5.0))
        attempts.append(detail)
        if ok:
            detail["attempt_count"] = len(attempts)
            return detail
        time.sleep(interval)
    last = attempts[-1] if attempts else {"name": probe.name, "url": probe.url}
    last = dict(last)
    last["ok"] = False
    last["attempt_count"] = len(attempts)
    last["timed_out"] = True
    return last


def _request_json(
    url: str,
    *,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
    token: str | None = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    body = None
    headers = {"User-Agent": "ptp-opensource-smoke"}
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
        raw = response.read(1024 * 1024).decode("utf-8", errors="replace")
    payload = json.loads(raw) if raw else {}
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object from {url}")
    return payload


def _unwrap_api_data(payload: dict[str, Any]) -> Any:
    if "code" not in payload:
        return payload
    if payload.get("code") != 0:
        raise ValueError(
            f"api_error code={payload.get('code')} message={payload.get('message')}"
        )
    return payload.get("data")


def _remaining_timeout(deadline: float, *, cap_seconds: float, context: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 1.0:
        raise TimeoutError(f"overall demo smoke deadline expired before {context}")
    return min(cap_seconds, remaining)


def _wait_for_demo_tasks(
    *,
    api_base: str,
    deadline: float,
    interval: float,
) -> dict[str, Any]:
    url = f"{api_base}/api/v1/tasks"
    result: dict[str, Any] = {
        "name": "ptp-demo-seed-tasks",
        "url": url,
        "ok": False,
        "expected_tasks": sorted(DEMO_TASK_EXPECTATIONS),
    }
    attempt_count = 0
    last_error = None
    while time.monotonic() < deadline:
        attempt_count += 1
        try:
            login_payload = _request_json(
                f"{api_base}/api/v1/auth/login",
                method="POST",
                json_body={"username": "demo_tester", "password": "ptp_demo_tester"},
            )
            token = str(login_payload.get("access_token") or "")
            if not token:
                raise ValueError("login response missing access_token")

            found: dict[str, dict[str, Any]] = {}
            for task_name, slug in DEMO_TASK_EXPECTATIONS.items():
                query = urlencode({"name": task_name, "pageSize": "100"})
                payload = _request_json(f"{url}?{query}", token=token)
                data = _unwrap_api_data(payload)
                items = data.get("items") if isinstance(data, dict) else []
                for item in items:
                    if not isinstance(item, dict) or item.get("name") != task_name:
                        continue
                    properties = item.get("properties")
                    if not isinstance(properties, dict):
                        continue
                    if properties.get("demo_seed_slug") != slug:
                        continue
                    task_id = item.get("id")
                    if not isinstance(task_id, int):
                        continue
                    detail = _unwrap_api_data(
                        _request_json(f"{url}/{task_id}", token=token)
                    )
                    if isinstance(detail, dict):
                        found[task_name] = detail
                        break

            missing = sorted(set(DEMO_TASK_EXPECTATIONS) - set(found))
            not_ready = {
                name: task.get("status")
                for name, task in found.items()
                if task.get("status") != "ready"
            }
            missing_proto = [
                name
                for name, task in found.items()
                if not task.get("proto_assets")
            ]
            missing_data = [
                name
                for name, task in found.items()
                if not task.get("data_assets")
            ]
            default_mismatches: dict[str, dict[str, Any]] = {}
            missing_variables: dict[str, list[str]] = {}
            for name, task in found.items():
                properties = task.get("properties")
                properties = properties if isinstance(properties, dict) else {}
                mismatches: dict[str, Any] = {}
                expected_thread_count = DEMO_TASK_THREAD_COUNT_DEFAULTS.get(name)
                if expected_thread_count is not None:
                    actual_thread_count = task.get("thread_count")
                    if actual_thread_count != expected_thread_count:
                        mismatches["thread_count"] = {
                            "expected": expected_thread_count,
                            "actual": actual_thread_count,
                        }
                for key, expected in DEMO_TASK_DEFAULTS.items():
                    if key == "duration":
                        actual = task.get(key)
                    else:
                        actual = properties.get(key)
                    if actual != expected:
                        mismatches[key] = {"expected": expected, "actual": actual}
                variables = properties.get("variables")
                variable_keys = set(variables) if isinstance(variables, dict) else set()
                missing_for_task = sorted(DEMO_TASK_REQUIRED_VARIABLES - variable_keys)
                if mismatches:
                    default_mismatches[name] = mismatches
                if missing_for_task:
                    missing_variables[name] = missing_for_task
            result.update(
                {
                    "attempt_count": attempt_count,
                    "found_tasks": {
                        name: {
                            "id": task.get("id"),
                            "status": task.get("status"),
                            "proto_asset_count": len(task.get("proto_assets") or []),
                            "data_asset_count": len(task.get("data_assets") or []),
                        }
                        for name, task in found.items()
                    },
                    "missing_tasks": missing,
                    "not_ready": not_ready,
                    "missing_proto_assets": missing_proto,
                    "missing_data_assets": missing_data,
                    "default_mismatches": default_mismatches,
                    "missing_variables": missing_variables,
                }
            )
            if (
                not missing
                and not not_ready
                and not missing_proto
                and not missing_data
                and not default_mismatches
                and not missing_variables
            ):
                result["ok"] = True
                return result
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            result.update({"attempt_count": attempt_count, "error": last_error})
        time.sleep(interval)

    result["timed_out"] = True
    result["attempt_count"] = attempt_count
    if last_error:
        result["error"] = last_error
    return result


def _task_stage_items(stages: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not isinstance(stages, list):
        return items
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        for item in stage.get("items") or []:
            if isinstance(item, dict) and item.get("type") == "task":
                items.append(item)
    return items


def _wait_for_demo_plans(
    *,
    api_base: str,
    deadline: float,
    interval: float,
) -> dict[str, Any]:
    url = f"{api_base}/api/v1/plans"
    result: dict[str, Any] = {
        "name": "ptp-demo-seed-plans",
        "url": url,
        "ok": False,
        "expected_plans": sorted(DEMO_PLAN_EXPECTATIONS),
    }
    attempt_count = 0
    last_error = None
    while time.monotonic() < deadline:
        attempt_count += 1
        try:
            login_payload = _request_json(
                f"{api_base}/api/v1/auth/login",
                method="POST",
                json_body={"username": "demo_tester", "password": "ptp_demo_tester"},
            )
            token = str(login_payload.get("access_token") or "")
            if not token:
                raise ValueError("login response missing access_token")

            found: dict[str, dict[str, Any]] = {}
            for plan_name, expectation in DEMO_PLAN_EXPECTATIONS.items():
                query = urlencode({"name": plan_name, "pageSize": "100"})
                payload = _request_json(f"{url}?{query}", token=token)
                data = _unwrap_api_data(payload)
                items = data.get("items") if isinstance(data, dict) else []
                marker = f"demo_seed_slug={expectation['slug']}"
                for item in items:
                    if not isinstance(item, dict) or item.get("name") != plan_name:
                        continue
                    if marker not in str(item.get("description") or ""):
                        continue
                    plan_id = item.get("plan_id")
                    if not isinstance(plan_id, int):
                        continue
                    detail = _unwrap_api_data(
                        _request_json(f"{url}/{plan_id}", token=token)
                    )
                    if isinstance(detail, dict):
                        found[plan_name] = detail
                        break

            missing = sorted(set(DEMO_PLAN_EXPECTATIONS) - set(found))
            not_ready = {
                name: plan.get("status")
                for name, plan in found.items()
                if plan.get("status") != "ready"
            }
            default_mismatches: dict[str, dict[str, Any]] = {}
            for name, plan in found.items():
                expectation = DEMO_PLAN_EXPECTATIONS[name]
                mismatches: dict[str, Any] = {}
                if plan.get("total_round") != DEMO_PLAN_DEFAULTS["total_round"]:
                    mismatches["total_round"] = {
                        "expected": DEMO_PLAN_DEFAULTS["total_round"],
                        "actual": plan.get("total_round"),
                    }
                if plan.get("enable_round") is not True:
                    mismatches["enable_round"] = {
                        "expected": True,
                        "actual": plan.get("enable_round"),
                    }
                task_items = _task_stage_items(plan.get("stages"))
                if len(task_items) != expectation["task_count"]:
                    mismatches["task_count"] = {
                        "expected": expectation["task_count"],
                        "actual": len(task_items),
                    }
                pod_mismatches = []
                expected_pod_count = int(
                    expectation.get("pod_count") or DEMO_PLAN_DEFAULTS["pod_count"]
                )
                for index, item in enumerate(task_items):
                    run_params = item.get("run_params")
                    run_params = run_params if isinstance(run_params, dict) else {}
                    pod_count = run_params.get("pod_count")
                    pod_num = run_params.get("pod_num")
                    if (
                        pod_count != expected_pod_count
                        or pod_num != expected_pod_count
                    ):
                        pod_mismatches.append(
                            {
                                "item_index": index,
                                "expected": expected_pod_count,
                                "pod_count": pod_count,
                                "pod_num": pod_num,
                            }
                        )
                if pod_mismatches:
                    mismatches["plan_task_pods"] = pod_mismatches
                if mismatches:
                    default_mismatches[name] = mismatches

            result.update(
                {
                    "attempt_count": attempt_count,
                    "found_plans": {
                        name: {
                            "plan_id": plan.get("plan_id"),
                            "status": plan.get("status"),
                            "total_round": plan.get("total_round"),
                            "task_count": len(_task_stage_items(plan.get("stages"))),
                        }
                        for name, plan in found.items()
                    },
                    "missing_plans": missing,
                    "not_ready": not_ready,
                    "default_mismatches": default_mismatches,
                }
            )
            if not missing and not not_ready and not default_mismatches:
                result["ok"] = True
                return result
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            result.update({"attempt_count": attempt_count, "error": last_error})
        time.sleep(interval)

    result["timed_out"] = True
    result["attempt_count"] = attempt_count
    if last_error:
        result["error"] = last_error
    return result


def _read_demo_target_metrics(demo_target_base: str) -> dict[str, int]:
    request = Request(
        f"{demo_target_base}/metrics",
        headers={"User-Agent": "ptp-opensource-smoke"},
        method="GET",
    )
    with urlopen(request, timeout=5.0) as response:  # nosec B310
        text = response.read(128 * 1024).decode("utf-8", errors="replace")
    metrics: dict[str, int] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[0] in {
            "openloadhub_demo_target_http_requests_total",
            "openloadhub_demo_target_grpc_requests_total",
        }:
            try:
                metrics[parts[0]] = int(float(parts[1]))
            except ValueError:
                metrics[parts[0]] = -1
    return metrics


def _wait_for_run_terminal(
    *,
    api_base: str,
    run_id: int,
    token: str,
    deadline: float,
    interval: float,
) -> dict[str, Any]:
    url = f"{api_base}/api/v1/runs/{run_id}"
    attempts = 0
    last_detail: dict[str, Any] = {"run_id": run_id, "ok": False, "url": url}
    terminal_statuses = {"succeeded", "failed", "stopped"}
    while time.monotonic() < deadline:
        attempts += 1
        try:
            payload = _request_json(url, token=token, timeout_seconds=5.0)
        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            last_detail = {
                "run_id": run_id,
                "url": url,
                "run_status": "poll_error",
                "attempt_count": attempts,
                "ok": False,
                "last_error": str(exc),
            }
            time.sleep(interval)
            continue
        data = _unwrap_api_data(payload)
        if not isinstance(data, dict):
            raise ValueError(f"run_detail_not_object run_id={run_id}")
        status = str(data.get("run_status") or "")
        last_detail = {
            "run_id": run_id,
            "url": url,
            "run_status": status,
            "run_status_detail": data.get("run_status_detail"),
            "total_requests": data.get("total_requests"),
            "success_rate": data.get("success_rate"),
            "error_rate": data.get("error_rate"),
            "attempt_count": attempts,
            "ok": status == "succeeded",
        }
        if status in terminal_statuses:
            return last_detail
        time.sleep(interval)
    last_detail["timed_out"] = True
    last_detail["ok"] = False
    return last_detail


def _api_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [item for item in data["items"] if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _endpoint_name(item: dict[str, Any]) -> str:
    for key in ("endpoint_name", "endpoint", "name", "label", "target"):
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _non_overall_endpoint_names(items: list[dict[str, Any]]) -> list[str]:
    names = {
        name
        for item in items
        for name in [_endpoint_name(item)]
        if name and name.lower() not in {"overall", "total"}
    }
    return sorted(names)


def _run_contract_expectations(task_name: str) -> dict[str, int]:
    if "JMeter" in task_name:
        return {
            "min_summary_non_overall": 4,
            "min_checks": 4,
            "min_trend_non_overall": 4,
        }
    return {
        "min_summary_non_overall": 4,
        "min_checks": 8,
        "min_trend_non_overall": 4,
    }


def _fetch_run_contract(
    run_id: int,
    task_name: str,
    token: str,
    *,
    api_base: str,
) -> dict[str, Any]:
    detail = _unwrap_api_data(
        _request_json(
            f"{api_base}/api/v1/runs/{run_id}",
            token=token,
            timeout_seconds=10.0,
        )
    )
    summary = _api_items(
        _unwrap_api_data(
            _request_json(
                f"{api_base}/api/v1/runs/{run_id}/summary-metrics",
                token=token,
                timeout_seconds=10.0,
            )
        )
    )
    checks = _api_items(
        _unwrap_api_data(
            _request_json(
                f"{api_base}/api/v1/runs/{run_id}/checks",
                token=token,
                timeout_seconds=10.0,
            )
        )
    )
    trends = _api_items(
        _unwrap_api_data(
            _request_json(
                f"{api_base}/api/v1/runs/"
                f"{run_id}/endpoint-trends?{urlencode({'metric': 'throughput', 'step_seconds': '10'})}",
                token=token,
                timeout_seconds=10.0,
            )
        )
    )
    logs = _api_items(
        _unwrap_api_data(
            _request_json(
                f"{api_base}/api/v1/runs/"
                f"{run_id}/logs?{urlencode({'limit': '20', 'order': 'desc'})}",
                token=token,
                timeout_seconds=10.0,
            )
        )
    )
    dashboards = _unwrap_api_data(
        _request_json(
            f"{api_base}/api/v1/runs/{run_id}/dashboards",
            token=token,
            timeout_seconds=10.0,
        )
    )
    report_frontdoor = _unwrap_api_data(
        _request_json(
            f"{api_base}/api/v1/reports/frontdoor/run/{run_id}/ensure",
            method="POST",
            token=token,
            timeout_seconds=20.0,
        )
    )
    summary_names = _non_overall_endpoint_names(summary)
    trend_names = _non_overall_endpoint_names(trends)
    expected = _run_contract_expectations(task_name)
    checks_count = len(checks)
    params = detail.get("params") if isinstance(detail, dict) else {}
    params = params if isinstance(params, dict) else {}
    required_params = {"DATA_FILE", "GRPC_HOST", "duration", "pod_count", "target_tps"}
    missing_params = sorted(key for key in required_params if key not in params)
    total_requests = detail.get("total_requests") if isinstance(detail, dict) else None
    pod_total = detail.get("pod_total") if isinstance(detail, dict) else None
    pod_actual = detail.get("pod_actual") if isinstance(detail, dict) else None
    pod_completed = detail.get("pod_completed") if isinstance(detail, dict) else None
    core_metric_mismatches = {
        key: value
        for key, value in {
            "total_requests": total_requests,
            "success_rate": detail.get("success_rate") if isinstance(detail, dict) else None,
            "error_rate": detail.get("error_rate") if isinstance(detail, dict) else None,
            "avg_rt_ms": detail.get("avg_rt_ms") if isinstance(detail, dict) else None,
            "p95_rt_ms": detail.get("p95_rt_ms") if isinstance(detail, dict) else None,
            "rps": detail.get("rps") if isinstance(detail, dict) else None,
        }.items()
        if value is None
    }
    expected_pod_count = int(DEMO_TASK_DEFAULTS["pod_count"])
    pod_mismatch = {
        "pod_total": pod_total,
        "pod_actual": pod_actual,
        "pod_completed": pod_completed,
    } if (pod_total, pod_actual, pod_completed) != (
        expected_pod_count,
        expected_pod_count,
        expected_pod_count,
    ) else {}
    report_ready = (
        isinstance(report_frontdoor, dict)
        and report_frontdoor.get("status") in {"ready", "template_fallback"}
        and isinstance(report_frontdoor.get("report_id"), int)
    )
    dashboard_items = _api_items(dashboards)
    dashboard_summary = (
        dashboards.get("summary")
        if isinstance(dashboards, dict) and isinstance(dashboards.get("summary"), dict)
        else {}
    )
    dashboard_types = {
        str(item.get("dashboard_type") or "")
        for item in dashboard_items
        if isinstance(item, dict)
    }
    has_engine_dashboard = bool(dashboard_summary.get("has_engine_grafana")) or (
        "engine_grafana" in dashboard_types
    )
    has_pod_dashboard = bool(dashboard_summary.get("has_pod_grafana")) or (
        "pod_grafana" in dashboard_types
    )
    failed_checks = [
        item
        for item in checks
        if float(item.get("success_rate") or 0.0) < 1.0
    ]
    ok = (
        len(summary_names) >= expected["min_summary_non_overall"]
        and checks_count >= expected["min_checks"]
        and len(trend_names) >= expected["min_trend_non_overall"]
        and not missing_params
        and not core_metric_mismatches
        and not pod_mismatch
        and len(logs) > 0
        and has_engine_dashboard
        and has_pod_dashboard
        and report_ready
        and not failed_checks
    )
    return {
        "run_status": detail.get("run_status") if isinstance(detail, dict) else None,
        "total_requests": total_requests,
        "success_rate": detail.get("success_rate") if isinstance(detail, dict) else None,
        "error_rate": detail.get("error_rate") if isinstance(detail, dict) else None,
        "avg_rt_ms": detail.get("avg_rt_ms") if isinstance(detail, dict) else None,
        "p95_rt_ms": detail.get("p95_rt_ms") if isinstance(detail, dict) else None,
        "rps": detail.get("rps") if isinstance(detail, dict) else None,
        "pod_total": pod_total,
        "pod_actual": pod_actual,
        "pod_completed": pod_completed,
        "required_param_values": {
            key: params.get(key)
            for key in sorted(required_params)
            if key in params
        },
        "missing_required_params": missing_params,
        "core_metric_mismatches": core_metric_mismatches,
        "pod_mismatch": pod_mismatch,
        "report_frontdoor": report_frontdoor if isinstance(report_frontdoor, dict) else None,
        "report_ready": report_ready,
        "summary_metric_count": len(summary),
        "summary_non_overall_count": len(summary_names),
        "summary_endpoint_names": summary_names,
        "check_count": checks_count,
        "trend_count": len(trends),
        "trend_non_overall_count": len(trend_names),
        "trend_endpoint_names": trend_names,
        "log_count": len(logs),
        "sample_logs": logs[:5],
        "dashboard_count": len(dashboard_items),
        "dashboard_types": sorted(dashboard_types),
        "dashboard_summary": dashboard_summary,
        "has_engine_dashboard": has_engine_dashboard,
        "has_pod_dashboard": has_pod_dashboard,
        "failed_checks": failed_checks,
        "expectations": expected,
        "sample_summary_metrics": summary[:5],
        "sample_checks": checks[:5],
        "sample_endpoint_trends": trends[:5],
        "ok": ok,
    }


def _extract_agent_hosts(run_detail: dict[str, Any]) -> list[str]:
    params = run_detail.get("params")
    if not isinstance(params, dict):
        return []
    hosts: set[str] = set()
    for key in ("agent_host", "agent_alias", "agent"):
        value = params.get(key)
        if value:
            hosts.add(str(value))
    for item in params.get("agent_runs") or []:
        if not isinstance(item, dict):
            continue
        for key in ("agent_host", "agent_alias", "agent"):
            value = item.get(key)
            if value:
                hosts.add(str(value))
    return sorted(hosts)


def _exercise_demo_runs(
    demo_task_probe: dict[str, Any],
    *,
    api_base: str,
    demo_target_base: str,
    deadline: float,
    interval: float,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": "ptp-demo-seed-runs",
        "url": f"{api_base}/api/v1/runs",
        "ok": False,
        "runs": [],
    }
    found_tasks = demo_task_probe.get("found_tasks")
    if not isinstance(found_tasks, dict) or not found_tasks:
        result["error"] = "demo task probe did not expose found_tasks"
        return result

    try:
        login_payload = _request_json(
            f"{api_base}/api/v1/auth/login",
            method="POST",
            json_body={"username": "demo_tester", "password": "ptp_demo_tester"},
        )
        token = str(login_payload.get("access_token") or "")
        if not token:
            raise ValueError("login response missing access_token")

        metrics_before = _read_demo_target_metrics(demo_target_base)
        run_results: list[dict[str, Any]] = []
        for task_name in sorted(DEMO_TASK_EXPECTATIONS):
            result["current_task_name"] = task_name
            result["current_action"] = "create_run"
            task_summary = found_tasks.get(task_name)
            if not isinstance(task_summary, dict) or not isinstance(task_summary.get("id"), int):
                raise ValueError(f"missing seeded task id for {task_name}")
            task_id = int(task_summary["id"])
            create_started = time.monotonic()
            create_payload = _request_json(
                f"{api_base}/api/v1/runs",
                method="POST",
                token=token,
                json_body={
                    "task_id": task_id,
                    "params": {
                        "duration": 8,
                        "target_tps": 4,
                        "thread_count": 1,
                        "pod_count": DEMO_TASK_DEFAULTS["pod_count"],
                        "smoke_run": True,
                    },
                },
                timeout_seconds=_remaining_timeout(
                    deadline,
                    cap_seconds=60.0,
                    context=f"create run for {task_name}",
                ),
            )
            create_elapsed_ms = round((time.monotonic() - create_started) * 1000, 1)
            created = _unwrap_api_data(create_payload)
            if not isinstance(created, dict) or not isinstance(created.get("run_id"), int):
                raise ValueError(f"run_create_missing_id task={task_name} response={created}")
            run_id = int(created["run_id"])
            result["current_action"] = "wait_run_terminal"
            terminal = _wait_for_run_terminal(
                api_base=api_base,
                run_id=run_id,
                token=token,
                deadline=deadline,
                interval=interval,
            )
            terminal["task_name"] = task_name
            terminal["task_id"] = task_id
            terminal["create_elapsed_ms"] = create_elapsed_ms
            run_detail = _unwrap_api_data(
                _request_json(
                    f"{api_base}/api/v1/runs/{run_id}",
                    token=token,
                    timeout_seconds=10.0,
                )
            )
            terminal["agent_hosts"] = (
                _extract_agent_hosts(run_detail) if isinstance(run_detail, dict) else []
            )
            terminal["contract"] = _fetch_run_contract(
                run_id,
                task_name,
                token,
                api_base=api_base,
            )
            terminal["ok"] = terminal.get("ok") and terminal["contract"].get("ok")
            run_results.append(terminal)
            if terminal.get("run_status") != "succeeded":
                result.update(
                    {
                        "runs": run_results,
                        "metrics_before": metrics_before,
                        "error": f"run did not succeed: {task_name}",
                    }
                )
                return result
            if not terminal["contract"].get("ok"):
                result.update(
                    {
                        "runs": run_results,
                        "metrics_before": metrics_before,
                        "error": f"run contract check failed: {task_name}",
                    }
                )
                return result

        metrics_after = _read_demo_target_metrics(demo_target_base)
        http_delta = (
            metrics_after.get("openloadhub_demo_target_http_requests_total", 0)
            - metrics_before.get("openloadhub_demo_target_http_requests_total", 0)
        )
        grpc_delta = (
            metrics_after.get("openloadhub_demo_target_grpc_requests_total", 0)
            - metrics_before.get("openloadhub_demo_target_grpc_requests_total", 0)
        )
        result.update(
            {
                "runs": run_results,
                "metrics_before": metrics_before,
                "metrics_after": metrics_after,
                "metric_deltas": {"http_requests": http_delta, "grpc_requests": grpc_delta},
                "agent_hosts_used": sorted(
                    {
                        host
                        for run in run_results
                        for host in run.get("agent_hosts", [])
                    }
                ),
                "ok": (
                    http_delta > 0
                    and grpc_delta > 0
                    and len(
                        {
                            host
                            for run in run_results
                            for host in run.get("agent_hosts", [])
                        }
                    )
                    >= DEMO_TASK_DEFAULTS["pod_count"]
                ),
            }
        )
        if not result["ok"]:
            result["error"] = "demo target HTTP/gRPC counters or four-agent dispatch proof missing"
        result.pop("current_task_name", None)
        result.pop("current_action", None)
        return result
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        result["error"] = str(exc)
        return result


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Open Source Demo Smoke Summary",
        "",
        f"- status: `{payload['status']}`",
        f"- checked_at: `{payload['checked_at']}`",
        f"- candidate_root: `{payload['candidate_root']}`",
        f"- project_name: `{payload['project_name']}`",
        f"- build: `{payload['build']}`",
        f"- keep_running: `{payload['keep_running']}`",
        "",
        "## Probes",
        "",
        "| Probe | URL | OK | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for probe in payload["probes"]:
        detail = probe.get("error") or probe.get("json_status") or probe.get("status_code") or ""
        lines.append(
            f"| `{probe['name']}` | `{probe['url']}` | `{probe.get('ok')}` | `{detail}` |"
        )
    if payload.get("failed_step"):
        lines.extend(["", "## Failed Step", "", f"- `{payload['failed_step']}`"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a focused smoke check against the clean open-source demo export."
    )
    parser.add_argument("--candidate-root", default=str(DEFAULT_CANDIDATE_ROOT))
    parser.add_argument("--project-name", default="ptp-opensource-smoke")
    parser.add_argument("--json-output", default=str(DEFAULT_JSON))
    parser.add_argument("--md-output", default=str(DEFAULT_MD))
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--interval-seconds", type=float, default=3.0)
    parser.add_argument("--build", action="store_true", help="Build images before starting.")
    parser.add_argument(
        "--skip-cleanup-before",
        action="store_true",
        help=(
            "Do not run compose down -v before starting. By default the smoke removes "
            "only the named demo project's volumes for a deterministic first run."
        ),
    )
    parser.add_argument(
        "--skip-run-exercise",
        action="store_true",
        help="Only verify seeded demo tasks and plans, without creating demo runs.",
    )
    parser.add_argument("--keep-running", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    candidate_root = _resolve(args.candidate_root)
    json_output = _resolve(args.json_output)
    md_output = _resolve(args.md_output)
    env = _build_smoke_env()
    urls = _build_smoke_urls(env)
    payload: dict[str, Any] = {
        "status": "failed",
        "exit_code": 1,
        "checked_at": _utc_now(),
        "candidate_root": str(candidate_root),
        "project_name": args.project_name,
        "build": bool(args.build),
        "keep_running": bool(args.keep_running),
        "env_overrides": env,
        "probes": [],
        "failed_step": None,
        "commands": [],
    }

    def record_command(step: str, proc: subprocess.CompletedProcess[str]) -> None:
        payload["commands"].append(
            {
                "step": step,
                "returncode": proc.returncode,
                "stdout_tail": proc.stdout[-4000:],
                "stderr_tail": proc.stderr[-4000:],
            }
        )

    try:
        if not (candidate_root / "docker-compose.demo.yml").exists():
            payload["failed_step"] = "missing_compose_file"
            return 2

        if not args.skip_cleanup_before:
            cleanup_before = _run(
                _compose_cmd(candidate_root, args.project_name, "down", "-v", "--remove-orphans"),
                cwd=candidate_root,
                env=env,
                timeout=120,
            )
            record_command("compose_cleanup_before", cleanup_before)
        else:
            payload["commands"].append(
                {
                    "step": "compose_cleanup_before",
                    "returncode": 0,
                    "stdout_tail": "skipped by --skip-cleanup-before",
                    "stderr_tail": "",
                }
            )

        config = _run(
            _compose_cmd(candidate_root, args.project_name, "config"),
            cwd=candidate_root,
            env=env,
            timeout=60,
        )
        record_command("compose_config", config)
        if config.returncode != 0:
            payload["failed_step"] = "compose_config"
            return 3

        up_args = ["up", "-d"]
        if args.build:
            up_args.append("--build")
        up = _run(
            _compose_cmd(candidate_root, args.project_name, *up_args),
            cwd=candidate_root,
            env=env,
            timeout=max(args.timeout_seconds, 60),
        )
        record_command("compose_up", up)
        if up.returncode != 0:
            payload["failed_step"] = "compose_up"
            return 4

        probes = [
            Probe("ptp-admin-health", f"{urls.api_base}/health", expected_json_status="ok"),
            Probe(
                "demo-target-health",
                f"{urls.demo_target_base}/health",
                expected_json_status="ok",
                expected_json_values={"service": "openloadhub-demo-target"},
            ),
            Probe(
                "ptp-admin-login",
                f"{urls.api_base}/api/v1/auth/login",
                method="POST",
                json_body={"username": "admin", "password": "ptp_demo_admin"},
                expected_json_values={
                    "token_type": "bearer",
                    "user.username": "admin",
                    "user.role": "ADMIN",
                },
            ),
            Probe(
                "ptp-demo-tester-login",
                f"{urls.api_base}/api/v1/auth/login",
                method="POST",
                json_body={"username": "demo_tester", "password": "ptp_demo_tester"},
                expected_json_values={
                    "token_type": "bearer",
                    "user.username": "demo_tester",
                    "user.role": "TESTER",
                },
            ),
            Probe("ptp-agent-health", f"{urls.agent_bases[0]}/health", expected_json_status="ok"),
            Probe("ptp-agent-2-health", f"{urls.agent_bases[1]}/health", expected_json_status="ok"),
            Probe("ptp-agent-3-health", f"{urls.agent_bases[2]}/health", expected_json_status="ok"),
            Probe("ptp-agent-4-health", f"{urls.agent_bases[3]}/health", expected_json_status="ok"),
            Probe("frontend-index", f"{urls.frontend_base}/", expected_text="<html"),
            Probe("prometheus-ready", f"{urls.prometheus_base}/-/ready", expected_status=200),
            Probe("grafana-login", f"{urls.grafana_base}/login", expected_status=200),
        ]
        deadline = time.monotonic() + args.timeout_seconds
        probe_results = [
            _wait_for_probe(probe, deadline=deadline, interval=args.interval_seconds)
            for probe in probes
        ]
        payload["probes"] = probe_results
        if not all(item.get("ok") for item in probe_results):
            payload["failed_step"] = "http_probes"
            return 5

        seed = _run(
            _compose_cmd(
                candidate_root,
                args.project_name,
                "run",
                "--rm",
                "--no-deps",
                "ptp-demo-seed",
            ),
            cwd=candidate_root,
            env=env,
            timeout=240,
        )
        record_command("demo_seed", seed)
        if seed.returncode != 0:
            payload["failed_step"] = "demo_seed"
            return 6

        demo_task_probe = _wait_for_demo_tasks(
            api_base=urls.api_base,
            deadline=deadline,
            interval=args.interval_seconds,
        )
        payload["probes"].append(demo_task_probe)
        if not demo_task_probe.get("ok"):
            payload["failed_step"] = "demo_task_seed"
            return 7

        demo_plan_probe = _wait_for_demo_plans(
            api_base=urls.api_base,
            deadline=deadline,
            interval=args.interval_seconds,
        )
        payload["probes"].append(demo_plan_probe)
        if not demo_plan_probe.get("ok"):
            payload["failed_step"] = "demo_plan_seed"
            return 7

        if not args.skip_run_exercise:
            demo_run_probe = _exercise_demo_runs(
                demo_task_probe,
                api_base=urls.api_base,
                demo_target_base=urls.demo_target_base,
                deadline=deadline,
                interval=args.interval_seconds,
            )
            payload["probes"].append(demo_run_probe)
            if not demo_run_probe.get("ok"):
                payload["failed_step"] = "demo_run_exercise"
                return 8

        payload["status"] = "passed"
        payload["exit_code"] = 0
        return 0
    finally:
        if not args.keep_running:
            down = _run(
                _compose_cmd(candidate_root, args.project_name, "down", "-v", "--remove-orphans"),
                cwd=candidate_root,
                env=env,
                timeout=120,
            )
            record_command("compose_down", down)
        _write_json(json_output, payload)
        _write_markdown(md_output, payload)
        print(f"summary_json={json_output}")
        print(f"summary_md={md_output}")
        print(f"status={payload['status']}")
        if payload.get("failed_step"):
            print(f"failed_step={payload['failed_step']}")


if __name__ == "__main__":
    raise SystemExit(main())
