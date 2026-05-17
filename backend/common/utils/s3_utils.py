from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

import boto3
from botocore.config import Config

from common.config.settings import settings


def _configured_endpoint() -> Optional[str]:
    return (
        os.getenv("S3_ENDPOINT")
        or os.getenv("AWS_S3_ENDPOINT")
        or os.getenv("AWS_ENDPOINT_URL")
        or settings.S3_ENDPOINT
        or None
    )


def _configured_presigned_endpoint() -> Optional[str]:
    return (
        os.getenv("S3_PRESIGNED_ENDPOINT")
        or os.getenv("S3_PUBLIC_ENDPOINT")
        or settings.S3_PRESIGNED_ENDPOINT
        or settings.S3_PUBLIC_ENDPOINT
        or _configured_endpoint()
    )


def _client(*, endpoint_url: Optional[str] = None):
    endpoint = endpoint_url if endpoint_url is not None else _configured_endpoint()
    # 默认忽略环境代理（与 httpx trust_env=False 保持一致），避免本地 MinIO/S3 访问被 http_proxy 劫持。
    # 如需显式启用代理（访问公网 S3），可设置：S3_TRUST_ENV=1
    config_kwargs = {"s3": {"addressing_style": "path"}}
    if os.getenv("S3_TRUST_ENV", "0") != "1":
        config_kwargs["proxies"] = {}
    config_kwargs["connect_timeout"] = float(
        os.getenv("S3_CONNECT_TIMEOUT_SECONDS", "2")
    )
    config_kwargs["read_timeout"] = float(os.getenv("S3_READ_TIMEOUT_SECONDS", "5"))
    config_kwargs["retries"] = {
        "max_attempts": int(os.getenv("S3_MAX_RETRY_ATTEMPTS", "1")),
        "mode": "standard",
    }
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.S3_REGION,
        config=Config(**config_kwargs),
    )


def parse_s3_uri(uri: str) -> Tuple[str, str]:
    """
    解析 s3://bucket/key 结构
    """
    if not uri.startswith("s3://"):
        raise ValueError("Invalid s3 uri")
    without = uri[len("s3://") :]
    parts = without.split("/", 1)
    if len(parts) != 2:
        raise ValueError("Invalid s3 uri")
    return parts[0], parts[1]


def upload_bytes(
    bucket: str, key: str, data: bytes, content_type: Optional[str] = None
):
    client = _client()
    extra = {"ContentType": content_type} if content_type else None
    client.put_object(Bucket=bucket, Key=key, Body=data, **(extra or {}))


def upload_file(
    bucket: str, key: str, file_path: str | Path, content_type: Optional[str] = None
):
    client = _client()
    if content_type:
        client.upload_file(
            str(file_path), bucket, key, ExtraArgs={"ContentType": content_type}
        )
        return
    client.upload_file(str(file_path), bucket, key)


def generate_presigned_put_url(
    bucket: str,
    key: str,
    *,
    expires_in: int,
    content_type: Optional[str] = None,
    metadata: Optional[dict[str, str]] = None,
) -> str:
    client = _client(endpoint_url=_configured_presigned_endpoint())
    params = {"Bucket": bucket, "Key": key}
    if content_type:
        params["ContentType"] = content_type
    if metadata:
        params["Metadata"] = metadata
    return client.generate_presigned_url(
        "put_object",
        Params=params,
        ExpiresIn=expires_in,
    )


def head_object(bucket: str, key: str) -> dict:
    client = _client()
    return client.head_object(Bucket=bucket, Key=key)


def download_bytes(bucket: str, key: str) -> bytes:
    client = _client()
    resp = client.get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


def download_file(bucket: str, key: str, file_path: str | Path) -> None:
    client = _client()
    client.download_file(bucket, key, str(file_path))


def delete_object(bucket: str, key: str) -> None:
    client = _client()
    client.delete_object(Bucket=bucket, Key=key)
