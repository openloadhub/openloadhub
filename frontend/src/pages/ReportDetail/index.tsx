import { useParams, useNavigate } from 'react-router-dom'
import { Button, Card, Col, Descriptions, Empty, Row, Space, Spin, Tabs, Tag, Typography, message } from 'antd'
import { ArrowLeftOutlined, DownloadOutlined, EyeOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo } from 'react'
import { ChartPanel, MetricCard } from '@/components/common'
import { reportApi, type Report } from '@/services/reportApi'
import { useThemeStore } from '@/store/themeStore'
import dayjs from 'dayjs'
import type { EChartsOption } from 'echarts'

const { Text } = Typography

const QUALITY_GATE_STATUS_COLORS: Record<string, string> = {
  ready: 'green',
  partial: 'orange',
  blocked: 'red',
  missing: 'red',
}

const QUALITY_SECTION_ORDER = [
  'report_frontdoor',
  'summary_metrics',
  'checks',
  'endpoint_trends',
  'observability',
]

const REPORT_CHART_COLORS = {
  primary: '#0d9488',
  success: 'rgba(100, 116, 139, 0.56)',
  warning: 'rgba(202, 138, 4, 0.62)',
  danger: 'rgba(220, 38, 38, 0.5)',
  neutral: '#64748b',
  muted: '#94a3b8',
}

type ReportChartTheme = {
  text: string
  title: string
  split: string
  axis: string
  pointer: string
  tooltipBg: string
  tooltipBorder: string
  pieBorder: string
}

const getReportChartTheme = (theme: 'light' | 'dark'): ReportChartTheme => (
  theme === 'dark'
    ? {
      text: '#a8b4c2',
      title: '#f4f8fb',
      split: 'rgba(148, 163, 184, 0.16)',
      axis: 'rgba(148, 163, 184, 0.32)',
      pointer: '#7dded2',
      tooltipBg: '#0a1017',
      tooltipBorder: '#273244',
      pieBorder: '#0b1118',
    }
    : {
      text: '#526173',
      title: '#0f172a',
      split: '#e5e7eb',
      axis: '#cbd5e1',
      pointer: '#64748b',
      tooltipBg: '#ffffff',
      tooltipBorder: '#d9e1e8',
      pieBorder: '#ffffff',
    }
)

/* ---------- helpers ---------- */

/** 安全取数 */
const num = (v: unknown): number | null =>
  typeof v === 'number' && Number.isFinite(v) ? v : null

/** 格式化数字 */
const fmt = (v: number | null | undefined, suffix = '', decimals = 2) =>
  v == null ? '-' : `${Number(v).toFixed(decimals)}${suffix}`

/** 从 metrics_data 中提取汇总指标 */
function extractSummary(report: Report) {
  const md = report.metrics_data ?? {}
  const jtl = (md.jtl_summary ?? {}) as Record<string, unknown>
  const k6 = (md.k6_summary ?? {}) as Record<string, unknown>
  const src = Object.keys(k6).length > 0 ? k6 : jtl

  return {
    totalRequests: num(report.total_requests) ?? num(src.total_requests) ?? 0,
    successfulRequests: num(report.successful_requests) ?? num(src.successful_requests),
    failedRequests: num(report.failed_requests) ?? num(src.failed_requests),
    errorRate: num(report.error_rate) ?? num(src.error_rate),
    avgRt: num(report.avg_response_time) ?? num(src.avg_response_time) ?? num(src.rt_avg_ms),
    p95Rt: num(report.p95_response_time) ?? num(src.p95_response_time) ?? num(src.rt_p95_ms),
    p99Rt: num(report.p99_response_time) ?? num(src.p99_response_time) ?? num(src.rt_p99_ms),
    throughput: num(report.throughput) ?? num(src.throughput) ?? num(src.rps),
    engineType: report.report_type,
  }
}

/* ---------- chart builders ---------- */

