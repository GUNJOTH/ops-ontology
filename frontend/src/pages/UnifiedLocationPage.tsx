import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { EnvironmentOutlined, SearchOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { Alert, Card, Col, Descriptions, Divider, Drawer, Flex, Input, Pagination, Row, Select, Space, Statistic, Table, Tag, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { getUnifiedLocation, getUnifiedLocationSummary, getUnifiedLocations } from '../api/client'
import type { UnifiedBusinessRecordEvidence, UnifiedLocation } from '../api/types'
import { PageHeader, ReadOnlyTag } from '../components/PageHeader'

const pageSize = 50

function display(value: unknown) {
  if (value === null || value === undefined || String(value).trim() === '') return '—'
  return String(value)
}

function number(value: number | undefined) {
  return (value ?? 0).toLocaleString()
}

export function UnifiedLocationPage() {
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState(params.get('search') ?? '')
  const [selectedId, setSelectedId] = useState<string>()
  const page = Number(params.get('page') ?? 1)
  const sourceSchema = params.get('source_schema') ?? 'all'
  const siteId = params.get('site_id') ?? ''
  const query = useMemo(() => ({ page, pageSize, search: params.get('search') ?? '', sourceSchema, siteId }), [page, params, siteId, sourceSchema])

  const summary = useQuery({ queryKey: ['unified-location-summary'], queryFn: getUnifiedLocationSummary })
  const locations = useQuery({ queryKey: ['unified-locations', query], queryFn: () => getUnifiedLocations(query) })
  const detail = useQuery({ queryKey: ['unified-location', selectedId], queryFn: () => getUnifiedLocation(selectedId as string), enabled: Boolean(selectedId) })

  const updateParam = (key: string, value?: string) => {
    const next = new URLSearchParams(params)
    if (value && value !== 'all') next.set(key, value); else next.delete(key)
    next.set('page', '1')
    setParams(next)
  }

  const rows = locations.data?.rows ?? []
  const columns: TableColumnsType<UnifiedLocation> = [
    { title: '来源系统', dataIndex: 'sourceSchema', width: 110, fixed: 'left', render: (value: string) => <Tag color={value === 'HD_SAAS' ? 'blue' : 'purple'}>{value}</Tag> },
    { title: '电厂', dataIndex: 'siteId', width: 105 },
    { title: '位置编码', dataIndex: 'locationCode', width: 175, render: (value: string) => <Typography.Text strong>{display(value)}</Typography.Text> },
    { title: '位置描述', dataIndex: 'description', width: 280, ellipsis: true, render: display },
    { title: '父级位置', dataIndex: 'parentLocation', width: 175, ellipsis: true, render: display },
    { title: '设备数', dataIndex: 'deviceCount', width: 90, render: (value: number) => number(value) },
    { title: '业务记录', dataIndex: 'businessRecordCount', width: 100, render: (value: number) => value ? <Tag color="green">{value}</Tag> : <Typography.Text type="secondary">0</Typography.Text> },
    { title: '状态', dataIndex: 'status', width: 120, render: (value: string) => <Tag>{display(value)}</Tag> },
  ]

  const businessColumns: TableColumnsType<UnifiedBusinessRecordEvidence> = [
    { title: '类型', dataIndex: 'event_type', width: 100 },
    { title: '源表', dataIndex: 'source_table', width: 150 },
    { title: '记录号', dataIndex: 'source_row_id', width: 180, ellipsis: true },
    { title: '描述', dataIndex: 'description', width: 280, ellipsis: true, render: display },
    { title: '设备对象', dataIndex: 'unified_device_id', width: 230, ellipsis: true, render: display },
    { title: '挂接状态', dataIndex: 'link_status', width: 150, render: (value: string) => <Tag color={value === 'accepted' ? 'green' : 'gold'}>{value}</Tag> },
  ]

  return <div>
    <PageHeader title="统一位置对象" description="位置是设备的上下文对象：通过来源系统、SITEID、LOCATION 和父级位置建立关系，不把位置文本拼接进设备名称。" extra={<Space><ReadOnlyTag /><Tag color="blue">只读快照</Tag></Space>} />
    {summary.isError && <Alert type="error" showIcon title="无法读取位置语义结果层" description="请确认身份快照中存在 function_location 和 location_hierarchy 关系表。" style={{ marginBottom: 16 }} />}
    {summary.data && <Alert type="info" showIcon icon={<SafetyCertificateOutlined />} title="位置对象来源可追溯" description={`当前有 ${number(summary.data.locationCount)} 个位置对象、${number(summary.data.hierarchyCount)} 条位置层级关系。位置层级缺失时不自动推断，业务记录只展示已有挂接证据。`} style={{ marginBottom: 16 }} />}
    <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title={<Space><EnvironmentOutlined />位置对象</Space>} value={summary.data?.locationCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title="位置层级关系" value={summary.data?.hierarchyCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title="HD 位置" value={summary.data?.sourceCounts.find((item) => item.sourceSchema === 'HD_SAAS')?.count ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card purple" variant="borderless"><Statistic title="XNY 位置" value={summary.data?.sourceCounts.find((item) => item.sourceSchema === 'XNY_SAAS')?.count ?? 0} /></Card></Col>
    </Row>
    <Card className="candidate-card" variant="borderless">
      <Flex justify="space-between" align="center" wrap gap={12} className="candidate-toolbar">
        <Input.Search allowClear prefix={<SearchOutlined />} placeholder="搜索位置编码、描述、父级位置" value={search} onChange={(event) => setSearch(event.target.value)} onSearch={(value) => updateParam('search', value.trim() || undefined)} style={{ width: 360 }} />
        <Space wrap><Select value={sourceSchema} onChange={(value) => updateParam('source_schema', value)} options={[{ value: 'all', label: '全部来源系统' }, { value: 'HD_SAAS', label: 'HD_SAAS' }, { value: 'XNY_SAAS', label: 'XNY_SAAS' }]} style={{ width: 160 }} /><Input placeholder="电厂 SITEID" value={siteId} allowClear onChange={(event) => { const next = new URLSearchParams(params); if (event.target.value) next.set('site_id', event.target.value); else next.delete('site_id'); next.set('page', '1'); setParams(next) }} style={{ width: 150 }} /></Space>
      </Flex>
      <Table<UnifiedLocation> rowKey="locationRecordId" loading={locations.isLoading} columns={columns} dataSource={rows} size="middle" scroll={{ x: 1200 }} pagination={false} onRow={(record) => ({ onClick: () => setSelectedId(record.locationRecordId), className: 'clickable-row' })} />
      <Flex justify="space-between" align="center" className="table-footer"><Typography.Text type="secondary">显示 {locations.data ? `${((page - 1) * pageSize) + (locations.data.total ? 1 : 0)}–${Math.min(page * pageSize, locations.data.total)}` : '—'} / {number(locations.data?.total)} 条</Typography.Text><Pagination current={page} pageSize={pageSize} total={locations.data?.total ?? 0} showSizeChanger={false} showQuickJumper onChange={(nextPage) => updateParam('page', String(nextPage))} /></Flex>
    </Card>
    <Drawer open={Boolean(selectedId)} onClose={() => setSelectedId(undefined)} size="large" destroyOnClose title={detail.data ? `位置 · ${detail.data.location.locationCode}` : '位置对象详情'}>
      {detail.isLoading && <Typography.Text type="secondary">正在读取位置层级、设备和业务记录…</Typography.Text>}
      {detail.data && <Space orientation="vertical" size={18} style={{ width: '100%' }}>
        <Descriptions column={2} size="small" bordered items={[
          { label: '位置编码', children: <Typography.Text copyable>{detail.data.location.locationCode}</Typography.Text> },
          { label: '来源系统', children: detail.data.location.sourceSchema },
          { label: '电厂', children: detail.data.location.siteId },
          { label: '位置描述', children: display(detail.data.location.description) },
          { label: '父级位置', children: display(detail.data.location.parentLocation) },
          { label: '源位置 ID', children: display(detail.data.location.sourceLocationId) },
          { label: '设备数', children: number(detail.data.location.deviceCount) },
          { label: '业务记录', children: number(detail.data.location.businessRecordCount) },
        ]} />
        <Divider titlePlacement="start">位置层级</Divider>
        <Typography.Paragraph className="audit-code">{detail.data.hierarchy.length ? JSON.stringify(detail.data.hierarchy, null, 2) : '当前没有位置层级证据'}</Typography.Paragraph>
        <Divider titlePlacement="start">该位置下的统一设备</Divider>
        <Table rowKey="unified_device_id" size="small" dataSource={detail.data.devices} pagination={{ pageSize: 10, showSizeChanger: false }} columns={[{ title: '统一设备 ID', dataIndex: 'unified_device_id', width: 230, ellipsis: true }, { title: '设备编码', dataIndex: 'asset_number', width: 150 }, { title: '设备名称', dataIndex: 'canonical_name', ellipsis: true }, { title: '父设备', dataIndex: 'parent_asset_number', width: 150, render: display }]} scroll={{ x: 700 }} />
        <Divider titlePlacement="start">该位置下的业务记录证据</Divider>
        <Table<UnifiedBusinessRecordEvidence> rowKey="event_record_id" size="small" dataSource={detail.data.businessRecords} pagination={{ pageSize: 10, showSizeChanger: false }} columns={businessColumns} scroll={{ x: 1000 }} />
      </Space>}
    </Drawer>
  </div>
}
