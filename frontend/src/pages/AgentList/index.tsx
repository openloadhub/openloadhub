import { useEffect, useMemo, useState } from 'react'
import { Button, Card, Modal, Popconfirm, Select, Space, Tag, Typography, message } from 'antd'
import { ReloadOutlined, StopOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { agentApi, type AgentBatchStopRunningRequest, type AgentOpsAlertItem } from '@/services/agentApi'
import { metaApi } from '@/services/metaApi'
import { runApi } from '@/services/runApi'

const { Text } = Typography

const STOPPED_RUN_ID_PREVIEW_LIMIT = 8
const AGENT_GOVERNANCE_POLL_MS = 3_000

const formatStoppedRunIdList = (runIds: number[]) => {
  const previewIds = runIds.slice(0, STOPPED_RUN_ID_PREVIEW_LIMIT)
  const previewText = previewIds.join('、')
  const remainingTotal = runIds.length - previewIds.length
  if (remainingTotal <= 0) {
    return `Run：${previewText}`
  }
  return `Run：${previewText}，另 ${remainingTotal} 个未展开`
}

const AgentList = () => {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [selectedEnvironmentCodes, setSelectedEnvironmentCodes] = useState<string[]>([])

  const { data: agents, error, isFetching: agentsFetching } = useQuery({
    queryKey: ['agents'],
    queryFn: () => agentApi.getAgentList(),
    refetchInterval: AGENT_GOVERNANCE_POLL_MS,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: 'always',
  })
  const { data: opsSummary } = useQuery({
    queryKey: ['agent-ops-summary'],
    queryFn: () => agentApi.getAgentOpsSummary(),
    refetchInterval: AGENT_GOVERNANCE_POLL_MS,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: 'always',
  })
  const { data: environments, error: environmentsError } = useQuery({
    queryKey: ['agent-governance-environments'],
    queryFn: () => metaApi.getEnvironments(),
  })

  useEffect(() => {
    if (error) message.error('获取 Agent 列表失败')
  }, [error])

  useEffect(() => {
    if (environmentsError) {
      message.error('获取环境列表失败')
    }
  }, [environmentsError])

  const healthyCount = agents?.filter(a => a.healthy).length ?? 0
  const totalCount = agents?.length ?? 0
  const busyCount = agents?.reduce((total, agent) => {
    const currentRunTotal = Number(agent.current_run_total)
    if (Number.isFinite(currentRunTotal) && currentRunTotal > 0) {
      return total + currentRunTotal
    }
    return total + (agent.availability_state === 'busy' ? 1 : 0)
  }, 0) ?? 0
  const idleCount = Math.max(totalCount - busyCount, 0)
  const environmentOptions = useMemo(
    () => (environments || []).map(item => ({ label: item.code, value: item.code })),
    [environments],
  )

  const refreshGovernanceView = () => {
    void queryClient.invalidateQueries({ queryKey: ['agents'] })
    void queryClient.invalidateQueries({ queryKey: ['agent-ops-summary'] })
    void queryClient.invalidateQueries({ queryKey: ['agent-governance-environments'] })
    void queryClient.invalidateQueries({ queryKey: ['runs'] })
  }

  const stopRunMutation = useMutation({
    mutationFn: (runId: number) => runApi.stopRun(runId, { reason: 'runtime_governance_v1_manual_stop' }),
    onSuccess: (_run, runId) => {
      message.success(`已提交 Run #${runId} 停止请求`)
      refreshGovernanceView()
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '停止 Run 失败')
    },
  })

  const batchStopRunningMutation = useMutation({
    mutationFn: (payload: AgentBatchStopRunningRequest) => agentApi.batchStopRunningAgents(payload),
    onSuccess: (result, variables) => {
      const remoteStopSummary = result.remote_stop_summary ?? result.remote_stop
      const matchedRunTotal = remoteStopSummary?.matched_run_total ?? result.matched_run_total
      const stoppedRunIds = remoteStopSummary?.stopped_run_ids ?? result.stopped_run_ids

      if (stoppedRunIds.length > 0) {
        const runSummary = `匹配 ${matchedRunTotal} 个运行中压测任务，已提交停止 ${formatStoppedRunIdList(stoppedRunIds)}`
        const scopeLabel = variables.envs?.length
          ? `所选环境${runSummary}`
          : runSummary
        message.success(scopeLabel, 5)
      } else {
        const scopeLabel = variables.envs?.length
          ? '所选环境当前没有运行中的压测任务占用执行节点'
          : '当前没有运行中的压测任务占用执行节点'
        message.warning(scopeLabel)
      }
      refreshGovernanceView()
      if (variables.envs?.length) {
        setSelectedEnvironmentCodes([])
      }
    },
    onError: (mutationError: unknown, variables) => {
      const fallbackMessage = variables.envs?.length
        ? '按环境停止运行中压测任务失败'
        : '停止全部运行中压测任务失败'
      message.error(mutationError instanceof Error ? mutationError.message : fallbackMessage)
    },
  })

  const renderGovernanceActions = (item: AgentOpsAlertItem) => {
    if (item.key === 'run_stuck') {
      return (
        <Space wrap size={[8, 8]}>
          {item.sample_run_ids.map(runId => (
            <Popconfirm
              key={`stop-run-${runId}`}
              title={`确认停止 Run #${runId} 吗？`}
              description="runtime governance v1 仅提交手工停止，不会自动做其他恢复动作"
              okText="停止 Run"
              cancelText="取消"
              onConfirm={() => stopRunMutation.mutate(runId)}
            >
              <Button
                size="small"
                danger
                type="primary"
                data-testid={`runtime-stop-run-${runId}`}
                loading={stopRunMutation.isPending && stopRunMutation.variables === runId}
              >
                {`停止 Run #${runId}`}
              </Button>
            </Popconfirm>
          ))}
          {item.sample_run_ids.map(runId => (
            <Button key={`open-run-${runId}`} size="small" onClick={() => navigate(`/runs/${runId}`)}>
              {`打开 Run #${runId}`}
            </Button>
          ))}
        </Space>
      )
    }

    if (item.key === 'agent_unhealthy') {
      return (
        <Space wrap size={[8, 8]}>
          {item.sample_hosts.map(host => (
            <Tag key={`host-${host}`} color="warning">{host}</Tag>
          ))}
        </Space>
      )
    }

    if (item.key === 'report_generation_failed') {
      return (
        <Space wrap size={[8, 8]}>
          {item.sample_report_ids.map(reportId => (
            <Button key={`report-${reportId}`} size="small" onClick={() => navigate(`/reports/${reportId}`)}>
              {`打开报告 #${reportId}`}
            </Button>
          ))}
        </Space>
      )
    }

    return (
      <Space wrap size={[8, 8]}>
        {item.sample_run_ids.map(runId => (
          <Button key={`fallback-run-${runId}`} size="small" onClick={() => navigate(`/runs/${runId}`)}>
            {`打开 Run #${runId}`}
          </Button>
        ))}
        {item.sample_report_ids.map(reportId => (
          <Button key={`fallback-report-${reportId}`} size="small" onClick={() => navigate(`/reports/${reportId}`)}>
            {`打开报告 #${reportId}`}
          </Button>
        ))}
        {item.sample_hosts.map(host => (
          <Tag key={`fallback-host-${host}`} color="warning">{host}</Tag>
        ))}
      </Space>
    )
  }

  const openBatchStopConfirm = (payload: AgentBatchStopRunningRequest) => {
    const hasEnvironmentScope = Boolean(payload.envs?.length)
    const environmentSummary = payload.envs?.join('、')

    Modal.confirm({
      title: hasEnvironmentScope ? '确认停止所选环境的运行中压测任务？' : '确认停止全部运行中压测任务？',
      content: hasEnvironmentScope
        ? `将尝试停止这些环境里占用执行节点的运行中压测任务：${environmentSummary}。该动作只释放执行节点占用，不会删除执行节点。`
        : '将尝试停止当前所有环境里占用执行节点的运行中压测任务。该动作只释放执行节点占用，不会删除执行节点。',
      okText: '确认停止',
      cancelText: '取消',
      okButtonProps: { danger: true },
      centered: true,
      onOk: async () => {
        await batchStopRunningMutation.mutateAsync(payload)
      },
    })
  }

  return (
    <div data-testid="agent-list" className="olh-page-shell olh-console-page olh-agent-list-page">
      <div className="olh-console-hero olh-agent-hero">
        <div className="olh-console-hero-main">
          <div className="olh-page-breadcrumb">OpenLoadHub / Runtime Agents</div>
          <div className="olh-console-title-row">
            <h1 className="olh-page-title" aria-label="Agent 管理">Agent 管理</h1>
            {agents ? (
              <span className={healthyCount === totalCount && totalCount > 0 ? 'olh-live-pill olh-live-pill--active' : 'olh-live-pill'}>
                {healthyCount}/{totalCount} 在线
              </span>
            ) : null}
          </div>
          <div className="olh-page-subtitle">
            观察执行节点健康、运行占用和值守红灯，手工治理动作集中在下方操作区。
          </div>
          <div className="olh-console-command-strip">
            <span>{`节点 ${totalCount}`}</span>
            <span>{`健康 ${healthyCount}`}</span>
            <span>{`忙碌 ${busyCount}`}</span>
            <span>{`空闲 ${idleCount}`}</span>
          </div>
        </div>
        <div className="olh-console-hero-side">
          <div className="olh-console-focus-panel">
            <div className="olh-console-focus-label">Runtime</div>
            <div className="olh-console-focus-value">{opsSummary?.has_alerts ? opsSummary.critical_total + opsSummary.warning_total : 0}</div>
            <div className="olh-console-focus-copy">
              {opsSummary?.has_alerts ? '当前存在需要处理的值守红灯' : '当前没有需要处理的值守红灯'}
            </div>
            <div className="olh-console-focus-meta">
              <span>{`critical ${opsSummary?.critical_total ?? 0}`}</span>
              <span>{`warning ${opsSummary?.warning_total ?? 0}`}</span>
            </div>
          </div>
          <Space wrap className="olh-console-actions">
            <Button
              icon={<ReloadOutlined />}
              onClick={refreshGovernanceView}
              loading={agentsFetching}
            >
              刷新
            </Button>
          </Space>
        </div>
      </div>

      <div className="olh-kpi-grid olh-agent-kpis">
        {[
          { key: 'total', label: '总节点数', value: totalCount, helper: '已发现执行节点', testId: 'agent-total-count' },
          { key: 'healthy', label: '健康', value: healthyCount, helper: `${totalCount > 0 ? Math.round((healthyCount / totalCount) * 100) : 0}% 在线`, testId: 'agent-healthy-count' },
          { key: 'busy', label: '忙碌', value: busyCount, helper: '当前承载运行任务', testId: 'agent-busy-count' },
          { key: 'idle', label: '空闲', value: idleCount, helper: '可继续调度容量', testId: 'agent-idle-count' },
        ].map(item => (
          <div key={item.key} className={`olh-kpi-card olh-kpi-card--${item.key}`}>
            <div className="olh-kpi-head">
              <div className="olh-kpi-label">{item.label}</div>
            </div>
            <div className="olh-kpi-value" data-testid={item.testId}>
              <span>{item.value}</span>
            </div>
            <div className="olh-kpi-helper">{item.helper}</div>
          </div>
        ))}
      </div>

      <Card
        className="olh-agent-governance-card"
        size="small"
        title="Runtime Governance v1"
        style={{ marginBottom: 24 }}
        data-testid="runtime-governance-card"
      >
        <Space direction="vertical" size={12} style={{ display: 'flex' }}>
          <div className="olh-agent-governance-note olh-agent-governance-note--info">
            <div>
              <Text strong>运行时治理入口 v1</Text>
              <Text type="secondary">
                只把现有值守红灯转换成手工治理动作：run_stuck 可 stop run，agent_unhealthy 保留异常 host，report_generation_failed 保留打开报告。
              </Text>
            </div>
            <Tag className="olh-agent-governance-tag">manual ops</Tag>
          </div>
          <div className={`olh-agent-governance-note ${opsSummary?.has_alerts ? 'olh-agent-governance-note--warning' : 'olh-agent-governance-note--ok'}`}>
            <div>
              <Text strong>{opsSummary?.has_alerts ? '当前存在需要处理的运行时红灯' : '当前没有需要处理的值守红灯'}</Text>
              <Text type="secondary">
                {opsSummary?.has_alerts
                  ? `critical ${opsSummary.critical_total} · warning ${opsSummary.warning_total}`
                  : 'run_stuck / agent_unhealthy / report_generation_failed 当前都未命中'}
              </Text>
            </div>
            <Tag color={opsSummary?.has_alerts ? 'warning' : 'success'}>
              {opsSummary?.has_alerts ? 'review' : 'clear'}
            </Tag>
          </div>
          <div className="olh-agent-alert-grid">
            {(opsSummary?.items || []).map(item => (
              <div key={item.key} className={`olh-agent-alert-card olh-agent-alert-card--${item.severity}`}>
                <Space direction="vertical" size={6} style={{ display: 'flex' }}>
                  <Space wrap>
                    <Text strong>{item.title}</Text>
                    <Tag color={item.severity === 'critical' ? 'error' : item.severity === 'warning' ? 'warning' : 'success'}>
                      {item.severity === 'critical' ? 'critical' : item.severity === 'warning' ? 'warning' : 'ok'}
                    </Tag>
                  </Space>
                  <div className="olh-agent-alert-count">{item.count}</div>
                  <Text type="secondary">{item.summary}</Text>
                  {item.detail ? <Text type="secondary">{item.detail}</Text> : null}
                  {renderGovernanceActions(item)}
                </Space>
              </div>
            ))}
          </div>
        </Space>
      </Card>

      <Card
        className="olh-filter-panel olh-console-filter-panel olh-agent-filter-panel"
        size="small"
        title="运行中压测任务治理"
        data-testid="agent-batch-stop-card"
      >
        <Space direction="vertical" size={12} style={{ display: 'flex' }}>
          <div className="olh-agent-stop-note">
            <div>
              <Text strong>停止动作需要人工确认</Text>
              <Text type="secondary">
                仅提交停止运行中压测任务的治理请求，用于释放执行节点占用，不会删除执行节点。
              </Text>
            </div>
          </div>
          <div className="olh-agent-governance-actions">
            <div>
              <Select
                mode="multiple"
                allowClear
                placeholder="选择需要停止的环境"
                value={selectedEnvironmentCodes}
                options={environmentOptions}
                onChange={setSelectedEnvironmentCodes}
                style={{ width: '100%' }}
                maxTagCount="responsive"
                data-testid="agent-stop-environment-select"
              />
            </div>
            <div>
              <Space wrap className="olh-agent-stop-actions">
                <Button
                  className="olh-agent-stop-button olh-agent-stop-button--danger"
                  danger
                  icon={<StopOutlined />}
                  onClick={() => openBatchStopConfirm({})}
                  loading={batchStopRunningMutation.isPending}
                  data-testid="agent-stop-all-button"
                >
                  停止全部
                </Button>
                <Button
                  className="olh-agent-stop-button"
                  icon={<StopOutlined />}
                  onClick={() => openBatchStopConfirm({ envs: selectedEnvironmentCodes })}
                  disabled={selectedEnvironmentCodes.length === 0}
                  loading={batchStopRunningMutation.isPending}
                  data-testid="agent-stop-selected-env-button"
                >
                  停止所选环境
                </Button>
              </Space>
            </div>
          </div>
        </Space>
      </Card>

    </div>
  )
}

export default AgentList
