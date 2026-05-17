from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from common.models.enums import ScriptStatus, ScriptType
from common.utils.time import to_rfc3339_z


class ScriptCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="脚本名称")
    description: Optional[str] = Field(None, description="脚本描述")
    script_type: ScriptType = Field(..., description="脚本类型")
    file_path: str = Field(..., max_length=500, description="脚本文件路径")
    file_size: Optional[int] = Field(None, ge=0, description="文件大小(字节)")
    content_hash: Optional[str] = Field(None, max_length=64, description="文件内容哈希")
    version: Optional[str] = Field("1.0", max_length=50, description="脚本版本")
    tags: Optional[list[str]] = Field(None, description="标签")
    parameters: Optional[dict[str, Any]] = Field(None, description="参数配置")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "登录接口测试脚本",
                "description": "测试登录接口的性能",
                "script_type": "JMETER",
                "file_path": "/scripts/login_test.jmx",
                "file_size": 10240,
                "content_hash": "abc123...",
                "version": "1.0",
                "tags": ["login", "api", "critical"],
                "parameters": {
                    "target_host": "api.example.com",
                    "timeout": 30,
                },
            }
        }
    )


class ScriptUpdate(BaseModel):
    name: Optional[str] = Field(
        None, min_length=1, max_length=255, description="脚本名称"
    )
    description: Optional[str] = Field(None, description="脚本描述")
    version: Optional[str] = Field(None, max_length=50, description="脚本版本")
    status: Optional[ScriptStatus] = Field(None, description="脚本状态")
    tags: Optional[list[str]] = Field(None, description="标签")
    parameters: Optional[dict[str, Any]] = Field(None, description="参数配置")


class ScriptContentUpdate(BaseModel):
    content: str = Field(..., description="脚本正文")
    task_id: Optional[int] = Field(
        None, gt=0, description="关联任务ID（可选，用于任务级私有脚本保存）"
    )


class CurlToK6ScriptCreate(BaseModel):
    curl_command: str = Field(..., min_length=1, description="原始 CURL 命令")
    name: Optional[str] = Field(
        None, min_length=1, max_length=255, description="脚本名称（可选）"
    )


class OpenApiToK6SpecParseRequest(BaseModel):
    spec_content: str = Field(..., min_length=1, description="OpenAPI JSON/YAML 文本")


class OpenApiToK6ScriptCreate(BaseModel):
    spec_content: str = Field(..., min_length=1, description="OpenAPI JSON/YAML 文本")
    path: str = Field(..., min_length=1, description="选中的 endpoint path")
    method: str = Field(..., min_length=1, description="选中的 HTTP method")
    name: Optional[str] = Field(
        None, min_length=1, max_length=255, description="脚本名称（可选）"
    )
    server_url: Optional[str] = Field(
        None, max_length=500, description="选中的 server URL（可选）"
    )


class HarToK6SpecParseRequest(BaseModel):
    har_content: str = Field(..., min_length=1, description="HAR JSON 文本")


class HarToK6ScriptCreate(BaseModel):
    har_content: str = Field(..., min_length=1, description="HAR JSON 文本")
    entry_index: int = Field(0, ge=0, description="选中的 HAR entry index")
    name: Optional[str] = Field(
        None, min_length=1, max_length=255, description="脚本名称（可选）"
    )


class CurlToK6VariableSuggestion(BaseModel):
    key: str = Field(..., min_length=1, max_length=128, description="建议变量名")
    value: str = Field(..., description="建议默认值")
    sensitive: bool = Field(default=False, description="是否为敏感变量")
    source: Optional[str] = Field(None, description="来源说明")


class CurlToK6FieldItem(BaseModel):
    key: str = Field(..., min_length=1, description="字段名")
    value: str = Field(..., description="字段值")


class ScriptResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    script_type: ScriptType
    file_path: str
    file_size: Optional[int]
    content_hash: Optional[str]
    version: str
    status: ScriptStatus
    tags: Optional[list[str]]
    parameters: Optional[dict[str, Any]]
    created_by: Optional[int]
    created_at: datetime
    updated_at: Optional[datetime]
    last_used_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)


class ScriptListResponse(BaseModel):
    items: list[ScriptResponse]
    total: int
    skip: int
    limit: int


class CurlToK6ParsedRequest(BaseModel):
    method: str
    url: str
    protocol: str = "http"
    connect_timeout_ms: Optional[int] = None
    response_timeout_ms: Optional[int] = None
    suggested_task_name: Optional[str] = None
    query_items: list[CurlToK6FieldItem] = Field(default_factory=list)
    header_items: list[CurlToK6FieldItem] = Field(default_factory=list)
    body_mode: Optional[str] = None
    body_present: bool = False
    body_preview: Optional[str] = None
    body_items: list[CurlToK6FieldItem] = Field(default_factory=list)


