import enum


class TaskStatus(enum.Enum):
    DRAFT = "draft"
    READY = "ready"
    SCRIPT_MISSING = "script_missing"
    # 历史审批流状态仅为兼容旧数据保留，不再属于主应用前门状态机。
    PENDING_SUBMIT_APPROVAL = "pending_submit_approval"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVAL_REJECTED = "approval_rejected"
    RUNNING = "running"


APPROVAL_COMPATIBILITY_TASK_STATUSES = frozenset(
    {
        TaskStatus.PENDING_SUBMIT_APPROVAL,
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.APPROVAL_REJECTED,
    }
)


class EngineType(enum.Enum):
    JMETER = "jmeter"
    K6 = "k6"
    CUSTOM = "custom"


class TaskPattern(enum.Enum):
    SCRIPT = "script"
    VISUALIZATION = "visualization"


class Protocol(enum.Enum):
    HTTP = "http"
    GRPC = "grpc"
    KAFKA = "kafka"
    WEBSOCKET = "websocket"
    BROWSER = "browser"
    OTHER = "other"


class RunStatus(enum.Enum):
    PREPARING = "preparing"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"


class RunBaselineScopeType(enum.Enum):
    TASK_ENV = "task_env"
    TASK_ENV_PROTOCOL = "task_env_protocol"


class RunBaselineSource(enum.Enum):
    MANUAL = "manual"
    AUTO_LATEST_GREEN = "auto_latest_green"


class PlanStatus(enum.Enum):
    READY = "ready"
    SUSPENDED = "suspended"
    RUNNING = "running"
    DELETED = "deleted"


class PlanExecType(enum.Enum):
    MANUAL = "manual"
    CRON = "cron"
    FIXED = "fixed"


class PlanStageItemType(enum.Enum):
    TASK = "task"
    POSTPROCESSOR = "postprocessor"


class PlanRunStatus(enum.Enum):
    PREPARING = "preparing"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"
    SUSPENDED = "suspended"
    DUPLICATED = "duplicated"


class ScriptType(enum.Enum):
    JMETER = "JMETER"
    K6 = "K6"


class ScriptStatus(enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    DELETED = "DELETED"


class ApprovalStatus(enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class ApprovalAction(enum.Enum):
    SUBMIT = "SUBMIT"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    CANCEL = "CANCEL"


class ReportType(enum.Enum):
    JMETER = "JMETER"
    K6 = "K6"
    COMPARISON = "COMPARISON"


class ReportStatus(enum.Enum):
    PENDING = "PENDING"
    GENERATING = "GENERATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DELETED = "DELETED"
