import { useQuery } from '@tanstack/react-query'
import { ArrowRightOutlined, BranchesOutlined, CheckCircleOutlined, ClockCircleOutlined, DatabaseOutlined, LockOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Col, Row, Space, Spin, Statistic, Steps, Tag, Typography } from 'antd'
import { useNavigate } from 'react-router-dom'
import { getWorldModelSummary } from '../api/client'
import { PageHeader, ReadOnlyTag } from '../components/PageHeader'

function coreTone(status: string) {
  if (status === 'active') return 'green'
  if (status === 'blocked') return 'purple'
  return ''
}

function stageStatus(status: string): 'finish' | 'process' | 'wait' {
  if (status === 'completed') return 'finish'
  if (status === 'in_progress') return 'process'
  return 'wait'
}

export function DashboardPage() {
  const navigate = useNavigate()
  const summary = useQuery({ queryKey: ['world-model-summary'], queryFn: getWorldModelSummary })
  const data = summary.data

  if (summary.isError) return <Alert type="error" showIcon title="无法读取本体运行层" description="请确认 FastAPI 已启动，并检查统一语义覆盖层；页面不会使用 mock 数据代替真实结果。" />
  if (!data) return <div className="page-loading"><Spin size="large" /><Typography.Text type="secondary">正在读取企业运维世界模型…</Typography.Text></div>

  return <div>
    <PageHeader
      title="企业运维世界模型"
      description="围绕统一业务对象组织关系、事件、状态、事实、规则、决策和行动；AI 推测与确定性事实严格分层。"
      extra={<Space><Tag color="blue">Ontology Runtime v0.2</Tag><ReadOnlyTag /></Space>}
    />

    <Card className="next-action-card" variant="borderless">
      <div className="world-next-action">
        <div>
          <Typography.Title level={4}>{data.nextAction.title}</Typography.Title>
          <Typography.Text type="secondary">{data.nextAction.description}</Typography.Text>
        </div>
        <Button type="primary" size="large" onClick={() => navigate(data.nextAction.path)}>进入状态中心 <ArrowRightOutlined /></Button>
      </div>
    </Card>

    <div className="world-flow" aria-label="本体运行闭环">
      {['对象', '事件', '事实', '规则', '决策', '行动'].map((label, index) => <div key={label} className="world-flow-node"><span>{label}</span>{index < 5 && <ArrowRightOutlined />}</div>)}
    </div>

    <Row gutter={[12, 12]} className="metrics-grid">
      {data.core.map((item) => <Col xs={12} sm={8} lg={6} xl={4} key={item.key}>
        <Card className={`metric-card world-core-card ${coreTone(item.status)}`} variant="borderless" hoverable onClick={() => navigate(item.path)}>
          <Statistic title={<Space><BranchesOutlined />{item.label}</Space>} value={item.count} />
          <Typography.Paragraph type="secondary" ellipsis={{ rows: 2 }}>{item.description}</Typography.Paragraph>
          <Tag color={item.status === 'active' ? 'success' : item.status === 'blocked' ? 'warning' : 'default'}>{item.status === 'active' ? '已运行' : item.status === 'blocked' ? '待确认' : item.status === 'governed' ? '受治理' : '待建设'}</Tag>
        </Card>
      </Col>)}
    </Row>

    <Row gutter={[16, 16]} className="content-row">
      <Col xs={24} xl={15}>
        <Card title="建设阶段" extra={<Typography.Text type="secondary">按真实依赖推进</Typography.Text>}>
          <Steps direction="vertical" size="small" items={data.stages.map((stage) => ({
            title: `${stage.key} · ${stage.label}`,
            description: stage.detail,
            status: stageStatus(stage.status),
            icon: stage.status === 'completed' ? <CheckCircleOutlined /> : stage.status === 'in_progress' ? <ClockCircleOutlined /> : undefined,
          }))} />
        </Card>
      </Col>
      <Col xs={24} xl={9}>
        <Space orientation="vertical" size={16} style={{ width: '100%' }}>
          <Card title="治理边界" variant="borderless">
            <Space orientation="vertical" size={12} style={{ width: '100%' }}>
              <Alert type="success" showIcon icon={<LockOutlined />} title="源系统保持只读" description="所有映射、事实、判断和审批只写本地语义覆盖层。" />
              <Alert type="info" showIcon icon={<SafetyCertificateOutlined />} title="行动不能直接执行" description="Decision 只能生成 ActionPlan；进入源系统前必须经过独立审批。" />
              <Alert type="warning" showIcon title="Unknown 是合法结果" description="缺少身份或状态证据时保留未知，不强制合并、不自动猜测。" />
            </Space>
          </Card>
          <Card title={<Space><DatabaseOutlined />已连接语义来源</Space>} variant="borderless">
            <Space wrap>{data.sourceSystems.map((source) => <Tag key={source}>{source}</Tag>)}</Space>
          </Card>
        </Space>
      </Col>
    </Row>
  </div>
}
