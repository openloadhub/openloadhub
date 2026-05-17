import { useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, Collapse, Empty, InputNumber, Modal, Radio, Segmented, Space, Table, Tag, Typography, message } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { DownloadOutlined, EyeOutlined, LinkOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { formatK6ControlActionError, formatK6ControlReason, formatK6ControlReasonList } from '@/utils/k6ControlReason'
import { formatLocalDateTime } from '@/utils/localDateTime'
import { publicAlphaFeatures } from '@/config/publicAlpha'

import { StatusBadge } from '@/components/common'
import RunDetail from '@/pages/RunDetail'
import { planApi, type PlanRun, type PlanRunAdvisorySummary, type PlanRunK6BroadcastAcceptedResponse, type PlanRunK6BroadcastResponse, type PlanRunReportSummary } from '@/services/planApi'
import { reportApi } from '@/services/reportApi'
import { runApi, type MetricsSeries, type RunDashboardLink, type SummaryMetricsResponse } from '@/services/runApi'

const { Text } = Typography
const { Panel } = Collapse

type PlanTaskExecRecord = NonNullable<PlanRun['task_exec_records']>[number]
type BatchK6BroadcastMode = 'ratio' | 'total_tps'
type BatchK6TargetSnapshot = {
  ratio: number
  targetTotalTps: number
}
type BatchK6BroadcastSubmitInput = BatchK6TargetSnapshot & {
  mode: BatchK6BroadcastMode
  previousTarget: BatchK6TargetSnapshot | null
}
type BatchEngineDashboardGroup = {
  engine: string
  runIds: number[]
  seedRunId: number
}

const PLAN_EXEC_TYPE_LABELS: Record<string, string> = {
  manual: '手动触发',
  cron: '周期执行',
  fixed: '单次定时',
}

const EXEC_STATUS_COLORS: Record<string, string> = {
  succeeded: 'green',
  completed: 'green',
  running: 'processing',
  preparing: 'processing',
  failed: 'red',
  stopped: 'orange',
  interrupted: 'orange',
  pending: 'default',
}

const ROUND_STATUS_LABELS: Record<string, string> = {
  preparing: '准备中',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  stopped: '已停止',
  suspended: '暂停',
  duplicated: '重复执行',
}

const REPORT_RESOLUTION_LABELS: Record<string, string> = {
  ready: '可下载',
  template_fallback: '待刷新模板',
  missing: '缺报告',
  not_applicable: '不适用',
}

const ADVISORY_VERDICT_LABELS: Record<string, string> = {
  pass: '通过候选',
  warn: '需复核',
  fail: '优先处理',
}

const ADVISORY_DELIVERY_STATUS_COLORS: Record<string, string> = {
  ready_for_review: 'green',
  needs_attention: 'orange',
  blocked: 'red',
}

const ACTIVE_PLAN_RUN_STATUSES = new Set<PlanRun['status']>(['preparing', 'running'])
const PLAN_RUN_DETAIL_ACTIVE_POLL_MS = 5_000
const BATCH_K6_LARGE_ADJUSTMENT_THRESHOLD = 0.5
const TERMINAL_TASK_STATUSES = new Set(['succeeded', 'completed', 'failed', 'stopped'])
const TASK_RECORD_STATUS_PRIORITY: Record<string, number> = {
  failed: 0,
  running: 1,
  preparing: 2,
  stopped: 3,
  pending: 4,
  succeeded: 5,
  completed: 5,
}

const isActivePlanRunStatus = (status?: PlanRun['status'] | null): boolean =>
  Boolean(status && ACTIVE_PLAN_RUN_STATUSES.has(status))

const isProblemTaskStatus = (status?: string | null): boolean => {
  const normalized = String(status || '').toLowerCase()
  return normalized === 'failed'
}

const isActiveTaskStatus = (status?: string | null): boolean => {
  const normalized = String(status || '').toLowerCase()
  return normalized === 'running' || normalized === 'preparing'
}

const supportsBatchK6Control = (
  engineType?: string | null,
  status?: string | null,
  runId?: number | null,
): boolean => {
  return Boolean(
    runId &&
    String(engineType || '').trim().toLowerCase() === 'k6' &&
    isActiveTaskStatus(status),
  )
}

const parsePositiveNumeric = (value: unknown): number | null => {
  if (value == null || value === '' || typeof value === 'boolean') {
    return null
  }
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null
}

const parseFiniteNumeric = (value: unknown): number | null => {
  if (value == null || value === '' || typeof value === 'boolean') {
    return null
  }
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

const extractRunParamNumber = (
  params: Record<string, unknown> | null | undefined,
  keys: string[],
): number | null => {
  if (!params) {
    return null
  }
  for (const key of keys) {
    const parsed = parsePositiveNumeric(params[key])
    if (parsed != null) {
      return parsed
    }
  }
  return null
}

const extractBatchBaseTargetTps = (record: PlanTaskExecRecord): number | null => {
  return extractRunParamNumber(record.run_params, [
    'target_tps',
    'total_tps',
    'fixed_tps',
    'TARGET_TPS',
  ])
}

const roundTo = (value: number, digits = 4): number => {
  const factor = 10 ** digits
  return Math.round(value * factor) / factor
}

const formatDuration = (value?: number | null): string => {
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
  const humanReadable = segments.slice(0, 2).join(' ')
  return `${humanReadable}（${totalSeconds}s）`
}

const formatMetricValue = (value?: number | null, suffix = '', digits = 1): string => {
  if (value == null || Number.isNaN(Number(value))) {
    return '-'
  }
  return `${Number(value).toFixed(digits)}${suffix}`
}

const buildBatchK6TargetSnapshot = (
  ratio?: number | null,
  targetTotalTps?: number | null,
  baseTotalTps?: number | null,
): BatchK6TargetSnapshot | null => {
  const parsedRatio = parsePositiveNumeric(ratio)
  const parsedTarget = parsePositiveNumeric(targetTotalTps)
  if (parsedRatio == null) {
    return null
  }
  if (parsedTarget != null) {
    return {
      ratio: parsedRatio,
      targetTotalTps: roundTo(parsedTarget, 4),
    }
  }
  const parsedBase = parsePositiveNumeric(baseTotalTps)
  if (parsedBase == null) {
    return null
  }
  return {
    ratio: parsedRatio,
    targetTotalTps: roundTo(parsedBase * parsedRatio, 4),
  }
}

const isValidMetricValue = (value: number | null | undefined): value is number =>
  typeof value === 'number' && Number.isFinite(value)

const formatLiveMetricValue = (
  value: number | null | undefined,
  status?: string | null,
  suffix = '',
  digits = 1,
): string => {
  if (value != null && !Number.isNaN(Number(value))) {
    return formatMetricValue(value, suffix, digits)
  }
  return isActiveTaskStatus(status) ? '观测中' : '-'
}

const formatPercentValue = (value?: number | null): string => {
  if (value == null || Number.isNaN(Number(value))) {
    return '-'
  }
  return `${(Number(value) * 100).toFixed(1)}%`
}

const getLatestSeriesValue = (series?: MetricsSeries | null): number | null => {
  if (!series?.points?.length) {
    return null
  }

  for (let index = series.points.length - 1; index >= 0; index -= 1) {
    const point = series.points[index]
    if (isValidMetricValue(point?.value)) {
      return point.value
    }
  }

  return null
}

const aggregateSummaryMetrics = (
  items?: SummaryMetricsResponse['items'] | null,
): {
  total_requests: number | null
  throughput: number | null
  avg_rt_ms: number | null
  p95_rt_ms: number | null
  p99_rt_ms: number | null
} | null => {
  if (!items?.length) {
    return null
  }

  const overallRow = items.find(item => item.endpoint_name === 'overall')
  const sourceItems = items.filter(item => item.endpoint_name !== 'overall')
  const metricItems = sourceItems.length > 0 ? sourceItems : items
  const weighted = (key: 'avg_rt_ms' | 'p95_rt_ms' | 'p99_rt_ms') => {
    const candidates = metricItems.filter(
      (item): item is typeof item & { [K in typeof key]: number } => isValidMetricValue(item[key]),
    )
    if (candidates.length === 0) {
      return null
    }
    const weightedTotal = candidates.reduce((sum, item) => sum + (item.total_requests || 0), 0)
    if (weightedTotal > 0) {
      return candidates.reduce((sum, item) => sum + (item[key] as number) * (item.total_requests || 0), 0) / weightedTotal
    }
    return candidates.reduce((sum, item) => sum + (item[key] as number), 0) / candidates.length
  }

  const totalRequests = metricItems.reduce((sum, item) => sum + (item.total_requests || 0), 0)
  const throughput = metricItems.reduce((sum, item) => sum + (item.throughput || 0), 0)

  return {
    total_requests:
      overallRow?.total_requests
      ?? (totalRequests > 0 ? totalRequests : null),
    throughput:
      overallRow?.throughput
      ?? (throughput > 0 ? throughput : null),
    avg_rt_ms: overallRow?.avg_rt_ms ?? weighted('avg_rt_ms'),
    p95_rt_ms: overallRow?.p95_rt_ms ?? weighted('p95_rt_ms'),
    p99_rt_ms: overallRow?.p99_rt_ms ?? weighted('p99_rt_ms'),
  }
}

const resolveErrorRateFromParams = (params: Record<string, unknown> | null | undefined): number | null => {
  if (!params) {
    return null
  }

  const k6Summary = params.k6_summary
  if (!k6Summary || typeof k6Summary !== 'object') {
    return null
  }

  return parseFiniteNumeric((k6Summary as Record<string, unknown>).error_rate)
}

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
  taskExecRecords: PlanTaskExecRecord[],
  nowMs: number,
): { remainingSeconds: number; estimatedEndAt: string; estimatedTotalSeconds: number } | null => {
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
      estimatedTotalSeconds,
    }
  }

  if ((record.round_total ?? 1) > 1) {
    return null
  }

  const totalUnits = taskExecRecords.length || Math.max((record.task_total ?? 0) + (record.postprocessor_total ?? 0), 0)
  if (totalUnits <= 0) {
    return null
  }

  const completedUnits = taskExecRecords.filter(item => TERMINAL_TASK_STATUSES.has(String(item.status || '').toLowerCase())).length
  if (completedUnits <= 0 || completedUnits >= totalUnits) {
    return null
  }

  const progressRatio = completedUnits / totalUnits
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
    estimatedTotalSeconds,
  }
}

const renderOperator = (operatorName?: string | null, createdBy?: number | null): string => {
  if (operatorName) {
    return operatorName
  }
  return createdBy ? `user#${createdBy}` : '-'
}

const renderListSummary = (items?: Array<string | number> | null): string => {
  if (!items || items.length === 0) {
    return '-'
  }
  return items.join(' / ')
}

const TASK_LOGIC_LABELS: Record<string, string> = {
  task: '任务',
  postprocessor: '后置处理',
}

const resolvePlanRunStatusLabel = (status?: string | null): string => {
  if (!status) {
    return '-'
  }
  return ROUND_STATUS_LABELS[status] || status
}

const summaryGridStyle = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))',
  gap: 12,
}

const summaryPanelStyle = {
  border: '1px solid var(--border-subtle)',
  borderRadius: 8,
  padding: 12,
  background: 'var(--card-bg)',
}

