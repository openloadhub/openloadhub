from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session


class MixedRunReportService:
    """No-op mixed run report service for the public alpha build."""

    def __init__(self, db: Session):
        self.db = db

    def get_latest_report(self, mixed_run_id: int, *, selected_round: Optional[int] = None):
        del mixed_run_id, selected_round
        return None

    def auto_generate_report_if_needed(
        self,
        *,
        mixed_run_id: int,
        selected_round: Optional[int] = None,
        user_id: Optional[int] = None,
    ):
        del mixed_run_id, selected_round, user_id
        return None

    def generate_report(
        self,
        *,
        mixed_run_id: int,
        selected_round: Optional[int] = None,
        selected_collection_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ):
        del mixed_run_id, selected_round, selected_collection_id, user_id
        raise RuntimeError("mixed run reports are not included in the public alpha build")
