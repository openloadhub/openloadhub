import { useDeferredValue, useEffect, useMemo, useState } from 'react'
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
  MinusCircleOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  DatePicker,
  Divider,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  message,
  Modal,
  Radio,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from 'antd'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import dayjs from 'dayjs'
import type { Dayjs } from 'dayjs'

import {
  planApi,
  type PlanCreateRequest,
  type PlanExecType,
  type Plan,
  type PlanStage,
  type PlanStageItem,
  type PlanStatus,
} from '@/services/planApi'
import { taskApi, type Task } from '@/services/taskApi'
import { metaApi, type MetaItem } from '@/services/metaApi'
import {
  buildParamRows,
  buildTaskFallbackRunParams,
  clearEditableScriptVariableParams,
  clearManagedRunControlParams,
  clearRunModeControlParams,
  coerceParamRowValue,
  createParamRowId,
  extractDurationSeconds,
  extractIterations,
  extractPodCount,
  extractThreadCount,
  hasRunModeControlParams,
  normalizeCompatibleRunParams,
  type ParamRow,
  type ParamType,
  resolveInitialRunMode,
  serializeParamRow,
  supportsIterationRunMode,
  type RunMode,
} from '@/utils/taskRunParams'

const { TextArea } = Input
const { Text } = Typography

const EXEC_TYPE_OPTIONS = [
  {
    label: '手动启动',
    value: 'manual' satisfies PlanExecType,
    hint: '由用户在列表页或详情页手动触发。',
  },
  {
    label: '周期巡检',
    value: 'cron' satisfies PlanExecType,
    hint: '按 CRON 周期自动执行，适合固定回归。',
  },
  { label: '单次定时', value: 'fixed' satisfies PlanExecType, hint: '只在指定时间执行一次。' },
]

type PlanComposerMode = 'simple' | 'advanced'
type SimpleRampMode = 'concurrency' | 'target_tps'

const COMPOSER_MODE_OPTIONS = [
  { label: '简版配置', value: 'simple' satisfies PlanComposerMode },
  { label: '高级编排', value: 'advanced' satisfies PlanComposerMode },
]

const SIMPLE_RAMP_MODE_OPTIONS = [
  { label: '按并发递增', value: 'concurrency' satisfies SimpleRampMode },
  { label: '按目标 TPS 递增', value: 'target_tps' satisfies SimpleRampMode },
]

const SIMPLE_DEFAULT_DURATION_SECONDS = 300
const SIMPLE_DEFAULT_INTERVAL_SECONDS = 240
const SIMPLE_DEFAULT_POD_COUNT = 1
const SIMPLE_DEFAULT_CAPACITY_VUS = 200
const SIMPLE_TARGET_TPS_ENGINE_TYPES = new Set(['k6', 'jmeter'])
const ACTIVE_PLAN_RUN_STATUSES = new Set(['preparing', 'running'])
const MATRIX_BORDER_BOTTOM = '1px solid var(--border-subtle)'

class PlanSubmitError extends Error {}

const isFormValidationError = (error: unknown) =>
  Boolean(error && typeof error === 'object' && 'errorFields' in error)

type FormValues = {
  name: string
  description?: string
  status: PlanStatus
  exec_type: PlanExecType
  cron?: string
  timezone?: string
  scheduled_at?: Dayjs
  composer_mode?: PlanComposerMode
  enable_round?: boolean
  total_round?: number
  simple_task_id?: number
  simple_ramp_type?: SimpleRampMode
  simple_gradient_values?: string
  simple_duration_seconds?: number
  simple_interval_seconds?: number
  simple_pod_count?: number
  simple_capacity_vus?: number
  business_lines?: string[]
  collaborator_ids?: string[]
}

type SimplePreviewRow = {
  key: string
  round: number
  rampValue: number
  durationSeconds: number
  intervalSeconds: number
  podCount: number
  capacityVus?: number | null
}

type SimplePlanPreset = {
  taskId: number
  rampMode: SimpleRampMode
  gradientValues: number[]
  durationSeconds: number
  intervalSeconds: number
  podCount: number
  capacityVus: number
}

const isZeroSleepOverride = (postParams?: Record<string, unknown> | null): boolean => {
  if (!postParams || !isSleepPostParams(postParams)) {
    return false
  }
  return resolveSleepSeconds(postParams) === 0
}

type StageItemDraftType = 'task' | 'postprocessor'

type StageItemRoundOverrideDraft = {
  round: number
  run_params_json: string
  post_params_json: string
  post_template: 'sleep' | 'custom'
  post_sleep_seconds?: number
}

type StageItemDraft = {
  type: StageItemDraftType
  task_id?: number
  run_params_json: string
  post_params_json: string
  round_overrides: StageItemRoundOverrideDraft[]
  post_template: 'sleep' | 'custom'
  post_sleep_seconds?: number
  retry_count: number
  retry_delay_seconds: number
  on_failure: 'ABORT' | 'SKIP' | 'CONTINUE'
}

type StageDraft = {
  items: StageItemDraft[]
}

type ItemLocator = {
  stageIndex: number
  itemIndex: number
  roundNumber?: number
}

type TaskParamSourceKey = 'default_only' | 'last_run_preferred' | 'plan_override_only'

type TaskParamDraft = {
  podCount: number
  threadCount: number
  runMode: RunMode
  durationSeconds: number
  iterations: number
  preservedParams: Record<string, unknown>
  rows: ParamRow[]
  sourceKey: TaskParamSourceKey
}

const createEmptyTaskItem = (): StageItemDraft => ({
  type: 'task',
  task_id: undefined,
  run_params_json: '',
  post_params_json: '',
  round_overrides: [],
  post_template: 'sleep',
  post_sleep_seconds: 1,
  retry_count: 0,
  retry_delay_seconds: 10,
  on_failure: 'ABORT',
})

const createEmptyStage = (): StageDraft => ({ items: [createEmptyTaskItem()] })

const resolveSleepSeconds = (postParams: Record<string, unknown> | null): number => {
  if (!postParams) {
    return Number.NaN
  }
  for (const key of ['sleep_time', 'sleep_seconds', 'seconds'] as const) {
    const rawValue = Number(postParams[key])
    if (Number.isFinite(rawValue) && rawValue >= 0) {
      return rawValue
    }
  }
  return Number.NaN
}

const isSleepPostParams = (postParams: Record<string, unknown> | null): boolean => {
  if (!postParams) {
    return false
  }
  return postParams.action === 'sleep' || Number.isFinite(resolveSleepSeconds(postParams))
}

const buildSleepPostParams = (seconds?: number) => ({
  sleep_time: Math.max(1, Math.floor(seconds || 1)),
})

const normalizeRoundOverridesForEditor = (
  roundOverrides: PlanStageItem['round_overrides'] | null | undefined
): StageItemRoundOverrideDraft[] => {
  if (!Array.isArray(roundOverrides) || roundOverrides.length === 0) {
    return []
  }

  const normalized: StageItemRoundOverrideDraft[] = []
  roundOverrides.forEach(override => {
    const round = Number(override.round)
    if (!Number.isInteger(round) || round <= 0) {
      return
    }
    const postParams =
      override.post_params && typeof override.post_params === 'object' ? override.post_params : null
    const isSleep = isSleepPostParams(postParams)
    const rawSeconds = isSleep ? resolveSleepSeconds(postParams) : Number.NaN
    normalized.push({
      round,
      run_params_json: override.run_params ? JSON.stringify(override.run_params, null, 2) : '',
      post_params_json: override.post_params ? JSON.stringify(override.post_params, null, 2) : '',
      post_template: isSleep ? 'sleep' : 'custom',
      post_sleep_seconds: Number.isFinite(rawSeconds) && rawSeconds > 0 ? rawSeconds : 1,
    })
  })
  return normalized.sort((a, b) => a.round - b.round)
}

const buildEmptyRoundOverrideDraft = (round: number): StageItemRoundOverrideDraft => ({
  round,
  run_params_json: '',
  post_params_json: '',
  post_template: 'sleep',
  post_sleep_seconds: 1,
})

const findRoundOverrideDraft = (
  item: StageItemDraft | null,
  roundNumber?: number
): StageItemRoundOverrideDraft | null => {
  if (!item || !roundNumber) {
    return null
  }
  return item.round_overrides.find(override => override.round === roundNumber) ?? null
}

const resolveTaskParamsForRound = (
  item: StageItemDraft,
  roundNumber?: number
): Record<string, unknown> => {
  const baseParams = safeParseObject(item.run_params_json)
  const override = findRoundOverrideDraft(item, roundNumber)
  if (!override?.run_params_json.trim()) {
    return baseParams
  }
  return { ...baseParams, ...safeParseObject(override.run_params_json) }
}

const normalizeStagesForEditor = (stages: PlanStage[] | null | undefined): StageDraft[] => {
  if (!Array.isArray(stages) || stages.length === 0) {
    return [createEmptyStage()]
  }

  const sorted = [...stages].sort((a, b) => Number(a.stage) - Number(b.stage))
  const normalized = sorted.map(stage => {
    if (!Array.isArray(stage.items) || stage.items.length === 0) {
      return createEmptyStage()
    }

    const items: StageItemDraft[] = stage.items.map(item => {
      const itemType: StageItemDraftType = item.type === 'postprocessor' ? 'postprocessor' : 'task'
      const postParams =
        item.post_params && typeof item.post_params === 'object' ? item.post_params : null
      const isSleep = isSleepPostParams(postParams)
      const rawSeconds = isSleep ? resolveSleepSeconds(postParams) : Number.NaN
      return {
        type: itemType,
        task_id: typeof item.task_id === 'number' ? item.task_id : undefined,
        run_params_json: item.run_params ? JSON.stringify(item.run_params, null, 2) : '',
        post_params_json: item.post_params ? JSON.stringify(item.post_params, null, 2) : '',
        round_overrides: normalizeRoundOverridesForEditor(item.round_overrides),
        post_template: isSleep ? 'sleep' : 'custom',
        post_sleep_seconds: Number.isFinite(rawSeconds) && rawSeconds > 0 ? rawSeconds : 1,
        retry_count: typeof item.retry_count === 'number' ? item.retry_count : 0,
        retry_delay_seconds:
          typeof item.retry_delay_seconds === 'number' ? item.retry_delay_seconds : 10,
        on_failure: ['ABORT', 'SKIP', 'CONTINUE'].includes(item.on_failure)
          ? item.on_failure
          : 'ABORT',
      }
    })

    return { items }
  })

  return normalized.length > 0 ? normalized : [createEmptyStage()]
}

const parseOptionalObjectJson = (
  raw: string,
  fieldLabel: string
): Record<string, unknown> | null => {
  const content = raw.trim()
  if (!content) return null

  const parsed = JSON.parse(content)
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
    throw new Error(`${fieldLabel} 必须是 JSON 对象`)
  }
  return parsed as Record<string, unknown>
}

const safeParseObject = (raw: string): Record<string, unknown> => {
  const content = raw.trim()
  if (!content) {
    return {}
  }
  try {
    const parsed = JSON.parse(content)
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
      return {}
    }
    return parsed as Record<string, unknown>
  } catch {
    return {}
  }
}

const stringifyObject = (value: Record<string, unknown> | null): string => {
  if (!value || Object.keys(value).length === 0) {
    return ''
  }
  return JSON.stringify(value, null, 2)
}

const normalizeConfiguredBusinessLines = (
  values: string[] | undefined,
  options: MetaItem[],
): string[] | null => {
  if (!Array.isArray(values)) {
    return null
  }
  const allowedCodes = new Set(options.map(item => item.code))
  const normalized = values
    .map(item => item.trim())
    .filter(item => item && allowedCodes.has(item))
  return normalized.length > 0 ? Array.from(new Set(normalized)) : []
}

const normalizeCollaboratorIds = (values: string[] | undefined): number[] | null => {
  if (!Array.isArray(values)) {
    return null
  }
  const normalized = values
    .map(item => Number(item))
    .filter(item => Number.isInteger(item) && item > 0)
  return normalized.length > 0 ? Array.from(new Set(normalized)) : []
}

const parseSimpleGradientValues = (raw: string): number[] => {
  const tokens = raw
    .split(/[\s,\n，]+/)
    .map(item => item.trim())
    .filter(Boolean)

  if (tokens.length === 0) {
    throw new Error('请至少输入一个梯度值')
  }

  const values = tokens.map(token => {
    const parsed = Number(token)
    if (!Number.isInteger(parsed) || parsed <= 0) {
      throw new Error('梯度值仅支持正整数，使用逗号分隔')
    }
    return parsed
  })

  for (let index = 1; index < values.length; index += 1) {
    if (values[index] <= values[index - 1]) {
      throw new Error('梯度值必须严格递增，且不能重复')
    }
  }

  return values
}

const pickNumericValue = (source: Record<string, unknown>, keys: string[]): number | null => {
  for (const key of keys) {
    const parsed = Number(source[key])
    if (Number.isFinite(parsed) && parsed > 0) {
      return parsed
    }
  }
  return null
}

