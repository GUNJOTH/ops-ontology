import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { DatabaseOutlined, DownloadOutlined, SearchOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Col, Descriptions, Drawer, Flex, Input, Pagination, Row, Select, Space, Statistic, Table, Tag, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { getMetadataCatalog, getMetadataCatalogDetail, getMetadataSummary, metadataExportUrl } from '../api/client'
import type { MetadataSemanticItem } from '../api/types'
import { PageHeader, ReadOnlyTag } from '../components/PageHeader'

const pageSize = 50

const conceptLabels: Record<string, string> = {
  object: '对象',
  table: '表',
  attribute: '属性',
  relationship: '关系',
  view: '视图',
  view_column: '视图字段',
  index: '索引',
  index_column: '索引字段',
}

const aiLabels: Record<string, string> = {
  system_configuration_difference: '系统配置差异',
  data_quality_or_missing_link: '数据质量/关联缺失',
  true_semantic_difference: '真正语义不同',
  needs_review: '需要复核',
  unclassified: '未分类',
}

function conceptLabel(value: string) {
  return conceptLabels[value] ?? value
}

function aiLabel(value: string) {
  return aiLabels[value] ?? value
}

function display(value: string | null | undefined) {
  return value?.trim() || '—'
}

export function MetadataPage() {
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState(params.get('search') ?? '')
  const [selectedId, setSelectedId] = useState<string>()
  const page = Number(params.get('page') ?? 1)
  const query = useMemo(() => ({
    page,
    pageSize,
    search: params.get('search') ?? '',
    conceptType: params.get('concept_type') ?? 'all',
    aiCategory: params.get('ai_category') ?? 'all',
    semanticStatus: params.get('semantic_status') ?? 'all',
    sourceSchema: params.get('source_schema') ?? 'all',
  }), [page, params])

  const summary = useQuery({ queryKey: ['metadata-summary'], queryFn: getMetadataSummary })
  const catalog = useQuery({ queryKey: ['metadata-catalog', query], queryFn: () => getMetadataCatalog(query) })
  const detail = useQuery({
    queryKey: ['metadata-detail', selectedId],
    queryFn: () => getMetadataCatalogDetail(selectedId as string),
    enabled: Boolean(selectedId),
  })

  const updateParam = (key: string, value?: string) => {
    const next = new URLSearchParams(params)
    if (value && value !== 'all') next.set(key, value); else next.delete(key)
    next.set('page', '1')
    setParams(next)
  }

  const summaryData = summary.data
  const conceptOptions = summaryData?.conceptTypes.map((item) => ({ value: item.value, label: `${conceptLabel(item.value)} (${item.count.toLocaleString()})` })) ?? []
  const aiOptions = summaryData?.aiCategories.map((item) => ({ value: item.value, label: `${aiLabel(item.value)} (${item.count.toLocaleString()})` })) ?? []
  const statusOptions = summaryData?.semanticStatuses.map((item) => ({ value: item.value, label: `${item.value} (${item.count.toLocaleString()})` })) ?? []

  const columns: TableColumnsType<MetadataSemanticItem> = [
    { title: '类型', dataIndex: 'conceptType', width: 100, fixed: 'left', render: (value: string) => <Tag color="blue">{conceptLabel(value)}</Tag> },
    { title: '规范名称', dataIndex: 'canonicalName', width: 190, render: (value: string) => <Typography.Text strong>{display(value)}</Typography.Text> },
    { title: '语义标签/说明', key: 'label', width: 260, render: (_, row) => <Space direction="vertical" size={0}><Typography.Text>{display(row.semanticLabelCandidate)}</Typography.Text><Typography.Text type="secondary" ellipsis>{display(row.description)}</Typography.Text></Space> },
    { title: '父表/父级', dataIndex: 'parentOrTable', width: 180, ellipsis: true, render: (value: string) => display(value) },
    { title: '来源', dataIndex: 'sourceSchemas', width: 160, ellipsis: true, render: (value: string) => display(value) },
    { title: '语义状态', dataIndex: 'semanticStatus', width: 120, render: (value: string) => <Tag color={value === 'aligned' ? 'green' : 'gold'}>{display(value)}</Tag> },
    { title: 'AI判断', dataIndex: 'aiCategory', width: 170, render: (value: string) => value ? <Tag>{aiLabel(value)}</Tag> : <Typography.Text type="secondary">—</Typography.Text> },
  ]

  return <div>
    <PageHeader title="元数据语义" description="统一查看 GRP 元数据的对象、表、属性、关系、视图和索引语义；当前页面只读本地结果层，不修改 DM8 源表。" extra={<><Tag color="blue">结果 {summaryData?.total.toLocaleString() ?? '—'} 条</Tag><ReadOnlyTag /></>} />

    {summary.isError && <Alert type="error" showIcon title="无法读取元数据语义结果层" description="请确认 FastAPI 已启动，且本地 metadata_semantics.sqlite3 已生成。" style={{ marginBottom: 16 }} />}
    {summaryData && <Alert type="info" showIcon icon={<SafetyCertificateOutlined />} title="只读结果层" description={`版本 ${summaryData.resultVersion}；已加载 ${summaryData.total.toLocaleString()} 条。DM8/GRP 源表不会被页面查询写入，当前正式发布标记为关闭。`} style={{ marginBottom: 16 }} />}

    <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title={<Space><DatabaseOutlined />全部语义</Space>} value={summaryData?.total ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title="对象/表" value={(summaryData?.conceptTypes.find((item) => item.value === 'object')?.count ?? 0) + (summaryData?.conceptTypes.find((item) => item.value === 'table')?.count ?? 0)} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title="属性/字段" value={(summaryData?.conceptTypes.find((item) => item.value === 'attribute')?.count ?? 0) + (summaryData?.conceptTypes.find((item) => item.value === 'view_column')?.count ?? 0)} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card purple" variant="borderless"><Statistic title="隔离明细" value={summaryData?.validation.findingCount ?? 0} /></Card></Col>
    </Row>

    <Card className="candidate-card" variant="borderless">
      <Flex justify="space-between" align="center" wrap gap={12} className="candidate-toolbar">
        <Input.Search allowClear prefix={<SearchOutlined />} placeholder="搜索名称、语义键、说明、父表" value={search} onChange={(event) => setSearch(event.target.value)} onSearch={(value) => updateParam('search', value.trim() || undefined)} style={{ width: 360 }} />
        <Space wrap>
          <Select allowClear placeholder="全部类型" value={query.conceptType === 'all' ? undefined : query.conceptType} onChange={(value) => updateParam('concept_type', value)} options={conceptOptions} style={{ width: 160 }} />
          <Select allowClear placeholder="AI判断" value={query.aiCategory === 'all' ? undefined : query.aiCategory} onChange={(value) => updateParam('ai_category', value)} options={aiOptions} style={{ width: 190 }} />
          <Select allowClear placeholder="语义状态" value={query.semanticStatus === 'all' ? undefined : query.semanticStatus} onChange={(value) => updateParam('semantic_status', value)} options={statusOptions} style={{ width: 150 }} />
          <Select allowClear placeholder="来源库" value={query.sourceSchema === 'all' ? undefined : query.sourceSchema} onChange={(value) => updateParam('source_schema', value)} options={[{ value: 'HD_SAAS', label: 'HD_SAAS' }, { value: 'XNY_SAAS', label: 'XNY_SAAS' }]} style={{ width: 130 }} />
          <Button icon={<DownloadOutlined />} href={metadataExportUrl(query)} download="metadata_semantic_dictionary.csv">导出</Button>
        </Space>
      </Flex>

      <Table<MetadataSemanticItem> rowKey="semanticId" loading={catalog.isLoading} columns={columns} dataSource={catalog.data?.items ?? []} size="middle" scroll={{ x: 1180 }} pagination={false} onRow={(record) => ({ onClick: () => setSelectedId(record.semanticId), className: 'clickable-row' })} />
      <Flex justify="space-between" align="center" className="table-footer"><Typography.Text type="secondary">显示 {catalog.data ? `${((page - 1) * pageSize) + 1}–${Math.min(page * pageSize, catalog.data.total)}` : '—'} / {catalog.data?.total.toLocaleString() ?? '—'} 条</Typography.Text><Pagination current={page} pageSize={pageSize} total={catalog.data?.total ?? 0} showSizeChanger={false} showQuickJumper onChange={(nextPage) => updateParam('page', String(nextPage))} /></Flex>
    </Card>

    <Drawer open={Boolean(selectedId)} onClose={() => setSelectedId(undefined)} size="large" destroyOnClose title={detail.data ? `${conceptLabel(detail.data.item.conceptType)} · ${detail.data.item.canonicalName}` : '元数据语义详情'}>
      {detail.data && <Space orientation="vertical" size={18} style={{ width: '100%' }}>
        <Descriptions column={2} size="small" bordered items={[
          { label: '语义类型', children: conceptLabel(detail.data.item.conceptType) },
          { label: '规范名称', children: display(detail.data.item.canonicalName) },
          { label: '语义键', children: <Typography.Text copyable>{detail.data.item.semanticKey}</Typography.Text> },
          { label: '父表/父级', children: display(detail.data.item.parentOrTable) },
          { label: '来源库', children: display(detail.data.item.sourceSchemas) },
          { label: '跨库状态', children: display(detail.data.item.crossSchemaStatus) },
          { label: '数据类型', children: display(detail.data.item.dataType) },
          { label: '长度', children: display(detail.data.item.length) },
          { label: '是否必填', children: display(detail.data.item.required) },
          { label: '域', children: display(detail.data.item.domainId) },
        ]} />
        <div><Typography.Text type="secondary">语义标签</Typography.Text><Typography.Title level={4} style={{ marginTop: 6 }}>{display(detail.data.item.semanticLabelCandidate)}</Typography.Title></div>
        <div><Typography.Text type="secondary">原始说明</Typography.Text><div className="source-description">{display(detail.data.item.description)}</div></div>
        <div><Typography.Text type="secondary">AI判断</Typography.Text><div style={{ marginTop: 8 }}><Space wrap><Tag>{aiLabel(detail.data.item.aiCategory || 'unclassified')}</Tag><Tag color="blue">置信度 {display(detail.data.item.aiConfidence)}</Tag></Space><Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>{display(detail.data.item.aiReason)}</Typography.Paragraph></div></div>
        <Typography.Paragraph className="audit-code">字典版本：{detail.data.item.dictionaryVersion}{'\n'}语义状态：{detail.data.item.semanticStatus}{'\n'}证据：{detail.data.item.evidence}{'\n'}结果层加载：{detail.data.item.loadedAt}</Typography.Paragraph>
      </Space>}
    </Drawer>
  </div>
}
