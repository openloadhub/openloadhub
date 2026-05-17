import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Descriptions,
  Empty,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  scriptApi,
  type OpenApiToK6EndpointItem,
  type OpenApiToK6PreviewResponse,
  type OpenApiToK6ScriptCreateResponse,
  type OpenApiToK6SpecParseResponse,
} from '@/services/scriptApi'

const { TextArea } = Input
const { Text } = Typography
type EndpointFilterMode = 'all' | 'supported' | 'unsupported'

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

interface OpenApiToK6ModalProps {
  open: boolean
  initialName?: string
  onCancel: () => void
  onSuccess: (result: OpenApiToK6ScriptCreateResponse) => void
}

const endpointRowKey = (item: OpenApiToK6EndpointItem) => `${item.method}:${item.path}`

const buildServerOptions = (
  parseResult: OpenApiToK6SpecParseResponse | null,
  endpoint?: OpenApiToK6EndpointItem | null,
): string[] => {
  const values = [endpoint?.server_url, ...(parseResult?.server_urls || [])]
  return values.filter((item, index, array): item is string => Boolean(item) && array.indexOf(item) === index)
}

const resolveServerSelection = (
  parseResult: OpenApiToK6SpecParseResponse | null,
  endpoint?: OpenApiToK6EndpointItem | null,
  current?: string,
): string | undefined => {
  const options = buildServerOptions(parseResult, endpoint)
  if (current && options.includes(current)) {
    return current
  }
  return options[0]
}

const matchesEndpointSearch = (item: OpenApiToK6EndpointItem, keyword: string): boolean => {
  const normalized = keyword.trim().toLowerCase()
  if (!normalized) {
    return true
  }
  return [
    item.method,
    item.path,
    item.summary,
    item.operation_id,
    ...(Array.isArray(item.tags) ? item.tags : []),
  ].some(value => String(value || '').toLowerCase().includes(normalized))
}

