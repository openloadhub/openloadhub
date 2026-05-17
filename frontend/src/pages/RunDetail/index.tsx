import { Alert, Card, Collapse, Space, Table, message, Button, Tag, Tabs, Empty, Steps, InputNumber, Radio, Typography, Modal, Input, Select, Tooltip } from 'antd'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import dayjs from 'dayjs'
import type { EChartsOption } from 'echarts'
import ReactECharts from 'echarts-for-react'
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import { StatusBadge } from '@/components/common'
import { reportApi, type RunReportFrontdoorResolution } from '@/services/reportApi'
import { getCanonicalUserLabel } from '@/utils/displayUser'
import { formatK6ControlActionError, formatK6ControlReason, formatK6ControlReasonList } from '@/utils/k6ControlReason'
import { formatLocalDateTime } from '@/utils/localDateTime'
import { publicAlphaFeatures } from '@/config/publicAlpha'
import { taskApi, type Protocol } from '@/services/taskApi'
import { useThemeStore } from '@/store/themeStore'
import {
  runApi,
  type InterfaceMetric,
  type LogItem,
  type MetricsSeries,
  type RunBaselineSummary,
  type RunAIAnalystSummary,
  type RunVerdictSummary,
  type GroupCheck,
  type RunDashboardLink,
  type RunDashboardSummary,
  type EndpointTrendMetric,
  type EndpointTrendSeries,
  type RunK6ControlResponse,
  type RunAIReportSummary,
  type RunAIReportFeedbackAction,
  type RunAIReportFeedbackRating,
  type RunAlertEvent,
  type RunAnalysisReadiness,
} from '@/services/runApi'

type TabKey = 'pressure' | 'monitor'
type K6ControlInputMode = 'ratio' | 'total_tps'
type K6LastSuccessfulConfig = {
  runId?: number | null
  targetTps: number
  startedAt?: string | null
  status?: string | null
  source: 'recent_success_run' | 'task_last_run_params'
}

type AgentRunEntry = {
  agent_host?: string | null
  agent_ip?: string | null
  node_name?: string | null
  pod_name?: string | null
}

type RunNodeTarget = {
  key: string
  label: string
  agentHost?: string | null
  podIp?: string | null
  podName?: string | null
  nodeName?: string | null
  sourceHints: string[]
}

type RunNodeLike = {
  agentHost?: string | null
  podIp?: string | null
  podName?: string | null
  nodeName?: string | null
}

type InfoWallItem = {
  label: string
  value: string
  helper?: string
}

const formatDateTime = (value?: string | null) => {
  if (!value) return '-'
  return formatLocalDateTime(value)
}
const fmt = (value: number | null | undefined, suffix = '', decimals = 2) =>
  value == null || !Number.isFinite(Number(value)) ? '-' : `${Number(value).toFixed(decimals)}${suffix}`
const roundTo = (value: number, digits = 4): number => Number(value.toFixed(digits))
const K6_TARGET_TPS_PARAM_KEYS = ['target_tps', 'base_target_tps', 'total_tps', 'fixed_tps', 'TARGET_TPS']
const aiPrimaryFocusSectionSelectorMap: Record<string, string> = {
  baseline: '[data-testid="run-detail-baseline"]',
  summary_metrics: '[data-testid="run-detail-summary-metrics"]',
  runtime_logs: '[data-testid="run-detail-log-workspace"]',
  monitor: '[data-testid="run-detail-monitor"]',
}
const parseK6TimeUnitSeconds = (value?: string | null) => {
  if (!value) return null
  const normalized = String(value).trim()
  const matched = normalized.match(/^(\d+(?:\.\d+)?)(ms|s|m|h)$/i)
  if (!matched) return null
  const amount = Number(matched[1])
  if (!Number.isFinite(amount) || amount <= 0) return null
  const unit = matched[2].toLowerCase()
  if (unit === 'ms') return amount / 1000
  if (unit === 's') return amount
  if (unit === 'm') return amount * 60
  if (unit === 'h') return amount * 3600
  return null
}
const getK6AgentScenarioTps = (agent: RunK6ControlResponse['agents'][number]) => {
  if (!Array.isArray(agent.scenario_configs) || agent.scenario_configs.length === 0) {
    return null
  }
  const total = agent.scenario_configs.reduce((sum, config) => {
    const rate = typeof config.rate === 'number' && Number.isFinite(config.rate) ? config.rate : null
    const seconds = parseK6TimeUnitSeconds(config.time_unit)
    if (rate == null || seconds == null || seconds <= 0) {
      return sum
    }
    return sum + (rate / seconds)
  }, 0)
  return total > 0 ? total : null
}
const getLatestTrendThroughput = (items?: EndpointTrendSeries[]) => {
  const normalizedItems = (items ?? []).filter(item => Array.isArray(item.points) && item.points.length > 0)
  if (normalizedItems.length === 0) {
    return null
  }

  const overallItem = normalizedItems.find(item => item.endpoint_name === 'overall')
  if (overallItem) {
    const latestOverallPoint = [...overallItem.points].reverse().find(point => typeof point.value === 'number' && Number.isFinite(point.value))
    if (latestOverallPoint && typeof latestOverallPoint.value === 'number') {
      return latestOverallPoint.value
    }
  }

  const latestBySeries = normalizedItems
    .filter(item => item.endpoint_name !== 'overall')
    .map(item => [...item.points].reverse().find(point => typeof point.value === 'number' && Number.isFinite(point.value))?.value ?? null)
    .filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
  if (latestBySeries.length === 0) {
    return null
  }
  return latestBySeries.reduce((sum, value) => sum + value, 0)
}
const resolveK6TargetTpsFromParams = (params?: Record<string, unknown> | null) => {
  if (!params || typeof params !== 'object') {
    return null
  }
  for (const key of K6_TARGET_TPS_PARAM_KEYS) {
    const rawValue = params[key]
    const numericValue = typeof rawValue === 'number'
      ? rawValue
      : typeof rawValue === 'string' && rawValue.trim()
        ? Number(rawValue)
        : null
    if (typeof numericValue === 'number' && Number.isFinite(numericValue) && numericValue > 0) {
      return numericValue
    }
  }
  return null
}
const isSucceededRunStatus = (status?: string | null) => {
  const normalized = String(status || '').trim().toLowerCase()
  return normalized === 'succeeded' || normalized === 'success'
}
const getAIReportStatusColor = (status?: string | null) => {
  if (status === 'success') return 'success'
  if (status === 'failed') return 'error'
  if (status === 'pending') return 'processing'
  return 'default'
}
const getAlertEventStatusColor = (status?: string | null) => {
  const normalized = normalizeText(status)
  if (normalized === 'firing') return 'error'
  if (normalized === 'resolved') return 'success'
  return 'default'
}
const getAlertEventSeverityColor = (severity?: string | null) => {
  const normalized = normalizeText(severity)
  if (normalized === 'critical') return 'red'
  if (normalized === 'high') return 'orange'
  if (normalized === 'warning') return 'gold'
  if (normalized === 'info') return 'blue'
  return 'default'
}
type RunAlertPolicySnapshot = {
  name: string
  source: string
  subscription: string
  alertname: string
  severity: string
  actions: string
  autoStop: boolean
  observeOnly: boolean
}
const asRecord = (value: unknown): Record<string, unknown> => (
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
)
const toStringList = (value: unknown): string[] => {
  if (Array.isArray(value)) {
    return value
      .map(item => String(item ?? '').trim())
      .filter(Boolean)
  }
  if (typeof value === 'string') {
    return value
      .split(',')
      .map(item => item.trim())
      .filter(Boolean)
  }
  return []
}
const firstString = (...values: unknown[]) => {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) {
      return value.trim()
    }
  }
  return '-'
}
const summarizeRunAlertPolicies = (params?: Record<string, unknown> | null): RunAlertPolicySnapshot[] => {
  const policies = Array.isArray(params?.alert_policies) ? params?.alert_policies : []
  return policies
    .map(policy => {
      const record = asRecord(policy)
      const match = asRecord(record.match)
      const severity = toStringList(match.severity)
      const actions = toStringList(record.actions)
      return {
        name: firstString(record.name, match.alertname, '告警策略'),
        source: firstString(record.source),
        subscription: firstString(match.subscription, record.subscription),
        alertname: firstString(match.alertname, record.alertname),
        severity: severity.length > 0 ? severity.join(', ') : '-',
        actions: actions.length > 0 ? actions.join(', ') : '-',
        autoStop: record.auto_stop_enabled === true,
        observeOnly: record.observe_only !== false,
      }
    })
    .filter(policy => policy.name !== '-' || policy.alertname !== '-' || policy.subscription !== '-')
}
const aiReportFeedbackRatingOptions: { label: string; value: RunAIReportFeedbackRating }[] = [
  { label: 'useful', value: 'useful' },
  { label: 'not useful', value: 'not_useful' },
  { label: 'neutral', value: 'neutral' },
]
const aiReportFeedbackActionOptions: { label: string; value: RunAIReportFeedbackAction }[] = [
  { label: 'accepted', value: 'accepted' },
  { label: 'needs rerun', value: 'needs_rerun' },
  { label: 'ignored', value: 'ignored' },
]
const aiReportFeedbackRatingLabel: Record<RunAIReportFeedbackRating, string> = {
  useful: 'useful',
  not_useful: 'not useful',
  neutral: 'neutral',
}
const aiReportFeedbackActionLabel: Record<RunAIReportFeedbackAction, string> = {
  accepted: 'accepted',
  needs_rerun: 'needs rerun',
  ignored: 'ignored',
}
const verdictStatusLabelMap: Record<RunVerdictSummary['verdict'], string> = {
  pass: '稳定',
  warn: '需复核',
  fail: '需排查',
}
const confidenceLabelMap: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
}
const runVerdictReasonLabelMap: Record<string, string> = {
  run_not_terminal: '运行尚未结束，结论仍可能继续变化',
  run_failed: '运行状态已失败',
  error_rate_fail_threshold: '错误率达到失败阈值',
  error_rate_warn_threshold: '错误率达到预警阈值',
  p95_threshold_exceeded: 'P95 时延超过任务阈值',
  throughput_degraded_fail: '吞吐相对基线明显下降',
  throughput_degraded_warn: '吞吐相对基线下降',
  avg_rt_ms_regression_fail: '平均 RT 相对基线明显上升',
  avg_rt_ms_regression_warn: '平均 RT 相对基线上升',
  p95_rt_ms_regression_fail: 'P95 RT 相对基线明显上升',
  p95_rt_ms_regression_warn: 'P95 RT 相对基线上升',
  p99_rt_ms_regression_fail: 'P99 RT 相对基线明显上升',
  p99_rt_ms_regression_warn: 'P99 RT 相对基线上升',
}
const runVerdictMetricLabelMap: Record<string, string> = {
  error_rate: '错误率',
  throughput: '吞吐',
  avg_rt_ms: '平均 RT',
  p95_rt_ms: 'P95 RT',
  p99_rt_ms: 'P99 RT',
}
const autoSummarySourceLabelMap: Record<string, string> = {
  verdict: '稳定性结论',
  summary_metrics: '核心指标',
  baseline: '基线',
  dashboards: '监控面板',
  runtime_detail: '运行状态',
  monitor: '执行节点监控',
}
const { Panel } = Collapse
const extractK6ControlActionErrorMessage = (error: unknown) => {
  if (!(error instanceof Error)) {
    return '下发 K6 控制能力失败'
  }
  const axiosLike = error as Error & {
    response?: {
      data?: {
        detail?: string
        data?: {
          detail?: string
        }
      }
    }
  }
  const detail = axiosLike.response?.data?.detail
    || axiosLike.response?.data?.data?.detail
    || error.message
  return formatK6ControlActionError(detail)
}
const buildK6UpshiftBlockedHint = ({
  error,
  currentTargetTps,
  requestedTargetTps,
  currentMaxVus,
}: {
  error: unknown
  currentTargetTps?: number | null
  requestedTargetTps?: number | null
  currentMaxVus?: number | null
}) => {
  const normalized = extractK6ControlActionErrorMessage(error)
  if (!normalized.includes('VU buffer/backpressure')) {
    return normalized
  }
  const currentTargetLabel = typeof currentTargetTps === 'number' ? `${currentTargetTps.toFixed(2)} TPS` : '当前档位'
  const requestedTargetLabel = typeof requestedTargetTps === 'number' ? `${requestedTargetTps.toFixed(2)} TPS` : '更高 TPS'
  const currentMaxVusLabel = typeof currentMaxVus === 'number' ? `${currentMaxVus.toFixed(0)}` : '-'
  return `当前运行在 ${currentTargetLabel} 档位时检测到吞吐回压，且观测吞吐未贴近目标（当前总容量上限 ${currentMaxVusLabel} VUs），已阻止继续上调到 ${requestedTargetLabel}。这里的判断不是把 VU 数直接当成 TPS 上限，而是结合当前吞吐和回压做保护；若当前吞吐已稳定贴近目标，应允许继续上调。若目标确实更高，请先分段上调，或增加执行节点后重跑。`
}
const formatCompactTimeAxisLabel = (value?: string | null) => {
  if (!value) return '-'
  const parsed = new Date(value)
  const resolved = Number.isNaN(parsed.getTime()) ? dayjs(value) : dayjs(parsed)
  if (!resolved.isValid()) {
    return '-'
  }
  return resolved.format('HH:mm:ss')
}
const isActiveRunStatus = (status?: string | null) => {
  const normalized = String(status ?? '').trim().toLowerCase()
  return normalized === 'preparing' || normalized === 'running'
}
const isStableDisplayValueMeaningful = (value: string | number, label: string) => {
  const normalized = String(value ?? '').trim()
  if (!normalized || normalized === '-' || normalized === '--') {
    return false
  }
  if (normalized === '未上报' || normalized === '聚合值待补' || normalized === '待配置' || normalized === '时间窗待补') {
    return false
  }
  if ((label === '接口数' || label === '总请求数' || label === 'Dashboard 总数') && Number(normalized) <= 0) {
    return false
  }
  return true
}
const formatReadablePercent = (value?: number | null, decimals = 2) => (
  typeof value === 'number' && Number.isFinite(value)
    ? `${(value * 100).toFixed(decimals)}%`
    : '-'
)
const ANALYSIS_READINESS_STATUS_COLORS: Record<string, string> = {
  ready: 'green',
  partial: 'orange',
  blocked: 'red',
  missing: 'red',
}
const ANALYSIS_READINESS_SECTION_ORDER = [
  'run_lifecycle',
  'summary_metrics',
  'checks',
  'execution_context',
  'observability_frontdoor',
]
const formatReadableMetricValue = (
  metric: string,
  value?: number | null,
  unit?: string | null,
) => {
  if (value == null || !Number.isFinite(value)) {
    return '-'
  }
  if (metric === 'error_rate' || unit === 'ratio') {
    return formatReadablePercent(value)
  }
  if (unit === 'ms') {
    return `${Number(value).toFixed(2)} ms`
  }
  if (unit === 'rep/s') {
    return `${Number(value).toFixed(2)} rep/s`
  }
  return unit ? `${Number(value).toFixed(2)} ${unit}` : `${Number(value).toFixed(2)}`
}
const buildVerdictSummaryText = (
  verdict: RunVerdictSummary,
  runStatusLabel?: string | null,
) => {
  const reasonCodes = new Set(verdict.reason_codes)
  if (reasonCodes.has('run_failed')) {
    return '本次运行已失败，建议优先排查错误率、时延、吞吐和运行日志。'
  }
  if (reasonCodes.has('run_not_terminal')) {
    const statusText = runStatusLabel ? `仍处于${runStatusLabel}` : '仍未结束'
    return `当前运行${statusText}，结论会继续变化，建议结束后再复核。`
  }
  if (verdict.verdict === 'pass') {
    return '当前运行未触发失败或预警规则，整体表现相对稳定。'
  }
  if (verdict.verdict === 'fail') {
    return '当前运行已触发失败级规则，建议优先检查错误率、时延和吞吐回退。'
  }
  return '当前运行存在波动，建议结合关键指标和基线继续复核。'
}
const buildVerdictReasonSummary = (reasonCodes: string[]) => {
  const readable = Array.from(new Set(
    reasonCodes
      .filter(code => code && code !== 'no_rule_triggered')
      .map(code => runVerdictReasonLabelMap[code] || code),
  ))
  if (readable.length === 0) {
    return []
  }
  return readable.slice(0, 3)
}
const buildVerdictMetricSummary = (
  item: RunVerdictSummary['metric_deltas'][number],
  baselineRunId?: number | null,
) => {
  const metricLabel = runVerdictMetricLabelMap[item.metric] || item.metric
  const currentText = formatReadableMetricValue(item.metric, item.current_value, item.unit)
  if (item.baseline_value == null || item.delta_ratio == null || !Number.isFinite(item.delta_ratio)) {
    return `当前${metricLabel}：${currentText}`
  }
  const baselineText = formatReadableMetricValue(item.metric, item.baseline_value, item.unit)
  const deltaPercent = `${Math.abs(item.delta_ratio * 100).toFixed(1)}%`
  const direction = item.metric === 'throughput'
    ? item.delta_ratio <= 0 ? '下降' : '上升'
    : item.delta_ratio >= 0 ? '上升' : '下降'
  const baselineHint = baselineRunId ? `相对基线 #${baselineRunId}` : '相对基线'
  return `${metricLabel}${baselineHint}${direction} ${deltaPercent}，当前 ${currentText}，基线 ${baselineText}`
}
const normalizeAutoSummaryText = (
  text?: string | null,
  verdict?: RunAIAnalystSummary['verdict'],
) => {
  const trimmed = formatRuleDiagnosisText(text)
  if (trimmed) {
    return trimmed
  }
  if (verdict === 'pass') {
    return '系统整理结果显示当前运行整体稳定。'
  }
  if (verdict === 'fail') {
    return '系统整理结果显示当前运行已出现重点风险。'
  }
  return '系统整理结果显示当前运行仍需继续复核。'
}
const formatRuleDiagnosisText = (value?: string | null) => {
  if (!value) return ''
  return String(value)
    .replace(/ai-analyst/gi, '规则诊断')
    .replace(/AI\s*分析摘要/g, '规则诊断摘要')
    .replace(/AI\s*分析/g, '规则诊断')
    .replace(/AI\s*建议/g, '建议')
    .replace(/\bAI\b\s*/gi, '')
    .trim()
}
const summarizeTextList = (items: string[], limit = 2) => {
  const normalized = Array.from(new Set(items.map(item => item.trim()).filter(Boolean)))
  if (normalized.length === 0) {
    return ''
  }
  const visible = normalized.slice(0, limit)
  return normalized.length > limit
    ? `${visible.join('；')} 等 ${normalized.length} 项`
    : visible.join('；')
}
const parseAIReportFailureMessage = (rawMessage?: string | null) => {
  const messageText = String(rawMessage || '').trim()
  if (!messageText) {
    return {
      title: 'AI Report 生成未成功',
      description: '请检查 AI provider 配置或稍后重试。',
    }
  }
  const knownPrefixes = ['AI Report 当前不可用：', 'AI Report 生成失败：']
  const matchedPrefix = knownPrefixes.find(prefix => messageText.startsWith(prefix))
  if (!matchedPrefix) {
    return {
      title: 'AI Report 生成失败',
      description: messageText,
    }
  }
  return {
    title: matchedPrefix.slice(0, -1),
    description: messageText.slice(matchedPrefix.length).trim(),
  }
}

const mergeStableInfoWallItems = (
  current: InfoWallItem[],
  next: InfoWallItem[],
  active: boolean,
) => {
  if (!active || current.length === 0) {
    return next
  }
  return next.map((item, index) => {
    const previous = current[index]
    if (!previous) {
      return item
    }
    if (!isStableDisplayValueMeaningful(previous.value, previous.label) && isStableDisplayValueMeaningful(item.value, item.label)) {
      return item
    }
    return previous
  })
}

const RUN_DETAIL_ACTIVE_POLL_MS = 1_000
const isValidMetricValue = (value: number | null | undefined): value is number => typeof value === 'number' && Number.isFinite(value)
const getTextValue = (value: unknown) => (typeof value === 'string' && value.trim() ? value.trim() : null)
const uniqueTextValues = (values: Array<string | null | undefined>) =>
  Array.from(new Set(values.filter((item): item is string => Boolean(item && item.trim()))))
const PROTOCOL_LABELS: Record<string, string> = {
  http: 'HTTP',
  grpc: 'GRPC',
  kafka: 'Kafka',
  websocket: 'WebSocket',
  browser: 'Browser',
  other: 'Other',
}

const getProtocolLabel = (protocol: string) => PROTOCOL_LABELS[String(protocol).toLowerCase()] || String(protocol).toUpperCase()
const formatProtocolList = (protocols: Array<Protocol | string> | null | undefined) => {
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
const formatRunParamValue = (label: string, value: unknown, unit = '') => {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return `${label} ${value}${unit}`
  }

  if (typeof value === 'string') {
    const trimmed = value.trim()
    if (!trimmed) {
      return null
    }
    return `${label} ${/^-?\d+(\.\d+)?$/.test(trimmed) ? `${trimmed}${unit}` : trimmed}`
  }

  return null
}

const getPreferredBaselineScopeType = (
  run: { protocol?: string | null } | null | undefined,
): 'task_env' | 'task_env_protocol' => (
  typeof run?.protocol === 'string' && run.protocol.trim()
    ? 'task_env_protocol'
    : 'task_env'
)

const coerceDisplayInt = (value: unknown): number | null => {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return Math.floor(value)
  }
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? Math.floor(parsed) : null
  }
  return null
}

const coerceDisplayFloat = (value: unknown): number | null => {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value
  }
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

const parseSummaryMetricSeedRows = (params: Record<string, unknown> | null | undefined): InterfaceMetric[] => {
  const rawRows = params?.summary_metrics
  if (!Array.isArray(rawRows)) {
    return []
  }

  const coerceNumber = (value: unknown): number | null => {
    if (typeof value === 'number' && Number.isFinite(value)) {
      return value
    }
    if (typeof value === 'string' && value.trim()) {
      const parsed = Number(value)
      return Number.isFinite(parsed) ? parsed : null
    }
    return null
  }

  return rawRows.flatMap(row => {
    if (!row || typeof row !== 'object') {
      return []
    }
    const payload = row as Record<string, unknown>
    const endpointName = getTextValue(payload.endpoint_name)
    if (!endpointName) {
      return []
    }
    return [{
      endpoint_name: endpointName,
      avg_rt_ms: coerceNumber(payload.avg_rt_ms),
      p95_rt_ms: coerceNumber(payload.p95_rt_ms),
      p99_rt_ms: coerceNumber(payload.p99_rt_ms),
      max_rt_ms: coerceNumber(payload.max_rt_ms),
      min_rt_ms: coerceNumber(payload.min_rt_ms),
      total_requests: coerceNumber(payload.total_requests),
      throughput: coerceNumber(payload.throughput),
    }]
  })
}

const buildRunNodeIdentityKey = ({ agentHost, podIp, podName, nodeName }: RunNodeLike) =>
  getTextValue(agentHost) ?? getTextValue(podIp) ?? getTextValue(podName) ?? getTextValue(nodeName) ?? null

const buildRunNodeTargetLabel = (
  {
    agentHost,
    podIp,
    podName,
    nodeName,
  }: RunNodeLike,
  options?: {
    preferAgentHost?: boolean
  },
) => {
  const stableAgentHost = getTextValue(agentHost)
  const stablePodIp = getTextValue(podIp)
  const stablePodName = getTextValue(podName)
  const stableNodeName = getTextValue(nodeName)
  const primary = options?.preferAgentHost
    ? stableAgentHost ?? stablePodIp ?? stablePodName ?? stableNodeName ?? '节点视角'
    : stablePodIp ?? stablePodName ?? stableNodeName ?? stableAgentHost ?? '节点视角'
  if (stableNodeName && stableNodeName !== primary) {
    return `${stableNodeName} / ${primary}`
  }
  return primary
}

