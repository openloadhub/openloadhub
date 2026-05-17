import { useMemo, useState } from 'react'
import { Button, Descriptions, Drawer, Empty, Modal, Space, Table, Tag, Typography } from 'antd'
import { useMutation, useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'

import {
  taskApi,
  type Task,
  type TaskScriptCompareResponse,
  type TaskVersionDetail,
  type TaskVersionRecord,
} from '@/services/taskApi'
import { formatLocalDateTime } from '@/utils/localDateTime'

const { Text } = Typography

type VersionTask = Pick<Task, 'id' | 'name'>

type DiffLine = {
  content: string
  line_no?: number
  changed: boolean
}

interface TaskVersionDrawerProps {
  open: boolean
  task: VersionTask | null
  currentScriptVersion?: string | null
  onPreviewVersion?: (compareData: TaskScriptCompareResponse) => void
  onClose: () => void
}

const buildDiffLines = (content: string, otherContent: string): DiffLine[] => {
  const lines = content.split('\n')
  const otherLines = otherContent.split('\n')
  const length = Math.max(lines.length, otherLines.length)
  return Array.from({ length }, (_item, index) => {
    const currentLine = lines[index]
    const otherLine = otherLines[index]
    const hasCurrentLine = typeof currentLine === 'string'
    return {
      content: currentLine ?? '',
      line_no: hasCurrentLine ? index + 1 : undefined,
      changed: currentLine !== otherLine,
    }
  })
}

const countChangedLines = (leftContent: string, rightContent: string) =>
  buildDiffLines(leftContent, rightContent).filter(item => item.changed).length

const asRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}

const formatSnapshotValue = (value: unknown): string => {
  if (Array.isArray(value)) {
    return value.length > 0 ? value.join(', ') : '-'
  }
  if (typeof value === 'boolean') {
    return value ? 'true' : 'false'
  }
  if (value === null || value === undefined || value === '') {
    return '-'
  }
  return String(value)
}

const renderCodePane = (
  title: string,
  version: string,
  fileName: string | null | undefined,
  lines: DiffLine[],
  tagColor: string
) => (
  <div className="olh-task-version-code-pane">
    <div className="olh-task-version-code-head">
      <Space direction="vertical" size={2}>
        <Text strong className="olh-task-version-code-title">
          {title}
        </Text>
        <Text type="secondary">{fileName || '未命名脚本'}</Text>
      </Space>
      <Tag color={tagColor} style={{ marginInlineEnd: 0 }}>
        {version}
      </Tag>
    </div>
    <div className="olh-task-version-code-body">
      {lines.map((line, index) => (
        <div
          key={`${title}-${index}`}
          className={`olh-task-version-code-line${line.changed ? ' olh-task-version-code-line--changed' : ''}`}
        >
          <span className="olh-task-version-code-line-no">{line.line_no ?? ''}</span>
          <span className="olh-task-version-code-content">
            {line.content || ' '}
          </span>
        </div>
      ))}
    </div>
  </div>
)

