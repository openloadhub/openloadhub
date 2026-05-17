from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Generic, Optional, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from pathlib import Path
import sys

COMMON_PARENT = Path(__file__).resolve().parents[3]
if COMMON_PARENT.exists():
    sys.path.append(str(COMMON_PARENT))

from common.utils.time import to_rfc3339_z

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    code: int = Field(default=0, description="业务状态码；0=成功")
    message: str = Field(default="success", description="提示信息")
    data: Optional[T] = Field(default=None, description="响应数据")
    timestamp: int = Field(default_factory=lambda: int(datetime.now(timezone.utc).timestamp() * 1000), description="时间戳（毫秒）")
    trace_id: str = Field(default_factory=lambda: str(uuid4()), description="追踪 ID")

    model_config = ConfigDict()

    @classmethod
    def success(cls, data: Optional[T] = None, message: str = "success") -> "ApiResponse[T]":
        return cls(code=0, message=message, data=data)

    @classmethod
    def error(cls, code: int, message: str, data: Any = None) -> "ApiResponse[Any]":
        return cls(code=code, message=message, data=data)


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int = Field(..., alias="pageSize")

    model_config = ConfigDict(populate_by_name=True)