const buildPodIpOnlyMonitorLabel = ({ podIp, podName, nodeName }: RunNodeLike) =>
  getTextValue(podIp) ?? getTextValue(podName) ?? getTextValue(nodeName) ?? 'pod_ip'

const getRunNodeTargetTokens = (target: RunNodeTarget) =>
  uniqueTextValues([target.agentHost, target.podIp, target.podName, target.nodeName, ...target.sourceHints])

const hostContainsPodIp = (agentHost?: string | null, podIp?: string | null) => {
  const host = normalizeText(agentHost)
  const ip = normalizeText(podIp)
  return Boolean(host && ip && host.includes(ip))
}

const shouldMergeMonitorPodTarget = (target: RunNodeTarget, candidate: RunNodeLike) => {
  const targetPodIp = normalizeText(target.podIp)
  const candidatePodIp = normalizeText(candidate.podIp)
  if (!targetPodIp || targetPodIp !== candidatePodIp) {
    return false
  }

  const targetPodName = normalizeText(target.podName)
  const candidatePodName = normalizeText(candidate.podName)
  if (targetPodName && candidatePodName && targetPodName === candidatePodName) {
    return true
  }

  const targetAgentHost = normalizeText(target.agentHost)
  const candidateAgentHost = normalizeText(candidate.agentHost)
  if (!targetAgentHost || !candidateAgentHost) {
    return true
  }
  if (targetAgentHost === candidateAgentHost) {
    return true
  }

  return !hostContainsPodIp(target.agentHost, target.podIp) || !hostContainsPodIp(candidate.agentHost, candidate.podIp)
}

const resolveMonitorPodAgentHost = (target: RunNodeTarget, candidate: RunNodeLike) => {
  const candidateAgentHost = getTextValue(candidate.agentHost)
  if (!candidateAgentHost) {
    return target.agentHost ?? null
  }

  const targetAgentHost = getTextValue(target.agentHost)
  if (!targetAgentHost) {
    return candidateAgentHost
  }

  if (
    normalizeText(target.podIp) === normalizeText(candidate.podIp)
    && hostContainsPodIp(candidateAgentHost, candidate.podIp)
    && !hostContainsPodIp(targetAgentHost, target.podIp)
  ) {
    return candidateAgentHost
  }

  return targetAgentHost
}

const logMatchesRunNodeTarget = (item: LogItem, target: RunNodeTarget) => {
  const source = (getTextValue(item.source) ?? '').toLowerCase()
  const message = (getTextValue(item.message) ?? '').toLowerCase()
  const agentHost = normalizeText(item.agent_host)

  if (target.agentHost) {
    const expectedHost = normalizeText(target.agentHost)
    if (agentHost === expectedHost || source.includes(expectedHost) || message.includes(expectedHost)) {
      return true
    }
  }

  return getRunNodeTargetTokens(target).some(rawToken => {
    const token = rawToken.toLowerCase()
    return (
      agentHost === token
      || source === token
      || source.includes(`@${token}`)
      || source.includes(token)
      || message.includes(token)
    )
  })
}

const parseAgentRunEntries = (params?: Record<string, unknown> | null): AgentRunEntry[] => {
  const agentRuns = params?.agent_runs
  if (!Array.isArray(agentRuns)) {
    return []
  }

  return agentRuns
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
    .map(item => ({
      agent_host: getTextValue(item.agent_host),
      agent_ip: getTextValue(item.agent_ip),
      node_name: getTextValue(item.node_name),
      pod_name: getTextValue(item.pod_name),
    }))
}
const normalizeText = (value?: string | null) => String(value || '').trim().toLowerCase()
const resolveLogSourceKind = (item: LogItem): 'tool' | 'platform' | 'unknown' => {
  if (item.source_kind === 'tool' || item.source_kind === 'platform' || item.source_kind === 'unknown') {
    return item.source_kind
  }

  const normalizedSource = normalizeText(item.source)
  if (normalizedSource.startsWith('tool-stdout') || normalizedSource.startsWith('tool-stderr')) {
    return 'tool'
  }
  if (normalizedSource.startsWith('ptp-agent') || normalizedSource.startsWith('ptp-admin')) {
    return 'platform'
  }
  return 'unknown'
}

const getLatestSeriesValue = (series?: MetricsSeries | null) => {
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

const buildFallbackTrendItems = (
  metric: EndpointTrendMetric,
  series?: MetricsSeries[],
): EndpointTrendSeries[] => {
  const metricMap: Partial<Record<EndpointTrendMetric, MetricsSeries['metric']>> = {
    throughput: 'rps',
    rt_avg_ms: 'rt_avg_ms',
    rt_p95_ms: 'rt_p95_ms',
    rt_p99_ms: 'rt_p99_ms',
    error_rate: 'error_rate',
  }
  const targetMetric = metricMap[metric]
  if (!targetMetric) {
    return []
  }

  const matched = series?.find(item => item.metric === targetMetric)
  if (!matched?.points?.length) {
    return []
  }

  const points = matched.points
    .filter(point => {
      const ts = new Date(point?.ts || '').getTime()
      return Number.isFinite(ts) && ts >= Date.UTC(2000, 0, 1)
    })
    .map(point => ({ ts: point.ts, value: point.value ?? null }))

  if (points.length === 0) {
    return []
  }

  return [
    {
      endpoint_name: 'overall',
      metric,
      unit: matched.unit,
      points,
    },
  ]
}

const primaryFlowWorkbenchStyle = {
  marginBottom: 14,
  border: '1px solid var(--border-color)',
  borderRadius: 8,
  background: 'var(--card-bg)',
  overflow: 'hidden',
  boxShadow: 'var(--card-shadow)',
} as const

const primaryFlowSectionStyle = {
  padding: '12px 14px 0',
  background: 'transparent',
} as const

const primaryFlowSectionDividerStyle = {
  borderTop: '1px solid var(--border-subtle)',
} as const

const primaryFlowHeaderStyle = {
  display: 'grid',
  gap: 14,
  padding: '16px 18px',
  borderBottom: '1px solid var(--border-subtle)',
  background: 'linear-gradient(135deg, color-mix(in srgb, var(--card-bg-elevated) 88%, var(--primary-color) 12%) 0%, var(--card-bg) 62%)',
} as const

const primaryFlowToolbarStyle = {
  display: 'flex',
  justifyContent: 'space-between',
  gap: 12,
  flexWrap: 'wrap',
  alignItems: 'center',
} as const

const primaryFlowTitleRowStyle = {
  display: 'flex',
  justifyContent: 'space-between',
  gap: 18,
  flexWrap: 'wrap',
  alignItems: 'flex-end',
} as const

const primaryFlowActionButtonBaseStyle = {
  height: 30,
  paddingInline: 12,
  borderRadius: 6,
  fontSize: 12,
  boxShadow: 'none',
} as const

const primaryFlowDarkActionButtonStyle = {
  ...primaryFlowActionButtonBaseStyle,
  background: 'var(--primary-color)',
  borderColor: 'var(--primary-color)',
  color: '#ffffff',
} as const

const primaryFlowLightActionStyle = {
  ...primaryFlowActionButtonBaseStyle,
  background: 'var(--card-bg-elevated)',
  borderColor: 'var(--border-color)',
  color: 'var(--text-primary)',
} as const

const primaryFlowDisabledActionStyle = {
  ...primaryFlowActionButtonBaseStyle,
  background: 'var(--input-bg)',
  borderColor: 'var(--border-subtle)',
  color: 'var(--text-muted)',
  cursor: 'not-allowed',
} as const

const primaryFlowLinkActionStyle = {
  height: 26,
  paddingInline: 2,
  fontSize: 12,
} as const

const primaryFlowLabelCellStyle = {
  width: 92,
  padding: '8px 10px',
  borderBottom: '1px solid var(--border-subtle)',
  color: 'var(--text-secondary)',
  fontSize: 11,
  fontWeight: 600,
  verticalAlign: 'top',
  background: 'var(--table-header-bg)',
} as const

const primaryFlowValueCellStyle = {
  padding: '8px 10px',
  borderBottom: '1px solid var(--border-subtle)',
  color: 'var(--text-primary)',
  fontSize: 12,
  background: 'var(--card-bg)',
  verticalAlign: 'top',
} as const

const monitorWorkspacePageStyle = {
  display: 'grid',
  gap: 8,
  padding: 8,
  border: '1px solid var(--border-color)',
  borderRadius: 4,
  background: 'var(--surface-quiet)',
} as const

const monitorWorkspaceShellStyle = {
  border: '1px solid var(--border-color)',
  borderRadius: 2,
  background: 'var(--card-bg)',
  overflow: 'hidden',
} as const

const monitorWorkspaceToolbarStyle = {
  padding: '6px 10px',
  borderBottom: '1px solid var(--border-subtle)',
  background: 'var(--surface-subtle)',
} as const

const monitorWorkspaceBodyStyle = {
  padding: '6px 10px 8px',
  background: 'var(--card-bg)',
} as const

const pressureWorkbenchSurfaceStyle = {
  border: '1px solid var(--border-color)',
  borderRadius: 8,
  background: 'linear-gradient(180deg, var(--card-bg) 0%, color-mix(in srgb, var(--card-bg) 90%, var(--surface-subtle) 10%) 100%)',
  overflow: 'hidden',
} as const

const pressureWorkbenchSectionStyle = {
  padding: 12,
  background: 'transparent',
} as const

const terminalLogSnippetStyle = {
  padding: '8px 10px',
  borderRadius: 4,
  background: 'var(--terminal-bg)',
  color: 'var(--terminal-text)',
  border: '1px solid var(--terminal-border)',
} as const

const openLoadHubMetricGridStyle = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fit, minmax(min(360px, 100%), 1fr))',
} as const

const primarySectionTitle = (title: string) => (
  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
    <span
      aria-hidden
      style={{
        width: 2,
        height: 10,
        borderRadius: 999,
        background: 'var(--primary-color)',
      }}
    />
    <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-primary)' }}>{title}</span>
  </div>
)

type RunDetailProps = {
  runIdOverride?: number
  breadcrumbLabel?: string
  titleText?: string
  backLabel?: string
  onBack?: () => void
}

