import { Modal, ModalProps } from 'antd'
import { ExclamationCircleOutlined } from '@ant-design/icons'

interface ConfirmModalProps extends Omit<ModalProps, 'onOk'> {
  title?: string
  content: React.ReactNode
  onConfirm: () => void | Promise<void>
  confirmText?: string
  cancelText?: string
  type?: 'warning' | 'danger' | 'info'
}

const ConfirmModal: React.FC<ConfirmModalProps> = ({
  title = '确认操作',
  content,
  onConfirm,
  confirmText = '确定',
  cancelText = '取消',
  type = 'warning',
  ...modalProps
}) => {
  const handleConfirm = async () => {
    try {
      await onConfirm()
      const onCancel = modalProps.onCancel
      if (onCancel) {
        onCancel({} as Parameters<typeof onCancel>[0])
      }
    } catch (error) {
      // 错误由调用方处理
      console.error('Confirm action failed:', error)
    }
  }

  const iconColor =
    type === 'danger'
      ? '#ff4d4f'
      : type === 'warning'
        ? '#faad14'
        : '#1890ff'

  return (
    <Modal
      title={
        <span>
          <ExclamationCircleOutlined
            style={{ color: iconColor, marginRight: 8 }}
          />
          {title}
        </span>
      }
      onOk={handleConfirm}
      okText={confirmText}
      cancelText={cancelText}
      okButtonProps={{
        danger: type === 'danger',
      }}
      {...modalProps}
    >
      {content}
    </Modal>
  )
}

export default ConfirmModal
