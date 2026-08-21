import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ApartmentOutlined, LinkOutlined, SafetyCertificateOutlined, SearchOutlined } from '@ant-design/icons'
import { Alert, Card, Col, Descriptions, Divider, Drawer, Flex, Input, Pagination, Row, Select, Space, Statistic, Table, Tag, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { getUnifiedDevice, getUnifiedDeviceSummary, getUnifiedDevices } from '../api/client'
import type { UnifiedBusinessLink, UnifiedBusinessRecordEvidence, UnifiedDeviceMapping, UnifiedDeviceRelation, UnifiedDeviceRow } from '../api/types'
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
  if (value === 'accepted') return 'green'
  if (value === 'blocked') return 'red'
  if (value === 'needs_review') return 'gold'
  return 'blue'
}

export function UnifiedDevicePage() {
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState(params.get('search') ?? '')
  const [selectedId, setSelectedId] = useState<string>()
  const page = Number(params.get('page') ?? 1)
  const sourceSchema = params.get('source_schema') ?? 'all'
  const siteId = params.get('site_id') ?? ''
  const query = useMemo(() => ({ page, pageSize, search: params.get('search') ?? '', sourceSchema, siteId }), [page, params, siteId, sourceSchema])

  const summary = useQuery({ queryKey: ['unified-device-summary'], queryFn: getUnifiedDeviceSummary })
  const devices = useQuery({ queryKey: ['unified-devices', query], queryFn: () => getUnifiedDevices(query) })
  const detail = useQuery({ queryKey: ['unified-device', selectedId], queryFn: () => getUnifiedDevice(selectedId as string), enabled: Boolean(selectedId) })

  const updateParam = (key: string, value?: string) => {
    const next = new URLSearchParams(params)
    if (value && value !== 'all') next.set(key, value); else next.delete(key)
    next.set('page', '1')
    setParams(next)
  }

  const summaryData = summary.data
  const rows = devices.data?.rows ?? []
  const columns: TableColumnsType<UnifiedDeviceRow> = [
    { title: '来源系统', dataIndex: 'sourceSchema', width: 110, fixed: 'left', render: (value: string) => <Tag color={value === 'HD_SAAS' ? 'blue' : 'purple'}>{value}</Tag> },
    { title: '电厂', dataIndex: 'siteId', width: 105 },
    { title: '统一设备 ID', dataIndex: 'unifiedDeviceId', width: 240, ellipsis: true, render: (value: string) => <Typography.Text copyable={{ text: value }} ellipsis>{value}</Typography.Text> },
    { title: '源设备编码', dataIndex: 'assetNumber', width: 155, render: (value: string) => <Typography.Text strong>{display(value)}</Typography.Text> },
    { title: '统一名称', dataIndex: 'canonicalName', width: 240, ellipsis: true, render: (value: string) => display(value) },
    { title: '位置上下文', dataIndex: 'locationCode', width: 150, ellipsis: true, render: (value: string) => display(value) },
    { title: '业务挂接', dataIndex: 'businessLinkCount', width: 100, render: (value: number) => value ? <Tag color="green">{value} 条</Tag> : <Typography.Text type="secondary">0</Typography.Text> },
    { title: '关系', dataIndex: 'relationCount', width: 80, render: (value: number) => value ? <Tag color="blue">{value}</Tag> : <Typography.Text type="secondary">0</Typography.Text> },
    { title: '状态', dataIndex: 'mappingStatus', width: 135, render: (value: string) => <Tag>{value}</Tag> },
  ]

  const mappingColumns: TableColumnsType<UnifiedDeviceMapping> = [
    { title: '来源', key: 'source', width: 110, render: (_, row) => `${row.source_schema} / ${row.source_table_group}` },
    { title: '源表', dataIndex: 'source_table', width: 150 },
    { title: '源记录号', dataIndex: 'source_row_id', width: 170, ellipsis: true },
    { title: '源键', dataIndex: 'source_key', width: 210, ellipsis: true },
    { title: '原始描述', dataIndex: 'raw_description', width: 260, ellipsis: true, render: display },
    { title: '判断', dataIndex: 'status', width: 105, render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> },
  ]

  const linkColumns: TableColumnsType<UnifiedBusinessLink> = [
    { title: '业务类型', dataIndex: 'business_type', width: 110 },
    { title: '来源', key: 'source', width: 135, render: (_, row) => `${row.source_schema} / ${row.source_table_group}` },
    { title: '源表', dataIndex: 'source_table', width: 150 },
    { title: '源记录号', dataIndex: 'source_row_id', width: 180, ellipsis: true },
    { title: '源键', dataIndex: 'source_key', width: 220, ellipsis: true },
    { title: '状态', dataIndex: 'status', width: 105, render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> },
  ]

  const relationColumns: TableColumnsType<UnifiedDeviceRelation> = [
    { title: '关系', dataIndex: 'predicate', width: 160 },
    { title: '对象统一设备', dataIndex: 'object_unified_device_id', width: 250, ellipsis: true, render: display },
    { title: '证据来源', key: 'evidence', width: 190, render: (_, row) => `${row.source_schema} / ${row.source_table}` },
    { title: '置信度', dataIndex: 'confidence', width: 90, render: (value: number) => `${Math.round(value * 100)}%` },
    { title: '状态', dataIndex: 'status', width: 105, render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> },
  ]

  const evidenceColumns: TableColumnsType<UnifiedBusinessRecordEvidence> = [
    { title: '业务类型', dataIndex: 'event_type', width: 105 },
    { title: '源表', dataIndex: 'source_table', width: 150 },
    { title: '源记录号', dataIndex: 'source_row_id', width: 180, ellipsis: true },
    { title: '业务描述', dataIndex: 'description', width: 280, ellipsis: true, render: display },
    { title: '位置', dataIndex: 'location_code', width: 160, ellipsis: true, render: display },
    { title: '挂接状态', dataIndex: 'link_status', width: 160, render: (value: string) => <Tag color={value === 'accepted' ? 'green' : 'gold'}>{value}</Tag> },
  ]

  return <div>
    <PageHeader title="统一设备对象" description="本体语义层的关系实现：统一设备 → 来源身份 → 巡检、缺陷、工单与设备关系。源系统和源表始终只读。" extra={<Space><ReadOnlyTag /><Tag color="blue">关系库 v1</Tag></Space>} />

    {summary.isError && <Alert type="error" showIcon title="无法读取统一设备结果层" description="请先运行本地关系层构建脚本，并确认 FastAPI 已正常启动。" style={{ marginBottom: 16 }} />}
    {summaryData && <Alert type="info" showIcon icon={<SafetyCertificateOutlined />} title="当前是本地关系型语义覆盖层" description={`快照 ${summaryData.sourceSnapshotId}；HD_SAAS 与 XNY_SAAS 当前按各自系统建立统一设备对象，不自动判断两套编码是否同一设备。身份映射、业务挂接和父子关系均保留证据与状态。`} style={{ marginBottom: 16 }} />}

    <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title={<Space><ApartmentOutlined />统一设备对象</Space>} value={summaryData?.unifiedDeviceCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title={<Space><LinkOutlined />身份映射</Space>} value={summaryData?.identityMapCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title="已挂接业务记录" value={summaryData?.acceptedBusinessLinkCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card purple" variant="borderless"><Statistic title="设备关系" value={summaryData?.relationCount ?? 0} /></Card></Col>
    </Row>

    {summaryData && <Card size="small" style={{ marginBottom: 16 }}>
      <Flex justify="space-between" align="center" wrap gap={12}>
        <Space wrap><Typography.Text type="secondary">来源系统</Typography.Text>{summaryData.sourceSystems.map((item) => <Tag key={item.system_key} color="green">{item.display_name} · 只读快照</Tag>)}<Tag>业务事件证据 {number(summaryData.businessEventCount)} 条</Tag><Tag>跨系统候选 {number(summaryData.crossSystemCandidateCount)} 条</Tag></Space>
        <Typography.Text type="secondary">运行批次 {summaryData.runId}</Typography.Text>
      </Flex>
    </Card>}

    <Card className="candidate-card" variant="borderless">
      <Flex justify="space-between" align="center" wrap gap={12} className="candidate-toolbar">
        <Input.Search allowClear prefix={<SearchOutlined />} placeholder="搜索设备编码、名称、位置或源 ID" value={search} onChange={(event) => setSearch(event.target.value)} onSearch={(value) => updateParam('search', value.trim() || undefined)} style={{ width: 360 }} />
        <Space wrap><Select value={sourceSchema} onChange={(value) => updateParam('source_schema', value)} options={[{ value: 'all', label: '全部来源系统' }, { value: 'HD_SAAS', label: 'HD_SAAS' }, { value: 'XNY_SAAS', label: 'XNY_SAAS' }]} style={{ width: 160 }} /><Input placeholder="电厂 SITEID" value={siteId} allowClear onChange={(event) => setParams((current) => { const next = new URLSearchParams(current); if (event.target.value) next.set('site_id', event.target.value); else next.delete('site_id'); next.set('page', '1'); return next })} onPressEnter={() => updateParam('site_id', siteId || undefined)} style={{ width: 150 }} /></Space>
      </Flex>
      <Table<UnifiedDeviceRow> rowKey="unifiedDeviceId" loading={devices.isLoading} columns={columns} dataSource={rows} size="middle" scroll={{ x: 1450 }} pagination={false} onRow={(record) => ({ onClick: () => setSelectedId(record.unifiedDeviceId), className: 'clickable-row' })} />
      <Flex justify="space-between" align="center" className="table-footer"><Typography.Text type="secondary">显示 {devices.data ? `${((page - 1) * pageSize) + (devices.data.total ? 1 : 0)}–${Math.min(page * pageSize, devices.data.total)}` : '—'} / {number(devices.data?.total)} 条</Typography.Text><Pagination current={page} pageSize={pageSize} total={devices.data?.total ?? 0} showSizeChanger={false} showQuickJumper onChange={(nextPage) => updateParam('page', String(nextPage))} /></Flex>
    </Card>

    <Drawer open={Boolean(selectedId)} onClose={() => setSelectedId(undefined)} size="large" destroyOnClose title={detail.data ? `统一设备 · ${detail.data.device.assetNumber}` : '统一设备详情'}>
      {detail.isLoading && <Typography.Text type="secondary">正在读取身份、业务挂接和关系证据…</Typography.Text>}
      {detail.data && <Space orientation="vertical" size={18} style={{ width: '100%' }}>
        <Descriptions column={2} size="small" bordered items={[
          { label: '统一设备 ID', children: <Typography.Text copyable>{detail.data.device.unifiedDeviceId}</Typography.Text> },
          { label: '来源系统', children: detail.data.device.sourceSchema },
          { label: '电厂', children: display(detail.data.device.siteId) },
          { label: '源设备编码', children: <Typography.Text strong>{display(detail.data.device.assetNumber)}</Typography.Text> },
          { label: '统一名称', children: display(detail.data.device.canonicalName) },
          { label: '位置编码', children: display(detail.data.device.locationCode) },
          { label: '父设备编码', children: display(detail.data.device.parentAssetNumber) },
          { label: '分类', children: display(detail.data.device.classificationId) },
          { label: '身份键', children: <Typography.Text copyable>{detail.data.sourceIdentity.sourceIdentityKey}</Typography.Text> },
          { label: '对象状态', children: <Tag>{display(detail.data.device.status)}</Tag> },
        ]} />
        <Alert type="success" showIcon title="可追溯" description={`身份映射 ${detail.data.mappings.length} 条；业务挂接 ${detail.data.businessLinks.length} 条；设备关系 ${detail.data.relations.length} 条。此页面没有源库写入或正式发布动作。`} />
        <Divider titlePlacement="start">来源身份映射</Divider>
        <Table<UnifiedDeviceMapping> rowKey={(row) => `${row.source_schema}-${row.source_table}-${row.source_row_id}`} columns={mappingColumns} dataSource={detail.data.mappings} size="small" scroll={{ x: 1000 }} pagination={{ pageSize: 10, showSizeChanger: false }} />
        <Divider titlePlacement="start">巡检 / 缺陷 / 工单挂接</Divider>
        <Table<UnifiedBusinessLink> rowKey="link_id" columns={linkColumns} dataSource={detail.data.businessLinks} size="small" scroll={{ x: 1000 }} pagination={{ pageSize: 10, showSizeChanger: false }} />
        <Divider titlePlacement="start">业务记录原始证据</Divider>
        <Table<UnifiedBusinessRecordEvidence> rowKey="event_record_id" columns={evidenceColumns} dataSource={detail.data.businessRecordEvidence} size="small" scroll={{ x: 1050 }} pagination={{ pageSize: 10, showSizeChanger: false }} />
        <Divider titlePlacement="start">设备关系</Divider>
        <Table<UnifiedDeviceRelation> rowKey="relation_id" columns={relationColumns} dataSource={detail.data.relations} size="small" scroll={{ x: 900 }} pagination={{ pageSize: 10, showSizeChanger: false }} />
      </Space>}
    </Drawer>
  </div>
}
