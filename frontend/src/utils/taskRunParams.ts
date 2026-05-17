import type { EngineType, Task } from '@/services/taskApi'

export type ParamType = 'int' | 'float' | 'bool' | 'string'
export type RunMode = 'script_default' | 'duration' | 'iterations'
export type RunParamPrimitive = string | number | boolean

export type ParamRow = {
  row_id: string
  name: string
  type: ParamType
  value: RunParamPrimitive
}

const INTEGER_PATTERN = /^-?\d+$/
const FLOAT_PATTERN = /^-?\d+\.\d+$/

const HIDDEN_RUN_PARAM_KEYS = new Set([
  'pod_count',
  'pod_num',
  'pod_total',
  'thread_count',
  'num_threads',
  'threads',
  'vus',
  'duration',
  'duration_seconds',
  'iterations',
  'request_count',
  'loops',
  'run_mode',
  'run_by',
  'total_requests',
  'tag',
  'seed_run_status',
])

const MANAGED_RUN_CONTROL_KEYS = [
  'thread_count',
  'num_threads',
  'threads',
  'vus',
  'duration',
  'duration_seconds',
  'iterations',
  'request_count',
  'loops',
  'run_mode',
  'run_by',
  'total_requests',
] as const

const RUN_MODE_CONTROL_KEYS = [
  'duration',
  'duration_seconds',
  'iterations',
  'request_count',
  'loops',
  'run_mode',
  'run_by',
  'total_requests',
] as const

export const supportsIterationRunMode = (engineType: EngineType | string | null | undefined) => (
  engineType === 'k6' || engineType === 'jmeter'
)

export const createParamRowId = () => `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`

export const isHiddenRunParamKey = (key: string) => (
  HIDDEN_RUN_PARAM_KEYS.has(key.trim().toLowerCase())
)

export const isManagedRunControlParamKey = (key: string) => (
  MANAGED_RUN_CONTROL_KEYS.includes(key.trim().toLowerCase() as typeof MANAGED_RUN_CONTROL_KEYS[number])
)

export const isScalarRunParamValue = (value: unknown): value is RunParamPrimitive => (
  typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'
)

export const inferParamType = (value: unknown): ParamType => {
  if (typeof value === 'boolean') {
    return 'bool'
  }
  if (typeof value === 'number') {
    return Number.isInteger(value) ? 'int' : 'float'
  }
  if (typeof value === 'string') {
    const trimmed = value.trim()
    if (trimmed === 'true' || trimmed === 'false') {
      return 'bool'
    }
    if (INTEGER_PATTERN.test(trimmed)) {
      return 'int'
    }
    if (FLOAT_PATTERN.test(trimmed)) {
      return 'float'
    }
  }
  return 'string'
}

export const coerceParamRowValue = (value: unknown, type: ParamType): RunParamPrimitive => {
  if (type === 'bool') {
    if (typeof value === 'boolean') {
      return value
    }
    return String(value).trim() === 'true'
  }
  if (type === 'int' || type === 'float') {
    if (typeof value === 'number') {
      return value
    }
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : 0
  }
  return typeof value === 'string' ? value : value == null ? '' : String(value)
}

const extractPositiveInt = (raw: unknown): number | null => {
  if (typeof raw === 'number' && Number.isFinite(raw) && raw > 0) {
    return Math.floor(raw)
  }
  if (typeof raw === 'string') {
    const parsed = Number(raw)
    if (Number.isFinite(parsed) && parsed > 0) {
      return Math.floor(parsed)
    }
  }
  return null
}

export const extractPodCount = (params: Record<string, unknown>) => (
  extractPositiveInt(params.pod_count ?? params.pod_num) ?? 1
)

export const extractThreadCount = (params: Record<string, unknown>, fallback?: number | null) => {
  const candidates = [params.thread_count, params.num_threads, params.threads, params.vus, fallback]
  for (const candidate of candidates) {
    const parsed = extractPositiveInt(candidate)
    if (parsed !== null) {
      return parsed
    }
  }
  return 1
}

