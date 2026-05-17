from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.core.database import Base


class RunAlertEvent(Base):
    __tablename__ = "run_alert_events"
    __table_args__ = {"sqlite_autoincrement": True}

    event_id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    run_id = Column(BigInteger, nullable=True, index=True)
    task_id = Column(BigInteger, nullable=True, index=True)
    mixed_run_id = Column(BigInteger, nullable=True, index=True)
    plan_run_id = Column(BigInteger, nullable=True, index=True)

    subscription = Column(String(128), nullable=True, index=True)
    source = Column(String(64), nullable=False, index=True)
    alertname = Column(String(255), nullable=True, index=True)
    severity = Column(String(64), nullable=True, index=True)
    priority = Column(String(64), nullable=True, index=True)
    status = Column(String(64), nullable=True, index=True)
    starts_at = Column(DateTime, nullable=True, index=True)
    ends_at = Column(DateTime, nullable=True)

    labels = Column(JSON, nullable=False, default=dict)
    annotations = Column(JSON, nullable=False, default=dict)
    dashboard_url = Column(String(1024), nullable=True)
    fingerprint = Column(String(128), nullable=False, index=True)
    source_event_id = Column(String(255), nullable=True, index=True)
    dedupe_key = Column(String(512), nullable=False, index=True)
    aggregation_key = Column(String(512), nullable=False, index=True)
    action_status = Column(String(64), nullable=False, index=True)
    raw_event = Column(JSON, nullable=False, default=dict)

    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    updated_at = Column(
        DateTime, nullable=True, server_default=func.now(), onupdate=func.now()
    )
