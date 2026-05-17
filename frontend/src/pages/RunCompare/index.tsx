import { useMemo } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Empty, Space, Table, Tag, Typography, message } from 'antd'
import dayjs from 'dayjs'

import { ChartPanel, StatusBadge } from '@/components/common'
import { runApi, type EndpointTrendMetric, type MetricName, type Run, type RunBaselineScopeType, type RunCompareBundle } from '@/services/runApi'
import { useThemeStore } from '@/store/themeStore'
import { formatLocalDateTime } from '@/utils/localDateTime'

const { Title, Text } = Typography

const INTERNAL_PARAM_KEYS = new Set([
  'seed_run_status',
  'seed_run_status_detail',
  'seed_started_at',
  'seed_ended_at',
  'seed_duration_seconds',
  'process',
  'summary_metrics',
  'endpoint_trends',
  'checks',
  'k8s_pods',
  'pods',
  'pod_monitor_series',
  'engine_grafana_url',
  'pod_grafana_url',
  'related_monitors',
  'logs',
  'metrics_s3',
  'checks_s3',
  'log_s3',
  'k8s_log_s3',
])

const PARAM_LABEL_MAP: Record<string, string> = {
  pod_count: '执行节点数',
  pod_num: '执行节点数',
  num_threads: '并发线程数',
  duration: '压测时长(秒)',
  total_tps: '总 TPS',
  target_tps: '目标 TPS（启动参数）',
  ramp_up: '预热时长(秒)',
  env: '压测环境',
  business_line: '业务线',
  region: '区域',
}

type CompareMetricRow = {
  key: string
  endpoint_name: string
  base_avg_rt_ms?: number | null
  comparator_avg_rt_ms?: number | null
  delta_avg_rt_ms?: number | null
  base_p95_rt_ms?: number | null
  comparator_p95_rt_ms?: number | null
  delta_p95_rt_ms?: number | null
  base_p99_rt_ms?: number | null
  comparator_p99_rt_ms?: number | null
  delta_p99_rt_ms?: number | null
  base_throughput?: number | null
  comparator_throughput?: number | null
  delta_throughput?: number | null
  base_total_requests?: number | null
  comparator_total_requests?: number | null
  delta_total_requests?: number | null
}

type CompareCheckRow = {
  key: string
  group_name: string
  check_name: string
  base_success_rate?: number | null
  comparator_success_rate?: number | null
  delta_success_rate?: number | null
}

type CompareParamRow = {
  key: string
  label: string
  base_value: string
  comparator_value: string
  changed: boolean
  delta_text?: string
  delta_value?: number | null
  delta_kind?: 'count' | 'throughput'
}

type CompareMonitorRow = {
  key: string
  label: string
  base_value: string
  comparator_value: string
  delta_text?: string
  delta_value?: number | null
  delta_kind?: 'count'
}

type CompareChartTheme = {
  text: string
  title: string
  empty: string
  split: string
  axis: string
  tooltipBg: string
  tooltipBorder: string
  palette: string[]
}

const metricTitleMap: Record<MetricName, string> = {
  rps: '实测吞吐量 (req/s)',
  rt_avg_ms: '平均响应时间 (ms)',
  rt_p95_ms: 'P95响应时间 (ms)',
  rt_p99_ms: 'P99响应时间 (ms)',
  error_rate: '错误率',
}

const formatDateTime = (value?: string | null) => formatLocalDateTime(value)

const formatRate = (value?: number | null) => (typeof value === 'number' ? `${(value * 100).toFixed(2)}%` : '-')
const formatMetric = (value?: number | null, fractionDigits = 2) => (typeof value === 'number' ? value.toFixed(fractionDigits) : '-')
const formatDelta = (value?: number | null, fractionDigits = 2) => {
  if (typeof value !== 'number') return '-'
  const prefix = value > 0 ? '+' : ''
  return `${prefix}${value.toFixed(fractionDigits)}`
}

const tryGetNumericDelta = (baseValue: unknown, comparatorValue: unknown) => {
  if (typeof baseValue !== 'number' || typeof comparatorValue !== 'number') {
    return null
  }
  return comparatorValue - baseValue
}

const getDeltaState = (kind: 'latency' | 'throughput' | 'count' | 'success_rate' | 'error_rate', value?: number | null) => {
  if (typeof value !== 'number' || value === 0) return 'neutral'
  if (kind === 'latency' || kind === 'error_rate') return value < 0 ? 'better' : 'worse'
  return value > 0 ? 'better' : 'worse'
}

const renderDeltaTag = (
  kind: 'latency' | 'throughput' | 'count' | 'success_rate' | 'error_rate',
  value?: number | null,
  fractionDigits = 2,
) => {
  if (typeof value !== 'number') return <Tag>-</Tag>
  const state = getDeltaState(kind, value)
  const color = state === 'better' ? 'green' : state === 'worse' ? 'red' : 'blue'
  return <Tag color={color}>{formatDelta(value, fractionDigits)}</Tag>
}

const renderRateDeltaTag = (value?: number | null) => {
  if (typeof value !== 'number') return <Tag>-</Tag>
  return renderDeltaTag('success_rate', value * 100, 2)
}

