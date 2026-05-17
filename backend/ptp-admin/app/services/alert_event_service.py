from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.schemas.alert_event import RunAlertEventSummary


class AlertEventService:
    """No-op alert event service for the public alpha build."""

    def __init__(self, db: Session):
        self.db = db

    def create_event(self, payload, *, raw_event: dict[str, Any]):
        del payload, raw_event
        return None

    def list_for_run(self, run_id: int, *, limit: int = 100):
        del run_id, limit
        return []

    def summarize_for_run(self, run_id: int, *, limit: int = 5) -> Optional[dict[str, Any]]:
        del run_id, limit
        return None

    def list_for_mixed_run(self, mixed_run_id: int, *, limit: int = 100):
        del mixed_run_id, limit
        return []

    def summarize_list_for_mixed_run(self, mixed_run_id: int) -> RunAlertEventSummary:
        del mixed_run_id
        return RunAlertEventSummary()
