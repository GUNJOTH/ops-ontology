import { useQuery } from '@tanstack/react-query'
import { ArrowRightOutlined, CheckCircleOutlined, DatabaseOutlined, FileSearchOutlined, LockOutlined, WarningOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Col, Flex, Progress, Row, Space, Statistic, Table, Tag, Typography } from 'antd'
import { getDashboard } from '../api/client'
import { PageHeader, ReadOnlyTag } from '../components/PageHeader'
import { useNavigate } from 'react-router-dom'

export function DashboardPage() {
  const navigate = useNavigate()
  const dashboard = useQuery({ queryKey: ['dashboard'], queryFn: getDashboard })
  const data = dashboard.data

  if (dashboard.isError) return <Alert type="error" showIcon message="无法读取语义治理数据" description="请确认 FastAPI 已启动，并检查后端日志；当前未使用 mock 数据替代真实结果。" />
  if (!data) return <div className="page-loading"><Progress type="circle" percent={65} /><Typography.Text type="secondary">正在加载批次概览…</Typography.Text></div>

  const metrics = [
    { label: '处理总量', value: data.inputCount, note: '当前高质量批次', icon: <DatabaseOutlined /> },
    { label: '候选生成', value: data.candidateCount, note: '高质量批次已冻结', icon: <FileSearchOutlined /> },
    { label: '待审核', value: data.pendingReviewCount, note: '优先处理分层样本', icon: <WarningOutlined />, tone: 'purple' },
    { label: '已通过', value: data.approvedCount, note: '人工确认后进入', icon: <CheckCircleOutlined /> },
    { label: '正式发布', value: data.publishedCount, note: '审批后独立发布', icon: <ArrowRightOutlined /> },
    { label: '源库写入', value: data.readOnlySource ? '否' : '是', note: 'MaxiEAM 保持只读', icon: <LockOutlined />, tone: 'green' },
  ]

  return <div>
    <PageHeader title="语义治理总览" description="设备描述统一语义处理进度与质量状态" extra={<><Tag color="blue">规则 {data.ruleVersion}</Tag><Tag>{data.sourceSnapshot}</Tag><ReadOnlyTag /></>} />

    <Card className="next-action-card" bordered={false}>
      <Flex justify="space-between" align="center" gap={16} wrap>
        <div><Typography.Title level={4}>下一步：审核分层样本</Typography.Title><Typography.Text type="secondary">{data.samplesReady} 条样本已准备，先确认“原描述 → 统一描述”的标准格式。</Typography.Text></div>
        <Space><Button type="primary" size="large" onClick={() => navigate('/candidates?quick_filter=pending&sample_only=true')}>继续审核 {data.samplesReady} 条 <ArrowRightOutlined /></Button><Button onClick={() => navigate('/candidates')}>查看候选</Button></Space>
      </Flex>
    </Card>

    <Row gutter={[12, 12]} className="metrics-grid">
      {metrics.map((metric) => <Col xs={12} sm={8} lg={4} xl={4} key={metric.label}><Card className={`metric-card ${metric.tone ?? ''}`} bordered={false}><Statistic title={<Space>{metric.icon}{metric.label}</Space>} value={metric.value} /><Typography.Text type="secondary">{metric.note}</Typography.Text></Card></Col>)}
    </Row>

    <Row gutter={[16, 16]} className="content-row">
      <Col xs={24} xl={14}><Card title="处理质量漏斗" extra={<Tag color="purple">当前：候选层</Tag>}><div className="funnel-list">
        {[['高质量输入', data.inputCount, 100], ['候选生成', data.candidateCount, 100], ['人工审核', data.pendingReviewCount, data.inputCount ? (data.pendingReviewCount / data.inputCount) * 100 : 0], ['批准发布', data.publishedCount, data.inputCount ? (data.publishedCount / data.inputCount) * 100 : 0]].map(([label, value, percent]) => <div className="funnel-item" key={String(label)}><Typography.Text type="secondary">{label}</Typography.Text><Progress percent={Number(percent)} showInfo={false} strokeColor={label === '批准发布' ? '#15803d' : '#2563eb'} /><Typography.Text strong>{Number(value).toLocaleString()}</Typography.Text></div>)}
      </div></Card></Col>
      <Col xs={24} xl={10}><Card title="需要关注" extra={<Typography.Text type="secondary">下一步建议</Typography.Text>}><Space direction="vertical" size={10} style={{ width: '100%' }}><Alert type="warning" showIcon message="当前没有已确认术语规则，候选描述暂时保留原描述。" /><Alert type="info" showIcon message="KKS 位段目录作为证据展示，不自动改写。" /><Alert type="success" showIcon message={`${data.samplesReady} 条分层样本已准备，可进入审核工作台。`} /></Space></Card></Col>
    </Row>

    <Row gutter={[16, 16]} className="content-row">
      <Col xs={24} xl={12}><Card title="电厂覆盖"><Table rowKey="siteId" size="small" pagination={false} dataSource={data.sites} columns={[{ title: '电厂', dataIndex: 'siteId' }, { title: '设备数', dataIndex: 'count', render: (value: number) => value.toLocaleString() }, { title: '占比', dataIndex: 'share', render: (value: number) => `${value}%` }]} /></Card></Col>
      <Col xs={24} xl={12}><Card title="上下文覆盖"><Space direction="vertical" size={14} style={{ width: '100%' }}>{data.contextCoverage.map((item) => <div className="coverage-item" key={item.label}><Flex justify="space-between"><Typography.Text>{item.label}</Typography.Text><Typography.Text strong>{item.value}%</Typography.Text></Flex><Progress percent={item.value} showInfo={false} /></div>)}</Space></Card></Col>
    </Row>
  </div>
}
