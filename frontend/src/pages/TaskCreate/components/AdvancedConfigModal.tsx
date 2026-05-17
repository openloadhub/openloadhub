import React, { useEffect, useState } from 'react'
import { Modal, Input, Button, Tabs, Form, InputNumber, message } from 'antd'
import { KeyValueEditor, type KeyValuePair } from '@/components/common'

const { TextArea } = Input

export interface AdvancedConfigValue {
  headers: KeyValuePair[]
  connect_timeout_ms?: number | null
  response_timeout_ms?: number | null
  target_url?: string | null
}

interface AdvancedConfigModalProps {
  open: boolean
  value?: AdvancedConfigValue
  showTimeouts?: boolean
  onCancel: () => void
  onOk: (values: AdvancedConfigValue) => void
}

const parseCurl = (
  curl: string,
  options?: { includeTimeouts?: boolean }
): Partial<AdvancedConfigValue> => {
  const result: Partial<AdvancedConfigValue> = {}

  const headers: KeyValuePair[] = []
  const headerPattern = /(?:-H|--header)\s+(?:"([^"]+)"|'([^']+)'|([^\s]+))/g
  let headerMatch: RegExpExecArray | null = headerPattern.exec(curl)
  while (headerMatch) {
    const raw = (headerMatch[1] || headerMatch[2] || headerMatch[3] || '').trim()
    const idx = raw.indexOf(':')
    if (idx > 0) {
      const key = raw.slice(0, idx).trim()
      const value = raw.slice(idx + 1).trim()
      if (key) headers.push({ key, value })
    }
    headerMatch = headerPattern.exec(curl)
  }
  if (headers.length > 0) result.headers = headers

  if (options?.includeTimeouts) {
    const connectTimeoutMatch = curl.match(/--connect-timeout\s+([0-9.]+)/)
    if (connectTimeoutMatch?.[1]) {
      const sec = Number(connectTimeoutMatch[1])
      if (Number.isFinite(sec)) {
        result.connect_timeout_ms = Math.round(sec * 1000)
      }
    }

    const maxTimeMatch = curl.match(/--max-time\s+([0-9.]+)/)
    if (maxTimeMatch?.[1]) {
      const sec = Number(maxTimeMatch[1])
      if (Number.isFinite(sec)) {
        result.response_timeout_ms = Math.round(sec * 1000)
      }
    }

    const urlMatch = curl.match(/https?:\/\/[^\s'"]+/)
    if (urlMatch?.[0]) {
      result.target_url = urlMatch[0]
    }
  }

  return result
}

const parseHar = (
  content: string,
  options?: { includeTimeouts?: boolean }
): Partial<AdvancedConfigValue> => {
  const parsed = JSON.parse(content) as {
    log?: {
      entries?: Array<{
        request?: {
          url?: string
          headers?: Array<{ name?: string; value?: string }>
        }
        timings?: {
          connect?: number
          wait?: number
          receive?: number
        }
      }>
    }
  }
  const entry = parsed.log?.entries?.find(item => item?.request?.url) ?? parsed.log?.entries?.[0]
  if (!entry?.request) {
    throw new Error('HAR 中没有可解析的请求')
  }

  const headers =
    entry.request.headers
      ?.map(item => ({
        key: String(item.name || '').trim(),
        value: String(item.value || '').trim(),
      }))
      .filter(item => item.key && item.value) ?? []
  const result: Partial<AdvancedConfigValue> = {}
  if (headers.length > 0) {
    result.headers = headers
  }
  if (options?.includeTimeouts) {
    if (entry.request.url) {
      result.target_url = entry.request.url
    }
    const connect = Number(entry.timings?.connect)
    if (Number.isFinite(connect) && connect > 0) {
      result.connect_timeout_ms = Math.round(connect)
    }
    const response = Number(entry.timings?.wait ?? 0) + Number(entry.timings?.receive ?? 0)
    if (Number.isFinite(response) && response > 0) {
      result.response_timeout_ms = Math.round(response)
    }
  }
  return result
}

const AdvancedConfigModal: React.FC<AdvancedConfigModalProps> = ({
  open,
  value,
  showTimeouts = true,
  onCancel,
  onOk,
}) => {
  const [form] = Form.useForm()
  const [curl, setCurl] = useState('')
  const [har, setHar] = useState('')
  const [activeTab, setActiveTab] = useState('curl')
  const [headers, setHeaders] = useState<KeyValuePair[]>(value?.headers || [])

  useEffect(() => {
    if (!open) return
    setHeaders(value?.headers || [])
    if (!showTimeouts) {
      setActiveTab('curl')
      return
    }
    form.setFieldsValue({
      connect_timeout_ms: value?.connect_timeout_ms ?? null,
      response_timeout_ms: value?.response_timeout_ms ?? null,
      target_url: value?.target_url ?? null,
    })
  }, [form, open, showTimeouts, value])

  const handleParseCurl = () => {
    if (!curl) {
      message.warning('请输入 CURL 命令')
      return
    }

    const parsed = parseCurl(curl, { includeTimeouts: showTimeouts })
    if (parsed.headers) {
      setHeaders(parsed.headers)
    }
    if (showTimeouts) {
      form.setFieldsValue({
        connect_timeout_ms: parsed.connect_timeout_ms ?? form.getFieldValue('connect_timeout_ms'),
        response_timeout_ms:
          parsed.response_timeout_ms ?? form.getFieldValue('response_timeout_ms'),
        target_url: parsed.target_url ?? form.getFieldValue('target_url'),
      })
    }
    message.success('CURL 解析完成')
  }

  const handleParseHar = () => {
    if (!har.trim()) {
      message.warning('请输入 HAR 内容')
      return
    }

    try {
      const parsed = parseHar(har, { includeTimeouts: showTimeouts })
      if (parsed.headers) {
        setHeaders(parsed.headers)
      }
      if (showTimeouts) {
        form.setFieldsValue({
          connect_timeout_ms: parsed.connect_timeout_ms ?? form.getFieldValue('connect_timeout_ms'),
          response_timeout_ms:
            parsed.response_timeout_ms ?? form.getFieldValue('response_timeout_ms'),
          target_url: parsed.target_url ?? form.getFieldValue('target_url'),
        })
      }
      message.success('HAR 解析完成')
    } catch (error) {
      message.error(error instanceof Error ? error.message : 'HAR 解析失败')
    }
  }

  const handleConfirm = async () => {
    const values = showTimeouts ? await form.validateFields() : {}
    const normalizedHeaders = headers.filter(item => item.key.trim() && item.value.trim())
    onOk({
      headers: normalizedHeaders,
      ...(showTimeouts
        ? {
            connect_timeout_ms: values.connect_timeout_ms ?? null,
            response_timeout_ms: values.response_timeout_ms ?? null,
            target_url: values.target_url?.trim() || null,
          }
        : {}),
    })
  }

  return (
    <Modal
      title="高级配置"
      open={open}
      onCancel={onCancel}
      width={800}
      footer={[
        <Button key="cancel" onClick={onCancel}>
          取消
        </Button>,
        <Button key="ok" type="primary" onClick={handleConfirm}>
          确定
        </Button>,
      ]}
    >
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: 'curl',
            label: 'CURL 导入',
            children: (
              <div>
                <TextArea
                  rows={6}
                  placeholder="粘贴 CURL 命令，自动解析配置"
                  value={curl}
                  onChange={e => setCurl(e.target.value)}
                  style={{ marginBottom: 16 }}
                />
                <Button type="primary" onClick={handleParseCurl}>
                  解析配置
                </Button>
              </div>
            ),
          },
          {
            key: 'har',
            label: 'HAR 导入',
            children: (
              <div>
                <TextArea
                  rows={6}
                  placeholder="粘贴浏览器导出的 HAR JSON，自动解析请求 Headers"
                  value={har}
                  onChange={e => setHar(e.target.value)}
                  style={{ marginBottom: 16 }}
                />
                <Button type="primary" onClick={handleParseHar}>
                  解析配置
                </Button>
              </div>
            ),
          },
          {
            key: 'headers',
            label: '全局 Headers',
            children: (
              <KeyValueEditor
                value={headers}
                onChange={setHeaders}
                keyPlaceholder="Header 名称"
                valuePlaceholder="Header 值"
              />
            ),
          },
          showTimeouts
            ? {
                key: 'timeouts',
                label: '超时设置',
                children: (
                  <Form form={form} layout="vertical">
                    <Form.Item name="connect_timeout_ms" label="连接超时 (ms)">
                      <InputNumber style={{ width: '100%' }} min={0} placeholder="默认 5000" />
                    </Form.Item>
                    <Form.Item name="response_timeout_ms" label="响应超时 (ms)">
                      <InputNumber style={{ width: '100%' }} min={0} placeholder="默认 30000" />
                    </Form.Item>
                    <Form.Item name="target_url" label="目标 URL">
                      <Input placeholder="例如：https://api.example.com/path" />
                    </Form.Item>
                  </Form>
                ),
              }
            : null,
        ].filter((item): item is NonNullable<typeof item> => Boolean(item))}
      />
    </Modal>
  )
}

export default AdvancedConfigModal
