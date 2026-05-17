import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Descriptions,
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
  type HarToK6EntryItem,
  type HarToK6PreviewResponse,
  type HarToK6ScriptCreateResponse,
  type HarToK6SpecParseResponse,
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

interface HarToK6ModalProps {
  open: boolean
  initialName?: string
  onCancel: () => void
  onSuccess: (result: HarToK6ScriptCreateResponse) => void
}

const entryRowKey = (item: HarToK6EntryItem) => String(item.index)

const HarToK6Modal: React.FC<HarToK6ModalProps> = ({
  open,
  initialName,
  onCancel,
  onSuccess,
}) => {
  const [harContent, setHarContent] = useState('')
  const [scriptName, setScriptName] = useState('')
  const [selectedEntryIndex, setSelectedEntryIndex] = useState<number>()
  const [parseResult, setParseResult] = useState<HarToK6SpecParseResponse | null>(null)
  const [preview, setPreview] = useState<HarToK6PreviewResponse | null>(null)
  const [parseLoading, setParseLoading] = useState(false)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (!open) {
      return
    }
    setHarContent('')
    setScriptName(initialName || '')
    setSelectedEntryIndex(undefined)
    setParseResult(null)
    setPreview(null)
  }, [initialName, open])

  const selectedEntry = useMemo(() => {
    if (!parseResult || selectedEntryIndex == null) {
      return null
    }
    return parseResult.entries.find(item => item.index === selectedEntryIndex) || null
  }, [parseResult, selectedEntryIndex])

  const handleParse = async () => {
    if (!harContent.trim()) {
      message.warning('请输入 HAR JSON')
      return
    }

    setParseLoading(true)
    try {
      const result = await scriptApi.parseHarSpec({ har_content: harContent.trim() })
      setParseResult(result)
      setSelectedEntryIndex(result.entries[0]?.index)
      setPreview(null)
      message.success(`已解析 ${result.entries.length} 个可生成条目`)
    } catch {
      setParseResult(null)
      setSelectedEntryIndex(undefined)
      setPreview(null)
    } finally {
      setParseLoading(false)
    }
  }

  const buildPayload = () => {
    if (!harContent.trim()) {
      message.warning('请输入 HAR JSON')
      return null
    }
    if (selectedEntryIndex == null) {
      message.warning('请先选择一个 HAR 条目')
      return null
    }
    return {
      har_content: harContent.trim(),
      entry_index: selectedEntryIndex,
      name: scriptName.trim() || undefined,
    }
  }

  const handlePreview = async () => {
    const payload = buildPayload()
    if (!payload) {
      return
    }

    setPreviewLoading(true)
    try {
      const result = await scriptApi.previewK6FromHar(payload)
      setPreview(result)
      message.success('HAR 单接口草稿已生成')
    } catch {
      setPreview(null)
    } finally {
      setPreviewLoading(false)
    }
  }

  const handleConfirm = async () => {
    const payload = buildPayload()
    if (!payload) {
      return
    }

    setSubmitting(true)
    try {
      const result = await scriptApi.generateK6FromHar(payload)
      onSuccess(result)
    } catch {
      // request.ts already surfaces the user-facing error message
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      title="从 HAR 生成 K6 脚本"
      open={open}
      onCancel={onCancel}
      destroyOnClose
      width={860}
      footer={[
        <Button key="cancel" onClick={onCancel}>取消</Button>,
        <Button key="parse" onClick={handleParse} loading={parseLoading}>
          解析 HAR
        </Button>,
        <Button key="preview" onClick={handlePreview} loading={previewLoading}>
          生成预览
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
        message="当前支持 HAR 单条目到平台标准 K6"
        description="本期先补齐 HAR 导入闭环：解析 HAR 条目、选择一个 HTTP/HTTPS 请求、预览并生成单接口 K6 脚本。多条目编排、复杂认证配置和文件上传会放到统一导入向导后续阶段。"
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
        HAR JSON
      </Text>
      <TextArea
        rows={8}
        placeholder="粘贴浏览器导出的 HAR JSON"
        value={harContent}
        onChange={event => {
          setHarContent(event.target.value)
          setParseResult(null)
          setSelectedEntryIndex(undefined)
          setPreview(null)
        }}
      />

      {parseResult ? (
        <div style={{ marginTop: 16 }}>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Space wrap>
              <Tag color="blue">{`可生成 ${parseResult.supported_entry_count}`}</Tag>
              <Tag>{`跳过 ${parseResult.unsupported_entry_count}`}</Tag>
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
                { title: 'URL', dataIndex: 'url', key: 'url', ellipsis: true },
                { title: '状态', dataIndex: 'status', key: 'status', width: 90 },
                {
                  title: 'Body',
                  dataIndex: 'body_present',
                  key: 'body_present',
                  width: 90,
                  render: value => (value ? <Tag color="orange">yes</Tag> : <Tag>no</Tag>),
                },
              ]}
              dataSource={parseResult.entries}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有可生成条目" /> }}
            />
            {parseResult.warnings.length > 0 ? (
              <Alert
                type="warning"
                showIcon
                message="解析提示"
                description={parseResult.warnings.join('；')}
              />
            ) : null}
          </Space>
        </div>
      ) : null}

      {preview ? (
        <>
          <Descriptions
            size="small"
            column={1}
            style={{ marginTop: 16 }}
            items={[
              { key: 'entry', label: '条目', children: preview.parsed.entry_index },
              { key: 'method', label: '请求方法', children: preview.parsed.method },
              { key: 'url', label: '目标 URL', children: preview.parsed.url },
              { key: 'task_name', label: '建议任务名', children: preview.parsed.suggested_task_name || '-' },
              { key: 'mime', label: 'MIME', children: preview.parsed.mime_type || '-' },
            ]}
          />

          <div style={{ marginTop: 16 }}>
            <Text type="secondary">建议变量</Text>
            <div style={{ marginTop: 8 }}>
              <Space wrap size={[8, 8]}>
                {preview.suggested_variables.map(item => (
                  <Space key={item.key} size={[6, 6]} wrap>
                    <Tag color={item.sensitive ? 'orange' : 'blue'}>
                      {item.key}
                    </Tag>
                    {item.source ? <Text type="secondary">{item.source}</Text> : null}
                  </Space>
                ))}
              </Space>
            </div>
          </div>

          <div style={{ marginTop: 16 }}>
            <Text type="secondary">请求 Headers</Text>
            <Table
              size="small"
              rowKey={record => `${record.key}-${record.value}`}
              pagination={false}
              columns={SIMPLE_COLUMNS}
              dataSource={preview.parsed.header_items}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无" /> }}
              style={{ marginTop: 8 }}
            />
          </div>

          <div style={{ marginTop: 16 }}>
            <Text type="secondary">Body 字段</Text>
            <Table
              size="small"
              rowKey={record => `${record.key}-${record.value}`}
              pagination={false}
              columns={SIMPLE_COLUMNS}
              dataSource={preview.parsed.body_items}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无结构化字段" /> }}
              style={{ marginTop: 8 }}
            />
          </div>

          {preview.parsed.body_preview ? (
            <div style={{ marginTop: 16 }}>
              <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
                Body 原始预览
              </Text>
              <TextArea rows={5} value={preview.parsed.body_preview} readOnly />
            </div>
          ) : null}

          {preview.warnings.length > 0 ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginTop: 16 }}
              message="生成提示"
              description={preview.warnings.join('；')}
            />
          ) : null}

          <div style={{ marginTop: 16 }}>
            <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
              生成脚本预览
            </Text>
            <TextArea rows={10} value={preview.script_content} readOnly />
          </div>
        </>
      ) : selectedEntry ? (
        <Alert
          type="success"
          showIcon
          style={{ marginTop: 16 }}
          message={`已选择 ${selectedEntry.method} ${selectedEntry.path}`}
          description={selectedEntry.url}
        />
      ) : null}
    </Modal>
  )
}

export default HarToK6Modal
