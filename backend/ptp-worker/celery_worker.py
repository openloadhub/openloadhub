#!/usr/bin/env python3
"""
Celery Worker 启动脚本

ptp-worker 使用与 ptp-admin 相同的代码库，但通过不同的入口点启动：
- ptp-admin: uvicorn main:app (启动 FastAPI)
- ptp-worker: celery -A app.core.celery_app worker (启动 Celery Worker)
"""

import os
import sys
from pathlib import Path

from celery.signals import worker_process_init

BACKEND_DIR = Path(__file__).resolve().parent.parent

# 添加 ptp-admin 的 app 目录到 Python 路径
PTP_ADMIN_DIR = BACKEND_DIR / "ptp-admin"

# 确保 Python 可以找到 ptp-admin 下的 app 包
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(PTP_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(PTP_ADMIN_DIR))

# 设置工作目录为 ptp-admin 目录
os.chdir(PTP_ADMIN_DIR)

from common.config.settings import ensure_host_runtime_safe, settings


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _worker_queues() -> str:
    from app.core.celery_app import CONTROL_QUEUE

    queues = ["celery", CONTROL_QUEUE]
    env_queues = os.getenv("PTP_WORKER_QUEUES", "")
    for queue in env_queues.split(","):
        queue = queue.strip()
        if queue and queue not in queues:
            queues.append(queue)
    return ",".join(queues)


def build_worker_argv() -> list[str]:
    argv = [
        "worker",
        f"--loglevel={os.getenv('PTP_WORKER_LOGLEVEL', 'info')}",
        f"--concurrency={os.getenv('PTP_WORKER_CONCURRENCY', '4')}",
        "--prefetch-multiplier=1",
        f"--queues={_worker_queues()}",
    ]
    if _env_flag("PTP_WORKER_ENABLE_BEAT", "1"):
        argv.extend(
            [
                "--beat",
                f"--schedule={os.getenv('PTP_BEAT_SCHEDULE_FILE', '/tmp/ptp-celerybeat-schedule')}",
            ]
        )
    return argv


def _init_worker_nacos() -> None:
    from app.core.nacos_client import init_nacos_client

    try:
        init_nacos_client(
            server_addresses=settings.NACOS_SERVER,
            namespace=settings.NACOS_NAMESPACE,
            username=settings.NACOS_USERNAME,
            password=settings.NACOS_PASSWORD,
        )
    except Exception as exc:
        print(f"[ptp-worker] init nacos client failed: {exc}", file=sys.stderr)


@worker_process_init.connect
def _on_worker_process_init(*args, **kwargs):
    _init_worker_nacos()


if __name__ == "__main__":
    if not _env_flag("TESTING"):
        ensure_host_runtime_safe(service_name="ptp-worker")

    # 导入 Celery 应用实例（从 ptp-admin）
    from app.core.celery_app import celery_app

    _init_worker_nacos()

    # 默认带 embedded beat，避免 fixed/cron 在 mixed-mode 下无人扫描。
    celery_app.worker_main(build_worker_argv())
