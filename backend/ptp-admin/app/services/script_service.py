"""
脚本管理 Service

负责业务逻辑层面的脚本操作
"""

import hashlib
import importlib.util
import json
import logging
import os
import re
import shlex
import xml.etree.ElementTree as ET
from base64 import b64encode
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse
from uuid import uuid4

from sqlalchemy.orm import Session
import yaml

from app.models.script import Script, ScriptStatus, ScriptType
from app.models.task import Task
from app.repositories.script_repository import ScriptRepository
from app.schemas.script import (
    CurlToK6FieldItem,
    CurlToK6ParsedRequest,
    CurlToK6PreviewResponse,
    CurlToK6ScriptCreate,
    CurlToK6VariableSuggestion,
    HarToK6EntryItem,
    HarToK6ParsedRequest,
    HarToK6PreviewResponse,
    HarToK6ScriptCreate,
    HarToK6SpecParseRequest,
    HarToK6SpecParseResponse,
    OpenApiToK6EndpointItem,
    OpenApiToK6ParsedRequest,
    OpenApiToK6PreviewResponse,
    OpenApiToK6ScriptCreate,
    OpenApiToK6SpecParseRequest,
    OpenApiToK6SpecParseResponse,
    ScriptContentUpdate,
    ScriptCreate,
    ScriptUpdate,
)
from common.config.settings import settings
from common.utils import s3_utils

logger = logging.getLogger(__name__)


