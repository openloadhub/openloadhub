from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class WebhookChannel(str, Enum):
    WECOM = "wecom"
    DINGTALK = "dingtalk"
    FEISHU = "feishu"


class WebhookEventType(str, Enum):
    PLAN_RUN_COMPLETED = "plan_run_completed"
    PLAN_RUN_FAILED = "plan_run_failed"
    THRESHOLD_BREACHED = "threshold_breached"
    REGRESSION_BLOCKED = "regression_blocked"


class WebhookSignatureType(str, Enum):
    NONE = "none"
    FEISHU_V1 = "feishu_v1"


class WebhookTemplatePreviewRequest(BaseModel):
    channel: WebhookChannel
    event_type: WebhookEventType
    variables: dict[str, Any] = Field(default_factory=dict)
    template: Optional[str] = Field(
        default=None,
        description="可选自定义模板，使用 {run_id} 形式引用变量",
    )
    title: Optional[str] = Field(default=None, max_length=128)

    model_config = ConfigDict(use_enum_values=True)


class WebhookSendRequest(WebhookTemplatePreviewRequest):
    webhook_url: str = Field(..., min_length=1, max_length=2048)
    signature_type: WebhookSignatureType = WebhookSignatureType.NONE
    signing_secret: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    max_retry_count: int = Field(default=0, ge=0, le=5)
    retry_interval_seconds: float = Field(default=0.0, ge=0, le=60)


class WebhookConfigCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    channel: WebhookChannel
    event_types: list[WebhookEventType] = Field(min_length=1)
    webhook_url: str = Field(..., min_length=1, max_length=2048)
    signature_type: WebhookSignatureType = WebhookSignatureType.NONE
    signing_secret: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    enabled: bool = True
    template: Optional[str] = None
    title: Optional[str] = Field(default=None, max_length=128)
    timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    max_retry_count: int = Field(default=0, ge=0, le=5)
    retry_interval_seconds: float = Field(default=0.0, ge=0, le=60)

    model_config = ConfigDict(use_enum_values=True)


class WebhookConfigUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    channel: Optional[WebhookChannel] = None
    event_types: Optional[list[WebhookEventType]] = Field(default=None, min_length=1)
    webhook_url: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    signature_type: Optional[WebhookSignatureType] = None
    signing_secret: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    enabled: Optional[bool] = None
    template: Optional[str] = None
    title: Optional[str] = Field(default=None, max_length=128)
    timeout_seconds: Optional[float] = Field(default=None, gt=0, le=30)
    max_retry_count: Optional[int] = Field(default=None, ge=0, le=5)
    retry_interval_seconds: Optional[float] = Field(default=None, ge=0, le=60)

    model_config = ConfigDict(use_enum_values=True)


class WebhookConfigResponse(BaseModel):
    config_id: int
    name: str
    channel: WebhookChannel
    event_types: list[WebhookEventType]
    webhook_url_masked: str
    webhook_host: str | None = None
    signature_type: WebhookSignatureType = WebhookSignatureType.NONE
    signing_secret_set: bool = False
    enabled: bool
    template: str | None = None
    title: str | None = None
    timeout_seconds: float
    max_retry_count: int
    retry_interval_seconds: float
    created_by: int | None = None
    created_at: Any | None = None
    updated_at: Any | None = None

    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


class WebhookTemplatePreviewResponse(BaseModel):
    channel: WebhookChannel
    event_type: WebhookEventType
    title: str
    rendered_text: str
    payload: dict[str, Any]
    covered_variables: list[str] = Field(default_factory=list)
    missing_variables: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    dry_run: bool = True

    model_config = ConfigDict(use_enum_values=True)


class WebhookSendRecordResponse(BaseModel):
    record_id: int
    channel: WebhookChannel
    event_type: WebhookEventType
    status: str
    title: str
    rendered_text: str
    payload: dict[str, Any]
    variables: dict[str, Any] | None = None
    webhook_url_masked: str | None = None
    webhook_host: str | None = None
    http_status_code: int | None = None
    response_body: str | None = None
    error_message: str | None = None
    attempt_count: int = 0
    config_id: int | None = None
    trigger_source: str = "manual"
    created_by: int | None = None
    created_at: Any | None = None
    updated_at: Any | None = None
    sent_at: Any | None = None

    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


class WebhookSendResponse(BaseModel):
    record: WebhookSendRecordResponse
    preview: WebhookTemplatePreviewResponse

    model_config = ConfigDict(use_enum_values=True)
