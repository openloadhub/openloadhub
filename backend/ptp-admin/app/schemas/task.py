from pathlib import Path
import sys

COMMON_PARENT = Path(__file__).resolve().parents[3]
if COMMON_PARENT.exists():
    sys.path.append(str(COMMON_PARENT))

from common.schemas.task import (  # type: ignore F401
    TaskBatchStopRequest,
    TaskBatchStopResponse,
    TaskCreate,
    TaskLastRunParamsResponse,
    TaskListResponse,
    TaskPodCapacityItem,
    TaskResourcePoolSummary,
    TaskPrepareRunResponse,
    TaskResponse,
    ScenarioQualityLint,
    ScenarioQualityLintIssue,
    TaskScriptCompareResponse,
    TaskScriptVersionContent,
    TaskSummaryResponse,
    TaskVersionDetailResponse,
    EngineType,
    Protocol,
    TaskPattern,
    TaskStatus,
    TaskUpdate,
    TaskVersionRecordResponse,
)

__all__ = [
    "TaskBatchStopRequest",
    "TaskBatchStopResponse",
    "TaskCreate",
    "TaskLastRunParamsResponse",
    "TaskListResponse",
    "TaskPodCapacityItem",
    "TaskResourcePoolSummary",
    "TaskPrepareRunResponse",
    "TaskResponse",
    "ScenarioQualityLint",
    "ScenarioQualityLintIssue",
    "TaskScriptCompareResponse",
    "TaskScriptVersionContent",
    "TaskSummaryResponse",
    "TaskVersionRecordResponse",
    "TaskVersionDetailResponse",
    "EngineType",
    "Protocol",
    "TaskPattern",
    "TaskStatus",
    "TaskUpdate",
]
