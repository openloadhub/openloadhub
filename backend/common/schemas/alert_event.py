from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RunAlertEventCreate(BaseModel):
    run_id: Optional[int] = None
    task_id: Optional[int] = None
    mixed_run_id: Optional[int] = None
    plan_run_id: Optional[int] = None
    subscription: Optional[str] = None
    source: str = "external"
    alertname: Optional[str] = None
    severity: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    labels: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    dashboard_url: Optional[str] = None
    fingerprint: Optional[str] = None
    source_event_id: Optional[str] = None
    dedupe_key: Optional[str] = None
    aggregation_key: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_alertmanager_keys(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        tags = payload.get("tags")
        labels = payload.get("labels")
        annotations = payload.get("annotations")
        if isinstance(tags, list):
            tag_labels: dict[str, Any] = {}
            for item in tags:
                if not isinstance(item, dict):
                    continue
                key = item.get("key")
                if not key:
                    continue
                tag_labels[str(key)] = item.get("value")
            if tag_labels:
                payload["labels"] = {**tag_labels, **(labels or {})}
                labels = payload["labels"]
        if isinstance(labels, dict):
            payload.setdefault("alertname", labels.get("alertname"))
            payload.setdefault("severity", labels.get("severity"))
            payload.setdefault("priority", labels.get("priority"))
            payload.setdefault(
                "source", labels.get("source") or payload.get("receiver")
            )
            label_alias_map = {
                "run_id": "run_id",
                "ptp_run_id": "run_id",
                "task_id": "task_id",
                "ptp_task_id": "task_id",
                "mixed_run_id": "mixed_run_id",
                "ptp_mixed_run_id": "mixed_run_id",
                "plan_run_id": "plan_run_id",
                "ptp_plan_run_id": "plan_run_id",
            }
            for src, dst in label_alias_map.items():
                if src in labels and dst not in payload:
                    payload[dst] = labels[src]
        if isinstance(annotations, dict):
            payload.setdefault(
                "dashboard_url",
                annotations.get("dashboard_url")
                or annotations.get("dashboardUrl")
                or annotations.get("grafana_url"),
            )
        alias_map = {
            "startsAt": "starts_at",
            "endsAt": "ends_at",
            "sourceEventId": "source_event_id",
            "dedupeKey": "dedupe_key",
            "aggregationKey": "aggregation_key",
            "dashboardUrl": "dashboard_url",
            "mixedRunId": "mixed_run_id",
            "planRunId": "plan_run_id",
            "runId": "run_id",
            "taskId": "task_id",
        }
        for src, dst in alias_map.items():
            if src in payload and dst not in payload:
                payload[dst] = payload[src]
        if "ruleName" in payload:
            payload.setdefault("source", "skywalking")
            if not payload.get("alertname"):
                payload["alertname"] = payload.get("ruleName")
            if not payload.get("source_event_id"):
                payload["source_event_id"] = payload.get("uuid")
            if not payload.get("fingerprint"):
                payload["fingerprint"] = payload.get("uuid")
            if isinstance(labels, dict):
                if not payload.get("severity"):
                    payload["severity"] = (
                        labels.get("severity")
                        or labels.get("level")
                        or labels.get("priority")
                    )
                if not payload.get("priority"):
                    payload["priority"] = labels.get("priority")
            alarm_message = payload.get("alarmMessage")
            if alarm_message and "annotations" not in payload:
                payload["annotations"] = {"summary": alarm_message}
            if "startTime" in payload and "starts_at" not in payload:
                payload["starts_at"] = datetime.fromtimestamp(
                    int(payload["startTime"]) / 1000,
                    tz=UTC,
                )
            recovery_time = payload.get("recoveryTime")
            if recovery_time and "ends_at" not in payload:
                payload["ends_at"] = datetime.fromtimestamp(
                    int(recovery_time) / 1000,
                    tz=UTC,
                )
            payload.setdefault("status", "resolved" if recovery_time else "firing")
        if not payload.get("source"):
            payload["source"] = "external"
        return payload

    model_config = ConfigDict(extra="allow")


class RunAlertEventResponse(BaseModel):
    event_id: int
    run_id: Optional[int] = None
    task_id: Optional[int] = None
    mixed_run_id: Optional[int] = None
    plan_run_id: Optional[int] = None
    subscription: Optional[str] = None
    source: str
    alertname: Optional[str] = None
    severity: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    labels: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    dashboard_url: Optional[str] = None
    fingerprint: str
    source_event_id: Optional[str] = None
    dedupe_key: str
    aggregation_key: str
    action_status: str
    raw_event: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class RunAlertEventSummary(BaseModel):
    total: int = 0
    firing_total: int = 0
    resolved_total: int = 0
    highest_severity: Optional[str] = None

    model_config = ConfigDict()


class RunAlertEventListResponse(BaseModel):
    items: list[RunAlertEventResponse] = Field(default_factory=list)
    summary: RunAlertEventSummary = Field(default_factory=RunAlertEventSummary)

    model_config = ConfigDict()


__all__ = [
    "RunAlertEventCreate",
    "RunAlertEventListResponse",
    "RunAlertEventResponse",
    "RunAlertEventSummary",
]
