import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Col, Descriptions, Divider, Drawer, Input, Pagination, Row, Select, Space, Statistic, Table, Tag, Typography, message } from 'antd'
import type { TableColumnsType } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { getSemanticActionPlan, getSemanticActionPlans, getSemanticDecisionSummary, getSemanticDecisions, reviewSemanticActionPlan } from '../api/client'
import type { SemanticActionPlanItem, SemanticDecisionItem } from '../api/types'
import { PageHeader, ReadOnlyTag } from '../components/PageHeader'

const pageSize = 50

function number(value: number | undefined) { return (value ?? 0).toLocaleString() }
function display(value: unknown) { return value === null || value === undefined || String(value).trim() === '' ? '—' : String(value) }
function statusColor(value: string) {
  if (value === 'accepted' || value === 'APPROVED') return 'green'
  if (value === 'PENDING_APPROVAL' || value === 'needs_review') return 'gold'
  if (value === 'REJECTED' || value === 'rejected') return 'red'
  return 'blue'
}

export function DecisionActionPage() {
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState(params.get('search') ?? '')
  const [selectedPlanId, setSelectedPlanId] = useState<string>()
  const queryClient = useQueryClient()
  const page = Number(params.get('page') ?? 1)
  const status = params.get('status') ?? 'all'
  const requiresAction = params.get('requires_action') ?? 'all'
  const activeTab = params.get('tab') ?? 'decisions'
  const query = { page, pageSize, status, requiresAction, search: params.get('search') ?? '' }
  const summary = useQuery({ queryKey: ['semantic-decision-summary'], queryFn: getSemanticDecisionSummary })
  const decisions = useQuery({ queryKey: ['semantic-decisions', query], queryFn: () => getSemanticDecisions(query), enabled: activeTab === 'decisions' })
  const plans = useQuery({ queryKey: ['semantic-action-plans', query], queryFn: () => getSemanticActionPlans({ page, pageSize, status, search: params.get('search') ?? '' }), enabled: activeTab === 'actions' })
  const detail = useQuery({ queryKey: ['semantic-action-plan', selectedPlanId], queryFn: () => getSemanticActionPlan(selectedPlanId as string), enabled: Boolean(selectedPlanId) })
  const approve = useMutation({
    mutationFn: (decision: 'approved' | 'rejected') => reviewSemanticActionPlan(selectedPlanId as string, { decision }),
    onSuccess: (result) => {
      message.success(result.status === 'APPROVED' ? '行动计划已审批通过，仍未执行源系统动作' : '行动计划已驳回')
      setSelectedPlanId(undefined)
      void queryClient.invalidateQueries({ queryKey: ['semantic-decision-summary'] })
      void queryClient.invalidateQueries({ queryKey: ['semantic-action-plans'] })
    },
    onError: (error) => message.error(error instanceof Error ? error.message : '审批失败'),
  })

  const updateParam = (key: string, value?: string) => {
    const next = new URLSearchParams(params)
    if (value && value !== 'all') next.set(key, value); else next.delete(key)
    if (key !== 'tab') next.set('page', '1')
    setParams(next)
  }

  const decisionColumns: TableColumnsType<SemanticDecisionItem> = [
    { title: '规则判断', dataIndex: 'decision', width: 190, fixed: 'left', render: (value: string) => <Tag color="blue">{value}</Tag> },
    { title: '统一设备对象', dataIndex: 'subject_key', width: 250, render: (value: string) => <Typography.Text code>{value}</Typography.Text> },
    { title: '规则 / 版本', width: 300, render: (_, row) => <Space orientation="vertical" size={0}><Typography.Text>{row.rule_asset_id}</Typography.Text><Typography.Text type="secondary">{row.rule_version_id}</Typography.Text></Space> },
    { title: '置信度', dataIndex: 'confidence', width: 100, render: (value: number) => `${(value * 100).toFixed(1)}%` },
    { title: '行动', width: 100, render: (_, row) => row.requires_action ? <Tag color="gold">需要行动</Tag> : <Tag>无行动</Tag> },
    { title: '行动计划', width: 145, render: (_, row) => row.plan_id ? <Tag color={statusColor(row.plan_status ?? '')}>{row.plan_status}</Tag> : '—' },
    { title: '状态', dataIndex: 'status', width: 100, render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> },
  ]

  const planColumns: TableColumnsType<SemanticActionPlanItem> = [
    { title: '行动类型', dataIndex: 'action_type', width: 180, fixed: 'left', render: (value: string) => <Tag color="purple">{value}</Tag> },
    { title: '目标', width: 230, render: (_, row) => <Space orientation="vertical" size={0}><Typography.Text>{row.target_type}</Typography.Text><Typography.Text type="secondary">{display(row.target_key)}</Typography.Text></Space> },
    { title: '风险', dataIndex: 'risk_level', width: 90, render: (value: string) => <Tag color={value === 'high' ? 'red' : 'gold'}>{value}</Tag> },
    { title: '审批状态', dataIndex: 'status', width: 145, render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> },
    { title: '审批凭据', dataIndex: 'approval_receipt', width: 180, ellipsis: true, render: display },
    { title: '更新时间', dataIndex: 'updated_at', width: 190 },
  ]

  const data = summary.data
  return <div>
    <PageHeader title="决策与行动" description="Fact → Rule Decision → ActionPlan → Approval。只写本地语义覆盖层，审批不会调用源系统。" extra={<Space><ReadOnlyTag /><Tag color="blue">Decision Runtime v1</Tag></Space>} />
    {summary.isError && <Alert type="error" showIcon title="决策行动层尚未就绪" description="请先运行 system/build_decision_action_layer.py；页面不会用 mock 数据代替真实结果。" style={{ marginBottom: 16 }} />}
    {data && <Alert type="info" showIcon title="行动边界" description="当前规则只确认已有来源事实和状态，不会因为“缺陷存在”就自动创建工单。只有显式配置的行动建议才会生成 ActionPlan，并且必须单独审批。" style={{ marginBottom: 16 }} />}
    <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title="规则判断" value={data?.decisionCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card purple" variant="borderless"><Statistic title="行动计划" value={data?.actionPlanCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title="待审批" value={data?.pendingApprovalCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card green" variant="borderless"><Statistic title="审批记录" value={data?.approvalCount ?? 0} /></Card></Col>
    </Row>
    {data && <Card size="small" style={{ marginBottom: 16 }}><Space wrap size={24}><Typography.Text>输入事实 {number(Number(data.latestRun.input_fact_count ?? 0))} 条</Typography.Text><Typography.Text>决策 {number(data.decisionCount)} 条</Typography.Text><Typography.Text>已登记行动规则 {number(data.actionRuleCount)} 条</Typography.Text><Typography.Text>规则状态命中 {number(data.actionRuleMatchCount)} 条</Typography.Text><Typography.Text>风险证据 {number(data.riskEvidenceCount)} 条</Typography.Text><Typography.Text>行动计划 {number(data.actionPlanCount)} 条</Typography.Text><Typography.Text>未绑定行动判断 {number(Number(data.latestRun.unbound_action_decision_count ?? 0))} 条</Typography.Text><Typography.Text type="secondary">最近运行：{String(data.latestRun.created_at)}</Typography.Text></Space></Card>}
    <Card className="candidate-card" variant="borderless">
      <Space wrap style={{ width: '100%', marginBottom: 16 }}>
        <Button type={activeTab === 'decisions' ? 'primary' : 'default'} onClick={() => updateParam('tab', 'decisions')}>规则判断</Button>
        <Button type={activeTab === 'actions' ? 'primary' : 'default'} onClick={() => updateParam('tab', 'actions')}>行动审批 {data?.pendingApprovalCount ? `(${data.pendingApprovalCount})` : ''}</Button>
        <Input.Search allowClear placeholder="搜索设备、规则或行动目标" value={search} onChange={(event) => setSearch(event.target.value)} onSearch={(value) => updateParam('search', value.trim() || undefined)} style={{ width: 330 }} />
        {activeTab === 'decisions' ? <><Select value={status} onChange={(value) => updateParam('status', value)} options={[{ value: 'all', label: '全部决策状态' }, { value: 'accepted', label: 'accepted' }, { value: 'needs_review', label: 'needs_review' }]} style={{ width: 160 }} /><Select value={requiresAction} onChange={(value) => updateParam('requires_action', value)} options={[{ value: 'all', label: '是否需要行动' }, { value: '1', label: '需要行动' }, { value: '0', label: '无行动' }]} style={{ width: 140 }} /></> : <Select value={status} onChange={(value) => updateParam('status', value)} options={[{ value: 'all', label: '全部计划状态' }, { value: 'PENDING_APPROVAL', label: '待审批' }, { value: 'APPROVED', label: '已批准' }, { value: 'REJECTED', label: '已驳回' }]} style={{ width: 160 }} />}
      </Space>
      {activeTab === 'decisions' && <><Table<SemanticDecisionItem> rowKey="decision_id" loading={decisions.isLoading} columns={decisionColumns} dataSource={decisions.data?.items ?? []} size="middle" scroll={{ x: 1220 }} pagination={false} /><Space style={{ width: '100%', justifyContent: 'space-between', marginTop: 16 }}><Typography.Text type="secondary">共 {decisions.data?.total ?? 0} 条规则判断</Typography.Text><Pagination current={page} pageSize={pageSize} total={decisions.data?.total ?? 0} showSizeChanger={false} showQuickJumper onChange={(next) => updateParam('page', String(next))} /></Space></>}
      {activeTab === 'actions' && <><Table<SemanticActionPlanItem> rowKey="plan_id" loading={plans.isLoading} columns={planColumns} dataSource={plans.data?.items ?? []} size="middle" scroll={{ x: 1120 }} pagination={false} onRow={(record) => ({ onClick: () => setSelectedPlanId(record.plan_id), className: 'clickable-row' })} /><Space style={{ width: '100%', justifyContent: 'space-between', marginTop: 16 }}><Typography.Text type="secondary">共 {plans.data?.total ?? 0} 条行动计划</Typography.Text><Pagination current={page} pageSize={pageSize} total={plans.data?.total ?? 0} showSizeChanger={false} showQuickJumper onChange={(next) => updateParam('page', String(next))} /></Space>{plans.data?.total === 0 && <Alert type="success" showIcon title="当前没有待审批行动" description={`这不是空壳：当前 ${number(data?.decisionCount ?? 0)} 条规则判断都明确标记为无需行动，系统没有擅自把事实推成工单。新增明确行动规则并通过回放后，ActionPlan 会在这里出现。`} style={{ marginTop: 16 }} />}</>}
    </Card>
    <Drawer open={Boolean(selectedPlanId)} onClose={() => setSelectedPlanId(undefined)} size="large" destroyOnClose title={detail.data ? `行动计划 · ${detail.data.plan.plan_id}` : '行动计划详情'}>
      {detail.isLoading && <Typography.Text type="secondary">正在读取决策和审批证据…</Typography.Text>}
      {detail.data && <Space orientation="vertical" size={18} style={{ width: '100%' }}>
        <Descriptions bordered size="small" column={2} items={[{ label: '行动类型', children: detail.data.plan.action_type }, { label: '状态', children: <Tag color={statusColor(detail.data.plan.status)}>{detail.data.plan.status}</Tag> }, { label: '目标', children: `${detail.data.plan.target_type} · ${display(detail.data.plan.target_key)}` }, { label: '风险', children: detail.data.plan.risk_level }, { label: '需要审批', children: detail.data.plan.requires_approval ? '是' : '否' }, { label: '来源决策', children: detail.data.plan.decision_id }]} />
        <Alert type="warning" showIcon title="审批不等于执行" description="本页面的批准只记录本地审批凭据。系统没有执行器，也不会写入 MaxiEAM、HD、XNY 或其他源系统。" />
        <Divider titlePlacement="start">规则判断</Divider>
        {detail.data.decision && <Descriptions bordered size="small" column={1} items={[{ label: '判断', children: detail.data.decision.decision }, { label: '解释', children: detail.data.decision.explanation }, { label: '输入事实', children: <Typography.Text code>{detail.data.decision.input_fact_ids_json}</Typography.Text> }]} />}
        {detail.data.plan.status === 'PENDING_APPROVAL' && <Space><Button type="primary" loading={approve.isPending} onClick={() => approve.mutate('approved')}>批准行动计划</Button><Button danger loading={approve.isPending} onClick={() => approve.mutate('rejected')}>驳回行动计划</Button></Space>}
      </Space>}
    </Drawer>
  </div>
}
