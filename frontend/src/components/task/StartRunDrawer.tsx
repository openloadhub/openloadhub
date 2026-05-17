import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Input,
  InputNumber,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'

import { runApi } from '@/services/runApi'
import { taskApi, type Task, type TaskSummary, type UpdateTaskDto } from '@/services/taskApi'
import {
  canStartTask,
  evaluateDataAssetAllDistributionGuard,
  getTaskEffectiveStartStatus,
  getTaskEffectiveStatusLabel,
} from '@/utils/taskStartGuard'
import {
  buildParamRows,
  coerceParamRowValue,
  createParamRowId,
  extractDurationSeconds,
  extractIterations,
  extractPodCount,
  extractThreadCount,
  type ParamRow,
  type ParamType,
  type RunParamPrimitive,
  resolveInitialRunMode,
  serializeParamRow,
  supportsIterationRunMode,
  type RunMode,
} from '@/utils/taskRunParams'

const { Text } = Typography

type TaskStartable = Pick<
  Task,
  | 'id'
  | 'name'
  | 'status'
  | 'env'
  | 'engine_type'
  | 'thread_count'
  | 'duration'
  | 'last_run_status'
  | 'last_run_status_label'
  | 'data_distribution'
  | 'properties'
  | 'data_assets'
  | 'proto_assets'
>

interface StartRunDrawerProps {
  open: boolean
  task: TaskStartable | null
  onClose: () => void
  onCreated?: (runId: number) => void
}

const PARAM_SOURCE_META = {
  default_only: {
    tag: '默认参数',
    color: 'default',
    text: '当前仅使用任务默认参数',
  },
  last_run_preferred: {
    tag: '最近运行参数',
    color: 'blue',
    text: '最近一次运行参数优先，未命中部分回退默认参数',
  },
} as const

const HISTORICAL_APPROVAL_TASK_STATUSES = new Set([
  'pending_submit_approval',
  'awaiting_approval',
  'approval_rejected',
])

const buildReadinessMessage = (task?: TaskStartable | null) => {
  const effectiveStatus = getTaskEffectiveStartStatus(task)
  const effectiveStatusLabel = getTaskEffectiveStatusLabel(task)
  const isHistoricalApprovalStatus = Boolean(
    task?.status && HISTORICAL_APPROVAL_TASK_STATUSES.has(task.status)
  )

  if (!effectiveStatus || canStartTask(task)) {
    return {
      blockReason: '当前任务已经处于“可执行”状态，可以直接填写参数并启动压测。',
      matchedRule: '若本次参数仍沿用默认值或最近一次运行值，通常无需额外处理，可直接启动。',
      nextAction: '核对执行节点数、脚本变量和最近执行参数后，直接点击“确认启动”。',
    }
  }
  if (effectiveStatus === 'preparing' || effectiveStatus === 'running') {
    return {
      blockReason: `当前任务已有${effectiveStatusLabel}的压测，本次启动入口已锁定。`,
      matchedRule:
        '关注任务页会优先消费最近一次执行态；只要最近一次执行仍在准备中/运行中，就不能重复启动。',
      nextAction: '请先等待当前执行结束，或回到任务列表执行“停止”后，再重新启动。',
    }
  }
  return {
    blockReason: isHistoricalApprovalStatus
      ? `当前状态为 ${effectiveStatusLabel}，这是兼容状态，启动入口会被阻断。`
      : `当前状态为 ${effectiveStatusLabel}，启动入口会被阻断。`,
    matchedRule: isHistoricalApprovalStatus
      ? '当前平台已不再走审批主链；这类状态只表示任务还停留在兼容口径，需要重新准备执行。'
      : '通常表示脚本缺失，或任务刚修改后尚未重新准备执行。',
    nextAction: isHistoricalApprovalStatus
      ? '先回到任务列表或详情页执行“准备执行”，必要时补脚本或检查任务配置，再回到这里启动。'
      : '先回到任务列表或详情页执行“准备执行”，必要时补脚本或检查任务配置，再回到这里启动。',
  }
}

