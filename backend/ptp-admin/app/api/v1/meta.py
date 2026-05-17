from __future__ import annotations

from fastapi import APIRouter

from app.core.config import settings
from app.schemas.metadata import BusinessLine, BusinessLineListResponse, EnvironmentItem, EnvironmentListResponse
from app.schemas.response import ApiResponse

router = APIRouter()


@router.get(
    "/business-lines",
    response_model=BusinessLineListResponse,
    response_model_by_alias=True,
    summary="获取业务线列表（从环境配置读取）",
)
def list_business_lines():
    lines = [BusinessLine(**item) for item in settings.business_line_items]
    return ApiResponse.success(lines)


@router.get(
    "/environments",
    response_model=EnvironmentListResponse,
    response_model_by_alias=True,
    summary="获取环境列表（从环境配置读取）",
)
def list_environments():
    envs = [EnvironmentItem(**item) for item in settings.environment_items]
    return ApiResponse.success(envs)
