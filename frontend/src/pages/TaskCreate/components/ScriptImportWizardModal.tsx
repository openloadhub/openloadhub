import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Descriptions,
  Empty,
  Input,
  Modal,
  Radio,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  scriptApi,
  type AIK6ScriptDraftResponse,
  type AIK6ScriptHumanFeedback,
  type AIK6ScriptRiskItem,
  type AIK6ScriptReviewResponse,
  type CurlToK6PreviewResponse,
  type CurlToK6ScriptCreateResponse,
  type CurlToK6VariableSuggestion,
  type HarToK6EntryItem,
  type HarToK6PreviewResponse,
  type HarToK6ScriptCreateResponse,
  type HarToK6SpecParseResponse,
  type OpenApiToK6EndpointItem,
  type OpenApiToK6PreviewResponse,
  type OpenApiToK6ScriptCreateResponse,
  type OpenApiToK6SpecParseResponse,
  type Script,
} from '@/services/scriptApi'
import { publicAlphaFeatures } from '@/config/publicAlpha'

const { TextArea } = Input
const { Text } = Typography

export type ScriptImportMode = 'curl' | 'openapi' | 'har' | 'ai'

type ScriptImportSuccess =
  | CurlToK6ScriptCreateResponse
  | OpenApiToK6ScriptCreateResponse
  | HarToK6ScriptCreateResponse
  | Script

interface PreviewState {
  mode: ScriptImportMode
  details: Array<{ key: string; label: string; value?: string | number | null }>
  queryItems: Array<{ key: string; value: string }>
  headerItems: Array<{ key: string; value: string }>
  bodyItems: Array<{ key: string; value: string }>
  bodyPreview?: string | null
  variables: CurlToK6VariableSuggestion[]
  warnings: string[]
  scriptContent: string
  scriptName?: string | null
  sourceType?: string
  parsedSource?: Record<string, unknown> | null
  aiReview?: AIK6ScriptReviewResponse | null
  llmGenerated?: boolean
  finalScriptStatus?: string
}

interface ScriptImportWizardModalProps {
  open: boolean
  initialName?: string
  allowedModes?: ScriptImportMode[]
  onCancel: () => void
  onSuccess: (mode: ScriptImportMode, result: ScriptImportSuccess) => void
}

type ScriptImportHistoryItem = {
  id: string
  mode: ScriptImportMode
  scriptName: string
  importedAt: string
  sourceSummary: string
}

const SIMPLE_COLUMNS = [
  { title: '字段', dataIndex: 'key', key: 'key', width: 180 },
  { title: '值', dataIndex: 'value', key: 'value' },
]

const SCRIPT_IMPORT_HISTORY_STORAGE_KEY = 'ptp.taskCreate.scriptImportWizard.history.v1'
const SCRIPT_IMPORT_HISTORY_LIMIT = 8

const endpointRowKey = (item: OpenApiToK6EndpointItem) => `${item.method}:${item.path}`
const entryRowKey = (item: HarToK6EntryItem) => String(item.index)

const buildServerOptions = (
  parseResult: OpenApiToK6SpecParseResponse | null,
  endpoint?: OpenApiToK6EndpointItem | null
): string[] => {
  const values = [endpoint?.server_url, ...(parseResult?.server_urls || [])]
  return values.filter(
    (item, index, array): item is string => Boolean(item) && array.indexOf(item) === index
  )
}

const resolveServerSelection = (
  parseResult: OpenApiToK6SpecParseResponse | null,
  endpoint?: OpenApiToK6EndpointItem | null,
  current?: string
): string | undefined => {
  const options = buildServerOptions(parseResult, endpoint)
  if (current && options.includes(current)) {
    return current
  }
  return options[0]
}

const detectImportMode = (content: string): ScriptImportMode | null => {
  const raw = content.trim()
  if (!raw) {
    return null
  }
  if (/^curl\s+/i.test(raw)) {
    return 'curl'
  }
  try {
    const parsed = JSON.parse(raw)
    if (parsed?.log?.entries && Array.isArray(parsed.log.entries)) {
      return 'har'
    }
    if (parsed?.openapi || parsed?.swagger || parsed?.paths) {
      return 'openapi'
    }
  } catch {
    // YAML/OpenAPI and shell snippets are handled by textual heuristics below.
  }
  if (
    /^\s*openapi\s*:/im.test(raw) ||
    /^\s*swagger\s*:/im.test(raw) ||
    /^\s*paths\s*:/im.test(raw)
  ) {
    return 'openapi'
  }
  return null
}

const getRiskItemColor = (severity?: string | null) => {
  const normalized = severity?.toLowerCase()
  if (normalized === 'critical' || normalized === 'high') {
    return 'red'
  }
  if (normalized === 'medium') {
    return 'orange'
  }
  if (normalized === 'low') {
    return 'green'
  }
  return 'default'
}

const getModeLabel = (value: ScriptImportMode) => {
  if (value === 'curl') {
    return 'cURL'
  }
  if (value === 'openapi') {
    return 'OpenAPI'
  }
  if (value === 'har') {
    return 'HAR'
  }
  return '接口描述（AI）'
}

const safePathSummary = (rawUrl?: string | null) => {
  if (!rawUrl) {
    return '-'
  }
  try {
    const parsed = new URL(rawUrl)
    return `${parsed.origin}${parsed.pathname}`
  } catch {
    return rawUrl.split('?')[0] || rawUrl
  }
}