const extractSimplePlanPreset = (plan?: Plan | null): SimplePlanPreset | null => {
  if (!plan || !Array.isArray(plan.stages) || plan.stages.length === 0) {
    return null
  }

  const stages = [...plan.stages].sort((left, right) => Number(left.stage) - Number(right.stage))
  if (stages.length < 1 || stages.length > 2) {
    return null
  }

  const taskStage = stages[0]
  if (!Array.isArray(taskStage.items) || taskStage.items.length !== 1) {
    return null
  }
  const taskItem = taskStage.items[0]
  if (taskItem.type !== 'task' || !taskItem.task_id) {
    return null
  }
  if (
    (taskItem.retry_count ?? 0) !== 0 ||
    (taskItem.retry_delay_seconds ?? 10) !== 10 ||
    (taskItem.on_failure ?? 'ABORT') !== 'ABORT'
  ) {
    return null
  }

  const intervalStage = stages[1]
  let intervalSeconds = 0
  if (intervalStage) {
    if (!Array.isArray(intervalStage.items) || intervalStage.items.length !== 1) {
      return null
    }
    const postItem = intervalStage.items[0]
    if (postItem.type !== 'postprocessor') {
      return null
    }
    if (
      (postItem.retry_count ?? 0) !== 0 ||
      (postItem.retry_delay_seconds ?? 10) !== 10 ||
      (postItem.on_failure ?? 'ABORT') !== 'ABORT'
    ) {
      return null
    }
    if (Array.isArray(postItem.round_overrides) && postItem.round_overrides.length > 0) {
      const expectedFinalRound = plan.enable_round ? (plan.total_round ?? 1) : 1
      if (
        postItem.round_overrides.length !== 1 ||
        Number(postItem.round_overrides[0]?.round) !== expectedFinalRound ||
        postItem.round_overrides[0]?.run_params != null ||
        !isZeroSleepOverride(
          postItem.round_overrides[0]?.post_params &&
            typeof postItem.round_overrides[0]?.post_params === 'object'
            ? (postItem.round_overrides[0]?.post_params as Record<string, unknown>)
            : null
        )
      ) {
        return null
      }
    }
    const postParams =
      postItem.post_params && typeof postItem.post_params === 'object'
        ? (postItem.post_params as Record<string, unknown>)
        : null
    if (!isSleepPostParams(postParams)) {
      return null
    }
    intervalSeconds = Math.max(0, Math.floor(resolveSleepSeconds(postParams)))
  }

  const baseRunParams =
    taskItem.run_params && typeof taskItem.run_params === 'object'
      ? normalizeCompatibleRunParams(taskItem.run_params as Record<string, unknown>, undefined)
      : {}

  const durationSeconds = extractDurationSeconds(baseRunParams)
  const podCount = extractPodCount(baseRunParams)
  const threadCount = extractThreadCount(baseRunParams)
  const baseTargetTps = pickNumericValue(baseRunParams, [
    'target_tps',
    'fixed_tps',
    'total_tps',
    'TARGET_TPS',
  ])

  const roundOverrides = Array.isArray(taskItem.round_overrides)
    ? [...taskItem.round_overrides].sort((left, right) => Number(left.round) - Number(right.round))
    : []

  const expectedRounds = roundOverrides.length + 1
  const declaredRounds = plan.enable_round ? (plan.total_round ?? expectedRounds) : 1
  if (declaredRounds !== expectedRounds) {
    return null
  }

  let rampMode: SimpleRampMode | null = null
  let gradientValues: number[] = []

  if (roundOverrides.length === 0) {
    if (baseTargetTps != null) {
      rampMode = 'target_tps'
      gradientValues = [Math.floor(baseTargetTps)]
    } else if (threadCount > 0) {
      rampMode = 'concurrency'
      gradientValues = [threadCount]
    } else {
      return null
    }
  } else {
    const targetOverrideValues: number[] = []
    const threadOverrideValues: number[] = []
    for (const override of roundOverrides) {
      if (
        !override ||
        Number(override.round) !== targetOverrideValues.length + threadOverrideValues.length + 2
      ) {
        return null
      }
      if (override.post_params != null) {
        return null
      }
      const overrideRunParams =
        override.run_params && typeof override.run_params === 'object'
          ? normalizeCompatibleRunParams(override.run_params as Record<string, unknown>, undefined)
          : null
      if (!overrideRunParams) {
        return null
      }
      const keys = Object.keys(overrideRunParams)
      if (keys.length !== 1) {
        return null
      }
      if (keys[0] === 'target_tps' && baseTargetTps != null) {
        const value = pickNumericValue(overrideRunParams, ['target_tps'])
        if (value == null) {
          return null
        }
        targetOverrideValues.push(Math.floor(value))
        continue
      }
      if (keys[0] === 'thread_count') {
        const value = pickNumericValue(overrideRunParams, ['thread_count'])
        if (value == null) {
          return null
        }
        threadOverrideValues.push(Math.floor(value))
        continue
      }
      return null
    }

    if (targetOverrideValues.length === roundOverrides.length && baseTargetTps != null) {
      rampMode = 'target_tps'
      gradientValues = [Math.floor(baseTargetTps), ...targetOverrideValues]
    } else if (threadOverrideValues.length === roundOverrides.length) {
      rampMode = 'concurrency'
      gradientValues = [threadCount, ...threadOverrideValues]
    } else {
      return null
    }
  }

  if (durationSeconds <= 0 || podCount <= 0 || gradientValues.length === 0) {
    return null
  }

  return {
    taskId: taskItem.task_id,
    rampMode,
    gradientValues,
    durationSeconds,
    intervalSeconds,
    podCount,
    capacityVus: threadCount > 0 ? threadCount : SIMPLE_DEFAULT_CAPACITY_VUS,
  }
}

const getTaskConfiguredPodCount = (task?: Task): number => {
  if (!task) {
    return SIMPLE_DEFAULT_POD_COUNT
  }
  if (typeof task.pod_count === 'number' && Number.isFinite(task.pod_count) && task.pod_count > 0) {
    return Math.floor(task.pod_count)
  }
  return extractPodCount(buildTaskFallbackRunParams(task))
}

const supportsSimpleTargetTps = (task?: Task): boolean => {
  if (!task) {
    return true
  }
  return SIMPLE_TARGET_TPS_ENGINE_TYPES.has(task.engine_type)
}

const buildSimplePlanStages = (params: {
  task?: Task
  rampMode: SimpleRampMode
  rampValues: number[]
  durationSeconds: number
  intervalSeconds: number
  podCount: number
  capacityVus: number
}): PlanStage[] => {
  const { task, rampMode, rampValues, durationSeconds, intervalSeconds, podCount, capacityVus } =
    params

  if (!task) {
    throw new Error('请选择任务')
  }
  if (rampMode === 'target_tps' && !supportsSimpleTargetTps(task)) {
    throw new Error('当前任务引擎暂不支持简版目标 TPS 递增，请切换为并发递增或使用高级编排')
  }
  if (rampValues.length === 0) {
    throw new Error('请至少输入一个梯度值')
  }

  const normalizedDurationSeconds = Math.max(1, Math.floor(durationSeconds))
  const normalizedIntervalSeconds = Math.max(0, Math.floor(intervalSeconds))
  const normalizedPodCount = Math.max(1, Math.floor(podCount))
  const normalizedCapacityVus = Math.max(1, Math.floor(capacityVus))

  const baseRunParams: Record<string, unknown> = {
    pod_count: normalizedPodCount,
    run_mode: 'duration',
    duration: normalizedDurationSeconds,
    duration_seconds: normalizedDurationSeconds,
  }
  if (rampMode === 'concurrency') {
    baseRunParams.thread_count = rampValues[0]
  } else {
    baseRunParams.thread_count = normalizedCapacityVus
    baseRunParams.target_tps = rampValues[0]
  }

  const roundOverrides = rampValues.slice(1).map((value, index) => ({
    round: index + 2,
    run_params: rampMode === 'concurrency' ? { thread_count: value } : { target_tps: value },
    post_params: null,
  }))

  const stages: PlanStage[] = [
    {
      stage: 0,
      items: [
        {
          type: 'task',
          task_id: task.id,
          task_name: task.name ?? null,
          task_pattern: task.task_pattern ?? null,
          run_params: baseRunParams,
          round_overrides: roundOverrides.length > 0 ? roundOverrides : undefined,
          retry_count: 0,
          retry_delay_seconds: 10,
          on_failure: 'ABORT',
        },
      ],
    },
  ]

  if (normalizedIntervalSeconds > 0) {
    stages.push({
      stage: 1,
      items: [
        {
          type: 'postprocessor',
          post_params: buildSleepPostParams(normalizedIntervalSeconds),
          retry_count: 0,
          retry_delay_seconds: 10,
          on_failure: 'ABORT',
        },
      ],
    })
  }

  return stages
}

const buildSimplePreviewRows = (params: {
  rampValues: number[]
  durationSeconds: number
  intervalSeconds: number
  podCount: number
  capacityVus: number
  rampMode: SimpleRampMode
}): SimplePreviewRow[] => {
  const { rampValues, durationSeconds, intervalSeconds, podCount, capacityVus, rampMode } = params

  return rampValues.map((rampValue, index) => ({
    key: `simple-round-${index + 1}`,
    round: index + 1,
    rampValue,
    durationSeconds: Math.max(1, Math.floor(durationSeconds)),
    intervalSeconds: Math.max(0, Math.floor(intervalSeconds)),
    podCount: Math.max(1, Math.floor(podCount)),
    capacityVus: rampMode === 'target_tps' ? Math.max(1, Math.floor(capacityVus)) : null,
  }))
}

const setOrDelete = (target: Record<string, unknown>, key: string, value: unknown) => {
  if (value === undefined || value === null || value === '') {
    delete target[key]
    return
  }
  target[key] = value
}

const TASK_PARAM_SOURCE_META: Record<
  TaskParamSourceKey,
  { tag: string; color: string; text: string }
> = {
  default_only: {
    tag: '默认参数',
    color: 'default',
    text: '当前先回填任务默认参数；保存后由当前计划模板固定下来。',
  },
  last_run_preferred: {
    tag: '最近执行参数',
    color: 'blue',
    text: '默认先合并最近一次执行参数，再由当前计划模板覆盖。',
  },
  plan_override_only: {
    tag: '计划模板',
    color: 'gold',
    text: '最近执行参数读取失败，先按计划当前模板编辑。',
  },
}

const pageShellStyle = {
  display: 'grid',
  gap: 16,
} as const

const workbenchHeaderStyle = {
  border: '1px solid var(--border-subtle)',
  borderRadius: 8,
  background: 'var(--card-bg)',
  padding: '14px 16px',
} as const

const summaryGridStyle = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
  gap: 8,
} as const

const summaryPanelStyle = {
  border: '1px solid var(--border-subtle)',
  borderRadius: 8,
  padding: '10px 12px',
  background: 'var(--card-bg)',
} as const

const stageItemCardStyle = {
  border: '1px solid var(--border-subtle)',
  borderRadius: 8,
  padding: '10px 12px',
  background: 'var(--card-bg-elevated)',
} as const

const stageItemTopRowStyle = {
  display: 'grid',
  gridTemplateColumns: 'minmax(0, 1fr) auto',
  gap: 12,
  alignItems: 'start',
} as const

const compactSummaryStyle = {
  border: '1px solid var(--border-subtle)',
  borderRadius: 8,
  background: 'var(--card-bg-elevated)',
  padding: '12px 14px',
  marginTop: 16,
} as const

const compactSummaryHeaderStyle = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'flex-start',
  gap: 12,
  flexWrap: 'wrap',
  marginBottom: 10,
} as const

const stageWorkbenchStyle = {
  border: '1px solid var(--border-subtle)',
  borderRadius: 8,
  background: 'var(--card-bg)',
  overflow: 'hidden',
} as const

const stageWorkbenchHeaderStyle = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'flex-start',
  gap: 12,
  flexWrap: 'wrap',
  padding: '10px 14px',
  borderBottom: '1px solid var(--border-subtle)',
  background: 'var(--surface-subtle)',
} as const

const stageWorkbenchBodyStyle = {
  display: 'grid',
  gap: 10,
  padding: '12px 14px',
} as const

const roundMatrixTableStyle = {
  width: '100%',
  minWidth: 900,
  borderCollapse: 'collapse',
} as const

const stageActionColumnStyle = {
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'flex-end',
  gap: 8,
} as const

const PLAN_FORM_ID = 'plan-create-edit-form'

