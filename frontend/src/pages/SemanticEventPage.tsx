import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { CalendarOutlined, DatabaseOutlined, LinkOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { Alert, Card, Col, Descriptions, Divider, Drawer, Input, Pagination, Row, Select, Space, Statistic, Table, Tag, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { getSemanticEvent, getSemanticEventSummary, getSemanticEvents } from '../api/client'
import type { SemanticEventItem } from '../api/types'
import { PageHeader, ReadOnlyTag } from '../components/PageHeader'

const pageSize = 50

function display(value: unknown) {
  if (value === null || value === undefined || String(value).trim() === '') return '—'
  return String(value)
}

function eventLabel(value: string) {
  return ({ InspectionEvent: '巡检事件', DefectEvent: '缺陷事件', WorkOrderEvent: '工单事件', BusinessEvent: '业务事件' } as Record<string, string>)[value] ?? value
}

function eventColor(value: string) {
  return value === 'DefectEvent' ? 'red' : value === 'WorkOrderEvent' ? 'purple' : value === 'InspectionEvent' ? 'blue' : 'default'
}

export function SemanticEventPage() {
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState(params.get('search') ?? '')
  const [selectedId, setSelectedId] = useState<string>()
  const page = Number(params.get('page') ?? 1)
  const eventType = params.get('event_type') ?? 'all'
  const status = params.get('status') ?? 'all'
  const query = { page, pageSize, eventType, status, search: params.get('search') ?? '' }
  const summary = useQuery({ queryKey: ['semantic-event-summary'], queryFn: getSemanticEventSummary })
  const events = useQuery({ queryKey: ['semantic-events', query], queryFn: () => getSemanticEvents(query) })
  const detail = useQuery({ queryKey: ['semantic-event-detail', selectedId], queryFn: () => getSemanticEvent(selectedId as string), enabled: Boolean(selectedId) })

  const updateParam = (key: string, value?: string) => {
    const next = new URLSearchParams(params)
    if (value && value !== 'all') next.set(key, value); else next.delete(key)
    next.set('page', '1')
    setParams(next)
  }

  const rows = events.data?.items ?? []
  const columns: TableColumnsType<SemanticEventItem> = [
    { title: '事件类型', dataIndex: 'event_type', width: 130, fixed: 'left', render: (value: string) => <Tag color={eventColor(value)}>{eventLabel(value)}</Tag> },
    { title: '发生时间', dataIndex: 'occurred_at', width: 170, render: display },
    { title: '电厂', dataIndex: 'site_id', width: 110, render: display },
    { title: '统一设备', dataIndex: 'subject_key', width: 210, render: (value: string) => <Typography.Text code>{value}</Typography.Text> },
    { title: '位置', dataIndex: 'location_code', width: 160, render: display },
    { title: '源表 / 源行', width: 210, render: (_, row) => `${row.source_table} · ${row.source_row_id}` },
    { title: '原始状态', dataIndex: 'raw_status', width: 140, render: (value: string | null) => <Typography.Text code>{display(value)}</Typography.Text> },
    { title: '当前缺陷状态', width: 130, render: (_, row) => row.current_state ? <Tag color="green">{row.current_state_display || row.current_state}</Tag> : '—' },
  ]
  const d = detail.data

  return <div>
    <PageHeader title="事件中心" description="将巡检、缺陷、工单统一投影为可追溯事件；事件是状态变化的输入，不直接产生行动。" extra={<Space><ReadOnlyTag /><Tag color="blue">Event Layer v1</Tag></Space>} />
    {summary.data && <Alert type="info" showIcon icon={<SafetyCertificateOutlined />} title="事件边界" description="事件只来自已有设备身份桥接证据的本地事实，保留源系统原值、时间和快照；当前状态由确定性迁移层计算。" style={{ marginBottom: 16 }} />}
    {summary.data?.reviewEventCount ? <Alert type="warning" showIcon title={`待复核事件 ${summary.data.reviewEventCount} 条`} description="身份桥接或来源证据不足的记录未进入当前状态计算。" style={{ marginBottom: 16 }} /> : null}
    <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title={<Space><CalendarOutlined />统一事件</Space>} value={summary.data?.eventCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card purple" variant="borderless"><Statistic title="巡检事件" value={summary.data?.eventTypes.find((item) => item.value === 'InspectionEvent')?.count ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title="缺陷事件" value={summary.data?.eventTypes.find((item) => item.value === 'DefectEvent')?.count ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card green" variant="borderless"><Statistic title={<Space><DatabaseOutlined />工单事件</Space>} value={summary.data?.eventTypes.find((item) => item.value === 'WorkOrderEvent')?.count ?? 0} /></Card></Col>
    </Row>
    <Card className="candidate-card" variant="borderless">
      <Space wrap style={{ width: '100%', marginBottom: 16 }}>
        <Input.Search allowClear placeholder="搜索设备、源行、位置或描述" value={search} onChange={(event) => setSearch(event.target.value)} onSearch={(value) => updateParam('search', value.trim() || undefined)} style={{ width: 330 }} />
        <Select aria-label="事件类型筛选" value={eventType} onChange={(value) => updateParam('event_type', value)} options={[{ value: 'all', label: '全部事件' }, { value: 'InspectionEvent', label: '巡检事件' }, { value: 'DefectEvent', label: '缺陷事件' }, { value: 'WorkOrderEvent', label: '工单事件' }]} style={{ width: 150 }} />
        <Select aria-label="事件状态筛选" value={status} onChange={(value) => updateParam('status', value)} options={[{ value: 'all', label: '全部状态' }, { value: 'accepted', label: '已接受' }, { value: 'needs_review', label: '待复核' }]} style={{ width: 140 }} />
      </Space>
      <Table<SemanticEventItem> rowKey="event_id" loading={events.isLoading} columns={columns} dataSource={rows} size="middle" scroll={{ x: 1340 }} pagination={false} onRow={(record) => ({ onClick: () => setSelectedId(record.event_id), className: 'clickable-row' })} />
      <Space style={{ width: '100%', justifyContent: 'space-between', marginTop: 16 }}><Typography.Text type="secondary">共 {events.data?.total ?? 0} 条</Typography.Text><Pagination current={page} pageSize={pageSize} total={events.data?.total ?? 0} showSizeChanger={false} showQuickJumper onChange={(nextPage) => updateParam('page', String(nextPage))} /></Space>
    </Card>
    <Drawer open={Boolean(selectedId)} onClose={() => setSelectedId(undefined)} size="large" destroyOnClose title={d ? `${eventLabel(d.event.event_type)} · 事件详情` : '事件详情'}>
      {detail.isLoading && <Typography.Text type="secondary">正在读取事件证据链…</Typography.Text>}
      {d && <Space orientation="vertical" size={18} style={{ width: '100%' }}>
        <Descriptions column={2} size="small" bordered items={[{ label: '事件类型', children: <Tag color={eventColor(d.event.event_type)}>{eventLabel(d.event.event_type)}</Tag> }, { label: '发生时间', children: display(d.event.occurred_at) }, { label: '统一设备', children: <Typography.Text code>{d.event.subject_key}</Typography.Text> }, { label: '电厂', children: display(d.event.site_id) }, { label: '位置', children: display(d.event.location_code) }, { label: '源定位', children: `${d.event.source_schema}.${d.event.source_table} · ${d.event.source_row_id}` }, { label: '身份证据', children: <Tag color="green">{d.event.identity_status}</Tag> }, { label: '当前状态', children: d.currentState ? <Tag color="blue">{display(d.currentState.current_state_display || d.currentState.current_state)}</Tag> : '—' }]} />
        {d.event.description && <Typography.Paragraph className="source-description">{d.event.description}</Typography.Paragraph>}
        <Divider titlePlacement="start"><Space><LinkOutlined />状态迁移</Space></Divider>
        <Table size="small" rowKey="transition_id" dataSource={d.transitions} pagination={false} columns={[{ title: '类型', dataIndex: 'transition_type' }, { title: '前状态', dataIndex: 'from_state', render: display }, { title: '后状态', dataIndex: 'to_state' }, { title: '发生时间', dataIndex: 'effective_at', render: display }, { title: '状态', dataIndex: 'status' }]} />
        <Divider titlePlacement="start">来源事实</Divider>
        <Table size="small" rowKey="fact_id" dataSource={d.sourceFacts} pagination={false} columns={[{ title: '事实类型', dataIndex: 'fact_type' }, { title: '事实 ID', dataIndex: 'fact_id' }, { title: '状态', dataIndex: 'status' }, { title: '置信度', dataIndex: 'confidence' }, { title: '来源快照', dataIndex: 'source_snapshot_id', ellipsis: true }]} />
      </Space>}
    </Drawer>
  </div>
}
