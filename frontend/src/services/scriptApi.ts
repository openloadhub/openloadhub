import request from '../utils/request.ts'
import type { PaginationParams, PaginationResponse } from '../types'

export interface Script {
  id: number
  name: string
  description?: string | null
  script_type: 'JMETER' | 'K6'
  file_path: string
  file_size?: number | null
  content_hash?: string | null
  version: string
  status: 'ACTIVE' | 'INACTIVE' | 'DELETED'
  tags?: string[] | null
  parameters?: Record<string, unknown> | null
  created_by?: number | null
  created_at: string
  updated_at?: string | null
  last_used_at?: string | null
}

export interface ScriptQuery extends PaginationParams {
  name?: string
  status?: Script['status']
  script_type?: Script['script_type']
}

export interface ScriptContentUpdateRequest {
  content: string
  task_id?: number
}

export interface ScriptExecutedContentResponse {
  content: string
  changed: boolean
  preview_note?: string | null
}

export interface CurlToK6VariableSuggestion {
  key: string
  value: string
  sensitive: boolean
  source?: string | null
}

export interface CurlToK6ParsedRequest {
  method: string
  url: string
  protocol: string
  connect_timeout_ms?: number | null
  response_timeout_ms?: number | null
  suggested_task_name?: string | null
  query_items: Array<{ key: string; value: string }>
  header_items: Array<{ key: string; value: string }>
  body_mode?: string | null
  body_present: boolean
  body_preview?: string | null
  body_items: Array<{ key: string; value: string }>
}

export interface CurlToK6ScriptCreateRequest {
  curl_command: string
  name?: string | null
}

export interface OpenApiToK6SpecParseRequest {
  spec_content: string
}

export interface OpenApiToK6ScriptCreateRequest {
  spec_content: string
  path: string
  method: string
  name?: string | null
  server_url?: string | null
}

export interface HarToK6SpecParseRequest {
  har_content: string
}

export interface HarToK6ScriptCreateRequest {
  har_content: string
  entry_index: number
  name?: string | null
}

export interface CurlToK6ScriptCreateResponse {
  script: Script
  parsed: CurlToK6ParsedRequest
  suggested_variables: CurlToK6VariableSuggestion[]
  warnings: string[]
}

export interface CurlToK6PreviewResponse {
  parsed: CurlToK6ParsedRequest
  suggested_variables: CurlToK6VariableSuggestion[]
  warnings: string[]
  script_content: string
}

export interface OpenApiToK6EndpointItem {
  method: string
  path: string
  summary?: string | null
  operation_id?: string | null
  tags: string[]
  request_content_types: string[]
  request_body_supported: boolean
  server_url?: string | null
}

export interface OpenApiToK6SpecParseResponse {
  title?: string | null
  version?: string | null
  server_urls: string[]
  endpoints: OpenApiToK6EndpointItem[]
  supported_endpoint_count: number
  unsupported_endpoint_count: number
  warnings: string[]
}

export interface OpenApiToK6ParsedRequest {
  title?: string | null
  version?: string | null
  method: string
  path: string
  protocol: string
  server_url?: string | null
  source_url: string
  summary?: string | null
  operation_id?: string | null
  suggested_task_name?: string | null
  request_content_type?: string | null
  body_mode?: string | null
  body_present: boolean
  path_items: Array<{ key: string; value: string }>
  query_items: Array<{ key: string; value: string }>
  header_items: Array<{ key: string; value: string }>
  body_preview?: string | null
  body_items: Array<{ key: string; value: string }>
}

export interface OpenApiToK6ScriptCreateResponse {
  script: Script
  parsed: OpenApiToK6ParsedRequest
  suggested_variables: CurlToK6VariableSuggestion[]
  warnings: string[]
}

export interface OpenApiToK6PreviewResponse {
  parsed: OpenApiToK6ParsedRequest
  suggested_variables: CurlToK6VariableSuggestion[]
  warnings: string[]
  script_content: string
}

export interface HarToK6EntryItem {
  index: number
  method: string
  url: string
  path: string
  status?: number | null
  mime_type?: string | null
  body_present: boolean
  started_at?: string | null
}

export interface HarToK6SpecParseResponse {
  entries: HarToK6EntryItem[]
  supported_entry_count: number
  unsupported_entry_count: number
  warnings: string[]
}

export interface HarToK6ParsedRequest {
  entry_index: number
  method: string
  url: string
  protocol: string
  status?: number | null
  mime_type?: string | null
  suggested_task_name?: string | null
  query_items: Array<{ key: string; value: string }>
  header_items: Array<{ key: string; value: string }>
  body_mode?: string | null
  body_present: boolean
  body_preview?: string | null
  body_items: Array<{ key: string; value: string }>
}

export interface HarToK6ScriptCreateResponse {
  script: Script
  parsed: HarToK6ParsedRequest
  suggested_variables: CurlToK6VariableSuggestion[]
  warnings: string[]
}

export interface HarToK6PreviewResponse {
  parsed: HarToK6ParsedRequest
  suggested_variables: CurlToK6VariableSuggestion[]
  warnings: string[]
  script_content: string
}

export interface AIK6ScriptStaticFinding {
  code: string
  severity: string
  message: string
}

export interface AIK6ScriptRiskItem {
  severity: string
  category: string
  label?: string | null
  message: string
  recommendation?: string | null
  source?: string | null
}

export interface AIK6ScriptReviewRequest {
  script_content: string
  source_type?: string | null
  source_summary?: string | null
}