class OpenApiToK6EndpointItem(BaseModel):
    method: str
    path: str
    summary: Optional[str] = None
    operation_id: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    request_content_types: list[str] = Field(default_factory=list)
    request_body_supported: bool = True
    server_url: Optional[str] = None


class OpenApiToK6SpecParseResponse(BaseModel):
    title: Optional[str] = None
    version: Optional[str] = None
    server_urls: list[str] = Field(default_factory=list)
    endpoints: list[OpenApiToK6EndpointItem] = Field(default_factory=list)
    supported_endpoint_count: int = Field(default=0, ge=0)
    unsupported_endpoint_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)


class HarToK6EntryItem(BaseModel):
    index: int = Field(..., ge=0)
    method: str
    url: str
    path: str
    status: Optional[int] = None
    mime_type: Optional[str] = None
    body_present: bool = False
    started_at: Optional[str] = None


class HarToK6SpecParseResponse(BaseModel):
    entries: list[HarToK6EntryItem] = Field(default_factory=list)
    supported_entry_count: int = Field(default=0, ge=0)
    unsupported_entry_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)


class OpenApiToK6ParsedRequest(BaseModel):
    title: Optional[str] = None
    version: Optional[str] = None
    method: str
    path: str
    protocol: str = "http"
    server_url: Optional[str] = None
    source_url: str
    summary: Optional[str] = None
    operation_id: Optional[str] = None
    suggested_task_name: Optional[str] = None
    request_content_type: Optional[str] = None
    body_mode: Optional[str] = None
    body_present: bool = False
    path_items: list[CurlToK6FieldItem] = Field(default_factory=list)
    query_items: list[CurlToK6FieldItem] = Field(default_factory=list)
    header_items: list[CurlToK6FieldItem] = Field(default_factory=list)
    body_preview: Optional[str] = None
    body_items: list[CurlToK6FieldItem] = Field(default_factory=list)


class HarToK6ParsedRequest(BaseModel):
    entry_index: int
    method: str
    url: str
    protocol: str = "http"
    status: Optional[int] = None
    mime_type: Optional[str] = None
    suggested_task_name: Optional[str] = None
    query_items: list[CurlToK6FieldItem] = Field(default_factory=list)
    header_items: list[CurlToK6FieldItem] = Field(default_factory=list)
    body_mode: Optional[str] = None
    body_present: bool = False
    body_preview: Optional[str] = None
    body_items: list[CurlToK6FieldItem] = Field(default_factory=list)


