# 通用组件库

Week 2 开发的通用组件库，包含 8 个可复用组件。

## 组件列表

### 1. KeyValueEditor - 键值对编辑器

用于编辑键值对数据，常用于配置变量、请求头等场景。

**使用示例**：
```tsx
import { KeyValueEditor } from '@/components/common'

const [variables, setVariables] = useState<KeyValuePair[]>([])

<KeyValueEditor
  value={variables}
  onChange={setVariables}
  keyPlaceholder="变量名"
  valuePlaceholder="变量值"
/>
```

**Props**：
- `value?: KeyValuePair[]` - 键值对数组
- `onChange?: (pairs: KeyValuePair[]) => void` - 变化回调
- `disabled?: boolean` - 是否禁用
- `keyPlaceholder?: string` - 键输入框占位符
- `valuePlaceholder?: string` - 值输入框占位符

---

### 2. FileUploader - 文件上传组件

基于 Ant Design Upload 封装的文件上传组件，支持文件大小、数量限制。

**使用示例**：
```tsx
import { FileUploader } from '@/components/common'

<FileUploader
  accept=".jmx,.js"
  maxSize={50}
  maxCount={1}
  action="/api/v1/scripts/upload"
  onChange={(fileList) => console.log(fileList)}
/>
```

**Props**：
- `accept?: string` - 接受的文件类型
- `maxSize?: number` - 最大文件大小（MB），默认 10
- `maxCount?: number` - 最大文件数量，默认 1
- `value?: UploadFile[]` - 文件列表
- `onChange?: (fileList: UploadFile[]) => void` - 变化回调
- `disabled?: boolean` - 是否禁用
- `action?: string` - 上传地址
- `beforeUpload?: (file: File) => boolean` - 上传前验证

---

### 3. MetricCard - 指标卡片

用于展示统计指标，支持前缀、后缀、趋势等。

**使用示例**：
```tsx
import { MetricCard } from '@/components/common'
import { PlayCircleOutlined } from '@ant-design/icons'

<MetricCard
  title="运行中的任务"
  value={10}
  prefix={<PlayCircleOutlined />}
  valueStyle={{ color: '#3f8600' }}
  trend={{ value: 5, isUp: true }}
/>
```

**Props**：
- `title: string` - 标题
- `value: string | number` - 数值
- `prefix?: ReactNode` - 前缀图标
- `suffix?: ReactNode` - 后缀文本
- `valueStyle?: React.CSSProperties` - 数值样式
- `trend?: { value: number; isUp: boolean }` - 趋势数据
- `loading?: boolean` - 加载状态

---

### 4. StatusBadge - 状态徽章

统一的状态显示组件，支持多种状态类型。

**使用示例**：
```tsx
import { StatusBadge } from '@/components/common'

<StatusBadge status="running" />
<StatusBadge status="failed" text="执行失败" showDot />
```

**Props**：
- `status: StatusType` - 状态类型
- `text?: string` - 自定义文本
- `showDot?: boolean` - 是否显示为 Badge 点

**状态类型**：
- `pending` - 待处理
- `running` - 运行中
- `completed` - 已完成
- `failed` - 失败
- `stopped` - 已停止
- `success` - 成功
- `warning` - 警告
- `error` - 错误

---

### 5. SearchFilter - 搜索筛选组件

通用的搜索筛选表单，支持多种字段类型。

**使用示例**：
```tsx
import { SearchFilter } from '@/components/common'

const fields = [
  { name: 'name', label: '任务名称', type: 'input' },
  { name: 'status', label: '状态', type: 'select', options: [
    { label: '运行中', value: 'running' },
    { label: '已完成', value: 'completed' }
  ]},
  { name: 'createTime', label: '创建时间', type: 'dateRange' }
]

<SearchFilter
  fields={fields}
  onSearch={(values) => console.log(values)}
  onReset={() => console.log('reset')}
/>
```

**Props**：
- `fields: FilterField[]` - 筛选字段配置
- `onSearch: (values: Record<string, any>) => void` - 搜索回调
- `onReset?: () => void` - 重置回调
- `initialValues?: Record<string, any>` - 初始值
- `loading?: boolean` - 加载状态

**字段类型**：
- `input` - 文本输入
- `select` - 下拉选择
- `date` - 日期选择
- `dateRange` - 日期范围