/** 请求分布饼图 */
function buildRequestPieOption(success: number | null, fail: number | null, chartTheme: ReportChartTheme): EChartsOption {
  const s = success ?? 0
  const f = fail ?? 0
  if (s === 0 && f === 0) return {}
  return {
    tooltip: {
      trigger: 'item',
      formatter: '{b}: {c} ({d}%)',
      backgroundColor: chartTheme.tooltipBg,
      borderColor: chartTheme.tooltipBorder,
      textStyle: { color: chartTheme.title },
    },
    legend: { bottom: 0, left: 'center', textStyle: { color: chartTheme.text } },
    series: [{
      type: 'pie',
      radius: ['48%', '64%'],
      avoidLabelOverlap: false,
      itemStyle: { borderRadius: 4, borderColor: chartTheme.pieBorder, borderWidth: 2 },
      label: { show: true, formatter: '{b}\n{d}%', color: chartTheme.text },
      data: [
        { value: s, name: '成功', itemStyle: { color: REPORT_CHART_COLORS.success } },
        { value: f, name: '失败', itemStyle: { color: REPORT_CHART_COLORS.danger } },
      ],
    }],
  }
}

/** 响应时间对比柱状图 */
function buildRtBarOption(avg: number | null, p95: number | null, p99: number | null, chartTheme: ReportChartTheme): EChartsOption {
  const items = [
    { name: 'AVG', value: avg },
    { name: 'P95', value: p95 },
    { name: 'P99', value: p99 },
  ].filter(i => i.value != null)
  if (items.length === 0) return {}
  return {
    tooltip: {
      trigger: 'axis',
      formatter: '{b}: {c} ms',
      backgroundColor: chartTheme.tooltipBg,
      borderColor: chartTheme.tooltipBorder,
      textStyle: { color: chartTheme.title },
      axisPointer: { lineStyle: { color: chartTheme.axis } },
    },
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    xAxis: {
      type: 'category',
      data: items.map(i => i.name),
      axisLine: { lineStyle: { color: chartTheme.axis } },
      axisTick: { lineStyle: { color: chartTheme.axis } },
      axisLabel: { color: chartTheme.text },
    },
    yAxis: {
      type: 'value',
      name: 'ms',
      nameTextStyle: { color: chartTheme.text },
      axisLine: { lineStyle: { color: chartTheme.axis } },
      axisTick: { lineStyle: { color: chartTheme.axis } },
      axisLabel: { formatter: '{value}', color: chartTheme.text },
      splitLine: { lineStyle: { color: chartTheme.split } },
    },
    series: [{
      type: 'bar',
      barWidth: '40%',
      data: items.map(i => ({
        value: i.value,
        itemStyle: {
          color: i.name === 'AVG' ? REPORT_CHART_COLORS.primary : i.name === 'P95' ? REPORT_CHART_COLORS.warning : REPORT_CHART_COLORS.danger,
          borderRadius: [4, 4, 0, 0],
        },
      })),
    }],
  }
}

/** 错误率仪表盘 */
function buildErrorGaugeOption(errorRate: number | null, chartTheme: ReportChartTheme): EChartsOption {
  if (errorRate == null) {
    return {
      title: {
        text: '暂无错误率数据',
        left: 'center',
        top: 'middle',
        textStyle: {
          color: chartTheme.text,
          fontSize: 14,
          fontWeight: 500,
        },
      },
    }
  }

  const val = +(errorRate * 100).toFixed(2)
  const quietTrack = 'rgba(100, 116, 139, 0.22)'
  const warningTrack = val > 0 ? REPORT_CHART_COLORS.warning : quietTrack
  const dangerTrack = val > 5 ? REPORT_CHART_COLORS.danger : quietTrack
  const primaryTrack = val > 0 ? REPORT_CHART_COLORS.primary : quietTrack
  return {
    series: [{
      type: 'gauge',
      startAngle: 200,
      endAngle: -20,
      min: 0,
      max: 100,
      radius: '90%',
      progress: { show: val > 0, width: 10 },
      axisLine: {
        lineStyle: {
          width: 10,
          color: [
            [0.05, primaryTrack],
            [0.2, warningTrack],
            [1, dangerTrack],
          ],
        },
      },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: { show: false },
      pointer: { show: true, length: '60%', width: 4, itemStyle: { color: chartTheme.pointer } },
      detail: {
        valueAnimation: true,
        formatter: '{value}%',
        fontSize: 20,
        fontWeight: 'bold',
        color: chartTheme.title,
        offsetCenter: [0, '70%'],
      },
      data: [{ value: val, name: '错误率' }],
      title: { offsetCenter: [0, '90%'], fontSize: 14, color: chartTheme.text },
    }],
  }
}

