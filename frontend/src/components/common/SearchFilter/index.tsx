import { Form, Input, Button, Space, DatePicker, Select } from 'antd'
import { SearchOutlined, ReloadOutlined } from '@ant-design/icons'
import type { Dayjs } from 'dayjs'

const { RangePicker } = DatePicker

export interface FilterField {
  name: string
  label: string
  type: 'input' | 'select' | 'date' | 'dateRange'
  options?: { label: string; value: string | number }[]
  placeholder?: string
  disabled?: boolean
}

type SearchValues = Record<string, unknown>

interface SearchFilterProps {
  fields: FilterField[]
  onSearch: (values: SearchValues) => void
  onReset?: () => void
  initialValues?: SearchValues
  loading?: boolean
}

const SearchFilter: React.FC<SearchFilterProps> = ({
  fields,
  onSearch,
  onReset,
  initialValues,
  loading = false,
}) => {
  const [form] = Form.useForm()

  const isDayjsLike = (value: unknown): value is Dayjs => {
    return (
      typeof value === 'object' &&
      value !== null &&
      'toDate' in value &&
      typeof (value as Dayjs).toDate === 'function'
    )
  }

  const handleSearch = () => {
    const values = form.getFieldsValue() as SearchValues
    // 处理日期范围
    Object.keys(values).forEach(key => {
      const current = values[key]
      if (Array.isArray(current) && current.length === 2 && isDayjsLike(current[0]) && isDayjsLike(current[1])) {
        const [start, end] = current
        const normalizedEnd =
          end.hour() === 0 && end.minute() === 0 && end.second() === 0 && end.millisecond() === 0
            ? end.endOf('day')
            : end
        // 分页/筛选契约：时间范围统一 from/to（RFC3339 UTC Z）
        if (key === 'createTime') {
          values.from = start?.toDate().toISOString()
          values.to = normalizedEnd?.toDate().toISOString()
        } else {
          values[`${key}From`] = start?.toDate().toISOString()
          values[`${key}To`] = normalizedEnd?.toDate().toISOString()
        }
        delete values[key]
      } else if (isDayjsLike(current)) {
        values[key] = current.toDate().toISOString()
      }
    })
    onSearch(values)
  }

  const handleReset = () => {
    form.resetFields()
    if (onReset) {
      onReset()
      return
    }
    onSearch({})
  }

  const renderField = (field: FilterField) => {
    switch (field.type) {
      case 'input':
        return (
          <Form.Item key={field.name} name={field.name} label={field.label}>
            <Input placeholder={field.placeholder || `请输入${field.label}`} disabled={field.disabled} />
          </Form.Item>
        )
      case 'select':
        return (
          <Form.Item key={field.name} name={field.name} label={field.label}>
            <Select
              placeholder={field.placeholder || `请选择${field.label}`}
              options={field.options}
              allowClear
              disabled={field.disabled}
            />
          </Form.Item>
        )
      case 'date':
        return (
          <Form.Item key={field.name} name={field.name} label={field.label}>
            <DatePicker
              placeholder={field.placeholder || `请选择${field.label}`}
              style={{ width: '100%' }}
              disabled={field.disabled}
            />
          </Form.Item>
        )
      case 'dateRange':
        return (
          <Form.Item key={field.name} name={field.name} label={field.label}>
            <RangePicker style={{ width: '100%' }} disabled={field.disabled} />
          </Form.Item>
        )
      default:
        return null
    }
  }

  return (
    <Form form={form} layout="inline" initialValues={initialValues} onFinish={handleSearch}>
      {fields.map(renderField)}
      <Form.Item>
        <Space>
          <Button
            htmlType="submit"
            type="primary"
            icon={<SearchOutlined />}
            loading={loading}
          >
            搜索
          </Button>
          <Button icon={<ReloadOutlined />} onClick={handleReset}>
            重置
          </Button>
        </Space>
      </Form.Item>
    </Form>
  )
}

export default SearchFilter
