from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

from app.models.run import Run
from app.services.run_service import RunService
from common.config.settings import settings

logger = logging.getLogger(__name__)

REPORT_TIMEZONE_NAME = "Asia/Shanghai"
REPORT_TIMEZONE = ZoneInfo(REPORT_TIMEZONE_NAME)


class ReportObservabilityAnalysisService:
    """Deterministic observability analysis used by HTML reports.

    This service does not call AI providers. It only consumes dashboard frontdoors,
    run windows and Prometheus machine metrics.
    """

    def __init__(self, db):
        self.db = db

    def build_for_run(self, run: Optional[Run]) -> dict[str, Any]:
        if run is None:
            return self._empty("当前报告未绑定 Run，无法分析关联监控。")

        dashboards = RunService(self.db).get_dashboards(int(run.run_id))
        observability = {
            "items": [
                {
                    "run_id": int(run.run_id),
                    "task_name": run.task_name,
                    "summary": dashboards.summary.model_dump(mode="json"),
                    "dashboards": [
                        item.model_dump(mode="json") for item in dashboards.items or []
                    ],
                }
            ],
            "summary": dashboards.summary.model_dump(mode="json"),
        }
        window = self._window_from_run(run)
        return self._build(observability=observability, window=window)

    def build_for_mixed(
        self, records: list[Any], observability: dict[str, Any]
    ) -> dict[str, Any]:
        return self._build(
            observability=observability,
            window=self._window_from_records(records),
        )

    def _build(
        self,
        *,
        observability: dict[str, Any],
        window: Optional[tuple[datetime, datetime]],
    ) -> dict[str, Any]:
        categories = self._categories(observability)
        domains: list[dict[str, Any]] = []
        limitations: list[str] = []

        if not any(categories.values()):
            return self._empty("当前未发现业务大盘、MySQL、Redis 或拓扑入口。")
        if window is None:
            return self._empty(
                "已发现业务大盘、MySQL、Redis 或拓扑入口，但缺少可查询的压测时间窗。"
            )

        start, end = window
        step_seconds = max(10, min(60, int((end - start).total_seconds() // 120) or 10))
        if categories["service"]:
            domains.append(self._service_target_domain(start, end, step_seconds))
        if categories["mysql"]:
            domains.append(self._mysql_domain(start, end, step_seconds))
        if categories["redis"]:
            domains.append(self._redis_domain(start, end, step_seconds))
        if categories["topology"]:
            domains.append(
                {
                    "domain": "topology",
                    "title": "链路拓扑 / Trace",
                    "status": "frontdoor_only",
                    "severity": "info",
                    "conclusion": (
                        "链路拓扑入口已关联；当前仅记录 Trace/SkyWalking 前门，"
                        "未读取 SkyWalking trace span 或调用图机器数据。"
                    ),
                    "metrics": {},
                }
            )
            limitations.append(
                "拓扑入口已关联，但本轮未接入 SkyWalking/Trace 机器可读 span 统计。"
            )

        findings = [
            str(domain.get("conclusion"))
            for domain in domains
            if str(domain.get("conclusion") or "").strip()
        ]
        has_machine_metrics = any(
            domain.get("status") in {"pass", "risk"} for domain in domains
        )
        if not has_machine_metrics and any(
            categories[key] for key in ("service", "mysql", "redis")
        ):
            limitations.append(
                "已关联外部监控入口，但 Prometheus 在本轮时间窗未返回对应指标样本。"
            )

        return {
            "summary": (
                self._format_summary(findings[:4])
                if findings
                else "已发现观测入口，但暂无可形成结论的机器指标。"
            ),
            "domains": domains,
            "findings": findings,
            "limitations": limitations,
            "window": {
                "started_at": self._dt(start),
                "ended_at": self._dt(end),
                "step_seconds": step_seconds,
            },
            "has_machine_metrics": has_machine_metrics,
        }

    def _service_target_domain(
        self, start: datetime, end: datetime, step_seconds: int
    ) -> dict[str, Any]:
        metrics = {
            "cpu_peak_percent": self._prom_stat(
                'target_service_process_cpu_percent{job="demo-target"}',
                start,
                end,
                step_seconds,
            ),
            "memory_peak_percent": self._prom_stat(
                '100 * target_service_process_resident_memory_bytes{job="demo-target"} / clamp_min(target_service_runtime_memory_total_bytes{job="demo-target"}, 1)',
                start,
                end,
                step_seconds,
            ),
            "http_p99_peak_ms": self._prom_stat(
                '1000 * histogram_quantile(0.99, sum by (le) (rate(target_service_http_request_duration_seconds_bucket{job="demo-target"}[1m])))',
                start,
                end,
                step_seconds,
            ),
            "grpc_p99_peak_ms": self._prom_stat(
                '1000 * histogram_quantile(0.99, sum by (le) (rate(target_service_grpc_request_duration_seconds_bucket{job="demo-target"}[1m])))',
                start,
                end,
                step_seconds,
            ),
            "http_error_tps_peak": self._prom_stat(
                'sum(rate(target_service_http_requests_total{job="demo-target",status!~"2.."}[1m]))',
                start,
                end,
                step_seconds,
            ),
            "grpc_error_tps_peak": self._prom_stat(
                'sum(rate(target_service_grpc_requests_total{job="demo-target",status!="ok"}[1m]))',
                start,
                end,
                step_seconds,
            ),
        }
        return self._finalize_domain(
            domain="service",
            title="被测服务",
            metrics=metrics,
            warn_rules=[
                ("cpu_peak_percent", 85, "被测服务 CPU 峰值达到 %.1f%%"),
                ("memory_peak_percent", 85, "被测服务内存峰值达到 %.1f%%"),
                ("http_p99_peak_ms", 200, "HTTP P99 峰值达到 %.1fms"),
                ("grpc_p99_peak_ms", 50, "gRPC P99 峰值达到 %.1fms"),
                ("http_error_tps_peak", 0, "HTTP 非 2xx TPS 峰值 %.4f"),
                ("grpc_error_tps_peak", 0, "gRPC 非 OK TPS 峰值 %.4f"),
            ],
            pass_text="被测服务指标未显示明显 CPU/内存、P99 或错误瓶颈。",
        )

    def _mysql_domain(
        self, start: datetime, end: datetime, step_seconds: int
    ) -> dict[str, Any]:
        metrics = {
            "up": self._prom_stat('mysql_up{job="mysql"}', start, end, step_seconds),
            "connection_usage_peak_percent": self._prom_stat(
                '100 * mysql_global_status_threads_connected{job="mysql"} / clamp_min(mysql_global_variables_max_connections{job="mysql"}, 1)',
                start,
                end,
                step_seconds,
            ),
            "qps_peak": self._prom_stat(
                'sum(rate(mysql_global_status_questions{job="mysql"}[1m]))',
                start,
                end,
                step_seconds,
            ),
            "threads_running_peak": self._prom_stat(
                'mysql_global_status_threads_running{job="mysql"}',
                start,
                end,
                step_seconds,
            ),
            "slow_query_tps_peak": self._prom_stat(
                'sum(rate(mysql_global_status_slow_queries{job="mysql"}[1m]))',
                start,
                end,
                step_seconds,
            ),
            "lock_wait_tps_peak": self._prom_stat(
                'sum(rate(mysql_global_status_innodb_row_lock_waits{job="mysql"}[1m]))',
                start,
                end,
                step_seconds,
            ),
        }
        return self._finalize_domain(
            domain="mysql",
            title="MySQL",
            metrics=metrics,
            warn_rules=[
                (
                    "connection_usage_peak_percent",
                    80,
                    "MySQL 连接使用率峰值达到 %.1f%%",
                ),
                ("slow_query_tps_peak", 0, "MySQL 慢查询 TPS 峰值 %.4f"),
                ("lock_wait_tps_peak", 0, "MySQL 行锁等待 TPS 峰值 %.4f"),
            ],
            pass_text="MySQL 指标未显示明显资源瓶颈，连接、慢查询或锁等待未见瓶颈。",
        )

    def _redis_domain(
        self, start: datetime, end: datetime, step_seconds: int
    ) -> dict[str, Any]:
        metrics = {
            "up": self._prom_stat('redis_up{job="redis"}', start, end, step_seconds),
            "connected_clients_peak": self._prom_stat(
                'redis_connected_clients{job="redis"}', start, end, step_seconds
            ),
            "commands_qps_peak": self._prom_stat(
                'sum(rate(redis_commands_processed_total{job="redis"}[1m]))',
                start,
                end,
                step_seconds,
            ),
            "hit_rate_min_percent": self._prom_stat(
                '100 * sum(rate(redis_keyspace_hits_total{job="redis"}[1m])) / clamp_min(sum(rate(redis_keyspace_hits_total{job="redis"}[1m])) + sum(rate(redis_keyspace_misses_total{job="redis"}[1m])), 1)',
                start,
                end,
                step_seconds,
                reducer="min",
            ),
            "blocked_clients_peak": self._prom_stat(
                'redis_blocked_clients{job="redis"}', start, end, step_seconds
            ),
            "memory_used_peak_mb": self._prom_stat(
                'redis_memory_used_bytes{job="redis"} / 1024 / 1024',
                start,
                end,
                step_seconds,
            ),
        }
        return self._finalize_domain(
            domain="redis",
            title="Redis",
            metrics=metrics,
            warn_rules=[("blocked_clients_peak", 0, "Redis blocked clients 峰值 %.0f")],
            min_warn_rules=[("hit_rate_min_percent", 95, "Redis 命中率最低 %.1f%%")],
            pass_text="Redis 指标未显示明显资源瓶颈，阻塞客户端、命中率或内存未见异常。",
            risk_context_text="Redis 连接数与内存使用未显示明显资源瓶颈。",
        )

    def _prom_stat(
        self,
        query: str,
        start: datetime,
        end: datetime,
        step_seconds: int,
        *,
        reducer: str = "max",
    ) -> dict[str, Any]:
        results = self._query_prometheus_range(query, start, end, step_seconds)
        values: list[float] = []
        latest: Optional[float] = None
        latest_ts: Optional[float] = None
        for result in results:
            for raw_point in result.get("values") or []:
                if not raw_point or len(raw_point) < 2:
                    continue
                try:
                    ts = float(raw_point[0])
                    value = float(raw_point[1])
                except (TypeError, ValueError):
                    continue
                if value != value or value in (float("inf"), float("-inf")):
                    continue
                values.append(value)
                if latest_ts is None or ts >= latest_ts:
                    latest_ts = ts
                    latest = value
        if not values:
            return {"query": query, "samples": 0, "value": None, "reducer": reducer}
        if reducer == "min":
            selected = min(values)
        elif reducer == "avg":
            selected = sum(values) / len(values)
        else:
            selected = max(values)
        return {
            "query": query,
            "samples": len(values),
            "series": len(results),
            "value": round(selected, 4),
            "latest": round(latest, 4) if latest is not None else None,
            "reducer": reducer,
        }

    def _query_prometheus_range(
        self, query: str, start: datetime, end: datetime, step_seconds: int
    ) -> list[dict[str, Any]]:
        prom = settings.PROMETHEUS_URL
        if not prom:
            return []
        try:
            response = httpx.get(
                f"{prom.rstrip('/')}/api/v1/query_range",
                params={
                    "query": query,
                    "start": int(start.timestamp()),
                    "end": int(end.timestamp()),
                    "step": max(1, step_seconds),
                },
                timeout=5.0,
                trust_env=False,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "success":
                return []
            return payload.get("data", {}).get("result") or []
        except Exception as exc:  # noqa: BLE001
            logger.debug("report observability prometheus query failed: %s", exc)
            return []

    def _finalize_domain(
        self,
        *,
        domain: str,
        title: str,
        metrics: dict[str, dict[str, Any]],
        warn_rules: list[tuple[str, float, str]],
        pass_text: str,
        min_warn_rules: Optional[list[tuple[str, float, str]]] = None,
        risk_context_text: Optional[str] = None,
    ) -> dict[str, Any]:
        sampled = {
            key: value
            for key, value in metrics.items()
            if int(value.get("samples") or 0) > 0
        }
        if not sampled:
            return {
                "domain": domain,
                "title": title,
                "status": "no_data",
                "severity": "unknown",
                "conclusion": f"{title} 已关联，但 Prometheus 未返回本轮时间窗指标样本。",
                "metrics": metrics,
            }

        warnings: list[str] = []
        for key, threshold, template in warn_rules:
            value = self._num((sampled.get(key) or {}).get("value"))
            if value is not None and value > threshold:
                warnings.append(template % value)
        for key, threshold, template in min_warn_rules or []:
            value = self._num((sampled.get(key) or {}).get("value"))
            if value is not None and value < threshold:
                warnings.append(template % value)
        if warnings:
            conclusion_parts = []
            if risk_context_text:
                conclusion_parts.append(self._strip_sentence_end(risk_context_text))
            conclusion_parts.append(f"{title} 发现风险：" + "；".join(warnings[:3]))
            return {
                "domain": domain,
                "title": title,
                "status": "risk",
                "severity": "warning",
                "conclusion": self._format_summary(conclusion_parts),
                "metrics": metrics,
            }
        return {
            "domain": domain,
            "title": title,
            "status": "pass",
            "severity": "info",
            "conclusion": pass_text,
            "metrics": metrics,
        }

    @staticmethod
    def _categories(observability: dict[str, Any]) -> dict[str, bool]:
        categories = {
            "service": False,
            "mysql": False,
            "redis": False,
            "topology": False,
        }
        for item in observability.get("items", []) or []:
            for dashboard in item.get("dashboards", []) or []:
                title = str(dashboard.get("title") or "").lower()
                url = str(dashboard.get("url") or "").lower()
                dashboard_type = str(dashboard.get("dashboard_type") or "").lower()
                if (
                    dashboard_type == "topology"
                    or "skywalking" in title
                    or "trace" in title
                ):
                    categories["topology"] = True
                if "mysql" in title or "mysql" in url:
                    categories["mysql"] = True
                if "redis" in title or "redis" in url:
                    categories["redis"] = True
                if (
                    dashboard_type in {"related_monitor", "engine_grafana"}
                    or "qa target" in title
                    or "demo-target" in title
                    or "service dashboard" in title
                    or "业务大盘" in title
                    or "被测服务" in title
                ):
                    categories["service"] = True
        return categories

    @classmethod
    def _window_from_run(cls, run: Run) -> Optional[tuple[datetime, datetime]]:
        start = cls._as_utc_datetime(run.started_at)
        end = cls._as_utc_datetime(run.ended_at)
        if start is None or end is None or end <= start:
            return None
        return start, end

    @classmethod
    def _window_from_records(
        cls, records: list[Any]
    ) -> Optional[tuple[datetime, datetime]]:
        starts: list[datetime] = []
        ends: list[datetime] = []
        for record in records:
            started = cls._as_utc_datetime(getattr(record, "started_at", None))
            ended = cls._as_utc_datetime(getattr(record, "ended_at", None))
            if started:
                starts.append(started)
            if ended:
                ends.append(ended)
        if not starts or not ends:
            return None
        start = min(starts)
        end = max(ends)
        return (start, end) if end > start else None

    @staticmethod
    def _as_utc_datetime(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _dt(value: datetime) -> str:
        return value.astimezone(REPORT_TIMEZONE).isoformat()

    @classmethod
    def _format_summary(cls, findings: list[str]) -> str:
        parts = [
            cls._strip_sentence_end(item)
            for item in findings
            if str(item or "").strip()
        ]
        return "；".join(parts) + "。" if parts else ""

    @staticmethod
    def _strip_sentence_end(value: Any) -> str:
        return str(value or "").strip().rstrip("。；; ")

    @staticmethod
    def _num(value: Any) -> Optional[float]:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return None

    @staticmethod
    def _empty(summary: str) -> dict[str, Any]:
        return {
            "summary": summary,
            "domains": [],
            "findings": [],
            "limitations": [],
            "has_machine_metrics": False,
        }