const compactSummaryPanelStyle = {
  border: '1px solid var(--border-subtle)',
  borderRadius: 8,
  padding: 10,
  background: 'var(--card-bg)',
}

const summaryMetricValueStyle = {
  fontSize: 18,
  fontWeight: 600,
  lineHeight: 1.4,
}

const selectedRunShellStyle = {
  marginBottom: 16,
  border: '1px solid var(--border-subtle)',
  borderRadius: 12,
  background: 'var(--card-bg)',
  overflow: 'hidden',
}

const buildInlineSummary = (segments: Array<string | null | undefined>): string =>
  segments.filter((item): item is string => Boolean(item)).join(' · ')

const buildRecordMetricSummary = (record: PlanTaskExecRecord): string | null => {
  const summary = buildInlineSummary([
    typeof record.total_requests === 'number' ? `总请求 ${record.total_requests}` : undefined,
    typeof record.rps === 'number' ? `TPS ${formatMetricValue(record.rps, '', 1)}` : undefined,
    typeof record.avg_rt_ms === 'number' ? `平均 ${formatMetricValue(record.avg_rt_ms, 'ms', 1)}` : undefined,
    typeof record.p95_rt_ms === 'number' ? `P95 ${formatMetricValue(record.p95_rt_ms, 'ms', 1)}` : undefined,
    typeof record.p99_rt_ms === 'number' ? `P99 ${formatMetricValue(record.p99_rt_ms, 'ms', 1)}` : undefined,
  ])
  return summary || null
}

const collectBatchRunIds = (
  records: PlanTaskExecRecord[],
  predicate?: (record: PlanTaskExecRecord) => boolean,
): number[] => {
  return Array.from(
    new Set(
      records
        .filter(record => (predicate ? predicate(record) : true))
        .map(record => Number(record.run_id || 0))
        .filter(runId => Number.isFinite(runId) && runId > 0),
    ),
  ).sort((left, right) => left - right)
}

const collectBatchEngineDashboardGroups = (records: PlanTaskExecRecord[]): BatchEngineDashboardGroup[] => {
  const byEngine = new Map<string, number[]>()
  records.forEach(record => {
    const engine = String(record.engine_type || '').trim().toLowerCase()
    const runId = Number(record.run_id || 0)
    if (!engine || !Number.isFinite(runId) || runId <= 0) {
      return
    }
    byEngine.set(engine, [...(byEngine.get(engine) || []), runId])
  })

  return Array.from(byEngine.entries())
    .map(([engine, runIds]) => {
      const uniqueRunIds = Array.from(new Set(runIds)).sort((left, right) => left - right)
      return {
        engine,
        runIds: uniqueRunIds,
        seedRunId: uniqueRunIds[0],
      }
    })
    .filter(group => group.runIds.length > 0)
    .sort((left, right) => {
      const priority: Record<string, number> = { k6: 0, jmeter: 1 }
      return (priority[left.engine] ?? 99) - (priority[right.engine] ?? 99) || left.engine.localeCompare(right.engine)
    })
}

const buildBatchDashboardUrl = (
  seedUrl: string | null | undefined,
  mode: 'engine' | 'pod',
  runIds: number[],
  engine?: string | null,
): string | null => {
  if (!seedUrl || runIds.length === 0) {
    return null
  }

  try {
    const url = new URL(seedUrl)
    if (mode === 'engine') {
      if (String(engine || '').trim().toLowerCase() === 'jmeter') {
        url.searchParams.set('var-run_id', runIds.join('|'))
        url.searchParams.delete('var-recordId')
      } else {
        url.searchParams.set('var-recordId', runIds.join('|'))
        url.searchParams.delete('var-run_id')
      }
      return url.toString()
    }

    url.searchParams.set('var-run_id', runIds.join('|'))
    url.searchParams.set('var-compose_service', '.*')
    url.searchParams.set('var-container_hint', '__no_container_hint__')
    url.searchParams.delete('var-node_label')
    url.searchParams.delete('var-agent_host')
    return url.toString()
  } catch {
    return null
  }
}

