import { useEffect, useMemo, useState } from 'react'
import { type Dayjs } from 'dayjs'
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import {
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import { formatLocalDateTime } from '@/utils/localDateTime'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { DataTable, StatusBadge } from '@/components/common'
import { metaApi, type MetaItem } from '@/services/metaApi'
import { planApi, type Plan, type PlanExecType, type PlanQuery, type PlanRunStatus, type PlanStatus } from '@/services/planApi'
import { useAuthStore } from '@/store/authStore'
import { resolveUserId } from '@/utils/auth'

const { RangePicker } = DatePicker
const { Text } = Typography

type PlanScope = 'mine' | 'all'
type PlanRoundMode = 'single' | 'multi'
type PlanListTimeField = 'created_at' | 'updated_at'

type DraftFilters = {
  plan_id: string
  name: string
  status?: PlanStatus
  business_line?: string
  env?: string
  engine_type?: string
  created_by_name: string
  round_mode?: PlanRoundMode
  time_field: PlanListTimeField
  time_range?: [Dayjs, Dayjs]
}

type PlanListFilters = Omit<PlanQuery, 'page' | 'pageSize' | 'participant_id'>

const PLAN_STATUS_OPTIONS = [
  { label: '可执行', value: 'ready' satisfies PlanStatus },
  { label: '暂停', value: 'suspended' satisfies PlanStatus },
  { label: '执行中', value: 'running' satisfies PlanStatus },
]

const PLAN_EXEC_TYPE_FILTER_OPTIONS = [
  { text: '手动触发', value: 'manual' satisfies PlanExecType },
  { text: '周期巡检', value: 'cron' satisfies PlanExecType },
  { text: '单次定时', value: 'fixed' satisfies PlanExecType },
]
const PLAN_EXEC_TYPE_VALUES: PlanExecType[] = ['manual', 'cron', 'fixed']

const PLAN_ROUND_MODE_OPTIONS = [
  { label: '单轮执行', value: 'single' satisfies PlanRoundMode },
  { label: '轮次编排', value: 'multi' satisfies PlanRoundMode },
]

const PLAN_TIME_FIELD_OPTIONS = [
  { label: '创建时间', value: 'created_at' satisfies PlanListTimeField },
  { label: '更新时间', value: 'updated_at' satisfies PlanListTimeField },
]

const PLAN_ENGINE_OPTIONS = [
  { label: 'JMeter', value: 'jmeter' },
  { label: 'K6', value: 'k6' },
  { label: 'Custom', value: 'custom' },
]

const PLAN_LIST_POLL_MS = 5_000
const ACTIVE_PLAN_RUN_STATUSES = new Set<PlanRunStatus>(['preparing', 'running'])

const EXEC_TYPE_LABELS: Record<Plan['exec_type'], string> = {
  manual: '手动触发',
  cron: '周期巡检',
  fixed: '单次定时',
}

const isPlanExecType = (value: string | undefined): value is PlanExecType => (
  value != null && PLAN_EXEC_TYPE_VALUES.includes(value as PlanExecType)
)

const getSingleExecTypeFilterValue = (value: unknown): PlanExecType | undefined => {
  if (!Array.isArray(value)) {
    return undefined
  }
  const candidate = value.find((item): item is string => typeof item === 'string' && item.trim().length > 0)?.trim()
  return isPlanExecType(candidate) ? candidate : undefined
}

const toExecTypeFilteredValue = (value: PlanExecType | undefined) => (value ? [value] : null)

const buildStageSummary = (plan: Plan): string => {
  const stageCount = plan.stage_total ?? (Array.isArray(plan.stages) ? plan.stages.length : 0)
  const taskTotal = plan.task_total ?? 0
  const postprocessorTotal = plan.postprocessor_total ?? 0
  return `${stageCount} 阶段 / ${taskTotal} 任务 / ${postprocessorTotal} 后置`
}

const sanitizeFilters = (draft: DraftFilters): PlanListFilters => {
  const trimmedPlanId = draft.plan_id.trim()
  const parsedPlanId = trimmedPlanId ? Number(trimmedPlanId) : Number.NaN
  const from = draft.time_range?.[0]?.toDate().toISOString()
  const to = draft.time_range?.[1]?.toDate().toISOString()
  return {
    plan_id: Number.isInteger(parsedPlanId) && parsedPlanId > 0 ? parsedPlanId : undefined,
    name: draft.name.trim() || undefined,
    status: draft.status,
    business_line: draft.business_line || undefined,
    env: draft.env || undefined,
    engine_type: draft.engine_type?.trim() || undefined,
    created_by_name: draft.created_by_name.trim() || undefined,
    round_mode: draft.round_mode || undefined,
    time_field: draft.time_field,
    from,
    to,
  }
}

const renderListValue = (items?: Array<string | number> | null): string => {
  if (!items || items.length === 0) {
    return '-'
  }
  return items.join(' / ')
}

const renderBusinessLine = (items: string[] | null | undefined, businessOptions: MetaItem[]): string => {
  if (!items || items.length === 0) {
    return '-'
  }
  return items
    .map(item => businessOptions.find(option => option.code === item || option.name === item)?.name || item)
    .join(' / ')
}

const formatDurationSeconds = (value?: number | null): string => {
  if (value == null || value < 0) {
    return '-'
  }

  const totalSeconds = Math.floor(value)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const parts: string[] = []

  if (hours > 0) {
    parts.push(`${hours}小时`)
  }
  if (minutes > 0) {
    parts.push(`${minutes}分`)
  }
  if (seconds > 0 || parts.length === 0) {
    parts.push(`${seconds}秒`)
  }

  return parts.join('')
}

const resolveRoundSummary = (plan: Plan): string => {
  const roundCount = plan.execution_estimate?.round_count ?? plan.total_round ?? 1
  return roundCount > 1 ? `轮次 ${roundCount}` : '单轮'
}

const buildExecutionEstimateSummary = (plan: Plan): string => {
  const estimate = plan.execution_estimate
  if (!estimate) {
    return '开始 - · 预计结束 - · 总时长 - · 间隔 -'
  }

  return [
    `开始 ${formatLocalDateTime(estimate.start_at)}`,
    `预计结束 ${formatLocalDateTime(estimate.end_at)}`,
    `总时长 ${formatDurationSeconds(estimate.total_seconds)}`,
    `间隔 ${formatDurationSeconds(estimate.interval_seconds)}`,
  ].join(' · ')
}

const buildPlanSecondarySummary = (plan: Plan): string => {
  const parts = [
    `模板ID ${plan.plan_id}`,
    buildStageSummary(plan),
    `环境 ${renderListValue(plan.env_list)}`,
    resolveRoundSummary(plan),
  ]
  return parts.join(' · ')
}

const buildLatestPlanRunSummary = (plan: Plan): string | null => {
  if (!plan.latest_plan_run_id || !plan.latest_plan_run_status) {
    return null
  }

  const parts = [
    `最近批次 ${formatLocalDateTime(plan.latest_plan_run_started_at)}`,
    plan.latest_plan_run_status_label || plan.latest_plan_run_status,
    `plan_run ${plan.latest_plan_run_id}`,
  ]
  return parts.join(' · ')
}

const renderPlanTimeWindow = (createdAt?: string | null, updatedAt?: string | null) => (
  <span className="olh-plan-time-window">
    <span className="olh-plan-time-line">
      <span>创建</span>
      <strong>{formatLocalDateTime(createdAt)}</strong>
    </span>
    <span className="olh-plan-time-line">
      <span>更新</span>
      <strong>{formatLocalDateTime(updatedAt)}</strong>
    </span>
  </span>
)

const hasActivePlanRun = (plan: Plan): boolean => (
  Boolean(plan.latest_plan_run_status && ACTIVE_PLAN_RUN_STATUSES.has(plan.latest_plan_run_status))
)

const canStartPlan = (plan: Plan): boolean => plan.status === 'ready' && !hasActivePlanRun(plan)

const hasActivePlanListItems = (items?: Plan[] | null): boolean => {
  if (!items || items.length === 0) {
    return false
  }
  return items.some(item => item.status === 'running' || hasActivePlanRun(item))
}

const PlanList = () => {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { token, user } = useAuthStore()
  const currentUserId = useMemo(() => resolveUserId(token, user), [token, user])

  const [scope, setScope] = useState<PlanScope>('mine')
  const [draftFilters, setDraftFilters] = useState<DraftFilters>({
    plan_id: '',
    name: '',
    created_by_name: '',
    time_field: 'updated_at',
  })
  const [filters, setFilters] = useState<PlanListFilters>({})
  const [pagination, setPagination] = useState({ current: 1, pageSize: 10 })
  const [businessOptions, setBusinessOptions] = useState<MetaItem[]>([])
  const [environmentOptions, setEnvironmentOptions] = useState<MetaItem[]>([])
  const [showAdvancedFilters, setShowAdvancedFilters] = useState(false)
  const [confirmingPlanId, setConfirmingPlanId] = useState<number | null>(null)

  useEffect(() => {
    metaApi.getBusinessLines().then(setBusinessOptions).catch(() => setBusinessOptions([]))
    metaApi.getEnvironments().then(setEnvironmentOptions).catch(() => setEnvironmentOptions([]))
  }, [])

  const queryParams: PlanQuery = useMemo(() => {
    return {
      page: pagination.current,
      pageSize: pagination.pageSize,
      ...filters,
      ...(scope === 'mine' && currentUserId ? { participant_id: currentUserId } : {}),
    }
  }, [currentUserId, filters, pagination, scope])

  const { data, isLoading } = useQuery({
    queryKey: ['plans', queryParams],
    queryFn: () => planApi.getPlanList(queryParams),
    refetchInterval: current => (hasActivePlanListItems(current?.state.data?.items) ? PLAN_LIST_POLL_MS : false),
  })

  const currentPageItems = data?.items || []

  const pageSummary = useMemo(() => {
    return {
      total: data?.total || 0,
      pageTotal: currentPageItems.length,
      ready: currentPageItems.filter(item => item.status === 'ready').length,
      running: currentPageItems.filter(item => item.status === 'running').length,
      activeBatchRuns: currentPageItems.filter(item => hasActivePlanRun(item)).length,
    }
  }, [currentPageItems, data?.total])

  const executeMutation = useMutation({
    mutationFn: (planId: number) => planApi.executePlan(planId, { start_type: 1 }),
    onSuccess: resp => {
      message.success('已启动批次，正在打开详情。')
      queryClient.invalidateQueries({ queryKey: ['plans'] })
      queryClient.invalidateQueries({ queryKey: ['plan-runs'] })
      navigate(`/plan-runs/${resp.plan_run_id}`)
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '触发失败')
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (planId: number) => planApi.deletePlan(planId),
    onSuccess: () => {
      message.success('计划已删除')
      queryClient.invalidateQueries({ queryKey: ['plans'] })
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '删除失败')
    },
  })

  const copyMutation = useMutation({
    mutationFn: (planId: number) => planApi.copyPlan(planId),
    onSuccess: copied => {
      message.success(`已复制计划，当前副本：${copied.name}`)
      queryClient.invalidateQueries({ queryKey: ['plans'] })
      navigate(`/plans/${copied.plan_id}/edit`)
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '复制计划失败')
    },
  })

  const renderPlanActions = (record: Plan) => {
    const startDisabled = !canStartPlan(record) || executeMutation.isPending || confirmingPlanId !== null
    const deleteDisabled = record.status === 'running' || hasActivePlanRun(record) || deleteMutation.isPending

    const handleStart = () => {
      if (!canStartPlan(record)) {
        message.warning('已有执行中的批次或当前模板不可执行，请稍后再试。')
        return
      }
      setConfirmingPlanId(record.plan_id)
      Modal.confirm({
        title: '确认启动批次？',
        content: `将为「${record.name}」创建一次新的批次执行。`,
        okText: '确认启动',
        cancelText: '取消',
        onOk: () => executeMutation.mutateAsync(record.plan_id),
        afterClose: () => setConfirmingPlanId(null),
      })
    }

    return (
      <div className="olh-plan-row-actions">
        <Button
          type="default"
          size="small"
          className="olh-plan-row-action olh-plan-row-action--primary"
          icon={<PlayCircleOutlined />}
          disabled={startDisabled}
          onClick={handleStart}
        >
          启动
        </Button>
        <div className="olh-plan-row-action-secondary">
          <Tooltip title="编辑模板">
            <Button
              type="text"
              size="small"
              aria-label="编辑模板"
              className="olh-plan-row-action"
              icon={<EditOutlined />}
              onClick={() => navigate(`/plans/${record.plan_id}/edit`)}
            />
          </Tooltip>
          <Tooltip title="复制模板">
            <Button
              type="text"
              size="small"
              aria-label="复制模板"
              className="olh-plan-row-action"
              icon={<CopyOutlined />}
              disabled={copyMutation.isPending}
              onClick={() => copyMutation.mutate(record.plan_id)}
            />
          </Tooltip>
          <Tooltip title="批次记录">
            <Button
              type="text"
              size="small"
              aria-label="批次记录"
              className="olh-plan-row-action"
              icon={<EyeOutlined />}
              onClick={() => navigate(`/plan-runs?plan_id=${record.plan_id}`)}
            />
          </Tooltip>
          <Popconfirm
            title={`确认删除计划「${record.name}」吗？`}
            description="删除后模板会从列表隐藏，已关联的批次记录仍保留。"
            okText="确定"
            cancelText="取消"
            trigger="click"
            disabled={deleteDisabled}
            onConfirm={() => deleteMutation.mutate(record.plan_id)}
          >
            <Tooltip title={deleteDisabled ? '执行中模板不可删除' : '删除模板'}>
              <Button
                type="text"
                size="small"
                aria-label="删除模板"
                className="olh-plan-row-action olh-plan-row-action--delete"
                icon={<DeleteOutlined />}
                disabled={deleteDisabled}
              />
            </Tooltip>
          </Popconfirm>
        </div>
      </div>
    )
  }

  const columns = [
    { title: 'ID', dataIndex: 'plan_id', key: 'plan_id', width: 64, fixed: 'left' as const, className: 'olh-fixed-key-column' },
    {
      title: '计划名称',
      dataIndex: 'name',
      key: 'name',
      width: 700,
      render: (value: string, record: Plan) => (
        <Space direction="vertical" size={2} className="olh-plan-name-stack">
          <span className="olh-plan-name-text" title={value}>{value}</span>
          <Text type="secondary" ellipsis={{ tooltip: buildPlanSecondarySummary(record) }}>
            {buildPlanSecondarySummary(record)}
          </Text>
          <Text
            type="secondary"
            ellipsis={{ tooltip: `归属 ${renderBusinessLine(record.business_lines, businessOptions)} · 创建人 ${record.created_by_name || '-'}` }}
          >
            {`归属 ${renderBusinessLine(record.business_lines, businessOptions)} · 创建人 ${record.created_by_name || '-'}`}
          </Text>
          <span
            className="olh-plan-execution-estimate"
            title={buildExecutionEstimateSummary(record)}
          >
            {buildExecutionEstimateSummary(record)}
          </span>
        </Space>
      ),
    },
    {
      title: '任务类型',
      dataIndex: 'exec_type',
      key: 'exec_type',
      width: 110,
      filters: PLAN_EXEC_TYPE_FILTER_OPTIONS,
      filterMultiple: false,
      filteredValue: toExecTypeFilteredValue(filters.exec_type),
      render: (_value: Plan['exec_type'], record: Plan) => record.exec_type_label || EXEC_TYPE_LABELS[record.exec_type] || record.exec_type,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 185,
      render: (status: Plan['status'], record: Plan) => (
        <Space direction="vertical" size={2}>
          <StatusBadge status={status} text={record.status_label || undefined} />
          {buildLatestPlanRunSummary(record) ? (
            <Text type="secondary">{buildLatestPlanRunSummary(record)}</Text>
          ) : null}
        </Space>
      ),
    },
    {
      title: '时间',
      dataIndex: 'updated_at',
      key: 'time_window',
      width: 220,
      render: (_value: string | null | undefined, record: Plan) => renderPlanTimeWindow(record.created_at, record.updated_at),
    },
    {
      title: '操作',
      dataIndex: 'plan_id',
      key: 'actions',
      width: 214,
      fixed: 'right' as const,
      render: (_value: number, record: Plan) => renderPlanActions(record),
    },
  ]

  const handleSearch = () => {
    setFilters(prev => ({ ...sanitizeFilters(draftFilters), exec_type: prev.exec_type }))
    setPagination(prev => ({ ...prev, current: 1 }))
  }

  const handleReset = () => {
    setDraftFilters({
      plan_id: '',
      name: '',
      business_line: undefined,
      env: undefined,
      engine_type: undefined,
      created_by_name: '',
      round_mode: undefined,
      time_field: 'updated_at',
      time_range: undefined,
      status: undefined,
    })
    setFilters({})
    setPagination(prev => ({ ...prev, current: 1 }))
    queryClient.invalidateQueries({ queryKey: ['plans'] })
  }

  return (
    <div className="olh-page-shell olh-console-page olh-plan-list-page">
      <div className="olh-console-hero olh-plan-hero">
        <div className="olh-console-hero-main">
          <div className="olh-page-breadcrumb">OpenLoadHub / Plan Templates</div>
          <div className="olh-console-title-row">
            <h1 className="olh-page-title" aria-label="批次模板">批次模板</h1>
            <span className={pageSummary.running > 0 ? 'olh-live-pill olh-live-pill--active' : 'olh-live-pill'}>
              {pageSummary.running > 0 ? 'RUNNING' : 'READY'}
            </span>
          </div>
          <div className="olh-page-subtitle">
            集中查看批次模板的编排方式、适用环境和最近执行结果。
          </div>
          <div className="olh-console-command-strip">
            <span>{`模板 ${pageSummary.total}`}</span>
            <span>{`当前页 ${pageSummary.pageTotal}`}</span>
            <span>{`可执行 ${pageSummary.ready}`}</span>
            <span>{`活跃批次 ${pageSummary.activeBatchRuns}`}</span>
          </div>
        </div>
        <div className="olh-console-hero-side">
          <div className="olh-console-focus-panel">
            <div className="olh-console-focus-label">Scope</div>
            <div className="olh-console-focus-value">{scope === 'mine' ? 'Mine' : 'All'}</div>
            <div className="olh-console-focus-copy">
              {scope === 'mine' ? '默认我的模板（创建/协作）' : '当前全部模板'}
            </div>
            <div className="olh-console-focus-meta">
              <span>{`执行中 ${pageSummary.running}`}</span>
              <span>{`可执行 ${pageSummary.ready}`}</span>
            </div>
          </div>
          <Space wrap className="olh-console-actions">
            <Button onClick={() => navigate('/plan-runs')}>批次记录</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate('/plans/new')}>
              新建批次模板
            </Button>
          </Space>
        </div>
      </div>

      {scope === 'mine' && !currentUserId ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="未识别当前登录用户，当前暂时按全部模板展示。请重新登录后再使用“我的模板（创建/协作）”筛选。"
        />
      ) : null}

      <Card className="olh-filter-panel olh-console-filter-panel olh-plan-filter-panel" bodyStyle={{ padding: 16 }}>
        <div className="olh-filter-panel-header">
          <div>
            <div className="olh-filter-panel-title">模板定位</div>
            <div className="olh-filter-panel-copy">优先按模板 ID、名称和业务线定位；状态、环境、引擎和时间口径保留在高级筛选。</div>
          </div>
          <Button type="link" onClick={() => setShowAdvancedFilters(prev => !prev)}>
            {showAdvancedFilters ? '收起高级筛选' : '更多筛选'}
          </Button>
        </div>
        <Tabs
          size="small"
          className="olh-plan-scope-tabs"
          activeKey={scope}
          onChange={value => {
            setScope(value as PlanScope)
            setPagination(prev => ({ ...prev, current: 1 }))
          }}
          items={[
            { key: 'mine', label: '我的模板' },
            { key: 'all', label: '全部模板' },
          ]}
        />

        <div className="olh-filter-chip-row">
          <span className="olh-data-chip">{`模板总数 ${pageSummary.total}`}</span>
          <span className="olh-data-chip">{`当前页记录 ${pageSummary.pageTotal}`}</span>
          <span className="olh-data-chip">{`当前页可执行 ${pageSummary.ready}`}</span>
          <span className="olh-data-chip">{`当前页执行中 ${pageSummary.running}`}</span>
          <span className="olh-data-chip">{`最近活跃批次 ${pageSummary.activeBatchRuns}`}</span>
        </div>

        <div className="olh-plan-filter-console">
          <Space wrap size={8}>
            <Input
              allowClear
              placeholder="请输入计划ID"
              aria-label="计划ID搜索"
              style={{ width: 160 }}
              value={draftFilters.plan_id}
              onChange={event => setDraftFilters(prev => ({ ...prev, plan_id: event.target.value }))}
              onPressEnter={handleSearch}
            />
            <Input
              allowClear
              placeholder="请输入计划名称"
              aria-label="计划名称搜索"
              prefix={<SearchOutlined />}
              style={{ width: 200 }}
              value={draftFilters.name}
              onChange={event => setDraftFilters(prev => ({ ...prev, name: event.target.value }))}
              onPressEnter={handleSearch}
            />
            <Select
              allowClear
              placeholder="请选择业务线"
              aria-label="业务线搜索"
              style={{ width: 160 }}
              options={businessOptions.map(item => ({ label: item.name || item.code, value: item.code }))}
              value={draftFilters.business_line}
              onChange={value => setDraftFilters(prev => ({ ...prev, business_line: value }))}
            />
            <Button type="primary" icon={<SearchOutlined />} onClick={handleSearch} loading={isLoading}>
              查询
            </Button>
            <Button icon={<ReloadOutlined />} onClick={handleReset}>
              重置
            </Button>
          </Space>
          <Space size={[8, 8]} wrap>
            <Tag color="blue">{scope === 'mine' ? '默认我的模板（创建/协作）' : '当前全部模板'}</Tag>
          </Space>
        </div>

        {showAdvancedFilters ? (
          <div style={{ marginTop: 8 }}>
            <Space wrap size={8}>
              <Select
                allowClear
                placeholder="请选择状态"
                aria-label="状态搜索"
                style={{ width: 140 }}
                options={PLAN_STATUS_OPTIONS}
                value={draftFilters.status}
                onChange={value => setDraftFilters(prev => ({ ...prev, status: value }))}
              />
            </Space>
            <Space wrap size={8} style={{ marginTop: 8 }}>
              <Select
                allowClear
                placeholder="请选择环境"
                aria-label="环境搜索"
                style={{ width: 150 }}
                options={environmentOptions.map(item => ({ label: item.code, value: item.code }))}
                value={draftFilters.env}
                onChange={value => setDraftFilters(prev => ({ ...prev, env: value }))}
              />
              <Select
                allowClear
                placeholder="请选择引擎"
                aria-label="引擎搜索"
                style={{ width: 150 }}
                options={PLAN_ENGINE_OPTIONS}
                value={draftFilters.engine_type}
                onChange={value => setDraftFilters(prev => ({ ...prev, engine_type: value }))}
              />
              <Input
                allowClear
                placeholder="请输入创建人"
                aria-label="创建人搜索"
                style={{ width: 160 }}
                value={draftFilters.created_by_name}
                onChange={event => setDraftFilters(prev => ({ ...prev, created_by_name: event.target.value }))}
                onPressEnter={handleSearch}
              />
              <Select
                allowClear
                placeholder="请选择轮次模式"
                aria-label="轮次模式搜索"
                style={{ width: 150 }}
                options={PLAN_ROUND_MODE_OPTIONS}
                value={draftFilters.round_mode}
                onChange={value => setDraftFilters(prev => ({ ...prev, round_mode: value }))}
              />
              <Select
                aria-label="时间口径搜索"
                style={{ width: 120 }}
                options={PLAN_TIME_FIELD_OPTIONS}
                value={draftFilters.time_field}
                onChange={value => setDraftFilters(prev => ({ ...prev, time_field: value }))}
              />
              <RangePicker
                showTime
                aria-label="时间范围搜索"
                value={draftFilters.time_range}
                onChange={value => setDraftFilters(prev => ({
                  ...prev,
                  time_range: value?.[0] && value?.[1] ? [value[0], value[1]] : undefined,
                }))}
              />
            </Space>
            <Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
              高级筛选会和主筛选一起透传到 `/api/v1/plans`；其中时间口径按你选择的创建时间或更新时间搜索。
            </Text>
            <Text type="secondary" style={{ display: 'block', marginTop: 4 }}>
              批次记录入口保留在操作列，避免首屏挤占模板摘要宽度。
            </Text>
          </div>
        ) : null}
      </Card>

      <div className="olh-table-shell olh-plan-table-shell">
        <div className="olh-data-section-heading">
          <div>
            <div className="olh-section-title">批次模板记录</div>
            <div className="olh-section-subtitle">
              模板摘要、最近批次和执行估算收敛在主表，操作列保持低噪声。
            </div>
          </div>
          <Space wrap size={8}>
            <span className="olh-data-chip">{`总计 ${data?.total ?? 0}`}</span>
            <span className="olh-data-chip">{scope === 'mine' ? '我的模板' : '全部模板'}</span>
          </Space>
        </div>
        <DataTable<Plan>
          rowKey="plan_id"
          columns={columns}
          dataSource={currentPageItems}
          loading={isLoading}
          scroll={{ x: 1580 }}
          size="small"
          className="olh-plan-table"
          rowClassName={record => `olh-plan-table-row olh-plan-table-row--${record.status}`}
          locale={{
            emptyText: (
              <div className="olh-plan-empty-state">
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前筛选下暂无批次模板" />
                <Space wrap className="olh-task-empty-actions">
                  <Button size="small" onClick={handleReset}>清空筛选</Button>
                  <Button size="small" type="primary" onClick={() => navigate('/plans/new')}>
                    新建批次模板
                  </Button>
                </Space>
              </div>
            ),
          }}
          pagination={{
            current: pagination.current,
            pageSize: pagination.pageSize,
            total: data?.total || 0,
            showSizeChanger: true,
            showTotal: total => `共 ${total} 条`,
          }}
          onChange={(paginationInfo, tableFilters) => {
            const nextExecType = getSingleExecTypeFilterValue(tableFilters.exec_type)
            const currentExecType = filters.exec_type
            if (nextExecType !== currentExecType) {
              setFilters(prev => ({ ...prev, exec_type: nextExecType }))
              setPagination(prev => ({
                current: 1,
                pageSize: paginationInfo.pageSize || prev.pageSize,
              }))
              return
            }

            if (paginationInfo.current !== pagination.current || paginationInfo.pageSize !== pagination.pageSize) {
              setPagination({
                current: paginationInfo.current || 1,
                pageSize: paginationInfo.pageSize || 10,
              })
            }
          }}
        />
      </div>

    </div>
  )
}

export default PlanList
