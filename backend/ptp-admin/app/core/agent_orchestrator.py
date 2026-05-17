"""
Agent 调度器

负责：
1. 从 Nacos 获取可用的 agent 实例
2. 负载均衡选择合适的 agent
3. 通过 HTTP 分发测试任务
4. 监控任务执行状态
"""

import httpx
import asyncio
import logging
import math
import os
import re
import subprocess
import shutil
import signal
import socket
import time
import json
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from typing import List, Dict, Optional
from datetime import datetime, timedelta, timezone

from app.core.nacos_client import get_nacos_client
from common.config.settings import get_run_artifact_prefix
from common.utils import s3_utils

logger = logging.getLogger(__name__)

K8S_LABEL_PREFIX = "ptp.io"
K8S_LABEL_VALUE_MAX_LENGTH = 63
K8S_LABEL_VALUE_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")
K8S_CLUSTER_KILL_STRONG_SCOPE_LABELS = {
    f"{K8S_LABEL_PREFIX}/mixed-run-id",
    f"{K8S_LABEL_PREFIX}/execution-session-id",
    f"{K8S_LABEL_PREFIX}/run-id",
    f"{K8S_LABEL_PREFIX}/job-name",
}


class AgentInstance:
    """Agent 实例信息"""

    def __init__(self, instance: Dict):
        self.ip = instance.get("ip")
        self.port = instance.get("port")
        self.host = f"{self.ip}:{self.port}"
        self.weight = float(instance.get("weight", 1) or 1)
        metadata = instance.get("metadata", {})
        self.metadata = dict(metadata) if isinstance(metadata, dict) else {}
        self.last_seen = datetime.now(timezone.utc)
        self.last_health_check_at: Optional[datetime] = None
        self.last_health_latency_ms: Optional[float] = None
        self.last_health_error: Optional[str] = None
        self.service: Optional[str] = None
        self.version: Optional[str] = None

    @property
    def url(self) -> str:
        return f"http://{self.host}"

    def is_healthy(self) -> bool:
        """检查 agent 是否健康"""
        if os.getenv("TESTING", "0") == "1":
            self.last_health_check_at = datetime.now(timezone.utc)
            self.last_health_latency_ms = 0.0
            self.service = self.service or "testing"
            self.version = self.version or "testing"
            return True
        url = f"{self.url}/health"
        timeout = float(os.getenv("AGENT_HEALTH_TIMEOUT_SECONDS", "3"))
        started = time.perf_counter()
        self.last_health_check_at = datetime.now(timezone.utc)
        self.last_health_error = None

        try:
            with urlopen(url, timeout=timeout) as response:  # nosec B310
                payload = json.load(response)
            self.last_health_latency_ms = round(
                (time.perf_counter() - started) * 1000, 1
            )
            self.service = payload.get("service")
            self.version = payload.get("version")
            payload_metadata = payload.get("metadata")
            if isinstance(payload_metadata, dict):
                self.metadata.update(
                    {
                        str(key): str(value)
                        for key, value in payload_metadata.items()
                        if value not in (None, "")
                    }
                )
            runtime_kind = payload.get("runtime_kind")
            if isinstance(runtime_kind, str) and runtime_kind.strip():
                self.metadata["runtime_kind"] = runtime_kind.strip()
            compose_service = payload.get("compose_service")
            if isinstance(compose_service, str) and compose_service.strip():
                self.metadata["compose_service"] = compose_service.strip()
            healthy = payload.get("status") == "ok"
            if not healthy:
                self.last_health_error = f"unexpected_status={payload.get('status')}"
            return healthy
        except (HTTPError, URLError, TimeoutError, ValueError, socket.timeout) as exc:
            self.last_health_latency_ms = round(
                (time.perf_counter() - started) * 1000, 1
            )
            self.last_health_error = str(exc)
            logger.debug("Agent health probe failed for %s: %s", self.host, exc)
            return False

    def effective_weight(self) -> int:
        """将外部 weight 收敛为当前阶段可解释的加权轮询整数权重。"""
        try:
            return max(1, int(round(self.weight)))
        except (TypeError, ValueError):
            return 1

    def capacity_slots(self) -> Optional[int]:
        """读取 agent 明确暴露的容量槽位；缺失时交给调用方回退 weight。"""
        for key in (
            "capacity",
            "capacity_slots",
            "agent_capacity",
            "max_capacity",
            "max_concurrent",
            "concurrency",
            "pod_capacity",
        ):
            raw_value = self.metadata.get(key)
            if raw_value in (None, ""):
                continue
            try:
                parsed = int(float(raw_value))
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        return None


