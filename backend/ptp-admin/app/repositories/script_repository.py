"""
脚本管理 Repository

负责数据库层面的脚本操作
"""

from sqlalchemy.orm import Session
from typing import List, Tuple, Optional
from app.models.script import Script, ScriptType, ScriptStatus

class ScriptRepository:
    """脚本数据访问层"""

    def __init__(self, db: Session):
        self.db = db

    def find_by_id(self, script_id: int) -> Optional[Script]:
        """根据 ID 查询"""
        return self.db.query(Script).filter(Script.id == script_id).first()

    def find_by_name(self, name: str) -> Optional[Script]:
        """根据名称查询"""
        return self.db.query(Script).filter(Script.name == name).first()

    def find_all(
        self,
        status: Optional[ScriptStatus] = None,
        script_type: Optional[ScriptType] = None,
        skip: int = 0,
        limit: int = 10
    ) -> Tuple[List[Script], int]:
        """查询所有脚本"""
        query = self.db.query(Script)

        if status:
            query = query.filter(Script.status == status)

        if script_type:
            query = query.filter(Script.script_type == script_type)

        total = query.count()
        scripts = query.order_by(Script.updated_at.desc()).offset(skip).limit(limit).all()

        return scripts, total

    def find_by_hash(self, content_hash: str) -> Optional[Script]:
        """根据内容哈希查询"""
        return self.db.query(Script).filter(Script.content_hash == content_hash).first()

    def search(self, keyword: str, skip: int = 0, limit: int = 10) -> Tuple[List[Script], int]:
        """搜索脚本（按名称或描述）"""
        query = self.db.query(Script).filter(
            Script.name.contains(keyword) | Script.description.contains(keyword)
        )

        total = query.count()
        scripts = query.order_by(Script.updated_at.desc()).offset(skip).limit(limit).all()

        return scripts, total

    def create(self, script: Script) -> Script:
        """创建脚本"""
        self.db.add(script)
        self.db.commit()
        self.db.refresh(script)
        return script

    def update(self, script: Script) -> Script:
        """更新脚本"""
        self.db.commit()
        self.db.refresh(script)
        return script

    def delete(self, script_id: int) -> bool:
        """删除脚本（软删除）"""
        script = self.find_by_id(script_id)
        if script:
            script.status = ScriptStatus.DELETED
            self.db.commit()
            return True
        return False

    def hard_delete(self, script_id: int) -> bool:
        """硬删除脚本"""
        script = self.find_by_id(script_id)
        if script:
            self.db.delete(script)
            self.db.commit()
            return True
        return False

    def get_statistics(self) -> dict:
        """获取脚本统计信息"""
        total = self.db.query(Script).count()
        active = self.db.query(Script).filter(Script.status == ScriptStatus.ACTIVE).count()
        jmeter = self.db.query(Script).filter(Script.script_type == ScriptType.JMETER).count()
        k6 = self.db.query(Script).filter(Script.script_type == ScriptType.K6).count()

        return {
            "total": total,
            "active": active,
            "inactive": total - active,
            "jmeter": jmeter,
            "k6": k6
        }
