import { useMemo, useState, useEffect, type CSSProperties, type Key } from 'react'
import {
  Card,
  Row,
  Col,
  Statistic,
  Tabs,
  Form,
  Input,
  Select,
  Button,
  Dropdown,
  Popconfirm,
  Table,
  Space,
  message,
  DatePicker,
  Alert,
  type TableProps,
} from 'antd'
import {
  SearchOutlined,
  PlayCircleOutlined,
  StopOutlined,
  CopyOutlined,
  EditOutlined,
  FileTextOutlined,
  MoreOutlined,
  BarsOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { taskApi, type Task } from '@/services/taskApi'
import { agentApi } from '@/services/agentApi'
import { runApi } from '@/services/runApi'
import { metaApi, type MetaItem } from '@/services/metaApi'
import { useAuthStore } from '@/store/authStore'
import type { PaginationResponse } from '@/types'
import { resolveUserId } from '@/utils/auth'
import { formatLocalDateTime } from '@/utils/localDateTime'
import { canEnterStartFlow, canPrepareTaskForRun, canStartTask } from '@/utils/taskStartGuard'
import StartRunDrawer from '@/components/task/StartRunDrawer'
import TaskScriptEditorDrawer from '@/components/task/TaskScriptEditorDrawer'
import { StatusBadge } from '@/components/common'
import { publicAlphaFeatures } from '@/config/publicAlpha'
import { getDemoScriptPreview } from '@/constants/demoScriptRegistry'

const { Option } = Select
const { RangePicker } = DatePicker
type OwnerFilterScope = 'mine' | 'all' | 'specific'

const OWNER_SCOPE_OPTIONS = [
  { label: '我的参与任务', value: 'mine' satisfies OwnerFilterScope },
  { label: '全部任务', value: 'all' satisfies OwnerFilterScope },
  { label: '指定成员', value: 'specific' satisfies OwnerFilterScope },
]
const DEFAULT_FILTER_VALUES = { owner_scope: 'mine' satisfies OwnerFilterScope }
const WATCHLIST_TASKS_POLL_MS = 5_000
const WATCHLIST_SUMMARY_POLL_MS = 3_000
const ENGINE_LABELS: Record<Task['engine_type'], string> = {
  jmeter: 'JMeter',
  k6: 'K6',
  custom: 'Custom',
}
const PROTOCOL_LABELS: Record<string, string> = {
  http: 'HTTP',
  grpc: 'GRPC',
  kafka: 'Kafka',
  websocket: 'WebSocket',
  browser: 'Browser',
  other: 'Other',
}
const SINGLE_LINE_TEXT_STYLE = {
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
} as const
const FULL_DATETIME_TEXT_STYLE = {
  whiteSpace: 'nowrap',
  fontVariantNumeric: 'tabular-nums',
} as const
const TOP_CARD_STYLE: CSSProperties = {
  width: '100%',
  height: '100%',
  minHeight: 104,
}
const TOP_CARD_BODY_STYLE: CSSProperties = {
  height: '100%',
  padding: '14px 22px',
  display: 'flex',
  flexDirection: 'column',
  justifyContent: 'flex-start',
}
const TOP_CARD_HEADER_STYLE: CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  gap: 16,
  marginBottom: 12,
}
const TOP_CARD_TITLE_STYLE: CSSProperties = {
  fontWeight: 600,
  fontSize: 16,
  lineHeight: '24px',
}
const TOP_CARD_METRICS_STYLE: CSSProperties = {
  display: 'grid',
  alignItems: 'start',
  columnGap: 20,
  rowGap: 12,
}
const SUMMARY_METRICS_STYLE: CSSProperties = {
  ...TOP_CARD_METRICS_STYLE,
  gridTemplateColumns: 'repeat(2, minmax(94px, 1fr))',
  columnGap: 18,
  rowGap: 6,
}
const RESOURCE_METRICS_STYLE: CSSProperties = {
  ...TOP_CARD_METRICS_STYLE,
  gridTemplateColumns: 'repeat(3, max-content)',
  columnGap: 48,
  justifyContent: 'start',
}
const FILTER_FORM_STYLE: CSSProperties = {
  marginBottom: 12,
  display: 'flex',
  flexWrap: 'wrap',
  columnGap: 18,
  rowGap: 10,
  alignItems: 'center',
}
const FILTER_ITEM_STYLE: CSSProperties = {
  marginBottom: 0,
  marginInlineEnd: 0,
}
const TASK_PATTERN_FILTER_OPTIONS: Array<{ text: string; value: Task['task_pattern'] }> = [
  { text: '脚本', value: 'script' },
]

const getProtocolLabel = (protocol: string) =>
  PROTOCOL_LABELS[String(protocol).toLowerCase()] || String(protocol).toUpperCase()
const DEMO_TEMPLATE_ITEMS = ['HTTP', 'GRPC', '混合标准']
type TaskWithLatestRunStatus = Task & {
  last_run_status?: 'preparing' | 'running' | 'succeeded' | 'failed' | 'stopped' | null
  last_run_status_label?: string | null
}
const resolveBusinessLine = (properties: Task['properties'], businessOptions: MetaItem[]) => {
  const bu =
    properties && typeof properties === 'object'
      ? (properties as Record<string, unknown>).business_line ?? (properties as Record<string, unknown>).bu_alias
      : null
  if (typeof bu !== 'string' || !bu) {
    return '-'
  }
  const matched = businessOptions.find(item => item.code === bu || item.name === bu)
  return matched ? matched.name : bu
}
const resolveCollaborators = (task: Task) => {
  if (Array.isArray(task.collaborator_names) && task.collaborator_names.length > 0) {
    return task.collaborator_names.join(', ')
  }
  if (Array.isArray(task.collaborator_ids) && task.collaborator_ids.length > 0) {
    return task.collaborator_ids.map(id => `user#${id}`).join(', ')
  }
  return '-'
}
const resolveTaskPattern = (taskPattern: Task['task_pattern']) =>
  taskPattern === 'visualization' ? '可视化' : '脚本'
