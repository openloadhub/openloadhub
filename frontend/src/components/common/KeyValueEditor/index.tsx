import { Button, Input, Space } from 'antd'
import { PlusOutlined, DeleteOutlined } from '@ant-design/icons'
import { useState, useEffect } from 'react'

export interface KeyValuePair {
  key: string
  value: string
}

interface KeyValueEditorProps {
  value?: KeyValuePair[]
  onChange?: (pairs: KeyValuePair[]) => void
  disabled?: boolean
  keyPlaceholder?: string
  valuePlaceholder?: string
}

const KeyValueEditor: React.FC<KeyValueEditorProps> = ({
  value = [],
  onChange,
  disabled = false,
  keyPlaceholder = '键',
  valuePlaceholder = '值',
}) => {
  const [pairs, setPairs] = useState<KeyValuePair[]>(value || [])

  // 同步外部 value 变化
  useEffect(() => {
    if (value !== undefined) {
      setPairs(value)
    }
  }, [value])

  const handleAdd = () => {
    const newPairs = [...pairs, { key: '', value: '' }]
    setPairs(newPairs)
    onChange?.(newPairs)
  }

  const handleRemove = (index: number) => {
    const newPairs = pairs.filter((_, i) => i !== index)
    setPairs(newPairs)
    onChange?.(newPairs)
  }

  const handleChange = (index: number, field: 'key' | 'value', val: string) => {
    const newPairs = [...pairs]
    newPairs[index] = { ...newPairs[index], [field]: val }
    setPairs(newPairs)
    onChange?.(newPairs)
  }

  return (
    <div>
      <Space direction="vertical" style={{ width: '100%' }} size="small">
        {pairs.map((pair, index) => (
          <Space key={index} style={{ width: '100%' }}>
            <Input
              placeholder={keyPlaceholder}
              value={pair.key}
              onChange={e => handleChange(index, 'key', e.target.value)}
              disabled={disabled}
              style={{ flex: 1 }}
            />
            <Input
              placeholder={valuePlaceholder}
              value={pair.value}
              onChange={e => handleChange(index, 'value', e.target.value)}
              disabled={disabled}
              style={{ flex: 1 }}
            />
            <Button
              type="text"
              danger
              icon={<DeleteOutlined />}
              onClick={() => handleRemove(index)}
              disabled={disabled}
            />
          </Space>
        ))}
        <Button
          type="dashed"
          onClick={handleAdd}
          disabled={disabled}
          block
          icon={<PlusOutlined />}
        >
          添加键值对
        </Button>
      </Space>
    </div>
  )
}

export default KeyValueEditor
