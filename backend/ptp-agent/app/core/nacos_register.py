import asyncio
import logging
import os
import socket
from typing import Optional

try:
    import nacos  # type: ignore
except Exception:  # pragma: no cover - nacos 非强制
    nacos = None

from common.config.settings import build_runtime_metadata, settings

logger = logging.getLogger(__name__)


def _get_host_ip() -> str:
    explicit = os.getenv("AGENT_IP")
    if explicit:
        return explicit
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


def _env_flag_enabled(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def detect_agent_runtime_kind() -> str:
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return "k8s"
    if os.getenv("COMPOSE_SERVICE"):
        return "docker"
    return "host"


def build_agent_runtime_metadata() -> dict[str, str]:
    metadata = build_runtime_metadata(service_name="ptp-agent")
    metadata["runtime_kind"] = detect_agent_runtime_kind()
    compose_service = str(os.getenv("COMPOSE_SERVICE", "")).strip()
    if compose_service:
        metadata["compose_service"] = compose_service
    nacos_service_name = str(os.getenv("NACOS_SERVICE_NAME", "")).strip()
    if nacos_service_name:
        metadata["service_name"] = nacos_service_name
    return metadata


class AgentNacosRegister:
    """
    负责在 agent 启动时向 Nacos 注册，并定时心跳；关闭时自动下线。
    默认尝试向 Nacos 注册，并定期心跳；只有显式关闭时才跳过。
    """

    def __init__(self):
        self.client: Optional["nacos.NacosClient"] = None  # type: ignore[name-defined]
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self.registered = False
        self.ip = _get_host_ip()
        self.port = int(os.getenv("AGENT_PORT", os.getenv("PORT", "9096")))
        self.service_name = os.getenv("NACOS_SERVICE_NAME", "ptp-agent")
        self.group_name = os.getenv("NACOS_GROUP", "DEFAULT_GROUP")
        self.cluster_name = os.getenv("NACOS_CLUSTER", "DEFAULT")
        self.heartbeat_interval = int(os.getenv("NACOS_HEARTBEAT_INTERVAL", "5"))
        self.register_retry_attempts = int(
            os.getenv("NACOS_REGISTER_RETRY_ATTEMPTS", "12")
        )
        self.register_retry_interval = int(
            os.getenv("NACOS_REGISTER_RETRY_INTERVAL_SECONDS", "5")
        )

    async def start(self) -> bool:
        if not _env_flag_enabled("ENABLE_NACOS", default=True):
            logger.info("ENABLE_NACOS disabled，跳过 Nacos 注册")
            return False
        if nacos is None:
            logger.warning("nacos-sdk-python 未安装，无法注册到 Nacos，建议 pip install nacos-sdk-python")
            return False
        if self.client is None:
            self.client = nacos.NacosClient(
                settings.NACOS_SERVER,
                namespace=settings.NACOS_NAMESPACE,
                username=settings.NACOS_USERNAME,
                password=settings.NACOS_PASSWORD,
            )

        metadata = build_agent_runtime_metadata()
        last_error: Optional[Exception] = None
        total_attempts = max(self.register_retry_attempts, 1)

        for attempt in range(1, total_attempts + 1):
            try:
                self.client.add_naming_instance(
                    service_name=self.service_name,
                    ip=self.ip,
                    port=self.port,
                    group_name=self.group_name,
                    cluster_name=self.cluster_name,
                    metadata=metadata,
                    ephemeral=True,
                    heartbeat_interval=self.heartbeat_interval,
                )
                self.registered = True
                self._stop_event = asyncio.Event()
                self._task = asyncio.create_task(self._heartbeat_loop(metadata))
                logger.info(
                    "Nacos 注册成功: %s:%s (%s/%s) -> %s",
                    self.ip,
                    self.port,
                    self.group_name,
                    self.cluster_name,
                    settings.NACOS_SERVER,
                )
                return True
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Nacos 注册失败（第 %s/%s 次），%ss 后重试: %s",
                    attempt,
                    total_attempts,
                    self.register_retry_interval,
                    exc,
                )
                if attempt >= total_attempts:
                    break
                await asyncio.sleep(max(self.register_retry_interval, 1))

        logger.warning("Nacos 注册失败，回退本地: %s", last_error)
        return False

    async def _heartbeat_loop(self, metadata: dict):
        assert self.client is not None
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                self.client.send_heartbeat(
                    service_name=self.service_name,
                    ip=self.ip,
                    port=self.port,
                    group_name=self.group_name,
                    cluster_name=self.cluster_name,
                    metadata=metadata,
                )
            except Exception as exc:  # pragma: no cover - 容错
                logger.debug("Nacos 心跳失败: %s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.heartbeat_interval)
            except asyncio.TimeoutError:
                continue

    async def stop(self):
        if self._stop_event:
            self._stop_event.set()
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.client and self.registered:
            try:
                self.client.remove_naming_instance(
                    service_name=self.service_name,
                    ip=self.ip,
                    port=self.port,
                    group_name=self.group_name,
                    cluster_name=self.cluster_name,
                )
                logger.info("Nacos 下线成功: %s:%s", self.ip, self.port)
            except Exception as exc:  # pragma: no cover - 容错
                logger.debug("Nacos 下线失败: %s", exc)
