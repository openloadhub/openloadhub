import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Divider,
  Form,
  Input,
  InputNumber,
  Radio,
  Row,
  Select,
  Space,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd'
import { DeleteOutlined, PlusOutlined, RocketOutlined, SettingOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { metaApi, type MetaItem } from '@/services/metaApi'
import {
  scriptApi,
  type CurlToK6ScriptCreateResponse,
  type HarToK6ScriptCreateResponse,
  type OpenApiToK6ScriptCreateResponse,
  type Script,
} from '@/services/scriptApi'
import { taskAssetApi, type TaskAsset } from '@/services/taskAssetApi'
import {
  taskApi,
  type CreateTaskDto,
  type EngineType,
  type Protocol,
  type TaskScriptCompareResponse,
} from '@/services/taskApi'
import StartRunDrawer from '@/components/task/StartRunDrawer'
import TaskAssetPanel from '@/components/task/TaskAssetPanel'
import TaskScriptEditorDrawer from '@/components/task/TaskScriptEditorDrawer'
import TaskVersionDrawer from '@/components/task/TaskVersionDrawer'
import { publicAlphaFeatures } from '@/config/publicAlpha'
import { formatLocalDateTime } from '@/utils/localDateTime'
import ScriptUploadPanel from './components/ScriptUploadPanel'
import AdvancedConfigModal, { type AdvancedConfigValue } from './components/AdvancedConfigModal'
import CurlToK6Modal from './components/CurlToK6Modal'
import HarToK6Modal from './components/HarToK6Modal'
import OpenApiToK6Modal from './components/OpenApiToK6Modal'
import ScriptImportWizardModal, {
  type ScriptImportMode,
} from './components/ScriptImportWizardModal'
import { isManagedRunControlParamKey } from '@/utils/taskRunParams'
import './index.css'

const { Text, Title } = Typography
const { TextArea } = Input
const TASK_FORM_ID = 'task-create-form'

type VariableType = 'string' | 'int' | 'float' | 'boolean'

type VariableRow = {
  id?: string
  key: string
  value: string
  valueType: VariableType
  sensitive?: boolean
  source?: string
  generated?: boolean
}

type TaskTemplatePreset = {
  title: string
  description: string
  formValues: Record<string, unknown>
  variables?: VariableRow[]
}

type RelatedMonitorForm = {
  title?: string
  name?: string
  url?: string
  link?: string
  kind?: string
  embed_mode?: 'new_tab' | 'iframe'
  description?: string
}

type AlertPolicyForm = {
  name?: string
  enabled?: boolean
  auto_stop_enabled?: boolean
  observe_only?: boolean
  source?: string
  match?: {
    subscription?: string
    alertname?: string
    severity?: string[]
  }
  actions?: string[]
  cooldown_seconds?: number
  for_seconds?: number
}

type E2EBridge = {
  setTaskScriptId?: (scriptId: number) => void
}

const DEFAULT_VARIABLE_ROW = (): VariableRow => ({
  id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
  key: '',
  value: '',
  valueType: 'string',
})

const DEFAULT_EXECUTION_THREAD_COUNT = 1
const DEFAULT_EXECUTION_DURATION = 300

const parsePositiveInteger = (value: unknown): number | undefined => {
  if (typeof value === 'number' && Number.isFinite(value) && value > 0) {
    return Math.floor(value)
  }
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value)
    if (Number.isFinite(parsed) && parsed > 0) {
      return Math.floor(parsed)
    }
  }
  return undefined
}

const DEFAULT_SCRIPT_CONFIG = {
  resource_type: 'K8S',
  cloud_vendor: 'AWS',
  pod_count: 1,
  data_distribution: 'all',
} as const
const RESOURCE_TYPE_OPTIONS = ['K8S', 'EC2'] as const
const CLOUD_VENDOR_OPTIONS = ['AWS'] as const

const STANDARD_K6_TEMPLATE_HINTS = [
  'demo-http-k6',
  'demo-grpc-k6',
  'demo-mixed-k6-variable',
  'benchmark-http-k6',
  'benchmark-grpc-k6',
]

const matchesStandardK6Template = (value?: string | null) => {
  const normalized = String(value || '')
    .trim()
    .toLowerCase()
  if (!normalized) {
    return false
  }
  return STANDARD_K6_TEMPLATE_HINTS.some(item => normalized.includes(item))
}

const DEFAULT_FORM_VALUES = {
  task_pattern: 'script',
  protocols: ['http'],
  thread_count: DEFAULT_EXECUTION_THREAD_COUNT,
  duration: DEFAULT_EXECUTION_DURATION,
  ...DEFAULT_SCRIPT_CONFIG,
}

const EXTERNAL_DATA_SOURCE_OPTIONS = [
  { label: '外部数据集', value: 'dataset' },
  { label: '外部造数任务', value: 'generation_task' },
  { label: '外部文件', value: 'file' },
  { label: '外部 API token', value: 'api_token' },
  { label: '其他', value: 'other' },
]

const normalizeExternalDataRef = (value: unknown): Record<string, string> | undefined => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return undefined
  }
  const raw = value as Record<string, unknown>
  const sourceType = typeof raw.source_type === 'string' ? raw.source_type.trim() : ''
  const sourceName = typeof raw.source_name === 'string' ? raw.source_name.trim() : ''
  const referenceId = typeof raw.reference_id === 'string' ? raw.reference_id.trim() : ''
  const referenceUrl = typeof raw.reference_url === 'string' ? raw.reference_url.trim() : ''
  const notes = typeof raw.notes === 'string' ? raw.notes.trim() : ''
  if (!sourceType && !sourceName && !referenceId && !referenceUrl && !notes) {
    return undefined
  }
  return {
    source_type: sourceType || 'other',
    source_name: sourceName,
    reference_id: referenceId,
    reference_url: referenceUrl,
    notes,
    boundary: 'external_only',
  }
}

const RELATED_LINK_SAMPLES = [
  { name: 'Link - 流量链路', url: 'https://trace.example.com/service' },
  { name: 'Link - 任务链路', url: 'https://ops.example.com/link/task' },
]

const TEMPLATE_PRESETS: Record<string, TaskTemplatePreset> = {
  HTTP: {
    title: 'HTTP Demo 模板',
    description:
      '预填 HTTP 协议和典型接口变量，用于快速起步而不是完整脚本生成。并发与持续时间改由脚本变量维护。',
    formValues: {
      engine_type: 'jmeter',
      protocols: ['http'],
      description: 'HTTP Demo 模板：预填基础 HTTP 请求变量和压测参数。',
    },
    variables: [
      { key: 'base_url', value: 'https://example.com', valueType: 'string' },
      { key: 'path', value: '/api/demo', valueType: 'string' },
      { key: 'method', value: 'GET', valueType: 'string' },
    ],
  },
  GRPC: {
    title: 'GRPC Demo 模板',
    description:
      '预填 GRPC 协议、服务名和方法名参数，方便快速建立脚本型 GRPC 任务。并发与持续时间改由脚本变量维护。',
    formValues: {
      engine_type: 'jmeter',
      protocols: ['grpc'],
      description: 'GRPC Demo 模板：预填服务名、方法名与目标地址。',
    },
    variables: [
      { key: 'grpc_host', value: '127.0.0.1:50051', valueType: 'string' },
      { key: 'service_name', value: 'demo.DemoService', valueType: 'string' },
      { key: 'method_name', value: 'Ping', valueType: 'string' },
    ],
  },
  文件读取: {
    title: '文件读取模板',
    description: '预填文件路径和数据读取方式，便于验证脚本中的本地/挂载文件依赖。',
    formValues: {
      engine_type: 'jmeter',
      protocols: ['http'],
      description: '文件读取模板：预填文件路径与批量读取参数。',
    },
    variables: [
      { key: 'file_path', value: '/data/demo.csv', valueType: 'string' },
      { key: 'encoding', value: 'utf-8', valueType: 'string' },
      { key: 'batch_size', value: '100', valueType: 'int' },
    ],
  },
  全局变量: {
    title: '全局变量模板',
    description: '预填全局变量示例，帮助区分脚本中的共享变量与局部参数。',
    formValues: {
      engine_type: 'jmeter',
      protocols: ['http'],
      description: '全局变量模板：预填 token、tenant 等共享参数。',
    },
    variables: [
      { key: 'tenant', value: 'ptp-demo', valueType: 'string' },
      { key: 'token', value: '${GLOBAL_TOKEN}', valueType: 'string' },
      { key: 'region', value: 'cn', valueType: 'string' },
    ],
  },
  上下文变量: {
    title: '上下文变量模板',
    description: '预填上下文变量引用，便于验证前后置步骤之间的变量传递。',
    formValues: {
      engine_type: 'jmeter',
      protocols: ['http'],
      description: '上下文变量模板：预填 request_id / trace_id 等上下文变量。',
    },
    variables: [
      { key: 'request_id', value: '${ctx.request_id}', valueType: 'string' },
      { key: 'trace_id', value: '${ctx.trace_id}', valueType: 'string' },
      { key: 'user_id', value: '${ctx.user_id}', valueType: 'string' },
    ],
  },
  '结果 checks': {
    title: '结果 checks 模板',
    description: '预填结果校验参数，帮助快速建立成功率/状态码等检查规则。',
    formValues: {
      engine_type: 'k6',
      protocols: ['http'],
      description: '结果 checks 模板：预填状态码和错误率检查参数。',
    },
    variables: [
      { key: 'expected_status', value: '200', valueType: 'int' },
      { key: 'max_error_rate', value: '0.01', valueType: 'float' },
      { key: 'check_name', value: 'status-ok', valueType: 'string' },
    ],
  },
  终止阈值: {
    title: '终止阈值模板',
    description: '预填阈值型参数，便于快速验证失败中止或 SLA 门限逻辑。',
    formValues: {
      engine_type: 'k6',
      protocols: ['http'],
      description: '终止阈值模板：预填响应时间和错误率阈值。',
    },
    variables: [
      { key: 'abort_error_rate', value: '0.05', valueType: 'float' },
      { key: 'p95_threshold_ms', value: '800', valueType: 'int' },
      { key: 'abort_after_seconds', value: '120', valueType: 'int' },
    ],
  },
  'nacos-http': {
    title: 'Nacos HTTP — 平台 runtime demo（非业务脚本样例）',
    description:
      '【平台连通性 demo，不是业务脚本样例】通过 xk6-nacos 发现 ptp-agent 并访问 /health，证明 Nacos HTTP 服务发现链路可用。若需业务场景，请上传含 ###{param}### 变量的自定义脚本。',
    formValues: {
      engine_type: 'k6',
      protocols: ['http'],
      description: 'Nacos HTTP runtime demo：通过 xk6-nacos 发现 ptp-agent 并访问 /health。',
    },
    variables: [
      { key: 'service_name', value: 'ptp-agent', valueType: 'string' },
      { key: 'group_name', value: 'DEFAULT_GROUP', valueType: 'string' },
      { key: 'request_path', value: '/health', valueType: 'string' },
    ],
  },
  'nacos-grpc': {
    title: 'Nacos GRPC — 平台 runtime demo（非业务脚本样例）',
    description:
      '【平台连通性 demo，不是业务脚本样例】通过 Nacos 发现 ptp-grpc-demo 并使用镜像内置 hello.proto 发起真实 gRPC 调用，证明 Nacos + gRPC runtime 链路可用。若需业务场景，请上传含 ###{param}### 变量的自定义脚本。',
    formValues: {
      engine_type: 'k6',
      protocols: ['grpc'],
      description: 'Nacos GRPC runtime demo：通过 Nacos 发现 ptp-grpc-demo 并发起 SayHello 调用。',
    },
    variables: [
      { key: 'service_name', value: 'ptp-grpc-demo', valueType: 'string' },
      { key: 'grpc_method', value: 'hello.Hello/SayHello', valueType: 'string' },
      { key: 'proto_file', value: 'hello.proto', valueType: 'string' },
    ],
  },
}

