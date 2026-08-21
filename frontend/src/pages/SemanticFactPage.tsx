import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ApartmentOutlined, AuditOutlined, BranchesOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { Alert, Card, Col, Descriptions, Divider, Drawer, Input, Pagination, Row, Select, Space, Statistic, Steps, Table, Tag, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { getSemanticFact, getSemanticFactSummary, getSemanticFacts } from '../api/client'
import type { SemanticFactDetail, SemanticFactItem } from '../api/types'
import { PageHeader, ReadOnlyTag } from '../components/PageHeader'

const pageSize = 50

function display(value: unknown) {
  if (value === null || value === undefined || String(value).trim() === '') return '—'
  return String(value)
}

function number(value: number | undefined) {
  return (value ?? 0).toLocaleString()
}

function prettyJson(value: unknown) {
  if (typeof value !== 'string') return display(value)
  try { return JSON.stringify(JSON.parse(value), null, 2) } catch { return value }
}

function factTypeLabel(value: string) {
  return ({ observation_event: '观测事件', defect_event: '缺陷事件', work_order_event: '工单事件', business_event: '业务事件' } as Record<string, string>)[value] ?? value
}

function statusColor(value: string) {
  if (value === 'observed' || value === 'accepted' || value === 'executed') return 'green'
  if (value === 'derived' || value === 'proposed' || value === 'planned') return 'blue'
  if (value === 'needs_review' || value === 'blocked') return 'gold'
  if (value === 'retracted' || value === 'rejected') return 'red'
  return 'default'
}

function relationTable(data: Array<Record<string, string | number | null>>, columns: TableColumnsType<Record<string, string | number | null>>) {
  return <Table size="small" rowKey={(row) => String(row.derivation_id ?? row.decision_id ?? row.action_id ?? JSON.stringify(row))} dataSource={data} columns={columns} pagination={{ pageSize: 5, showSizeChanger: false }} scroll={{ x: 900 }} />
}

export function SemanticFactPage() {
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState(params.get('search') ?? '')
  const [selectedId, setSelectedId] = useState<string>()
  const page = Number(params.get('page') ?? 1)
  const factType = params.get('fact_type') ?? 'all'
  const status = params.get('status') ?? 'all'
  const query = useMemo(() => ({ page, pageSize, search: params.get('search') ?? '', factType, status }), [factType, page, params, status])
  const summary = useQuery({ queryKey: ['semantic-facts-summary'], queryFn: getSemanticFactSummary })
  const facts = useQuery({ queryKey: ['semantic-facts', query], queryFn: () => getSemanticFacts(query) })
  const detail = useQuery({ queryKey: ['semantic-fact', selectedId], queryFn: () => getSemanticFact(selectedId as string), enabled: Boolean(selectedId) })
  const summaryData = summary.data
  const rows = facts.data?.items ?? []

  const updateParam = (key: string, value?: string) => {
    const next = new URLSearchParams(params)
    if (value && value !== 'all') next.set(key, value); else next.delete(key)
    next.set('page', '1')
    setParams(next)
  }

  const columns: TableColumnsType<SemanticFactItem> = [
    { title: '事实类型', dataIndex: 'fact_type', width: 130, fixed: 'left', render: (value: string) => <Tag color="blue">{factTypeLabel(value)}</Tag> },
    { title: '统一设备对象', dataIndex: 'subject_key', width: 270, ellipsis: true, render: (value: string) => <Typography.Text strong copyable={{ text: value }}>{value}</Typography.Text> },
    { title: '谓词', dataIndex: 'predicate', width: 120 },
    { title: '事实值', dataIndex: 'value_json', width: 310, ellipsis: true, render: (value: string) => <Typography.Text code>{prettyJson(value)}</Typography.Text> },
    { title: '来源', key: 'source', width: 260, render: (_, row) => <Space direction="vertical" size={0}><Typography.Text>{row.source_schema}.{row.source_table}</Typography.Text><Typography.Text type="secondary">行 {row.source_row_id}</Typography.Text></Space> },
    { title: '状态', dataIndex: 'status', width: 105, render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> },
    { title: '置信度', dataIndex: 'confidence', width: 90, render: (value: number) => `${(value * 100).toFixed(1)}%` },
  ]

  const d: SemanticFactDetail | undefined = detail.data
  return <div>
    <PageHeader title="事实 · 事理 · 行动" description="沿着设备、业务事件、规则判断、派生事实和升级行动查看机器可解释链路；当前只读本地语义覆盖层。" extra={<Space><ReadOnlyTag /><Tag color="blue">本体运行层 v1</Tag></Space>} />
    {summary.isError && <Alert type="error" showIcon title="无法读取事实语义层" description="请先运行事实层构建脚本，并确认本地语义数据库可读。" style={{ marginBottom: 16 }} />}
    {summaryData && <Alert type="info" showIcon icon={<SafetyCertificateOutlined />} title="事实生成边界" description="当前只登记已有来源关系的观测、缺陷和工单事件指针；没有来源字段时，不会推断温度、缺陷编号、工单编号，也不会生成正式行动。" style={{ marginBottom: 16 }} />}
    <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title={<Space><ApartmentOutlined />来源事实</Space>} value={summaryData?.observedFactCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title={<Space><BranchesOutlined />派生事实</Space>} value={summaryData?.derivedFactCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title={<Space><AuditOutlined />规则判断</Space>} value={summaryData?.decisionCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card purple" variant="borderless"><Statistic title="升级行动" value={summaryData?.actionCount ?? 0} /></Card></Col>
    </Row>
    {summaryData && <Card size="small" style={{ marginBottom: 16 }}>
      <Steps size="small" current={0} items={[{ title: '来源事实', description: `${number(summaryData.observedFactCount)} 条` }, { title: '规则判断', description: `${number(summaryData.decisionCount)} 条` }, { title: '派生事实', description: `${number(summaryData.derivedFactCount)} 条` }, { title: '升级行动', description: `${number(summaryData.actionCount)} 条` }]} />
    </Card>}
    {summaryData && <Card size="small" style={{ marginBottom: 16 }} title="事理规则库" extra={<Typography.Link href="/knowledge-assets?asset_type=rule">查看规则资产</Typography.Link>}>
      <Space orientation="vertical" size={8} style={{ width: '100%' }}><Space wrap size={24}><Typography.Text>规则资产 <Typography.Text strong>{number(summaryData.logicRuleCount)}</Typography.Text></Typography.Text><Typography.Text>机器契约就绪 <Typography.Text strong type="success">{number(summaryData.logicReadyCount)}</Typography.Text></Typography.Text><Typography.Text>本次确定性规则 <Typography.Text strong>{number(summaryData.deterministicRuleCount)}</Typography.Text></Typography.Text><Typography.Text>行动规格 <Typography.Text strong>{number(summaryData.logicActionSpecCount)}</Typography.Text></Typography.Text><Typography.Text type="secondary">规则已登记不等于已经对事实执行</Typography.Text></Space>{summaryData.latestReasoningRun && <Typography.Text type="secondary">最近推理批次：{String(summaryData.latestReasoningRun.run_id)} · {String(summaryData.latestReasoningRun.status)} · 输入 {String(summaryData.latestReasoningRun.input_fact_count)} 条 · 判断 {String(summaryData.latestReasoningRun.decision_count)} 条 · 派生 {String(summaryData.latestReasoningRun.derived_fact_count)} 条 · 行动 {String(summaryData.latestReasoningRun.action_count)} 条</Typography.Text>}</Space>
    </Card>}
    <Card className="candidate-card" variant="borderless">
      <Space wrap style={{ width: '100%', marginBottom: 16 }}>
        <Input.Search allowClear placeholder="搜索设备、源表、源行或事实 ID" value={search} onChange={(event) => setSearch(event.target.value)} onSearch={(value) => updateParam('search', value.trim() || undefined)} style={{ width: 360 }} />
        <Select value={factType} onChange={(value) => updateParam('fact_type', value)} options={[{ value: 'all', label: '全部事实类型' }, ...(summaryData?.factTypes.map((item) => ({ value: item.value, label: factTypeLabel(item.value) })) ?? [])]} style={{ width: 160 }} />
        <Select value={status} onChange={(value) => updateParam('status', value)} options={[{ value: 'all', label: '全部状态' }, ...(summaryData?.factStatuses.map((item) => ({ value: item.value, label: item.value })) ?? [])]} style={{ width: 140 }} />
      </Space>
      <Table<SemanticFactItem> rowKey="fact_id" loading={facts.isLoading} columns={columns} dataSource={rows} size="middle" scroll={{ x: 1320 }} pagination={false} onRow={(record) => ({ onClick: () => setSelectedId(record.fact_id), className: 'clickable-row' })} />
      <Space style={{ width: '100%', justifyContent: 'space-between', marginTop: 16 }}><Typography.Text type="secondary">显示 {facts.data ? `${((page - 1) * pageSize) + (facts.data.total ? 1 : 0)}–${Math.min(page * pageSize, facts.data.total)}` : '—'} / {number(facts.data?.total)} 条</Typography.Text><Pagination current={page} pageSize={pageSize} total={facts.data?.total ?? 0} showSizeChanger={false} showQuickJumper onChange={(nextPage) => updateParam('page', String(nextPage))} /></Space>
    </Card>
    <Drawer open={Boolean(selectedId)} onClose={() => setSelectedId(undefined)} size="large" destroyOnClose title={d ? `事实 · ${d.fact.fact_id}` : '事实详情'}>
      {detail.isLoading && <Typography.Text type="secondary">正在读取来源、推理链和行动记录…</Typography.Text>}
      {d && <Space orientation="vertical" size={18} style={{ width: '100%' }}>
        <Descriptions column={2} size="small" bordered items={[
          { label: '事实类型', children: factTypeLabel(d.fact.fact_type) },
          { label: '状态', children: <Tag color={statusColor(d.fact.status)}>{d.fact.status}</Tag> },
          { label: '统一设备对象', children: <Typography.Text copyable>{d.fact.subject_key}</Typography.Text> },
          { label: '谓词', children: d.fact.predicate },
          { label: '事实值', children: <Typography.Text code>{prettyJson(d.fact.value_json)}</Typography.Text> },
          { label: '置信度', children: `${(d.fact.confidence * 100).toFixed(1)}%` },
          { label: '来源', children: `${d.fact.source_schema}.${d.fact.source_table}` },
          { label: '来源行', children: d.fact.source_row_id },
          { label: '来源快照', children: display(d.fact.source_snapshot_id) },
        ]} />
        <Alert type="success" showIcon title="来源证据可追溯" description="该事实只表示来源关系已被登记。数值观测、规则结论和行动必须在后续层分别产生，并保留输入、版本、约束和审批证据。" />
        <Divider titlePlacement="start">来源 → 规则判断 → 派生事实 → 行动</Divider>
        {d.derivations.length === 0 && d.decisions.length === 0 && d.actions.length === 0 && <Alert type="info" showIcon title="当前没有后续推理结果" description="这条记录尚未经过确定性规则执行，因此没有派生事实、规则判断或升级行动。" />}
        {d.derivations.length > 0 && relationTable(d.derivations, [{ title: '规则资产', dataIndex: 'rule_asset_id', width: 220 }, { title: '规则版本', dataIndex: 'rule_version_id', width: 220 }, { title: '解释', dataIndex: 'explanation', width: 300 }, { title: '约束结果', dataIndex: 'constraint_results_json', ellipsis: true, render: prettyJson }, { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> }])}
        {d.decisions.length > 0 && relationTable(d.decisions, [{ title: '判断', dataIndex: 'decision', width: 180 }, { title: '规则资产', dataIndex: 'rule_asset_id', width: 220 }, { title: '解释', dataIndex: 'explanation', width: 300 }, { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> }, { title: '需要行动', dataIndex: 'requires_action', render: (value: number) => value ? '是' : '否' }])}
        {d.actions.length > 0 && relationTable(d.actions, [{ title: '行动类型', dataIndex: 'action_type', width: 180 }, { title: '目标', dataIndex: 'target_key', width: 220 }, { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> }, { title: '需要审批', dataIndex: 'requires_approval', render: (value: number) => value ? '是' : '否' }, { title: '载荷', dataIndex: 'payload_json', ellipsis: true, render: prettyJson }])}
      </Space>}
    </Drawer>
  </div>
}
