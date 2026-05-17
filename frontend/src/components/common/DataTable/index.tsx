import { Table, TableProps, Button, Space, Popconfirm } from 'antd'
import { EditOutlined, DeleteOutlined, EyeOutlined } from '@ant-design/icons'
import { ReactNode } from 'react'
import type { Key } from 'react'

type UnknownRecord = object

type ColumnRender<T> = {
  bivarianceHack(value: unknown, record: T, index: number): ReactNode
}['bivarianceHack']

export interface ActionConfig<T = UnknownRecord> {
  type: 'view' | 'edit' | 'delete' | 'custom'
  label?: string | ((record: T) => string)
  icon?: ReactNode | ((record: T) => ReactNode)
  disabled?: boolean | ((record: T) => boolean)
  hidden?: boolean | ((record: T) => boolean)
  onClick: (record: T) => void
  confirm?: {
    title: string | ((record: T) => string)
    description?: string | ((record: T) => string)
  }
}

interface DataTableProps<T = UnknownRecord> extends Omit<TableProps<T>, 'columns'> {
  columns: Array<{
    title: string
    dataIndex: string | number | Array<string | number>
    key?: string
    width?: number
    ellipsis?: boolean
    render?: ColumnRender<T>
    sorter?: boolean | ((a: T, b: T) => number)
    filters?: Array<{ text: string; value: string | number | boolean }>
    filteredValue?: Key[] | null
    filterMultiple?: boolean
  }>
  actions?: ActionConfig<T>[]
  actionColumnWidth?: number
  actionColumnFixed?: 'left' | 'right' | false
  loading?: boolean
}

const DataTable = <T extends UnknownRecord = UnknownRecord>({
  columns,
  actions,
  actionColumnWidth,
  actionColumnFixed = 'right',
  loading = false,
  ...tableProps
}: DataTableProps<T>) => {
  const actionColumn = actions
    ? {
        title: '操作',
        key: 'action',
        width: actionColumnWidth ?? 150,
        ...(actionColumnFixed ? { fixed: actionColumnFixed } : {}),
        render: (_: unknown, record: T) => (
          <Space size="small">
            {actions.map((action, index) => {
              const isHidden = typeof action.hidden === 'function' ? action.hidden(record) : action.hidden
              if (isHidden) return null
              const isDisabled = typeof action.disabled === 'function' ? action.disabled(record) : action.disabled

              const actionLabel =
                typeof action.label === 'function'
                  ? action.label(record)
                  : action.label

              if (action.type === 'view') {
                return (
                  <Button
                    key={index}
                    type="link"
                    size="small"
                    disabled={isDisabled}
                    icon={<EyeOutlined />}
                    onClick={() => action.onClick(record)}
                  >
                    {actionLabel ?? '查看'}
                  </Button>
                )
              }
              if (action.type === 'edit') {
                return (
                  <Button
                    key={index}
                    type="link"
                    size="small"
                    disabled={isDisabled}
                    icon={<EditOutlined />}
                    onClick={() => action.onClick(record)}
                  >
                    {actionLabel ?? '编辑'}
                  </Button>
                )
              }
              if (action.type === 'delete') {
                const confirmTitle =
                  typeof action.confirm?.title === 'function'
                    ? action.confirm.title(record)
                    : action.confirm?.title || '确定要删除吗？'
                const confirmDescription =
                  typeof action.confirm?.description === 'function'
                    ? action.confirm.description(record)
                    : action.confirm?.description
                return (
                  <Popconfirm
                    key={index}
                    title={confirmTitle}
                    description={confirmDescription}
                    onConfirm={() => action.onClick(record)}
                    okText="确定"
                    cancelText="取消"
                  >
                    <Button
                      type="link"
                      danger
                      size="small"
                      disabled={isDisabled}
                      icon={<DeleteOutlined />}
                    >
                      {actionLabel ?? '删除'}
                    </Button>
                  </Popconfirm>
                )
              }
              // custom
              const label =
                typeof action.label === 'function'
                  ? action.label(record)
                  : action.label
              const icon =
                typeof action.icon === 'function'
                  ? action.icon(record)
                  : action.icon
              return (
                <Button
                  key={index}
                  type="link"
                  size="small"
                  disabled={isDisabled}
                  icon={icon}
                  onClick={() => action.onClick(record)}
                >
                  {label}
                </Button>
              )
            })}
          </Space>
        ),
      }
    : null

  const finalColumns = actionColumn
    ? [...columns, actionColumn]
    : columns.map(col => {
        const dataIndexKey = Array.isArray(col.dataIndex)
          ? col.dataIndex.join('.')
          : String(col.dataIndex)
        return {
          ...col,
          key: col.key || dataIndexKey,
        }
      })

  return (
    <Table<T>
      columns={finalColumns as TableProps<T>['columns']}
      loading={loading}
      rowKey={tableProps.rowKey || 'id'}
      pagination={{
        showSizeChanger: true,
        showTotal: (total) => `共 ${total} 条`,
        pageSizeOptions: ['10', '20', '50', '100'],
        ...tableProps.pagination,
      }}
      {...tableProps}
    />
  )
}

export default DataTable
