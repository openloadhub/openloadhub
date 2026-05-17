import { useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Row,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import {
  EditOutlined,
  FileTextOutlined,
  LineChartOutlined,
  PlayCircleOutlined,
  RocketOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import type { ColumnsType } from 'antd/es/table'

import { StatusBadge } from '@/components/common'
import StartRunDrawer from '@/components/task/StartRunDrawer'
import { publicAlphaFeatures } from '@/config/publicAlpha'
import { runApi, type Run, type RunStatus } from '@/services/runApi'
import { taskApi, type Task, type TaskStatus } from '@/services/taskApi'
import { canEnterStartFlow, canPrepareTaskForRun, canStartTask, getTaskEffectiveStartStatus, getTaskEffectiveStatusLabel } from '@/utils/taskStartGuard'
import './index.css'

const { Title, Text, Link } = Typography

const TASK_STATUS_HINT: Record<TaskStatus, string> = {
  draft: '任务仍是草稿，启动前请先执行“准备执行”。',
  pending_submit_approval: '任务处于兼容状态，建议重新准备执行后再启动。',
  ready: '任务已处于可执行状态，可以直接启动压测。',
  script_missing: '任务缺少可用脚本，请先补齐脚本后再准备执行。',
  awaiting_approval: '任务处于兼容状态，建议重新准备执行后再启动。',
  approval_rejected: '任务处于兼容状态，建议重新准备执行后再启动。',
  running: '任务已有执行进行中，当前不允许再次准备执行。',
}

const parseStringArray = (value: unknown): string[] => {
  if (Array.isArray(value)) {
    return value.filter(item => typeof item === 'string' && item.trim()).map(item => item.trim())
  }
  if (typeof value === 'string' && value.trim()) {
    return value
      .split(',')
      .map(item => item.trim())
      .filter(Boolean)
  }
  return []
}

type RelatedMonitorView = {
  title: string
  url: string
  kind?: string
  embed_mode?: string
  description?: string
}

type AlertPolicyView = {
  name: string
  enabled: boolean
  auto_stop_enabled: boolean
  observe_only: boolean
  source?: string
  match: {
    subscription?: string
    alertname?: string
    severity?: string[]
  }
  actions: string[]
  cooldown_seconds?: number
  for_seconds?: number
}

const asRecord = (value: unknown): Record<string, unknown> => (
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
)

const parseBooleanValue = (value: unknown): boolean | undefined => {
  if (typeof value === 'boolean') {
    return value
  }
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase()
    if (normalized === 'true') {
      return true
    }
    if (normalized === 'false') {
      return false
    }
  }
  return undefined
}

const parsePositiveNumber = (value: unknown): number | undefined => {
  if (typeof value === 'number' && Number.isFinite(value) && value >= 0) {
    return value
  }
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value)
    if (Number.isFinite(parsed) && parsed >= 0) {
      return parsed
    }
  }
  return undefined
}

const normalizeRelatedMonitors = (value: unknown, compatibilityUrl?: unknown): RelatedMonitorView[] => {
  const items = Array.isArray(value) ? value : []
  const monitors: RelatedMonitorView[] = []
  items.forEach(item => {
    const raw = asRecord(item)
    const title = typeof raw.title === 'string' ? raw.title.trim() : typeof raw.name === 'string' ? raw.name.trim() : ''
    const url = typeof raw.url === 'string' ? raw.url.trim() : typeof raw.link === 'string' ? raw.link.trim() : ''
    const kind = typeof raw.kind === 'string' ? raw.kind.trim() : undefined
    const embedMode = typeof raw.embed_mode === 'string' ? raw.embed_mode.trim() : undefined
    const description = typeof raw.description === 'string' ? raw.description.trim() : undefined
    if (!title && !url && !kind && !description) {
      return
    }
    monitors.push({
      title: title || '关联监控',
      url,
      kind,
      embed_mode: embedMode,
      description,
    })
  })
  if (monitors.length > 0) {
    return monitors
  }
  const fallbackUrl = typeof compatibilityUrl === 'string' ? compatibilityUrl.trim() : ''
  return fallbackUrl ? [{ title: '关联监控', url: fallbackUrl, embed_mode: 'new_tab' }] : []
}

