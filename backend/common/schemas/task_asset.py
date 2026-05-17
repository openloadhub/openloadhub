from __future__ import annotations

from datetime import datetime
from collections.abc import Mapping
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _read_source_value(source: Any, key: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(key)
    return getattr(source, key, None)


def _task_asset_metadata(source: Any) -> dict[str, Any]:
    metadata = _read_source_value(source, "metadata_json")
    return metadata if isinstance(metadata, dict) else {}


class TaskAssetResponse(BaseModel):
    id: int
    task_id: Optional[int] = None
    category: str
    file_name: str
    file_path: str
    file_size: int
    content_hash: Optional[str] = None
    storage_type: str = "local"
    compression_type: Optional[str] = None
    compressed_file_size: Optional[int] = None
    line_count: Optional[int] = None
    ingest_status: str = "completed"
    ingest_error: Optional[str] = None
    metadata_json: Optional[dict[str, Any]] = None
    storage_key: Optional[str] = None
    storage_uri: Optional[str] = None
    shard_count: Optional[int] = None
    shard_manifest: Optional[dict[str, Any]] = None
    created_by: Optional[int] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="before")
    @classmethod
    def hydrate_asset_metadata_fields(cls, data: Any) -> Any:
        metadata = _task_asset_metadata(data)
        if not metadata:
            return data

        values = dict(data) if isinstance(data, Mapping) else {}
        if not values.get("storage_uri"):
            values["storage_uri"] = metadata.get("storage_uri") or _read_source_value(
                data, "file_path"
            )
        if not values.get("storage_key"):
            values["storage_key"] = metadata.get("storage_key")
        shard_manifest = metadata.get("shard_manifest")
        if isinstance(shard_manifest, dict):
            values.setdefault("shard_manifest", shard_manifest)
            values.setdefault("shard_count", shard_manifest.get("shard_count"))
        elif isinstance(metadata.get("shards"), list):
            values.setdefault(
                "shard_manifest",
                {
                    "mode": "avg",
                    "line_count": _read_source_value(data, "line_count") or 0,
                    "shard_count": metadata.get("shard_count")
                    or len(metadata["shards"]),
                    "shards": metadata["shards"],
                },
            )
            values.setdefault(
                "shard_count",
                metadata.get("shard_count") or len(metadata["shards"]),
            )
        if isinstance(data, Mapping):
            return values
        for field_name in (
            "id",
            "task_id",
            "category",
            "file_name",
            "file_path",
            "file_size",
            "content_hash",
            "storage_type",
            "compression_type",
            "compressed_file_size",
            "line_count",
            "ingest_status",
            "ingest_error",
            "metadata_json",
            "created_by",
            "created_at",
            "updated_at",
        ):
            values.setdefault(field_name, _read_source_value(data, field_name))
        return values


class TaskAssetBindRequest(BaseModel):
    task_id: int = Field(..., gt=0)
    asset_ids: list[int] = Field(..., min_length=1)


class TaskAssetDirectUploadCreateRequest(BaseModel):
    category: str = Field(..., min_length=1)
    file_name: str = Field(..., min_length=1, max_length=255)
    file_size: int = Field(..., gt=0)
    content_hash_sha256: str = Field(..., min_length=64, max_length=64)
    content_type: Optional[str] = Field(default=None, max_length=255)
    task_id: Optional[int] = Field(default=None, ge=1)
    shard_count: Optional[int] = Field(default=None, ge=1)


class TaskAssetDirectUploadSessionResponse(BaseModel):
    session_id: str
    upload_method: str = "PUT"
    upload_url: str
    upload_headers: dict[str, str] = Field(default_factory=dict)
    bucket: str
    object_key: str
    object_uri: str
    expires_in_seconds: int
    expires_at: int
    finalize_token: str


class TaskAssetDirectUploadFinalizeRequest(BaseModel):
    finalize_token: str = Field(..., min_length=16)


__all__ = [
    "TaskAssetResponse",
    "TaskAssetBindRequest",
    "TaskAssetDirectUploadCreateRequest",
    "TaskAssetDirectUploadFinalizeRequest",
    "TaskAssetDirectUploadSessionResponse",
]