class CurlToK6ScriptCreateResponse(BaseModel):
    script: ScriptResponse
    parsed: CurlToK6ParsedRequest
    suggested_variables: list[CurlToK6VariableSuggestion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CurlToK6PreviewResponse(BaseModel):
    parsed: CurlToK6ParsedRequest
    suggested_variables: list[CurlToK6VariableSuggestion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    script_content: str


class OpenApiToK6ScriptCreateResponse(BaseModel):
    script: ScriptResponse
    parsed: OpenApiToK6ParsedRequest
    suggested_variables: list[CurlToK6VariableSuggestion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OpenApiToK6PreviewResponse(BaseModel):
    parsed: OpenApiToK6ParsedRequest
    suggested_variables: list[CurlToK6VariableSuggestion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    script_content: str


class HarToK6ScriptCreateResponse(BaseModel):
    script: ScriptResponse
    parsed: HarToK6ParsedRequest
    suggested_variables: list[CurlToK6VariableSuggestion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class HarToK6PreviewResponse(BaseModel):
    parsed: HarToK6ParsedRequest
    suggested_variables: list[CurlToK6VariableSuggestion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    script_content: str


class AIK6ScriptStaticFinding(BaseModel):
    code: str = Field(..., min_length=1, max_length=128)
    severity: str = Field(..., min_length=1, max_length=32)
    message: str = Field(..., min_length=1)


class AIK6ScriptReviewRiskItem(BaseModel):
    severity: str = Field(..., min_length=1, max_length=32)
    category: str = Field(..., min_length=1, max_length=128)
    label: Optional[str] = Field(None, max_length=128)
    message: str = Field(..., min_length=1)
    recommendation: Optional[str] = None
    source: Optional[str] = Field(None, max_length=128)


class AIK6ScriptReviewRequest(BaseModel):
    script_content: str = Field(..., min_length=1, description="待评审的 K6 脚本草稿")
    source_type: Optional[str] = Field(None, max_length=64)
    source_summary: Optional[str] = Field(None, max_length=2000)


class AIK6ScriptReviewResponse(BaseModel):
    status: str
    prompt_version: str = Field(
        "ai-k6-script-assistant-v1", min_length=1, max_length=128
    )
    profile: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    summary: Optional[str] = None
    risk_level: str = "unknown"
    risks: list[str] = Field(default_factory=list)
    risk_items: list[AIK6ScriptReviewRiskItem] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    suggested_checks: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    static_findings: list[AIK6ScriptStaticFinding] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: Optional[int] = None
    error_message: Optional[str] = None
    disclaimer: str


class AIK6ScriptDraftRequest(BaseModel):
    source_type: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="curl / openapi / har / interface-description",
    )
    source_content: str = Field(..., min_length=1, description="来源文本")
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    path: Optional[str] = Field(None, min_length=1, description="OpenAPI endpoint path")
    method: Optional[str] = Field(None, min_length=1, description="OpenAPI method")
    server_url: Optional[str] = Field(None, max_length=500)
    entry_index: int = Field(0, ge=0, description="HAR entry index")


class AIK6ScriptDraftResponse(BaseModel):
    status: str
    prompt_version: str = Field(
        "ai-k6-script-assistant-v1", min_length=1, max_length=128
    )
    source_type: str
    script_name: Optional[str] = None
    script_content: str = ""
    suggested_variables: list[CurlToK6VariableSuggestion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    parsed_source: Optional[dict[str, Any]] = None
    ai_review: AIK6ScriptReviewResponse
    llm_generated: bool = False
    human_confirmation_required: bool = True
    human_confirmation_status: str = "pending"
    final_script_status: str = "draft_pending_human_review"


class AIK6ScriptReviewHumanFeedback(BaseModel):
    rating: Literal["useful", "not_useful", "needs_changes"]
    note: Optional[str] = Field(None, max_length=2000)
    action: Optional[Literal["accepted", "needs_revision", "ignored"]] = None


class AIK6ScriptConfirmSaveRequest(BaseModel):
    script_name: str = Field(..., min_length=1, max_length=255)
    script_content: str = Field(..., description="人工确认后的 K6 脚本正文")
    source_type: str = Field(..., min_length=1, max_length=64)
    parsed_source: Optional[dict[str, Any]] = None
    ai_review: AIK6ScriptReviewResponse
    human_feedback: Optional[AIK6ScriptReviewHumanFeedback] = None
    llm_generated: bool = False
    warnings: list[str] = Field(default_factory=list)
    suggested_variables: list[CurlToK6VariableSuggestion] = Field(default_factory=list)
    prompt_version: str = Field(
        "ai-k6-script-assistant-v1", min_length=1, max_length=128
    )
    human_confirmation_status: str = Field(..., min_length=1, max_length=64)
    final_script_status: str = Field(..., min_length=1, max_length=64)


__all__ = [
    "ScriptCreate",
    "CurlToK6ScriptCreate",
    "OpenApiToK6SpecParseRequest",
    "OpenApiToK6ScriptCreate",
    "HarToK6SpecParseRequest",
    "HarToK6ScriptCreate",
    "CurlToK6VariableSuggestion",
    "CurlToK6FieldItem",
    "CurlToK6ParsedRequest",
    "OpenApiToK6EndpointItem",
    "OpenApiToK6SpecParseResponse",
    "OpenApiToK6ParsedRequest",
    "HarToK6EntryItem",
    "HarToK6SpecParseResponse",
    "HarToK6ParsedRequest",
    "CurlToK6ScriptCreateResponse",
    "CurlToK6PreviewResponse",
    "OpenApiToK6ScriptCreateResponse",
    "OpenApiToK6PreviewResponse",
    "HarToK6ScriptCreateResponse",
    "HarToK6PreviewResponse",
    "AIK6ScriptStaticFinding",
    "AIK6ScriptReviewRiskItem",
    "AIK6ScriptReviewRequest",
    "AIK6ScriptReviewResponse",
    "AIK6ScriptDraftRequest",
    "AIK6ScriptDraftResponse",
    "AIK6ScriptReviewHumanFeedback",
    "AIK6ScriptConfirmSaveRequest",
    "ScriptUpdate",
    "ScriptContentUpdate",
    "ScriptResponse",
    "ScriptListResponse",
    "ScriptType",
    "ScriptStatus",
]
