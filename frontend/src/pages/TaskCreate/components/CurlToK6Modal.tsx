import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Descriptions,
  Divider,
  Empty,
  Input,
  Modal,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  scriptApi,
  type CurlToK6PreviewResponse,
  type CurlToK6ScriptCreateResponse,
} from '@/services/scriptApi'

const { TextArea } = Input
const { Text } = Typography

const SIMPLE_COLUMNS = [
  {
    title: '字段',
    dataIndex: 'key',
    key: 'key',
    width: 180,
  },
  {
    title: '值',
    dataIndex: 'value',
    key: 'value',
  },
]

interface CurlToK6ModalProps {
  open: boolean
  initialName?: string
  onCancel: () => void
  onSuccess: (result: CurlToK6ScriptCreateResponse) => void
}

const CurlToK6Modal: React.FC<CurlToK6ModalProps> = ({
  open,
  initialName,
  onCancel,
  onSuccess,
}) => {
  const [curlCommand, setCurlCommand] = useState('')
  const [scriptName, setScriptName] = useState('')
  const [preview, setPreview] = useState<CurlToK6PreviewResponse | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (!open) {
      return
    }
    setCurlCommand('')
    setScriptName(initialName || '')
    setPreview(null)
  }, [initialName, open])

  const handlePreview = async () => {
    if (!curlCommand.trim()) {
      message.warning('请输入 CURL 命令')
      return
    }

    setPreviewLoading(true)
    try {
      const result = await scriptApi.previewK6FromCurl({
        curl_command: curlCommand.trim(),
        name: scriptName.trim() || undefined,
      })
      setPreview(result)
      message.success('CURL 解析完成')
    } catch {
      setPreview(null)
    } finally {
      setPreviewLoading(false)
    }
  }

  const handleConfirm = async () => {
    if (!curlCommand.trim()) {
      message.warning('请输入 CURL 命令')
      return
    }

    setSubmitting(true)
    try {
      const result = await scriptApi.generateK6FromCurl({
        curl_command: curlCommand.trim(),
        name: scriptName.trim() || undefined,
      })
      onSuccess(result)
    } catch {
      // request.ts already surfaces the user-facing error message
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      title="从 CURL 生成 K6 脚本"
      open={open}
      onCancel={onCancel}
      destroyOnClose
      width={760}
      footer={[
        <Button key="cancel" onClick={onCancel}>取消</Button>,
        <Button key="preview" onClick={handlePreview} loading={previewLoading}>
          解析预览
        </Button>,
        <Button key="ok" type="primary" onClick={handleConfirm} loading={submitting}>
          生成脚本
        </Button>,
      ]}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="当前只支持单接口 HTTP CURL"
        description="生成结果会直接落成标准单接口 K6 模板，并自动回填到当前任务。执行并发、时长和次数由运行策略统一控制；启动前可设置总 TPS，运行中调 TPS 留到后续版本。"
      />

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
        style={{ marginBottom: 16 }}
      />

      <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
        CURL 命令
      </Text>
      <TextArea
        rows={10}
        placeholder={"例如：curl 'https://api.example.com/orders' -H 'Content-Type: application/json' --data '{\"id\":1}'"}
        value={curlCommand}
        onChange={event => {
          setCurlCommand(event.target.value)
          setPreview(null)
        }}
      />

      {preview ? (
        <>
          <Divider />
          <Descriptions
            size="small"
            column={1}
            items={[
              { key: 'method', label: '请求方法', children: preview.parsed.method },
              { key: 'url', label: '目标 URL', children: preview.parsed.url },
              { key: 'task_name', label: '建议任务名', children: preview.parsed.suggested_task_name || '-' },
              {
                key: 'timeout',
                label: '响应超时',
                children: preview.parsed.response_timeout_ms ? `${preview.parsed.response_timeout_ms} ms` : '-',
              },
              {
                key: 'body_mode',
                label: 'Body 类型',
                children: preview.parsed.body_present ? (preview.parsed.body_mode || 'raw') : '-',
              },
            ]}
          />

          <div style={{ marginTop: 12 }}>
            <Text type="secondary">建议变量</Text>
            <div style={{ marginTop: 8 }}>
              <Space wrap size={[8, 8]}>
                {preview.suggested_variables.length > 0 ? (
                  preview.suggested_variables.map(item => (
                    <Space key={item.key} size={[6, 6]} wrap>
                      <Tag color={item.sensitive ? 'orange' : 'blue'}>
                        {item.key}
                      </Tag>
                      {item.source ? <Text type="secondary">{item.source}</Text> : null}
                    </Space>
                  ))
                ) : (
                  <Text type="secondary">无</Text>
                )}
              </Space>
            </div>
          </div>

          <div style={{ marginTop: 16 }}>
            <Text type="secondary">Query 参数</Text>
            <div style={{ marginTop: 8 }}>
              <Table
                size="small"
                rowKey={record => `${record.key}-${record.value}`}
                pagination={false}
                columns={SIMPLE_COLUMNS}
                dataSource={preview.parsed.query_items}
                locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无" /> }}
              />
            </div>
          </div>

          <div style={{ marginTop: 16 }}>
            <Text type="secondary">请求 Headers</Text>
            <div style={{ marginTop: 8 }}>
              <Table
                size="small"
                rowKey={record => `${record.key}-${record.value}`}
                pagination={false}
                columns={SIMPLE_COLUMNS}
                dataSource={preview.parsed.header_items}
                locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无" /> }}
              />
            </div>
          </div>

          <div style={{ marginTop: 16 }}>
            <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
              Body 字段
            </Text>
            <Table
              size="small"
              rowKey={record => `${record.key}-${record.value}`}
              pagination={false}
              columns={SIMPLE_COLUMNS}
              dataSource={preview.parsed.body_items}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无结构化字段" /> }}
            />
          </div>

          <div style={{ marginTop: 16 }}>
            <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
              Body 原始预览
            </Text>
            {preview.parsed.body_present && preview.parsed.body_preview ? (
              <TextArea rows={6} value={preview.parsed.body_preview} readOnly />
            ) : (
              <Text type="secondary">无</Text>
            )}
          </div>

          {preview.warnings.length > 0 ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginTop: 16 }}
              message="解析提示"
              description={(
                <div>
                  {preview.warnings.map(item => (
                    <div key={item}>{item}</div>
                  ))}
                </div>
              )}
            />
          ) : null}

          <div style={{ marginTop: 16 }}>
            <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
              生成脚本预览
            </Text>
            <TextArea rows={12} value={preview.script_content} readOnly />
          </div>
        </>
      ) : null}
    </Modal>
  )
}

export default CurlToK6Modal