const getJoinedFilterValue = (value: unknown): string | undefined => {
  if (!Array.isArray(value)) {
    return undefined
  }
  const normalized = value
    .filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
    .map(item => item.trim())
  return normalized.length > 0 ? normalized.join(',') : undefined
}
const splitFilterValue = (value: unknown) => {
  if (typeof value !== 'string' || !value.trim()) {
    return null
  }
  const normalized = value
    .split(',')
    .map(item => item.trim())
    .filter(Boolean)
  return normalized.length > 0 ? normalized : null
}
const normalizeEnvFilterForOptions = (value: unknown, options: MetaItem[]) => {
  const values = splitFilterValue(value)
  if (!values) {
    return undefined
  }
  const optionCodes = new Set(options.map(item => item.code))
  const scopedValues = values.filter(item => optionCodes.has(item))
  return scopedValues.length > 0 ? scopedValues.join(',') : undefined
}
const renderSingleLineCell = (text: string | number | null | undefined, testId?: string) => {
  const value = text === null || text === undefined || text === '' ? '-' : String(text)
  return (
    <div data-testid={testId} title={value} style={SINGLE_LINE_TEXT_STYLE}>
      {value}
    </div>
  )
}
const renderFullDateTimeCell = (text: string | null | undefined, testId?: string) => {
  const value = formatLocalDateTime(text)
  return (
    <span data-testid={testId} title={value} style={FULL_DATETIME_TEXT_STYLE}>
      {value}
    </span>
  )
}
const resolveRelatedApps = (task: Task) => {
  if (Array.isArray(task.related_apps) && task.related_apps.length > 0) {
    return task.related_apps.join(', ')
  }
  const properties = task.properties
  if (!properties || typeof properties !== 'object') {
    return '-'
  }
  const raw =
    (properties as Record<string, unknown>).related_apps ??
    (properties as Record<string, unknown>).relate_project_names ??
    (properties as Record<string, unknown>).project_name ??
    (properties as Record<string, unknown>).app_name
  if (Array.isArray(raw)) {
    const values = raw.filter(
      (item): item is string => typeof item === 'string' && item.trim().length > 0
    )
    return values.length > 0 ? values.join(', ') : '-'
  }
  if (typeof raw === 'string' && raw.trim()) {
    return raw
  }
  return '-'
}

type WatchlistTabKey = 'test_tasks' | 'testnet_tasks' | 'mainnet_tasks'
type EnvironmentScope = NonNullable<MetaItem['scope']>

const resolveWatchlistEnvScope = (item: MetaItem): EnvironmentScope => {
  return item.scope || 'test'
}

const isEnvInTab = (item: MetaItem, activeTab: WatchlistTabKey) => {
  const resolvedScope = resolveWatchlistEnvScope(item)
  if (activeTab === 'testnet_tasks') {
    return resolvedScope === 'testnet'
  }
  if (activeTab === 'mainnet_tasks') {
    return resolvedScope === 'main'
  }
  return resolvedScope === 'test'
}

const resolveScopeForTab = (activeTab: WatchlistTabKey): EnvironmentScope => {
  if (activeTab === 'testnet_tasks') {
    return 'testnet'
  }
  if (activeTab === 'mainnet_tasks') {
    return 'main'
  }
  return 'test'
}

const resolveEmptyTextForTab = (
  activeTab: WatchlistTabKey,
  shouldForceEmptyForActiveTab: boolean
) => {
  if (!shouldForceEmptyForActiveTab) {
    return '暂无任务'
  }
  const scopeLabels: Record<EnvironmentScope, string> = {
    test: 'demo',
    testnet: 'testnet',
    main: 'mainnet',
  }
  const scopeLabel = scopeLabels[resolveScopeForTab(activeTab)]
  if (activeTab === 'testnet_tasks') {
    return `当前没有可用的 ${scopeLabel} 环境，请联系管理员配置后再试。`
  }
  if (activeTab === 'mainnet_tasks') {
    return `当前没有可用的 ${scopeLabel} 环境，请联系管理员配置后再试。`
  }
  return `当前没有可用的 ${scopeLabel} 环境，请联系管理员配置后再试。`
}

const resolveEnvCodeForTab = (
  activeTab: WatchlistTabKey,
  envOptions: MetaItem[]
): string | undefined => {
  const exactMatches = envOptions
    .filter(item => isEnvInTab(item, activeTab))
    .map(item => item.code)
    .filter(Boolean)
  if (exactMatches.length > 0) {
    return exactMatches.join(',')
  }
  return undefined
}

const inferClusterScope = (clusterCode: string): EnvironmentScope => {
  const normalized = clusterCode.trim().toLowerCase()
  if (/(^|[^a-z])(main|mainnet|prod|production)([^a-z]|$)/.test(normalized)) {
    return 'main'
  }
  if (/(^|[^a-z])(testnet|staging)([^a-z]|$)/.test(normalized)) {
    return 'testnet'
  }
  return 'test'
}

const upsertCopiedTaskPage = (
  current: PaginationResponse<Task> | undefined,
  copied: Task,
  fallbackPageSize: number
): PaginationResponse<Task> => {
  const existingItems = current?.items || []
  const nextItems = [copied, ...existingItems.filter(item => item.id !== copied.id)]

  return {
    items: nextItems.slice(0, current?.pageSize || fallbackPageSize),
    total: current
      ? current.total + (existingItems.some(item => item.id === copied.id) ? 0 : 1)
      : 1,
    page: current?.page || 1,
    pageSize: current?.pageSize || fallbackPageSize,
  }
}

