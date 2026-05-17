from __future__ import annotations

from typing import Any

from app.models.task import Task
from app.schemas.task import ScenarioQualityLint, ScenarioQualityLintIssue


class ScenarioQualityLintService:
    """Non-blocking public-safe scenario configuration lint."""

    @classmethod
    def evaluate_task(
        cls, task: Task, run_params: dict[str, Any] | None = None
    ) -> ScenarioQualityLint:
        properties = task.properties if isinstance(task.properties, dict) else {}
        params = run_params if isinstance(run_params, dict) else {}
        merged = {**properties, **params}
        issues: list[ScenarioQualityLintIssue] = []

        if not cls._has_checks(merged):
            issues.append(
                cls._issue(
                    "checks_missing",
                    "缺少 checks/assertions",
                    "当前任务未配置 checks 或断言参数，结果只能说明压测侧指标，业务正确性证据不足。",
                    "补齐 checks、断言阈值或脚本内校验逻辑。",
                )
            )
        if not cls._has_monitor(merged):
            issues.append(
                cls._issue(
                    "related_monitors_missing",
                    "缺少关联监控",
                    "当前任务未配置关联监控入口，RunDetail 和报告中的观测证据会不完整。",
                    "在任务高级配置中补齐 Grafana/业务监控链接。",
                )
            )
        if not cls._has_trace_or_topology(merged):
            issues.append(
                cls._issue(
                    "trace_or_topology_missing",
                    "缺少关联链路",
                    "当前任务未配置 trace/topology 入口，跨服务链路复核证据不足。",
                    "补齐 trace_link、topology_url 或 topology_dashboards。",
                )
            )
        if not cls._has_variables(merged):
            issues.append(
                cls._issue(
                    "script_variables_missing",
                    "缺少脚本变量",
                    "当前任务未预填脚本变量，后续运行参数复用和批次覆盖不够明确。",
                    "补齐 properties.variables，并维护变量类型映射。",
                )
            )

        pod_count = cls._parse_positive_int(
            merged.get("pod_count") or merged.get("pod_num")
        )
        data_distribution = str(merged.get("data_distribution") or "").strip()
        if pod_count and pod_count > 1 and data_distribution not in {"avg", "all"}:
            issues.append(
                cls._issue(
                    "data_distribution_missing",
                    "多节点缺少数据下发方式",
                    "pod_count 大于 1 但未明确数据下发方式，数据文件或参数分片语义不清晰。",
                    "选择平均分割 avg 或全量下发 all。",
                )
            )
        target_tps = cls._parse_positive_float(
            merged.get("target_tps")
            or merged.get("base_target_tps")
            or merged.get("fixed_tps")
            or cls._get_variable(merged, "target_tps")
            or cls._get_variable(merged, "base_target_tps")
            or cls._get_variable(merged, "fixed_tps")
        )
        vus = cls._parse_positive_int(
            merged.get("vus")
            or merged.get("thread_count")
            or cls._get_variable(merged, "vus")
            or cls._get_variable(merged, "thread_count")
            or getattr(task, "thread_count", None)
        )
        effective_pods = pod_count or 1
        if target_tps and vus and target_tps > max(1, vus * effective_pods * 20):
            issues.append(
                cls._issue(
                    "pod_count_target_tps_mismatch",
                    "节点数与目标 TPS 可能不协调",
                    "目标 TPS 明显高于当前 VUs/节点数的保守容量估计，可能导致压力端先成为瓶颈。",
                    "复核 target_tps、VUs/thread_count 和 pod_count，必要时分阶段加压。",
                )
            )

        return ScenarioQualityLint(
            status="warning" if issues else "clean",
            warning_count=len(issues),
            issues=issues,
        )

    @classmethod
    def summarize_plan(cls, tasks: list[Task]) -> ScenarioQualityLint:
        issues: list[ScenarioQualityLintIssue] = []
        seen: set[str] = set()
        for task in tasks:
            lint = cls.evaluate_task(task)
            task_label = f"任务 #{task.id}"
            if getattr(task, "name", None):
                task_label = f"{task.name} #{task.id}"
            for issue in lint.issues:
                key = f"{task.id}:{issue.code}"
                if key in seen:
                    continue
                seen.add(key)
                issues.append(
                    ScenarioQualityLintIssue(
                        code=issue.code,
                        label=f"{task_label}：{issue.label}",
                        detail=issue.detail,
                        severity=issue.severity,
                        recommended_action=issue.recommended_action,
                    )
                )
        return ScenarioQualityLint(
            status="warning" if issues else "clean",
            warning_count=len(issues),
            issues=issues[:20],
        )

    @staticmethod
    def _issue(
        code: str, label: str, detail: str, recommended_action: str
    ) -> ScenarioQualityLintIssue:
        return ScenarioQualityLintIssue(
            code=code,
            label=label,
            detail=detail,
            severity="warning",
            recommended_action=recommended_action,
        )

    @staticmethod
    def _parse_positive_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _parse_positive_float(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _has_non_empty_list(value: Any) -> bool:
        return isinstance(value, list) and any(bool(item) for item in value)

    @staticmethod
    def _has_non_empty_dict(value: Any) -> bool:
        return isinstance(value, dict) and any(str(key).strip() for key in value)

    @classmethod
    def _has_checks(cls, data: dict[str, Any]) -> bool:
        variables = (
            data.get("variables") if isinstance(data.get("variables"), dict) else {}
        )
        return (
            cls._has_non_empty_list(data.get("checks"))
            or cls._has_non_empty_dict(data.get("assertions"))
            or any(
                key in data or key in variables
                for key in (
                    "expected_status",
                    "max_error_rate",
                    "check_name",
                    "p95_threshold_ms",
                )
            )
        )

    @classmethod
    def _has_monitor(cls, data: dict[str, Any]) -> bool:
        return (
            cls._has_non_empty_list(data.get("related_monitors"))
            or cls._has_non_empty_list(data.get("monitor_dashboards"))
            or bool(
                str(
                    data.get("monitor_link") or data.get("monitor_dashboard_url") or ""
                ).strip()
            )
        )

    @classmethod
    def _has_trace_or_topology(cls, data: dict[str, Any]) -> bool:
        return cls._has_non_empty_list(data.get("topology_dashboards")) or bool(
            str(
                data.get("trace_link")
                or data.get("trace_url")
                or data.get("topology_url")
                or ""
            ).strip()
        )

    @classmethod
    def _has_variables(cls, data: dict[str, Any]) -> bool:
        return cls._has_non_empty_dict(
            data.get("variables")
        ) or cls._has_non_empty_dict(data.get("variable_types"))

    @staticmethod
    def _get_variable(data: dict[str, Any], key: str) -> Any:
        variables = data.get("variables")
        if not isinstance(variables, dict):
            return None
        return variables.get(key)