const parseStringArray = (value: unknown): string[] => {
  if (Array.isArray(value)) {
    return value.filter(item => typeof item === 'string' && item.trim()).map(item => item.trim())
  }
  if (typeof value === 'string' && value.trim()) {
    return value
      .split(',')
      .map(item => item.trim())
      .filter(Boolean)
  }
  return []
}

const asRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}

const parseBooleanValue = (value: unknown): boolean | undefined => {
  if (typeof value === 'boolean') {
    return value
  }
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase()
    if (normalized === 'true') {
      return true
    }
    if (normalized === 'false') {
      return false
    }
  }
  return undefined
}

const normalizeRelatedMonitors = (value: unknown, compatibilityUrl?: unknown): RelatedMonitorForm[] => {
  const rawItems = Array.isArray(value) ? value : []
  const monitors: RelatedMonitorForm[] = []
  rawItems.forEach(item => {
    const raw = asRecord(item)
    const title =
      typeof raw.title === 'string'
        ? raw.title.trim()
        : typeof raw.name === 'string'
          ? raw.name.trim()
          : ''
    const url =
      typeof raw.url === 'string'
        ? raw.url.trim()
        : typeof raw.link === 'string'
          ? raw.link.trim()
          : ''
    const kind = typeof raw.kind === 'string' ? raw.kind.trim() : ''
    const description = typeof raw.description === 'string' ? raw.description.trim() : ''
    const embedMode = raw.embed_mode === 'iframe' ? 'iframe' : 'new_tab'
    if (!title && !url && !kind && !description) {
      return
    }
    monitors.push({
      title,
      url,
      kind: kind || 'grafana',
      embed_mode: embedMode,
      description,
    })
  })

  if (monitors.length > 0) {
    return monitors
  }

  const fallbackUrl = typeof compatibilityUrl === 'string' ? compatibilityUrl.trim() : ''
  return fallbackUrl
    ? [{ title: '关联监控', url: fallbackUrl, kind: 'grafana', embed_mode: 'new_tab' }]
    : []
}

const normalizeAlertPolicies = (value: unknown): AlertPolicyForm[] => {
  const rawItems = Array.isArray(value) ? value : []
  const policies: AlertPolicyForm[] = []
  rawItems.forEach(item => {
    const raw = asRecord(item)
    const match = asRecord(raw.match)
    const name = typeof raw.name === 'string' ? raw.name.trim() : ''
    const source = typeof raw.source === 'string' ? raw.source.trim() : ''
    const subscription = typeof match.subscription === 'string' ? match.subscription.trim() : ''
    const alertname = typeof match.alertname === 'string' ? match.alertname.trim() : ''
    const severity = parseStringArray(match.severity)
    const actions = parseStringArray(raw.actions)
    const cooldownSeconds = parsePositiveInteger(raw.cooldown_seconds)
    const forSeconds = parsePositiveInteger(raw.for_seconds)
    const autoStopEnabled = parseBooleanValue(raw.auto_stop_enabled) ?? false
    const observeOnly = parseBooleanValue(raw.observe_only) ?? true
    if (
      !name &&
      !source &&
      !subscription &&
      !alertname &&
      severity.length === 0 &&
      actions.length === 0 &&
      !cooldownSeconds &&
      !forSeconds
    ) {
      return
    }
    policies.push({
      name,
      enabled: raw.enabled !== false,
      auto_stop_enabled: autoStopEnabled,
      observe_only: observeOnly,
      source: source || 'grafana_alertmanager',
      match: {
        ...(subscription ? { subscription } : {}),
        ...(alertname ? { alertname } : {}),
        ...(severity.length > 0 ? { severity } : {}),
      },
      actions,
      ...(cooldownSeconds ? { cooldown_seconds: cooldownSeconds } : {}),
      ...(forSeconds ? { for_seconds: forSeconds } : {}),
    })
  })
  return policies
}

const mergeSuggestedVariables = (
  current: VariableRow[],
  suggestions: Array<{ key: string; value: string; sensitive?: boolean; source?: string | null }>
): VariableRow[] => {
  const existing = current.filter(item => item.key.trim() || item.value.trim())
  const existingKeys = new Set(existing.map(item => item.key.trim()))
  const merged = [...existing]

  suggestions.forEach(item => {
    const key = item.key.trim()
    if (!key || existingKeys.has(key)) {
      return
    }
    merged.push({
      id: DEFAULT_VARIABLE_ROW().id,
      key,
      value: item.value,
      valueType: 'string',
      sensitive: Boolean(item.sensitive),
      source: item.source || undefined,
      generated: true,
    })
    existingKeys.add(key)
  })

  return merged.length > 0 ? merged : [DEFAULT_VARIABLE_ROW()]
}

const ENV_SCOPE_LABELS: Record<NonNullable<MetaItem['scope']>, string> = {
  test: 'demo',
  testnet: 'testnet',
  main: 'mainnet',
}

const deriveEnvType = (env?: string, envOptions: MetaItem[] = []): string => {
  if (!env) {
    return ''
  }
  const matchedEnv = envOptions.find(item => item.code === env)
  if (matchedEnv?.scope) {
    return ENV_SCOPE_LABELS[matchedEnv.scope]
  }
  const normalizedEnv = env.trim().toLowerCase()
  if (['prod', 'mainnet', 'production'].includes(normalizedEnv)) {
    return 'mainnet'
  }
  if (['stg', 'staging', 'testnet', 'preprod', 'pre-release'].includes(normalizedEnv)) {
    return 'testnet'
  }
  return 'demo'
}

const toEngineType = (scriptType?: 'JMETER' | 'K6'): EngineType | undefined => {
  if (scriptType === 'JMETER') {
    return 'jmeter'
  }
  if (scriptType === 'K6') {
    return 'k6'
  }
  return undefined
}

const getEngineScriptMismatchMessage = (engineType?: EngineType, script?: Script | null) => {
  if (!engineType || !script) {
    return undefined
  }
  const scriptEngineType = toEngineType(script.script_type)
  if (!scriptEngineType || scriptEngineType === engineType) {
    return undefined
  }
  if (engineType === 'jmeter') {
    return '当前压测脚本类型为 JMeter，请上传 .jmx 文件'
  }
  if (engineType === 'k6') {
    return '当前压测脚本类型为 K6，请上传 .js 文件'
  }
  return undefined
}

const normalizeCollaboratorIds = (value: unknown): number[] | undefined => {
  if (!Array.isArray(value)) {
    return undefined
  }
  const ids = value.map(item => Number(item)).filter(item => Number.isFinite(item) && item > 0)
  return ids.length > 0 ? ids : undefined
}

const normalizeResourceType = (value: unknown): string => {
  return typeof value === 'string' &&
    RESOURCE_TYPE_OPTIONS.includes(value as (typeof RESOURCE_TYPE_OPTIONS)[number])
    ? value
    : DEFAULT_SCRIPT_CONFIG.resource_type
}

const normalizeCloudVendor = (value: unknown): string => {
  return typeof value === 'string' &&
    CLOUD_VENDOR_OPTIONS.includes(value as (typeof CLOUD_VENDOR_OPTIONS)[number])
    ? value
    : DEFAULT_SCRIPT_CONFIG.cloud_vendor
}

