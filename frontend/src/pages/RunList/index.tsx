import { useEffect, useMemo, useState } from 'react'
import { Button, message, Card, Space, Tooltip } from 'antd'
import type { CSSProperties } from 'react'
import {
  CheckCircleOutlined,
  ControlOutlined,
  DashboardOutlined,
  DownloadOutlined,
  EyeOutlined,
  FieldTimeOutlined,
  FilterOutlined,
  SyncOutlined,
  StopOutlined,
  ThunderboltOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { DataTable, SearchFilter, StatusBadge } from '@/components/common'
import type { FilterField } from '@/components/common'
import { runApi, type Run, type RunQuery, type RunStatus } from '@/services/runApi'
import { reportApi } from '@/services/reportApi'
import { metaApi, type MetaItem } from '@/services/metaApi'
import { getCanonicalUserLabel } from '@/utils/displayUser'
import { formatLocalDateTime } from '@/utils/localDateTime'

const RUN_STATUS_VALUES: RunStatus[] = ['preparing', 'running', 'succeeded', 'failed', 'stopped']
const RUN_STATUS_LABELS: Record<RunStatus, string> = {
  preparing: '准备中',
  running: '运行中',
  succeeded: '成功',
  failed: '失败',
  stopped: '已停止',
}
const PROTOCOL_LABELS: Record<string, string> = {
  http: 'HTTP',
  grpc: 'GRPC',
  kafka: 'Kafka',
  websocket: 'WebSocket',
  browser: 'Browser',
  other: 'Other',
}
const TASK_PATTERN_LABELS: Record<string, string> = {
  script: '脚本',
  visualization: '可视化',
}
const SINGLE_LINE_TEXT_STYLE = {
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
} as const
const DEFAULT_RUN_LIST_PAGE_SIZE = 10

const scheduleIdleTask = (callback: () => void) => {
  if (typeof window !== 'undefined' && 'requestIdleCallback' in window) {
    const idleId = window.requestIdleCallback(callback)
    return () => window.cancelIdleCallback(idleId)
  }
  const timeoutId = globalThis.setTimeout(callback, 250)
  return () => globalThis.clearTimeout(timeoutId)
}

const getProtocolLabel = (protocol: string) => PROTOCOL_LABELS[String(protocol).toLowerCase()] || String(protocol).toUpperCase()

function renderSingleLineText(
  text: string | number | null | undefined,
  testId?: string,
) {
  const value = text === null || text === undefined || text === '' ? '-' : String(text)
  return (
    <div data-testid={testId} title={value} style={SINGLE_LINE_TEXT_STYLE}>
      {value}
    </div>
  )
}

function renderRunMetadataCell(
  primary: string | null | undefined,
  snapshot: string | null | undefined,
  testId: string,
) {
  const primaryValue = primary?.trim() || snapshot?.trim() || '-'
  const snapshotValue = snapshot?.trim() || '-'
  const showSnapshot = Boolean(primary?.trim()) && primaryValue !== snapshotValue

  return (
    <div data-testid={testId}>
      <div title={primaryValue} style={SINGLE_LINE_TEXT_STYLE}>
        {primaryValue}
      </div>
      {showSnapshot ? (
        <div title={`运行快照 ${snapshotValue}`} style={{ ...SINGLE_LINE_TEXT_STYLE, color: 'var(--text-muted)', fontSize: 12 }}>
          运行快照 {snapshotValue}
        </div>
      ) : null}
    </div>
  )
}

function formatProtocolList(protocols: string[] | null | undefined) {
  if (!Array.isArray(protocols) || protocols.length === 0) {
    return null
  }
  const values = protocols
    .map(item => String(item || '').trim())
    .filter(Boolean)
  if (values.length === 0) {
    return null
  }
  return values.map(item => getProtocolLabel(item)).join(' / ')
}

function formatRunSnapshotProtocol(protocol: string | null | undefined) {
  const value = protocol?.trim()
  return value || '-'
}

function renderRunProtocolCell(
  currentProtocols: string[] | null | undefined,
  snapshotProtocol: string | null | undefined,
  testId: string,
) {
  const primaryValue = formatProtocolList(currentProtocols) || formatRunSnapshotProtocol(snapshotProtocol)
  const snapshotValue = formatRunSnapshotProtocol(snapshotProtocol)
  const showSnapshot = Boolean(formatProtocolList(currentProtocols)) && primaryValue !== snapshotValue

  return (
    <div data-testid={testId}>
      <div title={primaryValue} style={SINGLE_LINE_TEXT_STYLE}>
        {primaryValue}
      </div>
      {showSnapshot ? (
        <div title={`运行快照 ${snapshotValue}`} style={{ ...SINGLE_LINE_TEXT_STYLE, color: 'var(--text-muted)', fontSize: 12 }}>
          运行快照 {snapshotValue}
        </div>
      ) : null}
    </div>
  )
}

function formatRunWindowDateTime(value?: string | null) {
  if (!value) return '-'
  return formatLocalDateTime(value)
}

function formatCompactNumber(value: number | null | undefined, fractionDigits = 0) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return '-'
  }
  return value.toLocaleString('zh-CN', {
    maximumFractionDigits: fractionDigits,
    minimumFractionDigits: 0,
  })
}