const normalizeParamValue = (value: unknown): string => {
  if (value == null) return '-'
  if (typeof value === 'string') return value || '-'
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

const resolveOverviewMetric = (primary: number | null | undefined, fallback: number | null | undefined) => {
  if (typeof primary === 'number') return primary
  if (typeof fallback === 'number') return fallback
  return null
}

const renderOverviewMissingReason = (kind: 'error_rate' | 'success_rate') => {
  if (kind === 'error_rate') {
    return <Tag>当前样本无错误率源数据</Tag>
  }
  return <Tag>当前样本无 checks 源数据</Tag>
}

const aggregateEndpointTrendPoints = (
  metric: EndpointTrendMetric,
  items: Array<{ points: Array<{ ts: string; value?: number | null }> }>,
) => {
  const pointMap = new Map<string, number[]>()
  for (const item of items) {
    for (const point of item.points || []) {
      if (typeof point.value !== 'number') continue
      const bucket = pointMap.get(point.ts) || []
      bucket.push(point.value)
      pointMap.set(point.ts, bucket)
    }
  }
  return Array.from(pointMap.entries())
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([ts, values]) => ({
      ts,
      value:
        metric === 'throughput'
          ? values.reduce((sum, value) => sum + value, 0)
          : values.reduce((sum, value) => sum + value, 0) / values.length,
    }))
}

const aggregateSummaryMetrics = (items: Array<{
  total_requests?: number | null
  throughput?: number | null
  avg_rt_ms?: number | null
  p95_rt_ms?: number | null
  p99_rt_ms?: number | null
}>) => {
  const totalRequests = items.reduce((sum, item) => sum + (item.total_requests || 0), 0)
  const throughput = items.reduce((sum, item) => sum + (item.throughput || 0), 0)
  const weighted = (key: 'avg_rt_ms' | 'p95_rt_ms' | 'p99_rt_ms') => {
    const candidates = items.filter((item): item is typeof item & { [K in typeof key]: number } => typeof item[key] === 'number')
    if (candidates.length === 0) return null
    const totalWeight = candidates.reduce((sum, item) => sum + (item.total_requests || 0), 0)
    if (totalWeight > 0) {
      return candidates.reduce((sum, item) => sum + (item[key] as number) * (item.total_requests || 0), 0) / totalWeight
    }
    return candidates.reduce((sum, item) => sum + (item[key] as number), 0) / candidates.length
  }
  return {
    total_requests: totalRequests > 0 ? totalRequests : null,
    throughput: throughput > 0 ? throughput : null,
    avg_rt_ms: weighted('avg_rt_ms'),
    p95_rt_ms: weighted('p95_rt_ms'),
    p99_rt_ms: weighted('p99_rt_ms'),
  }
}

const aggregateChecksSuccessRate = (items: Array<{ success_rate?: number | null }>) => {
  const values = items.filter((item): item is { success_rate: number } => typeof item.success_rate === 'number')
  if (values.length === 0) return null
  return values.reduce((sum, item) => sum + item.success_rate, 0) / values.length
}

const resolveOverviewErrorRate = (detail: Pick<Run, 'success_rate' | 'error_rate'>) => {
  if (typeof detail.error_rate === 'number') {
    return detail.error_rate
  }
  if (typeof detail.success_rate === 'number') {
    return Math.max(0, Math.min(1, 1 - detail.success_rate))
  }
  return null
}

const getComparableParams = (run?: Run) => {
  const params = run?.params ?? {}
  return Object.entries(params).filter(([key, value]) => {
    if (INTERNAL_PARAM_KEYS.has(key) || key.startsWith('seed_')) return false
    if (value == null) return false
    return ['string', 'number', 'boolean'].includes(typeof value)
  })
}

function buildMetricOption(
  baseRunId: number,
  comparatorRunId: number,
  metric: MetricName,
  basePoints: Array<{ ts: string; value?: number | null }>,
  comparatorPoints: Array<{ ts: string; value?: number | null }>,
  chartTheme: CompareChartTheme,
) {
  const allTs = Array.from(new Set([...basePoints.map(point => point.ts), ...comparatorPoints.map(point => point.ts)])).sort()
  const unit = metric === 'error_rate' ? 'ratio' : metric === 'rps' ? 'req/s' : 'ms'
  const hasData = allTs.length > 0

  return {
    graphic: hasData
      ? undefined
      : {
          type: 'text',
          left: 'center',
          top: 'middle',
          style: {
            text: '暂无数据',
            fill: chartTheme.empty,
            fontSize: 14,
          },
        },
    color: chartTheme.palette,
    tooltip: {
      trigger: 'axis' as const,
      backgroundColor: chartTheme.tooltipBg,
      borderColor: chartTheme.tooltipBorder,
      textStyle: { color: chartTheme.title },
      axisPointer: { lineStyle: { color: chartTheme.axis } },
    },
    legend: {
      data: [`基准 #${baseRunId}`, `对比 #${comparatorRunId}`],
      textStyle: { color: chartTheme.text },
    },
    grid: { left: 40, right: 20, top: 40, bottom: 40 },
    xAxis: {
      type: 'category' as const,
      data: allTs.map(ts => dayjs(ts).format('HH:mm:ss')),
      axisLine: { lineStyle: { color: chartTheme.axis } },
      axisTick: { lineStyle: { color: chartTheme.axis } },
      axisLabel: { color: chartTheme.text },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value' as const,
      name: unit,
      scale: true,
      nameTextStyle: { color: chartTheme.text },
      axisLine: { lineStyle: { color: chartTheme.axis } },
      axisTick: { lineStyle: { color: chartTheme.axis } },
      axisLabel: { color: chartTheme.text },
      splitLine: { lineStyle: { color: chartTheme.split } },
    },
    series: [
      {
        name: `基准 #${baseRunId}`,
        type: 'line' as const,
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 2 },
        data: allTs.map(ts => basePoints.find(point => point.ts === ts)?.value ?? null),
      },
      {
        name: `对比 #${comparatorRunId}`,
        type: 'line' as const,
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 2 },
        data: allTs.map(ts => comparatorPoints.find(point => point.ts === ts)?.value ?? null),
      },
    ],
  }
}

