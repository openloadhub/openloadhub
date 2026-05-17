import { Upload, Button, message } from 'antd'
import { UploadOutlined } from '@ant-design/icons'
import type { UploadFile, UploadProps } from 'antd'
import { useState, useEffect } from 'react'

interface FileUploaderProps {
  accept?: string
  maxSize?: number // MB
  maxCount?: number
  value?: UploadFile[]
  onChange?: (fileList: UploadFile[]) => void
  disabled?: boolean
  action?: string
  beforeUpload?: (file: File) => boolean | Promise<boolean>
}

const FileUploader: React.FC<FileUploaderProps> = ({
  accept,
  maxSize = 10,
  maxCount = 1,
  value,
  onChange,
  disabled = false,
  action,
  beforeUpload,
}) => {
  const [fileList, setFileList] = useState<UploadFile[]>(value || [])

  // 同步外部 value 变化
  useEffect(() => {
    if (value !== undefined) {
      setFileList(value)
    }
  }, [value])

  const handleChange: UploadProps['onChange'] = info => {
    let newFileList = [...info.fileList]

    // 限制文件数量
    if (maxCount && newFileList.length > maxCount) {
      newFileList = newFileList.slice(-maxCount)
      message.warning(`最多只能上传 ${maxCount} 个文件`)
    }

    // 读取响应并显示文件链接
    newFileList = newFileList.map(file => {
      if (file.response) {
        file.url = file.response.url
      }
      return file
    })

    setFileList(newFileList)
    onChange?.(newFileList)
  }

  const handleBeforeUpload: UploadProps['beforeUpload'] = async (file) => {
    // 检查文件大小
    const isLtMaxSize = file.size / 1024 / 1024 < maxSize
    if (!isLtMaxSize) {
      message.error(`文件大小不能超过 ${maxSize}MB`)
      return false
    }

    // 自定义验证
    if (beforeUpload) {
      return await beforeUpload(file as File)
    }

    return true
  }

  const handleRemove = (file: UploadFile) => {
    const newFileList = fileList.filter(item => item.uid !== file.uid)
    setFileList(newFileList)
    onChange?.(newFileList)
  }

  return (
    <Upload
      fileList={fileList}
      onChange={handleChange}
      beforeUpload={handleBeforeUpload}
      onRemove={handleRemove}
      accept={accept}
      disabled={disabled}
      action={action}
      maxCount={maxCount}
    >
      {fileList.length < maxCount && (
        <Button icon={<UploadOutlined />} disabled={disabled}>
          选择文件
        </Button>
      )}
    </Upload>
  )
}

export default FileUploader
