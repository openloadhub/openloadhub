import { Upload, message, Typography } from 'antd'
import { InboxOutlined, FileTextOutlined } from '@ant-design/icons'
import type { UploadProps } from 'antd'
import { scriptApi, type Script } from '@/services/scriptApi'

const { Dragger } = Upload
const { Text } = Typography

interface ScriptUploadPanelProps {
  value?: number // selected script ID
  onChange?: (scriptId: number) => void
  uploadedScripts?: Script[] // Optional: pass externally or fetch internally
  onRefresh?: () => void
  compact?: boolean
  engineType?: 'jmeter' | 'k6' | 'custom'
}

const SCRIPT_UPLOAD_CONFIG = {
  jmeter: {
    accept: '.jmx',
    hint: '仅支持 JMeter (.jmx) 脚本，单个文件不超过 10MB',
    error: '当前压测脚本类型为 JMeter，只能上传 .jmx 文件',
  },
  k6: {
    accept: '.js',
    hint: '仅支持 K6 (.js) 脚本，单个文件不超过 10MB',
    error: '当前压测脚本类型为 K6，只能上传 .js 文件',
  },
  custom: {
    accept: '.jmx,.js',
    hint: '支持 JMeter (.jmx) 和 K6 (.js) 格式，单个文件不超过 10MB',
    error: '仅支持上传 .jmx 或 .js 脚本文件',
  },
} as const

function resolveUploadedScriptId(payload: unknown): number | undefined {
  if (!payload || typeof payload !== 'object') {
    return undefined
  }

  const directId = (payload as { id?: unknown }).id
  if (typeof directId === 'number' && Number.isFinite(directId)) {
    return directId
  }

  const nestedId = (payload as { data?: { id?: unknown } }).data?.id
  if (typeof nestedId === 'number' && Number.isFinite(nestedId)) {
    return nestedId
  }

  return undefined
}

const ScriptUploadPanel: React.FC<ScriptUploadPanelProps> = ({
  value,
  onChange,
  onRefresh,
  compact = false,
  engineType = 'custom',
}) => {
  const uploadConfig = SCRIPT_UPLOAD_CONFIG[engineType] ?? SCRIPT_UPLOAD_CONFIG.custom

  const beforeUpload: UploadProps['beforeUpload'] = file => {
    const fileName = file.name.toLowerCase()
    const allowedSuffixes = uploadConfig.accept.split(',').map(item => item.trim().toLowerCase())
    const matched = allowedSuffixes.some(suffix => fileName.endsWith(suffix))
    if (!matched) {
      message.error(uploadConfig.error)
      return Upload.LIST_IGNORE
    }
    return true
  }

  const handleUploadChange: UploadProps['onChange'] = async (info) => {
    const { status } = info.file

    if (status === 'done') {
      const uploadedScriptId = resolveUploadedScriptId(info.file.response)
      if (uploadedScriptId && uploadedScriptId !== value) {
        onChange?.(uploadedScriptId)
      }
      message.success(`${info.file.name} 上传成功`)
      onRefresh?.()
    } else if (status === 'error') {
      message.error(`${info.file.name} 上传失败`)
    }
  }

  const customRequest: UploadProps['customRequest'] = async (options) => {
    const { file, onSuccess, onError } = options
    try {
      const resp = await scriptApi.uploadScript(file as File)
      onSuccess?.(resp)
      onChange?.(resp.id) // Auto-select uploaded script
    } catch (err) {
      onError?.(err as Error)
    }
  }

  const props: UploadProps = {
    name: 'file',
    multiple: false,
    accept: uploadConfig.accept,
    beforeUpload,
    customRequest,
    onChange: handleUploadChange,
    onDrop(e) {
      console.log('Dropped files', e.dataTransfer.files)
    },
    showUploadList: false, // We'll show a custom list below
  }

  return (
    <div className="script-upload-panel">
      <Dragger
        {...props}
        style={{
          background: 'var(--color-bg-container)',
          borderColor: 'var(--border-color)',
          padding: compact ? '8px 12px' : undefined,
        }}
      >
        <p className="ant-upload-drag-icon">
          <InboxOutlined style={{ color: 'var(--primary-color)', fontSize: compact ? 28 : undefined }} />
        </p>
        <p
          className="ant-upload-text"
          style={{ color: 'var(--text-primary)', marginBottom: compact ? 4 : undefined }}
        >
          点击或拖拽脚本文件到此区域上传
        </p>
        <p
          className="ant-upload-hint"
          style={{ color: 'var(--text-secondary)', marginBottom: 0, fontSize: compact ? 12 : undefined }}
        >
          {uploadConfig.hint}
        </p>
      </Dragger>

      {value && (
        <div
          style={{
            marginTop: compact ? 10 : 16,
            padding: compact ? 10 : 12,
            border: '1px solid var(--border-color)',
            borderRadius: 8,
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center' }}>
              <FileTextOutlined
                style={{ fontSize: 24, marginRight: 12, color: 'var(--primary-color)' }}
              />
              <div>
                <Text strong style={{ color: 'var(--text-primary)' }}>
                  当前已选脚本版本：{value}
                </Text>
                <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                  上传完成，可直接使用
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default ScriptUploadPanel
