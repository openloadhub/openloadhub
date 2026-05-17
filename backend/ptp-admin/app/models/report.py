from pathlib import Path
import sys

from sqlalchemy import Column, BigInteger, String, Text, DateTime, Integer, JSON, Enum as SQLEnum, Float
from sqlalchemy.sql import func

COMMON_PARENT = Path(__file__).resolve().parents[3]
if COMMON_PARENT.exists():
    sys.path.append(str(COMMON_PARENT))

from app.core.database import Base
from common.models.enums import ReportStatus, ReportType

class Report(Base):
    __tablename__ = "olh_report"
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="报告ID"
    )
    task_id = Column(BigInteger, nullable=False, index=True, comment="关联的任务ID")
    run_id = Column(BigInteger, nullable=True, index=True, comment="关联的执行记录ID")
    name = Column(String(255), nullable=False, comment="报告名称")
    description = Column(Text, comment="报告描述")
    report_type = Column(SQLEnum(ReportType), nullable=False, comment="报告类型")
    file_path = Column(String(500), comment="报告文件路径")
    file_size = Column(Integer, comment="文件大小(字节)")
    status = Column(SQLEnum(ReportStatus), nullable=False, default=ReportStatus.PENDING, index=True, comment="报告状态")

    # 性能指标
    total_requests = Column(Integer, comment="总请求数")
    successful_requests = Column(Integer, comment="成功请求数")
    failed_requests = Column(Integer, comment="失败请求数")
    error_rate = Column(Float, comment="错误率(%)")
    avg_response_time = Column(Float, comment="平均响应时间(ms)")
    min_response_time = Column(Float, comment="最小响应时间(ms)")
    max_response_time = Column(Float, comment="最大响应时间(ms)")
    p95_response_time = Column(Float, comment="P95响应时间(ms)")
    p99_response_time = Column(Float, comment="P99响应时间(ms)")
    throughput = Column(Float, comment="吞吐量(req/s)")

    # 配置信息
    test_config = Column(JSON, comment="测试配置(JSON)")
    metrics_data = Column(JSON, comment="详细指标数据(JSON)")

    generated_by = Column(BigInteger, comment="生成人ID")
    generated_at = Column(DateTime, comment="生成时间")
    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True, comment="创建时间")
    updated_at = Column(DateTime, onupdate=func.now(), server_default=func.now(), comment="更新时间")

    def __repr__(self):
        return f"<Report(id={self.id}, task_id={self.task_id}, run_id={self.run_id}, type={self.report_type}, status={self.status})>"
