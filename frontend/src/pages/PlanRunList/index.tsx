import { useEffect, useMemo, useState } from 'react'
import { type Dayjs } from 'dayjs'
import { Button, Card, DatePicker, Empty, Input, Select, Space, Tag, Tooltip, Typography, message } from 'antd'
import { EyeOutlined, LinkOutlined, ReloadOutlined, SearchOutlined, StopOutlined } from '@ant-design/icons'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { DataTable, StatusBadge } from '@/components/common'
import { metaApi, type MetaItem } from '@/services/metaApi'
import { planApi, type PlanRun, type PlanRunQuery, type PlanRunStatus } from '@/services/planApi'
import { formatLocalDateTime } from '@/utils/localDateTime'

const { RangePicker } = DatePicker
const { Text } = Typography

type DraftFilters = {
  plan_name: string
  plan_id: string
  plan_run_id: string
  created_by_name: string
  business_line?: string
  env?: string
  engine_type?: string
  round_mode?: 'single' | 'multi'
  status?: PlanRunStatus
  started_at?: [Dayjs, Dayjs]
}

type RoundModeFilterValue = 'single' | 'multi'

const PLAN_RUN_STATUS_OPTIONS = [
  { label: '准备中', value: 'preparing' satisfies PlanRunStatus },
  { label: '执行中', value: 'running' satisfies PlanRunStatus },
  { label: '已完成', value: 'succeeded' satisfies PlanRunStatus },
  { label: '失败', value: 'failed' satisfies PlanRunStatus },
  { label: '已停止', value: 'stopped' satisfies PlanRunStatus },
]
const ROUND_MODE_FILTER_OPTIONS = [
  { label: '单轮批次', value: 'single' satisfies RoundModeFilterValue },
  { label: '多轮批次', value: 'multi' satisfies RoundModeFilterValue },
]

const ACTIVE_PLAN_RUN_STATUSES = new Set<PlanRunStatus>(['preparing', 'running'])
const PLAN_RUN_LIST_POLL_MS = 5_000

const ADVISORY_VERDICT_LABELS: Record<string, string> = {
  pass: '通过候选',
  warn: '需复核',
  fail: '优先处理',
}

const toPositiveInt = (value?: string | null): number | undefined => {
  if (!value) {
    return undefined
  }
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined
}

const renderUserLabel = (userName?: string | null, userId?: number | null): string => {
  if (userName) {
    return userName
  }
  if (!userId) {
    return '-'
  }
  return `user#${userId}`
}

