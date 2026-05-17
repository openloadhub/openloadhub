import { Alert, Button, Card, Descriptions, Space, Tag, Typography, message } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import dayjs from 'dayjs'

import { planApi } from '@/services/planApi'

const { Text } = Typography

const renderListSummary = (items?: string[] | null) => {
  if (!items || items.length === 0) {
    return '-'
  }
  return items.join(' / ')
}

const PlanDetail = () => {
  const navigate = useNavigate()
  const { planId } = useParams<{ planId: string }>()
  const planIdNum = Number(planId)

  const { data, isLoading, error } = useQuery({
    queryKey: ['plan', planIdNum],
    queryFn: () => planApi.getPlanDetail(planIdNum),
    enabled: Number.isFinite(planIdNum),
  })

  if (error) {
    message.error('加载 Plan 失败')
  }

  const stageTotal = data?.stage_total ?? data?.stages?.length ?? 0
  const taskTotal = data?.task_total ?? data?.stages?.reduce((total, stage) => (
    total + (stage.items ?? []).filter(item => item.type === 'task').length
  ), 0) ?? 0
  const postprocessorTotal = data?.postprocessor_total ?? data?.stages?.reduce((total, stage) => (
    total + (stage.items ?? []).filter(item => item.type === 'postprocessor').length
  ), 0) ?? 0

  return (
    <div className="olh-page-shell olh-console-page olh-plan-detail-page">
      <div className="olh-console-hero olh-plan-detail-hero">
        <div className="olh-console-hero-main">
          <div className="olh-page-breadcrumb">OpenLoadHub / Plan Templates / Detail</div>
          <div className="olh-console-title-row">
            <h1 className="olh-page-title">批次模板详情</h1>
            {data?.status ? <Tag color="processing">{data.status_label || data.status}</Tag> : null}
          </div>
          <div className="olh-page-subtitle">
            {data?.name || '查看模板基础信息、编排快照和场景配置提示。'}
          </div>
        </div>
        <Space className="olh-console-actions">
          <Button onClick={() => navigate('/plans')}>返回模板列表</Button>
          {Number.isFinite(planIdNum) ? (
            <Button type="primary" onClick={() => navigate(`/plans/${planIdNum}/edit`)}>
              编辑模板
            </Button>
          ) : null}
        </Space>
      </div>

      <Card className="olh-dashboard-panel olh-plan-detail-panel" loading={isLoading}>
        <div className="olh-plan-detail-metrics">
          <div className="olh-plan-detail-metric">
            <Text type="secondary">阶段</Text>
            <strong>{stageTotal}</strong>
          </div>
          <div className="olh-plan-detail-metric">
            <Text type="secondary">任务</Text>
            <strong>{taskTotal}</strong>
          </div>
          <div className="olh-plan-detail-metric">
            <Text type="secondary">后置处理</Text>
            <strong>{postprocessorTotal}</strong>
          </div>
          <div className="olh-plan-detail-metric">
            <Text type="secondary">执行方式</Text>
            <strong>{data?.exec_type_label || data?.exec_type || '-'}</strong>
          </div>
        </div>

        <Descriptions className="olh-plan-detail-descriptions" column={2} size="small">
          <Descriptions.Item label="plan_id">{data?.plan_id}</Descriptions.Item>
          <Descriptions.Item label="name">{data?.name}</Descriptions.Item>
          <Descriptions.Item label="status">{data?.status}</Descriptions.Item>
          <Descriptions.Item label="exec_type">{data?.exec_type}</Descriptions.Item>
          <Descriptions.Item label="cron">{data?.cron ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="scheduled_at">
            {data?.scheduled_at ? dayjs(data.scheduled_at).format('YYYY-MM-DD HH:mm:ss') : '-'}
          </Descriptions.Item>
          <Descriptions.Item label="env_list">{renderListSummary(data?.env_list)}</Descriptions.Item>
          <Descriptions.Item label="engine_types">{renderListSummary(data?.engine_types)}</Descriptions.Item>
        </Descriptions>

        {data?.scenario_quality_lint?.warning_count ? (
          <Alert
            type="warning"
            showIcon
            className="olh-plan-detail-alert"
            message={`批次场景配置提示：${data.scenario_quality_lint.warning_count} 项建议补齐`}
            description={(
              <Space direction="vertical" size={4}>
                {data.scenario_quality_lint.issues.slice(0, 8).map((issue, index) => (
                  <Text key={`${issue.code}-${index}`}>
                    {issue.label}：{issue.detail}
                    {issue.recommended_action ? ` 建议：${issue.recommended_action}` : ''}
                  </Text>
                ))}
              </Space>
            )}
          />
        ) : null}

        <div className="olh-plan-detail-stage-shell">
          <div className="olh-plan-detail-stage-header">
            <Text strong>编排快照</Text>
            <Tag>{`stages ${stageTotal}`}</Tag>
          </div>
          <pre>{JSON.stringify({ stages: data?.stages }, null, 2)}</pre>
        </div>
      </Card>
    </div>
  )
}

export default PlanDetail