const TaskVersionDrawer: React.FC<TaskVersionDrawerProps> = ({
  open,
  task,
  currentScriptVersion,
  onPreviewVersion,
  onClose,
}) => {
  const [compareData, setCompareData] = useState<TaskScriptCompareResponse | null>(null)
  const [snapshotDetail, setSnapshotDetail] = useState<TaskVersionDetail | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['task-versions', task?.id],
    queryFn: () => taskApi.getTaskVersions(task!.id, { page: 1, pageSize: 20 }),
    enabled: open && !!task,
  })

  const compareMutation = useMutation({
    mutationFn: ({ historyVersion }: { historyVersion: string; action: 'compare' | 'preview' }) =>
      taskApi.compareTaskScript(task!.id, historyVersion),
    onSuccess: (response, variables) => {
      if (variables.action === 'preview') {
        onPreviewVersion?.(response)
        onClose()
        return
      }
      setCompareData(response)
    },
  })

  const snapshotMutation = useMutation({
    mutationFn: (version: string) => taskApi.getTaskVersionDetail(task!.id, version),
    onSuccess: response => {
      setSnapshotDetail(response)
    },
  })

  const currentVersion = data?.items?.find(item => item.is_current)?.version ?? data?.items?.[0]?.version

  const columns = useMemo(
    () => [
      {
        title: 'Task ID',
        key: 'task_id',
        width: 88,
        render: () => task?.id ?? '-',
      },
      {
        title: 'Task 名称',
        key: 'task_name',
        width: 220,
        render: () => task?.name || '-',
      },
      {
        title: '任务版本',
        dataIndex: 'version',
        key: 'version',
        width: 120,
        render: (value: string, record: TaskVersionRecord) => (
          <Space size={6}>
            <Tag color="cyan">{value}</Tag>
            {record.is_current ? <Tag color="green">当前版本</Tag> : null}
          </Space>
        ),
      },
      {
        title: '脚本',
        dataIndex: 'script_name',
        key: 'script_name',
        width: 220,
        render: (value: string | null | undefined) => value || '-',
      },
      {
        title: '修改人',
        dataIndex: 'created_by_name',
        key: 'created_by_name',
        width: 110,
        render: (value: string | null | undefined) => value || '-',
      },
      {
        title: '更新时间',
        dataIndex: 'updated_at',
        key: 'updated_at',
        width: 170,
        render: (value: string | null | undefined, record: TaskVersionRecord) =>
          formatLocalDateTime(value || record.created_at),
      },
      {
        title: '操作',
        key: 'action',
        width: 220,
        render: (_value: unknown, record: TaskVersionRecord) => {
          if (record.version === currentVersion) {
            return (
              <Space size={0}>
                <Button
                  type="link"
                  size="small"
                  loading={snapshotMutation.isPending}
                  onClick={() => snapshotMutation.mutate(record.version)}
                >
                  查看版本
                </Button>
                <Text type="secondary">当前版本</Text>
              </Space>
            )
          }
          return (
            <Space size={0}>
              <Button
                type="link"
                size="small"
                loading={snapshotMutation.isPending}
                onClick={() => snapshotMutation.mutate(record.version)}
              >
                查看版本
              </Button>
              {onPreviewVersion ? (
                <Button
                  type="link"
                  size="small"
                  loading={compareMutation.isPending}
                  onClick={() => compareMutation.mutate({ historyVersion: record.version, action: 'preview' })}
                >
                  预览脚本
                </Button>
              ) : null}
              <Button
                type="link"
                size="small"
                loading={compareMutation.isPending}
                onClick={() => compareMutation.mutate({ historyVersion: record.version, action: 'compare' })}
              >
                对比脚本
              </Button>
            </Space>
          )
        },
      },
    ],
    [compareMutation, currentVersion, onPreviewVersion, snapshotMutation, task?.name]
  )

  const changedLineCount = compareData
    ? countChangedLines(compareData.current.content, compareData.history.content)
    : 0

  const currentDiffLines = compareData
    ? buildDiffLines(compareData.current.content, compareData.history.content)
    : []
  const historyDiffLines = compareData
    ? buildDiffLines(compareData.history.content, compareData.current.content)
    : []

  const snapshot = asRecord(snapshotDetail?.snapshot)
  const snapshotProperties = asRecord(snapshot.properties)
  const snapshotVariables = asRecord(snapshotProperties.variables)
  const snapshotVariableTypes = asRecord(snapshotProperties.variable_types)
  const variableEntries = Object.entries(snapshotVariables)

  return (
    <>
      <Drawer
        title="任务历史版本"
        placement="right"
        width={760}
        open={open}
        onClose={onClose}
        extra={(
          <Space direction="vertical" size={0} style={{ alignItems: 'flex-end' }}>
            <Text type="secondary">{task?.name || '-'}</Text>
            <Text type="secondary">{`共 ${data?.total ?? 0} 个任务版本${currentVersion ? ` · 当前版本 ${currentVersion}` : ''}`}</Text>
            {currentScriptVersion ? <Text type="secondary">{`当前脚本版本 ${currentScriptVersion}`}</Text> : null}
          </Space>
        )}
      >
        {onPreviewVersion ? (
          <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
            任务版本记录每次保存后的配置；脚本版本记录脚本素材。可预览历史脚本，并与当前版本对比。
          </Text>
        ) : null}
        <Table<TaskVersionRecord>
          rowKey="id"
          loading={isLoading}
          pagination={false}
          size="small"
          dataSource={data?.items ?? []}
          columns={columns}
          locale={{
            emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无历史版本" />,
          }}
        />
      </Drawer>

      <Modal
        title="任务版本详情"
        open={!!snapshotDetail}
        onCancel={() => setSnapshotDetail(null)}
        footer={null}
        width={960}
        destroyOnClose
      >
        {snapshotDetail ? (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions
              bordered
              size="small"
              column={2}
              items={[
                { key: 'version', label: '任务版本', children: snapshotDetail.version },
                { key: 'created_by_name', label: '修改人', children: snapshotDetail.created_by_name || '-' },
                { key: 'updated_at', label: '更新时间', children: formatLocalDateTime(snapshotDetail.updated_at || snapshotDetail.created_at) },
                { key: 'script_version', label: '当前脚本版本', children: currentScriptVersion || '-' },
                { key: 'name', label: '任务名称', children: formatSnapshotValue(snapshot.name) },
                { key: 'description', label: '任务描述', children: formatSnapshotValue(snapshot.description) },
                { key: 'env', label: '压测环境', children: formatSnapshotValue(snapshot.env) },
                { key: 'business_line', label: '业务线', children: formatSnapshotValue(snapshotProperties.business_line) },
                { key: 'engine_type', label: '脚本类型', children: formatSnapshotValue(snapshot.engine_type) },
                { key: 'protocols', label: '协议类型', children: formatSnapshotValue(snapshot.protocols) },
                { key: 'resource_type', label: '资源类型', children: formatSnapshotValue(snapshotProperties.resource_type) },
                { key: 'cloud_vendor', label: '云环境', children: formatSnapshotValue(snapshotProperties.cloud_vendor) },
                { key: 'pod_count', label: '节点数量', children: formatSnapshotValue(snapshotProperties.pod_count ?? snapshotProperties.pod_num) },
                { key: 'data_distribution', label: '数据下发方式', children: formatSnapshotValue(snapshotProperties.data_distribution) },
                { key: 'related_apps', label: '关联应用', children: formatSnapshotValue(snapshotProperties.related_apps ?? snapshotProperties.related_apps) },
                { key: 'monitor_link', label: '关联监控', children: formatSnapshotValue(snapshotProperties.monitor_link ?? snapshotProperties.monitor_dashboard_url) },
                { key: 'trace_link', label: '关联链路', children: formatSnapshotValue(snapshotProperties.trace_link) },
                { key: 'alert_subscriptions', label: '告警订阅', children: formatSnapshotValue(snapshotProperties.alert_subscriptions) },
              ]}
            />

            <div>
              <Text strong>脚本变量</Text>
              <div className="olh-task-version-snapshot-box">
                {variableEntries.length > 0 ? (
                  <Space direction="vertical" size={8} style={{ width: '100%' }}>
                    {variableEntries.map(([key, value]) => (
                      <div key={key}>
                        <Text strong>{key}</Text>
                        <Text type="secondary">{` (${formatSnapshotValue(snapshotVariableTypes[key])})`}</Text>
                        <div>{formatSnapshotValue(value)}</div>
                      </div>
                    ))}
                  </Space>
                ) : (
                  <Text type="secondary">当前版本没有脚本变量。</Text>
                )}
              </div>
            </div>

            <div>
              <Text strong>原始任务配置 (JSON)</Text>
              <pre
                style={{
                  marginTop: 8,
                  padding: 12,
                  borderRadius: 8,
                  background: '#0f172a',
                  color: '#e2e8f0',
                  fontSize: 12,
                  lineHeight: 1.6,
                  overflow: 'auto',
                  maxHeight: 320,
                }}
              >
                {JSON.stringify(snapshotDetail.snapshot, null, 2)}
              </pre>
            </div>
          </Space>
        ) : null}
      </Modal>

      <Modal
        title="脚本版本对比"
        open={!!compareData}
        onCancel={() => setCompareData(null)}
        footer={null}
        width={1200}
        destroyOnClose
      >
        {compareData ? (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: '1fr auto 1fr',
                gap: 12,
                alignItems: 'stretch',
              }}
            >
              <div className="olh-task-version-compare-card olh-task-version-compare-card--current">
                <Text type="secondary">左侧口径</Text>
                <div style={{ marginTop: 4 }}>
                  <Tag color="green" style={{ marginInlineEnd: 8 }}>当前版本 {compareData.current.version}</Tag>
                  <Text strong>{compareData.current.file_name || '当前脚本'}</Text>
                </div>
                <div style={{ marginTop: 6 }}>
                  <Text type="secondary">
                    {compareData.current.created_by_name || '未知修改人'}
                    {compareData.current.updated_at ? ` · ${dayjs(compareData.current.updated_at).format('YYYY-MM-DD HH:mm:ss')}` : ''}
                  </Text>
                </div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <Tag color="processing" style={{ marginInlineEnd: 0 }}>差异行数 {changedLineCount}</Tag>
              </div>
              <div className="olh-task-version-compare-card olh-task-version-compare-card--history">
                <Text type="secondary">右侧口径</Text>
                <div style={{ marginTop: 4 }}>
                  <Tag color="purple" style={{ marginInlineEnd: 8 }}>对比版本 {compareData.history.version}</Tag>
                  <Text strong>{compareData.history.file_name || '历史脚本'}</Text>
                </div>
                <div style={{ marginTop: 6 }}>
                  <Text type="secondary">
                    {compareData.history.created_by_name || '未知修改人'}
                    {compareData.history.updated_at ? ` · ${dayjs(compareData.history.updated_at).format('YYYY-MM-DD HH:mm:ss')}` : ''}
                  </Text>
                </div>
              </div>
            </div>
            <Text type="secondary">
              左右两侧按行高亮差异，便于快速确认脚本改动范围。
            </Text>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
              {renderCodePane(
                '当前版本脚本',
                compareData.current.version,
                compareData.current.file_name,
                currentDiffLines,
                'geekblue'
              )}
              {renderCodePane(
                '对比版本脚本',
                compareData.history.version,
                compareData.history.file_name,
                historyDiffLines,
                'orange'
              )}
            </div>
          </Space>
        ) : null}
      </Modal>
    </>
  )
}

export default TaskVersionDrawer