class AgentOrchestrator:
    """Agent 调度器"""

    def __init__(self):
        self.http_client = httpx.AsyncClient(timeout=30.0, trust_env=False)
        self._agent_cache: List[AgentInstance] = []
        self._last_discovery = None
        self.k8s_namespace = os.getenv("K8S_NAMESPACE", "default")

    @staticmethod
    def _agent_health_stale_grace_seconds() -> float:
        try:
            return max(0.0, float(os.getenv("AGENT_HEALTH_STALE_GRACE_SECONDS", "15")))
        except (TypeError, ValueError):
            return 15.0

    def _recent_cached_agents(
        self,
        cached_agents: List[AgentInstance],
        last_discovery: Optional[datetime],
        now: datetime,
    ) -> List[AgentInstance]:
        grace_seconds = self._agent_health_stale_grace_seconds()
        if not cached_agents or last_discovery is None or grace_seconds <= 0:
            return []
        if now - last_discovery > timedelta(seconds=grace_seconds):
            return []
        age_seconds = round((now - last_discovery).total_seconds(), 1)
        logger.warning(
            "Agent discovery returned 0 healthy agents; reusing %d cached agent(s) discovered %.1fs ago within %.1fs grace window",
            len(cached_agents),
            age_seconds,
            grace_seconds,
        )
        return cached_agents

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        # execute_task / stop_run 也会在 Celery prefork 下反复创建/关闭 event loop。
        # 复用绑定到旧 loop 的 AsyncClient 会在 live Docker 场景炸 "Event loop is closed"。
        # 这条控制面链路吞吐很低，直接按请求创建短命 client，优先稳定性。
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as fresh:
            return await fresh.request(method, url, **kwargs)

    async def _fresh_request(self, method: str, url: str, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as fresh:
            return await fresh.request(method, url, **kwargs)

    @staticmethod
    def _format_http_exception(exc: Exception) -> str:
        text = str(exc).strip()
        if text:
            return f"{type(exc).__name__}: {text}"
        return type(exc).__name__

    @staticmethod
    def _sanitize_k8s_label_value(value: object) -> Optional[str]:
        if value is None:
            return None
        sanitized = K8S_LABEL_VALUE_PATTERN.sub("-", str(value).strip())
        sanitized = sanitized.strip("-_.")
        if not sanitized:
            return None
        sanitized = sanitized[:K8S_LABEL_VALUE_MAX_LENGTH].strip("-_.")
        return sanitized or None

    def _build_k8s_job_labels(self, task_id: int, task_data: Dict) -> Dict[str, str]:
        mixed_run_id = task_data.get("mixed_run_id") or task_data.get("plan_run_id")
        execution_session_id = (
            task_data.get("execution_session_id")
            or task_data.get("executionSessionId")
            or (f"planrun-{mixed_run_id}" if mixed_run_id else None)
        )
        raw_labels = {
            "app.kubernetes.io/name": "ptp-agent",
            "app.kubernetes.io/managed-by": "ptp-admin",
            f"{K8S_LABEL_PREFIX}/task-id": task_id,
            f"{K8S_LABEL_PREFIX}/mixed-run-id": mixed_run_id,
            f"{K8S_LABEL_PREFIX}/run-id": task_data.get("run_id"),
            f"{K8S_LABEL_PREFIX}/execution-session-id": execution_session_id,
            f"{K8S_LABEL_PREFIX}/env": task_data.get("env")
            or task_data.get("environment")
            or os.getenv("PTP_ENV"),
        }
        labels: Dict[str, str] = {}
        for key, value in raw_labels.items():
            sanitized = self._sanitize_k8s_label_value(value)
            if sanitized:
                labels[key] = sanitized
        return labels

    def build_k8s_cluster_kill_dry_run(self, meta: dict) -> Optional[dict]:
        """Build a non-destructive kubectl delete preview for matching K8S jobs/pods."""
        labels = meta.get("labels") if isinstance(meta, dict) else None
        if not isinstance(labels, dict):
            return None
        selector_labels: Dict[str, str] = {}
        for key, value in labels.items():
            if not key.startswith(f"{K8S_LABEL_PREFIX}/"):
                continue
            sanitized = self._sanitize_k8s_label_value(value)
            if sanitized:
                selector_labels[key] = sanitized
        if not selector_labels:
            return None
        if not any(
            key in K8S_CLUSTER_KILL_STRONG_SCOPE_LABELS for key in selector_labels
        ):
            return None
        selector = ",".join(
            f"{key}={value}" for key, value in sorted(selector_labels.items())
        )
        namespace = meta.get("namespace", self.k8s_namespace)
        command = [
            "kubectl",
            "delete",
            "job,pod",
            "-l",
            selector,
            "-n",
            namespace,
            "--ignore-not-found=true",
            "--dry-run=server",
        ]
        return {
            "dry_run": True,
            "selector": selector,
            "labels": selector_labels,
            "namespace": namespace,
            "command": command,
        }

    def build_k8s_cleanup_dry_run(self, meta: dict) -> Optional[dict]:
        """Build a non-destructive cleanup preview for one scoped K8S agent job."""
        if not isinstance(meta, dict):
            return None
        job_name = meta.get("job_name")
        if not isinstance(job_name, str) or not job_name.strip():
            return None
        labels = meta.get("labels")
        if not isinstance(labels, dict):
            return None
        selector_labels: Dict[str, str] = {}
        for key, value in labels.items():
            if not isinstance(key, str) or not key.startswith(f"{K8S_LABEL_PREFIX}/"):
                continue
            sanitized = self._sanitize_k8s_label_value(value)
            if sanitized:
                selector_labels[key] = sanitized
        if not any(
            key in K8S_CLUSTER_KILL_STRONG_SCOPE_LABELS for key in selector_labels
        ):
            return None
        namespace = meta.get("namespace", self.k8s_namespace)
        service_name = meta.get("service_name") or f"{job_name.strip()}-svc"
        items = []
        commands = []
        for kind, name in (("job", job_name.strip()), ("service", service_name)):
            command = [
                "kubectl",
                "delete",
                kind,
                name,
                "-n",
                namespace,
                "--ignore-not-found=true",
                "--dry-run=server",
            ]
            items.append({"kind": kind, "name": name, "command": command})
            commands.append(command)
        return {
            "dry_run": True,
            "namespace": namespace,
            "job_name": job_name.strip(),
            "service_name": service_name,
            "labels": selector_labels,
            "items": items,
            "commands": commands,
        }

    @staticmethod
    def _extract_response_error_detail(
        response: Optional[httpx.Response],
    ) -> Optional[str]:
        if response is None:
            return None
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()
            data = payload.get("data")
            if isinstance(data, dict):
                nested_detail = data.get("detail")
                if isinstance(nested_detail, str) and nested_detail.strip():
                    return nested_detail.strip()
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        try:
            text = response.text.strip()
        except Exception:
            text = ""
        return text or None

    async def discover_agents(self) -> List[AgentInstance]:
        """
        从 Nacos 发现可用的 agent 实例
        """
        try:
            previous_cache = list(self._agent_cache)
            previous_discovery = self._last_discovery
            static_hosts = os.getenv("AGENT_HOSTS")
            static_agents: List[AgentInstance] = []
            if static_hosts:
                hosts = [h.strip() for h in static_hosts.split(",") if h.strip()]
                static_agents = [
                    AgentInstance(
                        {
                            "ip": h.split(":")[0],
                            "port": int(h.split(":")[1]) if ":" in h else 9096,
                            "weight": 1,
                        }
                    )
                    for h in hosts
                ]

            # 优先尝试 Nacos
            try:
                nacos_client = get_nacos_client()
            except Exception as exc:  # pragma: no cover - 容错
                logger.warning("Agent discovery failed to init nacos client: %s", exc)
                nacos_client = None
            has_real_discovery = bool(
                nacos_client and getattr(nacos_client, "client", None) is not None
            )
            agents: List[AgentInstance] = []
            if nacos_client:
                try:
                    instances = nacos_client.get_service_instances("ptp-agent")
                    agents = [
                        AgentInstance(instance)
                        for instance in instances
                        if instance.get("healthy", True)
                        and instance.get("enabled", True)
                    ]
                except Exception as exc:  # pragma: no cover - 容错
                    logger.warning("Nacos discovery failed: %s", exc)

            # 仅当 discovery client 不可用时才回退静态配置。
            # 若 client 已启用但未发现实例，应暴露空池而不是静态冒充已发现实例。
            if not agents and not has_real_discovery:
                if static_agents:
                    agents = static_agents

            now = datetime.now(timezone.utc)
            healthy_agents = [agent for agent in agents if agent.is_healthy()]
            if healthy_agents:
                self._agent_cache = healthy_agents
                self._last_discovery = now
                logger.info("Discovered %d healthy agents", len(self._agent_cache))
                return self._agent_cache

            fallback_agents = self._recent_cached_agents(
                previous_cache,
                previous_discovery,
                now,
            )
            self._agent_cache = fallback_agents
            logger.info("Discovered %d healthy agents", len(self._agent_cache))
            return self._agent_cache

        except Exception as e:
            logger.error(f"Failed to discover agents: {e}")
            return self._agent_cache

    async def select_agent(self, task_id: int) -> Optional[AgentInstance]:
        """
        选择合适的 agent（负载均衡）
        """
        agents = await self.select_agents(task_id, 1)
        return agents[0]

    @staticmethod
    def _coerce_non_negative_int(value: object) -> Optional[int]:
        if value in (None, "") or isinstance(value, bool):
            return None
        try:
            if isinstance(value, float) and math.isnan(value):
                return None
            parsed = int(float(value))
        except (TypeError, ValueError):
            return None
        return max(0, parsed)

    @classmethod
    def _capacity_context_for_host(
        cls, capacity_context: Optional[Dict], host: str
    ) -> Dict:
        if not isinstance(capacity_context, dict):
            return {}

        per_host = capacity_context.get(host)
        if isinstance(per_host, dict):
            return per_host

        hosts = capacity_context.get("hosts")
        if isinstance(hosts, dict) and isinstance(hosts.get(host), dict):
            return hosts[host]

        context: Dict[str, object] = {}
        for source_key, target_key in (
            ("in_use_by_host", "in_use"),
            ("capacity_by_host", "capacity"),
            ("health_by_host", "healthy"),
            ("feedback_penalty_by_host", "feedback_penalty"),
        ):
            source = capacity_context.get(source_key)
            if isinstance(source, dict) and host in source:
                context[target_key] = source[host]
        return context

    def _capacity_ranked_agents(
        self,
        fallback_order: List[AgentInstance],
        capacity_context: Optional[Dict],
    ) -> Optional[List[AgentInstance]]:
        ranked: list[tuple[int, float, int, int, int, AgentInstance]] = []
        saw_capacity_signal = False

        for fallback_rank, agent in enumerate(fallback_order):
            host_context = self._capacity_context_for_host(capacity_context, agent.host)
            healthy_value = host_context.get("healthy")
            if healthy_value is False or str(healthy_value).lower() == "false":
                continue

            explicit_capacity = self._coerce_non_negative_int(
                host_context.get("capacity")
            )
            metadata_capacity = agent.capacity_slots()
            capacity = (
                explicit_capacity or metadata_capacity or agent.effective_weight()
            )
            capacity = max(1, capacity)

            raw_in_use = host_context.get("in_use")
            if raw_in_use is None:
                raw_in_use = agent.metadata.get("in_use")
            in_use = self._coerce_non_negative_int(raw_in_use)

            if (
                explicit_capacity is not None
                or metadata_capacity is not None
                or in_use is not None
            ):
                saw_capacity_signal = True

            normalized_in_use = in_use if in_use is not None else 0
            available = max(capacity - normalized_in_use, 0)
            utilization = normalized_in_use / capacity
            feedback_penalty = self._coerce_non_negative_int(
                host_context.get("feedback_penalty")
            )
            ranked.append(
                (
                    -available,
                    utilization,
                    -capacity,
                    feedback_penalty if feedback_penalty is not None else 0,
                    fallback_rank,
                    agent,
                )
            )

        if not saw_capacity_signal or not ranked:
            return None

        ranked.sort(key=lambda item: item[:5])
        return [item[5] for item in ranked]

    async def select_agents(
        self,
        task_id: int,
        count: int,
        capacity_context: Optional[Dict] = None,
    ) -> List[AgentInstance]:
        """
        选择多个 agent（容量信号优先；缺失时保持加权轮询 deterministic fallback）
        """
        agents = await self.discover_agents()
        if not agents:
            raise ValueError("No healthy agents available")

        desired = max(1, int(count or 1))
        weighted_agents: List[AgentInstance] = []
        for agent in agents:
            weighted_agents.extend([agent] * agent.effective_weight())
        if not weighted_agents:
            raise ValueError("No healthy agents available")

        fallback_order: List[AgentInstance] = []
        seen_hosts: set[str] = set()
        start_index = task_id % len(weighted_agents)
        for offset in range(len(weighted_agents)):
            agent = weighted_agents[(start_index + offset) % len(weighted_agents)]
            if agent.host in seen_hosts:
                continue
            fallback_order.append(agent)
            seen_hosts.add(agent.host)

        ordered_agents = self._capacity_ranked_agents(
            fallback_order,
            capacity_context,
        )
        if ordered_agents is None:
            ordered_agents = fallback_order

        selected = ordered_agents[:desired]

        if not selected:
            raise ValueError("No healthy agents available")

        logger.info(
            "Selected %d agent(s) for task %s: %s",
            len(selected),
            task_id,
            ",".join(agent.host for agent in selected),
        )
        return selected

    async def execute_task(
        self, task_id: int, agent: AgentInstance, task_data: Dict
    ) -> Dict:
        """
        在指定 agent 上执行测试任务
        """
        if os.getenv("TESTING", "0") == "1":
            return {
                "status": "success",
                "agent": agent.host,
                "agent_host": agent.host,
                "run_token": f"testing-token-{task_id}",
                "response": {
                    "run_token": f"testing-token-{task_id}",
                    "message": "mocked test success",
                },
                "k8s_job": (
                    {"job_name": f"mock-job-{task_id}"}
                    if os.getenv("USE_K8S_AGENT") == "1"
                    else None
                ),
            }

        try:
            # 可选：在 K8S 启动 agent Job（需要预置 kubectl 权限与镜像），用于临时 agent 调度。
            job_meta = None
            if os.getenv("USE_K8S_AGENT") == "1":
                job_host, job_meta = self._launch_k8s_job(task_id, task_data)
                if job_host:
                    agent.host = job_host
                    logger.info("K8S Job 提供临时 agent host: %s", job_host)

            url = f"{agent.url}/agent/execute"

            # 构造执行请求
            request_data = {
                "task_id": task_id,
                "script_id": task_data.get("script_id"),
                "engine_type": task_data.get("engine_type"),
                "pod_count": task_data.get("pod_count"),
                "pod_num": task_data.get("pod_num"),
                "thread_count": task_data.get("thread_count"),
                "duration": task_data.get("duration"),
                "ramp_up": task_data.get("ramp_up", 0),
                "protocol": task_data.get("protocol"),
                "properties": task_data.get("properties"),
                "run_id": task_data.get("run_id"),
            }
            # 透传脚本路径/存储地址，供 agent 在真实模式下执行；缺省由 agent 回退模拟
            if task_data.get("script_path"):
                request_data["script_path"] = task_data["script_path"]
            if task_data.get("script_s3"):
                request_data["script_s3"] = task_data["script_s3"]
            if task_data.get("script_content") is not None:
                request_data["script_content"] = task_data["script_content"]
            if task_data.get("script_file_name"):
                request_data["script_file_name"] = task_data["script_file_name"]
            if task_data.get("data_asset_manifest"):
                request_data["data_asset_manifest"] = task_data["data_asset_manifest"]
            if task_data.get("proto_asset_manifest"):
                request_data["proto_asset_manifest"] = task_data["proto_asset_manifest"]
            if task_data.get("data_distribution"):
                request_data["data_distribution"] = task_data["data_distribution"]

            logger.info(f"Dispatching task {task_id} to {url}")

            # K8S agent Pod 通过 readiness probe 后 HTTP server 仍可能短暂未就绪，加重试保护
            # K8S 模式下每次 port-forward 建立新隧道，复用共享连接池会命中死连接（RemoteProtocolError）
            # 故 K8S 模式下用独立短命 client，非 K8S 模式继续复用共享池以减少开销
            max_retries = (
                int(os.getenv("AGENT_EXECUTE_RETRIES", "3"))
                if os.getenv("USE_K8S_AGENT") == "1"
                else 1
            )
            use_fresh_client = os.getenv("USE_K8S_AGENT") == "1"
            last_exc: Exception = RuntimeError("no attempts made")
            for attempt in range(1, max_retries + 1):
                try:
                    if use_fresh_client:
                        async with httpx.AsyncClient(
                            timeout=30.0, trust_env=False
                        ) as fresh:
                            response = await fresh.post(url, json=request_data)
                    else:
                        response = await self._request("POST", url, json=request_data)
                    response.raise_for_status()
                    break
                except (
                    httpx.RemoteProtocolError,
                    httpx.ConnectError,
                    httpx.ReadError,
                ) as e:
                    last_exc = e
                    if attempt < max_retries:
                        wait = 2**attempt
                        logger.warning(
                            "execute_task attempt %d/%d failed (%s), retrying in %ds...",
                            attempt,
                            max_retries,
                            e,
                            wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise
            else:
                raise last_exc

            result = response.json()
            logger.info(f"Task {task_id} dispatched successfully: {result}")

            return {
                "status": "success",
                "agent": agent.host,
                "agent_host": agent.host,
                "run_token": result.get("run_token"),
                "response": result,
                "k8s_job": job_meta,
            }

        except httpx.HTTPError as e:
            logger.error(f"HTTP error dispatching task {task_id}: {e}")
            return {
                "status": "error",
                "agent": agent.host,
                "agent_host": agent.host,
                "k8s_job": job_meta,
                "error": str(e),
            }
        except Exception as e:
            logger.error(f"Unexpected error dispatching task {task_id}: {e}")
            return {
                "status": "error",
                "agent": agent.host,
                "agent_host": agent.host,
                "k8s_job": job_meta,
                "error": str(e),
            }

    def _launch_k8s_job(
        self, task_id: int, task_data: Dict
    ) -> tuple[Optional[str], Optional[dict]]:
        """在 K8S 创建一次性 Job 运行 agent。返回 host 与 job 元数据。"""
        if not shutil.which("kubectl"):
            logger.warning("USE_K8S_AGENT=1 但未安装 kubectl，跳过 K8S Job 创建")
            return None, None
        image = os.getenv("AGENT_IMAGE")
        if not image:
            logger.warning("USE_K8S_AGENT=1 但未配置 AGENT_IMAGE，跳过 K8S Job 创建")
            return None, None
        job_id = task_data.get("run_id") or task_id
        job_name = f"ptp-agent-run-{job_id}"
        namespace = self.k8s_namespace
        labels = self._build_k8s_job_labels(task_id, task_data)
        labels[f"{K8S_LABEL_PREFIX}/job-name"] = (
            self._sanitize_k8s_label_value(job_name) or job_name[:63]
        )
        service_port = int(os.getenv("K8S_AGENT_PORT", "9096"))
        host_override = os.getenv(
            "K8S_AGENT_HOST"
        )  # 可选：外部暴露的 Host/IP
        access_mode = (
            (os.getenv("K8S_AGENT_ACCESS_MODE") or "service")
            .strip()
            .lower()
            .replace("-", "_")
        )
        logger.info(
            "K8S_AGENT_ACCESS_MODE env=%r resolved access_mode=%r",
            os.getenv("K8S_AGENT_ACCESS_MODE"),
            access_mode,
        )
        if access_mode not in {"service", "port_forward"}:
            logger.warning("未知 K8S_AGENT_ACCESS_MODE=%s，回退为 service", access_mode)
            access_mode = "service"
        env_map: Dict[str, str] = {}
        passthrough_keys = {
            "AGENT_EXEC_MODE",
            "PUSHGATEWAY_URL",
            "LOG_ARCHIVE_S3",
            "USE_S3",
            "S3_RUN_ARTIFACT_PREFIX",
            "S3_REPORT_ARTIFACT_PREFIX",
            "RUN_ARTIFACT_PREFIX",
            "LOG_STREAM_UPLOAD_INTERVAL",
            "AGENT_PORT",
            "AGENT_IP",
            "ENABLE_NACOS",
            "NACOS_SERVER",
            "NACOS_USERNAME",
            "NACOS_PASSWORD",
            "NACOS_NAMESPACE",
            "NACOS_SERVICE_NAME",
            "NACOS_GROUP",
            "NACOS_CLUSTER",
            "NACOS_HEARTBEAT_INTERVAL",
        }
        aws_keys = {
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_DEFAULT_REGION",
            "AWS_REGION",
            "AWS_ENDPOINT_URL",
            "AWS_S3_ENDPOINT",
        }
        for k, v in os.environ.items():
            if k.startswith("S3_") or k in passthrough_keys or k in aws_keys:
                env_map[k] = str(v)

        # K8S 环境覆盖：宿主机与 Pod 访问同一中间件时，地址通常不同（例如 minikube(docker driver) 下 Pod 用 host.minikube.internal）。
        # - K8S_S3_ENDPOINT：覆盖 Pod 内 S3_ENDPOINT
        # - K8S_PUSHGATEWAY_URL：覆盖 Pod 内 PUSHGATEWAY_URL
        # - K8S_NACOS_SERVER：覆盖 Pod 内 NACOS_SERVER（若启用 ENABLE_NACOS=1）
        # - K8S_JMETER_HOME：覆盖 Pod 内 JMETER_HOME（避免误透传宿主机路径）
        # - K8S_K6_BIN：覆盖 Pod 内 K6_BIN（避免误透传宿主机路径）
        #
        # 说明：部分历史镜像的 K6 执行逻辑仅依赖 K6_BIN（默认可能解析到 /app/k6），
        # 为保证本地 minikube 闭环可复现，这里为 K6_BIN/K6_BINARY 提供默认兜底（/usr/local/bin/k6）。
        k6_bin_fallback = "/usr/local/bin/k6"
        override_pairs = {
            "S3_ENDPOINT": os.getenv("K8S_S3_ENDPOINT"),
            "PUSHGATEWAY_URL": os.getenv("K8S_PUSHGATEWAY_URL"),
            "NACOS_SERVER": os.getenv("K8S_NACOS_SERVER"),
            "JMETER_HOME": os.getenv("K8S_JMETER_HOME"),
            "K6_BIN": os.getenv("K8S_K6_BIN")
            or os.getenv("K8S_K6_BINARY")
            or k6_bin_fallback,
            "K6_BINARY": os.getenv("K8S_K6_BINARY")
            or os.getenv("K8S_K6_BIN")
            or k6_bin_fallback,
        }
        for key, value in override_pairs.items():
            if value:
                env_map[key] = str(value)
        # 确保端口一致，避免重复
        env_map["AGENT_PORT"] = str(service_port)
        env_map["PORT"] = str(service_port)
        env_list = [{"name": k, "value": v} for k, v in env_map.items()]

        resources = None
        resources_env = os.getenv("K8S_AGENT_RESOURCES")
        if resources_env:
            try:
                resources = json.loads(resources_env)
            except json.JSONDecodeError:
                logger.warning("K8S_AGENT_RESOURCES 不是合法 JSON，已忽略")
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": job_name, "namespace": namespace, "labels": labels},
            "spec": {
                "backoffLimit": 0,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "agent",
                                "image": image,
                                "imagePullPolicy": os.getenv(
                                    "K8S_IMAGE_PULL_POLICY", "IfNotPresent"
                                ),
                                "env": env_list,
                                "ports": [{"containerPort": service_port}],
                            }
                        ],
                    },
                },
            },
        }
        if service_port != 9096:
            manifest["spec"]["template"]["spec"]["containers"][0]["args"] = [
                "uvicorn",
                "main:app",
                "--host",
                "0.0.0.0",
                "--port",
                str(service_port),
            ]
            logger.warning(
                "K8S_AGENT_PORT 非默认 9096，已覆盖容器启动端口为 %s", service_port
            )
        if resources:
            manifest["spec"]["template"]["spec"]["containers"][0][
                "resources"
            ] = resources
        node_selector_env = os.getenv("K8S_NODE_SELECTOR")
        if node_selector_env:
            try:
                manifest["spec"]["template"]["spec"]["nodeSelector"] = json.loads(
                    node_selector_env
                )
            except json.JSONDecodeError:
                logger.warning("K8S_NODE_SELECTOR 不是合法 JSON，已忽略")
        try:
            proc = subprocess.run(
                ["kubectl", "apply", "-f", "-"],
                input=json.dumps(manifest).encode(),
                check=True,
                capture_output=True,
            )
            logger.info("K8S Job 创建成功: %s", proc.stdout.decode(errors="ignore"))
        except Exception as exc:
            logger.warning("K8S Job 创建失败: %s", exc)
            return None, None

        service_name = f"{job_name}-svc"
        service_type = os.getenv("K8S_SERVICE_TYPE", "ClusterIP")
        svc_manifest = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": service_name,
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "selector": labels,
                "ports": [{"port": service_port, "targetPort": service_port}],
                "type": service_type,
            },
        }
        try:
            subprocess.run(
                ["kubectl", "apply", "-f", "-"],
                input=json.dumps(svc_manifest).encode(),
                check=True,
                capture_output=True,
            )
            logger.info("K8S Service 创建成功: %s", svc_manifest["metadata"]["name"])
        except Exception as exc:  # pragma: no cover - 容错
            logger.warning("K8S Service 创建失败: %s", exc)

        wait_ready = os.getenv("K8S_WAIT_READY", "1") == "1"
        if wait_ready:
            timeout = int(os.getenv("K8S_WAIT_TIMEOUT", "90"))
            self._wait_for_pod_ready(job_name, namespace, timeout)

        port_forward = None
        if access_mode == "port_forward":
            host, port_forward = self._start_port_forward(
                service_name, namespace, service_port
            )
            if host:
                logger.info(
                    "K8S port-forward 就绪: %s -> %s/%s:%s",
                    host,
                    namespace,
                    service_name,
                    service_port,
                )
            else:
                logger.warning(
                    "K8S port-forward 未就绪，回退为 Service 解析（可能仅集群内可达）"
                )
                host = self._resolve_service_host(service_name, namespace, service_port)
                if host_override:
                    host = self._coerce_host_override(
                        host_override, host, namespace, service_name, service_port
                    )
                host = host or f"{service_name}.{namespace}.svc:{service_port}"
        else:
            host = self._resolve_service_host(service_name, namespace, service_port)
            if host_override:
                host = self._coerce_host_override(
                    host_override, host, namespace, service_name, service_port
                )
            host = host or f"{service_name}.{namespace}.svc:{service_port}"

        meta = {
            "job_name": job_name,
            "namespace": namespace,
            "service_port": service_port,
            "service_name": service_name,
            "labels": labels,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if port_forward:
            meta["port_forward"] = port_forward
        return host, meta

    def cleanup_k8s_job(self, meta: Optional[dict]):
        if not meta or os.getenv("K8S_CLEANUP", "1") != "1":
            return
        if os.getenv("K8S_CLEANUP_DRY_RUN", "0") == "1":
            preview = self.build_k8s_cleanup_dry_run(meta)
            if preview:
                logger.info("K8S cleanup dry-run preview: %s", preview)
            else:
                logger.warning("K8S cleanup dry-run skipped: missing strong scope")
            return
        self._stop_port_forward(meta)
        job_name = meta.get("job_name")
        namespace = meta.get("namespace", self.k8s_namespace)
        if not job_name:
            return
        service_name = meta.get("service_name") or f"{job_name}-svc"
        for kind, name in (("job", job_name), ("service", service_name)):
            try:
                subprocess.run(
                    [
                        "kubectl",
                        "delete",
                        kind,
                        name,
                        "-n",
                        namespace,
                        "--ignore-not-found=true",
                    ],
                    check=False,
                    capture_output=True,
                )
                logger.info("清理 K8S %s: %s/%s", kind, namespace, name)
            except Exception as exc:  # pragma: no cover - 容错
                logger.debug("清理 K8S %s 失败: %s", name, exc)

    def _coerce_host_override(
        self,
        host_override: str,
        resolved_host: Optional[str],
        namespace: str,
        service_name: str,
        service_port: int,
    ) -> str:
        """将 K8S_AGENT_HOST 规范化为 host:port。若仅提供 host，则根据 Service 类型补齐端口。"""
        override = host_override.strip()
        if not override:
            return resolved_host or f"{service_name}.{namespace}.svc:{service_port}"
        if ":" in override:
            return override
        # 仅提供 host 时：NodePort 补齐 nodePort；否则补齐 service_port
        try:
            proc = subprocess.run(
                ["kubectl", "get", "svc", service_name, "-n", namespace, "-o", "json"],
                check=True,
                capture_output=True,
            )
            svc = json.loads(proc.stdout.decode() or "{}")
            spec = svc.get("spec", {})
            if spec.get("type") == "NodePort":
                for p in spec.get("ports") or []:
                    if p.get("nodePort"):
                        return f"{override}:{p['nodePort']}"
        except Exception:
            pass
        return f"{override}:{service_port}"

    def _start_port_forward(
        self, service_name: str, namespace: str, service_port: int
    ) -> tuple[Optional[str], Optional[dict]]:
        """启动 `kubectl port-forward svc/<name>`，返回可访问 host 与元信息（pid/port）。"""
        address = (os.getenv("K8S_PORT_FORWARD_ADDRESS") or "127.0.0.1").strip()
        bind_host = (address.split(",")[0].strip() or "127.0.0.1").replace(
            "localhost", "127.0.0.1"
        )
        connect_host = "127.0.0.1" if bind_host in {"0.0.0.0", "::"} else bind_host
        try:
            local_port = int(os.getenv("K8S_PORT_FORWARD_LOCAL_PORT", "0"))
        except ValueError:
            local_port = 0
        if local_port <= 0:
            local_port = self._pick_free_port(bind_host)

        cmd = [
            "kubectl",
            "port-forward",
            f"svc/{service_name}",
            f"{local_port}:{service_port}",
            "-n",
            namespace,
            "--address",
            address,
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:  # pragma: no cover - 容错
            logger.warning("启动 kubectl port-forward 失败: %s", exc)
            return None, None

        timeout = int(os.getenv("K8S_PORT_FORWARD_TIMEOUT", "10"))
        if not self._wait_for_tcp_listen(connect_host, local_port, proc.pid, timeout):
            self._kill_process(proc.pid)
            return None, None

        meta = {
            "pid": proc.pid,
            "address": address,
            "local_port": local_port,
            "target": f"svc/{service_name}",
        }
        host_str = f"{connect_host}:{local_port}"

        # TCP 端口可连不等于 Uvicorn ASGI 已就绪，额外轮询 /health 直到 HTTP 200
        warmup_timeout = int(os.getenv("K8S_HTTP_WARMUP_TIMEOUT", "20"))
        warmup_deadline = time.time() + warmup_timeout
        warmup_ok = False
        while time.time() < warmup_deadline:
            try:
                import urllib.request

                with urllib.request.urlopen(
                    f"http://{host_str}/health", timeout=2
                ) as resp:
                    if resp.status == 200:
                        warmup_ok = True
                        break
            except Exception:
                time.sleep(0.5)
        if not warmup_ok:
            logger.warning(
                "port-forward HTTP warmup 超时（%ds），agent 可能未完全就绪，将尝试继续",
                warmup_timeout,
            )

        return host_str, meta

    def _stop_port_forward(self, meta: dict) -> None:
        pf = (meta or {}).get("port_forward") or {}
        pid = pf.get("pid")
        if not pid:
            return
        self._kill_process(int(pid))

    def _kill_process(self, pid: int) -> None:
        if pid <= 0:
            return
        try:
            try:
                os.killpg(pid, signal.SIGTERM)
            except Exception:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception as exc:  # pragma: no cover - 容错
            logger.debug("终止进程失败 pid=%s: %s", pid, exc)
            return
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        try:
            try:
                os.killpg(pid, signal.SIGKILL)
            except Exception:
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass

    def _pick_free_port(self, host: str) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            return int(s.getsockname()[1])

    def _wait_for_tcp_listen(
        self, host: str, port: int, pid: int, timeout_seconds: int
    ) -> bool:
        deadline = time.time() + max(1, timeout_seconds)
        while time.time() < deadline:
            # 若进程已退出则直接失败
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.2)
        return False

    def _resolve_service_host(
        self, service_name: str, namespace: str, port: int
    ) -> Optional[str]:
        """尝试获取 Service 的可访问地址，支持 LoadBalancer / NodePort / ClusterIP。"""
        try:
            proc = subprocess.run(
                ["kubectl", "get", "svc", service_name, "-n", namespace, "-o", "json"],
                check=True,
                capture_output=True,
            )
            svc = json.loads(proc.stdout.decode() or "{}")
            status = svc.get("status", {})
            spec = svc.get("spec", {})
            # LoadBalancer 外部入口
            ingress = (status.get("loadBalancer") or {}).get("ingress") or []
            if ingress:
                addr = ingress[0].get("hostname") or ingress[0].get("ip")
                if addr:
                    return f"{addr}:{port}"
            # NodePort + 节点 IP（最佳努力，用 clusterIP/ClusterIP DNS 兜底）
            if spec.get("type") == "NodePort":
                node_port = None
                for p in spec.get("ports") or []:
                    if p.get("nodePort"):
                        node_port = p["nodePort"]
                        break
                if node_port:
                    try:
                        nodes = subprocess.run(
                            ["kubectl", "get", "nodes", "-o", "json"],
                            check=True,
                            capture_output=True,
                        )
                        nodes_data = (
                            json.loads(nodes.stdout.decode() or "{}").get("items") or []
                        )
                        if nodes_data:
                            addresses = (
                                nodes_data[0].get("status", {}).get("addresses") or []
                            )
                            node_ip = None
                            for addr in addresses:
                                if addr.get("type") in ("InternalIP", "ExternalIP"):
                                    node_ip = addr.get("address")
                                    break
                            if node_ip:
                                return f"{node_ip}:{node_port}"
                    except Exception:
                        logger.debug("解析 NodePort 节点地址失败，使用 ClusterIP 回退")
                cluster_ip = spec.get("clusterIP")
                if cluster_ip:
                    return f"{cluster_ip}:{port}"
            # 默认 ClusterIP DNS
            return f"{service_name}.{namespace}.svc:{port}"
        except Exception as exc:  # pragma: no cover - 容错
            logger.debug("解析 K8S Service 访问地址失败: %s", exc)
            return None

    def _wait_for_pod_ready(
        self, job_name: str, namespace: str, timeout_seconds: int
    ) -> None:
        """最佳努力等待 Pod 就绪，避免创建后立即分发导致 404。"""
        deadline = time.time() + timeout_seconds
        label_selector = f"job-name={job_name}"
        while time.time() < deadline:
            try:
                pods = subprocess.run(
                    [
                        "kubectl",
                        "get",
                        "pods",
                        "-l",
                        label_selector,
                        "-n",
                        namespace,
                        "-o",
                        "json",
                    ],
                    check=True,
                    capture_output=True,
                )
                data = json.loads(pods.stdout.decode() or "{}")
                items = data.get("items") or []
                if not items:
                    time.sleep(2)
                    continue
                pod = items[0]
                status = pod.get("status", {})
                phase = status.get("phase")
                conditions = status.get("conditions") or []
                ready = any(
                    c.get("type") == "Ready" and c.get("status") == "True"
                    for c in conditions
                )
                container_statuses = status.get("containerStatuses") or []
                if not ready and container_statuses:
                    for c in container_statuses:
                        if c.get("ready") is True:
                            ready = True
                            break
                if ready and phase == "Running":
                    return
                if phase in {"Succeeded", "Failed"}:
                    logger.warning("Pod 状态为 %s，可能已退出，job=%s", phase, job_name)
                    break
                time.sleep(2)
            except Exception:
                time.sleep(2)
        logger.warning("等待 K8S Pod 就绪超时（job=%s, ns=%s）", job_name, namespace)

    async def fetch_run_status(self, agent_host: str, run_token: str) -> Optional[Dict]:
        """从 agent 查询 run 状态"""
        if os.getenv("TESTING", "0") == "1":
            return {"status": "succeeded", "jtl_summary": None, "k6_summary": None}

        url = f"http://{agent_host}/agent/runs/{run_token}/status"
        max_retries = max(1, int(os.getenv("AGENT_STATUS_FETCH_RETRIES", "3")))
        backoff_seconds = max(
            0.2, float(os.getenv("AGENT_STATUS_FETCH_BACKOFF_SECONDS", "0.5"))
        )
        last_exc: Exception | None = None

        for attempt in range(1, max_retries + 1):
            try:
                # poll_run_status 由 Celery prefork 多次创建/关闭 event loop 驱动，状态轮询不能复用共享 AsyncClient。
                resp = await self._fresh_request("GET", url)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                logger.warning(
                    "fetch_run_status failed host=%s token=%s attempt=%d/%d error=%s",
                    agent_host,
                    run_token,
                    attempt,
                    max_retries,
                    self._format_http_exception(exc),
                )
                break
            except (httpx.RequestError, httpx.TimeoutException, ValueError) as exc:
                last_exc = exc
                if attempt < max_retries:
                    logger.info(
                        "fetch_run_status transient host=%s token=%s attempt=%d/%d error=%s",
                        agent_host,
                        run_token,
                        attempt,
                        max_retries,
                        self._format_http_exception(exc),
                    )
                    await asyncio.sleep(backoff_seconds * attempt)
                    continue
                logger.warning(
                    "fetch_run_status failed host=%s token=%s attempt=%d/%d error=%s",
                    agent_host,
                    run_token,
                    attempt,
                    max_retries,
                    self._format_http_exception(exc),
                )
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "fetch_run_status unexpected host=%s token=%s error=%s",
                    agent_host,
                    run_token,
                    self._format_http_exception(exc),
                )
                break
        return None

    async def stop_run(self, agent_host: str, run_token: str) -> Optional[Dict]:
        """请求 agent 停止 run。"""
        if os.getenv("TESTING", "0") == "1":
            return {"status": "success", "message": "mocked stop success"}

        url = f"http://{agent_host}/agent/runs/{run_token}/stop"
        try:
            resp = await self._request("POST", url)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning(
                "stop_run failed for host=%s token=%s: %s", agent_host, run_token, exc
            )
            return None

    async def fetch_run_k6_control(
        self, agent_host: str, run_token: str
    ) -> Optional[Dict]:
        del agent_host, run_token
        return {"available": False, "reason": "dynamic_k6_control_roadmap_only"}

    async def update_run_k6_control(
        self, agent_host: str, run_token: str, payload: Dict
    ) -> Optional[Dict]:
        del agent_host, run_token, payload
        return {"available": False, "reason": "dynamic_k6_control_roadmap_only"}

    def fetch_k8s_events(self, meta: dict, limit: int = 50) -> Optional[list]:
        job_name = meta.get("job_name")
        namespace = meta.get("namespace", self.k8s_namespace)
        if not job_name:
            return None
        try:
            events = subprocess.run(
                [
                    "kubectl",
                    "get",
                    "events",
                    "--field-selector",
                    f"involvedObject.name={job_name}",
                    "-n",
                    namespace,
                    "-o",
                    "json",
                ],
                check=True,
                capture_output=True,
            )
            data = json.loads(events.stdout.decode())
            items = data.get("items") or []
            items = sorted(
                items,
                key=lambda x: x.get("lastTimestamp") or x.get("eventTime") or "",
                reverse=True,
            )
            return items[:limit]
        except Exception as exc:  # pragma: no cover - 容错
            logger.debug("fetch_k8s_events failed: %s", exc)
            return None

    def fetch_k8s_logs(self, meta: dict, tail: int = 1000) -> Optional[str]:
        job_name = meta.get("job_name")
        namespace = meta.get("namespace", self.k8s_namespace)
        if not job_name:
            return None
        try:
            pods = subprocess.run(
                [
                    "kubectl",
                    "get",
                    "pods",
                    "-l",
                    f"job-name={job_name}",
                    "-n",
                    namespace,
                    "-o",
                    "json",
                ],
                check=True,
                capture_output=True,
            )
            data = json.loads(pods.stdout.decode())
            items = data.get("items") or []
            if not items:
                return None
            pod = items[0]
            pod_name = pod.get("metadata", {}).get("name")
            if not pod_name:
                return None
            logs = subprocess.run(
                ["kubectl", "logs", pod_name, "-n", namespace, f"--tail={tail}"],
                check=True,
                capture_output=True,
            )
            return logs.stdout.decode(errors="ignore")
        except Exception as exc:  # pragma: no cover - 容错
            logger.debug("fetch_k8s_logs failed: %s", exc)
            return None

    def upload_k8s_logs_to_s3(self, meta: dict, content: str) -> Optional[str]:
        bucket = os.getenv("S3_BUCKET")
        use_s3 = (
            os.getenv("USE_S3", "0") == "1" or os.getenv("LOG_ARCHIVE_S3", "0") == "1"
        )
        if not bucket or not use_s3 or not content:
            return None
        prefix = get_run_artifact_prefix()
        job_name = meta.get("job_name", "k8s-job")
        key = f"{prefix}/{job_name}.k8s.log"
        try:
            s3_utils.upload_bytes(
                bucket, key, content.encode("utf-8"), content_type="text/plain"
            )
            return f"s3://{bucket}/{key}"
        except Exception as exc:  # pragma: no cover - 容错
            logger.debug("upload k8s logs to s3 failed: %s", exc)
            return None

    async def wait_for_completion(
        self,
        agent_host: str,
        run_token: str,
        timeout_seconds: int = 120,
        interval_seconds: int = 2,
    ) -> Dict:
        """
        轮询 agent 状态，等待 run 结束
        """
        start = datetime.now(timezone.utc)
        while True:
            status_payload = await self.fetch_run_status(agent_host, run_token)
            status_value = (status_payload or {}).get("status")
            if status_value in {"succeeded", "failed", "stopped"}:
                return {
                    "status": status_value,
                    "payload": status_payload or {},
                }
            if (datetime.now(timezone.utc) - start).total_seconds() > timeout_seconds:
                return {"status": "timeout", "payload": status_payload or {}}
            await asyncio.sleep(interval_seconds)

    async def monitor_task(self, task_id: int, agent: AgentInstance) -> Dict:
        """
        监控任务执行状态

        Public alpha uses run-level polling for user-visible status.
        """
        return {"status": "running", "progress": 0}

    def fetch_k8s_job_status(self, meta: dict) -> Optional[dict]:
        """查询 K8S Job/Pod 状态并尝试抓取末尾日志（最佳努力）。"""
        job_name = meta.get("job_name")
        namespace = meta.get("namespace", self.k8s_namespace)
        if not job_name:
            return None
        try:
            pods = subprocess.run(
                [
                    "kubectl",
                    "get",
                    "pods",
                    "-l",
                    f"job-name={job_name}",
                    "-n",
                    namespace,
                    "-o",
                    "json",
                ],
                check=True,
                capture_output=True,
            )
            data = json.loads(pods.stdout.decode())
            items = data.get("items") or []
            if not items:
                return None
            pod = items[0]
            status = pod.get("status", {})
            phase = status.get("phase")
            container_statuses = status.get("containerStatuses") or []
            detail = None
            for c in container_statuses:
                st = c.get("state", {})
                if "terminated" in st:
                    term = st["terminated"]
                    detail = term.get("reason") or term.get("message")
                    exit_code = term.get("exitCode")
                    if exit_code not in (None, 0):
                        detail = detail or f"exitCode={exit_code}"
            log_tail = None
            try:
                pod_name = pod.get("metadata", {}).get("name")
                if pod_name:
                    logs = subprocess.run(
                        ["kubectl", "logs", pod_name, "-n", namespace, "--tail=50"],
                        check=True,
                        capture_output=True,
                    )
                    log_tail = logs.stdout.decode(errors="ignore").splitlines()[-50:]
            except Exception:
                pass
            return {"phase": phase, "detail": detail, "log_tail": log_tail}
        except Exception as exc:  # pragma: no cover - 容错
            logger.debug("fetch_k8s_job_status failed: %s", exc)
            return None

    async def close(self):
        """关闭 HTTP 客户端"""
        await self.http_client.aclose()


# 全局调度器实例
orchestrator = AgentOrchestrator()