const resolveIdleCapacity = (summary?: TaskSummary): number => {
  const resourcePoolIdle = Number(summary?.resource_pool?.idle_total ?? Number.NaN)
  if (Number.isFinite(resourcePoolIdle)) {
    return Math.max(0, resourcePoolIdle)
  }
  const podCapacity = summary?.pod_capacity ?? []
  return podCapacity.reduce((sum, item) => sum + Number(item.available_pod_count || 0), 0)
}

const isRecord = (value: unknown): value is Record<string, unknown> => (
  typeof value === 'object' && value !== null && !Array.isArray(value)
)

const DATA_FILE_VARIABLE_KEYS = new Set(['data_file'])
const PROTO_FILE_VARIABLE_KEYS = new Set([
  'proto_file',
  'grpc_proto_file',
  'ptp_proto_file',
])

const normalizeRuntimeFileNames = (value: RunParamPrimitive): string[] => {
  if (typeof value !== 'string') {
    return []
  }
  const trimmed = value.trim()
  if (!trimmed) {
    return []
  }
  return trimmed
    .split(',')
    .map(item => item.trim().replace(/\\/g, '/'))
    .map(item => item.split('/').pop()?.trim() ?? '')
    .filter(Boolean)
}

const buildBoundFileNameSet = (
  assets: Array<{ file_name?: string | null }> | null | undefined,
) => new Set(
  (assets ?? [])
    .map(asset => asset.file_name?.trim())
    .filter((name): name is string => Boolean(name))
)

const validateRuntimeAssetFileReferences = (
  taskDetail: TaskStartable | null | undefined,
  rows: ParamRow[],
): string | null => {
  const dataFileNames = buildBoundFileNameSet(taskDetail?.data_assets)
  const protoFileNames = buildBoundFileNameSet(taskDetail?.proto_assets)

  for (const row of rows) {
    const variableName = row.name.trim()
    const normalizedKey = variableName.toLowerCase()
    const fileNames = normalizeRuntimeFileNames(serializeParamRow(row))
    if (fileNames.length === 0) {
      continue
    }
    if (
      DATA_FILE_VARIABLE_KEYS.has(normalizedKey) &&
      dataFileNames.size > 0 &&
      fileNames.some(fileName => !dataFileNames.has(fileName))
    ) {
      const missingFileName = fileNames.find(fileName => !dataFileNames.has(fileName))
      return `DATA_FILE=${missingFileName} 未匹配已绑定数据文件；已绑定：${Array.from(dataFileNames).join(', ')}`
    }
    if (
      PROTO_FILE_VARIABLE_KEYS.has(normalizedKey) &&
      protoFileNames.size > 0 &&
      fileNames.some(fileName => !protoFileNames.has(fileName))
    ) {
      const missingFileName = fileNames.find(fileName => !protoFileNames.has(fileName))
      return `${variableName}=${missingFileName} 未匹配已绑定 proto 文件；已绑定：${Array.from(protoFileNames).join(', ')}`
    }
  }
  return null
}

const buildTaskVariablePersistencePatch = (
  taskDetail: Task,
  rows: ParamRow[],
): UpdateTaskDto => {
  const properties = isRecord(taskDetail.properties) ? { ...taskDetail.properties } : {}
  const previousMeta = isRecord(properties.variable_meta)
    ? (properties.variable_meta as Record<string, unknown>)
    : {}

  const variables: Record<string, unknown> = {}
  const variableTypes: Record<string, ParamType> = {}
  const variableMeta: Record<string, unknown> = {}

  rows.forEach(row => {
    const name = row.name.trim()
    if (!name) {
      return
    }
    variables[name] = serializeParamRow(row)
    variableTypes[name] = row.type
    if (Object.prototype.hasOwnProperty.call(previousMeta, name)) {
      variableMeta[name] = previousMeta[name]
    }
  })

  if (Object.keys(variables).length > 0) {
    properties.variables = variables
    properties.variable_types = variableTypes
    if (Object.keys(variableMeta).length > 0) {
      properties.variable_meta = variableMeta
    } else {
      delete properties.variable_meta
    }
  } else {
    delete properties.variables
    delete properties.variable_types
    delete properties.variable_meta
  }

  return { properties }
}

