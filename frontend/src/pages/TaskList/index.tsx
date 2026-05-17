import { useCallback, useEffect, useMemo, useState, type Key } from 'react'
import { type Dayjs } from 'dayjs'
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Input,
  Popconfirm,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Tabs,
  Tooltip,
  Typography,
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { PlusOutlined, SearchOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import StartRunDrawer from '@/components/task/StartRunDrawer'
import TaskScriptEditorDrawer from '@/components/task/TaskScriptEditorDrawer'
import TaskVersionDrawer from '@/components/task/TaskVersionDrawer'
import { StatusBadge } from '@/components/common'
import { publicAlphaFeatures } from '@/config/publicAlpha'
import { metaApi, type MetaItem } from '@/services/metaApi'
import { runApi } from '@/services/runApi'
import { taskApi, type EngineType, type Task, type TaskQuery } from '@/services/taskApi'
import { useAuthStore } from '@/store/authStore'
import { resolveUserId } from '@/utils/auth'
import { formatLocalDateTime } from '@/utils/localDateTime'
import { canEnterStartFlow, canPrepareTaskForRun, canStartTask } from '@/utils/taskStartGuard'
import { getDemoScriptPreview } from '@/constants/demoScriptRegistry'

const { RangePicker } = DatePicker
const { Text, Title } = Typography

type DraftFilters = {
  task_id?: string
  name?: string
  app_name?: string
  env?: string
  engine_type?: EngineType
  business_line?: string
  status?: Task['status']
  participant_id?: string
  create_time?: [Dayjs, Dayjs]
  owner_scope: OwnerFilterScope
}

type QueryFilters = Omit<TaskQuery, 'page' | 'pageSize'> & {
  participant_id?: number
  business_line?: string
}

type SummaryQueryFilters = Pick<QueryFilters, 'env' | 'participant_id'>

type TaskScope = 'test' | 'testnet' | 'main'
type OwnerFilterScope = 'mine' | 'all' | 'specific'

const initialDraftFilters = (): DraftFilters => ({
  owner_scope: 'mine',
})

const normalizeScope = (scope?: TaskScope): TaskScope => scope ?? 'test'

const matchScopeEnvironment = (item: MetaItem, scope: TaskScope): boolean => {
  return normalizeScope(item.scope) === scope
}

const TASK_STATUS_OPTIONS = [
  { label: '草稿', value: 'draft' },
  { label: '可执行', value: 'ready' },
  { label: '脚本缺失', value: 'script_missing' },
  { label: '执行中', value: 'running' },
] satisfies Array<{ label: string; value: Task['status'] }>

const ENGINE_OPTIONS = [
  { label: 'JMeter', value: 'jmeter' satisfies EngineType },
  { label: 'K6', value: 'k6' satisfies EngineType },
  { label: 'Custom', value: 'custom' satisfies EngineType },
]

const OWNER_SCOPE_OPTIONS = [
  { label: '我的参与任务', value: 'mine' satisfies OwnerFilterScope },
  { label: '全部任务', value: 'all' satisfies OwnerFilterScope },
  { label: '指定成员', value: 'specific' satisfies OwnerFilterScope },
]

const DEMO_TEMPLATE_ITEMS = ['HTTP', 'GRPC', '混合标准']

const toPositiveInt = (value?: string): number | undefined => {
  if (!value) {
    return undefined
  }
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined
}

const renderCollaborators = (names?: string[] | null, ids?: number[] | null): string => {
  if (Array.isArray(names) && names.length > 0) {
    return names.join(', ')
  }
  if (!Array.isArray(ids) || ids.length === 0) {
    return '-'
  }
  return ids.map(item => `user#${item}`).join(', ')
}

const renderBusinessLine = (task: Task, businessOptions: MetaItem[]): string => {
  const raw =
    task.properties && typeof task.properties === 'object'
      ? (task.properties as Record<string, unknown>).business_line
      : null
  if (typeof raw !== 'string' || !raw) {
    return '-'
  }
  const matched = businessOptions.find(item => item.code === raw || item.name === raw)
  return matched?.name || raw
}

const renderRelatedApps = (properties?: Record<string, unknown> | null): string => {
  if (!properties || typeof properties !== 'object') {
    return '-'
  }
  const raw =
    properties.related_apps ??
    properties.related_apps ??
    properties.project_name ??
    properties.app_name
  if (Array.isArray(raw)) {
    const values = raw.filter(item => typeof item === 'string' && item).join(', ')
    return values || '-'
  }
  if (typeof raw === 'string' && raw.trim()) {
    return raw
  }
  return '-'
}

const buildTaskSecondarySummary = (task: Task, businessOptions: MetaItem[]): string => {
  const segments = [
    task.env,
    task.created_by_name || (task.created_by ? `user#${task.created_by}` : '-'),
    renderCollaborators(task.collaborator_names, task.collaborator_ids),
    renderBusinessLine(task, businessOptions),
  ]
  return segments.join(' · ')
}

const buildTrendPath = (taskId: number): string => `/trend-analysis?task_id=${taskId}&auto_open=1`
const buildScriptEditPath = (taskId: number): string => `/tasks/${taskId}/edit?focus=script`

const buildQueryFilters = (draft: DraftFilters, currentUserId?: number): QueryFilters | null => {
  if (!draft.env) {
    message.warning('请先选择压测环境')
    return null
  }

  const ownerScope = draft.owner_scope ?? 'mine'
  const participantId =
    ownerScope === 'mine'
      ? currentUserId
      : ownerScope === 'specific'
        ? toPositiveInt(draft.participant_id)
        : undefined
  if (ownerScope === 'mine' && !participantId) {
    message.warning('未识别当前登录用户，无法按“我的参与任务”查询')
    return null
  }
  if (ownerScope === 'specific' && !participantId) {
    message.warning('选择指定成员后，请填写创建人/协作人 ID')
    return null
  }

  const taskId = toPositiveInt(draft.task_id)
  return {
    task_id: taskId,
    name: draft.name?.trim() || undefined,
    app_name: draft.app_name?.trim() || undefined,
    env: draft.env,
    engine_type: draft.engine_type,
    business_line: draft.business_line || undefined,
    status: draft.status,
    participant_id: participantId,
    from: draft.create_time?.[0]?.toDate().toISOString(),
    to: draft.create_time?.[1]?.toDate().toISOString(),
  }
}

const TaskList = () => {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { token, user } = useAuthStore()
  const currentUserId = useMemo(() => resolveUserId(token, user), [token, user])

  const [draftFilters, setDraftFilters] = useState<DraftFilters>(() => initialDraftFilters())
  const [queryFilters, setQueryFilters] = useState<QueryFilters | null>(null)
  const [pagination, setPagination] = useState({ current: 1, pageSize: 10 })
  const [selectedRowKeys, setSelectedRowKeys] = useState<Key[]>([])
  const [startingTask, setStartingTask] = useState<Task | null>(null)
  const [startFlowTaskId, setStartFlowTaskId] = useState<number | null>(null)
  const [versionTask, setVersionTask] = useState<Task | null>(null)
  const [demoPreviewKey, setDemoPreviewKey] = useState<string | null>(null)
  const [envOptions, setEnvOptions] = useState<MetaItem[]>([])
  const [businessOptions, setBusinessOptions] = useState<MetaItem[]>([])
  const [scope, setScope] = useState<TaskScope>('test')
  const ownerFilterScope: OwnerFilterScope = draftFilters.owner_scope ?? 'mine'

  useEffect(() => {
    metaApi
      .getEnvironments()
      .then(setEnvOptions)
      .catch(() => setEnvOptions([]))
    metaApi
      .getBusinessLines()
      .then(setBusinessOptions)
      .catch(() => setBusinessOptions([]))
  }, [])

  const scopedEnvOptions = useMemo(
    () => envOptions.filter(item => matchScopeEnvironment(item, scope)),
    [envOptions, scope]
  )

  useEffect(() => {
    const nextEnvOptions = scopedEnvOptions
    if (!draftFilters.env) {
      return
    }
    if (nextEnvOptions.some(item => item.code === draftFilters.env)) {
      return
    }
    setDraftFilters(prev => ({
      ...prev,
      env: undefined,
    }))
    setQueryFilters(null)
    setSelectedRowKeys([])
  }, [draftFilters.env, scopedEnvOptions])

  const taskQueryParams = useMemo<TaskQuery | null>(() => {
    if (!queryFilters) {
      return null
    }
    return {
      ...queryFilters,
      page: pagination.current,
      pageSize: pagination.pageSize,
    }
  }, [pagination, queryFilters])

  const summaryQueryParams = useMemo<SummaryQueryFilters | null>(() => {
    if (!queryFilters) {
      return null
    }
    return {
      env: queryFilters.env,
      participant_id: queryFilters.participant_id,
    }
  }, [queryFilters])

  const { data: taskData, isLoading } = useQuery({
    queryKey: ['tasks', taskQueryParams],
    queryFn: () => taskApi.getTaskList(taskQueryParams as TaskQuery),
    enabled: !!taskQueryParams,
  })

  const { data: summaryData, isLoading: summaryLoading } = useQuery({
    queryKey: ['tasks-summary', summaryQueryParams],
    queryFn: () => taskApi.getTaskSummary(summaryQueryParams as SummaryQueryFilters),
    enabled: !!summaryQueryParams,
  })
  const visibleTaskItems = queryFilters ? (taskData?.items ?? []) : []
  const visibleTaskTotal = queryFilters ? (taskData?.total ?? 0) : 0
  const visibleSummary = summaryQueryParams ? summaryData : undefined

  const deleteMutation = useMutation({
    mutationFn: (id: number) => taskApi.deleteTask(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['tasks-summary'] })
    },
  })

  const batchStopMutation = useMutation({
    mutationFn: (taskIds: number[]) =>
      taskApi.batchStopTasks(taskIds, 'stopped_from_tasks_list_batch'),
    onSuccess: result => {
      if (result.stopped_run_ids.length > 0) {
        message.success(`已停止 ${result.stopped_run_ids.length} 个运行中的压测`)
      } else if (result.skipped_task_ids.length > 0) {
        message.warning('选中的任务里没有运行中的压测')
      } else {
        message.success('批量终止请求已提交')
      }
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['tasks-summary'] })
      queryClient.invalidateQueries({ queryKey: ['runs'] })
      setSelectedRowKeys([])
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '批量终止失败')
    },
  })

  const stopRunMutation = useMutation({
    mutationFn: async (taskId: number) => {
      const running = await runApi.getRunList({
        page: 1,
        pageSize: 1,
        task_id: taskId,
        run_status: 'running',
      })
      const currentRun = running.items[0]
      if (!currentRun) {
        return null
      }
      return runApi.stopRun(currentRun.run_id, { reason: 'stopped_from_tasks_list' })
    },
    onSuccess: result => {
      if (!result) {
        message.warning('当前任务没有运行中的执行记录')
        return
      }
      message.success('已停止压测')
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['tasks-summary'] })
      queryClient.invalidateQueries({ queryKey: ['runs'] })
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '停止压测失败')
    },
  })

  const prepareTaskMutation = useMutation({
    mutationFn: (taskInfo: Task) => taskApi.prepareTaskForRun(taskInfo.id),
    onSuccess: (payload, taskInfo) => {
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
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

  const copyTaskMutation = useMutation({
    mutationFn: (taskId: number) => taskApi.copyTask(taskId),
    onSuccess: copied => {
      message.success(`已复制任务，当前副本：${copied.name}`)
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['tasks-summary'] })
      navigate(`/tasks/${copied.id}/edit`)
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '复制任务失败')
    },
  })

  const handleSearch = () => {
    const nextQueryFilters = buildQueryFilters(draftFilters, currentUserId)
    if (!nextQueryFilters) {
      return
    }
    setSelectedRowKeys([])
    setPagination(prev => ({ ...prev, current: 1 }))
    setQueryFilters(nextQueryFilters)
  }

  const handleReset = () => {
    setDraftFilters(initialDraftFilters())
    setQueryFilters(null)
    setSelectedRowKeys([])
    setPagination({ current: 1, pageSize: 10 })
  }

  const handleSingleDelete = useCallback(
    async (task: Task) => {
      try {
        await deleteMutation.mutateAsync(task.id)
        message.success(`已删除任务 ${task.name}`)
        setSelectedRowKeys(keys => keys.filter(key => Number(key) !== task.id))
      } catch (error) {
        message.error(error instanceof Error ? error.message : '任务删除失败')
      }
    },
    [deleteMutation]
  )

  const handleBatchDelete = async () => {
    if (selectedRowKeys.length === 0) {
      return
    }
    try {
      await Promise.all(selectedRowKeys.map(key => deleteMutation.mutateAsync(Number(key))))
      message.success(`已删除 ${selectedRowKeys.length} 个任务`)
      setSelectedRowKeys([])
    } catch (error) {
      message.error(error instanceof Error ? error.message : '批量删除失败')
    }
  }

  const handleOpenTemplate = (template: string) => {
    setDemoPreviewKey(template)
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

  const columns = useMemo<ColumnsType<Task>>(
    () => [
      {
        title: '任务ID',
        dataIndex: 'id',
        key: 'id',
        width: 84,
      },
      {
        title: '任务',
        dataIndex: 'name',
        key: 'name',
        width: 260,
        render: (value: string, record) => (
          <Space direction="vertical" size={2}>
            <Text strong>{value}</Text>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {buildTaskSecondarySummary(record, businessOptions)}
            </Text>
          </Space>
        ),
      },
      {
        title: '环境',
        dataIndex: 'env',
        key: 'env',
        width: 110,
        render: (value: string) => value || '-',
      },
      {
        title: '状态',
        dataIndex: 'status',
        key: 'status',
        width: 110,
        render: (value: Task['status']) => <StatusBadge status={value} />,
      },
      {
        title: '引擎类型',
        dataIndex: 'engine_type',
        key: 'engine_type',
        width: 90,
        render: (value: Task['engine_type']) => value?.toUpperCase() || '-',
      },
      {
        title: '任务模式',
        dataIndex: 'task_pattern',
        key: 'task_pattern',
        width: 100,
        render: (value: Task['task_pattern']) => (
          <Tag color={value === 'script' ? 'blue' : 'purple'} style={{ margin: 0 }}>
            {value === 'script' ? '脚本' : '可视化'}
          </Tag>
        ),
      },
      {
        title: '协议类型',
        dataIndex: 'protocols',
        key: 'protocols',
        width: 120,
        render: (value: Task['protocols']) =>
          Array.isArray(value) && value.length > 0 ? value.join(', ').toUpperCase() : '-',
      },
      {
        title: '关联应用',
        dataIndex: 'properties',
        key: 'related_apps',
        width: 150,
        render: (value: Task['properties']) => renderRelatedApps(value),
      },
      {
        title: '创建人',
        dataIndex: 'created_by_name',
        key: 'created_by_name',
        width: 140,
        render: (_value: string | null | undefined, record) =>
          record.created_by_name || (record.created_by ? `user#${record.created_by}` : '-'),
      },
      {
        title: '协作人',
        key: 'collaborators',
        width: 180,
        render: (_value: unknown, record) =>
          renderCollaborators(record.collaborator_names, record.collaborator_ids),
      },
      {
        title: '业务线',
        key: 'business_line',
        width: 140,
        render: (_value: unknown, record) => renderBusinessLine(record, businessOptions),
      },
      {
        title: '创建时间',
        dataIndex: 'created_at',
        key: 'created_at',
        width: 180,
        render: (value: string | null | undefined) => formatLocalDateTime(value),
      },
      {
        title: '更新时间',
        dataIndex: 'updated_at',
        key: 'updated_at',
        width: 180,
        render: (value: string | null | undefined) => formatLocalDateTime(value),
      },
      {
        title: '操作',
        key: 'actions',
        fixed: 'right',
        width: 520,
        render: (_value, record) => (
          <Space size={4} wrap>
            <Button
              type="link"
              size="small"
              disabled={!canEnterStartFlow(record)}
              loading={startFlowTaskId === record.id && prepareTaskMutation.isPending}
              onClick={() => handleStartTask(record)}
            >
              启动
            </Button>
            <Button
              type="link"
              size="small"
              loading={stopRunMutation.isPending}
              onClick={() => stopRunMutation.mutate(record.id)}
            >
              停止
            </Button>
            <Button type="link" size="small" onClick={() => navigate(`/tasks/${record.id}`)}>
              详情
            </Button>
            {publicAlphaFeatures.trendAnalysis ? (
              <Button type="link" size="small" onClick={() => navigate(buildTrendPath(record.id))}>
                趋势分析
              </Button>
            ) : null}
            <Button type="link" size="small" onClick={() => navigate(`/runs?task_id=${record.id}`)}>
              结果列表
            </Button>
            <Button type="link" size="small" onClick={() => navigate(`/tasks/${record.id}/edit`)}>
              编辑
            </Button>
            <Button
              type="link"
              size="small"
              onClick={() => navigate(buildScriptEditPath(record.id))}
            >
              脚本编辑
            </Button>
            <Button type="link" size="small" onClick={() => setVersionTask(record)}>
              版本对比
            </Button>
            <Tooltip title="复制后会基于原任务生成一个新副本，并跳转到副本编辑页。">
              <Button
                type="link"
                size="small"
                loading={copyTaskMutation.isPending}
                onClick={() => copyTaskMutation.mutate(record.id)}
              >
                复制
              </Button>
            </Tooltip>
            <Popconfirm
              title="确定要删除这个任务吗？"
              description={`任务名称：${record.name}`}
              okText="确定"
              cancelText="取消"
              onConfirm={() => handleSingleDelete(record)}
            >
              <Button type="link" danger size="small">
                删除
              </Button>
            </Popconfirm>
          </Space>
        ),
      },
    ],
    [
      businessOptions,
      copyTaskMutation,
      handleSingleDelete,
      handleStartTask,
      navigate,
      prepareTaskMutation.isPending,
      startFlowTaskId,
      stopRunMutation,
    ]
  )

  return (
    <Space direction="vertical" size={16} style={{ display: 'flex' }}>
      <Card size="small">
        <Row justify="space-between" align="middle" gutter={[16, 16]}>
          <Col>
            <Space direction="vertical" size={4}>
              <Title level={3} style={{ margin: 0 }}>
                任务列表
              </Title>
              <Text type="secondary">
                筛选任务、启动压测，并进入详情、结果列表、脚本编辑与版本对比。
              </Text>
            </Space>
          </Col>
          <Col>
            <Space size={24} wrap>
              <Statistic
                title="压测任务总数"
                value={visibleSummary?.task_total ?? 0}
                loading={summaryLoading}
              />
              <Statistic
                title="执行成功"
                value={visibleSummary?.task_success_total ?? 0}
                loading={summaryLoading}
                valueStyle={{ fontSize: 18, color: '#237804' }}
              />
              <Statistic
                title="执行失败"
                value={visibleSummary?.task_failed_total ?? 0}
                loading={summaryLoading}
                valueStyle={{ fontSize: 18, color: '#cf1322' }}
              />
              <Statistic
                title="报告总数"
                value={visibleSummary?.report_total ?? 0}
                loading={summaryLoading}
              />
            </Space>
          </Col>
        </Row>
        <div style={{ marginTop: 12 }}>
          {visibleSummary?.pod_capacity?.length ? (
            <Space wrap size={[12, 8]}>
              {visibleSummary.pod_capacity.map(item => (
                <Tag
                  key={item.cluster_code}
                >{`${item.cluster_name} ${item.available_pod_count}`}</Tag>
              ))}
            </Space>
          ) : (
            <Text type="secondary">选择环境后查看执行节点实时余量</Text>
          )}
        </div>
      </Card>

      <Card size="small">
        <Space direction="vertical" size={16} style={{ display: 'flex' }}>
          <Tabs
            size="small"
            activeKey={scope}
            onChange={value => {
              setScope(value as TaskScope)
              setQueryFilters(null)
              setSelectedRowKeys([])
            }}
            items={[
              { key: 'test', label: '测试任务' },
              { key: 'testnet', label: 'TestNet任务' },
              { key: 'main', label: '主网任务' },
            ]}
          />
          <Row gutter={[12, 12]}>
            <Col xs={24} md={5}>
              <Text type="secondary">任务ID</Text>
              <Input
                size="small"
                placeholder="请输入任务ID"
                value={draftFilters.task_id}
                onChange={event =>
                  setDraftFilters(prev => ({ ...prev, task_id: event.target.value }))
                }
              />
            </Col>
            <Col xs={24} md={5}>
              <Text type="secondary">任务名称</Text>
              <Input
                size="small"
                placeholder="请输入任务名称"
                value={draftFilters.name}
                onChange={event => setDraftFilters(prev => ({ ...prev, name: event.target.value }))}
              />
            </Col>
            <Col xs={24} md={4}>
              <Text type="secondary">应用名</Text>
              <Input
                size="small"
                placeholder="请输入应用名"
                value={draftFilters.app_name}
                onChange={event =>
                  setDraftFilters(prev => ({ ...prev, app_name: event.target.value }))
                }
              />
            </Col>
            <Col xs={24} md={4}>
              <Text type="secondary">业务线</Text>
              <Select
                size="small"
                allowClear
                style={{ width: '100%' }}
                placeholder="请选择业务线"
                value={draftFilters.business_line}
                options={businessOptions.map(item => ({
                  label: item.name || item.code,
                  value: item.code,
                }))}
                onChange={value => setDraftFilters(prev => ({ ...prev, business_line: value }))}
              />
            </Col>
            <Col xs={24} md={4}>
              <Text type="secondary">引擎类型</Text>
              <Select
                size="small"
                allowClear
                style={{ width: '100%' }}
                placeholder="请选择引擎"
                value={draftFilters.engine_type}
                options={ENGINE_OPTIONS}
                onChange={value => setDraftFilters(prev => ({ ...prev, engine_type: value }))}
              />
            </Col>
            <Col xs={24} md={4}>
              <Text type="secondary">状态</Text>
              <Select
                size="small"
                allowClear
                style={{ width: '100%' }}
                placeholder="请选择状态"
                value={draftFilters.status}
                options={TASK_STATUS_OPTIONS}
                onChange={value => setDraftFilters(prev => ({ ...prev, status: value }))}
              />
            </Col>
            <Col xs={24} md={4}>
              <Text type="secondary">压测环境</Text>
              <Select
                size="small"
                style={{ width: '100%' }}
                placeholder={
                  scope === 'test'
                    ? '请选择测试环境'
                    : scope === 'testnet'
                      ? '请选择 TestNet 环境'
                      : '请选择主网环境'
                }
                value={draftFilters.env}
                options={scopedEnvOptions.map(item => ({
                  label: item.name || item.code,
                  value: item.code,
                }))}
                onChange={value => setDraftFilters(prev => ({ ...prev, env: value }))}
              />
            </Col>
            <Col xs={24} md={4}>
              <Text type="secondary">创建时间</Text>
              <RangePicker
                size="small"
                style={{ width: '100%' }}
                value={draftFilters.create_time}
                onChange={value =>
                  setDraftFilters(prev => ({
                    ...prev,
                    create_time: value as [Dayjs, Dayjs] | undefined,
                  }))
                }
              />
            </Col>
            <Col xs={24} md={4}>
              <Text type="secondary">创建人 / 协作人</Text>
              <Input
                size="small"
                disabled={ownerFilterScope !== 'specific'}
                placeholder={ownerFilterScope === 'specific' ? '请输入用户ID' : '当前范围不需要用户ID'}
                value={draftFilters.participant_id}
                onChange={event =>
                  setDraftFilters(prev => ({ ...prev, participant_id: event.target.value }))
                }
              />
            </Col>
          </Row>

          <Row justify="space-between" align="middle" gutter={[16, 12]}>
            <Col xs={24} md={12}>
              <Space wrap>
                <Select<OwnerFilterScope>
                  size="small"
                  value={ownerFilterScope}
                  style={{ width: 150 }}
                  options={OWNER_SCOPE_OPTIONS}
                  aria-label="任务归属范围"
                  onChange={value => {
                    setDraftFilters(prev => ({
                      ...prev,
                      owner_scope: value,
                      participant_id: value === 'specific' ? prev.participant_id : undefined,
                    }))
                  }}
                />
                <Text type="secondary">
                  {ownerFilterScope === 'mine'
                    ? '按我创建或协作的任务查询'
                    : ownerFilterScope === 'specific'
                      ? '需填写创建人 / 协作人 ID'
                      : '查看当前环境范围内全部任务'}
                </Text>
                <Tag color="processing">
                  {scope === 'test'
                    ? '当前测试任务环境'
                    : scope === 'testnet'
                      ? '当前 TestNet 环境'
                      : '当前主网环境'}
                </Tag>
              </Space>
            </Col>
            <Col xs={24} md={12}>
              <Space wrap style={{ width: '100%', justifyContent: 'flex-end' }}>
                <Button icon={<SearchOutlined />} type="primary" onClick={handleSearch}>
                  查询
                </Button>
                <Button onClick={handleReset}>重置</Button>
                <Button
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={() => navigate('/tasks/new')}
                >
                  创建压测脚本
                </Button>
              </Space>
            </Col>
          </Row>

          <Alert
            type="info"
            showIcon
            message="k6 标准脚本模板"
            description={
              <Space wrap size={[8, 8]}>
                <Button
                  type="link"
                  size="small"
                  style={{ paddingInline: 0 }}
                  onClick={() => navigate('/guides/k6-standard-scripts')}
                >
                  使用指南
                </Button>
                {DEMO_TEMPLATE_ITEMS.map(item => (
                  <Button key={item} size="small" onClick={() => handleOpenTemplate(item)}>
                    {item}
                  </Button>
                ))}
              </Space>
            }
          />
        </Space>
      </Card>

      {!queryFilters && (
        <Alert
          type="info"
          showIcon
          message="先选择压测环境，再选择归属范围或指定创建人 / 协作人进行查询。"
        />
      )}

      {selectedRowKeys.length > 0 && (
        <Card size="small">
          <Space wrap>
            <Text>已选择 {selectedRowKeys.length} 项</Text>
            <Popconfirm
              title="确定要批量终止所选任务吗？"
              description="仅会停止运行中的压测，不会删除任务本身"
              okText="确定"
              cancelText="取消"
              onConfirm={() => batchStopMutation.mutate(selectedRowKeys.map(key => Number(key)))}
            >
              <Button loading={batchStopMutation.isPending}>批量终止</Button>
            </Popconfirm>
            <Popconfirm
              title="确定要删除选中的任务吗？"
              description="该操作不可恢复"
              okText="确定"
              cancelText="取消"
              onConfirm={handleBatchDelete}
            >
              <Button danger loading={deleteMutation.isPending}>
                批量删除
              </Button>
            </Popconfirm>
            <Button onClick={() => setSelectedRowKeys([])}>取消选择</Button>
          </Space>
        </Card>
      )}

      <Card size="small" bodyStyle={{ padding: 0 }}>
        <Table<Task>
          rowKey="id"
          columns={columns}
          loading={isLoading}
          dataSource={visibleTaskItems}
          rowSelection={{
            selectedRowKeys,
            onChange: setSelectedRowKeys,
          }}
          scroll={{ x: 2200 }}
          pagination={{
            current: pagination.current,
            pageSize: pagination.pageSize,
            total: visibleTaskTotal,
            showSizeChanger: true,
            showTotal: total => `共 ${total} 条`,
            onChange: (page, pageSize) => setPagination({ current: page, pageSize }),
          }}
          locale={{
            emptyText: queryFilters ? '当前筛选条件下没有任务' : '请选择环境并执行查询',
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
      />

      <TaskVersionDrawer
        open={!!versionTask}
        task={versionTask}
        onClose={() => setVersionTask(null)}
      />

      <TaskScriptEditorDrawer
        open={!!demoPreviewKey}
        previewVersion={getDemoScriptPreview(demoPreviewKey || '')?.preview || null}
        onClose={() => setDemoPreviewKey(null)}
      />
    </Space>
  )
}

export default TaskList