const Watchlist = () => {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [form] = Form.useForm()
  const [activeTab, setActiveTab] = useState<WatchlistTabKey>('test_tasks')
  const [envOptions, setEnvOptions] = useState<MetaItem[]>([])
  const [envOptionsLoaded, setEnvOptionsLoaded] = useState(false)
  const [businessOptions, setBusinessOptions] = useState<MetaItem[]>([])

  const envFromTab = useMemo(() => {
    return resolveEnvCodeForTab(activeTab, envOptions)
  }, [activeTab, envOptions])
  const scopedEnvOptions = useMemo(
    () => envOptions.filter(item => isEnvInTab(item, activeTab)),
    [activeTab, envOptions]
  )
  const shouldForceEmptyForActiveTab =
    envOptionsLoaded && envOptions.length > 0 && scopedEnvOptions.length === 0
  const activeTabEmptyText = resolveEmptyTextForTab(activeTab, shouldForceEmptyForActiveTab)

  // Filter State
  const [filterValues, setFilterValues] = useState<Record<string, unknown>>(DEFAULT_FILTER_VALUES)
  const [currentPage, setCurrentPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [selectedRowKeys, setSelectedRowKeys] = useState<Key[]>([])
  const [startingTask, setStartingTask] = useState<Task | null>(null)
  const [startFlowTaskId, setStartFlowTaskId] = useState<number | null>(null)
  const [editingScriptTask, setEditingScriptTask] = useState<Task | null>(null)
  const [demoPreviewKey, setDemoPreviewKey] = useState<string | null>(null)
  const { token, user, isAdmin } = useAuthStore()

  const currentUserId = useMemo(() => resolveUserId(token, user), [token, user])

  const queryParams = useMemo(() => {
    const raw = filterValues as {
      task_id?: number | string
      name?: string
      status?: Task['status']
      last_run_status?: 'preparing' | 'running' | 'succeeded' | 'failed' | 'stopped'
      engine_type?: Task['engine_type']
      protocol?: Task['protocols'][number]
      task_pattern?: Task['task_pattern']
      owner_scope?: OwnerFilterScope
      created_by?: number | string
      participant_id?: number | string
      from?: string
      to?: string
      env?: string
      business_line?: string
      created_by_name?: string
    }
    const parsedTaskId =
      raw.task_id === '' || raw.task_id === undefined ? undefined : Number(raw.task_id)
    const normalizedName = typeof raw.name === 'string' ? raw.name.trim() || undefined : raw.name
    const rawCreatedBy = typeof raw.created_by === 'string' ? raw.created_by.trim() : raw.created_by
    const parsedCreatedBy =
      rawCreatedBy === '' || rawCreatedBy === undefined ? undefined : Number(rawCreatedBy)
    const createdByNameValue =
      typeof raw.created_by_name === 'string'
        ? raw.created_by_name.trim() || undefined
        : typeof rawCreatedBy === 'string' && !Number.isFinite(parsedCreatedBy)
          ? rawCreatedBy
          : undefined
    const hasExplicitTaskLocator =
      Number.isFinite(parsedTaskId) || (typeof raw.name === 'string' && raw.name.trim().length > 0)
    const ownerScope = raw.owner_scope ?? 'mine'
    const participantIdValue = ownerScope === 'mine' && !hasExplicitTaskLocator ? currentUserId : undefined
    const createdByValue = ownerScope === 'specific' && Number.isFinite(parsedCreatedBy) ? parsedCreatedBy : undefined
    const normalizedEnv = normalizeEnvFilterForOptions(raw.env, scopedEnvOptions)
    const envSelected = normalizedEnv || (hasExplicitTaskLocator ? undefined : envFromTab)
    const usesLatestRunStatusFilter = raw.status === 'running'

    return {
      page: currentPage,
      pageSize,
      task_id: Number.isFinite(parsedTaskId) ? parsedTaskId : undefined,
      name: normalizedName,
      status: usesLatestRunStatusFilter ? undefined : raw.status,
      last_run_status: usesLatestRunStatusFilter ? ('running' as const) : undefined,
      engine_type: raw.engine_type,
      protocol: raw.protocol,
      task_pattern: raw.task_pattern,
      env: envSelected,
      business_line: raw.business_line || undefined,
      created_by: createdByValue,
      created_by_name: createdByNameValue,
      participant_id: participantIdValue,
      from: raw.from,
      to: raw.to,
    } satisfies Parameters<typeof taskApi.getTaskList>[0]
  }, [currentPage, currentUserId, envFromTab, filterValues, pageSize, scopedEnvOptions])

  // Task Query
  const { data: taskData, isLoading } = useQuery({
    queryKey: ['dashboard-tasks', queryParams],
    queryFn: () => taskApi.getTaskList(queryParams),
    enabled: envOptionsLoaded && !shouldForceEmptyForActiveTab,
    refetchInterval: WATCHLIST_TASKS_POLL_MS,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: 'always',
  })

  const summaryQueryParams = useMemo(() => ({}), [])

  const { data: summaryData, isLoading: summaryLoading } = useQuery({
    queryKey: ['dashboard-task-summary', summaryQueryParams],
    queryFn: () => taskApi.getTaskSummary(summaryQueryParams),
    refetchInterval: WATCHLIST_SUMMARY_POLL_MS,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: 'always',
  })
  const { data: agentsData, isLoading: agentsLoading } = useQuery({
    queryKey: ['agents'],
    queryFn: () => agentApi.getAgentList(),
    enabled: isAdmin,
    refetchInterval: WATCHLIST_SUMMARY_POLL_MS,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: 'always',
    retry: false,
  })

  const prepareTaskMutation = useMutation({
    mutationFn: (taskInfo: Task) => taskApi.prepareTaskForRun(taskInfo.id),
    onSuccess: (payload, taskInfo) => {
      queryClient.invalidateQueries({ queryKey: ['dashboard-tasks'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard-task-summary'] })
      queryClient.invalidateQueries({ queryKey: ['task', payload.task.id] })
      if (payload.task.status === 'script_missing') {
        message.warning(payload.status_hint || '任务缺少可用脚本，请先补齐脚本后再启动')
        return
      }
      if (payload.next_action === 'start_run' || payload.task.status === 'ready') {
        message.success(payload.status_hint || '任务已通过启动前校验，继续填写参数后即可启动压测')
        setStartingTask({
          ...taskInfo,
          ...payload.task,
          last_run_status: undefined,
          last_run_status_label: undefined,
        })
        return
      }
      message.warning(payload.status_hint || '当前任务状态暂不能启动压测')
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '启动前校验失败')
    },
    onSettled: () => {
      setStartFlowTaskId(null)
    },
  })

  const stopRunMutation = useMutation({
    mutationFn: async (taskId: number) => {
      const resp = await runApi.getRunList({
        page: 1,
        pageSize: 1,
        task_id: taskId,
        run_status: 'running',
      })
      const running = resp.items?.[0]
      if (!running) return null
      return runApi.stopRun(running.run_id, { reason: 'stopped_from_watchlist' })
    },
    onSuccess: stopped => {
      queryClient.invalidateQueries({ queryKey: ['runs'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard-runs-count'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard-tasks'] })
      if (!stopped) {
        message.warning('当前任务没有运行中的执行记录')
        return
      }
      message.success('已停止压测')
    },
  })

  const batchStopMutation = useMutation({
    mutationFn: (taskIds: number[]) =>
      taskApi.batchStopTasks(taskIds, 'stopped_from_watchlist_batch'),
    onSuccess: result => {
      if (result.stopped_run_ids.length > 0) {
        message.success(`已停止 ${result.stopped_run_ids.length} 个运行中的压测`)
      } else if (result.skipped_task_ids.length > 0) {
        message.warning('选中的任务里没有运行中的压测')
      } else {
        message.success('批量终止请求已提交')
      }
      queryClient.invalidateQueries({ queryKey: ['dashboard-tasks'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard-task-summary'] })
      queryClient.invalidateQueries({ queryKey: ['runs'] })
      setSelectedRowKeys([])
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '批量终止失败')
    },
  })

  const copyTaskMutation = useMutation({
    mutationFn: (taskId: number) => taskApi.copyTask(taskId),
    onSuccess: copied => {
      const rawTaskIdFilter = filterValues.task_id
      const rawNameFilter = typeof filterValues.name === 'string' ? filterValues.name.trim() : ''
      const shouldClearTaskId =
        rawTaskIdFilter !== undefined &&
        rawTaskIdFilter !== '' &&
        Number(rawTaskIdFilter) !== copied.id
      const shouldClearName =
        Boolean(rawNameFilter) && !copied.name.toLowerCase().includes(rawNameFilter.toLowerCase())
      const nextFilterValues = {
        ...filterValues,
        task_id: shouldClearTaskId ? undefined : filterValues.task_id,
        name: shouldClearName ? undefined : filterValues.name,
      }
      const nextQueryParams = {
        ...queryParams,
        page: 1,
        task_id: shouldClearTaskId ? undefined : queryParams.task_id,
        name: shouldClearName ? undefined : queryParams.name,
      }

      if (shouldClearTaskId || shouldClearName) {
        form.setFieldsValue({
          task_id: shouldClearTaskId ? undefined : form.getFieldValue('task_id'),
          name: shouldClearName ? undefined : form.getFieldValue('name'),
        })
        setFilterValues(nextFilterValues)
      }

      setCurrentPage(1)
      queryClient.setQueryData<PaginationResponse<Task>>(
        ['dashboard-tasks', nextQueryParams],
        current => upsertCopiedTaskPage(current, copied, pageSize)
      )
      queryClient.invalidateQueries({ queryKey: ['dashboard-tasks'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard-task-summary'] })
      message.success(`已复制任务，新增副本：${copied.name}`)
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '复制任务失败')
    },
  })

  const getTaskStatusTag = (
    status: Task['status'],
    latestRun?: TaskWithLatestRunStatus['last_run_status'],
    latestRunLabel?: string | null
  ) => {
    if (latestRun) {
      const latestMapping: Record<
        NonNullable<TaskWithLatestRunStatus['last_run_status']>,
        { text: string }
      > = {
        preparing: { text: '准备中' },
        running: { text: '运行中' },
        succeeded: { text: '成功' },
        failed: { text: '失败' },
        stopped: { text: '已停止' },
      }
      const latestValue = latestMapping[latestRun] ?? {
        text: latestRunLabel || latestRun,
      }
      return <StatusBadge status={latestRun} text={latestRunLabel || latestValue.text} />
    }
    const mapping: Record<Task['status'], { text: string }> = {
      draft: { text: '新建' },
      pending_submit_approval: { text: '待处理' },
      ready: { text: '当前可压测' },
      script_missing: { text: '未上传脚本' },
      awaiting_approval: { text: '待处理' },
      approval_rejected: { text: '待处理' },
      running: { text: '当前正在压测' },
    }
    const value = mapping[status] ?? { text: status }
    return <StatusBadge status={status} text={value.text} />
  }

  const businessLineFilters = useMemo(
    () => businessOptions.map(item => ({ text: item.name || item.code, value: item.code })),
    [businessOptions]
  )

  const environmentFilters = useMemo(
    () => scopedEnvOptions.map(item => ({ text: item.code, value: item.code })),
    [scopedEnvOptions]
  )

  const engineFilters = useMemo(
    () =>
      (Object.entries(ENGINE_LABELS) as Array<[Task['engine_type'], string]>).map(
        ([value, text]) => ({
          text,
          value,
        })
      ),
    []
  )

  const protocolFilters = useMemo(
    () =>
      Object.keys(PROTOCOL_LABELS).map(value => ({
        text: getProtocolLabel(String(value)),
        value: value as Task['protocols'][number],
      })),
    []
  )

  const taskPatternFilters = useMemo(() => TASK_PATTERN_FILTER_OPTIONS, [])

  // Columns for the detailed table
  const columns: TableProps<Task>['columns'] = [
    {
      title: '任务 ID',
      dataIndex: 'id',
      key: 'id',
      width: 72,
      fixed: 'left',
      className: 'olh-fixed-key-column',
    },
    {
      title: '任务',
      dataIndex: 'name',
      key: 'name',
      width: 380,
      ellipsis: true,
      render: (text: string, record: Task) => (
        <div className="olh-watchlist-task-cell">
          <div
            data-testid={`watchlist-task-name-${record.id}`}
            title={text}
            style={{ ...SINGLE_LINE_TEXT_STYLE, fontWeight: 500 }}
          >
            {text}
          </div>
          <div
            title={record.description || '无描述'}
            style={{ ...SINGLE_LINE_TEXT_STYLE, fontSize: 12, color: 'var(--text-secondary)' }}
          >
            {record.description || '无描述'}
          </div>
        </div>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 118,
      render: (status: Task['status'], record: Task) => {
        const latestRecord = record as TaskWithLatestRunStatus
        return getTaskStatusTag(
          status,
          latestRecord.last_run_status ?? null,
          latestRecord.last_run_status_label
        )
      },
    },
    {
      title: '压测环境',
      dataIndex: 'env',
      key: 'environment',
      width: 104,
      filters: environmentFilters,
      filterMultiple: true,
      filteredValue: splitFilterValue(filterValues.env),
      render: value => renderSingleLineCell(value),
    },
    {
      title: '创建人',
      dataIndex: 'created_by',
      key: 'creator',
      width: 124,
      ellipsis: true,
      render: (v: number | null | undefined, record) =>
        renderSingleLineCell(
          record.created_by_name || (v ? `user#${v}` : '-'),
          `watchlist-creator-${record.id}`
        ),
    },
    {
      title: '协作人',
      key: 'collaborators',
      width: 124,
      ellipsis: true,
      render: (_value, record) => renderSingleLineCell(resolveCollaborators(record)),
    },
    {
      title: '业务线',
      dataIndex: 'properties',
      key: 'businessLine',
      width: 112,
      filters: businessLineFilters,
      filterMultiple: true,
      filteredValue: splitFilterValue(filterValues.business_line),
      render: (properties: Task['properties']) => resolveBusinessLine(properties, businessOptions),
    },
    {
      title: '引擎类型',
      dataIndex: 'engine_type',
      key: 'engine',
      width: 104,
      filters: engineFilters,
      filterMultiple: true,
      filteredValue: splitFilterValue(filterValues.engine_type),
      render: (v: Task['engine_type']) => ENGINE_LABELS[v] || v?.toUpperCase?.() || '-',
    },
    {
      title: '任务模式',
      dataIndex: 'task_pattern',
      key: 'taskPattern',
      width: 108,
      filters: taskPatternFilters,
      filterMultiple: true,
      filteredValue: splitFilterValue(filterValues.task_pattern),
      render: (taskPattern: Task['task_pattern']) => resolveTaskPattern(taskPattern),
    },
    {
      title: '协议类型',
      dataIndex: 'protocols',
      key: 'protocol',
      width: 132,
      ellipsis: true,
      filters: protocolFilters,
      filterMultiple: true,
      filteredValue: splitFilterValue(filterValues.protocol),
      render: (protocols: Task['protocols']) =>
        renderSingleLineCell(
          Array.isArray(protocols) && protocols.length > 0
            ? protocols.map(protocol => getProtocolLabel(String(protocol))).join(' / ')
            : '-'
        ),
    },
    {
      title: '关联应用',
      key: 'relatedApps',
      width: 144,
      ellipsis: true,
      render: (_value, record) => renderSingleLineCell(resolveRelatedApps(record)),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'createdAt',
      width: 220,
      render: (value: string | null | undefined, record) =>
        renderFullDateTimeCell(value, `watchlist-created-at-${record.id}`),
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updatedAt',
      width: 220,
      render: (value: string | null | undefined, record) =>
        renderFullDateTimeCell(value, `watchlist-updated-at-${record.id}`),
    },
    {
      title: '操作',
      key: 'action',
      width: 258,
      fixed: 'right',
      render: (_value, record) => {
        const moreItems = [
          {
            key: 'stop',
            icon: <StopOutlined />,
            label: '停止',
            disabled: stopRunMutation.isPending,
          },
          {
            key: 'script',
            icon: <FileTextOutlined />,
            label: '脚本',
            disabled: !record.script_id,
          },
          ...(publicAlphaFeatures.trendAnalysis
            ? [
                {
                  key: 'trend',
                  icon: <BarsOutlined />,
                  label: '趋势分析',
                },
              ]
            : []),
          {
            key: 'copy',
            icon: <CopyOutlined />,
            label: '复制',
            disabled: copyTaskMutation.isPending,
          },
        ]

        return (
          <div className="olh-watchlist-row-actions">
            <Button
              type="link"
              size="small"
              className="olh-watchlist-row-action olh-watchlist-row-action--primary"
              icon={<PlayCircleOutlined />}
              disabled={!canEnterStartFlow(record)}
              loading={startFlowTaskId === record.id && prepareTaskMutation.isPending}
              onClick={() => handleStartTask(record)}
            >
              启动
            </Button>
            <Button
              type="link"
              size="small"
              className="olh-watchlist-row-action"
              icon={<FileTextOutlined />}
              onClick={() => navigate(`/runs?task_id=${record.id}`)}
            >
              结果
            </Button>
            <Button
              type="link"
              size="small"
              className="olh-watchlist-row-action"
              icon={<EditOutlined />}
              onClick={() => navigate(`/tasks/${record.id}/edit`)}
            >
              编辑
            </Button>
            <div className="olh-watchlist-row-action-secondary">
              <Dropdown
                trigger={['hover', 'click']}
                menu={{
                  items: moreItems,
                  onClick: ({ key }) => {
                    if (key === 'stop') {
                      stopRunMutation.mutate(record.id)
                    } else if (key === 'script') {
                      setEditingScriptTask(record)
                    } else if (key === 'trend') {
                      navigate(`/trend-analysis?task_id=${record.id}&auto_open=1`)
                    } else if (key === 'copy') {
                      copyTaskMutation.mutate(record.id)
                    }
                  },
                }}
              >
                <Button
                  type="link"
                  size="small"
                  aria-label="更多操作"
                  className="olh-watchlist-row-action olh-watchlist-row-action--icon"
                  icon={<MoreOutlined />}
                />
              </Dropdown>
            </div>
          </div>
        )
      },
    },
  ]

  const handleOpenTemplate = (template: string) => {
    setDemoPreviewKey(template)
  }

  const handleCreateTaskClick = () => {
    navigate('/tasks/new')
  }

  const handleStartTask = (task: Task) => {
    if (canStartTask(task)) {
      setStartingTask(task)
      return
    }
    if (!canPrepareTaskForRun(task)) {
      message.warning('当前任务暂不能启动压测')
      return
    }
    setStartFlowTaskId(task.id)
    prepareTaskMutation.mutate(task)
  }

  // 拉取元数据（环境 / 业务线）
  useEffect(() => {
    metaApi
      .getEnvironments()
      .then(setEnvOptions)
      .catch(() => setEnvOptions([]))
      .finally(() => setEnvOptionsLoaded(true))
    metaApi
      .getBusinessLines()
      .then(setBusinessOptions)
      .catch(() => setBusinessOptions([]))
  }, [])

  useEffect(() => {
    const currentEnv = form.getFieldValue('env')
    const shouldClearFormEnv =
      typeof currentEnv === 'string' &&
      currentEnv.trim().length > 0 &&
      !scopedEnvOptions.some(item => item.code === currentEnv)
    const currentFilterEnv =
      typeof filterValues.env === 'string' && filterValues.env.trim()
        ? filterValues.env
        : undefined
    const nextFilterEnv = normalizeEnvFilterForOptions(filterValues.env, scopedEnvOptions)
    const shouldUpdateFilterEnv = currentFilterEnv !== nextFilterEnv

    if (!shouldClearFormEnv && !shouldUpdateFilterEnv) {
      return
    }
    if (shouldClearFormEnv) {
      form.setFieldValue('env', undefined)
    }
    if (shouldUpdateFilterEnv) {
      setFilterValues(current => {
        const currentEnvValue =
          typeof current.env === 'string' && current.env.trim() ? current.env : undefined
        const nextEnvValue = normalizeEnvFilterForOptions(current.env, scopedEnvOptions)
        if (currentEnvValue === nextEnvValue) {
          return current
        }
        return {
          ...current,
          env: nextEnvValue,
        }
      })
    }
    setCurrentPage(1)
    setSelectedRowKeys([])
  }, [activeTab, filterValues.env, form, scopedEnvOptions])

  const resourcePoolSummary = useMemo(() => {
    if (agentsData?.length) {
      const standbyTotal = agentsData.length
      const inUseTotal = agentsData.reduce((total, agent) => {
        const currentRunTotal = Number(agent.current_run_total)
        if (Number.isFinite(currentRunTotal) && currentRunTotal > 0) {
          return total + currentRunTotal
        }
        return total + (agent.availability_state === 'busy' ? 1 : 0)
      }, 0)
      return {
        standbyTotal,
        inUseTotal: Math.min(inUseTotal, standbyTotal),
        idleTotal: Math.max(standbyTotal - inUseTotal, 0),
      }
    }

    const capacity = summaryData?.pod_capacity || []
    let testTotal = 0
    let testnetTotal = 0
    let mainnetTotal = 0

    for (const item of capacity) {
      const scope = inferClusterScope(String(item.cluster_code || ''))
      if (scope === 'test') {
        testTotal += Number(item.available_pod_count || 0)
      } else if (scope === 'testnet') {
        testnetTotal += Number(item.available_pod_count || 0)
      } else {
        mainnetTotal += Number(item.available_pod_count || 0)
      }
    }

    const targetScope = resolveScopeForTab(activeTab)
    const capacityDerivedStandbyTotal =
      targetScope === 'main' ? mainnetTotal : targetScope === 'testnet' ? testnetTotal : testTotal
    const resourcePool = summaryData?.resource_pool
    if (resourcePool) {
      return {
        standbyTotal: Number(resourcePool.standby_total || 0),
        inUseTotal: Number(resourcePool.in_use_total || 0),
        idleTotal: Number(resourcePool.idle_total || 0),
      }
    }

    return {
      standbyTotal: capacityDerivedStandbyTotal,
      inUseTotal: 0,
      idleTotal: capacityDerivedStandbyTotal,
    }
  }, [activeTab, agentsData, summaryData])
  const resourcePoolLoading = summaryLoading || (isAdmin && agentsLoading)

  return (
    <div className="olh-page-shell olh-watchlist-page olh-focus-task-page">
      {/* 顶部统计区域 */}
      <Row
        className="olh-watchlist-summary-row"
        gutter={[12, 12]}
        align="stretch"
        data-testid="watchlist-top-summary-row"
        style={{ marginBottom: 16 }}
      >
        <Col xs={24} xl={8} style={{ display: 'flex' }}>
          <Card
            className="olh-watchlist-summary-card"
            data-testid="watchlist-summary-card"
            style={TOP_CARD_STYLE}
            bodyStyle={TOP_CARD_BODY_STYLE}
          >
            <div style={TOP_CARD_HEADER_STYLE}>
              <span style={TOP_CARD_TITLE_STYLE}>压测任务概览</span>
            </div>
            <div className="olh-watchlist-summary-metrics" style={SUMMARY_METRICS_STYLE}>
              <Statistic
                title="压测任务总数"
                value={summaryData?.task_total ?? taskData?.total ?? 0}
                valueStyle={{ fontSize: 20, fontWeight: 'bold' }}
                loading={summaryLoading}
              />
              <Statistic
                title="执行成功"
                value={summaryData?.task_success_total ?? 0}
                valueStyle={{ color: 'var(--success-color)', fontSize: 16 }}
                suffix={<span style={{ fontSize: 12, color: 'var(--text-muted)' }}>次</span>}
                loading={summaryLoading}
              />
              <Statistic
                title="执行失败"
                value={summaryData?.task_failed_total ?? 0}
                valueStyle={{ color: 'var(--error-color)', fontSize: 16 }}
                suffix={<span style={{ fontSize: 12, color: 'var(--text-muted)' }}>次</span>}
                loading={summaryLoading}
              />
              <Statistic
                title="报告总数"
                value={summaryData?.report_total ?? 0}
                valueStyle={{ fontSize: 16 }}
                suffix={<span style={{ fontSize: 12, color: 'var(--text-muted)' }}>次</span>}
                loading={summaryLoading}
              />
            </div>
          </Card>
        </Col>
        <Col xs={24} xl={16} style={{ display: 'flex' }}>
          <Card
            className="olh-watchlist-resource-card"
            data-testid="watchlist-resource-card"
            style={TOP_CARD_STYLE}
            bodyStyle={TOP_CARD_BODY_STYLE}
          >
            <div style={TOP_CARD_HEADER_STYLE}>
              <span style={TOP_CARD_TITLE_STYLE}>执行节点资源池</span>
              <a
                href="/guides/k6-standard-scripts"
                style={{ fontSize: 12 }}
                onClick={event => {
                  event.preventDefault()
                  navigate('/guides/k6-standard-scripts')
                }}
              >
                使用指南
              </a>
            </div>
            <div className="olh-watchlist-resource-metrics" style={RESOURCE_METRICS_STYLE}>
              <Statistic
                title="总量"
                value={resourcePoolSummary.standbyTotal}
                loading={resourcePoolLoading}
                valueStyle={{ fontSize: 16 }}
              />
              <Statistic
                title="使用中"
                value={resourcePoolSummary.inUseTotal}
                loading={resourcePoolLoading}
                valueStyle={{ fontSize: 16 }}
              />
              <Statistic
                title="空闲"
                value={resourcePoolSummary.idleTotal}
                loading={resourcePoolLoading}
                valueStyle={{ fontSize: 16 }}
              />
            </div>
          </Card>
        </Col>
      </Row>

      {/* 主内容区域 */}
      <Card className="olh-watchlist-main-card" bodyStyle={{ padding: '0 18px 18px 18px' }}>
        <Tabs
          className="olh-watchlist-tabs"
          activeKey={activeTab}
          onChange={key => {
            setActiveTab(key as WatchlistTabKey)
            setCurrentPage(1)
            setSelectedRowKeys([])
          }}
          items={[
            { key: 'test_tasks', label: 'demo任务' },
            { key: 'testnet_tasks', label: 'testnet任务' },
            { key: 'mainnet_tasks', label: 'mainnet任务' },
          ]}
          style={{ marginBottom: 16 }}
        />

        <Alert
          className="olh-watchlist-guide-alert"
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="第一步：直接启动内置 demo 任务，或导入已有 JMeter / k6 脚本"
          description="public alpha 会自动准备 k6 HTTP+gRPC 和 JMeter HTTP+gRPC 两个任务；如果列表为空，请稍后刷新，或联系管理员确认初始化状态。"
        />

        {/* 筛选栏 */}
        <Form
          className="olh-watchlist-filter-form"
          form={form}
          initialValues={DEFAULT_FILTER_VALUES}
          layout="inline"
          style={FILTER_FORM_STYLE}
          onFinish={values => {
            const parsedTaskId = values.task_id ? Number(values.task_id) : undefined
            const normalizedName =
              typeof values.name === 'string' ? values.name.trim() || undefined : values.name
            const normalizedCreatedByInput =
              typeof values.created_by === 'string' ? values.created_by.trim() : values.created_by
            const parsedCreatedBy = normalizedCreatedByInput
              ? Number(normalizedCreatedByInput)
              : undefined
            const ownerScope = (values.owner_scope || 'mine') as OwnerFilterScope
            const participantIdValue = ownerScope === 'mine' ? currentUserId : undefined
            const createdByValue = ownerScope === 'specific' && Number.isFinite(parsedCreatedBy) ? parsedCreatedBy : undefined
            const createdByNameValue =
              ownerScope === 'specific' &&
              typeof normalizedCreatedByInput === 'string' &&
              normalizedCreatedByInput &&
              !Number.isFinite(parsedCreatedBy)
                ? normalizedCreatedByInput
                : undefined
            const next: Record<string, unknown> = {
              ...filterValues,
              ...values,
              task_id: Number.isFinite(parsedTaskId) ? parsedTaskId : undefined,
              name: normalizedName,
              created_by: createdByValue,
              created_by_name: createdByNameValue,
              participant_id: participantIdValue,
            }
            if (ownerScope === 'mine' && !participantIdValue) {
              message.warning('未识别登录用户，无法按“我的参与任务”筛选，请重新登录')
            }
            if (Array.isArray(values.createTime) && values.createTime.length === 2) {
              const [start, end] = values.createTime
              const normalizedEnd =
                end?.hour?.() === 0 &&
                end?.minute?.() === 0 &&
                end?.second?.() === 0 &&
                end?.millisecond?.() === 0
                  ? end.endOf('day')
                  : end
              next.from = start?.toDate?.().toISOString?.()
              next.to = normalizedEnd?.toDate?.().toISOString?.()
            } else {
              delete next.from
              delete next.to
            }
            delete next.createTime
            setFilterValues(next)
            setCurrentPage(1)
            setSelectedRowKeys([])
          }}
        >
          <Form.Item name="task_id" label="任务ID" style={FILTER_ITEM_STYLE}>
            <Input placeholder="请输入" style={{ width: 120 }} />
          </Form.Item>
          <Form.Item name="name" label="任务名称" style={FILTER_ITEM_STYLE}>
            <Input placeholder="请输入" style={{ width: 160 }} />
          </Form.Item>
          <Form.Item name="status" label="状态" style={FILTER_ITEM_STYLE}>
            <Select placeholder="请选择" style={{ width: 120 }} allowClear>
              <Option value="draft">新建</Option>
              <Option value="ready">当前可压测</Option>
              <Option value="script_missing">未上传脚本</Option>
              <Option value="running">当前正在压测</Option>
            </Select>
          </Form.Item>
          <Form.Item name="env" label="环境" style={FILTER_ITEM_STYLE}>
            <Select placeholder="请选择" style={{ width: 140 }} allowClear>
              {scopedEnvOptions.map(item => (
                <Option key={item.code} value={item.code}>
                  {item.code}
                </Option>
              ))}
            </Select>
          </Form.Item>
          <Form.Item
            className="olh-watchlist-filter-secondary"
            name="created_by"
            label="成员"
            style={FILTER_ITEM_STYLE}
          >
            <Input placeholder="请输入姓名或ID" style={{ width: 140 }} />
          </Form.Item>
          <Form.Item
            className="olh-watchlist-filter-secondary"
            name="createTime"
            label="创建时间"
            style={FILTER_ITEM_STYLE}
          >
            <RangePicker />
          </Form.Item>
          <Form.Item
            className="olh-watchlist-filter-secondary"
            name="owner_scope"
            label="任务范围"
            style={FILTER_ITEM_STYLE}
          >
            <Select style={{ width: 150 }} options={OWNER_SCOPE_OPTIONS} />
          </Form.Item>
          <Form.Item className="olh-watchlist-filter-actions" style={FILTER_ITEM_STYLE}>
            <Space>
              <Button
                onClick={() => {
                  form.resetFields()
                  setFilterValues(DEFAULT_FILTER_VALUES)
                  setCurrentPage(1)
                  setSelectedRowKeys([])
                }}
              >
                重置
              </Button>
              <Button type="primary" htmlType="submit" icon={<SearchOutlined />}>
                查询
              </Button>
            </Space>
          </Form.Item>

          <Form.Item
            className="olh-watchlist-bulk-actions"
            style={{ ...FILTER_ITEM_STYLE, marginLeft: 'auto' }}
          >
            <Space>
              {selectedRowKeys.length > 0 ? (
                <span className="olh-watchlist-bulk-count">{`已选择 ${selectedRowKeys.length} 项`}</span>
              ) : null}
              <Popconfirm
                title="确定要批量终止所选任务吗？"
                description="仅会停止运行中的压测，不会删除任务本身"
                okText="确定"
                cancelText="取消"
                disabled={selectedRowKeys.length === 0}
                onConfirm={() => batchStopMutation.mutate(selectedRowKeys.map(key => Number(key)))}
              >
                <Button
                  disabled={selectedRowKeys.length === 0}
                  loading={batchStopMutation.isPending}
                >
                  批量终止
                </Button>
              </Popconfirm>
              <Button type="primary" onClick={handleCreateTaskClick}>
                创建压测脚本
              </Button>
            </Space>
          </Form.Item>
        </Form>

        {/* 演示 Demo 链接 */}
        <div style={{ marginBottom: 16, fontSize: 12, color: 'var(--text-secondary)' }}>
          <Space split="|">
            <span>推荐先启动内置 demo 任务，再查看结果列表和报告</span>
          </Space>
        </div>
        <div className="olh-watchlist-template-strip" style={{ marginBottom: 16, fontSize: 12, color: 'var(--text-secondary)' }}>
          <Space wrap size={[8, 8]}>
            <span>k6 核心模板:</span>
            {DEMO_TEMPLATE_ITEMS.map(item => (
              <Button key={item} size="small" onClick={() => handleOpenTemplate(item)}>
                {item}
              </Button>
            ))}
          </Space>
        </div>

        {/* 任务表格 */}
        <Table<Task>
          className="olh-watchlist-table"
          rowSelection={{
            type: 'checkbox',
            selectedRowKeys,
            onChange: setSelectedRowKeys,
            fixed: true,
          }}
          columns={columns}
          dataSource={shouldForceEmptyForActiveTab ? [] : taskData?.items || []}
          loading={!envOptionsLoaded || (!shouldForceEmptyForActiveTab && isLoading)}
          rowKey="id"
          tableLayout="fixed"
          scroll={{ x: 2300 }}
          pagination={{
            total: shouldForceEmptyForActiveTab ? 0 : taskData?.total || 0,
            pageSize,
            current: currentPage,
            showSizeChanger: true,
            showQuickJumper: true,
            showTotal: total => `共 ${total} 条`,
          }}
          locale={{
            emptyText: activeTabEmptyText,
          }}
          onChange={(pagination, filters, _sorter, extra) => {
            if (extra.action === 'paginate') {
              setCurrentPage(pagination.current || 1)
              setPageSize(pagination.pageSize || pageSize)
              return
            }
            const nextBusinessLine = getJoinedFilterValue(filters.businessLine)
            const nextEnv = getJoinedFilterValue(filters.environment)
            const nextEngineType = getJoinedFilterValue(filters.engine)
            const nextProtocol = getJoinedFilterValue(filters.protocol)
            const nextTaskPattern = getJoinedFilterValue(filters.taskPattern)
            setCurrentPage(1)
            setSelectedRowKeys([])
            setFilterValues(current => ({
              ...current,
              business_line: nextBusinessLine,
              env: nextEnv,
              engine_type: nextEngineType,
              protocol: nextProtocol,
              task_pattern: nextTaskPattern,
            }))
          }}
        />
      </Card>

      <StartRunDrawer
        open={!!startingTask}
        task={startingTask}
        onClose={() => {
          setStartingTask(null)
          setStartFlowTaskId(null)
        }}
        onCreated={() => {
          queryClient.invalidateQueries({ queryKey: ['runs'] })
          queryClient.invalidateQueries({ queryKey: ['dashboard-runs-count'] })
          queryClient.invalidateQueries({ queryKey: ['dashboard-tasks'] })
          queryClient.invalidateQueries({ queryKey: ['dashboard-task-summary'] })
        }}
      />

      <TaskScriptEditorDrawer
        open={!!editingScriptTask || !!demoPreviewKey}
        scriptId={editingScriptTask?.script_id}
        taskId={editingScriptTask?.id}
        previewVersion={getDemoScriptPreview(demoPreviewKey || '')?.preview || null}
        onClose={() => {
          setEditingScriptTask(null)
          setDemoPreviewKey(null)
        }}
      />

    </div>
  )
}

export default Watchlist