const normalizeAlertPolicies = (value: unknown): AlertPolicyView[] => {
  const items = Array.isArray(value) ? value : []
  const policies: AlertPolicyView[] = []
  items.forEach(item => {
    const raw = asRecord(item)
    const match = asRecord(raw.match)
    const name = typeof raw.name === 'string' ? raw.name.trim() : ''
    const source = typeof raw.source === 'string' ? raw.source.trim() : undefined
    const subscription = typeof match.subscription === 'string' ? match.subscription.trim() : undefined
    const alertname = typeof match.alertname === 'string' ? match.alertname.trim() : undefined
    const severity = parseStringArray(match.severity)
    const actions = parseStringArray(raw.actions)
    const cooldownSeconds = parsePositiveNumber(raw.cooldown_seconds)
    const forSeconds = parsePositiveNumber(raw.for_seconds)
    const autoStopEnabled = parseBooleanValue(raw.auto_stop_enabled) ?? false
    const observeOnly = parseBooleanValue(raw.observe_only) ?? true
    if (!name && !source && !subscription && !alertname && severity.length === 0 && actions.length === 0 && cooldownSeconds === undefined && forSeconds === undefined) {
      return
    }
    policies.push({
      name: name || '未命名策略',
      enabled: raw.enabled !== false,
      auto_stop_enabled: autoStopEnabled,
      observe_only: observeOnly,
      source,
      match: {
        ...(subscription ? { subscription } : {}),
        ...(alertname ? { alertname } : {}),
        ...(severity.length > 0 ? { severity } : {}),
      },
      actions,
      ...(cooldownSeconds !== undefined ? { cooldown_seconds: cooldownSeconds } : {}),
      ...(forSeconds !== undefined ? { for_seconds: forSeconds } : {}),
    })
  })
  return policies
}

const getTaskFocusFields = (task: Task) => {
  const properties = task.properties && typeof task.properties === 'object'
    ? task.properties as Record<string, unknown>
    : {}
  const compatibilityMonitorLink = task.monitor_link || (
    typeof properties.monitor_link === 'string'
      ? properties.monitor_link
      : typeof properties.monitor_dashboard_url === 'string'
        ? properties.monitor_dashboard_url
        : null
  )

  return {
    scriptName: task.script_name || null,
    businessLine: task.business_line || (typeof properties.business_line === 'string' ? properties.business_line : null),
    relatedApps: task.related_apps ?? parseStringArray(properties.related_apps ?? properties.related_apps),
    monitorLink: compatibilityMonitorLink,
    relatedMonitors: normalizeRelatedMonitors(properties.related_monitors, compatibilityMonitorLink),
    alertSubscriptions: parseStringArray(properties.alert_subscriptions),
    alertPolicies: normalizeAlertPolicies(properties.alert_policies),
    traceLink: task.trace_link || (typeof properties.trace_link === 'string' ? properties.trace_link : null),
    resourceType: task.resource_type || (typeof properties.resource_type === 'string' ? properties.resource_type : null),
    cloudVendor: task.cloud_vendor || (typeof properties.cloud_vendor === 'string' ? properties.cloud_vendor : null),
    podCount: task.pod_count ?? (
      typeof properties.pod_count === 'number'
        ? properties.pod_count
        : typeof properties.pod_num === 'number'
          ? properties.pod_num
          : null
    ),
    dataDistribution: task.data_distribution || (
      typeof properties.data_distribution === 'string' ? properties.data_distribution : null
    ),
    metricsEnabled: task.metrics_enabled ?? (
      typeof properties.metrics_enabled === 'boolean' ? properties.metrics_enabled : null
    ),
    extraDashboardUrl: task.extra_dashboard_url || (
      typeof properties.extra_dashboard_url === 'string' ? properties.extra_dashboard_url : null
    ),
  }
}

const toDistributionLabel = (value: string | null) => {
  if (value === 'all') {
    return '全量下发'
  }
  if (value === 'avg') {
    return '平均分割'
  }
  return '-'
}

