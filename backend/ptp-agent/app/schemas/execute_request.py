from pathlib import Path
import sys
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

COMMON_PARENT = Path(__file__).resolve().parents[3]
if COMMON_PARENT.exists():
    sys.path.append(str(COMMON_PARENT))

from common.models.enums import EngineType  # type: ignore F401


DEFAULT_EXECUTE_DURATION_SECONDS = 300


def _normalize_data_distribution(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"all", "full", "full_data"}:
        return "all"
    if normalized in {"avg", "average", "split", "avg_split_data"}:
        return "avg"
    return normalized or None


def _coerce_slice_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


class RuntimeDataShard(BaseModel):
    shard_index: Optional[int] = None
    file_name: Optional[str] = None
    file_size: Optional[int] = None
    content_hash: Optional[str] = None
    content_base64: Optional[str] = None
    checksum_sha256: Optional[str] = None
    source_uri: Optional[str] = None
    storage_type: Optional[str] = None
    storage_key: Optional[str] = None
    local_path: Optional[str] = None
    compression_type: Optional[str] = None
    compressed_file_size: Optional[int] = None
    line_count: Optional[int] = None
    data_line_count: Optional[int] = None
    has_header: Optional[bool] = None
    header_line_count: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RuntimeDataAsset(BaseModel):
    asset_id: Optional[int] = None
    category: Optional[str] = None
    file_name: Optional[str] = None
    file_size: Optional[int] = None
    content_hash: Optional[str] = None
    content_base64: Optional[str] = None
    checksum_sha256: Optional[str] = None
    source_uri: Optional[str] = None
    storage_type: Optional[str] = None
    storage_key: Optional[str] = None
    local_path: Optional[str] = None
    compression_type: Optional[str] = None
    compressed_file_size: Optional[int] = None
    line_count: Optional[int] = None
    ingest_status: Optional[str] = None
    ingest_error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    metadata_json: Dict[str, Any] = Field(default_factory=dict)
    shards: list[RuntimeDataShard] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consume_metadata_json_shards(self) -> "RuntimeDataAsset":
        if self.metadata_json:
            merged_metadata = dict(self.metadata_json)
            merged_metadata.update(self.metadata or {})
            self.metadata = merged_metadata
        raw_shards = self.metadata.get("shards")
        if not self.shards and isinstance(raw_shards, list):
            self.shards = [
                RuntimeDataShard.model_validate(item)
                for item in raw_shards
                if isinstance(item, dict)
            ]
        return self


class DataAssetManifest(BaseModel):
    manifest_version: Optional[int] = None
    task_id: Optional[int] = None
    task_pattern: Optional[str] = None
    data_distribution: Optional[str] = None
    data_file_names: list[str] = Field(default_factory=list)
    storage_keys: list[str] = Field(default_factory=list)
    data_files: list[RuntimeDataAsset] = Field(default_factory=list)

    @field_validator("data_distribution", mode="before")
    @classmethod
    def _normalize_distribution(cls, value: Any) -> Optional[str]:
        return _normalize_data_distribution(value)

    def iter_runtime_data_assets(self) -> list[RuntimeDataAsset]:
        assets = list(self.data_files)
        existing_keys = {
            asset.storage_key
            for asset in assets
            if isinstance(asset.storage_key, str) and asset.storage_key
        }
        for index, storage_key in enumerate(self.storage_keys):
            if (
                not isinstance(storage_key, str)
                or not storage_key
                or storage_key in existing_keys
            ):
                continue
            file_name = (
                self.data_file_names[index]
                if index < len(self.data_file_names) and self.data_file_names[index]
                else Path(storage_key).name
            )
            assets.append(
                RuntimeDataAsset(
                    category="data",
                    file_name=file_name,
                    storage_type="s3",
                    storage_key=storage_key,
                )
            )
        return assets


class ProtoAssetManifest(BaseModel):
    task_id: Optional[int] = None
    task_pattern: Optional[str] = None
    proto_file_names: list[str] = Field(default_factory=list)
    storage_keys: list[str] = Field(default_factory=list)
    proto_files: list[RuntimeDataAsset] = Field(default_factory=list)

    def iter_runtime_proto_assets(self) -> list[RuntimeDataAsset]:
        assets = list(self.proto_files)
        existing_keys = {
            asset.storage_key
            for asset in assets
            if isinstance(asset.storage_key, str) and asset.storage_key
        }
        for index, storage_key in enumerate(self.storage_keys):
            if (
                not isinstance(storage_key, str)
                or not storage_key
                or storage_key in existing_keys
            ):
                continue
            file_name = (
                self.proto_file_names[index]
                if index < len(self.proto_file_names) and self.proto_file_names[index]
                else Path(storage_key).name
            )
            assets.append(
                RuntimeDataAsset(
                    category="proto",
                    file_name=file_name,
                    storage_type="s3",
                    storage_key=storage_key,
                )
            )
        return assets