export const extractDurationSeconds = (params: Record<string, unknown>, fallback?: number | null) => {
  const candidates = [params.duration_seconds, params.duration, fallback]
  for (const candidate of candidates) {
    const parsed = extractPositiveInt(candidate)
    if (parsed !== null) {
      return parsed
    }
  }
  return 60
}

export const extractIterations = (params: Record<string, unknown>) => {
  const candidates = [params.iterations, params.request_count, params.loops, params.total_requests]
  for (const candidate of candidates) {
    const parsed = extractPositiveInt(candidate)
    if (parsed !== null) {
      return parsed
    }
  }
  return 1
}

export const resolveInitialRunMode = (
  engineType: EngineType | string | null | undefined,
  params: Record<string, unknown>,
): RunMode => {
  const explicitMode = String(params.run_mode || '').trim()
  if (explicitMode === 'script_default' || explicitMode === 'duration' || explicitMode === 'iterations') {
    if (explicitMode === 'iterations' && !supportsIterationRunMode(engineType)) {
      return 'duration'
    }
    return explicitMode
  }
  if (
    supportsIterationRunMode(engineType)
    && [params.iterations, params.request_count, params.loops, params.total_requests].some(
      candidate => extractPositiveInt(candidate) !== null,
    )
  ) {
    return 'iterations'
  }
  if (params.duration != null || params.duration_seconds != null) {
    return 'duration'
  }
  return 'script_default'
}

export const buildParamRows = (params: Record<string, unknown>): ParamRow[] =>
  Object.entries(params)
    .filter(([key, value]) => !isHiddenRunParamKey(key) && isScalarRunParamValue(value))
    .map(([name, value]) => {
      const type = inferParamType(value)
      return {
        row_id: createParamRowId(),
        name,
        type,
        value: coerceParamRowValue(value, type),
      }
    })

export const serializeParamRow = (row: ParamRow): RunParamPrimitive => {
  if (row.type === 'bool') {
    return Boolean(row.value)
  }
  if (row.type === 'int' || row.type === 'float') {
    return Number(row.value)
  }
  return String(row.value ?? '')
}

export const normalizeCompatibleRunParams = (
  params: Record<string, unknown>,
  engineType: EngineType | string | null | undefined,
): Record<string, unknown> => {
  const normalized = { ...params }
  if (
    normalized.iterations == null
    && normalized.request_count == null
    && normalized.loops == null
    && normalized.total_requests != null
  ) {
    normalized.iterations = normalized.total_requests
  }
  delete normalized.total_requests
  const runMode = resolveInitialRunMode(engineType, normalized)
  if (runMode === 'script_default') {
    delete normalized.run_mode
  } else {
    normalized.run_mode = runMode
  }
  return normalized
}

export const clearManagedRunControlParams = (target: Record<string, unknown>) => {
  MANAGED_RUN_CONTROL_KEYS.forEach(key => {
    delete target[key]
  })
}

export const clearRunModeControlParams = (target: Record<string, unknown>) => {
  RUN_MODE_CONTROL_KEYS.forEach(key => {
    delete target[key]
  })
}

export const hasRunModeControlParams = (params: Record<string, unknown>) => (
  RUN_MODE_CONTROL_KEYS.some(key => params[key] != null)
)

export const clearEditableScriptVariableParams = (target: Record<string, unknown>) => {
  Object.entries(target).forEach(([key, value]) => {
    if (!isHiddenRunParamKey(key) && isScalarRunParamValue(value)) {
      delete target[key]
    }
  })
}

export const buildTaskFallbackRunParams = (
  task?: Pick<Task, 'properties'> | null,
): Record<string, unknown> => {
  const params: Record<string, unknown> = {}
  const properties = task?.properties
  if (!properties || typeof properties !== 'object') {
    return params
  }

  const rawPodCount = properties.pod_count ?? properties.pod_num
  const podCount = extractPositiveInt(rawPodCount)
  if (podCount !== null) {
    params.pod_count = podCount
  }

  const rawVariables = properties.variables
  if (rawVariables && typeof rawVariables === 'object' && !Array.isArray(rawVariables)) {
    Object.entries(rawVariables).forEach(([key, value]) => {
      if (typeof key === 'string' && key) {
        params[key] = value
      }
    })
  }

  return params
}