class ScriptService:
    """脚本业务逻辑层"""

    _LOCAL_SCRIPT_VOLUME_PREFIX = "/tmp_scripts"

    _JMX_INFLUX_BACKEND_CLASSNAME = (
        "org.apache.jmeter.visualizers.backend.influxdb.InfluxdbBackendListenerClient"
    )
    _JMX_COMPAT_INFLUX_BACKEND_CLASSNAME = "io.github.mderevyankoaqa.influxdb2.visualizer.InfluxDatabaseBackendListenerClient"
    _JMX_COMPAT_INFLUX_BACKEND_CLASSNAME_V1 = (
        "org.md.jmeter.influxdb2.visualizer.InfluxDatabaseBackendListenerClient"
    )
    _JMX_INFLUX_BACKEND_CLASSNAME_CANDIDATES = (
        _JMX_INFLUX_BACKEND_CLASSNAME,
        _JMX_COMPAT_INFLUX_BACKEND_CLASSNAME,
        _JMX_COMPAT_INFLUX_BACKEND_CLASSNAME_V1,
    )
    _JMX_INFLUX_ARGUMENTS: tuple[tuple[str, str], ...] = (
        (
            "influxdbMetricsSender",
            "${__P(influxdbMetricsSender,org.apache.jmeter.visualizers.backend.influxdb.HttpMetricsSender)}",
        ),
        (
            "influxdbUrl",
            "${__P(influxdbUrl,http://influxdb:8086/api/v2/write?org=ptp&bucket=ptp)}",
        ),
        ("influxdbToken", "${__P(influxdbToken,)}"),
        ("application", "${__P(application,R001)}"),
        ("measurement", "${__P(measurement,jmeter)}"),
        ("summaryOnly", "${__P(summaryOnly,false)}"),
        ("samplersRegex", "${__P(samplersRegex,.*)}"),
        ("percentiles", "${__P(percentiles,90;95;99)}"),
        ("testTitle", "${__P(testTitle,Test name)}"),
        ("eventTags", "${__P(eventTags,)}"),
        ("TAG_runId", "${__P(TAG_runId,R001)}"),
        ("TAG_taskId", "${__P(TAG_taskId,0)}"),
        ("TAG_nodeName", "${__P(TAG_nodeName,ptp-agent)}"),
    )

    _SENSITIVE_HEADER_ENV_KEYS = {
        "authorization": "AUTHORIZATION",
        "cookie": "COOKIE",
    }

    _NO_VALUE_FLAGS = {
        "-L",
        "--location",
        "-s",
        "--silent",
        "-S",
        "--show-error",
        "-v",
        "--verbose",
        "-k",
        "--insecure",
        "--compressed",
        "--globoff",
        "--http1.1",
        "--http2",
        "--fail",
    }

    _VALUE_IGNORED_OPTIONS = {
        "--proxy",
        "--resolve",
        "--connect-to",
        "--retry",
        "--retry-delay",
        "--max-redirs",
    }

    _OPENAPI_HTTP_METHODS = (
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
    )

    _OPENAPI_SUPPORTED_BODY_CONTENT_TYPES = (
        "application/json",
        "application/x-www-form-urlencoded",
        "text/plain",
    )

    def __init__(self, db: Session):
        self.db = db
        self.repo = ScriptRepository(db)

    @classmethod
    def default_local_scripts_dir(cls) -> Path:
        override = (
            os.getenv("PTP_LOCAL_SCRIPT_DIR")
            or os.getenv("PTP_SCRIPT_DIR")
            or ""
        ).strip()
        if override:
            return cls._resolve_configured_path(override)

        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / "backend" / "ptp-admin").exists():
                return parent / "tmp_scripts"
        return Path(cls._LOCAL_SCRIPT_VOLUME_PREFIX)

    @classmethod
    def _resolve_configured_path(cls, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if path.is_absolute():
            return path
        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / "backend" / "ptp-admin").exists():
                return parent / path
        return Path.cwd() / path

    @classmethod
    def _script_mirror_candidates(cls, file_path: str) -> list[Path]:
        mirror_raw = (
            os.getenv("PTP_LOCAL_SCRIPT_MIRROR_DIR")
            or os.getenv("OPENLOADHUB_HOST_SCRIPT_MIRROR_DIR")
            or ""
        ).strip()
        if not mirror_raw:
            return []

        prefix = (
            os.getenv("PTP_DOCKER_SCRIPT_PREFIX")
            or cls._LOCAL_SCRIPT_VOLUME_PREFIX
        ).rstrip("/")
        path_text = str(file_path or "")
        if not prefix or not path_text.startswith(f"{prefix}/"):
            return []

        relative_name = path_text[len(prefix) :].lstrip("/")
        if not relative_name:
            return []

        mirror_root = cls._resolve_configured_path(mirror_raw)
        return [mirror_root / relative_name]

    @classmethod
    def _resolve_local_script_path(cls, file_path: str, *, for_write: bool = False) -> Path:
        path = Path(file_path)
        if path.exists():
            return path

        for candidate in cls._script_mirror_candidates(file_path):
            if candidate.exists() or for_write:
                if for_write:
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                return candidate
        return path

    @classmethod
    def read_local_script_bytes(cls, file_path: str) -> bytes:
        return cls._resolve_local_script_path(file_path).read_bytes()

    @classmethod
    def write_local_script_text(cls, file_path: str, content: str) -> None:
        target = cls._resolve_local_script_path(file_path, for_write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def load_script_bytes(self, script: Script) -> bytes:
        return self._load_script_bytes(script)

    def load_script_text(self, script: Script) -> str:
        return self._load_script_bytes(script).decode("utf-8", errors="replace")

    def create_script(self, script_in: ScriptCreate, user_id: int) -> Script:
        """创建脚本"""
        # 创建脚本对象
        script_data = script_in.model_dump()
        script_data["created_by"] = user_id
        script_data["status"] = ScriptStatus.ACTIVE

        script = Script(**script_data)
        return self.repo.create(script)

    def get_script_by_hash(self, content_hash: str) -> Optional[Script]:
        if not content_hash:
            return None
        return self.repo.find_by_hash(content_hash)

    def update_script(
        self, script_id: int, script_in: ScriptUpdate
    ) -> Optional[Script]:
        """更新脚本"""
        script = self.repo.find_by_id(script_id)
        if not script:
            raise ValueError("Script not found")

        # 更新字段
        update_data = script_in.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(script, key, value)

        return self.repo.update(script)

    def update_script_content(
        self,
        script_id: int,
        payload: ScriptContentUpdate,
        user_id: Optional[int] = None,
    ) -> Optional[Script]:
        script = self.repo.find_by_id(script_id)
        if not script:
            raise ValueError("Script not found")

        script = self._ensure_task_private_script(
            script=script,
            task_id=payload.task_id,
            user_id=user_id,
        )

        content = payload.content
        content_bytes = content.encode("utf-8")
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        file_size = len(content_bytes)

        if isinstance(script.file_path, str) and script.file_path.startswith("s3://"):
            bucket, key = s3_utils.parse_s3_uri(script.file_path)
            s3_utils.upload_bytes(
                bucket, key, content_bytes, content_type="text/plain; charset=utf-8"
            )
        else:
            self.write_local_script_text(str(script.file_path), content)

        script.content_hash = content_hash
        script.file_size = file_size
        script.version = self._next_version(script.version)
        return self.repo.update(script)

    def _ensure_task_private_script(
        self,
        *,
        script: Script,
        task_id: Optional[int],
        user_id: Optional[int],
    ) -> Script:
        if task_id is None:
            return script

        task = self.db.query(Task).filter(Task.id == task_id).first()
        if task is None:
            raise ValueError("Task not found")
        if int(task.script_id or 0) != int(script.id or 0):
            return script

        shared_count = self.db.query(Task).filter(Task.script_id == script.id).count()
        if shared_count <= 1:
            return script

        content_bytes = self._load_script_bytes(script)
        cloned_path, file_size = self._store_script_copy(
            content_bytes=content_bytes,
            script=script,
        )
        cloned = Script(
            name=script.name,
            description=script.description,
            script_type=script.script_type,
            file_path=cloned_path,
            file_size=file_size,
            content_hash=hashlib.sha256(content_bytes).hexdigest(),
            version=script.version,
            status=script.status,
            tags=(
                json.loads(json.dumps(script.tags)) if script.tags is not None else None
            ),
            parameters=(
                json.loads(json.dumps(script.parameters))
                if script.parameters is not None
                else None
            ),
            created_by=user_id if user_id is not None else script.created_by,
            last_used_at=None,
        )
        self.db.add(cloned)
        self.db.flush()
        task.script_id = cloned.id
        self.db.commit()
        self.db.refresh(cloned)
        return cloned

    def _load_script_bytes(self, script: Script) -> bytes:
        if isinstance(script.file_path, str) and script.file_path.startswith("s3://"):
            bucket, key = s3_utils.parse_s3_uri(script.file_path)
            return s3_utils.download_bytes(bucket, key)
        return self.read_local_script_bytes(str(script.file_path))

    def _store_script_copy(
        self, *, content_bytes: bytes, script: Script
    ) -> tuple[str, int]:
        source_name = Path(script.name or "script").stem or "script"
        suffix = Path(str(script.file_path or "")).suffix or (
            ".js" if script.script_type == ScriptType.K6 else ".jmx"
        )
        unique_name = f"{source_name}-{uuid4().hex[:8]}"
        use_s3 = (
            settings.USE_S3
            and settings.AWS_ACCESS_KEY_ID
            and settings.AWS_SECRET_ACCESS_KEY
        )
        if use_s3:
            key = f"scripts/{unique_name}{suffix}"
            content_type = (
                "text/javascript; charset=utf-8"
                if suffix == ".js"
                else "application/xml"
            )
            s3_utils.upload_bytes(
                settings.S3_BUCKET, key, content_bytes, content_type=content_type
            )
            return f"s3://{settings.S3_BUCKET}/{key}", len(content_bytes)

        scripts_dir = self.default_local_scripts_dir()
        scripts_dir.mkdir(parents=True, exist_ok=True)
        stored_path = scripts_dir / f"{unique_name}{suffix}"
        stored_path.write_bytes(content_bytes)
        return str(stored_path), stored_path.stat().st_size

    def get_script(self, script_id: int) -> Optional[Script]:
        """获取脚本详情"""
        return self.repo.find_by_id(script_id)

    def build_runtime_prepared_content(
        self, script: Script, content: str
    ) -> Optional[str]:
        if script.script_type != ScriptType.JMETER:
            return None
        try:
            runner_cls = self._load_agent_jmeter_runner()
            if runner_cls is not None:
                rendered, changed = runner_cls.render_influx_backend_listener_preview(
                    content,
                    properties={
                        "jmeter_influx_enabled": "1",
                        "influxdbToken": "__ptp_preview__",
                    },
                )
            else:
                rendered, changed = (
                    self._render_jmeter_influx_backend_listener_preview_fallback(
                        content,
                        properties={
                            "jmeter_influx_enabled": "1",
                            "influxdbToken": "__ptp_preview__",
                        },
                    )
                )
        except Exception as exc:
            logger.warning(
                "build_runtime_prepared_content failed for script %s: %s",
                getattr(script, "id", None),
                exc,
            )
            return None
        return rendered if changed else content

    def list_scripts(
        self,
        status: Optional[ScriptStatus] = None,
        script_type: Optional[ScriptType] = None,
        skip: int = 0,
        limit: int = 10,
    ) -> Tuple[List[Script], int]:
        """查询脚本列表"""
        return self.repo.find_all(
            status=status, script_type=script_type, skip=skip, limit=limit
        )

    def search_scripts(
        self, keyword: str, skip: int = 0, limit: int = 10
    ) -> Tuple[List[Script], int]:
        """搜索脚本"""
        return self.repo.search(keyword=keyword, skip=skip, limit=limit)

    def delete_script(self, script_id: int) -> bool:
        """删除脚本（软删除）"""
        script = self.repo.find_by_id(script_id)
        if not script:
            raise ValueError("Script not found")

        return self.repo.delete(script_id)

    def get_script_statistics(self) -> dict:
        """获取脚本统计信息"""
        return self.repo.get_statistics()

    def update_last_used(self, script_id: int) -> Optional[Script]:
        """更新最后使用时间"""
        from datetime import datetime, timezone

        script = self.repo.find_by_id(script_id)
        if script:
            script.last_used_at = datetime.now(timezone.utc)
            return self.repo.update(script)
        return None

    def calculate_file_hash(self, file_path: str) -> Optional[str]:
        """计算文件 SHA256 哈希"""
        if not os.path.exists(file_path):
            return None

        sha256_hash = hashlib.sha256()
        try:
            with open(file_path, "rb") as f:
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
            return sha256_hash.hexdigest()
        except Exception:
            return None

    def build_k6_script_from_curl(self, payload: CurlToK6ScriptCreate) -> tuple[
        str,
        str,
        CurlToK6ParsedRequest,
        list[CurlToK6VariableSuggestion],
        list[str],
    ]:
        parsed = self._parse_curl_command(payload.curl_command)
        content, suggestions, warnings = self._render_k6_script(parsed)
        suggested_name = self._suggest_task_name(parsed["url"], parsed["method"])
        script_name = self._resolve_generated_script_name(payload.name, suggested_name)
        parsed_response = CurlToK6ParsedRequest(
            method=parsed["method"],
            url=parsed["url"],
            protocol="http",
            connect_timeout_ms=parsed.get("connect_timeout_ms"),
            response_timeout_ms=parsed.get("response_timeout_ms"),
            suggested_task_name=suggested_name,
            query_items=parsed.get("query_items") or [],
            header_items=parsed.get("header_items") or [],
            body_mode=parsed.get("body_mode"),
            body_present=bool(parsed.get("body_present")),
            body_preview=parsed.get("body_preview"),
            body_items=parsed.get("body_items") or [],
        )
        return content, script_name, parsed_response, suggestions, warnings

    def preview_k6_script_from_curl(
        self, payload: CurlToK6ScriptCreate
    ) -> CurlToK6PreviewResponse:
        content, _script_name, parsed, suggestions, warnings = (
            self.build_k6_script_from_curl(payload)
        )
        return CurlToK6PreviewResponse(
            parsed=parsed,
            suggested_variables=suggestions,
            warnings=warnings,
            script_content=content,
        )

    def parse_openapi_spec(
        self, payload: OpenApiToK6SpecParseRequest
    ) -> OpenApiToK6SpecParseResponse:
        spec = self._load_openapi_spec(payload.spec_content)
        endpoints = self._collect_openapi_endpoints(spec)
        info = spec.get("info") if isinstance(spec.get("info"), dict) else {}
        server_urls = self._extract_openapi_server_urls(spec.get("servers"))
        supported_endpoint_count = sum(
            1 for item in endpoints if item.request_body_supported
        )
        unsupported_endpoint_count = len(endpoints) - supported_endpoint_count
        warnings: list[str] = []
        if unsupported_endpoint_count > 0:
            warnings.append(
                "共解析 "
                f"{len(endpoints)} 个 endpoint，其中 {supported_endpoint_count} 个可直接生成，"
                f"{unsupported_endpoint_count} 个因请求体类型超出最小范围暂不支持。"
            )
        if len(server_urls) > 1:
            warnings.append(
                "OpenAPI 顶层定义了多个 servers，生成前可先切换目标 server。"
            )
        if len(endpoints) >= 20:
            warnings.append(
                "当前 spec endpoint 较多，建议先按 method/path/summary 搜索后再选单接口。"
            )
        return OpenApiToK6SpecParseResponse(
            title=str(info.get("title") or "").strip() or None,
            version=str(info.get("version") or "").strip() or None,
            server_urls=server_urls,
            endpoints=endpoints,
            supported_endpoint_count=supported_endpoint_count,
            unsupported_endpoint_count=unsupported_endpoint_count,
            warnings=warnings,
        )

    def build_k6_script_from_openapi(self, payload: OpenApiToK6ScriptCreate) -> tuple[
        str,
        str,
        OpenApiToK6ParsedRequest,
        list[CurlToK6VariableSuggestion],
        list[str],
    ]:
        spec = self._load_openapi_spec(payload.spec_content)
        prepared = self._prepare_openapi_generation(
            spec=spec,
            path=payload.path,
            method=payload.method,
            preferred_server_url=payload.server_url,
        )
        content, suggestions, render_warnings = self._render_k6_script(
            prepared["render_payload"]
        )
        warnings = [*prepared["warnings"], *render_warnings]
        script_name = self._resolve_generated_script_name(
            payload.name,
            prepared["suggested_task_name"],
        )
        info = spec.get("info") if isinstance(spec.get("info"), dict) else {}
        parsed_response = OpenApiToK6ParsedRequest(
            title=str(info.get("title") or "").strip() or None,
            version=str(info.get("version") or "").strip() or None,
            method=prepared["method"],
            path=prepared["path"],
            protocol=prepared["protocol"],
            server_url=prepared["server_url"],
            source_url=prepared["source_url"],
            summary=prepared["summary"],
            operation_id=prepared["operation_id"],
            suggested_task_name=prepared["suggested_task_name"],
            request_content_type=prepared["request_content_type"],
            body_mode=prepared["body_mode"],
            body_present=prepared["body_present"],
            path_items=prepared["path_items"],
            query_items=prepared["query_items"],
            header_items=prepared["header_items"],
            body_preview=prepared["body_preview"],
            body_items=prepared["body_items"],
        )
        return content, script_name, parsed_response, suggestions, warnings

    def preview_k6_script_from_openapi(
        self, payload: OpenApiToK6ScriptCreate
    ) -> OpenApiToK6PreviewResponse:
        content, _script_name, parsed, suggestions, warnings = (
            self.build_k6_script_from_openapi(payload)
        )
        return OpenApiToK6PreviewResponse(
            parsed=parsed,
            suggested_variables=suggestions,
            warnings=warnings,
            script_content=content,
        )

    def parse_har_spec(
        self, payload: HarToK6SpecParseRequest
    ) -> HarToK6SpecParseResponse:
        har = self._load_har_archive(payload.har_content)
        entries, unsupported = self._collect_har_entries(har)
        warnings: list[str] = []
        if unsupported > 0:
            warnings.append(
                f"HAR 中有 {unsupported} 个 entry 因缺少 HTTP/HTTPS URL 暂不支持生成。"
            )
        if len(entries) > 20:
            warnings.append("HAR entry 较多，建议先按 URL/方法筛选后再选单接口生成。")
        return HarToK6SpecParseResponse(
            entries=entries,
            supported_entry_count=len(entries),
            unsupported_entry_count=unsupported,
            warnings=warnings,
        )

    def build_k6_script_from_har(self, payload: HarToK6ScriptCreate) -> tuple[
        str,
        str,
        HarToK6ParsedRequest,
        list[CurlToK6VariableSuggestion],
        list[str],
    ]:
        har = self._load_har_archive(payload.har_content)
        prepared = self._prepare_har_generation(har, payload.entry_index)
        content, suggestions, render_warnings = self._render_k6_script(
            prepared["render_payload"]
        )
        warnings = [*prepared["warnings"], *render_warnings]
        script_name = self._resolve_generated_script_name(
            payload.name,
            prepared["suggested_task_name"],
        )
        parsed_response = HarToK6ParsedRequest(
            entry_index=prepared["entry_index"],
            method=prepared["method"],
            url=prepared["url"],
            protocol=prepared["protocol"],
            status=prepared["status"],
            mime_type=prepared["mime_type"],
            suggested_task_name=prepared["suggested_task_name"],
            query_items=prepared["query_items"],
            header_items=prepared["header_items"],
            body_mode=prepared["body_mode"],
            body_present=prepared["body_present"],
            body_preview=prepared["body_preview"],
            body_items=prepared["body_items"],
        )
        return content, script_name, parsed_response, suggestions, warnings

    def preview_k6_script_from_har(
        self, payload: HarToK6ScriptCreate
    ) -> HarToK6PreviewResponse:
        content, _script_name, parsed, suggestions, warnings = (
            self.build_k6_script_from_har(payload)
        )
        return HarToK6PreviewResponse(
            parsed=parsed,
            suggested_variables=suggestions,
            warnings=warnings,
            script_content=content,
        )

    def _next_version(self, current_version: Optional[str]) -> str:
        if not current_version:
            return "1.0"
        parts = current_version.split(".")
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            major, minor = parts
            return f"{major}.{int(minor) + 1}"
        if current_version.isdigit():
            return str(int(current_version) + 1)
        return f"{current_version}.1"

    def _load_agent_jmeter_runner(self):
        candidate_paths = [
            Path(__file__).resolve().parents[3]
            / "ptp-agent"
            / "app"
            / "core"
            / "jmeter_runner.py",
            Path("/app/backend/ptp-agent/app/core/jmeter_runner.py"),
            Path("/workspace/backend/ptp-agent/app/core/jmeter_runner.py"),
        ]
        for runner_path in candidate_paths:
            if not runner_path.exists():
                continue
            spec = importlib.util.spec_from_file_location(
                "ptp_agent_jmeter_runner_for_preview",
                runner_path,
            )
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.JMeterRunner
        return None

    @classmethod
    def _render_jmeter_influx_backend_listener_preview_fallback(
        cls,
        content: str,
        *,
        properties: Optional[dict[str, Any]] = None,
        backend_classname: Optional[str] = None,
    ) -> tuple[str, bool]:
        if not properties:
            return content, False
        enabled = str(properties.get("jmeter_influx_enabled", "1")).strip().lower()
        if enabled in {"0", "false", "off", "no"}:
            return content, False
        if not str(properties.get("influxdbToken") or "").strip():
            return content, False

        resolved_backend_classname = (
            str(backend_classname or cls._JMX_INFLUX_BACKEND_CLASSNAME).strip()
            or cls._JMX_INFLUX_BACKEND_CLASSNAME
        )

        root = ET.fromstring(content)
        root_hash_tree = root.find("hashTree")
        if root_hash_tree is None:
            return content, False

        children = list(root_hash_tree)
        testplan_hash_tree = next(
            (child for child in children if child.tag == "hashTree"),
            None,
        )
        if testplan_hash_tree is None:
            return content, False

        direct_listeners = list(testplan_hash_tree.findall("BackendListener"))
        if direct_listeners:
            if any(
                not cls._is_supported_influx_backend_listener(listener)
                for listener in direct_listeners
            ):
                return content, False
            changed = False
            for listener in direct_listeners:
                changed = (
                    cls._canonicalize_influx_backend_listener_fallback(
                        listener,
                        backend_classname=resolved_backend_classname,
                    )
                    or changed
                )
            if changed:
                tree = ET.ElementTree(root)
                if hasattr(ET, "indent"):
                    ET.indent(tree, space="  ")
                return ET.tostring(root, encoding="unicode"), True
            return content, False

        active_listeners = [
            listener
            for listener in root.findall(".//BackendListener")
            if cls._is_enabled_backend_listener(listener)
        ]
        for listener in active_listeners:
            if not cls._is_supported_influx_backend_listener(listener):
                continue
            changed = cls._canonicalize_influx_backend_listener_fallback(
                listener,
                backend_classname=resolved_backend_classname,
            )
            if changed:
                tree = ET.ElementTree(root)
                if hasattr(ET, "indent"):
                    ET.indent(tree, space="  ")
                return ET.tostring(root, encoding="unicode"), True
            return content, False

        if active_listeners:
            return content, False

        backend_listener = cls._build_influx_backend_listener_fallback(
            backend_classname=resolved_backend_classname
        )
        testplan_hash_tree.append(backend_listener)
        testplan_hash_tree.append(ET.Element("hashTree"))

        tree = ET.ElementTree(root)
        if hasattr(ET, "indent"):
            ET.indent(tree, space="  ")
        return ET.tostring(root, encoding="unicode"), True

    @staticmethod
    def _is_enabled_backend_listener(listener: ET.Element) -> bool:
        enabled = str(listener.attrib.get("enabled", "")).strip().lower()
        return not enabled or enabled == "true"

    @classmethod
    def _is_supported_influx_backend_listener(cls, listener: ET.Element) -> bool:
        classname = listener.find("./stringProp[@name='classname']")
        current = (classname.text or "").strip() if classname is not None else ""
        return current in cls._JMX_INFLUX_BACKEND_CLASSNAME_CANDIDATES

    @classmethod
    def _build_influx_backend_listener_fallback(
        cls,
        *,
        backend_classname: str,
    ) -> ET.Element:
        backend_listener = ET.Element(
            "BackendListener",
            {
                "guiclass": "BackendListenerGui",
                "testclass": "BackendListener",
                "testname": "Backend Listener",
                "enabled": "true",
            },
        )
        arguments = ET.SubElement(
            backend_listener,
            "elementProp",
            {
                "name": "arguments",
                "elementType": "Arguments",
                "guiclass": "ArgumentsPanel",
                "testclass": "Arguments",
                "enabled": "true",
            },
        )
        collection = ET.SubElement(
            arguments,
            "collectionProp",
            {"name": "Arguments.arguments"},
        )
        for name, value in cls._JMX_INFLUX_ARGUMENTS:
            argument = ET.SubElement(
                collection,
                "elementProp",
                {"name": name, "elementType": "Argument"},
            )
            ET.SubElement(argument, "stringProp", {"name": "Argument.name"}).text = name
            ET.SubElement(argument, "stringProp", {"name": "Argument.value"}).text = (
                value
            )
            ET.SubElement(
                argument, "stringProp", {"name": "Argument.metadata"}
            ).text = "="
        ET.SubElement(backend_listener, "stringProp", {"name": "classname"}).text = (
            backend_classname
        )
        return backend_listener

    @classmethod
    def _canonicalize_influx_backend_listener_fallback(
        cls,
        listener: ET.Element,
        *,
        backend_classname: str,
    ) -> bool:
        expected = cls._build_influx_backend_listener_fallback(
            backend_classname=backend_classname
        )
        changed = False
        for key in ("guiclass", "testclass", "testname"):
            expected_value = expected.attrib.get(key)
            if listener.attrib.get(key) != expected_value:
                listener.set(key, expected_value or "")
                changed = True
        classname = listener.find("./stringProp[@name='classname']")
        if classname is None:
            classname = ET.SubElement(listener, "stringProp", {"name": "classname"})
            changed = True
        if (classname.text or "").strip() != backend_classname:
            classname.text = backend_classname
            changed = True
        queue_size = listener.find("./stringProp[@name='queueSize']")
        if queue_size is not None:
            listener.remove(queue_size)
            changed = True
        existing_arguments = listener.find("./elementProp[@name='arguments']")
        if existing_arguments is not None:
            listener.remove(existing_arguments)
            changed = True
        expected_arguments = expected.find("./elementProp[@name='arguments']")
        if expected_arguments is not None:
            listener.insert(0, expected_arguments)
        return changed

    def _parse_curl_command(self, curl_command: str) -> dict[str, Any]:
        raw = str(curl_command or "").strip()
        if not raw:
            raise ValueError("CURL 命令不能为空")

        normalized = raw.replace("\r\n", "\n")
        normalized = re.sub(r"\\\s*\n", " ", normalized)
        try:
            tokens = shlex.split(normalized, posix=True)
        except ValueError as exc:
            raise ValueError(f"CURL 命令解析失败：{exc}") from exc

        if not tokens:
            raise ValueError("CURL 命令不能为空")

        executable = Path(tokens[0]).name.lower()
        if executable not in {"curl", "curl.exe"}:
            raise ValueError("仅支持以 curl 开头的命令")

        headers: dict[str, str] = {}
        data_segments: list[str] = []
        url: Optional[str] = None
        method: Optional[str] = None
        connect_timeout_ms: Optional[int] = None
        response_timeout_ms: Optional[int] = None
        body_mode = "raw"
        force_get = False

        index = 1
        while index < len(tokens):
            token = tokens[index]

            if token in self._NO_VALUE_FLAGS:
                index += 1
                continue

            if token in {"-X", "--request"}:
                index += 1
                method = self._require_option_value(tokens, index, token).upper()
                index += 1
                continue

            if token in {"-H", "--header"}:
                index += 1
                header_value = self._require_option_value(tokens, index, token)
                key, value = self._split_header(header_value)
                headers[key] = value
                index += 1
                continue

            if token in {"-A", "--user-agent"}:
                index += 1
                headers["User-Agent"] = self._require_option_value(tokens, index, token)
                index += 1
                continue

            if token in {"-e", "--referer"}:
                index += 1
                headers["Referer"] = self._require_option_value(tokens, index, token)
                index += 1
                continue

            if token in {"-u", "--user"}:
                index += 1
                user_info = self._require_option_value(tokens, index, token)
                encoded = b64encode(user_info.encode("utf-8")).decode("ascii")
                headers["Authorization"] = f"Basic {encoded}"
                index += 1
                continue

            if token in {"-b", "--cookie"}:
                index += 1
                cookie_value = self._require_option_value(tokens, index, token)
                if headers.get("Cookie"):
                    headers["Cookie"] = f"{headers['Cookie']}; {cookie_value}"
                else:
                    headers["Cookie"] = cookie_value
                index += 1
                continue

            if token in {"-d", "--data", "--data-raw", "--data-binary", "--data-ascii"}:
                index += 1
                data_segments.append(self._require_option_value(tokens, index, token))
                index += 1
                continue

            if token == "--json":
                index += 1
                data_segments.append(self._require_option_value(tokens, index, token))
                headers.setdefault("Content-Type", "application/json")
                headers.setdefault("Accept", "application/json")
                body_mode = "json"
                index += 1
                continue

            if token in {"-G", "--get"}:
                force_get = True
                index += 1
                continue

            if token in {"-I", "--head"}:
                method = "HEAD"
                index += 1
                continue

            if token == "--url":
                index += 1
                url = self._require_option_value(tokens, index, token)
                index += 1
                continue

            if token == "--connect-timeout":
                index += 1
                connect_timeout_ms = self._parse_timeout_ms(
                    self._require_option_value(tokens, index, token),
                    option_name=token,
                )
                index += 1
                continue

            if token == "--max-time":
                index += 1
                response_timeout_ms = self._parse_timeout_ms(
                    self._require_option_value(tokens, index, token),
                    option_name=token,
                )
                index += 1
                continue

            if (
                token in {"-F", "--form", "--form-string", "-T", "--upload-file"}
                or token.startswith("--form=")
                or token.startswith("--form-string=")
                or token.startswith("--upload-file=")
                or (token.startswith("-F") and token != "-F")
                or (token.startswith("-T") and token != "-T")
            ):
                raise ValueError(
                    "当前仅支持普通 HTTP 请求，暂不支持 form/file 上传类 CURL"
                )

            if token in self._VALUE_IGNORED_OPTIONS:
                index += 2
                continue

            if self._looks_like_http_url(token):
                url = token
                index += 1
                continue

            if token.startswith("-"):
                index += 1
                continue

            if url is None and self._looks_like_http_url(token):
                url = token

            index += 1

        if url is None:
            raise ValueError("未识别到有效的 HTTP/HTTPS URL")

        if data_segments:
            body = "&".join(segment for segment in data_segments if segment)
        else:
            body = None

        if force_get and body:
            url = self._append_query_string(url, body)
            body = None

        if method is None:
            method = "POST" if body else "GET"

        method = method.upper()
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("仅支持 HTTP/HTTPS 单接口 CURL")

        content_type = headers.get("Content-Type") or headers.get("content-type") or ""
        if body_mode != "json" and "application/json" in content_type.lower():
            body_mode = "json"

        query_items = [
            CurlToK6FieldItem(key=key or "(empty)", value=value)
            for key, value in parse_qsl(parsed_url.query, keep_blank_values=True)
        ]
        header_items = [
            CurlToK6FieldItem(key=key, value=value) for key, value in headers.items()
        ]
        body_items: list[CurlToK6FieldItem] = []
        body_preview: Optional[str] = None
        if isinstance(body, str) and body:
            if body_mode == "json":
                try:
                    parsed_body = json.loads(body)
                    body_preview = json.dumps(parsed_body, ensure_ascii=False, indent=2)
                    if isinstance(parsed_body, dict):
                        body_items = [
                            CurlToK6FieldItem(
                                key=str(key),
                                value=(
                                    value
                                    if isinstance(value, str)
                                    else json.dumps(value, ensure_ascii=False)
                                ),
                            )
                            for key, value in parsed_body.items()
                        ]
                except json.JSONDecodeError:
                    body_preview = body
            else:
                body_preview = body
                if "=" in body:
                    body_items = [
                        CurlToK6FieldItem(key=key or "(empty)", value=value)
                        for key, value in parse_qsl(body, keep_blank_values=True)
                    ]
            if len(body_preview) > 1000:
                body_preview = f"{body_preview[:1000]}..."

        return {
            "method": method,
            "url": url,
            "headers": headers,
            "body": body,
            "body_mode": body_mode,
            "body_present": bool(body),
            "body_preview": body_preview,
            "body_items": body_items,
            "connect_timeout_ms": connect_timeout_ms,
            "response_timeout_ms": response_timeout_ms,
            "query_items": query_items,
            "header_items": header_items,
        }

    def _render_k6_script(
        self, parsed: dict[str, Any]
    ) -> tuple[str, list[CurlToK6VariableSuggestion], list[str]]:
        method = str(parsed["method"]).upper()
        url = str(parsed["url"])
        parsed_url = urlparse(url)
        base_url = (
            f"{parsed_url.scheme}://{parsed_url.netloc}"
            if parsed_url.scheme and parsed_url.netloc
            else "https://example.com"
        )
        request_path = parsed_url.path or "/"
        if parsed_url.query:
            request_path = f"{request_path}?{parsed_url.query}"
        endpoint_name = str(
            parsed.get("endpoint_name") or f"{method} {parsed_url.path or '/'}"
        )

        suggestions: list[CurlToK6VariableSuggestion] = [
            CurlToK6VariableSuggestion(
                key="BASE_URL",
                value=base_url,
                sensitive=False,
                source=str(parsed.get("base_url_source") or "curl:url"),
            ),
            CurlToK6VariableSuggestion(
                key="target_tps",
                value="10",
                sensitive=False,
                source="platform:standard_k6_total_tps",
            ),
            CurlToK6VariableSuggestion(
                key="vus",
                value="10",
                sensitive=False,
                source="platform:vu_mode_default",
            ),
            CurlToK6VariableSuggestion(
                key="duration",
                value="300",
                sensitive=False,
                source="platform:duration_mode_default",
            ),
            CurlToK6VariableSuggestion(
                key="loops",
                value="0",
                sensitive=False,
                source="platform:iterations_mode_optional",
            ),
        ]
        warnings: list[str] = []
        suggestion_keys = {item.key for item in suggestions}

        for item in parsed.get("extra_suggestions") or []:
            if (
                isinstance(item, CurlToK6VariableSuggestion)
                and item.key not in suggestion_keys
            ):
                suggestions.append(item)
                suggestion_keys.add(item.key)

        header_lines: list[str] = []
        render_headers = parsed.get("render_headers")
        if isinstance(render_headers, list) and render_headers:
            for item in render_headers:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "").strip()
                if not key:
                    continue
                lowered = key.lower()
                if lowered in {"host", "content-length"}:
                    warnings.append(f"已忽略不应固化到脚本中的 Header：{key}")
                    continue
                if lowered == "accept-encoding":
                    warnings.append(
                        "已忽略 Accept-Encoding，避免压测脚本和运行时压缩策略耦合"
                    )
                    continue
                fallback_value = self._stringify_openapi_sample(item.get("value"))
                env_key = str(item.get("env_key") or "").strip()
                if env_key:
                    header_lines.append(
                        f"  {json.dumps(key, ensure_ascii=False)}: __ENV.{env_key} || {json.dumps(fallback_value, ensure_ascii=False)},"
                    )
                else:
                    header_lines.append(
                        f"  {json.dumps(key, ensure_ascii=False)}: {json.dumps(fallback_value, ensure_ascii=False)},"
                    )
        else:
            for key, value in parsed.get("headers", {}).items():
                lowered = key.lower()
                if lowered in {"host", "content-length"}:
                    warnings.append(f"已忽略不应固化到脚本中的 Header：{key}")
                    continue
                if lowered == "accept-encoding":
                    warnings.append(
                        "已忽略 Accept-Encoding，避免压测脚本和运行时压缩策略耦合"
                    )
                    continue

                if lowered in self._SENSITIVE_HEADER_ENV_KEYS:
                    env_key = self._SENSITIVE_HEADER_ENV_KEYS[lowered]
                    header_lines.append(
                        f"  {json.dumps(key, ensure_ascii=False)}: __ENV.{env_key} || 'replace-me',"
                    )
                    if env_key not in suggestion_keys:
                        suggestions.append(
                            CurlToK6VariableSuggestion(
                                key=env_key,
                                value="replace-me",
                                sensitive=True,
                                source=f"curl:header:{key}",
                            )
                        )
                        suggestion_keys.add(env_key)
                else:
                    header_lines.append(
                        f"  {json.dumps(key, ensure_ascii=False)}: {json.dumps(value, ensure_ascii=False)},"
                    )

        body_block = "const requestBody = undefined;"
        request_body_source = str(parsed.get("request_body_source") or "").strip()
        if request_body_source:
            body_block = f"const requestBody = {request_body_source};"
        else:
            request_body = parsed.get("body")
            if isinstance(request_body, str) and request_body:
                if parsed.get("body_mode") == "json":
                    try:
                        payload = json.loads(request_body)
                    except json.JSONDecodeError:
                        body_block = (
                            "const requestBody = "
                            f"{json.dumps(request_body, ensure_ascii=False)};"
                        )
                    else:
                        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
                        body_block = (
                            f"const requestBody = JSON.stringify({serialized});"
                        )
                else:
                    body_block = (
                        "const requestBody = "
                        f"{json.dumps(request_body, ensure_ascii=False)};"
                    )

        timeout_block = ""
        response_timeout_ms = parsed.get("response_timeout_ms")
        if isinstance(response_timeout_ms, int) and response_timeout_ms > 0:
            timeout_block = (
                "  timeout: "
                f"__ENV.REQUEST_TIMEOUT_MS ? `${{Number(__ENV.REQUEST_TIMEOUT_MS)}}ms` : '{response_timeout_ms}ms',\n"
            )
            suggestions.append(
                CurlToK6VariableSuggestion(
                    key="REQUEST_TIMEOUT_MS",
                    value=str(response_timeout_ms),
                    sensitive=False,
                    source="curl:max-time",
                )
            )

        connect_timeout_ms = parsed.get("connect_timeout_ms")
        if isinstance(connect_timeout_ms, int) and connect_timeout_ms > 0:
            warnings.append(
                "curl 的 connect-timeout 已识别，但当前 k6 脚本只映射整体请求超时，不单独透传连接超时。"
            )

        request_path_prelude = str(parsed.get("request_path_prelude") or "").strip()
        request_path_source = str(parsed.get("request_path_source") or "").strip()
        if request_path_source:
            request_path_block = (
                f"{request_path_prelude}\nconst requestPath = {request_path_source};"
                if request_path_prelude
                else f"const requestPath = {request_path_source};"
            )
        else:
            request_path_block = (
                f"const requestPath = {json.dumps(request_path, ensure_ascii=False)};"
            )

        headers_block = (
            "const requestHeaders = {\n"
            + ("\n".join(header_lines) + "\n" if header_lines else "")
            + "};"
        )

        script_content = (
            "import http from 'k6/http';\n"
            "import { check, group } from 'k6';\n\n"
            f"const method = {json.dumps(method)};\n"
            f"{request_path_block}\n"
            f"const endpointName = {json.dumps(endpoint_name, ensure_ascii=False)};\n"
            f"const defaultBaseUrl = {json.dumps(base_url, ensure_ascii=False)};\n"
            "const baseUrl = (__ENV.BASE_URL || defaultBaseUrl || 'http://example.test').replace(/\\/+$/, '');\n"
            "const totalTargetTps = Math.max(0, Number(__ENV.target_tps || __ENV.TARGET_TPS || '0'));\n"
            "const podCount = Math.max(1, Number(__ENV.pod_count || __ENV.POD_COUNT || '1'));\n"
            "const vus = Math.max(1, Number(__ENV.vus || __ENV.VUS || __ENV.PTP_THREAD_COUNT || '1'));\n"
            "const durationSeconds = Math.max(0, Number(__ENV.duration || __ENV.DURATION || __ENV.PTP_DURATION_SECONDS || '0'));\n"
            "const loops = Math.max(0, Number(__ENV.loops || __ENV.LOOPS || __ENV.PTP_LOOPS || '0'));\n"
            f"{headers_block}\n"
            f"{body_block}\n\n"
            "function gcd(a, b) {\n"
            "  let left = Math.abs(a);\n"
            "  let right = Math.abs(b);\n"
            "  while (right > 0) {\n"
            "    const next = left % right;\n"
            "    left = right;\n"
            "    right = next;\n"
            "  }\n"
            "  return Math.max(1, left || 1);\n"
            "}\n\n"
            "function buildArrivalRateScenario(totalTps, workers) {\n"
            "  const normalizedTps = Number.isFinite(totalTps) ? Math.max(0, Math.floor(totalTps)) : 0;\n"
            "  const denominator = Math.max(1, Math.floor(workers));\n"
            "  if (normalizedTps <= 0) {\n"
            "    return null;\n"
            "  }\n"
            "  const divisor = gcd(normalizedTps, denominator);\n"
            "  const rate = Math.max(1, Math.floor(normalizedTps / divisor));\n"
            "  const timeUnitSeconds = Math.max(1, Math.floor(denominator / divisor));\n"
            "  const preAllocatedVUs = Math.max(1, vus, Math.ceil(normalizedTps / denominator));\n"
            "  return {\n"
            "    executor: 'constant-arrival-rate',\n"
            "    rate,\n"
            "    timeUnit: `${timeUnitSeconds}s`,\n"
            "    duration: `${Math.max(1, durationSeconds || 300)}s`,\n"
            "    preAllocatedVUs,\n"
            "    maxVUs: Math.max(preAllocatedVUs, preAllocatedVUs * 4),\n"
            "  };\n"
            "}\n\n"
            "function buildOptions() {\n"
            "  if (durationSeconds > 0 && loops > 0) {\n"
            "    throw new Error('duration and loops are mutually exclusive for the generated standard k6 scenario');\n"
            "  }\n"
            "  if (totalTargetTps > 0 && loops > 0) {\n"
            "    throw new Error('target_tps mode requires duration-based execution for the generated standard k6 scenario');\n"
            "  }\n"
            "  if (totalTargetTps > 0) {\n"
            "    return {\n"
            "      scenarios: {\n"
            "        request_endpoint: {\n"
            "          ...buildArrivalRateScenario(totalTargetTps, podCount),\n"
            "          exec: 'runRequestScenario',\n"
            "        },\n"
            "      },\n"
            "    };\n"
            "  }\n"
            "  if (loops > 0) {\n"
            "    return {\n"
            "      vus,\n"
            "      iterations: loops,\n"
            "    };\n"
            "  }\n"
            "  return {\n"
            "    vus,\n"
            "    duration: `${Math.max(1, durationSeconds || 300)}s`,\n"
            "  };\n"
            "}\n\n"
            "export const options = {\n"
            "  ...buildOptions(),\n"
            "  thresholds: {\n"
            "    [`http_req_failed{name:${endpointName}}`]: ['rate<1'],\n"
            "    [`http_req_duration{name:${endpointName}}`]: ['p(95)>=0'],\n"
            "  },\n"
            "};\n\n"
            "function doRequest() {\n"
            "  return group(endpointName, () => {\n"
            "    const response = http.request(method, `${baseUrl}${requestPath}`, requestBody, {\n"
            "      headers: requestHeaders,\n"
            f"{timeout_block}"
            "      tags: {\n"
            "        name: endpointName,\n"
            "        endpoint_name: endpointName,\n"
            "      },\n"
            "    });\n\n"
            "    check(response, {\n"
            "      'status is ok': (r) => r.status >= 200 && r.status < 400,\n"
            "    });\n"
            "    return response;\n"
            "  });\n"
            "}\n\n"
            "export function runRequestScenario() {\n"
            "  return doRequest();\n"
            "}\n\n"
            "export default function () {\n"
            "  return doRequest();\n"
            "}\n"
        )
        return script_content, suggestions, warnings

    def _load_openapi_spec(self, spec_content: str) -> dict[str, Any]:
        raw = str(spec_content or "").strip()
        if not raw:
            raise ValueError("OpenAPI spec 不能为空")

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            try:
                payload = yaml.safe_load(raw)
            except Exception as exc:
                raise ValueError(f"OpenAPI spec 解析失败：{exc}") from exc

        if not isinstance(payload, dict):
            raise ValueError("OpenAPI spec 必须是 JSON/YAML 对象")

        version = str(payload.get("openapi") or "").strip()
        if not version.startswith("3."):
            raise ValueError("当前仅支持 OpenAPI 3.x JSON/YAML")

        paths = payload.get("paths")
        if not isinstance(paths, dict) or not paths:
            raise ValueError("OpenAPI spec 未包含可解析的 paths")

        return payload

    def _load_har_archive(self, har_content: str) -> dict[str, Any]:
        raw = str(har_content or "").strip()
        if not raw:
            raise ValueError("HAR 内容不能为空")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"HAR JSON 解析失败：{exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("HAR 顶层必须是 JSON 对象")
        log = payload.get("log")
        if not isinstance(log, dict) or not isinstance(log.get("entries"), list):
            raise ValueError("HAR 缺少 log.entries")
        return payload

    def _collect_har_entries(
        self, har: dict[str, Any]
    ) -> tuple[list[HarToK6EntryItem], int]:
        entries = har.get("log", {}).get("entries", [])
        supported: list[HarToK6EntryItem] = []
        unsupported = 0
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                unsupported += 1
                continue
            request = entry.get("request")
            if not isinstance(request, dict):
                unsupported += 1
                continue
            method = str(request.get("method") or "GET").upper()
            url = str(request.get("url") or "").strip()
            parsed_url = urlparse(url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                unsupported += 1
                continue
            response = (
                entry.get("response") if isinstance(entry.get("response"), dict) else {}
            )
            content = (
                response.get("content")
                if isinstance(response.get("content"), dict)
                else {}
            )
            post_data = (
                request.get("postData")
                if isinstance(request.get("postData"), dict)
                else {}
            )
            supported.append(
                HarToK6EntryItem(
                    index=index,
                    method=method,
                    url=url,
                    path=parsed_url.path or "/",
                    status=(
                        int(response["status"])
                        if isinstance(response.get("status"), int)
                        else None
                    ),
                    mime_type=(
                        str(
                            post_data.get("mimeType") or content.get("mimeType") or ""
                        ).strip()
                        or None
                    ),
                    body_present=bool(post_data.get("text") or post_data.get("params")),
                    started_at=(
                        str(entry.get("startedDateTime"))
                        if entry.get("startedDateTime")
                        else None
                    ),
                )
            )
        return supported, unsupported

    def _prepare_har_generation(
        self,
        har: dict[str, Any],
        entry_index: int,
    ) -> dict[str, Any]:
        entries = har.get("log", {}).get("entries", [])
        if entry_index >= len(entries):
            raise ValueError("HAR entry_index 超出范围")
        entry = entries[entry_index]
        if not isinstance(entry, dict) or not isinstance(entry.get("request"), dict):
            raise ValueError("选中的 HAR entry 缺少 request")

        request = entry["request"]
        method = str(request.get("method") or "GET").upper()
        url = str(request.get("url") or "").strip()
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("选中的 HAR entry 不是 HTTP/HTTPS 请求")

        headers = self._har_name_value_items_to_dict(request.get("headers"))
        query_items = self._har_name_value_items_to_field_items(
            request.get("queryString")
        )
        if not query_items:
            query_items = [
                CurlToK6FieldItem(key=key or "(empty)", value=value)
                for key, value in parse_qsl(parsed_url.query, keep_blank_values=True)
            ]
        header_items = [
            CurlToK6FieldItem(key=key, value=value) for key, value in headers.items()
        ]

        post_data = (
            request.get("postData") if isinstance(request.get("postData"), dict) else {}
        )
        body, body_mode, body_preview, body_items = self._extract_har_body(post_data)
        response = (
            entry.get("response") if isinstance(entry.get("response"), dict) else {}
        )
        status = (
            int(response["status"]) if isinstance(response.get("status"), int) else None
        )
        mime_type = str(post_data.get("mimeType") or "").strip() or None
        suggested_task_name = self._suggest_task_name(url, method)
        warnings: list[str] = []
        if body and body_mode == "raw":
            warnings.append(
                "HAR 请求体已按 raw 文本生成；复杂 multipart/file 上传需人工复核。"
            )
        if not headers:
            warnings.append("HAR entry 未包含请求 Header，脚本将使用空 header。")

        return {
            "entry_index": entry_index,
            "method": method,
            "url": url,
            "protocol": parsed_url.scheme,
            "status": status,
            "mime_type": mime_type,
            "suggested_task_name": suggested_task_name,
            "query_items": query_items,
            "header_items": header_items,
            "body_mode": body_mode,
            "body_present": bool(body),
            "body_preview": body_preview,
            "body_items": body_items,
            "warnings": warnings,
            "render_payload": {
                "method": method,
                "url": url,
                "headers": headers,
                "body": body,
                "body_mode": body_mode,
                "body_present": bool(body),
                "body_preview": body_preview,
                "body_items": body_items,
                "query_items": query_items,
                "header_items": header_items,
                "endpoint_name": f"{method} {parsed_url.path or '/'}",
                "base_url_source": "har:request.url",
            },
        }

    @staticmethod
    def _har_name_value_items_to_dict(items: Any) -> dict[str, str]:
        result: dict[str, str] = {}
        if not isinstance(items, list):
            return result
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            result[name] = str(item.get("value") or "")
        return result

    @staticmethod
    def _har_name_value_items_to_field_items(items: Any) -> list[CurlToK6FieldItem]:
        result: list[CurlToK6FieldItem] = []
        if not isinstance(items, list):
            return result
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            result.append(
                CurlToK6FieldItem(key=name, value=str(item.get("value") or ""))
            )
        return result

    def _extract_har_body(
        self, post_data: dict[str, Any]
    ) -> tuple[Optional[str], Optional[str], Optional[str], list[CurlToK6FieldItem]]:
        if not post_data:
            return None, None, None, []
        mime_type = str(post_data.get("mimeType") or "").lower()
        body_mode = "json" if "application/json" in mime_type else "raw"
        text = post_data.get("text")
        body = str(text) if isinstance(text, str) and text else None
        params = self._har_name_value_items_to_field_items(post_data.get("params"))
        if body is None and params:
            body = urlencode([(item.key, item.value) for item in params])
            body_mode = "raw"

        body_preview: Optional[str] = None
        body_items: list[CurlToK6FieldItem] = params
        if body:
            if body_mode == "json":
                try:
                    parsed_body = json.loads(body)
                    body_preview = json.dumps(parsed_body, ensure_ascii=False, indent=2)
                    if isinstance(parsed_body, dict):
                        body_items = [
                            CurlToK6FieldItem(
                                key=str(key),
                                value=(
                                    value
                                    if isinstance(value, str)
                                    else json.dumps(value, ensure_ascii=False)
                                ),
                            )
                            for key, value in parsed_body.items()
                        ]
                except json.JSONDecodeError:
                    body_preview = body
            else:
                body_preview = body
                if not body_items and "=" in body:
                    body_items = [
                        CurlToK6FieldItem(key=key or "(empty)", value=value)
                        for key, value in parse_qsl(body, keep_blank_values=True)
                    ]
            if body_preview and len(body_preview) > 1000:
                body_preview = f"{body_preview[:1000]}..."
        return body, body_mode, body_preview, body_items

    def _collect_openapi_endpoints(
        self, spec: dict[str, Any]
    ) -> list[OpenApiToK6EndpointItem]:
        endpoints: list[OpenApiToK6EndpointItem] = []
        paths = spec.get("paths") if isinstance(spec.get("paths"), dict) else {}
        for path in sorted(paths.keys(), key=str):
            path_item = self._resolve_openapi_node(spec, paths[path])
            if not isinstance(path_item, dict):
                continue
            for method in self._OPENAPI_HTTP_METHODS:
                operation = self._resolve_openapi_node(spec, path_item.get(method))
                if not isinstance(operation, dict):
                    continue
                content_types = self._list_openapi_request_content_types(
                    spec, operation
                )
                endpoints.append(
                    OpenApiToK6EndpointItem(
                        method=method.upper(),
                        path=str(path),
                        summary=str(
                            operation.get("summary")
                            or operation.get("description")
                            or ""
                        ).strip()
                        or None,
                        operation_id=str(operation.get("operationId") or "").strip()
                        or None,
                        tags=[
                            str(item).strip()
                            for item in (operation.get("tags") or [])
                            if str(item).strip()
                        ],
                        request_content_types=content_types,
                        request_body_supported=(
                            not content_types
                            or any(
                                self._is_supported_openapi_content_type(item)
                                for item in content_types
                            )
                        ),
                        server_url=self._pick_openapi_server_url(
                            spec=spec,
                            path_item=path_item,
                            operation=operation,
                            preferred_server_url=None,
                        ),
                    )
                )
        if not endpoints:
            raise ValueError("OpenAPI spec 未识别到可用 HTTP endpoint")
        return endpoints

    def _prepare_openapi_generation(
        self,
        *,
        spec: dict[str, Any],
        path: str,
        method: str,
        preferred_server_url: Optional[str],
    ) -> dict[str, Any]:
        normalized_path = str(path or "").strip()
        normalized_method = str(method or "").strip().lower()
        if normalized_method not in self._OPENAPI_HTTP_METHODS:
            raise ValueError("当前仅支持标准 HTTP method 的 OpenAPI endpoint")

        paths = spec.get("paths") if isinstance(spec.get("paths"), dict) else {}
        path_item = self._resolve_openapi_node(spec, paths.get(normalized_path))
        if not isinstance(path_item, dict):
            raise ValueError(f"OpenAPI spec 中未找到 endpoint：{normalized_path}")

        operation = self._resolve_openapi_node(spec, path_item.get(normalized_method))
        if not isinstance(operation, dict):
            raise ValueError(
                f"OpenAPI spec 中未找到 endpoint：{normalized_method.upper()} {normalized_path}"
            )

        warnings: list[str] = []
        server_candidates = [
            item
            for item in [
                *self._extract_openapi_server_urls(operation.get("servers")),
                *self._extract_openapi_server_urls(path_item.get("servers")),
                *self._extract_openapi_server_urls(spec.get("servers")),
            ]
            if item
        ]
        if preferred_server_url and preferred_server_url not in server_candidates:
            server_candidates = [preferred_server_url, *server_candidates]
        server_url = self._pick_openapi_server_url(
            spec=spec,
            path_item=path_item,
            operation=operation,
            preferred_server_url=preferred_server_url,
        )
        if len(server_candidates) > 1 and not preferred_server_url:
            warnings.append(
                "OpenAPI 定义了多个 servers，当前先使用第一个可用 server；如需切换，请在生成前显式选择。"
            )

        base_url, path_prefix, server_warnings = self._resolve_openapi_server_context(
            server_url
        )
        warnings.extend(server_warnings)

        suggestions: list[CurlToK6VariableSuggestion] = []
        query_map: dict[str, dict[str, str]] = {}
        header_map: dict[str, dict[str, Any]] = {}
        path_param_map: dict[str, dict[str, str]] = {}

        def add_suggestion(
            *,
            key: str,
            value: str,
            sensitive: bool,
            source: str,
        ) -> None:
            if any(item.key == key for item in suggestions):
                return
            suggestions.append(
                CurlToK6VariableSuggestion(
                    key=key,
                    value=value,
                    sensitive=sensitive,
                    source=source,
                )
            )

        def add_query_param(name: str, sample_text: str, *, source: str) -> None:
            env_key = self._normalize_env_key("QUERY", name)
            query_map[name] = {"name": name, "sample": sample_text, "env_key": env_key}
            add_suggestion(
                key=env_key,
                value=sample_text,
                sensitive=self._looks_sensitive_name(name),
                source=source,
            )

        def add_header(
            name: str, sample_text: str, *, source: str, sensitive: bool | None = None
        ) -> None:
            env_key = self._normalize_env_key("HEADER", name)
            header_map[name.lower()] = {
                "key": name,
                "value": sample_text,
                "env_key": env_key,
            }
            add_suggestion(
                key=env_key,
                value=sample_text,
                sensitive=(
                    self._looks_sensitive_name(name) if sensitive is None else sensitive
                ),
                source=source,
            )

        def add_path_param(name: str, sample_text: str, *, source: str) -> None:
            env_key = self._normalize_env_key("PATH", name)
            path_param_map[name] = {
                "name": name,
                "sample": sample_text,
                "env_key": env_key,
            }
            add_suggestion(
                key=env_key,
                value=sample_text,
                sensitive=False,
                source=source,
            )

        parameters = self._merge_openapi_parameters(
            spec,
            path_item.get("parameters"),
            operation.get("parameters"),
        )
        for parameter in parameters:
            location = str(parameter.get("in") or "").strip().lower()
            name = str(parameter.get("name") or "").strip()
            if not name or location not in {"path", "query", "header"}:
                continue
            sample_text = (
                self._stringify_openapi_sample(
                    self._build_openapi_parameter_sample(spec, parameter)
                )
                or "demo"
            )
            if location == "path":
                add_path_param(name, sample_text, source=f"openapi:path:{name}")
            elif location == "query":
                add_query_param(name, sample_text, source=f"openapi:query:{name}")
            elif location == "header":
                add_header(name, sample_text, source=f"openapi:header:{name}")

        security_headers, security_queries, security_suggestions, security_warnings = (
            self._build_openapi_security_context(
                spec,
                operation,
            )
        )
        warnings.extend(security_warnings)
        for key, value in security_headers.items():
            header_map[key] = value
        for item in security_queries:
            query_map[item["name"]] = item
        for suggestion in security_suggestions:
            add_suggestion(
                key=suggestion.key,
                value=suggestion.value,
                sensitive=suggestion.sensitive,
                source=str(suggestion.source or "openapi:security"),
            )

        full_path_template = self._join_openapi_paths(path_prefix, normalized_path)
        path_tokens = re.findall(r"{([^{}]+)}", normalized_path)
        for token in path_tokens:
            if token in path_param_map:
                continue
            add_path_param(token, "demo", source=f"openapi:path:{token}")
            warnings.append(
                f"OpenAPI path 参数 {token} 缺少 schema/example，已使用 demo 占位。"
            )

        sample_request_path = full_path_template
        js_request_path = full_path_template
        ordered_path_items: list[CurlToK6FieldItem] = []
        for token in path_tokens:
            context = path_param_map[token]
            sample_request_path = sample_request_path.replace(
                "{" + token + "}",
                quote(context["sample"], safe=""),
            )
            js_request_path = js_request_path.replace(
                "{" + token + "}",
                f"${{encodeURIComponent(__ENV.{context['env_key']} || {json.dumps(context['sample'], ensure_ascii=False)})}}",
            )
            ordered_path_items.append(
                CurlToK6FieldItem(key=context["name"], value=context["sample"])
            )

        request_path_source = (
            f"`{js_request_path}`"
            if "${" in js_request_path
            else json.dumps(js_request_path, ensure_ascii=False)
        )
        request_path_prelude = ""
        query_items = [
            CurlToK6FieldItem(key=item["name"], value=item["sample"])
            for item in query_map.values()
        ]
        if query_map:
            request_path_prelude = (
                "const requestQuery = new URLSearchParams({\n"
                + "\n".join(
                    [
                        f"  {json.dumps(item['name'], ensure_ascii=False)}: String(__ENV.{item['env_key']} || {json.dumps(item['sample'], ensure_ascii=False)}),"
                        for item in query_map.values()
                    ]
                )
                + "\n}).toString();"
            )
            request_path_source = (
                f"{request_path_source} + (requestQuery ? `?${{requestQuery}}` : '')"
            )
            sample_query = urlencode(
                [(item["name"], item["sample"]) for item in query_map.values()],
                doseq=True,
            )
            if sample_query:
                sample_request_path = f"{sample_request_path}?{sample_query}"

        request_body_source = ""
        body_mode: Optional[str] = None
        body_present = False
        body_preview: Optional[str] = None
        body_items: list[CurlToK6FieldItem] = []
        request_content_type: Optional[str] = None
        request_body = self._resolve_openapi_node(spec, operation.get("requestBody"))
        if isinstance(request_body, dict):
            content = (
                request_body.get("content")
                if isinstance(request_body.get("content"), dict)
                else {}
            )
            request_content_type = self._select_openapi_request_content_type(
                list(content.keys())
            )
            media = self._resolve_openapi_node(spec, content.get(request_content_type))
            if request_content_type and not self._is_supported_openapi_content_type(
                request_content_type
            ):
                raise ValueError(
                    "当前 OpenAPI -> k6 最小实现只支持无 body、application/json、application/x-www-form-urlencoded 或 text/plain"
                )
            example_value = (
                self._extract_openapi_example(media)
                if isinstance(media, dict)
                else None
            )
            if example_value is None and isinstance(media, dict):
                schema = self._resolve_openapi_node(spec, media.get("schema"))
                example_value = self._build_openapi_schema_example(
                    spec, schema, hint="body"
                )
                if example_value not in (None, {}, []):
                    warnings.append(
                        "当前 endpoint 的请求体缺少显式 example，脚本草稿已按 schema 自动推断最小示例。"
                    )

            if self._is_json_content_type(request_content_type):
                body_mode = "json"
                body_present = True
                if example_value is None:
                    example_value = {}
                body_preview = json.dumps(example_value, ensure_ascii=False, indent=2)
                request_body_source = f"JSON.stringify({json.dumps(example_value, ensure_ascii=False, indent=2)})"
                if isinstance(example_value, dict):
                    body_items = [
                        CurlToK6FieldItem(
                            key=str(key),
                            value=self._stringify_openapi_sample(value),
                        )
                        for key, value in example_value.items()
                    ]
                header_map.setdefault(
                    "content-type",
                    {
                        "key": "Content-Type",
                        "value": request_content_type,
                        "env_key": "",
                    },
                )
            elif request_content_type == "application/x-www-form-urlencoded":
                body_mode = "form"
                body_present = True
                if not isinstance(example_value, dict):
                    example_value = {}
                body_preview = json.dumps(example_value, ensure_ascii=False, indent=2)
                body_items = [
                    CurlToK6FieldItem(
                        key=str(key), value=self._stringify_openapi_sample(value)
                    )
                    for key, value in example_value.items()
                ]
                request_body_source = (
                    "new URLSearchParams({\n"
                    + "\n".join(
                        [
                            f"  {json.dumps(str(key), ensure_ascii=False)}: {json.dumps(self._stringify_openapi_sample(value), ensure_ascii=False)},"
                            for key, value in example_value.items()
                        ]
                    )
                    + "\n}).toString()"
                )
                header_map.setdefault(
                    "content-type",
                    {
                        "key": "Content-Type",
                        "value": request_content_type,
                        "env_key": "",
                    },
                )
            elif request_content_type == "text/plain":
                body_mode = "raw"
                body_present = True
                body_preview = self._stringify_openapi_sample(example_value)
                request_body_source = json.dumps(body_preview, ensure_ascii=False)
                header_map.setdefault(
                    "content-type",
                    {
                        "key": "Content-Type",
                        "value": request_content_type,
                        "env_key": "",
                    },
                )

        header_items = [
            CurlToK6FieldItem(
                key=str(item["key"]),
                value=self._stringify_openapi_sample(item.get("value")),
            )
            for item in header_map.values()
        ]
        render_headers = list(header_map.values())

        source_url = f"{base_url.rstrip('/')}{sample_request_path}"
        suggested_task_name = self._suggest_task_name(
            source_url, normalized_method.upper()
        )
        summary = (
            str(operation.get("summary") or operation.get("description") or "").strip()
            or None
        )
        operation_id = str(operation.get("operationId") or "").strip() or None
        protocol = urlparse(source_url).scheme or "https"

        return {
            "method": normalized_method.upper(),
            "path": normalized_path,
            "protocol": protocol,
            "server_url": server_url,
            "source_url": source_url,
            "summary": summary,
            "operation_id": operation_id,
            "suggested_task_name": suggested_task_name,
            "request_content_type": request_content_type,
            "body_mode": body_mode,
            "body_present": body_present,
            "body_preview": body_preview,
            "body_items": body_items,
            "path_items": ordered_path_items,
            "query_items": query_items,
            "header_items": header_items,
            "warnings": warnings,
            "render_payload": {
                "method": normalized_method.upper(),
                "url": source_url,
                "endpoint_name": f"{normalized_method.upper()} {normalized_path}",
                "base_url_source": "openapi:server",
                "request_path_prelude": request_path_prelude,
                "request_path_source": request_path_source,
                "render_headers": render_headers,
                "request_body_source": request_body_source,
                "body_mode": body_mode,
                "extra_suggestions": suggestions,
            },
        }

    def _resolve_openapi_node(
        self, spec: dict[str, Any], node: Any, *, depth: int = 0
    ) -> Any:
        if depth > 12 or not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if not ref:
            return node
        resolved = self._resolve_openapi_ref(spec, str(ref))
        if not isinstance(resolved, dict):
            return resolved
        if len(node) == 1:
            return self._resolve_openapi_node(spec, resolved, depth=depth + 1)
        merged = dict(resolved)
        for key, value in node.items():
            if key != "$ref":
                merged[key] = value
        return self._resolve_openapi_node(spec, merged, depth=depth + 1)

    def _resolve_openapi_ref(self, spec: dict[str, Any], ref: str) -> Any:
        if not ref.startswith("#/"):
            raise ValueError(f"当前仅支持 OpenAPI 本地 $ref：{ref}")
        cursor: Any = spec
        for part in ref[2:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(cursor, dict) or key not in cursor:
                raise ValueError(f"OpenAPI $ref 无法解析：{ref}")
            cursor = cursor[key]
        return cursor

    def _extract_openapi_server_urls(self, servers: Any) -> list[str]:
        if not isinstance(servers, list):
            return []
        results: list[str] = []
        for item in servers:
            resolved = item if isinstance(item, dict) else None
            url = str((resolved or {}).get("url") or "").strip()
            if url:
                results.append(url)
        return results

    def _pick_openapi_server_url(
        self,
        *,
        spec: dict[str, Any],
        path_item: dict[str, Any],
        operation: dict[str, Any],
        preferred_server_url: Optional[str],
    ) -> Optional[str]:
        if preferred_server_url:
            return str(preferred_server_url).strip() or None
        for candidate in (
            *self._extract_openapi_server_urls(operation.get("servers")),
            *self._extract_openapi_server_urls(path_item.get("servers")),
            *self._extract_openapi_server_urls(spec.get("servers")),
        ):
            if candidate:
                return candidate
        return None

    def _resolve_openapi_server_context(
        self, server_url: Optional[str]
    ) -> tuple[str, str, list[str]]:
        warnings: list[str] = []
        raw = str(server_url or "").strip()
        if not raw:
            warnings.append(
                "OpenAPI 未定义 server，已用 https://example.com 作为 BASE_URL 默认值；生成后请先调整为真实地址。"
            )
            return "https://example.com", "", warnings

        parsed = urlparse(raw)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            return base_url, (parsed.path or "").rstrip("/"), warnings

        if raw.startswith("/"):
            warnings.append(
                "OpenAPI server 使用相对路径，脚本已把 https://example.com 作为 BASE_URL 默认值，并把 server path 追加到请求路径。"
            )
            return "https://example.com", raw.rstrip("/"), warnings

        warnings.append(
            "OpenAPI server 不是标准 HTTP/HTTPS URL，脚本已直接使用其字面值作为 BASE_URL 默认值。"
        )
        return raw.rstrip("/"), "", warnings

    def _list_openapi_request_content_types(
        self, spec: dict[str, Any], operation: dict[str, Any]
    ) -> list[str]:
        request_body = self._resolve_openapi_node(spec, operation.get("requestBody"))
        if not isinstance(request_body, dict):
            return []
        content = request_body.get("content")
        if not isinstance(content, dict):
            return []
        return [str(key).strip() for key in content.keys() if str(key).strip()]

    def _select_openapi_request_content_type(
        self, content_types: list[str]
    ) -> Optional[str]:
        if not content_types:
            return None
        for preferred in (
            "application/json",
            "application/x-www-form-urlencoded",
            "text/plain",
        ):
            for candidate in content_types:
                if preferred == "application/json" and self._is_json_content_type(
                    candidate
                ):
                    return candidate
                if candidate == preferred:
                    return candidate
        return content_types[0]

    def _is_supported_openapi_content_type(self, content_type: str) -> bool:
        candidate = str(content_type or "").strip().lower()
        return (
            self._is_json_content_type(candidate)
            or candidate in self._OPENAPI_SUPPORTED_BODY_CONTENT_TYPES
        )

    @staticmethod
    def _is_json_content_type(content_type: Optional[str]) -> bool:
        candidate = str(content_type or "").strip().lower()
        return candidate == "application/json" or candidate.endswith("+json")

    def _merge_openapi_parameters(
        self,
        spec: dict[str, Any],
        path_parameters: Any,
        operation_parameters: Any,
    ) -> list[dict[str, Any]]:
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for raw_group in (path_parameters, operation_parameters):
            if not isinstance(raw_group, list):
                continue
            for item in raw_group:
                resolved = self._resolve_openapi_node(spec, item)
                if not isinstance(resolved, dict):
                    continue
                name = str(resolved.get("name") or "").strip()
                location = str(resolved.get("in") or "").strip().lower()
                if not name or not location:
                    continue
                merged[(name, location)] = resolved
        return list(merged.values())

    def _build_openapi_security_context(
        self, spec: dict[str, Any], operation: dict[str, Any]
    ) -> tuple[
        dict[str, dict[str, Any]],
        list[dict[str, str]],
        list[CurlToK6VariableSuggestion],
        list[str],
    ]:
        warnings: list[str] = []
        headers: dict[str, dict[str, Any]] = {}
        queries: list[dict[str, str]] = []
        suggestions: list[CurlToK6VariableSuggestion] = []
        raw_security = operation.get("security")
        if raw_security is None:
            raw_security = spec.get("security")
        if raw_security == []:
            return headers, queries, suggestions, warnings
        if not isinstance(raw_security, list):
            return headers, queries, suggestions, warnings

        requirement = next(
            (item for item in raw_security if isinstance(item, dict)), None
        )
        if requirement is None:
            return headers, queries, suggestions, warnings
        if sum(1 for item in raw_security if isinstance(item, dict)) > 1:
            warnings.append(
                "当前 endpoint 定义了多个 security 备选方案，脚本草稿只自动映射第一组可识别方案。"
            )

        components = (
            spec.get("components") if isinstance(spec.get("components"), dict) else {}
        )
        security_schemes = (
            components.get("securitySchemes")
            if isinstance(components.get("securitySchemes"), dict)
            else {}
        )
        for scheme_name in requirement.keys():
            scheme = self._resolve_openapi_node(spec, security_schemes.get(scheme_name))
            if not isinstance(scheme, dict):
                warnings.append(f"安全方案 {scheme_name} 未找到定义，已跳过自动映射。")
                continue
            scheme_type = str(scheme.get("type") or "").strip().lower()
            if scheme_type == "http":
                auth_scheme = str(scheme.get("scheme") or "").strip().lower()
                if auth_scheme == "bearer":
                    headers["authorization"] = {
                        "key": "Authorization",
                        "value": "Bearer replace-me",
                        "env_key": "AUTHORIZATION",
                    }
                    suggestions.append(
                        CurlToK6VariableSuggestion(
                            key="AUTHORIZATION",
                            value="Bearer replace-me",
                            sensitive=True,
                            source=f"openapi:security:{scheme_name}",
                        )
                    )
                elif auth_scheme == "basic":
                    headers["authorization"] = {
                        "key": "Authorization",
                        "value": "Basic replace-me",
                        "env_key": "AUTHORIZATION",
                    }
                    suggestions.append(
                        CurlToK6VariableSuggestion(
                            key="AUTHORIZATION",
                            value="Basic replace-me",
                            sensitive=True,
                            source=f"openapi:security:{scheme_name}",
                        )
                    )
                    warnings.append(
                        "HTTP Basic 鉴权仅生成 Authorization 占位值，真实 base64 凭证请在生成后手工替换。"
                    )
                else:
                    warnings.append(
                        f"安全方案 {scheme_name} 使用的 HTTP scheme={auth_scheme or 'unknown'} 暂不自动接线。"
                    )
            elif scheme_type == "apiKey":
                param_name = str(scheme.get("name") or scheme_name or "api_key").strip()
                location = str(scheme.get("in") or "").strip().lower()
                env_prefix = "QUERY" if location == "query" else "HEADER"
                env_key = self._normalize_env_key(env_prefix, param_name)
                suggestion = CurlToK6VariableSuggestion(
                    key=env_key,
                    value="replace-me",
                    sensitive=True,
                    source=f"openapi:security:{scheme_name}",
                )
                suggestions.append(suggestion)
                if location == "header":
                    headers[param_name.lower()] = {
                        "key": param_name,
                        "value": "replace-me",
                        "env_key": env_key,
                    }
                elif location == "query":
                    queries.append(
                        {"name": param_name, "sample": "replace-me", "env_key": env_key}
                    )
                else:
                    warnings.append(
                        f"安全方案 {scheme_name} 使用 apiKey in={location or 'unknown'}，当前不自动映射。"
                    )
            else:
                warnings.append(
                    f"安全方案 {scheme_name} 的 type={scheme_type or 'unknown'} 当前不自动映射。"
                )

        return headers, queries, suggestions, warnings

    def _extract_openapi_example(self, node: Any) -> Any:
        if not isinstance(node, dict):
            return None
        if "example" in node:
            return node.get("example")
        examples = node.get("examples")
        if isinstance(examples, dict):
            for item in examples.values():
                if isinstance(item, dict) and "value" in item:
                    return item.get("value")
                if item is not None:
                    return item
        return None

    def _build_openapi_parameter_sample(
        self, spec: dict[str, Any], parameter: dict[str, Any]
    ) -> Any:
        example = self._extract_openapi_example(parameter)
        if example is not None:
            return example
        schema = self._resolve_openapi_node(spec, parameter.get("schema"))
        return self._build_openapi_schema_example(
            spec,
            schema,
            hint=str(parameter.get("name") or "param"),
        )

    def _build_openapi_schema_example(
        self,
        spec: dict[str, Any],
        schema: Any,
        *,
        hint: str = "value",
        depth: int = 0,
    ) -> Any:
        if depth > 8:
            return None
        resolved = self._resolve_openapi_node(spec, schema, depth=0)
        if not isinstance(resolved, dict):
            return None

        example = self._extract_openapi_example(resolved)
        if example is not None:
            return example
        if "default" in resolved:
            return resolved.get("default")
        enum_values = resolved.get("enum")
        if isinstance(enum_values, list) and enum_values:
            return enum_values[0]
        one_of = resolved.get("oneOf")
        if isinstance(one_of, list) and one_of:
            return self._build_openapi_schema_example(
                spec, one_of[0], hint=hint, depth=depth + 1
            )
        any_of = resolved.get("anyOf")
        if isinstance(any_of, list) and any_of:
            return self._build_openapi_schema_example(
                spec, any_of[0], hint=hint, depth=depth + 1
            )
        all_of = resolved.get("allOf")
        if isinstance(all_of, list) and all_of:
            merged: dict[str, Any] = {}
            for item in all_of:
                value = self._build_openapi_schema_example(
                    spec, item, hint=hint, depth=depth + 1
                )
                if isinstance(value, dict):
                    merged.update(value)
            return merged or None

        schema_type = str(resolved.get("type") or "").strip().lower()
        if schema_type == "object" or isinstance(resolved.get("properties"), dict):
            properties = (
                resolved.get("properties")
                if isinstance(resolved.get("properties"), dict)
                else {}
            )
            result: dict[str, Any] = {}
            for key, value in properties.items():
                child = self._build_openapi_schema_example(
                    spec, value, hint=str(key), depth=depth + 1
                )
                if child is not None:
                    result[str(key)] = child
            return result
        if schema_type == "array":
            items = resolved.get("items")
            child = self._build_openapi_schema_example(
                spec, items, hint=hint, depth=depth + 1
            )
            return [] if child is None else [child]
        if schema_type == "integer":
            return 1
        if schema_type == "number":
            return 1
        if schema_type == "boolean":
            return True
        if schema_type == "string":
            fmt = str(resolved.get("format") or "").strip().lower()
            if fmt == "uuid":
                return "00000000-0000-0000-0000-000000000000"
            if fmt == "date":
                return "2026-04-16"
            if fmt == "date-time":
                return "2026-04-16T00:00:00Z"
            if fmt == "email":
                return "demo@example.com"
            return "demo"
        return None

    @staticmethod
    def _normalize_env_key(prefix: str, name: str) -> str:
        normalized = (
            re.sub(r"[^a-zA-Z0-9]+", "_", str(name or "")).strip("_").upper() or "VALUE"
        )
        return f"{prefix}_{normalized}" if prefix else normalized

    @staticmethod
    def _join_openapi_paths(prefix: str, path: str) -> str:
        parts = [
            segment.strip("/")
            for segment in (prefix, path)
            if str(segment or "").strip("/")
        ]
        return "/" + "/".join(parts) if parts else "/"

    @staticmethod
    def _stringify_openapi_sample(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    @staticmethod
    def _looks_sensitive_name(name: str) -> bool:
        lowered = str(name or "").strip().lower()
        return any(
            token in lowered for token in ("auth", "token", "secret", "cookie", "key")
        )

    def _resolve_generated_script_name(
        self, requested_name: Optional[str], suggested_name: str
    ) -> str:
        base_name = str(requested_name or suggested_name or "curl-k6-script").strip()
        normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", base_name).strip("-._")
        if not normalized:
            normalized = "curl-k6-script"
        return normalized[:255]

    def _suggest_task_name(self, url: str, method: str) -> str:
        parsed = urlparse(url)
        host = re.sub(r"[^a-zA-Z0-9]+", "-", parsed.netloc).strip("-")
        path = re.sub(r"[^a-zA-Z0-9]+", "-", parsed.path or "/").strip("-")
        suffix = path or "root"
        method_prefix = method.lower()
        name = (
            f"{method_prefix}-{host}-{suffix}" if host else f"{method_prefix}-{suffix}"
        )
        return name[:120] or "curl-k6-task"

    @staticmethod
    def _require_option_value(tokens: list[str], index: int, option_name: str) -> str:
        if index >= len(tokens):
            raise ValueError(f"{option_name} 缺少参数值")
        return tokens[index]

    @staticmethod
    def _split_header(raw_header: str) -> tuple[str, str]:
        if ":" not in raw_header:
            raise ValueError(f"Header 格式非法：{raw_header}")
        key, value = raw_header.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Header 键为空：{raw_header}")
        return key, value

    @staticmethod
    def _parse_timeout_ms(raw_value: str, option_name: str) -> int:
        try:
            seconds = float(raw_value)
        except ValueError as exc:
            raise ValueError(f"{option_name} 不是合法数字：{raw_value}") from exc
        if seconds <= 0:
            raise ValueError(f"{option_name} 必须大于 0")
        return round(seconds * 1000)

    @staticmethod
    def _looks_like_http_url(value: str) -> bool:
        lowered = str(value or "").lower()
        return lowered.startswith("http://") or lowered.startswith("https://")

    @staticmethod
    def _append_query_string(url: str, body: str) -> str:
        parsed = urlparse(url)
        existing = parse_qsl(parsed.query, keep_blank_values=True)
        appended = parse_qsl(body, keep_blank_values=True)
        query = urlencode(existing + appended, doseq=True)
        return urlunparse(parsed._replace(query=query))