const normalizeStoredHistory = (value: unknown): ScriptImportHistoryItem[] => {
  if (!Array.isArray(value)) {
    return []
  }
  return value
    .filter((item): item is ScriptImportHistoryItem => {
      if (!item || typeof item !== 'object') {
        return false
      }
      const raw = item as Record<string, unknown>
      return (
        typeof raw.id === 'string' &&
        ['curl', 'openapi', 'har', 'ai'].includes(String(raw.mode)) &&
        typeof raw.scriptName === 'string' &&
        typeof raw.importedAt === 'string' &&
        typeof raw.sourceSummary === 'string'
      )
    })
    .slice(0, SCRIPT_IMPORT_HISTORY_LIMIT)
}

const readImportHistory = (): ScriptImportHistoryItem[] => {
  if (typeof window === 'undefined') {
    return []
  }
  try {
    return normalizeStoredHistory(
      JSON.parse(window.localStorage.getItem(SCRIPT_IMPORT_HISTORY_STORAGE_KEY) || '[]')
    )
  } catch {
    return []
  }
}

const writeImportHistory = (items: ScriptImportHistoryItem[]) => {
  if (typeof window === 'undefined') {
    return
  }
  try {
    window.localStorage.setItem(
      SCRIPT_IMPORT_HISTORY_STORAGE_KEY,
      JSON.stringify(items.slice(0, SCRIPT_IMPORT_HISTORY_LIMIT))
    )
  } catch {
    // Import success must not depend on browser storage availability.
  }
}

const buildHistorySourceSummary = (
  mode: ScriptImportMode,
  result: ScriptImportSuccess,
  preview: PreviewState | null
) => {
  if (mode === 'curl') {
    const parsed = (result as CurlToK6ScriptCreateResponse).parsed
    return `${parsed.method} ${safePathSummary(parsed.url)}`
  }
  if (mode === 'openapi') {
    const parsed = (result as OpenApiToK6ScriptCreateResponse).parsed
    return `${parsed.method} ${parsed.path}${parsed.server_url ? ` @ ${safePathSummary(parsed.server_url)}` : ''}`
  }
  if (mode === 'har') {
    const parsed = (result as HarToK6ScriptCreateResponse).parsed
    return `${parsed.method} ${safePathSummary(parsed.url)}`
  }
  const aiSource = preview?.sourceType || 'interface-description'
  const aiStatus = preview?.aiReview?.risk_level ? ` / risk ${preview.aiReview.risk_level}` : ''
  return `${aiSource}${aiStatus}`
}

const resolveHistoryScriptName = (
  mode: ScriptImportMode,
  result: ScriptImportSuccess,
  preview: PreviewState | null
) => {
  if (mode === 'ai') {
    return ((result as Script).name || preview?.scriptName || 'AI K6 草稿').trim()
  }
  const response = result as
    | CurlToK6ScriptCreateResponse
    | OpenApiToK6ScriptCreateResponse
    | HarToK6ScriptCreateResponse
  return (
    response.script?.name ||
    response.parsed?.suggested_task_name ||
    preview?.scriptName ||
    'K6 导入脚本'
  ).trim()
}

const renderRiskItems = (items?: AIK6ScriptRiskItem[]) => {
  const normalized = (items || []).filter(item => item.message)
  if (normalized.length === 0) {
    return null
  }
  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Text strong>结构化风险</Text>
      <Space direction="vertical" size={6} style={{ width: '100%' }}>
        {normalized.map((item, index) => (
          <Space
            key={`${item.severity}-${item.category}-${index}`}
            size={[6, 4]}
            wrap
            style={{ width: '100%' }}
          >
            <Tag color={getRiskItemColor(item.severity)}>{item.severity || 'unknown'}</Tag>
            <Tag>{item.label || item.category || 'general'}</Tag>
            {item.source ? <Tag color="blue">{item.source}</Tag> : null}
            <Text>{item.message}</Text>
            {item.recommendation ? <Text type="secondary">{item.recommendation}</Text> : null}
          </Space>
        ))}
      </Space>
    </Space>
  )
}

const buildCurlPreview = (preview: CurlToK6PreviewResponse): PreviewState => ({
  mode: 'curl',
  details: [
    { key: 'method', label: '请求方法', value: preview.parsed.method },
    { key: 'url', label: '目标 URL', value: preview.parsed.url },
    { key: 'task_name', label: '建议任务名', value: preview.parsed.suggested_task_name || '-' },
    {
      key: 'body_mode',
      label: 'Body 类型',
      value: preview.parsed.body_present ? preview.parsed.body_mode || 'raw' : '-',
    },
  ],
  queryItems: preview.parsed.query_items,
  headerItems: preview.parsed.header_items,
  bodyItems: preview.parsed.body_items,
  bodyPreview: preview.parsed.body_preview,
  variables: preview.suggested_variables,
  warnings: preview.warnings,
  scriptContent: preview.script_content,
})

const buildOpenApiPreview = (preview: OpenApiToK6PreviewResponse): PreviewState => ({
  mode: 'openapi',
  details: [
    { key: 'method', label: '请求方法', value: preview.parsed.method },
    { key: 'path', label: '接口路径', value: preview.parsed.path },
    { key: 'source_url', label: '目标 URL', value: preview.parsed.source_url },
    { key: 'task_name', label: '建议任务名', value: preview.parsed.suggested_task_name || '-' },
    {
      key: 'content_type',
      label: 'Content-Type',
      value: preview.parsed.request_content_type || '-',
    },
  ],
  queryItems: preview.parsed.query_items,
  headerItems: preview.parsed.header_items,
  bodyItems: preview.parsed.body_items,
  bodyPreview: preview.parsed.body_preview,
  variables: preview.suggested_variables,
  warnings: preview.warnings,
  scriptContent: preview.script_content,
})