const buildStagesPayload = (stageDrafts: StageDraft[], taskMap: Map<number, Task>): PlanStage[] => {
  if (stageDrafts.length === 0) {
    throw new Error('至少需要一个阶段')
  }

  return stageDrafts.map((stage, stageIndex) => {
    if (!Array.isArray(stage.items) || stage.items.length === 0) {
      throw new Error(`阶段 ${stageIndex + 1} 至少需要一个并行项`)
    }

    const items: PlanStageItem[] = stage.items.map((item, itemIndex) => {
      const title = `阶段 ${stageIndex + 1} - 并行项 ${itemIndex + 1}`
      const roundOverrides: NonNullable<PlanStageItem['round_overrides']> = []
      item.round_overrides.forEach(override => {
        const runParams = parseOptionalObjectJson(
          override.run_params_json,
          `${title} 轮次 ${override.round} 运行参数`
        )
        const postParams =
          item.type === 'postprocessor'
            ? override.post_template === 'sleep'
              ? buildSleepPostParams(override.post_sleep_seconds)
              : parseOptionalObjectJson(
                  override.post_params_json,
                  `${title} 轮次 ${override.round} post_params`
                )
            : parseOptionalObjectJson(
                override.post_params_json,
                `${title} 轮次 ${override.round} post_params`
              )
        if (!runParams && !postParams) {
          return
        }
        roundOverrides.push({
          round: override.round,
          run_params: runParams,
          post_params: postParams,
        })
      })
      if (item.type === 'task') {
        if (!item.task_id || item.task_id <= 0) {
          throw new Error(`${title} 未选择任务`)
        }
        const taskMeta = taskMap.get(item.task_id)
        return {
          type: 'task',
          task_id: item.task_id,
          task_name: taskMeta?.name ?? null,
          task_pattern: taskMeta?.task_pattern ?? null,
          run_params: parseOptionalObjectJson(item.run_params_json, `${title} 运行参数`),
          round_overrides: roundOverrides.length > 0 ? roundOverrides : undefined,
          retry_count: item.retry_count,
          retry_delay_seconds: item.retry_delay_seconds,
          on_failure: item.on_failure,
        }
      }

      return {
        type: 'postprocessor',
        post_params:
          item.post_template === 'sleep'
            ? buildSleepPostParams(item.post_sleep_seconds)
            : (parseOptionalObjectJson(item.post_params_json, `${title} post_params`) ??
              buildSleepPostParams(item.post_sleep_seconds)),
        round_overrides: roundOverrides.length > 0 ? roundOverrides : undefined,
        retry_count: item.retry_count,
        retry_delay_seconds: item.retry_delay_seconds,
        on_failure: item.on_failure,
      }
    })

    return { stage: stageIndex, items }
  })
}

const getTaskBusinessLines = (task?: Task): string[] => {
  if (!task) {
    return []
  }

  const values = new Set<string>()
  if (task.business_line) {
    values.add(task.business_line)
  }

  const rawProperties = task.properties
  if (rawProperties && typeof rawProperties === 'object') {
    const businessLine = rawProperties.business_line
    if (typeof businessLine === 'string' && businessLine) {
      values.add(businessLine)
    }

    const businessLines = rawProperties.business_lines
    if (Array.isArray(businessLines)) {
      businessLines.forEach(item => {
        if (typeof item === 'string' && item) {
          values.add(item)
        }
      })
    }
  }

  return Array.from(values)
}

const getTaskCollaborators = (task?: Task): string[] => {
  if (!task?.collaborator_names) {
    return []
  }
  return task.collaborator_names.filter((item): item is string => Boolean(item))
}

const getFallbackPlanBusinessLines = (plan?: Plan | null): string[] => {
  if (!plan?.business_lines) {
    return []
  }
  return plan.business_lines.filter((item): item is string => Boolean(item))
}

const getFallbackPlanCollaborators = (plan?: Plan | null): string[] => {
  if (!plan?.collaborator_names) {
    return []
  }
  return plan.collaborator_names.filter((item): item is string => Boolean(item))
}

const buildPlanCollaboratorNameMap = (plan?: Plan | null): Record<string, string> => {
  const ids = Array.isArray(plan?.collaborator_ids) ? plan?.collaborator_ids : []
  const names = Array.isArray(plan?.collaborator_names) ? plan?.collaborator_names : []
  const mapping: Record<string, string> = {}
  ids.forEach((id, index) => {
    const label = names[index]
    if (typeof id === 'number' && label) {
      mapping[String(id)] = label
    }
  })
  return mapping
}