const RunCompare = () => {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [searchParams] = useSearchParams()
  const uiTheme = useThemeStore(state => state.theme)
  const baseId = Number(searchParams.get('base_id'))
  const comparatorId = Number(searchParams.get('comparator_id'))
  const validIds = Number.isFinite(baseId) && baseId > 0 && Number.isFinite(comparatorId) && comparatorId > 0

  const compareQuery = useQuery({
    queryKey: ['run-compare-page', baseId, comparatorId],
    queryFn: () => runApi.getRunCompare(baseId, comparatorId),
    enabled: validIds,
  })

  const loading = compareQuery.isLoading
  const baseData = compareQuery.data?.base
  const comparatorData = compareQuery.data?.comparator

  const chartTheme = useMemo<CompareChartTheme>(() => (
    uiTheme === 'dark'
      ? {
          text: '#A8B6C4',
          title: '#D8E2EC',
          empty: '#7F8DA0',
          split: 'rgba(148, 163, 184, 0.10)',
          axis: 'rgba(148, 163, 184, 0.16)',
          tooltipBg: '#1A2636',
          tooltipBorder: '#334155',
          palette: ['#80B8FF', '#9BE278', '#F2B963', '#FF7F8E', '#66D9CE'],
        }
      : {
          text: '#526173',
          title: '#0F172A',
          empty: '#999999',
          split: '#E6ECF2',
          axis: '#CBD5E1',
          tooltipBg: '#FFFFFF',
          tooltipBorder: '#D8E1EA',
          palette: ['#4F6FE5', '#73C461', '#B7791F', '#D92D20', '#0D9488'],
        }
  ), [uiTheme])

  const setBaselineMutation = useMutation({
    mutationFn: ({ runId, scopeType }: { runId: number; scopeType?: RunBaselineScopeType }) =>
      runApi.setRunBaseline(runId, { scope_type: scopeType }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['run-compare-page', baseId, comparatorId] })
      message.success('已更新基线')
    },
    onError: error => {
      message.error(error instanceof Error ? error.message : '更新基线失败')
    },
  })

  const sameTask = useMemo(() => {
    if (!baseData?.detail || !comparatorData?.detail) return true
    return baseData.detail.task_id === comparatorData.detail.task_id
  }, [baseData, comparatorData])

  const summaryRows = useMemo<CompareMetricRow[]>(() => {
    if (!baseData || !comparatorData) return []
    const baseItems = baseData.summary.items
    const comparatorItems = comparatorData.summary.items
    const endpoints = Array.from(
      new Set([...baseItems.map(item => item.endpoint_name), ...comparatorItems.map(item => item.endpoint_name)]),
    )
    return endpoints.map(endpoint_name => {
      const base = baseItems.find(item => item.endpoint_name === endpoint_name)
      const comparator = comparatorItems.find(item => item.endpoint_name === endpoint_name)
      const baseAvg = base?.avg_rt_ms ?? null
      const comparatorAvg = comparator?.avg_rt_ms ?? null
      const baseP95 = base?.p95_rt_ms ?? null
      const comparatorP95 = comparator?.p95_rt_ms ?? null
      const baseP99 = base?.p99_rt_ms ?? null
      const comparatorP99 = comparator?.p99_rt_ms ?? null
      const baseThroughput = base?.throughput ?? null
      const comparatorThroughput = comparator?.throughput ?? null
      const baseTotalRequests = base?.total_requests ?? null
      const comparatorTotalRequests = comparator?.total_requests ?? null
      return {
        key: endpoint_name,
        endpoint_name,
        base_avg_rt_ms: baseAvg,
        comparator_avg_rt_ms: comparatorAvg,
        delta_avg_rt_ms:
          typeof baseAvg === 'number' && typeof comparatorAvg === 'number' ? comparatorAvg - baseAvg : null,
        base_p95_rt_ms: baseP95,
        comparator_p95_rt_ms: comparatorP95,
        delta_p95_rt_ms:
          typeof baseP95 === 'number' && typeof comparatorP95 === 'number' ? comparatorP95 - baseP95 : null,
        base_p99_rt_ms: baseP99,
        comparator_p99_rt_ms: comparatorP99,
        delta_p99_rt_ms:
          typeof baseP99 === 'number' && typeof comparatorP99 === 'number' ? comparatorP99 - baseP99 : null,
        base_throughput: baseThroughput,
        comparator_throughput: comparatorThroughput,
        delta_throughput:
          typeof baseThroughput === 'number' && typeof comparatorThroughput === 'number'
            ? comparatorThroughput - baseThroughput
            : null,
        base_total_requests: baseTotalRequests,
        comparator_total_requests: comparatorTotalRequests,
        delta_total_requests:
          typeof baseTotalRequests === 'number' && typeof comparatorTotalRequests === 'number'
            ? comparatorTotalRequests - baseTotalRequests
            : null,
      }
    })
  }, [baseData, comparatorData])

  const checkRows = useMemo<CompareCheckRow[]>(() => {
    if (!baseData || !comparatorData) return []
    const baseItems = baseData.checks.items
    const comparatorItems = comparatorData.checks.items
    const keys = Array.from(
      new Set([
        ...baseItems.map(item => `${item.group_name}::${item.check_name}`),
        ...comparatorItems.map(item => `${item.group_name}::${item.check_name}`),
      ]),
    )
    return keys.map(key => {
      const [group_name, check_name] = key.split('::')
      const base = baseItems.find(item => item.group_name === group_name && item.check_name === check_name)
      const comparator = comparatorItems.find(item => item.group_name === group_name && item.check_name === check_name)
      const baseSuccessRate = base?.success_rate ?? null
      const comparatorSuccessRate = comparator?.success_rate ?? null
      return {
        key,
        group_name,
        check_name,
        base_success_rate: baseSuccessRate,
        comparator_success_rate: comparatorSuccessRate,
        delta_success_rate:
          typeof baseSuccessRate === 'number' && typeof comparatorSuccessRate === 'number'
            ? comparatorSuccessRate - baseSuccessRate
            : null,
      }
    })
  }, [baseData, comparatorData])

  const metricCharts = useMemo(() => {
    if (!baseData || !comparatorData) return []
    const metrics: MetricName[] = ['rps', 'rt_avg_ms', 'rt_p95_ms', 'rt_p99_ms', 'error_rate']
    return metrics.map(metric => {
      const fallbackMetricMap: Record<MetricName, EndpointTrendMetric | null> = {
        rps: 'throughput',
        rt_avg_ms: 'rt_avg_ms',
        rt_p95_ms: 'rt_p95_ms',
        rt_p99_ms: 'rt_p99_ms',
        error_rate: 'error_rate',
      }
      const baseSeries = baseData.metrics.series.find(series => series.metric === metric)?.points
        ?? (fallbackMetricMap[metric]
          ? aggregateEndpointTrendPoints(
              fallbackMetricMap[metric] as EndpointTrendMetric,
              baseData.endpoint_trends[fallbackMetricMap[metric] as EndpointTrendMetric]?.items ?? [],
            )
          : [])
      const comparatorSeries = comparatorData.metrics.series.find(series => series.metric === metric)?.points
        ?? (fallbackMetricMap[metric]
          ? aggregateEndpointTrendPoints(
              fallbackMetricMap[metric] as EndpointTrendMetric,
              comparatorData.endpoint_trends[fallbackMetricMap[metric] as EndpointTrendMetric]?.items ?? [],
            )
          : [])
      return {
        metric,
        option: buildMetricOption(baseId, comparatorId, metric, baseSeries, comparatorSeries, chartTheme),
      }
    })
  }, [baseData, comparatorData, baseId, comparatorId, chartTheme])

  const overviewItems = useMemo(() => {
    if (!baseData?.detail || !comparatorData?.detail) return []
    const base = baseData.detail
    const comparator = comparatorData.detail
    const baseOverview = baseData.overview_summary || base.overview_summary
    const comparatorOverview = comparatorData.overview_summary || comparator.overview_summary
    const baseSummaryAggregate = aggregateSummaryMetrics(baseData.summary.items)
    const comparatorSummaryAggregate = aggregateSummaryMetrics(comparatorData.summary.items)
    const baseThroughput = resolveOverviewMetric(
      baseOverview?.throughput,
      resolveOverviewMetric(base.rps, baseSummaryAggregate.throughput),
    )
    const comparatorThroughput = resolveOverviewMetric(
      comparatorOverview?.throughput,
      resolveOverviewMetric(comparator.rps, comparatorSummaryAggregate.throughput),
    )
    const baseAvgRt = resolveOverviewMetric(
      baseOverview?.avg_rt_ms,
      resolveOverviewMetric(base.avg_rt_ms, baseSummaryAggregate.avg_rt_ms),
    )
    const comparatorAvgRt = resolveOverviewMetric(
      comparatorOverview?.avg_rt_ms,
      resolveOverviewMetric(comparator.avg_rt_ms, comparatorSummaryAggregate.avg_rt_ms),
    )
    const baseP95Rt = resolveOverviewMetric(
      baseOverview?.p95_rt_ms,
      resolveOverviewMetric(base.p95_rt_ms, baseSummaryAggregate.p95_rt_ms),
    )
    const comparatorP95Rt = resolveOverviewMetric(
      comparatorOverview?.p95_rt_ms,
      resolveOverviewMetric(comparator.p95_rt_ms, comparatorSummaryAggregate.p95_rt_ms),
    )
    const baseErrorRate = resolveOverviewMetric(baseOverview?.error_rate, resolveOverviewErrorRate(base))
    const comparatorErrorRate = resolveOverviewMetric(comparatorOverview?.error_rate, resolveOverviewErrorRate(comparator))
    const baseRequestCount = resolveOverviewMetric(
      baseOverview?.total_requests,
      resolveOverviewMetric(base.total_requests, baseSummaryAggregate.total_requests),
    )
    const comparatorRequestCount = resolveOverviewMetric(
      comparatorOverview?.total_requests,
      resolveOverviewMetric(comparator.total_requests, comparatorSummaryAggregate.total_requests),
    )
    const baseCheckRate = resolveOverviewMetric(
      baseOverview?.checks_success_rate,
      resolveOverviewMetric(base.success_rate, aggregateChecksSuccessRate(baseData.checks.items)),
    )
    const comparatorCheckRate = resolveOverviewMetric(
      comparatorOverview?.checks_success_rate,
      resolveOverviewMetric(comparator.success_rate, aggregateChecksSuccessRate(comparatorData.checks.items)),
    )

    return [
      {
        label: '总请求量',
        base: baseRequestCount,
        comparator: comparatorRequestCount,
        delta:
          typeof baseRequestCount === 'number' && typeof comparatorRequestCount === 'number'
            ? comparatorRequestCount - baseRequestCount
            : null,
        unit: '',
        kind: 'count' as const,
      },
      {
        label: '实测吞吐量 (req/s)',
        base: baseThroughput,
        comparator: comparatorThroughput,
        delta: typeof baseThroughput === 'number' && typeof comparatorThroughput === 'number' ? comparatorThroughput - baseThroughput : null,
        unit: '',
        kind: 'throughput' as const,
      },
      {
        label: '平均响应时间 (ms)',
        base: baseAvgRt,
        comparator: comparatorAvgRt,
        delta: typeof baseAvgRt === 'number' && typeof comparatorAvgRt === 'number' ? comparatorAvgRt - baseAvgRt : null,
        unit: '',
        kind: 'latency' as const,
      },
      {
        label: 'P95 响应时间 (ms)',
        base: baseP95Rt,
        comparator: comparatorP95Rt,
        delta: typeof baseP95Rt === 'number' && typeof comparatorP95Rt === 'number' ? comparatorP95Rt - baseP95Rt : null,
        unit: '',
        kind: 'latency' as const,
      },
      {
        label: '错误率',
        base: baseErrorRate,
        comparator: comparatorErrorRate,
        delta:
          typeof baseErrorRate === 'number' && typeof comparatorErrorRate === 'number'
            ? comparatorErrorRate - baseErrorRate
            : null,
        unit: 'ratio',
        kind: 'error_rate' as const,
      },
      {
        label: 'Checks 成功率',
        base: baseCheckRate,
        comparator: comparatorCheckRate,
        delta:
          typeof baseCheckRate === 'number' && typeof comparatorCheckRate === 'number'
            ? comparatorCheckRate - baseCheckRate
            : null,
        unit: 'ratio',
        kind: 'success_rate' as const,
      },
    ]
  }, [baseData, comparatorData])

  const parameterRows = useMemo<CompareParamRow[]>(() => {
    const baseParams = new Map(getComparableParams(baseData?.detail))
    const comparatorParams = new Map(getComparableParams(comparatorData?.detail))
    const keys = Array.from(new Set([...baseParams.keys(), ...comparatorParams.keys()]))

    return keys
      .map(key => {
        const baseRawValue = baseParams.get(key)
        const comparatorRawValue = comparatorParams.get(key)
        const baseValue = normalizeParamValue(baseRawValue)
        const comparatorValue = normalizeParamValue(comparatorRawValue)
        const numericDelta = tryGetNumericDelta(baseRawValue, comparatorRawValue)
        const deltaKind: CompareParamRow['delta_kind'] =
          typeof numericDelta === 'number'
            ? ['pod_count', 'pod_num', 'num_threads'].includes(key)
              ? 'count'
              : key.includes('tps')
                ? 'throughput'
                : 'count'
            : undefined
        return {
          key,
          label: PARAM_LABEL_MAP[key] || key,
          base_value: baseValue,
          comparator_value: comparatorValue,
          changed: baseValue !== comparatorValue,
          delta_value: numericDelta,
          delta_text:
            typeof numericDelta === 'number'
              ? formatDelta(numericDelta, Number.isInteger(numericDelta) ? 0 : 2)
              : baseValue !== comparatorValue
                ? '已变化'
                : '一致',
          delta_kind: deltaKind,
        }
      })
      .sort((left, right) => left.label.localeCompare(right.label, 'zh-Hans-CN'))
  }, [baseData, comparatorData])

  const parameterSummary = useMemo(() => {
    const changedCount = parameterRows.filter(row => row.changed).length
    return {
      total: parameterRows.length,
      changed: changedCount,
      unchanged: parameterRows.length - changedCount,
    }
  }, [parameterRows])

  const monitorRows = useMemo<CompareMonitorRow[]>(() => {
    if (!baseData || !comparatorData) return []

    const buildCounter = (label: string, baseCount: number, comparatorCount: number) => ({
      key: label,
      label,
      base_value: String(baseCount),
      comparator_value: String(comparatorCount),
      delta_value: comparatorCount - baseCount,
      delta_text: formatDelta(comparatorCount - baseCount, 0),
      delta_kind: 'count' as const,
    })

    const baseDashboards = baseData.dashboards.items
    const comparatorDashboards = comparatorData.dashboards.items
    const baseRelatedMonitorCount = baseDashboards.filter(item => item.dashboard_type === 'related_monitor').length
    const comparatorRelatedMonitorCount = comparatorDashboards.filter(item => item.dashboard_type === 'related_monitor').length
    const baseEngineGrafana = Boolean(baseDashboards.find(item => item.dashboard_type === 'engine_grafana')?.url)
    const comparatorEngineGrafana = Boolean(comparatorDashboards.find(item => item.dashboard_type === 'engine_grafana')?.url)
    const basePodGrafana = Boolean(baseDashboards.find(item => item.dashboard_type === 'pod_grafana')?.url)
    const comparatorPodGrafana = Boolean(comparatorDashboards.find(item => item.dashboard_type === 'pod_grafana')?.url)

    return [
      buildCounter('接口指标项', baseData.summary.items.length, comparatorData.summary.items.length),
      buildCounter('Checks 项', baseData.checks.items.length, comparatorData.checks.items.length),
      buildCounter('趋势序列', baseData.metrics.series.length, comparatorData.metrics.series.length),
      buildCounter('关联监控入口', baseRelatedMonitorCount, comparatorRelatedMonitorCount),
      {
        key: 'engine-grafana',
        label: '引擎 Grafana',
        base_value: baseEngineGrafana ? '已接通' : '未接通',
        comparator_value: comparatorEngineGrafana ? '已接通' : '未接通',
        delta_value: null,
        delta_text: baseEngineGrafana === comparatorEngineGrafana ? '一致' : '已变化',
      },
      {
        key: 'pod-grafana',
        label: 'Pod Grafana',
        base_value: basePodGrafana ? '已接通' : '未接通',
        comparator_value: comparatorPodGrafana ? '已接通' : '未接通',
        delta_value: null,
        delta_text: basePodGrafana === comparatorPodGrafana ? '一致' : '已变化',
      },
    ]
  }, [baseData, comparatorData])

  const compareDataAvailability = useMemo(() => {
    if (!baseData || !comparatorData) {
      return {
        missingChecks: false,
        missingErrorRate: false,
      }
    }
    const missingChecks = baseData.checks.items.length === 0 && comparatorData.checks.items.length === 0
    const baseHasErrorSource = typeof baseData.detail.error_rate === 'number'
      || (baseData.metrics.series.find(series => series.metric === 'error_rate')?.points.length || 0) > 0
      || (baseData.endpoint_trends.error_rate?.items.length || 0) > 0
    const comparatorHasErrorSource = typeof comparatorData.detail.error_rate === 'number'
      || (comparatorData.metrics.series.find(series => series.metric === 'error_rate')?.points.length || 0) > 0
      || (comparatorData.endpoint_trends.error_rate?.items.length || 0) > 0
    return {
      missingChecks,
      missingErrorRate: !baseHasErrorSource && !comparatorHasErrorSource,
    }
  }, [baseData, comparatorData])

  const renderOverviewValue = (value?: number | null, unit?: string) => {
    if (typeof value !== 'number') {
      return <Text type="secondary">暂无</Text>
    }
    if (unit === 'ratio') return formatRate(value)
    return formatMetric(value)
  }

  const resolveBaselineScopeType = (bundle?: RunCompareBundle): RunBaselineScopeType | undefined => {
    if (!bundle?.detail) {
      return undefined
    }
    if (bundle.baseline?.scope_type) {
      return bundle.baseline.scope_type
    }
    return bundle.detail.protocol ? 'task_env_protocol' : 'task_env'
  }

  const renderRunMeta = (title: string, bundle?: RunCompareBundle) => {
    const run = bundle?.detail
    const baseline = bundle?.baseline
    const scopeType = resolveBaselineScopeType(bundle)
    return (
    <Card
      className="olh-run-compare-record-card"
      title={title}
      loading={loading}
      data-testid={title === '基准记录' ? 'run-compare-base-card' : 'run-compare-comparator-card'}
    >
      {run ? (
        <div style={{ display: 'grid', gap: 12 }}>
          <div className="olh-run-compare-run-head">
            <div className="olh-run-compare-run-title-block">
              <div className="olh-run-compare-run-title">{run.task_name || '-'}</div>
              <div className="olh-run-compare-muted">{`Run #${run.run_id} / Task #${run.task_id}`}</div>
            </div>
            <StatusBadge status={run.run_status} text={run.run_status_label || undefined} />
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <Tag>{run.engine_type_label || run.engine_type}</Tag>
            <Tag>{run.protocol || '-'}</Tag>
            <Tag>{run.env || '-'}</Tag>
            <Tag>{run.business_line || '无业务线'}</Tag>
            <Tag>{`操作人 ${run.operator_name || '-'}`}</Tag>
            <Tag>{`耗时 ${run.duration_seconds ? `${run.duration_seconds}s` : '-'}`}</Tag>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 10 }}>
            <div className="olh-run-compare-meta-cell">
              <div className="olh-run-compare-muted olh-run-compare-meta-label">开始时间</div>
              <div>{formatDateTime(run.started_at)}</div>
            </div>
            <div className="olh-run-compare-meta-cell">
              <div className="olh-run-compare-muted olh-run-compare-meta-label">结束时间</div>
              <div>{formatDateTime(run.ended_at)}</div>
            </div>
          </div>
          <div className="olh-run-compare-baseline-box">
            <div className="olh-run-compare-baseline-content">
              <div style={{ display: 'grid', gap: 4 }}>
                <div className="olh-run-compare-muted">Baseline</div>
                {baseline ? (
                  <>
                    <div>{baseline.scope_label}</div>
                    <Space wrap size={[6, 6]}>
                      <Tag color="blue">{baseline.scope_type}</Tag>
                      <Tag color={baseline.current_run_matches_baseline ? 'success' : 'default'}>
                        {baseline.current_run_matches_baseline ? '当前 Run 即 baseline' : `baseline #${baseline.baseline_run_id}`}
                      </Tag>
                      <Tag>{baseline.baseline_source}</Tag>
                    </Space>
                  </>
                ) : (
                  <Text type="secondary">当前 scope 尚未设置 baseline</Text>
                )}
              </div>
              <Button
                size="small"
                type="primary"
                loading={setBaselineMutation.isPending}
                disabled={!scopeType}
                onClick={() => setBaselineMutation.mutate({ runId: run.run_id, scopeType })}
              >
                设为当前 scope baseline
              </Button>
            </div>
          </div>
          <div className="olh-run-compare-muted">
            {`接口项 ${run.overview_summary?.endpoint_total ?? 0} / Pod ${run.pod_completed ?? 0}/${run.pod_actual ?? 0}/${run.pod_total ?? 0}`}
          </div>
        </div>
      ) : (
        <Empty description="暂无数据" />
      )}
    </Card>
    )
  }

  if (!validIds) {
    return (
      <Empty description="缺少对比参数">
        <Button onClick={() => navigate('/runs')}>返回结果列表</Button>
      </Empty>
    )
  }

  return (
    <div className="olh-page-shell olh-run-compare-page" data-testid="run-compare-page">
      <div className="olh-run-compare-header">
        <div>
          <Title level={2} style={{ margin: 0 }}>结果对比</Title>
          <Text type="secondary">{`基准 #${baseId} vs 对比 #${comparatorId}`}</Text>
        </div>
        <Space wrap className="olh-run-compare-header-actions">
          <Button onClick={() => navigate(-1)}>返回</Button>
          <Button onClick={() => navigate(`/runs/${baseId}`)}>查看基准详情</Button>
          <Button onClick={() => navigate(`/runs/${comparatorId}`)}>查看对比详情</Button>
        </Space>
      </div>

      {!sameTask && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="当前两条记录来自不同任务"
          description="已允许查看基础对比结果，但建议优先选择同一任务下的两条执行记录进行比对。"
        />
      )}

      {(compareDataAvailability.missingChecks || compareDataAvailability.missingErrorRate) && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="当前样本存在部分对比空态"
          description={[
            compareDataAvailability.missingChecks ? '这组样本当前没有 checks 实源，Checks 成功率会显示明确空态。' : null,
            compareDataAvailability.missingErrorRate ? '这组样本当前没有 error_rate 实源，错误率会显示明确空态。' : null,
            '如需完整对比链路，优先选择成功样本进行结果对比。',
          ].filter(Boolean).join(' ') }
        />
      )}

      <div className="olh-run-compare-record-grid">
        {renderRunMeta('基准记录', baseData)}
        {renderRunMeta('对比记录', comparatorData)}
      </div>

      <Card
        title="对比摘要"
        extra={<Text type="secondary">摘要为实测结果；目标 TPS 在参数快照中展示启动配置</Text>}
        style={{ marginBottom: 16 }}
        data-testid="run-compare-overview-card"
      >
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 12 }}>
          {overviewItems.map(item => (
            <div key={item.label} className="olh-run-compare-summary-cell">
              <div className="olh-run-compare-muted olh-run-compare-meta-label">{item.label}</div>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, marginBottom: 6 }}>
                <span>基准</span>
                <strong>{renderOverviewValue(item.base, item.unit)}</strong>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, marginBottom: 6 }}>
                <span>对比</span>
                <strong>{renderOverviewValue(item.comparator, item.unit)}</strong>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                <span>差值与趋势</span>
                {typeof item.base !== 'number' && typeof item.comparator !== 'number' && (item.kind === 'error_rate' || item.kind === 'success_rate')
                  ? renderOverviewMissingReason(item.kind)
                  : item.kind === 'success_rate'
                    ? renderRateDeltaTag(item.delta)
                    : item.unit === 'ratio'
                      ? renderDeltaTag(item.kind, typeof item.delta === 'number' ? item.delta * 100 : item.delta, 2)
                      : renderDeltaTag(item.kind, item.delta, item.kind === 'count' ? 0 : 2)}
              </div>
            </div>
          ))}
        </div>
      </Card>

      <Card title="参数快照对比" style={{ marginBottom: 16 }} data-testid="run-compare-params-card">
        <Space size={[8, 8]} wrap style={{ marginBottom: 12 }}>
          <Tag color="blue">{`参数项 ${parameterSummary.total}`}</Tag>
          <Tag color={parameterSummary.changed > 0 ? 'orange' : 'green'}>{`已变化 ${parameterSummary.changed}`}</Tag>
          <Tag color="green">{`一致 ${parameterSummary.unchanged}`}</Tag>
        </Space>
        <Table<CompareParamRow>
          rowKey="key"
          pagination={false}
          dataSource={parameterRows}
          locale={{ emptyText: '当前两条记录暂无可对比的运行参数' }}
          columns={[
            { title: '参数项', dataIndex: 'label', key: 'label', width: 220 },
            { title: `基准 #${baseId}`, dataIndex: 'base_value', key: 'base_value', width: 220 },
            { title: `对比 #${comparatorId}`, dataIndex: 'comparator_value', key: 'comparator_value', width: 220 },
            {
              title: '变化详情',
              dataIndex: 'delta_text',
              key: 'delta_text',
              width: 140,
              render: (_, record) =>
                record.delta_kind && typeof record.delta_value === 'number'
                  ? renderDeltaTag(record.delta_kind, record.delta_value, record.delta_kind === 'count' ? 0 : 2)
                  : <Tag color={record.changed ? 'orange' : 'green'}>{record.delta_text}</Tag>,
            },
            {
              title: '变化',
              dataIndex: 'changed',
              key: 'changed',
              width: 120,
              render: (_, record) => <Tag color={record.changed ? 'orange' : 'green'}>{record.changed ? '已变化' : '一致'}</Tag>,
            },
          ]}
          loading={loading}
        />
      </Card>

      <Card title="监控接通性对比" style={{ marginBottom: 16 }} data-testid="run-compare-monitor-card">
        <Table<CompareMonitorRow>
          rowKey="key"
          pagination={false}
          dataSource={monitorRows}
          columns={[
            { title: '观测项', dataIndex: 'label', key: 'label', width: 220 },
            { title: `基准 #${baseId}`, dataIndex: 'base_value', key: 'base_value', width: 220 },
            { title: `对比 #${comparatorId}`, dataIndex: 'comparator_value', key: 'comparator_value', width: 220 },
            {
              title: '变化详情',
              dataIndex: 'delta_text',
              key: 'delta_text',
              width: 160,
              render: (_, record) =>
                record.delta_kind && typeof record.delta_value === 'number'
                  ? renderDeltaTag(record.delta_kind, record.delta_value, 0)
                  : <Tag color={record.delta_text === '一致' ? 'green' : 'orange'}>{record.delta_text || '-'}</Tag>,
            },
          ]}
          loading={loading}
        />
      </Card>

      <Card title="核心指标对比" style={{ marginBottom: 16 }} data-testid="run-compare-summary-card">
        <Table<CompareMetricRow>
          rowKey="key"
          pagination={false}
          scroll={{ x: 1280 }}
          dataSource={summaryRows}
          columns={[
            { title: '接口名称', dataIndex: 'endpoint_name', key: 'endpoint_name', width: 220 },
            { title: `AVG #${baseId}`, dataIndex: 'base_avg_rt_ms', key: 'base_avg_rt_ms', width: 120, render: value => value ?? '-' },
            { title: `AVG #${comparatorId}`, dataIndex: 'comparator_avg_rt_ms', key: 'comparator_avg_rt_ms', width: 120, render: value => value ?? '-' },
            { title: 'AVG 差值', dataIndex: 'delta_avg_rt_ms', key: 'delta_avg_rt_ms', width: 120, render: value => renderDeltaTag('latency', value) },
            { title: `P95 #${baseId}`, dataIndex: 'base_p95_rt_ms', key: 'base_p95_rt_ms', width: 120, render: value => value ?? '-' },
            { title: `P95 #${comparatorId}`, dataIndex: 'comparator_p95_rt_ms', key: 'comparator_p95_rt_ms', width: 120, render: value => value ?? '-' },
            { title: 'P95 差值', dataIndex: 'delta_p95_rt_ms', key: 'delta_p95_rt_ms', width: 120, render: value => renderDeltaTag('latency', value) },
            { title: `P99 #${baseId}`, dataIndex: 'base_p99_rt_ms', key: 'base_p99_rt_ms', width: 120, render: value => value ?? '-' },
            { title: `P99 #${comparatorId}`, dataIndex: 'comparator_p99_rt_ms', key: 'comparator_p99_rt_ms', width: 120, render: value => value ?? '-' },
            { title: 'P99 差值', dataIndex: 'delta_p99_rt_ms', key: 'delta_p99_rt_ms', width: 120, render: value => renderDeltaTag('latency', value) },
            { title: `TPS #${baseId}`, dataIndex: 'base_throughput', key: 'base_throughput', width: 120, render: value => value ?? '-' },
            { title: `TPS #${comparatorId}`, dataIndex: 'comparator_throughput', key: 'comparator_throughput', width: 120, render: value => value ?? '-' },
            { title: 'TPS 差值', dataIndex: 'delta_throughput', key: 'delta_throughput', width: 120, render: value => renderDeltaTag('throughput', value) },
            { title: `请求总量 #${baseId}`, dataIndex: 'base_total_requests', key: 'base_total_requests', width: 140, render: value => value ?? '-' },
            { title: `请求总量 #${comparatorId}`, dataIndex: 'comparator_total_requests', key: 'comparator_total_requests', width: 140, render: value => value ?? '-' },
            { title: '请求总量差值', dataIndex: 'delta_total_requests', key: 'delta_total_requests', width: 140, render: value => renderDeltaTag('count', value, 0) },
          ]}
          loading={loading}
        />
      </Card>

      <Card title="Group-Checks 对比" style={{ marginBottom: 16 }} data-testid="run-compare-checks-card">
        <Table<CompareCheckRow>
          rowKey="key"
          pagination={false}
          scroll={{ x: 920 }}
          dataSource={checkRows}
          columns={[
            { title: 'groups', dataIndex: 'group_name', key: 'group_name', width: 220 },
            { title: 'checks', dataIndex: 'check_name', key: 'check_name', width: 360 },
            { title: `成功率 #${baseId}`, dataIndex: 'base_success_rate', key: 'base_success_rate', width: 160, render: value => formatRate(value) },
            { title: `成功率 #${comparatorId}`, dataIndex: 'comparator_success_rate', key: 'comparator_success_rate', width: 160, render: value => formatRate(value) },
            { title: '成功率变化', dataIndex: 'delta_success_rate', key: 'delta_success_rate', width: 140, render: value => renderRateDeltaTag(value) },
          ]}
          locale={{ emptyText: '当前两条记录暂无 checks 对比数据（本样本未产出 checks 源数据）' }}
          loading={loading}
        />
      </Card>

      <div className="olh-run-compare-trend-grid" data-testid="run-compare-trend-grid">
        {metricCharts.map(chart => (
          <ChartPanel
            key={chart.metric}
            title={metricTitleMap[chart.metric]}
            option={chart.option}
            height={280}
            loading={loading}
            data-testid={`run-compare-chart-${chart.metric}`}
          />
        ))}
      </div>
    </div>
  )
}

export default RunCompare
