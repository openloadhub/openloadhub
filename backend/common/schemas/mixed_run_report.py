from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_serializer


REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")


class MixedRunReportGenerateRequest(BaseModel):
    round: Optional[int] = Field(default=None, ge=1)
    collection_id: Optional[int] = Field(default=None, ge=1)

    model_config = ConfigDict()


class MixedRunReportResponse(BaseModel):
    report_id: int
    mixed_run_id: int
    round: int
    collection_id: Optional[int] = None
    version: int
    status: str
    summary: Optional[str] = None
    payload_json: Optional[dict[str, Any]] = None
    artifact_path: Optional[str] = None
    file_size: Optional[int] = None
    input_sources: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    error_message: Optional[str] = None
    generated_by: Optional[int] = None
    generated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("generated_at", "created_at", "updated_at", when_used="json")
    def _serialize_report_datetime(self, value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(REPORT_TIMEZONE).isoformat(timespec="seconds")


class MixedRunReportTaskAcceptedResponse(BaseModel):
    mixed_run_id: int
    round: Optional[int] = None
    collection_id: Optional[int] = None
    accepted: bool
    async_task_id: str
    status: str
    report: Optional[MixedRunReportResponse] = None

    model_config = ConfigDict()


class MixedRunReportTaskStatusResponse(BaseModel):
    mixed_run_id: int
    async_task_id: str
    job_status: str
    completed: bool
    result: Optional[MixedRunReportResponse] = None
    error: Optional[str] = None

    model_config = ConfigDict()
