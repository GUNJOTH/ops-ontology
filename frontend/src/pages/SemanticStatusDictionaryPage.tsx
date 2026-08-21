import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BookOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Col, Descriptions, Divider, Drawer, Input, Pagination, Row, Select, Space, Statistic, Table, Tag, Timeline, Typography, message } from 'antd'
import type { TableColumnsType } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { getSemanticStatesSummary, getSemanticStatusDictionary, getSemanticStatusDictionaryItem, getSemanticStatusDictionarySummary, replaySemanticStatusDictionary, reviewSemanticStatusDictionary } from '../api/client'
import type { SemanticStatusDictionaryItem } from '../api/types'
import { PageHeader, ReadOnlyTag } from '../components/PageHeader'

const pageSize = 50

function display(value: unknown) {
  if (value === null || value === undefined || String(value).trim() === '') return '—'
  return String(value)
}

function number(value: number | undefined) {
  return (value ?? 0).toLocaleString()
}

function statusColor(value: string) {
  if (value === 'approved') return 'green'
  if (value === 'rejected') return 'red'
  return 'gold'
}

export function SemanticStatusDictionaryPage() {
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState(params.get('search') ?? '')
  const [selectedId, setSelectedId] = useState<string>()
  const [canonicalState, setCanonicalState] = useState<string>()
  const [meaning, setMeaning] = useState('')
  const [notes, setNotes] = useState('')
  const [reviewer, setReviewer] = useState('人工审核')
  const queryClient = useQueryClient()
  const page = Number(params.get('page') ?? 1)
  const sourceSchema = params.get('source_schema') ?? 'all'
  const sourceTable = params.get('source_table') ?? 'all'
  const mappingStatus = params.get('mapping_status') ?? 'all'
  const query = { page, pageSize, search: params.get('search') ?? '', sourceSchema, sourceTable, mappingStatus }
  const summary = useQuery({ queryKey: ['semantic-status-summary'], queryFn: getSemanticStatusDictionarySummary })
  const states = useQuery({ queryKey: ['semantic-states-summary'], queryFn: getSemanticStatesSummary })
  const items = useQuery({ queryKey: ['semantic-status-dictionary', query], queryFn: () => getSemanticStatusDictionary(query) })
  const detail = useQuery({ queryKey: ['semantic-status-detail', selectedId], queryFn: () => getSemanticStatusDictionaryItem(selectedId as string), enabled: Boolean(selectedId) })
  const review = useMutation({
    mutationFn: (payload: { decision: 'approved' | 'rejected' }) => reviewSemanticStatusDictionary(selectedId as string, { decision: payload.decision, canonicalState, businessMeaning: meaning, notes, reviewer }),
    onSuccess: (_, variables) => {
      message.success(variables.decision === 'approved' ? '已确认状态业务含义' : '已标记为不采用')
      void queryClient.invalidateQueries({ queryKey: ['semantic-status-summary'] })
      void queryClient.invalidateQueries({ queryKey: ['semantic-status-dictionary'] })
      void queryClient.invalidateQueries({ queryKey: ['semantic-status-detail', selectedId] })
    },
    onError: (error) => message.error(error instanceof Error ? error.message : '状态字典审核失败'),
  })
  const replay = useMutation({
    mutationFn: replaySemanticStatusDictionary,
    onSuccess: (result) => {
      message[result.status === 'completed' ? 'success' : 'warning'](`状态映射回放：${result.status}`)
      void queryClient.invalidateQueries({ queryKey: ['semantic-status-summary'] })
      void queryClient.invalidateQueries({ queryKey: ['semantic-facts-summary'] })
      void queryClient.invalidateQueries({ queryKey: ['semantic-facts'] })
      void queryClient.invalidateQueries({ queryKey: ['semantic-states-summary'] })
    },
    onError: (error) => message.error(error instanceof Error ? error.message : '状态映射回放失败'),
  })

  const updateParam = (key: string, value?: string) => {
    const next = new URLSearchParams(params)
    if (value && value !== 'all') next.set(key, value); else next.delete(key)
    next.set('page', '1')
    setParams(next)
  }

  const openDetail = (item: SemanticStatusDictionaryItem) => {
    setSelectedId(item.status_id)
    setCanonicalState(item.canonical_state ?? undefined)
    setMeaning(item.business_meaning ?? '')
    setNotes(item.mapping_notes ?? '')
    setReviewer(item.reviewer ?? '人工审核')
  }

  const submitReview = (decision: 'approved' | 'rejected') => {
    if (decision === 'approved' && !canonicalState) {
      message.warning('确认前请选择标准缺陷状态')
      return
    }
    review.mutate({ decision })
  }

  const rows = items.data?.items ?? []
  const columns: TableColumnsType<SemanticStatusDictionaryItem> = [
    { title: '系统', dataIndex: 'source_schema', width: 130, fixed: 'left' },
    { title: '源表', dataIndex: 'source_table', width: 180 },
    { title: '原始状态值', dataIndex: 'raw_status', width: 200, render: (value: string) => <Typography.Text code>{value.trim() ? value : '(空值)'}</Typography.Text> },
    { title: '证据条数', dataIndex: 'evidence_count', width: 100, render: (value: number) => number(value) },
    { title: '已挂设备证据', dataIndex: 'linked_device_evidence_count', width: 125, render: (value: number) => number(value) },
    { title: '标准状态', dataIndex: 'canonical_state', width: 130, render: (value: string | null) => value ? <Tag color="blue">{value}</Tag> : '—' },
    { title: '业务显示名', dataIndex: 'business_meaning', width: 160, render: display },
    { title: '版本', dataIndex: 'mapping_version', width: 80, render: (value: number) => `v${value}` },
    { title: '状态', dataIndex: 'mapping_status', width: 100, render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> },
  ]

  const d = detail.data?.item
  return <div>
    <PageHeader title="状态中心 · 缺陷状态" description="将 HD/XNY 原始状态映射为可计算的标准缺陷状态；保留原值、映射版本、审核记录和回放证据。" extra={<Space><ReadOnlyTag /><Tag color="blue">Canonical State v1</Tag></Space>} />
    {summary.isError && <Alert type="error" showIcon title="无法读取缺陷状态字典" description="请先运行状态字典构建脚本。" style={{ marginBottom: 16 }} />}
    {summary.data && <Alert type="info" showIcon icon={<SafetyCertificateOutlined />} title="确认边界" description="页面不会自动猜测原始编码。确认时选择固定标准状态；证据不足可选择 UNKNOWN，或继续保持待确认。所有确认只写本地语义覆盖层。" style={{ marginBottom: 16 }} />}
    {summary.data?.latestReplay && <Alert type={String(summary.data.latestReplay.status) === 'completed' ? 'success' : 'warning'} showIcon title={`最近状态映射回放：${String(summary.data.latestReplay.status)}`} description={`批次 ${String(summary.data.latestReplay.run_id)} · 已确认映射 ${String(summary.data.latestReplay.approved_mapping_count)} 条 · 匹配事实 ${String(summary.data.latestReplay.matched_fact_count)} 条 · 判断 ${String(summary.data.latestReplay.decision_count)} 条 · 派生 ${String(summary.data.latestReplay.derived_fact_count)} 条 · 行动 ${String(summary.data.latestReplay.action_count)} 条 · ${String(summary.data.latestReplay.note)}`} style={{ marginBottom: 16 }} />}
    {states.data?.latestRun && <Alert type={states.data.reviewTransitionCount ? 'warning' : 'success'} showIcon title={`当前状态层：${states.data.currentStateCount} 条`} description={`迁移记录 ${states.data.transitionCount} 条 · 待复核迁移 ${states.data.reviewTransitionCount} 条 · ${String(states.data.latestRun.note ?? '')}`} style={{ marginBottom: 16 }} />}
    <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title={<Space><BookOutlined />状态候选</Space>} value={summary.data?.candidateCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card purple" variant="borderless"><Statistic title="待确认" value={summary.data?.pendingCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title="已确认" value={summary.data?.approvedCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title="证据记录" value={summary.data?.evidenceRowCount ?? 0} /></Card></Col>
    </Row>
    <Card className="candidate-card" variant="borderless" extra={<Button type="primary" ghost loading={replay.isPending} onClick={() => replay.mutate()}>运行状态映射回放</Button>}>
      <Space wrap style={{ width: '100%', marginBottom: 16 }}>
        <Input.Search allowClear placeholder="搜索原始编码、业务含义或状态 ID" value={search} onChange={(event) => setSearch(event.target.value)} onSearch={(value) => updateParam('search', value.trim() || undefined)} style={{ width: 340 }} />
        <select aria-label="系统筛选" value={sourceSchema} onChange={(event) => updateParam('source_schema', event.target.value)} style={{ height: 32, minWidth: 130, borderColor: '#d9d9d9', borderRadius: 6, padding: '0 8px' }}><option value="all">全部系统</option>{(summary.data?.systems ?? []).map((item) => <option key={item.value} value={item.value}>{item.value}</option>)}</select>
        <select aria-label="审核状态筛选" value={mappingStatus} onChange={(event) => updateParam('mapping_status', event.target.value)} style={{ height: 32, minWidth: 130, borderColor: '#d9d9d9', borderRadius: 6, padding: '0 8px' }}><option value="all">全部状态</option><option value="pending">待确认</option><option value="approved">已确认</option><option value="rejected">不采用</option></select>
      </Space>
      <Table<SemanticStatusDictionaryItem> rowKey="status_id" loading={items.isLoading} columns={columns} dataSource={rows} size="middle" scroll={{ x: 1280 }} pagination={false} onRow={(record) => ({ onClick: () => openDetail(record), className: 'clickable-row' })} />
      <Space style={{ width: '100%', justifyContent: 'space-between', marginTop: 16 }}><Typography.Text type="secondary">显示 {items.data ? `${((page - 1) * pageSize) + (items.data.total ? 1 : 0)}–${Math.min(page * pageSize, items.data.total)}` : '—'} / {number(items.data?.total)} 条</Typography.Text><Pagination current={page} pageSize={pageSize} total={items.data?.total ?? 0} showSizeChanger={false} showQuickJumper onChange={(nextPage) => updateParam('page', String(nextPage))} /></Space>
    </Card>
    <Drawer open={Boolean(selectedId)} onClose={() => setSelectedId(undefined)} size="large" destroyOnClose title={d ? `${d.source_schema}.${d.source_table} · 状态字典` : '状态字典详情'}>
      {detail.isLoading && <Typography.Text type="secondary">正在读取状态证据样本…</Typography.Text>}
      {d && <Space orientation="vertical" size={18} style={{ width: '100%' }}>
        <Descriptions column={2} size="small" bordered items={[{ label: '系统', children: d.source_schema }, { label: '源表', children: d.source_table }, { label: '原始状态值', children: <Typography.Text code>{d.raw_status.trim() ? d.raw_status : '(空值)'}</Typography.Text> }, { label: '证据条数', children: number(d.evidence_count) }, { label: '挂接设备证据', children: number(d.linked_device_evidence_count) }, { label: '来源快照', children: d.source_snapshot_id }, { label: '审核状态', children: <Tag color={statusColor(d.mapping_status)}>{d.mapping_status}</Tag> }, { label: '映射版本', children: `v${d.mapping_version}` }, { label: '标准状态', children: d.canonical_state ? <Tag color="blue">{d.canonical_state}</Tag> : '—' }, { label: '确认人', children: display(d.reviewer) }]} />
        <Alert type="warning" showIcon title="原始值不等于业务含义" description="请根据 HD/XNY 各自的状态字典或业务负责人确认。不要因为两个系统的编码看起来相似，就直接合并含义。" />
        <Divider titlePlacement="start">来源样本</Divider>
        <Table size="small" rowKey="event_record_id" dataSource={d.examples ?? []} pagination={false} columns={[{ title: '源行', dataIndex: 'source_row_id', width: 170 }, { title: '电厂', dataIndex: 'site_id', width: 130 }, { title: '位置', dataIndex: 'location_code', width: 170 }, { title: '原始状态', dataIndex: 'status', width: 140, render: display }, { title: '描述', dataIndex: 'description', ellipsis: true }, { title: '身份挂接', dataIndex: 'link_status', width: 150 }]} scroll={{ x: 900 }} />
        <Divider titlePlacement="start">人工确认</Divider>
        <Select
          placeholder="选择标准缺陷状态"
          value={canonicalState}
          onChange={(value) => {
            setCanonicalState(value)
            const option = summary.data?.canonicalStates.find((item) => item.value === value)
            if (option && !meaning.trim()) setMeaning(option.display_name)
          }}
          options={(summary.data?.canonicalStates ?? []).map((item) => ({ value: item.value, label: `${item.value} · ${item.display_name}`, title: item.description }))}
          style={{ width: '100%' }}
        />
        {canonicalState && <Alert type="info" showIcon title={summary.data?.canonicalStates.find((item) => item.value === canonicalState)?.display_name} description={summary.data?.canonicalStates.find((item) => item.value === canonicalState)?.description} />}
        <Input placeholder="业务显示名；为空时使用标准状态中文名" value={meaning} onChange={(event) => setMeaning(event.target.value)} maxLength={200} />
        <Input.TextArea placeholder="确认依据或备注（可选）" value={notes} onChange={(event) => setNotes(event.target.value)} maxLength={500} autoSize={{ minRows: 3, maxRows: 6 }} />
        <Input placeholder="审核人" value={reviewer} onChange={(event) => setReviewer(event.target.value)} maxLength={100} />
        <Space><Button type="primary" loading={review.isPending} onClick={() => submitReview('approved')}>确认标准状态</Button><Button danger loading={review.isPending} onClick={() => submitReview('rejected')}>拒绝此映射</Button></Space>
        {(detail.data?.reviews?.length ?? 0) > 0 && <><Divider titlePlacement="start">审核历史</Divider><Timeline items={(detail.data?.reviews ?? []).map((item) => ({ color: item.decision === 'approved' ? 'green' : 'red', children: <div><Typography.Text strong>{String(item.decision)} · v{String(item.mapping_version)}</Typography.Text><br /><Typography.Text>{display(item.canonical_state)} · {display(item.business_meaning)}</Typography.Text><br /><Typography.Text type="secondary">{display(item.reviewer)} · {display(item.reviewed_at)} · {display(item.notes)}</Typography.Text></div> }))} /></>}
      </Space>}
    </Drawer>
  </div>
}
