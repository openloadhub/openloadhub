import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# 根据 ENV_FILE 选择环境文件，默认 .env。
# 平台级配置 SSOT 仅允许显式指向仓库根环境文件，不再隐式回退 cwd/.env 或服务私有 .env。
ENV_FILE = os.getenv("ENV_FILE", ".env")


def _detect_repo_root() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "docker-compose.demo.yml").exists():
            return candidate
        if (candidate / "main.py").exists() and (candidate / "common").exists():
            return candidate
    return current.parents[3]


REPO_ROOT = _detect_repo_root()


def _resolve_env_file_path(
    env_file: str | Path | None = None, *, repo_root: Path | None = None
) -> Path:
    root = repo_root or REPO_ROOT
    raw_value = env_file if env_file is not None else ENV_FILE
    candidate = Path(raw_value)
    if candidate.is_absolute():
        return candidate
    return root / candidate


def load_repo_env(
    env_file: str | Path | None = None, *, repo_root: Path | None = None
) -> Path:
    env_path = _resolve_env_file_path(env_file, repo_root=repo_root)
    load_dotenv(env_path, override=False)
    return env_path


LOADED_ENV_FILE = load_repo_env()

DOCKER_ONLY_HOSTNAMES = frozenset(
    {
        "mysql",
        "redis",
        "minio",
        "nacos",
        "prometheus",
        "pushgateway",
        "grafana",
        "ptp-agent",
        "frontend",
        "cadvisor",
    }
)
HOST_RUNTIME_GUARD_KEYS = (
    "DATABASE_URL",
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
    "S3_ENDPOINT",
    "NACOS_SERVER",
    "PROMETHEUS_URL",
    "PUSHGATEWAY_URL",
    "GRAFANA_BASE_URL",
)


def _is_containerized_runtime(in_container: bool | None = None) -> bool:
    if in_container is not None:
        return in_container
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return True
    if Path("/.dockerenv").exists():
        return True
    cgroup_path = Path("/proc/1/cgroup")
    if not cgroup_path.exists():
        return False
    try:
        content = cgroup_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return "docker" in content or "kubepods" in content


def _extract_endpoint_hosts(raw_value: str) -> List[str]:
    hosts: List[str] = []
    for part in [item.strip() for item in raw_value.split(",") if item.strip()]:
        parsed = urlparse(part if "://" in part else f"//{part}")
        hostname = (parsed.hostname or "").strip().lower()
        if hostname:
            hosts.append(hostname)
    return hosts


def get_runtime_endpoint_config(
    config: Mapping[str, str] | None = None,
) -> Dict[str, str]:
    if config is not None:
        return {key: str(value) for key, value in config.items()}
    return {
        "DATABASE_URL": settings.DATABASE_URL,
        "CELERY_BROKER_URL": settings.CELERY_BROKER_URL,
        "CELERY_RESULT_BACKEND": settings.CELERY_RESULT_BACKEND,
        "S3_ENDPOINT": settings.S3_ENDPOINT or "",
        "NACOS_SERVER": settings.NACOS_SERVER,
        "PROMETHEUS_URL": settings.PROMETHEUS_URL,
        "PUSHGATEWAY_URL": settings.PUSHGATEWAY_URL,
        "GRAFANA_BASE_URL": settings.GRAFANA_BASE_URL,
    }


def detect_host_runtime_docker_hostname_issues(
    config: Mapping[str, str] | None = None,
    *,
    in_container: bool | None = None,
) -> List[Dict[str, str]]:
    if _is_containerized_runtime(in_container):
        return []

    resolved = get_runtime_endpoint_config(config)
    issues: List[Dict[str, str]] = []
    for key in HOST_RUNTIME_GUARD_KEYS:
        raw_value = resolved.get(key, "").strip()
        if not raw_value:
            continue
        for hostname in _extract_endpoint_hosts(raw_value):
            if hostname in DOCKER_ONLY_HOSTNAMES:
                issues.append({"key": key, "host": hostname, "value": raw_value})
                break
    return issues


