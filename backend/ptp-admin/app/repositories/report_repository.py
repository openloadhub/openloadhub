"""
报告数据访问层
"""

from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, desc, func
from app.models.report import Report, ReportStatus, ReportType

class ReportRepository:
    """报告数据访问仓库"""

    def __init__(self, db: Session):
        self.db = db

    def create(self, report_data: Dict[str, Any]) -> Report:
        """创建报告"""
        report = Report(**report_data)
        self.db.add(report)
        self.db.commit()
        self.db.refresh(report)
        return report

    def get_by_id(self, report_id: int) -> Optional[Report]:
        """根据ID获取报告"""
        return self.db.query(Report).filter(Report.id == report_id).first()

    def get_by_task_id(self, task_id: int) -> List[Report]:
        """根据任务ID获取报告列表"""
        return (
            self.db.query(Report)
            .filter(Report.task_id == task_id)
            .order_by(desc(Report.created_at))
            .all()
        )

    def get_by_run_id(self, run_id: int) -> List[Report]:
        """根据执行记录ID获取报告列表"""
        return (
            self.db.query(Report)
            .filter(Report.run_id == run_id)
            .order_by(desc(Report.created_at))
            .all()
        )

    def get_completed_by_run_id(self, run_id: int) -> List[Report]:
        """获取某次执行下所有已完成报告，按最新创建时间倒序。"""
        return (
            self.db.query(Report)
            .filter(
                Report.run_id == run_id,
                Report.status == ReportStatus.COMPLETED,
            )
            .order_by(desc(Report.created_at), desc(Report.id))
            .all()
        )

    def get_multi(
        self,
        skip: int = 0,
        limit: int = 100,
        task_id: Optional[int] = None,
        run_id: Optional[int] = None,
        status: Optional[ReportStatus] = None,
        report_type: Optional[ReportType] = None
    ) -> List[Report]:
        """获取报告列表（分页）"""
        query = self.db.query(Report)

        if task_id:
            query = query.filter(Report.task_id == task_id)
        if run_id:
            query = query.filter(Report.run_id == run_id)
        if status:
            query = query.filter(Report.status == status)
        if report_type:
            query = query.filter(Report.report_type == report_type)

        return query.order_by(desc(Report.created_at)).offset(skip).limit(limit).all()

    def count(
        self,
        task_id: Optional[int] = None,
        run_id: Optional[int] = None,
        status: Optional[ReportStatus] = None,
        report_type: Optional[ReportType] = None
    ) -> int:
        """统计报告数量"""
        query = self.db.query(func.count(Report.id))

        if task_id:
            query = query.filter(Report.task_id == task_id)
        if run_id:
            query = query.filter(Report.run_id == run_id)
        if status:
            query = query.filter(Report.status == status)
        if report_type:
            query = query.filter(Report.report_type == report_type)

        return query.scalar()

    def update(self, report_id: int, update_data: Dict[str, Any]) -> Optional[Report]:
        """更新报告"""
        report = self.get_by_id(report_id)
        if not report:
            return None

        for key, value in update_data.items():
            setattr(report, key, value)

        self.db.commit()
        self.db.refresh(report)
        return report

    def delete(self, report_id: int) -> bool:
        """删除报告（软删除）"""
        report = self.get_by_id(report_id)
        if not report:
            return False

        report.status = ReportStatus.DELETED
        self.db.commit()
        return True

    def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息"""
        # 总报告数
        total = self.count()

        # 按状态统计
        status_stats = {}
        for status in ReportStatus:
            count = self.count(status=status)
            status_stats[status] = count

        # 按类型统计
        type_stats = {}
        for report_type in ReportType:
            count = self.count(report_type=report_type)
            type_stats[report_type] = count

        # 平均生成时间（简化实现）
        avg_generation_time = None

        return {
            "total_reports": total,
            "by_status": status_stats,
            "by_type": type_stats,
            "avg_generation_time": avg_generation_time
        }

    def search(self, keyword: str, skip: int = 0, limit: int = 100) -> List[Report]:
        """搜索报告"""
        return (
            self.db.query(Report)
            .filter(
                and_(
                    Report.status != ReportStatus.DELETED,
                    or_(
                        Report.name.like(f"%{keyword}%"),
                        Report.description.like(f"%{keyword}%")
                    )
                )
            )
            .order_by(desc(Report.created_at))
            .offset(skip)
            .limit(limit)
            .all()
        )