function formatPercentValue(value: number | null | undefined) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return '-'
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)}%`
}

function normalizePercentValue(value: number | null | undefined) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return null
  }
  const percentValue = value <= 1 ? value * 100 : value
  return Math.max(0, Math.min(100, percentValue))
}

function getAverageMetric(values: Array<number | null>) {
  const validValues = values.filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
  if (validValues.length === 0) {
    return { value: null, sampleCount: 0 }
  }
  return {
    value: validValues.reduce((sum, value) => sum + value, 0) / validValues.length,
    sampleCount: validValues.length,
  }
}

function resolveRunSuccessPercent(run: Run) {
  return normalizePercentValue(run.overview_summary?.checks_success_rate ?? run.success_rate)
}

function resolveRunP95(run: Run) {
  const value = run.p95_rt_ms ?? run.overview_summary?.p95_rt_ms
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
}

function getPercentShare(value: number, total: number) {
  if (total <= 0) return 0
  return Math.max(0, Math.min(100, (value / total) * 100))
}

function formatTaskPatternLabel(taskPattern: string | null | undefined) {
  const normalized = String(taskPattern || '').trim().toLowerCase()
  return TASK_PATTERN_LABELS[normalized] || (normalized ? normalized : '-')
}

const getJoinedFilterValue = (value: unknown): string | undefined => {
  if (!Array.isArray(value)) {
    return undefined
  }
  const normalized = value
    .filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
    .map((item) => item.trim())
  return normalized.length > 0 ? normalized.join(',') : undefined
}

const splitFilterValue = (value: unknown) => {
  if (typeof value !== 'string' || !value.trim()) {
    return null
  }
  const normalized = value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
  return normalized.length > 0 ? normalized : null
}

function sanitizeRunListFilters(values: Record<string, unknown>): Partial<RunQuery> {
  const sanitizeId = (value: unknown): number | undefined => {
    if (typeof value === 'number' && Number.isFinite(value)) {
      return value
    }
    if (typeof value !== 'string') {
      return undefined
    }
    const trimmed = value.trim()
    if (!trimmed) {
      return undefined
    }
    const parsed = Number(trimmed)
    return Number.isFinite(parsed) ? parsed : undefined
  }

  const sanitizeText = (value: unknown): string | undefined => {
    if (typeof value !== 'string') {
      return undefined
    }
    const trimmed = value.trim()
    return trimmed || undefined
  }

  const sanitized: Partial<RunQuery> = {}
  const taskId = sanitizeId(values.task_id)
  const runId = sanitizeId(values.run_id)
  const runStatus = sanitizeText(values.run_status)
  const env = sanitizeText(values.env)
  const engineType = sanitizeText(values.engine_type)
  const protocol = sanitizeText(values.protocol)
  const businessLine = sanitizeText(values.business_line)
  const taskName = sanitizeText(values.task_name)
  const operatorName = sanitizeText(values.operator_name)
  const from = sanitizeText(values.from)
  const to = sanitizeText(values.to)

  if (taskId !== undefined) {
    sanitized.task_id = taskId
  }
  if (runId !== undefined) {
    sanitized.run_id = runId
  }
  if (runStatus && RUN_STATUS_VALUES.includes(runStatus as RunStatus)) {
    sanitized.run_status = runStatus as RunStatus
  }
  if (env) {
    sanitized.env = env
  }
  if (engineType) {
    sanitized.engine_type = engineType as RunQuery['engine_type']
  }
  if (protocol) {
    sanitized.protocol = protocol
  }
  if (businessLine) {
    sanitized.business_line = businessLine
  }
  if (taskName) {
    sanitized.task_name = taskName
  }
  if (operatorName) {
    sanitized.operator_name = operatorName
  }
  if (from) {
    sanitized.from = from
  }
  if (to) {
    sanitized.to = to
  }

  return sanitized
}

const RunList = () => {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const queryClient = useQueryClient()

  const initialFilters = useMemo<Record<string, unknown>>(() => {
    const taskIdParam = searchParams.get('task_id')
    if (!taskIdParam) return {}

    const taskId = Number(taskIdParam)
    if (!Number.isFinite(taskId)) return {}

    return { task_id: taskId }
  }, [searchParams])

  const [filterValues, setFilterValues] = useState<Record<string, unknown>>(initialFilters)
  const [pagination, setPagination] = useState({ current: 1, pageSize: DEFAULT_RUN_LIST_PAGE_SIZE })
  const [downloadingRunId, setDownloadingRunId] = useState<number | null>(null)
  const [viewingRunId, setViewingRunId] = useState<number | null>(null)
  const [selectedRunIds, setSelectedRunIds] = useState<number[]>([])
  const [selectedRuns, setSelectedRuns] = useState<Run[]>([])
  const [showAdvancedFilters, setShowAdvancedFilters] = useState(false)
  const [businessOptions, setBusinessOptions] = useState<MetaItem[]>([])
  const [envOptions, setEnvOptions] = useState<MetaItem[]>([])
  const searchFilterKey = JSON.stringify(initialFilters)

  useEffect(() => {
    setFilterValues(initialFilters)
    setPagination(prev => ({ ...prev, current: 1 }))
    setSelectedRunIds([])
    setSelectedRuns([])
  }, [initialFilters])

  useEffect(() => {
    return scheduleIdleTask(() => {
      metaApi.getBusinessLines().then(setBusinessOptions).catch(() => setBusinessOptions([]))
      metaApi.getEnvironments().then(setEnvOptions).catch(() => setEnvOptions([]))
    })
  }, [])

  const queryParams: RunQuery = useMemo(() => {
    return {
      page: pagination.current,
      pageSize: pagination.pageSize,
      ...(filterValues as unknown as Partial<RunQuery>),
    }
  }, [filterValues, pagination])

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ['runs', queryParams],
    queryFn: () => runApi.getRunList(queryParams),
    placeholderData: previousData => previousData,
  })

  const stopMutation = useMutation({
    mutationFn: (runId: number) => runApi.stopRun(runId, { reason: 'manual stop' }),
    onSuccess: () => {
      message.success('已停止')
      queryClient.invalidateQueries({ queryKey: ['runs'] })
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '停止失败')
    },
  })

  const selectedTaskIds = useMemo(
    () => Array.from(new Set(selectedRuns.map(run => run.task_id))),
    [selectedRuns],
  )

  const openCompareModal = () => {
    if (selectedRuns.length !== 2) {
      message.warning('请选择两条结果记录进行对比')
      return
    }
    if (selectedTaskIds.length !== 1) {
      message.warning('结果对比当前仅支持同一任务下的两条记录')
      return
    }
    navigate(`/runs/compare?base_id=${selectedRuns[0].run_id}&comparator_id=${selectedRuns[1].run_id}`)
  }

  const resetSelection = () => {
    setSelectedRunIds([])
    setSelectedRuns([])
  }

  const environmentSelectOptions = useMemo(
    () => envOptions.map((item) => ({ label: item.code, value: item.code })),
    [envOptions],
  )

  const primaryFilterFields: FilterField[] = [
    { name: 'task_id', label: '任务ID', type: 'input', placeholder: '任务ID' },
    { name: 'task_name', label: '任务名称', type: 'input', placeholder: '任务名称' },
    { name: 'run_id', label: 'Run ID', type: 'input', placeholder: 'Run ID' },
    {
      name: 'run_status',
      label: '状态',
      type: 'select',
      options: [
        { label: '全部', value: '' },
        { label: RUN_STATUS_LABELS.preparing, value: 'preparing' satisfies RunStatus },
        { label: RUN_STATUS_LABELS.running, value: 'running' satisfies RunStatus },
        { label: RUN_STATUS_LABELS.succeeded, value: 'succeeded' satisfies RunStatus },
        { label: RUN_STATUS_LABELS.failed, value: 'failed' satisfies RunStatus },
        { label: RUN_STATUS_LABELS.stopped, value: 'stopped' satisfies RunStatus },
      ],
    },
    {
      name: 'env',
      label: '压测环境',
      type: 'select',
      placeholder: '当前任务环境',
      options: environmentSelectOptions,
    },
    { name: 'operator_name', label: '操作人', type: 'input', placeholder: '操作人' },
  ]

  const advancedFilterFields: FilterField[] = [
    { name: 'createTime', label: '开始时间', type: 'dateRange' },
  ]

  const businessLineFilters = useMemo(
    () => businessOptions.map((item) => ({ text: item.name || item.code, value: item.code })),
    [businessOptions],
  )

  const environmentFilters = useMemo(
    () => envOptions.map((item) => ({ text: item.code, value: item.code })),
    [envOptions],
  )

  const engineFilters = useMemo(
    () => [
      { text: 'JMeter', value: 'jmeter' },
      { text: 'K6', value: 'k6' },
      { text: 'Custom', value: 'custom' },
    ],
    [],
  )

  const protocolFilters = useMemo(
    () =>
      Object.keys(PROTOCOL_LABELS).map((value) => ({
        text: getProtocolLabel(value),
        value,
      })),
    [],
  )

  const taskPatternFilters = useMemo(
    () => [
      { text: '脚本', value: 'script' },
    ],
    [],
  )

  const filterFields: FilterField[] = showAdvancedFilters
    ? [...primaryFilterFields, ...advancedFilterFields]
    : primaryFilterFields

  const activeFilterCount = Object.values(filterValues).filter(value => value !== undefined && value !== null && value !== '').length

  const selectedSummaryLabel = useMemo(() => {
    if (selectedRuns.length === 0) {
      return '未选择结果记录'
    }
    if (selectedRuns.length === 1) {
      return `已选择 1 条：Run #${selectedRuns[0].run_id}`
    }
    return `已选择 ${selectedRuns.length} 条，可发起结果对比`
  }, [selectedRuns])

  const currentItems = data?.items ?? []
  const runningCount = currentItems.filter(item => item.run_status === 'running').length
  const failedCount = currentItems.filter(item => item.run_status === 'failed').length
  const completedCount = currentItems.filter(item => item.run_status === 'succeeded').length
  const stoppedCount = currentItems.filter(item => item.run_status === 'stopped').length
  const preparingCount = currentItems.filter(item => item.run_status === 'preparing').length
  const totalCount = data?.total ?? currentItems.length
  const currentPageCount = currentItems.length
  const totalRequests = currentItems.reduce((sum, item) => sum + (item.total_requests ?? item.overview_summary?.total_requests ?? 0), 0)
  const successRateSummary = getAverageMetric(currentItems.map(resolveRunSuccessPercent))
  const p95Summary = getAverageMetric(currentItems.map(resolveRunP95))
  const focusRun = currentItems[0]
  const statusHealthLabel = failedCount > 0 ? 'ATTENTION' : runningCount > 0 ? 'LIVE LOAD' : 'STABLE'

  const statusSegments = useMemo(
    () => [
      { key: 'running', label: '运行中', value: runningCount, color: 'var(--status-running-color)' },
      { key: 'failed', label: '失败', value: failedCount, color: 'var(--error-color)' },
      { key: 'succeeded', label: '成功', value: completedCount, color: 'var(--status-success-color)' },
      { key: 'stopped', label: '停止', value: stoppedCount, color: 'var(--status-stopped-color)' },
      { key: 'preparing', label: '准备', value: preparingCount, color: 'var(--status-preparing-color)' },
    ],
    [completedCount, failedCount, preparingCount, runningCount, stoppedCount],
  )

  const runListSummaryCards = useMemo(() => [
    {
      key: 'total',
      label: '结果库存',
      value: formatCompactNumber(totalCount),
      unit: 'runs',
      helper: activeFilterCount > 0 ? `${activeFilterCount} 个筛选生效` : '默认运行视图',
      icon: <DashboardOutlined />,
      meterPercent: getPercentShare(currentPageCount, totalCount),
      tone: 'var(--primary-color)',
    },
    {
      key: 'running',
      label: '实时运行',
      value: runningCount,
      unit: 'active',
      helper: runningCount > 0 ? '可进入详情观察 pod / 指标' : '当前页无运行态',
      icon: <SyncOutlined spin={runningCount > 0} />,
      meterPercent: getPercentShare(runningCount, currentPageCount),
      tone: 'var(--collecting-color)',
    },
    {
      key: 'successRate',
      label: '成功率样本均值',
      value: formatPercentValue(successRateSummary.value),
      unit: 'avg',
      helper: successRateSummary.sampleCount > 0
        ? `${successRateSummary.sampleCount}/${currentPageCount} 条有成功率样本`
        : '当前页暂无成功率样本',
      icon: <CheckCircleOutlined />,
      meterPercent: successRateSummary.value ?? 0,
      tone: 'var(--success-color)',
    },
    {
      key: 'p95',
      label: 'P95 样本均值',
      value: formatCompactNumber(p95Summary.value, 1),
      unit: 'ms',
      helper: p95Summary.sampleCount > 0
        ? `${p95Summary.sampleCount}/${currentPageCount} 条有延迟样本`
        : failedCount > 0 ? `${failedCount} 条失败需复核` : '当前页未发现失败态',
      icon: failedCount > 0 ? <WarningOutlined /> : <ThunderboltOutlined />,
      meterPercent: getPercentShare(p95Summary.sampleCount, currentPageCount),
      tone: failedCount > 0 ? 'var(--error-color)' : 'var(--warning-color)',
    },
  ], [activeFilterCount, currentPageCount, failedCount, p95Summary.sampleCount, p95Summary.value, runningCount, successRateSummary.sampleCount, successRateSummary.value, totalCount])

  const columns = useMemo(() => [
    {
      title: 'Run ID',
      dataIndex: 'run_id',
      key: 'run_id',
      width: 100,
      fixed: 'left' as const,
      className: 'olh-fixed-key-column',
      render: (_: unknown, record: Run) => (
        <span className="olh-run-record-id">{`#${record.run_id}`}</span>
      ),
    },
    {
      title: '任务',
      dataIndex: 'task_name',
      key: 'task_name',
      width: 280,
      ellipsis: true,
      render: (_: unknown, record: Run) => (
        <div className="olh-run-task-cell">
          <div
            data-testid={`run-task-name-${record.run_id}`}
            title={record.task_name || '-'}
            style={{ ...SINGLE_LINE_TEXT_STYLE, fontWeight: 500 }}
          >
            {record.task_name || '-'}
          </div>
          <div
            data-testid={`run-task-meta-${record.run_id}`}
            title={`ID ${record.task_id}${record.business_line ? ` · ${record.business_line}` : ''}`}
            style={{ ...SINGLE_LINE_TEXT_STYLE, color: 'var(--text-muted)', fontSize: 12 }}
          >
            ID {record.task_id}{record.business_line ? ` · ${record.business_line}` : ''}
          </div>
        </div>
      ),
    },
    {
      title: '状态',
      dataIndex: 'run_status',
      key: 'run_status',
      width: 100,
      render: (_: Run['run_status'], record: Run) => (
        <div className={`olh-run-status-cell olh-run-status-cell--${record.run_status}`}>
          <StatusBadge status={record.run_status} text={record.run_status_label || undefined} />
        </div>
      ),
    },
    {
      title: '完成进度',
      dataIndex: 'pod_total',
      key: 'agent_progress',
      width: 176,
      sorter: (a: Run, b: Run) => (a.pod_completed ?? 0) - (b.pod_completed ?? 0),
      render: (_: unknown, record: Run) => {
        const total = record.pod_total ?? record.pod_actual
        const active = record.pod_actual ?? 0
        const completed = record.pod_completed ?? 0
        const text = total != null ? `${completed} completed / ${total} total` : '-'
        return (
          <div className="olh-run-progress-cell">
            {renderSingleLineText(text, `run-agent-progress-${record.run_id}`)}
            {total != null ? <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>{`执行节点 / Agent ${active}`}</div> : null}
          </div>
        )
      },
    },
    {
      title: '引擎类型',
      dataIndex: 'engine_type',
      key: 'engine_type',
      width: 90,
      filters: engineFilters,
      filterMultiple: true,
      filteredValue: splitFilterValue(filterValues.engine_type),
      render: (_: unknown, record: Run) => record.engine_type_label || record.engine_type,
    },
    {
      title: '任务模式',
      dataIndex: 'task_id',
      key: 'task_pattern',
      width: 92,
      filters: taskPatternFilters,
      filterMultiple: true,
      filteredValue: splitFilterValue(filterValues.task_pattern),
      render: (_: unknown, record: Run) => formatTaskPatternLabel(record.current_task_pattern),
    },
    {
      title: '协议类型',
      dataIndex: 'protocol',
      key: 'protocol',
      width: 164,
      filters: protocolFilters,
      filterMultiple: true,
      filteredValue: splitFilterValue(filterValues.protocol),
      render: (_value: string | null | undefined, record: Run) =>
        renderRunProtocolCell(record.current_task_protocols, record.protocol, `run-protocol-${record.run_id}`),
    },
    {
      title: '业务线',
      dataIndex: 'business_line',
      key: 'business_line',
      width: 180,
      filters: businessLineFilters,
      filterMultiple: true,
      filteredValue: splitFilterValue(filterValues.business_line),
      render: (_value: string | null | undefined, record: Run) =>
        renderRunMetadataCell(record.current_task_business_line, record.business_line, `run-business-line-${record.run_id}`),
    },
    {
      title: '压测环境',
      dataIndex: 'env',
      key: 'env',
      width: 180,
      filters: environmentFilters,
      filterMultiple: true,
      filteredValue: splitFilterValue(filterValues.env),
      render: (_value: string | null | undefined, record: Run) =>
        renderRunMetadataCell(record.current_task_env, record.env, `run-env-${record.run_id}`),
    },
    {
      title: '压测时间段',
      dataIndex: 'started_at',
      key: 'run_window',
      width: 210,
      render: (_: unknown, record: Run) => (
        <div>
          <div data-testid={`run-window-start-${record.run_id}`}>{formatRunWindowDateTime(record.started_at)}</div>
          <div data-testid={`run-window-end-${record.run_id}`} style={{ color: 'var(--text-muted)', fontSize: 12 }}>
            {formatRunWindowDateTime(record.ended_at)}
          </div>
        </div>
      ),
    },
    {
      title: '操作人',
      dataIndex: 'operator_name',
      key: 'operator_name',
      width: 160,
      ellipsis: true,
      render: (v: string | null | undefined, record: Run) =>
        renderSingleLineText(getCanonicalUserLabel(v), `run-operator-${record.run_id}`),
    },
    {
      title: '操作',
      dataIndex: 'run_id',
      key: 'actions',
      width: 148,
      fixed: 'right' as const,
      render: (_: unknown, record: Run) => (
        <div className="olh-run-row-actions">
          <Tooltip title="查看详情">
            <Button
              type="primary"
              size="small"
              className="olh-run-row-action olh-run-row-action--primary"
              icon={<EyeOutlined />}
              onClick={() => navigate(`/runs/${record.run_id}`)}
            >
              查看
            </Button>
          </Tooltip>
          <div className="olh-run-row-action-secondary">
            <Tooltip title="打开报告">
              <Button
                type="text"
                size="small"
                aria-label="打开报告"
                className="olh-run-row-action"
                icon={<DashboardOutlined />}
                disabled={viewingRunId === record.run_id}
                onClick={async () => {
                  setViewingRunId(record.run_id)
                  const hide = message.loading('正在准备报告...', 0)
                  try {
                    const frontdoor = await reportApi.ensureRunReportFrontdoor(record.run_id)
                    if (frontdoor.status !== 'ready' || !frontdoor.report_id) {
                      message.warning(frontdoor.message)
                      return
                    }
                    navigate(`/reports/${frontdoor.report_id}/view`)
                  } catch (error) {
                    message.error(error instanceof Error ? error.message : '打开报告失败')
                  } finally {
                    hide()
                    setViewingRunId(null)
                  }
                }}
              />
            </Tooltip>
            <Tooltip title="下载报告">
              <Button
                type="text"
                size="small"
                aria-label="下载报告"
                className="olh-run-row-action"
                icon={<DownloadOutlined />}
                disabled={downloadingRunId === record.run_id}
                onClick={async () => {
                  setDownloadingRunId(record.run_id)
                  const hide = message.loading('正在下载报告...', 0)
                  try {
                    const frontdoor = await reportApi.getRunReportFrontdoor(record.run_id)
                    if (frontdoor.status !== 'ready' || !frontdoor.report_id) {
                      message.warning(frontdoor.message)
                      return
                    }

                    const blob = await reportApi.downloadReport(frontdoor.report_id)
                    const url = window.URL.createObjectURL(blob)
                    const link = document.createElement('a')
                    link.style.display = 'none'
                    link.href = url
                    link.download = `report_${frontdoor.report_id}.html`
                    document.body.appendChild(link)
                    link.click()
                    window.URL.revokeObjectURL(url)
                    document.body.removeChild(link)
                    message.success(`报告 #${frontdoor.report_id} 下载成功`)
                  } catch (error) {
                    message.error(error instanceof Error ? error.message : '下载报告失败')
                  } finally {
                    hide()
                    setDownloadingRunId(null)
                  }
                }}
              />
            </Tooltip>
            {record.run_status === 'running' ? (
              <Tooltip title="停止运行">
                <Button
                  type="text"
                  danger
                  size="small"
                  aria-label="停止运行"
                  className="olh-run-row-action olh-run-row-action--stop"
                  icon={<StopOutlined />}
                  disabled={stopMutation.isPending}
                  onClick={() => stopMutation.mutate(record.run_id)}
                />
              </Tooltip>
            ) : null}
          </div>
        </div>
      ),
    },
  ], [
    businessLineFilters,
    downloadingRunId,
    engineFilters,
    environmentFilters,
    filterValues.business_line,
    filterValues.engine_type,
    filterValues.env,
    filterValues.protocol,
    filterValues.task_pattern,
    navigate,
    protocolFilters,
    stopMutation,
    taskPatternFilters,
    viewingRunId,
  ])

  return (
    <div data-testid="run-list" className="olh-page-shell olh-run-list-page olh-console-page">
      <div className="olh-console-hero olh-run-list-hero">
        <div className="olh-console-hero-main">
          <div className="olh-page-breadcrumb">OpenLoadHub / Run Operations</div>
          <div className="olh-console-title-row">
            <h1 className="olh-page-title" data-testid="run-list-title">结果列表</h1>
            <span className={runningCount > 0 ? 'olh-live-pill olh-live-pill--active' : 'olh-live-pill'}>
              {runningCount > 0 ? 'LIVE' : 'READY'}
            </span>
          </div>
          <div className="olh-page-subtitle">
            汇总压测运行、报告入口和环境快照；优先呈现运行态、失败态与可对比记录。
          </div>
          <div className="olh-console-command-strip">
            <span><FieldTimeOutlined /> {`可见 ${currentPageCount} / 总计 ${formatCompactNumber(totalCount)}`}</span>
            <span><FilterOutlined /> {activeFilterCount > 0 ? `${activeFilterCount} 个筛选` : '默认筛选'}</span>
            <span><ControlOutlined /> {statusHealthLabel}</span>
            <span><ThunderboltOutlined /> {`请求 ${formatCompactNumber(totalRequests)}`}</span>
          </div>
        </div>
        <div className="olh-console-hero-side">
          <div className="olh-console-focus-panel">
            <div className="olh-console-focus-label">Focus run</div>
            <div className="olh-console-focus-value">
              {focusRun ? `#${focusRun.run_id}` : '-'}
            </div>
            <div className="olh-console-focus-copy" title={focusRun?.task_name || undefined}>
              {focusRun?.task_name || '等待运行数据'}
            </div>
            <div className="olh-console-focus-meta">
              <span>{focusRun ? RUN_STATUS_LABELS[focusRun.run_status] : '无记录'}</span>
              <span>{focusRun?.env || '-'}</span>
              <span>{focusRun?.engine_type_label || focusRun?.engine_type || '-'}</span>
            </div>
          </div>
          <Space wrap className="olh-console-actions">
            <Button onClick={() => navigate('/my-focus/focus-task')}>关注任务</Button>
          </Space>
        </div>
      </div>

      <div className="olh-kpi-grid olh-run-list-kpis">
        {runListSummaryCards.map(card => (
          <div
            key={card.key}
            className={`olh-kpi-card olh-kpi-card--${card.key}`}
            style={{ '--kpi-color': card.tone } as CSSProperties}
          >
            <div className="olh-kpi-head">
              <div className="olh-kpi-label">{card.label}</div>
              <div className="olh-kpi-icon">{card.icon}</div>
            </div>
            <div className="olh-kpi-value">
              <span>{card.value}</span>
              <span className="olh-kpi-unit">{card.unit}</span>
            </div>
            <div className="olh-kpi-helper">{card.helper}</div>
            <div className="olh-kpi-meter" aria-hidden="true">
              <span style={{ width: `${card.meterPercent}%` }} />
            </div>
          </div>
        ))}
      </div>

      <Card className="olh-filter-panel olh-console-filter-panel olh-run-filter-panel" style={{ marginBottom: 16 }} bodyStyle={{ padding: 16 }}>
        <div className="olh-filter-panel-header">
          <div>
            <div className="olh-filter-panel-title">结果定位</div>
            <div className="olh-filter-panel-copy">优先按任务、Run ID、状态和执行人定位；协议、环境和业务线保留在表格列头。</div>
            <div className="olh-filter-chip-row">
              <span className="olh-data-chip">任务口径</span>
              <span className="olh-data-chip">run 快照</span>
              <span className="olh-data-chip">列头筛选</span>
            </div>
          </div>
          <Button type="link" onClick={() => setShowAdvancedFilters(value => !value)}>
            {showAdvancedFilters ? '收起筛选' : '更多筛选'}
          </Button>
        </div>
        <div className="olh-run-filter-console">
          <SearchFilter
            key={searchFilterKey}
            fields={filterFields}
            initialValues={initialFilters}
            onSearch={values => {
              setFilterValues(sanitizeRunListFilters(values))
              setPagination(prev => ({ ...prev, current: 1 }))
              resetSelection()
            }}
            onReset={() => {
              setFilterValues(sanitizeRunListFilters(initialFilters))
              setPagination(prev => ({ ...prev, current: 1 }))
              resetSelection()
            }}
            loading={isFetching}
          />
        </div>
      </Card>

      <div className="olh-table-shell">
        <div className="olh-data-section-heading">
          <div>
            <div className="olh-section-title">压测结果记录</div>
            <div className="olh-section-subtitle">
              当前页按任务、协议、环境和运行状态聚合展示，行内保留 run 快照差异提示。
            </div>
          </div>
          <Space wrap size={8}>
            <span className="olh-data-chip">{`总计 ${data?.total ?? 0}`}</span>
            <span className="olh-data-chip">{`已选 ${selectedRuns.length}`}</span>
            <span className="olh-data-chip">{activeFilterCount > 0 ? `筛选 ${activeFilterCount}` : '默认视图'}</span>
          </Space>
        </div>
        <div className="olh-run-status-strip">
          <div className="olh-status-rail olh-status-rail--compact">
            {statusSegments.map(segment => (
              <div
                key={segment.key}
                className={`olh-status-segment olh-status-segment--${segment.key}`}
                style={{ '--segment-color': segment.color } as CSSProperties}
              >
                <span>{segment.label}</span>
                <strong>{segment.value}</strong>
              </div>
            ))}
          </div>
          <div className="olh-status-loadbar olh-status-loadbar--inline" aria-hidden="true">
            {statusSegments.map(segment => (
              <span
                key={segment.key}
                style={{
                  '--segment-color': segment.color,
                  width: `${getPercentShare(segment.value, currentPageCount)}%`,
                } as CSSProperties}
              />
            ))}
          </div>
          <div className="olh-selection-inline">
            <span>对比</span>
            <strong>{selectedSummaryLabel}</strong>
            <Button
              size="small"
              type="primary"
              ghost
              onClick={openCompareModal}
              disabled={selectedRuns.length !== 2}
            >
              结果对比
            </Button>
          </div>
        </div>
        <div className="olh-run-table-frame">
          <DataTable<Run>
            columns={columns}
            dataSource={data?.items || []}
            loading={isLoading || isFetching}
            rowKey="run_id"
            size="small"
            className="olh-run-table"
            tableLayout="fixed"
            scroll={{ x: 1568 }}
            rowClassName={(record) => `olh-run-table-row olh-run-table-row--${record.run_status}`}
            rowSelection={{
              selectedRowKeys: selectedRunIds,
              fixed: true,
              onChange: (keys, rows) => {
                setSelectedRunIds(keys.map(key => Number(key)))
                setSelectedRuns(rows as Run[])
              },
            }}
            pagination={{
              current: pagination.current,
              pageSize: pagination.pageSize,
              total: data?.total || 0,
              showSizeChanger: true,
              showTotal: total => `共 ${total} 条`,
            }}
            onChange={(paginationInfo, filters) => {
              const nextBusinessLine = getJoinedFilterValue(filters.business_line)
              const nextEnv = getJoinedFilterValue(filters.env)
              const nextEngineType = getJoinedFilterValue(filters.engine_type)
              const nextTaskPattern = getJoinedFilterValue(filters.task_pattern)
              const nextProtocol = getJoinedFilterValue(filters.protocol)
              const currentBusinessLine = typeof filterValues.business_line === 'string' ? filterValues.business_line : undefined
              const currentEnv = typeof filterValues.env === 'string' ? filterValues.env : undefined
              const currentEngineType = typeof filterValues.engine_type === 'string' ? filterValues.engine_type : undefined
              const currentTaskPattern = typeof filterValues.task_pattern === 'string' ? filterValues.task_pattern : undefined
              const currentProtocol = typeof filterValues.protocol === 'string' ? filterValues.protocol : undefined
              const filtersChanged =
                nextBusinessLine !== currentBusinessLine
                || nextEnv !== currentEnv
                || nextEngineType !== currentEngineType
                || nextTaskPattern !== currentTaskPattern
                || nextProtocol !== currentProtocol

              if (filtersChanged) {
                setFilterValues((current) => ({
                  ...current,
                  business_line: nextBusinessLine,
                  env: nextEnv,
                  engine_type: nextEngineType,
                  task_pattern: nextTaskPattern,
                  protocol: nextProtocol,
                }))
                setPagination((prev) => ({
                  current: 1,
                  pageSize: paginationInfo.pageSize || prev.pageSize,
                }))
                resetSelection()
                return
              }

              if (paginationInfo.current !== pagination.current || paginationInfo.pageSize !== pagination.pageSize) {
                setPagination({
                  current: paginationInfo.current || 1,
                  pageSize: paginationInfo.pageSize || 20,
                })
                resetSelection()
              }
            }}
          />
        </div>
      </div>
    </div>
  )
}

export default RunList