const OpenApiToK6Modal: React.FC<OpenApiToK6ModalProps> = ({
  open,
  initialName,
  onCancel,
  onSuccess,
}) => {
  const [specContent, setSpecContent] = useState('')
  const [scriptName, setScriptName] = useState('')
  const [selectedEndpointKey, setSelectedEndpointKey] = useState<string>()
  const [selectedServerUrl, setSelectedServerUrl] = useState<string>()
  const [parseResult, setParseResult] = useState<OpenApiToK6SpecParseResponse | null>(null)
  const [preview, setPreview] = useState<OpenApiToK6PreviewResponse | null>(null)
  const [endpointSearch, setEndpointSearch] = useState('')
  const [endpointFilter, setEndpointFilter] = useState<EndpointFilterMode>('supported')
  const [parseLoading, setParseLoading] = useState(false)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (!open) {
      return
    }
    setSpecContent('')
    setScriptName(initialName || '')
    setSelectedEndpointKey(undefined)
    setSelectedServerUrl(undefined)
    setParseResult(null)
    setPreview(null)
    setEndpointSearch('')
    setEndpointFilter('supported')
  }, [initialName, open])

  const selectedEndpoint = useMemo(() => {
    if (!parseResult || !selectedEndpointKey) {
      return null
    }
    return parseResult.endpoints.find(item => endpointRowKey(item) === selectedEndpointKey) || null
  }, [parseResult, selectedEndpointKey])

  const filteredEndpoints = useMemo(() => {
    if (!parseResult) {
      return []
    }
    return parseResult.endpoints.filter(item => {
      if (endpointFilter === 'supported' && !item.request_body_supported) {
        return false
      }
      if (endpointFilter === 'unsupported' && item.request_body_supported) {
        return false
      }
      return matchesEndpointSearch(item, endpointSearch)
    })
  }, [endpointFilter, endpointSearch, parseResult])

  const serverOptions = useMemo(() => {
    return buildServerOptions(parseResult, selectedEndpoint).map(item => ({ label: item, value: item }))
  }, [parseResult, selectedEndpoint])

  useEffect(() => {
    if (!parseResult) {
      return
    }

    const nextSelected =
      (selectedEndpointKey
        ? filteredEndpoints.find(item => endpointRowKey(item) === selectedEndpointKey)
        : null) || null

    if (nextSelected) {
      const nextServerUrl = resolveServerSelection(parseResult, nextSelected, selectedServerUrl)
      if (nextServerUrl !== selectedServerUrl) {
        setSelectedServerUrl(nextServerUrl)
      }
      return
    }

    const fallbackEndpoint =
      filteredEndpoints.find(item => item.request_body_supported) || filteredEndpoints[0] || null
    setSelectedEndpointKey(fallbackEndpoint ? endpointRowKey(fallbackEndpoint) : undefined)
    setSelectedServerUrl(resolveServerSelection(parseResult, fallbackEndpoint))
    setPreview(null)
  }, [filteredEndpoints, parseResult, selectedEndpointKey, selectedServerUrl])

  const handleParse = async () => {
    if (!specContent.trim()) {
      message.warning('请输入 OpenAPI JSON/YAML')
      return
    }

    setParseLoading(true)
    try {
      const result = await scriptApi.parseOpenApiSpec({
        spec_content: specContent.trim(),
      })
      const nextFilter: EndpointFilterMode =
        result.supported_endpoint_count > 0 ? 'supported' : 'all'
      const nextEndpoints = result.endpoints.filter(item => (
        nextFilter !== 'supported' || item.request_body_supported
      ))
      const firstSupported = nextEndpoints.find(item => item.request_body_supported) || nextEndpoints[0] || result.endpoints[0]
      setParseResult(result)
      setEndpointSearch('')
      setEndpointFilter(nextFilter)
      setSelectedEndpointKey(firstSupported ? endpointRowKey(firstSupported) : undefined)
      setSelectedServerUrl(resolveServerSelection(result, firstSupported))
      setPreview(null)
      message.success(`已解析 ${result.endpoints.length} 个接口，可生成 ${result.supported_endpoint_count} 个`)
    } catch {
      setParseResult(null)
      setSelectedEndpointKey(undefined)
      setSelectedServerUrl(undefined)
      setPreview(null)
      setEndpointSearch('')
      setEndpointFilter('supported')
    } finally {
      setParseLoading(false)
    }
  }

  const handlePreview = async () => {
    if (!specContent.trim()) {
      message.warning('请输入 OpenAPI JSON/YAML')
      return
    }
    if (!selectedEndpoint) {
      message.warning('请先选择一个接口')
      return
    }
    if (!selectedEndpoint.request_body_supported) {
      message.warning('当前接口的请求体类型暂不支持自动生成')
      return
    }

    setPreviewLoading(true)
    try {
      const result = await scriptApi.previewK6FromOpenApi({
        spec_content: specContent.trim(),
        path: selectedEndpoint.path,
        method: selectedEndpoint.method,
        name: scriptName.trim() || undefined,
        server_url: selectedServerUrl || selectedEndpoint.server_url || undefined,
      })
      setPreview(result)
      message.success('OpenAPI 单接口草稿已生成')
    } catch {
      setPreview(null)
    } finally {
      setPreviewLoading(false)
    }
  }

  const handleConfirm = async () => {
    if (!specContent.trim()) {
      message.warning('请输入 OpenAPI JSON/YAML')
      return
    }
    if (!selectedEndpoint) {
      message.warning('请先选择一个接口')
      return
    }
    if (!selectedEndpoint.request_body_supported) {
      message.warning('当前接口的请求体类型暂不支持自动生成')
      return
    }

    setSubmitting(true)
    try {
      const result = await scriptApi.generateK6FromOpenApi({
        spec_content: specContent.trim(),
        path: selectedEndpoint.path,
        method: selectedEndpoint.method,
        name: scriptName.trim() || undefined,
        server_url: selectedServerUrl || selectedEndpoint.server_url || undefined,
      })
      onSuccess(result)
    } catch {
      // request.ts already surfaces user-facing errors
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      title="从 OpenAPI 生成 K6 草稿"
      open={open}
      onCancel={onCancel}
      destroyOnClose
      width={960}
      footer={[
        <Button key="cancel" onClick={onCancel}>取消</Button>,
        <Button key="parse" onClick={handleParse} loading={parseLoading}>
          解析接口
        </Button>,
        <Button
          key="preview"
          onClick={handlePreview}
          loading={previewLoading}
          disabled={!selectedEndpoint || !selectedEndpoint.request_body_supported}
        >
          预览草稿
        </Button>,
        <Button
          key="ok"
          type="primary"
          onClick={handleConfirm}
          loading={submitting}
          disabled={!selectedEndpoint || !selectedEndpoint.request_body_supported}
        >
          生成脚本
        </Button>,
      ]}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="当前只支持 OpenAPI 3.x 的单接口草稿生成"
        description="支持粘贴 OpenAPI JSON/YAML、选择一个接口并生成单接口 K6 草稿；多接口编排和复杂请求体需要手动补充。"
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
        OpenAPI Spec
      </Text>
      <TextArea
        rows={12}
        placeholder="支持直接粘贴 OpenAPI 3.x JSON 或 YAML"
        value={specContent}
        onChange={event => {
          setSpecContent(event.target.value)
          setParseResult(null)
          setSelectedEndpointKey(undefined)
          setSelectedServerUrl(undefined)
          setPreview(null)
        }}
      />

      {parseResult ? (
        <div style={{ marginTop: 16 }}>
          <Descriptions
            size="small"
            column={2}
            items={[
              { key: 'title', label: '接口文档标题', children: parseResult.title || '-' },
              { key: 'version', label: '接口文档版本', children: parseResult.version || '-' },
              { key: 'count', label: '接口数量', children: parseResult.endpoints.length },
              { key: 'supported', label: '可生成', children: parseResult.supported_endpoint_count },
              { key: 'unsupported', label: '暂不支持', children: parseResult.unsupported_endpoint_count },
              { key: 'filtered', label: '当前筛选', children: filteredEndpoints.length },
              {
                key: 'server',
                label: '默认服务地址',
                children: serverOptions[0]?.value || '-',
              },
            ]}
          />

          {parseResult.warnings.length > 0 ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginTop: 12 }}
              message="解析提示"
              description={(
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {parseResult.warnings.map(item => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              )}
            />
          ) : null}

          {serverOptions.length > 0 ? (
            <div style={{ marginTop: 12 }}>
              <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
                生成时使用的服务地址
              </Text>
              <Select
                style={{ width: '100%' }}
                value={selectedServerUrl}
                onChange={value => {
                  setSelectedServerUrl(value)
                  setPreview(null)
                }}
                options={serverOptions}
              />
            </div>
          ) : null}

          <div style={{ marginTop: 16 }}>
            <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
              选择要生成的接口
            </Text>
            <div style={{ display: 'flex', gap: 12, marginBottom: 12, flexWrap: 'wrap' }}>
              <Input
                allowClear
                placeholder="按方法、路径、说明或标签搜索"
                value={endpointSearch}
                onChange={event => {
                  setEndpointSearch(event.target.value)
                  setPreview(null)
                }}
                style={{ flex: '1 1 320px', minWidth: 280 }}
              />
              <Select
                style={{ width: 220 }}
                value={endpointFilter}
                onChange={value => {
                  setEndpointFilter(value)
                  setPreview(null)
                }}
                options={[
                  { label: '仅看可生成', value: 'supported' },
                  { label: '查看全部', value: 'all' },
                  { label: '仅看暂不支持', value: 'unsupported' },
                ]}
              />
            </div>
            <Table
              size="small"
              rowKey={endpointRowKey}
              pagination={false}
              dataSource={filteredEndpoints}
              rowSelection={{
                type: 'radio',
                selectedRowKeys: selectedEndpointKey ? [selectedEndpointKey] : [],
                onChange: keys => {
                  const nextKey = String(keys[0] || '')
                  const nextEndpoint = filteredEndpoints.find(item => endpointRowKey(item) === nextKey) || null
                  setSelectedEndpointKey(nextKey || undefined)
                  setSelectedServerUrl(resolveServerSelection(parseResult, nextEndpoint, selectedServerUrl))
                  setPreview(null)
                },
              }}
              columns={[
                {
                  title: '方法',
                  dataIndex: 'method',
                  key: 'method',
                  width: 100,
                  render: (value: string) => <Tag color="blue">{String(value).toUpperCase()}</Tag>,
                },
                {
                  title: '接口路径',
                  dataIndex: 'path',
                  key: 'path',
                },
                {
                  title: '接口说明',
                  dataIndex: 'summary',
                  key: 'summary',
                  render: (value?: string | null) => value || '-',
                },
                {
                  title: '请求体',
                  dataIndex: 'request_content_types',
                  key: 'request_content_types',
                  width: 220,
                  render: (value: string[]) => (
                    <Space wrap size={[4, 4]}>
                      {(Array.isArray(value) && value.length > 0 ? value : ['无请求体']).map(item => (
                        <Tag key={String(item)}>{String(item)}</Tag>
                      ))}
                    </Space>
                  ),
                },
                {
                  title: '状态',
                  dataIndex: 'request_body_supported',
                  key: 'request_body_supported',
                  width: 120,
                  render: (value: boolean) => (
                    <Tag color={value ? 'green' : 'orange'}>
                      {value ? '可生成' : '需人工处理'}
                    </Tag>
                  ),
                },
              ]}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前筛选条件下无匹配接口" /> }}
            />
          </div>
        </div>
      ) : null}

      {selectedEndpoint && !selectedEndpoint.request_body_supported ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 16 }}
          message="当前接口暂不支持自动生成"
          description="当前只覆盖常见请求体类型。若该接口需要更复杂的上传或鉴权，请先改走手写脚本。"
        />
      ) : null}

      {preview ? (
        <div style={{ marginTop: 16 }}>
          <Descriptions
            size="small"
            column={1}
            items={[
              { key: 'method', label: '请求方法', children: preview.parsed.method },
              { key: 'path', label: '接口路径', children: preview.parsed.path },
              { key: 'source_url', label: '示例 URL', children: preview.parsed.source_url },
              { key: 'summary', label: '接口摘要', children: preview.parsed.summary || '-' },
              { key: 'operation_id', label: '接口操作标识', children: preview.parsed.operation_id || '-' },
              { key: 'content_type', label: '请求体类型', children: preview.parsed.request_content_type || '-' },
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
            <Text type="secondary">路径参数</Text>
            <div style={{ marginTop: 8 }}>
              <Table
                size="small"
                rowKey={record => `${record.key}-${record.value}`}
                pagination={false}
                columns={SIMPLE_COLUMNS}
                dataSource={preview.parsed.path_items}
                locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无" /> }}
              />
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
              请求体字段
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
              请求体原始预览
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
            <TextArea rows={14} value={preview.script_content} readOnly />
          </div>
        </div>
      ) : null}
    </Modal>
  )
}

export default OpenApiToK6Modal
