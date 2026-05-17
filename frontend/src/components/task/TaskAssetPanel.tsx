import { useMemo } from 'react'
import { Button, Empty, Space, Table, Tag, Upload, message } from 'antd'
import type { UploadProps } from 'antd'
import { DownloadOutlined, InboxOutlined, DeleteOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import dayjs from 'dayjs'

import { taskAssetApi, type TaskAsset, type TaskAssetCategory } from '@/services/taskAssetApi'
import { getTaskAssetShardCount } from '@/utils/taskStartGuard'

const { Dragger } = Upload

const INGEST_STATUS_META: Record<string, { label: string; color: string }> = {
  pending: { label: '等待', color: 'default' },
  running: { label: '处理中', color: 'processing' },
  processing: { label: '处理中', color: 'processing' },
  completed: { label: '已完成', color: 'success' },
  succeeded: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
  error: { label: '失败', color: 'error' },
}

const formatFileSize = (value: number) => {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

const formatCompressionRatio = (asset: TaskAsset): string | null => {
  if (!asset.compressed_file_size || !asset.file_size || asset.file_size <= 0) {
    return null
  }
  return `${Math.round((asset.compressed_file_size / asset.file_size) * 100)}%`
}

const renderIngestStatusTag = (status?: string | null) => {
  const normalized = status?.trim().toLowerCase() || 'completed'
  const meta = INGEST_STATUS_META[normalized] ?? { label: normalized, color: 'default' }
  return <Tag color={meta.color}>{`ingest ${meta.label}`}</Tag>
}

interface TaskAssetPanelProps {
  category: TaskAssetCategory
  title: string
  accept: string
  helperText: string
  taskId?: number
  serverAssets?: TaskAsset[]
  tempAssets: TaskAsset[]
  onTempAssetsChange: (assets: TaskAsset[]) => void
  compact?: boolean
}

const TaskAssetPanel: React.FC<TaskAssetPanelProps> = ({
  category,
  title,
  accept,
  helperText,
  taskId,
  serverAssets = [],
  tempAssets,
  onTempAssetsChange,
  compact = false,
}) => {
  const queryClient = useQueryClient()

  const { data: boundAssets, isLoading } = useQuery({
    queryKey: ['task-assets', taskId, category],
    queryFn: () => taskAssetApi.listAssets(taskId!, category),
    enabled: !!taskId,
  })

  const visibleAssets = useMemo(
    () => (taskId ? (boundAssets ?? serverAssets) : tempAssets),
    [boundAssets, serverAssets, taskId, tempAssets]
  )

  const uploadMutation = useMutation({
    mutationFn: (file: File) => taskAssetApi.uploadAssetForTask(category, file, taskId),
    onSuccess: asset => {
      message.success(`${asset.file_name} 上传成功`)
      if (taskId) {
        queryClient.invalidateQueries({ queryKey: ['task-assets', taskId, category] })
      } else {
        onTempAssetsChange([asset, ...tempAssets])
      }
    },
    onError: error => {
      message.error(error instanceof Error ? error.message : '文件上传失败')
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (asset: TaskAsset) => taskAssetApi.deleteAsset(asset.id),
    onSuccess: (_result, asset) => {
      message.success(`${asset.file_name} 已删除`)
      if (taskId) {
        queryClient.invalidateQueries({ queryKey: ['task-assets', taskId, category] })
      } else {
        onTempAssetsChange(tempAssets.filter(item => item.id !== asset.id))
      }
    },
    onError: error => {
      message.error(error instanceof Error ? error.message : '文件删除失败')
    },
  })

  const handleDownload = async (asset: TaskAsset) => {
    try {
      const blob = await taskAssetApi.downloadAsset(asset.id)
      const url = window.URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = asset.file_name
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.URL.revokeObjectURL(url)
    } catch (error) {
      message.error(error instanceof Error ? error.message : '文件下载失败')
    }
  }

  const uploadProps: UploadProps = {
    name: 'file',
    multiple: false,
    accept,
    customRequest: async options => {
      try {
        await uploadMutation.mutateAsync(options.file as File)
        options.onSuccess?.({})
      } catch (error) {
        options.onError?.(error as Error)
      }
    },
    showUploadList: false,
  }

  return (
    <div data-testid={`task-asset-panel-${category}`}>
      <Space direction="vertical" size={12} style={{ display: 'flex' }}>
        <Tag color="blue">{title}</Tag>
        <Dragger
          {...uploadProps}
          disabled={uploadMutation.isPending}
          style={{
            padding: compact ? '8px 12px' : '20px 16px',
          }}
        >
          <p className="ant-upload-drag-icon">
            <InboxOutlined style={{ color: '#1677ff', fontSize: compact ? 28 : undefined }} />
          </p>
          <p className="ant-upload-text" style={{ marginBottom: compact ? 4 : undefined }}>
            点击或拖拽文件到此区域上传
          </p>
          <p
            className="ant-upload-hint"
            style={{ marginBottom: 0, fontSize: compact ? 12 : undefined }}
          >
            {helperText}
          </p>
        </Dragger>

        <Table<TaskAsset>
          rowKey="id"
          size="small"
          loading={isLoading}
          dataSource={visibleAssets}
          pagination={false}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={taskId ? '当前任务未显式绑定文件' : '可先上传，保存任务后自动绑定'}
              />
            ),
          }}
          footer={() => {
            if (!taskId || visibleAssets.length > 0) {
              return null
            }
            return '当前编辑页只展示显式绑定到任务实体的资产；平台不会从脚本或历史 run 自动反推 proto/data 依赖。'
          }}
          columns={[
            {
              title: '文件名',
              dataIndex: 'file_name',
              key: 'file_name',
              render: (_value: string, record: TaskAsset) => (
                <Button type="link" size="small" onClick={() => handleDownload(record)}>
                  {record.file_name}
                </Button>
              ),
            },
            {
              title: '大小',
              dataIndex: 'file_size',
              key: 'file_size',
              width: 120,
              render: (value: number) => formatFileSize(value),
            },
            {
              title: category === 'data' ? '数据状态' : '类型',
              key: 'asset_type',
              width: category === 'data' ? 240 : 150,
              render: (_value: string, record: TaskAsset) => {
                const shardCount = getTaskAssetShardCount(record)
                const compressionRatio = formatCompressionRatio(record)
                return (
                  <Space size={4} wrap>
                    {category === 'data' ? renderIngestStatusTag(record.ingest_status) : null}
                    {record.compression_type ? (
                      <Tag color="purple">
                        {compressionRatio
                          ? `${record.compression_type} ${compressionRatio}`
                          : record.compression_type}
                      </Tag>
                    ) : null}
                    <Tag>{record.storage_type || 'local'}</Tag>
                    {typeof record.line_count === 'number' ? (
                      <Tag>{`${record.line_count} 行`}</Tag>
                    ) : null}
                    {typeof shardCount === 'number' ? (
                      <Tag color="geekblue">{`${shardCount} 片`}</Tag>
                    ) : null}
                  </Space>
                )
              },
            },
            {
              title: '上传时间',
              dataIndex: 'created_at',
              key: 'created_at',
              width: 180,
              render: (value: string) => dayjs(value).format('YYYY-MM-DD HH:mm:ss'),
            },
            {
              title: '操作',
              key: 'action',
              width: 140,
              render: (_value, record) => (
                <Space size={4}>
                  <Button
                    type="link"
                    size="small"
                    icon={<DownloadOutlined />}
                    onClick={() => handleDownload(record)}
                  >
                    下载
                  </Button>
                  <Button
                    type="link"
                    danger
                    size="small"
                    icon={<DeleteOutlined />}
                    loading={deleteMutation.isPending}
                    onClick={() => deleteMutation.mutate(record)}
                  >
                    删除
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      </Space>
    </div>
  )
}

export default TaskAssetPanel