/** 吞吐量仪表盘 */
function buildThroughputGaugeOption(throughput: number | null, chartTheme: ReportChartTheme): EChartsOption {
  const val = throughput ?? 0
  // 根据实际值动态设置上限
  const max = Math.max(val * 2, 100)
  return {
    series: [{
      type: 'gauge',
      startAngle: 200,
      endAngle: -20,
      min: 0,
      max,
      radius: '90%',
      progress: { show: true, width: 10 },
      axisLine: {
        lineStyle: {
          width: 10,
          color: [
            [0.9, REPORT_CHART_COLORS.neutral],
            [1, REPORT_CHART_COLORS.primary],
          ],
        },
      },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: { show: false },
      pointer: { show: true, length: '60%', width: 4, itemStyle: { color: chartTheme.pointer } },
      detail: {
        valueAnimation: true,
        formatter: (v: number) => `${v.toFixed(1)} rps`,
        fontSize: 20,
        fontWeight: 'bold',
        color: chartTheme.title,
        offsetCenter: [0, '70%'],
      },
      data: [{ value: val, name: '吞吐量' }],
      title: { offsetCenter: [0, '90%'], fontSize: 14, color: chartTheme.text },
    }],
  }
}

/* ---------- status helpers ---------- */

function statusColor(status: Report['status']) {
  switch (status) {
    case 'COMPLETED': return 'success'
    case 'FAILED': return 'error'
    case 'GENERATING': return 'processing'
    case 'PENDING': return 'default'
    case 'DELETED': return 'default'
    default: return 'default'
  }
}

function statusText(status: Report['status']) {
  switch (status) {
    case 'COMPLETED': return '已完成'
    case 'FAILED': return '失败'
    case 'GENERATING': return '生成中'
    case 'PENDING': return '待生成'
    case 'DELETED': return '已删除'
    default: return status
  }
}

/* ---------- component ---------- */

