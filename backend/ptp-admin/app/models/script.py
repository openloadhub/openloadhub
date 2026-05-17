from pathlib import Path
import sys
from sqlalchemy import Column, BigInteger, String, Text, DateTime, Integer, JSON, Enum as SQLEnum
from sqlalchemy.sql import func

COMMON_PARENT = Path(__file__).resolve().parents[3]
if COMMON_PARENT.exists():
    sys.path.append(str(COMMON_PARENT))

from app.core.database import Base
from common.models.enums import ScriptStatus, ScriptType

class Script(Base):
    __tablename__ = "olh_script"
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="脚本ID"
    )
    name = Column(String(255), nullable=False, comment="脚本名称")
    description = Column(Text, comment="脚本描述")
    script_type = Column(SQLEnum(ScriptType), nullable=False, comment="脚本类型")
    file_path = Column(String(500), nullable=False, comment="脚本文件路径")
    file_size = Column(Integer, comment="文件大小(字节)")
    content_hash = Column(String(64), comment="文件内容哈希(SHA256)")
    version = Column(String(50), default="1.0", comment="脚本版本")
    status = Column(SQLEnum(ScriptStatus), nullable=False, default=ScriptStatus.ACTIVE, index=True, comment="脚本状态")
    tags = Column(JSON, comment="标签(JSON数组)")
    parameters = Column(JSON, comment="参数配置(JSON)")
    created_by = Column(BigInteger, comment="创建人ID")
    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True, comment="创建时间")
    updated_at = Column(DateTime, onupdate=func.now(), server_default=func.now(), comment="更新时间")
    last_used_at = Column(DateTime, comment="最后使用时间")

    def __repr__(self):
        return f"<Script(id={self.id}, name={self.name}, type={self.script_type})>"
