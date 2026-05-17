from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.sql import func

from app.core.database import Base


class WebhookConfig(Base):
    __tablename__ = "olh_webhook_config"
    __table_args__ = {"sqlite_autoincrement": True}

    config_id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="Webhook 配置ID",
    )
    name = Column(String(128), nullable=False)
    channel = Column(String(32), nullable=False, index=True)
    event_types = Column(JSON, nullable=False)
    webhook_url = Column(String(2048), nullable=False)
    signature_type = Column(String(32), nullable=False, default="none")
    signing_secret = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    template = Column(Text, nullable=True)
    title = Column(String(128), nullable=True)
    timeout_seconds = Column(Float, nullable=False, default=5.0)
    max_retry_count = Column(Integer, nullable=False, default=0)
    retry_interval_seconds = Column(Float, nullable=False, default=0.0)
    created_by = Column(BigInteger, nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    updated_at = Column(
        DateTime, nullable=True, server_default=func.now(), onupdate=func.now()
    )


class WebhookSendRecord(Base):
    __tablename__ = "olh_webhook_send_record"
    __table_args__ = {"sqlite_autoincrement": True}

    record_id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="Webhook 发送记录ID",
    )
    channel = Column(String(32), nullable=False, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="pending", index=True)
    title = Column(String(128), nullable=False)
    rendered_text = Column(Text, nullable=False)
    payload = Column(JSON, nullable=False)
    variables = Column(JSON, nullable=True)
    webhook_url_masked = Column(String(512), nullable=True)
    webhook_host = Column(String(255), nullable=True, index=True)
    http_status_code = Column(Integer, nullable=True)
    response_body = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    config_id = Column(BigInteger, nullable=True, index=True)
    trigger_source = Column(String(32), nullable=False, default="manual", index=True)
    created_by = Column(BigInteger, nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    updated_at = Column(
        DateTime, nullable=True, server_default=func.now(), onupdate=func.now()
    )
    sent_at = Column(DateTime, nullable=True)
