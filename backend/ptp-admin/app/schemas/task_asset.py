from pathlib import Path
import sys

COMMON_PARENT = Path(__file__).resolve().parents[3]
if COMMON_PARENT.exists():
    sys.path.append(str(COMMON_PARENT))

from common.schemas.task_asset import (  # type: ignore F401
    TaskAssetBindRequest,
    TaskAssetDirectUploadCreateRequest,
    TaskAssetDirectUploadFinalizeRequest,
    TaskAssetDirectUploadSessionResponse,
    TaskAssetResponse,
)

__all__ = [
    "TaskAssetBindRequest",
    "TaskAssetDirectUploadCreateRequest",
    "TaskAssetDirectUploadFinalizeRequest",
    "TaskAssetDirectUploadSessionResponse",
    "TaskAssetResponse",
]