const ReportDetail = () => {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const theme = useThemeStore(state => state.theme)

  const {
    data: report,
    isLoading,
    error,
  } = useQuery({
    queryKey: ['report', id],
    queryFn: () => reportApi.getReportDetail(Number(id)),
    enabled: !!id,
  })

  const summary = useMemo(() => report ? extractSummary(report) : null, [report])

  useEffect(() => {
    if (error) message.error('加载报告失败')
  }, [error])

  if (error) {
    return (
      <div className="olh-page-shell olh-console-page olh-report-detail-page olh-report-state-page">
        <Empty description="加载报告失败" />
        <Button type="link" onClick={() => navigate('/my-focus/result-list')}>返回列表</Button>
      </div>
    )
  }

  if (isLoading || !report || !summary) {
    return (
      <div className="olh-page-shell olh-console-page olh-report-detail-page olh-report-state-page">
        <Spin size="large" tip="加载中..." />
      </div>
    )
  }

  const testConfig = report.test_config ?? {}
  const metricsData = report.metrics_data ?? {}
  const hasMetrics = Object.keys(metricsData).length > 0
  const qualityGate = report.quality_gate
  const fileSizeLabel = report.file_size ? `${(report.file_size / 1024).toFixed(1)} KB` : '-'
  const chartTheme = getReportChartTheme(theme)

  return (
    <div className="olh-page-shell olh-console-page olh-report-detail-page">
      <div className="olh-console-hero olh-report-hero">
        <div className="olh-console-hero-main">
          <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate('/my-focus/result-list')}>
            返回
          </Button>
          <div className="olh-page-breadcrumb">OpenLoadHub / Report Detail</div>
          <div className="olh-console-title-row">
            <h1 className="olh-page-title">{report.name}</h1>
            <span className={report.status === 'GENERATING' ? 'olh-live-pill olh-live-pill--active' : 'olh-live-pill'}>
              {statusText(report.status)}
            </span>
          </div>
          <div className="olh-page-subtitle">
            汇总报告元信息、质量门禁、核心指标和原始指标快照；下载与 HTML 查看仍按报告文件状态展示。
          </div>
          <div className="olh-console-command-strip">
            <span>{`报告 #${report.id}`}</span>
            <span>{`任务 #${report.task_id}`}</span>
            <span>{report.report_type}</span>
            <span>{`文件 ${fileSizeLabel}`}</span>
          </div>
        </div>
        <div className="olh-console-hero-side">
          <div className="olh-console-focus-panel">
            <div className="olh-console-focus-label">Report status</div>
            <div className="olh-console-focus-value">{statusText(report.status)}</div>
            <div className="olh-console-focus-copy">
              {report.generated_at ? dayjs(report.generated_at).format('YYYY-MM-DD HH:mm:ss') : '等待生成时间'}
            </div>
            <div className="olh-console-focus-meta">
              <span>{report.file_path ? 'HTML ready' : 'No file'}</span>
              <span>{report.report_type}</span>
            </div>
          </div>
          <Space wrap className="olh-console-actions">
            {report.file_path && (
              <Button
                icon={<EyeOutlined />}
                onClick={() => navigate(`/reports/${report.id}/view`)}
              >
                查看 HTML
              </Button>
            )}
            {report.file_path && (
              <Button
                icon={<DownloadOutlined />}
                onClick={async () => {
                  const hide = message.loading('下载中...', 0)
                  try {
                    const blob = await reportApi.downloadReport(report.id)
                    const url = window.URL.createObjectURL(blob)
                    const a = document.createElement('a')
                    a.style.display = 'none'
                    a.href = url
                    a.download = `report_${report.id}.html`
                    document.body.appendChild(a)
                    a.click()
                    window.URL.revokeObjectURL(url)
                    document.body.removeChild(a)
                  } catch (error) {
                    message.error('下载失败，文件可能不存在')
                  } finally {
                    hide()
                  }
                }}
              >
                下载报告
              </Button>
            )}
          </Space>
        </div>
      </div>

      {/* 基本信息 */}
      <Card className="olh-dashboard-panel olh-report-info-panel" size="small" title="基本信息">
        <Descriptions column={{ xs: 1, sm: 2, lg: 4 }} size="small">
          <Descriptions.Item label="报告ID">{report.id}</Descriptions.Item>
          <Descriptions.Item label="任务ID">{report.task_id}</Descriptions.Item>
          <Descriptions.Item label="报告类型">{report.report_type}</Descriptions.Item>
          <Descriptions.Item label="状态">
            <Tag color={statusColor(report.status)}>{statusText(report.status)}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label="创建时间">
            {dayjs(report.created_at).format('YYYY-MM-DD HH:mm:ss')}
          </Descriptions.Item>
          <Descriptions.Item label="生成时间">
            {report.generated_at ? dayjs(report.generated_at).format('YYYY-MM-DD HH:mm:ss') : '-'}
          </Descriptions.Item>
          <Descriptions.Item label="文件大小">{fileSizeLabel}</Descriptions.Item>
          <Descriptions.Item label="描述">{report.description || '-'}</Descriptions.Item>
        </Descriptions>
      </Card>

      {qualityGate ? (
        <Card
          className="olh-dashboard-panel olh-report-quality-panel"
          size="small"
          title="报告质量"
          extra={
            <Space size={8}>
              <Tag color={QUALITY_GATE_STATUS_COLORS[qualityGate.status] || 'default'}>
                {qualityGate.status_label || qualityGate.status}
              </Tag>
              {qualityGate.evidence_ready ? <Tag color="green">证据就绪</Tag> : null}
              {qualityGate.current_template ? <Tag color="blue">当前模板</Tag> : <Tag color="orange">需重生模板</Tag>}
            </Space>
          }
        >
          <Space direction="vertical" size={12} style={{ display: 'flex' }}>
            <Row gutter={[12, 12]}>
              {QUALITY_SECTION_ORDER
                .map(key => [key, qualityGate.required_sections[key]] as const)
                .filter(([, section]) => Boolean(section))
                .map(([key, section]) => (
                  <Col xs={24} sm={12} lg={8} key={key}>
                    <Card className="olh-report-quality-section" size="small" bordered>
                      <Space direction="vertical" size={6} style={{ display: 'flex' }}>
                        <Space wrap>
                          <Text strong>{section.label}</Text>
                          <Tag color={QUALITY_GATE_STATUS_COLORS[section.status] || 'default'}>
                            {section.status}
                          </Tag>
                        </Space>
                        <Text type="secondary">{section.detail || '-'}</Text>
                        {section.gaps.length > 0 ? (
                          <Space wrap size={[4, 4]}>
                            {section.gaps.map(item => (
                              <Tag key={item}>{item}</Tag>
                            ))}
                          </Space>
                        ) : null}
                      </Space>
                    </Card>
                  </Col>
                ))}
            </Row>
            {qualityGate.recommended_actions.length > 0 ? (
              <div className="olh-report-quality-note">
                <Text type="secondary">建议动作</Text>
                {qualityGate.recommended_actions.map(item => (
                  <Text key={item}>{item}</Text>
                ))}
              </div>
            ) : null}
            {qualityGate.limitations.length > 0 ? (
              <div className="olh-report-quality-note">
                <Text type="secondary">限制说明</Text>
                {qualityGate.limitations.map(item => (
                  <Text key={item}>{item}</Text>
                ))}
              </div>
            ) : null}
          </Space>
        </Card>
      ) : null}

      {/* 核心指标卡片 */}
      <Row gutter={[16, 16]} className="olh-report-metric-grid">
        <Col xs={12} sm={6}>
          <MetricCard
            title="总请求数"
            value={summary.totalRequests.toLocaleString()}
            valueStyle={{ color: 'var(--report-metric-primary)', fontSize: 28 }}
          />
        </Col>
        <Col xs={12} sm={6}>
          <MetricCard
            title="吞吐量 (RPS)"
            value={fmt(summary.throughput, ' rps', 1)}
            valueStyle={{ color: 'var(--report-metric-primary)', fontSize: 28 }}
          />
        </Col>
        <Col xs={12} sm={6}>
          <MetricCard
            title="错误率"
            value={fmt(summary.errorRate != null ? summary.errorRate * 100 : null, '%')}
            valueStyle={{
              color: summary.errorRate == null
                ? 'var(--text-tertiary)'
                : summary.errorRate > 0.05 ? 'var(--report-metric-danger)' : 'var(--report-metric-success)',
              fontSize: 28,
            }}
          />
        </Col>
        <Col xs={12} sm={6}>
          <MetricCard
            title="P95 响应时间"
            value={fmt(summary.p95Rt, ' ms', 1)}
            valueStyle={{ color: 'var(--report-metric-warning)', fontSize: 28 }}
          />
        </Col>
      </Row>

      {/* 图表区域 */}
      <Tabs
        className="olh-report-tabs"
        defaultActiveKey="charts"
        items={[
          {
            key: 'charts',
            label: '可视化分析',
            children: (
              <Row gutter={[16, 16]}>
                {/* 错误率仪表盘 */}
                <Col xs={24} md={12}>
                  <ChartPanel
                    title="错误率"
                    option={buildErrorGaugeOption(summary.errorRate, chartTheme)}
                    height={280}
                  />
                </Col>
                {/* 吞吐量仪表盘 */}
                <Col xs={24} md={12}>
                  <ChartPanel
                    title="吞吐量"
                    option={buildThroughputGaugeOption(summary.throughput, chartTheme)}
                    height={280}
                  />
                </Col>
                {/* 响应时间对比 */}
                <Col xs={24} md={12}>
                  <ChartPanel
                    title="响应时间对比"
                    option={buildRtBarOption(summary.avgRt, summary.p95Rt, summary.p99Rt, chartTheme)}
                    height={280}
                  />
                </Col>
                {/* 请求成功/失败分布 */}
                <Col xs={24} md={12}>
                  <ChartPanel
                    title="请求分布"
                    option={buildRequestPieOption(summary.successfulRequests, summary.failedRequests, chartTheme)}
                    height={280}
                  />
                </Col>
              </Row>
            ),
          },
          {
            key: 'config',
            label: '测试配置',
            children: (
              <Card>
                {Object.keys(testConfig).length > 0 ? (
                  <Descriptions column={2} size="small" bordered>
                    {Object.entries(testConfig).map(([k, v]) => (
                      <Descriptions.Item key={k} label={k}>
                        <Text code>{typeof v === 'object' ? JSON.stringify(v) : String(v ?? '-')}</Text>
                      </Descriptions.Item>
                    ))}
                  </Descriptions>
                ) : (
                  <Empty description="暂无测试配置数据" />
                )}
              </Card>
            ),
          },
          {
            key: 'raw',
            label: '原始数据',
            children: (
              <Card>
                {hasMetrics ? (
                  <pre className="olh-report-raw-pre">
                    {JSON.stringify(metricsData, null, 2)}
                  </pre>
                ) : (
                  <Empty description="暂无原始指标数据" />
                )}
              </Card>
            ),
          },
        ]}
      />
    </div>
  )
}

export default ReportDetail