const PlanRunDetail = () => {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const publicAlphaMode = publicAlphaFeatures.publicAlphaMode
  const dynamicK6ControlEnabled = publicAlphaFeatures.dynamicK6Control
  const { planRunId } = useParams<{ planRunId: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const planRunIdNum = Number(planRunId)
  const selectedRound = Number(searchParams.get('round') || 1)
  const selectedCollectionId = Number(searchParams.get('collection_id') || 0) || undefined
  const [nowMs, setNowMs] = useState(() => Date.now())
  const [batchK6BroadcastMode, setBatchK6BroadcastMode] = useState<BatchK6BroadcastMode>('ratio')
  const [batchK6RatioInput, setBatchK6RatioInput] = useState<number | null>(null)
  const [batchK6TotalTpsInput, setBatchK6TotalTpsInput] = useState<number | null>(null)
  const [lastBatchK6BroadcastResult, setLastBatchK6BroadcastResult] = useState<PlanRunK6BroadcastResponse | null>(null)
  const [pendingBatchK6BroadcastTask, setPendingBatchK6BroadcastTask] = useState<PlanRunK6BroadcastAcceptedResponse | null>(null)
  const [currentBatchK6Target, setCurrentBatchK6Target] = useState<BatchK6TargetSnapshot | null>(null)
  const [rollbackBatchK6Target, setRollbackBatchK6Target] = useState<BatchK6TargetSnapshot | null>(null)
  const [fullRunDetailExpanded, setFullRunDetailExpanded] = useState(false)

  const { data, isLoading, error, dataUpdatedAt } = useQuery({
    queryKey: ['plan-run', planRunIdNum, selectedRound, selectedCollectionId ?? null],
    queryFn: () => planApi.getPlanRunDetail(planRunIdNum, { round: selectedRound, collection_id: selectedCollectionId }),
    enabled: Number.isFinite(planRunIdNum),
    refetchInterval: current =>
      isActivePlanRunStatus(current?.state.data?.status)
        ? PLAN_RUN_DETAIL_ACTIVE_POLL_MS
        : false,
  })

  const stopMutation = useMutation({
    mutationFn: (targetPlanRunId: number) => planApi.stopPlanRun(targetPlanRunId),
    onSuccess: () => {
      message.success('已停止批次执行')
      queryClient.invalidateQueries({ queryKey: ['plan-run', planRunIdNum] })
      queryClient.invalidateQueries({ queryKey: ['plan-runs'] })
    },
    onError: (mutationError: unknown) => {
      message.error(mutationError instanceof Error ? mutationError.message : '停止失败')
    },
  })

  useEffect(() => {
    setCurrentBatchK6Target(null)
    setRollbackBatchK6Target(null)
  }, [planRunIdNum, selectedRound])

  useEffect(() => {
    setFullRunDetailExpanded(false)
  }, [planRunIdNum, selectedRound, selectedCollectionId])

  const taskExecRecords = useMemo(() => data?.task_exec_records ?? [], [data?.task_exec_records])
  const taskRecordStats = useMemo(() => {
    const linkedRunTotal = taskExecRecords.filter(item => Boolean(item.run_id)).length
    const failedTotal = taskExecRecords.filter(item => isProblemTaskStatus(item.status)).length
    const runningTotal = taskExecRecords.filter(item => isActiveTaskStatus(item.status)).length
    const pendingTotal = taskExecRecords.filter(item => String(item.status || '').toLowerCase() === 'pending').length
    const unlinkedTotal = taskExecRecords.filter(item => !item.run_id).length
    const taskTotal = taskExecRecords.filter(item => String(item.logic_type || '').toLowerCase() !== 'postprocessor').length
    const postprocessorTotal = taskExecRecords.filter(item => String(item.logic_type || '').toLowerCase() === 'postprocessor').length
    return {
      total: taskExecRecords.length,
      linkedRunTotal,
      failedTotal,
      runningTotal,
      pendingTotal,
      unlinkedTotal,
      taskTotal,
      postprocessorTotal,
    }
  }, [taskExecRecords])
  const batchK6Rows = useMemo(() => {
    return taskExecRecords
      .filter(record => supportsBatchK6Control(record.engine_type, record.status, record.run_id))
      .map(record => ({
        collectionId: record.collection_id ?? null,
        runId: Number(record.run_id || 0),
        taskName: record.task_name || '-',
        env: record.env || null,
        engineType: record.engine_type || null,
        currentTps: typeof record.rps === 'number' ? record.rps : null,
        baseTargetTps: extractBatchBaseTargetTps(record),
        status: record.status || null,
      }))
  }, [taskExecRecords])
  const batchK6BaseTotalTps = useMemo(
    () => roundTo(batchK6Rows.reduce((sum, item) => sum + (item.baseTargetTps || 0), 0), 4),
    [batchK6Rows],
  )
  const batchK6ObservedTotalTps = useMemo(
    () => {
      const values = batchK6Rows
        .map(item => item.currentTps)
        .filter((value): value is number => value != null && !Number.isNaN(Number(value)))
      if (!values.length) {
        return null
      }
      return roundTo(values.reduce((sum, value) => sum + value, 0), 4)
    },
    [batchK6Rows],
  )
  const batchK6ControllableRows = useMemo(
    () => batchK6Rows.filter(item => item.baseTargetTps != null && item.baseTargetTps > 0),
    [batchK6Rows],
  )
  const batchK6MissingBaseRows = useMemo(
    () => batchK6Rows.filter(item => item.baseTargetTps == null),
    [batchK6Rows],
  )
  const batchK6ResolvedRatio = useMemo(() => {
    if (batchK6BroadcastMode === 'ratio') {
      return parsePositiveNumeric(batchK6RatioInput)
    }
    if (batchK6BaseTotalTps <= 0) {
      return null
    }
    const totalTps = parsePositiveNumeric(batchK6TotalTpsInput)
    if (totalTps == null) {
      return null
    }
    return totalTps / batchK6BaseTotalTps
  }, [batchK6BaseTotalTps, batchK6BroadcastMode, batchK6RatioInput, batchK6TotalTpsInput])
  const batchK6PreviewRows = useMemo(() => {
    const ratio = batchK6ResolvedRatio
    return batchK6Rows.map(item => ({
      ...item,
      previewTargetTps: ratio != null && item.baseTargetTps != null
        ? roundTo(item.baseTargetTps * ratio, 4)
        : null,
    }))
  }, [batchK6ResolvedRatio, batchK6Rows])
  const batchK6PreviewTotalTps = useMemo(
    () => roundTo(batchK6PreviewRows.reduce((sum, item) => sum + (item.previewTargetTps || 0), 0), 4),
    [batchK6PreviewRows],
  )
  const resolvedCurrentBatchK6Target = useMemo<BatchK6TargetSnapshot | null>(() => {
    if (currentBatchK6Target) {
      return currentBatchK6Target
    }
    if (batchK6BaseTotalTps <= 0) {
      return null
    }
    return {
      ratio: 1,
      targetTotalTps: batchK6BaseTotalTps,
    }
  }, [batchK6BaseTotalTps, currentBatchK6Target])
  const filteredTaskExecRecords = useMemo(() => {
    const filtered = [...taskExecRecords]
    filtered.sort((left, right) => {
      const leftPriority = TASK_RECORD_STATUS_PRIORITY[String(left.status || 'pending').toLowerCase()] ?? 999
      const rightPriority = TASK_RECORD_STATUS_PRIORITY[String(right.status || 'pending').toLowerCase()] ?? 999
      if (leftPriority !== rightPriority) {
        return leftPriority - rightPriority
      }
      if ((left.stage || 0) !== (right.stage || 0)) {
        return (left.stage || 0) - (right.stage || 0)
      }
      return (left.collection_id || 0) - (right.collection_id || 0)
    })
    return filtered
  }, [taskExecRecords])

  useEffect(() => {
    if (!filteredTaskExecRecords.length) {
      return
    }
    const currentExists = filteredTaskExecRecords.some(item => item.collection_id === selectedCollectionId)
    if (!currentExists && filteredTaskExecRecords[0].collection_id) {
      setSearchParams(prev => {
        const next = new URLSearchParams(prev)
        next.set('round', String(selectedRound))
        next.set('collection_id', String(filteredTaskExecRecords[0].collection_id))
        return next
      }, { replace: true })
    }
  }, [filteredTaskExecRecords, selectedCollectionId, selectedRound, setSearchParams])

  const selectedRecord = useMemo(() => {
    if (!taskExecRecords.length) {
      return null
    }
    return (
      taskExecRecords.find(item => item.collection_id === selectedCollectionId) ??
      filteredTaskExecRecords[0] ??
      taskExecRecords[0]
    )
  }, [filteredTaskExecRecords, selectedCollectionId, taskExecRecords])

  const batchLinkedRunIds = useMemo(
    () => collectBatchRunIds(taskExecRecords),
    [taskExecRecords],
  )
  const batchEngineDashboardGroups = useMemo(
    () => collectBatchEngineDashboardGroups(taskExecRecords),
    [taskExecRecords],
  )
  const batchDashboardSeedRunId = batchLinkedRunIds[0] ?? null
  const batchEngineDashboardSeedRunIds = useMemo(
    () => batchEngineDashboardGroups.map(group => group.seedRunId),
    [batchEngineDashboardGroups],
  )
  const executionSummary = data?.execution_summary
  const selectedRoundSummary = data?.selected_round_summary
  const selectedTaskSummaryData = data?.selected_task_summary
  const selectedRunSummaryData = data?.selected_run_summary
  const activeCollectionId = selectedCollectionId ?? selectedRecord?.collection_id ?? selectedTaskSummaryData?.collection_id
  const selectedRunId = selectedRecord?.run_id
  const selectedTaskSummary = useMemo(() => {
    if (!selectedRecord || !selectedTaskSummaryData?.collection_id) {
      return null
    }
    return selectedTaskSummaryData.collection_id === selectedRecord.collection_id
      ? selectedTaskSummaryData
      : null
  }, [selectedRecord, selectedTaskSummaryData])
  const selectedRunSummary = useMemo(() => {
    if (!selectedRunId || !selectedRunSummaryData?.run_id) {
      return null
    }
    return selectedRunSummaryData.run_id === selectedRunId
      ? selectedRunSummaryData
      : null
  }, [selectedRunId, selectedRunSummaryData])
  const selectedRunStatus = selectedRunSummary?.status || selectedTaskSummary?.status || selectedRecord?.status
  const isSelectedRunActive = isActiveTaskStatus(selectedRunStatus)
  const isPlanRunActive = isActivePlanRunStatus(data?.status)
  const planRunElapsedSeconds = resolveElapsedSeconds(
    data?.started_at,
    data?.ended_at,
    data?.duration_seconds,
    nowMs,
  )
  const planRunEta = data ? estimatePlanRunEta(data, taskExecRecords, nowMs) : null
  const lastBatchMetricsSyncedAt = dataUpdatedAt
    ? formatLocalDateTime(new Date(dataUpdatedAt).toISOString())
    : '-'

  useEffect(() => {
    if (!isPlanRunActive) {
      return
    }
    const timer = window.setInterval(() => {
      setNowMs(Date.now())
    }, 1000)
    return () => window.clearInterval(timer)
  }, [isPlanRunActive])

  const { data: runDetail, isLoading: runDetailLoading } = useQuery({
    queryKey: ['plan-run-detail-selected-run', selectedRunId],
    queryFn: () => runApi.getRunDetail(selectedRunId as number),
    enabled: Boolean(selectedRunId && fullRunDetailExpanded),
    refetchInterval:
      selectedRunId && fullRunDetailExpanded && isSelectedRunActive
        ? PLAN_RUN_DETAIL_ACTIVE_POLL_MS
        : false,
  })

  const { data: summaryMetricsData, isLoading: summaryLoading } = useQuery({
    queryKey: ['plan-run-detail-summary-metrics', selectedRunId, selectedRunStatus ?? null],
    queryFn: () => runApi.getSummaryMetrics(selectedRunId as number),
    enabled: Boolean(selectedRunId && isSelectedRunActive),
    refetchInterval: isSelectedRunActive ? PLAN_RUN_DETAIL_ACTIVE_POLL_MS : false,
  })
  const { data: selectedErrorRateMetrics } = useQuery({
    queryKey: ['plan-run-detail-error-rate-metrics', selectedRunId, selectedRunStatus ?? null],
    queryFn: () => runApi.getRunMetrics(selectedRunId as number, { metric: 'error_rate', step_seconds: 10 }),
    enabled: Boolean(selectedRunId && isSelectedRunActive),
    refetchInterval: isSelectedRunActive ? PLAN_RUN_DETAIL_ACTIVE_POLL_MS : false,
  })
  const { data: batchDashboardSeedLinks } = useQuery({
    queryKey: ['plan-run-detail-batch-dashboards', batchDashboardSeedRunId, batchEngineDashboardSeedRunIds],
    enabled: Boolean(batchDashboardSeedRunId || batchEngineDashboardSeedRunIds.length > 0),
    queryFn: async () => {
      const targets = Array.from(
        new Set(
          [batchDashboardSeedRunId, ...batchEngineDashboardSeedRunIds]
            .filter((runId): runId is number => typeof runId === 'number' && runId > 0),
        ),
      )
      const responses = await Promise.all(
        targets.map(async runId => ({
          runId,
          dashboards: await runApi.getRunDashboards(runId),
        })),
      )
      const byRunId = new Map<number, RunDashboardLink[]>(
        responses.map(item => [item.runId, item.dashboards.items]),
      )
      return {
        pod: batchDashboardSeedRunId
          ? byRunId.get(batchDashboardSeedRunId)?.find(item => item.dashboard_type === 'pod_grafana') ?? null
          : null,
        engineByRunId: byRunId,
      }
    },
  })

  const selectedRunControlEnabled = dynamicK6ControlEnabled && supportsBatchK6Control(
    selectedTaskSummary?.engine_type || selectedRecord?.engine_type || selectedRunSummary?.engine_type,
    selectedTaskSummary?.status || selectedRecord?.status || selectedRunSummary?.status,
    selectedRunId,
  )
  const shouldQuerySelectedRunK6Control = selectedRunControlEnabled
  const { data: selectedRunK6Control } = useQuery({
    queryKey: ['plan-run-detail-selected-run-k6-control', selectedRunId],
    queryFn: () => runApi.getRunK6Control(selectedRunId as number),
    enabled: dynamicK6ControlEnabled && shouldQuerySelectedRunK6Control,
    refetchInterval: shouldQuerySelectedRunK6Control && isSelectedRunActive ? PLAN_RUN_DETAIL_ACTIVE_POLL_MS : false,
  })
  const { data: pendingBatchK6BroadcastStatus } = useQuery({
    queryKey: ['plan-run-detail-batch-k6-broadcast-task', planRunIdNum, pendingBatchK6BroadcastTask?.async_task_id],
    queryFn: () => planApi.getAsyncPlanRunK6ControlTask(planRunIdNum, pendingBatchK6BroadcastTask?.async_task_id || ''),
    enabled: dynamicK6ControlEnabled && Boolean(pendingBatchK6BroadcastTask?.async_task_id),
    refetchInterval: current =>
      current?.state.data?.completed
        ? false
        : 2_000,
  })

  const reportSummary = data?.report_summary
  const advisorySummary = data?.advisory_summary
  const showPlanRunInsightPanel = !publicAlphaMode && Boolean(advisorySummary || reportSummary)
  const readyReportItems = useMemo(
    () => (reportSummary?.items || []).filter(item => item.resolution_status === 'ready' && item.report_id),
    [reportSummary?.items],
  )
  const firstReadyReportItem = readyReportItems[0]
  const selectedReportItem = useMemo(() => {
    if (!reportSummary?.items?.length) {
      return null
    }
    if (activeCollectionId) {
      const matched = reportSummary.items.find(item => item.collection_id === activeCollectionId)
      if (matched) {
        return matched
      }
    }
    if (selectedRunId) {
      return reportSummary.items.find(item => item.run_id === selectedRunId) ?? null
    }
    return null
  }, [activeCollectionId, reportSummary?.items, selectedRunId])
  const selectedReportStatus = selectedReportItem?.resolution_status ?? null
  const selectedReportMessage = selectedReportItem?.message ?? null
  const downloadableReportId = selectedReportStatus === 'ready'
    ? selectedReportItem?.report_id ?? null
    : null
  const selectedRunStatusLabel =
    selectedRunSummary?.status_label ||
    selectedTaskSummary?.status_label ||
    selectedRecord?.status_label ||
    resolvePlanRunStatusLabel(selectedRunSummary?.status || selectedTaskSummary?.status || selectedRecord?.status)
  const selectedRunStatusTagColor =
    EXEC_STATUS_COLORS[selectedRunSummary?.status || selectedTaskSummary?.status || selectedRecord?.status || 'pending'] || 'default'
  const selectedRunStopReason = runDetail?.stop_reason
  const selectedRunK6Summary = selectedRunK6Control?.summary
  const batchEngineGrafanaLinks = useMemo(
    () => batchEngineDashboardGroups
      .map(group => {
        const dashboard = batchDashboardSeedLinks?.engineByRunId
          ?.get(group.seedRunId)
          ?.find(item => item.dashboard_type === 'engine_grafana')
        const url = buildBatchDashboardUrl(dashboard?.url, 'engine', group.runIds, group.engine)
        if (!url) {
          return null
        }
        return {
          engine: group.engine,
          label: `性能指标 ${group.engine.toUpperCase()}-Grafana`,
          url,
        }
      })
      .filter((item): item is { engine: string; label: string; url: string } => Boolean(item)),
    [batchDashboardSeedLinks?.engineByRunId, batchEngineDashboardGroups],
  )
  const batchPodGrafanaUrl = useMemo(
    () => buildBatchDashboardUrl(batchDashboardSeedLinks?.pod?.url, 'pod', batchLinkedRunIds),
    [batchDashboardSeedLinks?.pod?.url, batchLinkedRunIds],
  )
  const selectedRunK6RejectedMessage = useMemo(() => {
    if (!selectedRunK6Control) {
      return null
    }
    if (String(selectedRunK6Control.summary?.controller_status || '').trim().toLowerCase() === 'rejected') {
      return formatK6ControlActionError(selectedRunK6Control.summary.controller_message || '最近一次动态调整未通过 runtime 校验')
    }
    return null
  }, [selectedRunK6Control])
  const selectedRunK6UnavailableMessage = useMemo(() => {
    if (!selectedRunK6Control || selectedRunK6Control.available || !isSelectedRunActive) {
      return null
    }
    if (!selectedRunK6Control.available && selectedRunK6Control.reason) {
      return formatK6ControlReason(selectedRunK6Control.reason)
    }
    const failedAgent = selectedRunK6Control.agents.find(agent => !agent.available && agent.reason)
    return failedAgent?.reason ? formatK6ControlReason(failedAgent.reason) : null
  }, [isSelectedRunActive, selectedRunK6Control])
  const selectedRunK6AdjustingMessage = selectedRunK6Control?.summary?.controller_status === 'adjusting'
    ? selectedRunK6Control.summary.controller_message || '最近一次动态调整仍在收敛中'
    : null
  const planRunLikelyUserStoppedFalseFailure = Boolean(
    data?.status === 'failed' &&
    selectedRunStopReason &&
    selectedRunStopReason.startsWith(`stopped_from_plan_run:${planRunIdNum}`) &&
    taskExecRecords.length > 0 &&
    taskExecRecords.every(item => String(item.status || '').trim().toLowerCase() === 'stopped') &&
    (executionSummary?.failed_total ?? 0) === 0,
  )
  const selectedRunTimeSummary = buildInlineSummary([
    `开始 ${formatLocalDateTime(selectedRunSummary?.started_at || selectedTaskSummary?.started_at || selectedRecord?.started_at)}`,
    `结束 ${formatLocalDateTime(selectedRunSummary?.ended_at || selectedTaskSummary?.ended_at || selectedRecord?.ended_at)}`,
  ])
  const selectedRecordIndex = useMemo(() => {
    if (!selectedRecord) {
      return null
    }
    const resolvedIndex = filteredTaskExecRecords.findIndex(item => item.collection_id === selectedRecord.collection_id)
    return resolvedIndex >= 0 ? resolvedIndex + 1 : null
  }, [filteredTaskExecRecords, selectedRecord])
  const selectedLiveSummaryAggregate = useMemo(
    () => aggregateSummaryMetrics(summaryMetricsData?.items),
    [summaryMetricsData?.items],
  )
  const selectedErrorRateMetricsLatest = useMemo(
    () => getLatestSeriesValue(selectedErrorRateMetrics?.series.find(item => item.metric === 'error_rate')),
    [selectedErrorRateMetrics?.series],
  )
  const selectedRequestsTotal =
    selectedTaskSummary?.total_requests ??
    selectedRunSummary?.total_requests ??
    selectedRecord?.total_requests ??
    runDetail?.total_requests ??
    selectedLiveSummaryAggregate?.total_requests ??
    null
  const selectedObservedTps =
    selectedTaskSummary?.rps ??
    selectedRunSummary?.rps ??
    selectedRecord?.rps ??
    runDetail?.rps ??
    selectedLiveSummaryAggregate?.throughput ??
    null
  const selectedAvgRtMs =
    selectedTaskSummary?.avg_rt_ms ??
    selectedRunSummary?.avg_rt_ms ??
    selectedRecord?.avg_rt_ms ??
    runDetail?.avg_rt_ms ??
    selectedLiveSummaryAggregate?.avg_rt_ms ??
    null
  const selectedP95RtMs =
    selectedTaskSummary?.p95_rt_ms ??
    selectedRunSummary?.p95_rt_ms ??
    selectedRecord?.p95_rt_ms ??
    runDetail?.p95_rt_ms ??
    selectedLiveSummaryAggregate?.p95_rt_ms ??
    null
  const selectedP99RtMs =
    selectedTaskSummary?.p99_rt_ms ??
    selectedRunSummary?.p99_rt_ms ??
    selectedRecord?.p99_rt_ms ??
    runDetail?.p99_rt_ms ??
    selectedLiveSummaryAggregate?.p99_rt_ms ??
    null
  const selectedErrorRate =
    selectedTaskSummary?.error_rate ??
    selectedRunSummary?.error_rate ??
    selectedRecord?.error_rate ??
    runDetail?.error_rate ??
    selectedErrorRateMetricsLatest ??
    resolveErrorRateFromParams(runDetail?.params ?? null) ??
    (isValidMetricValue(runDetail?.success_rate) ? Math.max(0, Math.min(1, 1 - runDetail.success_rate)) : null) ??
    null
  const selectedTaskMetricSummary =
    (summaryMetricsData?.items?.length
      ? `已聚合 ${summaryMetricsData.items.filter(item => item.endpoint_name !== 'overall').length || summaryMetricsData.items.length} 个 endpoint 的实时 summary`
      : null) ||
    (selectedRecord?.status_reason ? selectedRecord.status_reason : null) ||
    null
  const selectedReportSummaryText = selectedReportStatus
    ? `${REPORT_RESOLUTION_LABELS[selectedReportStatus] || selectedReportStatus}${selectedReportMessage ? ` · ${selectedReportMessage}` : ''}`
    : '-'
  const roundItems = useMemo(
    () => data?.rounds ?? [{ round: data?.selected_round || 1, label: `轮次 ${data?.selected_round || 1}` }],
    [data?.rounds, data?.selected_round],
  )
  const hasMultipleRounds = roundItems.length > 1
  const roundSegmentOptions = useMemo(
    () => roundItems.map(item => ({
      label: item.label || `轮次 ${item.round}`,
      value: item.round,
    })),
    [roundItems],
  )
  const selectedRoundInlineSummary = buildInlineSummary([
    selectedRoundSummary?.label || `轮次 ${data?.selected_round || data?.round || 1}`,
    selectedRoundSummary?.status_label || (data?.selected_round_status ? ROUND_STATUS_LABELS[data.selected_round_status] || data.selected_round_status : undefined),
    selectedRoundSummary?.started_at || selectedRoundSummary?.ended_at
      ? `${formatLocalDateTime(selectedRoundSummary?.started_at)} ~ ${formatLocalDateTime(selectedRoundSummary?.ended_at)}`
      : undefined,
  ])
  const focusNotices = useMemo(() => {
    const items: Array<{ key: string; tone: 'warning' | 'info'; title: string; detail: string }> = []
    if (planRunLikelyUserStoppedFalseFailure) {
      items.push({
        key: 'false-failed-stop',
        tone: 'warning',
        title: '当前批次更像是“用户停止”而不是任务真实失败',
        detail: `当前选中 Run 的 stop_reason=${selectedRunStopReason}，且本轮明细都以 stopped 收尾；顶层 failed 更像旧 stop 覆写口径。`,
      })
    }
    if (selectedRunK6RejectedMessage) {
      items.push({
        key: 'k6-rejected',
        tone: 'warning',
        title: '最近一次动态调整未通过',
        detail: selectedRunK6Summary?.control_strategy === 'scenario_direct'
          ? `${selectedRunK6RejectedMessage}。当前是 scenario_direct，建议分两到三次逐步上调。`
          : selectedRunK6RejectedMessage,
      })
    }
    if (selectedRunK6UnavailableMessage) {
      items.push({
        key: 'k6-unavailable',
        tone: 'warning',
        title: '控制能力当前不可用',
        detail: selectedRunK6UnavailableMessage,
      })
    }
    if (selectedRunK6AdjustingMessage) {
      items.push({
        key: 'k6-adjusting',
        tone: 'info',
        title: '最近一次动态调整仍在收敛中',
        detail: selectedRunK6AdjustingMessage,
      })
    }
    if (isSelectedRunActive && selectedRunK6Control?.warnings?.length) {
      items.push({
        key: 'k6-warning-detail',
        tone: 'info',
        title: '控制能力存在附加信息',
        detail: formatK6ControlReasonList(selectedRunK6Control.warnings).join('；'),
      })
    }
    return items
  }, [
    isSelectedRunActive,
    planRunLikelyUserStoppedFalseFailure,
    selectedRunK6AdjustingMessage,
    selectedRunK6Control?.warnings,
    selectedRunK6RejectedMessage,
    selectedRunK6Summary?.control_strategy,
    selectedRunStopReason,
    selectedRunK6UnavailableMessage,
  ])
  const handleBackToPlanRunList = () => navigate('/plan-runs')

  const moveSelectionToCollection = (collectionId?: number | null) => {
    if (!collectionId) {
      return
    }
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.set('round', String(selectedRound))
      next.set('collection_id', String(collectionId))
      return next
    })
  }

  const moveSelectionToRound = (round: number) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.set('round', String(round))
      next.delete('collection_id')
      return next
    })
  }

  const handleAdvisoryPrimaryFocus = (focus: PlanRunAdvisorySummary['primary_focus']) => {
    if (!focus) {
      return
    }
    if (focus.kind === 'ready_report' && focus.report_id) {
      navigate(`/reports/${focus.report_id}/view`)
      return
    }
    if (focus.collection_id) {
      moveSelectionToCollection(focus.collection_id)
      return
    }
    if (focus.run_id) {
      navigate(`/runs/${focus.run_id}`)
    }
  }

  const downloadReportManifestMutation = useMutation({
    mutationFn: async () => {
      if (!Number.isFinite(planRunIdNum)) {
        throw new Error('无效的批次记录 ID')
      }
      return planApi.downloadPlanRunReportManifest(planRunIdNum, {
        round: selectedRound,
        collection_id: selectedCollectionId || undefined,
      })
    },
    onSuccess: blob => {
      const url = window.URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.style.display = 'none'
      link.href = url
      link.download = `plan-run-${planRunIdNum}-round-${selectedRound}-report-manifest.txt`
      document.body.appendChild(link)
      link.click()
      window.URL.revokeObjectURL(url)
      document.body.removeChild(link)
      message.success('批次报告清单导出已开始')
    },
    onError: (mutationError: unknown) => {
      message.error(mutationError instanceof Error ? mutationError.message : '导出批次报告清单失败')
    },
  })

  const downloadReportMutation = useMutation({
    mutationFn: async (reportId: number) => {
      const blob = await reportApi.downloadReport(reportId)
      const url = window.URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.style.display = 'none'
      link.href = url
      link.download = `plan-run-${planRunIdNum}-report-${reportId}.html`
      document.body.appendChild(link)
      link.click()
      window.URL.revokeObjectURL(url)
      document.body.removeChild(link)
    },
    onSuccess: () => {
      message.success('报告下载已开始')
    },
    onError: (mutationError: unknown) => {
      message.error(mutationError instanceof Error ? mutationError.message : '下载报告失败')
    },
  })
  const batchK6BroadcastMutation = useMutation({
    mutationFn: async (input: BatchK6BroadcastSubmitInput) => {
      return planApi.submitAsyncPlanRunK6Control(planRunIdNum, {
        selected_round: selectedRound,
        ratio: input.mode === 'ratio' ? input.ratio : undefined,
        target_total_tps: input.mode === 'total_tps' ? input.targetTotalTps : undefined,
        async_mode: true,
      })
    },
    onSuccess: (payload, input) => {
      const acceptedTarget = buildBatchK6TargetSnapshot(payload.ratio, payload.target_total_tps, payload.base_total_tps) ?? {
        ratio: input.ratio,
        targetTotalTps: input.targetTotalTps,
      }
      setPendingBatchK6BroadcastTask(payload)
      setLastBatchK6BroadcastResult(null)
      setCurrentBatchK6Target(acceptedTarget)
      setRollbackBatchK6Target(input.previousTarget)
      message.success(
        `批次广播已提交，倍率 ${roundTo(payload.ratio, 3)}，总 TPS ${formatMetricValue(payload.target_total_tps, '', 1)}，后台处理中...`,
      )
    },
    onError: (mutationError: unknown) => {
      setLastBatchK6BroadcastResult(null)
      setPendingBatchK6BroadcastTask(null)
      message.error(mutationError instanceof Error ? mutationError.message : '批量广播 K6 控制失败')
    },
  })

  const submitBatchK6Broadcast = () => {
    const ratio = batchK6ResolvedRatio
    if (ratio == null || ratio <= 0) {
      message.error('请先填写有效的倍率或总 TPS')
      return
    }
    if (batchK6ControllableRows.length === 0) {
      message.error('当前批次没有可广播控制的运行中 K6 项')
      return
    }
    const targetTotalTps = parsePositiveNumeric(batchK6PreviewTotalTps)
    if (targetTotalTps == null) {
      message.error('当前预览总 TPS 无效')
      return
    }

    const submitInput: BatchK6BroadcastSubmitInput = {
      mode: batchK6BroadcastMode,
      ratio,
      targetTotalTps,
      previousTarget: resolvedCurrentBatchK6Target,
    }
    const targetDeltaRatio = batchK6BaseTotalTps > 0
      ? Math.abs(targetTotalTps - batchK6BaseTotalTps) / batchK6BaseTotalTps
      : 0
    if (targetDeltaRatio >= BATCH_K6_LARGE_ADJUSTMENT_THRESHOLD) {
      Modal.confirm({
        title: '确认大幅调整批次 K6 TPS',
        content: `当前批次基线总 TPS ${formatMetricValue(batchK6BaseTotalTps, '', 1)}，即将广播到 ${formatMetricValue(targetTotalTps, '', 1)}。`,
        okText: '确认广播',
        cancelText: '取消',
        onOk: () => batchK6BroadcastMutation.mutate(submitInput),
      })
      return
    }

    batchK6BroadcastMutation.mutate(submitInput)
  }

  useEffect(() => {
    if (!pendingBatchK6BroadcastTask || !pendingBatchK6BroadcastStatus?.completed) {
      return
    }
    if (pendingBatchK6BroadcastStatus.result) {
      const payload = pendingBatchK6BroadcastStatus.result
      const summary = `已向 ${payload.success_run_total}/${payload.controllable_run_total} 个 K6 Run 广播，倍率 ${roundTo(payload.ratio, 3)}，总 TPS ${formatMetricValue(payload.target_total_tps, '', 1)}`
      setLastBatchK6BroadcastResult(payload)
      const completedTarget = buildBatchK6TargetSnapshot(payload.ratio, payload.target_total_tps, payload.base_total_tps)
      if (completedTarget) {
        setCurrentBatchK6Target(completedTarget)
      }
      if (payload.failed_run_total > 0) {
        message.warning(`${summary}，失败 ${payload.failed_run_total} 个`)
      } else {
        message.success(summary)
      }
      if (payload.warnings.length > 0) {
        message.info(`批次广播补充信息：${formatK6ControlReasonList(payload.warnings).join('；')}`)
      }
      if (selectedRunId) {
        queryClient.invalidateQueries({ queryKey: ['plan-run-detail-selected-run-k6-control', selectedRunId] })
      }
    } else if (pendingBatchK6BroadcastStatus.error) {
      message.error(pendingBatchK6BroadcastStatus.error)
    }
    setPendingBatchK6BroadcastTask(null)
  }, [pendingBatchK6BroadcastStatus, pendingBatchK6BroadcastTask, queryClient, selectedRunId])

  const detailColumns: ColumnsType<PlanTaskExecRecord> = [
    {
      title: '任务',
      dataIndex: 'task_name',
      key: 'task_name',
      width: 280,
      render: (value: string | null | undefined, record) => (
        <Space direction="vertical" size={2}>
          <Space size={6} wrap>
            <span>{value || '-'}</span>
            {record.logic_type === 'postprocessor' ? <Tag>后置处理</Tag> : null}
            {supportsBatchK6Control(record.engine_type, record.status, record.run_id) ? (
              <Tag color="green">K6 可控</Tag>
            ) : null}
          </Space>
          <Text type="secondary">
            {buildInlineSummary([
              record.logic_type_label || TASK_LOGIC_LABELS[record.logic_type],
              record.env ? `环境 ${record.env}` : undefined,
              record.engine_type ? `引擎 ${record.engine_type}` : undefined,
            ])}
          </Text>
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (value: string | null | undefined, record) => (
        <Tag color={EXEC_STATUS_COLORS[value || 'pending']}>
          {record.status_label || resolvePlanRunStatusLabel(value)}
        </Tag>
      ),
    },
    {
      title: '运行快照',
      key: 'run_snapshot',
      width: 260,
      render: (_value: unknown, record) => (
        <Space direction="vertical" size={2}>
          <Text type="secondary">
            {buildInlineSummary([
              record.run_id ? `Run ${record.run_id}` : '未绑定 Run',
              record.env ? `环境 ${record.env}` : undefined,
              record.engine_type ? `引擎 ${record.engine_type}` : undefined,
            ])}
          </Text>
          <Text type="secondary">
            {buildInlineSummary([
              record.run_id ? `阶段 ${record.stage ?? '-'}` : undefined,
              typeof record.rps === 'number' ? `TPS ${formatMetricValue(record.rps, '', 1)}` : undefined,
            ]) || '-'}
          </Text>
        </Space>
      ),
    },
    {
      title: '性能摘要',
      dataIndex: 'metric_summary',
      key: 'metric_summary',
      render: (value: string | null | undefined, record) => {
        if (record.logic_type === 'postprocessor') {
          return value || record.status_reason || (
            typeof record.postprocessor_seconds === 'number'
              ? `sleep ${record.postprocessor_seconds}s`
              : '后置处理无性能指标'
          )
        }
        return value || buildRecordMetricSummary(record) || (record.run_id ? '已关联 Run，等待指标' : '当前无性能指标')
      },
    },
    {
      title: '控制',
      key: 'control',
      width: 120,
      render: (_value: unknown, record) => (
        supportsBatchK6Control(record.engine_type, record.status, record.run_id) ? (
          <Button
            type="link"
            size="small"
            onClick={event => {
              event.stopPropagation()
              navigate(`/runs/${record.run_id}`)
            }}
          >
            打开 K6 控制
          </Button>
        ) : (
          <Text type="secondary">-</Text>
        )
      ),
    },
  ]

  const renderReportSummaryCard = (summary: PlanRunReportSummary | null | undefined) => {
    if (!summary) {
      return null
    }

    return (
      <Card size="small" title="报告就绪度">
        <Space direction="vertical" size={10} style={{ display: 'flex' }}>
          <Space wrap size={[8, 8]}>
            <Tag color={summary.ready_total > 0 ? 'green' : 'default'}>{`可下载 ${summary.ready_total}`}</Tag>
            <Tag>{`已绑定 Run ${summary.linked_run_total}`}</Tag>
            {summary.template_fallback_total > 0 ? <Tag color="orange">{`待刷新模板 ${summary.template_fallback_total}`}</Tag> : null}
            {summary.missing_total > 0 ? <Tag>{`缺报告 ${summary.missing_total}`}</Tag> : null}
          </Space>
          <Text>{summary.conclusion_text}</Text>
          <Text type="secondary">
            报告检查只判断当前批次每个 Run 是否已有可查看报告；缺报告项不是可点击动作，需要先在对应 Run 生成报告，再回到这里复核。
          </Text>
          {summary.selected_run_message ? (
            <Text type="secondary">
              {`当前选中项：${REPORT_RESOLUTION_LABELS[summary.selected_run_status || 'missing'] || summary.selected_run_status || '未就绪'} · ${summary.selected_run_message}`}
            </Text>
          ) : null}
          {summary.missing_total > 0 ? (
            <Alert
              type="warning"
              showIcon
              message={`还有 ${summary.missing_total} 个 Run 缺少可查看报告`}
              description="这是一条状态提示，不是跳转入口。请选择缺报告的任务项进入完整 RunDetail，生成或重建报告后再使用批次报告清单复核。"
            />
          ) : null}
          <Space wrap size={[8, 8]}>
            <Button
              size="small"
              disabled={!firstReadyReportItem?.collection_id}
              onClick={() => moveSelectionToCollection(firstReadyReportItem?.collection_id)}
            >
              定位首个已就绪报告
            </Button>
            <Button
              size="small"
              disabled={!firstReadyReportItem?.report_id}
              icon={<EyeOutlined />}
              onClick={() => {
                if (!firstReadyReportItem?.report_id) {
                  return
                }
                navigate(`/reports/${firstReadyReportItem.report_id}/view`)
              }}
            >
              查看首个已就绪报告
            </Button>
            <Button
              size="small"
              disabled={!firstReadyReportItem?.report_id}
              loading={downloadReportMutation.isPending && downloadableReportId === firstReadyReportItem?.report_id}
              onClick={() => {
                if (!firstReadyReportItem?.report_id) {
                  return
                }
                downloadReportMutation.mutate(firstReadyReportItem.report_id)
              }}
            >
              下载首个已就绪报告
            </Button>
            <Button
              size="small"
              loading={downloadReportManifestMutation.isPending}
              onClick={() => downloadReportManifestMutation.mutate()}
            >
              导出报告清单
            </Button>
          </Space>
        </Space>
      </Card>
    )
  }

  const renderAdvisorySummaryCard = (summary: PlanRunAdvisorySummary | null | undefined) => {
    if (!summary) {
      return null
    }

    const tagColor = summary.verdict === 'pass'
      ? 'success'
      : summary.verdict === 'warn'
        ? 'warning'
        : 'error'

    return (
      <Card size="small" title="批次确定性结论">
        <Space direction="vertical" size={10} style={{ display: 'flex' }}>
          <Space wrap size={[8, 8]}>
            <Tag color={tagColor}>{ADVISORY_VERDICT_LABELS[summary.verdict] || summary.verdict}</Tag>
            {summary.delivery_status ? (
              <Tag color={ADVISORY_DELIVERY_STATUS_COLORS[summary.delivery_status] || 'default'}>
                {summary.delivery_status_label || summary.delivery_status}
              </Tag>
            ) : null}
            {summary.evidence_ready ? <Tag color="green">证据就绪</Tag> : null}
            {summary.input_sources.map(source => (
              <Tag key={source}>{source}</Tag>
            ))}
          </Space>
          <Text>{summary.summary_text}</Text>
          <Text type="secondary">
            这里不是 AI 结论，也不替代人工验收；只基于执行状态、报告就绪度和已采集证据给出确定性检查。
          </Text>
          {summary.primary_focus ? (
            <div style={{ display: 'grid', gap: 6 }}>
              <Text type="secondary">建议关注项</Text>
              <Space wrap size={[8, 8]}>
                {summary.primary_focus.kind === 'missing_report' ? (
                  <Tag color="orange">{summary.primary_focus.label}</Tag>
                ) : (
                  <Button
                    size="small"
                    type="primary"
                    onClick={() => handleAdvisoryPrimaryFocus(summary.primary_focus)}
                  >
                    {summary.primary_focus.label}
                  </Button>
                )}
                {summary.primary_focus.run_id ? <Tag>{`Run #${summary.primary_focus.run_id}`}</Tag> : null}
                {summary.primary_focus.report_id ? <Tag>{`Report #${summary.primary_focus.report_id}`}</Tag> : null}
              </Space>
              {summary.primary_focus.detail ? (
                <Text type="secondary">{summary.primary_focus.detail}</Text>
              ) : null}
            </div>
          ) : null}
          {summary.recommended_actions.length > 0 ? (
            <div style={{ display: 'grid', gap: 6 }}>
              <Text type="secondary">建议动作</Text>
              {summary.recommended_actions.slice(0, 3).map(item => (
                <Text key={item}>{item}</Text>
              ))}
            </div>
          ) : null}
          {summary.evidence_gaps && summary.evidence_gaps.length > 0 ? (
            <div style={{ display: 'grid', gap: 6 }}>
              <Text type="secondary">证据缺口</Text>
              {summary.evidence_gaps.slice(0, 3).map(item => (
                <Text key={item}>{item}</Text>
              ))}
            </div>
          ) : null}
          {summary.key_findings.length > 0 ? (
            <div style={{ display: 'grid', gap: 6 }}>
              <Text type="secondary">关键发现</Text>
              {summary.key_findings.slice(0, 2).map(item => (
                <Text key={`${item.label}-${item.detail}`}>{`${item.label}：${item.detail}`}</Text>
              ))}
            </div>
          ) : null}
          {summary.blocking_reasons?.length ? (
            <div style={{ display: 'grid', gap: 6 }}>
              <Text type="secondary">阻断项</Text>
              {summary.blocking_reasons.map(item => (
                <Text key={item} type="danger">{item}</Text>
              ))}
            </div>
          ) : null}
          {summary.evidence_gaps?.length ? (
            <div style={{ display: 'grid', gap: 6 }}>
              <Text type="secondary">证据缺口</Text>
              <Space wrap size={[6, 6]}>
                {summary.evidence_gaps.map(item => (
                  <Tag key={item}>{item}</Tag>
                ))}
              </Space>
            </div>
          ) : null}
          {summary.recommended_actions.length > 0 ? (
            <div style={{ display: 'grid', gap: 6 }}>
              <Text type="secondary">建议动作</Text>
              {summary.recommended_actions.map(item => (
                <Text key={item}>{item}</Text>
              ))}
            </div>
          ) : null}
          {summary.limitations.length > 0 ? (
            <div style={{ display: 'grid', gap: 6 }}>
              <Text type="secondary">限制说明</Text>
              {summary.limitations.map(item => (
                <Text key={item}>{item}</Text>
              ))}
            </div>
          ) : null}
        </Space>
      </Card>
    )
  }

  const currentFocusCard = (
    <Card
      className="olh-dashboard-panel olh-plan-run-focus-card"
      size="small"
      title="压测关键信息"
      loading={Boolean(selectedRunId && fullRunDetailExpanded && runDetailLoading)}
      extra={
        <Space size={8}>
          {batchEngineGrafanaLinks.map(link => (
            <Button
              key={link.engine}
              type="link"
              icon={<LinkOutlined />}
              href={link.url}
              target="_blank"
            >
              {link.label}
            </Button>
          ))}
          {batchPodGrafanaUrl ? (
            <Button
              type="link"
              icon={<LinkOutlined />}
              href={batchPodGrafanaUrl}
              target="_blank"
            >
              批次执行节点监控
            </Button>
          ) : null}
          {downloadableReportId ? (
            <Button
              type="link"
              icon={<EyeOutlined />}
              onClick={() => navigate(`/reports/${downloadableReportId}/view`)}
            >
              查看报告
            </Button>
          ) : null}
          {downloadableReportId ? (
            <Button
              type="link"
              icon={<DownloadOutlined />}
              loading={downloadReportMutation.isPending}
              onClick={() => downloadReportMutation.mutate(downloadableReportId)}
            >
              下载报告
            </Button>
          ) : null}
          {selectedRunId ? (
            <Button
              type="link"
              icon={<LinkOutlined />}
              onClick={() => navigate(`/runs/${selectedRunId}`)}
            >
              独立打开结果详情
            </Button>
          ) : null}
        </Space>
      }
      style={{ marginBottom: 16 }}
    >
      {!selectedRecord ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="请选择一条批次执行明细" />
      ) : (
        <Space direction="vertical" size={12} style={{ display: 'flex' }}>
          {focusNotices.length > 0 ? (
            <div style={{ display: 'grid', gap: 8 }}>
              {focusNotices.map(item => (
                <div
                  className={`olh-plan-run-focus-notice olh-plan-run-focus-notice--${item.tone}`}
                  key={item.key}
                  style={{
                    borderRadius: 8,
                    padding: '8px 10px',
                  }}
                >
                  <Text strong>{item.title}</Text>
                  <div style={{ marginTop: 4 }}>
                    <Text type="secondary">{item.detail}</Text>
                  </div>
                </div>
              ))}
            </div>
          ) : null}
          <Space wrap size={[8, 8]}>
            <Tag color={selectedRunStatusTagColor}>{selectedRunStatusLabel}</Tag>
            <Tag>{selectedRoundSummary?.label || `轮次 ${selectedRound}`}</Tag>
            {selectedRoundInlineSummary ? <Text type="secondary">{selectedRoundInlineSummary}</Text> : null}
          </Space>
          <div className="olh-plan-run-summary-grid" style={summaryGridStyle}>
            <div className="olh-plan-run-compact-panel" style={compactSummaryPanelStyle}>
              <Space direction="vertical" size={4} style={{ display: 'flex' }}>
                <Text strong>当前任务</Text>
                <Text>{selectedTaskSummary?.task_name || selectedRecord.task_name || '-'}</Text>
                <Text type="secondary">
                  {buildInlineSummary([
                    `第 ${selectedTaskSummary?.record_index ?? selectedRecordIndex ?? '-'} / ${selectedTaskSummary?.record_total ?? taskExecRecords.length} 项`,
                    `阶段 ${selectedTaskSummary?.stage ?? selectedRecord.stage}`,
                    selectedTaskSummary?.logic_type_label || selectedRecord.logic_type_label || TASK_LOGIC_LABELS[selectedTaskSummary?.logic_type || selectedRecord.logic_type] || '-',
                    selectedRunId ? `Run ${selectedRunId}` : '未绑定 Run',
                  ])}
                </Text>
              </Space>
            </div>
            <div className="olh-plan-run-compact-panel" style={compactSummaryPanelStyle}>
              <Space direction="vertical" size={4} style={{ display: 'flex' }}>
                <Text strong>压测表现</Text>
                <Text type="secondary">
                  {buildInlineSummary([
                    `总请求 ${selectedRequestsTotal ?? '-'}`,
                    `TPS ${formatMetricValue(selectedObservedTps, '', 1)}`,
                    `错误率 ${formatPercentValue(selectedErrorRate)}`,
                  ])}
                </Text>
                <Text type="secondary">
                  {buildInlineSummary([
                    `平均 ${formatMetricValue(selectedAvgRtMs, 'ms', 1)}`,
                    `P95 ${formatMetricValue(selectedP95RtMs, 'ms', 1)}`,
                    `P99 ${formatMetricValue(selectedP99RtMs, 'ms', 1)}`,
                  ])}
                </Text>
                <Text type="secondary">
                  {buildInlineSummary([
                    `耗时 ${formatDuration(selectedTaskSummary?.duration_seconds ?? selectedRunSummary?.duration_seconds ?? runDetail?.duration_seconds)}`,
                    summaryLoading ? 'endpoint 明细加载中' : selectedTaskMetricSummary || undefined,
                  ]) || '已关联 Run，等待更稳定指标'}
                </Text>
              </Space>
            </div>
            <div className="olh-plan-run-compact-panel" style={compactSummaryPanelStyle}>
              <Space direction="vertical" size={4} style={{ display: 'flex' }}>
                <Text strong>状态入口</Text>
                <Text type="secondary">
                  {buildInlineSummary([
                    `当前状态 ${selectedRunStatusLabel}`,
                    `批次状态 ${data?.status_label || resolvePlanRunStatusLabel(data?.status)}`,
                  ])}
                </Text>
                <Text type="secondary">
                  {buildInlineSummary([
                    shouldQuerySelectedRunK6Control
                      ? `控制器 ${selectedRunK6Summary?.controller_status || '-'}`
                      : `报告前门 ${selectedReportSummaryText}`,
                    shouldQuerySelectedRunK6Control
                      ? `目标 TPS ${formatMetricValue(selectedRunK6Summary?.target_tps, '', 1)}`
                      : undefined,
                    shouldQuerySelectedRunK6Control
                      ? `观测 TPS ${formatMetricValue(selectedRunK6Summary?.observed_tps, '', 1)}`
                      : undefined,
                    selectedRunStopReason ? `停止原因 ${selectedRunStopReason}` : undefined,
                  ])}
                </Text>
                <Text type="secondary">
                  {buildInlineSummary([
                    `任务耗时 ${formatDuration(selectedTaskSummary?.duration_seconds ?? selectedRunSummary?.duration_seconds ?? runDetail?.duration_seconds)}`,
                    selectedRunTimeSummary || undefined,
                  ]) || '当前尚无运行时间'}
                </Text>
              </Space>
            </div>
          </div>
          <div style={{ display: 'grid', gap: 8 }}>
            <Space wrap size={[8, 8]}>
              <Text strong>批次项切换</Text>
              <Text type="secondary">点击切换当前关注项</Text>
              <Tag>{`已绑定 Run ${taskRecordStats.linkedRunTotal}`}</Tag>
              <Tag>{`运行中/准备中 ${taskRecordStats.runningTotal}`}</Tag>
              <Tag>{`异常项 ${taskRecordStats.failedTotal}`}</Tag>
            </Space>
            <div className="olh-plan-run-task-grid" style={{ ...summaryGridStyle, gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))' }}>
              {taskExecRecords.map(record => {
                const isActive = Boolean(record.collection_id && record.collection_id === activeCollectionId)
                const tileAvgRtMs = record.avg_rt_ms
                const tileP95RtMs = record.p95_rt_ms
                const tileMetricSummary = buildInlineSummary([
                  typeof record.rps === 'number' ? `TPS ${formatMetricValue(record.rps, '', 1)}` : undefined,
                  typeof tileAvgRtMs === 'number' ? `平均 ${formatMetricValue(tileAvgRtMs, 'ms', 1)}` : undefined,
                  typeof tileP95RtMs === 'number' ? `P95 ${formatMetricValue(tileP95RtMs, 'ms', 1)}` : undefined,
                ])
                return (
                  <div
                    className={`olh-plan-run-task-tile${isActive ? ' olh-plan-run-task-tile--active' : ''}`}
                    key={record.collection_id ?? `${record.stage}-${record.task_id ?? record.logic_type}`}
                    role="button"
                    tabIndex={0}
                    onClick={() => moveSelectionToCollection(record.collection_id)}
                    onKeyDown={event => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault()
                        moveSelectionToCollection(record.collection_id)
                      }
                    }}
                    style={{
                      ...compactSummaryPanelStyle,
                      cursor: record.collection_id ? 'pointer' : 'default',
                    }}
                  >
                    <Space direction="vertical" size={4} style={{ display: 'flex' }}>
                      <Space size={[6, 6]} wrap>
                        <Text strong>{record.task_name || '-'}</Text>
                        {record.collection_id ? <Tag>{`#${record.collection_id}`}</Tag> : null}
                        <Tag color={EXEC_STATUS_COLORS[record.status || 'pending'] || 'default'}>
                          {record.status_label || resolvePlanRunStatusLabel(record.status)}
                        </Tag>
                      </Space>
                      <Text type="secondary">
                        {buildInlineSummary([
                          record.run_id ? `Run ${record.run_id}` : '未绑定 Run',
                          record.logic_type_label || TASK_LOGIC_LABELS[record.logic_type],
                        ])}
                      </Text>
                      <Text type="secondary">
                        {tileMetricSummary || record.metric_summary || buildRecordMetricSummary(record) || (record.run_id ? '已关联 Run，等待指标' : '当前无性能指标')}
                      </Text>
                    </Space>
                  </div>
                )
              })}
            </div>
          </div>
          {!selectedRunId ? (
            <Alert
              type="info"
              showIcon
              message="当前选中批次项尚未绑定 Run 结果详情"
              description="一旦绑定 run_id，可在下方展开完整 RunDetail。"
            />
          ) : null}
          <Collapse className="olh-plan-run-detail-collapse olh-plan-run-detail-inline-collapse" size="small" ghost destroyInactivePanel>
            <Panel
              header={(
                <Space size={8} wrap>
                  <Text strong>执行明细</Text>
                  {hasMultipleRounds ? <Tag>{`总轮次 ${data?.round_total ?? 1}`}</Tag> : null}
                  <Tag>{`当前轮 ${taskExecRecords.length} 条`}</Tag>
                  {batchK6Rows.length > 0 ? <Tag color="green">{`运行中 K6 ${batchK6Rows.length}`}</Tag> : null}
                </Space>
              )}
              key="raw-execution-detail"
            >
              <Table<PlanTaskExecRecord>
                className="olh-plan-run-detail-table"
                rowKey={record => String(record.collection_id ?? `${record.stage}-${record.task_id ?? record.logic_type}`)}
                size="small"
                columns={detailColumns}
                dataSource={taskExecRecords}
                rowClassName={record => (record.collection_id && record.collection_id === activeCollectionId ? 'ant-table-row-selected' : '')}
                onRow={record => ({
                  onClick: () => {
                    if (!record.collection_id) {
                      return
                    }
                    moveSelectionToCollection(record.collection_id)
                  },
                })}
                locale={{ emptyText: '当前轮暂无执行明细' }}
                pagination={false}
                scroll={{ x: 980 }}
              />
            </Panel>
          </Collapse>
          {dynamicK6ControlEnabled && (batchK6Rows.length > 0 || lastBatchK6BroadcastResult) ? (
            <Collapse className="olh-plan-run-detail-collapse olh-plan-run-detail-inline-collapse" size="small" ghost destroyInactivePanel>
              <Panel
                header={(
                  <Space size={8} wrap>
                    <Text strong>批次 K6 控制</Text>
                    <Tag color={batchK6Rows.length > 0 ? 'green' : 'default'}>{`运行中 K6 ${batchK6Rows.length}`}</Tag>
                    <Tag color={batchK6ControllableRows.length > 0 ? 'processing' : 'default'}>{`可广播 ${batchK6ControllableRows.length}`}</Tag>
                  </Space>
                )}
                key="batch-k6-control"
              >
                <div className="olh-plan-run-compact-panel" style={{ ...compactSummaryPanelStyle, padding: 12 }}>
              <Space direction="vertical" size={12} style={{ display: 'flex' }}>
                <Text type="secondary">批次/计划任务以预先配置好的执行为主，批次 K6 控制默认折叠；只有需要临时调速时再展开使用。</Text>
                {batchK6Rows.length === 0 ? (
                  <Alert
                    type="info"
                    showIcon
                    message="当前批次暂无可广播的运行中 K6 项"
                    description="只有运行中的 K6 Run 会出现在这里；JMeter、后置处理项和未绑定 Run 的项不参与批量广播。"
                  />
                ) : (
                  <Space direction="vertical" size={12} style={{ display: 'flex' }}>
                    <Alert
                      type="info"
                      showIcon
                      message="当前支持按倍率或总 TPS 对运行中的 K6 项广播"
                      description={`基线总 TPS ${formatMetricValue(batchK6BaseTotalTps, '', 1)} · 当前观测总 TPS ${batchK6ObservedTotalTps != null ? formatMetricValue(batchK6ObservedTotalTps, '', 1) : '观测中'} · 最近更新时间 ${lastBatchMetricsSyncedAt}`}
                    />
                    {batchK6MissingBaseRows.length > 0 ? (
                      <Alert
                        type="warning"
                        showIcon
                        message="部分 K6 项缺少基线 target_tps，当前不会参与广播"
                        description={batchK6MissingBaseRows.map(item => `${item.taskName}(run ${item.runId})`).join('、')}
                      />
                    ) : null}
                    <Space wrap size={12}>
                      <Radio.Group
                        value={batchK6BroadcastMode}
                        onChange={event => setBatchK6BroadcastMode(event.target.value as BatchK6BroadcastMode)}
                      >
                        <Space size={12}>
                          <Radio.Button value="ratio">倍率广播</Radio.Button>
                          <Radio.Button value="total_tps">总 TPS 广播</Radio.Button>
                        </Space>
                      </Radio.Group>
                      {batchK6BroadcastMode === 'ratio' ? (
                        <InputNumber
                          min={0.01}
                          step={0.1}
                          value={batchK6RatioInput ?? undefined}
                          onChange={value => setBatchK6RatioInput(typeof value === 'number' ? value : null)}
                          style={{ width: 160 }}
                          placeholder="输入倍率，如 0.5"
                        />
                      ) : (
                        <InputNumber
                          min={1}
                          step={10}
                          value={batchK6TotalTpsInput ?? undefined}
                          onChange={value => setBatchK6TotalTpsInput(typeof value === 'number' ? value : null)}
                          style={{ width: 180 }}
                          placeholder="输入总 TPS"
                        />
                      )}
                      <Button
                        type="primary"
                        disabled={batchK6ControllableRows.length === 0}
                        loading={batchK6BroadcastMutation.isPending}
                        onClick={submitBatchK6Broadcast}
                      >
                        广播到当前运行中 K6
                      </Button>
                      {resolvedCurrentBatchK6Target ? (
                        <Button
                          onClick={() => {
                            setBatchK6BroadcastMode('total_tps')
                            setBatchK6TotalTpsInput(roundTo(resolvedCurrentBatchK6Target.targetTotalTps, 4))
                          }}
                        >
                          回填当前目标
                        </Button>
                      ) : null}
                      {rollbackBatchK6Target ? (
                        <Button
                          onClick={() => {
                            setBatchK6BroadcastMode('total_tps')
                            setBatchK6TotalTpsInput(roundTo(rollbackBatchK6Target.targetTotalTps, 4))
                          }}
                        >
                          回填上次目标
                        </Button>
                      ) : null}
                    </Space>
                    <Text type="secondary">
                      {batchK6ResolvedRatio != null
                        ? `当前预览倍率 ${roundTo(batchK6ResolvedRatio, 3)}，预览总 TPS ${formatMetricValue(batchK6PreviewTotalTps, '', 1)}`
                        : '先输入倍率或总 TPS，系统会按每个脚本的基线 target_tps 自动拆分广播值。'}
                    </Text>
                    {resolvedCurrentBatchK6Target ? (
                      <Text type="secondary">
                        {`当前记录目标 ${formatMetricValue(resolvedCurrentBatchK6Target.targetTotalTps, '', 1)} TPS（倍率 ${roundTo(resolvedCurrentBatchK6Target.ratio, 3)}）${
                          rollbackBatchK6Target ? ` · 上次目标 ${formatMetricValue(rollbackBatchK6Target.targetTotalTps, '', 1)} TPS（倍率 ${roundTo(rollbackBatchK6Target.ratio, 3)}）` : ''
                        }`}
                      </Text>
                    ) : null}
                    {pendingBatchK6BroadcastTask ? (
                      <Alert
                        type="info"
                        showIcon
                        message="最近一次批次 K6 广播任务处理中"
                        description={`任务 ${pendingBatchK6BroadcastTask.async_task_id} · 倍率 ${roundTo(pendingBatchK6BroadcastTask.ratio, 3)} · 总 TPS ${formatMetricValue(pendingBatchK6BroadcastTask.target_total_tps, '', 1)} · 当前状态 ${pendingBatchK6BroadcastStatus?.job_status || 'accepted'}`}
                      />
                    ) : null}
                    <Table
                      className="olh-plan-run-k6-table"
                      size="small"
                      rowKey={record => String(record.collectionId ?? record.runId)}
                      pagination={false}
                      dataSource={batchK6PreviewRows}
                      columns={[
                        { title: '任务', dataIndex: 'taskName', key: 'taskName' },
                        { title: 'Run', dataIndex: 'runId', key: 'runId', width: 90 },
                        { title: '环境', dataIndex: 'env', key: 'env', width: 100, render: value => value || '-' },
                        { title: '基线 TPS', dataIndex: 'baseTargetTps', key: 'baseTargetTps', width: 110, render: value => formatMetricValue(value, '', 1) },
                        { title: '当前 TPS', dataIndex: 'currentTps', key: 'currentTps', width: 110, render: (value, record) => formatLiveMetricValue(value, record.status, '', 1) },
                        { title: '广播目标 TPS', dataIndex: 'previewTargetTps', key: 'previewTargetTps', width: 130, render: value => formatMetricValue(value, '', 1) },
                        { title: '状态', dataIndex: 'status', key: 'status', width: 100, render: value => <Tag color={EXEC_STATUS_COLORS[String(value || 'pending')] || 'default'}>{resolvePlanRunStatusLabel(String(value || 'pending'))}</Tag> },
                      ]}
                    />
                    {lastBatchK6BroadcastResult ? (
                      <Card
                        size="small"
                        title="最近一次批次 K6 广播结果"
                        extra={<Text type="secondary">{`倍率 ${roundTo(lastBatchK6BroadcastResult.ratio, 3)} · 总 TPS ${formatMetricValue(lastBatchK6BroadcastResult.target_total_tps, '', 1)}`}</Text>}
                      >
                        <Space wrap size={16} style={{ marginBottom: 12 }}>
                          <Text strong>{`成功 ${lastBatchK6BroadcastResult.success_run_total}`}</Text>
                          <Text type="secondary">{`失败 ${lastBatchK6BroadcastResult.failed_run_total}`}</Text>
                          <Text type="secondary">{`跳过 ${lastBatchK6BroadcastResult.skipped_run_total}`}</Text>
                        </Space>
                        {lastBatchK6BroadcastResult.warnings.length > 0 ? (
                          <Alert
                            type="warning"
                            showIcon
                            style={{ marginBottom: 12 }}
                            message="本次广播存在补充信息"
                            description={formatK6ControlReasonList(lastBatchK6BroadcastResult.warnings).join('；')}
                          />
                        ) : null}
                        <Table
                          className="olh-plan-run-k6-table"
                          size="small"
                          rowKey={record => String(record.collection_id ?? record.run_id)}
                          pagination={false}
                          dataSource={lastBatchK6BroadcastResult.items}
                          columns={[
                            { title: '任务', dataIndex: 'task_name', key: 'task_name' },
                            { title: 'Run', dataIndex: 'run_id', key: 'run_id', width: 90 },
                            { title: '基线 TPS', dataIndex: 'base_target_tps', key: 'base_target_tps', width: 110, render: value => formatMetricValue(value, '', 1) },
                            { title: '目标 TPS', dataIndex: 'target_tps', key: 'target_tps', width: 110, render: value => formatMetricValue(value, '', 1) },
                            { title: '结果', dataIndex: 'success', key: 'success', width: 90, render: value => <Tag color={value ? 'green' : 'red'}>{value ? '成功' : '失败'}</Tag> },
                            { title: '详情', dataIndex: 'detail', key: 'detail', render: value => value ? formatK6ControlReason(value) : '-' },
                          ]}
                        />
                      </Card>
                    ) : null}
                  </Space>
                )}
              </Space>
                </div>
              </Panel>
            </Collapse>
          ) : null}
        </Space>
      )}
    </Card>
  )

  return (
    <div className="olh-page-shell olh-console-page olh-plan-run-detail-page">
      <div className="olh-console-hero olh-plan-run-detail-hero">
        <div className="olh-console-hero-main">
          <div className="olh-page-breadcrumb">OpenLoadHub / Plan Runs / Detail</div>
          <div className="olh-console-title-row">
            <h1 className="olh-page-title" aria-label="批次详情">批次详情</h1>
            <span className={isPlanRunActive ? 'olh-live-pill olh-live-pill--active' : 'olh-live-pill'}>
              {isPlanRunActive ? 'LIVE' : 'READY'}
            </span>
          </div>
          <div className="olh-page-subtitle">
            首屏只保留当前关注项、关键摘要和任务明细，其余信息默认下沉。
          </div>
        </div>
        <Space className="olh-console-actions">
          <Button onClick={() => navigate('/plan-runs')}>返回批次记录</Button>
        </Space>
      </div>

      {error ? (
        <Alert type="error" showIcon style={{ marginBottom: 16 }} message="加载批次详情失败" />
      ) : null}

      <Card
        className="olh-run-detail-workbench olh-plan-run-overview-card"
        size="small"
        loading={isLoading}
        title="批次概览"
        extra={
          <Space size={8}>
            {data?.can_stop ? <Tag color="orange">当前可停止</Tag> : null}
            <Button
              size="small"
              disabled={!data?.can_stop || stopMutation.isPending}
              loading={stopMutation.isPending}
              onClick={() => {
                if (!data?.plan_run_id) {
                  return
                }
                stopMutation.mutate(data.plan_run_id)
              }}
            >
              停止批次
            </Button>
          </Space>
        }
        style={{ marginBottom: 16 }}
      >
        <Space direction="vertical" size={12} style={{ display: 'flex' }}>
          <Alert
            type={isPlanRunActive ? 'info' : 'success'}
            showIcon
            message={isPlanRunActive ? '当前批次详情实时刷新中' : '当前批次已停止自动刷新'}
            description={
              isPlanRunActive
                ? `当前批次与执行明细默认每 5 秒自动刷新一次，最近更新 ${lastBatchMetricsSyncedAt}。`
                : `最近一次批次详情更新时间 ${lastBatchMetricsSyncedAt}。`
            }
          />
          <Space wrap size={[8, 8]}>
            {data?.status ? <StatusBadge status={data.status} /> : null}
            <Tag>{selectedRoundSummary?.label || `轮次 ${selectedRound}`}</Tag>
            <Text type="secondary">{`批次ID ${data?.plan_run_id || '-'}`}</Text>
            <Text type="secondary">{`累计耗时 ${formatDuration(planRunElapsedSeconds)}`}</Text>
            {planRunEta ? <Text type="secondary">{`预计剩余 ${formatDuration(planRunEta.remainingSeconds)}`}</Text> : null}
            {planRunEta ? <Text type="secondary">{`预计结束 ${formatLocalDateTime(planRunEta.estimatedEndAt)}`}</Text> : null}
          </Space>
          {hasMultipleRounds ? (
            <Space direction="vertical" size={8} style={{ display: 'flex' }}>
              <Text type="secondary">轮次切换</Text>
              <Segmented
                data-testid="planrun-round-switcher"
                options={roundSegmentOptions}
                value={selectedRound}
                onChange={value => moveSelectionToRound(Number(value))}
              />
            </Space>
          ) : null}
          <div className="olh-plan-run-summary-grid" style={summaryGridStyle} data-testid="planrun-overview-summary">
            <div className="olh-plan-run-summary-panel" style={summaryPanelStyle}>
              <Space direction="vertical" size={6} style={{ display: 'flex' }}>
                <Text strong>执行项</Text>
                <div style={summaryMetricValueStyle}>{executionSummary?.total_records ?? 0}</div>
                <Text type="secondary">
                  {buildInlineSummary([
                    `任务 ${executionSummary?.task_total ?? taskRecordStats.taskTotal}`,
                    `后置处理 ${executionSummary?.postprocessor_total ?? taskRecordStats.postprocessorTotal}`,
                    `已启动 Run ${executionSummary?.launched_run_total ?? 0}`,
                  ])}
                </Text>
                <Text type="secondary">
                  {buildInlineSummary([
                    `成功 ${executionSummary?.succeeded_total ?? 0}`,
                    `失败 ${executionSummary?.failed_total ?? 0}`,
                    `待处理 ${executionSummary?.pending_total ?? 0}`,
                  ])}
                </Text>
              </Space>
            </div>
            <div className="olh-plan-run-summary-panel" style={summaryPanelStyle}>
              <Space direction="vertical" size={6} style={{ display: 'flex' }}>
                <Text strong>总 TPS</Text>
                <div style={summaryMetricValueStyle}>{formatMetricValue(executionSummary?.total_throughput, ' TPS', 1)}</div>
                <Text type="secondary">
                  {buildInlineSummary([
                    `加权错误率 ${formatPercentValue(executionSummary?.weighted_error_rate)}`,
                    `最差平均 ${formatMetricValue(executionSummary?.max_avg_rt_ms, 'ms', 1)}`,
                  ])}
                </Text>
                <Text type="secondary">
                  {buildInlineSummary([
                    `最差 P95 ${formatMetricValue(executionSummary?.max_p95_rt_ms, 'ms', 1)}`,
                    `最差 P99 ${formatMetricValue(executionSummary?.max_p99_rt_ms, 'ms', 1)}`,
                  ])}
                </Text>
              </Space>
            </div>
            <div className="olh-plan-run-summary-panel" style={summaryPanelStyle}>
              <Space direction="vertical" size={6} style={{ display: 'flex' }}>
                <Text strong>批次上下文</Text>
                <Text>{data?.plan_name || '-'}</Text>
                <Text type="secondary">
                  {buildInlineSummary([
                    `操作人 ${renderOperator(data?.created_by_name, data?.created_by)}`,
                    `计划类型 ${data?.plan_exec_type_label || (data?.plan_exec_type ? PLAN_EXEC_TYPE_LABELS[data.plan_exec_type] || data.plan_exec_type : '-')}`,
                  ])}
                </Text>
                <Text type="secondary">
                  {buildInlineSummary([
                    `环境 ${renderListSummary(data?.env_list)}`,
                    `引擎 ${renderListSummary(data?.engine_types)}`,
                  ])}
                </Text>
              </Space>
            </div>
            <div className="olh-plan-run-summary-panel" style={summaryPanelStyle}>
              <Space direction="vertical" size={6} style={{ display: 'flex' }}>
                <Text strong>编排与时序</Text>
                <Text type="secondary">
                  {buildInlineSummary([
                    `执行模式 ${data?.round_mode_label || '单轮执行'}`,
                    `总轮次 ${data?.round_total ?? 1}`,
                    `阶段 ${data?.stage_total ?? 0}`,
                  ])}
                </Text>
                <Text type="secondary">
                  {buildInlineSummary([
                    `任务 ${data?.task_total ?? 0}`,
                    `后置处理 ${data?.postprocessor_total ?? 0}`,
                    `协作人 ${renderListSummary(data?.collaborator_names)}`,
                  ])}
                </Text>
                <Text type="secondary">
                  {`执行时间段 ${formatLocalDateTime(data?.started_at)} ~ ${formatLocalDateTime(data?.ended_at)}`}
                </Text>
              </Space>
            </div>
          </div>
        </Space>
      </Card>

      {currentFocusCard}

      {showPlanRunInsightPanel ? (
        <Collapse className="olh-plan-run-detail-collapse" size="small" style={{ marginBottom: 16 }} destroyInactivePanel>
          {showPlanRunInsightPanel ? (
            <Panel
              header={(
                <Space size={8} wrap>
                  <Text strong>批次结论与报告（确定性检查）</Text>
                  {advisorySummary?.verdict ? <Tag>{ADVISORY_VERDICT_LABELS[advisorySummary.verdict] || advisorySummary.verdict}</Tag> : null}
                  {reportSummary ? <Tag color={reportSummary.ready_total > 0 ? 'green' : 'default'}>{`可下载 ${reportSummary.ready_total}`}</Tag> : null}
                </Space>
              )}
              key="insights"
            >
              <Space direction="vertical" size={12} style={{ display: 'flex' }}>
                {renderAdvisorySummaryCard(advisorySummary)}
                {renderReportSummaryCard(reportSummary)}
              </Space>
            </Panel>
          ) : null}
        </Collapse>
      ) : null}

      {selectedRunId ? (
        <Collapse
          className="olh-plan-run-detail-collapse"
          size="small"
          style={{ marginBottom: 16 }}
          destroyInactivePanel
          activeKey={fullRunDetailExpanded ? ['full-run-detail'] : []}
          onChange={key => {
            const activeKeys = Array.isArray(key) ? key : key ? [key] : []
            setFullRunDetailExpanded(activeKeys.includes('full-run-detail'))
          }}
        >
          <Panel
            header={(
              <Space size={8} wrap>
                <Text strong>完整 RunDetail</Text>
                <Tag color={selectedRunStatusTagColor}>{selectedRunStatusLabel}</Tag>
                <Tag color="processing">{`Run #${selectedRunId}`}</Tag>
              </Space>
            )}
            key="full-run-detail"
          >
            <div className="olh-plan-run-selected-run-shell" style={selectedRunShellStyle}>
              <div style={{ padding: 12 }}>
                {fullRunDetailExpanded ? (
                  <RunDetail
                    runIdOverride={selectedRunId}
                    breadcrumbLabel="OpenLoadHub / 批次记录 / 批次详情"
                    titleText={`${selectedRoundSummary?.label || `轮次 ${selectedRound}`} · 批次项结果详情`}
                    backLabel="返回批次记录"
                    onBack={handleBackToPlanRunList}
                  />
                ) : null}
              </div>
            </div>
          </Panel>
        </Collapse>
      ) : null}
    </div>
  )
}

export default PlanRunDetail