const TaskDetail = () => {
  const navigate = useNavigate()
  const { taskId } = useParams<{ taskId: string }>()
  const queryClient = useQueryClient()
  const taskIdNum = Number(taskId)
  const [startDrawerOpen, setStartDrawerOpen] = useState(false)
  const [startTaskSnapshot, setStartTaskSnapshot] = useState<Task | null>(null)
  const [startActionPending, setStartActionPending] = useState(false)

  const { data: task, isLoading: taskLoading, error: taskError } = useQuery({
    queryKey: ['task', taskIdNum],
    queryFn: () => taskApi.getTaskDetail(taskIdNum),
    enabled: Number.isFinite(taskIdNum),
  })

  const { data: runsData, isLoading: runsLoading } = useQuery({
    queryKey: ['runs', 'recent', taskIdNum],
    queryFn: () => runApi.getRunList({ task_id: taskIdNum, page: 1, pageSize: 5 }),
    enabled: Number.isFinite(taskIdNum),
  })

  const prepareTaskMutation = useMutation({
    mutationFn: () => taskApi.prepareTaskForRun(taskIdNum),
    onSuccess: payload => {
      queryClient.invalidateQueries({ queryKey: ['task', taskIdNum] })
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      if (payload.task.status === 'script_missing') {
        message.warning(payload.status_hint || '任务缺少可用脚本，请先补齐脚本后再启动')
        return
      }
      if (!task) {
        return
      }
      if (payload.next_action === 'start_run' || payload.task.status === 'ready') {
        const nextTask = {
          ...task,
          ...payload.task,
          last_run_status: undefined,
          last_run_status_label: undefined,
        }
        queryClient.setQueryData(['task', taskIdNum], nextTask)
        setStartTaskSnapshot(nextTask)
        setStartDrawerOpen(true)
        message.success(payload.status_hint || '任务已通过启动前校验，继续填写参数后即可启动压测')
        return
      }
      message.warning(payload.status_hint || '当前任务状态暂不能启动压测')
    },
    onError: error => {
      message.error(error instanceof Error ? error.message : '启动前校验失败')
    },
    onSettled: () => {
      setStartActionPending(false)
    },
  })

  const recentRuns = runsData?.items ?? []
  const focusFields = task ? getTaskFocusFields(task) : null
  const canStart = canStartTask(task)
  const canEnterStart = canEnterStartFlow(task)
  const effectiveStatus = getTaskEffectiveStartStatus(task)
  const effectiveStatusLabel = getTaskEffectiveStatusLabel(task)

  const handleStartTask = () => {
    if (!task) {
      return
    }
    if (canStartTask(task)) {
      setStartTaskSnapshot(task)
      setStartDrawerOpen(true)
      return
    }
    if (!canPrepareTaskForRun(task)) {
      message.warning('当前任务暂不能启动压测')
      return
    }
    setStartActionPending(true)
    prepareTaskMutation.mutate()
  }

  const runColumns: ColumnsType<Run> = useMemo(
    () => [
      {
        title: 'Run ID',
        dataIndex: 'run_id',
        key: 'run_id',
        width: 86,
        render: (id: number) => <a onClick={() => navigate(`/runs/${id}`)}>{id}</a>,
      },
      {
        title: '状态',
        dataIndex: 'run_status',
        key: 'run_status',
        width: 100,
        render: (status: string) => <StatusBadge status={status as RunStatus} />,
      },
      {
        title: '引擎',
        dataIndex: 'engine_type',
        key: 'engine_type',
        width: 88,
        render: (engine: string) => <Tag>{engine.toUpperCase()}</Tag>,
      },
      {
        title: '环境',
        dataIndex: 'env',
        key: 'env',
        width: 88,
      },
      {
        title: '开始时间',
        dataIndex: 'started_at',
        key: 'started_at',
        width: 168,
        render: (time: string) => time ? dayjs(time).format('YYYY-MM-DD HH:mm:ss') : '-',
      },
      {
        title: '耗时',
        dataIndex: 'duration_seconds',
        key: 'duration_seconds',
        width: 88,
        render: (duration: number) => duration ? `${duration}s` : '-',
      },
      {
        title: 'RPS',
        dataIndex: 'rps',
        key: 'rps',
        width: 90,
        render: (rps: number) => rps ? rps.toFixed(2) : '-',
      },
      {
        title: '成功率',
        dataIndex: 'success_rate',
        key: 'success_rate',
        width: 96,
        render: (rate: number) => rate !== null && rate !== undefined
          ? (
              <Tag color={rate >= 95 ? 'success' : rate >= 80 ? 'warning' : 'error'}>
                {(rate * 100).toFixed(1)}%
              </Tag>
            )
          : '-',
      },
    ],
    [navigate]
  )

  if (taskError) {
    message.error('加载任务详情失败')
    return null
  }

  if (taskLoading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 100 }}>
        <Spin size="large" />
      </div>
    )
  }

  if (!task || !focusFields) {
    return null
  }

  return (
    <Space direction="vertical" size={16} className="olh-task-detail-page" style={{ display: 'flex' }}>
      <Card size="small" className="olh-task-detail-hero">
        <Row justify="space-between" align="middle" gutter={[16, 16]}>
          <Col flex="auto">
            <Space direction="vertical" size={6}>
              <Space wrap size={[8, 8]}>
                <Title level={4} style={{ margin: 0 }}>{task.name}</Title>
                <StatusBadge status={task.status as TaskStatus} />
                {focusFields.scriptName ? <Tag color="blue">{focusFields.scriptName}</Tag> : null}
                {focusFields.businessLine ? <Tag>{focusFields.businessLine}</Tag> : null}
              </Space>
              <Space wrap size={[8, 8]}>
                <Text type="secondary">任务 ID {task.id}</Text>
                <Text type="secondary">环境 {task.env}</Text>
                <Text type="secondary">引擎 {task.engine_type.toUpperCase()}</Text>
                <Text type="secondary">任务模式 {task.task_pattern}</Text>
                <Text type="secondary">创建人 {task.created_by_name || '-'}</Text>
              </Space>
              {focusFields.relatedApps.length > 0 ? (
                <Space wrap size={[8, 8]}>
                  <Text type="secondary">关联应用</Text>
                  {focusFields.relatedApps.map(app => (
                    <Tag key={app}>{app}</Tag>
                  ))}
                </Space>
              ) : null}
            </Space>
          </Col>
          <Col>
            <Space wrap>
              <Button
                type="primary"
                icon={<PlayCircleOutlined />}
                disabled={!canEnterStart}
                loading={startActionPending && prepareTaskMutation.isPending}
                onClick={handleStartTask}
              >
                启动压测
              </Button>
              <Button icon={<EditOutlined />} onClick={() => navigate(`/tasks/${task.id}/edit`)}>
                编辑任务
              </Button>
            </Space>
          </Col>
        </Row>
      </Card>

      <Alert
        type={canStart ? 'success' : 'info'}
        showIcon
        message={`当前状态：${canStart ? '可执行' : effectiveStatusLabel}`}
        description={
          effectiveStatus === 'running'
            ? '任务最近一次执行仍在运行中，当前不允许再次启动。'
            : effectiveStatus === 'preparing'
              ? '任务最近一次执行仍在准备中，请等待当前执行进入运行/终态后再尝试。'
              : TASK_STATUS_HINT[task.status as TaskStatus]
        }
      />

      {task.scenario_quality_lint?.warning_count ? (
        <Alert
          type="warning"
          showIcon
          message={`场景配置提示：${task.scenario_quality_lint.warning_count} 项建议补齐`}
          description={(
            <Space direction="vertical" size={4}>
              {task.scenario_quality_lint.issues.slice(0, 6).map(issue => (
                <Text key={issue.code}>
                  {issue.label}：{issue.detail}
                  {issue.recommended_action ? ` 建议：${issue.recommended_action}` : ''}
                </Text>
              ))}
            </Space>
          )}
        />
      ) : null}

      <Row gutter={[16, 16]}>
        <Col xs={24} xl={12}>
          <Card size="small" title="任务基础信息" className="olh-task-detail-panel">
            <Descriptions column={1} size="small" colon>
              <Descriptions.Item label="任务名称">{task.name}</Descriptions.Item>
              <Descriptions.Item label="任务描述">{task.description || '-'}</Descriptions.Item>
              <Descriptions.Item label="脚本名称">{focusFields.scriptName || '-'}</Descriptions.Item>
              <Descriptions.Item label="任务模式">{task.task_pattern}</Descriptions.Item>
              <Descriptions.Item label="协议类型">
                <Space wrap size={[8, 8]}>
                  {task.protocols.map(protocol => (
                    <Tag key={protocol}>{protocol.toUpperCase()}</Tag>
                  ))}
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="线程 / 时长 / 预热">
                {`${task.thread_count} / ${task.duration}s / ${task.ramp_up || 0}s`}
              </Descriptions.Item>
              <Descriptions.Item label="协作人">
                {task.collaborator_names?.length ? task.collaborator_names.join('、') : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="创建时间">
                {task.created_at ? dayjs(task.created_at).format('YYYY-MM-DD HH:mm:ss') : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="更新时间">
                {task.updated_at ? dayjs(task.updated_at).format('YYYY-MM-DD HH:mm:ss') : '-'}
              </Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>

        <Col xs={24} xl={12}>
          <Card size="small" title="关联信息与资源" className="olh-task-detail-panel">
            <Descriptions column={1} size="small" colon>
              <Descriptions.Item label="业务线">{focusFields.businessLine || '-'}</Descriptions.Item>
              <Descriptions.Item label="压测环境">{task.env}</Descriptions.Item>
              <Descriptions.Item label="资源配置">
                {[
                  focusFields.resourceType,
                  focusFields.cloudVendor,
                  focusFields.podCount ? `${focusFields.podCount} Pods` : null,
                ].filter(Boolean).join(' / ') || '-'}
              </Descriptions.Item>
              <Descriptions.Item label="数据下发">{toDistributionLabel(focusFields.dataDistribution)}</Descriptions.Item>
              <Descriptions.Item label="指标上报">
                {typeof focusFields.metricsEnabled === 'boolean'
                  ? focusFields.metricsEnabled ? '开启' : '关闭'
                  : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="关联应用">
                {focusFields.relatedApps.length > 0 ? focusFields.relatedApps.join('、') : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="关联监控">
                {focusFields.relatedMonitors.length > 0 ? (
                  <Space direction="vertical" size={8} style={{ display: 'flex' }}>
                    {focusFields.relatedMonitors.map((monitor, index) => (
                      <div key={`${monitor.url || monitor.title}-${index}`}>
                        <Space wrap size={[6, 6]}>
                          <Text strong>{monitor.title}</Text>
                          {monitor.kind ? <Tag>{monitor.kind}</Tag> : null}
                          {monitor.embed_mode ? <Tag color={monitor.embed_mode === 'iframe' ? 'blue' : 'default'}>{monitor.embed_mode}</Tag> : null}
                        </Space>
                        {monitor.url ? (
                          <div>
                            <Link href={monitor.url} target="_blank">{monitor.url}</Link>
                          </div>
                        ) : null}
                        {monitor.description ? <Text type="secondary">{monitor.description}</Text> : null}
                      </div>
                    ))}
                  </Space>
                ) : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="告警订阅">
                {focusFields.alertSubscriptions.length > 0 ? (
                  <Space wrap size={[6, 6]}>
                    {focusFields.alertSubscriptions.map(item => <Tag key={item}>{item}</Tag>)}
                  </Space>
                ) : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="告警策略">
                {focusFields.alertPolicies.length > 0 ? (
                  <Space direction="vertical" size={8} style={{ display: 'flex' }}>
                    {focusFields.alertPolicies.map((policy, index) => (
                      <div key={`${policy.name}-${index}`}>
                        <Space wrap size={[6, 6]}>
                          <Text strong>{policy.name}</Text>
                          <Tag color={policy.enabled ? 'success' : 'default'}>{policy.enabled ? 'enabled' : 'disabled'}</Tag>
                          <Tag color={policy.auto_stop_enabled ? 'red' : 'default'}>
                            {policy.auto_stop_enabled ? '自动止停开启' : '自动止停关闭'}
                          </Tag>
                          <Tag color={policy.observe_only ? 'gold' : 'blue'}>
                            {policy.observe_only ? '仅观测' : '执行动作'}
                          </Tag>
                          {policy.source ? <Tag>{policy.source}</Tag> : null}
                          {policy.match.subscription ? <Tag color="blue">{policy.match.subscription}</Tag> : null}
                          {policy.match.alertname ? <Tag color="purple">{policy.match.alertname}</Tag> : null}
                          {policy.match.severity?.map(item => <Tag color="orange" key={item}>{item}</Tag>)}
                        </Space>
                        <div>
                          <Text type="secondary">
                            {[
                              `auto_stop_enabled: ${policy.auto_stop_enabled ? 'true' : 'false'}`,
                              `observe_only: ${policy.observe_only ? 'true' : 'false'}`,
                              policy.actions.length > 0 ? `actions: ${policy.actions.join(', ')}` : null,
                              policy.cooldown_seconds !== undefined ? `cooldown: ${policy.cooldown_seconds}s` : null,
                              policy.for_seconds !== undefined ? `for: ${policy.for_seconds}s` : null,
                            ].filter(Boolean).join(' / ') || '已保存匹配条件，当前未配置动作或时间窗口'}
                          </Text>
                        </div>
                      </div>
                    ))}
                  </Space>
                ) : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="关联链路">
                {focusFields.traceLink ? <Link href={focusFields.traceLink} target="_blank">{focusFields.traceLink}</Link> : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="扩展大盘">
                {focusFields.extraDashboardUrl
                  ? <Link href={focusFields.extraDashboardUrl} target="_blank">{focusFields.extraDashboardUrl}</Link>
                  : '-'}
              </Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
      </Row>

      <Card
        size="small"
        title="最近执行记录"
        className="olh-task-detail-panel olh-task-detail-run-table"
        extra={<a onClick={() => navigate(`/runs?task_id=${task.id}`)}>查看全部</a>}
      >
        <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
          默认展示最近 5 条执行，便于快速确认最近一次启动结果和基础指标。
        </Text>
        <Table
          columns={runColumns}
          dataSource={recentRuns}
          loading={runsLoading}
          rowKey="run_id"
          pagination={false}
          size="small"
          scroll={{ x: 960 }}
          locale={{ emptyText: '暂无执行记录' }}
        />
      </Card>

      <Card size="small" title="快捷入口" className="olh-task-detail-panel">
        <Row gutter={[12, 12]}>
          <Col xs={12} md={8} xl={4}>
            <Card size="small" hoverable style={{ textAlign: 'center' }} onClick={() => navigate(`/tasks/${task.id}/edit`)}>
              <EditOutlined style={{ fontSize: 20, marginBottom: 8 }} />
              <div>编辑任务</div>
            </Card>
          </Col>
          <Col xs={12} md={8} xl={4}>
            <Card size="small" hoverable style={{ textAlign: 'center' }} onClick={() => navigate(`/runs?task_id=${task.id}`)}>
              <PlayCircleOutlined style={{ fontSize: 20, marginBottom: 8 }} />
              <div>结果列表</div>
            </Card>
          </Col>
          {publicAlphaFeatures.trendAnalysis ? (
            <Col xs={12} md={8} xl={4}>
              <Card size="small" hoverable style={{ textAlign: 'center' }} onClick={() => navigate(`/trend-analysis?task_id=${task.id}&auto_open=1`)}>
                <LineChartOutlined style={{ fontSize: 20, marginBottom: 8 }} />
                <div>趋势分析</div>
              </Card>
            </Col>
          ) : null}
          <Col xs={12} md={8} xl={4}>
            <Card size="small" hoverable style={{ textAlign: 'center' }} onClick={() => navigate(`/reports?task_id=${task.id}`)}>
              <FileTextOutlined style={{ fontSize: 20, marginBottom: 8 }} />
              <div>报告列表</div>
            </Card>
          </Col>
          <Col xs={12} md={8} xl={4}>
            <Card
              size="small"
              hoverable={canEnterStart}
              style={{ textAlign: 'center', opacity: canEnterStart ? 1 : 0.6 }}
              onClick={handleStartTask}
            >
              <RocketOutlined style={{ fontSize: 20, marginBottom: 8 }} />
              <div>启动压测</div>
            </Card>
          </Col>
        </Row>
      </Card>

      <StartRunDrawer
        open={startDrawerOpen}
        task={startTaskSnapshot ?? task}
        onClose={() => {
          setStartDrawerOpen(false)
          setStartTaskSnapshot(null)
        }}
        onCreated={() => {
          queryClient.invalidateQueries({ queryKey: ['runs'] })
          queryClient.invalidateQueries({ queryKey: ['runs', 'recent', taskIdNum] })
        }}
      />
    </Space>
  )
}

export default TaskDetail
