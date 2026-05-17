import { Card, Statistic } from 'antd'
import { ReactNode } from 'react'

interface MetricCardProps {
  title: string
  value: string | number
  prefix?: ReactNode
  suffix?: ReactNode
  valueStyle?: React.CSSProperties
  trend?: {
    value: number
    isUp: boolean
  }
  loading?: boolean
}

const MetricCard: React.FC<MetricCardProps> = ({
  title,
  value,
  prefix,
  suffix,
  valueStyle,
  trend,
  loading = false,
}) => {
  return (
    <Card loading={loading}>
      <Statistic
        title={title}
        value={value}
        prefix={prefix}
        suffix={suffix}
        valueStyle={valueStyle}
      />
      {trend && (
        <div
          style={{
            marginTop: 8,
            fontSize: 12,
            color: trend.isUp ? '#3f8600' : '#cf1322',
          }}
        >
          {trend.isUp ? '↑' : '↓'} {Math.abs(trend.value)}%
        </div>
      )}
    </Card>
  )
}

export default MetricCard

