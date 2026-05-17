from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import json
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from shutil import move
import tempfile
from typing import Any, Optional
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.models.script import Script
from app.models.task import Task
from app.models.task_asset import TaskAsset
from app.models.user import User, UserRole
from app.repositories.task_asset_repository import TaskAssetRepository
from common.utils import s3_utils
from common.config.settings import settings


def _task_assets_dir() -> Path:
    root_dir = Path(__file__).resolve().parent.parent.parent.parent.parent
    assets_dir = root_dir / "tmp_task_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    return assets_dir


@dataclass
class _StagedUpload:
    path: Path
    file_size: int
    content_hash: str


@dataclass
class _PreparedAsset:
    path: Path
    file_name: str
    file_size: int
    content_hash: str
    line_count: Optional[int]
    compression_type: Optional[str]
    compressed_file_size: Optional[int]
    metadata_json: dict[str, Any]
    content_type: Optional[str]


@dataclass
class _DataHeaderInfo:
    has_header: bool
    header_line_count: int
    data_line_count: int
    header_line: Optional[bytes]


class TaskAssetService:
    CHUNK_SIZE = 1024 * 1024
    SMALL_DATA_MAX_SIZE = 50 * 1024 * 1024
    DEFAULT_ALL_DISTRIBUTION_MAX_BYTES = 200 * 1024 * 1024
    DEFAULT_DATA_SHARD_COUNT = 1
    DEFAULT_MAX_DATA_SHARD_COUNT = 10000
    DEFAULT_LARGE_DATA_MAX_SIZE = 5 * 1024 * 1024 * 1024
    DEFAULT_ZIP_MAX_COMPRESSION_RATIO = 100
    DEFAULT_DIRECT_UPLOAD_EXPIRE_SECONDS = 3600
    DATA_FILE_PARAM_KEYS = ("DATA_FILE",)
    PROTO_FILE_PARAM_KEYS = (
        "proto_file",
        "PROTO_FILE",
        "GRPC_PROTO_FILE",
        "grpc_proto_file",
        "PTP_PROTO_FILE",
    )
    SCRIPT_PROTO_LOAD_PATTERN = re.compile(
        r"""\bload\s*\(\s*(?:\[[^\)]*\]\s*,\s*)?["']([^"']+\.proto)["']""",
        re.IGNORECASE | re.DOTALL,
    )
    SCRIPT_PROTO_SCAN_MAX_BYTES = 512 * 1024

    CATEGORY_RULES = {
        "proto": {
            "extensions": {"proto"},
            "max_size": 1 * 1024 * 1024,
        },
        "data": {
            "extensions": {"csv", "txt", "json", "zip"},
            "max_size": DEFAULT_LARGE_DATA_MAX_SIZE,
        },
    }

    def __init__(self, db: Session):
        self.db = db
        self.repo = TaskAssetRepository(db)

    def upload_asset(
        self,
        file: UploadFile,
        category: str,
        user_id: Optional[int],
        task_id: Optional[int] = None,
        shard_count: Optional[int] = None,
    ) -> TaskAsset:
        category_value = category.strip().lower()
        filename = file.filename or "uploaded"
        rule, ext, max_size = self._validate_asset_upload_request(
            category=category_value,
            filename=filename,
        )

        if task_id is not None:
            self._ensure_task_owner(task_id, user_id)

        staged = self._stage_upload(file, max_size=max_size, category=category_value)
        prepared: Optional[_PreparedAsset] = None
        try:
            prepared = self._prepare_asset(
                staged=staged,
                filename=filename,
                category=category_value,
                ext=ext,
                content_type=file.content_type,
            )
            requires_s3 = self._requires_object_storage(
                prepared, category=category_value
            )
            if requires_s3 and not self._s3_enabled():
                raise ValueError("Large data assets require MinIO/S3 storage")

            unique_name = self._build_unique_storage_name(prepared.file_name)
            file_path = self._store_prepared_file(
                prepared.path,
                unique_name,
                category_value,
                prepared.content_type,
                force_s3=requires_s3,
            )
            try:
                metadata_json = self._build_asset_metadata(
                    prepared=prepared,
                    category=category_value,
                    file_path=file_path,
                    shard_count=shard_count,
                    source_path=self._resolve_metadata_source_path(
                        prepared.path, file_path
                    ),
                )
            except Exception:
                self._remove_file(file_path)
                raise
            asset = TaskAsset(
                task_id=task_id,
                category=category_value,
                file_name=prepared.file_name,
                file_path=file_path,
                file_size=prepared.file_size,
                content_hash=prepared.content_hash,
                storage_type="s3" if str(file_path).startswith("s3://") else "local",
                compression_type=prepared.compression_type,
                compressed_file_size=prepared.compressed_file_size,
                line_count=prepared.line_count,
                ingest_status="completed",
                ingest_error=None,
                metadata_json=metadata_json,
                created_by=user_id,
            )
            return self.repo.create(asset)
        finally:
            self._remove_temp_path(staged.path)
            if prepared is not None:
                self._remove_temp_path(prepared.path)

    def create_direct_upload_session(
        self,
        *,
        category: str,
        file_name: str,
        file_size: int,
        content_hash_sha256: str,
        content_type: Optional[str],
        task_id: Optional[int],
        user_id: Optional[int],
        shard_count: Optional[int] = None,
    ) -> dict[str, Any]:
        category_value = category.strip().lower()
        filename = self._safe_upload_file_name(file_name)
        self._validate_asset_upload_request(
            category=category_value,
            filename=filename,
            file_size=file_size,
        )
        expected_hash = self._normalize_sha256(content_hash_sha256)
        if not expected_hash:
            raise ValueError("content_hash_sha256 must be a sha256 hex digest")
        if task_id is not None:
            self._ensure_task_owner(task_id, user_id)
        if not self._s3_enabled():
            raise ValueError("Direct task asset upload requires MinIO/S3 storage")

        expires_in = self._env_int(
            "TASK_ASSET_DIRECT_UPLOAD_EXPIRE_SECONDS",
            self.DEFAULT_DIRECT_UPLOAD_EXPIRE_SECONDS,
        )
        if expires_in <= 0:
            expires_in = self.DEFAULT_DIRECT_UPLOAD_EXPIRE_SECONDS
        expires_at = int(time.time()) + expires_in
        session_id = uuid4().hex
        normalized_content_type = (
            (content_type or "").strip()
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )
        key = (
            "task-assets/direct-upload/"
            f"{user_id or 'anonymous'}/{session_id}/{filename}"
        )
        metadata = {
            "sha256": expected_hash,
            "session-id": session_id,
            "category": category_value,
        }
        upload_url = s3_utils.generate_presigned_put_url(
            settings.S3_BUCKET,
            key,
            expires_in=expires_in,
            content_type=normalized_content_type,
            metadata=metadata,
        )
        token_payload = {
            "bucket": settings.S3_BUCKET,
            "category": category_value,
            "content_hash_sha256": expected_hash,
            "content_type": normalized_content_type,
            "expires_at": expires_at,
            "file_name": filename,
            "file_size": int(file_size),
            "object_key": key,
            "session_id": session_id,
            "shard_count": self._normalize_shard_count(shard_count),
            "task_id": int(task_id) if task_id is not None else None,
            "user_id": int(user_id) if user_id is not None else None,
        }
        return {
            "session_id": session_id,
            "upload_method": "PUT",
            "upload_url": upload_url,
            "upload_headers": {
                "Content-Type": normalized_content_type,
                "x-amz-meta-sha256": expected_hash,
                "x-amz-meta-session-id": session_id,
                "x-amz-meta-category": category_value,
            },
            "bucket": settings.S3_BUCKET,
            "object_key": key,
            "object_uri": f"s3://{settings.S3_BUCKET}/{key}",
            "expires_in_seconds": expires_in,
            "expires_at": expires_at,
            "finalize_token": self._sign_direct_upload_token(token_payload),
        }

    def finalize_direct_upload(
        self,
        *,
        session_id: str,
        finalize_token: str,
        user_id: Optional[int],
    ) -> TaskAsset:
        payload = self._verify_direct_upload_token(
            finalize_token,
            expected_session_id=session_id,
        )
        payload_user_id = payload.get("user_id")
        if payload_user_id is not None and int(payload_user_id) != int(user_id or 0):
            raise PermissionError("Forbidden: upload session owner only")
        task_id = payload.get("task_id")
        if task_id is not None:
            self._ensure_task_owner(int(task_id), user_id)

        bucket = str(payload["bucket"])
        key = str(payload["object_key"])
        file_name = str(payload["file_name"])
        category = str(payload["category"])
        expected_size = int(payload["file_size"])
        expected_hash = str(payload["content_hash_sha256"])
        content_type = str(payload.get("content_type") or "") or None
        shard_count = self._normalize_shard_count(payload.get("shard_count"))

        try:
            head = s3_utils.head_object(bucket, key)
        except Exception as exc:
            raise ValueError("Direct upload object is missing") from exc
        content_length = int(head.get("ContentLength") or 0)
        if content_length != expected_size:
            raise ValueError("Direct upload object size mismatch")

        try:
            staged_path = self._download_direct_upload_object(bucket, key, file_name)
        except Exception as exc:
            raise ValueError("Direct upload object download failed") from exc
        staged = _StagedUpload(
            path=staged_path,
            file_size=staged_path.stat().st_size,
            content_hash=self._hash_file(staged_path),
        )
        prepared: Optional[_PreparedAsset] = None
        stored_file_path: Optional[str] = None
        try:
            if staged.file_size != expected_size:
                raise ValueError("Direct upload object size mismatch")
            if staged.content_hash != expected_hash:
                raise ValueError("Direct upload checksum mismatch")

            _, ext, _ = self._validate_asset_upload_request(
                category=category,
                filename=file_name,
                file_size=staged.file_size,
            )
            prepared = self._prepare_asset(
                staged=staged,
                filename=file_name,
                category=category,
                ext=ext,
                content_type=content_type,
            )
            if prepared.path == staged.path:
                stored_file_path = f"s3://{bucket}/{key}"
                storage_type = "s3"
            else:
                unique_name = self._build_unique_storage_name(prepared.file_name)
                stored_file_path = self._store_prepared_file(
                    prepared.path,
                    unique_name,
                    category,
                    prepared.content_type,
                    force_s3=True,
                )
                storage_type = "s3"
                try:
                    s3_utils.delete_object(bucket, key)
                except Exception:
                    pass
            try:
                metadata_json = self._build_asset_metadata(
                    prepared=prepared,
                    category=category,
                    file_path=stored_file_path,
                    shard_count=shard_count,
                    source_path=self._resolve_metadata_source_path(
                        prepared.path, stored_file_path
                    ),
                )
            except Exception:
                if stored_file_path:
                    self._remove_file(stored_file_path)
                raise
            metadata_json.update(
                {
                    "upload_method": "direct_s3",
                    "direct_upload_session_id": session_id,
                    "direct_upload_source_uri": f"s3://{bucket}/{key}",
                }
            )

            asset = TaskAsset(
                task_id=int(task_id) if task_id is not None else None,
                category=category,
                file_name=prepared.file_name,
                file_path=stored_file_path,
                file_size=prepared.file_size,
                content_hash=prepared.content_hash,
                storage_type=storage_type,
                compression_type=prepared.compression_type,
                compressed_file_size=prepared.compressed_file_size,
                line_count=prepared.line_count,
                ingest_status="completed",
                ingest_error=None,
                metadata_json=metadata_json,
                created_by=user_id,
            )
            return self.repo.create(asset)
        finally:
            self._remove_temp_path(staged.path)
            if prepared is not None and prepared.path != staged.path:
                self._remove_temp_path(prepared.path)

    def list_assets(
        self,
        task_id: Optional[int] = None,
        category: Optional[str] = None,
        created_by: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> list[TaskAsset]:
        if task_id is not None:
            self._ensure_task_owner(task_id, user_id)
        return self.repo.find_all(
            task_id=task_id, category=category, created_by=created_by
        )

    def bind_assets(
        self, task_id: int, asset_ids: list[int], user_id: Optional[int]
    ) -> list[TaskAsset]:
        self._ensure_task_owner(task_id, user_id)
        assets = self.repo.find_many(asset_ids)
        if len(assets) != len(set(asset_ids)):
            raise ValueError("Some assets were not found")
        bound_assets: list[TaskAsset] = []
        for asset in assets:
            if (
                asset.created_by
                and user_id
                and int(asset.created_by) != int(user_id)
                and not self._is_task_access_exempt_user(user_id)
            ):
                raise PermissionError("Forbidden: owner only")
            bound_assets.append(
                self.clone_asset_to_task(
                    asset=asset,
                    task_id=task_id,
                    user_id=user_id,
                )
            )
        return bound_assets

    def clone_asset_to_task(
        self,
        *,
        asset: TaskAsset,
        task_id: int,
        user_id: Optional[int],
    ) -> TaskAsset:
        if self._should_clone_by_reference(asset):
            return self.repo.create(
                TaskAsset(
                    task_id=task_id,
                    category=asset.category,
                    file_name=asset.file_name,
                    file_path=asset.file_path,
                    file_size=asset.file_size,
                    content_hash=asset.content_hash,
                    storage_type=(
                        "s3"
                        if str(asset.file_path).startswith("s3://")
                        else asset.storage_type
                    ),
                    compression_type=asset.compression_type,
                    compressed_file_size=asset.compressed_file_size,
                    line_count=asset.line_count,
                    ingest_status=asset.ingest_status,
                    ingest_error=asset.ingest_error,
                    metadata_json=asset.metadata_json,
                    created_by=user_id if user_id is not None else asset.created_by,
                )
            )

        content = self._read_file_bytes(asset.file_path)
        file_path = self._store_cloned_asset_file(
            content=content,
            asset=asset,
        )
        new_asset = TaskAsset(
            task_id=task_id,
            category=asset.category,
            file_name=asset.file_name,
            file_path=file_path,
            file_size=len(content),
            content_hash=hashlib.sha256(content).hexdigest(),
            storage_type="s3" if str(file_path).startswith("s3://") else "local",
            compression_type=asset.compression_type,
            compressed_file_size=asset.compressed_file_size,
            line_count=asset.line_count,
            ingest_status=asset.ingest_status,
            ingest_error=asset.ingest_error,
            metadata_json=asset.metadata_json,
            created_by=user_id if user_id is not None else asset.created_by,
        )
        return self.repo.create(new_asset)

    def delete_asset(self, asset_id: int, user_id: Optional[int]) -> None:
        asset = self.repo.find_by_id(asset_id)
        if not asset:
            raise ValueError("Asset not found")
        if asset.task_id is not None:
            self._ensure_task_owner(asset.task_id, user_id)
        elif (
            asset.created_by
            and user_id
            and int(asset.created_by) != int(user_id)
            and not self._is_task_access_exempt_user(user_id)
        ):
            raise PermissionError("Forbidden: owner only")

        should_remove_file = not self.repo.has_other_with_file_path(
            asset.file_path, asset.id
        )
        if should_remove_file:
            self._remove_shard_files(asset)
            self._remove_file(asset.file_path)
        self.repo.delete(asset)

    def get_asset(self, asset_id: int, user_id: Optional[int]) -> TaskAsset:
        asset = self.repo.find_by_id(asset_id)
        if not asset:
            raise ValueError("Asset not found")
        if asset.task_id is not None:
            self._ensure_task_owner(asset.task_id, user_id)
        elif (
            asset.created_by
            and user_id
            and int(asset.created_by) != int(user_id)
            and not self._is_task_access_exempt_user(user_id)
        ):
            raise PermissionError("Forbidden: owner only")
        return asset

    def read_asset_bytes(
        self, asset_id: int, user_id: Optional[int]
    ) -> tuple[TaskAsset, bytes]:
        asset = self.get_asset(asset_id, user_id)
        if isinstance(asset.file_path, str) and asset.file_path.startswith("s3://"):
            bucket, key = s3_utils.parse_s3_uri(asset.file_path)
            return asset, s3_utils.download_bytes(bucket, key)
        return asset, Path(asset.file_path).read_bytes()

    def build_runtime_manifest(
        self,
        task_id: int,
        *,
        execution_properties: Optional[dict[str, Any]] = None,
        task_pattern: Optional[str] = None,
    ) -> dict[str, Any]:
        data_files = self._build_runtime_assets(task_id, category="data")

        manifest: dict[str, Any] = {
            "manifest_version": 1,
            "task_id": task_id,
            "data_files": data_files,
            "data_file_names": [item["file_name"] for item in data_files],
        }
        storage_keys = [
            item["storage_key"]
            for item in data_files
            if isinstance(item.get("storage_key"), str)
        ]
        if storage_keys:
            manifest["storage_keys"] = storage_keys

        data_distribution = self._normalize_data_distribution(execution_properties)
        if data_distribution:
            manifest["data_distribution"] = data_distribution
        if task_pattern:
            manifest["task_pattern"] = task_pattern
        return manifest

    def build_proto_runtime_manifest(
        self,
        task_id: int,
        *,
        task_pattern: Optional[str] = None,
    ) -> dict[str, Any]:
        proto_files = self._build_runtime_assets(task_id, category="proto")
        manifest: dict[str, Any] = {
            "task_id": task_id,
            "proto_files": proto_files,
            "proto_file_names": [item["file_name"] for item in proto_files],
        }
        storage_keys = [
            item["storage_key"]
            for item in proto_files
            if isinstance(item.get("storage_key"), str)
        ]
        if storage_keys:
            manifest["storage_keys"] = storage_keys
        if task_pattern:
            manifest["task_pattern"] = task_pattern
        return manifest

    def inject_runtime_asset_file_defaults(
        self,
        task: Task,
        params: dict[str, Any],
    ) -> None:
        """Inject lightweight single-file defaults without downloading assets."""

        if not self._has_runtime_param_key(params, "DATA_FILE"):
            data_assets = self.repo.find_all(task_id=task.id, category="data")
            if len(data_assets) == 1 and data_assets[0].file_name:
                params["DATA_FILE"] = data_assets[0].file_name

    def validate_runtime_asset_bindings(
        self,
        task: Task,
        params: dict[str, Any],
    ) -> None:
        """Validate file-name references against bound task assets.

        This is intentionally metadata-only: DB asset rows and small local script
        text are enough for the default startup guard.
        """

        data_file_names = self._bound_asset_file_names(task.id, "data")
        data_refs = self._collect_param_file_refs(params, self.DATA_FILE_PARAM_KEYS)
        self._raise_for_unbound_refs(
            data_refs,
            data_file_names,
            asset_label="数据文件",
        )

        proto_file_names = self._bound_asset_file_names(task.id, "proto")
        proto_refs = self._collect_param_file_refs(params, self.PROTO_FILE_PARAM_KEYS)
        proto_refs.extend(
            ("脚本引用 proto", file_name)
            for file_name in self._extract_script_proto_file_refs(task)
        )
        self._raise_for_unbound_refs(
            proto_refs,
            proto_file_names,
            asset_label="proto 文件",
        )

    def _bound_asset_file_names(self, task_id: int, category: str) -> set[str]:
        return {
            asset.file_name
            for asset in self.repo.find_all(task_id=task_id, category=category)
            if isinstance(asset.file_name, str) and asset.file_name.strip()
        }

    @classmethod
    def _has_runtime_param_key(cls, params: dict[str, Any], expected_key: str) -> bool:
        return cls._resolve_runtime_param_value(params, expected_key) is not None

    @classmethod
    def _collect_param_file_refs(
        cls,
        params: dict[str, Any],
        keys: tuple[str, ...],
    ) -> list[tuple[str, str]]:
        refs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for key in keys:
            resolved = cls._resolve_runtime_param_value(params, key)
            if resolved is None:
                continue
            actual_key, value = resolved
            for file_name in cls._normalize_runtime_file_values(value):
                ref = (actual_key, file_name)
                if ref not in seen:
                    refs.append(ref)
                    seen.add(ref)
        return refs

    @classmethod
    def _resolve_runtime_param_value(
        cls,
        params: Any,
        expected_key: str,
    ) -> Optional[tuple[str, Any]]:
        if not isinstance(params, dict):
            return None
        normalized_expected = expected_key.lower()
        for key, value in params.items():
            if isinstance(key, str) and key.lower() == normalized_expected:
                return key, value

        variables = params.get("variables")
        if isinstance(variables, dict):
            resolved = cls._resolve_runtime_param_value(variables, expected_key)
            if resolved is not None:
                return resolved

        properties = params.get("properties")
        if isinstance(properties, dict):
            for key, value in properties.items():
                if isinstance(key, str) and key.lower() == normalized_expected:
                    return key, value
            property_variables = properties.get("variables")
            if isinstance(property_variables, dict):
                return cls._resolve_runtime_param_value(
                    property_variables, expected_key
                )
        return None

    @classmethod
    def _normalize_runtime_file_values(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            names: list[str] = []
            for item in value:
                names.extend(cls._normalize_runtime_file_values(item))
            return names
        if not isinstance(value, str):
            return []
        names: list[str] = []
        for raw_item in value.split(","):
            item = raw_item.strip().strip("\"'")
            if not item:
                continue
            item = item.replace("\\", "/")
            names.append(item.rsplit("/", 1)[-1])
        return names

    def _extract_script_proto_file_refs(self, task: Task) -> list[str]:
        script_id = getattr(task, "script_id", None)
        if not script_id:
            return []
        script = self.db.query(Script).filter(Script.id == script_id).first()
        file_path = getattr(script, "file_path", None)
        if not isinstance(file_path, str) or "://" in file_path:
            return []
        path = Path(file_path)
        try:
            if (
                not path.is_file()
                or path.stat().st_size > self.SCRIPT_PROTO_SCAN_MAX_BYTES
            ):
                return []
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        refs: list[str] = []
        seen: set[str] = set()
        for match in self.SCRIPT_PROTO_LOAD_PATTERN.findall(content):
            file_name = match.replace("\\", "/").rsplit("/", 1)[-1].strip()
            if file_name and file_name not in seen:
                refs.append(file_name)
                seen.add(file_name)
        return refs

    @staticmethod
    def _raise_for_unbound_refs(
        refs: list[tuple[str, str]],
        bound_file_names: set[str],
        *,
        asset_label: str,
    ) -> None:
        if not refs or not bound_file_names:
            return
        invalid_refs = [
            (source, file_name)
            for source, file_name in refs
            if file_name not in bound_file_names
        ]
        if not invalid_refs:
            return
        invalid_text = "; ".join(
            f"{source}={file_name}" for source, file_name in invalid_refs
        )
        bound_text = ", ".join(sorted(bound_file_names))
        raise ValueError(
            f"启动前校验失败：{invalid_text} 未匹配已绑定{asset_label}；已绑定：{bound_text}"
        )

    def _store_file(
        self, content: bytes, file_name: str, category: str, content_type: Optional[str]
    ) -> str:
        use_s3 = (
            settings.USE_S3
            and settings.AWS_ACCESS_KEY_ID
            and settings.AWS_SECRET_ACCESS_KEY
        )
        if use_s3:
            key = f"task-assets/{category}/{file_name}"
            s3_utils.upload_bytes(
                settings.S3_BUCKET, key, content, content_type=content_type
            )
            return f"s3://{settings.S3_BUCKET}/{key}"
        target = _task_assets_dir() / file_name
        target.write_bytes(content)
        return str(target)

    def _store_prepared_file(
        self,
        source_path: Path,
        file_name: str,
        category: str,
        content_type: Optional[str],
        *,
        force_s3: bool = False,
    ) -> str:
        use_s3 = force_s3 or self._s3_enabled()
        if use_s3:
            key = f"task-assets/{category}/{file_name}"
            s3_utils.upload_file(
                settings.S3_BUCKET, key, source_path, content_type=content_type
            )
            return f"s3://{settings.S3_BUCKET}/{key}"
        target = _task_assets_dir() / file_name
        move(str(source_path), str(target))
        return str(target)

    def _store_cloned_asset_file(self, *, content: bytes, asset: TaskAsset) -> str:
        source_name = Path(asset.file_name or "asset").stem or "asset"
        suffix = Path(asset.file_name or "").suffix
        unique_name = f"{source_name}-{uuid4().hex[:8]}{suffix}"
        return self._store_file(
            content,
            unique_name,
            str(asset.category),
            None,
        )

    def _read_file_bytes(self, file_path: str) -> bytes:
        if file_path.startswith("s3://"):
            bucket, key = s3_utils.parse_s3_uri(file_path)
            return s3_utils.download_bytes(bucket, key)
        return Path(file_path).read_bytes()

    def _remove_file(self, file_path: str) -> None:
        if file_path.startswith("s3://"):
            bucket, key = s3_utils.parse_s3_uri(file_path)
            try:
                s3_utils.delete_object(bucket, key)
            except Exception:
                return
            return
        path = Path(file_path)
        if path.exists():
            path.unlink()

    def _validate_asset_upload_request(
        self,
        *,
        category: str,
        filename: str,
        file_size: Optional[int] = None,
    ) -> tuple[dict[str, Any], str, int]:
        category_value = category.strip().lower()
        rule = self.CATEGORY_RULES.get(category_value)
        if not rule:
            raise ValueError("Unsupported asset category")
        ext = filename.split(".")[-1].lower() if "." in filename else ""
        if ext not in rule["extensions"]:
            raise ValueError(f"Invalid file extension for {category_value}")
        max_size = int(rule["max_size"])
        if category_value == "data":
            max_size = self._env_int("TASK_ASSET_LARGE_DATA_MAX_BYTES", max_size)
        if file_size is not None and int(file_size) > max_size:
            raise ValueError(f"File too large for {category_value}")
        return rule, ext, max_size

    @classmethod
    def build_avg_shard_manifest(
        cls,
        *,
        line_count: Optional[int],
        shard_count: Optional[int] = None,
        header_line_count: int = 0,
        data_line_count: Optional[int] = None,
    ) -> dict[str, Any]:
        total_lines = max(int(line_count or 0), 0)
        header_lines = 1 if int(header_line_count or 0) > 0 and total_lines > 0 else 0
        total_data_lines = (
            max(int(data_line_count), 0)
            if data_line_count is not None
            else max(total_lines - header_lines, 0)
        )
        requested_shards = cls._normalize_shard_count(shard_count)
        effective_shard_count = (
            min(requested_shards, total_data_lines) if total_data_lines else 0
        )
        shards: list[dict[str, int]] = []
        if effective_shard_count:
            base_size = total_data_lines // effective_shard_count
            remainder = total_data_lines % effective_shard_count
            data_line_start = 1
            for shard_index in range(1, effective_shard_count + 1):
                shard_size = base_size + (1 if shard_index <= remainder else 0)
                data_line_end = data_line_start + shard_size - 1
                line_start = header_lines + data_line_start
                line_end = header_lines + data_line_end
                shards.append(
                    {
                        "shard_index": shard_index,
                        "line_start": line_start,
                        "line_end": line_end,
                        "data_line_start": data_line_start,
                        "data_line_end": data_line_end,
                        "data_line_count": shard_size,
                        "header_line_count": header_lines,
                    }
                )
                data_line_start = data_line_end + 1
        return {
            "mode": "avg",
            "line_count": total_lines,
            "data_line_count": total_data_lines,
            "has_header": header_lines > 0,
            "header_line_count": header_lines,
            "shard_count": effective_shard_count,
            "shards": shards,
        }

    @classmethod
    def is_all_distribution_blocked(
        cls,
        *,
        file_size: Optional[int],
        compressed_file_size: Optional[int] = None,
        threshold_bytes: Optional[int] = None,
    ) -> bool:
        threshold = (
            int(threshold_bytes)
            if threshold_bytes is not None
            else cls._env_int(
                "TASK_ASSET_ALL_DISTRIBUTION_MAX_BYTES",
                cls.DEFAULT_ALL_DISTRIBUTION_MAX_BYTES,
            )
        )
        if threshold <= 0:
            return False
        size = max(int(file_size or 0), int(compressed_file_size or 0))
        return size > threshold

    def build_all_distribution_blockers(self, task_id: int) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        for asset in self.repo.find_all(task_id=task_id, category="data"):
            if not self.is_all_distribution_blocked(
                file_size=asset.file_size,
                compressed_file_size=asset.compressed_file_size,
            ):
                continue
            blockers.append(
                {
                    "asset_id": int(asset.id),
                    "file_name": asset.file_name,
                    "file_size": int(asset.file_size or 0),
                    "compressed_file_size": (
                        int(asset.compressed_file_size)
                        if asset.compressed_file_size is not None
                        else None
                    ),
                    "threshold_bytes": self._env_int(
                        "TASK_ASSET_ALL_DISTRIBUTION_MAX_BYTES",
                        self.DEFAULT_ALL_DISTRIBUTION_MAX_BYTES,
                    ),
                    "reason": "data_asset_too_large_for_all_distribution",
                }
            )
        return blockers

    @classmethod
    def _normalize_shard_count(cls, value: Optional[Any]) -> int:
        default_value = cls._env_int(
            "TASK_ASSET_DEFAULT_SHARD_COUNT",
            cls.DEFAULT_DATA_SHARD_COUNT,
        )
        try:
            parsed = int(value) if value is not None else int(default_value)
        except (TypeError, ValueError):
            parsed = int(default_value)
        max_shards = cls._env_int(
            "TASK_ASSET_MAX_SHARD_COUNT",
            cls.DEFAULT_MAX_DATA_SHARD_COUNT,
        )
        return min(max(parsed, 1), max(max_shards, 1))

    @classmethod
    def _inspect_data_header(
        cls,
        *,
        source_path: Optional[Path],
        file_name: str,
        line_count: Optional[int],
    ) -> _DataHeaderInfo:
        total_lines = max(int(line_count or 0), 0)
        if total_lines <= 0:
            return _DataHeaderInfo(False, 0, 0, None)
        if Path(file_name or "").suffix.lower() != ".csv" or source_path is None:
            return _DataHeaderInfo(False, 0, total_lines, None)

        header_line = cls._read_first_line_bytes(source_path)
        if not header_line:
            return _DataHeaderInfo(False, 0, total_lines, None)
        try:
            sample = cls._read_header_sample(source_path)
            has_header = bool(sample and csv.Sniffer().has_header(sample))
        except (csv.Error, UnicodeDecodeError):
            has_header = False
        if not has_header:
            return _DataHeaderInfo(False, 0, total_lines, None)
        return _DataHeaderInfo(True, 1, max(total_lines - 1, 0), header_line)

    @staticmethod
    def _read_first_line_bytes(path: Path) -> Optional[bytes]:
        with path.open("rb") as handle:
            line = handle.readline()
        return line or None

    @staticmethod
    def _read_header_sample(path: Path) -> str:
        with path.open("rb") as handle:
            sample = handle.read(8192)
        return sample.decode("utf-8", errors="strict")

    def _build_asset_metadata(
        self,
        *,
        prepared: _PreparedAsset,
        category: str,
        file_path: str,
        shard_count: Optional[int],
        source_path: Optional[Path] = None,
    ) -> dict[str, Any]:
        metadata = dict(prepared.metadata_json or {})
        if file_path:
            self._attach_storage_metadata(metadata, file_path)
        if category == "data":
            header_info = self._inspect_data_header(
                source_path=source_path,
                file_name=prepared.file_name,
                line_count=prepared.line_count,
            )
            shard_manifest = self.build_avg_shard_manifest(
                line_count=prepared.line_count,
                shard_count=shard_count,
                header_line_count=header_info.header_line_count,
                data_line_count=header_info.data_line_count,
            )
            if source_path is not None and shard_manifest.get("shards"):
                shard_manifest = self._materialize_shard_sources(
                    source_path=source_path,
                    asset_file_path=file_path,
                    source_file_name=prepared.file_name,
                    source_content_hash=prepared.content_hash,
                    shard_manifest=shard_manifest,
                    content_type=prepared.content_type,
                    header_line=header_info.header_line,
                )
            metadata["shard_manifest"] = shard_manifest
            metadata["shard_count"] = shard_manifest["shard_count"]
            metadata["shards"] = shard_manifest["shards"]
        return metadata

    @staticmethod
    def _attach_storage_metadata(metadata: dict[str, Any], file_path: str) -> None:
        metadata["storage_uri"] = file_path
        if file_path.startswith("s3://"):
            _, key = s3_utils.parse_s3_uri(file_path)
            metadata["storage_key"] = key

    @staticmethod
    def _resolve_metadata_source_path(
        prepared_path: Path,
        file_path: str,
    ) -> Optional[Path]:
        if prepared_path.exists():
            return prepared_path
        if file_path and not file_path.startswith("s3://"):
            local_path = Path(file_path)
            if local_path.exists():
                return local_path
        return None

    def _materialize_shard_sources(
        self,
        *,
        source_path: Path,
        asset_file_path: str,
        source_file_name: str,
        source_content_hash: str,
        shard_manifest: dict[str, Any],
        content_type: Optional[str],
        header_line: Optional[bytes],
    ) -> dict[str, Any]:
        created_refs: list[str] = []
        enriched_shards: list[dict[str, Any]] = []
        try:
            for shard in shard_manifest.get("shards") or []:
                if not isinstance(shard, dict):
                    continue
                shard_index = int(shard.get("shard_index") or 0)
                line_start = int(shard.get("line_start") or 0)
                line_end = int(shard.get("line_end") or 0)
                if shard_index <= 0 or line_start <= 0 or line_end < line_start:
                    continue
                staged = self._stage_shard_file(
                    source_path=source_path,
                    line_start=line_start,
                    line_end=line_end,
                    source_file_name=source_file_name,
                    header_line=header_line,
                )
                try:
                    stored = self._store_shard_file(
                        staged["path"],
                        asset_file_path=asset_file_path,
                        source_file_name=source_file_name,
                        source_content_hash=source_content_hash,
                        shard_index=shard_index,
                        content_type=content_type,
                    )
                finally:
                    self._remove_temp_path(staged["path"])
                created_refs.append(stored["source_uri"])
                enriched = dict(shard)
                enriched.update(
                    {
                        "file_name": stored["file_name"],
                        "file_size": staged["file_size"],
                        "content_hash": staged["content_hash"],
                        "checksum_sha256": staged["content_hash"],
                        "line_count": staged["line_count"],
                        "data_line_count": staged["data_line_count"],
                        "has_header": staged["header_line_count"] > 0,
                        "header_line_count": staged["header_line_count"],
                        "source_uri": stored["source_uri"],
                        "storage_uri": stored["source_uri"],
                        "storage_type": stored["storage_type"],
                        "compression_type": None,
                    }
                )
                if stored.get("storage_key"):
                    enriched["storage_key"] = stored["storage_key"]
                if stored.get("local_path"):
                    enriched["local_path"] = stored["local_path"]
                enriched_shards.append(enriched)
        except Exception:
            for ref in created_refs:
                self._remove_file(ref)
            raise

        manifest = dict(shard_manifest)
        manifest["shards"] = enriched_shards
        manifest["source_mode"] = "object"
        return manifest

    def _stage_shard_file(
        self,
        *,
        source_path: Path,
        line_start: int,
        line_end: int,
        source_file_name: str,
        header_line: Optional[bytes] = None,
    ) -> dict[str, Any]:
        suffix = Path(source_file_name or "").suffix or ".data"
        handle = tempfile.NamedTemporaryFile(
            delete=False,
            dir=_task_assets_dir(),
            prefix="shard-",
            suffix=suffix,
        )
        path = Path(handle.name)
        hasher = hashlib.sha256()
        line_count = 0
        data_line_count = 0
        header_line_count = 0
        file_size = 0
        try:
            with source_path.open("rb") as source, handle:
                if header_line:
                    handle.write(header_line)
                    hasher.update(header_line)
                    file_size += len(header_line)
                    line_count += 1
                    header_line_count = 1
                for line_number, line in enumerate(source, start=1):
                    if line_number < line_start:
                        continue
                    if line_number > line_end:
                        break
                    handle.write(line)
                    hasher.update(line)
                    file_size += len(line)
                    line_count += 1
                    data_line_count += 1
            if line_count <= 0:
                raise ValueError("Generated data asset shard is empty")
            return {
                "path": path,
                "file_size": file_size,
                "content_hash": hasher.hexdigest(),
                "line_count": line_count,
                "data_line_count": data_line_count,
                "header_line_count": header_line_count,
            }
        except Exception:
            self._remove_temp_path(path)
            raise

    def _store_shard_file(
        self,
        source_path: Path,
        *,
        asset_file_path: str,
        source_file_name: str,
        source_content_hash: str,
        shard_index: int,
        content_type: Optional[str],
    ) -> dict[str, Any]:
        shard_file_name = self._build_shard_file_name(
            source_file_name=source_file_name,
            source_content_hash=source_content_hash,
            shard_index=shard_index,
        )
        if asset_file_path.startswith("s3://"):
            bucket, key = s3_utils.parse_s3_uri(asset_file_path)
            shard_key = self._build_shard_storage_key(
                source_key=key,
                shard_file_name=shard_file_name,
                source_content_hash=source_content_hash,
            )
            s3_utils.upload_file(
                bucket,
                shard_key,
                source_path,
                content_type=content_type,
            )
            return {
                "file_name": shard_file_name,
                "source_uri": f"s3://{bucket}/{shard_key}",
                "storage_key": shard_key,
                "storage_type": "s3",
            }

        shard_dir = _task_assets_dir() / "shards" / source_content_hash[:12]
        shard_dir.mkdir(parents=True, exist_ok=True)
        target = shard_dir / shard_file_name
        if target.exists():
            target = (
                shard_dir
                / f"{Path(shard_file_name).stem}-{uuid4().hex[:8]}{Path(shard_file_name).suffix}"
            )
        move(str(source_path), str(target))
        return {
            "file_name": target.name,
            "source_uri": str(target),
            "local_path": str(target),
            "storage_type": "local",
        }

    @staticmethod
    def _build_shard_file_name(
        *,
        source_file_name: str,
        source_content_hash: str,
        shard_index: int,
    ) -> str:
        source = Path(source_file_name or "data")
        suffix = source.suffix or ".data"
        stem = source.stem or "data"
        return f"{stem}-{source_content_hash[:12]}-shard-{shard_index:05d}{suffix}"

    @staticmethod
    def _build_shard_storage_key(
        *,
        source_key: str,
        shard_file_name: str,
        source_content_hash: str,
    ) -> str:
        parent = str(Path(source_key).parent).strip(".")
        prefix = (
            f"{parent}/shards/{source_content_hash[:12]}"
            if parent
            else f"shards/{source_content_hash[:12]}"
        )
        return f"{prefix}/{shard_file_name}"

    def _remove_shard_files(self, asset: TaskAsset) -> None:
        metadata = asset.metadata_json if isinstance(asset.metadata_json, dict) else {}
        shard_sources: set[str] = set()
        for shard in metadata.get("shards") or []:
            if not isinstance(shard, dict):
                continue
            source_uri = shard.get("source_uri") or shard.get("storage_uri")
            if isinstance(source_uri, str) and source_uri:
                shard_sources.add(source_uri)
                continue
            storage_key = shard.get("storage_key")
            if isinstance(storage_key, str) and storage_key and settings.S3_BUCKET:
                shard_sources.add(f"s3://{settings.S3_BUCKET}/{storage_key}")
        for source_uri in sorted(shard_sources):
            self._remove_file(source_uri)

    @staticmethod
    def _safe_upload_file_name(file_name: str) -> str:
        name = Path((file_name or "").replace("\\", "/")).name.strip()
        if not name or name in {".", ".."}:
            raise ValueError("Invalid file name")
        return name

    @staticmethod
    def _normalize_sha256(value: Optional[str]) -> Optional[str]:
        normalized = (value or "").strip().lower()
        if len(normalized) != 64:
            return None
        try:
            int(normalized, 16)
        except ValueError:
            return None
        return normalized

    @staticmethod
    def _b64_encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    @staticmethod
    def _b64_decode(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))

    def _sign_direct_upload_token(self, payload: dict[str, Any]) -> str:
        raw_payload = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        payload_part = self._b64_encode(raw_payload)
        signature = hmac.new(
            settings.SECRET_KEY.encode("utf-8"),
            payload_part.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{payload_part}.{self._b64_encode(signature)}"

    def _verify_direct_upload_token(
        self,
        token: str,
        *,
        expected_session_id: str,
    ) -> dict[str, Any]:
        try:
            payload_part, signature_part = token.split(".", 1)
            expected_signature = hmac.new(
                settings.SECRET_KEY.encode("utf-8"),
                payload_part.encode("ascii"),
                hashlib.sha256,
            ).digest()
            actual_signature = self._b64_decode(signature_part)
            if not hmac.compare_digest(expected_signature, actual_signature):
                raise ValueError("signature mismatch")
            payload = json.loads(self._b64_decode(payload_part).decode("utf-8"))
        except Exception as exc:
            raise ValueError("Invalid direct upload finalize token") from exc
        if not isinstance(payload, dict):
            raise ValueError("Invalid direct upload finalize token")
        if payload.get("session_id") != expected_session_id:
            raise ValueError("Direct upload session mismatch")
        if int(payload.get("expires_at") or 0) < int(time.time()):
            raise ValueError("Direct upload session expired")
        for key in (
            "bucket",
            "category",
            "content_hash_sha256",
            "file_name",
            "file_size",
            "object_key",
            "session_id",
        ):
            if key not in payload:
                raise ValueError("Invalid direct upload finalize token")
        return payload

    def _download_direct_upload_object(
        self,
        bucket: str,
        key: str,
        file_name: str,
    ) -> Path:
        suffix = Path(file_name).suffix or ".upload"
        handle = tempfile.NamedTemporaryFile(
            delete=False,
            dir=_task_assets_dir(),
            prefix="direct-upload-",
            suffix=suffix,
        )
        path = Path(handle.name)
        handle.close()
        try:
            s3_utils.download_file(bucket, key, path)
            return path
        except Exception:
            self._remove_temp_path(path)
            raise

    def _stage_upload(
        self, file: UploadFile, *, max_size: int, category: str
    ) -> _StagedUpload:
        suffix = Path(file.filename or "uploaded").suffix or ".upload"
        handle = tempfile.NamedTemporaryFile(
            delete=False,
            dir=_task_assets_dir(),
            prefix="upload-",
            suffix=suffix,
        )
        hasher = hashlib.sha256()
        total = 0
        try:
            with handle:
                while True:
                    chunk = file.file.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_size:
                        raise ValueError(f"File too large for {category}")
                    hasher.update(chunk)
                    handle.write(chunk)
            return _StagedUpload(
                path=Path(handle.name),
                file_size=total,
                content_hash=hasher.hexdigest(),
            )
        except Exception:
            self._remove_temp_path(Path(handle.name))
            raise

    def _prepare_asset(
        self,
        *,
        staged: _StagedUpload,
        filename: str,
        category: str,
        ext: str,
        content_type: Optional[str],
    ) -> _PreparedAsset:
        if category == "data" and ext == "zip":
            return self._prepare_zip_asset(staged=staged, upload_filename=filename)

        line_count = self._count_text_lines(staged.path) if category == "data" else None
        return _PreparedAsset(
            path=staged.path,
            file_name=filename,
            file_size=staged.file_size,
            content_hash=staged.content_hash,
            line_count=line_count,
            compression_type=None,
            compressed_file_size=None,
            metadata_json={"upload_file_name": filename},
            content_type=content_type,
        )

    def _prepare_zip_asset(
        self,
        *,
        staged: _StagedUpload,
        upload_filename: str,
    ) -> _PreparedAsset:
        try:
            with ZipFile(staged.path) as archive:
                members = [item for item in archive.infolist() if not item.is_dir()]
                if len(members) != 1:
                    raise ValueError(
                        "Zip data asset must contain exactly one data file"
                    )
                member = members[0]
                if member.flag_bits & 0x1:
                    raise ValueError("Encrypted zip data assets are not supported")
                inner_name = self._safe_zip_member_name(member.filename)
                inner_ext = Path(inner_name).suffix.lower().lstrip(".")
                if inner_ext not in {"csv", "txt", "json"}:
                    raise ValueError("Zip data asset must contain csv/txt/json data")
                if inner_ext == "zip":
                    raise ValueError("Nested zip data assets are not supported")
                max_expanded = self._env_int(
                    "TASK_ASSET_ZIP_MAX_EXPANDED_BYTES",
                    self._env_int(
                        "TASK_ASSET_LARGE_DATA_MAX_BYTES",
                        self.DEFAULT_LARGE_DATA_MAX_SIZE,
                    ),
                )
                if member.file_size > max_expanded:
                    raise ValueError("Expanded zip data asset is too large")
                ratio_limit = self._env_int(
                    "TASK_ASSET_ZIP_MAX_COMPRESSION_RATIO",
                    self.DEFAULT_ZIP_MAX_COMPRESSION_RATIO,
                )
                if (
                    member.compress_size > 0
                    and member.file_size / member.compress_size > ratio_limit
                ):
                    raise ValueError("Zip compression ratio is too high")

                extracted = self._extract_single_zip_member(archive, member)
        except BadZipFile as exc:
            raise ValueError("Invalid zip data asset") from exc

        content_hash, line_count = self._hash_and_count_text_lines(extracted)
        return _PreparedAsset(
            path=extracted,
            file_name=Path(inner_name).name,
            file_size=extracted.stat().st_size,
            content_hash=content_hash,
            line_count=line_count,
            compression_type="zip",
            compressed_file_size=staged.file_size,
            metadata_json={
                "upload_file_name": upload_filename,
                "zip_member_name": inner_name,
                "zip_content_hash": staged.content_hash,
                "expanded_file_size": extracted.stat().st_size,
            },
            content_type=mimetypes.guess_type(inner_name)[0],
        )

    def _extract_single_zip_member(self, archive: ZipFile, member: Any) -> Path:
        suffix = Path(member.filename).suffix or ".data"
        handle = tempfile.NamedTemporaryFile(
            delete=False,
            dir=_task_assets_dir(),
            prefix="ingest-",
            suffix=suffix,
        )
        try:
            with archive.open(member) as source, handle:
                while True:
                    chunk = source.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
            return Path(handle.name)
        except Exception:
            self._remove_temp_path(Path(handle.name))
            raise

    @staticmethod
    def _safe_zip_member_name(raw_name: str) -> str:
        normalized = raw_name.replace("\\", "/").strip()
        path = Path(normalized)
        if not normalized or normalized.startswith("/") or ".." in path.parts:
            raise ValueError("Unsafe zip member path")
        if any(part == "" for part in normalized.split("/")):
            raise ValueError("Unsafe zip member path")
        return normalized

    @classmethod
    def _hash_and_count_text_lines(cls, path: Path) -> tuple[str, int]:
        hasher = hashlib.sha256()
        line_count = 0
        last_byte = b""
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(cls.CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
                line_count += chunk.count(b"\n")
                last_byte = chunk[-1:]
        if path.stat().st_size > 0 and last_byte != b"\n":
            line_count += 1
        return hasher.hexdigest(), line_count

    @classmethod
    def _hash_file(cls, path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(cls.CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()

    @classmethod
    def _count_text_lines(cls, path: Path) -> int:
        _, line_count = cls._hash_and_count_text_lines(path)
        return line_count

    @classmethod
    def _requires_object_storage(
        cls, prepared: _PreparedAsset, *, category: str
    ) -> bool:
        if category != "data":
            return False
        threshold = cls._env_int(
            "TASK_ASSET_SMALL_DATA_MAX_BYTES", cls.SMALL_DATA_MAX_SIZE
        )
        return (
            prepared.file_size > threshold
            or int(prepared.compressed_file_size or 0) > threshold
        )

    @staticmethod
    def _s3_enabled() -> bool:
        return bool(
            settings.USE_S3
            and settings.S3_BUCKET
            and settings.AWS_ACCESS_KEY_ID
            and settings.AWS_SECRET_ACCESS_KEY
        )

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _build_unique_storage_name(file_name: str) -> str:
        source = Path(file_name or "asset")
        suffix = source.suffix
        stem = source.stem or "asset"
        return f"{stem}-{uuid4().hex[:8]}{suffix}"

    @classmethod
    def _should_clone_by_reference(cls, asset: TaskAsset) -> bool:
        if str(asset.category) != "data":
            return False
        if (
            str(asset.file_path).startswith("s3://")
            or str(asset.storage_type or "").lower() == "s3"
        ):
            return True
        if asset.compression_type:
            return True
        return int(asset.file_size or 0) > cls._env_int(
            "TASK_ASSET_SMALL_DATA_MAX_BYTES", cls.SMALL_DATA_MAX_SIZE
        )

    @staticmethod
    def _remove_temp_path(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            return

    def _ensure_task_owner(self, task_id: int, user_id: Optional[int]) -> None:
        task = self.db.query(Task).filter(Task.id == task_id).first()
        if not task:
            raise ValueError("Task not found")
        if user_id is None:
            return
        if (
            task.created_by
            and int(task.created_by) != int(user_id)
            and not self._is_task_access_exempt_user(user_id)
        ):
            raise PermissionError("Forbidden: owner only")

    def _is_task_access_exempt_user(self, user_id: int) -> bool:
        try:
            parsed_user_id = int(user_id)
        except (TypeError, ValueError):
            return False

        user = self.db.get(User, parsed_user_id)
        if user is None:
            return False
        if bool(user.is_superuser):
            return True

        role_value = (
            user.role.value
            if isinstance(user.role, UserRole)
            else str(user.role).strip().upper()
        )
        return role_value == UserRole.ADMIN.value

    @staticmethod
    def _normalize_data_distribution(
        execution_properties: Optional[dict[str, Any]],
    ) -> Optional[str]:
        if not isinstance(execution_properties, dict):
            return None
        raw_value = execution_properties.get("data_distribution")
        if not isinstance(raw_value, str):
            return None
        normalized = raw_value.strip().lower()
        if normalized in {"all", "full", "full_data"}:
            return "all"
        if normalized in {"avg", "average", "split", "avg_split_data"}:
            return "avg"
        return normalized or None

    @classmethod
    def _inline_asset_dispatch_max_bytes(cls) -> int:
        raw_value = os.getenv("PTP_INLINE_ASSET_DISPATCH_MAX_BYTES", "2097152")
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError):
            parsed = 2 * 1024 * 1024
        return max(parsed, 0)

    @classmethod
    def _attach_inline_runtime_asset_content(
        cls,
        payload: dict[str, Any],
        *,
        file_path: str,
        file_size: Optional[int],
    ) -> None:
        if file_path.startswith("s3://"):
            return

        inline_limit = cls._inline_asset_dispatch_max_bytes()
        if inline_limit <= 0:
            return
        if file_size is not None and int(file_size or 0) > inline_limit:
            return

        source = Path(file_path)
        if not source.is_file():
            return
        try:
            payload["content_base64"] = base64.b64encode(source.read_bytes()).decode(
                "ascii"
            )
        except OSError:
            return

    @classmethod
    def _serialize_runtime_asset(cls, asset: TaskAsset) -> dict[str, Any]:
        file_path = str(asset.file_path)
        storage_type = (
            "s3" if file_path.startswith("s3://") else (asset.storage_type or "local")
        )
        metadata = asset.metadata_json if isinstance(asset.metadata_json, dict) else {}
        payload: dict[str, Any] = {
            "asset_id": int(asset.id),
            "category": asset.category,
            "file_name": asset.file_name,
            "file_size": asset.file_size,
            "content_hash": asset.content_hash,
            "source_uri": file_path,
            "storage_uri": metadata.get("storage_uri") or file_path,
            "storage_type": storage_type,
            "compression_type": asset.compression_type,
            "compressed_file_size": asset.compressed_file_size,
            "line_count": asset.line_count,
            "ingest_status": asset.ingest_status,
            "ingest_error": asset.ingest_error,
            "metadata": metadata,
        }
        shard_manifest = metadata.get("shard_manifest")
        if isinstance(shard_manifest, dict):
            payload["shard_manifest"] = shard_manifest
            payload["shard_count"] = shard_manifest.get("shard_count")
        if isinstance(metadata.get("shards"), list):
            shards: list[dict[str, Any]] = []
            for raw_shard in metadata["shards"]:
                if not isinstance(raw_shard, dict):
                    continue
                shard = dict(raw_shard)
                shard_file_path = (
                    shard.get("local_path")
                    or shard.get("source_uri")
                    or shard.get("storage_uri")
                    or ""
                )
                if isinstance(shard_file_path, str) and shard_file_path:
                    shard_size: Optional[int]
                    try:
                        shard_size = (
                            int(shard["file_size"])
                            if shard.get("file_size") is not None
                            else None
                        )
                    except (TypeError, ValueError):
                        shard_size = None
                    cls._attach_inline_runtime_asset_content(
                        shard,
                        file_path=shard_file_path,
                        file_size=shard_size,
                    )
                shards.append(shard)
            payload["shards"] = shards
            payload.setdefault("shard_count", len(metadata["shards"]))
            payload.setdefault(
                "shard_manifest",
                {
                    "mode": "avg",
                    "line_count": int(asset.line_count or 0),
                    "shard_count": payload["shard_count"],
                    "shards": metadata["shards"],
                },
            )
        if file_path.startswith("s3://"):
            _, key = s3_utils.parse_s3_uri(file_path)
            payload["storage_key"] = metadata.get("storage_key") or key
        else:
            payload["local_path"] = file_path
            cls._attach_inline_runtime_asset_content(
                payload,
                file_path=file_path,
                file_size=asset.file_size,
            )
        return payload

    def _build_runtime_assets(
        self, task_id: int, *, category: str
    ) -> list[dict[str, Any]]:
        assets = sorted(
            self.repo.find_all(task_id=task_id, category=category),
            key=lambda asset: (int(asset.id or 0), asset.file_name or ""),
        )
        return [self._serialize_runtime_asset(asset) for asset in assets]
