import { Card } from 'antd'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import { ReactNode } from 'react'

interface ChartPanelProps {
  title?: string
  option: EChartsOption
  height?: number | string
  loading?: boolean
  extra?: ReactNode
  'data-testid'?: string
  onChartReady?: (chart: unknown) => void
}

const ChartPanel: React.FC<ChartPanelProps> = ({
  title,
  option,
  height = 400,
  loading = false,
  extra,
  'data-testid': dataTestId,
  onChartReady,
}) => {
  return (
    <div data-testid={dataTestId}>
      <Card title={title} extra={extra} loading={loading}>
        <ReactECharts
          option={option}
          style={{ height: typeof height === 'number' ? `${height}px` : height }}
          onChartReady={onChartReady}
          opts={{ renderer: 'canvas' }}
        />
      </Card>
    </div>
  )
}

export default ChartPanel