const buildHarPreview = (preview: HarToK6PreviewResponse): PreviewState => ({
  mode: 'har',
  details: [
    { key: 'entry', label: '条目', value: preview.parsed.entry_index },
    { key: 'method', label: '请求方法', value: preview.parsed.method },
    { key: 'url', label: '目标 URL', value: preview.parsed.url },
    { key: 'task_name', label: '建议任务名', value: preview.parsed.suggested_task_name || '-' },
    { key: 'mime', label: 'MIME', value: preview.parsed.mime_type || '-' },
  ],
  queryItems: preview.parsed.query_items,
  headerItems: preview.parsed.header_items,
  bodyItems: preview.parsed.body_items,
  bodyPreview: preview.parsed.body_preview,
  variables: preview.suggested_variables,
  warnings: preview.warnings,
  scriptContent: preview.script_content,
})

const ScriptImportWizardModal: React.FC<ScriptImportWizardModalProps> = ({
  open,
  initialName,
  allowedModes,
  onCancel,
  onSuccess,
}) => {
  const availableModes = useMemo(() => {
    const baseModes: ScriptImportMode[] = publicAlphaFeatures.aiFeatures
      ? ['curl', 'openapi', 'har', 'ai']
      : ['curl', 'openapi', 'har']
    const requestedModes = allowedModes && allowedModes.length > 0 ? allowedModes : baseModes
    return requestedModes.filter(
      (item, index, array) => baseModes.includes(item) && array.indexOf(item) === index
    )
  }, [allowedModes])
  const modeOptions = useMemo(
    () => availableModes.map(item => ({ label: getModeLabel(item), value: item })),
    [availableModes]
  )
  const supportsMode = (value: ScriptImportMode | null): value is ScriptImportMode =>
    Boolean(value && availableModes.includes(value))
  const [mode, setMode] = useState<ScriptImportMode>('curl')
  const [sourceContent, setSourceContent] = useState('')
  const [scriptName, setScriptName] = useState('')
  const [openApiParse, setOpenApiParse] = useState<OpenApiToK6SpecParseResponse | null>(null)
  const [harParse, setHarParse] = useState<HarToK6SpecParseResponse | null>(null)
  const [selectedEndpointKey, setSelectedEndpointKey] = useState<string>()
  const [selectedServerUrl, setSelectedServerUrl] = useState<string>()
  const [selectedEntryIndex, setSelectedEntryIndex] = useState<number>()
  const [preview, setPreview] = useState<PreviewState | null>(null)
  const [parseLoading, setParseLoading] = useState(false)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [feedbackRating, setFeedbackRating] = useState<AIK6ScriptHumanFeedback['rating']>('useful')
  const [feedbackAction, setFeedbackAction] =
    useState<AIK6ScriptHumanFeedback['action']>('accepted')
  const [feedbackNote, setFeedbackNote] = useState('')
  const [importHistory, setImportHistory] = useState<ScriptImportHistoryItem[]>([])

  useEffect(() => {
    if (!open) {
      return
    }
    setImportHistory(readImportHistory())
    setMode(availableModes[0] || 'curl')
    setSourceContent('')
    setScriptName(initialName || '')
    setOpenApiParse(null)
    setHarParse(null)
    setSelectedEndpointKey(undefined)
    setSelectedServerUrl(undefined)
    setSelectedEntryIndex(undefined)
    setPreview(null)
    setFeedbackRating('useful')
    setFeedbackAction('accepted')
    setFeedbackNote('')
  }, [availableModes, initialName, open])

  const persistImportHistoryItem = (successMode: ScriptImportMode, result: ScriptImportSuccess) => {
    const nextItem: ScriptImportHistoryItem = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      mode: successMode,
      scriptName: resolveHistoryScriptName(successMode, result, preview),
      importedAt: new Date().toISOString(),
      sourceSummary: buildHistorySourceSummary(successMode, result, preview),
    }
    const nextItems = [
      nextItem,
      ...importHistory.filter(
        item =>
          item.mode !== nextItem.mode ||
          item.scriptName !== nextItem.scriptName ||
          item.sourceSummary !== nextItem.sourceSummary
      ),
    ].slice(0, SCRIPT_IMPORT_HISTORY_LIMIT)
    setImportHistory(nextItems)
    writeImportHistory(nextItems)
  }

  const handleReuseHistory = (item: ScriptImportHistoryItem) => {
    if (!supportsMode(item.mode)) {
      message.warning('当前入口暂不支持该导入模式')
      return
    }
    setMode(item.mode)
    setScriptName(item.scriptName)
    setSourceContent('')
    resetParsedState()
    message.info('已回填导入模式和脚本名称；本地历史不保存来源正文，请重新粘贴后再生成')
  }

  const handleClearHistory = () => {
    setImportHistory([])
    writeImportHistory([])
    message.success('已清空本地导入历史')
  }

  const selectedEndpoint = useMemo(() => {
    if (!openApiParse || !selectedEndpointKey) {
      return null
    }
    return openApiParse.endpoints.find(item => endpointRowKey(item) === selectedEndpointKey) || null
  }, [openApiParse, selectedEndpointKey])

  const serverOptions = useMemo(
    () =>
      buildServerOptions(openApiParse, selectedEndpoint).map(item => ({
        label: item,
        value: item,
      })),
    [openApiParse, selectedEndpoint]
  )

  const selectedEntry = useMemo(() => {
    if (!harParse || selectedEntryIndex == null) {
      return null
    }
    return harParse.entries.find(item => item.index === selectedEntryIndex) || null
  }, [harParse, selectedEntryIndex])

  const resetParsedState = () => {
    setOpenApiParse(null)
    setHarParse(null)
    setSelectedEndpointKey(undefined)
    setSelectedServerUrl(undefined)
    setSelectedEntryIndex(undefined)
    setPreview(null)
    setFeedbackRating('useful')
    setFeedbackAction('accepted')
    setFeedbackNote('')
  }

  const handleDetect = () => {
    const detected = detectImportMode(sourceContent)
    if (!supportsMode(detected)) {
      message.warning(
        publicAlphaFeatures.aiFeatures
          ? `暂未识别来源类型，请手动选择 ${availableModes.map(getModeLabel).join(' / ')}`
          : `暂未识别来源类型，请手动选择 ${availableModes.map(getModeLabel).join(' / ')}`
      )
      return
    }
    setMode(detected)
    resetParsedState()
    message.success(
      `已识别为 ${detected === 'curl' ? 'cURL' : detected === 'openapi' ? 'OpenAPI' : 'HAR'}`
    )
  }

  const handleParse = async () => {
    if (!sourceContent.trim()) {
      message.warning('请先粘贴导入内容')
      return
    }
    if (!supportsMode(mode)) {
      message.warning('当前入口暂不支持该导入模式')
      return
    }
    if (mode === 'curl') {
      await handlePreview()
      return
    }
    if (mode === 'ai') {
      if (!publicAlphaFeatures.aiFeatures) {
        message.warning('AI 脚本辅助未在 public alpha 开放')
        return
      }
      await handleAIDraft()
      return
    }

    setParseLoading(true)
    try {
      if (mode === 'openapi') {
        const result = await scriptApi.parseOpenApiSpec({ spec_content: sourceContent.trim() })
        const firstSupported =
          result.endpoints.find(item => item.request_body_supported) || result.endpoints[0]
        setOpenApiParse(result)
        setHarParse(null)
        setSelectedEndpointKey(firstSupported ? endpointRowKey(firstSupported) : undefined)
        setSelectedServerUrl(resolveServerSelection(result, firstSupported))
        setSelectedEntryIndex(undefined)
        setPreview(null)
        message.success(
          `已解析 ${result.endpoints.length} 个接口，可生成 ${result.supported_endpoint_count} 个`
        )
        return
      }
      const result = await scriptApi.parseHarSpec({ har_content: sourceContent.trim() })
      setHarParse(result)
      setOpenApiParse(null)
      setSelectedEndpointKey(undefined)
      setSelectedServerUrl(undefined)
      setSelectedEntryIndex(result.entries[0]?.index)
      setPreview(null)
      message.success(`已解析 ${result.entries.length} 个可生成条目`)
    } catch {
      resetParsedState()
    } finally {
      setParseLoading(false)
    }
  }

  const handlePreview = async () => {
    if (!sourceContent.trim()) {
      message.warning('请先粘贴导入内容')
      return
    }
    if (!supportsMode(mode)) {
      message.warning('当前入口暂不支持该导入模式')
      return
    }

    setPreviewLoading(true)
    try {
      if (mode === 'ai') {
        await handleAIDraft()
        return
      }
      if (mode === 'curl') {
        const result = await scriptApi.previewK6FromCurl({
          curl_command: sourceContent.trim(),
          name: scriptName.trim() || undefined,
        })
        setPreview(buildCurlPreview(result))
        message.success('cURL 单接口草稿已生成')
        return
      }
      if (mode === 'openapi') {
        if (!selectedEndpoint) {
          message.warning('请先解析并选择一个接口')
          return
        }
        if (!selectedEndpoint.request_body_supported) {
          message.warning('当前接口的请求体类型暂不支持自动生成')
          return
        }
        const result = await scriptApi.previewK6FromOpenApi({
          spec_content: sourceContent.trim(),
          path: selectedEndpoint.path,
          method: selectedEndpoint.method,
          name: scriptName.trim() || undefined,
          server_url: selectedServerUrl || selectedEndpoint.server_url || undefined,
        })
        setPreview(buildOpenApiPreview(result))
        message.success('OpenAPI 单接口草稿已生成')
        return
      }
      if (selectedEntryIndex == null) {
        message.warning('请先解析并选择一个 HAR 条目')
        return
      }
      const result = await scriptApi.previewK6FromHar({
        har_content: sourceContent.trim(),
        entry_index: selectedEntryIndex,
        name: scriptName.trim() || undefined,
      })
      setPreview(buildHarPreview(result))
      message.success('HAR 单接口草稿已生成')
    } catch {
      setPreview(null)
    } finally {
      setPreviewLoading(false)
    }
  }

  const buildAIDraftPayload = () => {
    const trimmedName = scriptName.trim() || undefined
    if (mode === 'openapi') {
      if (!selectedEndpoint) {
        throw new Error('请先解析并选择一个接口')
      }
      if (!selectedEndpoint.request_body_supported) {
        throw new Error('当前接口的请求体类型暂不支持自动生成')
      }
      return {
        source_type: 'openapi',
        source_content: sourceContent.trim(),
        path: selectedEndpoint.path,
        method: selectedEndpoint.method,
        name: trimmedName,
        server_url: selectedServerUrl || selectedEndpoint.server_url || undefined,
      }
    }
    if (mode === 'har') {
      if (selectedEntryIndex == null) {
        throw new Error('请先解析并选择一个 HAR 条目')
      }
      return {
        source_type: 'har',
        source_content: sourceContent.trim(),
        entry_index: selectedEntryIndex,
        name: trimmedName,
      }
    }
    if (mode === 'ai') {
      return {
        source_type: 'interface-description',
        source_content: sourceContent.trim(),
        name: trimmedName,
      }
    }
    return {
      source_type: 'curl',
      source_content: sourceContent.trim(),
      name: trimmedName,
    }
  }

  const buildAIPreview = (draft: AIK6ScriptDraftResponse): PreviewState => {
    const parsed = draft.parsed_source || {}
    const details = [
      { key: 'source_type', label: '来源类型', value: draft.source_type },
      { key: 'script_name', label: '脚本名称', value: draft.script_name || '-' },
      { key: 'status', label: 'AI 状态', value: draft.status },
      { key: 'risk', label: '风险等级', value: draft.ai_review.risk_level || '-' },
      { key: 'final_status', label: '保存状态', value: draft.final_script_status },
    ]
    const queryItems = Array.isArray(parsed.query_items)
      ? (parsed.query_items as Array<{ key: string; value: string }>)
      : []
    const headerItems = Array.isArray(parsed.header_items)
      ? (parsed.header_items as Array<{ key: string; value: string }>)
      : []
    const bodyItems = Array.isArray(parsed.body_items)
      ? (parsed.body_items as Array<{ key: string; value: string }>)
      : []
    return {
      mode,
      details,
      queryItems,
      headerItems,
      bodyItems,
      bodyPreview: typeof parsed.body_preview === 'string' ? parsed.body_preview : undefined,
      variables: draft.suggested_variables,
      warnings: draft.warnings,
      scriptContent: draft.script_content,
      scriptName: draft.script_name,
      sourceType: draft.source_type,
      parsedSource: draft.parsed_source || null,
      aiReview: draft.ai_review,
      llmGenerated: draft.llm_generated,
      finalScriptStatus: draft.final_script_status,
    }
  }

  const handleAIDraft = async () => {
    if (!publicAlphaFeatures.aiFeatures) {
      message.warning('AI 脚本辅助未在 public alpha 开放')
      return
    }
    if (!sourceContent.trim()) {
      message.warning('请先输入接口来源或描述')
      return
    }
    setPreviewLoading(true)
    try {
      const draft = await scriptApi.draftAIK6Script(buildAIDraftPayload())
      setPreview(buildAIPreview(draft))
      setFeedbackRating('useful')
      setFeedbackAction('accepted')
      setFeedbackNote('')
      if (draft.status === 'success') {
        message.success(draft.llm_generated ? 'AI K6 草稿已生成' : 'K6 草稿已完成 AI 评审')
      } else {
        message.warning(
          draft.ai_review.error_message || 'AI K6 草稿生成未成功，请检查 AI provider 配置'
        )
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : 'AI K6 草稿生成失败')
      setPreview(null)
    } finally {
      setPreviewLoading(false)
    }
  }

  const confirmSaveAIScript = async (): Promise<Script> => {
    if (!preview?.scriptContent.trim()) {
      throw new Error('请先生成 AI K6 草稿')
    }
    const name = (preview.scriptName || scriptName || 'ai-k6-draft').trim()
    const humanFeedback: AIK6ScriptHumanFeedback = {
      rating: feedbackRating,
      ...(feedbackAction ? { action: feedbackAction } : {}),
      ...(feedbackNote.trim() ? { note: feedbackNote.trim() } : {}),
    }
    return scriptApi.confirmSaveAIK6Script({
      script_name: name,
      script_content: preview.scriptContent,
      source_type: preview.sourceType || 'interface-description',
      parsed_source: preview.parsedSource || null,
      ai_review: preview.aiReview || null,
      llm_generated: Boolean(preview.llmGenerated),
      warnings: preview.warnings,
      suggested_variables: preview.variables,
      human_confirmation_status: 'confirmed',
      final_script_status: 'confirmed_saved',
      human_feedback: humanFeedback,
    })
  }

  const handleConfirm = async () => {
    if (!sourceContent.trim()) {
      message.warning('请先粘贴导入内容')
      return
    }
    if (!supportsMode(mode)) {
      message.warning('当前入口暂不支持该导入模式')
      return
    }

    setSubmitting(true)
    try {
      if (mode === 'ai') {
        if (!publicAlphaFeatures.aiFeatures) {
          message.warning('AI 脚本辅助未在 public alpha 开放')
          return
        }
        const result = await confirmSaveAIScript()
        persistImportHistoryItem('ai', result)
        onSuccess('ai', result)
        return
      }
      if (mode === 'curl') {
        const result = await scriptApi.generateK6FromCurl({
          curl_command: sourceContent.trim(),
          name: scriptName.trim() || undefined,
        })
        persistImportHistoryItem('curl', result)
        onSuccess('curl', result)
        return
      }
      if (mode === 'openapi') {
        if (!selectedEndpoint) {
          message.warning('请先解析并选择一个接口')
          return
        }
        if (!selectedEndpoint.request_body_supported) {
          message.warning('当前接口的请求体类型暂不支持自动生成')
          return
        }
        const result = await scriptApi.generateK6FromOpenApi({
          spec_content: sourceContent.trim(),
          path: selectedEndpoint.path,
          method: selectedEndpoint.method,
          name: scriptName.trim() || undefined,
          server_url: selectedServerUrl || selectedEndpoint.server_url || undefined,
        })
        persistImportHistoryItem('openapi', result)
        onSuccess('openapi', result)
        return
      }
      if (selectedEntryIndex == null) {
        message.warning('请先解析并选择一个 HAR 条目')
        return
      }
      const result = await scriptApi.generateK6FromHar({
        har_content: sourceContent.trim(),
        entry_index: selectedEntryIndex,
        name: scriptName.trim() || undefined,
      })
      persistImportHistoryItem('har', result)
      onSuccess('har', result)
    } catch {
      // request.ts already surfaces the user-facing error message
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      title={
        availableModes.length === 2 &&
        availableModes.includes('curl') &&
        availableModes.includes('har')
          ? '从 cURL / HAR 导入 K6 脚本'
          : '统一导入生成 K6 脚本'
      }
      open={open}
      onCancel={onCancel}
      destroyOnClose
      width={980}
      footer={[
        <Button key="cancel" onClick={onCancel}>
          取消
        </Button>,
        <Button key="detect" onClick={handleDetect}>
          智能识别
        </Button>,
        <Button key="parse" onClick={handleParse} loading={parseLoading}>
          {mode === 'curl' ? '解析预览' : '解析来源'}
        </Button>,
        publicAlphaFeatures.aiFeatures ? (
          <Button key="ai" onClick={handleAIDraft} loading={previewLoading}>
            AI 生成/评审
          </Button>
        ) : null,
        <Button key="preview" onClick={handlePreview} loading={previewLoading}>
          预览脚本
        </Button>,
        <Button key="ok" type="primary" onClick={handleConfirm} loading={submitting}>
          生成并使用
        </Button>,
      ]}
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Alert
          type="info"
          showIcon
          message={
            availableModes.length === 2 &&
            availableModes.includes('curl') &&
            availableModes.includes('har')
              ? '一个入口承载 cURL / HAR'
              : '一个入口承载 cURL / OpenAPI / HAR'
          }
          description={
            availableModes.length === 2 &&
            availableModes.includes('curl') &&
            availableModes.includes('har')
              ? '本向导支持从 cURL 命令或浏览器 HAR 文件生成单接口 K6 草稿脚本，并回填脚本变量。'
              : publicAlphaFeatures.aiFeatures
                ? '支持从 cURL、OpenAPI 或 HAR 生成单接口 K6 草稿；AI 入口只生成或评审草稿，必须人工确认后才保存为脚本。复杂编排、文件上传和自动鉴权后续再补。'
                : '支持从 cURL、OpenAPI 或 HAR 生成单接口 K6 草稿，并完成来源识别、单接口选择、脚本预览和保存。复杂编排、文件上传和自动鉴权仍需人工处理。'
          }
        />

        <Alert
          type="success"
          showIcon
          message="最近导入历史仅保存在当前浏览器"
          description="历史只记录导入模式、脚本名、保存时间和来源摘要，不保存来源原文、Header、Body、token 或 API key。复用历史会回填模式和脚本名，来源正文需要重新粘贴。"
        />

        {importHistory.length > 0 ? (
          <Table
            size="small"
            rowKey="id"
            pagination={false}
            dataSource={importHistory}
            columns={[
              {
                title: '模式',
                dataIndex: 'mode',
                key: 'mode',
                width: 120,
                render: value => <Tag color="blue">{getModeLabel(value as ScriptImportMode)}</Tag>,
              },
              {
                title: '脚本名',
                dataIndex: 'scriptName',
                key: 'scriptName',
                width: 180,
                ellipsis: true,
              },
              {
                title: '来源摘要',
                dataIndex: 'sourceSummary',
                key: 'sourceSummary',
                ellipsis: true,
              },
              {
                title: '时间',
                dataIndex: 'importedAt',
                key: 'importedAt',
                width: 180,
                render: value => new Date(String(value)).toLocaleString(),
              },
              {
                title: '操作',
                key: 'action',
                width: 150,
                render: (_, item: ScriptImportHistoryItem) => (
                  <Button size="small" onClick={() => handleReuseHistory(item)}>
                    复用
                  </Button>
                ),
              },
            ]}
            footer={() => (
              <Button size="small" onClick={handleClearHistory}>
                清空本地历史
              </Button>
            )}
          />
        ) : null}

        <Radio.Group
          optionType="button"
          buttonStyle="solid"
          value={mode}
          onChange={event => {
            setMode(event.target.value)
            resetParsedState()
          }}
          options={modeOptions}
        />

        <div>
          <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
            脚本名称（可选）
          </Text>
          <Input
            placeholder="默认根据 method + path 自动生成"
            value={scriptName}
            onChange={event => {
              setScriptName(event.target.value)
              setPreview(null)
            }}
          />
        </div>

        <div>
          <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
            导入内容
          </Text>
          <TextArea
            rows={10}
            placeholder={
              mode === 'ai'
                ? '描述接口、鉴权、请求体、断言和压测目标，例如：GET /orders，带 Bearer token，检查 HTTP 200 和返回字段 status=ok'
                : availableModes.length === 2 &&
                    availableModes.includes('curl') &&
                    availableModes.includes('har')
                  ? '粘贴 cURL 命令或浏览器导出的 HAR JSON'
                  : '粘贴 cURL 命令、OpenAPI JSON/YAML 或浏览器导出的 HAR JSON'
            }
            value={sourceContent}
            onChange={event => {
              setSourceContent(event.target.value)
              resetParsedState()
            }}
          />
        </div>

        {mode === 'openapi' && openApiParse ? (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Descriptions
              size="small"
              column={3}
              items={[
                { key: 'title', label: '接口文档', children: openApiParse.title || '-' },
                { key: 'count', label: '接口数量', children: openApiParse.endpoints.length },
                {
                  key: 'supported',
                  label: '可生成',
                  children: openApiParse.supported_endpoint_count,
                },
              ]}
            />
            <Table
              size="small"
              rowKey={endpointRowKey}
              pagination={{ pageSize: 5 }}
              rowSelection={{
                type: 'radio',
                selectedRowKeys: selectedEndpointKey ? [selectedEndpointKey] : [],
                onChange: keys => {
                  const nextKey = String(keys[0] || '')
                  const nextEndpoint =
                    openApiParse.endpoints.find(item => endpointRowKey(item) === nextKey) || null
                  setSelectedEndpointKey(nextKey || undefined)
                  setSelectedServerUrl(resolveServerSelection(openApiParse, nextEndpoint))
                  setPreview(null)
                },
              }}
              columns={[
                { title: '方法', dataIndex: 'method', key: 'method', width: 90 },
                { title: '接口路径', dataIndex: 'path', key: 'path', width: 260 },
                { title: '接口说明', dataIndex: 'summary', key: 'summary', ellipsis: true },
                {
                  title: '状态',
                  key: 'supported',
                  width: 110,
                  render: (_, item: OpenApiToK6EndpointItem) =>
                    item.request_body_supported ? (
                      <Tag color="green">可生成</Tag>
                    ) : (
                      <Tag color="orange">需人工处理</Tag>
                    ),
                },
              ]}
              dataSource={openApiParse.endpoints}
              locale={{
                emptyText: (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有接口" />
                ),
              }}
            />
            {serverOptions.length > 0 ? (
              <Select
                style={{ width: '100%' }}
                value={selectedServerUrl}
                options={serverOptions}
                onChange={value => {
                  setSelectedServerUrl(value)
                  setPreview(null)
                }}
                placeholder="选择服务地址"
              />
            ) : null}
            {openApiParse.warnings.length > 0 ? (
              <Alert
                type="warning"
                showIcon
                message="解析提示"
                description={openApiParse.warnings.join('；')}
              />
            ) : null}
          </Space>
        ) : null}

        {mode === 'har' && harParse ? (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Space wrap>
              <Tag color="blue">{`可生成 ${harParse.supported_entry_count}`}</Tag>
              <Tag>{`跳过 ${harParse.unsupported_entry_count}`}</Tag>
            </Space>
            <Table
              size="small"
              rowKey={entryRowKey}
              pagination={{ pageSize: 5 }}
              rowSelection={{
                type: 'radio',
                selectedRowKeys: selectedEntryIndex != null ? [String(selectedEntryIndex)] : [],
                onChange: keys => {
                  const next = Number(keys[0])
                  setSelectedEntryIndex(Number.isFinite(next) ? next : undefined)
                  setPreview(null)
                },
              }}
              columns={[
                { title: '#', dataIndex: 'index', key: 'index', width: 64 },
                { title: '方法', dataIndex: 'method', key: 'method', width: 90 },
                { title: '路径', dataIndex: 'path', key: 'path', width: 220 },
                { title: '完整地址', dataIndex: 'url', key: 'url', ellipsis: true },
                { title: '状态', dataIndex: 'status', key: 'status', width: 90 },
                {
                  title: '请求体',
                  dataIndex: 'body_present',
                  key: 'body_present',
                  width: 90,
                  render: value => (value ? <Tag color="orange">有</Tag> : <Tag>无</Tag>),
                },
              ]}
              dataSource={harParse.entries}
              locale={{
                emptyText: (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有可生成条目" />
                ),
              }}
            />
            {selectedEntry ? (
              <Alert
                type="success"
                showIcon
                message={`已选择 ${selectedEntry.method} ${selectedEntry.path}`}
                description={selectedEntry.url}
              />
            ) : null}
            {harParse.warnings.length > 0 ? (
              <Alert
                type="warning"
                showIcon
                message="解析提示"
                description={harParse.warnings.join('；')}
              />
            ) : null}
          </Space>
        ) : null}

        {preview ? (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions
              size="small"
              column={1}
              items={preview.details.map(item => ({
                key: item.key,
                label: item.label,
                children: item.value ?? '-',
              }))}
            />
            <div>
              <Text type="secondary">建议变量</Text>
              <div style={{ marginTop: 8 }}>
                <Space wrap size={[8, 8]}>
                  {preview.variables.length > 0 ? (
                    preview.variables.map(item => (
                      <Space key={item.key} size={[6, 6]} wrap>
                        <Tag color={item.sensitive ? 'orange' : 'blue'}>{item.key}</Tag>
                        {item.source ? <Text type="secondary">{item.source}</Text> : null}
                      </Space>
                    ))
                  ) : (
                    <Text type="secondary">无</Text>
                  )}
                </Space>
              </div>
            </div>
            <Table
              size="small"
              rowKey={record => `${record.key}-${record.value}`}
              pagination={false}
              columns={SIMPLE_COLUMNS}
              dataSource={preview.queryItems}
              title={() => 'Query 参数'}
              locale={{
                emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无" />,
              }}
            />
            <Table
              size="small"
              rowKey={record => `${record.key}-${record.value}`}
              pagination={false}
              columns={SIMPLE_COLUMNS}
              dataSource={preview.headerItems}
              title={() => '请求 Headers'}
              locale={{
                emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无" />,
              }}
            />
            <Table
              size="small"
              rowKey={record => `${record.key}-${record.value}`}
              pagination={false}
              columns={SIMPLE_COLUMNS}
              dataSource={preview.bodyItems}
              title={() => '请求体字段'}
              locale={{
                emptyText: (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无结构化字段" />
                ),
              }}
            />
            {preview.bodyPreview ? (
              <div>
                <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
                  请求体原始预览
                </Text>
                <TextArea rows={5} value={preview.bodyPreview} readOnly />
              </div>
            ) : null}
            {preview.warnings.length > 0 ? (
              <Alert
                type="warning"
                showIcon
                message="生成提示"
                description={preview.warnings.join('；')}
              />
            ) : null}
            {preview.aiReview ? (
              <Space direction="vertical" size={10} style={{ width: '100%' }}>
                <Alert
                  type={preview.aiReview.status === 'success' ? 'info' : 'warning'}
                  showIcon
                  message={`AI 评审：${preview.aiReview.risk_level || '未知风险'}`}
                  description={
                    preview.aiReview.summary ||
                    preview.aiReview.error_message ||
                    preview.aiReview.disclaimer
                  }
                />
                <Space wrap>
                  {preview.aiReview.provider ? <Tag>{preview.aiReview.provider}</Tag> : null}
                  {preview.aiReview.model ? <Tag>{preview.aiReview.model}</Tag> : null}
                  {typeof preview.aiReview.latency_ms === 'number' ? (
                    <Tag>{`${preview.aiReview.latency_ms} ms`}</Tag>
                  ) : null}
                  {preview.llmGenerated ? (
                    <Tag color="purple">LLM 草稿</Tag>
                  ) : (
                    <Tag color="blue">规则草稿 + AI 评审</Tag>
                  )}
                  {preview.finalScriptStatus ? (
                    <Tag color="orange">{preview.finalScriptStatus}</Tag>
                  ) : null}
                </Space>
                {renderRiskItems(preview.aiReview.risk_items)}
                {preview.aiReview.static_findings.length > 0 ? (
                  <Table
                    size="small"
                    rowKey={record => `${record.code}-${record.message}`}
                    pagination={false}
                    columns={[
                      { title: '级别', dataIndex: 'severity', key: 'severity', width: 100 },
                      { title: '代码', dataIndex: 'code', key: 'code', width: 180 },
                      { title: '说明', dataIndex: 'message', key: 'message' },
                    ]}
                    dataSource={preview.aiReview.static_findings}
                  />
                ) : null}
                {preview.aiReview.risks.length > 0 ? (
                  <Alert
                    type="warning"
                    showIcon
                    message="风险"
                    description={preview.aiReview.risks.join('；')}
                  />
                ) : null}
                {preview.aiReview.improvements.length > 0 ? (
                  <Alert
                    type="info"
                    showIcon
                    message="改进建议"
                    description={preview.aiReview.improvements.join('；')}
                  />
                ) : null}
                {mode === 'ai' ? (
                  <Space direction="vertical" size={8} style={{ width: '100%' }}>
                    <Text strong>人工反馈</Text>
                    <Space size={[8, 8]} wrap>
                      <Text type="secondary">rating</Text>
                      <Radio.Group
                        optionType="button"
                        size="small"
                        value={feedbackRating}
                        onChange={event => setFeedbackRating(event.target.value)}
                        options={[
                          { label: 'useful', value: 'useful' },
                          { label: 'not useful', value: 'not_useful' },
                          { label: 'needs changes', value: 'needs_changes' },
                        ]}
                      />
                      <Text type="secondary">action</Text>
                      <Radio.Group
                        optionType="button"
                        size="small"
                        value={feedbackAction}
                        onChange={event => setFeedbackAction(event.target.value)}
                        options={[
                          { label: 'accepted', value: 'accepted' },
                          { label: 'needs revision', value: 'needs_revision' },
                          { label: 'ignored', value: 'ignored' },
                        ]}
                      />
                    </Space>
                    <TextArea
                      rows={2}
                      value={feedbackNote}
                      placeholder="人工反馈备注（可选）"
                      onChange={event => setFeedbackNote(event.target.value)}
                    />
                  </Space>
                ) : null}
                <Alert type="info" showIcon message={preview.aiReview.disclaimer} />
              </Space>
            ) : null}
            <div>
              <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
                生成脚本预览
              </Text>
              <TextArea rows={10} value={preview.scriptContent} readOnly />
            </div>
          </Space>
        ) : null}
      </Space>
    </Modal>
  )
}

export default ScriptImportWizardModal
