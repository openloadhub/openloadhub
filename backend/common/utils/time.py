from __future__ import annotations

from datetime import datetime, timezone


def to_rfc3339_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    value = value.replace(microsecond=0)
    return value.isoformat().replace("+00:00", "Z")


def utc_now() -> datetime:
    """返回 timezone-aware 的 UTC 时间"""
    return datetime.now(timezone.utc)
