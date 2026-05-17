import { useState, useEffect } from 'react'
import { Button, message, Modal, Upload } from 'antd'
import { PlusOutlined, UploadOutlined, EyeOutlined } from '@ant-design/icons'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { DataTable, StatusBadge } from '@/components/common'
import type { ActionConfig } from '@/components/common'
import { scriptApi, type Script } from '@/services/scriptApi'
import Editor from '@monaco-editor/react'
import dayjs from 'dayjs'
import './index.css'

const { Dragger } = Upload

const ScriptList = () => {
  const queryClient = useQueryClient()
  const [uploadVisible, setUploadVisible] = useState(false)
  const [detailVisible, setDetailVisible] = useState(false)
  const [selectedScript, setSelectedScript] = useState<Script | null>(null)
  const [scriptContent, setScriptContent] = useState<string>('')

  // 查询脚本列表
  const {
    data: scriptData,
    isLoading,
  } = useQuery({
    queryKey: ['scripts'],
    queryFn: () => scriptApi.getScriptList({ page: 1, pageSize: 100 }),
  })

  // 查询脚本内容
  const { data: scriptContentData, isLoading: detailLoading } = useQuery({
    queryKey: ['script-content', selectedScript?.id],
    queryFn: () => scriptApi.getScriptContent(selectedScript!.id),
    enabled: !!selectedScript && detailVisible,
  })

  // 更新脚本内容
  useEffect(() => {
    if (scriptContentData) {
      setScriptContent(scriptContentData.content)
    } else if (selectedScript && detailVisible) {
      setScriptContent('// 加载中...')
    }
  }, [scriptContentData, selectedScript, detailVisible])

  // 上传脚本
  const uploadMutation = useMutation({
    mutationFn: (file: File) => scriptApi.uploadScript(file),
    onSuccess: () => {
      message.success('脚本上传成功')
      queryClient.invalidateQueries({ queryKey: ['scripts'] })
      setUploadVisible(false)
    },
    onError: (error: unknown) => {
      const errorMessage =
        error instanceof Error ? error.message : '脚本上传失败'
      message.error(errorMessage)
    },
  })

  // 删除脚本
  const deleteMutation = useMutation({
    mutationFn: (id: number) => scriptApi.deleteScript(id),
    onSuccess: () => {
      message.success('脚本删除成功')
      queryClient.invalidateQueries({ queryKey: ['scripts'] })
    },
    onError: (error: unknown) => {
      const errorMessage =
        error instanceof Error ? error.message : '脚本删除失败'
      message.error(errorMessage)
    },
  })

  // 处理文件上传
  const handleUpload = (file: File) => {
    const ext = file.name.split('.').pop()?.toLowerCase()
    if (ext !== 'jmx' && ext !== 'js') {
      message.error('只支持上传 .jmx 或 .js 文件')
      return false
    }
    uploadMutation.mutate(file)
    return false // 阻止默认上传行为
  }

  // 查看脚本详情
  const handleViewDetail = (script: Script) => {
    setSelectedScript(script)
    setDetailVisible(true)
    setScriptContent('') // 重置内容，等待加载
  }

  // 表格列配置
  const columns = [
    {
      title: '脚本名称',
      dataIndex: 'name',
      key: 'name',
      width: 200,
    },
    {
      title: '类型',
      dataIndex: 'script_type',
      key: 'script_type',
      width: 100,
      render: (type: Script['script_type']) => (
        <StatusBadge
          status={type === 'JMETER' ? 'success' : 'warning'}
          text={type}
        />
      ),
    },
    {
      title: '文件名',
      dataIndex: 'file_path',
      key: 'file_path',
      width: 250,
      render: (filePath: string) => filePath?.split('/').pop() || '-',
    },
    {
      title: '文件大小',
      dataIndex: 'file_size',
      key: 'file_size',
      width: 120,
      render: (size?: number | null) => {
        if (!size) return '-'
        if (size < 1024) return `${size} B`
        if (size < 1024 * 1024) return `${(size / 1024).toFixed(2)} KB`
        return `${(size / (1024 * 1024)).toFixed(2)} MB`
      },
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      sorter: true,
      render: (text: string) =>
        text ? dayjs(text).format('YYYY-MM-DD HH:mm:ss') : '-',
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      width: 180,
      sorter: true,
      render: (text: string) =>
        text ? dayjs(text).format('YYYY-MM-DD HH:mm:ss') : '-',
    },
  ]

  // 操作列配置
  const actions: ActionConfig<Script>[] = [
    {
      type: 'custom',
      label: '详情',
      icon: <EyeOutlined />,
      onClick: (record: Script) => {
        handleViewDetail(record)
      },
    },
    {
      type: 'delete',
      onClick: (record: Script) => {
        deleteMutation.mutate(record.id)
      },
      confirm: {
        title: '确定要删除这个脚本吗？',
        description: (record: Script) => `脚本名称：${record.name}`,
      },
    },
  ]

  return (
    <div className="olh-script-list-page">
      <div className="olh-script-list-hero">
        <div>
          <div className="olh-script-list-eyebrow">Script Console</div>
          <h1>脚本管理</h1>
          <p>维护 JMeter 与 K6 脚本文件，快速查看内容与版本来源。</p>
        </div>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => setUploadVisible(true)}
        >
          上传脚本
        </Button>
      </div>

      {/* 脚本列表 */}
      <div className="olh-script-table-shell">
        <DataTable<Script>
          className="olh-script-table"
          columns={columns}
          dataSource={scriptData?.items || []}
          loading={isLoading}
          actions={actions}
          actionColumnWidth={74}
          actionColumnFixed={false}
          pagination={{
            current: scriptData?.page || 1,
            pageSize: scriptData?.pageSize || 10,
            total: scriptData?.total || 0,
            showSizeChanger: true,
            showTotal: total => `共 ${total} 条`,
          }}
        />
      </div>

      {/* 上传脚本弹窗 */}
      <Modal
        title="上传脚本"
        open={uploadVisible}
        onCancel={() => setUploadVisible(false)}
        footer={null}
        width={600}
        rootClassName="olh-script-upload-modal"
      >
        <Dragger
          accept=".jmx,.js"
          beforeUpload={handleUpload}
          showUploadList={false}
          disabled={uploadMutation.isPending}
        >
          <p className="ant-upload-drag-icon">
            <UploadOutlined />
          </p>
          <p className="ant-upload-text">点击或拖拽文件到此区域上传</p>
          <p className="ant-upload-hint">
            支持上传 JMeter (.jmx) 或 K6 (.js) 脚本文件
          </p>
        </Dragger>
        {uploadMutation.isPending && (
          <div style={{ textAlign: 'center', marginTop: 16 }}>
            上传中...
          </div>
        )}
      </Modal>

      {/* 脚本详情弹窗 */}
      <Modal
        title={`脚本详情 - ${selectedScript?.name}`}
        open={detailVisible}
        onCancel={() => {
          setDetailVisible(false)
          setSelectedScript(null)
          setScriptContent('')
        }}
        footer={null}
        width={900}
        style={{ top: 20 }}
        rootClassName="olh-script-detail-modal"
      >
        {detailLoading ? (
          <div style={{ textAlign: 'center', padding: 40 }}>
            加载中...
          </div>
        ) : (
          <Editor
            height="600px"
            language={selectedScript?.script_type === 'JMETER' ? 'xml' : 'javascript'}
            value={scriptContent}
            theme="vs-dark"
            options={{
              readOnly: true,
              minimap: { enabled: false },
              scrollBeyondLastLine: false,
            }}
          />
        )}
      </Modal>
    </div>
  )
}

export default ScriptList