class ExecuteRequest(BaseModel):
    task_id: int
    script_id: int
    script_path: Optional[str] = None
    script_s3: Optional[str] = None
    script_content: Optional[str] = None
    script_file_name: Optional[str] = None
    engine_type: EngineType
    thread_count: int
    duration: Optional[int] = None
    ramp_up: int = 0
    protocol: Optional[str] = None
    properties: Optional[Dict[str, Any]] = None
    run_id: Optional[int] = None
    pod_count: Optional[int] = None
    pod_num: Optional[int] = None
    data_distribution: Optional[str] = None
    data_asset_manifest: Optional[DataAssetManifest] = None
    proto_asset_manifest: Optional[ProtoAssetManifest] = None
    runtime_assets: Optional[Any] = None
    metadata_json: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("data_distribution", mode="before")
    @classmethod
    def _normalize_distribution(cls, value: Any) -> Optional[str]:
        return _normalize_data_distribution(value)

    @property
    def effective_data_distribution(self) -> Optional[str]:
        if self.data_distribution:
            return self.data_distribution
        if self.data_asset_manifest:
            return self.data_asset_manifest.data_distribution
        runtime_manifest = self._runtime_assets_manifest()
        if runtime_manifest:
            return runtime_manifest.data_distribution
        return None

    @property
    def runtime_data_assets(self) -> list[RuntimeDataAsset]:
        manifest = self.data_asset_manifest or self._runtime_assets_manifest()
        if not manifest:
            return []
        return manifest.iter_runtime_data_assets()

    @property
    def runtime_proto_assets(self) -> list[RuntimeDataAsset]:
        if not self.proto_asset_manifest:
            return []
        return self.proto_asset_manifest.iter_runtime_proto_assets()

    def _runtime_assets_manifest(self) -> Optional[DataAssetManifest]:
        for candidate in (
            self.runtime_assets,
            self.metadata_json.get("runtime_assets"),
            self.metadata_json.get("data_asset_manifest"),
        ):
            manifest = self._coerce_data_asset_manifest(candidate)
            if manifest:
                return manifest
        data_files = self.metadata_json.get("data_files")
        if isinstance(data_files, list):
            return self._coerce_data_asset_manifest(
                {
                    "manifest_version": self.metadata_json.get("manifest_version"),
                    "task_id": self.metadata_json.get("task_id"),
                    "task_pattern": self.metadata_json.get("task_pattern"),
                    "data_distribution": self.metadata_json.get("data_distribution"),
                    "data_files": data_files,
                    "data_file_names": self.metadata_json.get("data_file_names") or [],
                    "storage_keys": self.metadata_json.get("storage_keys") or [],
                }
            )
        return None

    @staticmethod
    def _coerce_data_asset_manifest(value: Any) -> Optional[DataAssetManifest]:
        if isinstance(value, DataAssetManifest):
            return value
        if isinstance(value, dict):
            return DataAssetManifest.model_validate(value)
        if isinstance(value, list):
            return DataAssetManifest(
                data_files=[
                    RuntimeDataAsset.model_validate(item)
                    for item in value
                    if isinstance(item, dict)
                ]
            )
        return None

    @property
    def runtime_data_split_type(self) -> Optional[str]:
        raw_value = (self.properties or {}).get("PTP_DATA_SPLIT_TYPE")
        if not isinstance(raw_value, str):
            return None
        normalized = raw_value.strip().lower()
        return normalized or None

    @property
    def runtime_data_slice_start(self) -> Optional[int]:
        return _coerce_slice_int((self.properties or {}).get("PTP_DATA_SLICE_START"))

    @property
    def runtime_data_slice_total(self) -> Optional[int]:
        return _coerce_slice_int((self.properties or {}).get("PTP_DATA_SLICE_TOTAL"))

    @staticmethod
    def _parse_duration_seconds(value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            return parsed if parsed > 0 else None
        if not isinstance(value, str):
            return None
        raw = value.strip()
        if not raw:
            return None
        import re

        match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", raw, re.IGNORECASE)
        if not match:
            return None
        amount = float(match.group(1))
        unit = match.group(2).lower()
        if amount <= 0:
            return None
        if unit == "ms":
            return max(1, int(amount / 1000.0 + 0.999999))
        if unit == "s":
            return max(1, int(amount + 0.999999))
        if unit == "m":
            return max(1, int(amount * 60 + 0.999999))
        if unit == "h":
            return max(1, int(amount * 3600 + 0.999999))
        return None

    @property
    def resolved_duration(self) -> int:
        direct_duration = self._parse_duration_seconds(self.duration)
        if direct_duration:
            return direct_duration
        properties = self.properties or {}
        for key in ("PTP_DURATION_SECONDS", "duration", "DURATION"):
            parsed = self._parse_duration_seconds(properties.get(key))
            if parsed:
                return parsed
        return DEFAULT_EXECUTE_DURATION_SECONDS


class ExecuteResponse(BaseModel):
    task_id: int
    status: str
    pid: int
    agent_id: str
    run_token: str


class K6ControlRequest(BaseModel):
    target_tps: Optional[float] = Field(default=None, gt=0)
