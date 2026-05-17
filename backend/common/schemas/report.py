from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from common.models.enums import ReportStatus, ReportType
from common.utils.time import to_rfc3339_z


class ReportBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="报告名称")
    description: Optional[str] = Field(None, description="报告描述")
    report_type: ReportType = Field(..., description="报告类型")


class ReportCreate(ReportBase):
    task_id: int = Field(..., description="关联的任务ID")
    run_id: Optional[int] = Field(None, description="关联的执行记录ID")
    test_config: Optional[dict[str, Any]] = Field(None, description="测试配置")


class ReportUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    status: Optional[ReportStatus] = None
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    total_requests: Optional[int] = None
    successful_requests: Optional[int] = None
    failed_requests: Optional[int] = None
    error_rate: Optional[float] = None
    avg_response_time: Optional[float] = None
    min_response_time: Optional[float] = None
    max_response_time: Optional[float] = None
    p95_response_time: Optional[float] = None
    p99_response_time: Optional[float] = None
    throughput: Optional[float] = None
    test_config: Optional[dict[str, Any]] = None
    metrics_data: Optional[dict[str, Any]] = None


class ReportQualityGateSection(BaseModel):
    status: str
    label: str
    detail: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class ReportQualityGate(BaseModel):
    status: str
    status_label: str
    evidence_ready: bool = False
    current_template: bool = False
    required_sections: dict[str, ReportQualityGateSection] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)


class ReportResponse(ReportBase):
    id: int
    task_id: int
    run_id: Optional[int] = None
    status: ReportStatus
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    total_requests: Optional[int] = None
    successful_requests: Optional[int] = None
    failed_requests: Optional[int] = None
    error_rate: Optional[float] = None
    avg_response_time: Optional[float] = None
    min_response_time: Optional[float] = None
    max_response_time: Optional[float] = None
    p95_response_time: Optional[float] = None
    p99_response_time: Optional[float] = None
    throughput: Optional[float] = None
    test_config: Optional[dict[str, Any]] = None
    metrics_data: Optional[dict[str, Any]] = None
    quality_gate: Optional[ReportQualityGate] = None
    generated_by: Optional[int] = None
    generated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ReportStatistics(BaseModel):
    total_reports: int
    by_status: dict[ReportStatus, int]
    by_type: dict[ReportType, int]
    avg_generation_time: Optional[float] = None


class ReportListResponse(BaseModel):
    items: list[ReportResponse]
    total: int
    page: int
    size: int
    pages: int


__all__ = [
    "ReportBase",
    "ReportCreate",
    "ReportUpdate",
    "ReportResponse",
    "ReportQualityGate",
    "ReportQualityGateSection",
    "ReportStatistics",
    "ReportListResponse",
    "ReportStatus",
    "ReportType",
]