const TaskCreate = () => {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { taskId } = useParams<{ taskId?: string }>()
  const taskIdNum = taskId ? Number(taskId) : undefined
  const [searchParams] = useSearchParams()
  const isEdit = typeof taskIdNum === 'number' && Number.isFinite(taskIdNum) && taskIdNum > 0
  const requestedType = searchParams.get('type')
  const requestedTemplate = searchParams.get('template')
  const [form] = Form.useForm()
  const watchedScriptId = Form.useWatch('script_id', form)
  const selectedEngineType = Form.useWatch('engine_type', form) as EngineType | undefined
  const selectedEnv = Form.useWatch('env', form)
  const selectedProtocols = Form.useWatch('protocols', form) as Protocol[] | undefined
  const publicAlphaMode = publicAlphaFeatures.publicAlphaMode
  const scriptImportModes = useMemo(
    () => (publicAlphaMode ? (['curl', 'har'] as ScriptImportMode[]) : undefined),
    [publicAlphaMode]
  )

  const [envOptions, setEnvOptions] = useState<MetaItem[]>([])
  const [businessOptions, setBusinessOptions] = useState<MetaItem[]>([])
  const [advancedModalOpen, setAdvancedModalOpen] = useState(false)
  const [scriptImportWizardOpen, setScriptImportWizardOpen] = useState(false)
  const [curlToK6Open, setCurlToK6Open] = useState(false)
  const [openApiToK6Open, setOpenApiToK6Open] = useState(false)
  const [harToK6Open, setHarToK6Open] = useState(false)
  const [scriptEditorOpen, setScriptEditorOpen] = useState(false)
  const [startDrawerOpen, setStartDrawerOpen] = useState(false)
  const [versionDrawerOpen, setVersionDrawerOpen] = useState(false)
  const [previewCompareData, setPreviewCompareData] = useState<TaskScriptCompareResponse | null>(
    null
  )
  const [tempProtoAssets, setTempProtoAssets] = useState<TaskAsset[]>([])
  const [tempDataAssets, setTempDataAssets] = useState<TaskAsset[]>([])
  const [advancedConfig, setAdvancedConfig] = useState<AdvancedConfigValue>({ headers: [] })
  const [variables, setVariables] = useState<VariableRow[]>([DEFAULT_VARIABLE_ROW()])
  const [scriptValidationMessage, setScriptValidationMessage] = useState<string>()
  const [appliedTemplate, setAppliedTemplate] = useState<string>()
  const grpcProtocolSelected = useMemo(
    () =>
      Array.isArray(selectedProtocols) &&
      selectedProtocols.some(item => String(item).toLowerCase() === 'grpc'),
    [selectedProtocols]
  )
  const selectedScriptId = useMemo(() => {
    if (
      typeof watchedScriptId === 'number' &&
      Number.isFinite(watchedScriptId) &&
      watchedScriptId > 0
    ) {
      return watchedScriptId
    }
    if (typeof watchedScriptId === 'string' && watchedScriptId.trim()) {
      const normalizedId = Number(watchedScriptId)
      if (Number.isFinite(normalizedId) && normalizedId > 0) {
        return normalizedId
      }
    }
    return undefined
  }, [watchedScriptId])

  const handleCancel = () => {
    if (typeof window !== 'undefined' && window.history.length > 1) {
      navigate(-1)
      return
    }
    navigate('/my-focus/focus-task')
  }

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

  const { data: taskDetail, isLoading: detailLoading } = useQuery({
    queryKey: ['task', taskIdNum],
    queryFn: () => taskApi.getTaskDetail(taskIdNum!),
    enabled: isEdit,
  })

  const collaboratorOptions = useMemo(() => {
    const ids = Array.isArray(taskDetail?.collaborator_ids) ? taskDetail.collaborator_ids : []
    const names = Array.isArray(taskDetail?.collaborator_names) ? taskDetail.collaborator_names : []
    return ids.map((id, index) => {
      const name = names[index]
      return {
        value: String(id),
        label: typeof name === 'string' && name.trim() ? name.trim() : `user#${id}`,
      }
    })
  }, [taskDetail?.collaborator_ids, taskDetail?.collaborator_names])

  const { data: versionData } = useQuery({
    queryKey: ['task-versions-badge', taskIdNum],
    queryFn: () => taskApi.getTaskVersions(taskIdNum!, { page: 1, pageSize: 20 }),
    enabled: isEdit,
  })

  const { data: selectedScript } = useQuery({
    queryKey: ['script', selectedScriptId],
    queryFn: () => scriptApi.getScriptDetail(selectedScriptId!),
    enabled: selectedScriptId !== undefined,
  })

  useEffect(() => {
    if (!isEdit) {
      form.setFieldsValue(DEFAULT_FORM_VALUES)
    }
  }, [form, isEdit])

  useEffect(() => {
    if (isEdit) {
      return
    }
    if (!requestedTemplate) {
      setAppliedTemplate(undefined)
      return
    }
    const preset = TEMPLATE_PRESETS[requestedTemplate]
    if (!preset) {
      setAppliedTemplate(undefined)
      return
    }
    form.setFieldsValue({
      ...DEFAULT_FORM_VALUES,
      ...preset.formValues,
    })
    setVariables(
      preset.variables && preset.variables.length > 0
        ? preset.variables.map(item => ({ ...item, id: DEFAULT_VARIABLE_ROW().id }))
        : [DEFAULT_VARIABLE_ROW()]
    )
    setAdvancedConfig({ headers: [] })
    setAppliedTemplate(requestedTemplate)
  }, [form, isEdit, requestedTemplate])

  useEffect(() => {
    if (!taskDetail) {
      return
    }
    const properties =
      taskDetail.properties && typeof taskDetail.properties === 'object'
        ? (taskDetail.properties as Record<string, unknown>)
        : {}
    const variablesMap =
      properties.variables &&
      typeof properties.variables === 'object' &&
      !Array.isArray(properties.variables)
        ? (properties.variables as Record<string, unknown>)
        : {}
    const variableTypes =
      properties.variable_types &&
      typeof properties.variable_types === 'object' &&
      !Array.isArray(properties.variable_types)
        ? (properties.variable_types as Record<string, unknown>)
        : {}
    const variableMeta =
      properties.variable_meta &&
      typeof properties.variable_meta === 'object' &&
      !Array.isArray(properties.variable_meta)
        ? (properties.variable_meta as Record<string, unknown>)
        : {}
    const variableRows = Object.entries(variablesMap)
      .filter(([key]) => !isManagedRunControlParamKey(key))
      .map(([key, value]) => ({
        id: DEFAULT_VARIABLE_ROW().id,
        key,
        value: String(value),
        valueType:
          typeof variableTypes[key] === 'string' ? (variableTypes[key] as VariableType) : 'string',
        sensitive: Boolean(
          variableMeta[key] &&
          typeof variableMeta[key] === 'object' &&
          !Array.isArray(variableMeta[key]) &&
          (variableMeta[key] as Record<string, unknown>).sensitive
        ),
        source:
          variableMeta[key] &&
          typeof variableMeta[key] === 'object' &&
          !Array.isArray(variableMeta[key]) &&
          typeof (variableMeta[key] as Record<string, unknown>).source === 'string'
            ? ((variableMeta[key] as Record<string, unknown>).source as string)
            : undefined,
        generated: Boolean(
          variableMeta[key] &&
          typeof variableMeta[key] === 'object' &&
          !Array.isArray(variableMeta[key]) &&
          (variableMeta[key] as Record<string, unknown>).generated
        ),
      }))

    const httpConfig =
      properties.http_config &&
      typeof properties.http_config === 'object' &&
      !Array.isArray(properties.http_config)
        ? (properties.http_config as Record<string, unknown>)
        : {}
    const rawHeaders =
      httpConfig.headers &&
      typeof httpConfig.headers === 'object' &&
      !Array.isArray(httpConfig.headers)
        ? (httpConfig.headers as Record<string, unknown>)
        : {}
    const externalDataRef =
      properties.external_data_ref &&
      typeof properties.external_data_ref === 'object' &&
      !Array.isArray(properties.external_data_ref)
        ? (properties.external_data_ref as Record<string, unknown>)
        : {}
    const compatibilityMonitorUrl =
      taskDetail.monitor_link ??
      (typeof properties.monitor_link === 'string'
        ? properties.monitor_link
        : typeof properties.monitor_dashboard_url === 'string'
          ? properties.monitor_dashboard_url
          : undefined)

    form.setFieldsValue({
      name: taskDetail.name,
      description: taskDetail.description,
      env: taskDetail.env,
      engine_type: taskDetail.engine_type,
      task_pattern: 'script',
      protocols: taskDetail.protocols,
      script_id: taskDetail.script_id,
      thread_count: taskDetail.thread_count,
      duration: taskDetail.duration,
      ramp_up: taskDetail.ramp_up,
      business_line:
        taskDetail.business_line ??
        (typeof properties.business_line === 'string' ? properties.business_line : undefined),
      collaborator_ids: Array.isArray(taskDetail.collaborator_ids)
        ? taskDetail.collaborator_ids.map(String)
        : [],
      related_apps:
        taskDetail.related_apps ??
        parseStringArray(properties.related_apps),
      alert_subscriptions: parseStringArray(properties.alert_subscriptions),
      related_monitors: normalizeRelatedMonitors(properties.related_monitors, compatibilityMonitorUrl),
      alert_policies: normalizeAlertPolicies(properties.alert_policies),
      monitor_link: compatibilityMonitorUrl,
      trace_link:
        taskDetail.trace_link ??
        (typeof properties.trace_link === 'string' ? properties.trace_link : undefined),
      resource_type: normalizeResourceType(taskDetail.resource_type ?? properties.resource_type),
      cloud_vendor: normalizeCloudVendor(taskDetail.cloud_vendor ?? properties.cloud_vendor),
      pod_count:
        typeof taskDetail.pod_count === 'number'
          ? taskDetail.pod_count
          : typeof properties.pod_count === 'number'
            ? properties.pod_count
            : typeof properties.pod_num === 'number'
              ? properties.pod_num
              : DEFAULT_SCRIPT_CONFIG.pod_count,
      data_distribution:
        taskDetail.data_distribution ??
        (typeof properties.data_distribution === 'string'
          ? properties.data_distribution
          : DEFAULT_SCRIPT_CONFIG.data_distribution),
      external_data_ref: {
        source_type:
          typeof externalDataRef.source_type === 'string' ? externalDataRef.source_type : undefined,
        source_name:
          typeof externalDataRef.source_name === 'string' ? externalDataRef.source_name : undefined,
        reference_id:
          typeof externalDataRef.reference_id === 'string'
            ? externalDataRef.reference_id
            : undefined,
        reference_url:
          typeof externalDataRef.reference_url === 'string'
            ? externalDataRef.reference_url
            : undefined,
        notes: typeof externalDataRef.notes === 'string' ? externalDataRef.notes : undefined,
      },
    })

    setVariables(variableRows.length > 0 ? variableRows : [DEFAULT_VARIABLE_ROW()])
    setAdvancedConfig({
      headers: Object.entries(rawHeaders).map(([key, value]) => ({ key, value: String(value) })),
      connect_timeout_ms:
        typeof httpConfig.connect_timeout_ms === 'number'
          ? httpConfig.connect_timeout_ms
          : undefined,
      response_timeout_ms:
        typeof httpConfig.response_timeout_ms === 'number'
          ? httpConfig.response_timeout_ms
          : undefined,
      target_url: typeof httpConfig.target_url === 'string' ? httpConfig.target_url : undefined,
    })
  }, [form, taskDetail])

  useEffect(() => {
    const mismatchMessage = getEngineScriptMismatchMessage(selectedEngineType, selectedScript)
    if (!mismatchMessage) {
      return
    }
    form.setFieldValue('script_id', undefined)
    form.setFields([{ name: 'script_id', value: undefined, errors: [] }])
    setScriptValidationMessage(mismatchMessage)
    setPreviewCompareData(null)
  }, [form, selectedEngineType, selectedScript])

  useEffect(() => {
    if (searchParams.get('focus') === 'script' && selectedScriptId !== undefined) {
      setPreviewCompareData(null)
      setScriptEditorOpen(true)
    }
  }, [searchParams, selectedScriptId])

  useEffect(() => {
    if (typeof window === 'undefined') {
      return
    }
    const e2eBridgeHost = window as Window & { __PTP_E2E__?: E2EBridge }
    const e2eBridge = e2eBridgeHost.__PTP_E2E__ ?? {}
    e2eBridge.setTaskScriptId = (scriptId: number) => {
      form.setFieldValue('script_id', scriptId)
      form.setFields([{ name: 'script_id', value: scriptId, errors: [] }])
      setScriptValidationMessage(undefined)
    }
    e2eBridgeHost.__PTP_E2E__ = e2eBridge

    return () => {
      if (e2eBridgeHost.__PTP_E2E__) {
        delete e2eBridgeHost.__PTP_E2E__.setTaskScriptId
      }
    }
  }, [form])

  const mutation = useMutation({
    mutationFn: (data: CreateTaskDto) => {
      if (isEdit) {
        return taskApi.updateTask(taskIdNum!, data)
      }
      return taskApi.createTask(data)
    },
    onSuccess: async (task, data) => {
      try {
        const savedProtocols = Array.isArray(data.protocols) ? data.protocols : []
        const savedWithGrpc = savedProtocols.some(item => String(item).toLowerCase() === 'grpc')
        if (!isEdit) {
          const protoAssetsToBind = savedWithGrpc ? tempProtoAssets : []
          const assetIds = [...protoAssetsToBind, ...tempDataAssets].map(item => item.id)
          if (assetIds.length > 0) {
            await taskAssetApi.bindAssets(task.id, assetIds)
          }
        } else if (!savedWithGrpc) {
          const protoAssets = await taskAssetApi.listAssets(task.id, 'proto')
          await Promise.all(protoAssets.map(asset => taskAssetApi.deleteAsset(asset.id)))
        }
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: ['task', task.id] }),
          queryClient.invalidateQueries({ queryKey: ['task-assets', task.id, 'proto'] }),
          queryClient.invalidateQueries({ queryKey: ['task-assets', task.id, 'data'] }),
          queryClient.invalidateQueries({ queryKey: ['task-versions-badge', task.id] }),
          queryClient.invalidateQueries({ queryKey: ['task-versions', task.id] }),
          queryClient.invalidateQueries({ queryKey: ['tasks'] }),
          queryClient.invalidateQueries({ queryKey: ['tasks-summary'] }),
          queryClient.invalidateQueries({ queryKey: ['dashboard-tasks'] }),
          queryClient.invalidateQueries({ queryKey: ['dashboard-task-summary'] }),
          queryClient.invalidateQueries({ queryKey: ['runs'] }),
          queryClient.invalidateQueries({ queryKey: ['dashboard-runs-count'] }),
        ])
        message.success('保存成功')
        navigate('/my-focus/focus-task')
      } catch (error) {
        message.error(
          error instanceof Error
            ? `任务已保存，但附件处理失败：${error.message}`
            : '任务已保存，但附件处理失败'
        )
      }
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '操作失败')
    },
  })
  const deleteScriptMutation = useMutation({
    mutationFn: (scriptId: number) => scriptApi.deleteScript(scriptId),
    onSuccess: () => {
      clearSelectedScript('脚本已删除，可重新上传')
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '脚本删除失败')
    },
  })

  const variableRows = useMemo(
    () => variables.map((item, index) => ({ ...item, index })),
    [variables]
  )
  const isSubmitting = mutation.isPending || detailLoading
  const currentVersionEntry =
    versionData?.items?.find(item => item.is_current) ?? versionData?.items?.[0] ?? null
  const selectedScriptVersion = selectedScript?.version ?? null
  const currentScriptUpdatedAt =
    selectedScript?.updated_at ??
    currentVersionEntry?.updated_at ??
    currentVersionEntry?.created_at ??
    null
  const savedScriptId = taskDetail?.script_id
  const currentScriptName = useMemo(() => {
    if (selectedScript?.name) {
      return selectedScript.name
    }
    if (
      typeof savedScriptId === 'number' &&
      typeof selectedScriptId === 'number' &&
      savedScriptId === selectedScriptId
    ) {
      return taskDetail?.script_name ?? null
    }
    return null
  }, [savedScriptId, selectedScript?.name, selectedScriptId, taskDetail?.script_name])
  const isSavedScriptSelection = Boolean(
    isEdit &&
    typeof savedScriptId === 'number' &&
    typeof selectedScriptId === 'number' &&
    savedScriptId === selectedScriptId
  )
  const hasUnsavedScriptSelection = Boolean(
    isEdit &&
    typeof savedScriptId === 'number' &&
    typeof selectedScriptId === 'number' &&
    savedScriptId !== selectedScriptId
  )
  const aiFeaturesEnabled = publicAlphaFeatures.aiFeatures
  const isStandardK6Template = matchesStandardK6Template(currentScriptName)
  const generatedScriptSource =
    typeof selectedScript?.parameters?.generated_from === 'string'
      ? selectedScript.parameters.generated_from.trim().toLowerCase()
      : null
  const isCurlGeneratedK6Script = generatedScriptSource === 'curl'
  const isOpenApiGeneratedK6Script = generatedScriptSource === 'openapi'
  const isHarGeneratedK6Script = generatedScriptSource === 'har'
  const isGeneratedK6Script =
    isCurlGeneratedK6Script || isOpenApiGeneratedK6Script || isHarGeneratedK6Script
  const isManagedK6Template = isStandardK6Template || isGeneratedK6Script
  const generatedScriptMethod =
    typeof selectedScript?.parameters?.http_method === 'string'
      ? selectedScript.parameters.http_method.trim().toUpperCase()
      : null
  const generatedScriptBodyMode =
    typeof selectedScript?.parameters?.body_mode === 'string'
      ? selectedScript.parameters.body_mode.trim()
      : null
  const generatedScriptContentType =
    typeof selectedScript?.parameters?.request_content_type === 'string'
      ? selectedScript.parameters.request_content_type.trim()
      : typeof selectedScript?.parameters?.mime_type === 'string'
        ? selectedScript.parameters.mime_type.trim()
        : null
  const generatedScriptResponseTimeout =
    typeof selectedScript?.parameters?.response_timeout_ms === 'number'
      ? selectedScript.parameters.response_timeout_ms
      : null
  const generatedScriptSourceUrl =
    typeof selectedScript?.parameters?.source_url === 'string'
      ? selectedScript.parameters.source_url.trim()
      : null
  const generatedScriptAiReviewStatus =
    aiFeaturesEnabled && typeof selectedScript?.parameters?.ai_review_status === 'string'
      ? selectedScript.parameters.ai_review_status.trim()
      : null
  const generatedScriptHumanConfirmationRequired =
    selectedScript?.parameters?.human_confirmation_required === true
  const generatedScriptHumanConfirmationStatus =
    typeof selectedScript?.parameters?.human_confirmation_status === 'string'
      ? selectedScript.parameters.human_confirmation_status.trim()
      : null
  const generatedScriptFinalStatus =
    typeof selectedScript?.parameters?.final_script_status === 'string'
      ? selectedScript.parameters.final_script_status.trim()
      : null
  const clearSelectedScript = (successMessage: string) => {
    form.setFieldValue('script_id', undefined)
    form.setFields([{ name: 'script_id', value: undefined, errors: [] }])
    setPreviewCompareData(null)
    setScriptEditorOpen(false)
    setScriptValidationMessage(undefined)
    message.success(successMessage)
  }

  const applyGeneratedK6Script = ({
    scriptId,
    suggestedTaskName,
    targetUrl,
    suggestedVariables,
    connectTimeoutMs,
    responseTimeoutMs,
  }: {
    scriptId: number
    suggestedTaskName?: string | null
    targetUrl?: string | null
    suggestedVariables: Array<{
      key: string
      value: string
      sensitive: boolean
      source?: string | null
    }>
    connectTimeoutMs?: number | null
    responseTimeoutMs?: number | null
  }) => {
    form.setFieldValue('script_id', scriptId)
    form.setFieldValue('engine_type', 'k6')
    form.setFieldValue('protocols', ['http'])
    form.setFields([{ name: 'script_id', value: scriptId, errors: [] }])
    setScriptValidationMessage(undefined)
    setPreviewCompareData(null)
    setScriptEditorOpen(false)
    if (!String(form.getFieldValue('name') || '').trim() && suggestedTaskName) {
      form.setFieldValue('name', suggestedTaskName)
    }
    setAdvancedConfig(prev => ({
      ...prev,
      connect_timeout_ms: connectTimeoutMs ?? prev.connect_timeout_ms,
      response_timeout_ms: responseTimeoutMs ?? prev.response_timeout_ms,
      target_url: targetUrl || prev.target_url,
    }))
    setVariables(prev => mergeSuggestedVariables(prev, suggestedVariables))
  }

  const handleCurlToK6Success = (result: CurlToK6ScriptCreateResponse) => {
    applyGeneratedK6Script({
      scriptId: result.script.id,
      suggestedTaskName: result.parsed.suggested_task_name,
      targetUrl: result.parsed.url,
      connectTimeoutMs: result.parsed.connect_timeout_ms,
      responseTimeoutMs: result.parsed.response_timeout_ms,
      suggestedVariables: result.suggested_variables.map(item => ({
        key: item.key,
        value: item.value,
        sensitive: item.sensitive,
        source: item.source,
      })),
    })
    setCurlToK6Open(false)
    message.success('已从 CURL 生成 K6 脚本')
    if (result.warnings.length > 0) {
      message.info(result.warnings.slice(0, 2).join('；'))
    }
  }

  const handleOpenApiToK6Success = (result: OpenApiToK6ScriptCreateResponse) => {
    applyGeneratedK6Script({
      scriptId: result.script.id,
      suggestedTaskName: result.parsed.suggested_task_name,
      targetUrl: result.parsed.source_url,
      suggestedVariables: result.suggested_variables.map(item => ({
        key: item.key,
        value: item.value,
        sensitive: item.sensitive,
        source: item.source,
      })),
    })
    setOpenApiToK6Open(false)
    message.success('已从 OpenAPI 生成 K6 草稿脚本')
    if (result.warnings.length > 0) {
      message.info(result.warnings.slice(0, 2).join('；'))
    }
  }

  const handleHarToK6Success = (result: HarToK6ScriptCreateResponse) => {
    applyGeneratedK6Script({
      scriptId: result.script.id,
      suggestedTaskName: result.parsed.suggested_task_name,
      targetUrl: result.parsed.url,
      suggestedVariables: result.suggested_variables.map(item => ({
        key: item.key,
        value: item.value,
        sensitive: item.sensitive,
        source: item.source,
      })),
    })
    setHarToK6Open(false)
    message.success('已从 HAR 生成 K6 草稿脚本')
    if (result.warnings.length > 0) {
      message.info(result.warnings.slice(0, 2).join('；'))
    }
  }

  const handleScriptImportWizardSuccess = (
    mode: ScriptImportMode,
    result:
      | CurlToK6ScriptCreateResponse
      | OpenApiToK6ScriptCreateResponse
      | HarToK6ScriptCreateResponse
      | Script
  ) => {
    if (mode === 'curl') {
      handleCurlToK6Success(result as CurlToK6ScriptCreateResponse)
    } else if (mode === 'openapi') {
      handleOpenApiToK6Success(result as OpenApiToK6ScriptCreateResponse)
    } else if (mode === 'ai') {
      const script = result as Script
      applyGeneratedK6Script({
        scriptId: script.id,
        suggestedTaskName: script.name,
        suggestedVariables: [],
      })
      message.success('K6 草稿已人工确认并保存为脚本')
    } else {
      handleHarToK6Success(result as HarToK6ScriptCreateResponse)
    }
    setScriptImportWizardOpen(false)
  }

  const handleDownloadScript = async () => {
    if (!selectedScriptId || !selectedScript) {
      return
    }
    try {
      const blob = await scriptApi.downloadScript(selectedScriptId)
      const url = window.URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = currentScriptName || selectedScript.name
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.URL.revokeObjectURL(url)
    } catch (error) {
      message.error(error instanceof Error ? error.message : '下载脚本失败')
    }
  }

  const handleVariableChange = (index: number, patch: Partial<VariableRow>) => {
    setVariables(prev =>
      prev.map((item, current) => (current === index ? { ...item, ...patch } : item))
    )
  }

  const handleVariableAdd = () => {
    setVariables(prev => [...prev, DEFAULT_VARIABLE_ROW()])
  }

  const handleVariableDelete = (index: number) => {
    setVariables(prev => {
      if (prev.length === 1) {
        return [DEFAULT_VARIABLE_ROW()]
      }
      return prev.filter((_, current) => current !== index)
    })
  }

  const openCurrentScriptEditor = () => {
    setPreviewCompareData(null)
    setScriptEditorOpen(true)
  }

  const closeScriptEditor = () => {
    setScriptEditorOpen(false)
    setPreviewCompareData(null)
  }

  const handleSubmit = async () => {
    try {
      setScriptValidationMessage(undefined)
      const values = await form.validateFields()
      const mismatchMessage = getEngineScriptMismatchMessage(values.engine_type, selectedScript)
      if (mismatchMessage) {
        form.setFieldValue('script_id', undefined)
        form.setFields([{ name: 'script_id', value: undefined, errors: [mismatchMessage] }])
        setScriptValidationMessage(mismatchMessage)
        setPreviewCompareData(null)
        return
      }

      const existingProperties =
        isEdit &&
        taskDetail?.properties &&
        typeof taskDetail.properties === 'object' &&
        !Array.isArray(taskDetail.properties)
          ? { ...(taskDetail.properties as Record<string, unknown>) }
          : {}
      const properties: Record<string, unknown> = existingProperties
      const setProperty = (key: string, value: unknown) => {
        if (value === undefined || value === null) {
          delete properties[key]
          return
        }
        if (typeof value === 'string') {
          const normalized = value.trim()
          if (!normalized) {
            delete properties[key]
            return
          }
          properties[key] = normalized
          return
        }
        if (Array.isArray(value)) {
          if (value.length === 0) {
            delete properties[key]
            return
          }
          properties[key] = value
          return
        }
        properties[key] = value
      }
      const variableMap: Record<string, string> = {}
      const variableTypeMap: Record<string, VariableType> = {}
      const variableMetaMap: Record<
        string,
        { sensitive?: boolean; source?: string; generated?: boolean }
      > = {}
      variables.forEach(item => {
        const key = item.key.trim()
        const value = item.value.trim()
        if (!key || !value) {
          return
        }
        if (isManagedRunControlParamKey(key)) {
          return
        }
        variableMap[key] = value
        variableTypeMap[key] = item.valueType
        if (item.sensitive || item.source || item.generated) {
          variableMetaMap[key] = {
            ...(item.sensitive ? { sensitive: true } : {}),
            ...(item.source ? { source: item.source } : {}),
            ...(item.generated ? { generated: true } : {}),
          }
        }
      })
      if (Object.keys(variableMap).length > 0) {
        setProperty('variables', variableMap)
        setProperty('variable_types', variableTypeMap)
        setProperty('variable_meta', variableMetaMap)
      } else {
        delete properties.variables
        delete properties.variable_types
        delete properties.variable_meta
      }

      const normalizedHeaders = advancedConfig.headers
        .filter(item => item.key.trim() && item.value.trim())
        .map(item => ({ key: item.key.trim(), value: item.value.trim() }))
      if (publicAlphaMode) {
        if (normalizedHeaders.length > 0) {
          setProperty('http_config', {
            headers: Object.fromEntries(normalizedHeaders.map(item => [item.key, item.value])),
          })
        } else {
          delete properties.http_config
        }
      } else if (
        normalizedHeaders.length > 0 ||
        typeof advancedConfig.connect_timeout_ms === 'number' ||
        typeof advancedConfig.response_timeout_ms === 'number' ||
        advancedConfig.target_url?.trim()
      ) {
        setProperty('http_config', {
          ...(normalizedHeaders.length > 0
            ? { headers: Object.fromEntries(normalizedHeaders.map(item => [item.key, item.value])) }
            : {}),
          ...(typeof advancedConfig.connect_timeout_ms === 'number'
            ? { connect_timeout_ms: advancedConfig.connect_timeout_ms }
            : {}),
          ...(typeof advancedConfig.response_timeout_ms === 'number'
            ? { response_timeout_ms: advancedConfig.response_timeout_ms }
            : {}),
          ...(advancedConfig.target_url?.trim()
            ? { target_url: advancedConfig.target_url.trim() }
            : {}),
        })
      } else {
        delete properties.http_config
      }

      setProperty('business_line', values.business_line)
      if (publicAlphaMode) {
        delete properties.related_apps
        delete properties.alert_subscriptions
        delete properties.alert_policies
      } else {
        setProperty('related_apps', parseStringArray(values.related_apps))
        setProperty('alert_subscriptions', parseStringArray(values.alert_subscriptions))
        setProperty('alert_policies', normalizeAlertPolicies(values.alert_policies))
      }
      const relatedMonitors = normalizeRelatedMonitors(values.related_monitors)
      const firstMonitorUrl = relatedMonitors[0]?.url
      setProperty('related_monitors', relatedMonitors)
      setProperty('monitor_link', firstMonitorUrl)
      setProperty('monitor_dashboard_url', firstMonitorUrl)
      setProperty('trace_link', values.trace_link)
      const resourceType = normalizeResourceType(values.resource_type)
      const cloudVendor = normalizeCloudVendor(values.cloud_vendor)
      setProperty('resource_type', resourceType)
      setProperty('cloud_vendor', cloudVendor)
      setProperty('pod_count', values.pod_count)
      setProperty('pod_num', values.pod_count)
      setProperty('data_distribution', values.data_distribution)
      if (publicAlphaMode) {
        delete properties.external_data_ref
      } else {
        const externalDataRef = normalizeExternalDataRef(values.external_data_ref)
        if (externalDataRef) {
          setProperty('external_data_ref', externalDataRef)
        } else {
          delete properties.external_data_ref
        }
      }

      const threadCount =
        parsePositiveInteger(values.thread_count) ??
        parsePositiveInteger(taskDetail?.thread_count) ??
        DEFAULT_EXECUTION_THREAD_COUNT
      const duration =
        parsePositiveInteger(values.duration) ??
        parsePositiveInteger(taskDetail?.duration) ??
        DEFAULT_EXECUTION_DURATION

      const payload: CreateTaskDto = {
        name: values.name,
        description: values.description,
        env: values.env,
        script_id: selectedScriptId ?? Number(values.script_id),
        engine_type: values.engine_type,
        task_pattern: 'script',
        protocols: values.protocols,
        thread_count: threadCount,
        duration,
        collaborator_ids: normalizeCollaboratorIds(values.collaborator_ids),
        properties,
      }

      mutation.mutate(payload)
    } catch (error) {
      setScriptValidationMessage(form.getFieldError('script_id')[0])
      console.error('task form validation failed', error)
    }
  }

  return (
    <Space direction="vertical" size={16} className="olh-task-create-page" style={{ display: 'flex' }}>
      {requestedType && requestedType !== 'script' && (
        <Alert
          type="info"
          showIcon
          message={
            requestedType === 'http'
              ? '已从“我的关注”进入 HTTP 任务快捷创建'
              : requestedType === 'grpc'
                ? '已从“我的关注”进入 GRPC 任务快捷创建'
                : '当前入口仍复用脚本型任务创建'
          }
          description="模板会预填基础参数，依旧需手动上传/编辑脚本，继续保持脚本型流程，暂不开放独立可视化编辑器。"
        />
      )}

      {!isEdit && appliedTemplate && TEMPLATE_PRESETS[appliedTemplate] && (
        <Alert
          type="info"
          showIcon
          message={TEMPLATE_PRESETS[appliedTemplate].title}
          description={TEMPLATE_PRESETS[appliedTemplate].description}
        />
      )}

      <Card size="small" className="olh-task-create-hero">
        <Row justify="space-between" align="middle" gutter={[16, 16]}>
          <Col>
            <Space direction="vertical" size={4}>
              <Title level={3} style={{ margin: 0 }}>
                {isEdit ? '编辑压测脚本任务' : '创建压测脚本任务'}
              </Title>
              <Text type="secondary">
                集中维护基础信息、脚本变量、资源配置和文件管理。
              </Text>
              {isEdit && taskDetail ? (
                <Space wrap size={[8, 8]}>
                  <Tag color="blue">任务 ID {taskDetail.id}</Tag>
                  <Tag>{taskDetail.business_line || '未设置业务线'}</Tag>
                  <Tag>{taskDetail.env || '-'}</Tag>
                  {currentScriptName ? <Tag color="processing">{currentScriptName}</Tag> : null}
                </Space>
              ) : null}
            </Space>
          </Col>
          <Col>
            <Space
              direction="vertical"
              size={8}
              style={{ display: 'flex', alignItems: 'flex-end' }}
            >
              <div className="olh-task-create-sticky-actions">
                <Space wrap>
                  <Button
                    type="primary"
                    htmlType="submit"
                    form={TASK_FORM_ID}
                    loading={isSubmitting}
                    aria-label={isEdit ? '顶部保存任务' : '顶部创建任务'}
                    data-testid="task-create-sticky-submit"
                  >
                    {isEdit ? '保存任务' : '创建任务'}
                  </Button>
                  <Button
                    onClick={handleCancel}
                    aria-label="顶部取消任务编辑"
                    data-testid="task-create-sticky-cancel"
                  >
                    取消
                  </Button>
                </Space>
              </div>
              <Space wrap>
                {isEdit && taskDetail ? (
                  <Button
                    type="primary"
                    icon={<RocketOutlined />}
                    disabled={taskDetail.status !== 'ready'}
                    onClick={() => setStartDrawerOpen(true)}
                  >
                    启动压测
                  </Button>
                ) : null}
                {isEdit ? (
                  <Button onClick={() => setVersionDrawerOpen(true)}>历史版本</Button>
                ) : null}
                <Button
                  disabled={typeof selectedScriptId !== 'number' || selectedScriptId <= 0}
                  onClick={openCurrentScriptEditor}
                >
                  {previewCompareData ? '查看当前脚本' : '脚本编辑'}
                </Button>
                <Button icon={<SettingOutlined />} onClick={() => setAdvancedModalOpen(true)}>
                  高级配置
                </Button>
              </Space>
            </Space>
          </Col>
        </Row>
      </Card>

      <Form id={TASK_FORM_ID} form={form} layout="vertical" onFinish={handleSubmit}>
        <Form.Item name="task_pattern" hidden>
          <Input />
        </Form.Item>

        <Card size="small" title="基础信息" className="olh-task-create-panel">
          <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
            先确认任务名称、业务线、压测环境和协作人，再继续调整脚本与资源配置。
          </Text>
          <Row gutter={[16, 0]}>
            <Col xs={24} md={12}>
              <Form.Item
                name="name"
                label="任务名称"
                rules={[{ required: true, message: '请输入任务名称' }]}
              >
                <Input placeholder="请输入任务名称" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item
                name="business_line"
                label="业务线"
                rules={[{ required: true, message: '请选择业务线' }]}
              >
                <Select
                  placeholder="请选择业务线"
                  options={businessOptions.map(item => ({
                    label: item.name || item.code,
                    value: item.code,
                  }))}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item
                name="env"
                label="压测环境"
                rules={[{ required: true, message: '请选择压测环境' }]}
              >
                <Select
                  placeholder="请选择压测环境"
                  options={envOptions.map(item => ({
                    label: item.code,
                    value: item.code,
                  }))}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item label="环境类型">
                <Input
                  value={deriveEnvType(selectedEnv, envOptions)}
                  placeholder="选择环境后自动展示"
                  disabled
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="collaborator_ids" label="协作人">
                <Select
                  mode="tags"
                  tokenSeparators={[',']}
                  placeholder="选择协作人，或输入用户 ID"
                  options={collaboratorOptions}
                  optionFilterProp="label"
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="description" label="任务描述">
                <TextArea rows={3} placeholder="请输入任务描述（可选）" />
              </Form.Item>
            </Col>
          </Row>
        </Card>

        <Collapse
          className="olh-task-create-collapse"
          size="small"
          style={{ marginTop: 16 }}
          expandIconPosition="end"
          items={[
            {
              key: 'related-info',
              label: '关联信息',
              children: (
                <>
                  <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
                    {publicAlphaMode
                      ? 'OpenLoadHub public alpha 保留关联监控和关联链路入口，用于在结果详情中快速跳转到外部观测面。'
                      : '当前先集中展示关联应用与告警订阅，后续再加深与应用、监控和链路系统的联动。'}
                  </Text>
                  <Tabs type="card" defaultActiveKey={publicAlphaMode ? 'monitor' : 'apps'}>
                    {!publicAlphaMode ? (
                      <Tabs.TabPane tab="关联应用" key="apps">
                        <Form.Item
                          name="related_apps"
                          label="应用列表"
                          style={{ marginBottom: 12 }}
                        >
                          <Select
                            mode="tags"
                            tokenSeparators={[',']}
                            placeholder="输入应用名，回车确认"
                          />
                        </Form.Item>
                        <Form.Item
                          noStyle
                          shouldUpdate={(prev, next) => prev.related_apps !== next.related_apps}
                        >
                          {({ getFieldValue }) => {
                            const relatedApps = parseStringArray(getFieldValue('related_apps'))
                            return (
                              <div className="olh-task-related-apps-preview">
                                <div
                                  className="olh-task-related-apps-preview-head"
                                  style={{
                                    display: 'grid',
                                    gridTemplateColumns: '72px 1fr',
                                  }}
                                >
                                  <span>序号</span>
                                  <span>应用名称</span>
                                </div>
                                {relatedApps.length > 0 ? (
                                  relatedApps.map((item, index) => (
                                    <div
                                      key={`${item}-${index}`}
                                      className="olh-task-related-apps-preview-row"
                                      style={{
                                        display: 'grid',
                                        gridTemplateColumns: '72px 1fr',
                                      }}
                                    >
                                      <span>{index + 1}</span>
                                      <span>{item}</span>
                                    </div>
                                  ))
                                ) : (
                                  <div className="olh-task-related-apps-preview-empty">
                                    当前未设置关联应用。
                                  </div>
                                )}
                              </div>
                            )
                          }}
                        </Form.Item>
                      </Tabs.TabPane>
                    ) : null}
                    <Tabs.TabPane tab="关联监控" key="monitor">
                      <Form.List name="related_monitors">
                        {(fields, { add, remove }) => (
                          <Space direction="vertical" size={12} style={{ width: '100%' }}>
                            {fields.map(field => (
                              <Card
                                size="small"
                                key={field.key}
                                title={`监控入口 ${field.name + 1}`}
                                extra={
                                  <Button
                                    type="text"
                                    danger
                                    icon={<DeleteOutlined />}
                                    aria-label="删除关联监控"
                                    onClick={() => remove(field.name)}
                                  />
                                }
                              >
                                <Row gutter={[12, 0]}>
                                  <Col xs={24} md={8}>
                                    <Form.Item
                                      name={[field.name, 'title']}
                                      label="名称"
                                      style={{ marginBottom: 12 }}
                                    >
                                      <Input placeholder="例如：订单服务 Grafana" />
                                    </Form.Item>
                                  </Col>
                                  <Col xs={24} md={16}>
                                    <Form.Item
                                      name={[field.name, 'url']}
                                      label="链接"
                                      style={{ marginBottom: 12 }}
                                    >
                                      <Input placeholder="https://grafana.example.com/d/..." />
                                    </Form.Item>
                                  </Col>
                                  <Col xs={24} md={8}>
                                    <Form.Item
                                      name={[field.name, 'kind']}
                                      label="类型"
                                      style={{ marginBottom: 12 }}
                                    >
                                      <Select
                                        placeholder="选择类型"
                                        options={[
                                          { label: 'Grafana', value: 'grafana' },
                                          { label: 'Prometheus', value: 'prometheus' },
                                          { label: 'SkyWalking', value: 'skywalking' },
                                          { label: '其他', value: 'other' },
                                        ]}
                                      />
                                    </Form.Item>
                                  </Col>
                                  <Col xs={24} md={8}>
                                    <Form.Item
                                      name={[field.name, 'embed_mode']}
                                      label="打开方式"
                                      style={{ marginBottom: 12 }}
                                    >
                                      <Radio.Group
                                        optionType="button"
                                        buttonStyle="solid"
                                        options={[
                                          { label: '新窗口', value: 'new_tab' },
                                          { label: '内嵌', value: 'iframe' },
                                        ]}
                                      />
                                    </Form.Item>
                                  </Col>
                                  <Col xs={24} md={8}>
                                    <Form.Item
                                      name={[field.name, 'description']}
                                      label="说明"
                                      style={{ marginBottom: 12 }}
                                    >
                                      <Input placeholder="指标范围或看板说明" />
                                    </Form.Item>
                                  </Col>
                                </Row>
                              </Card>
                            ))}
                            <Button
                              icon={<PlusOutlined />}
                              onClick={() => add({ kind: 'grafana', embed_mode: 'new_tab' })}
                            >
                              添加监控入口
                            </Button>
                          </Space>
                        )}
                      </Form.List>
                    </Tabs.TabPane>
                    <Tabs.TabPane tab="关联链路" key="links">
                      <Form.Item name="trace_link" label="关联链路" style={{ marginBottom: 12 }}>
                        <Input placeholder="请输入链路入口或说明" />
                      </Form.Item>
                      <Space direction="vertical" style={{ width: '100%' }}>
                        {RELATED_LINK_SAMPLES.map(item => (
                          <Card size="small" key={item.url} className="olh-task-related-link-card">
                            <Text strong>{item.name}</Text>
                            <div>
                              <Text type="secondary">{item.url}</Text>
                            </div>
                          </Card>
                        ))}
                      </Space>
                    </Tabs.TabPane>
                  </Tabs>
                </>
              ),
            },
          ]}
        />

        <Collapse
          className="olh-task-create-collapse"
          size="small"
          style={{ marginTop: 16 }}
          expandIconPosition="end"
          items={[
            {
              key: 'alert-subscriptions',
              label: '告警订阅',
              children: publicAlphaMode ? (
                <Alert
                  type="info"
                  showIcon
                  message="告警订阅将在后续版本开放"
                  description="public alpha 暂不内置 Alertmanager / Prometheus 告警接入；当前请在外部监控系统中维护告警规则，通过关联监控和关联链路保存跳转入口。"
                />
              ) : (
                <>
                  <Form.Item
                    name="alert_subscriptions"
                    label="订阅对象"
                    style={{ marginBottom: 12 }}
                  >
                    <Select
                      mode="tags"
                      tokenSeparators={[',']}
                      placeholder="输入订阅对象或告警组，回车确认"
                    />
                  </Form.Item>
                  <Alert
                    type="info"
                    showIcon
                    message="这里配置的是 OpenLoadHub 接收外部告警后的匹配与动作策略"
                    description={
                      <Space direction="vertical" size={2}>
                        <Text type="secondary">
                          保存后会随 Run 或 mixed run
                          创建快照；只有同时开启“自动止停”且关闭“仅观测”时，策略中的 stop_run /
                          stop_mixed_run 才会执行真实止停动作。
                        </Text>
                        <Text type="secondary">
                          本页不会自动创建 Grafana / Prometheus / SkyWalking
                          告警规则；触发阈值、持续时间和 firing/resolved
                          判定由外部告警源定义，外部事件进入 OpenLoadHub 后才会在结果详情展示。
                        </Text>
                        <Text type="secondary">
                          本地 Docker 阈值当前仍手动维护在 `docker/prometheus.rules.yml`；若只想配
                          demo-target 的资源阈值，建议先维护 CPU / Memory
                          两条规则，再在这里把匹配源配置成实际告警链路，例如
                          `prometheus_alertmanager + TargetServiceHighCpu/HighMemory`。
                        </Text>
                      </Space>
                    }
                    style={{ marginBottom: 12 }}
                  />
                  <Form.List name="alert_policies">
                    {(fields, { add, remove }) => (
                      <Space direction="vertical" size={12} style={{ width: '100%' }}>
                        {fields.map(field => (
                          <Card
                            size="small"
                            key={field.key}
                            title={`告警策略 ${field.name + 1}`}
                            extra={
                              <Button
                                type="text"
                                danger
                                icon={<DeleteOutlined />}
                                aria-label="删除告警策略"
                                onClick={() => remove(field.name)}
                              />
                            }
                          >
                            <Row gutter={[12, 0]}>
                              <Col xs={24} md={8}>
                                <Form.Item
                                  name={[field.name, 'name']}
                                  label="策略名称"
                                  style={{ marginBottom: 12 }}
                                >
                                  <Input placeholder="例如：High 告警止停" />
                                </Form.Item>
                              </Col>
                              <Col xs={24} md={8}>
                                <Form.Item name={[field.name, 'enabled']} label="启用" initialValue>
                                  <Radio.Group
                                    optionType="button"
                                    buttonStyle="solid"
                                    options={[
                                      { label: '启用', value: true },
                                      { label: '停用', value: false },
                                    ]}
                                  />
                                </Form.Item>
                              </Col>
                              <Col xs={24} md={8}>
                                <Form.Item
                                  name={[field.name, 'auto_stop_enabled']}
                                  label="自动止停"
                                  initialValue={false}
                                >
                                  <Radio.Group
                                    optionType="button"
                                    buttonStyle="solid"
                                    options={[
                                      { label: '关闭', value: false },
                                      { label: '开启', value: true },
                                    ]}
                                  />
                                </Form.Item>
                              </Col>
                              <Col xs={24} md={8}>
                                <Form.Item
                                  name={[field.name, 'observe_only']}
                                  label="仅观测"
                                  initialValue
                                >
                                  <Radio.Group
                                    optionType="button"
                                    buttonStyle="solid"
                                    options={[
                                      { label: '仅观测', value: true },
                                      { label: '执行动作', value: false },
                                    ]}
                                  />
                                </Form.Item>
                              </Col>
                              <Col xs={24} md={8}>
                                <Form.Item
                                  name={[field.name, 'source']}
                                  label="来源"
                                  style={{ marginBottom: 12 }}
                                >
                                  <Select
                                    placeholder="选择告警源"
                                    options={[
                                      {
                                        label: 'Grafana Alertmanager',
                                        value: 'grafana_alertmanager',
                                      },
                                      {
                                        label: 'Prometheus Alertmanager',
                                        value: 'prometheus_alertmanager',
                                      },
                                      { label: 'SkyWalking', value: 'skywalking' },
                                      { label: '其他', value: 'other' },
                                    ]}
                                  />
                                </Form.Item>
                              </Col>
                              <Col xs={24} md={8}>
                                <Form.Item
                                  name={[field.name, 'match', 'subscription']}
                                  label="匹配订阅"
                                  style={{ marginBottom: 12 }}
                                >
                                  <Input placeholder="订阅对象或告警组" />
                                </Form.Item>
                              </Col>
                              <Col xs={24} md={8}>
                                <Form.Item
                                  name={[field.name, 'match', 'alertname']}
                                  label="匹配告警名"
                                  style={{ marginBottom: 12 }}
                                >
                                  <Input placeholder="AlertName" />
                                </Form.Item>
                              </Col>
                              <Col xs={24} md={8}>
                                <Form.Item
                                  name={[field.name, 'match', 'severity']}
                                  label="匹配级别"
                                  style={{ marginBottom: 12 }}
                                >
                                  <Select
                                    mode="tags"
                                    tokenSeparators={[',']}
                                    placeholder="warning / high / critical"
                                  />
                                </Form.Item>
                              </Col>
                              <Col xs={24} md={8}>
                                <Form.Item
                                  name={[field.name, 'actions']}
                                  label="动作"
                                  style={{ marginBottom: 12 }}
                                >
                                  <Select
                                    mode="tags"
                                    tokenSeparators={[',']}
                                    placeholder="选择或输入动作"
                                    options={[
                                      { label: '通知协作人', value: 'notify_collaborators' },
                                      { label: '标记风险', value: 'mark_risk' },
                                      { label: '停止 Run', value: 'stop_run' },
                                      { label: '停止混压', value: 'stop_mixed_run' },
                                      { label: '停止并标记失败', value: 'stop_and_mark_failed' },
                                      { label: 'Webhook', value: 'webhook' },
                                    ]}
                                  />
                                </Form.Item>
                              </Col>
                              <Col xs={24} md={8}>
                                <Form.Item
                                  name={[field.name, 'cooldown_seconds']}
                                  label="冷却秒数"
                                  style={{ marginBottom: 12 }}
                                >
                                  <InputNumber
                                    min={0}
                                    precision={0}
                                    style={{ width: '100%' }}
                                    placeholder="300"
                                  />
                                </Form.Item>
                              </Col>
                              <Col xs={24} md={8}>
                                <Form.Item
                                  name={[field.name, 'for_seconds']}
                                  label="持续秒数"
                                  style={{ marginBottom: 12 }}
                                >
                                  <InputNumber
                                    min={0}
                                    precision={0}
                                    style={{ width: '100%' }}
                                    placeholder="300"
                                  />
                                </Form.Item>
                              </Col>
                            </Row>
                          </Card>
                        ))}
                        <Button
                          icon={<PlusOutlined />}
                          onClick={() =>
                            add({
                              enabled: true,
                              auto_stop_enabled: false,
                              observe_only: true,
                              source: 'grafana_alertmanager',
                              match: {},
                              actions: ['notify_collaborators'],
                              cooldown_seconds: 300,
                            })
                          }
                        >
                          添加告警策略
                        </Button>
                      </Space>
                    )}
                  </Form.List>
                </>
              ),
            },
          ]}
        />

        <Card size="small" title="脚本变量" className="olh-task-create-panel" style={{ marginTop: 16 }}>
          <Space direction="vertical" size={12} style={{ display: 'flex' }}>
            <Text type="secondary">
              页面中直接输入变量名和值即可；脚本内引用格式为 `###{'{param}'}###`。
            </Text>
            {variableRows.map(item => (
              <Row gutter={[12, 12]} key={item.id}>
                <Col xs={24} md={7}>
                  <Space direction="vertical" size={6} style={{ display: 'flex' }}>
                    <Input
                      placeholder="参数名"
                      value={item.key}
                      onChange={event =>
                        handleVariableChange(item.index, { key: event.target.value })
                      }
                    />
                    {item.generated || item.sensitive || item.source ? (
                      <Space wrap size={[6, 6]}>
                        {item.generated ? <Tag color="blue">生成变量</Tag> : null}
                        {item.sensitive ? <Tag color="orange">敏感占位</Tag> : null}
                        {item.source ? (
                          <Text type="secondary">{`来源：${item.source}`}</Text>
                        ) : null}
                      </Space>
                    ) : null}
                  </Space>
                </Col>
                <Col xs={24} md={5}>
                  <Select<VariableType>
                    style={{ width: '100%' }}
                    value={item.valueType}
                    onChange={value => handleVariableChange(item.index, { valueType: value })}
                    options={[
                      { label: 'string', value: 'string' },
                      { label: 'int', value: 'int' },
                      { label: 'float', value: 'float' },
                      { label: 'boolean', value: 'boolean' },
                    ]}
                  />
                </Col>
                <Col xs={24} md={9}>
                  <Input
                    placeholder="变量值"
                    value={item.value}
                    onChange={event =>
                      handleVariableChange(item.index, { value: event.target.value })
                    }
                  />
                </Col>
                <Col xs={24} md={3}>
                  <Space>
                    <Button onClick={() => handleVariableAdd()}>新增</Button>
                    <Button danger onClick={() => handleVariableDelete(item.index)}>
                      删除
                    </Button>
                  </Space>
                </Col>
              </Row>
            ))}
          </Space>
        </Card>

        <Card size="small" title="压测脚本" className="olh-task-create-panel" style={{ marginTop: 16 }}>
          <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
            左侧维护脚本类型与资源配置，右侧处理数据文件和脚本版本入口；选择 GRPC 后展示 proto 文件。
          </Text>
          <Row gutter={[20, 12]}>
            <Col xs={24} lg={10}>
              <Space direction="vertical" size={12} style={{ display: 'flex' }}>
                <Form.Item
                  name="engine_type"
                  label="压测脚本类型"
                  rules={[{ required: true, message: '请选择压测脚本类型' }]}
                >
                  <Select
                    placeholder="请选择压测脚本类型"
                    options={[
                      { label: 'JMeter', value: 'jmeter' satisfies EngineType },
                      { label: 'K6', value: 'k6' satisfies EngineType },
                      ...(!publicAlphaMode
                        ? [{ label: 'Custom', value: 'custom' satisfies EngineType }]
                        : []),
                    ]}
                  />
                </Form.Item>

                <Form.Item
                  name="protocols"
                  label="协议类型"
                  rules={[{ required: true, message: '请选择协议类型' }]}
                >
                  <Select
                    mode="multiple"
                    placeholder="请选择协议类型"
                    options={[
                      { label: 'HTTP', value: 'http' satisfies Protocol },
                      { label: 'GRPC', value: 'grpc' satisfies Protocol },
                      { label: 'Kafka', value: 'kafka' satisfies Protocol },
                      { label: 'WebSocket', value: 'websocket' satisfies Protocol },
                      { label: 'Browser', value: 'browser' satisfies Protocol },
                      { label: 'Other', value: 'other' satisfies Protocol },
                    ]}
                  />
                </Form.Item>

                <Form.Item name="resource_type" label="资源类型">
                  <Radio.Group>
                    <Radio value="K8S">K8S</Radio>
                    <Radio value="EC2" disabled>
                      EC2（只读）
                    </Radio>
                  </Radio.Group>
                </Form.Item>

                <Form.Item name="cloud_vendor" label="云环境">
                  <Radio.Group>
                    <Radio value="AWS">AWS</Radio>
                    <Radio value="ALIYUN" disabled>
                      阿里云
                    </Radio>
                  </Radio.Group>
                </Form.Item>

                <Row gutter={[16, 0]}>
                  <Col span={24}>
                    <Form.Item
                      name="pod_count"
                      label="节点数量"
                      rules={[{ required: true, message: '请输入节点数量' }]}
                    >
                      <InputNumber min={1} max={50} style={{ width: '100%' }} />
                    </Form.Item>
                  </Col>
                </Row>

                <Form.Item name="data_distribution" label="数据下发方式">
                  <Radio.Group>
                    <Radio value="all">全量下发</Radio>
                    <Radio value="avg">平均分割</Radio>
                  </Radio.Group>
                </Form.Item>
              </Space>
            </Col>

            <Col xs={24} lg={14}>
              <Space direction="vertical" size={12} style={{ display: 'flex' }}>
                {grpcProtocolSelected ? (
                  <Card size="small" title="协议文件（proto）" bodyStyle={{ padding: 12 }}>
                    <TaskAssetPanel
                      category="proto"
                      title="协议文件"
                      accept=".proto"
                      helperText="支持上传 proto 文件，单文件不超过 1MB"
                      taskId={taskDetail?.id}
                      serverAssets={taskDetail?.proto_assets ?? []}
                      tempAssets={tempProtoAssets}
                      onTempAssetsChange={setTempProtoAssets}
                      compact
                    />
                  </Card>
                ) : null}

                <Card size="small" title="压测数据" bodyStyle={{ padding: 12 }}>
                  <TaskAssetPanel
                    category="data"
                    title="压测数据"
                    accept=".csv,.txt,.json,.zip"
                    helperText="支持 csv/txt/json/zip；大数据文件需启用 MinIO/S3，zip 内仅允许一个数据文件"
                    taskId={taskDetail?.id}
                    serverAssets={taskDetail?.data_assets ?? []}
                    tempAssets={tempDataAssets}
                    onTempAssetsChange={setTempDataAssets}
                    compact
                  />
                </Card>

                {!publicAlphaMode ? (
                  <Card size="small" title="外部数据引用" bodyStyle={{ padding: 12 }}>
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 12 }}
                      message="OpenLoadHub 只保存外部引用"
                      description="这里仅记录外部数据集、文件、API token 或造数任务 ID 的轻量引用；OpenLoadHub 不托管数据、不执行造数、不处理脱敏生命周期。"
                    />
                    <Row gutter={[12, 0]}>
                      <Col xs={24} md={12}>
                        <Form.Item name={['external_data_ref', 'source_type']} label="引用类型">
                          <Select
                            allowClear
                            placeholder="请选择外部引用类型"
                            options={EXTERNAL_DATA_SOURCE_OPTIONS}
                          />
                        </Form.Item>
                      </Col>
                      <Col xs={24} md={12}>
                        <Form.Item name={['external_data_ref', 'source_name']} label="外部来源名称">
                          <Input placeholder="例如 CRM 样本集 / 造数平台任务" />
                        </Form.Item>
                      </Col>
                      <Col xs={24} md={12}>
                        <Form.Item name={['external_data_ref', 'reference_id']} label="外部引用 ID">
                          <Input placeholder="例如 dataset_id / file_id / task_id" />
                        </Form.Item>
                      </Col>
                      <Col xs={24} md={12}>
                        <Form.Item
                          name={['external_data_ref', 'reference_url']}
                          label="外部引用 URL"
                        >
                          <Input placeholder="请输入外部平台或文件入口 URL" />
                        </Form.Item>
                      </Col>
                      <Col span={24}>
                        <Form.Item name={['external_data_ref', 'notes']} label="备注">
                          <TextArea rows={3} placeholder="补充外部数据口径、负责人或使用限制" />
                        </Form.Item>
                      </Col>
                    </Row>
                  </Card>
                ) : null}

                <Card size="small" title="压测脚本" bodyStyle={{ padding: 12 }}>
                  <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
                    {publicAlphaMode
                      ? '支持上传现有 JMeter / K6 脚本；K6 可从 cURL 或 HAR 导入生成。'
                      : '支持上传现有脚本，优先通过统一导入向导生成 K6 草稿脚本。'}
                  </Text>
                  <Space wrap size={[8, 8]} style={{ marginBottom: 12 }}>
                    <Button type="primary" onClick={() => setScriptImportWizardOpen(true)}>
                      {publicAlphaMode ? '从 cURL / HAR 导入 K6' : '统一导入生成 K6'}
                    </Button>
                    {!publicAlphaMode ? (
                      <>
                        <Button type="dashed" onClick={() => setCurlToK6Open(true)}>
                          经典导入：CURL
                        </Button>
                        <Button type="dashed" onClick={() => setOpenApiToK6Open(true)}>
                          经典导入：OpenAPI
                        </Button>
                        <Button type="dashed" onClick={() => setHarToK6Open(true)}>
                          经典导入：HAR
                        </Button>
                      </>
                    ) : null}
                  </Space>
                  <Alert
                    type="info"
                    showIcon
                    style={{ marginBottom: 12 }}
                    message={
                      publicAlphaMode ? '导入入口支持 cURL 和 HAR' : '统一导入向导为推荐入口'
                    }
                    description={
                      publicAlphaMode
                        ? '从 cURL 或 HAR 生成单接口 K6 草稿脚本；复杂编排、OpenAPI 生成、AI 辅助和自动鉴权后续再补。'
                        : '统一导入向导支持 cURL、OpenAPI 和 HAR 生成单接口 K6 草稿，并记录当前浏览器的最近导入摘要；复杂编排、自动鉴权和文件上传仍需手动补充。'
                    }
                  />
                  <Form.Item
                    name="script_id"
                    hidden
                    rules={[{ required: true, message: '请上传或选择脚本' }]}
                  >
                    <Input />
                  </Form.Item>
                  <Form.Item
                    validateStatus={scriptValidationMessage ? 'error' : undefined}
                    help={scriptValidationMessage}
                  >
                    <ScriptUploadPanel
                      value={selectedScriptId}
                      engineType={selectedEngineType}
                      compact
                      onChange={scriptId => {
                        form.setFieldValue('script_id', scriptId)
                        form.setFields([{ name: 'script_id', value: scriptId, errors: [] }])
                        setScriptValidationMessage(undefined)
                      }}
                    />
                  </Form.Item>
                  {typeof selectedScriptId === 'number' && selectedScriptId > 0 ? (
                    <Button
                      size="small"
                      danger
                      icon={<DeleteOutlined />}
                      loading={deleteScriptMutation.isPending}
                      onClick={() => {
                        if (isSavedScriptSelection) {
                          clearSelectedScript('当前脚本已移除，请重新上传并保存任务')
                          return
                        }
                        deleteScriptMutation.mutate(selectedScriptId)
                      }}
                    >
                      删除当前脚本
                    </Button>
                  ) : null}
                  {selectedScript && (
                    <>
                      <Divider style={{ margin: '12px 0' }} />
                      <Row gutter={[16, 12]}>
                        <Col span={12}>
                          <Text type="secondary">脚本名称</Text>
                          <div>
                            <Button type="link" size="small" onClick={handleDownloadScript}>
                              {currentScriptName || selectedScript.name}
                            </Button>
                          </div>
                        </Col>
                        <Col span={6}>
                          <Text type="secondary">脚本类型</Text>
                          <div>{selectedScript.script_type}</div>
                        </Col>
                        <Col span={6}>
                          <Text type="secondary">脚本版本</Text>
                          <div>{selectedScript.version}</div>
                        </Col>
                      </Row>
                      <Space wrap size={[8, 8]} style={{ marginTop: 12 }}>
                        <Tag color="geekblue">脚本版本 {selectedScript.version}</Tag>
                        {!publicAlphaMode && selectedScript.script_type === 'K6' ? (
                          <Tag color={isManagedK6Template ? 'green' : 'orange'}>
                            {isManagedK6Template ? '平台管理 k6 模板' : '自定义 k6 脚本'}
                          </Tag>
                        ) : null}
                        {isGeneratedK6Script ? (
                          <Tag color={aiFeaturesEnabled ? 'purple' : 'blue'}>
                            {isCurlGeneratedK6Script
                              ? aiFeaturesEnabled
                                ? 'AI 生成：curl 到 k6'
                                : '导入生成：curl 到 k6'
                              : isOpenApiGeneratedK6Script
                                ? aiFeaturesEnabled
                                  ? 'AI 生成：OpenAPI 到 k6'
                                  : '导入生成：OpenAPI 到 k6'
                                : aiFeaturesEnabled
                                  ? 'AI 生成：HAR 到 k6'
                                  : '导入生成：HAR 到 k6'}
                          </Tag>
                        ) : null}
                        {generatedScriptMethod ? <Tag>{generatedScriptMethod}</Tag> : null}
                        {generatedScriptBodyMode ? (
                          <Tag>{`body:${generatedScriptBodyMode}`}</Tag>
                        ) : null}
                        {generatedScriptContentType ? (
                          <Tag>{generatedScriptContentType}</Tag>
                        ) : null}
                        {generatedScriptResponseTimeout ? (
                          <Tag>{`timeout ${generatedScriptResponseTimeout}ms`}</Tag>
                        ) : null}
                        {generatedScriptHumanConfirmationRequired ? (
                          <Tag color="orange">需人工确认</Tag>
                        ) : null}
                        {generatedScriptAiReviewStatus ? (
                          <Tag>{`AI review:${generatedScriptAiReviewStatus}`}</Tag>
                        ) : null}
                        {currentScriptUpdatedAt ? (
                          <Tag>当前脚本更新 {formatLocalDateTime(currentScriptUpdatedAt)}</Tag>
                        ) : null}
                        {previewCompareData ? (
                          <Tag color="orange">历史预览 {previewCompareData.history.version}</Tag>
                        ) : null}
                        <Button size="small" onClick={openCurrentScriptEditor}>
                          {previewCompareData ? '回到当前脚本' : '编辑当前脚本'}
                        </Button>
                        {isEdit ? (
                          <Button size="small" onClick={() => setVersionDrawerOpen(true)}>
                            历史版本
                          </Button>
                        ) : null}
                      </Space>
                      <Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
                        当前页外层只展示当前脚本版本与更新时间；任务版本快照请在“历史版本”抽屉中查看。
                      </Text>
                      {isGeneratedK6Script ? (
                        <Alert
                          type="info"
                          showIcon
                          style={{ marginTop: 12 }}
                          message={
                            isCurlGeneratedK6Script
                              ? aiFeaturesEnabled
                                ? '当前脚本来源于 AI 辅助生成（cURL）'
                                : '当前脚本来源于导入向导（cURL）'
                              : isOpenApiGeneratedK6Script
                                ? aiFeaturesEnabled
                                  ? '当前脚本来源于 AI 辅助生成（OpenAPI）'
                                  : '当前脚本来源于导入向导（OpenAPI）'
                                : aiFeaturesEnabled
                                  ? '当前脚本来源于 AI 辅助生成（HAR）'
                                  : '当前脚本来源于导入向导（HAR）'
                          }
                          description={
                            generatedScriptSourceUrl
                              ? `来源请求：${generatedScriptSourceUrl}${generatedScriptMethod ? `；方法：${generatedScriptMethod}` : ''}${generatedScriptContentType ? `；content-type：${generatedScriptContentType}` : ''}${generatedScriptResponseTimeout ? `；超时：${generatedScriptResponseTimeout}ms` : ''}${generatedScriptHumanConfirmationRequired ? `；人工确认：${generatedScriptHumanConfirmationStatus || 'pending'}` : ''}${generatedScriptFinalStatus ? `；脚本状态：${generatedScriptFinalStatus}` : ''}`
                              : '来源请求 URL 已写入脚本参数，可用于后续结果分析与报告留痕；生成脚本默认需要人工确认后再进入正式压测。'
                          }
                        />
                      ) : null}
                    </>
                  )}
                  {previewCompareData ? (
                    <Alert
                      type="warning"
                      showIcon
                      style={{ marginTop: 16 }}
                      message={`当前正在预览历史版本 ${previewCompareData.history.version}`}
                      description={`编辑器已切换到只读模式。点击“编辑当前脚本”即可回到当前版本 ${selectedScriptVersion ?? '-'} 继续修改。`}
                    />
                  ) : null}
                  {hasUnsavedScriptSelection ? (
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginTop: 16 }}
                      message="当前脚本选择尚未保存到任务版本"
                      description="已切换脚本版本。保存任务后，新的脚本版本关联才会写入任务历史；历史版本抽屉当前仍基于已保存任务。"
                    />
                  ) : null}
                </Card>
              </Space>
            </Col>
          </Row>
        </Card>

        <Form.Item style={{ marginTop: 24, marginBottom: 0 }}>
          <Space size="middle">
            <Button type="primary" htmlType="submit" loading={isSubmitting}>
              {isEdit ? '保存任务' : '创建任务'}
            </Button>
            <Button onClick={handleCancel}>取消</Button>
          </Space>
        </Form.Item>
      </Form>

      <AdvancedConfigModal
        open={advancedModalOpen}
        value={advancedConfig}
        showTimeouts={!publicAlphaMode}
        onCancel={() => setAdvancedModalOpen(false)}
        onOk={value => {
          setAdvancedConfig(value)
          setAdvancedModalOpen(false)
          message.success('高级配置已保存')
        }}
      />

      <CurlToK6Modal
        open={curlToK6Open}
        initialName={String(form.getFieldValue('name') || '')}
        onCancel={() => setCurlToK6Open(false)}
        onSuccess={handleCurlToK6Success}
      />

      <ScriptImportWizardModal
        open={scriptImportWizardOpen}
        initialName={String(form.getFieldValue('name') || '')}
        allowedModes={scriptImportModes}
        onCancel={() => setScriptImportWizardOpen(false)}
        onSuccess={handleScriptImportWizardSuccess}
      />

      <OpenApiToK6Modal
        open={openApiToK6Open}
        initialName={String(form.getFieldValue('name') || '')}
        onCancel={() => setOpenApiToK6Open(false)}
        onSuccess={handleOpenApiToK6Success}
      />

      <HarToK6Modal
        open={harToK6Open}
        initialName={String(form.getFieldValue('name') || '')}
        onCancel={() => setHarToK6Open(false)}
        onSuccess={handleHarToK6Success}
      />

      <TaskScriptEditorDrawer
        open={scriptEditorOpen}
        scriptId={typeof selectedScriptId === 'number' ? selectedScriptId : undefined}
        taskId={taskDetail?.id}
        onScriptSelected={scriptId => {
          form.setFieldValue('script_id', scriptId)
          form.setFields([{ name: 'script_id', value: scriptId, errors: [] }])
          setScriptValidationMessage(undefined)
          setPreviewCompareData(null)
        }}
        previewVersion={previewCompareData?.history ?? null}
        currentScriptVersion={selectedScriptVersion}
        onViewCurrent={() => setPreviewCompareData(null)}
        onClose={closeScriptEditor}
      />

      <TaskVersionDrawer
        open={versionDrawerOpen}
        task={taskDetail ? { id: taskDetail.id, name: taskDetail.name } : null}
        currentScriptVersion={selectedScriptVersion}
        onPreviewVersion={compareData => {
          setPreviewCompareData(compareData)
          setScriptEditorOpen(true)
        }}
        onClose={() => setVersionDrawerOpen(false)}
      />

      <StartRunDrawer
        open={startDrawerOpen}
        task={
          taskDetail
            ? {
                id: taskDetail.id,
                name: taskDetail.name,
                status: taskDetail.status,
                env: taskDetail.env,
                engine_type: taskDetail.engine_type,
                thread_count: taskDetail.thread_count,
                duration: taskDetail.duration,
                last_run_status: taskDetail.last_run_status ?? null,
                last_run_status_label: taskDetail.last_run_status_label ?? null,
                data_distribution: taskDetail.data_distribution,
                properties: taskDetail.properties,
                data_assets: taskDetail.data_assets,
                proto_assets: taskDetail.proto_assets,
              }
            : null
        }
        onClose={() => setStartDrawerOpen(false)}
        onCreated={() => {
          queryClient.invalidateQueries({ queryKey: ['runs'] })
          if (taskDetail?.id) {
            queryClient.invalidateQueries({ queryKey: ['task-last-run-params', taskDetail.id] })
          }
        }}
      />
    </Space>
  )
}

export default TaskCreate
