import { Badge, Tag } from 'antd'
import type { BadgeProps } from 'antd'
import { PlayCircleOutlined, SyncOutlined } from '@ant-design/icons'

export type StatusType =
  | 'draft'
  | 'pending_submit_approval'
  | 'ready'
  | 'script_missing'
  | 'awaiting_approval'
  | 'approval_rejected'
  | 'pending'
  | 'preparing'
  | 'running'
  | 'succeeded'
  | 'completed'
  | 'failed'
  | 'stopped'
  | 'suspended'
  | 'deleted'
  | 'duplicated'
  | 'success'
  | 'warning'
  | 'error'

interface StatusBadgeProps {
  status: StatusType
  text?: string
  showDot?: boolean
  className?: string
}

const statusConfig: Record<
  StatusType,
  { color: NonNullable<BadgeProps['status']>; text: string }
> = {
  draft: { color: 'default', text: '草稿' },
  pending_submit_approval: { color: 'default', text: '待处理' },
  awaiting_approval: { color: 'default', text: '待处理' },
  approval_rejected: { color: 'default', text: '待处理' },
  script_missing: { color: 'warning', text: '脚本缺失' },
  ready: { color: 'success', text: '可执行' },
  pending: { color: 'default', text: '待处理' },
  preparing: { color: 'processing', text: '准备中' },
  running: { color: 'processing', text: '运行中' },
  succeeded: { color: 'success', text: '成功' },
  completed: { color: 'success', text: '已完成' },
  failed: { color: 'error', text: '失败' },
  stopped: { color: 'default', text: '已停止' },
  suspended: { color: 'warning', text: '已挂起' },
  deleted: { color: 'default', text: '已删除' },
  duplicated: { color: 'warning', text: '重复' },
  success: { color: 'success', text: '成功' },
  warning: { color: 'warning', text: '警告' },
  error: { color: 'error', text: '错误' },
}

const getStatusIcon = (status: StatusType) => {
  if (status === 'running') {
    return <PlayCircleOutlined spin />
  }
  if (status === 'preparing') {
    return <SyncOutlined spin />
  }
  return undefined
}

const StatusBadge: React.FC<StatusBadgeProps> = ({
  status,
  text,
  showDot = false,
  className,
}) => {
  const config = statusConfig[status]
  const displayText = text || config.text
  const mergedClassName = [
    'olh-status-badge',
    `olh-status-badge--${status}`,
    className,
  ].filter(Boolean).join(' ')

  if (showDot) {
    return (
      <Badge
        className={mergedClassName}
        status={config.color}
        text={displayText}
      />
    )
  }

  return (
    <Tag
      className={mergedClassName}
      color={config.color}
      icon={getStatusIcon(status)}
      style={{ margin: 0 }}
    >
      {displayText}
    </Tag>
  )
}

export default StatusBadge