const StartRunDrawer: React.FC<StartRunDrawerProps> = ({ open, task, onClose, onCreated }) => {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [podCount, setPodCount] = useState(1)
  const [threadCount, setThreadCount] = useState(1)
  const [runMode, setRunMode] = useState<RunMode>('script_default')
  const [durationSeconds, setDurationSeconds] = useState(60)
  const [iterations, setIterations] = useState(1)
  const [rows, setRows] = useState<ParamRow[]>([])
  const [rowsDirty, setRowsDirty] = useState(false)

  const canStart = canStartTask(task)
  const effectiveStatus = getTaskEffectiveStartStatus(task)
  const effectiveStatusLabel = getTaskEffectiveStatusLabel(task)
  const supportsIterationMode = supportsIterationRunMode(task?.engine_type)

  const {
    data,
    isLoading,
    isFetching: paramsIsFetching,
    error,
  } = useQuery({
    queryKey: ['task-last-run-params', task?.id],
    queryFn: () => taskApi.getTaskLastRunParams(task!.id),
    enabled: open && !!task,
    staleTime: 0,
  })

  const {
    data: summaryData,
    isFetching: summaryIsFetching,
    refetch: refetchSummary,
  } = useQuery({
    queryKey: ['task-start-capacity', task?.env],
    queryFn: () => taskApi.getTaskSummary({ env: task!.env }),
    enabled: open && !!task?.env,
    staleTime: 0,
  })

  const {
    data: taskDetailData,
    isFetching: taskDetailIsFetching,
    error: taskDetailError,
    refetch: refetchTaskDetail,
  } = useQuery({
    queryKey: ['task-start-detail', task?.id],
    queryFn: () => taskApi.getTaskDetail(task!.id),
    enabled: open && !!task?.id,
    staleTime: 0,
  })

  const defaultParams = useMemo(() => data?.default_run_params ?? {}, [data])

  const mergedParams = useMemo(
    () => ({
      ...defaultParams,
      ...(data?.last_run_params ?? {}),
    }),
    [data, defaultParams]
  )

  const paramSourceMeta = data?.param_source
    ? PARAM_SOURCE_META[data.param_source]
    : data?.last_run_params
      ? PARAM_SOURCE_META.last_run_preferred
      : PARAM_SOURCE_META.default_only
  const readinessMessage = buildReadinessMessage(task)
  const idleCapacity = useMemo(() => resolveIdleCapacity(summaryData), [summaryData])
  const insufficientCapacity = !summaryIsFetching && canStart && podCount > idleCapacity
  const taskForAssetGuard = taskDetailData ?? task
  const dataAssetDistributionGuard = useMemo(
    () => evaluateDataAssetAllDistributionGuard(taskForAssetGuard),
    [taskForAssetGuard]
  )
  const dataAssetDistributionBlocked = canStart && dataAssetDistributionGuard.blocked

  useEffect(() => {
    if (!open) {
      return
    }
    const initialMode = resolveInitialRunMode(task?.engine_type, mergedParams)
    setPodCount(extractPodCount(mergedParams))
    setThreadCount(extractThreadCount(mergedParams, task?.thread_count))
    setRunMode(initialMode)
    setDurationSeconds(extractDurationSeconds(mergedParams, task?.duration))
    setIterations(extractIterations(mergedParams))
    setRows(buildParamRows(mergedParams))
    setRowsDirty(false)
  }, [mergedParams, open, task])

  const createRunMutation = useMutation({
    mutationFn: async (latestTaskDetail?: Task | null) => {
      if (!task) {
        throw new Error('任务不存在')
      }
      if (!Number.isFinite(threadCount) || threadCount <= 0) {
        throw new Error('执行并发必须大于 0')
      }
      if (runMode === 'duration' && (!Number.isFinite(durationSeconds) || durationSeconds <= 0)) {
        throw new Error('执行时长必须大于 0')
      }
      if (runMode === 'iterations' && (!Number.isFinite(iterations) || iterations <= 0)) {
        throw new Error('执行次数必须大于 0')
      }
      const normalizedNames = rows.map(row => row.name.trim())
      if (normalizedNames.some(name => !name)) {
        throw new Error('脚本变量名称不能为空')
      }
      if (new Set(normalizedNames).size !== normalizedNames.length) {
        throw new Error('脚本变量名称不能重复')
      }

      const params: Record<string, unknown> = {
        pod_count: podCount,
        thread_count: threadCount,
        run_mode: runMode,
      }
      const controlProperties: Record<string, unknown> = {
        thread_count: String(threadCount),
        threads: String(threadCount),
        vus: String(threadCount),
      }
      if (runMode === 'duration') {
        params.duration = durationSeconds
        params.duration_seconds = durationSeconds
        controlProperties.duration = String(durationSeconds)
        controlProperties.duration_seconds = String(durationSeconds)
      }
      if (runMode === 'iterations') {
        params.iterations = iterations
        params.run_by = 'loops'
        controlProperties.iterations = String(iterations)
        controlProperties.request_count = String(iterations)
        controlProperties.loops = String(iterations)
      }
      rows.forEach(row => {
        const name = row.name.trim()
        if (!name) {
          return
        }
        if (runMode === 'iterations' && task?.engine_type === 'k6' && name === 'target_tps') {
          return
        }
        params[name] = serializeParamRow(row)
      })
      params.properties = controlProperties
      if (rowsDirty) {
        if (!latestTaskDetail) {
          throw new Error('任务详情未刷新，无法保存启动参数')
        }
        await taskApi.updateTask(
          task.id,
          buildTaskVariablePersistencePatch(latestTaskDetail, rows),
        )
      }
      return runApi.createRun(
        {
          task_id: task.id,
          params,
        },
        `task-${task.id}-${Date.now()}`
      )
    },
    onSuccess: run => {
      message.success('已创建执行，正在打开详情。')
      queryClient.invalidateQueries({ queryKey: ['runs'] })
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['task', task?.id] })
      queryClient.invalidateQueries({ queryKey: ['task-last-run-params', task?.id] })
      onCreated?.(run.run_id)
      onClose()
      navigate(`/runs/${run.run_id}`)
    },
    onError: (mutationError: unknown) => {
      message.error(mutationError instanceof Error ? mutationError.message : '创建执行失败')
    },
  })

  const handleConfirmStart = async () => {
    if (!task) {
      return
    }

    const latestSummary = await refetchSummary()
    if (latestSummary.error) {
      message.error(
        latestSummary.error instanceof Error ? latestSummary.error.message : '刷新可用执行节点失败'
      )
      return
    }

    const latestTaskDetail = await refetchTaskDetail()
    if (latestTaskDetail.error) {
      message.error(
        latestTaskDetail.error instanceof Error
          ? latestTaskDetail.error.message
          : '刷新任务资产失败'
      )
      return
    }
    const latestAssetGuard = evaluateDataAssetAllDistributionGuard(latestTaskDetail.data ?? task)
    if (latestAssetGuard.blocked) {
      message.error(latestAssetGuard.message ?? '当前数据下发方式存在风险，已阻断启动')
      return
    }

    const assetReferenceError = validateRuntimeAssetFileReferences(latestTaskDetail.data ?? task, rows)
    if (assetReferenceError) {
      message.error(assetReferenceError)
      return
    }

    const latestIdleCapacity = resolveIdleCapacity(latestSummary.data)
    if (podCount > latestIdleCapacity) {
      message.error(`执行节点不足：当前空闲 ${latestIdleCapacity} 个，申请 ${podCount} 个`)
      return
    }

    createRunMutation.mutate(latestTaskDetail.data ?? null)
  }

  const updateRow = (rowId: string, patch: Partial<ParamRow>) => {
    setRowsDirty(true)
    setRows(current => current.map(row => (row.row_id === rowId ? { ...row, ...patch } : row)))
  }

  const appendRow = () => {
    setRowsDirty(true)
    setRows(current => [
      ...current,
      {
        row_id: createParamRowId(),
        name: '',
        type: 'string',
        value: '',
      },
    ])
  }

  const removeRow = (rowId: string) => {
    setRowsDirty(true)
    setRows(current => current.filter(row => row.row_id !== rowId))
  }

  return (
    <Drawer
      title={task ? `启动压测 - ${task.name}` : '启动压测'}
      placement="right"
      width={640}
      open={open}
      rootClassName="olh-start-run-drawer-root"
      className="olh-start-run-drawer"
      onClose={onClose}
      destroyOnClose
      footer={
        <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
          <Button onClick={onClose}>取消</Button>
          <Button
            type="primary"
            disabled={
              !canStart ||
              insufficientCapacity ||
              dataAssetDistributionBlocked ||
              summaryIsFetching ||
              taskDetailIsFetching ||
              paramsIsFetching
            }
            loading={createRunMutation.isPending || summaryIsFetching || taskDetailIsFetching}
            onClick={handleConfirmStart}
          >
            确认启动
          </Button>
        </Space>
      }
    >
      {!canStart && task ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="当前任务状态不可直接启动"
          description={`当前状态为 ${effectiveStatusLabel}，请先在任务列表或详情页处理当前执行或重新准备任务，只有“可执行”状态才能从这里启动压测。`}
        />
      ) : null}

      {insufficientCapacity ? (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="执行节点不足"
          description={`当前空闲执行节点 ${idleCapacity} 个，申请 ${podCount} 个。请减少执行节点数，或等待资源释放后再启动。`}
        />
      ) : null}

      {dataAssetDistributionBlocked ? (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message={dataAssetDistributionGuard.message}
          description={dataAssetDistributionGuard.description}
        />
      ) : null}

      {taskDetailError ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="任务资产信息未刷新"
          description={
            taskDetailError instanceof Error
              ? taskDetailError.message
              : '启动前会再次刷新任务资产，避免漏过大文件 all 分发风险。'
          }
        />
      ) : null}

      {error ? (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="加载启动参数失败"
          description={error instanceof Error ? error.message : '请稍后重试'}
        />
      ) : null}

      <Card size="small" style={{ marginBottom: 16 }}>
        <Descriptions
          column={3}
          size="small"
          items={[
            { key: 'task_id', label: '任务ID', children: task?.id ?? '-' },
            {
              key: 'task_status',
              label: '任务状态',
              children: task ? (
                <Tag
                  color={
                    canStart
                      ? 'green'
                      : effectiveStatus === 'running' || effectiveStatus === 'preparing'
                        ? 'processing'
                        : 'default'
                  }
                >
                  {effectiveStatusLabel}
                </Tag>
              ) : (
                '-'
              ),
            },
            {
              key: 'param_source',
              label: '参数来源',
              children: (
                <Space size={8} wrap>
                  <Tag color={paramSourceMeta.color}>{paramSourceMeta.tag}</Tag>
                  <Text type="secondary">{paramSourceMeta.text}</Text>
                </Space>
              ),
            },
            {
              key: 'last_run',
              label: '最近执行',
              children: data?.last_run_id
                ? `#${data.last_run_id}${data.last_run_started_at ? ` · ${data.last_run_started_at.slice(0, 19).replace('T', ' ')}` : ''}`
                : '暂无历史执行',
            },
            {
              key: 'param_count',
              label: '参数数量',
              children: rows.length,
            },
            {
              key: 'run_entry',
              label: '启动入口',
              children: '任务页',
            },
          ]}
        />
      </Card>

      <Card size="small" style={{ marginBottom: 16 }}>
        <Space direction="vertical" size={12} style={{ display: 'flex' }}>
          <div>
            <Text strong>资源配置</Text>
            <Text type="secondary" style={{ display: 'block', marginTop: 4 }}>
              这里只配置本次要占用的执行节点数量。
            </Text>
          </div>
          <div>
            <Text type="secondary">执行节点 / Agent 数量</Text>
            <InputNumber
              min={1}
              precision={0}
              style={{ width: 160, display: 'block', marginTop: 6 }}
              value={podCount}
              onChange={value =>
                setPodCount(typeof value === 'number' && value > 0 ? value : 1)
              }
            />
          </div>
        </Space>
      </Card>

      <Card size="small" style={{ marginBottom: 16 }}>
        <Space direction="vertical" size={12} style={{ display: 'flex' }}>
          <div>
            <Text strong>执行策略</Text>
            <Text type="secondary" style={{ display: 'block', marginTop: 4 }}>
              这里统一控制并发、时长和次数，不再混入资源配置。
            </Text>
          </div>
          <Space size={16} wrap align="start">
            <div>
              <Text type="secondary">执行并发 / VUs</Text>
              <InputNumber
                min={1}
                precision={0}
                style={{ width: 160, display: 'block', marginTop: 6 }}
                value={threadCount}
                onChange={value =>
                  setThreadCount(typeof value === 'number' && value > 0 ? value : 1)
                }
              />
            </div>
            <div>
              <Text type="secondary">执行时长 / 次数策略</Text>
              <Select
                value={runMode}
                style={{ width: 180, display: 'block', marginTop: 6 }}
                onChange={value => setRunMode(value as RunMode)}
                options={[
                  { label: '按脚本默认', value: 'script_default' },
                  { label: '按时长', value: 'duration' },
                  ...(supportsIterationMode ? [{ label: '按次数', value: 'iterations' }] : []),
                ]}
              />
            </div>
            {runMode === 'duration' ? (
              <div>
                <Text type="secondary">执行时长（秒）</Text>
                <InputNumber
                  min={1}
                  precision={0}
                  style={{ width: 160, display: 'block', marginTop: 6 }}
                  value={durationSeconds}
                  onChange={value =>
                    setDurationSeconds(typeof value === 'number' && value > 0 ? value : 1)
                  }
                />
              </div>
            ) : null}
            {runMode === 'iterations' ? (
              <div>
                <Text type="secondary">执行次数</Text>
                <InputNumber
                  min={1}
                  precision={0}
                  style={{ width: 160, display: 'block', marginTop: 6 }}
                  value={iterations}
                  onChange={value =>
                    setIterations(typeof value === 'number' && value > 0 ? value : 1)
                  }
                />
                {task?.engine_type === 'jmeter' ? (
                  <Text type="secondary" style={{ display: 'block', marginTop: 6, maxWidth: 260 }}>
                    {`JMeter 当前会写成 LoopController.loops=${iterations}；总请求量通常约等于 ${iterations} × ${threadCount} 线程 × ${podCount} 个执行节点。`}
                  </Text>
                ) : null}
              </div>
            ) : null}
          </Space>
        </Space>
      </Card>

      <Card size="small" style={{ marginBottom: 16 }}>
        <Space direction="vertical" size={12} style={{ display: 'flex' }}>
          <div>
            <Text strong>运行参数</Text>
            <Text type="secondary" style={{ display: 'block', marginTop: 4 }}>
              这里只放脚本业务变量和 readiness 相关信息；上面的执行策略已经独立控制并发、时长和次数。
            </Text>
          </div>
          <Alert
            type="info"
            showIcon
            message="业务变量与 readiness"
            description={
              <Space direction="vertical" size={8} style={{ display: 'flex' }}>
                <span>1. 表格里只保留脚本业务变量，不再把执行节点 / Agent 数量混写进参数区。</span>
                <span>2. readiness 只负责说明当前是否可以启动，以及为什么会阻断。</span>
                <span>3. 若脚本未消费这些变量，平台仍按脚本默认逻辑执行。</span>
                <div>
                  <Button
                    type="link"
                    size="small"
                    style={{ paddingInline: 0 }}
                    onClick={() => navigate('/guides/k6-standard-scripts')}
                  >
                    查看使用指南
                  </Button>
                </div>
              </Space>
            }
          />
          <div>
            <Text strong>阻断原因</Text>
            <Text type="secondary" style={{ display: 'block', marginTop: 4 }}>
              {readinessMessage.blockReason}
            </Text>
          </div>
          <div>
            <Text strong>当前判断</Text>
            <Text type="secondary" style={{ display: 'block', marginTop: 4 }}>
              {readinessMessage.matchedRule}
            </Text>
          </div>
          <div>
            <Text strong>下一步动作</Text>
            <Text type="secondary" style={{ display: 'block', marginTop: 4 }}>
              {readinessMessage.nextAction}
            </Text>
          </div>
        </Space>
      </Card>

      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 12 }}>
        <Text strong>运行参数表</Text>
        <Button size="small" icon={<PlusOutlined />} onClick={appendRow}>
          新增变量
        </Button>
      </Space>

      <Table<ParamRow>
        rowKey="row_id"
        loading={isLoading}
        pagination={false}
        size="small"
        dataSource={rows}
        locale={{
          emptyText: (
            <Empty
              description="暂无可编辑参数，可直接确认启动，或手动新增脚本变量"
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            />
          ),
        }}
        columns={[
          {
            title: '参数名',
            dataIndex: 'name',
            key: 'name',
            width: 180,
            render: (value: string, record: ParamRow) => (
              <Input
                size="small"
                placeholder="例如 total_tps"
                value={value}
                onChange={event => updateRow(record.row_id, { name: event.target.value })}
              />
            ),
          },
          {
            title: '类型',
            dataIndex: 'type',
            key: 'type',
            width: 110,
            render: (value: ParamType, record: ParamRow) => (
              <Select
                size="small"
                value={value}
                style={{ width: '100%' }}
                options={[
                  { label: '整数', value: 'int' },
                  { label: '小数', value: 'float' },
                  { label: '布尔', value: 'bool' },
                  { label: '字符串', value: 'string' },
                ]}
                onChange={nextType =>
                  updateRow(record.row_id, {
                    type: nextType as ParamType,
                    value: coerceParamRowValue(record.value, nextType as ParamType),
                  })
                }
              />
            ),
          },
          {
            title: '值',
            dataIndex: 'value',
            key: 'value',
            render: (value: ParamRow['value'], record: ParamRow) => {
              if (record.type === 'bool') {
                return (
                  <Select
                    size="small"
                    value={String(value)}
                    style={{ width: '100%' }}
                    options={[
                      { label: 'true', value: 'true' },
                      { label: 'false', value: 'false' },
                    ]}
                    onChange={nextValue =>
                      updateRow(record.row_id, { value: nextValue === 'true' })
                    }
                  />
                )
              }
              if (record.type === 'int' || record.type === 'float') {
                return (
                  <InputNumber
                    size="small"
                    style={{ width: '100%' }}
                    value={typeof value === 'number' ? value : Number(value) || 0}
                    precision={record.type === 'float' ? 2 : 0}
                    onChange={nextValue =>
                      updateRow(record.row_id, {
                        value: typeof nextValue === 'number' ? nextValue : 0,
                      })
                    }
                  />
                )
              }
              return (
                <Input
                  size="small"
                  value={String(value ?? '')}
                  onChange={event => updateRow(record.row_id, { value: event.target.value })}
                />
              )
            },
          },
          {
            title: '操作',
            key: 'action',
            width: 88,
            render: (_value: unknown, record: ParamRow) => (
              <Button
                type="text"
                danger
                size="small"
                icon={<DeleteOutlined />}
                onClick={() => removeRow(record.row_id)}
              />
            ),
          },
        ]}
      />
    </Drawer>
  )
}

export default StartRunDrawer
