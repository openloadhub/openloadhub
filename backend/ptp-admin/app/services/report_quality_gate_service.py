from __future__ import annotations

from typing import Any

from app.models.report import Report, ReportStatus
from app.schemas.report import ReportQualityGate, ReportQualityGateSection


class ReportQualityGateService:
    """Deterministic report completeness gate for public-safe delivery signals."""

    TREND_METRICS = ("throughput", "rt_avg_ms", "rt_p95_ms", "rt_p99_ms")

    @classmethod
    def evaluate(
        cls,
        report: Report,
        *,
        current_template: bool,
        has_report_file: bool,
    ) -> ReportQualityGate:
        metrics_data = (
            report.metrics_data if isinstance(report.metrics_data, dict) else {}
        )
        sections = {
            "report_frontdoor": cls._frontdoor_section(
                report=report,
                current_template=current_template,
                has_report_file=has_report_file,
            ),
            "summary_metrics": cls._summary_metrics_section(metrics_data),
            "checks": cls._checks_section(metrics_data),
            "endpoint_trends": cls._endpoint_trends_section(metrics_data),
            "observability": cls._observability_section(metrics_data),
        }
        gaps: list[str] = []
        evidence: list[str] = []
        limitations: list[str] = []
        recommended_actions: list[str] = []

        for name, section in sections.items():
            evidence.extend(f"{name}:{item}" for item in section.evidence)
            gaps.extend(f"{name}:{item}" for item in section.gaps)

        if not current_template:
            recommended_actions.append("重新生成当前模板报告后，再作为交付报告使用。")
        if sections["summary_metrics"].status != "ready":
            recommended_actions.append(
                "补齐 summary_metrics，确保报告至少包含总体和接口级核心指标。"
            )
        if sections["checks"].status != "ready":
            recommended_actions.append(
                "补齐 checks 或断言结果，避免报告缺少业务正确性证据。"
            )
        if sections["endpoint_trends"].status != "ready":
            recommended_actions.append(
                "补齐 endpoint_trends，确保报告包含吞吐和响应时间趋势。"
            )
        if sections["observability"].status != "ready":
            limitations.append(
                "关联监控分析不完整时，报告只能说明压测侧结果，不能扩展为资源瓶颈结论。"
            )

        blocking_sections = [
            name for name, section in sections.items() if section.status == "missing"
        ]
        partial_sections = [
            name for name, section in sections.items() if section.status == "partial"
        ]

        if blocking_sections:
            status = "blocked"
            status_label = "缺少交付证据"
        elif partial_sections:
            status = "partial"
            status_label = "部分证据待补齐"
        else:
            status = "ready"
            status_label = "可人工复核"

        evidence_ready = status == "ready"
        if evidence_ready:
            recommended_actions.append("报告核心证据已齐备，可进入人工复核确认。")

        return ReportQualityGate(
            status=status,
            status_label=status_label,
            evidence_ready=evidence_ready,
            current_template=current_template,
            required_sections=sections,
            evidence=list(dict.fromkeys(evidence)),
            gaps=list(dict.fromkeys(gaps)),
            limitations=list(dict.fromkeys(limitations)),
            recommended_actions=list(dict.fromkeys(recommended_actions)),
        )

    @staticmethod
    def _coerce_rows(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def _has_value(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict)):
            return bool(value)
        return True

    @classmethod
    def _frontdoor_section(
        cls,
        *,
        report: Report,
        current_template: bool,
        has_report_file: bool,
    ) -> ReportQualityGateSection:
        evidence: list[str] = []
        gaps: list[str] = []
        status_value = getattr(report.status, "value", str(report.status))
        if report.status == ReportStatus.COMPLETED:
            evidence.append("report_completed")
        else:
            gaps.append(f"report_status_{status_value}")
        if has_report_file:
            evidence.append("report_file_present")
        else:
            gaps.append("report_file_missing")
        if current_template:
            evidence.append("current_template")
        else:
            gaps.append("current_template_missing")

        status = "ready" if not gaps else "missing"
        return ReportQualityGateSection(
            status=status,
            label="报告前门",
            detail=(
                "报告文件存在且兼容当前模板。"
                if status == "ready"
                else "报告前门未满足当前模板下载要求。"
            ),
            evidence=evidence,
            gaps=gaps,
        )

    @classmethod
    def _summary_metrics_section(
        cls, metrics_data: dict[str, Any]
    ) -> ReportQualityGateSection:
        rows = cls._coerce_rows(metrics_data.get("summary_metrics"))
        evidence = [f"rows={len(rows)}"] if rows else []
        missing_fields: set[str] = set()
        required_any = ("total_requests", "throughput", "avg_rt_ms", "p95_rt_ms")
        for field in required_any:
            if not any(cls._has_value(row.get(field)) for row in rows):
                missing_fields.add(field)

        if not rows:
            return ReportQualityGateSection(
                status="missing",
                label="汇总指标",
                detail="缺少 summary_metrics。",
                gaps=["summary_metrics_missing"],
            )
        if missing_fields:
            return ReportQualityGateSection(
                status="partial",
                label="汇总指标",
                detail="summary_metrics 存在，但核心字段不完整。",
                evidence=evidence,
                gaps=[f"missing_{field}" for field in sorted(missing_fields)],
            )
        return ReportQualityGateSection(
            status="ready",
            label="汇总指标",
            detail="summary_metrics 已包含请求量、吞吐和响应时间核心字段。",
            evidence=evidence,
        )

    @classmethod
    def _checks_section(cls, metrics_data: dict[str, Any]) -> ReportQualityGateSection:
        rows = cls._coerce_rows(metrics_data.get("checks"))
        if not rows:
            return ReportQualityGateSection(
                status="missing",
                label="Checks",
                detail="缺少 checks 或断言结果。",
                gaps=["checks_missing"],
            )
        has_success_rate = any(cls._has_value(row.get("success_rate")) for row in rows)
        if not has_success_rate:
            return ReportQualityGateSection(
                status="partial",
                label="Checks",
                detail="checks 存在，但缺少 success_rate。",
                evidence=[f"rows={len(rows)}"],
                gaps=["success_rate_missing"],
            )
        return ReportQualityGateSection(
            status="ready",
            label="Checks",
            detail="checks 已包含断言成功率。",
            evidence=[f"rows={len(rows)}"],
        )

    @classmethod
    def _endpoint_trends_section(
        cls, metrics_data: dict[str, Any]
    ) -> ReportQualityGateSection:
        trend_map = (
            metrics_data.get("endpoint_trends")
            if isinstance(metrics_data.get("endpoint_trends"), dict)
            else {}
        )
        ready_metrics: list[str] = []
        missing_metrics: list[str] = []
        for metric in cls.TREND_METRICS:
            payload = trend_map.get(metric)
            rows = (
                cls._coerce_rows(payload.get("items"))
                if isinstance(payload, dict)
                else []
            )
            if rows:
                ready_metrics.append(metric)
            else:
                missing_metrics.append(metric)

        if len(ready_metrics) == len(cls.TREND_METRICS):
            return ReportQualityGateSection(
                status="ready",
                label="接口趋势",
                detail="接口吞吐和响应时间趋势已齐备。",
                evidence=[f"metric={item}" for item in ready_metrics],
            )
        if ready_metrics:
            return ReportQualityGateSection(
                status="partial",
                label="接口趋势",
                detail="endpoint_trends 只覆盖部分指标。",
                evidence=[f"metric={item}" for item in ready_metrics],
                gaps=[f"missing_{item}" for item in missing_metrics],
            )
        return ReportQualityGateSection(
            status="missing",
            label="接口趋势",
            detail="缺少 endpoint_trends。",
            gaps=["endpoint_trends_missing"],
        )

    @classmethod
    def _observability_section(
        cls, metrics_data: dict[str, Any]
    ) -> ReportQualityGateSection:
        analysis = (
            metrics_data.get("observability_analysis")
            if isinstance(metrics_data.get("observability_analysis"), dict)
            else {}
        )
        domains = cls._coerce_rows(analysis.get("domains"))
        summary = analysis.get("summary")
        has_machine_metrics = bool(analysis.get("has_machine_metrics"))
        limitations = [
            str(item)
            for item in analysis.get("limitations", []) or []
            if str(item).strip()
        ]

        if domains and has_machine_metrics:
            return ReportQualityGateSection(
                status="ready",
                label="关联监控",
                detail="关联监控分析包含机器可读指标。",
                evidence=[f"domains={len(domains)}", "has_machine_metrics"],
            )
        if domains or cls._has_value(summary):
            return ReportQualityGateSection(
                status="partial",
                label="关联监控",
                detail="关联监控分析存在，但机器可读指标不足。",
                evidence=[f"domains={len(domains)}"] if domains else ["summary"],
                gaps=(limitations or ["machine_metrics_missing"]),
            )
        return ReportQualityGateSection(
            status="missing",
            label="关联监控",
            detail="缺少 observability_analysis。",
            gaps=["observability_analysis_missing"],
        )
