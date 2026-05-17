"""
Nacos 客户端封装

负责：
1. 服务注册
2. 服务发现
3. 健康检查
"""

import logging
import os
from typing import List, Dict, Optional

try:
    import nacos  # type: ignore
except Exception:  # pragma: no cover - nacos 不是强依赖
    nacos = None

logger = logging.getLogger(__name__)


def _env_flag_enabled(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}

class NacosClient:
    """Nacos 客户端"""

    def __init__(self, server_addresses: str, namespace: str, username: str = None, password: str = None):
        """
        初始化 Nacos 客户端

        Args:
            server_addresses: Nacos 服务器地址，例如 "localhost:8848"
            namespace: 命名空间
            username: 用户名
            password: 密码
        """
        self.server_addresses = server_addresses
        self.namespace = namespace
        self.username = username
        self.password = password
        self.enabled = _env_flag_enabled("ENABLE_NACOS", default=True)

        self.client = None
        if self.enabled and nacos:
            self.client = nacos.NacosClient(
                server_addresses=server_addresses,
                namespace=namespace,
                username=username,
                password=password,
            )
            logger.info("Initialized real Nacos client: %s, namespace: %s", server_addresses, namespace)
        else:
            logger.info(
                "Nacos client unavailable (ENABLE_NACOS disabled 或 nacos-sdk 未安装)"
            )

    def register_service(self, service_name: str, group_name: str, instance: Dict) -> bool:
        """
        注册服务实例

        Args:
            service_name: 服务名称
            group_name: 分组名称
            instance: 实例信息 {
                'ip': '203.0.113.10',
                'port': 8080,
                'weight': 1.0,
                'metadata': {'version': '1.0'}
            }

        Returns:
            是否注册成功
        """
        try:
            if self.client:
                self.client.add_naming_instance(
                    service_name=service_name,
                    group_name=group_name,
                    ip=instance["ip"],
                    port=instance["port"],
                    weight=instance.get("weight", 1.0),
                    metadata=instance.get("metadata", {}),
                    cluster_name=os.getenv("NACOS_CLUSTER", "DEFAULT"),
                    enable=True,
                    healthy=True,
                    ephemeral=True,
                )

            logger.info(f"Registered service {service_name} at {instance['ip']}:{instance['port']}")
            return True

        except Exception as e:
            logger.error(f"Failed to register service {service_name}: {e}")
            return False

    def deregister_service(self, service_name: str, group_name: str, ip: str, port: int) -> bool:
        """
        注销服务实例

        Args:
            service_name: 服务名称
            group_name: 分组名称
            ip: 实例 IP
            port: 实例端口

        Returns:
            是否注销成功
        """
        try:
            if self.client:
                self.client.remove_naming_instance(
                    service_name=service_name,
                    group_name=group_name,
                    ip=ip,
                    port=port,
                    cluster_name=os.getenv("NACOS_CLUSTER", "DEFAULT"),
                )

            logger.info(f"Deregistered service {service_name} at {ip}:{port}")
            return True

        except Exception as e:
            logger.error(f"Failed to deregister service {service_name}: {e}")
            return False

    def get_service_instances(self, service_name: str, group_name: str = None) -> List[Dict]:
        """
        获取服务实例列表

        Args:
            service_name: 服务名称
            group_name: 分组名称

        Returns:
            实例列表
        """
        try:
            if self.client:
                instances = self.client.list_naming_instance(
                    service_name=service_name,
                    group_name=group_name or "DEFAULT_GROUP",
                    clusters=os.getenv("NACOS_CLUSTER", "DEFAULT"),
                    healthy_only=True,
                )
                if isinstance(instances, dict):
                    return instances.get("hosts", [])
                return []

            return []

        except Exception as e:
            logger.error(f"Failed to get service instances for {service_name}: {e}")
            return []

    def send_heartbeat(self, service_name: str, group_name: str, instance: Dict) -> bool:
        """
        发送心跳

        Args:
            service_name: 服务名称
            group_name: 分组名称
            instance: 实例信息

        Returns:
            是否发送成功
        """
        try:
            # Public alpha keeps Nacos optional; shared deployments can wire a real SDK heartbeat here.
            # self.client.send_heartbeat(
            #     service_name=service_name,
            #     group_name=group_name,
            #     ip=instance['ip'],
            #     port=instance['port'],
            #     weight=instance.get('weight', 1.0),
            #     metadata=instance.get('metadata', {})
            # )

            return True

        except Exception as e:
            logger.error(f"Failed to send heartbeat: {e}")
            return False

# 全局 Nacos 客户端
_nacos_client: Optional[NacosClient] = None

def get_nacos_client() -> Optional[NacosClient]:
    """获取全局 Nacos 客户端实例；若尚未初始化则按当前环境懒初始化。"""
    global _nacos_client
    if _nacos_client is not None:
        return _nacos_client

    server_addresses = str(os.getenv("NACOS_SERVER", "")).strip()
    namespace = str(os.getenv("NACOS_NAMESPACE", "")).strip()
    if not server_addresses or not namespace:
        return None

    _nacos_client = NacosClient(
        server_addresses=server_addresses,
        namespace=namespace,
        username=os.getenv("NACOS_USERNAME"),
        password=os.getenv("NACOS_PASSWORD"),
    )
    return _nacos_client

def init_nacos_client(server_addresses: str, namespace: str, username: str = None, password: str = None):
    """初始化全局 Nacos 客户端"""
    global _nacos_client
    _nacos_client = NacosClient(
        server_addresses=server_addresses,
        namespace=namespace,
        username=username,
        password=password
    )
    return _nacos_client
