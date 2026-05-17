from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from common.schemas.run import RunAIEvidenceItem


@dataclass(frozen=True)
class ObservabilityQueryResult:
    evidence: list[RunAIEvidenceItem] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


class ObservabilityQueryService:
    MACHINE_RESULT_KEYS = ("result", "summary", "value")
    SUPPORTED_PROVIDERS = {"prometheus", "grafana", "tempo", "external_summary"}
    SUCCESS_STATUSES = {"success", "succeeded", "ok"}

    def build_evidence_from_params(self, params: Any) -> ObservabilityQueryResult:
        if not isinstance(params, dict):
            return ObservabilityQueryResult()

        entries = self._extract_entries(params)
        if not entries:
            return ObservabilityQueryResult()

        evidence: list[RunAIEvidenceItem] = []
        limitations: list[str] = []
        skipped_without_result = 0
        skipped_without_success = 0
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            machine_result = self._first_present(entry, self.MACHINE_RESULT_KEYS)
            if machine_result is not None and not self._is_successful_entry(entry):
                skipped_without_success += 1
                continue
            if machine_result is None:
                if entry.get("query") or entry.get("url") or entry.get("dashboard_url"):
                    skipped_without_result += 1
                continue

            evidence_item = self._build_evidence_item(entry, machine_result, index)
            if evidence_item is not None:
                evidence.append(evidence_item)

        if skipped_without_result:
            limitations.append(
                "observability_queries 中存在仅配置 URL/query、未提供机器可读结果的条目；"
                "AI evidence 未声称已读取纯 dashboard 或外部查询页面。"
            )
        if skipped_without_success:
            limitations.append(
                "observability_queries 中存在未标记 status=success 的机器结果；"
                "AI evidence 未将其作为已读取外部指标。"
            )

        return ObservabilityQueryResult(evidence=evidence, limitations=limitations)

    @classmethod
    def _extract_entries(cls, params: dict[str, Any]) -> list[Any]:
        entries: list[Any] = []
        for key in ("observability_queries", "query_templates"):
            value = params.get(key)
            if isinstance(value, list):
                entries.extend(value)
        return entries

    @staticmethod
    def _first_present(entry: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            if key in entry and entry[key] not in (None, ""):
                return entry[key]
        return None

    @classmethod
    def _is_successful_entry(cls, entry: dict[str, Any]) -> bool:
        raw_status = (
            entry.get("status")
            or entry.get("query_status")
            or entry.get("result_status")
        )
        status = cls._clean_str(raw_status)
        return bool(status and status.lower() in cls.SUCCESS_STATUSES)

    @classmethod
    def _build_evidence_item(
        cls, entry: dict[str, Any], machine_result: Any, index: int
    ) -> Optional[RunAIEvidenceItem]:
        title = cls._clean_str(entry.get("name")) or cls._clean_str(entry.get("title"))
        if not title:
            title = f"观测查询 {index + 1}"

        provider = cls._clean_str(entry.get("provider"))
        if provider:
            provider = provider.lower()
        if provider and provider not in cls.SUPPORTED_PROVIDERS:
            provider = None

        metric = cls._clean_str(entry.get("metric"))
        unit = cls._clean_str(entry.get("unit"))
        result_text = cls._format_machine_result(machine_result)
        if not result_text:
            return None

        detail_parts: list[str] = []
        if provider:
            detail_parts.append(f"provider={provider}")
        if metric:
            detail_parts.append(f"metric={metric}")
        if unit:
            detail_parts.append(f"unit={unit}")
        query_ref = cls._query_reference(entry)
        if query_ref:
            detail_parts.append(query_ref)
        detail_parts.append(f"result={result_text}")

        return RunAIEvidenceItem(
            source="observability_queries",
            label=title,
            detail="，".join(detail_parts),
            target_section="monitor",
            metric=metric,
        )

    @classmethod
    def _format_machine_result(cls, value: Any) -> str:
        if isinstance(value, str):
            return value.strip()[:500]
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, dict):
            parts: list[str] = []
            for key, item in value.items():
                if not isinstance(key, str) or item in (None, ""):
                    continue
                item_text = cls._format_machine_result(item)
                if item_text:
                    parts.append(f"{key}={item_text}")
                if len(parts) >= 6:
                    break
            return "，".join(parts)[:500]
        if isinstance(value, list):
            parts = [cls._format_machine_result(item) for item in value[:6]]
            return "；".join(item for item in parts if item)[:500]
        return ""

    @classmethod
    def _query_reference(cls, entry: dict[str, Any]) -> Optional[str]:
        template_id = cls._clean_str(entry.get("template_id") or entry.get("id"))
        if template_id:
            return f"template_id={template_id[:120]}"
        query = cls._clean_str(entry.get("query") or entry.get("expr"))
        if query:
            return f"query={query[:160]}"
        return None

    @staticmethod
    def _clean_str(value: Any) -> Optional[str]:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None
