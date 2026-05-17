import os
import sys
from time import perf_counter

from celery import Celery
from celery.signals import task_failure, task_postrun, task_prerun
from app.core.config import settings
from app.services.self_apm_service import SelfApmService
from common.config.settings import ensure_host_runtime_safe

CONTROL_QUEUE = "control"
CONTROL_TASK_NAMES = (
    "execute_run_k6_control_task",
    "execute_plan_run_k6_control_task",
)


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _is_direct_celery_worker_invocation(argv: list[str] | None = None) -> bool:
    args = [item.strip() for item in (argv or sys.argv) if item and item.strip()]
    if not args:
        return False

    executable_path = args[0].replace("\\", "/")
    executable = os.path.basename(executable_path).lower()
    command_start = 1
    if "celery" in executable:
        command_start = 1
    elif executable_path.lower().endswith("/celery/__main__.py"):
        command_start = 1
    elif (
        "python" in executable
        and len(args) >= 3
        and args[1] == "-m"
        and args[2].lower() == "celery"
    ):
        command_start = 3
    else:
        return False

    command_tokens = {
        item.lower() for item in args[command_start:] if not item.startswith("-")
    }
    return "worker" in command_tokens


def _ensure_direct_worker_host_runtime_safe() -> None:
    if _env_flag("TESTING"):
        return
    if not _is_direct_celery_worker_invocation():
        return
    ensure_host_runtime_safe(service_name="ptp-worker (direct celery worker)")


_ensure_direct_worker_host_runtime_safe()

# 创建 Celery 应用实例
celery_app = Celery(
    "ptp-worker",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.test_executor",
        "app.tasks.report_generator",
        "app.tasks.plan_executor",
    ],
)

# 配置 Celery
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_routes={
        task_name: {"queue": CONTROL_QUEUE} for task_name in CONTROL_TASK_NAMES
    },
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    beat_schedule={
        "scan-scheduled-plans": {
            "task": "scan_scheduled_plans_task",
            "schedule": max(
                10,
                int(os.getenv("PLAN_SCHEDULER_SCAN_INTERVAL_SECONDS", "10")),
            ),
        },
        "timeout-governance-watchdog": {
            "task": "timeout_governance_watchdog_task",
            "schedule": max(
                15,
                int(os.getenv("TIMEOUT_GOVERNANCE_SCAN_INTERVAL_SECONDS", "30")),
            ),
        },
    },
)


@task_prerun.connect
def _record_self_apm_task_started(
    task_id=None,
    task=None,
    **kwargs,
):
    del task_id, kwargs
    if task is not None and getattr(task, "request", None) is not None:
        task.request.ptp_self_apm_started_at = perf_counter()


@task_postrun.connect
def _record_self_apm_task_finished(
    task_id=None,
    task=None,
    state=None,
    **kwargs,
):
    del kwargs
    if task is None:
        return
    request = getattr(task, "request", None)
    started_at = getattr(request, "ptp_self_apm_started_at", None)
    duration_ms = (perf_counter() - started_at) * 1000 if started_at else 0.0
    SelfApmService.record_task(
        task_name=getattr(task, "name", None) or task.__class__.__name__,
        task_id=task_id,
        status=state or "UNKNOWN",
        duration_ms=duration_ms,
        error=getattr(request, "ptp_self_apm_error", None),
    )


@task_failure.connect
def _record_self_apm_task_failure(
    task_id=None,
    exception=None,
    sender=None,
    **kwargs,
):
    del kwargs
    request = getattr(sender, "request", None) if sender is not None else None
    if request is not None:
        request.ptp_self_apm_error = str(exception) if exception is not None else None


# 自动发现任务
celery_app.autodiscover_tasks()
