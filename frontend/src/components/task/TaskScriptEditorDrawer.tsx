import { useEffect, useState } from 'react'
import { Alert, Button, Card, Drawer, Space, Spin, Tag, Tooltip, Typography, message } from 'antd'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Editor from '@monaco-editor/react'
import dayjs from 'dayjs'

import { publicAlphaFeatures } from '@/config/publicAlpha'
import { scriptApi, type AIK6ScriptReviewResponse, type AIK6ScriptRiskItem } from '@/services/scriptApi'
import { taskApi, type TaskScriptVersionContent } from '@/services/taskApi'

const { Paragraph, Text } = Typography

const ensureScriptFilename = (name: string | undefined, scriptType: 'JMETER' | 'K6' | undefined, scriptId?: number) => {
  const fallbackBase = `task-script-${scriptId ?? Date.now()}`
  const trimmed = name?.trim() || fallbackBase
  const lower = trimmed.toLowerCase()
  if (scriptType === 'JMETER') {
    return lower.endsWith('.jmx') ? trimmed : `${trimmed}.jmx`
  }
  return lower.endsWith('.js') ? trimmed : `${trimmed}.js`
}

const resolveScriptMimeType = (scriptType: 'JMETER' | 'K6' | undefined) => {
  if (scriptType === 'JMETER') {
    return 'text/xml'
  }
  return 'application/javascript'
}

