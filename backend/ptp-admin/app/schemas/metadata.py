from typing import List

from pydantic import BaseModel

from app.schemas.response import ApiResponse


class BusinessLine(BaseModel):
    code: str
    name: str


class BusinessLineListResponse(ApiResponse[List[BusinessLine]]):
    data: List[BusinessLine]


class EnvironmentItem(BaseModel):
    code: str
    name: str
    scope: str


class EnvironmentListResponse(ApiResponse[List[EnvironmentItem]]):
    data: List[EnvironmentItem]
