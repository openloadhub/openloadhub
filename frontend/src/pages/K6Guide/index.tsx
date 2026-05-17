import { Alert, Button, Card, Col, Row, Space, Typography } from 'antd'
import { useNavigate } from 'react-router-dom'
import type { CSSProperties } from 'react'
import './index.css'

const { Paragraph, Text, Title } = Typography

const codeBlockStyle: CSSProperties = {
  borderRadius: 12,
  padding: '16px 18px',
  fontSize: 13,
  lineHeight: 1.7,
  overflowX: 'auto',
  fontFamily:
    'SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace',
}

const sectionCardStyle: CSSProperties = {
  borderRadius: 8,
}

const K6Guide = () => {
  const navigate = useNavigate()

  return (
    <div className="olh-k6-guide-page">
      <div className="olh-k6-guide-inner">
        <Card
          className="olh-k6-guide-hero"
          style={{
            ...sectionCardStyle,
            marginBottom: 20,
          }}
          bodyStyle={{ padding: 24 }}
        >
          <Space direction="vertical" size={16} style={{ display: 'flex' }}>
            <Space wrap size={12} style={{ justifyContent: 'space-between' }}>
              <div>
                <Text className="olh-k6-guide-eyebrow">K6 使用指南</Text>
                <Title level={2} style={{ margin: '8px 0 0' }}>
                  总 TPS、标准脚本与自定义脚本说明
                </Title>
              </div>
              <Button onClick={() => navigate('/my-focus/focus-task')}>返回关注任务</Button>
            </Space>
            <Alert
              type="info"
              showIcon
              message="先记住两句话"
              description={
                <Space direction="vertical" size={4} style={{ display: 'flex' }}>
                  <span>1. 平台语义里，`target_tps` 一律表示总 TPS。</span>
                  <span>
                    2. 只有“标准脚本模板”保证多节点按 `pod_count`
                    自动拆分；自定义脚本如果不用这些变量，就按脚本默认逻辑运行。
                  </span>
                </Space>
              }
            />
          </Space>
        </Card>

        <Row gutter={[20, 20]}>
          <Col xs={24} lg={16}>
            <Space direction="vertical" size={20} style={{ display: 'flex' }}>
              <Card className="olh-k6-guide-panel" title="快速结论" style={sectionCardStyle} bodyStyle={{ padding: 24 }}>
                <Paragraph style={{ marginBottom: 12 }}>
                  普通任务执行和批次执行，对外都按 <Text code>target_tps = 总 TPS</Text> 理解。
                </Paragraph>
                <Paragraph style={{ marginBottom: 12 }}>
                  批次页多一个“倍率”输入，只是把倍率先换算成每个 Run 的总{' '}
                  <Text code>target_tps</Text>，最终仍然走同一条 Run 级控制链路。
                </Paragraph>
                <Paragraph style={{ marginBottom: 0 }}>
                  如果上传的是平台标准脚本模板，多节点会按 <Text code>pod_count</Text>{' '}
                  自动拆分；如果上传的是任意自定义脚本，且脚本没有消费这些变量，平台不会强行改写你的业务逻辑。
                </Paragraph>
                <Paragraph style={{ marginTop: 12, marginBottom: 0 }}>
                  当前从 <Text code>cURL</Text> 或 HAR 生成的单接口 HTTP K6
                  脚本，属于平台标准执行模板；执行并发、时长和次数由运行策略统一控制，启动前可设置总 TPS。
                </Paragraph>
                <Paragraph style={{ marginTop: 12, marginBottom: 0 }}>
                  <Text code>OpenAPI -&gt; k6</Text> 当前已开放为最小闭环：支持粘贴 OpenAPI 3.x
                  JSON/YAML、选择一个 endpoint、生成单接口 k6
                  草稿。它仍不代表已支持多接口自动编排或复杂鉴权自动接线。
                </Paragraph>
              </Card>

              <Card className="olh-k6-guide-panel" title="推荐上传方式" style={sectionCardStyle} bodyStyle={{ padding: 24 }}>
                <Paragraph style={{ marginBottom: 12 }}>
                  推荐直接从页面里的 <Text strong>k6 标准脚本模板</Text> 开始，当前模板包括：
                </Paragraph>
                <Space wrap size={[8, 8]} style={{ marginBottom: 16 }}>
                  {['HTTP 标准模板', 'GRPC 标准模板', '混合标准模板'].map(item => (
                    <span
                      key={item}
                      className="olh-k6-guide-template-chip"
                    >
                      {item}
                    </span>
                  ))}
                </Space>
                <Paragraph style={{ marginBottom: 12 }}>这些模板已经内置了：</Paragraph>
                <Paragraph style={{ marginBottom: 0 }}>
                  <Text code>target_tps</Text> 读取、<Text code>pod_count</Text> 拆分、
                  <Text code>constant-arrival-rate</Text> 场景，以及后续 v0.2
                  动态控制可复用的命名约定。
                </Paragraph>
              </Card>

              <Card className="olh-k6-guide-panel" title="标准脚本最小合同" style={sectionCardStyle} bodyStyle={{ padding: 24 }}>
                <Paragraph>标准脚本建议至少读取这些运行时变量：</Paragraph>
                <pre
                  style={codeBlockStyle}
                >{`const totalTargetTps = Number(__ENV.target_tps || __ENV.TARGET_TPS || "0");
const podCount = Math.max(1, Number(__ENV.pod_count || __ENV.POD_COUNT || "1"));
const localTargetTps = totalTargetTps > 0 ? totalTargetTps / podCount : 0;
const durationSeconds = Math.max(1, Number(__ENV.PTP_DURATION_SECONDS || __ENV.duration || "300"));
const threadCount = Math.max(1, Number(__ENV.PTP_THREAD_COUNT || __ENV.threads || __ENV.vus || "1"));`}</pre>
                <Paragraph style={{ marginTop: 16, marginBottom: 0 }}>
                  你可以自己扩展业务请求和数据逻辑，但只要继续保留这套变量读取方式，多节点和动态控制就能保持总
                  TPS 语义。
                </Paragraph>
              </Card>

              <Card className="olh-k6-guide-panel" title="自定义脚本如何处理" style={sectionCardStyle} bodyStyle={{ padding: 24 }}>
                <Paragraph style={{ marginBottom: 12 }}>
                  如果自定义脚本没有使用 <Text code>target_tps</Text>、<Text code>pod_count</Text>{' '}
                  这些变量，平台就按脚本默认逻辑运行。
                </Paragraph>
                <Paragraph style={{ marginBottom: 12 }}>这意味着：</Paragraph>
                <Paragraph style={{ marginBottom: 12 }}>- 脚本自己怎么写，就怎么执行</Paragraph>
                <Paragraph style={{ marginBottom: 12 }}>
                  - 平台不会替你自动把业务脚本改成“总 TPS 拆分”
                </Paragraph>
                <Paragraph style={{ marginBottom: 0 }}>
                  - 如果脚本自定义了复杂 <Text code>scenarios</Text>，当前也不保证能接入标准的{' '}
                  <Text code>scenario_direct</Text> 热更新链路
                </Paragraph>
              </Card>
            </Space>
          </Col>

          <Col xs={24} lg={8}>
            <Space
              direction="vertical"
              size={20}
              style={{ display: 'flex', position: 'sticky', top: 24 }}
            >
              <Card className="olh-k6-guide-panel" title="变量说明" style={sectionCardStyle} bodyStyle={{ padding: 20 }}>
                <Space direction="vertical" size={10} style={{ display: 'flex' }}>
                  <div>
                    <Text code>target_tps</Text>：总 TPS
                  </div>
                  <div>
                    <Text code>pod_count</Text>：本次运行的 agent 数
                  </div>
                  <div>
                    <Text code>PTP_DURATION_SECONDS</Text>：本次运行时长
                  </div>
                  <div>
                    <Text code>PTP_THREAD_COUNT</Text>：本地预分配并发参考值
                  </div>
                  <div>
                    <Text code>PTP_DATA_DIR</Text>：运行时数据文件目录
                  </div>
                  <div>
                    <Text code>PTP_PROTO_DIR</Text>：运行时 proto 目录
                  </div>
                </Space>
              </Card>

              <Card className="olh-k6-guide-panel" title="用户怎么选" style={sectionCardStyle} bodyStyle={{ padding: 20 }}>
                <Space direction="vertical" size={10} style={{ display: 'flex' }}>
                  <div>想省心：直接复制标准模板上传。</div>
                  <div>
                    已有自定义脚本：至少补上 <Text code>target_tps</Text> 和{' '}
                    <Text code>pod_count</Text> 读取。
                  </div>
                  <div>
                    完全不想改脚本：就按脚本默认逻辑执行，不要把平台输入的 TPS 当成一定生效。
                  </div>
                </Space>
              </Card>

              <Card className="olh-k6-guide-panel" title="入口说明" style={sectionCardStyle} bodyStyle={{ padding: 20 }}>
                <Paragraph style={{ marginBottom: 12 }}>
                  关注任务页右上角的 <Text strong>使用指南</Text> 是规则说明入口；
                  <Text strong> k6 标准脚本模板</Text> 是可直接参考或复制的上传模板入口；
                  <Text strong> 从 CURL 生成 K6 / 从 OpenAPI 生成 K6</Text> 是当前导入向导的最小草稿入口。
                </Paragraph>
                <Button type="primary" onClick={() => navigate('/my-focus/focus-task')}>
                  去关注任务页查看模板
                </Button>
              </Card>
            </Space>
          </Col>
        </Row>
      </div>
    </div>
  )
}

export default K6Guide
