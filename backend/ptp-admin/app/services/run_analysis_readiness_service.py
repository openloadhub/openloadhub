from __future__ import annotations

from typing import Any

from app.models.run import Run, RunStatus
from app.schemas.run import RunAnalysisReadiness, RunAnalysisReadinessSection


class RunAnalysisReadinessService:
    """Deterministic readiness signal for RunDetail analysis inputs."""

    TERMINAL_STATUSES = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.STOPPED}

    @classmethod
    def evaluate(cls, run: Run) -> RunAnalysisReadiness:
        params = run.params if isinstance(run.params, dict) else {}
        overview = getattr(run, "overview_summary", None)
        sections = {
            "run_lifecycle": cls._run_lifecycle_section(run),
            "summary_metrics": cls._summary_metrics_section(run, params, overview),
            "checks": cls._checks_section(params, overview),
            "execution_context": cls._execution_context_section(run, params),
            "observability_frontdoor": cls._observability_frontdoor_section(
                run, params
            ),
        }

        evidence: list[str] = []
        gaps: list[str] = []
        limitations: list[str] = []
        recommended_actions: list[str] = []

        for name, section in sections.items():
            evidence.extend(f"{name}:{item}" for item in section.evidence)
            gaps.extend(f"{name}:{item}" for item in section.gaps)

        if sections["run_lifecycle"].status != "ready":
            recommended_actions.append("等待运行进入终态后，再整理最终分析结论。")
        if sections["summary_metrics"].status != "ready":
            recommended_actions.append("补齐核心指标，至少需要请求量、吞吐和响应时间。")
        if sections["checks"].status != "ready":
            recommended_actions.append("补齐 checks 或断言结果，用于判断业务正确性。")
        if sections["execution_context"].status != "ready":
            recommended_actions.append("补齐施压节点和运行参数，便于复核执行范围。")
        if sections["observability_frontdoor"].status != "ready":
            limitations.append(
                "监控入口不完整时，只能基于压测侧指标复核，不能扩展为资源瓶颈结论。"
            )

        missing_sections = [
            name for name, section in sections.items() if section.status == "missing"
        ]
        partial_sections = [
            name for name, section in sections.items() if section.status == "partial"
        ]

        if missing_sections:
            status = "blocked"
            status_label = "分析输入缺失"
        elif partial_sections:
            status = "partial"
            status_label = "分析输入待补齐"
        else:
            status = "ready"
            status_label = "可开始人工分析"
            recommended_actions.append("运行分析输入已齐备，可进入人工复核。")

        return RunAnalysisReadiness(
            status=status,
            status_label=status_label,
            evidence_ready=status == "ready",
            required_sections=sections,
            evidence=list(dict.fromkeys(evidence)),
            gaps=list(dict.fromkeys(gaps)),
            limitations=list(dict.fromkeys(limitations)),
            recommended_actions=list(dict.fromkeys(recommended_actions)),
        )

    @staticmethod
    def _has_value(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict)):
            return bool(value)
        return True

    @staticmethod
    def _coerce_rows(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    @classmethod
    def _run_lifecycle_section(cls, run: Run) -> RunAnalysisReadinessSection:
        status_value = getattr(run.run_status, "value", str(run.run_status))
        evidence: list[str] = []
        gaps: list[str] = []
        if run.run_status in cls.TERMINAL_STATUSES:
            evidence.append(f"run_status_{status_value}")
        else:
            gaps.append(f"run_status_{status_value}")
        if run.started_at:
            evidence.append("started_at_present")
        else:
            gaps.append("started_at_missing")
        if run.ended_at:
            evidence.append("ended_at_present")
        elif run.run_status in cls.TERMINAL_STATUSES:
            gaps.append("ended_at_missing")

        status = "ready" if not gaps else "missing"
        return RunAnalysisReadinessSection(
            status=status,
            label="运行生命周期",
            detail=(
                "运行已进入终态且具备时间窗口。"
                if status == "ready"
                else "运行终态或时间窗口尚不完整。"
            ),
            evidence=evidence,
            gaps=gaps,
        )

    @classmethod
    def _summary_metrics_section(
        cls, run: Run, params: dict[str, Any], overview: Any
    ) -> RunAnalysisReadinessSection:
        rows = cls._coerce_rows(params.get("summary_metrics"))
        evidence: list[str] = []
        missing_fields: list[str] = []
        checks = {
            "total_requests": any(
                cls._has_value(row.get("total_requests")) for row in rows
            )
            or cls._has_value(getattr(overview, "total_requests", None))
            or cls._has_value(run.total_requests),
            "throughput": any(cls._has_value(row.get("throughput")) for row in rows)
            or cls._has_value(getattr(overview, "throughput", None))
            or cls._has_value(run.rps),
            "avg_rt_ms": any(cls._has_value(row.get("avg_rt_ms")) for row in rows)
            or cls._has_value(getattr(overview, "avg_rt_ms", None))
            or cls._has_value(run.avg_rt_ms),
            "p95_rt_ms": any(cls._has_value(row.get("p95_rt_ms")) for row in rows)
            or cls._has_value(getattr(overview, "p95_rt_ms", None))
            or cls._has_value(run.p95_rt_ms),
        }
        if rows:
            evidence.append(f"rows={len(rows)}")
        for field, ready in checks.items():
            if ready:
                evidence.append(field)
            else:
                missing_fields.append(field)

        if len(missing_fields) == len(checks):
            return RunAnalysisReadinessSection(
                status="missing",
                label="核心指标",
                detail="缺少请求量、吞吐和响应时间核心指标。",
                gaps=["summary_metrics_missing"],
            )
        if missing_fields:
            return RunAnalysisReadinessSection(
                status="partial",
                label="核心指标",
                detail="核心指标存在，但字段不完整。",
                evidence=evidence,
                gaps=[f"missing_{item}" for item in missing_fields],
            )
        return RunAnalysisReadinessSection(
            status="ready",
            label="核心指标",
            detail="请求量、吞吐和响应时间核心指标已齐备。",
            evidence=evidence,
        )

    @classmethod
    def _checks_section(
        cls, params: dict[str, Any], overview: Any
    ) -> RunAnalysisReadinessSection:
        rows = cls._coerce_rows(params.get("checks"))
        overview_rate = getattr(overview, "checks_success_rate", None)
        if not rows and overview_rate is None:
            return RunAnalysisReadinessSection(
                status="missing",
                label="Checks",
                detail="缺少 checks 或断言成功率。",
                gaps=["checks_missing"],
            )
        evidence = [f"rows={len(rows)}"] if rows else ["overview_checks_success_rate"]
        if rows and not any(cls._has_value(row.get("success_rate")) for row in rows):
            return RunAnalysisReadinessSection(
                status="partial",
                label="Checks",
                detail="checks 存在，但缺少 success_rate。",
                evidence=evidence,
                gaps=["success_rate_missing"],
            )
        return RunAnalysisReadinessSection(
            status="ready",
            label="Checks",
            detail="checks 已包含断言成功率。",
            evidence=evidence,
        )

    @classmethod
    def _execution_context_section(
        cls, run: Run, params: dict[str, Any]
    ) -> RunAnalysisReadinessSection:
        evidence: list[str] = []
        gaps: list[str] = []
        pod_total = getattr(run, "pod_total", None)
        pod_actual = getattr(run, "pod_actual", None)
        agent_runs = cls._coerce_rows(params.get("agent_runs"))
        if cls._has_value(pod_total) or cls._has_value(pod_actual):
            evidence.append(f"pods={pod_actual or 0}/{pod_total or 0}")
        elif agent_runs:
            evidence.append(f"agent_runs={len(agent_runs)}")
        else:
            gaps.append("pressure_nodes_missing")

        param_keys = [
            key
            for key in (
                "vus",
                "thread_count",
                "target_tps",
                "iterations",
                "loops",
                "duration",
            )
            if cls._has_value(params.get(key))
        ]
        if param_keys:
            evidence.append("params=" + ",".join(param_keys))
        else:
            gaps.append("run_params_missing")

        if not gaps:
            status = "ready"
            detail = "施压节点和关键运行参数已齐备。"
        elif evidence:
            status = "partial"
            detail = "执行上下文存在，但仍有字段待补齐。"
        else:
            status = "missing"
            detail = "缺少施压节点和运行参数。"
        return RunAnalysisReadinessSection(
            status=status,
            label="执行上下文",
            detail=detail,
            evidence=evidence,
            gaps=gaps,
        )

    @classmethod
    def _observability_frontdoor_section(
        cls, run: Run, params: dict[str, Any]
    ) -> RunAnalysisReadinessSection:
        evidence: list[str] = []
        gaps: list[str] = []
        if cls._coerce_rows(params.get("related_monitors")):
            evidence.append("related_monitors")
        elif cls._coerce_rows(params.get("monitor_dashboards")):
            evidence.append("monitor_dashboards")
        else:
            gaps.append("related_monitors_missing")

        if cls._coerce_rows(params.get("topology_dashboards")):
            evidence.append("topology_dashboards")
        elif cls._has_value(params.get("trace_url")) or cls._has_value(
            params.get("topology_url")
        ):
            evidence.append("trace_or_topology_url")
        else:
            gaps.append("trace_or_topology_missing")

        if cls._has_value(getattr(run, "run_window_label", None)):
            evidence.append("run_window_label")
        else:
            gaps.append("run_window_missing")

        if not gaps:
            status = "ready"
            detail = "监控、链路入口和运行窗口已齐备。"
        elif evidence:
            status = "partial"
            detail = "已有部分观测入口，但不完整。"
        else:
            status = "missing"
            detail = "缺少监控和链路入口。"
        return RunAnalysisReadinessSection(
            status=status,
            label="观测入口",
            detail=detail,
            evidence=evidence,
            gaps=gaps,
        )