def build_host_runtime_docker_env_error(
    *,
    service_name: str,
    issues: List[Dict[str, str]],
    env_file_path: str | Path | None = None,
) -> str:
    env_path = Path(env_file_path) if env_file_path is not None else LOADED_ENV_FILE
    issue_summary = ", ".join(f"{item['key']} -> {item['host']}" for item in issues)
    host_env_file = REPO_ROOT / ".env.host"
    return (
        f"检测到宿主机启动 {service_name} 时加载了 Docker 网络地址：{issue_summary}。"
        f" 当前运行不是容器内进程，但环境文件 {env_path} 仍包含 docker-only hostname；"
        "这通常意味着误读了仓库根 .env。"
        f" 请改用仓库根 .env.host，例如先执行 `export ENV_FILE={host_env_file}`，"
        "然后再启动宿主机进程。"
    )


def ensure_host_runtime_safe(
    *,
    service_name: str,
    config: Mapping[str, str] | None = None,
    in_container: bool | None = None,
    env_file_path: str | Path | None = None,
) -> None:
    issues = detect_host_runtime_docker_hostname_issues(
        config, in_container=in_container
    )
    if not issues:
        return
    raise RuntimeError(
        build_host_runtime_docker_env_error(
            service_name=service_name,
            issues=issues,
            env_file_path=env_file_path,
        )
    )


def _normalize_artifact_prefix(raw: Optional[str], default: str) -> str:
    value = (raw or "").strip().strip("/")
    return value or default


def _normalize_artifact_namespace(raw: Optional[str]) -> str:
    return (raw or "").strip().strip("/")


def _apply_artifact_namespace(prefix: str, namespace: Optional[str]) -> str:
    normalized_prefix = (prefix or "").strip().strip("/")
    normalized_namespace = _normalize_artifact_namespace(namespace)
    if not normalized_prefix:
        return normalized_namespace
    if not normalized_namespace:
        return normalized_prefix
    if normalized_prefix == normalized_namespace or normalized_prefix.startswith(
        f"{normalized_namespace}/"
    ):
        return normalized_prefix
    return f"{normalized_namespace}/{normalized_prefix}"


def _default_artifact_namespace_prefix() -> str:
    return _normalize_artifact_namespace(
        os.getenv("ARTIFACT_NAMESPACE_PREFIX")
        or os.getenv("PROJECT_ARTIFACT_NAMESPACE")
    )


def _default_run_artifact_prefix() -> str:
    return _apply_artifact_namespace(
        _normalize_artifact_prefix(
            os.getenv("S3_RUN_ARTIFACT_PREFIX") or os.getenv("RUN_ARTIFACT_PREFIX"),
            "runs",
        ),
        _default_artifact_namespace_prefix(),
    )


def _default_report_artifact_prefix() -> str:
    return _apply_artifact_namespace(
        _normalize_artifact_prefix(
            os.getenv("S3_REPORT_ARTIFACT_PREFIX") or os.getenv("RUN_ARTIFACT_PREFIX"),
            "reports",
        ),
        _default_artifact_namespace_prefix(),
    )


def _default_lifecycle_prefixes() -> str:
    prefixes: List[str] = []
    for prefix in (_default_run_artifact_prefix(), _default_report_artifact_prefix()):
        candidate = f"{prefix}/"
        if candidate not in prefixes:
            prefixes.append(candidate)
    return ",".join(prefixes)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


TESTNET_SCOPE_SYNONYMS = {
    "testnet",
    "test-net",
    "test_net",
    "staging",
    "stg",
    "preprod",
    "pre-prod",
    "pre-release",
    "pre release",
    "preview",
    "预发",
    "灰度",
}
MAIN_SCOPE_SYNONYMS = {
    "mainnet",
    "prod",
    "production",
    "主网",
    "生产",
}
TESTNET_HINT_TOKENS = (
    "testnet",
    "test-net",
    "test_net",
    "staging",
    "stg",
    "preprod",
    "pre-prod",
    "pre-release",
    "pre release",
    "preview",
    "预发",
    "灰度",
)
MAIN_HINT_TOKENS = (
    "mainnet",
    "main",
    "prod",
    "production",
    "主网",
    "生产",
)


