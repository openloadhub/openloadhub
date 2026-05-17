import { useEffect, useMemo, useState } from 'react'
import { message, Button, Card, Popconfirm, Space, Tag, Tooltip } from 'antd'
import { CheckCircleOutlined, DeleteOutlined, DownloadOutlined, EyeOutlined, FileTextOutlined, SyncOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SearchFilter, DataTable } from '@/components/common'
import type { FilterField } from '@/components/common'
import { reportApi, type Report, type ReportListQuery } from '@/services/reportApi'
import dayjs from 'dayjs'

const REPORT_STATUS_LABELS: Record<Report['status'], string> = {
  PENDING: '待生成',
  GENERATING: '生成中',
  COMPLETED: '已完成',
  FAILED: '失败',
  DELETED: '已删除',
}

const ReportList = () => {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const [filterValues, setFilterValues] = useState<Record<string, unknown>>({})
  const [pagination, setPagination] = useState({
    current: 1,
    pageSize: 10,
  })

  const initialValues = useMemo(() => {
    const taskIdParam = searchParams.get('taskId') || searchParams.get('task_id')
    if (!taskIdParam) return undefined
    const taskId = Number(taskIdParam)
    if (!Number.isFinite(taskId)) return undefined
    return { task_id: taskId }
  }, [searchParams])

  useEffect(() => {
    if (!initialValues) return
    setFilterValues(initialValues)
    setPagination(prev => ({ ...prev, current: 1 }))
  }, [initialValues])

  // 查询报告列表
  const {
    data: reportData,
    isLoading,
  } = useQuery({
    queryKey: ['reports', filterValues, pagination],
    queryFn: () => {
      const params: ReportListQuery = {
        page: pagination.current,
        pageSize: pagination.pageSize,
        ...(filterValues as unknown as Partial<ReportListQuery>),
      }
      return reportApi.getReportList(params)
    },
  })
  const currentReports = reportData?.items || []
  const reportSummary = useMemo(() => {
    const completed = currentReports.filter(item => item.status === 'COMPLETED').length
    const generating = currentReports.filter(item => item.status === 'GENERATING').length
    const failed = currentReports.filter(item => item.status === 'FAILED').length
    const readyFiles = currentReports.filter(item => Boolean(item.file_path)).length

    return {
      total: reportData?.total || 0,
      pageTotal: currentReports.length,
      completed,
      generating,
      failed,
      readyFiles,
    }
  }, [currentReports, reportData?.total])

  // 删除报告
  const deleteMutation = useMutation({
    mutationFn: (id: number) => reportApi.deleteReport(id),
    onSuccess: () => {
      message.success('报告删除成功')
      queryClient.invalidateQueries({ queryKey: ['reports'] })
    },
    onError: (error: unknown) => {
      const errorMessage =
        error instanceof Error ? error.message : '报告删除失败'
      message.error(errorMessage)
    },
  })

  const handleDownloadReport = async (record: Report) => {
    const hide = message.loading('下载中...', 0)
    try {
      const blob = await reportApi.downloadReport(record.id)
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.style.display = 'none'
      a.href = url
      a.download = `report_${record.id}.html`
      document.body.appendChild(a)
      a.click()
      window.URL.revokeObjectURL(url)
      document.body.removeChild(a)
    } catch (error) {
      message.error('下载失败，文件可能不存在')
    } finally {
      hide()
    }
  }

  // 搜索筛选字段配置
  const filterFields: FilterField[] = [
    {
      name: 'task_id',
      label: '任务ID',
      type: 'input',
      placeholder: '请输入任务ID',
    },
    {
      name: 'status',
      label: '状态',
      type: 'select',
      options: [
        { label: '生成中', value: 'GENERATING' },
        { label: '已完成', value: 'COMPLETED' },
        { label: '失败', value: 'FAILED' },
        { label: '待生成', value: 'PENDING' },
        { label: '已删除', value: 'DELETED' },
      ],
    },
    {
      name: 'createTime',
      label: '创建时间',
      type: 'dateRange',
    },
  ]

  // 表格列配置
  const columns = [
    {
      title: '任务ID',
      dataIndex: 'task_id',
      key: 'task_id',
      width: 96,
      fixed: 'left' as const,
      className: 'olh-fixed-key-column',
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 112,
      render: (status: Report['status']) => (
        <span className={`olh-report-status-pill olh-report-status-pill--${status.toLowerCase()}`}>
          {REPORT_STATUS_LABELS[status] || status}
        </span>
      ),
    },
    {
      title: '总请求数',
      dataIndex: 'total_requests',
      key: 'total_requests',
      width: 112,
      render: (v: number | null | undefined) => (v ?? '-'),
    },
    {
      title: '错误率',
      dataIndex: 'error_rate',
      key: 'error_rate',
      width: 100,
      render: (rate: number | null | undefined) => (rate == null ? '-' : `${(rate * 100).toFixed(2)}%`),
    },
    {
      title: 'P95延迟',
      dataIndex: 'p95_response_time',
      key: 'p95_response_time',
      width: 112,
      render: (latency: number | null | undefined) => (latency == null ? '-' : `${latency}ms`),
    },
    {
      title: '生成时间',
      dataIndex: 'generated_at',
      key: 'generated_at',
      width: 176,
      render: (text: string) =>
        text ? dayjs(text).format('YYYY-MM-DD HH:mm:ss') : '-',
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 176,
      render: (text: string) =>
        text ? dayjs(text).format('YYYY-MM-DD HH:mm:ss') : '-',
    },
    {
      title: '操作',
      dataIndex: 'id',
      key: 'actions',
      width: 158,
      render: (_: number, record: Report) => {
        const fileReady = Boolean(record.file_path)

        return (
          <div className="olh-report-row-actions">
            <Button
              className="olh-report-row-action-primary"
              type="link"
              size="small"
              icon={<EyeOutlined />}
              onClick={() => navigate(`/reports/${record.id}`)}
            >
              详情
            </Button>
            <Tooltip title={fileReady ? '查看 HTML' : '暂无报告文件'}>
              <span>
                <Button
                  type="text"
                  size="small"
                  aria-label="查看 HTML"
                  disabled={!fileReady}
                  icon={<FileTextOutlined />}
                  onClick={() => navigate(`/reports/${record.id}/view`)}
                />
              </span>
            </Tooltip>
            <Tooltip title={fileReady ? '下载报告' : '暂无报告文件'}>
              <span>
                <Button
                  type="text"
                  size="small"
                  aria-label="下载报告"
                  disabled={!fileReady}
                  icon={<DownloadOutlined />}
                  onClick={() => handleDownloadReport(record)}
                />
              </span>
            </Tooltip>
            <Popconfirm
              title="确定要删除这个报告吗？"
              description={`报告名称：${record.name}`}
              onConfirm={() => deleteMutation.mutate(record.id)}
              okText="确定"
              cancelText="取消"
            >
              <Button
                type="text"
                danger
                size="small"
                aria-label="删除报告"
                icon={<DeleteOutlined />}
              />
            </Popconfirm>
          </div>
        )
      },
    },
  ]

  return (
    <div data-testid="report-list-page" className="olh-page-shell olh-console-page olh-report-list-page">
      <div className="olh-console-hero olh-report-hero">
        <div className="olh-console-hero-main">
          <div className="olh-page-breadcrumb">OpenLoadHub / Report Archive</div>
          <div className="olh-console-title-row">
            <h1 className="olh-page-title" data-testid="report-list-title">报告列表</h1>
            <span className={reportSummary.generating > 0 ? 'olh-live-pill olh-live-pill--active' : 'olh-live-pill'}>
              {reportSummary.generating > 0 ? 'GENERATING' : 'READY'}
            </span>
          </div>
          <div className="olh-page-subtitle">
            汇总压测报告、HTML 入口和下载状态；报告数据保持后端契约，只在前端收敛显示层级。
          </div>
          <div className="olh-console-command-strip">
            <span><FileTextOutlined /> {`报告 ${reportSummary.pageTotal} / ${reportSummary.total}`}</span>
            <span><CheckCircleOutlined /> {`已完成 ${reportSummary.completed}`}</span>
            <span><EyeOutlined /> {`HTML ${reportSummary.readyFiles}`}</span>
            <span><SyncOutlined /> {`生成中 ${reportSummary.generating}`}</span>
          </div>
        </div>
        <div className="olh-console-hero-side">
          <div className="olh-console-focus-panel">
            <div className="olh-console-focus-label">Report state</div>
            <div className="olh-console-focus-value">{reportSummary.readyFiles}</div>
            <div className="olh-console-focus-copy">当前页可直接打开 HTML 的报告数量</div>
            <div className="olh-console-focus-meta">
              <span>{`失败 ${reportSummary.failed}`}</span>
              <span>{`筛选 ${Object.keys(filterValues).length}`}</span>
            </div>
          </div>
          <Space wrap className="olh-console-actions">
            <Button onClick={() => navigate('/runs')}>结果列表</Button>
          </Space>
        </div>
      </div>

      {/* 搜索筛选 */}
      <Card className="olh-filter-panel olh-report-filter-panel" bodyStyle={{ padding: 16 }} data-testid="report-list-filter-card">
        <div className="olh-filter-panel-header">
          <div>
            <div className="olh-filter-panel-title">报告定位</div>
            <div className="olh-filter-panel-copy">按任务、状态和创建时间定位报告；HTML / 下载入口仍按后端 file_path 控制。</div>
          </div>
          <Space wrap size={8}>
            <Tag>{`总计 ${reportData?.total || 0}`}</Tag>
            <Tag color="green">{`可打开 ${reportSummary.readyFiles}`}</Tag>
          </Space>
        </div>
        <div className="olh-report-filter-console">
          <SearchFilter
            fields={filterFields}
            initialValues={initialValues}
            onSearch={values => {
              setFilterValues(values)
              setPagination({ current: 1, pageSize: pagination.pageSize })
            }}
            onReset={() => {
              setFilterValues({})
              setPagination({ current: 1, pageSize: pagination.pageSize })
            }}
            loading={isLoading}
          />
        </div>
      </Card>

      {/* 数据表格 */}
      <div className="olh-table-shell olh-report-table-shell">
        <div className="olh-data-section-heading">
          <div>
            <div className="olh-section-title">报告记录</div>
            <div className="olh-section-subtitle">
              保留报告详情、HTML 查看、下载和删除入口，操作列降为低噪声工具区。
            </div>
          </div>
          <Space wrap size={8}>
            <span className="olh-data-chip">{`当前页 ${reportSummary.pageTotal}`}</span>
            <span className="olh-data-chip">{`已完成 ${reportSummary.completed}`}</span>
          </Space>
        </div>
        <DataTable<Report>
          columns={columns}
          dataSource={currentReports}
          loading={isLoading}
          rowKey={record => record.id}
          size="small"
          className="olh-report-table"
          scroll={{ x: 1042 }}
          pagination={{
            current: pagination.current,
            pageSize: pagination.pageSize,
            total: reportData?.total || 0,
            showSizeChanger: true,
            showTotal: total => `共 ${total} 条`,
          }}
          onChange={(paginationInfo) => {
            if (
              paginationInfo.current !== pagination.current ||
              paginationInfo.pageSize !== pagination.pageSize
            ) {
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

export default ReportList
