import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ClearOutlined, ColumnHeightOutlined, DownloadOutlined, FilterOutlined, SearchOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Drawer, Flex, Input, Pagination, Select, Space, Table, Tag, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { getCandidate, getCandidates } from '../api/client'
import type { CandidateDetail, CandidateQuery, CandidateRow } from '../api/types'
import { PageHeader, ReadOnlyTag } from '../components/PageHeader'
import { StatusTag } from '../components/StatusTag'
import { ReviewDrawer } from './ReviewDrawer'

const pageSize = 50

export function CandidatePage() {
  const [params, setParams] = useSearchParams()
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([])
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [detail, setDetail] = useState<CandidateDetail | null>(null)
  const query: CandidateQuery = useMemo(() => ({
    page: Number(params.get('page') ?? 1), pageSize, search: params.get('search') ?? '', siteId: params.get('site_id') ?? undefined,
    classification: params.get('classification') ?? undefined, quickFilter: (params.get('quick_filter') as CandidateQuery['quickFilter']) ?? 'all', sampleOnly: params.get('sample_only') === 'true',
  }), [params])
  const candidates = useQuery({ queryKey: ['candidates', query], queryFn: () => getCandidates(query) })

  const updateParam = (key: string, value?: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value); else next.delete(key)
    next.set('page', '1')
    setParams(next)
  }

  const openDetail = async (row: CandidateRow) => {
    const selected = await getCandidate(row.candidateId)
    setDetail(selected); setDrawerOpen(true)
  }

  const columns: TableColumnsType<CandidateRow> = [
    { title: '设备编码', dataIndex: 'assetNumber', fixed: 'left', width: 128, render: (value: string) => <Typography.Text strong>{value}</Typography.Text> },
    { title: '站点', dataIndex: 'siteId', width: 100 },
    { title: '原描述', dataIndex: 'originalDescription', width: 160, ellipsis: true },
    { title: '统一描述候选', dataIndex: 'candidateDescription', width: 180, ellipsis: true },
    { title: 'KKS / 位置', dataIndex: 'kks', width: 180, render: (value: string, row) => <Space direction="vertical" size={0}><Typography.Text>{value}</Typography.Text><Typography.Text type="secondary">{row.locationDescription}</Typography.Text></Space> },
    { title: '分类', dataIndex: 'classificationDescription', width: 100 },
    { title: '状态', dataIndex: 'validatorStatus', width: 110, render: (value) => <StatusTag value={value} /> },
    { title: '更新时间', dataIndex: 'updatedAt', width: 150 },
  ]

  return <div>
    <PageHeader title={query.sampleOnly ? '300 条样本审核' : '设备候选'} description={query.sampleOnly ? '按电厂与分类分层抽取的固定样本，只写本地审核记录' : '只处理当前高质量批次，搜索与筛选结果保存在 URL'} extra={<><Tag color="blue">{candidates.data?.total.toLocaleString() ?? '…'} 条</Tag>{query.sampleOnly && <Tag color="purple">样本范围</Tag>}<ReadOnlyTag /></>} />
    {candidates.isError && <Alert type="error" showIcon message="无法读取候选数据" description="请确认 FastAPI 已启动；当前未使用 mock 数据替代真实结果。" style={{ marginBottom: 14 }} />}
    <Card className="candidate-card" bordered={false}>
      <Flex justify="space-between" align="center" wrap gap={12} className="candidate-toolbar">
        <Input allowClear prefix={<SearchOutlined />} placeholder="搜索 ASSETNUM、KKS 或原描述" value={query.search} onChange={(event) => updateParam('search', event.target.value)} style={{ width: 360 }} />
        <Space wrap><Select allowClear placeholder="全部电厂" value={query.siteId} onChange={(value) => updateParam('site_id', value)} options={[{ value: 'ZJFD000', label: 'ZJFD000' }, { value: 'XZHR200', label: 'XZHR200' }, { value: 'HRCS000', label: 'HRCS000' }]} /><Select allowClear placeholder="全部分类" value={query.classification} onChange={(value) => updateParam('classification', value)} options={[{ value: '泵', label: '泵' }, { value: '阀门', label: '阀门' }, { value: '电动机', label: '电动机' }]} /><Button icon={<ColumnHeightOutlined />}>密度</Button><Button icon={<DownloadOutlined />}>导出当前页</Button></Space>
      </Flex>
      <Flex align="center" gap={8} wrap className="quick-filter-row"><Typography.Text type="secondary"><FilterOutlined /> 快捷筛选</Typography.Text>{(['all', 'pending', 'context', 'low'] as const).map((filter) => <Button key={filter} type={query.quickFilter === filter ? 'primary' : 'default'} size="small" onClick={() => updateParam('quick_filter', filter === 'all' ? undefined : filter)}>{({ all: '当前批次', pending: '待审核', context: '有上下文差异', low: '低置信度' })[filter]}</Button>)}<Button size="small" type={query.sampleOnly ? 'primary' : 'default'} onClick={() => updateParam('sample_only', query.sampleOnly ? undefined : 'true')}>300 条样本</Button>{query.search && <Tag closable onClose={() => updateParam('search')}>搜索：{query.search}</Tag>}{query.siteId && <Tag closable onClose={() => updateParam('site_id')}>电厂：{query.siteId}</Tag>}<Button type="link" size="small" icon={<ClearOutlined />} onClick={() => setParams({ page: '1' })}>清除筛选</Button></Flex>
      {selectedRowKeys.length > 0 && <Flex justify="space-between" align="center" className="selection-bar"><Typography.Text>已选 <strong>{selectedRowKeys.length}</strong> 条 · 批量操作仅作用于当前页</Typography.Text><Space><Button size="small" onClick={() => setSelectedRowKeys([])}>取消选择</Button><Button size="small" danger>标记待补证据</Button></Space></Flex>}
      <Table<CandidateRow> rowKey="candidateId" loading={candidates.isLoading} columns={columns} dataSource={candidates.data?.rows ?? []} size="middle" scroll={{ x: 1180 }} pagination={false} rowSelection={{ selectedRowKeys, onChange: setSelectedRowKeys, getCheckboxProps: (record) => ({ disabled: record.reviewState !== 'pending' }) }} onRow={(record) => ({ onClick: () => openDetail(record), className: 'clickable-row' })} />
      <Flex justify="space-between" align="center" className="table-footer"><Typography.Text type="secondary">显示 {candidates.data ? `${((query.page - 1) * pageSize) + 1}–${Math.min(query.page * pageSize, candidates.data.total)}` : '…'} / {candidates.data?.total.toLocaleString() ?? '…'} 条</Typography.Text><Pagination current={query.page} pageSize={pageSize} total={candidates.data?.total ?? 0} showSizeChanger={false} onChange={(page) => { const next = new URLSearchParams(params); next.set('page', String(page)); setParams(next) }} showQuickJumper /></Flex>
    </Card>
    <Drawer open={drawerOpen} onClose={() => setDrawerOpen(false)} width={520} destroyOnClose title={detail ? `${detail.assetNumber} · 设备详情` : '设备详情'}>{detail && <ReviewDrawer detail={detail} onCompleted={() => { setDrawerOpen(false); candidates.refetch() }} />}</Drawer>
  </div>
}