const PlanCreateEdit = () => {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { planId } = useParams<{ planId?: string }>()
  const planIdNum = Number(planId)
  const isEdit = Number.isFinite(planIdNum)

  const [form] = Form.useForm<FormValues>()
  const [postprocessorForm] = Form.useForm()
  const [stageDrafts, setStageDrafts] = useState<StageDraft[]>([createEmptyStage()])
  const [taskModalLocator, setTaskModalLocator] = useState<ItemLocator | null>(null)
  const [taskParamDraft, setTaskParamDraft] = useState<TaskParamDraft | null>(null)
  const [postprocessorModalLocator, setPostprocessorModalLocator] = useState<ItemLocator | null>(
    null
  )
  const [showPostprocessorAdvanced, setShowPostprocessorAdvanced] = useState(false)
  const [taskSearchInput, setTaskSearchInput] = useState('')
  const [taskCatalog, setTaskCatalog] = useState<Map<number, Task>>(new Map())
  const [startConfirmOpen, setStartConfirmOpen] = useState(false)

  const composerMode = Form.useWatch('composer_mode', form) ?? (isEdit ? 'advanced' : 'simple')
  const currentExecType = Form.useWatch('exec_type', form) ?? 'manual'
  const currentStatus = Form.useWatch('status', form) ?? 'ready'
  const currentScheduledAt = Form.useWatch('scheduled_at', form)
  const enableRound = Form.useWatch('enable_round', form) ?? false
  const totalRound = Form.useWatch('total_round', form) ?? 10
  const simpleTaskId = Form.useWatch('simple_task_id', form)
  const simpleRampType = Form.useWatch('simple_ramp_type', form) ?? 'concurrency'
  const simpleGradientValues = Form.useWatch('simple_gradient_values', form) ?? ''
  const simpleDurationSeconds =
    Form.useWatch('simple_duration_seconds', form) ?? SIMPLE_DEFAULT_DURATION_SECONDS
  const simpleIntervalSeconds =
    Form.useWatch('simple_interval_seconds', form) ?? SIMPLE_DEFAULT_INTERVAL_SECONDS
  const simplePodCount = Form.useWatch('simple_pod_count', form) ?? SIMPLE_DEFAULT_POD_COUNT
  const simpleCapacityVus =
    Form.useWatch('simple_capacity_vus', form) ?? SIMPLE_DEFAULT_CAPACITY_VUS
  const explicitBusinessLines = Form.useWatch('business_lines', form) ?? []
  const explicitCollaboratorIds = Form.useWatch('collaborator_ids', form) ?? []
  const deferredTaskSearchInput = useDeferredValue(taskSearchInput)

  const normalizedTaskSearchInput = deferredTaskSearchInput.trim()
  const { data: planDetail, isLoading: detailLoading } = useQuery({
    queryKey: ['plan', planIdNum],
    queryFn: () => planApi.getPlanDetail(planIdNum),
    enabled: isEdit,
  })
  const collaboratorNameMap = useMemo(() => buildPlanCollaboratorNameMap(planDetail), [planDetail])

  const { data: taskListData, isLoading: taskListLoading } = useQuery({
    queryKey: ['plan-stage-task-options', normalizedTaskSearchInput],
    queryFn: () => {
      const numericTaskId = Number(normalizedTaskSearchInput)
      const isNumericTaskId =
        normalizedTaskSearchInput !== '' && Number.isInteger(numericTaskId) && numericTaskId > 0
      return taskApi.getTaskList({
        page: 1,
        pageSize: normalizedTaskSearchInput ? 20 : 100,
        task_id: isNumericTaskId ? numericTaskId : undefined,
        name: !isNumericTaskId && normalizedTaskSearchInput ? normalizedTaskSearchInput : undefined,
      })
    },
    staleTime: 60_000,
  })

  const missingSelectedTaskIds = useMemo(() => {
    const ids = new Set<number>()
    if (simpleTaskId && !taskCatalog.has(simpleTaskId)) {
      ids.add(simpleTaskId)
    }
    stageDrafts.forEach(stage => {
      stage.items.forEach(item => {
        if (item.type !== 'task' || !item.task_id) {
          return
        }
        if (!taskCatalog.has(item.task_id)) {
          ids.add(item.task_id)
        }
      })
    })
    return Array.from(ids).sort((a, b) => a - b)
  }, [stageDrafts, taskCatalog])

  const { data: missingSelectedTasks } = useQuery({
    queryKey: ['plan-stage-selected-task-options', missingSelectedTaskIds],
    queryFn: async () =>
      Promise.all(missingSelectedTaskIds.map(taskId => taskApi.getTaskDetail(taskId))),
    enabled: missingSelectedTaskIds.length > 0,
    staleTime: 60_000,
  })
  const { data: businessLineOptions = [], isLoading: businessLineOptionsLoading } = useQuery({
    queryKey: ['plan-business-lines'],
    queryFn: () => metaApi.getBusinessLines(),
    staleTime: 60_000,
  })

  const businessLineSelectOptions = useMemo(
    () => businessLineOptions.map(item => ({ label: item.name || item.code, value: item.code })),
    [businessLineOptions],
  )

  useEffect(() => {
    if (!Array.isArray(explicitBusinessLines) || businessLineOptions.length === 0) {
      return
    }
    const normalized = normalizeConfiguredBusinessLines(explicitBusinessLines, businessLineOptions) ?? []
    if (
      normalized.length !== explicitBusinessLines.length ||
      normalized.some((value, index) => value !== explicitBusinessLines[index])
    ) {
      form.setFieldValue('business_lines', normalized)
    }
  }, [businessLineOptions, explicitBusinessLines, form])

  useEffect(() => {
    if (!taskListData?.items?.length) {
      return
    }
    setTaskCatalog(current => {
      const next = new Map(current)
      taskListData.items.forEach(task => {
        next.set(task.id, task)
      })
      return next
    })
  }, [taskListData])

  useEffect(() => {
    if (!missingSelectedTasks?.length) {
      return
    }
    setTaskCatalog(current => {
      const next = new Map(current)
      missingSelectedTasks.forEach(task => {
        next.set(task.id, task)
      })
      return next
    })
  }, [missingSelectedTasks])

  const stageSelectedTaskIds = useMemo(() => {
    const ids = new Set<number>()
    stageDrafts.forEach(stage => {
      stage.items.forEach(item => {
        if (item.type === 'task' && item.task_id) {
          ids.add(item.task_id)
        }
      })
    })
    return ids
  }, [stageDrafts])

  const selectedTaskIds = useMemo(() => {
    const ids = new Set<number>(stageSelectedTaskIds)
    if (simpleTaskId) {
      ids.add(simpleTaskId)
    }
    return ids
  }, [simpleTaskId, stageSelectedTaskIds])

  const simplePlanPreset = useMemo(() => extractSimplePlanPreset(planDetail), [planDetail])

  const taskOptions = useMemo(() => {
    const seen = new Set<number>()
    const options: Array<{ label: string; value: number }> = []
    for (const task of taskListData?.items ?? []) {
      if (seen.has(task.id)) {
        continue
      }
      seen.add(task.id)
      options.push({
        label: `#${task.id} ${task.name} (${task.engine_type}/${task.env})`,
        value: task.id,
      })
    }
    for (const taskId of selectedTaskIds) {
      if (seen.has(taskId)) {
        continue
      }
      const task = taskCatalog.get(taskId)
      if (!task) {
        continue
      }
      seen.add(task.id)
      options.push({
        label: `#${task.id} ${task.name} (${task.engine_type}/${task.env})`,
        value: task.id,
      })
    }
    return options
  }, [selectedTaskIds, taskCatalog, taskListData])

  const taskMap = useMemo(() => {
    const map = new Map<number, Task>()
    for (const task of taskCatalog.values()) {
      map.set(task.id, task)
    }
    return map
  }, [taskCatalog])

  useEffect(() => {
    if (!isEdit) {
      form.setFieldsValue({
        status: 'ready',
        exec_type: 'manual',
        composer_mode: 'simple',
        enable_round: false,
        total_round: 10,
        simple_task_id: undefined,
        simple_ramp_type: 'concurrency',
        simple_gradient_values: '',
        simple_duration_seconds: SIMPLE_DEFAULT_DURATION_SECONDS,
        simple_interval_seconds: SIMPLE_DEFAULT_INTERVAL_SECONDS,
        simple_pod_count: SIMPLE_DEFAULT_POD_COUNT,
        simple_capacity_vus: SIMPLE_DEFAULT_CAPACITY_VUS,
        business_lines: [],
        collaborator_ids: [],
      })
      setStageDrafts([createEmptyStage()])
      return
    }

    if (!planDetail) return

    form.setFieldsValue({
      name: planDetail.name,
      description: planDetail.description ?? undefined,
      status: planDetail.status,
      exec_type: planDetail.exec_type,
      composer_mode: simplePlanPreset ? 'simple' : 'advanced',
      cron: planDetail.cron ?? undefined,
      timezone: planDetail.timezone ?? undefined,
      scheduled_at: planDetail.scheduled_at ? dayjs(planDetail.scheduled_at) : undefined,
      enable_round: Boolean(planDetail.enable_round),
      total_round: planDetail.total_round ?? 10,
      simple_task_id: simplePlanPreset?.taskId,
      simple_ramp_type: simplePlanPreset?.rampMode ?? 'concurrency',
      simple_gradient_values: simplePlanPreset ? simplePlanPreset.gradientValues.join(',') : '',
      simple_duration_seconds: simplePlanPreset?.durationSeconds ?? SIMPLE_DEFAULT_DURATION_SECONDS,
      simple_interval_seconds: simplePlanPreset?.intervalSeconds ?? SIMPLE_DEFAULT_INTERVAL_SECONDS,
      simple_pod_count: simplePlanPreset?.podCount ?? SIMPLE_DEFAULT_POD_COUNT,
      simple_capacity_vus: simplePlanPreset?.capacityVus ?? SIMPLE_DEFAULT_CAPACITY_VUS,
      business_lines: Array.isArray(planDetail.business_lines) ? planDetail.business_lines : [],
      collaborator_ids: Array.isArray(planDetail.collaborator_ids)
        ? planDetail.collaborator_ids.map(String)
        : [],
    })
    setStageDrafts(normalizeStagesForEditor(planDetail.stages))
  }, [form, isEdit, planDetail, simplePlanPreset])

  const mutation = useMutation({
    mutationFn: async (data: PlanCreateRequest) => {
      if (isEdit) {
        return planApi.updatePlan(planIdNum, data)
      }
      return planApi.createPlan(data)
    },
    onSuccess: async plan => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['plans'] }),
        queryClient.invalidateQueries({ queryKey: ['plan-runs'] }),
        queryClient.invalidateQueries({ queryKey: ['plan', plan.plan_id] }),
      ])
    },
  })
  const executeMutation = useMutation({
    mutationFn: (planId: number) => planApi.executePlan(planId, { start_type: 1 }),
    onSuccess: resp => {
      message.success('已启动批次，正在打开详情。')
      queryClient.invalidateQueries({ queryKey: ['plans'] })
      queryClient.invalidateQueries({ queryKey: ['plan-runs'] })
      navigate(`/plan-runs/${resp.plan_run_id}`)
    },
  })
  const latestPlanRunActive = Boolean(
    planDetail?.latest_plan_run_status &&
      ACTIVE_PLAN_RUN_STATUSES.has(String(planDetail.latest_plan_run_status))
  )
  const saveDisabled = mutation.isPending || (isEdit && (detailLoading || !planDetail))
  const startDisabled =
    saveDisabled ||
    executeMutation.isPending ||
    startConfirmOpen ||
    currentStatus !== 'ready' ||
    latestPlanRunActive

  const updateStageItem = (
    stageIndex: number,
    itemIndex: number,
    patch: Partial<StageItemDraft>
  ) => {
    setStageDrafts(prev =>
      prev.map((stage, sIdx) => {
        if (sIdx !== stageIndex) return stage
        return {
          ...stage,
          items: stage.items.map((item, iIdx) =>
            iIdx === itemIndex ? { ...item, ...patch } : item
          ),
        }
      })
    )
  }

  const addParallelItem = (stageIndex: number) => {
    setStageDrafts(prev =>
      prev.map((stage, sIdx) => {
        if (sIdx !== stageIndex) return stage
        return {
          ...stage,
          items: [...stage.items, createEmptyTaskItem()],
        }
      })
    )
  }

  const removeParallelItem = (stageIndex: number, itemIndex: number) => {
    setStageDrafts(prev => {
      const target = prev[stageIndex]
      if (!target || target.items.length <= 1) {
        message.warning('每个阶段至少保留一个并行项')
        return prev
      }
      return prev.map((stage, sIdx) => {
        if (sIdx !== stageIndex) return stage
        return {
          ...stage,
          items: stage.items.filter((_, iIdx) => iIdx !== itemIndex),
        }
      })
    })
  }

  const addStage = () => {
    setStageDrafts(prev => [...prev, createEmptyStage()])
  }

  const removeStage = (stageIndex: number) => {
    setStageDrafts(prev => {
      if (prev.length <= 1) {
        message.warning('至少保留一个阶段')
        return prev
      }
      return prev.filter((_, idx) => idx !== stageIndex)
    })
  }

  const moveStage = (stageIndex: number, direction: 'up' | 'down') => {
    setStageDrafts(prev => {
      const target = direction === 'up' ? stageIndex - 1 : stageIndex + 1
      if (target < 0 || target >= prev.length) {
        return prev
      }
      const next = [...prev]
      ;[next[stageIndex], next[target]] = [next[target], next[stageIndex]]
      return next
    })
  }

  const moveParallelItem = (stageIndex: number, itemIndex: number, direction: 'up' | 'down') => {
    setStageDrafts(prev => {
      const stage = prev[stageIndex]
      if (!stage) {
        return prev
      }
      const target = direction === 'up' ? itemIndex - 1 : itemIndex + 1
      if (target < 0 || target >= stage.items.length) {
        return prev
      }
      const nextItems = [...stage.items]
      ;[nextItems[itemIndex], nextItems[target]] = [nextItems[target], nextItems[itemIndex]]
      return prev.map((item, idx) => (idx === stageIndex ? { ...item, items: nextItems } : item))
    })
  }

  const getItem = (locator: ItemLocator | null): StageItemDraft | null => {
    if (!locator) {
      return null
    }
    return stageDrafts[locator.stageIndex]?.items[locator.itemIndex] ?? null
  }

  const closeTaskConfigModal = () => {
    setTaskModalLocator(null)
    setTaskParamDraft(null)
  }

  const updateTaskParamDraft = (patch: Partial<TaskParamDraft>) => {
    setTaskParamDraft(current => (current ? { ...current, ...patch } : current))
  }

  const updateTaskParamRow = (rowId: string, patch: Partial<ParamRow>) => {
    setTaskParamDraft(current => {
      if (!current) {
        return current
      }
      return {
        ...current,
        rows: current.rows.map(row => (row.row_id === rowId ? { ...row, ...patch } : row)),
      }
    })
  }

  const appendTaskParamRow = () => {
    setTaskParamDraft(current => {
      if (!current) {
        return current
      }
      return {
        ...current,
        rows: [
          ...current.rows,
          {
            row_id: createParamRowId(),
            name: '',
            type: 'string',
            value: '',
          },
        ],
      }
    })
  }

  const removeTaskParamRow = (rowId: string) => {
    setTaskParamDraft(current => {
      if (!current) {
        return current
      }
      return {
        ...current,
        rows: current.rows.filter(row => row.row_id !== rowId),
      }
    })
  }

  const openTaskConfigModal = async (
    stageIndex: number,
    itemIndex: number,
    roundNumber?: number
  ) => {
    const item = stageDrafts[stageIndex]?.items[itemIndex]
    if (!item) {
      return
    }
    if (!item.task_id) {
      message.warning('请先选择任务，再配置变量参数')
      return
    }
    const taskMeta = taskMap.get(item.task_id)
    const currentRunParams = resolveTaskParamsForRound(item, roundNumber)
    let sourceKey: TaskParamSourceKey = 'plan_override_only'
    let baselineParams: Record<string, unknown> = buildTaskFallbackRunParams(taskMeta)

    try {
      const payload = await queryClient.fetchQuery({
        queryKey: ['task-last-run-params', item.task_id],
        queryFn: () => taskApi.getTaskLastRunParams(item.task_id!),
        staleTime: 0,
      })
      baselineParams = {
        ...baselineParams,
        ...(payload.default_run_params ?? {}),
        ...(payload.last_run_params ?? {}),
      }
      sourceKey =
        payload.param_source ?? (payload.last_run_params ? 'last_run_preferred' : 'default_only')
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : '稍后再试'
      message.warning(`读取任务默认/最近执行参数失败，先按计划模板编辑：${errorMessage}`)
    }

    const normalizedBaselineParams = normalizeCompatibleRunParams(
      baselineParams,
      taskMeta?.engine_type
    )
    const normalizedCurrentRunParams = normalizeCompatibleRunParams(
      currentRunParams,
      taskMeta?.engine_type
    )
    const mergedRunParams = { ...normalizedBaselineParams }
    if (hasRunModeControlParams(normalizedCurrentRunParams)) {
      clearRunModeControlParams(mergedRunParams)
    }
    Object.assign(mergedRunParams, normalizedCurrentRunParams)
    setTaskParamDraft({
      podCount: extractPodCount(mergedRunParams),
      threadCount: extractThreadCount(mergedRunParams, taskMeta?.thread_count),
      runMode: resolveInitialRunMode(taskMeta?.engine_type, mergedRunParams),
      durationSeconds: extractDurationSeconds(mergedRunParams, taskMeta?.duration),
      iterations: extractIterations(mergedRunParams),
      preservedParams: mergedRunParams,
      rows: buildParamRows(mergedRunParams),
      sourceKey,
    })
    setTaskModalLocator({ stageIndex, itemIndex, roundNumber })
  }

  const saveTaskConfigModal = () => {
    if (!taskModalLocator || !taskParamDraft) {
      return
    }
    const currentItem = getItem(taskModalLocator)
    const currentTask = currentItem?.task_id ? taskMap.get(currentItem.task_id) : undefined
    const supportsIteration = supportsIterationRunMode(currentTask?.engine_type)
    if (!Number.isFinite(taskParamDraft.podCount) || taskParamDraft.podCount <= 0) {
      message.error('节点数量必须大于 0')
      return
    }
    if (!Number.isFinite(taskParamDraft.threadCount) || taskParamDraft.threadCount <= 0) {
      message.error('执行并发必须大于 0')
      return
    }
    if (
      taskParamDraft.runMode === 'duration' &&
      (!Number.isFinite(taskParamDraft.durationSeconds) || taskParamDraft.durationSeconds <= 0)
    ) {
      message.error('执行时长必须大于 0')
      return
    }
    if (taskParamDraft.runMode === 'iterations' && !supportsIteration) {
      message.error('当前任务引擎不支持按次数运行')
      return
    }
    if (
      taskParamDraft.runMode === 'iterations' &&
      (!Number.isFinite(taskParamDraft.iterations) || taskParamDraft.iterations <= 0)
    ) {
      message.error('执行次数必须大于 0')
      return
    }
    const normalizedNames = taskParamDraft.rows.map(row => row.name.trim())
    if (normalizedNames.some(name => !name)) {
      message.error('脚本变量名称不能为空')
      return
    }
    if (new Set(normalizedNames).size !== normalizedNames.length) {
      message.error('脚本变量名称不能重复')
      return
    }

    const merged = { ...taskParamDraft.preservedParams }
    clearManagedRunControlParams(merged)
    clearEditableScriptVariableParams(merged)
    setOrDelete(merged, 'pod_count', Math.floor(taskParamDraft.podCount))
    setOrDelete(merged, 'pod_num', Math.floor(taskParamDraft.podCount))
    setOrDelete(merged, 'thread_count', Math.floor(taskParamDraft.threadCount))
    if (taskParamDraft.runMode !== 'script_default') {
      setOrDelete(merged, 'run_mode', taskParamDraft.runMode)
    }
    if (taskParamDraft.runMode === 'duration') {
      setOrDelete(merged, 'duration', Math.floor(taskParamDraft.durationSeconds))
      setOrDelete(merged, 'duration_seconds', Math.floor(taskParamDraft.durationSeconds))
    }
    if (taskParamDraft.runMode === 'iterations') {
      setOrDelete(merged, 'iterations', Math.floor(taskParamDraft.iterations))
    }
    taskParamDraft.rows.forEach(row => {
      const name = row.name.trim()
      if (!name) {
        return
      }
      merged[name] = serializeParamRow(row)
    })
    const normalizedPayload = normalizeCompatibleRunParams(merged, currentTask?.engine_type)
    if (taskModalLocator.roundNumber) {
      const nextRoundOverrides = [...(currentItem?.round_overrides ?? [])]
      const existingIndex = nextRoundOverrides.findIndex(
        override => override.round === taskModalLocator.roundNumber
      )
      const nextOverride = {
        ...(existingIndex >= 0
          ? nextRoundOverrides[existingIndex]
          : buildEmptyRoundOverrideDraft(taskModalLocator.roundNumber)),
        round: taskModalLocator.roundNumber,
        run_params_json: stringifyObject(normalizedPayload),
      }
      if (existingIndex >= 0) {
        nextRoundOverrides[existingIndex] = nextOverride
      } else {
        nextRoundOverrides.push(nextOverride)
      }
      nextRoundOverrides.sort((a, b) => a.round - b.round)
      updateStageItem(taskModalLocator.stageIndex, taskModalLocator.itemIndex, {
        round_overrides: nextRoundOverrides,
      })
    } else {
      updateStageItem(taskModalLocator.stageIndex, taskModalLocator.itemIndex, {
        run_params_json: stringifyObject(normalizedPayload),
      })
    }
    closeTaskConfigModal()
  }

  const openPostprocessorModal = (stageIndex: number, itemIndex: number, roundNumber?: number) => {
    const item = stageDrafts[stageIndex]?.items[itemIndex]
    if (!item) {
      return
    }
    const roundOverride = findRoundOverrideDraft(item, roundNumber)
    postprocessorForm.setFieldsValue({
      post_template: roundOverride?.post_template ?? item.post_template,
      post_sleep_seconds: roundOverride?.post_sleep_seconds ?? item.post_sleep_seconds ?? 1,
      retry_count: item.retry_count,
      retry_delay_seconds: item.retry_delay_seconds,
      on_failure: item.on_failure,
      post_params_json: roundOverride?.post_params_json ?? item.post_params_json,
    })
    setShowPostprocessorAdvanced(false)
    setPostprocessorModalLocator({ stageIndex, itemIndex, roundNumber })
  }

  const savePostprocessorModal = async () => {
    if (!postprocessorModalLocator) {
      return
    }
    const values = await postprocessorForm.validateFields()
    const normalizedPostTemplate =
      values.post_template === 'custom' && !String(values.post_params_json || '').trim()
        ? 'sleep'
        : values.post_template
    const currentItem = getItem(postprocessorModalLocator)
    if (postprocessorModalLocator.roundNumber) {
      const nextRoundOverrides = [...(currentItem?.round_overrides ?? [])]
      const existingIndex = nextRoundOverrides.findIndex(
        override => override.round === postprocessorModalLocator.roundNumber
      )
      const nextOverride = {
        ...(existingIndex >= 0
          ? nextRoundOverrides[existingIndex]
          : buildEmptyRoundOverrideDraft(postprocessorModalLocator.roundNumber)),
        round: postprocessorModalLocator.roundNumber,
        post_template: normalizedPostTemplate,
        post_sleep_seconds: values.post_sleep_seconds ?? 1,
        post_params_json: values.post_params_json || '',
      }
      if (existingIndex >= 0) {
        nextRoundOverrides[existingIndex] = nextOverride
      } else {
        nextRoundOverrides.push(nextOverride)
      }
      nextRoundOverrides.sort((a, b) => a.round - b.round)
      updateStageItem(postprocessorModalLocator.stageIndex, postprocessorModalLocator.itemIndex, {
        round_overrides: nextRoundOverrides,
      })
    } else {
      updateStageItem(postprocessorModalLocator.stageIndex, postprocessorModalLocator.itemIndex, {
        post_template: normalizedPostTemplate,
        post_sleep_seconds: values.post_sleep_seconds ?? 1,
        retry_count: values.retry_count ?? 0,
        retry_delay_seconds: values.retry_delay_seconds ?? 10,
        on_failure: values.on_failure,
        post_params_json: values.post_params_json || '',
      })
    }
    setShowPostprocessorAdvanced(false)
    setPostprocessorModalLocator(null)
  }

  const buildSubmitPayload = (values: FormValues): PlanCreateRequest => {
    if (isEdit && !planDetail) {
      throw new PlanSubmitError('计划详情加载中，请稍后再试')
    }

    let stages: PlanStage[]
    let enableRoundValue = Boolean(values.enable_round)
    let totalRoundValue = values.enable_round ? Math.max(2, Number(values.total_round || 2)) : null
    try {
      if (composerMode === 'simple') {
        const rampValues = parseSimpleGradientValues(values.simple_gradient_values || '')
        stages = buildSimplePlanStages({
          task: values.simple_task_id ? taskMap.get(values.simple_task_id) : undefined,
          rampMode: values.simple_ramp_type ?? 'concurrency',
          rampValues,
          durationSeconds: Number(
            values.simple_duration_seconds || SIMPLE_DEFAULT_DURATION_SECONDS
          ),
          intervalSeconds: Number(values.simple_interval_seconds || 0),
          podCount: Number(values.simple_pod_count || SIMPLE_DEFAULT_POD_COUNT),
          capacityVus: Number(values.simple_capacity_vus || SIMPLE_DEFAULT_CAPACITY_VUS),
        })
        enableRoundValue = rampValues.length > 1
        totalRoundValue = rampValues.length > 1 ? rampValues.length : null
      } else {
        stages = buildStagesPayload(stageDrafts, taskMap)
      }
    } catch (error) {
      throw new PlanSubmitError(error instanceof Error ? error.message : '计划配置校验失败')
    }

    return {
      name: values.name,
      description: values.description ?? null,
      status: values.status,
      exec_type: values.exec_type,
      cron: values.exec_type === 'cron' ? (values.cron ?? null) : null,
      scheduled_at:
        values.exec_type === 'fixed' && values.scheduled_at
          ? values.scheduled_at.toDate().toISOString()
          : null,
      timezone: values.timezone ?? null,
      enable_round: enableRoundValue,
      total_round: totalRoundValue,
      business_lines: normalizeConfiguredBusinessLines(values.business_lines, businessLineOptions),
      collaborator_ids: normalizeCollaboratorIds(values.collaborator_ids),
      stages,
    }
  }

  const saveTemplate = async (values: FormValues): Promise<Plan> => {
    const payload = buildSubmitPayload(values)
    return mutation.mutateAsync(payload)
  }

  const handleSubmit = async (values: FormValues) => {
    try {
      await saveTemplate(values)
      message.success(isEdit ? '批次模板已更新' : '批次模板已创建')
      navigate('/plans')
    } catch (error) {
      if (error instanceof PlanSubmitError) {
        message.error(error instanceof Error ? error.message : '操作失败')
      }
    }
  }

  const handleSaveAndStart = async () => {
    if (!isEdit || !Number.isFinite(planIdNum)) {
      return
    }
    if (startConfirmOpen) {
      return
    }
    if (currentStatus !== 'ready') {
      message.warning('当前模板状态不是可执行，请先切换为可执行后再启动。')
      return
    }
    if (latestPlanRunActive) {
      message.warning('已有执行中的批次，请等待结束后再启动。')
      return
    }

    let values: FormValues
    try {
      values = await form.validateFields()
    } catch (error) {
      if (!isFormValidationError(error)) {
        message.error(error instanceof Error ? error.message : '计划配置校验失败')
      }
      return
    }

    setStartConfirmOpen(true)
    Modal.confirm({
      title: '保存并启动批次？',
      content: '将先保存当前页面配置，再为该模板创建一次新的批次执行。',
      okText: '保存并启动',
      cancelText: '取消',
      afterClose: () => setStartConfirmOpen(false),
      onOk: async () => {
        try {
          await saveTemplate(values)
          await executeMutation.mutateAsync(planIdNum)
        } catch (error) {
          if (error instanceof PlanSubmitError) {
            message.error(error instanceof Error ? error.message : '启动批次失败')
          }
          throw error
        }
      },
    })
  }

  const renderStartDisabledReason = () => {
    if (!isEdit) {
      return ''
    }
    if (latestPlanRunActive) {
      return '已有执行中的批次'
    }
    if (currentStatus !== 'ready') {
      return '当前模板不可执行'
    }
    return ''
  }

  const renderTaskSummaryTags = (item: StageItemDraft) => {
    const taskMeta = item.task_id ? taskMap.get(item.task_id) : undefined
    const runParams = normalizeCompatibleRunParams(
      safeParseObject(item.run_params_json),
      taskMeta?.engine_type
    )
    const tags: string[] = []
    if (
      runParams.thread_count != null ||
      runParams.num_threads != null ||
      runParams.threads != null ||
      runParams.vus != null
    ) {
      tags.push(`并发 ${extractThreadCount(runParams)}`)
    }
    const runMode = resolveInitialRunMode(taskMeta?.engine_type, runParams)
    if (runMode === 'iterations' && extractIterations(runParams) > 0) {
      tags.push(`按次数 ${extractIterations(runParams)}`)
    }
    if (
      runMode === 'duration' &&
      (runParams.duration != null || runParams.duration_seconds != null) &&
      extractDurationSeconds(runParams) > 0
    ) {
      tags.push(`按时长 ${extractDurationSeconds(runParams)}s`)
    }
    buildParamRows(runParams)
      .slice(0, 2)
      .forEach(row => {
        tags.push(`${row.name}=${row.value}`)
      })
    if (typeof runParams.tag === 'string' && runParams.tag) {
      tags.push(`标签 ${runParams.tag}`)
    }
    return tags.length > 0 ? tags : ['默认参数']
  }

  const renderItemLabel = (item: StageItemDraft) => {
    if (item.type === 'postprocessor') {
      return '后置处理'
    }
    if (!item.task_id) {
      return '未选择任务'
    }
    return taskMap.get(item.task_id)?.name || `Task #${item.task_id}`
  }

  const matrixRows = stageDrafts.flatMap((stage, stageIndex) =>
    stage.items.map((item, itemIndex) => ({
      key: `matrix-${stageIndex}-${itemIndex}`,
      label: `阶段${stageIndex + 1}-${renderItemLabel(item)}`,
      type: item.type,
      stageIndex,
      itemIndex,
    }))
  )

  const simpleTask = simpleTaskId ? taskMap.get(simpleTaskId) : undefined
  const simpleGradientParseResult = useMemo(() => {
    try {
      return {
        values: simpleGradientValues ? parseSimpleGradientValues(simpleGradientValues) : [],
        error: null as string | null,
      }
    } catch (error) {
      return {
        values: [] as number[],
        error: error instanceof Error ? error.message : '梯度值格式不正确',
      }
    }
  }, [simpleGradientValues])

  const simplePreviewRows = useMemo(
    () =>
      buildSimplePreviewRows({
        rampValues: simpleGradientParseResult.values,
        durationSeconds: Number(simpleDurationSeconds || SIMPLE_DEFAULT_DURATION_SECONDS),
        intervalSeconds: Number(simpleIntervalSeconds || 0),
        podCount: Number(simplePodCount || SIMPLE_DEFAULT_POD_COUNT),
        capacityVus: Number(simpleCapacityVus || SIMPLE_DEFAULT_CAPACITY_VUS),
        rampMode: simpleRampType,
      }),
    [
      simpleCapacityVus,
      simpleDurationSeconds,
      simpleGradientParseResult.values,
      simpleIntervalSeconds,
      simplePodCount,
      simpleRampType,
    ]
  )
  const simpleRoundCount = simplePreviewRows.length
  const simpleStageCount =
    simpleRoundCount === 0 ? 0 : Number(simpleIntervalSeconds || 0) > 0 ? 2 : 1
  const simpleTotalSeconds = simplePreviewRows.reduce(
    (sum, row) => sum + row.durationSeconds + row.intervalSeconds,
    0
  )
  const simpleEstimatedEndAt =
    currentExecType === 'fixed' &&
    currentScheduledAt &&
    dayjs.isDayjs(currentScheduledAt) &&
    simpleTotalSeconds > 0
      ? currentScheduledAt.add(simpleTotalSeconds, 'second')
      : null

  const composerSummary = useMemo(() => {
    const businessLines = new Set<string>()
    const collaborators = new Set<string>()
    const taskNames = new Set<string>()
    const envs = new Set<string>()
    if (composerMode === 'simple') {
      if (simpleTask) {
        getTaskBusinessLines(simpleTask).forEach(value => businessLines.add(value))
        getTaskCollaborators(simpleTask).forEach(value => collaborators.add(value))
        if (simpleTask.name) {
          taskNames.add(simpleTask.name)
        }
        if (simpleTask.env) {
          envs.add(simpleTask.env)
        }
      }
    } else {
      stageDrafts.forEach(stage => {
        stage.items.forEach(item => {
          if (item.type !== 'task' || !item.task_id) {
            return
          }

          const task = taskMap.get(item.task_id)
          if (!task) {
            return
          }

          getTaskBusinessLines(task).forEach(value => businessLines.add(value))
          getTaskCollaborators(task).forEach(value => collaborators.add(value))
          if (task.name) {
            taskNames.add(task.name)
          }
          if (task.env) {
            envs.add(task.env)
          }
        })
      })
    }

    const derivedBusinessLines = Array.from(businessLines)
    const derivedCollaborators = Array.from(collaborators)

    return {
      businessLines:
        derivedBusinessLines.length > 0
          ? derivedBusinessLines
          : getFallbackPlanBusinessLines(planDetail),
      collaborators:
        derivedCollaborators.length > 0
          ? derivedCollaborators
          : getFallbackPlanCollaborators(planDetail),
      taskNames: Array.from(taskNames),
      envs: Array.from(envs),
    }
  }, [composerMode, planDetail, simpleTask, stageDrafts, taskMap])

  const activeTaskItem = getItem(taskModalLocator)
  const activeTask = activeTaskItem?.task_id ? taskMap.get(activeTaskItem.task_id) : undefined
  const activeTaskSupportsIteration = supportsIterationRunMode(activeTask?.engine_type)
  const taskParamSourceMeta =
    TASK_PARAM_SOURCE_META[taskParamDraft?.sourceKey ?? 'plan_override_only']
  const activeTaskRoundLabel = taskModalLocator?.roundNumber
    ? `轮次 ${taskModalLocator.roundNumber}`
    : '基础模板'
  const activePostprocessorRoundLabel = postprocessorModalLocator?.roundNumber
    ? `轮次 ${postprocessorModalLocator.roundNumber}`
    : '基础模板'

  return (
    <div className="olh-page-shell olh-console-page olh-plan-editor-page" style={pageShellStyle}>
      <div className="olh-console-hero olh-plan-editor-hero" style={workbenchHeaderStyle}>
        <div
          className="olh-plan-editor-hero-row"
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'flex-start',
            gap: 16,
            flexWrap: 'wrap',
          }}
        >
          <div className="olh-console-hero-main">
            <div className="olh-page-breadcrumb" style={{ color: 'var(--text-secondary)', fontSize: 12, marginBottom: 6 }}>
              OpenLoadHub / 批次模板 / {isEdit ? '编辑模板' : '新建模板'}
            </div>
            <h1
              className="olh-page-title"
              aria-label={isEdit ? '编辑批次模板' : '新建批次模板'}
            >
              {isEdit ? '编辑批次模板' : '新建批次模板'}
            </h1>
            <div className="olh-page-subtitle">
              {composerMode === 'simple'
                ? '先用单任务阶梯配置快速生成一条批次模板；需要并行、多阶段或复杂后置处理时，再切到高级编排。'
                : '把阶段编排、任务参数和后置处理集中在一页完成，直接得到可执行的批次模板。'}
            </div>
          </div>
          <Space className="olh-console-actions">
            {isEdit ? (
              <Button
                type="primary"
                ghost
                icon={<PlayCircleOutlined />}
                loading={executeMutation.isPending}
                disabled={startDisabled}
                onClick={handleSaveAndStart}
                title={renderStartDisabledReason() || undefined}
                data-testid="plan-start-button"
              >
                启动批次
              </Button>
            ) : null}
            <Button onClick={() => navigate('/plans')}>返回模板列表</Button>
            <Button
              type="primary"
              htmlType="submit"
              form={PLAN_FORM_ID}
              loading={mutation.isPending}
              disabled={saveDisabled}
              data-testid="plan-save-button"
            >
              保存模板
            </Button>
          </Space>
        </div>
        <div className="olh-console-command-strip" style={{ marginTop: 12 }}>
          <Space wrap size={[8, 8]}>
            <Tag color={composerMode === 'simple' ? 'gold' : 'blue'}>
              {composerMode === 'simple' ? '简版配置' : '高级编排'}
            </Tag>
            <Tag
              color={
                composerMode === 'simple'
                  ? simpleRoundCount > 1
                    ? 'green'
                    : 'default'
                  : form.getFieldValue('enable_round')
                    ? 'green'
                    : 'default'
              }
            >
              {composerMode === 'simple'
                ? simpleRoundCount > 0
                  ? `共 ${simpleRoundCount} 轮`
                  : '待输入梯度'
                : form.getFieldValue('enable_round')
                  ? `共 ${form.getFieldValue('total_round') || 2} 轮`
                  : '单轮'}
            </Tag>
            <Tag>{`阶段 ${composerMode === 'simple' ? simpleStageCount : stageDrafts.length}`}</Tag>
            <Tag>{`并行项 ${composerMode === 'simple' ? (simpleTask ? 1 : 0) : matrixRows.length}`}</Tag>
            <Tag>{`任务 ${composerSummary.taskNames.length}`}</Tag>
            <Tag>{`环境 ${composerSummary.envs.join(' / ') || '-'}`}</Tag>
          </Space>
        </div>
      </div>

      <Form id={PLAN_FORM_ID} form={form} layout="vertical" onFinish={handleSubmit}>
        <Card className="olh-dashboard-panel olh-plan-editor-panel" loading={detailLoading} title="基础信息" style={{ marginBottom: 16 }}>
          <Form.Item name="composer_mode" label="配置模式" style={{ marginBottom: 16 }}>
            <Radio.Group optionType="button" buttonStyle="solid" options={COMPOSER_MODE_OPTIONS} />
          </Form.Item>

          <Alert
            className="olh-plan-editor-mode-note"
            showIcon
            type={composerMode === 'simple' ? 'info' : 'warning'}
            style={{ marginBottom: 16 }}
            message={composerMode === 'simple' ? '当前使用简版配置' : '当前使用高级编排'}
            description={
              composerMode === 'simple'
                ? '简版只支持单任务、固定时长、固定间隔和固定节点数，适合快速生成标准阶梯压测计划。'
                : '高级编排保留阶段串行、阶段内并行、高级轮次矩阵和后置处理等完整能力。'
            }
          />

          <Row gutter={16}>
            <Col xs={24} md={12}>
              <Form.Item
                name="name"
                label="计划名称"
                rules={[{ required: true, message: '请输入计划名称' }]}
              >
                <Input placeholder="请输入计划名称" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="description" label="计划描述">
                <Input placeholder="可选" />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item
                name="status"
                label="执行状态"
                rules={[{ required: true, message: '请选择状态' }]}
              >
                <Select
                  options={[
                    { label: '可执行', value: 'ready' satisfies PlanStatus },
                    { label: '暂停', value: 'suspended' satisfies PlanStatus },
                    { label: '执行中', value: 'running' satisfies PlanStatus },
                  ]}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item
                name="exec_type"
                label="执行方式"
                rules={[{ required: true, message: '请选择执行方式' }]}
              >
                <Select options={EXEC_TYPE_OPTIONS.map(({ hint: _hint, ...rest }) => rest)} />
              </Form.Item>
            </Col>
            {composerMode === 'advanced' ? (
              <Col xs={24} md={8}>
                <Form.Item name="enable_round" label="轮次编排" valuePropName="checked">
                  <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                </Form.Item>
              </Col>
            ) : null}
          </Row>

          <Form.Item
            noStyle
            shouldUpdate={(prev, next) =>
              prev.exec_type !== next.exec_type ||
              prev.enable_round !== next.enable_round ||
              prev.composer_mode !== next.composer_mode
            }
          >
            {({ getFieldValue }) => {
              const execType = getFieldValue('exec_type') as PlanExecType | undefined
              const enableRoundInner = Boolean(getFieldValue('enable_round'))
              const composerModeInner =
                (getFieldValue('composer_mode') as PlanComposerMode | undefined) ?? 'simple'
              return (
                <Row gutter={16}>
                  {execType === 'cron' ? (
                    <>
                      <Col xs={24} md={12}>
                        <Form.Item
                          name="cron"
                          label="CRON"
                          rules={[{ required: true, message: 'exec_type=cron 时 cron 必填' }]}
                        >
                          <Input placeholder="例如：0 */5 * * *" />
                        </Form.Item>
                      </Col>
                      <Col xs={24} md={12}>
                        <Form.Item name="timezone" label="时区">
                          <Input placeholder="例如：Asia/Shanghai（可选）" />
                        </Form.Item>
                      </Col>
                    </>
                  ) : null}
                  {execType === 'fixed' ? (
                    <Col xs={24} md={12}>
                      <Form.Item
                        name="scheduled_at"
                        label="开始时间"
                        rules={[{ required: true, message: 'exec_type=fixed 时计划执行时间必填' }]}
                      >
                        <DatePicker showTime style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                  ) : null}
                  {composerModeInner === 'advanced' && enableRoundInner ? (
                    <Col xs={24} md={12}>
                      <Form.Item
                        name="total_round"
                        label="循环次数"
                        rules={[{ required: true, message: '开启轮次编排后请填写循环次数' }]}
                      >
                        <InputNumber min={2} max={20} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                  ) : null}
                </Row>
              )
            }}
          </Form.Item>

          <Form.Item noStyle shouldUpdate={(prev, next) => prev.exec_type !== next.exec_type}>
            {({ getFieldValue }) => {
              const execType = getFieldValue('exec_type') as PlanExecType | undefined
              const matched = EXEC_TYPE_OPTIONS.find(item => item.value === execType)
              if (!matched) {
                return null
              }
              return (
                <Alert
                  className="olh-plan-editor-exec-note"
                  showIcon
                  type="info"
                  style={{ marginBottom: 16 }}
                  message={`当前执行方式：${matched.label}`}
                  description={matched.hint}
                />
              )
            }}
          </Form.Item>

          <Card className="olh-plan-editor-inner-panel" size="small" type="inner" title="计划级元数据">
            <Space direction="vertical" size={10} style={{ width: '100%' }}>
              <Row gutter={16}>
                <Col xs={24} md={12}>
                  <Form.Item name="business_lines" label="显式业务线">
                    <Select
                      mode="multiple"
                      allowClear
                      showSearch={false}
                      loading={businessLineOptionsLoading}
                      disabled={businessLineOptions.length === 0}
                      options={businessLineSelectOptions}
                      placeholder={
                        businessLineOptions.length > 0
                          ? '选择业务线，留空则按已选任务自动汇总'
                          : '暂无业务线配置'
                      }
                    />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item name="collaborator_ids" label="显式协作人">
                    <Select
                      mode="tags"
                      tokenSeparators={[',']}
                      placeholder="输入协作人 ID，留空则按已选任务自动汇总"
                      options={explicitCollaboratorIds.map(value => ({
                        value,
                        label: collaboratorNameMap[value] || value,
                      }))}
                    />
                  </Form.Item>
                </Col>
              </Row>
            </Space>
          </Card>

          <div className="olh-plan-editor-summary" style={compactSummaryStyle}>
            <div style={compactSummaryHeaderStyle}>
              <div>
                <Text strong>计划范围与自动汇总</Text>
                <div>
                  <Text type="secondary">
                    显式值优先，留空按已选任务自动汇总，减少编辑页来回对照。
                  </Text>
                </div>
              </div>
              <Tag>{`阶段 ${composerMode === 'simple' ? simpleStageCount : stageDrafts.length} · 并行项 ${composerMode === 'simple' ? (simpleTask ? 1 : 0) : matrixRows.length} · 任务 ${composerSummary.taskNames.length}`}</Tag>
            </div>
            <div style={summaryGridStyle}>
              <div className="olh-plan-editor-summary-panel" style={summaryPanelStyle}>
                <Space direction="vertical" size={4} style={{ width: '100%' }}>
                  <Text strong>执行方式</Text>
                  <Text type="secondary">
                    {EXEC_TYPE_OPTIONS.find(item => item.value === form.getFieldValue('exec_type'))
                      ?.label || '-'}
                  </Text>
                  <Text type="secondary">
                    {composerMode === 'simple'
                      ? simpleRoundCount > 0
                        ? `简版共 ${simpleRoundCount} 轮`
                        : '等待输入梯度值'
                      : form.getFieldValue('enable_round')
                        ? `共 ${form.getFieldValue('total_round') || 2} 轮`
                        : '当前单轮'}
                  </Text>
                </Space>
              </div>
              <div className="olh-plan-editor-summary-panel" style={summaryPanelStyle}>
                <Space direction="vertical" size={4} style={{ width: '100%' }}>
                  <Text strong>计划级元数据</Text>
                  <Text type="secondary">{`业务线 ${(explicitBusinessLines.length > 0 ? explicitBusinessLines : composerSummary.businessLines).join(' / ') || '-'}`}</Text>
                  <Text type="secondary">{`协作人 ${
                    (explicitCollaboratorIds.length > 0
                      ? explicitCollaboratorIds
                          .map(value => collaboratorNameMap[value] || value)
                          .join(' / ')
                      : composerSummary.collaborators.join(' / ')) || '-'
                  }`}</Text>
                </Space>
              </div>
              <div className="olh-plan-editor-summary-panel" style={summaryPanelStyle}>
                <Space direction="vertical" size={4} style={{ width: '100%' }}>
                  <Text strong>{composerMode === 'simple' ? '简版范围' : '任务覆盖'}</Text>
                  <Text type="secondary">{`环境 ${composerSummary.envs.join(' / ') || '-'}`}</Text>
                  <Text type="secondary">
                    {composerMode === 'simple'
                      ? `节点 ${simplePodCount || SIMPLE_DEFAULT_POD_COUNT} · 间隔 ${simpleIntervalSeconds || 0}s`
                      : `已选任务 ${composerSummary.taskNames.join(' / ') || '待选择'}`}
                  </Text>
                </Space>
              </div>
            </div>
          </div>
        </Card>

        {composerMode === 'simple' ? (
          <Card
            className="olh-dashboard-panel olh-plan-editor-panel olh-plan-composer-panel"
            title="任务设置"
            style={{ marginBottom: 16 }}
            extra={<Text type="secondary">简版只保留单任务、固定时长、固定间隔和固定节点数。</Text>}
          >
            <Space direction="vertical" style={{ width: '100%' }} size={16}>
              <Row gutter={16}>
                <Col xs={24} md={12}>
                  <Form.Item
                    name="simple_task_id"
                    label="选择任务"
                    rules={[
                      {
                        validator: async (_, value) => {
                          if (composerMode !== 'simple') {
                            return
                          }
                          if (!value) {
                            throw new Error('请选择任务')
                          }
                        },
                      },
                    ]}
                  >
                    <Select<number>
                      data-testid="plan-simple-task-select"
                      showSearch
                      allowClear
                      loading={taskListLoading}
                      placeholder="选择任务"
                      filterOption={false}
                      options={taskOptions}
                      onSearch={value => setTaskSearchInput(value)}
                      onChange={value => {
                        setTaskSearchInput('')
                        const selectedTask = value ? taskMap.get(value) : undefined
                        const currentPodCount = Number(
                          form.getFieldValue('simple_pod_count') || SIMPLE_DEFAULT_POD_COUNT
                        )
                        if (selectedTask && currentPodCount === SIMPLE_DEFAULT_POD_COUNT) {
                          form.setFieldValue(
                            'simple_pod_count',
                            getTaskConfiguredPodCount(selectedTask)
                          )
                        }
                      }}
                    />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item name="simple_ramp_type" label="递增方式">
                    <Select
                      data-testid="plan-simple-ramp-select"
                      options={SIMPLE_RAMP_MODE_OPTIONS}
                    />
                  </Form.Item>
                </Col>
                <Col xs={24}>
                  <Form.Item
                    name="simple_gradient_values"
                    label="梯度值"
                    extra={
                      simpleRampType === 'concurrency'
                        ? '输入并发梯度，使用逗号分隔，例如：1,10,20,50,100'
                        : '输入目标 TPS 梯度，使用逗号分隔，例如：10,50,100,200'
                    }
                    rules={[
                      {
                        validator: async (_, value) => {
                          if (composerMode !== 'simple') {
                            return
                          }
                          parseSimpleGradientValues(String(value || ''))
                        },
                      },
                    ]}
                  >
                    <Input
                      data-testid="plan-simple-gradient-input"
                      placeholder={
                        simpleRampType === 'concurrency'
                          ? '例如：1,10,20,50,100'
                          : '例如：10,50,100,200'
                      }
                    />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item
                    name="simple_duration_seconds"
                    label="单轮压测时长（秒）"
                    rules={[{ required: composerMode === 'simple', message: '请输入单轮压测时长' }]}
                  >
                    <InputNumber
                      data-testid="plan-simple-duration-input"
                      min={1}
                      style={{ width: '100%' }}
                    />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item
                    name="simple_interval_seconds"
                    label="轮次间隔（秒）"
                    rules={[{ required: composerMode === 'simple', message: '请输入轮次间隔' }]}
                  >
                    <InputNumber
                      data-testid="plan-simple-interval-input"
                      min={0}
                      style={{ width: '100%' }}
                    />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item
                    name="simple_pod_count"
                    label="节点数量"
                    rules={[{ required: composerMode === 'simple', message: '请输入节点数量' }]}
                  >
                    <InputNumber
                      data-testid="plan-simple-pod-count-input"
                      min={1}
                      style={{ width: '100%' }}
                    />
                  </Form.Item>
                </Col>
              </Row>

              {simpleRampType === 'target_tps' ? (
                <Card className="olh-plan-editor-inner-panel" size="small" type="inner" title="高级项">
                  <Space direction="vertical" size={8} style={{ width: '100%' }}>
                    <Form.Item
                      name="simple_capacity_vus"
                      label={
                        simpleTask?.engine_type === 'k6'
                          ? 'VUs（每节点）'
                          : '承载并发（K6 对应 VUs）'
                      }
                      style={{ marginBottom: 0 }}
                    >
                      <InputNumber
                        data-testid="plan-simple-capacity-input"
                        min={1}
                        style={{ width: '100%' }}
                      />
                    </Form.Item>
                    <Text type="secondary">
                      目标 TPS 表示整轮总目标值；节点数量变化时，系统会按节点数分摊执行。
                    </Text>
                  </Space>
                </Card>
              ) : null}

              {simpleRampType === 'target_tps' && !supportsSimpleTargetTps(simpleTask) ? (
                <Alert
                  className="olh-plan-editor-task-note"
                  type="warning"
                  showIcon
                  message="当前任务引擎暂不支持简版目标 TPS"
                  description="请切换为并发递增，或改用高级编排配置。"
                />
              ) : null}

              {taskOptions.length === 0 ? (
                <Alert
                  type="warning"
                  showIcon
                  message="暂无任务可选，建议先创建任务后再配置计划。"
                />
              ) : null}
              {simpleGradientParseResult.error ? (
                <Alert type="warning" showIcon message={simpleGradientParseResult.error} />
              ) : null}

              <div className="olh-plan-editor-summary" style={compactSummaryStyle}>
                <div data-testid="plan-simple-preview">
                  <div style={compactSummaryHeaderStyle}>
                    <div>
                      <Text strong>简版预览</Text>
                      <div>
                        <Text type="secondary">
                          保存后会自动生成单任务多轮计划，高级阶段编排保持不变。
                        </Text>
                      </div>
                    </div>
                    <Tag>{simpleRoundCount > 0 ? `${simpleRoundCount} 轮` : '待生成'}</Tag>
                  </div>
                  <div style={summaryGridStyle}>
                    <div className="olh-plan-editor-summary-panel" style={summaryPanelStyle}>
                      <Space direction="vertical" size={4} style={{ width: '100%' }}>
                        <Text strong>执行摘要</Text>
                        <Text type="secondary">
                          {simpleTask ? `任务 ${simpleTask.name}` : '待选择任务'}
                        </Text>
                        <Text type="secondary">
                          {simpleRampType === 'concurrency' ? '按并发递增' : '按目标 TPS 递增'}
                        </Text>
                      </Space>
                    </div>
                    <div className="olh-plan-editor-summary-panel" style={summaryPanelStyle}>
                      <Space direction="vertical" size={4} style={{ width: '100%' }}>
                        <Text strong>时间预估</Text>
                        <Text type="secondary">
                          {simpleTotalSeconds > 0 ? `总时长 ${simpleTotalSeconds}s` : '等待梯度值'}
                        </Text>
                        <Text type="secondary">
                          {simpleEstimatedEndAt
                            ? `预计结束 ${simpleEstimatedEndAt.format('YYYY-MM-DD HH:mm:ss')}`
                            : '固定开始时间后可预估结束时间'}
                        </Text>
                      </Space>
                    </div>
                    <div className="olh-plan-editor-summary-panel" style={summaryPanelStyle}>
                      <Space direction="vertical" size={4} style={{ width: '100%' }}>
                        <Text strong>资源设置</Text>
                        <Text type="secondary">{`节点 ${simplePodCount || SIMPLE_DEFAULT_POD_COUNT}`}</Text>
                        <Text type="secondary">
                          {simpleRampType === 'target_tps'
                            ? `VUs ${simpleCapacityVus || SIMPLE_DEFAULT_CAPACITY_VUS}`
                            : `单轮 ${simpleDurationSeconds || SIMPLE_DEFAULT_DURATION_SECONDS}s / 间隔 ${simpleIntervalSeconds || 0}s`}
                        </Text>
                      </Space>
                    </div>
                  </div>

                  {simplePreviewRows.length > 0 ? (
                    <div style={{ overflowX: 'auto', marginTop: 12 }}>
                      <table className="olh-plan-editor-matrix-table" style={roundMatrixTableStyle}>
                        <thead>
                          <tr>
                            <th
                              style={{
                                textAlign: 'left',
                                padding: '8px 12px',
                                borderBottom: MATRIX_BORDER_BOTTOM,
                              }}
                            >
                              轮次
                            </th>
                            <th
                              style={{
                                textAlign: 'left',
                                padding: '8px 12px',
                                borderBottom: MATRIX_BORDER_BOTTOM,
                              }}
                            >
                              {simpleRampType === 'concurrency' ? '并发' : '目标 TPS'}
                            </th>
                            <th
                              style={{
                                textAlign: 'left',
                                padding: '8px 12px',
                                borderBottom: MATRIX_BORDER_BOTTOM,
                              }}
                            >
                              时长
                            </th>
                            <th
                              style={{
                                textAlign: 'left',
                                padding: '8px 12px',
                                borderBottom: MATRIX_BORDER_BOTTOM,
                              }}
                            >
                              间隔
                            </th>
                            <th
                              style={{
                                textAlign: 'left',
                                padding: '8px 12px',
                                borderBottom: MATRIX_BORDER_BOTTOM,
                              }}
                            >
                              节点
                            </th>
                            {simpleRampType === 'target_tps' ? (
                              <th
                                style={{
                                  textAlign: 'left',
                                  padding: '8px 12px',
                                  borderBottom: MATRIX_BORDER_BOTTOM,
                                }}
                              >
                                VUs
                              </th>
                            ) : null}
                          </tr>
                        </thead>
                        <tbody>
                          {simplePreviewRows.map(row => (
                            <tr key={row.key}>
                              <td
                                style={{ padding: '10px 12px', borderBottom: MATRIX_BORDER_BOTTOM }}
                              >{`轮次 ${row.round}`}</td>
                              <td
                                style={{ padding: '10px 12px', borderBottom: MATRIX_BORDER_BOTTOM }}
                              >
                                {row.rampValue}
                              </td>
                              <td
                                style={{ padding: '10px 12px', borderBottom: MATRIX_BORDER_BOTTOM }}
                              >{`${row.durationSeconds}s`}</td>
                              <td
                                style={{ padding: '10px 12px', borderBottom: MATRIX_BORDER_BOTTOM }}
                              >{`${row.intervalSeconds}s`}</td>
                              <td
                                style={{ padding: '10px 12px', borderBottom: MATRIX_BORDER_BOTTOM }}
                              >
                                {row.podCount}
                              </td>
                              {simpleRampType === 'target_tps' ? (
                                <td
                                  style={{
                                    padding: '10px 12px',
                                    borderBottom: MATRIX_BORDER_BOTTOM,
                                  }}
                                >
                                  {row.capacityVus}
                                </td>
                              ) : null}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : null}
                </div>
              </div>
            </Space>
          </Card>
        ) : (
          <Card
            className="olh-dashboard-panel olh-plan-editor-panel olh-plan-composer-panel"
            title="任务设置"
            style={{ marginBottom: 16 }}
            extra={
              <Text type="secondary">
                按阶段顺位自上而下编排；任务参数与后置处理都在当前工作区内完成。
              </Text>
            }
          >
            <Space direction="vertical" style={{ width: '100%' }} size={12}>
              {stageDrafts.map((stage, stageIndex) => (
                <div className="olh-plan-stage-workbench" key={`stage-${stageIndex}`} style={stageWorkbenchStyle}>
                  <div className="olh-plan-stage-header" style={stageWorkbenchHeaderStyle}>
                    <Space direction="vertical" size={6}>
                      <Space wrap size={[8, 8]}>
                        <Text strong>{`阶段${stageIndex}`}</Text>
                        <Tag>{`串行顺位 ${stageIndex + 1}`}</Tag>
                        <Tag>{`${stage.items.filter(item => item.type === 'task').length} 个任务项`}</Tag>
                        <Tag color="purple">{`${stage.items.filter(item => item.type === 'postprocessor').length} 个后置处理`}</Tag>
                      </Space>
                      <Text type="secondary">同一阶段内并行执行，不同阶段串行执行。</Text>
                    </Space>
                    <Space size={4}>
                      <Button
                        type="text"
                        disabled={stageIndex === 0}
                        onClick={() => moveStage(stageIndex, 'up')}
                      >
                        上移
                      </Button>
                      <Button
                        type="text"
                        disabled={stageIndex === stageDrafts.length - 1}
                        onClick={() => moveStage(stageIndex, 'down')}
                      >
                        下移
                      </Button>
                      <Button type="text" danger onClick={() => removeStage(stageIndex)}>
                        删除阶段
                      </Button>
                    </Space>
                  </div>
                  <div className="olh-plan-stage-body" style={stageWorkbenchBodyStyle}>
                    {stage.items.map((item, itemIndex) => (
                      <div className="olh-plan-stage-item" key={`item-${stageIndex}-${itemIndex}`} style={stageItemCardStyle}>
                        <div className="olh-plan-stage-item-grid" style={stageItemTopRowStyle}>
                          <Space direction="vertical" style={{ width: '100%' }} size={10}>
                            <Space wrap>
                              <Select<StageItemDraftType>
                                style={{ width: 140 }}
                                value={item.type}
                                options={[
                                  { label: '任务', value: 'task' },
                                  { label: '后置处理', value: 'postprocessor' },
                                ]}
                                onChange={value => {
                                  updateStageItem(stageIndex, itemIndex, {
                                    type: value,
                                    task_id: value === 'task' ? item.task_id : undefined,
                                    post_template:
                                      value === 'postprocessor'
                                        ? item.post_template || 'sleep'
                                        : 'sleep',
                                  })
                                }}
                              />
                              {item.type === 'task' ? (
                                <Select<number>
                                  showSearch
                                  allowClear
                                  loading={taskListLoading}
                                  style={{ minWidth: 420, flex: 1 }}
                                  placeholder="选择任务"
                                  filterOption={false}
                                  value={item.task_id}
                                  options={taskOptions}
                                  onSearch={value => setTaskSearchInput(value)}
                                  onChange={value => {
                                    setTaskSearchInput('')
                                    updateStageItem(stageIndex, itemIndex, { task_id: value })
                                  }}
                                />
                              ) : (
                                <Tag color="purple">后置处理</Tag>
                              )}
                            </Space>

                            {item.type === 'task' ? (
                              <>
                                <Space wrap>
                                  {renderTaskSummaryTags(item).map(label => (
                                    <Tag key={`${label}-${stageIndex}-${itemIndex}`} color="blue">
                                      {label}
                                    </Tag>
                                  ))}
                                  {item.task_id ? (
                                    <Tag>{`任务 #${item.task_id}`}</Tag>
                                  ) : (
                                    <Tag>待选择任务</Tag>
                                  )}
                                  {item.task_id && taskMap.get(item.task_id)?.env ? (
                                    <Tag>{taskMap.get(item.task_id)?.env}</Tag>
                                  ) : null}
                                  {item.task_id && taskMap.get(item.task_id)?.engine_type ? (
                                    <Tag>
                                      {String(taskMap.get(item.task_id)?.engine_type).toUpperCase()}
                                    </Tag>
                                  ) : null}
                                </Space>
                                <Space wrap>
                                  <Button
                                    icon={<SettingOutlined />}
                                    onClick={() => {
                                      void openTaskConfigModal(stageIndex, itemIndex)
                                    }}
                                  >
                                    任务参数配置
                                  </Button>
                                  <Text type="secondary">
                                    把本阶段任务的并发、时长、总量与标签集中收口。
                                  </Text>
                                </Space>
                              </>
                            ) : (
                              <>
                                <Space wrap>
                                  <Tag color="purple">
                                    {item.post_template === 'sleep'
                                      ? `sleep ${Math.max(1, Math.floor(item.post_sleep_seconds || 1))} 秒`
                                      : '自定义 JSON'}
                                  </Tag>
                                  <Tag>失败策略 {item.on_failure}</Tag>
                                  <Tag>{`重试 ${item.retry_count || 0} 次`}</Tag>
                                </Space>
                                <Space wrap>
                                  <Button
                                    icon={<SettingOutlined />}
                                    onClick={() => openPostprocessorModal(stageIndex, itemIndex)}
                                  >
                                    后置处理配置
                                  </Button>
                                  <Text type="secondary">
                                    默认先用轻量 sleep 配置，只有特殊场景再展开 JSON。
                                  </Text>
                                </Space>
                              </>
                            )}
                          </Space>

                          <div className="olh-plan-stage-actions" style={stageActionColumnStyle}>
                            <Space size={4}>
                              <Button
                                type="text"
                                icon={<ArrowUpOutlined />}
                                disabled={itemIndex === 0}
                                onClick={() => moveParallelItem(stageIndex, itemIndex, 'up')}
                              >
                                上移
                              </Button>
                              <Button
                                type="text"
                                icon={<ArrowDownOutlined />}
                                disabled={itemIndex === stage.items.length - 1}
                                onClick={() => moveParallelItem(stageIndex, itemIndex, 'down')}
                              >
                                下移
                              </Button>
                              <Button
                                type="text"
                                danger
                                icon={<MinusCircleOutlined />}
                                onClick={() => removeParallelItem(stageIndex, itemIndex)}
                              >
                                删除并行项
                              </Button>
                            </Space>
                            <Text type="secondary">{`并行项 ${itemIndex + 1}`}</Text>
                          </div>
                        </div>
                      </div>
                    ))}

                    <Button
                      className="olh-plan-stage-add-button"
                      icon={<PlusOutlined />}
                      onClick={() => addParallelItem(stageIndex)}
                    >
                      增加并行项
                    </Button>
                  </div>
                </div>
              ))}

              <Button className="olh-plan-stage-add-button olh-plan-stage-add-button--stage" icon={<PlusOutlined />} onClick={addStage}>
                增加阶段
              </Button>

              {taskOptions.length === 0 ? (
                <Alert
                  type="warning"
                  showIcon
                  message="暂无任务可选，建议先创建任务后再配置计划。"
                />
              ) : null}
            </Space>
          </Card>
        )}

        {composerMode === 'advanced' ? (
          <Card className="olh-dashboard-panel olh-plan-editor-panel" title="轮次编排" style={{ marginBottom: 16 }}>
            <Form.Item
              noStyle
              shouldUpdate={(prev, next) =>
                prev.enable_round !== next.enable_round || prev.total_round !== next.total_round
              }
            >
              {({ getFieldValue }) => {
                const enableRoundInner = Boolean(getFieldValue('enable_round'))
                const roundCount = Number(getFieldValue('total_round') || totalRound || 0)
                return (
                  <Space direction="vertical" size={12} style={{ width: '100%' }}>
                    <Space wrap>
                      <Tag color={enableRoundInner ? 'green' : 'default'}>
                        {enableRoundInner ? '已开启轮次编排' : '当前单轮'}
                      </Tag>
                      {enableRoundInner ? <Tag>{roundCount} 轮</Tag> : null}
                    </Space>
                    <Text type="secondary">开启后展示循环次数，并在下方按批次序号调整参数。</Text>
                  </Space>
                )
              }}
            </Form.Item>
          </Card>
        ) : null}

        {composerMode === 'advanced' && enableRound ? (
          <Collapse
            className="olh-plan-editor-collapse"
            defaultActiveKey={['advanced-round-matrix']}
            style={{ marginBottom: 16 }}
            items={[
              {
                key: 'advanced-round-matrix',
                label: '高级矩阵',
                extra: <Text type="secondary">按批次序号复用当前阶段项，并支持逐次调整参数。</Text>,
                children: (
                  <div style={{ overflowX: 'auto' }}>
                    <table className="olh-plan-editor-matrix-table" style={roundMatrixTableStyle}>
                      <thead>
                        <tr>
                          <th
                            style={{
                              textAlign: 'left',
                              padding: '8px 12px',
                              borderBottom: MATRIX_BORDER_BOTTOM,
                            }}
                          >
                            任务
                          </th>
                          {Array.from({ length: Number(totalRound || 0) }).map((_, idx) => (
                            <th
                              key={`round-${idx + 1}`}
                              style={{
                                textAlign: 'left',
                                padding: '8px 12px',
                                borderBottom: MATRIX_BORDER_BOTTOM,
                              }}
                            >
                              轮次 {idx + 1}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {matrixRows.map(row => (
                          <tr key={row.key}>
                            <td
                              style={{
                                padding: '10px 12px',
                                borderBottom: MATRIX_BORDER_BOTTOM,
                                whiteSpace: 'nowrap',
                              }}
                            >
                              {row.label}
                            </td>
                            {Array.from({ length: Number(totalRound || 0) }).map((_, idx) => (
                              <td
                                key={`${row.key}-${idx + 1}`}
                                style={{ padding: '10px 12px', borderBottom: MATRIX_BORDER_BOTTOM }}
                              >
                                {row.type === 'task' ? (
                                  <Space size={4} wrap>
                                    <Button
                                      size="small"
                                      type="link"
                                      onClick={() => {
                                        void openTaskConfigModal(
                                          row.stageIndex,
                                          row.itemIndex,
                                          idx + 1
                                        )
                                      }}
                                    >
                                      参数配置
                                    </Button>
                                  </Space>
                                ) : (
                                  <Button
                                    size="small"
                                    type="link"
                                    onClick={() =>
                                      openPostprocessorModal(row.stageIndex, row.itemIndex, idx + 1)
                                    }
                                  >
                                    后置处理配置
                                  </Button>
                                )}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ),
              },
            ]}
          />
        ) : null}
      </Form>

      <Drawer
        title={
          taskModalLocator
            ? `任务参数 - ${renderItemLabel(getItem(taskModalLocator) || createEmptyTaskItem())}`
            : '任务参数'
        }
        open={Boolean(taskModalLocator)}
        onClose={closeTaskConfigModal}
        width={640}
        destroyOnClose
        extra={
          <Button type="primary" onClick={saveTaskConfigModal}>
            确认
          </Button>
        }
      >
        <Space wrap size={[8, 8]} style={{ marginBottom: 16 }}>
          <Tag>{renderItemLabel(getItem(taskModalLocator) || createEmptyTaskItem())}</Tag>
          <Tag
            color={taskModalLocator?.roundNumber ? 'purple' : 'blue'}
          >{`当前设置：${activeTaskRoundLabel}`}</Tag>
          <Tag color={taskParamSourceMeta.color}>{taskParamSourceMeta.tag}</Tag>
          <Text type="secondary">
            {taskModalLocator?.roundNumber
              ? `当前修改会保存为${activeTaskRoundLabel}参数覆盖；未单独设置的字段继续沿用基础模板。`
              : '默认先回填任务默认/最近执行参数，再由当前计划模板覆盖，不会立即启动压测。'}
          </Text>
        </Space>
        <Card size="small" style={{ marginBottom: 16 }}>
          <Space direction="vertical" size={12} style={{ display: 'flex' }}>
            <div>
              <Text strong>运行控制</Text>
              <Text type="secondary" style={{ display: 'block', marginTop: 4 }}>
                {taskParamSourceMeta.text}
              </Text>
            </div>
            <Space size={16} wrap align="start">
              <div>
                <Text type="secondary">节点数量</Text>
                <InputNumber
                  min={1}
                  precision={0}
                  style={{ width: 160, display: 'block', marginTop: 6 }}
                  value={taskParamDraft?.podCount ?? 1}
                  onChange={value =>
                    updateTaskParamDraft({
                      podCount: typeof value === 'number' && value > 0 ? value : 1,
                    })
                  }
                />
              </div>
              <div>
                <Text type="secondary">执行并发 / VUs</Text>
                <InputNumber
                  min={1}
                  precision={0}
                  style={{ width: 160, display: 'block', marginTop: 6 }}
                  value={taskParamDraft?.threadCount ?? 1}
                  onChange={value =>
                    updateTaskParamDraft({
                      threadCount: typeof value === 'number' && value > 0 ? value : 1,
                    })
                  }
                />
              </div>
              <div>
                <Text type="secondary">运行方式</Text>
                <Select
                  value={taskParamDraft?.runMode ?? 'script_default'}
                  style={{ width: 180, display: 'block', marginTop: 6 }}
                  onChange={value => updateTaskParamDraft({ runMode: value as RunMode })}
                  options={[
                    { label: '按脚本默认', value: 'script_default' },
                    { label: '按时长', value: 'duration' },
                    ...(activeTaskSupportsIteration
                      ? [{ label: '按次数', value: 'iterations' }]
                      : []),
                  ]}
                />
              </div>
              {taskParamDraft?.runMode === 'duration' ? (
                <div>
                  <Text type="secondary">执行时长（秒）</Text>
                  <InputNumber
                    min={1}
                    precision={0}
                    style={{ width: 160, display: 'block', marginTop: 6 }}
                    value={taskParamDraft?.durationSeconds ?? 1}
                    onChange={value =>
                      updateTaskParamDraft({
                        durationSeconds: typeof value === 'number' && value > 0 ? value : 1,
                      })
                    }
                  />
                </div>
              ) : null}
              {taskParamDraft?.runMode === 'iterations' ? (
                <div>
                  <Text type="secondary">执行次数</Text>
                  <InputNumber
                    min={1}
                    precision={0}
                    style={{ width: 160, display: 'block', marginTop: 6 }}
                    value={taskParamDraft?.iterations ?? 1}
                    onChange={value =>
                      updateTaskParamDraft({
                        iterations: typeof value === 'number' && value > 0 ? value : 1,
                      })
                    }
                  />
                  {activeTask?.engine_type === 'jmeter' ? (
                    <Text
                      type="secondary"
                      style={{ display: 'block', marginTop: 6, maxWidth: 260 }}
                    >
                      {`JMeter 当前会写成 LoopController.loops=${taskParamDraft?.iterations ?? 1}；总请求量通常约等于 ${taskParamDraft?.iterations ?? 1} × ${taskParamDraft?.threadCount ?? 1} 线程 × 实际执行节点数。`}
                    </Text>
                  ) : null}
                </div>
              ) : null}
            </Space>
          </Space>
        </Card>

        <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 12 }}>
          <Text strong>脚本变量</Text>
          <Button size="small" icon={<PlusOutlined />} onClick={appendTaskParamRow}>
            新增变量
          </Button>
        </Space>

        <Table<ParamRow>
          rowKey="row_id"
          pagination={false}
          size="small"
          dataSource={taskParamDraft?.rows ?? []}
          locale={{
            emptyText: (
              <Empty
                description="暂无可编辑脚本变量，可直接确认，或手动新增变量"
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
                  placeholder="例如 target_tps"
                  value={value}
                  onChange={event =>
                    updateTaskParamRow(record.row_id, { name: event.target.value })
                  }
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
                    updateTaskParamRow(record.row_id, {
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
                        updateTaskParamRow(record.row_id, { value: nextValue === 'true' })
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
                        updateTaskParamRow(record.row_id, {
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
                    onChange={event =>
                      updateTaskParamRow(record.row_id, { value: event.target.value })
                    }
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
                  onClick={() => removeTaskParamRow(record.row_id)}
                />
              ),
            },
          ]}
        />
      </Drawer>

      <Modal
        title={
          postprocessorModalLocator
            ? `阶段${postprocessorModalLocator.stageIndex + 1}的后置处理 - ${activePostprocessorRoundLabel}`
            : '后置处理配置'
        }
        open={Boolean(postprocessorModalLocator)}
        onCancel={() => {
          setPostprocessorModalLocator(null)
          setShowPostprocessorAdvanced(false)
        }}
        onOk={savePostprocessorModal}
        okText="确定"
        destroyOnClose
      >
        <Form form={postprocessorForm} layout="vertical">
          <Space direction="vertical" style={{ width: '100%' }} size={12}>
            <Form.Item label="执行时长" name="post_sleep_seconds">
              <InputNumber min={1} max={86_400} addonAfter="秒" style={{ width: '100%' }} />
            </Form.Item>
            <Text type="secondary">默认只配置 sleep 秒数，必要时再展开高级控制。</Text>
            <Button
              type="link"
              style={{ padding: 0, alignSelf: 'flex-start' }}
              onClick={() => setShowPostprocessorAdvanced(prev => !prev)}
            >
              {showPostprocessorAdvanced ? '收起高级配置' : '展开高级配置'}
            </Button>
          </Space>
          {showPostprocessorAdvanced ? (
            <>
              <Divider style={{ margin: '12px 0' }} />
              <Row gutter={12}>
                <Col span={24}>
                  <Form.Item name="post_template" label="处理模板">
                    <Select
                      options={[
                        { label: 'sleep 模板', value: 'sleep' },
                        { label: '自定义 JSON', value: 'custom' },
                      ]}
                    />
                  </Form.Item>
                </Col>
              </Row>
              <Form.Item
                noStyle
                shouldUpdate={(prev, next) => prev.post_template !== next.post_template}
              >
                {({ getFieldValue }) =>
                  getFieldValue('post_template') === 'custom' ? (
                    <Form.Item name="post_params_json" label="post_params JSON">
                      <TextArea rows={6} placeholder='例如：{"action":"sleep","seconds":300}' />
                    </Form.Item>
                  ) : null
                }
              </Form.Item>
              <Row gutter={12}>
                <Col span={8}>
                  <Form.Item name="retry_count" label="重试次数">
                    <InputNumber min={0} max={10} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="retry_delay_seconds" label="重试间隔(秒)">
                    <InputNumber min={0} max={600} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="on_failure" label="失败策略">
                    <Select
                      options={[
                        { label: 'ABORT', value: 'ABORT' },
                        { label: 'SKIP', value: 'SKIP' },
                        { label: 'CONTINUE', value: 'CONTINUE' },
                      ]}
                    />
                  </Form.Item>
                </Col>
              </Row>
            </>
          ) : null}
        </Form>
      </Modal>
    </div>
  )
}

export default PlanCreateEdit
