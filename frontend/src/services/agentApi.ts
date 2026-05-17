import request from '../utils/request'

export interface AgentInfo {
  host: string
  ip: string | null
  port: number | null
  healthy: boolean
  version: string | null
  service: string | null
  latency_ms: number | null
  availability_state: 'idle' | 'busy' | 'draining' | 'disabled' | 'unhealthy'
  current_run_total: number
  max_concurrency: number | null
  current_load_ratio: number | null
  runtime_kind: string | null
  last_error: string | null
}

export interface AgentOpsAlertItem {
  key: 'run_stuck' | 'agent_unhealthy' | 'report_generation_failed' | string
  title: string
  severity: 'ok' | 'warning' | 'critical' | string
  count: number
  summary: string
  detail?: string | null
  sample_run_ids: number[]
  sample_report_ids: number[]
  sample_hosts: string[]
}

export interface AgentOpsSummary {
  has_alerts: boolean
  critical_total: number
  warning_total: number
  items: AgentOpsAlertItem[]
}

export interface AgentBatchStopRunningRequest {
  envs?: string[]
}

export interface AgentRemoteStopSummary {
  matched_run_total?: number
  stopped_run_ids?: number[]
  attempted?: number
  succeeded?: number
  failed?: number
  timed_out?: number
}

export interface AgentBatchStopRunningResponse {
  target_envs: string[]
  matched_run_total: number
  scope: 'all' | 'envs' | string
  stopped_run_ids: number[]
  remote_stop?: AgentRemoteStopSummary | null
  remote_stop_summary?: AgentRemoteStopSummary | null
}

export const agentApi = {
  /** 获取已发现的 agent 列表及健康状态 */
  getAgentList: (): Promise<AgentInfo[]> => {
    return request.get('/agents')
  },
  getAgentOpsSummary: (): Promise<AgentOpsSummary> => {
    return request.get('/agents/ops-summary')
  },
  batchStopRunningAgents: (data: AgentBatchStopRunningRequest = {}): Promise<AgentBatchStopRunningResponse> => {
    return request.post('/agents/stop-active-runs', data)
  },
}