const getRiskColor = (riskLevel?: string | null) => {
  const normalized = riskLevel?.toLowerCase()
  if (normalized === 'high' || normalized === 'critical') {
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

const formatUsageValue = (value: unknown): string => {
  if (value === null || value === undefined) {
    return '-'
  }
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  return JSON.stringify(value)
}

const renderReviewList = (title: string, items?: string[]) => {
  const normalized = (items || []).filter(Boolean)
  if (normalized.length === 0) {
    return null
  }
  return (
    <div>
      <Text strong>{title}</Text>
      <Space size={[4, 4]} wrap style={{ marginLeft: 8 }}>
        {normalized.map((item, index) => (
          <Tag key={`${title}-${index}`}>{item}</Tag>
        ))}
      </Space>
    </div>
  )
}

const renderRiskItems = (items?: AIK6ScriptRiskItem[]) => {
  const normalized = (items || []).filter(item => item.message)
  if (normalized.length === 0) {
    return null
  }
  return (
    <div>
      <Text strong>结构化风险</Text>
      <Space direction="vertical" size={6} style={{ width: '100%', marginTop: 6 }}>
        {normalized.map((item, index) => (
          <Space key={`${item.severity}-${item.category}-${index}`} size={[6, 4]} wrap>
            <Tag color={getRiskColor(item.severity)}>{item.severity || 'unknown'}</Tag>
            <Tag>{item.label || item.category || 'general'}</Tag>
            {item.source ? <Tag color="blue">{item.source}</Tag> : null}
            <Text>{item.message}</Text>
            {item.recommendation ? <Text type="secondary">{item.recommendation}</Text> : null}
          </Space>
        ))}
      </Space>
    </div>
  )
}

const renderReviewResult = (result: AIK6ScriptReviewResponse) => {
  const usageEntries = Object.entries(result.usage || {})
  return (
    <Card
      size="small"
      title="AI 评审结果"
      style={{ marginBottom: 16 }}
      bodyStyle={{ padding: 12 }}
    >
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        <Space size={[8, 4]} wrap>
          <Tag color={result.status === 'success' ? 'green' : 'orange'}>{result.status}</Tag>
          <Tag color={getRiskColor(result.risk_level)}>risk: {result.risk_level || '-'}</Tag>
          {result.provider ? <Tag>{result.provider}</Tag> : null}
          {result.model ? <Tag>{result.model}</Tag> : null}
          {result.latency_ms !== null && result.latency_ms !== undefined ? <Tag>{result.latency_ms}ms</Tag> : null}
          {usageEntries.map(([key, value]) => (
            <Tag key={key}>{key}: {formatUsageValue(value)}</Tag>
          ))}
        </Space>
        {result.summary ? (
          <Paragraph style={{ marginBottom: 0 }}>{result.summary}</Paragraph>
        ) : null}
        {renderRiskItems(result.risk_items)}
        {renderReviewList('风险', result.risks)}
        {renderReviewList('改进', result.improvements)}
        {renderReviewList('建议检查', result.suggested_checks)}
        {renderReviewList('限制', result.limitations)}
        {result.static_findings?.length ? (
          <div>
            <Text strong>静态发现</Text>
            <Space size={[4, 4]} wrap style={{ marginLeft: 8 }}>
              {result.static_findings.map((finding, index) => (
                <Tag key={`${finding.code}-${index}`} color={getRiskColor(finding.severity)}>
                  {finding.severity}: {finding.code} - {finding.message}
                </Tag>
              ))}
            </Space>
          </div>
        ) : null}
        {result.error_message ? (
          <Alert type="warning" showIcon message={result.error_message} />
        ) : null}
        {result.disclaimer ? (
          <Text type="secondary">{result.disclaimer}</Text>
        ) : null}
      </Space>
    </Card>
  )
}

interface TaskScriptEditorDrawerProps {
  open: boolean
  scriptId?: number
  taskId?: number
  onScriptSelected?: (scriptId: number) => void
  previewVersion?: TaskScriptVersionContent | null
  currentScriptVersion?: string | null
  onViewCurrent?: () => void
  onClose: () => void
}

const TaskScriptEditorDrawer: React.FC<TaskScriptEditorDrawerProps> = ({
  open,
  scriptId,
  taskId,
  onScriptSelected,
  previewVersion,
  currentScriptVersion,
  onViewCurrent,
  onClose,
}) => {
  const queryClient = useQueryClient()
  const [content, setContent] = useState('')
  const [showRuntimePreview, setShowRuntimePreview] = useState(false)
  const [aiReviewResult, setAIReviewResult] = useState<AIK6ScriptReviewResponse | null>(null)
  const isHistoryPreview = !!previewVersion

  const { data: scriptDetail, isLoading: detailLoading, error: detailError } = useQuery({
    queryKey: ['script', scriptId],
    queryFn: () => scriptApi.getScriptDetail(scriptId!),
    enabled: open && !!scriptId && !isHistoryPreview,
  })

  const { data: contentData, isLoading: contentLoading, error: contentError } = useQuery({
    queryKey: ['script-content', scriptId],
    queryFn: () => scriptApi.getScriptContent(scriptId!),
    enabled: open && !!scriptId && !isHistoryPreview,
  })

  const isJmeterScript = scriptDetail?.script_type === 'JMETER'
  const {
    data: runtimePreviewData,
    isLoading: runtimePreviewLoading,
    error: runtimePreviewError,
  } = useQuery({
    queryKey: ['script-executed-content', scriptId],
    queryFn: () => scriptApi.getScriptExecutedContent(scriptId!),
    enabled: open && !!scriptId && !isHistoryPreview && isJmeterScript,
  })

  useEffect(() => {
    if (previewVersion) {
      setContent(previewVersion.content)
      setAIReviewResult(null)
      return
    }
    if (contentData?.content !== undefined) {
      setContent(contentData.content)
      setAIReviewResult(null)
    }
  }, [contentData, previewVersion])

  useEffect(() => {
    if (!open) {
      setShowRuntimePreview(false)
    }
  }, [open, scriptId])

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (!scriptId) {
        throw new Error('脚本不存在')
      }
      const fileName = ensureScriptFilename(scriptDetail?.name, scriptDetail?.script_type, scriptId)
      const uploadedScript = await scriptApi.uploadScript(
        new File(
          [content],
          fileName,
          { type: resolveScriptMimeType(scriptDetail?.script_type) },
        ),
      )
      if (taskId) {
        await taskApi.updateTask(taskId, { script_id: uploadedScript.id })
      } else {
        onScriptSelected?.(uploadedScript.id)
      }
      return uploadedScript
    },
    onSuccess: (updatedScript) => {
      message.success(`脚本已保存，当前版本 ${updatedScript.version}`)
      queryClient.invalidateQueries({ queryKey: ['script', scriptId] })
      queryClient.invalidateQueries({ queryKey: ['script-content', scriptId] })
      queryClient.invalidateQueries({ queryKey: ['script-executed-content', scriptId] })
      queryClient.invalidateQueries({ queryKey: ['task-versions', taskId] })
      queryClient.invalidateQueries({ queryKey: ['task-versions-badge', taskId] })
      queryClient.invalidateQueries({ queryKey: ['task', taskId] })
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['script', updatedScript.id] })
      queryClient.invalidateQueries({ queryKey: ['script-content', updatedScript.id] })
      queryClient.invalidateQueries({ queryKey: ['script-executed-content', updatedScript.id] })
      onClose()
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '脚本保存失败')
    },
  })

  const aiReviewMutation = useMutation({
    mutationFn: () => scriptApi.reviewAIK6Script({
      script_content: content,
      source_type: 'script-editor',
      source_summary: [
        scriptDetail?.name ? `script=${scriptDetail.name}` : null,
        scriptDetail?.version ? `version=${scriptDetail.version}` : null,
        scriptId ? `script_id=${scriptId}` : null,
        taskId ? `task_id=${taskId}` : null,
        scriptDetail?.description ? `description=${scriptDetail.description}` : null,
      ].filter(Boolean).join('; '),
    }),
    onSuccess: (result) => {
      setAIReviewResult(result)
      message.success('AI 评审已完成')
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : 'AI 评审失败')
    },
  })

  useEffect(() => {
    if (!open) {
      setAIReviewResult(null)
      aiReviewMutation.reset()
    }
  }, [open, scriptId])

  const copyPreviewContent = async () => {
    try {
      await navigator.clipboard.writeText(editorValue)
      message.success('脚本内容已复制')
    } catch (error) {
      message.error(error instanceof Error ? error.message : '脚本复制失败')
    }
  }

  const loading = isHistoryPreview ? false : detailLoading || contentLoading || (showRuntimePreview && runtimePreviewLoading)
  const error = isHistoryPreview ? null : (showRuntimePreview ? runtimePreviewError : null) || detailError || contentError
  const language = previewVersion?.file_name?.toLowerCase().endsWith('.jmx') || scriptDetail?.script_type === 'JMETER'
    ? 'xml'
    : 'javascript'
  const title = isHistoryPreview
    ? `脚本版本预览 - ${previewVersion?.file_name || '历史脚本'}`
    : scriptDetail
      ? `脚本编辑 - ${scriptDetail.name}`
      : '脚本编辑'
  const runtimePreviewEnabled = !isHistoryPreview && isJmeterScript
  const editorValue = showRuntimePreview ? runtimePreviewData?.content ?? content : content
  const isK6Script = scriptDetail?.script_type === 'K6'
  const aiReviewDisabledReason = (() => {
    if (!publicAlphaFeatures.aiFeatures) {
      return 'AI 评审未在 public alpha 开放'
    }
    if (loading) {
      return '脚本加载中'
    }
    if (isHistoryPreview || showRuntimePreview) {
      return '只读预览不支持 AI 评审'
    }
    if (!scriptId) {
      return '脚本不存在'
    }
    if (!isK6Script) {
      return 'AI 评审仅支持 K6 脚本'
    }
    if (!content.trim()) {
      return '脚本内容为空'
    }
    return null
  })()

  return (
    <Drawer
      title={title}
      placement="right"
      width={960}
      open={open}
      onClose={onClose}
      destroyOnClose
      extra={(
        <Space>
          {isHistoryPreview ? (
            <>
              <Text type="secondary">历史版本 {previewVersion?.version}</Text>
              {previewVersion?.created_by_name ? <Text type="secondary">修改人 {previewVersion.created_by_name}</Text> : null}
              {previewVersion?.updated_at ? (
                <Text type="secondary">{dayjs(previewVersion.updated_at).format('YYYY-MM-DD HH:mm:ss')}</Text>
              ) : null}
              <Button onClick={copyPreviewContent}>复制脚本</Button>
              {currentScriptVersion ? <Text type="secondary">当前版本 {currentScriptVersion}</Text> : null}
              {onViewCurrent ? <Button onClick={onViewCurrent}>查看当前脚本</Button> : null}
              <Button onClick={onClose}>关闭</Button>
            </>
          ) : (
            <>
              {scriptDetail?.version ? <Text type="secondary">版本 {scriptDetail.version}</Text> : null}
              {runtimePreviewEnabled ? (
                <Button onClick={() => setShowRuntimePreview(value => !value)}>
                  {showRuntimePreview ? '返回原始脚本' : '查看执行预览'}
                </Button>
              ) : null}
              {publicAlphaFeatures.aiFeatures ? (
                <Tooltip title={aiReviewDisabledReason || '使用当前编辑器内容发起 AI 评审，不会保存或执行脚本'}>
                  <Button
                    loading={aiReviewMutation.isPending}
                    disabled={!!aiReviewDisabledReason}
                    onClick={() => aiReviewMutation.mutate()}
                  >
                    AI 评审
                  </Button>
                </Tooltip>
              ) : null}
              <Button onClick={onClose}>取消</Button>
              <Button type="primary" loading={saveMutation.isPending} disabled={showRuntimePreview} onClick={() => saveMutation.mutate()}>
                保存
              </Button>
            </>
          )}
        </Space>
      )}
    >
      {isHistoryPreview ? (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="当前为历史版本只读预览"
          description="历史版本不会直接覆盖当前任务脚本。需要继续修改时，请切回当前脚本后再编辑。"
        />
      ) : null}

      {showRuntimePreview && runtimePreviewData ? (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={runtimePreviewData.changed ? '当前展示执行预览（含运行期补齐内容）' : '当前脚本执行前无需额外补齐'}
          description={
            runtimePreviewData.preview_note
              ?? (runtimePreviewData.changed
                ? '这里展示的是运行前预处理后的脚本内容，包含平台会补齐的 InfluxDB BackendListener；该预览只读，不会直接覆盖原脚本。'
                : '当前脚本已经具备运行期所需结构，预览与原始脚本一致。')
          }
        />
      ) : null}

      {error ? (
        <Alert
          type="error"
          showIcon
          message="脚本内容加载失败"
          description={error instanceof Error ? error.message : '请稍后重试'}
        />
      ) : null}

      {aiReviewMutation.error ? (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="AI 评审失败"
          description={aiReviewMutation.error instanceof Error ? aiReviewMutation.error.message : '请稍后重试'}
        />
      ) : null}

      {aiReviewResult ? renderReviewResult(aiReviewResult) : null}

      {loading ? (
        <div style={{ padding: '80px 0', textAlign: 'center' }}>
          <Spin />
        </div>
      ) : (
        <Editor
          height="calc(100vh - 180px)"
          language={language}
          value={editorValue}
          theme="vs-dark"
          onChange={(value) => {
            setContent(value ?? '')
            setAIReviewResult(null)
            aiReviewMutation.reset()
          }}
          options={{
            minimap: { enabled: false },
            readOnly: isHistoryPreview || showRuntimePreview,
            scrollBeyondLastLine: false,
            wordWrap: 'on',
          }}
        />
      )}
    </Drawer>
  )
}

export default TaskScriptEditorDrawer
