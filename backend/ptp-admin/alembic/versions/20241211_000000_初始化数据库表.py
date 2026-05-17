"""初始化数据库表

Revision ID: 000000
Revises:
Create Date: 2024-12-11 00:00:00

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '000000'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 创建用户表
    op.create_table('olh_user',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False, comment='用户ID'),
        sa.Column('username', sa.String(length=100), nullable=False, comment='用户名'),
        sa.Column('email', sa.String(length=255), nullable=False, comment='邮箱'),
        sa.Column('full_name', sa.String(length=255), nullable=False, comment='全名'),
        sa.Column('hashed_password', sa.String(length=255), nullable=False, comment='加密密码'),
        sa.Column('role', sa.Enum('ADMIN', 'MANAGER', 'TESTER', 'VIEWER', name='userrole'), nullable=False, comment='用户角色'),
        sa.Column('status', sa.Enum('ACTIVE', 'INACTIVE', 'LOCKED', name='userstatus'), nullable=False, comment='用户状态'),
        sa.Column('is_superuser', sa.Boolean(), nullable=True, comment='是否超级用户'),
        sa.Column('is_active', sa.Boolean(), nullable=True, comment='是否激活'),
        sa.Column('last_login_at', sa.DateTime(), nullable=True, comment='最后登录时间'),
        sa.Column('login_count', sa.Integer(), nullable=True, comment='登录次数'),
        sa.Column('failed_login_attempts', sa.Integer(), nullable=True, comment='失败登录次数'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False, comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=True, comment='更新时间'),
        sa.PrimaryKeyConstraint('id'),
        sa.Index('ix_olh_user_email', 'email'),
        sa.Index('ix_olh_user_username', 'username'),
        sa.UniqueConstraint('username'),
        sa.UniqueConstraint('email')
    )

    # 创建脚本表
    op.create_table('olh_script',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False, comment='脚本ID'),
        sa.Column('name', sa.String(length=255), nullable=False, comment='脚本名称'),
        sa.Column('description', sa.Text(), nullable=True, comment='脚本描述'),
        sa.Column('script_type', sa.Enum('JMETER', 'K6', name='scripttype'), nullable=False, comment='脚本类型'),
        sa.Column('file_path', sa.String(length=500), nullable=False, comment='脚本文件路径'),
        sa.Column('file_size', sa.Integer(), nullable=True, comment='文件大小(字节)'),
        sa.Column('content_hash', sa.String(length=64), nullable=True, comment='文件内容哈希(SHA256)'),
        sa.Column('version', sa.String(length=50), nullable=True, comment='脚本版本'),
        sa.Column('status', sa.Enum('ACTIVE', 'INACTIVE', 'DELETED', name='scriptstatus'), nullable=False, comment='脚本状态'),
        sa.Column('tags', sa.JSON(), nullable=True, comment='标签(JSON数组)'),
        sa.Column('parameters', sa.JSON(), nullable=True, comment='参数配置(JSON)'),
        sa.Column('created_by', sa.BigInteger(), nullable=True, comment='创建人ID'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False, comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=True, comment='更新时间'),
        sa.Column('last_used_at', sa.DateTime(), nullable=True, comment='最后使用时间'),
        sa.PrimaryKeyConstraint('id'),
        sa.Index('ix_olh_script_created_at', 'created_at'),
        sa.Index('ix_olh_script_status', 'status')
    )

    # 创建任务表
    op.create_table('olh_task',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False, comment='任务ID'),
        sa.Column('name', sa.String(length=255), nullable=False, comment='任务名称'),
        sa.Column('description', sa.Text(), nullable=True, comment='任务描述'),
        sa.Column('script_id', sa.BigInteger(), nullable=False, comment='关联的脚本ID'),
        sa.Column('engine_type', sa.Enum('JMETER', 'K6', name='testenginetype'), nullable=False, comment='测试引擎类型'),
        sa.Column('thread_count', sa.Integer(), nullable=False, comment='线程数'),
        sa.Column('duration', sa.Integer(), nullable=False, comment='测试持续时间(秒)'),
        sa.Column('ramp_up', sa.Integer(), nullable=True, comment=' Ramp Up 时间(秒)'),
        sa.Column('status', sa.Enum('PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'CANCELLED', name='taskstatus'), nullable=False, comment='任务状态'),
        sa.Column('properties', sa.JSON(), nullable=True, comment='扩展属性(JSON)'),
        sa.Column('parameters', sa.JSON(), nullable=True, comment='参数配置(JSON)'),
        sa.Column('created_by', sa.BigInteger(), nullable=True, comment='创建人ID'),
        sa.Column('assigned_to', sa.BigInteger(), nullable=True, comment='执行人ID'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False, comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=True, comment='更新时间'),
        sa.Column('started_at', sa.DateTime(), nullable=True, comment='开始时间'),
        sa.Column('finished_at', sa.DateTime(), nullable=True, comment='完成时间'),
        sa.Column('priority', sa.Integer(), nullable=True, comment='优先级'),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['script_id'], ['olh_script.id'], ),
        sa.Index('ix_olh_task_created_at', 'created_at'),
        sa.Index('ix_olh_task_status', 'status')
    )

    # 创建报告表
    op.create_table('olh_report',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False, comment='报告ID'),
        sa.Column('task_id', sa.BigInteger(), nullable=False, comment='关联的任务ID'),
        sa.Column('name', sa.String(length=255), nullable=False, comment='报告名称'),
        sa.Column('description', sa.Text(), nullable=True, comment='报告描述'),
        sa.Column('report_type', sa.Enum('JMETER', 'K6', 'COMPARISON', name='reporttype'), nullable=False, comment='报告类型'),
        sa.Column('file_path', sa.String(length=500), nullable=True, comment='报告文件路径'),
        sa.Column('file_size', sa.Integer(), nullable=True, comment='文件大小(字节)'),
        sa.Column('status', sa.Enum('PENDING', 'GENERATING', 'COMPLETED', 'FAILED', 'DELETED', name='reportstatus'), nullable=False, comment='报告状态'),
        sa.Column('total_requests', sa.Integer(), nullable=True, comment='总请求数'),
        sa.Column('successful_requests', sa.Integer(), nullable=True, comment='成功请求数'),
        sa.Column('failed_requests', sa.Integer(), nullable=True, comment='失败请求数'),
        sa.Column('error_rate', sa.Float(), nullable=True, comment='错误率(%)'),
        sa.Column('avg_response_time', sa.Float(), nullable=True, comment='平均响应时间(ms)'),
        sa.Column('min_response_time', sa.Float(), nullable=True, comment='最小响应时间(ms)'),
        sa.Column('max_response_time', sa.Float(), nullable=True, comment='最大响应时间(ms)'),
        sa.Column('p95_response_time', sa.Float(), nullable=True, comment='P95响应时间(ms)'),
        sa.Column('p99_response_time', sa.Float(), nullable=True, comment='P99响应时间(ms)'),
        sa.Column('throughput', sa.Float(), nullable=True, comment='吞吐量(req/s)'),
        sa.Column('test_config', sa.JSON(), nullable=True, comment='测试配置(JSON)'),
        sa.Column('metrics_data', sa.JSON(), nullable=True, comment='详细指标数据(JSON)'),
        sa.Column('generated_by', sa.BigInteger(), nullable=True, comment='生成人ID'),
        sa.Column('generated_at', sa.DateTime(), nullable=True, comment='生成时间'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False, comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=True, comment='更新时间'),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['task_id'], ['olh_task.id'], ),
        sa.Index('ix_olh_report_created_at', 'created_at'),
        sa.Index('ix_olh_report_status', 'status')
    )

    # 创建审批表
    op.create_table('olh_approval',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False, comment='审批ID'),
        sa.Column('task_id', sa.BigInteger(), nullable=False, comment='关联的任务ID'),
        sa.Column('submitter_id', sa.BigInteger(), nullable=False, comment='提交人ID'),
        sa.Column('approver_id', sa.BigInteger(), nullable=True, comment='审批人ID'),
        sa.Column('status', sa.Enum('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED', name='approvalstatus'), nullable=False, comment='审批状态'),
        sa.Column('reason', sa.Text(), nullable=True, comment='提交理由/审批意见'),
        sa.Column('comment', sa.Text(), nullable=True, comment='审批备注'),
        sa.Column('priority', sa.Integer(), nullable=True, comment='优先级(0-10, 数字越大优先级越高)'),
        sa.Column('submitted_at', sa.DateTime(), server_default=sa.func.now(), nullable=False, comment='提交时间'),
        sa.Column('approved_at', sa.DateTime(), nullable=True, comment='审批时间'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False, comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=True, comment='更新时间'),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['task_id'], ['olh_task.id'], ),
        sa.Index('ix_olh_approval_created_at', 'created_at'),
        sa.Index('ix_olh_approval_status', 'status')
    )

    # 创建审批历史表
    op.create_table('olh_approval_history',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False, comment='历史记录ID'),
        sa.Column('approval_id', sa.BigInteger(), nullable=False, comment='审批ID'),
        sa.Column('action', sa.Enum('SUBMIT', 'APPROVE', 'REJECT', 'CANCEL', name='approvalaction'), nullable=False, comment='动作类型'),
        sa.Column('user_id', sa.BigInteger(), nullable=False, comment='操作用户ID'),
        sa.Column('comment', sa.Text(), nullable=True, comment='操作备注'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False, comment='操作时间'),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['approval_id'], ['olh_approval.id'], ),
        sa.Index('ix_olh_approval_history_created_at', 'created_at')
    )


def downgrade() -> None:
    op.drop_table('olh_approval_history')
    op.drop_table('olh_approval')
    op.drop_table('olh_report')
    op.drop_table('olh_task')
    op.drop_table('olh_script')
    op.drop_table('olh_user')