const formatDurationSeconds = (value?: number | null): string => {
  if (value == null || value < 0) {
    return '-'
  }
  const totalSeconds = Math.max(0, Math.floor(value))
  const days = Math.floor(totalSeconds / 86400)
  const hours = Math.floor((totalSeconds % 86400) / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60

  const segments: string[] = []
  if (days > 0) segments.push(`${days}天`)
  if (hours > 0) segments.push(`${hours}小时`)
  if (minutes > 0) segments.push(`${minutes}分`)
  if (seconds > 0 || segments.length === 0) segments.push(`${seconds}秒`)
  return segments.slice(0, 2).join(' ')
}

const renderTimeLine = (label: string, value?: string | null) => (
  <span className="olh-plan-run-time-line">
    <span>{label}</span>
    <strong>{formatLocalDateTime(value)}</strong>
  </span>
)

const resolveElapsedSeconds = (
  startedAt?: string | null,
  endedAt?: string | null,
  durationSeconds?: number | null,
  nowMs: number = Date.now(),
): number | null => {
  if (typeof durationSeconds === 'number' && durationSeconds >= 0 && endedAt) {
    return durationSeconds
  }
  if (!startedAt) {
    return typeof durationSeconds === 'number' && durationSeconds >= 0 ? durationSeconds : null
  }
  const startedMs = Date.parse(startedAt)
  if (Number.isNaN(startedMs)) {
    return typeof durationSeconds === 'number' && durationSeconds >= 0 ? durationSeconds : null
  }
  const endMs = endedAt ? Date.parse(endedAt) : nowMs
  if (Number.isNaN(endMs) || endMs <= startedMs) {
    return typeof durationSeconds === 'number' && durationSeconds >= 0 ? durationSeconds : 0
  }
  const computed = Math.max(1, Math.ceil((endMs - startedMs) / 1000))
  if (typeof durationSeconds === 'number' && durationSeconds >= 0) {
    return Math.max(durationSeconds, computed)
  }
  return computed
}

const estimatePlanRunEta = (
  record: PlanRun,
  nowMs: number,
): { remainingSeconds: number; estimatedEndAt: string } | null => {
  if (!ACTIVE_PLAN_RUN_STATUSES.has(record.status) || !record.started_at) {
    return null
  }

  const elapsedSeconds = resolveElapsedSeconds(
    record.started_at,
    record.ended_at,
    record.duration_seconds,
    nowMs,
  )
  if (elapsedSeconds == null || elapsedSeconds <= 0) {
    return null
  }

  if (typeof record.planned_total_seconds === 'number' && record.planned_total_seconds > 0) {
    const estimatedTotalSeconds = Math.max(record.planned_total_seconds, elapsedSeconds)
    const remainingSeconds = Math.max(0, estimatedTotalSeconds - elapsedSeconds)
    const estimatedEndAt = new Date(Date.parse(record.started_at) + estimatedTotalSeconds * 1000).toISOString()
    return {
      remainingSeconds,
      estimatedEndAt,
    }
  }

  const roundTotal = record.round_total && record.round_total > 0 ? record.round_total : 1
  const taskUnits = Math.max((record.task_total ?? 0) + (record.postprocessor_total ?? 0), 0)

  let progressRatio: number | null = null
  if (taskUnits > 0 && typeof record.launched_run_total === 'number' && record.launched_run_total > 0) {
    const totalUnits = Math.max(taskUnits * roundTotal, 1)
    progressRatio = record.launched_run_total / totalUnits
  }
  if (
    (progressRatio == null || progressRatio <= 0) &&
    roundTotal > 1 &&
    typeof record.round === 'number' &&
    record.round > 0
  ) {
    progressRatio = record.round / roundTotal
  }
  if (progressRatio == null || progressRatio <= 0) {
    progressRatio = 0.5
  }

  const normalizedProgress = Math.min(Math.max(progressRatio, 0.05), 0.95)
  const estimatedTotalSeconds = Math.max(
    elapsedSeconds,
    Math.ceil(elapsedSeconds / normalizedProgress),
  )
  const remainingSeconds = Math.max(0, estimatedTotalSeconds - elapsedSeconds)
  const estimatedEndAt = new Date(Date.parse(record.started_at) + estimatedTotalSeconds * 1000).toISOString()

  return {
    remainingSeconds,
    estimatedEndAt,
  }
}

const buildFilters = (draft: DraftFilters): Omit<PlanRunQuery, 'page' | 'pageSize'> => ({
  plan_name: draft.plan_name.trim() || undefined,
  plan_id: toPositiveInt(draft.plan_id),
  plan_run_id: toPositiveInt(draft.plan_run_id),
  created_by_name: draft.created_by_name.trim() || undefined,
  business_line: draft.business_line || undefined,
  env: draft.env || undefined,
  engine_type: draft.engine_type || undefined,
  round_mode: draft.round_mode || undefined,
  status: draft.status,
  from: draft.started_at?.[0]?.toDate().toISOString(),
  to: draft.started_at?.[1]?.toDate().toISOString(),
})

const renderBusinessLine = (items: string[] | null | undefined, businessOptions: MetaItem[]): string => {
  if (!items || items.length === 0) {
    return '-'
  }
  return items
    .map(item => businessOptions.find(option => option.code === item || option.name === item)?.name || item)
    .join(' / ')
}

const buildFilterSummary = (draft: DraftFilters, businessOptions: MetaItem[]): string[] => {
  const segments: string[] = []
  if (draft.plan_name.trim()) segments.push(`模板名称 ${draft.plan_name.trim()}`)
  if (draft.plan_id.trim()) segments.push(`模板ID ${draft.plan_id.trim()}（非批次ID）`)
  if (draft.plan_run_id.trim()) segments.push(`批次ID ${draft.plan_run_id.trim()}`)
  if (draft.created_by_name.trim()) segments.push(`执行人 ${draft.created_by_name.trim()}`)
  if (draft.business_line) {
    const label = businessOptions.find(item => item.code === draft.business_line || item.name === draft.business_line)?.name || draft.business_line
    segments.push(`业务线 ${label}`)
  }
  if (draft.env) {
    segments.push(`环境 ${draft.env}`)
  }
  if (draft.engine_type) {
    segments.push(`引擎 ${draft.engine_type}`)
  }
  if (draft.round_mode) {
    const label = ROUND_MODE_FILTER_OPTIONS.find(item => item.value === draft.round_mode)?.label || draft.round_mode
    segments.push(`轮次数量 ${label}`)
  }
  if (draft.status) {
    const label = PLAN_RUN_STATUS_OPTIONS.find(item => item.value === draft.status)?.label || draft.status
    segments.push(`状态 ${label}`)
  }
  if (draft.started_at?.[0] && draft.started_at?.[1]) {
    segments.push(`时间 ${draft.started_at[0].format('YYYY-MM-DD HH:mm')} ~ ${draft.started_at[1].format('YYYY-MM-DD HH:mm')}`)
  }
  return segments
}

const getAdvisoryTagClassName = (verdict?: string | null): string => {
  const normalized = verdict === 'pass' || verdict === 'warn' || verdict === 'fail' ? verdict : 'pending'
  return `olh-plan-run-verdict-tag olh-plan-run-verdict-tag--${normalized}`
}

const PlanRunList = () => {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [searchParams] = useSearchParams()
  const [showAdvancedFilters, setShowAdvancedFilters] = useState(
    Boolean(
      searchParams.get('business_line')
      || searchParams.get('env')
      || searchParams.get('engine_type')
      || searchParams.get('round_mode')
      || searchParams.get('from')
      || searchParams.get('to'),
    )
  )
  const [businessOptions, setBusinessOptions] = useState<MetaItem[]>([])
  const [environmentOptions, setEnvironmentOptions] = useState<MetaItem[]>([])

  useEffect(() => {
    metaApi.getBusinessLines().then(setBusinessOptions).catch(() => setBusinessOptions([]))
    metaApi.getEnvironments().then(setEnvironmentOptions).catch(() => setEnvironmentOptions([]))
  }, [])

  const [draftFilters, setDraftFilters] = useState<DraftFilters>({
    plan_name: searchParams.get('plan_name') || '',
    plan_id: searchParams.get('plan_id') || '',
    plan_run_id: searchParams.get('plan_run_id') || '',
    created_by_name: searchParams.get('created_by_name') || '',
    business_line: searchParams.get('business_line') || undefined,
    env: searchParams.get('env') || undefined,
    engine_type: searchParams.get('engine_type') || undefined,
    round_mode: (searchParams.get('round_mode') as RoundModeFilterValue | null) || undefined,
    status: (searchParams.get('status') as PlanRunStatus | null) || undefined,
    started_at: undefined,
  })
  const [filters, setFilters] = useState<Omit<PlanRunQuery, 'page' | 'pageSize'>>(() =>
    buildFilters({
      plan_name: searchParams.get('plan_name') || '',
      plan_id: searchParams.get('plan_id') || '',
      plan_run_id: searchParams.get('plan_run_id') || '',
      created_by_name: searchParams.get('created_by_name') || '',
      business_line: searchParams.get('business_line') || undefined,
      env: searchParams.get('env') || undefined,
      engine_type: searchParams.get('engine_type') || undefined,
      round_mode: (searchParams.get('round_mode') as RoundModeFilterValue | null) || undefined,
      status: (searchParams.get('status') as PlanRunStatus | null) || undefined,
      started_at: undefined,
    })
  )
  const [pagination, setPagination] = useState({ current: 1, pageSize: 10 })
  const queryParams: PlanRunQuery = useMemo(() => {
    return {
      page: pagination.current,
      pageSize: pagination.pageSize,
      ...filters,
    }
  }, [filters, pagination])

  const { data, isLoading } = useQuery({
    queryKey: ['plan-runs', queryParams],
    queryFn: () => planApi.getPlanRunList(queryParams),
    refetchInterval: PLAN_RUN_LIST_POLL_MS,
  })

  const activePlanRunCount = useMemo(
    () => (data?.items || []).filter(item => ACTIVE_PLAN_RUN_STATUSES.has(item.status)).length,
    [data],
  )
  const envOptions = useMemo(
    () => {
      const optionMap = new Map<string, { label: string; value: string }>()
      environmentOptions.forEach(item => {
        const value = item.code?.trim()
        if (!value || optionMap.has(value)) {
          return
        }
        optionMap.set(value, {
          label: value,
          value,
        })
      })
      if (draftFilters.env && !optionMap.has(draftFilters.env)) {
        optionMap.set(draftFilters.env, {
          label: draftFilters.env,
          value: draftFilters.env,
        })
      }
      return Array.from(optionMap.values()).sort((left, right) => left.value.localeCompare(right.value))
    },
    [draftFilters.env, environmentOptions],
  )

  const [nowMs, setNowMs] = useState(() => Date.now())

  useEffect(() => {
    if (activePlanRunCount === 0) {
      return
    }
    const timer = window.setInterval(() => {
      setNowMs(Date.now())
    }, 15000)
    return () => window.clearInterval(timer)
  }, [activePlanRunCount])

  const pageSummary = useMemo(() => {
    const items = data?.items || []
    return {
      total: data?.total || 0,
      preparing: items.filter(item => item.status === 'preparing').length,
      running: items.filter(item => item.status === 'running').length,
      failed: items.filter(item => item.status === 'failed').length,
      advisoryWarn: items.filter(item => item.advisory_verdict === 'warn').length,
      advisoryFail: items.filter(item => item.advisory_verdict === 'fail').length,
      stoppable: items.filter(item => item.can_stop).length,
    }
  }, [data])

  const executionCenterSummary = useMemo(() => {
    const items = data?.items || []
    const launchedRunTotal = items.reduce((sum, item) => sum + (item.launched_run_total || 0), 0)
    const taskUnitTotal = items.reduce(
      (sum, item) => sum + (item.task_total || 0) + (item.postprocessor_total || 0),
      0,
    )
    const multiRoundTotal = items.filter(item => (item.round_total || 1) > 1).length
    const envCoverage = Array.from(new Set(items.flatMap(item => item.env_list || []).filter(Boolean))).length
    const engineCoverage = Array.from(new Set(items.flatMap(item => item.engine_types || []).filter(Boolean))).length
    return {
      launchedRunTotal,
      taskUnitTotal,
      multiRoundTotal,
      envCoverage,
      engineCoverage,
    }
  }, [data])

  const sortedItems = useMemo(() => {
    const items = [...(data?.items || [])]
    items.sort((left, right) => right.plan_run_id - left.plan_run_id)
    return items
  }, [data?.items])

  const filterSummary = useMemo(() => buildFilterSummary(draftFilters, businessOptions), [businessOptions, draftFilters])

  const stopMutation = useMutation({
    mutationFn: (planRunId: number) => planApi.stopPlanRun(planRunId),
    onSuccess: () => {
      message.success('已停止批次执行')
      queryClient.invalidateQueries({ queryKey: ['plan-runs'] })
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '停止失败')
    },
  })

  const columns = [
    {
      title: '批次ID',
      dataIndex: 'plan_run_id',
      key: 'plan_run_id',
      width: 72,
      fixed: 'left' as const,
      className: 'olh-fixed-key-column',
      render: (value: number) => <Text strong>{value}</Text>,
    },
    {
      title: '批次模板',
      dataIndex: 'plan_name',
      key: 'plan_name',
      width: 260,
      render: (value: string | null | undefined, record: PlanRun) => {
        const planName = value || '-'
        const templateMeta = `模板ID ${record.plan_id} · ${renderBusinessLine(record.business_lines, businessOptions)} · ${
          record.round_label ||
          (record.round_total && record.round_total > 1
            ? `${record.round ? `轮次 ${record.round}` : '执行中'} / 共 ${record.round_total}`
            : '单轮执行')
        } · 已启动执行 ${record.launched_run_total ?? 0}`
        const engineMeta = `环境 ${(record.env_list || []).join(' / ') || '-'} · 引擎 ${(record.engine_types || []).join(' / ') || '-'} · ${
          record.round_mode_label || '单轮执行'
        }`
        return (
          <Space className="olh-plan-run-template-cell" direction="vertical" size={2}>
            <span className="olh-plan-run-template-title" title={planName}>{planName}</span>
            <Text className="olh-plan-run-template-meta" type="secondary" title={templateMeta}>
              {templateMeta}
            </Text>
            <Text className="olh-plan-run-template-meta" type="secondary" title={engineMeta}>
              {engineMeta}
            </Text>
          </Space>
        )
      },
    },
    {
      title: '批次状态',
      dataIndex: 'status',
      key: 'status',
      width: 104,
      render: (status: PlanRun['status'], record: PlanRun) => (
        <span className={`olh-plan-run-status-token olh-plan-run-status-token--${status}`}>
          <StatusBadge status={status} text={record.status_label || undefined} />
        </span>
      ),
    },
    {
      title: '批次结论',
      dataIndex: 'advisory_summary',
      key: 'advisory_summary',
      width: 220,
      align: 'left' as const,
      className: 'olh-plan-run-verdict-column',
      render: (_value: string | null | undefined, record: PlanRun) => (
        <div className="olh-plan-run-verdict-cell">
          <div className="olh-plan-run-verdict-tags">
            {record.advisory_verdict ? (
              <Tag className={getAdvisoryTagClassName(record.advisory_verdict)}>
                {ADVISORY_VERDICT_LABELS[record.advisory_verdict] || record.advisory_verdict}
              </Tag>
            ) : null}
            <Tag className="olh-plan-run-report-token">{`已就绪 ${record.report_ready_total ?? 0}`}</Tag>
            {(record.report_template_fallback_total || 0) > 0 ? (
              <Tag className="olh-plan-run-report-token olh-plan-run-report-token--refresh">{`需刷新 ${record.report_template_fallback_total}`}</Tag>
            ) : null}
            {(record.report_missing_total || 0) > 0 ? (
              <Tag className="olh-plan-run-report-token olh-plan-run-report-token--missing">{`缺失 ${record.report_missing_total}`}</Tag>
            ) : null}
          </div>
          <Text className="olh-plan-run-verdict-summary" type="secondary">
            {record.advisory_summary?.summary_text || '当前批次结论待生成'}
          </Text>
        </div>
      ),
    },
    {
      title: '执行人',
      dataIndex: 'created_by',
      key: 'created_by',
      width: 92,
      render: (value: number | null | undefined, record: PlanRun) => renderUserLabel(record.created_by_name, value),
    },
    {
      title: '时间窗口',
      dataIndex: 'started_at',
      key: 'time_window',
      width: 250,
      render: (_value: string | null | undefined, record: PlanRun) => {
        const elapsedSeconds = resolveElapsedSeconds(
          record.started_at,
          record.ended_at,
          record.duration_seconds,
          nowMs,
        )
        const eta = estimatePlanRunEta(record, nowMs)

        return (
          <Space className="olh-plan-run-time-window" direction="vertical" size={2}>
            {renderTimeLine('开始', record.started_at)}
            {renderTimeLine('结束', record.ended_at)}
            <Text type="secondary">{`耗时 ${formatDurationSeconds(elapsedSeconds)}`}</Text>
            {eta ? (
              <>
                <Text type="secondary">{`预计剩余 ${formatDurationSeconds(eta.remainingSeconds)}`}</Text>
                <Text type="secondary">{`预计结束 ${formatLocalDateTime(eta.estimatedEndAt)}`}</Text>
              </>
            ) : null}
          </Space>
        )
      },
    },
    {
      title: '操作',
      dataIndex: 'plan_run_id',
      key: 'actions',
      width: 136,
      fixed: 'right' as const,
      align: 'left' as const,
      className: 'olh-plan-run-actions-cell',
      render: (_: unknown, record: PlanRun) => (
        <div className="olh-plan-run-row-actions">
          <div className="olh-plan-run-row-action-secondary">
            <Tooltip title="查看详情">
              <Button
                type="text"
                size="small"
                aria-label="查看详情"
                className="olh-plan-run-row-action"
                icon={<EyeOutlined />}
                onClick={() => navigate(`/plan-runs/${record.plan_run_id}`)}
              />
            </Tooltip>
            {record.can_stop ? (
              <Tooltip title="停止批次">
                <Button
                  type="text"
                  size="small"
                  aria-label="停止批次"
                  className="olh-plan-run-row-action olh-plan-run-row-action--stop"
                  icon={<StopOutlined />}
                  disabled={stopMutation.isPending}
                  onClick={() => stopMutation.mutate(record.plan_run_id)}
                />
              </Tooltip>
            ) : null}
            {record.latest_failed_run_id ? (
              <Tooltip title="查看最近失败执行">
                <Button
                  type="text"
                  size="small"
                  aria-label="查看最近失败执行"
                  className="olh-plan-run-row-action"
                  icon={<LinkOutlined />}
                  onClick={() => navigate(`/runs/${record.latest_failed_run_id}`)}
                />
              </Tooltip>
            ) : null}
          </div>
        </div>
      ),
    },
  ]

  const handleSearch = () => {
    setFilters(buildFilters(draftFilters))
    setPagination(prev => ({ ...prev, current: 1 }))
  }

  const handleReset = () => {
    const resetDraft: DraftFilters = {
      plan_name: '',
      plan_id: '',
      plan_run_id: '',
      created_by_name: '',
      business_line: undefined,
      env: undefined,
      engine_type: undefined,
      round_mode: undefined,
      status: undefined,
      started_at: undefined,
    }
    setDraftFilters(resetDraft)
    setFilters({})
    setPagination(prev => ({ ...prev, current: 1 }))
  }

  const failedSummaryHelper = pageSummary.failed > 0
    ? `失败结论 ${pageSummary.advisoryFail}${pageSummary.preparing > 0 ? ` · 准备中 ${pageSummary.preparing}` : ''}`
    : pageSummary.preparing > 0
      ? `准备中 ${pageSummary.preparing}`
      : '无失败批次'
  const coverageSummaryHelper = [
    executionCenterSummary.multiRoundTotal > 0 ? `多轮 ${executionCenterSummary.multiRoundTotal}` : '单轮为主',
    pageSummary.stoppable > 0 ? `可停 ${pageSummary.stoppable}` : '无可停',
    pageSummary.advisoryWarn > 0 ? `复核 ${pageSummary.advisoryWarn}` : '无复核',
  ].join(' · ')

  return (
    <div className="olh-page-shell olh-console-page olh-plan-run-list-page">
      <div className="olh-console-hero olh-plan-run-hero">
        <div className="olh-console-hero-main">
          <div className="olh-page-breadcrumb">OpenLoadHub / Plan Runs</div>
          <div className="olh-console-title-row">
            <h1 className="olh-page-title" aria-label="批次记录">批次记录</h1>
            <span className={pageSummary.running > 0 ? 'olh-live-pill olh-live-pill--active' : 'olh-live-pill'}>
              {pageSummary.running > 0 ? 'LIVE' : 'READY'}
            </span>
          </div>
          <div className="olh-page-subtitle">
            主列表默认按最新批次 ID 倒序展示；运行中和失败批次在上方优先关注区单独提示。
          </div>
          <div className="olh-console-command-strip">
            <span>{`批次 ${pageSummary.total}`}</span>
            <span>{`运行中 ${pageSummary.running}`}</span>
            <span>{`失败 ${pageSummary.failed}`}</span>
            <span>{`可停止 ${pageSummary.stoppable}`}</span>
          </div>
        </div>
        <div className="olh-console-hero-side">
          <div className="olh-console-focus-panel">
            <div className="olh-console-focus-label">Execution</div>
            <div className="olh-console-focus-value">{executionCenterSummary.launchedRunTotal}</div>
            <div className="olh-console-focus-copy">当前页已启动执行</div>
            <div className="olh-console-focus-meta">
              <span>{`${executionCenterSummary.envCoverage} 环境`}</span>
              <span>{`${executionCenterSummary.engineCoverage} 引擎`}</span>
            </div>
          </div>
        </div>
      </div>

      <Card className="olh-filter-panel olh-console-filter-panel olh-plan-run-filter-panel" bodyStyle={{ padding: 12 }}>
        <div className="olh-filter-panel-header">
          <div>
            <div className="olh-filter-panel-title">批次定位</div>
            <div className="olh-filter-panel-copy">按模板、批次 ID、执行人和状态快速定位；环境、引擎和轮次筛选收进高级区。</div>
          </div>
          <Button type="link" onClick={() => setShowAdvancedFilters(prev => !prev)}>
            {showAdvancedFilters ? '收起高级筛选' : '更多筛选'}
          </Button>
        </div>
        <Space direction="vertical" size={8} style={{ display: 'flex' }}>
          <div className="olh-plan-run-summary-strip" aria-label="批次状态摘要">
            {[
              {
                label: '运行中批次',
                value: pageSummary.running,
                helper: pageSummary.running > 0 ? '当前主线盯盘' : '无运行中批次',
              },
              {
                label: '失败待处理',
                value: pageSummary.failed,
                helper: failedSummaryHelper,
              },
              {
                label: '已启动执行',
                value: executionCenterSummary.launchedRunTotal,
                helper: `任务/后置 ${executionCenterSummary.taskUnitTotal}`,
              },
              {
                label: '覆盖面',
                value: `${executionCenterSummary.envCoverage} 环境 / ${executionCenterSummary.engineCoverage} 引擎`,
                helper: coverageSummaryHelper,
              },
            ].map(item => (
              <div key={item.label} className="olh-plan-run-summary-item">
                <span className="olh-plan-run-summary-label">{item.label}</span>
                <strong className="olh-plan-run-summary-value">{item.value}</strong>
                <span className="olh-plan-run-summary-helper">{item.helper}</span>
              </div>
            ))}
          </div>
          <div className="olh-plan-run-filter-meta">
            <span>
              {filterSummary.length > 0
                ? `当前筛选：${filterSummary.join(' · ')}`
                : '当前筛选：默认展示全部批次记录，并按批次ID倒序显示'}
            </span>
            <span className="olh-plan-run-filter-hint">计划ID 对应计划本身 ID；结果ID 对应左侧第一列。</span>
          </div>
          <div className="olh-plan-run-filter-console">
            <Space wrap size={8}>
              <Input
                allowClear
                placeholder="请输入计划名称"
                prefix={<SearchOutlined />}
                style={{ width: 220 }}
                value={draftFilters.plan_name}
                onChange={event => setDraftFilters(prev => ({ ...prev, plan_name: event.target.value }))}
                onPressEnter={handleSearch}
              />
              <Input
                allowClear
                placeholder="请输入计划ID（不是结果ID）"
                style={{ width: 120 }}
                value={draftFilters.plan_id}
                onChange={event => setDraftFilters(prev => ({ ...prev, plan_id: event.target.value }))}
                onPressEnter={handleSearch}
              />
              <Input
                allowClear
                placeholder="请输入结果ID"
                style={{ width: 120 }}
                value={draftFilters.plan_run_id}
                onChange={event => setDraftFilters(prev => ({ ...prev, plan_run_id: event.target.value }))}
                onPressEnter={handleSearch}
              />
              <Input
                allowClear
                placeholder="请输入执行人"
                style={{ width: 140 }}
                value={draftFilters.created_by_name}
                onChange={event => setDraftFilters(prev => ({ ...prev, created_by_name: event.target.value }))}
                onPressEnter={handleSearch}
              />
              <Select
                allowClear
                placeholder="请选择状态"
                style={{ width: 130 }}
                options={PLAN_RUN_STATUS_OPTIONS}
                value={draftFilters.status}
                onChange={value => setDraftFilters(prev => ({ ...prev, status: value }))}
              />
              <Button type="primary" icon={<SearchOutlined />} onClick={handleSearch} loading={isLoading}>
                查询
              </Button>
              <Button icon={<ReloadOutlined />} onClick={handleReset}>
                重置
              </Button>
            </Space>
            <div className="olh-plan-run-quick-filters" aria-label="批次快捷筛选">
              <button
                type="button"
                className={draftFilters.status === 'running' ? 'olh-plan-run-quick-chip olh-plan-run-quick-chip--active' : 'olh-plan-run-quick-chip'}
                onClick={() => {
                  setDraftFilters(prev => ({ ...prev, status: 'running' }))
                  setFilters(prev => ({ ...prev, status: 'running' }))
                  setPagination(prev => ({ ...prev, current: 1 }))
                }}
              >
                运行中
              </button>
              <button
                type="button"
                className={draftFilters.status === 'failed' ? 'olh-plan-run-quick-chip olh-plan-run-quick-chip--danger' : 'olh-plan-run-quick-chip'}
                onClick={() => {
                  setDraftFilters(prev => ({ ...prev, status: 'failed' }))
                  setFilters(prev => ({ ...prev, status: 'failed' }))
                  setPagination(prev => ({ ...prev, current: 1 }))
                }}
              >
                失败
              </button>
            </div>
          </div>
          {showAdvancedFilters ? (
            <Space direction="vertical" size={4} style={{ display: 'flex' }}>
              <Space wrap size={8}>
                <Select
                  allowClear
                  placeholder="请选择业务线"
                  style={{ width: 150 }}
                  options={businessOptions.map(item => ({ label: item.name || item.code, value: item.code }))}
                  value={draftFilters.business_line}
                  onChange={value => setDraftFilters(prev => ({ ...prev, business_line: value }))}
                />
                <Select
                  allowClear
                  placeholder="请选择环境（平台元数据）"
                  style={{ width: 220 }}
                  options={envOptions}
                  value={draftFilters.env}
                  onChange={value => setDraftFilters(prev => ({ ...prev, env: value }))}
                />
                <Input
                  allowClear
                  placeholder="请输入引擎，如 k6 / jmeter"
                  style={{ width: 200 }}
                  value={draftFilters.engine_type}
                  onChange={event => setDraftFilters(prev => ({ ...prev, engine_type: event.target.value }))}
                  onPressEnter={handleSearch}
                />
                <Select
                  allowClear
                  placeholder="按轮次数量筛选"
                  style={{ width: 150 }}
                  options={ROUND_MODE_FILTER_OPTIONS}
                  value={draftFilters.round_mode}
                  onChange={value => setDraftFilters(prev => ({ ...prev, round_mode: value }))}
                />
                <RangePicker
                  showTime
                  style={{ width: 320 }}
                  value={draftFilters.started_at}
                  onChange={value => setDraftFilters(prev => ({ ...prev, started_at: value ? [value[0]!, value[1]!] : undefined }))}
                />
              </Space>
              <Text type="secondary">
                说明：环境来自平台环境元数据；引擎支持手动输入关键字；轮次筛选当前按单轮/多轮归类，不代表系统只存在这两种执行配置。
              </Text>
            </Space>
          ) : null}
        </Space>
      </Card>

      <div className="olh-table-shell olh-plan-run-table-shell">
        <div className="olh-data-section-heading">
          <div>
            <div className="olh-section-title">批次执行记录</div>
            <div className="olh-section-subtitle">
              批次模板、结论摘要、时间估算和停止入口集中在主表。
            </div>
          </div>
          <Space wrap size={8}>
            <span className="olh-data-chip">{`总计 ${data?.total ?? 0}`}</span>
            <span className="olh-data-chip">{`运行中 ${pageSummary.running}`}</span>
            <span className="olh-data-chip">{`失败 ${pageSummary.failed}`}</span>
          </Space>
        </div>
        <DataTable<PlanRun>
          rowKey="plan_run_id"
          columns={columns}
          dataSource={sortedItems}
          loading={isLoading}
          scroll={{ x: 1180 }}
          size="small"
          className="olh-plan-run-table"
          rowClassName={record => `olh-plan-run-table-row olh-plan-run-table-row--${record.status}`}
          locale={{
            emptyText: (
              <div className="olh-plan-empty-state">
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前筛选下暂无批次记录" />
                <Space wrap className="olh-task-empty-actions">
                  <Button size="small" onClick={handleReset}>清空筛选</Button>
                  <Button size="small" onClick={() => navigate('/plans')}>查看批次模板</Button>
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
          onChange={paginationInfo => {
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

export default PlanRunList