---

### 6. DataTable - 可配置表格

基于 Ant Design Table 封装的可配置表格，支持操作列。

**使用示例**：
```tsx
import { DataTable } from '@/components/common'

const columns = [
  { title: '任务名称', dataIndex: 'name', key: 'name' },
  { title: '状态', dataIndex: 'status', key: 'status' },
  { title: '创建时间', dataIndex: 'createdAt', key: 'createdAt', sorter: true }
]

const actions = [
  { type: 'view', onClick: (record) => console.log('view', record) },
  { type: 'edit', onClick: (record) => console.log('edit', record) },
  {
    type: 'delete',
    onClick: (record) => console.log('delete', record),
    confirm: { title: '确定要删除这个任务吗？' }
  }
]

<DataTable
  columns={columns}
  dataSource={data}
  actions={actions}
  loading={loading}
/>
```

**Props**：
- `columns` - 列配置（扩展 Ant Design Table columns）
- `actions?: ActionConfig[]` - 操作列配置
- `loading?: boolean` - 加载状态
- 其他 Ant Design Table 的 props

**操作类型**：
- `view` - 查看
- `edit` - 编辑
- `delete` - 删除（带确认）
- `custom` - 自定义操作

---

### 7. ChartPanel - 图表面板

基于 ECharts 的图表面板组件。

**使用示例**：
```tsx
import { ChartPanel } from '@/components/common'

const option = {
  xAxis: { type: 'category', data: ['Mon', 'Tue', 'Wed'] },
  yAxis: { type: 'value' },
  series: [{ data: [120, 200, 150], type: 'line' }]
}

<ChartPanel
  title="TPS 趋势图"
  option={option}
  height={400}
  loading={loading}
/>
```

**Props**：
- `title?: string` - 图表标题
- `option: EChartsOption` - ECharts 配置
- `height?: number | string` - 图表高度，默认 400
- `loading?: boolean` - 加载状态
- `extra?: ReactNode` - 右上角额外内容
- `onChartReady?: (chart: any) => void` - 图表就绪回调

---

### 8. ConfirmModal - 确认弹窗

统一的确认弹窗组件，支持不同类型的确认操作。

**使用示例**：
```tsx
import { ConfirmModal } from '@/components/common'

const [visible, setVisible] = useState(false)

<ConfirmModal
  visible={visible}
  title="删除任务"
  content="确定要删除这个任务吗？删除后无法恢复。"
  type="danger"
  onConfirm={async () => {
    await deleteTask(id)
    message.success('删除成功')
  }}
  onCancel={() => setVisible(false)}
/>
```

**Props**：
- `title?: string` - 标题，默认"确认操作"
- `content: ReactNode` - 内容
- `onConfirm: () => void | Promise<void>` - 确认回调
- `confirmText?: string` - 确认按钮文本，默认"确定"
- `cancelText?: string` - 取消按钮文本，默认"取消"
- `type?: 'warning' | 'danger' | 'info'` - 类型，默认 'warning'
- 其他 Ant Design Modal 的 props

---

## 统一导入

所有组件可以通过统一入口导入：

```tsx
import {
  KeyValueEditor,
  FileUploader,
  MetricCard,
  StatusBadge,
  SearchFilter,
  DataTable,
  ChartPanel,
  ConfirmModal,
} from '@/components/common'
```

## 类型导出

所有组件的 TypeScript 类型都已导出，可以直接使用：

```tsx
import type {
  KeyValuePair,
  StatusType,
  FilterField,
  ActionConfig,
} from '@/components/common'
```

## 注意事项

1. **KeyValueEditor**：需要配合 Form 使用时，建议使用 `Form.Item` 包裹
2. **FileUploader**：上传地址需要后端支持，或使用自定义 `beforeUpload`
3. **SearchFilter**：日期范围会自动转换为 `xxxStart` 和 `xxxEnd` 两个字段
4. **DataTable**：操作列会自动添加到最右侧，可通过 `actions` 配置
5. **ChartPanel**：需要确保已安装 `echarts` 和 `echarts-for-react`

## 后续优化

- [ ] 添加 Storybook 文档
- [ ] 添加单元测试
- [ ] 优化组件性能（memo、useMemo）
- [ ] 添加更多配置选项

