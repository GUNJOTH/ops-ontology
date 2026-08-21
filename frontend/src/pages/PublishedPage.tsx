import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { DownloadOutlined, SearchOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Descriptions, Drawer, Flex, Input, Pagination, Select, Space, Table, Tag, Typography, message } from 'antd'
import type { TableColumnsType } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { getPublished, getPublishedDetail, publishedExportUrl } from '../api/client'
import type { PublishedRow } from '../api/types'
import { PageHeader, ReadOnlyTag } from '../components/PageHeader'

const pageSize = 50

export function PublishedPage() {
  const [params, setParams] = useSearchParams()
  const [detail, setDetail] = useState<PublishedRow | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const query = useMemo(() => ({
    page: Number(params.get('page') ?? 1),
    pageSize,
    search: params.get('search') ?? '',
    siteId: params.get('site_id') ?? undefined,
    rule: params.get('rule') ?? undefined,
  }), [params])
  const published = useQuery({ queryKey: ['published', query], queryFn: () => getPublished(query) })

  const updateParam = (key: string, value?: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value); else next.delete(key)
    next.set('page', '1')
    setParams(next)
  }

  const openDetail = async (row: PublishedRow) => {
    try {
      setDetail(await getPublishedDetail(row.publicationId))
      setDrawerOpen(true)
    } catch {
      message.error('无法读取正式结果详情，请稍后重试')
    }
  }

  const columns: TableColumnsType<PublishedRow> = [
    { title: '设备编码', dataIndex: 'assetNumber', fixed: 'left', width: 130, render: (value: string) => <Typography.Text strong>{value}</Typography.Text> },
    { title: '电厂', dataIndex: 'siteId', width: 100 },
    { title: '原始描述', dataIndex: 'originalDescription', width: 190, ellipsis: true },
    { title: '正式统一描述', dataIndex: 'finalDescription', width: 220, ellipsis: true, render: (value: string) => <Typography.Text type="success">{value}</Typography.Text> },
    { title: 'KKS / 位置', dataIndex: 'kks', width: 210, render: (value: string, row) => <Space direction="vertical" size={0}><Typography.Text>{value || '—'}</Typography.Text><Typography.Text type="secondary">{row.locationDescription || '—'}</Typography.Text></Space> },
    { title: '规则', dataIndex: 'appliedRules', width: 250, render: (rules: string[]) => <Space wrap>{rules.map((rule) => <Tag color="blue" key={rule}>{rule}</Tag>)}</Space> },
    { title: '发布时间', dataIndex: 'publishedAt', width: 180 },
  ]

  return <div>
    <PageHeader title="正式结果" description="已通过审批、规则回放和发布门禁的设备描述；源 MaxiEAM 保持只读。" extra={<><Tag color="green">{published.data?.total.toLocaleString() ?? '—'} 条</Tag><ReadOnlyTag /></>} />
    {published.isError && <Alert type="error" showIcon message="无法读取正式结果" description="请确认 FastAPI 已启动且 SQLite 正式结果层可读。" style={{ marginBottom: 14 }} />}
    <Card className="candidate-card" bordered={false}>
      <Flex justify="space-between" align="center" wrap gap={12} className="candidate-toolbar">
        <Input allowClear prefix={<SearchOutlined />} placeholder="搜索设备编码、原描述、正式描述、KKS" value={query.search} onChange={(event) => updateParam('search', event.target.value)} style={{ width: 390 }} />
        <Space wrap>
          <Select allowClear placeholder="全部电厂" value={query.siteId} onChange={(value) => updateParam('site_id', value)} options={(published.data?.sites ?? []).map((site) => ({ value: site.siteId, label: `${site.siteId} (${site.count.toLocaleString()})` }))} />
          <Select allowClear placeholder="全部规则" value={query.rule} onChange={(value) => updateParam('rule', value)} options={(published.data?.rules ?? []).map((item) => ({ value: item.rule, label: `${item.rule} (${item.count.toLocaleString()})` }))} />
          <Button icon={<DownloadOutlined />} href={publishedExportUrl(query)} download="published_descriptions.csv">导出当前筛选</Button>
        </Space>
      </Flex>
      <Table<PublishedRow> rowKey="publicationId" loading={published.isLoading} columns={columns} dataSource={published.data?.rows ?? []} size="middle" scroll={{ x: 1300 }} pagination={false} onRow={(record) => ({ onClick: () => openDetail(record), className: 'clickable-row' })} />
      <Flex justify="space-between" align="center" className="table-footer"><Typography.Text type="secondary">显示 {published.data ? `${((query.page - 1) * pageSize) + 1}–${Math.min(query.page * pageSize, published.data.total)}` : '—'} / {published.data?.total.toLocaleString() ?? '—'} 条</Typography.Text><Pagination current={query.page} pageSize={pageSize} total={published.data?.total ?? 0} showSizeChanger={false} onChange={(page) => { const next = new URLSearchParams(params); next.set('page', String(page)); setParams(next) }} showQuickJumper /></Flex>
    </Card>
    <Drawer open={drawerOpen} onClose={() => setDrawerOpen(false)} width={560} destroyOnClose title={detail ? `${detail.assetNumber} · 正式结果详情` : '正式结果详情'}>
      {detail && <Space direction="vertical" size={18} style={{ width: '100%' }}>
        <Descriptions column={2} size="small" bordered items={[{ label: 'SITEID', children: detail.siteId }, { label: 'ASSETNUM', children: detail.assetNumber }, { label: 'KKS', children: detail.kks || '—' }, { label: '分类', children: detail.classificationDescription || '—' }, { label: '位置', children: detail.locationDescription || '—' }, { label: '位置父级', children: detail.locationParent || '—' }]} />
        <div><Typography.Text type="secondary">原始描述</Typography.Text><div className="source-description">{detail.originalDescription || '—'}</div></div>
        <div><Typography.Text type="secondary">正式统一描述</Typography.Text><Typography.Title level={4} style={{ marginTop: 6 }}>{detail.finalDescription}</Typography.Title></div>
        <div><Typography.Text type="secondary">规则与审计</Typography.Text><div style={{ marginTop: 8 }}><Space wrap>{detail.appliedRules.map((rule) => <Tag color="blue" key={rule}>{rule}</Tag>)}<Tag>规则 {detail.ruleVersion}</Tag><Tag>验证器 {detail.validatorVersion}</Tag></Space></div></div>
        <Typography.Paragraph className="audit-code">发布批次：{detail.batchId}{'\n'}发布人：{detail.publishedBy}{'\n'}审批人：{detail.reviewer}{'\n'}审批凭据：{detail.approvalReceipt}{'\n'}回放：{detail.replayId}{'\n'}发布时间：{detail.publishedAt}</Typography.Paragraph>
      </Space>}
    </Drawer>
  </div>
}