def _normalize_environment_scope(raw_scope: Optional[str]) -> Optional[str]:
    value = (raw_scope or "").strip().lower()
    if not value:
        return None
    if value in {"test", "testnet", "main"}:
        return value
    if value in MAIN_SCOPE_SYNONYMS:
        return "main"
    if value in TESTNET_SCOPE_SYNONYMS:
        return "testnet"
    return None


def _infer_environment_scope(code: str, name: str) -> str:
    haystack = f"{code} {name}".strip().lower()
    if any(token in haystack for token in TESTNET_HINT_TOKENS):
        return "testnet"
    if any(token in haystack for token in MAIN_HINT_TOKENS):
        return "main"
    return "test"


class Settings(BaseSettings):
    """应用全局配置（共享给 admin / worker / agent）"""

    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=True,
        env_file_encoding="utf-8",
    )

    APP_NAME: str = "OpenLoadHub"
    VERSION: str = "2.0.0"
    DEBUG: bool = True
    RUNTIME_GIT_SHA: str = os.getenv("PTP_RUNTIME_GIT_SHA", "")
    RUNTIME_GIT_BRANCH: str = os.getenv("PTP_RUNTIME_GIT_BRANCH", "")
    RUNTIME_WORKTREE_ROOT: str = os.getenv("PTP_RUNTIME_WORKTREE_ROOT", "")
    RUNTIME_BASELINE_ID: str = os.getenv("PTP_RUNTIME_BASELINE_ID", "")
    RUNTIME_COMPOSE_PROJECT: str = os.getenv("PTP_RUNTIME_COMPOSE_PROJECT", "")
    PTP_PUBLIC_ALPHA_MODE: bool = _env_bool("PTP_PUBLIC_ALPHA_MODE", True)
    PTP_ENABLE_MIXED_RUNS: bool = _env_bool(
        "PTP_ENABLE_MIXED_RUNS", not PTP_PUBLIC_ALPHA_MODE
    )
    PTP_ENABLE_SELF_APM: bool = _env_bool(
        "PTP_ENABLE_SELF_APM", not PTP_PUBLIC_ALPHA_MODE
    )
    PTP_ENABLE_NOTIFICATIONS: bool = _env_bool(
        "PTP_ENABLE_NOTIFICATIONS", not PTP_PUBLIC_ALPHA_MODE
    )
    PTP_ENABLE_ALERTS: bool = _env_bool("PTP_ENABLE_ALERTS", not PTP_PUBLIC_ALPHA_MODE)
    PTP_ENABLE_PLANS: bool = _env_bool("PTP_ENABLE_PLANS", True)
    PTP_ENABLE_PLAN_RUNS: bool = _env_bool("PTP_ENABLE_PLAN_RUNS", True)
    PTP_ENABLE_TREND_ANALYSIS: bool = _env_bool("PTP_ENABLE_TREND_ANALYSIS", False)
    PTP_ENABLE_AI_FEATURES: bool = _env_bool(
        "PTP_ENABLE_AI_FEATURES", False
    )

    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "mysql+pymysql://ptp:ptp_demo_password@mysql:3306/ptp"
    )

    NACOS_SERVER: str = os.getenv("NACOS_SERVER", "localhost:8848")
    NACOS_NAMESPACE: str = os.getenv("NACOS_NAMESPACE", "default")
    NACOS_USERNAME: str = os.getenv("NACOS_USERNAME", "nacos")
    NACOS_PASSWORD: str = os.getenv("NACOS_PASSWORD", "nacos")

    CELERY_BROKER_URL: str = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    CELERY_RESULT_BACKEND: str = os.getenv(
        "CELERY_RESULT_BACKEND", "redis://localhost:6379/1"
    )

    NOTIFICATION_ENV_LABEL: str = os.getenv("NOTIFICATION_ENV_LABEL", "")
    PTP_WEBHOOK_BOOTSTRAP_FILE: str = os.getenv("PTP_WEBHOOK_BOOTSTRAP_FILE", "")
    PTP_PUBLIC_BASE_URL: str = os.getenv("PTP_PUBLIC_BASE_URL", "http://127.0.0.1:3000")

    AWS_ACCESS_KEY_ID: Optional[str] = os.getenv("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY: Optional[str] = os.getenv("AWS_SECRET_ACCESS_KEY")
    S3_BUCKET: str = os.getenv("S3_BUCKET", "ptp-scripts")
    S3_REGION: str = os.getenv("S3_REGION", "us-west-2")
    S3_ENDPOINT: Optional[str] = (
        os.getenv("S3_ENDPOINT")
        or os.getenv("AWS_S3_ENDPOINT")
        or os.getenv("AWS_ENDPOINT_URL")
    )
    S3_PUBLIC_ENDPOINT: Optional[str] = os.getenv("S3_PUBLIC_ENDPOINT") or None
    S3_PRESIGNED_ENDPOINT: Optional[str] = (
        os.getenv("S3_PRESIGNED_ENDPOINT") or os.getenv("S3_PUBLIC_ENDPOINT") or None
    )
    USE_S3: bool = os.getenv("USE_S3", "0") == "1"
    S3_ARTIFACT_RETENTION_DAYS: int = int(os.getenv("S3_ARTIFACT_RETENTION_DAYS", "30"))
    S3_RUN_ARTIFACT_PREFIX: str = _default_run_artifact_prefix()
    S3_REPORT_ARTIFACT_PREFIX: str = _default_report_artifact_prefix()
    S3_LIFECYCLE_PREFIXES: str = os.getenv(
        "S3_LIFECYCLE_PREFIXES", _default_lifecycle_prefixes()
    )
    PUSHGATEWAY_URL: str = os.getenv("PUSHGATEWAY_URL", "")
    PROMETHEUS_URL: str = os.getenv("PROMETHEUS_URL", "")
    PROMETHEUS_RETENTION_TIME: str = os.getenv("PROMETHEUS_RETENTION_TIME", "15d")
    PROMETHEUS_RETENTION_SIZE: str = os.getenv("PROMETHEUS_RETENTION_SIZE", "2GB")
    BUSINESS_LINES: str = os.getenv("BUSINESS_LINES", "")
    ENVIRONMENTS: str = os.getenv("ENVIRONMENTS", "")
    LOCAL_ARTIFACT_RETENTION_DAYS: int = int(
        os.getenv("LOCAL_ARTIFACT_RETENTION_DAYS", "7")
    )
    TMP_REPORT_RETENTION_DAYS: int = int(
        os.getenv(
            "TMP_REPORT_RETENTION_DAYS",
            str(os.getenv("LOCAL_ARTIFACT_RETENTION_DAYS", "7")),
        )
    )
    SELF_APM_PERSIST_PATH: str = os.getenv(
        "SELF_APM_PERSIST_PATH",
        ".tmp/self_apm/self_apm_events.jsonl",
    )
    SELF_APM_PERSIST_MAX_EVENTS: int = int(
        os.getenv("SELF_APM_PERSIST_MAX_EVENTS", "5000")
    )

    SECRET_KEY: str = os.getenv(
        "SECRET_KEY",
        "change-me-in-env-file-for-local-demo-only",
    )
    ALGORITHM: str = os.getenv("ALGORITHM", "HS256")
    ACCESS_TOKEN_EXPIRE_SECONDS: int = int(
        os.getenv("ACCESS_TOKEN_EXPIRE_SECONDS", "3600")
    )
    TESTING: bool = os.getenv("TESTING", "0") == "1"
    TRUSTED_AUTH_HEADER_CIDRS: str = os.getenv("TRUSTED_AUTH_HEADER_CIDRS", "")
    ALLOW_SELF_REGISTER: bool = (
        os.getenv("ALLOW_SELF_REGISTER", "1" if TESTING else "0") == "1"
    )
    ARTIFACT_NAMESPACE_PREFIX: str = _default_artifact_namespace_prefix()
    DEFAULT_ADMIN_USERNAME: str = os.getenv("DEFAULT_ADMIN_USERNAME", "")
    DEFAULT_ADMIN_PASSWORD: str = os.getenv("DEFAULT_ADMIN_PASSWORD", "")
    DEFAULT_ADMIN_EMAIL: str = os.getenv("DEFAULT_ADMIN_EMAIL", "admin@example.com")
    DEFAULT_ADMIN_FULL_NAME: str = os.getenv("DEFAULT_ADMIN_FULL_NAME", "Administrator")
    DEFAULT_TESTER_USERNAME: str = os.getenv("DEFAULT_TESTER_USERNAME", "")
    DEFAULT_TESTER_PASSWORD: str = os.getenv("DEFAULT_TESTER_PASSWORD", "")
    DEFAULT_TESTER_EMAIL: str = os.getenv(
        "DEFAULT_TESTER_EMAIL", "demo_tester@example.com"
    )
    DEFAULT_TESTER_FULL_NAME: str = os.getenv("DEFAULT_TESTER_FULL_NAME", "Demo Tester")

    # Grafana 配置
    GRAFANA_BASE_URL: str = os.getenv("GRAFANA_BASE_URL", "")
    GRAFANA_PUBLIC_BASE_URL: str = os.getenv("GRAFANA_PUBLIC_BASE_URL", "")
    GRAFANA_ORG_ID: str = os.getenv("GRAFANA_ORG_ID", "1")
    INFLUXDB_RETENTION: str = os.getenv("INFLUXDB_RETENTION", "168h")
    K6_HTTP_DASHBOARD_UID: str = os.getenv("K6_HTTP_DASHBOARD_UID", "k6-prometheus-ptp")
    K6_GRPC_DASHBOARD_UID: str = os.getenv("K6_GRPC_DASHBOARD_UID", "k6-grpc-ptp")
    K6_WS_DASHBOARD_UID: str = os.getenv("K6_WS_DASHBOARD_UID", "21Ev3D0Ik")
    K6_KAFKA_DASHBOARD_UID: str = os.getenv("K6_KAFKA_DASHBOARD_UID", "usA2Xd_4z")
    K6_BROWSER_DASHBOARD_UID: str = os.getenv("K6_BROWSER_DASHBOARD_UID", "j9zA7u9Ik")
    JMETER_DASHBOARD_UID: str = os.getenv(
        "JMETER_DASHBOARD_UID", "jmeter-load-test-influx"
    )
    POD_DASHBOARD_UID: str = os.getenv("POD_DASHBOARD_UID", "pod-monitor-dashboard")
    POD_HOST_DASHBOARD_UID: str = os.getenv(
        "POD_HOST_DASHBOARD_UID", "pod-monitor-dashboard-host"
    )

    @staticmethod
    def _parse_named_items(raw: str) -> List[Dict[str, str]]:
        if not raw:
            return []

        raw = raw.strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None

        if isinstance(data, list):
            items: List[Dict[str, str]] = []
            for item in data:
                if isinstance(item, dict):
                    code = str(
                        item.get("code")
                        or item.get("id")
                        or item.get("alias")
                        or item.get("name")
                        or ""
                    ).strip()
                    name = str(item.get("name") or item.get("label") or code).strip()
                else:
                    code = str(item).strip()
                    name = code
                if code:
                    items.append({"code": code, "name": name or code})
            return items

        items = []
        for part in [value.strip() for value in raw.split(",") if value.strip()]:
            if ":" in part:
                code, name = part.split(":", 1)
            elif "=" in part:
                code, name = part.split("=", 1)
            else:
                code, name = part, part
            code = code.strip()
            name = name.strip() or code
            if code:
                items.append({"code": code, "name": name})
        return items

    @staticmethod
    def _parse_environment_items(raw: str) -> List[Dict[str, str]]:
        if not raw:
            return []

        raw = raw.strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None

        if isinstance(data, list):
            items: List[Dict[str, str]] = []
            for item in data:
                if isinstance(item, dict):
                    code = str(
                        item.get("code")
                        or item.get("id")
                        or item.get("alias")
                        or item.get("name")
                        or ""
                    ).strip()
                    name = str(item.get("name") or item.get("label") or code).strip()
                    scope = _normalize_environment_scope(
                        str(
                            item.get("scope")
                            or item.get("group")
                            or item.get("env_group")
                            or ""
                        ).strip()
                    )
                else:
                    code = str(item).strip()
                    name = code
                    scope = None
                if code:
                    items.append(
                        {
                            "code": code,
                            "name": name or code,
                            "scope": scope
                            or _infer_environment_scope(code, name or code),
                        }
                    )
            return items

        items: List[Dict[str, str]] = []
        for part in [value.strip() for value in raw.split(",") if value.strip()]:
            if ":" in part:
                parts = [item.strip() for item in part.split(":")]
                if len(parts) >= 3:
                    code = parts[0]
                    scope = _normalize_environment_scope(parts[-1])
                    name = ":".join(parts[1:-1]).strip() or code
                else:
                    code, name = parts[0], parts[1]
                    scope = None
            elif "=" in part:
                code, name = part.split("=", 1)
                code = code.strip()
                name = name.strip()
                scope = None
            else:
                code, name = part, part
                scope = None
            code = code.strip()
            name = name.strip() or code
            if code:
                items.append(
                    {
                        "code": code,
                        "name": name,
                        "scope": scope or _infer_environment_scope(code, name),
                    }
                )
        return items

    @property
    def business_line_items(self) -> List[Dict[str, str]]:
        return self._parse_named_items(self.BUSINESS_LINES)

    @property
    def environment_items(self) -> List[Dict[str, str]]:
        return self._parse_environment_items(self.ENVIRONMENTS)

    @property
    def default_admin_enabled(self) -> bool:
        return bool(self.DEFAULT_ADMIN_USERNAME and self.DEFAULT_ADMIN_PASSWORD)


settings = Settings()


def build_runtime_metadata(*, service_name: str | None = None) -> Dict[str, str]:
    metadata: Dict[str, str] = {"version": settings.VERSION}
    if service_name:
        metadata["service"] = service_name

    runtime_fields = {
        "git_sha": os.getenv("PTP_RUNTIME_GIT_SHA") or settings.RUNTIME_GIT_SHA,
        "git_branch": os.getenv("PTP_RUNTIME_GIT_BRANCH")
        or settings.RUNTIME_GIT_BRANCH,
        "worktree_root": os.getenv("PTP_RUNTIME_WORKTREE_ROOT")
        or settings.RUNTIME_WORKTREE_ROOT,
        "baseline_id": os.getenv("PTP_RUNTIME_BASELINE_ID")
        or settings.RUNTIME_BASELINE_ID,
        "compose_project": os.getenv("PTP_RUNTIME_COMPOSE_PROJECT")
        or settings.RUNTIME_COMPOSE_PROJECT,
    }
    for key, value in runtime_fields.items():
        normalized = str(value or "").strip()
        if normalized:
            metadata[key] = normalized
    return metadata


def get_run_artifact_prefix() -> str:
    return _apply_artifact_namespace(
        _normalize_artifact_prefix(
            os.getenv("S3_RUN_ARTIFACT_PREFIX")
            or os.getenv("RUN_ARTIFACT_PREFIX")
            or settings.S3_RUN_ARTIFACT_PREFIX,
            "runs",
        ),
        os.getenv("ARTIFACT_NAMESPACE_PREFIX")
        or os.getenv("PROJECT_ARTIFACT_NAMESPACE")
        or settings.ARTIFACT_NAMESPACE_PREFIX,
    )


def get_report_artifact_prefix() -> str:
    return _apply_artifact_namespace(
        _normalize_artifact_prefix(
            os.getenv("S3_REPORT_ARTIFACT_PREFIX")
            or os.getenv("RUN_ARTIFACT_PREFIX")
            or settings.S3_REPORT_ARTIFACT_PREFIX,
            "reports",
        ),
        os.getenv("ARTIFACT_NAMESPACE_PREFIX")
        or os.getenv("PROJECT_ARTIFACT_NAMESPACE")
        or settings.ARTIFACT_NAMESPACE_PREFIX,
    )
