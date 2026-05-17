import { useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, Space, Spin, Typography } from 'antd'
import { ArrowLeftOutlined, DownloadOutlined, ExportOutlined } from '@ant-design/icons'
import { useNavigate, useParams } from 'react-router-dom'

import { reportApi } from '@/services/reportApi'
import { useThemeStore } from '@/store/themeStore'
import { buildReportPreviewBlob } from './reportPreviewTheme'

const { Text } = Typography

type ReportHtmlViewerProps = {
  kind: 'run'
}

const downloadBlob = (blob: Blob, filename: string) => {
  const url = window.URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.style.display = 'none'
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  window.URL.revokeObjectURL(url)
  document.body.removeChild(link)
}

const ReportHtmlViewer = ({ kind }: ReportHtmlViewerProps) => {
  const navigate = useNavigate()
  const theme = useThemeStore(state => state.theme)
  const { id } = useParams<{ id?: string }>()
  const [htmlBlob, setHtmlBlob] = useState<Blob | null>(null)
  const [objectUrl, setObjectUrl] = useState<string | null>(null)
  const [previewObjectUrl, setPreviewObjectUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const resolvedReportId = useMemo(() => {
    const value = Number(id)
    return Number.isFinite(value) && value > 0 ? value : null
  }, [id])

  const filename = resolvedReportId ? `report_${resolvedReportId}.html` : 'report.html'

  useEffect(() => {
    let cancelled = false
    let nextObjectUrl: string | null = null

    const load = async () => {
      if (kind !== 'run' || !resolvedReportId) {
        setError('报告链接参数无效')
        setLoading(false)
        return
      }

      setLoading(true)
      setError(null)
      try {
        const blob = await reportApi.downloadReport(resolvedReportId)
        if (cancelled) {
          return
        }
        nextObjectUrl = window.URL.createObjectURL(blob)
        setHtmlBlob(blob)
        setObjectUrl(nextObjectUrl)
      } catch (loadError) {
        if (!cancelled) {
          setError(loadError instanceof Error ? loadError.message : '报告加载失败')
        }
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }

    load()

    return () => {
      cancelled = true
      if (nextObjectUrl) {
        window.URL.revokeObjectURL(nextObjectUrl)
      }
    }
  }, [kind, resolvedReportId])

  useEffect(() => {
    let cancelled = false
    let nextPreviewObjectUrl: string | null = null

    const buildPreview = async () => {
      if (!htmlBlob) {
        setPreviewObjectUrl(null)
        return
      }

      const previewBlob = await buildReportPreviewBlob(htmlBlob, theme)
      if (cancelled) {
        return
      }
      nextPreviewObjectUrl = window.URL.createObjectURL(previewBlob)
      setPreviewObjectUrl(nextPreviewObjectUrl)
    }

    buildPreview()

    return () => {
      cancelled = true
      if (nextPreviewObjectUrl) {
        window.URL.revokeObjectURL(nextPreviewObjectUrl)
      }
    }
  }, [htmlBlob, theme])

  return (
    <div className="olh-page-shell olh-console-page olh-report-viewer-page" data-testid="report-html-viewer-page">
      <Card className="olh-report-viewer-toolbar" size="small" styles={{ body: { padding: 12 } }}>
        <div className="olh-report-viewer-toolbar-inner">
          <Space className="olh-report-viewer-title" direction="vertical" size={0}>
            <Text strong>子结果报告查看</Text>
            <Text type="secondary">{`Report #${resolvedReportId || '-'}`}</Text>
          </Space>
          <Space className="olh-report-viewer-actions" wrap>
            <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(-1)}>
              返回
            </Button>
            <Button
              icon={<ExportOutlined />}
              disabled={!objectUrl}
              onClick={() => objectUrl && window.open(objectUrl, '_blank', 'noopener,noreferrer')}
            >
              新标签打开
            </Button>
            <Button
              icon={<DownloadOutlined />}
              disabled={!htmlBlob}
              onClick={() => htmlBlob && downloadBlob(htmlBlob, filename)}
            >
              下载 HTML
            </Button>
          </Space>
        </div>
      </Card>

      <Card className="olh-report-viewer-frame-card" styles={{ body: { padding: 0 } }}>
        {loading ? (
          <div className="olh-report-viewer-status" data-testid="report-html-viewer-loading">
            <Spin tip="正在加载报告..." />
          </div>
        ) : error ? (
          <div className="olh-report-viewer-status" data-testid="report-html-viewer-error">
            <Alert type="error" showIcon message="报告加载失败" description={error} />
          </div>
        ) : objectUrl ? (
          <iframe
            className="olh-report-viewer-frame"
            data-preview-ready={previewObjectUrl ? 'true' : 'false'}
            data-preview-theme={theme}
            title="子结果报告查看"
            src={previewObjectUrl || objectUrl}
            sandbox="allow-scripts allow-same-origin allow-popups"
          />
        ) : (
          <div className="olh-report-viewer-status" data-testid="report-html-viewer-empty">
            <Alert type="warning" showIcon message="暂无可查看报告" />
          </div>
        )}
      </Card>
    </div>
  )
}

export default ReportHtmlViewer
