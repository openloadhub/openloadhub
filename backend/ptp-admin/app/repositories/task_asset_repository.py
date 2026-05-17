from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.models.task_asset import TaskAsset


class TaskAssetRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, asset: TaskAsset) -> TaskAsset:
        self.db.add(asset)
        self.db.commit()
        self.db.refresh(asset)
        return asset

    def find_by_id(self, asset_id: int) -> Optional[TaskAsset]:
        return self.db.query(TaskAsset).filter(TaskAsset.id == asset_id).first()

    def find_all(
        self,
        task_id: Optional[int] = None,
        category: Optional[str] = None,
        created_by: Optional[int] = None,
    ) -> list[TaskAsset]:
        query = self.db.query(TaskAsset)
        if task_id is not None:
            query = query.filter(TaskAsset.task_id == task_id)
        if category:
            query = query.filter(TaskAsset.category == category)
        if created_by is not None:
            query = query.filter(TaskAsset.created_by == created_by)
        return query.order_by(TaskAsset.created_at.desc(), TaskAsset.id.desc()).all()

    def find_many(self, asset_ids: list[int]) -> list[TaskAsset]:
        if not asset_ids:
            return []
        return self.db.query(TaskAsset).filter(TaskAsset.id.in_(asset_ids)).all()

    def has_other_with_file_path(self, file_path: str, asset_id: int) -> bool:
        return (
            self.db.query(TaskAsset)
            .filter(TaskAsset.file_path == file_path)
            .filter(TaskAsset.id != asset_id)
            .first()
            is not None
        )

    def save(self, asset: TaskAsset) -> TaskAsset:
        self.db.add(asset)
        self.db.commit()
        self.db.refresh(asset)
        return asset

    def delete(self, asset: TaskAsset) -> None:
        self.db.delete(asset)
        self.db.commit()
