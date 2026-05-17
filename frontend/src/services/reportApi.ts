import request from '../utils/request'
import type { PaginationResponse } from '../types'

export interface Report {
  id: number
  name: string
  description?: string | null
  report_type: 'JMETER' | 'K6' | 'COMPARISON'
  task_id: number
  run_id?: number | null
  status: 'PENDING' | 'GENERATING' | 'COMPLETED' | 'FAILED' | 'DELETED'
  file_path?: string | null
  file_size?: number | null
  total_requests?: number | null
  successful_requests?: number | null
  failed_requests?: number | null
  error_rate?: number | null
  avg_response_time?: number | null
  p95_response_time?: number | null
  p99_response_time?: number | null
  throughput?: number | null
  test_config?: Record<string, unknown> | null
  metrics_data?: Record<string, unknown> | null
  quality_gate?: ReportQualityGate | null
  generated_at?: string | null
  created_at: string
  updated_at?: string | null
}

export interface ReportQualityGateSection {
  status: 'ready' | 'partial' | 'missing' | string
  label: string
  detail?: string | null
  evidence: string[]
  gaps: string[]
}

export interface ReportQualityGate {
  status: 'ready' | 'partial' | 'blocked' | string
  status_label: string
  evidence_ready: boolean
  current_template: boolean
  required_sections: Record<string, ReportQualityGateSection>
  evidence: string[]
  gaps: string[]
  limitations: string[]
  recommended_actions: string[]
}

export interface ReportListQuery {
  task_id?: number
  run_id?: number
  status?: Report['status']
  report_type?: Report['report_type']
  page: number
  pageSize: number
}

export interface RunReportFrontdoorResolution {
  status: 'ready' | 'template_fallback' | 'missing' | 'generating'
  message: string
  report_id?: number | null
  report_name?: string | null
  generated?: boolean
  accepted?: boolean
  completed?: boolean
  async_task_id?: string | null
}

export interface RunReportFrontdoorTaskStatus {
  run_id: number
  report_id: number
  async_task_id: string
  job_status: string
  completed: boolean
  result?: RunReportFrontdoorResolution | null
  error?: string | null
}

export const reportApi = {
  // 获取报告列表
  getReportList: (params: ReportListQuery): Promise<PaginationResponse<Report>> => {
    return request.get('/reports', { params })
  },

  // 获取报告详情
  getReportDetail: (id: number): Promise<Report> => {
    return request.get(`/reports/${id}`)
  },

  getRunReportFrontdoor: (runId: number): Promise<RunReportFrontdoorResolution> => {
    return request.get(`/reports/frontdoor/run/${runId}`)
  },

  ensureRunReportFrontdoor: (runId: number): Promise<RunReportFrontdoorResolution> => {
    return request.post(`/reports/frontdoor/run/${runId}/ensure`)
  },

  ensureRunReportFrontdoorAsync: (runId: number): Promise<RunReportFrontdoorResolution> => {
    return request.post(`/reports/frontdoor/run/${runId}/ensure/async`, {}, { skipErrorHandler: true })
  },

  regenerateRunReportFrontdoor: (runId: number): Promise<RunReportFrontdoorResolution> => {
    return request.post(`/reports/frontdoor/run/${runId}/regenerate`)
  },

  regenerateRunReportFrontdoorAsync: (runId: number): Promise<RunReportFrontdoorResolution> => {
    return request.post(`/reports/frontdoor/run/${runId}/regenerate/async`, {}, { skipErrorHandler: true })
  },

  getRunReportFrontdoorTaskStatus: (
    runId: number,
    taskId: string,
    reportId: number,
  ): Promise<RunReportFrontdoorTaskStatus> => {
    return request.get(`/reports/frontdoor/run/${runId}/tasks/${taskId}`, {
      params: { report_id: reportId },
      skipErrorHandler: true,
    })
  },

  // 删除报告
  deleteReport: (id: number): Promise<null> => {
    return request.delete(`/reports/${id}`)
  },

  // 下载报告
  downloadReport: (id: number): Promise<Blob> => {
    return request.get(`/reports/${id}/download`, { responseType: 'blob' })
  },
}