const RunDetail = ({
  runIdOverride,
  breadcrumbLabel = 'OpenLoadHub / 结果列表 / 详情',
  titleText = '结果详情',
  backLabel = '返回列表',
  onBack,
}: RunDetailProps = {}) => {
  type LogView = 'all' | 'exception'
  const navigate = useNavigate()
  const { runId } = useParams<{ runId: string }>()
  const queryClient = useQueryClient()
  const theme = useThemeStore(state => state.theme)
  const processCardRef = useRef<HTMLDivElement | null>(null)
  const [logOrderAsc, setLogOrderAsc] = useState(true)
  const [logView, setLogView] = useState<LogView>('all')
  const [latencyMetric, setLatencyMetric] = useState<EndpointTrendMetric>('rt_avg_ms')
  const [logs, setLogs] = useState<LogItem[]>([])
  const [nextCursor, setNextCursor] = useState<string | null | undefined>(null)
  const [loadingLogs, setLoadingLogs] = useState(false)
  const [downloadingReport, setDownloadingReport] = useState(false)
  const [viewingReport, setViewingReport] = useState(false)
  const [regeneratingReport, setRegeneratingReport] = useState(false)
  const [latestGeneratedReportId, setLatestGeneratedReportId] = useState<number | null>(null)
  const reportBusy = viewingReport || downloadingReport || regeneratingReport
  const [stablePodGrafanaIframeState, setStablePodGrafanaIframeState] = useState<{ url: string | null; selectionKey: string }>({
    url: null,
    selectionKey: '__single__',
  })
  const [stableEngineGrafanaIframeUrl, setStableEngineGrafanaIframeUrl] = useState<string | null>(null)
  const [stableMonitorInfoWallItems, setStableMonitorInfoWallItems] = useState<InfoWallItem[]>([])
  const [k6TargetTpsInput, setK6TargetTpsInput] = useState<number | null>(null)
  const [k6TargetRatioInput, setK6TargetRatioInput] = useState<number | null>(null)
  const [k6ControlInputMode, setK6ControlInputMode] = useState<K6ControlInputMode>('total_tps')
  const [k6RollbackTargetTps, setK6RollbackTargetTps] = useState<number | null>(null)
  const [pendingK6ControlTask, setPendingK6ControlTask] = useState<{ taskId: string; targetTps?: number | null } | null>(null)
  const [pendingAIReportTask, setPendingAIReportTask] = useState<{ taskId: string; reportId: number } | null>(null)
  const [aiReportFeedbackRating, setAIReportFeedbackRating] = useState<RunAIReportFeedbackRating>('neutral')
  const [aiReportFeedbackNote, setAIReportFeedbackNote] = useState('')
  const [aiReportFeedbackAction, setAIReportFeedbackAction] = useState<RunAIReportFeedbackAction | undefined>(undefined)
  const dynamicK6ControlEnabled = publicAlphaFeatures.dynamicK6Control
  const publicAlphaMode = publicAlphaFeatures.publicAlphaMode
  const aiFeaturesEnabled = publicAlphaFeatures.aiFeatures

  // 一级 Tab 状态
  const [activeTab, setActiveTab] = useState<TabKey>('pressure')
  // 二级 Tab 状态（发压端）
  const [activeSubTab, setActiveSubTab] = useState<string>('stats')
  // 双节点页面层统一按“IP / 节点”切换
  const [selectedNodeKey, setSelectedNodeKey] = useState<string | null>(null)
  const [selectedMonitorPodKey, setSelectedMonitorPodKey] = useState<string | null>(null)
  const runIdNum = typeof runIdOverride === 'number' ? runIdOverride : Number(runId)
  const currentRunIdRef = useRef(runIdNum)

  useLayoutEffect(() => {
    if (typeof runIdOverride === 'number' || !Number.isFinite(runIdNum)) {
      return undefined
    }

    const scrollToTop = () => {
      window.scrollTo({ top: 0, left: 0, behavior: 'auto' })
      const targets = [
        document.scrollingElement,
        document.documentElement,
        document.body,
        document.querySelector('.ant-layout-content'),
        document.querySelector('.olh-main-layout'),
        document.querySelector('.olh-app-content'),
      ]
      targets.forEach(target => {
        if (target instanceof HTMLElement) {
          target.scrollTop = 0
          target.scrollLeft = 0
        }
      })
    }

    scrollToTop()
    const frameId = window.requestAnimationFrame(scrollToTop)
    const timerIds = [0, 64, 160].map(delay => window.setTimeout(scrollToTop, delay))
    return () => {
      window.cancelAnimationFrame(frameId)
      timerIds.forEach(timerId => window.clearTimeout(timerId))
    }
  }, [runIdNum, runIdOverride])

  useEffect(() => {
    currentRunIdRef.current = runIdNum
    setLatestGeneratedReportId(null)
  }, [runIdNum])

  const isCurrentReportAction = (actionRunId: number) => currentRunIdRef.current === actionRunId

  const handleBack = () => {
    if (onBack) {
      onBack()
      return
    }
    navigate('/runs')
  }

  const stopMutation = useMutation({
    mutationFn: (targetRunId: number) => runApi.stopRun(targetRunId, { reason: 'manual stop from run detail' }),
    onSuccess: () => {
      message.success('已结束压测。')
      queryClient.invalidateQueries({ queryKey: ['run', runIdNum] })
      queryClient.invalidateQueries({ queryKey: ['run-process', runIdNum] })
      queryClient.invalidateQueries({ queryKey: ['run-summary-metrics', runIdNum] })
      queryClient.invalidateQueries({ queryKey: ['run-checks', runIdNum] })
      queryClient.invalidateQueries({ queryKey: ['run-endpoint-trends', runIdNum] })
      queryClient.invalidateQueries({ queryKey: ['run-pods', runIdNum] })
      queryClient.invalidateQueries({ queryKey: ['run-pods-monitor', runIdNum] })
      queryClient.invalidateQueries({ queryKey: ['run-dashboards', runIdNum] })
      queryClient.invalidateQueries({ queryKey: ['run-alert-events', runIdNum] })
      queryClient.invalidateQueries({ queryKey: ['run-metrics', runIdNum] })
      queryClient.invalidateQueries({ queryKey: ['run-k6-control', runIdNum] })
      queryClient.invalidateQueries({ queryKey: ['runs'] })
    },
    onError: (mutationError: unknown) => {
      message.error(mutationError instanceof Error ? mutationError.message : '结束压测失败')
    },
  })
  const setBaselineMutation = useMutation({
    mutationFn: ({ runId, body }: { runId: number; body: { scope_type?: 'task_env' | 'task_env_protocol'; note?: string | null } }) =>
      runApi.setRunBaseline(runId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['run-baseline', runIdNum] })
      message.success('已更新基线')
    },
    onError: error => {
      message.error(error instanceof Error ? error.message : '更新基线失败')
    },
  })
  const generateAIReportMutation = useMutation({
    mutationFn: () => runApi.submitAsyncRunAIReport(runIdNum),
    onSuccess: response => {
      if (response.report) {
        queryClient.setQueryData<RunAIReportSummary>(['run-ai-report-latest', runIdNum], response.report)
      } else {
        queryClient.invalidateQueries({ queryKey: ['run-ai-report-latest', runIdNum] })
      }
      setPendingAIReportTask({ taskId: response.async_task_id, reportId: response.report_id })
      message.success('AI Report 已提交，后台生成中')
    },
    onError: error => {
      message.error(error instanceof Error ? error.message : 'AI Report 提交失败')
    },
  })
  const submitAIReportFeedbackMutation = useMutation({
    mutationFn: ({ reportId, body }: {
      reportId: number
      body: { rating: RunAIReportFeedbackRating; note?: string; action?: RunAIReportFeedbackAction }
    }) => runApi.submitRunAIReportFeedback(runIdNum, reportId, body),
    onSuccess: report => {
      queryClient.setQueryData<RunAIReportSummary>(['run-ai-report-latest', runIdNum], report)
      message.success('AI Report 反馈已保存')
    },
    onError: error => {
      message.error(error instanceof Error ? error.message : 'AI Report 反馈保存失败')
    },
  })
  const k6ControlMutation = useMutation({
    mutationFn: (payload: { runId: number; body: { target_tps?: number } }) =>
      runApi.submitAsyncRunK6Control(payload.runId, payload.body),
    onSuccess: response => {
      message.success('已提交 K6 控制能力，后台处理中')
      const nextTargetTps = typeof response.target_tps === 'number' ? response.target_tps : null
      const rawParams = (data?.params ?? {}) as Record<string, unknown>
      const baseTargetTps = coerceDisplayFloat(rawParams.target_tps ?? rawParams.base_target_tps ?? rawParams.fixed_tps)
      setK6TargetTpsInput(nextTargetTps)
      setK6TargetRatioInput(
        nextTargetTps != null && baseTargetTps && baseTargetTps > 0
          ? roundTo(nextTargetTps / baseTargetTps, 4)
          : null,
      )
      setPendingK6ControlTask({ taskId: response.async_task_id, targetTps: nextTargetTps })
      queryClient.invalidateQueries({ queryKey: ['run-k6-control', runIdNum] })
      queryClient.invalidateQueries({ queryKey: ['run-process', runIdNum] })
    },
  })

  const { data, error } = useQuery({
    queryKey: ['run', runIdNum],
    queryFn: () => runApi.getRunDetail(runIdNum),
    enabled: Number.isFinite(runIdNum),
    refetchInterval: current => (isActiveRunStatus(current?.state.data?.run_status) ? RUN_DETAIL_ACTIVE_POLL_MS : false),
  })
  const { data: taskDetail } = useQuery({
    queryKey: ['run-detail-task', data?.task_id],
    queryFn: () => taskApi.getTaskDetail(Number(data?.task_id)),
    enabled: Number.isFinite(Number(data?.task_id)),
  })

  const { data: metricsData } = useQuery({
    queryKey: ['run-metrics', runIdNum],
    queryFn: () => runApi.getRunMetrics(runIdNum, { step_seconds: 5 }),
    enabled: Number.isFinite(runIdNum),
    refetchInterval: data?.run_status === 'running' ? 5_000 : false,
    staleTime: 0,
    refetchOnMount: 'always',
  })

  const { data: throughputTrendData, isLoading: throughputTrendLoading } = useQuery({
    queryKey: ['run-endpoint-trends', runIdNum, 'throughput'],
    queryFn: () => runApi.getRunEndpointTrends(runIdNum, { metric: 'throughput', step_seconds: 5 }),
    enabled: Number.isFinite(runIdNum),
    refetchInterval: data?.run_status === 'running' ? 5_000 : false,
    staleTime: 0,
    refetchOnMount: 'always',
  })

  const { data: latencyTrendData, isLoading: latencyTrendLoading } = useQuery({
    queryKey: ['run-endpoint-trends', runIdNum, latencyMetric],
    queryFn: () => runApi.getRunEndpointTrends(runIdNum, { metric: latencyMetric, step_seconds: 5 }),
    enabled: Number.isFinite(runIdNum),
    refetchInterval: data?.run_status === 'running' ? 5_000 : false,
    staleTime: 0,
    refetchOnMount: 'always',
  })

  const { data: summaryMetricsData, isLoading: summaryLoading } = useQuery({
    queryKey: ['run-summary-metrics', runIdNum, data?.run_status ?? null, data?.ended_at ?? null],
    queryFn: () => runApi.getSummaryMetrics(runIdNum),
    enabled: Number.isFinite(runIdNum),
    refetchInterval: isActiveRunStatus(data?.run_status) ? 5_000 : false,
  })

  const { data: checksData, isLoading: checksLoading } = useQuery({
    queryKey: ['run-checks', runIdNum, data?.run_status ?? null, data?.ended_at ?? null],
    queryFn: () => runApi.getChecks(runIdNum),
    enabled: Number.isFinite(runIdNum),
    refetchInterval: data?.run_status === 'running' ? 5_000 : false,
  })
  const [stickyChecksItems, setStickyChecksItems] = useState<GroupCheck[]>([])

  useEffect(() => {
    const nextItems = checksData?.items ?? []
    if (nextItems.length > 0) {
      setStickyChecksItems(nextItems)
      return
    }
    if (!isActiveRunStatus(data?.run_status)) {
      setStickyChecksItems([])
    }
  }, [checksData?.items, data?.run_status])

  // 运行过程（阶段流转）
  const { data: processData, isLoading: processLoading } = useQuery({
    queryKey: ['run-process', runIdNum],
    queryFn: () => runApi.getRunProcess(runIdNum),
    enabled: Number.isFinite(runIdNum),
    refetchInterval: data?.run_status === 'running' ? 5_000 : false,
  })
  const preferredBaselineScopeType = getPreferredBaselineScopeType(data)
  const { data: baselineData, isLoading: baselineLoading } = useQuery({
    queryKey: ['run-baseline', runIdNum, preferredBaselineScopeType],
    queryFn: () => runApi.getRunBaseline(runIdNum, { scope_type: preferredBaselineScopeType }),
    enabled: Number.isFinite(runIdNum),
  })
  const { data: verdictData, isLoading: verdictLoading } = useQuery({
    queryKey: ['run-verdict', runIdNum, data?.run_status ?? null, data?.ended_at ?? null],
    queryFn: () => runApi.getRunVerdict(runIdNum),
    enabled: Number.isFinite(runIdNum),
  })
  const { data: aiAnalystData, isLoading: aiAnalystLoading } = useQuery({
    queryKey: ['run-ai-analyst', runIdNum, data?.run_status ?? null, data?.ended_at ?? null],
    queryFn: () => runApi.getRunAIAnalyst(runIdNum),
    enabled: aiFeaturesEnabled && Number.isFinite(runIdNum),
  })
  const {
    data: aiReportData,
    isLoading: aiReportLoading,
    error: aiReportError,
  } = useQuery({
    queryKey: ['run-ai-report-latest', runIdNum],
    queryFn: () => runApi.getLatestRunAIReport(runIdNum),
    enabled: aiFeaturesEnabled && Number.isFinite(runIdNum),
    retry: false,
  })
  const { data: aiReportTaskStatus } = useQuery({
    queryKey: ['run-ai-report-task', runIdNum, pendingAIReportTask?.taskId, pendingAIReportTask?.reportId],
    queryFn: () => runApi.getAsyncRunAIReportTask(
      runIdNum,
      String(pendingAIReportTask?.taskId),
      Number(pendingAIReportTask?.reportId),
    ),
    enabled: aiFeaturesEnabled && Number.isFinite(runIdNum) && Boolean(pendingAIReportTask?.taskId && pendingAIReportTask.reportId),
    refetchInterval: current => (current?.state.data?.completed ? false : 1_500),
    retry: false,
  })

  useEffect(() => {
    if (!aiFeaturesEnabled) {
      return
    }
    if (!pendingAIReportTask || !aiReportTaskStatus?.completed) {
      return
    }
    const result = aiReportTaskStatus.result ?? null
    if (result) {
      queryClient.setQueryData<RunAIReportSummary>(['run-ai-report-latest', runIdNum], result)
      if (result.status === 'success') {
        message.success('AI Report 已生成')
      } else {
        const failure = parseAIReportFailureMessage(result.error_message)
        message.warning(failure.description)
      }
    } else if (aiReportTaskStatus.error) {
      message.warning(aiReportTaskStatus.error)
    } else {
      message.warning('AI Report 任务已结束，但未返回报告结果；请刷新后确认。')
    }
    setPendingAIReportTask(null)
    queryClient.invalidateQueries({ queryKey: ['run-ai-report-latest', runIdNum] })
  }, [aiFeaturesEnabled, aiReportTaskStatus, pendingAIReportTask, queryClient, runIdNum])

  useEffect(() => {
    if (!aiFeaturesEnabled) {
      return
    }
    if (!aiReportData) {
      return
    }
    setAIReportFeedbackRating(aiReportData.feedback_rating ?? 'neutral')
    setAIReportFeedbackNote(aiReportData.feedback_note ?? '')
    setAIReportFeedbackAction(aiReportData.feedback_action ?? undefined)
  }, [aiFeaturesEnabled, aiReportData])

  const hasK6ControlContext = useMemo(() => {
    const rawParams = (data?.params ?? {}) as Record<string, unknown>
    const topLevelContext =
      typeof rawParams.agent_host === 'string'
      && rawParams.agent_host.trim()
      && typeof rawParams.agent_run_token === 'string'
      && rawParams.agent_run_token.trim()
    if (topLevelContext) {
      return true
    }
    const agentRuns = Array.isArray(rawParams.agent_runs) ? rawParams.agent_runs : []
    return agentRuns.some(item => (
      item
      && typeof item === 'object'
      && typeof (item as Record<string, unknown>).agent_host === 'string'
      && String((item as Record<string, unknown>).agent_host || '').trim()
      && typeof (item as Record<string, unknown>).agent_run_token === 'string'
      && String((item as Record<string, unknown>).agent_run_token || '').trim()
    ))
  }, [data?.params])
  const {
    data: k6ControlData,
    isLoading: k6ControlLoading,
    error: k6ControlError,
  } = useQuery({
    queryKey: ['run-k6-control', runIdNum],
    queryFn: () => runApi.getRunK6Control(runIdNum),
    enabled: dynamicK6ControlEnabled && Number.isFinite(runIdNum) && data?.engine_type === 'k6' && hasK6ControlContext,
    refetchInterval: dynamicK6ControlEnabled && data?.run_status === 'running' && data?.engine_type === 'k6' && hasK6ControlContext ? 5_000 : false,
    retry: false,
  })
  const { data: k6ControlTaskStatus } = useQuery({
    queryKey: ['run-k6-control-task', runIdNum, pendingK6ControlTask?.taskId],
    queryFn: () => runApi.getAsyncRunK6ControlTask(runIdNum, String(pendingK6ControlTask?.taskId)),
    enabled: dynamicK6ControlEnabled && Number.isFinite(runIdNum) && Boolean(pendingK6ControlTask?.taskId),
    refetchInterval: current => (current?.state.data?.completed ? false : 1_500),
    retry: false,
  })
  const { data: k6TaskLastRunParams, isFetching: k6TaskLastRunParamsFetching } = useQuery({
    queryKey: ['run-detail-k6-task-last-run-params', data?.task_id],
    queryFn: () => taskApi.getTaskLastRunParams(Number(data?.task_id)),
    enabled: Number.isFinite(Number(data?.task_id)) && data?.engine_type === 'k6' && data?.run_status === 'running',
    staleTime: 0,
  })
  useEffect(() => {
    if (!pendingK6ControlTask || !k6ControlTaskStatus?.completed) {
      return
    }
    const result = k6ControlTaskStatus.result ?? null
    if (result) {
      queryClient.setQueryData<RunK6ControlResponse>(['run-k6-control', runIdNum], result)
      const strategyLabel = result.summary.control_strategy === 'scenario_direct'
        ? '直接调整'
        : result.summary.control_strategy === 'auto_tps_fallback'
          ? '自动兜底'
          : '暂不可用'
      const nextTargetTps = typeof result.summary.target_tps === 'number'
        ? result.summary.target_tps
        : pendingK6ControlTask.targetTps ?? null
      const rawParams = (data?.params ?? {}) as Record<string, unknown>
      const baseTargetTps = coerceDisplayFloat(rawParams.target_tps ?? rawParams.base_target_tps ?? rawParams.fixed_tps)
      setK6TargetTpsInput(nextTargetTps)
      setK6TargetRatioInput(
        nextTargetTps != null && baseTargetTps && baseTargetTps > 0
          ? roundTo(nextTargetTps / baseTargetTps, 4)
          : null,
      )
      message.success(`已下发 K6 控制能力，当前策略 ${strategyLabel}`)
    } else if (k6ControlTaskStatus.error) {
      message.warning(formatK6ControlActionError(k6ControlTaskStatus.error))
    } else {
      message.warning('K6 控制能力任务已结束，但未返回控制结果；请刷新控制状态确认。')
    }
    setPendingK6ControlTask(null)
    queryClient.invalidateQueries({ queryKey: ['run-k6-control', runIdNum] })
    queryClient.invalidateQueries({ queryKey: ['run-process', runIdNum] })
  }, [data?.params, k6ControlTaskStatus, pendingK6ControlTask, queryClient, runIdNum])
  const { data: k6SucceededRunList, isFetching: k6SucceededRunListFetching } = useQuery({
    queryKey: ['run-detail-k6-last-successful-runs', data?.task_id],
    queryFn: () => runApi.getRunList({
      page: 1,
      pageSize: 3,
      task_id: Number(data?.task_id),
      engine_type: 'k6',
      run_status: 'succeeded',
    }),
    enabled: Number.isFinite(Number(data?.task_id)) && data?.engine_type === 'k6' && data?.run_status === 'running',
    staleTime: 0,
  })
  const k6LastSuccessfulRunId = useMemo(() => {
    const items = k6SucceededRunList?.items ?? []
    const matched = items.find(item => Number(item.run_id) !== runIdNum)
    return matched?.run_id ?? null
  }, [k6SucceededRunList?.items, runIdNum])
  const { data: k6LastSuccessfulRunDetail, isFetching: k6LastSuccessfulRunDetailFetching } = useQuery({
    queryKey: ['run-detail-k6-last-successful-run-detail', k6LastSuccessfulRunId],
    queryFn: () => runApi.getRunDetail(Number(k6LastSuccessfulRunId)),
    enabled: Number.isFinite(Number(k6LastSuccessfulRunId)) && Number(k6LastSuccessfulRunId) > 0,
    staleTime: 0,
  })
  const k6LastSuccessfulConfig = useMemo<K6LastSuccessfulConfig | null>(() => {
    const lastSuccessRunTargetTps = resolveK6TargetTpsFromParams(k6LastSuccessfulRunDetail?.params)
    if (typeof lastSuccessRunTargetTps === 'number') {
      return {
        runId: k6LastSuccessfulRunDetail?.run_id ?? k6LastSuccessfulRunId,
        targetTps: roundTo(lastSuccessRunTargetTps, 4),
        startedAt: k6LastSuccessfulRunDetail?.started_at ?? null,
        status: k6LastSuccessfulRunDetail?.run_status ?? 'succeeded',
        source: 'recent_success_run',
      }
    }

    const lastRunTargetTps = resolveK6TargetTpsFromParams(k6TaskLastRunParams?.last_run_params)
    if (
      typeof lastRunTargetTps === 'number'
      && isSucceededRunStatus(k6TaskLastRunParams?.last_run_status)
      && Number(k6TaskLastRunParams?.last_run_id) !== runIdNum
    ) {
      return {
        runId: k6TaskLastRunParams?.last_run_id ?? null,
        targetTps: roundTo(lastRunTargetTps, 4),
        startedAt: k6TaskLastRunParams?.last_run_started_at ?? null,
        status: k6TaskLastRunParams?.last_run_status ?? null,
        source: 'task_last_run_params',
      }
    }

    return null
  }, [
    k6LastSuccessfulRunDetail,
    k6LastSuccessfulRunId,
    k6TaskLastRunParams?.last_run_id,
    k6TaskLastRunParams?.last_run_params,
    k6TaskLastRunParams?.last_run_started_at,
    k6TaskLastRunParams?.last_run_status,
    runIdNum,
  ])
  const k6LastSuccessfulConfigLoading = k6TaskLastRunParamsFetching
    || k6SucceededRunListFetching
    || k6LastSuccessfulRunDetailFetching

  // Pod 状态列表
  const { data: podsData } = useQuery({
    queryKey: ['run-pods', runIdNum],
    queryFn: () => runApi.getRunPods(runIdNum),
    enabled: Number.isFinite(runIdNum),
    refetchInterval: isActiveRunStatus(data?.run_status) ? 5_000 : false,
  })

  // Pod 资源监控数据
  const { data: podsMonitorData, isLoading: podsMonitorLoading } = useQuery({
    queryKey: ['run-pods-monitor', runIdNum],
    queryFn: () => runApi.getRunPodsMonitor(runIdNum, { step_seconds: 10 }),
    enabled: Number.isFinite(runIdNum),
    refetchInterval: data?.run_status === 'running' ? 10_000 : false,
  })

  // Dashboard 入口列表
  const { data: dashboardsData, isLoading: dashboardsLoading } = useQuery({
    // Active runs use Grafana's relative live window, so the iframe can self-refresh
    // without being recreated by page polling. When the run flips to terminal, refetch
    // once so iframe src picks up the final absolute started_at/ended_at window.
    queryKey: ['run-dashboards', runIdNum, data?.run_status ?? null, data?.ended_at ?? null],
    queryFn: () => runApi.getRunDashboards(runIdNum),
    enabled: Number.isFinite(runIdNum),
    refetchInterval: false,
  })
  const {
    data: alertEventsData,
    isLoading: alertEventsLoading,
    isError: alertEventsIsError,
  } = useQuery({
    queryKey: ['run-alert-events', runIdNum, data?.run_status ?? null, data?.ended_at ?? null],
    queryFn: () => runApi.getRunAlertEvents(runIdNum),
    enabled: Number.isFinite(runIdNum),
    refetchInterval: isActiveRunStatus(data?.run_status) ? 10_000 : false,
    retry: false,
  })

  const loadLogs = async (reset = false) => {
    if (!Number.isFinite(runIdNum)) return
    setLoadingLogs(true)
    try {
      const res = await runApi.getRunLogs(runIdNum, {
        cursor: reset ? undefined : nextCursor || undefined,
        limit: 100,
        view: logView,
        order: logOrderAsc ? 'asc' : 'desc',
      })
      setNextCursor(res.next_cursor)
      setLogs(currentLogs => (reset ? res.items : [...currentLogs, ...res.items]))
    } catch (e) {
      message.error('加载日志失败')
    } finally {
      setLoadingLogs(false)
    }
  }

  useEffect(() => {
    setLogs([])
    setNextCursor(null)
    loadLogs(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runIdNum, logOrderAsc, logView])

  useEffect(() => {
    setStablePodGrafanaIframeState({ url: null, selectionKey: '__single__' })
    setStableEngineGrafanaIframeUrl(null)
    setStableMonitorInfoWallItems([])
    setK6TargetTpsInput(null)
    setK6TargetRatioInput(null)
    setK6ControlInputMode('total_tps')
  }, [runIdNum])

  useEffect(() => {
    if (!k6ControlData?.summary) {
      return
    }
    const rawParams = (data?.params ?? {}) as Record<string, unknown>
    const baseTargetTps = coerceDisplayFloat(rawParams.target_tps ?? rawParams.base_target_tps ?? rawParams.fixed_tps)
    const suggestedTargetTps = typeof k6ControlData.summary.target_tps === 'number'
      ? k6ControlData.summary.target_tps
      : baseTargetTps
    setK6TargetTpsInput(current => current ?? suggestedTargetTps ?? null)
    setK6TargetRatioInput(current => (
      current
      ?? (
        suggestedTargetTps != null && baseTargetTps && baseTargetTps > 0
          ? roundTo(suggestedTargetTps / baseTargetTps, 4)
          : null
      )
    ))
  }, [data?.params, k6ControlData])

  if (error) {
    message.error('加载 Run 失败')
  }

  const chartTheme = useMemo(() => (
    theme === 'dark'
      ? {
        text: '#a8b4c2',
        title: '#f4f8fb',
        split: 'rgba(148, 163, 184, 0.12)',
        axis: 'rgba(148, 163, 184, 0.18)',
        tooltipBg: '#0a1017',
        tooltipBorder: '#273244',
        palette: ['#14b8a6', '#22c55e', '#f59e0b', '#ff5c6c', '#5eead4'],
      }
      : {
        text: '#526173',
        title: '#0f172a',
        split: '#e5e7eb',
        axis: '#cbd5e1',
        tooltipBg: '#ffffff',
        tooltipBorder: '#d9e1e8',
        palette: ['#0d9488', '#16a34a', '#b7791f', '#d92d20', '#059669'],
      }
  ), [theme])

  const buildEndpointTrendOption = (
    trendItems: EndpointTrendSeries[] | undefined,
    unitLabel: string,
    stepSeconds?: number,
  ) => {
    const validItems = (trendItems ?? []).filter(item => item.points.length > 0)
    // 采样步长的 3 倍视为"理应有点却缺失" → 当成 gap 插入断点，
    // 让 run 期间 runtime 停摆（如宿主机 Idle Sleep、Docker VM 冻结、Prometheus 抓取中断）
    // 在曲线上明确断开，与 Grafana 视觉一致，避免被 ECharts smooth 拉成"假平稳"
    const resolvedStep = stepSeconds && stepSeconds > 0 ? stepSeconds : 5
    const gapMs = Math.max(resolvedStep * 3, 60) * 1000

    const buildSeriesData = (item: EndpointTrendSeries) => {
      const parsed = item.points
        .map(point => {
          const ts = Date.parse(point.ts)
          return Number.isNaN(ts) || point.value == null
            ? null
            : ([ts, point.value] as [number, number])
        })
        .filter((point): point is [number, number] => Array.isArray(point))
        .sort((a, b) => a[0] - b[0])

      if (parsed.length === 0) {
        return []
      }

      const withGaps: Array<[number, number] | [number, null]> = []
      for (let i = 0; i < parsed.length; i += 1) {
        const current = parsed[i]
        if (i > 0) {
          const previous = parsed[i - 1]
          if (current[0] - previous[0] > gapMs) {
            // 在 gap 区间中插入一个 null 让 connectNulls=false 起作用
            withGaps.push([previous[0] + gapMs / 2, null])
          }
        }
        withGaps.push(current)
      }
      return withGaps
    }

    return {
      color: chartTheme.palette,
      tooltip: {
        trigger: 'axis' as const,
        backgroundColor: chartTheme.tooltipBg,
        borderColor: chartTheme.tooltipBorder,
        textStyle: { color: chartTheme.title },
        axisPointer: {
          lineStyle: {
            color: chartTheme.axis,
          },
        },
      },
      grid: { left: 42, right: 22, top: 24, bottom: 34 },
      legend: {
        show: false,
        top: 0,
        type: 'scroll' as const,
        textStyle: { color: chartTheme.text },
      },
      xAxis: {
        type: 'time' as const,
        splitNumber: 4,
        axisLine: { lineStyle: { color: chartTheme.axis } },
        axisTick: { lineStyle: { color: chartTheme.axis } },
        axisLabel: {
          color: chartTheme.text,
          rotate: 0,
          hideOverlap: true,
          margin: 10,
          formatter: (value: number) => formatCompactTimeAxisLabel(new Date(value).toISOString()),
        },
        splitLine: {
          show: false,
        },
      },
      yAxis: {
        type: 'value' as const,
        name: unitLabel,
        nameTextStyle: { color: chartTheme.text },
        axisLine: { lineStyle: { color: chartTheme.axis } },
        axisTick: { lineStyle: { color: chartTheme.axis } },
        axisLabel: { color: chartTheme.text },
        splitLine: {
          lineStyle: {
            color: chartTheme.split,
          },
        },
        scale: true,
      },
      series: validItems.map(item => ({
        name: item.endpoint_name,
        type: 'line' as const,
        smooth: true,
        showSymbol: false,
        connectNulls: false,
        lineStyle: { width: 2 },
        emphasis: { focus: 'series' as const },
        data: buildSeriesData(item),
      })),
    }
  }

  const fallbackThroughputItems = useMemo(
    () => buildFallbackTrendItems('throughput', metricsData?.series),
    [metricsData?.series],
  )
  const fallbackLatencyItems = useMemo(
    () => buildFallbackTrendItems(latencyMetric, metricsData?.series),
    [latencyMetric, metricsData?.series],
  )
  const sanitizeEndpointTrendItems = (items?: EndpointTrendSeries[]): EndpointTrendSeries[] => {
    return (items ?? [])
      .map(item => ({
        ...item,
        points: (item.points ?? []).filter(point => {
          const ts = new Date(point.ts).getTime()
          return Number.isFinite(ts) && ts >= Date.UTC(2000, 0, 1)
        }),
      }))
      .filter(item => item.points.length > 0)
  }
  const focusedThroughputItems = useMemo(
    () => sanitizeEndpointTrendItems(throughputTrendData?.items),
    [throughputTrendData?.items],
  )
  const focusedLatencyItems = useMemo(
    () => sanitizeEndpointTrendItems(latencyTrendData?.items),
    [latencyTrendData?.items],
  )
  const effectiveThroughputItems = focusedThroughputItems.length > 0 ? focusedThroughputItems : fallbackThroughputItems
  const effectiveLatencyItems = focusedLatencyItems.length > 0 ? focusedLatencyItems : fallbackLatencyItems
  const throughputChartOption = useMemo(
    () => buildEndpointTrendOption(
      effectiveThroughputItems,
      'rep/s',
      throughputTrendData?.step_seconds ?? metricsData?.step_seconds,
    ),
    [chartTheme, effectiveThroughputItems, throughputTrendData?.step_seconds, metricsData?.step_seconds],
  )
  const latencyChartOption = useMemo(
    () => buildEndpointTrendOption(
      effectiveLatencyItems,
      'ms',
      latencyTrendData?.step_seconds ?? metricsData?.step_seconds,
    ),
    [chartTheme, effectiveLatencyItems, latencyTrendData?.step_seconds, metricsData?.step_seconds],
  )
  const hasThroughputTrend = (effectiveThroughputItems?.length ?? 0) > 0
  const hasLatencyTrend = (effectiveLatencyItems?.length ?? 0) > 0
  const endpointTrendLoading = throughputTrendLoading || latencyTrendLoading
  const runDetailHeroStats = useMemo(() => {
    const metricsSeriesByName = new Map((metricsData?.series ?? []).map(item => [item.metric, item]))
    const overall = summaryMetricsData?.items?.find(item => item.endpoint_name === 'overall')
    const throughput =
      overall?.throughput
      ?? data?.overview_summary?.throughput
      ?? data?.rps
      ?? getLatestTrendThroughput(effectiveThroughputItems)
      ?? getLatestSeriesValue(metricsSeriesByName.get('rps'))
    const avgRt =
      overall?.avg_rt_ms
      ?? data?.overview_summary?.avg_rt_ms
      ?? data?.avg_rt_ms
      ?? getLatestSeriesValue(metricsSeriesByName.get('rt_avg_ms'))
    const p95 =
      overall?.p95_rt_ms
      ?? data?.overview_summary?.p95_rt_ms
      ?? data?.p95_rt_ms
      ?? getLatestSeriesValue(metricsSeriesByName.get('rt_p95_ms'))
    const totalRequests =
      overall?.total_requests
      ?? data?.overview_summary?.total_requests
      ?? data?.total_requests
      ?? null
    const agentTotal = data?.pod_total ?? null
    const agentCompleted = data?.pod_completed ?? 0
    const agentActive = data?.pod_actual ?? 0
    const agentValue = agentTotal != null ? `${agentCompleted}/${agentActive}/${agentTotal}` : `${agentCompleted}/${agentActive}/-`

    return [
      {
        key: 'throughput',
        label: '当前吞吐',
        value: fmt(throughput, '', 1),
        unit: 'req/s',
        helper: hasThroughputTrend ? '趋势已接入' : '等待趋势数据',
      },
      {
        key: 'p95',
        label: 'P95 响应',
        value: fmt(p95, '', 1),
        unit: 'ms',
        helper: avgRt != null ? `avg ${fmt(avgRt, '', 1)} ms` : '平均响应待接入',
      },
      {
        key: 'requests',
        label: '请求总量',
        value: fmt(totalRequests, '', 0),
        unit: 'count',
        helper: data?.engine_type_label || data?.engine_type?.toUpperCase() || 'engine',
      },
      {
        key: 'agents',
        label: '执行节点',
        value: agentValue,
        unit: 'done/run/all',
        helper: agentTotal != null ? '完成/执行/总数' : '总数待接入',
      },
    ]
  }, [
    data?.avg_rt_ms,
    data?.engine_type,
    data?.engine_type_label,
    data?.overview_summary?.avg_rt_ms,
    data?.overview_summary?.p95_rt_ms,
    data?.overview_summary?.throughput,
    data?.overview_summary?.total_requests,
    data?.p95_rt_ms,
    data?.pod_actual,
    data?.pod_completed,
    data?.pod_total,
    data?.rps,
    data?.total_requests,
    effectiveThroughputItems,
    hasThroughputTrend,
    metricsData?.series,
    summaryMetricsData?.items,
  ])

  const throughputEndpoints = useMemo(
    () => [...new Set((effectiveThroughputItems ?? []).map(item => item.endpoint_name))],
    [effectiveThroughputItems],
  )
  const latencyEndpoints = useMemo(
    () => [...new Set((effectiveLatencyItems ?? []).map(item => item.endpoint_name))],
    [effectiveLatencyItems],
  )

  const renderEndpointLegend = (endpoints: string[], testId: string) => {
    const normalizedEndpoints = endpoints.filter(endpoint => endpoint && endpoint !== 'overall')
    if (normalizedEndpoints.length <= 1) {
      return null
    }

    return (
      <div
        data-testid={testId}
        style={{
          display: 'flex',
          gap: 8,
          flexWrap: 'wrap',
          marginTop: 6,
          color: 'var(--text-secondary)',
          fontSize: 11,
        }}
      >
        {normalizedEndpoints.map((endpoint, index) => (
          <span
            key={endpoint}
            title={endpoint}
            className="olh-endpoint-legend-item"
            style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}
          >
            <span
              aria-hidden
              style={{
                width: 6,
                height: 6,
                borderRadius: 999,
                background: chartTheme.palette[index % chartTheme.palette.length],
                opacity: 0.85,
              }}
            />
            <span>{endpoint}</span>
          </span>
        ))}
      </div>
    )
  }

  const podGrafanaDashboard = useMemo(
    () => dashboardsData?.items?.find(item => item.dashboard_type === 'pod_grafana'),
    [dashboardsData],
  )
  const engineGrafanaDashboard = useMemo(
    () => dashboardsData?.items?.find(item => item.dashboard_type === 'engine_grafana'),
    [dashboardsData],
  )
  const relatedMonitorDashboards = useMemo(
    () => dashboardsData?.items?.filter(item => item.dashboard_type === 'related_monitor') ?? [],
    [dashboardsData],
  )
  const runParams = data?.params ?? null
  const seedSummaryMetricItems = useMemo(() => parseSummaryMetricSeedRows(runParams), [runParams])
  const agentRunEntries = useMemo(() => parseAgentRunEntries(runParams), [runParams])
  const dashboardSummary: RunDashboardSummary | undefined = dashboardsData?.summary
  const previousRunStatusRef = useRef<string | null>(null)

  useEffect(() => {
    const previousStatus = previousRunStatusRef.current
    const currentStatus = data?.run_status ?? null
    previousRunStatusRef.current = currentStatus

    if (!Number.isFinite(runIdNum)) {
      return
    }
    if (!previousStatus || !isActiveRunStatus(previousStatus) || !currentStatus || isActiveRunStatus(currentStatus)) {
      return
    }

    void Promise.all([
      queryClient.invalidateQueries({ queryKey: ['run-summary-metrics', runIdNum] }),
      queryClient.invalidateQueries({ queryKey: ['run-checks', runIdNum] }),
      queryClient.invalidateQueries({ queryKey: ['run-endpoint-trends', runIdNum] }),
      queryClient.invalidateQueries({ queryKey: ['run-process', runIdNum] }),
      queryClient.invalidateQueries({ queryKey: ['run-pods', runIdNum] }),
      queryClient.invalidateQueries({ queryKey: ['run-pods-monitor', runIdNum] }),
      queryClient.invalidateQueries({ queryKey: ['run-dashboards', runIdNum] }),
      queryClient.invalidateQueries({ queryKey: ['run-metrics', runIdNum] }),
    ])
  }, [data?.run_status, queryClient, runIdNum])

  const nodeSwitchTargets = useMemo(() => {
    const targets: RunNodeTarget[] = []

    const upsertTarget = ({
      podIp,
      podName,
      nodeName,
      agentHost,
      sourceHints = [],
    }: {
      podIp?: string | null
      podName?: string | null
      nodeName?: string | null
      agentHost?: string | null
      sourceHints?: string[]
    }) => {
      const normalized = {
        podIp: getTextValue(podIp),
        podName: getTextValue(podName),
        nodeName: getTextValue(nodeName),
        agentHost: getTextValue(agentHost),
        sourceHints: uniqueTextValues(sourceHints),
      }
      const matchTokens = uniqueTextValues([normalized.podIp, normalized.podName, normalized.nodeName, ...normalized.sourceHints])
      const existing = normalized.agentHost
        ? targets.find(target => normalizeText(target.agentHost) === normalizeText(normalized.agentHost))
        : targets.find(target =>
            !target.agentHost && matchTokens.some(token => getRunNodeTargetTokens(target).includes(token)),
          )

      if (existing) {
        existing.agentHost = existing.agentHost ?? normalized.agentHost
        existing.podIp = existing.podIp ?? normalized.podIp
        existing.podName = existing.podName ?? normalized.podName
        existing.nodeName = existing.nodeName ?? normalized.nodeName
        existing.sourceHints = uniqueTextValues([...existing.sourceHints, ...normalized.sourceHints])
        existing.label = buildRunNodeTargetLabel(existing)
        return existing
      }

      const key = buildRunNodeIdentityKey(normalized) ?? `node-${targets.length + 1}`
      const created: RunNodeTarget = {
        key,
        label: '',
        podIp: normalized.podIp,
        podName: normalized.podName,
        nodeName: normalized.nodeName,
        agentHost: normalized.agentHost,
        sourceHints: normalized.sourceHints,
      }
      created.label = buildRunNodeTargetLabel(created)
      targets.push(created)
      return created
    }

    ;(podsData?.items ?? []).forEach((item, index) => {
      const agentRun = agentRunEntries[index]
      upsertTarget({
        podIp: item.pod_ip,
        podName: item.pod_name ?? agentRun?.pod_name,
        nodeName: item.node_name ?? agentRun?.node_name,
        agentHost: item.agent_host ?? agentRun?.agent_host ?? agentRun?.agent_ip,
        sourceHints: uniqueTextValues([
          item.agent_host,
          item.pod_ip,
          agentRun?.agent_host,
          agentRun?.agent_ip,
        ]),
      })
    })

    if (targets.length === 0) {
      agentRunEntries.forEach(entry =>
        upsertTarget({
          podIp: entry.agent_ip,
          podName: entry.pod_name,
          nodeName: entry.node_name,
          agentHost: entry.agent_host,
        }),
      )
    }

    if (targets.length === 0) {
      upsertTarget({
        podIp: getTextValue(runParams?.agent_ip),
        agentHost: getTextValue(runParams?.agent_host),
      })
    }

    logs.forEach(item => {
      const source = getTextValue(item.source)
      if (!source) {
        return
      }

      const matched = targets.find(target =>
        getRunNodeTargetTokens(target).some(token => normalizeText(source).includes(normalizeText(token))),
      )

      if (matched) {
        matched.sourceHints = uniqueTextValues([...matched.sourceHints, source])
        matched.label = buildRunNodeTargetLabel(matched)
      } else if (targets.length === 0) {
        upsertTarget({ agentHost: source, sourceHints: [source] })
      }
    })

    const duplicatedPodIps = new Set(
      targets
        .map(target => normalizeText(target.podIp))
        .filter(Boolean)
        .filter((podIp, index, list) => list.indexOf(podIp) !== index),
    )

    return targets.map(target => ({
      ...target,
      label: buildRunNodeTargetLabel(target, { preferAgentHost: duplicatedPodIps.has(normalizeText(target.podIp)) }),
      sourceHints: uniqueTextValues(target.sourceHints),
    }))
  }, [agentRunEntries, logs, podsData, runParams])
  const nodeSwitchOptions = useMemo(
    () => nodeSwitchTargets.map(target => ({ label: target.label, value: target.key })),
    [nodeSwitchTargets],
  )
  const hasMultipleNodes = nodeSwitchOptions.length > 1

  useEffect(() => {
    if (nodeSwitchTargets.length === 0) {
      setSelectedNodeKey(null)
      return
    }

    setSelectedNodeKey(current =>
      current && nodeSwitchTargets.some(target => target.key === current) ? current : nodeSwitchTargets[0].key,
    )
  }, [nodeSwitchTargets])

  const selectedNodeTarget = useMemo(
    () => nodeSwitchTargets.find(target => target.key === selectedNodeKey) ?? nodeSwitchTargets[0] ?? null,
    [nodeSwitchTargets, selectedNodeKey],
  )
  const selectedNodeLabel = selectedNodeTarget?.label ?? '日志终端'
  const logWorkspaceTargetLabel = useMemo(() => {
    if (selectedNodeTarget?.label) {
      return selectedNodeTarget.label
    }

    const primaryPod = podsData?.items?.find(item => item.pod_ip || item.pod_name)
    if (primaryPod?.pod_ip) {
      return primaryPod.pod_ip
    }
    if (primaryPod?.pod_name) {
      return primaryPod.pod_name
    }

    const agentHost = data?.params?.agent_host
    if (typeof agentHost === 'string' && agentHost.trim()) {
      return agentHost
    }

    const agentIp = data?.params?.agent_ip
    if (typeof agentIp === 'string' && agentIp.trim()) {
      return agentIp
    }

    return '日志终端'
  }, [data?.params, podsData, selectedNodeTarget])
  const filteredLogs = useMemo(() => {
    if (!selectedNodeTarget || nodeSwitchTargets.length <= 1) {
      return logs
    }

    return logs.filter(item => logMatchesRunNodeTarget(item, selectedNodeTarget))
  }, [logs, nodeSwitchTargets.length, selectedNodeTarget])
  const failedCheckItems = useMemo(
    () => (checksData?.items ?? []).filter(item => typeof item.success_rate === 'number' && item.success_rate < 1),
    [checksData],
  )
  const toolTerminalLogs = useMemo(
    () => filteredLogs.filter(item => resolveLogSourceKind(item) === 'tool'),
    [filteredLogs],
  )
  const platformEventLogs = useMemo(
    () => filteredLogs.filter(item => resolveLogSourceKind(item) === 'platform'),
    [filteredLogs],
  )
  const terminalDisplayLogs = useMemo(
    () => (toolTerminalLogs.length > 0 ? toolTerminalLogs : platformEventLogs),
    [platformEventLogs, toolTerminalLogs],
  )
  const exceptionSummaryLogs = useMemo(
    () =>
      toolTerminalLogs
        .filter(item => ['error', 'fatal', 'critical', 'warn', 'warning'].includes(String(item.level || '').toLowerCase()))
        .slice(0, 6),
    [toolTerminalLogs],
  )

  const monitorIdentityCount = useMemo(
    () =>
      new Set(
        [
          ...(podsData?.items ?? []).map(item =>
            buildRunNodeIdentityKey({
              agentHost: item.agent_host,
              podIp: item.pod_ip,
              podName: item.pod_name,
              nodeName: item.node_name,
            }),
          ),
          ...(podsMonitorData?.series ?? []).map(item =>
            buildRunNodeIdentityKey({
              agentHost: item.agent_host,
              podIp: item.pod_ip,
              podName: item.pod_name,
            }),
          ),
        ].filter((item): item is string => Boolean(item)),
      ).size,
    [podsData, podsMonitorData],
  )

  const monitorHealthItems = useMemo(() => {
    const podCount = podsData?.items?.length ?? 0
    const metricsPodCount = monitorIdentityCount
    const relatedMonitorCount = dashboardSummary?.related_monitor_total ?? 0
    const hasEngineGrafana = dashboardSummary?.has_engine_grafana ?? Boolean(engineGrafanaDashboard?.url)
    const hasPodGrafana = dashboardSummary?.has_pod_grafana ?? Boolean(podGrafanaDashboard?.url)

    return [
      { label: 'Pod 状态', value: podCount > 0 ? `${podCount} 个` : '未上报', status: podCount > 0 ? 'green' : 'default' },
      {
        label: '资源时序',
        value: metricsPodCount > 0 ? `${metricsPodCount} 个执行节点` : '未上报',
        status: metricsPodCount > 0 ? 'green' : 'default',
      },
      {
        label: 'Pod Grafana',
        value: hasPodGrafana ? '已接通' : '未配置',
        status: hasPodGrafana ? 'blue' : 'default',
      },
      {
        label: `${data?.engine_type?.toUpperCase() || '引擎'} Grafana`,
        value: hasEngineGrafana ? '已接通' : '未配置',
        status: hasEngineGrafana ? 'blue' : 'default',
      },
      {
        label: '关联监控',
        value: relatedMonitorCount > 0 ? `${relatedMonitorCount} 个` : '无',
        status: relatedMonitorCount > 0 ? 'green' : 'default',
      },
    ]
  }, [dashboardSummary, data?.engine_type, engineGrafanaDashboard, monitorIdentityCount, podGrafanaDashboard, podsData])

  const logTerminalContent = useMemo(
    () =>
      terminalDisplayLogs
        .map(item => {
          const rawLog = item.raw_message || item.message || ''
          return rawLog.endsWith('\n') ? rawLog : `${rawLog}\n`
        })
        .join('')
        .trimEnd(),
    [terminalDisplayLogs],
  )

  const monitorWindowLabel = useMemo(() => {
    if (isActiveRunStatus(data?.run_status)) {
      return '最近 5 分钟（实时）'
    }
    const start = formatDateTime(data?.started_at)
    const end = formatDateTime(data?.ended_at)
    if (start === '-' && end === '-') {
      return '时间窗待补'
    }
    return `${start} 至 ${end}`
  }, [data?.ended_at, data?.run_status, data?.started_at])

  const monitorInfoWallItems = useMemo(() => {
    const summary = podsMonitorData?.summary
    const dashboardTotal = dashboardSummary?.total_dashboard_count ?? dashboardsData?.items?.length ?? 0
    const podCount = monitorIdentityCount || (podsData?.items?.length ?? 0)
    const aggregatedScopeHelper = podCount > 0 ? `${podCount} 个执行节点聚合值 · 默认按 pod_ip 明细；同 IP 时按节点补充分流` : '聚合值待补'
    const resourceScopeLabel = summary?.resource_scope_label || '--'
    const resourceScopeHelper =
      summary?.runtime_kind === 'host'
        ? '当前按 Host / EC2 资源口径展示：CPU / MEM / Socket / network / disk I/O 以 agent 进程树为主，CPU Load / 磁盘容量保留宿主机口径'
        : summary?.runtime_kind === 'docker' || summary?.runtime_kind === 'k8s'
          ? '当前按 Docker / K8S 资源口径展示容器资源'
          : '当前监控对象口径待识别'

    return [
      {
        label: '观测窗口',
        value: monitorWindowLabel,
        helper: isActiveRunStatus(data?.run_status)
          ? '运行中固定显示最近 5 分钟实时窗口 · 结束后切真实压测时段'
          : podCount > 0
            ? `${podCount} 个执行节点聚合窗口 · 默认按 pod_ip 明细；同 IP 时按节点补充分流`
            : '未上报',
      },
      {
        label: '观测口径',
        value: resourceScopeLabel,
        helper: resourceScopeHelper,
      },
      {
        label: 'Pod CPU 峰值',
        value: summary?.cpu_summary_label || '--',
        helper: aggregatedScopeHelper,
      },
      {
        label: 'Pod 内存峰值',
        value: summary?.memory_summary_label || '--',
        helper: summary?.memory_usage_peak_percent != null ? aggregatedScopeHelper : '聚合值待补',
      },
      {
        label: '网络峰值',
        value: summary?.network_summary_label || '--',
        helper:
          summary?.runtime_kind === 'host'
            ? 'host / EC2 口径下 network / disk 当前以保守模式处理；显示留空时表示不提供可横比的宿主机累计量'
            : podCount > 0
              ? '收/发聚合值 · 默认按 pod_ip 明细；同 IP 时按节点补充分流'
              : '聚合值待补',
      },
      {
        label: 'Socket 峰值',
        value: summary?.runtime_summary_label || '--',
        helper: podCount > 0 ? '连接数聚合值 · 默认按 pod_ip 明细；同 IP 时按节点补充分流' : '聚合值待补',
      },
      {
        label: 'Dashboard 总数',
        value: String(dashboardTotal),
        helper: podGrafanaDashboard?.title ? 'Grafana-first · 默认按 pod_ip；同 IP 时按节点补充分流' : '待配置',
      },
    ]
  }, [dashboardSummary?.total_dashboard_count, dashboardsData?.items?.length, data?.run_status, monitorIdentityCount, monitorWindowLabel, podGrafanaDashboard?.title, podsData?.items?.length, podsMonitorData?.summary])

  useEffect(() => {
    setStableMonitorInfoWallItems(current =>
      mergeStableInfoWallItems(current, monitorInfoWallItems, isActiveRunStatus(data?.run_status)),
    )
  }, [data?.run_status, monitorInfoWallItems])

  const monitorAgentRunSeeds = useMemo(
    () => parseAgentRunEntries(data?.params).map(item => ({
      agentHost: item.agent_host,
      podIp: item.agent_ip,
      podName: item.pod_name,
      nodeName: item.node_name,
    })),
    [data?.params],
  )

  const monitorPodOptions = useMemo(() => {
    const targets: RunNodeTarget[] = []
    const upsert = (candidate: RunNodeLike) => {
      const normalized = {
        agentHost: getTextValue(candidate.agentHost),
        podIp: getTextValue(candidate.podIp),
        podName: getTextValue(candidate.podName),
        nodeName: getTextValue(candidate.nodeName),
        sourceHints: [] as string[],
      }
      const existing = normalized.agentHost
        ? targets.find(target => normalizeText(target.agentHost) === normalizeText(normalized.agentHost))
        : targets.find(target =>
            buildRunNodeIdentityKey(target)
            && buildRunNodeIdentityKey(target) === buildRunNodeIdentityKey(normalized),
          )
      const podIpExisting = existing ?? targets.find(target => shouldMergeMonitorPodTarget(target, normalized))
      if (podIpExisting) {
        podIpExisting.agentHost = resolveMonitorPodAgentHost(podIpExisting, normalized)
        podIpExisting.podIp = podIpExisting.podIp ?? normalized.podIp
        podIpExisting.podName = podIpExisting.podName ?? normalized.podName
        podIpExisting.nodeName = podIpExisting.nodeName ?? normalized.nodeName
        return
      }
      const key = buildRunNodeIdentityKey(normalized)
      if (!key) {
        return
      }
      targets.push({
        key,
        label: '',
        agentHost: normalized.agentHost,
        podIp: normalized.podIp,
        podName: normalized.podName,
        nodeName: normalized.nodeName,
        sourceHints: [],
      })
    }

    monitorAgentRunSeeds.forEach(item => upsert(item))
    ;(podsData?.items ?? []).forEach(item =>
      upsert({
        agentHost: item.agent_host,
        podIp: item.pod_ip,
        podName: item.pod_name,
        nodeName: item.node_name,
      }),
    )
    ;(podsMonitorData?.series ?? []).forEach(item =>
      upsert({
        agentHost: item.agent_host,
        podIp: item.pod_ip,
        podName: item.pod_name,
      }),
    )

    return targets.map(target => ({
      key: target.key,
      label: buildPodIpOnlyMonitorLabel(target),
      agentHost: target.agentHost ?? null,
      podIp: target.podIp ?? null,
      podName: target.podName ?? null,
    }))
  }, [monitorAgentRunSeeds, podsData, podsMonitorData])
  const hasMonitorPodSwitch = Boolean(podGrafanaDashboard?.url) && monitorPodOptions.length > 1
  const monitorPodSelectionKey = hasMonitorPodSwitch ? (selectedMonitorPodKey ?? '__all__') : '__single__'

  useEffect(() => {
    if (!hasMonitorPodSwitch) {
      setSelectedMonitorPodKey(null)
      return
    }
    setSelectedMonitorPodKey(current =>
      current && monitorPodOptions.some(option => option.key === current) ? current : null,
    )
  }, [hasMonitorPodSwitch, monitorPodOptions])

  const nextPodGrafanaIframeUrl = useMemo(() => {
    const baseUrl = podGrafanaDashboard?.url
    if (!baseUrl) {
      return null
    }
    try {
      const url = new URL(baseUrl)
      if (!String(url.searchParams.get('var-compose_service') || '').trim()) {
        url.searchParams.set('var-compose_service', '.*')
      }
      const nodeLabelParam = String(url.searchParams.get('var-node_label') || '').trim()
      if (!nodeLabelParam || nodeLabelParam === '.*') {
        url.searchParams.delete('var-node_label')
      }
      const agentHostParam = String(url.searchParams.get('var-agent_host') || '').trim()
      if (!agentHostParam || agentHostParam === '.*') {
        url.searchParams.delete('var-agent_host')
      }
      if (hasMonitorPodSwitch) {
        if (selectedMonitorPodKey) {
          const selectedOption = monitorPodOptions.find(option => option.key === selectedMonitorPodKey)
          if (selectedOption?.agentHost) {
            url.searchParams.set('var-agent_host', selectedOption.agentHost)
          }
          if (selectedOption?.podIp) {
            url.searchParams.set('var-pod_ip', selectedOption.podIp)
          }
          if (selectedOption?.podName) {
            url.searchParams.set('var-container_hint', selectedOption.podName)
          }
        } else {
          url.searchParams.delete('var-agent_host')
          url.searchParams.delete('var-pod_ip')
          url.searchParams.delete('var-container_hint')
        }
      }
      return url.toString()
    } catch {
      return baseUrl
    }
  }, [hasMonitorPodSwitch, monitorPodOptions, podGrafanaDashboard?.url, selectedMonitorPodKey])

  useEffect(() => {
    setStablePodGrafanaIframeState(current => {
      if (!nextPodGrafanaIframeUrl) {
        return { url: null, selectionKey: monitorPodSelectionKey }
      }
      const selectionChanged = current.selectionKey !== monitorPodSelectionKey
      if (!current.url || selectionChanged) {
        return { url: nextPodGrafanaIframeUrl, selectionKey: monitorPodSelectionKey }
      }
      if (!isActiveRunStatus(data?.run_status)) {
        return { url: nextPodGrafanaIframeUrl, selectionKey: monitorPodSelectionKey }
      }
      return current
    })
  }, [data?.run_status, monitorPodSelectionKey, nextPodGrafanaIframeUrl])

  const podGrafanaIframeUrl = stablePodGrafanaIframeState.url

  useEffect(() => {
    setStableEngineGrafanaIframeUrl(current => {
      const nextUrl = engineGrafanaDashboard?.url || null
      if (!nextUrl) {
        return null
      }
      if (!current) {
        return nextUrl
      }
      if (!isActiveRunStatus(data?.run_status)) {
        return nextUrl
      }
      return current
    })
  }, [data?.run_status, engineGrafanaDashboard?.url])

  const engineGrafanaIframeUrl = stableEngineGrafanaIframeUrl || engineGrafanaDashboard?.url || null
  const runTerminalActionLocked = isActiveRunStatus(data?.run_status)
  const terminalRunActionTooltip = '压测完成后才能操作'
  const reportActionDisabled = !data || reportBusy || runTerminalActionLocked
  const baselineActionDisabled = !data || setBaselineMutation.isPending || runTerminalActionLocked

  const ensureTerminalRunActionAllowed = (actionLabel: string) => {
    if (!data) {
      return false
    }
    if (isActiveRunStatus(data.run_status)) {
      message.warning(`压测完成后才能${actionLabel}`)
      return false
    }
    return true
  }

  const handleSetBaseline = () => {
    if (!Number.isFinite(runIdNum) || setBaselineMutation.isPending) {
      return
    }
    if (!ensureTerminalRunActionAllowed('设为基线')) {
      return
    }
    setBaselineMutation.mutate({
      runId: runIdNum,
      body: { scope_type: preferredBaselineScopeType },
    })
  }

  const handleScrollToProcess = () => {
    processCardRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  const handleAIPrimaryFocusAction = (focus: RunAIAnalystSummary['primary_focus']) => {
    if (!focus) {
      return
    }
    if (focus.url) {
      window.open(focus.url, '_blank', 'noopener,noreferrer')
      return
    }
    const selector = focus.target_section ? aiPrimaryFocusSectionSelectorMap[focus.target_section] : undefined
    if (!selector) {
      return
    }
    const node = document.querySelector(selector)
    if (node instanceof HTMLElement) {
      node.scrollIntoView({ behavior: 'smooth', block: 'start' })
      return
    }
    message.info('未找到推荐的排查区块')
  }

  const waitForRunReportFrontdoor = async (
    initial: RunReportFrontdoorResolution,
    actionRunId: number,
  ): Promise<RunReportFrontdoorResolution> => {
    if (initial.status === 'ready' && initial.report_id) {
      return initial
    }
    if (!initial.async_task_id || !initial.report_id) {
      throw new Error(initial.message || '报告生成任务未返回 task id')
    }
    for (let attempt = 0; attempt < 120; attempt += 1) {
      const status = await reportApi.getRunReportFrontdoorTaskStatus(
        actionRunId,
        initial.async_task_id,
        initial.report_id,
      )
      if (status.completed) {
        if (status.result?.status === 'ready' && status.result.report_id) {
          return status.result
        }
        throw new Error(status.error || '报告生成任务结束，但未返回可查看报告')
      }
      await new Promise(resolve => setTimeout(resolve, 1500))
    }
    throw new Error('报告生成仍在后台执行，请稍后刷新后重试')
  }

  const handleDownloadReport = async () => {
    if (!data || reportBusy) return
    if (!ensureTerminalRunActionAllowed('下载报告')) return
    const actionRunId = data.run_id
    setDownloadingReport(true)
    const hide = message.loading('正在准备报告...', 0)
    try {
      let targetReportId = latestGeneratedReportId
      let generated = Boolean(targetReportId)
      if (!targetReportId) {
        const frontdoor = await waitForRunReportFrontdoor(
          await reportApi.ensureRunReportFrontdoorAsync(actionRunId),
          actionRunId,
        )
        if (!isCurrentReportAction(actionRunId)) return
        if (frontdoor.status !== 'ready' || !frontdoor.report_id) {
          message.warning(frontdoor.message)
          return
        }
        targetReportId = frontdoor.report_id
        generated = Boolean(frontdoor.generated)
      }

      if (!isCurrentReportAction(actionRunId)) return
      const blob = await reportApi.downloadReport(targetReportId)
      if (!isCurrentReportAction(actionRunId)) return
      const url = window.URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.style.display = 'none'
      link.href = url
      link.download = `report_${targetReportId}.html`
      document.body.appendChild(link)
      link.click()
      window.URL.revokeObjectURL(url)
      document.body.removeChild(link)
      message.success(
        generated
          ? `报告 #${targetReportId} 已重新生成并下载成功`
          : `报告 #${targetReportId} 下载成功`
      )
    } catch (downloadError) {
      message.error(downloadError instanceof Error ? downloadError.message : '下载报告失败')
    } finally {
      hide()
      setDownloadingReport(false)
    }
  }

  const handleViewReport = async () => {
    if (!data || reportBusy) return
    if (!ensureTerminalRunActionAllowed('查看报告')) return
    const actionRunId = data.run_id
    if (latestGeneratedReportId) {
      navigate(`/reports/${latestGeneratedReportId}/view`)
      return
    }
    setViewingReport(true)
    const hide = message.loading('正在准备报告...', 0)
    try {
      const frontdoor = await waitForRunReportFrontdoor(
        await reportApi.ensureRunReportFrontdoorAsync(actionRunId),
        actionRunId,
      )
      if (!isCurrentReportAction(actionRunId)) return
      if (frontdoor.status !== 'ready' || !frontdoor.report_id) {
        message.warning(frontdoor.message)
        return
      }
      setLatestGeneratedReportId(frontdoor.generated ? frontdoor.report_id : null)
      navigate(`/reports/${frontdoor.report_id}/view`)
    } catch (viewError) {
      message.error(viewError instanceof Error ? viewError.message : '打开报告失败')
    } finally {
      hide()
      setViewingReport(false)
    }
  }

  const handleRegenerateReport = async () => {
    if (!data || reportBusy) return
    if (!ensureTerminalRunActionAllowed('重新生成报告')) return
    const actionRunId = data.run_id
    setRegeneratingReport(true)
    const hide = message.loading('正在重新生成报告...', 0)
    try {
      const frontdoor = await waitForRunReportFrontdoor(
        await reportApi.regenerateRunReportFrontdoorAsync(actionRunId),
        actionRunId,
      )
      if (!isCurrentReportAction(actionRunId)) return
      if (frontdoor.status !== 'ready' || !frontdoor.report_id) {
        message.warning(frontdoor.message)
        return
      }
      setLatestGeneratedReportId(frontdoor.report_id)
      message.success(`报告 #${frontdoor.report_id} 已重新生成`)
    } catch (regenerateError) {
      message.error(regenerateError instanceof Error ? regenerateError.message : '重新生成报告失败')
    } finally {
      hide()
      setRegeneratingReport(false)
    }
  }

  // 运行阶段流转展示
  const renderProcessStages = () => {
    const isEmpty = !processData?.stages || processData.stages.length === 0
    if (isEmpty) {
    return (
      <Card title="运行阶段" size="small" className="olh-run-detail-process-card" style={{ height: '100%' }} loading={processLoading} data-testid="run-detail-process">
        <div style={{ color: 'var(--text-muted)', textAlign: 'center', padding: '24px 0' }}>暂无阶段信息</div>
      </Card>
    )
  }

    const statusMap: Record<string, 'wait' | 'process' | 'finish' | 'error'> = {
      pending: 'wait',
      running: 'process',
      completed: 'finish',
      failed: 'error',
    }

    return (
      <Card title="运行阶段" size="small" className="olh-run-detail-process-card" style={{ height: '100%' }} loading={processLoading} data-testid="run-detail-process">
        <Steps
          current={processData.stages.findIndex(s => s.status === 'running')}
          status={processData.stages.some(s => s.status === 'failed') ? 'error' : 'process'}
          items={processData.stages.map(stage => ({
            title: stage.name,
            description: stage.message || (stage.progress != null ? `${stage.progress}%` : undefined),
            status: statusMap[stage.status] || 'wait',
          }))}
        />
        {processData.run_status_detail && (
          <Tag className="olh-run-detail-status-code" color="error">
            {processData.run_status_detail}
          </Tag>
        )}
      </Card>
    )
  }

  const renderK6ControlCard = () => {
    if (data?.engine_type !== 'k6') {
      return null
    }

    const summary = k6ControlData?.summary
    const agents = k6ControlData?.agents ?? []
    const isK6ControlBusy = k6ControlMutation.isPending || Boolean(pendingK6ControlTask)
    const disabled = !Number.isFinite(runIdNum) || isK6ControlBusy || data?.run_status !== 'running'
    const rawParams = (data?.params ?? {}) as Record<string, unknown>
    const baseTargetTps = coerceDisplayFloat(rawParams.target_tps ?? rawParams.base_target_tps ?? rawParams.fixed_tps)
    const resolvedTargetTps = k6ControlInputMode === 'ratio'
      ? (
        typeof k6TargetRatioInput === 'number'
        && k6TargetRatioInput > 0
        && typeof baseTargetTps === 'number'
        && baseTargetTps > 0
          ? roundTo(baseTargetTps * k6TargetRatioInput, 4)
          : null
      )
      : (
        typeof k6TargetTpsInput === 'number' && k6TargetTpsInput > 0
          ? roundTo(k6TargetTpsInput, 4)
          : null
      )
    const resolvedRatio = typeof resolvedTargetTps === 'number'
      && typeof baseTargetTps === 'number'
      && baseTargetTps > 0
      ? roundTo(resolvedTargetTps / baseTargetTps, 4)
      : null
    const currentControlTargetTps = typeof summary?.target_tps === 'number'
      ? summary.target_tps
      : baseTargetTps
    const recommendsSteppedUpshift = summary?.control_strategy === 'scenario_direct'
      && (summary?.agent_total ?? 0) > 1
      && typeof currentControlTargetTps === 'number'
      && typeof resolvedTargetTps === 'number'
      && resolvedTargetTps > currentControlTargetTps + 15
    const strategyLabel = summary?.control_strategy === 'scenario_direct'
      ? '直接调整'
      : summary?.control_strategy === 'auto_tps_fallback'
        ? '自动兜底'
        : '暂不可用'
    const lastSuccessfulConfigSourceLabel = k6LastSuccessfulConfig?.source === 'recent_success_run'
      ? '最近成功运行'
      : '任务最近成功配置'
    const lastSuccessfulConfigDescription = k6LastSuccessfulConfig
      ? `${lastSuccessfulConfigSourceLabel} · 目标 TPS ${fmt(k6LastSuccessfulConfig.targetTps, '', 2)}${k6LastSuccessfulConfig.startedAt ? ` · ${formatDateTime(k6LastSuccessfulConfig.startedAt)}` : ''}`
      : '暂无可复用的最近成功 K6 目标 TPS'
    const latestTrendThroughput = getLatestTrendThroughput(effectiveThroughputItems)
    const monitorThroughput = typeof latestTrendThroughput === 'number'
      ? latestTrendThroughput
      : null
    const busyVus = typeof summary?.active_vus === 'number'
      ? summary.active_vus
      : null
    const scenarioPreAllocatedVus = typeof summary?.scenario_pre_allocated_vus === 'number'
      ? summary.scenario_pre_allocated_vus
      : summary?.current_vus ?? null
    const scenarioMaxVus = typeof summary?.scenario_max_vus === 'number'
      ? summary.scenario_max_vus
      : summary?.current_max_vus ?? null
    const residentVus = typeof summary?.current_vus === 'number'
      ? summary.current_vus
      : scenarioPreAllocatedVus
    const isK6ControlRunning = data?.run_status === 'running'
    const controlUnavailableReason = !isK6ControlRunning
      && String(k6ControlData?.reason || '').trim().startsWith('k6_control_unreachable')
      ? '该运行已结束，agent 的 k6 控制端已关闭。'
      : !isK6ControlRunning && !String(k6ControlData?.reason || '').trim()
        ? '该运行已结束，控制能力仅在运行中可用。'
        : formatK6ControlReason(k6ControlData?.reason)
    const controlIntroMessage = k6ControlData?.available
      ? '当前支持按倍率或总 TPS 调整当前运行中的 K6'
      : !isK6ControlRunning
        ? '当前 Run 已结束，控制能力仅在运行中可用'
        : '当前运行不支持在线控制能力'
    const shouldExplainZeroActiveVus = k6ControlData?.available
      && typeof residentVus === 'number'
      && residentVus > 0
      && typeof busyVus === 'number'
      && busyVus === 0
      && typeof monitorThroughput === 'number'
      && monitorThroughput > 0
    const controlIntroDescription = k6ControlData?.available
      ? `基线总 TPS ${fmt(baseTargetTps, '', 2)} · 监控吞吐 TPS ${fmt(monitorThroughput, '', 2)} · 控制面观测 TPS ${fmt(summary?.observed_tps, '', 2)} · 当前驻留 VUs ${fmt(residentVus, '', 0)} · 瞬时 busy VUs ${fmt(busyVus, '', 0)}${summary?.last_synced_at ? ` · 最近更新时间 ${formatDateTime(summary.last_synced_at)}` : ''}`
      : `${controlUnavailableReason}${typeof baseTargetTps === 'number' && baseTargetTps > 0 ? ` 当前基线总 TPS 为 ${fmt(baseTargetTps, '', 2)}。` : ''}`
    const applyControl = (targetTpsOverride?: number, sourceLabel?: string) => {
      if (!Number.isFinite(runIdNum)) {
        return
      }
      const body: { target_tps?: number } = {}
      if (typeof targetTpsOverride === 'number' && targetTpsOverride > 0) {
        body.target_tps = roundTo(targetTpsOverride, 4)
        setK6ControlInputMode('total_tps')
        setK6TargetTpsInput(body.target_tps)
      } else if (k6ControlInputMode === 'ratio' && !(typeof baseTargetTps === 'number' && baseTargetTps > 0)) {
        message.warning('当前 Run 缺少基线 target_tps，暂不支持倍率换算')
        return
      } else if (summary?.supports_target_tps && typeof resolvedTargetTps === 'number' && resolvedTargetTps > 0) {
        body.target_tps = resolvedTargetTps
      }
      if (Object.keys(body).length === 0) {
        message.warning(k6ControlInputMode === 'ratio' ? '请先填写有效的倍率' : '请先填写有效的总 TPS')
        return
      }
      const submitControl = () => {
        if (typeof currentControlTargetTps === 'number' && currentControlTargetTps > 0) {
          setK6RollbackTargetTps(currentControlTargetTps)
        }
        k6ControlMutation.mutate(
          { runId: runIdNum, body },
          {
            onError: (mutationError: unknown) => {
              message.error(buildK6UpshiftBlockedHint({
                error: mutationError,
                currentTargetTps: typeof currentControlTargetTps === 'number' ? currentControlTargetTps : null,
                requestedTargetTps: typeof body.target_tps === 'number' ? body.target_tps : null,
                currentMaxVus: typeof summary?.current_max_vus === 'number' ? summary.current_max_vus : null,
              }))
            },
          },
        )
      }
      const targetDeltaRatio = (
        typeof currentControlTargetTps === 'number'
        && currentControlTargetTps > 0
        && typeof body.target_tps === 'number'
      )
        ? Math.abs(body.target_tps - currentControlTargetTps) / currentControlTargetTps
        : 0
      if (targetDeltaRatio >= 0.5) {
        Modal.confirm({
          title: '确认大幅调整 K6 TPS',
          content: `${sourceLabel ? `${sourceLabel}：` : ''}当前目标 ${fmt(currentControlTargetTps, ' TPS', 2)}，即将下发 ${fmt(body.target_tps, ' TPS', 2)}。`,
          okText: '确认下发',
          cancelText: '取消',
          onOk: submitControl,
        })
        return
      }
      submitControl()
    }

    return (
      <section data-testid="run-detail-k6-control" style={primaryFlowSectionStyle}>
        <Collapse size="small" ghost>
          <Panel
            header={(
              <Space size={8} wrap>
                <Text strong>K6 控制能力</Text>
                <Tag color={summary?.controllable_agent_total === summary?.agent_total ? 'green' : 'orange'}>
                  {`可控 agent ${summary?.controllable_agent_total ?? 0}/${summary?.agent_total ?? 0}`}
                </Tag>
                <Tag color={summary?.control_strategy === 'scenario_direct' ? 'green' : summary?.control_strategy === 'auto_tps_fallback' ? 'orange' : 'default'}>
                  {strategyLabel}
                </Tag>
                {k6LastSuccessfulConfig ? (
                  <Tag color="blue">{`上次成功 ${fmt(k6LastSuccessfulConfig.targetTps, ' TPS', 2)}`}</Tag>
                ) : null}
              </Space>
            )}
            key="run-detail-k6-control-panel"
          >
            <Card
              size="small"
              loading={k6ControlLoading}
            >
        {k6ControlError ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="K6 控制能力暂不可用"
            description={k6ControlError instanceof Error ? k6ControlError.message : '控制状态查询失败'}
          />
        ) : null}
        {!summary ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前 Run 暂无 K6 控制能力数据" />
        ) : (
          <Space direction="vertical" size={12} style={{ display: 'flex' }}>
            <Alert
              type="info"
              showIcon
              message={controlIntroMessage}
              description={controlIntroDescription}
            />
            {!k6ControlData?.available ? (
              <Alert
                type="warning"
                showIcon
                message={isK6ControlRunning ? '当前 Run 不可控制能力' : '当前 Run 已结束'}
                description={controlUnavailableReason}
              />
            ) : null}
            {k6ControlData?.warnings?.length ? (
              <Alert
                type="info"
                showIcon
                message="部分 agent 返回了附加信息"
                description={formatK6ControlReasonList(k6ControlData.warnings).join(' ; ')}
              />
            ) : null}
            {pendingK6ControlTask ? (
              <Alert
                type="info"
                showIcon
                message="K6 控制能力后台处理中"
                description={`任务 ${pendingK6ControlTask.taskId}${typeof pendingK6ControlTask.targetTps === 'number' ? ` · 目标 TPS ${fmt(pendingK6ControlTask.targetTps, '', 2)}` : ''}。页面会自动刷新控制状态。`}
              />
            ) : null}
            {summary?.preferred_control_path === 'scenario_direct' && summary?.control_strategy === 'auto_tps_fallback' ? (
              <Alert
                type="warning"
                showIcon
                message="当前使用总 TPS 控制"
                description={summary.scenario_patch_reason || '当前运行时暂不支持场景级热更新，系统会按总 TPS 控制本次运行。'}
              />
            ) : null}
            <Alert
              type={k6LastSuccessfulConfig ? 'success' : 'info'}
              showIcon
              message={k6LastSuccessfulConfig ? '可应用上次成功配置' : k6LastSuccessfulConfigLoading ? '正在读取上次成功配置' : '暂无上次成功配置'}
              description={k6LastSuccessfulConfigLoading && !k6LastSuccessfulConfig ? '正在读取同任务最近成功 K6 Run 与任务最近参数。' : lastSuccessfulConfigDescription}
              action={k6LastSuccessfulConfig ? (
                <Button
                  size="small"
                  disabled={disabled || !k6ControlData?.available || !summary.supports_target_tps}
                  loading={isK6ControlBusy}
                  onClick={() => applyControl(k6LastSuccessfulConfig.targetTps, '应用上次成功配置')}
                >
                  应用上次成功配置
                </Button>
              ) : null}
            />
            {recommendsSteppedUpshift ? (
              <Alert
                type="warning"
                showIcon
                message="建议分段上调 TPS"
                description="大步上调可能在切档瞬间造成少量请求丢弃；建议分两到三次逐步调高。"
              />
            ) : null}
            {shouldExplainZeroActiveVus ? (
              <Alert
                type="info"
                showIcon
                message="瞬时 busy VUs 为 0 不代表没有 VU"
                description="当前吞吐正常且当前驻留 VUs 已到位，但瞬时忙碌 VUs 为 0，通常表示短请求在监控抓取瞬间没有 VU 正在忙；请优先结合当前驻留 VUs、场景预分配总 VUs 和场景容量上限总 VUs 一起解读。"
              />
            ) : null}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 12 }}>
              <div><div style={{ color: 'var(--text-secondary)', fontSize: 12 }}>监控吞吐 TPS</div><div style={{ fontSize: 18, fontWeight: 600 }}>{fmt(monitorThroughput, '', 2)}</div></div>
              <div><div style={{ color: 'var(--text-secondary)', fontSize: 12 }}>控制面观测 TPS</div><div style={{ fontSize: 18, fontWeight: 600 }}>{fmt(summary.observed_tps, '', 2)}</div></div>
              <div><div style={{ color: 'var(--text-secondary)', fontSize: 12 }}>当前驻留 VUs</div><div style={{ fontSize: 18, fontWeight: 600 }}>{fmt(residentVus, '', 0)}</div></div>
              <div><div style={{ color: 'var(--text-secondary)', fontSize: 12 }}>瞬时 busy VUs</div><div style={{ fontSize: 18, fontWeight: 600 }}>{fmt(busyVus, '', 0)}</div></div>
              <div><div style={{ color: 'var(--text-secondary)', fontSize: 12 }}>场景预分配总 VUs</div><div style={{ fontSize: 18, fontWeight: 600 }}>{fmt(scenarioPreAllocatedVus, '', 0)}</div></div>
              <div><div style={{ color: 'var(--text-secondary)', fontSize: 12 }}>场景容量上限总 VUs</div><div style={{ fontSize: 18, fontWeight: 600 }}>{fmt(scenarioMaxVus, '', 0)}</div></div>
              <div><div style={{ color: 'var(--text-secondary)', fontSize: 12 }}>控制器状态</div><div style={{ fontSize: 18, fontWeight: 600 }}>{summary.controller_status || '-'}</div></div>
              <div><div style={{ color: 'var(--text-secondary)', fontSize: 12 }}>最近成功目标 TPS</div><div style={{ fontSize: 18, fontWeight: 600 }}>{fmt(k6LastSuccessfulConfig?.targetTps, '', 2)}</div></div>
            </div>
            {summary.controller_message ? (
              <div style={{ color: 'var(--text-secondary)', fontSize: 12 }}>
                {summary.controller_message}
                {summary.last_synced_at ? ` · ${formatDateTime(summary.last_synced_at)}` : ''}
              </div>
            ) : null}
            <Space wrap size={12}>
              <Radio.Group
                value={k6ControlInputMode}
                onChange={event => setK6ControlInputMode(event.target.value as K6ControlInputMode)}
              >
                <Space size={12}>
                  <Radio.Button value="ratio">倍率广播</Radio.Button>
                  <Radio.Button value="total_tps">总 TPS</Radio.Button>
                </Space>
              </Radio.Group>
              <div>
                <div style={{ color: 'var(--text-secondary)', fontSize: 12, marginBottom: 4 }}>{k6ControlInputMode === 'ratio' ? '倍率' : '目标 TPS'}</div>
                {k6ControlInputMode === 'ratio' ? (
                  <InputNumber
                    min={0.01}
                    step={0.1}
                    disabled={disabled || !summary.supports_target_tps || !(typeof baseTargetTps === 'number' && baseTargetTps > 0)}
                    value={k6TargetRatioInput ?? undefined}
                    onChange={value => setK6TargetRatioInput(typeof value === 'number' ? value : null)}
                    style={{ width: 160 }}
                    placeholder={summary.supports_target_tps ? '输入倍率，如 0.5' : '当前不支持'}
                  />
                ) : (
                  <InputNumber
                    min={1}
                    disabled={disabled || !summary.supports_target_tps}
                    value={k6TargetTpsInput ?? undefined}
                    onChange={value => setK6TargetTpsInput(typeof value === 'number' ? value : null)}
                    style={{ width: 160 }}
                    placeholder={summary.supports_target_tps ? '输入总 TPS' : '当前不支持'}
                  />
                )}
              </div>
              <div style={{ alignSelf: 'end' }}>
                <Button type="primary" disabled={disabled || !k6ControlData?.available} loading={isK6ControlBusy} onClick={() => applyControl()}>
                  应用控制
                </Button>
              </div>
              {typeof currentControlTargetTps === 'number' && currentControlTargetTps > 0 ? (
                <div style={{ alignSelf: 'end' }}>
                  <Button
                    onClick={() => {
                      setK6ControlInputMode('total_tps')
                      setK6TargetTpsInput(roundTo(currentControlTargetTps, 4))
                    }}
                  >
                    回填当前目标
                  </Button>
                </div>
              ) : null}
              {typeof k6RollbackTargetTps === 'number' && k6RollbackTargetTps > 0 ? (
                <div style={{ alignSelf: 'end' }}>
                  <Button
                    onClick={() => {
                      setK6ControlInputMode('total_tps')
                      setK6TargetTpsInput(roundTo(k6RollbackTargetTps, 4))
                    }}
                  >
                    回填上次目标
                  </Button>
                </div>
              ) : null}
            </Space>
            <div style={{ color: 'var(--text-secondary)', fontSize: 12 }}>
              {!k6ControlData?.available
                ? isK6ControlRunning
                  ? '当前仅展示控制能力状态；如需调整目标 TPS，请在启动前设置对应参数。'
                  : '当前仅展示结束态控制能力记录；如需调整 TPS，请重新启动新的运行。'
                : resolvedRatio != null
                ? `当前预览倍率 ${roundTo(resolvedRatio, 3)}，预览总 TPS ${fmt(resolvedTargetTps, '', 2)}`
                : '先输入倍率或总 TPS，系统会按本次运行的基线 target_tps 进行换算。'}
            </div>
            <Table<RunK6ControlResponse['agents'][number]>
              size="small"
              rowKey={record => record.agent_host || 'agent'}
              pagination={false}
              dataSource={agents}
              columns={[
                { title: 'Agent', dataIndex: 'agent_host', key: 'agent_host', render: value => value || '-' },
                { title: '状态', dataIndex: 'available', key: 'available', width: 90, render: value => <Tag color={value ? 'green' : 'red'}>{value ? '可控' : '不可控'}</Tag> },
                { title: '观测 TPS', dataIndex: 'observed_tps', key: 'observed_tps', width: 110, render: value => fmt(value, '', 2) },
                { title: '当前驻留 VUs', dataIndex: 'current_vus', key: 'current_vus', width: 120, render: (value, record) => fmt(value ?? record.scenario_pre_allocated_vus, '', 0) },
                { title: '瞬时 busy VUs', dataIndex: 'active_vus', key: 'active_vus', width: 120, render: value => fmt(value, '', 0) },
                {
                  title: '预分配VUs',
                  dataIndex: 'scenario_pre_allocated_vus',
                  key: 'scenario_pre_allocated_vus',
                  width: 110,
                  render: (value, record) => fmt(value ?? record.current_vus, '', 0),
                },
                {
                  title: '容量上限VUs',
                  dataIndex: 'scenario_max_vus',
                  key: 'scenario_max_vus',
                  width: 130,
                  render: (value, record) => fmt(value ?? record.current_max_vus, '', 0),
                },
                { title: '本机场景 TPS', key: 'scenario_target_tps', width: 110, render: (_value, record) => fmt(getK6AgentScenarioTps(record), '', 2) },
                { title: '策略', dataIndex: 'control_strategy', key: 'control_strategy', width: 150, render: value => value || '-' },
                {
                  title: '原因',
                  dataIndex: 'reason',
                  key: 'reason',
                  render: (value, record) => {
                    if (record.available) {
                      return '-'
                    }
                    if (!isK6ControlRunning && String(value || '').trim().startsWith('k6_control_unreachable')) {
                      return '运行已结束，agent 的 k6 控制端已关闭。'
                    }
                    return formatK6ControlReason(value)
                  },
                },
              ]}
            />
          </Space>
        )}
            </Card>
          </Panel>
        </Collapse>
      </section>
    )
  }

  // 基础信息卡
  const renderBaseInfo = () => {
    if (!data) return null

    const agentCount = data.pod_total ?? 0
    const agentActive = data.pod_actual ?? 0
    const agentCompleted = data.pod_completed ?? 0
    const rawParams = (data.params ?? {}) as Record<string, unknown>
    const actualVus = rawParams.vus ?? rawParams.thread_count ?? rawParams.num_threads ?? null
    const targetTps = rawParams.target_tps ?? null
    const iterations = rawParams.iterations ?? rawParams.request_count ?? null
    const loops = rawParams.loops ?? null
    const duration = rawParams.duration ?? rawParams.duration_seconds ?? rawParams.seed_duration_seconds ?? null
    const resolvedThreadCount = coerceDisplayInt(actualVus)
    const resolvedAgentCount = coerceDisplayInt(rawParams.pod_count) ?? agentCount
    const resolvedLoops = coerceDisplayInt(loops)
    const runParamText = [
      formatRunParamValue('VUs', actualVus),
      formatRunParamValue('TPS', targetTps),
      formatRunParamValue('Iterations', iterations),
      formatRunParamValue('Loops', loops),
      formatRunParamValue('Duration', duration, 's'),
    ].filter(Boolean).join(' / ') || '-'
    const protocolText = formatProtocolList(taskDetail?.protocols) || (data.protocol ? getProtocolLabel(data.protocol) : '-')
    const scenarioNote = data.engine_type === 'k6' && targetTps != null
      ? '控制卡中的“当前驻留 VUs”表示当前已驻留/已初始化的 VU 总量；“瞬时忙碌 VUs”才是抓取瞬间正在忙的 VU。场景预分配总 VUs / 场景容量上限总 VUs 仍分别表示场景预分配总量与容量上限。'
      : data.engine_type === 'jmeter'
        ? resolvedLoops && resolvedThreadCount && resolvedAgentCount
          ? `JMeter 按次数当前写入 LoopController.loops=${resolvedLoops}；总请求量通常约等于 ${resolvedLoops} × ${resolvedThreadCount} 线程 × ${resolvedAgentCount} 个执行节点。`
          : 'JMeter 场景通过脚本变量控制并发/时长/TPS/次数；按次数通常对应每线程的 LoopController.loops，结果需同时结合成功率、日志与监控判断。'
        : '-'
    const infoRows = [
      [
        {
          label: '任务名称',
          value: (
            <span>
              {data.task_name || '-'}
              <span style={{ color: 'var(--text-secondary)', fontSize: 12 }}> ({data.task_id})</span>
            </span>
          ),
        },
        {
          label: '操作人',
          value: getCanonicalUserLabel(data.operator_name),
        },
        {
          label: '执行节点数',
          value: (
            <span>
              {agentCount > 0 ? `${agentCompleted}/${agentActive}/${agentCount}` : '-'}
              <span style={{ color: '#9ca3af', fontSize: 11, marginLeft: 8 }}>完成/执行/总数</span>
            </span>
          ),
        },
      ],
      [
        {
          label: '压测时间段',
          value: `${formatDateTime(data.started_at)} - ${formatDateTime(data.ended_at)}`,
        },
        {
          label: '压测结果',
          value: (
            <Space size="small">
              <StatusBadge status={data.run_status} text={data.run_status_label || undefined} />
              {data.run_status_detail ? <span style={{ color: '#ff4d4f', fontSize: 12 }}>{data.run_status_detail}</span> : null}
            </Space>
          ),
        },
        {
          label: '引擎类型',
          value: data.engine_type_label || data.engine_type?.toUpperCase() || '-',
        },
      ],
      [
        {
          label: '运行参数',
          value: runParamText,
        },
        {
          label: '场景说明',
          value: scenarioNote,
        },
        {
          label: '协议',
          value: protocolText,
        },
      ],
    ]

    return (
      <>
        <section
          data-testid="run-detail-base-info"
          className="olh-run-detail-info-section"
          style={primaryFlowSectionStyle}
        >
          <div style={{ marginBottom: 4 }}>{primarySectionTitle('基础信息')}</div>
          <table
            className="olh-run-detail-info-table"
            style={{
              width: '100%',
              borderCollapse: 'collapse',
              tableLayout: 'fixed',
              background: 'var(--card-bg)',
              borderTop: '1px solid var(--border-subtle)',
              borderBottom: '1px solid var(--border-subtle)',
            }}
          >
            <colgroup>
              <col style={{ width: '10%' }} />
              <col style={{ width: '30%' }} />
              <col style={{ width: '9%' }} />
              <col style={{ width: '24%' }} />
              <col style={{ width: '9%' }} />
              <col style={{ width: '18%' }} />
            </colgroup>
            <tbody>
              {infoRows.map((row, rowIndex) => (
                <tr key={`info-row-${rowIndex}`}>
                  {row.map(item => (
                    <Fragment key={item.label}>
                      <td style={primaryFlowLabelCellStyle}>{item.label}</td>
                      <td style={primaryFlowValueCellStyle}>{item.value}</td>
                    </Fragment>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </section>
        {publicAlphaMode ? null : renderAnalysisReadiness(data.analysis_readiness)}
        {renderBaselineSummary(baselineData, baselineLoading)}
        {publicAlphaMode ? null : renderVerdictSummary(verdictData, verdictLoading)}
        {aiFeaturesEnabled ? renderAIAnalystSummary(aiAnalystData, aiAnalystLoading) : null}
        {aiFeaturesEnabled ? renderAIReportSummary(aiReportData, aiReportLoading, aiReportError) : null}
      </>
    )
  }

  const renderAnalysisReadiness = (readiness?: RunAnalysisReadiness | null) => {
    if (!readiness) {
      return null
    }
    return (
      <section data-testid="run-detail-analysis-readiness" style={primaryFlowSectionStyle}>
        <Collapse size="small" ghost>
          <Panel
            header={(
              <Space size={8} wrap>
                <Text strong>分析准备度</Text>
                <Tag color={ANALYSIS_READINESS_STATUS_COLORS[readiness.status] || 'default'}>
                  {readiness.status_label || readiness.status}
                </Tag>
                {readiness.evidence_ready ? <Tag color="green">证据就绪</Tag> : null}
              </Space>
            )}
            key="run-detail-analysis-readiness-panel"
          >
            <Card size="small">
              <Space direction="vertical" size={10} style={{ width: '100%' }}>
                <Space wrap>
                  {ANALYSIS_READINESS_SECTION_ORDER
                    .map(key => readiness.required_sections[key])
                    .filter(Boolean)
                    .map(section => (
                      <Tag key={section.label} color={ANALYSIS_READINESS_STATUS_COLORS[section.status] || 'default'}>
                        {section.label}: {section.status}
                      </Tag>
                    ))}
                </Space>
                {ANALYSIS_READINESS_SECTION_ORDER
                  .map(key => readiness.required_sections[key])
                  .filter(Boolean)
                  .map(section => (
                    <div key={section.label} style={{ display: 'grid', gap: 2 }}>
                      <Text strong>{section.label}</Text>
                      <Text type="secondary">{section.detail || '-'}</Text>
                      {section.gaps.length > 0 ? (
                        <Text type="secondary">缺口：{section.gaps.join(' / ')}</Text>
                      ) : null}
                    </div>
                  ))}
                {readiness.recommended_actions.length > 0 ? (
                  <div style={{ display: 'grid', gap: 4 }}>
                    <Text type="secondary">建议动作：</Text>
                    {readiness.recommended_actions.map(item => (
                      <Text key={item}>{item}</Text>
                    ))}
                  </div>
                ) : null}
                {readiness.limitations.length > 0 ? (
                  <div style={{ display: 'grid', gap: 4 }}>
                    <Text type="secondary">限制说明：</Text>
                    {readiness.limitations.map(item => (
                      <Text key={item}>{item}</Text>
                    ))}
                  </div>
                ) : null}
              </Space>
            </Card>
          </Panel>
        </Collapse>
      </section>
    )
  }

  const renderBaselineSummary = (baseline: RunBaselineSummary | null | undefined, loading: boolean) => {
    if (loading) {
      return (
        <section data-testid="run-detail-baseline" style={primaryFlowSectionStyle}>
          <Collapse size="small" ghost>
            <Panel header={<Text strong>基线</Text>} key="run-detail-baseline-panel">
              <Card size="small" loading />
            </Panel>
          </Collapse>
        </section>
      )
    }

    return (
      <section data-testid="run-detail-baseline" style={primaryFlowSectionStyle}>
        <Collapse size="small" ghost>
          <Panel header={<Text strong>基线</Text>} key="run-detail-baseline-panel">
        <Card size="small">
          {!baseline ? (
            <Space direction="vertical" size={8}>
              <Text type="secondary">当前作用域尚未设置基线</Text>
              <Tooltip title={runTerminalActionLocked ? terminalRunActionTooltip : undefined}>
                <span>
                  <Button
                    size="small"
                    type="primary"
                    disabled={baselineActionDisabled}
                    loading={setBaselineMutation.isPending}
                    onClick={handleSetBaseline}
                  >
                    设为当前基线
                  </Button>
                </span>
              </Tooltip>
            </Space>
          ) : (
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Space wrap>
                <Tag color="blue">{baseline.scope_label}</Tag>
                <Tag color={baseline.current_run_matches_baseline ? 'success' : 'default'}>
                  {baseline.current_run_matches_baseline ? '当前 Run 即基线' : `基线 Run #${baseline.baseline_run_id}`}
                </Tag>
                <Tag>{baseline.baseline_source}</Tag>
              </Space>
              <div style={{ display: 'grid', gap: 6 }}>
                <Text>基线作用域：{baseline.scope_key}</Text>
                <Text>基线生效时间：{formatDateTime(baseline.effective_from)}</Text>
                {baseline.baseline_run ? (
                  <Text>基线运行：#{baseline.baseline_run.run_id} / {baseline.baseline_run.task_name || '-'}</Text>
                ) : null}
                {baseline.note ? <Text>备注：{baseline.note}</Text> : null}
              </div>
              {!baseline.current_run_matches_baseline ? (
                <Tooltip title={runTerminalActionLocked ? terminalRunActionTooltip : undefined}>
                  <span>
                    <Button
                      size="small"
                      type="primary"
                      disabled={baselineActionDisabled}
                      loading={setBaselineMutation.isPending}
                      onClick={handleSetBaseline}
                    >
                      改为当前基线
                    </Button>
                  </span>
                </Tooltip>
              ) : null}
            </Space>
          )}
        </Card>
          </Panel>
        </Collapse>
      </section>
    )
  }

  const renderVerdictSummary = (verdict: RunVerdictSummary | undefined, loading: boolean) => {
    if (loading) {
      return (
        <section data-testid="run-detail-verdict" style={primaryFlowSectionStyle}>
          <Collapse size="small" ghost>
            <Panel header={<Text strong>稳定性结论</Text>} key="run-detail-verdict-panel">
              <Card size="small" loading />
            </Panel>
          </Collapse>
        </section>
      )
    }
    if (!verdict) {
      return null
    }

    const verdictColor = verdict.verdict === 'pass' ? 'success' : verdict.verdict === 'warn' ? 'warning' : 'error'
    const verdictStatusLabel = verdictStatusLabelMap[verdict.verdict] || verdict.verdict.toUpperCase()
    const readableReasons = buildVerdictReasonSummary(verdict.reason_codes)
    const metricHighlights = verdict.metric_deltas
      .map(item => buildVerdictMetricSummary(item, verdict.baseline_run_id))
      .slice(0, 3)
    const verdictSummaryText = buildVerdictSummaryText(
      verdict,
      data?.run_status_label || data?.run_status || null,
    )

    return (
      <section data-testid="run-detail-verdict" style={primaryFlowSectionStyle}>
        <Collapse size="small" ghost>
          <Panel
            header={(
              <Space size={8} wrap>
                <Text strong>稳定性结论</Text>
                <Tag color={verdictColor}>{verdictStatusLabel}</Tag>
              </Space>
            )}
            key="run-detail-verdict-panel"
          >
        <Card size="small">
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Space wrap>
              <Tag color={verdictColor}>{verdictStatusLabel}</Tag>
              {verdict.baseline_scope_label ? <Tag color="blue">{verdict.baseline_scope_label}</Tag> : null}
              {verdict.baseline_run_id ? <Tag>baseline #{verdict.baseline_run_id}</Tag> : null}
            </Space>
            <Text>{verdictSummaryText}</Text>
            {readableReasons.length > 0 ? (
              <div style={{ display: 'grid', gap: 4 }}>
                <Text type="secondary">当前关注点：</Text>
                {readableReasons.map(item => (
                  <Text key={item}>{item}</Text>
                ))}
              </div>
            ) : null}
            {metricHighlights.length > 0 ? (
              <div style={{ display: 'grid', gap: 4 }}>
                <Text type="secondary">关键指标：</Text>
                {metricHighlights.map(item => (
                  <Text key={item}>{item}</Text>
                ))}
              </div>
            ) : null}
          </Space>
        </Card>
          </Panel>
        </Collapse>
      </section>
    )
  }

  const renderAIAnalystSummary = (summary: RunAIAnalystSummary | undefined, loading: boolean) => {
    if (loading) {
      return (
        <section data-testid="run-detail-ai-analyst" style={primaryFlowSectionStyle}>
          <Collapse size="small" ghost>
            <Panel header={<Text strong>规则诊断摘要</Text>} key="run-detail-ai-analyst-panel">
              <Card size="small" loading />
            </Panel>
          </Collapse>
        </section>
      )
    }
    if (!summary) {
      return null
    }

    const confidenceColor = summary.confidence === 'high' ? 'green' : summary.confidence === 'low' ? 'orange' : 'blue'
    const confidenceLabel = confidenceLabelMap[String(summary.confidence).toLowerCase()] || String(summary.confidence).toUpperCase()
    const summaryVerdictLabel = verdictStatusLabelMap[summary.verdict] || summary.verdict.toUpperCase()
    const sourceSummary = summarizeTextList(
      summary.input_sources.map(source => autoSummarySourceLabelMap[source] || source),
      3,
    ) || '稳定性结论'
    const focusSummary = summary.primary_focus
      ? `${formatRuleDiagnosisText(summary.primary_focus.label)}${
          summary.primary_focus.detail ? `：${formatRuleDiagnosisText(summary.primary_focus.detail)}` : ''
        }`
      : '先结合稳定性结论和核心指标继续复核'
    const findingsSummary = summarizeTextList(
      summary.key_findings.map(item => `${formatRuleDiagnosisText(item.label)}：${formatRuleDiagnosisText(item.detail)}`),
      2,
    )
    const actionsSummary = summarizeTextList(summary.recommended_actions.map(formatRuleDiagnosisText), 2)
    const limitationSummary = summarizeTextList(summary.limitations.map(formatRuleDiagnosisText), 2)

    return (
      <section data-testid="run-detail-ai-analyst" style={primaryFlowSectionStyle}>
        <Collapse size="small" ghost>
          <Panel
            header={(
              <Space size={8} wrap>
                <Text strong>规则诊断摘要</Text>
                <Tag color={confidenceColor}>整理置信度 {confidenceLabel}</Tag>
              </Space>
            )}
            key="run-detail-ai-analyst-panel"
          >
        <Card size="small">
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Space wrap>
              <Tag color={summary.verdict === 'pass' ? 'success' : summary.verdict === 'warn' ? 'warning' : 'error'}>
                {summaryVerdictLabel}
              </Tag>
              <Tag color={confidenceColor}>整理置信度 {confidenceLabel}</Tag>
              <Tag>规则整理</Tag>
            </Space>
            <Text>{normalizeAutoSummaryText(summary.analyst_summary, summary.verdict)}</Text>
            <div style={{ display: 'grid', gap: 6 }}>
              <div style={{ display: 'grid', gap: 2 }}>
                <Text type="secondary">数据来源：</Text>
                <Text>{sourceSummary}</Text>
              </div>
              <div style={{ display: 'grid', gap: 2 }}>
                <Text type="secondary">优先排查：</Text>
                <Text>{focusSummary}</Text>
              </div>
              {findingsSummary ? (
                <div style={{ display: 'grid', gap: 2 }}>
                  <Text type="secondary">关键结论：</Text>
                  <Text>{findingsSummary}</Text>
                </div>
              ) : null}
              {actionsSummary ? (
                <div style={{ display: 'grid', gap: 2 }}>
                  <Text type="secondary">建议动作：</Text>
                  <Text>{actionsSummary}</Text>
                </div>
              ) : null}
              {limitationSummary ? (
                <div style={{ display: 'grid', gap: 2 }}>
                  <Text type="secondary">补充说明：</Text>
                  <Text>{limitationSummary}</Text>
                </div>
              ) : null}
            </div>
            {summary.primary_focus ? (
              <div style={{ display: 'grid', gap: 6 }}>
                <Text type="secondary">快速入口：</Text>
                <Space wrap size={[8, 8]}>
                  {summary.primary_focus.url ? (
                    <Button
                      size="small"
                      type="primary"
                      href={summary.primary_focus.url}
                      target="_blank"
                    >
                      {formatRuleDiagnosisText(summary.primary_focus.label)}
                    </Button>
                  ) : (
                    <Button
                      size="small"
                      type="primary"
                      onClick={() => handleAIPrimaryFocusAction(summary.primary_focus)}
                    >
                      {formatRuleDiagnosisText(summary.primary_focus.label)}
                    </Button>
                  )}
                  {summary.primary_focus.metric ? <Tag>{formatRuleDiagnosisText(summary.primary_focus.metric)}</Tag> : null}
                  {summary.primary_focus.dashboard_type ? <Tag>{formatRuleDiagnosisText(summary.primary_focus.dashboard_type)}</Tag> : null}
                </Space>
                {summary.primary_focus.detail ? (
                  <Text type="secondary">{formatRuleDiagnosisText(summary.primary_focus.detail)}</Text>
                ) : null}
              </div>
            ) : null}
          </Space>
        </Card>
          </Panel>
        </Collapse>
      </section>
    )
  }

  const renderStringList = (title: string, items?: string[]) => {
    const normalized = (items ?? []).filter(Boolean)
    if (normalized.length === 0) {
      return null
    }
    return (
      <div style={{ display: 'grid', gap: 4 }}>
        <Text type="secondary">{title}：</Text>
        {normalized.map(item => (
          <Text key={`${title}-${item}`}>{item}</Text>
        ))}
      </div>
    )
  }

  const renderAIReportSummary = (
    report: RunAIReportSummary | undefined,
    loading: boolean,
    loadError: unknown,
  ) => {
    const reportMissing = !loading && !report && loadError
    const failurePresentation = parseAIReportFailureMessage(report?.error_message)
    const aiReportTaskRunning = Boolean(pendingAIReportTask)
    const header = (
      <Space size={8} wrap>
        <Text strong>AI Report</Text>
        {report ? <Tag color={getAIReportStatusColor(report.status)}>{report.status}</Tag> : null}
        {report?.provider ? <Tag>{report.provider}</Tag> : null}
        {report?.model ? <Tag>{report.model}</Tag> : null}
      </Space>
    )
    const handleSubmitAIReportFeedback = () => {
      if (!report?.report_id) {
        return
      }
      const trimmedNote = aiReportFeedbackNote.trim()
      submitAIReportFeedbackMutation.mutate({
        reportId: report.report_id,
        body: {
          rating: aiReportFeedbackRating,
          ...(trimmedNote ? { note: trimmedNote } : {}),
          ...(aiReportFeedbackAction ? { action: aiReportFeedbackAction } : {}),
        },
      })
    }
    return (
      <section data-testid="run-detail-ai-report" style={primaryFlowSectionStyle}>
        <Collapse size="small" ghost>
          <Panel header={header} key="run-detail-ai-report-panel">
            {loading ? (
              <Card size="small" loading />
            ) : (
              <Card size="small">
                <Space direction="vertical" size={10} style={{ width: '100%' }}>
                  <Space wrap>
                    <Button
                      size="small"
                      type="primary"
                      loading={generateAIReportMutation.isPending}
                      disabled={aiReportTaskRunning}
                      onClick={() => generateAIReportMutation.mutate()}
                    >
                      生成 AI Report
                    </Button>
                    {report?.created_at ? <Text type="secondary">{formatDateTime(report.created_at)}</Text> : null}
                    {typeof report?.latency_ms === 'number' ? <Tag>{`${report.latency_ms} ms`}</Tag> : null}
                    {typeof report?.usage?.total_tokens === 'number' ? <Tag>{`${report.usage.total_tokens} tokens`}</Tag> : null}
                  </Space>
                  {reportMissing ? (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无 AI Report" />
                  ) : null}
                  {pendingAIReportTask ? (
                    <Alert
                      type="info"
                      showIcon
                      message="AI Report 后台生成中"
                      description={`任务 ${pendingAIReportTask.taskId}，状态 ${aiReportTaskStatus?.job_status ?? 'pending'}。页面会自动刷新报告结果。`}
                    />
                  ) : null}
                  {report?.error_message ? (
                    <Alert
                      type="warning"
                      showIcon
                      message={failurePresentation.title}
                      description={failurePresentation.description}
                    />
                  ) : null}
                  {report?.summary ? <Text>{report.summary}</Text> : null}
                  {renderStringList('关键发现', report?.key_findings)}
                  {renderStringList('异常', report?.anomalies)}
                  {renderStringList('根因假设', report?.root_cause_hypotheses)}
                  {renderStringList('下一步建议', report?.next_actions)}
                  {(report?.evidence_references ?? []).length > 0 ? (
                    <Space wrap size={[6, 6]}>
                      <Text type="secondary">证据引用：</Text>
                      {(report?.evidence_references ?? []).map(item => <Tag key={item}>{item}</Tag>)}
                    </Space>
                  ) : null}
                  {renderStringList('限制说明', report?.limitations)}
                  <Alert type="info" showIcon message={report?.disclaimer || 'AI 生成，仅供辅助判断；结论必须结合 evidence pack 与人工复核。'} />
                  {report ? (
                    <div
                      data-testid="run-detail-ai-report-feedback"
                      style={{
                        display: 'grid',
                        gap: 8,
                        paddingTop: 10,
                        borderTop: '1px solid var(--border-subtle)',
                      }}
                    >
                      <Space size={8} wrap>
                        <Text strong>人工反馈</Text>
                        {report.feedback_rating ? (
                          <Tag color="blue">{`已保存：${aiReportFeedbackRatingLabel[report.feedback_rating]}`}</Tag>
                        ) : null}
                        {report.feedback_action ? <Tag>{aiReportFeedbackActionLabel[report.feedback_action]}</Tag> : null}
                        {report.feedback_by != null ? <Text type="secondary">{`by ${report.feedback_by}`}</Text> : null}
                        {report.feedback_at ? <Text type="secondary">{formatDateTime(report.feedback_at)}</Text> : null}
                      </Space>
                      {report.feedback_note ? <Text type="secondary">{`反馈备注：${report.feedback_note}`}</Text> : null}
                      <Space size={8} wrap align="start">
                        <Radio.Group
                          data-testid="ai-report-feedback-rating"
                          size="small"
                          options={aiReportFeedbackRatingOptions}
                          optionType="button"
                          value={aiReportFeedbackRating}
                          onChange={event => setAIReportFeedbackRating(event.target.value)}
                        />
                        <Select
                          data-testid="ai-report-feedback-action"
                          size="small"
                          allowClear
                          placeholder="action"
                          style={{ width: 140 }}
                          options={aiReportFeedbackActionOptions}
                          value={aiReportFeedbackAction}
                          onChange={value => setAIReportFeedbackAction(value)}
                        />
                        <Button
                          size="small"
                          type="primary"
                          loading={submitAIReportFeedbackMutation.isPending}
                          onClick={handleSubmitAIReportFeedback}
                        >
                          保存反馈
                        </Button>
                      </Space>
                      <Input.TextArea
                        data-testid="ai-report-feedback-note"
                        rows={2}
                        placeholder="note"
                        value={aiReportFeedbackNote}
                        onChange={event => setAIReportFeedbackNote(event.target.value)}
                      />
                    </div>
                  ) : null}
                </Space>
              </Card>
            )}
          </Panel>
        </Collapse>
      </section>
    )
  }

  // 接口级核心指标表
  const renderSummaryMetrics = () => {
    const metricsSeriesByName = new Map((metricsData?.series ?? []).map(item => [item.metric, item]))
    const fallbackOverallRow = {
      endpoint_name: 'overall',
      avg_rt_ms:
        summaryMetricsData?.items?.find(item => item.endpoint_name === 'overall')?.avg_rt_ms
        ?? data?.overview_summary?.avg_rt_ms
        ?? data?.avg_rt_ms
        ?? getLatestSeriesValue(metricsSeriesByName.get('rt_avg_ms')),
      p95_rt_ms:
        summaryMetricsData?.items?.find(item => item.endpoint_name === 'overall')?.p95_rt_ms
        ?? data?.overview_summary?.p95_rt_ms
        ?? data?.p95_rt_ms
        ?? getLatestSeriesValue(metricsSeriesByName.get('rt_p95_ms')),
      p99_rt_ms:
        summaryMetricsData?.items?.find(item => item.endpoint_name === 'overall')?.p99_rt_ms
        ?? data?.p99_rt_ms
        ?? getLatestSeriesValue(metricsSeriesByName.get('rt_p99_ms')),
      max_rt_ms: summaryMetricsData?.items?.find(item => item.endpoint_name === 'overall')?.max_rt_ms ?? null,
      min_rt_ms: summaryMetricsData?.items?.find(item => item.endpoint_name === 'overall')?.min_rt_ms ?? null,
      total_requests:
        summaryMetricsData?.items?.find(item => item.endpoint_name === 'overall')?.total_requests
        ?? data?.overview_summary?.total_requests
        ?? data?.total_requests
        ?? null,
      throughput:
        summaryMetricsData?.items?.find(item => item.endpoint_name === 'overall')?.throughput
        ?? data?.overview_summary?.throughput
        ?? data?.rps
        ?? getLatestSeriesValue(metricsSeriesByName.get('rps')),
    }
    const normalizedItems = (() => {
      const liveItems = summaryMetricsData?.items ?? []
      const items = liveItems.some(item => item.endpoint_name && item.endpoint_name !== 'overall')
        ? liveItems
        : seedSummaryMetricItems
      if (items.length === 0) {
        return [
          fallbackOverallRow.avg_rt_ms,
          fallbackOverallRow.p95_rt_ms,
          fallbackOverallRow.p99_rt_ms,
          fallbackOverallRow.max_rt_ms,
          fallbackOverallRow.min_rt_ms,
          fallbackOverallRow.total_requests,
          fallbackOverallRow.throughput,
        ].some(value => isValidMetricValue(value))
          ? [fallbackOverallRow]
          : []
      }

      return items.map(item => {
        if (item.endpoint_name !== 'overall') {
          return item
        }

        return {
          ...item,
          avg_rt_ms: item.avg_rt_ms ?? fallbackOverallRow.avg_rt_ms,
          p95_rt_ms: item.p95_rt_ms ?? fallbackOverallRow.p95_rt_ms,
          p99_rt_ms: item.p99_rt_ms ?? fallbackOverallRow.p99_rt_ms,
          total_requests: item.total_requests ?? fallbackOverallRow.total_requests,
          throughput: item.throughput ?? fallbackOverallRow.throughput,
        }
      })
    })()
    const hasData = normalizedItems.length > 0
    const columns = [
      { title: '接口名称', key: 'endpoint_name' },
      { title: '平均响应时间 (ms)', key: 'avg_rt_ms' },
      { title: 'P95响应时间 (ms)', key: 'p95_rt_ms' },
      { title: 'P99响应时间 (ms)', key: 'p99_rt_ms' },
      { title: '最大响应时间 (ms)', key: 'max_rt_ms' },
      { title: '最小响应时间 (ms)', key: 'min_rt_ms' },
      { title: '请求总量 (count)', key: 'total_requests' },
      { title: '吞吐量 (rep/s)', key: 'throughput' },
    ] as const

    return (
      <section
        data-testid="run-detail-summary-metrics"
        className="olh-run-detail-info-section"
        style={{
          ...primaryFlowSectionStyle,
          ...primaryFlowSectionDividerStyle,
        }}
      >
        <div style={{ marginBottom: 4, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
          {primarySectionTitle('压测核心指标')}
          {summaryLoading ? <span style={{ color: '#9ca3af', fontSize: 11 }}>加载中...</span> : null}
        </div>
        {hasData ? (
          <div style={{ overflowX: 'auto' }}>
            <table
              className="olh-run-detail-metric-table"
              style={{
                width: '100%',
                minWidth: 1080,
                borderCollapse: 'collapse',
                tableLayout: 'fixed',
                background: 'var(--card-bg)',
                borderTop: '1px solid var(--border-subtle)',
                borderBottom: '1px solid var(--border-subtle)',
              }}
            >
              <thead>
                <tr style={{ background: 'var(--table-header-bg)' }}>
                  {columns.map((column, index) => (
                    <th
                      key={column.key}
                      style={{
                        padding: '7px 10px',
                        borderBottom: '1px solid var(--border-subtle)',
                        borderRight: index === columns.length - 1 ? 'none' : '1px solid var(--border-subtle)',
                        color: 'var(--text-secondary)',
                        fontSize: 11,
                        fontWeight: 600,
                        textAlign: index === 0 ? 'left' : 'center',
                      }}
                    >
                      {column.title}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {normalizedItems.map(item => (
                  <tr key={item.endpoint_name}>
                    <td style={{ padding: '7px 10px', borderBottom: '1px solid var(--border-subtle)', color: 'var(--text-primary)', fontSize: 11 }}>
                      {item.endpoint_name}
                    </td>
                    {[
                      item.avg_rt_ms,
                      item.p95_rt_ms,
                      item.p99_rt_ms,
                      item.max_rt_ms,
                      item.min_rt_ms,
                      item.total_requests,
                      item.throughput,
                    ].map((value, index) => (
                      <td
                        key={`${item.endpoint_name}-${index}`}
                        style={{
                          padding: '7px 10px',
                          borderBottom: '1px solid var(--border-subtle)',
                          color: 'var(--text-primary)',
                          fontSize: 11,
                          textAlign: 'center',
                        }}
                      >
                        {value ?? '-'}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            description={metricsData?.series?.length ? '接口级指标缺失，已回退到聚合指标' : '接口级指标数据待接入'}
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          />
        )}
      </section>
    )
  }

  // Group-Checks 表
  const renderGroupChecks = () => {
    const displayItems = checksData?.items?.length ? checksData.items : stickyChecksItems
    const hasData = displayItems.length > 0
    if (!hasData && !checksLoading && !isActiveRunStatus(data?.run_status)) {
      return null
    }

    return (
      <section
        data-testid="run-detail-group-checks"
        className="olh-run-detail-info-section"
        style={{
          ...primaryFlowSectionStyle,
          ...primaryFlowSectionDividerStyle,
          paddingBottom: 10,
        }}
      >
        <div style={{ marginBottom: 4, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
          {primarySectionTitle('Group-Checks')}
          {checksLoading ? <span style={{ color: '#9ca3af', fontSize: 11 }}>加载中...</span> : null}
        </div>
        {hasData ? (
          <div style={{ overflowX: 'auto' }}>
            <table
              className="olh-run-detail-metric-table"
              style={{
                width: '100%',
                minWidth: 720,
                borderCollapse: 'collapse',
                tableLayout: 'fixed',
                background: 'var(--card-bg)',
                borderTop: '1px solid var(--border-subtle)',
                borderBottom: '1px solid var(--border-subtle)',
              }}
            >
              <thead>
                <tr style={{ background: 'var(--table-header-bg)' }}>
                  {['groups', 'checks', '成功率'].map((title, index) => (
                    <th
                      key={title}
                      style={{
                        padding: '7px 10px',
                        borderBottom: '1px solid var(--border-subtle)',
                        borderRight: index === 2 ? 'none' : '1px solid var(--border-subtle)',
                        color: 'var(--text-secondary)',
                        fontSize: 11,
                        fontWeight: 600,
                        textAlign: index === 2 ? 'center' : 'left',
                      }}
                    >
                      {title}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {displayItems.map(record => (
                  <tr key={`${record.group_name}-${record.check_name}`}>
                    <td style={{ padding: '7px 10px', borderBottom: '1px solid var(--border-subtle)', color: 'var(--text-primary)', fontSize: 11 }}>
                      {record.group_name}
                    </td>
                    <td style={{ padding: '7px 10px', borderBottom: '1px solid var(--border-subtle)', color: 'var(--text-primary)', fontSize: 11 }}>
                      {record.check_name}
                    </td>
                    <td style={{ padding: '7px 10px', borderBottom: '1px solid var(--border-subtle)', color: 'var(--text-primary)', fontSize: 11, textAlign: 'center' }}>
                      {`${(record.success_rate * 100).toFixed(2)}%`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            description="当前暂无 checks 留证"
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          />
        )}
      </section>
    )
  }

  // 发压端二级 Tab 内容
  const renderPressureTab = () => {
    const subTabs = [
      { key: 'stats', label: '性能指标' },
      { key: 'logs', label: '日志详情' },
      { key: 'monitor', label: '执行节点监控' },
      { key: 'grafana', label: `性能指标 ${data?.engine_type?.toUpperCase()}-Grafana` },
    ]

    const latencyMetricSwitcher = (
      <Space size="small">
        {[
          { label: 'avg', value: 'rt_avg_ms' },
          { label: 'p95', value: 'rt_p95_ms' },
          { label: 'p99', value: 'rt_p99_ms' },
        ].map(option => (
          <Button
            key={option.value}
            size="small"
            type={latencyMetric === option.value ? 'primary' : 'default'}
            onClick={() => setLatencyMetric(option.value as EndpointTrendMetric)}
          >
            {option.label}
          </Button>
        ))}
      </Space>
    )

    const renderTrendWorkbench = ({
      title,
      subtitle,
      hasData,
      loading,
      option,
      testId,
      emptyDescription,
      extra,
      height,
      legend,
      embedded = false,
    }: {
      title: string
      subtitle: string
      hasData: boolean
      loading: boolean
      option: EChartsOption
      testId: string
      emptyDescription: string
      extra?: ReactNode
      height: number
      legend?: ReactNode
      embedded?: boolean
    }) => (
      <section
        data-testid={testId}
        className={`olh-run-detail-chart-panel${embedded ? ' olh-run-detail-chart-panel--embedded' : ''}`}
        style={{
          border: embedded ? '1px solid var(--border-subtle)' : '1px solid var(--border-color)',
          borderRadius: 8,
          background: 'linear-gradient(180deg, var(--card-bg) 0%, color-mix(in srgb, var(--card-bg) 88%, var(--surface-subtle) 12%) 100%)',
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            gap: 12,
            flexWrap: 'wrap',
            alignItems: embedded ? 'flex-start' : 'center',
            padding: '10px 12px 6px',
            borderBottom: '1px solid var(--border-subtle)',
            background: 'var(--surface-subtle)',
          }}
        >
          <div style={{ display: 'grid', gap: embedded ? 2 : 4 }}>
            <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.3, color: 'var(--text-primary)' }}>{title}</div>
            <div style={{ color: 'var(--text-muted)', fontSize: 11 }}>{subtitle}</div>
          </div>
          {extra}
        </div>
        {hasData ? (
          <div style={{ padding: embedded ? '8px 10px 10px' : '8px 10px 10px' }}>
            <ReactECharts option={option} style={{ height: `${height}px` }} opts={{ renderer: 'canvas' }} showLoading={loading} />
            {legend ? <div style={{ marginTop: embedded ? 6 : 8 }}>{legend}</div> : null}
          </div>
        ) : (
          <div style={{ padding: embedded ? '30px 12px 24px' : '36px 20px' }}>
            <Empty description={emptyDescription} image={Empty.PRESENTED_IMAGE_SIMPLE} />
          </div>
        )}
      </section>
    )

    return (
      <div>
        <Tabs
          activeKey={activeSubTab}
          onChange={setActiveSubTab}
          items={subTabs}
          style={{ marginBottom: activeSubTab === 'grafana' ? 8 : 16 }}
        />
        {activeSubTab === 'stats' && (
          <div
            data-testid="run-detail-stats-surface"
            style={{
              ...pressureWorkbenchSurfaceStyle,
            }}
          >
            <div
              data-testid="run-detail-stats-header"
              style={{
                padding: '8px 10px',
                borderBottom: '1px solid var(--border-subtle)',
                background: 'var(--surface-subtle)',
                display: 'flex',
                justifyContent: 'space-between',
                gap: 10,
                flexWrap: 'wrap',
                alignItems: 'center',
              }}
            >
              <div>
                <div style={{ fontSize: 12, fontWeight: 600, lineHeight: 1.2, color: 'var(--text-primary)' }}>OpenLoadHub 性能指标</div>
                <div style={{ marginTop: 2, color: 'var(--text-muted)', fontSize: 11 }}>
                  吞吐趋势优先展示，右侧补充延迟趋势与图例说明
                </div>
              </div>
              <div style={{ color: 'var(--text-muted)', fontSize: 11 }}>
                轻量双图布局
              </div>
            </div>
            <div
              data-testid="run-detail-stats-canvas"
              style={{
                ...openLoadHubMetricGridStyle,
              }}
            >
              <div
                style={{
                  ...pressureWorkbenchSectionStyle,
                }}
              >
                {renderTrendWorkbench({
                  title: '主要吞吐趋势 (req/s)',
                  subtitle: '优先观察吞吐变化',
                  hasData: hasThroughputTrend,
                  loading: endpointTrendLoading,
                  option: throughputChartOption,
                  testId: 'run-metric-throughput',
                  emptyDescription: '暂无接口级吞吐量趋势数据',
                  height: 320,
                  legend: renderEndpointLegend(throughputEndpoints, 'run-metric-throughput-legend'),
                  embedded: true,
                })}
              </div>
              <div
                style={{
                  ...pressureWorkbenchSectionStyle,
                  display: 'grid',
                  gap: 12,
                  alignContent: 'start',
                }}
              >
                {renderTrendWorkbench({
                  title: `延迟趋势 (ms) - ${latencyMetric === 'rt_avg_ms' ? 'avg' : latencyMetric === 'rt_p95_ms' ? 'p95' : 'p99'}`,
                  subtitle: '切换 avg / p95 / p99',
                  hasData: hasLatencyTrend,
                  loading: endpointTrendLoading,
                  option: latencyChartOption,
                  testId: 'run-metric-latency',
                  emptyDescription: '暂无接口级响应时间趋势数据',
                  extra: latencyMetricSwitcher,
                  height: 280,
                  legend: renderEndpointLegend(latencyEndpoints, 'run-metric-latency-legend'),
                  embedded: true,
                })}
                <div
                  style={{
                    border: '1px solid var(--border-color)',
                    borderRadius: 4,
                    background: 'var(--surface-subtle)',
                    padding: '10px 12px',
                  }}
                >
                  <div style={{ fontSize: 12, fontWeight: 600, lineHeight: 1.3, color: 'var(--text-primary)' }}>
                    关键说明
                  </div>
                  <div style={{ marginTop: 6, color: 'var(--text-secondary)', fontSize: 11, lineHeight: 1.6 }}>
                    先看吞吐是否贴近执行目标，再结合 avg / p95 / p99 判断延迟分布；图例按接口拆分。
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}
        {activeSubTab === 'logs' && (
          <div
            data-testid="run-detail-log-workspace"
            style={{
              ...pressureWorkbenchSurfaceStyle,
              minHeight: 560,
            }}
          >
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                gap: 10,
                flexWrap: 'wrap',
                alignItems: 'center',
                padding: '6px 10px',
                borderBottom: '1px solid var(--border-subtle)',
                background: 'var(--surface-subtle)',
              }}
            >
              <div style={{ display: 'grid', gap: 2 }}>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
                  <span style={{ fontWeight: 600, color: 'var(--text-primary)', fontSize: 12 }}>日志详情</span>
                  <span style={{ color: 'var(--primary-color)', fontSize: 12, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
                    {logWorkspaceTargetLabel}
                  </span>
                </div>
                <div style={{ color: 'var(--text-muted)', fontSize: 11, lineHeight: 1.2 }}>
                  终端主视图 · 默认仅展示工具原生日志 · 按 IP / 节点切换
                </div>
              </div>
              <Space size="small" wrap>
                {hasMultipleNodes ? (
                  <div data-testid="run-detail-log-source-switch">
                    <Space size={6} wrap>
                      {nodeSwitchOptions.map(option => (
                        <Button
                          key={option.value}
                          size="small"
                          type={selectedNodeKey === option.value ? 'primary' : 'default'}
                          data-testid={`run-detail-log-source-option-${option.value}`}
                          onClick={() => setSelectedNodeKey(option.value)}
                        >
                          {option.label}
                        </Button>
                      ))}
                    </Space>
                  </div>
                ) : null}
                <Button size="small" type="text" onClick={() => setLogOrderAsc(current => !current)}>
                  切换为{logOrderAsc ? '倒序' : '正序'}
                </Button>
                <Space size={6}>
                  <Button
                    size="small"
                    type={logView === 'all' ? 'primary' : 'default'}
                    onClick={() => setLogView('all')}
                  >
                    所有日志
                  </Button>
                  <Button
                    size="small"
                    type={logView === 'exception' ? 'primary' : 'default'}
                    onClick={() => setLogView('exception')}
                  >
                    异常日志
                  </Button>
                </Space>
                <Button size="small" type="text" onClick={() => loadLogs(true)} disabled={loadingLogs}>
                  刷新
                </Button>
                {nextCursor ? (
                  <Button size="small" type="text" onClick={() => loadLogs(false)} disabled={loadingLogs}>
                    加载更多
                  </Button>
                ) : null}
              </Space>
            </div>
            <div
              style={{
                padding: '10px 12px 12px',
                background: 'var(--card-bg)',
              }}
            >
              {failedCheckItems.length > 0 || exceptionSummaryLogs.length > 0 ? (
                <div
                  data-testid="run-detail-log-error-summary"
                  style={{
                    marginBottom: 12,
                    border: '1px solid color-mix(in srgb, var(--error-color) 42%, var(--border-color))',
                    borderRadius: 4,
                    background: 'var(--error-soft)',
                    overflow: 'hidden',
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      gap: 12,
                      flexWrap: 'wrap',
                      alignItems: 'center',
                      padding: '8px 10px',
                      borderBottom: '1px solid color-mix(in srgb, var(--error-color) 28%, var(--border-color))',
                      background: 'color-mix(in srgb, var(--error-color) 16%, var(--card-bg))',
                    }}
                  >
                    <div>
                      <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--error-color)', lineHeight: 1.3 }}>错误摘要</div>
                      <div style={{ marginTop: 2, fontSize: 11, color: 'var(--warning-color)', lineHeight: 1.3 }}>
                        先看失败接口/检查项，再下钻原始终端日志
                      </div>
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--error-color)' }}>
                      {selectedNodeLabel}
                    </div>
                  </div>
                  <div style={{ padding: 10, display: 'grid', gap: 10 }}>
                    {failedCheckItems.length > 0 ? (
                      <div>
                        <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-primary)', marginBottom: 6 }}>失败检查项</div>
                        <div style={{ display: 'grid', gap: 6 }}>
                          {failedCheckItems.map(item => (
                            <div
                              key={`${item.group_name}-${item.check_name}`}
                              style={{
                                display: 'grid',
                                gridTemplateColumns: 'minmax(0, 1fr) auto',
                                gap: 8,
                                padding: '8px 10px',
                                borderRadius: 4,
                                background: 'var(--card-bg)',
                                border: '1px solid color-mix(in srgb, var(--error-color) 24%, var(--border-color))',
                              }}
                            >
                              <div>
                                <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-primary)' }}>{item.group_name}</div>
                                <div style={{ marginTop: 2, fontSize: 12, color: 'var(--text-secondary)' }}>{item.check_name}</div>
                              </div>
                              <div style={{ fontSize: 12, fontWeight: 600, color: '#b91c1c', alignSelf: 'center' }}>
                                {(item.success_rate * 100).toFixed(2)}%
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    ) : null}
                    {exceptionSummaryLogs.length > 0 ? (
                      <div>
                        <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-primary)', marginBottom: 6 }}>异常日志摘录</div>
                        <div style={{ display: 'grid', gap: 6 }}>
                          {exceptionSummaryLogs.map(item => (
                            <div
                              key={`${item.seq}-${item.ts}`}
                              style={terminalLogSnippetStyle}
                            >
                              <div style={{ fontSize: 11, color: 'var(--terminal-accent)', marginBottom: 4 }}>
                                [{String(item.level || '').toUpperCase()}] {formatDateTime(item.ts)} {item.source ? `· ${item.source}` : ''}
                              </div>
                              <div style={{ fontSize: 12, lineHeight: 1.6, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                                {item.message}
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    ) : null}
                  </div>
                </div>
              ) : null}
              <div
                data-testid="run-detail-log-terminal"
                style={{
                  overflow: 'hidden',
                  minHeight: 520,
                  border: '1px solid var(--terminal-border)',
                  borderRadius: 2,
                  background: 'var(--terminal-bg)',
                }}
              >
                {terminalDisplayLogs.length > 0 ? (
                  <div
                    style={{
                      minHeight: 520,
                      background: 'linear-gradient(180deg, var(--terminal-bg-raised) 0%, var(--terminal-bg) 100%)',
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 8,
                        padding: '6px 12px',
                        borderBottom: '1px solid var(--terminal-border-subtle)',
                        background: 'var(--terminal-header-bg)',
                      }}
                    >
                      <span style={{ width: 10, height: 10, borderRadius: 999, background: '#fb7185' }} />
                      <span style={{ width: 10, height: 10, borderRadius: 999, background: '#fbbf24' }} />
                      <span style={{ width: 10, height: 10, borderRadius: 999, background: '#4ade80' }} />
                      <span style={{ marginLeft: 4, color: 'var(--terminal-muted)', fontSize: 11 }}>
                        {toolTerminalLogs.length > 0 ? '工具日志' : '平台事件'}
                      </span>
                      {selectedNodeTarget ? (
                        <span style={{ marginLeft: 'auto', color: 'var(--terminal-accent)', fontSize: 11 }}>
                          当前节点: {selectedNodeLabel}
                        </span>
                      ) : null}
                    </div>
                    <pre
                      style={{
                        margin: 0,
                        padding: '14px 16px 18px',
                        minHeight: 472,
                        fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace',
                        fontSize: 12,
                        color: 'var(--terminal-text)',
                        whiteSpace: 'pre-wrap',
                        wordBreak: 'break-word',
                        lineHeight: 1.72,
                      }}
                    >
                      {logTerminalContent}
                    </pre>
                  </div>
                ) : (
                  <div
                    style={{
                      minHeight: 520,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      padding: 24,
                    }}
                  >
                    <Empty
                      description={
                        <span style={{ color: 'var(--terminal-muted)' }}>
                          {platformEventLogs.length > 0 ? '暂无工具日志；平台事件请看下方独立区域' : '暂无工具日志'}
                        </span>
                      }
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                    />
                  </div>
                )}
              </div>
              {platformEventLogs.length > 0 ? (
                <div
                  style={{
                    marginTop: 12,
                    border: '1px solid var(--border-color)',
                    borderRadius: 4,
                    overflow: 'hidden',
                    background: 'var(--surface-subtle)',
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      gap: 12,
                      flexWrap: 'wrap',
                      alignItems: 'center',
                      padding: '8px 10px',
                      borderBottom: '1px solid var(--border-subtle)',
                      background: 'var(--table-header-bg)',
                    }}
                  >
                    <div>
                      <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-primary)', lineHeight: 1.3 }}>平台事件</div>
                      <div style={{ marginTop: 2, fontSize: 11, color: 'var(--text-secondary)', lineHeight: 1.3 }}>
                        平台运行事件与工具日志分开展示，不再混在同一视图
                      </div>
                    </div>
                    <Tag color="default">{platformEventLogs.length} 条</Tag>
                  </div>
                  <div style={{ display: 'grid', gap: 8, padding: 10 }}>
                    {platformEventLogs.map(item => (
                      <div
                        key={`platform-${item.seq}-${item.ts}`}
                        style={{
                          padding: '8px 10px',
                          borderRadius: 4,
                          border: '1px solid var(--border-subtle)',
                          background: 'var(--card-bg)',
                        }}
                      >
                        <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 4 }}>
                          [{String(item.level || '').toUpperCase()}] {formatDateTime(item.ts)} {item.source ? `· ${item.source}` : ''}
                        </div>
                        <div style={{ fontSize: 12, color: 'var(--text-primary)', lineHeight: 1.6, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                          {item.message}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>
          </div>
        )}
        {activeSubTab === 'monitor' && (
          <div
            data-testid="run-detail-agent-monitor"
            style={monitorWorkspacePageStyle}
          >
            {(!podsData?.items || podsData.items.length === 0) && !podsMonitorLoading ? (
              <div style={monitorWorkspaceShellStyle}>
                <div style={{ padding: 24 }}>
                  <Empty
                    description="暂无执行节点数据"
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                  />
                </div>
              </div>
            ) : (
              <div style={{ display: 'grid', gap: 12 }}>
                <div
                  data-testid="run-detail-agent-monitor-hero"
                  style={{
                    ...monitorWorkspaceShellStyle,
                  }}
                >
                  <div
                    style={{
                      ...monitorWorkspaceToolbarStyle,
                      display: 'flex',
                      justifyContent: 'space-between',
                      gap: 8,
                      flexWrap: 'wrap',
                      alignItems: 'center',
                      padding: '6px 8px',
                      background: 'var(--surface-subtle)',
                    }}
                  >
                    <div>
                      <div style={{ fontSize: 12, fontWeight: 600, lineHeight: 1.3, color: 'var(--text-primary)' }}>
                        {podGrafanaDashboard?.title || 'Pod Grafana'}
                      </div>
                      <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 2 }}>
                        多执行节点同看板展示 · 默认按 pod_ip，多 agent 同 IP 时按节点补充分流
                      </div>
                    </div>
                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
                      {hasMonitorPodSwitch ? (
                        <div data-testid="run-detail-monitor-pod-switch">
                          <Space size={6} wrap>
                            {monitorPodOptions.map(option => (
                              <Button
                                key={option.key}
                                size="small"
                                type={selectedMonitorPodKey === option.key ? 'primary' : 'default'}
                                data-testid={`run-detail-monitor-pod-option-${option.key}`}
                                onClick={() => setSelectedMonitorPodKey(option.key)}
                              >
                                {option.label}
                              </Button>
                            ))}
                          </Space>
                        </div>
                      ) : null}
                      {podGrafanaIframeUrl ? (
                        <Button type="link" href={podGrafanaIframeUrl} target="_blank" style={{ paddingInline: 0 }}>
                          新窗口打开
                        </Button>
                      ) : null}
                    </div>
                  </div>
                  <div
                    data-testid="run-detail-agent-monitor-info-wall"
                    style={{
                      display: 'grid',
                      gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
                      gap: 4,
                      padding: '4px 8px',
                      borderBottom: '1px solid var(--border-subtle)',
                      background: 'var(--card-bg)',
                    }}
                  >
                    {(stableMonitorInfoWallItems.length > 0 ? stableMonitorInfoWallItems : monitorInfoWallItems).map(item => (
                      <div key={item.label} style={{ padding: '4px 6px', borderRadius: 2, background: 'var(--surface-subtle)' }}>
                        <div style={{ fontSize: 11, color: 'var(--text-secondary)', lineHeight: 1.25 }}>{item.label}</div>
                        <div style={{ marginTop: 5, fontSize: 12, fontWeight: 600, lineHeight: 1.35, color: 'var(--text-primary)' }}>{item.value}</div>
                        {item.helper ? <div style={{ marginTop: 2, fontSize: 11, color: 'var(--text-muted)', lineHeight: 1.25 }}>{item.helper}</div> : null}
                      </div>
                    ))}
                  </div>
                  <div
                    data-testid="run-detail-monitor-health"
                    style={{
                      padding: '4px 8px',
                      borderBottom: '1px solid var(--border-subtle)',
                      background: 'var(--card-bg)',
                      fontSize: 11,
                      color: 'var(--text-secondary)',
                    }}
                  >
                    {monitorHealthItems.map(item => `${item.label}: ${item.value}`).join(' / ')}
                  </div>
                  <div style={monitorWorkspaceBodyStyle}>
                    <div
                      data-testid="run-detail-pod-grafana"
                      style={{ border: '1px solid var(--border-subtle)', borderRadius: 2, overflow: 'hidden', background: 'var(--card-bg)' }}
                    >
                      {!podGrafanaDashboard ? (
                        <div style={{ padding: 24 }}>
                          <Empty description="暂无 Pod Grafana 看板" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                        </div>
                      ) : podGrafanaDashboard.embed_mode === 'new_tab' || !podGrafanaIframeUrl ? (
                        <div style={{ padding: 24, textAlign: 'center' }}>
                          <Empty description="该看板仅支持新窗口打开" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                          <Button type="primary" href={podGrafanaIframeUrl || undefined} target="_blank" style={{ marginTop: 16 }}>
                            点击打开 Grafana
                          </Button>
                        </div>
                      ) : (
                        <div style={{ position: 'relative' }}>
                          <iframe
                            src={podGrafanaIframeUrl}
                            style={{ width: '100%', height: '860px', border: 'none', display: 'block', background: 'var(--card-bg)' }}
                            title={podGrafanaDashboard.title}
                          />
                        </div>
                      )}
                    </div>
                  </div>
                </div>

              </div>
            )}
          </div>
        )}
        {activeSubTab === 'grafana' && (
          <div
            data-testid="run-detail-grafana"
            style={{
              display: 'grid',
              gap: 6,
              padding: 6,
              background: 'var(--card-bg)',
              border: '1px solid var(--border-subtle)',
              borderRadius: 4,
            }}
          >
            {(() => {
              const engineGrafana = engineGrafanaDashboard
              return (
                <>
                  <div
                    data-testid="run-detail-grafana-page-header"
                    style={{
                      border: '1px solid var(--border-color)',
                      borderRadius: 4,
                      background: 'var(--card-bg)',
                    }}
                  >
                    <div
                      style={{
                        padding: '8px 10px',
                        display: 'flex',
                        justifyContent: 'space-between',
                        gap: 12,
                        flexWrap: 'wrap',
                        alignItems: 'center',
                      }}
                    >
                      <div>
                        <div style={{ fontSize: 12, fontWeight: 600, lineHeight: 1.2, color: 'var(--text-primary)' }}>
                          {engineGrafana?.title || `${data?.engine_type?.toUpperCase() || 'ENGINE'} Grafana`}
                        </div>
                        <div style={{ marginTop: 2, color: 'var(--text-muted)', fontSize: 11 }}>
                          原始看板优先 · 运行中自动刷新，结束后切终态时间窗
                        </div>
                      </div>
                      <Space size="small" wrap>
                        <Tag color="blue">原始看板优先</Tag>
                        {engineGrafanaIframeUrl ? (
                          <Button size="small" type="link" href={engineGrafanaIframeUrl} target="_blank">
                            新窗口打开
                          </Button>
                        ) : null}
                        <Button size="small" type="link" onClick={() => setActiveSubTab('stats')}>
                          页面侧性能指标
                        </Button>
                      </Space>
                    </div>
                  </div>
                  {!engineGrafana ? (
                    <div style={{ padding: 24, background: 'var(--card-bg)', border: '1px solid var(--border-color)', borderRadius: 2 }}>
                      <Empty description="暂无引擎 Grafana 看板" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                    </div>
                  ) : engineGrafana.embed_mode === 'new_tab' || !engineGrafanaIframeUrl ? (
                    <div style={{ padding: 24, textAlign: 'center', background: 'var(--card-bg)', border: '1px solid var(--border-color)', borderRadius: 2 }}>
                      <Empty description="原始 Grafana 仅支持新窗口打开" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                      <Button type="primary" href={engineGrafanaIframeUrl || undefined} target="_blank" style={{ marginTop: 16 }}>
                        点击打开原始 Grafana
                      </Button>
                    </div>
                  ) : (
                    <>
                  <div
                    data-testid="run-detail-grafana-toolbar"
                    style={{
                      ...monitorWorkspaceShellStyle,
                    }}
                  >
                    <div
                      style={{
                        ...monitorWorkspaceToolbarStyle,
                        display: 'flex',
                        justifyContent: 'space-between',
                        gap: 8,
                        flexWrap: 'wrap',
                        alignItems: 'center',
                        padding: '6px 8px',
                        background: 'var(--surface-subtle)',
                      }}
                    >
                      <div>
                        <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-primary)', lineHeight: 1.3 }}>{engineGrafana.title}</div>
                      </div>
                      <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>原始 Grafana 主视图 · 运行中锁定 iframe，终态刷新一次</div>
                    </div>
                  </div>
                  <div style={{ ...monitorWorkspaceShellStyle, padding: 0 }}>
                    <div style={{ position: 'relative' }}>
                      <iframe
                        src={engineGrafanaIframeUrl || undefined}
                        data-testid="run-detail-grafana-iframe"
                        style={{ width: '100%', height: '1120px', border: 'none', display: 'block', background: 'var(--card-bg)' }}
                        title={engineGrafana.title}
                      />
                    </div>
                  </div>
                    </>
                  )}
                </>
              )
            })()}
          </div>
        )}
      </div>
    )
  }

  // 渲染单个 Dashboard 卡片
  const renderDashboardCard = (dashboard: RunDashboardLink) => {
    const canEmbed = dashboard.embed_mode === 'iframe' && Boolean(dashboard.url)
    if (!canEmbed) {
      return (
        <Card
          size="small"
          title={dashboard.title}
          extra={
            <Button type="link" href={dashboard.url} target="_blank">
              新窗口打开
            </Button>
          }
        >
          <Empty description={`${dashboard.title} 需要在新窗口打开`} image={Empty.PRESENTED_IMAGE_SIMPLE} />
        </Card>
      )
    }
    return (
      <Card
        size="small"
        title={dashboard.title}
        extra={
          <Button type="link" href={dashboard.url} target="_blank">
            新窗口打开
          </Button>
        }
      >
        <iframe
          src={dashboard.url}
          style={{ width: '100%', height: '500px', border: 'none' }}
          title={dashboard.title}
        />
      </Card>
    )
  }

  const renderAlertEventsPanel = () => {
    const alertEvents = alertEventsData?.items ?? []
    const summary = alertEventsData?.summary
    const highestSeverity = summary?.highest_severity ?? null
    const runParams = (data?.params ?? {}) as Record<string, unknown>
    const alertPolicySnapshots = summarizeRunAlertPolicies(runParams)
    const alertSubscriptions = toStringList(runParams.alert_subscriptions)
    const hasRunAlertConfig = alertPolicySnapshots.length > 0 || alertSubscriptions.length > 0
    const summaryText = summary
      ? `共 ${summary.total ?? alertEvents.length} 条 · firing ${summary.firing_total ?? 0} · resolved ${summary.resolved_total ?? 0}`
      : '外部告警事件已进入 OpenLoadHub'

    return (
      <Card
        title="外部告警事件"
        size="small"
        data-testid="run-detail-alert-events"
        loading={alertEventsLoading}
        extra={
          <Space size="small" wrap>
            {highestSeverity ? (
              <Tag color={getAlertEventSeverityColor(highestSeverity)}>{highestSeverity}</Tag>
            ) : null}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>{summaryText}</Typography.Text>
          </Space>
        }
      >
        {alertEventsIsError ? (
          <Alert
            type="warning"
            showIcon
            message="外部告警事件暂不可用"
            description="RunDetail 主体不受影响；待告警事件接口恢复后会在此展示。"
          />
        ) : alertEvents.length === 0 ? (
          hasRunAlertConfig ? (
            <Space direction="vertical" size={10} style={{ width: '100%' }} data-testid="run-detail-alert-events-empty-reason">
              <Alert
                type="info"
                showIcon
                message="本 Run 已快照告警策略，但还没有外部告警事件进入 OpenLoadHub"
                description="任务里的告警订阅不是阈值规则创建器；触发阈值、持续时间和 firing/resolved 判定由 Grafana / Prometheus / SkyWalking 等外部告警源定义。外部 webhook 事件进入 OpenLoadHub 后，才会在这里展示并按匹配策略执行动作。"
              />
              {alertSubscriptions.length > 0 ? (
                <Space size={[6, 6]} wrap>
                  <Typography.Text type="secondary">订阅快照</Typography.Text>
                  {alertSubscriptions.map(item => <Tag key={item}>{item}</Tag>)}
                </Space>
              ) : null}
              {alertPolicySnapshots.length > 0 ? (
                <Table<RunAlertPolicySnapshot>
                  size="small"
                  rowKey={(record, index) => `${record.name}-${record.alertname}-${index}`}
                  dataSource={alertPolicySnapshots}
                  pagination={false}
                  scroll={{ x: 920 }}
                  columns={[
                    { title: '策略', dataIndex: 'name', key: 'name' },
                    { title: '来源', dataIndex: 'source', key: 'source', width: 170 },
                    { title: '匹配订阅', dataIndex: 'subscription', key: 'subscription', width: 160 },
                    { title: '匹配告警名', dataIndex: 'alertname', key: 'alertname', width: 180 },
                    { title: '级别', dataIndex: 'severity', key: 'severity', width: 140 },
                    { title: '动作', dataIndex: 'actions', key: 'actions', width: 200 },
                    {
                      title: '执行边界',
                      key: 'execution',
                      width: 180,
                      render: (_: unknown, record: RunAlertPolicySnapshot) => (
                        <Space size={4} wrap>
                          <Tag color={record.autoStop ? 'red' : 'default'}>
                            {record.autoStop ? '自动止停开启' : '自动止停关闭'}
                          </Tag>
                          <Tag color={record.observeOnly ? 'blue' : 'orange'}>
                            {record.observeOnly ? '仅观测' : '执行动作'}
                          </Tag>
                        </Space>
                      ),
                    },
                  ]}
                />
              ) : null}
            </Space>
          ) : (
            <Empty description="暂无外部告警事件进入 OpenLoadHub" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          )
        ) : (
          <Table<RunAlertEvent>
            size="small"
            rowKey={(record, index) => String(record.event_id ?? record.id ?? `${record.alertname ?? 'alert'}-${record.starts_at ?? index}`)}
            dataSource={alertEvents}
            pagination={false}
            scroll={{ x: 920 }}
            columns={[
              {
                title: 'Status',
                dataIndex: 'status',
                key: 'status',
                width: 110,
                render: (value: string | null | undefined) => (
                  <Tag color={getAlertEventStatusColor(value)}>{value || '-'}</Tag>
                ),
              },
              {
                title: 'Severity / Priority',
                key: 'severity',
                width: 170,
                render: (_: unknown, record: RunAlertEvent) => (
                  <Space size={4} wrap>
                    <Tag color={getAlertEventSeverityColor(record.severity)}>{record.severity || '-'}</Tag>
                    {record.priority ? <Tag>{record.priority}</Tag> : null}
                  </Space>
                ),
              },
              {
                title: 'Alertname',
                dataIndex: 'alertname',
                key: 'alertname',
                render: (value: string | null | undefined) => value || '-',
              },
              {
                title: 'Subscription / Source',
                key: 'source',
                width: 220,
                render: (_: unknown, record: RunAlertEvent) => (
                  <div style={{ display: 'grid', gap: 2 }}>
                    <span>{record.subscription || '-'}</span>
                    {record.source ? <Typography.Text type="secondary">{record.source}</Typography.Text> : null}
                  </div>
                ),
              },
              {
                title: 'Starts At',
                dataIndex: 'starts_at',
                key: 'starts_at',
                width: 170,
                render: (value: string | null | undefined) => formatDateTime(value),
              },
              {
                title: 'Dashboard',
                dataIndex: 'dashboard_url',
                key: 'dashboard_url',
                width: 130,
                render: (value: string | null | undefined) => value ? (
                  <Button type="link" size="small" href={value} target="_blank">
                    打开看板
                  </Button>
                ) : '-',
              },
              {
                title: 'Action Status',
                dataIndex: 'action_status',
                key: 'action_status',
                width: 150,
                render: (value: string | null | undefined) => value || '-',
              },
            ]}
          />
        )}
      </Card>
    )
  }

  // 关联监控 Tab
  const renderMonitorTab = () => {
    return (
      <div style={{ display: 'grid', gap: 12 }}>
        {renderAlertEventsPanel()}
        <Card title="关联监控" size="small" data-testid="run-detail-monitor" loading={dashboardsLoading}>
          {relatedMonitorDashboards.length === 0 ? (
            <Empty description="暂无关联监控" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          ) : (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(500px, 1fr))', gap: 16 }}>
              {relatedMonitorDashboards.map(dashboard => (
                <div key={dashboard.dashboard_type + dashboard.title}>{renderDashboardCard(dashboard)}</div>
              ))}
            </div>
          )}
        </Card>
      </div>
    )
  }

  const tabItems = [
    { key: 'pressure', label: '发压端', children: renderPressureTab() },
    { key: 'monitor', label: '关联监控', children: renderMonitorTab() },
  ]

  return (
    <div data-testid="run-detail" className="olh-page-shell olh-run-detail-page olh-console-page">
      <>
        <div
          data-testid="run-detail-primary-flow"
          className="olh-run-detail-workbench"
          style={primaryFlowWorkbenchStyle}
        >
          <div style={primaryFlowHeaderStyle} className="olh-run-detail-hero">
            <div style={primaryFlowToolbarStyle} className="olh-run-detail-toolbar">
              <div className="olh-page-breadcrumb">{breadcrumbLabel}</div>
              <Space data-testid="run-detail-actions" wrap size="small" className="olh-run-detail-actions">
                <Tooltip title={dynamicK6ControlEnabled ? '跳转到运行时控制区' : '动态 TPS 调整将在后续版本开放'}>
                  <span>
                    <Button
                      size="small"
                      style={dynamicK6ControlEnabled ? primaryFlowDarkActionButtonStyle : primaryFlowLightActionStyle}
                      disabled={!dynamicK6ControlEnabled}
                      onClick={dynamicK6ControlEnabled ? handleScrollToProcess : undefined}
                    >
                      运行时控制
                    </Button>
                  </span>
                </Tooltip>
                <Button
                  className="olh-run-detail-action-stop"
                  size="small"
                  style={primaryFlowLightActionStyle}
                  disabled={!data || !isActiveRunStatus(data.run_status)}
                  loading={stopMutation.isPending}
                  onClick={() => Number.isFinite(runIdNum) && stopMutation.mutate(runIdNum)}
                >
                  结束压测
                </Button>
                <Tooltip title={runTerminalActionLocked ? terminalRunActionTooltip : undefined}>
                  <span>
                    <Button
                      className="olh-run-detail-action-view-report"
                      size="small"
                      style={reportActionDisabled ? primaryFlowDisabledActionStyle : primaryFlowDarkActionButtonStyle}
                      disabled={reportActionDisabled}
                      loading={viewingReport}
                      onClick={handleViewReport}
                    >
                      查看报告
                    </Button>
                  </span>
                </Tooltip>
                <Tooltip title={runTerminalActionLocked ? terminalRunActionTooltip : undefined}>
                  <span>
                    <Button
                      size="small"
                      style={reportActionDisabled ? primaryFlowDisabledActionStyle : primaryFlowDarkActionButtonStyle}
                      disabled={reportActionDisabled}
                      loading={downloadingReport}
                      onClick={handleDownloadReport}
                    >
                      下载报告
                    </Button>
                  </span>
                </Tooltip>
                <Tooltip title={runTerminalActionLocked ? terminalRunActionTooltip : undefined}>
                  <span>
                    <Button
                      size="small"
                      style={reportActionDisabled ? primaryFlowDisabledActionStyle : primaryFlowLightActionStyle}
                      disabled={reportActionDisabled}
                      loading={regeneratingReport}
                      onClick={handleRegenerateReport}
                    >
                      重新生成
                    </Button>
                  </span>
                </Tooltip>
                <Tooltip title={runTerminalActionLocked ? terminalRunActionTooltip : undefined}>
                  <span>
                    <Button
                      size="small"
                      style={baselineActionDisabled ? primaryFlowDisabledActionStyle : primaryFlowDarkActionButtonStyle}
                      disabled={baselineActionDisabled}
                      loading={setBaselineMutation.isPending}
                      onClick={handleSetBaseline}
                    >
                      设为基线
                    </Button>
                  </span>
                </Tooltip>
                <Button size="small" type="link" style={primaryFlowLinkActionStyle} onClick={handleBack}>
                  {backLabel}
                </Button>
              </Space>
            </div>
            <div style={primaryFlowTitleRowStyle} className="olh-run-detail-title-row">
              <div className="olh-run-detail-title-copy">
                <Space align="center" size={6}>
                  <h1 className="olh-page-title" style={{ margin: 0 }} data-testid="run-detail-title">{titleText}</h1>
                  {data?.run_status && <StatusBadge status={data.run_status} text={data.run_status_label || undefined} />}
                </Space>
                <div className="olh-page-subtitle">{data?.task_name || '-'}</div>
              </div>
              <div className="olh-run-detail-id">执行 ID {runIdNum || '-'}</div>
            </div>
            <div className="olh-kpi-grid olh-run-detail-kpis">
              {runDetailHeroStats.map(stat => (
                <div key={stat.key} className={`olh-kpi-card olh-kpi-card--${stat.key} olh-run-detail-kpi-card`}>
                  <div className="olh-kpi-label">{stat.label}</div>
                  <div className="olh-kpi-value">
                    {stat.value}
                    <span className="olh-kpi-unit">{stat.unit}</span>
                  </div>
                  <div className="olh-kpi-helper">{stat.helper}</div>
                </div>
              ))}
            </div>
          </div>

          {renderBaseInfo()}

          {/* 接口级核心指标表 */}
          {renderSummaryMetrics()}

          {/* Group-Checks 表 */}
          {renderGroupChecks()}
        </div>
        <div ref={processCardRef} style={{ marginBottom: 16 }}>
          {renderProcessStages()}
          {dynamicK6ControlEnabled ? renderK6ControlCard() : null}
        </div>
      </>

      {/* 一级 Tab 容器 */}
      <Tabs
        activeKey={activeTab}
        onChange={key => setActiveTab(key as TabKey)}
        items={tabItems}
      />
    </div>
  )
}

export default RunDetail
const { Text } = Typography