export interface AIK6ScriptReviewResponse {
  status: string
  prompt_version: string
  provider?: string | null
  model?: string | null
  summary?: string | null
  risk_level: string
  risk_items: AIK6ScriptRiskItem[]
  risks: string[]
  improvements: string[]
  suggested_checks: string[]
  limitations: string[]
  static_findings: AIK6ScriptStaticFinding[]
  usage: Record<string, unknown>
  latency_ms?: number | null
  error_message?: string | null
  disclaimer: string
}

export interface AIK6ScriptDraftRequest {
  source_type: 'curl' | 'openapi' | 'har' | 'interface-description' | string
  source_content: string
  name?: string | null
  path?: string | null
  method?: string | null
  server_url?: string | null
  entry_index?: number
}

export interface AIK6ScriptDraftResponse {
  status: string
  prompt_version: string
  source_type: string
  script_name?: string | null
  script_content: string
  suggested_variables: CurlToK6VariableSuggestion[]
  warnings: string[]
  parsed_source?: Record<string, unknown> | null
  ai_review: AIK6ScriptReviewResponse
  llm_generated: boolean
  human_confirmation_required: boolean
  human_confirmation_status: string
  final_script_status: string
}

export interface AIK6ScriptHumanFeedback {
  rating: 'useful' | 'not_useful' | 'needs_changes'
  note?: string
  action?: 'accepted' | 'needs_revision' | 'ignored'
}

export interface AIK6ScriptConfirmSaveRequest {
  script_name: string
  script_content: string
  source_type: string
  parsed_source?: Record<string, unknown> | null
  ai_review?: AIK6ScriptReviewResponse | null
  llm_generated: boolean
  warnings: string[]
  suggested_variables: CurlToK6VariableSuggestion[]
  human_confirmation_status: 'confirmed'
  final_script_status: 'confirmed_saved'
  human_feedback?: AIK6ScriptHumanFeedback
}

export const scriptApi = {
  // 获取脚本列表
  getScriptList: (params: ScriptQuery): Promise<PaginationResponse<Script>> => {
    return request.get('/scripts', { params })
  },

  // 获取脚本详情
  getScriptDetail: (id: number): Promise<Script> => {
    return request.get(`/scripts/${id}`)
  },

  // 获取脚本内容
  getScriptContent: (id: number): Promise<{ content: string }> => {
    return request.get(`/scripts/${id}/content`)
  },

  getScriptExecutedContent: (id: number): Promise<ScriptExecutedContentResponse> => {
    return request.get(`/scripts/${id}/executed-content`)
  },

  updateScriptContent: (id: number, data: ScriptContentUpdateRequest): Promise<Script> => {
    return request.put(`/scripts/${id}/content`, data)
  },

  // 上传脚本
  uploadScript: (file: File): Promise<Script> => {
    const formData = new FormData()
    formData.append('file', file)
    return request.post('/scripts/upload', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    })
  },

  generateK6FromCurl: (data: CurlToK6ScriptCreateRequest): Promise<CurlToK6ScriptCreateResponse> => {
    return request.post('/scripts/from-curl', data)
  },

  previewK6FromCurl: (data: CurlToK6ScriptCreateRequest): Promise<CurlToK6PreviewResponse> => {
    return request.post('/scripts/preview-curl', data)
  },

  parseOpenApiSpec: (data: OpenApiToK6SpecParseRequest): Promise<OpenApiToK6SpecParseResponse> => {
    return request.post('/scripts/parse-openapi', data)
  },

  previewK6FromOpenApi: (data: OpenApiToK6ScriptCreateRequest): Promise<OpenApiToK6PreviewResponse> => {
    return request.post('/scripts/preview-openapi', data)
  },

  generateK6FromOpenApi: (data: OpenApiToK6ScriptCreateRequest): Promise<OpenApiToK6ScriptCreateResponse> => {
    return request.post('/scripts/from-openapi', data)
  },

  parseHarSpec: (data: HarToK6SpecParseRequest): Promise<HarToK6SpecParseResponse> => {
    return request.post('/scripts/parse-har', data)
  },

  previewK6FromHar: (data: HarToK6ScriptCreateRequest): Promise<HarToK6PreviewResponse> => {
    return request.post('/scripts/preview-har', data)
  },

  generateK6FromHar: (data: HarToK6ScriptCreateRequest): Promise<HarToK6ScriptCreateResponse> => {
    return request.post('/scripts/from-har', data)
  },

  reviewAIK6Script: (data: AIK6ScriptReviewRequest): Promise<AIK6ScriptReviewResponse> => {
    void data
    return Promise.reject(new Error('AI K6 assistant is not included in public alpha'))
  },

  draftAIK6Script: (data: AIK6ScriptDraftRequest): Promise<AIK6ScriptDraftResponse> => {
    void data
    return Promise.reject(new Error('AI K6 assistant is not included in public alpha'))
  },

  confirmSaveAIK6Script: (data: AIK6ScriptConfirmSaveRequest): Promise<Script> => {
    void data
    return Promise.reject(new Error('AI K6 assistant is not included in public alpha'))
  },

  // 删除脚本
  deleteScript: (id: number): Promise<null> => {
    return request.delete(`/scripts/${id}`)
  },

  // 下载脚本
  downloadScript: (id: number): Promise<Blob> => {
    return request.get(`/scripts/${id}/download`, {
      responseType: 'blob',
      skipErrorHandler: true,
    })
  },
}
