import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ClearOutlined, ColumnHeightOutlined, DownloadOutlined, FilterOutlined, RobotOutlined, SearchOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Drawer, Flex, Input, InputNumber, Modal, Pagination, Select, Space, Table, Tag, Typography, message } from 'antd'
import type { TableColumnsType } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { getAgentAuditPreview, getAiReviewPreview, getCandidate, getCandidateFacets, getCandidates, runAiAutoApproval, runPendingAgentAudit, submitReview } from '../api/client'
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
  const [aiRunning, setAiRunning] = useState(false)
  const [agentRunning, setAgentRunning] = useState(false)
  const [agentBatchSize, setAgentBatchSize] = useState(100)
  const [searchDraft, setSearchDraft] = useState('')
  const [compactRows, setCompactRows] = useState(false)
  const query: CandidateQuery = useMemo(() => ({
    page: Number(params.get('page') ?? 1), pageSize, search: params.get('search') ?? '', siteId: params.get('site_id') ?? undefined,
    classification: params.get('classification') ?? undefined, quickFilter: (params.get('quick_filter') as CandidateQuery['quickFilter']) ?? 'all', sampleOnly: params.get('sample_only') === 'true', sampleSize: Number(params.get('sample_size') ?? 300),
  }), [params])
  const candidates = useQuery({ queryKey: ['candidates', query], queryFn: () => getCandidates(query) })
  const facets = useQuery({ queryKey: ['candidate-facets', query.quickFilter, query.sampleOnly, query.sampleSize], queryFn: () => getCandidateFacets(query), staleTime: 30_000 })
  const agentPreview = useQuery({ queryKey: ['ai-agent-preview', agentBatchSize], queryFn: () => getAgentAuditPreview(agentBatchSize), staleTime: 15_000 })
  useEffect(() => setSearchDraft(query.search ?? ''), [query.search])

  const openAiApproval = async () => {
    try {
      const preview = await getAiReviewPreview('sample')
      Modal.confirm({
        title: 'AI 辅助预审当前样本',
        content: <div><p>预计建议通过 <strong>{preview.eligibleCount.toLocaleString()}</strong> 条，剩余 {preview.pendingCount.toLocaleString()} 条继续人工审核。</p><p>策略：{preview.policyVersion}。仅写入本地审核记录，不回写 MaxiEAM；AI 不拥有正式审批或发布权限。</p></div>,
        okText: '执行辅助预审',
        cancelText: '取消',
        onOk: async () => {
          setAiRunning(true)
          try {
            const result = await runAiAutoApproval('sample', preview.batchId)
            message.success(`AI 辅助预审已记录 ${result.appliedCount.toLocaleString()} 条建议，${result.skippedCount.toLocaleString()} 条因并发变化跳过`)
            setSelectedRowKeys([])
            await candidates.refetch()
          } catch {
            message.error('AI 辅助预审失败，未改变当前页面数据')
          } finally { setAiRunning(false) }
        },
      })
    } catch { message.error('无法读取 AI 审批预估，请确认后端服务正常') }
  }

  const updateParam = (key: string, value?: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value); else next.delete(key)
    next.set('page', '1')
    setParams(next)
  }

  const openAgentAudit = () => {
    const preview = agentPreview.data
    if (!preview?.configured) {
      message.error('AI 审核智能体尚未配置，当前未改变任何候选状态')
      return
    }
    if (preview.eligibleCount === 0) {
      message.info('当前没有满足高质量门禁且尚未审阅的记录')
      return
    }
    Modal.confirm({
      title: `AI 审核下一批 ${preview.nextBatchSize.toLocaleString()} 条`,
      width: 560,
      content: <div><p>AI 将读取原描述、候选描述及 KKS/位置/分类等已有证据。</p><p><strong>只允许两种结果：</strong>高置信度保留原文并自动通过；其他情况自动隔离为“待补证据”，交给人工复核。</p><p>本次只处理下一批，不把全量数据发送给模型；不写 MaxiEAM，不正式发布。</p><Typography.Text type="secondary">策略：{preview.policyVersion} · 模型：{preview.model}</Typography.Text></div>,
      okText: '开始 AI 审核',
      cancelText: '取消',
      onOk: async () => {
        setAgentRunning(true)
        try {
          const result = await runPendingAgentAudit({ idempotencyKey: `candidate-agent-${preview.batchId}-${agentBatchSize}-${preview.eligibleCount}`, batchSize: agentBatchSize, note: '候选页 AI 辅助预审；非通过项自动隔离，等待人工复核。' })
          message.success(`AI 已处理 ${result.candidateCount.toLocaleString()} 条：自动通过 ${result.approvedCount.toLocaleString()} 条，隔离 ${result.isolatedCount.toLocaleString()} 条`)
          await Promise.all([candidates.refetch(), agentPreview.refetch()])
        } catch (error) {
          message.error(error instanceof Error ? error.message : 'AI 审核失败，未改变当前批次')
        } finally { setAgentRunning(false) }
      },
    })
  }

  const openDetail = async (row: CandidateRow) => {
    try {
      const selected = await getCandidate(row.candidateId)
      setDetail(selected); setDrawerOpen(true)
    } catch {
      message.error('无法读取设备详情，请稍后重试')
    }
  }

  const columns: TableColumnsType<CandidateRow> = [
    { title: '设备编码', dataIndex: 'assetNumber', fixed: 'left', width: 128, render: (value: string) => <Typography.Text strong>{value}</Typography.Text> },
    { title: '站点', dataIndex: 'siteId', width: 100 },
    { title: '原描述', dataIndex: 'originalDescription', width: 160, ellipsis: true },
    { title: '统一描述候选', dataIndex: 'candidateDescription', width: 180, ellipsis: true },
    { title: 'KKS / 位置', dataIndex: 'kks', width: 180, render: (value: string, row) => <Space direction="vertical" size={0}><Typography.Text>{value}</Typography.Text><Typography.Text type="secondary">{row.locationDescription}</Typography.Text></Space> },
    { title: '分类', dataIndex: 'classificationDescription', width: 100 },
    { title: '审核 / 校验', dataIndex: 'reviewState', width: 150, render: (_value, row) => <Space wrap size={4}><StatusTag value={row.reviewState} /><Tag color="blue">{row.validatorStatus}</Tag></Space> },
    { title: '更新时间', dataIndex: 'updatedAt', width: 150 },
  ]

  return <div>
    <PageHeader title={query.sampleOnly ? `${query.sampleSize} 条高质量样本` : '设备候选'} description={query.sampleOnly ? '按电厂与分类分层抽取的固定样本，只写本地审核记录；数量可调整为 1–5000 条' : '只处理当前高质量批次，搜索与筛选结果保存在 URL'} extra={<><Tag color="blue">{candidates.data?.total.toLocaleString() ?? '…'} 条</Tag>{query.sampleOnly && <Tag color="purple">高质量样本</Tag>}<ReadOnlyTag /></>} />
    {candidates.isError && <Alert type="error" showIcon message="无法读取候选数据" description="请确认 FastAPI 已启动；当前未使用 mock 数据替代真实结果。" style={{ marginBottom: 14 }} />}
    <Card bordered={false} style={{ marginBottom: 14 }} title={<Space><RobotOutlined />AI 辅助预审</Space>} extra={<Tag color={agentPreview.data?.configured ? 'green' : 'orange'}>{agentPreview.data?.configured ? '智能体已连接' : '未配置'}</Tag>}>
      <Flex justify="space-between" align="center" wrap gap={16}>
        <Space size="large" wrap>
          <Typography.Text>待审核 <strong>{agentPreview.data?.pendingCount.toLocaleString() ?? '…'}</strong></Typography.Text>
          <Typography.Text type="success">AI 可处理 <strong>{agentPreview.data?.eligibleCount.toLocaleString() ?? '…'}</strong></Typography.Text>
          <Typography.Text type="warning">已隔离 <strong>{agentPreview.data?.isolatedCount.toLocaleString() ?? '…'}</strong></Typography.Text>
        </Space>
        <Space><Typography.Text type="secondary">每批</Typography.Text><Select value={agentBatchSize} onChange={setAgentBatchSize} options={[50, 100, 200, 500].map((value) => ({ value, label: `${value} 条` }))} style={{ width: 100 }} /><Button type="primary" icon={<RobotOutlined />} loading={agentRunning} disabled={!agentPreview.data?.configured || !agentPreview.data?.eligibleCount} onClick={openAgentAudit}>AI 审核下一批</Button></Space>
      </Flex>
      <Typography.Paragraph type="secondary" style={{ margin: '10px 0 0' }}>AI 只生成保守的预审建议：高置信度建议保留原文；不确定/冲突自动隔离。正式审批、规则启用和发布仍需人工确认。</Typography.Paragraph>
    </Card>
    <Card className="candidate-card" bordered={false}>
      <Flex justify="space-between" align="center" wrap gap={12} className="candidate-toolbar">
        <Input allowClear prefix={<SearchOutlined />} placeholder="搜索 ASSETNUM、KKS 或原描述，回车应用" value={searchDraft} onChange={(event) => { setSearchDraft(event.target.value); if (!event.target.value) updateParam('search') }} onPressEnter={() => updateParam('search', searchDraft.trim() || undefined)} style={{ width: 360 }} />
        <Space wrap><Select allowClear placeholder="全部电厂" value={query.siteId} onChange={(value) => updateParam('site_id', value)} options={(facets.data?.sites ?? []).map((item) => ({ value: item.value, label: `${item.value} · ${item.count}` }))} /><Select allowClear placeholder="全部分类" value={query.classification} onChange={(value) => updateParam('classification', value)} options={(facets.data?.classifications ?? []).map((item) => ({ value: item.value, label: `${item.value} · ${item.count}` }))} /><Button icon={<ColumnHeightOutlined />} onClick={() => setCompactRows((value) => !value)}>密度：{compactRows ? '紧凑' : '标准'}</Button><Button icon={<DownloadOutlined />} onClick={() => { const fields = ['siteId', 'assetNumber', 'originalDescription', 'candidateDescription', 'kks', 'locationDescription', 'classificationDescription', 'reviewState']; const escape = (value: unknown) => `"${String(value ?? '').replaceAll('"', '""')}"`; const csv = [fields.join(','), ...(candidates.data?.rows ?? []).map((row) => fields.map((field) => escape(row[field as keyof typeof row])).join(','))].join('\n'); const url = URL.createObjectURL(new Blob([`\ufeff${csv}`], { type: 'text/csv;charset=utf-8' })); const link = document.createElement('a'); link.href = url; link.download = 'candidate-page.csv'; link.click(); URL.revokeObjectURL(url) }}>导出当前页</Button>{query.sampleOnly && <Button type="primary" ghost icon={<RobotOutlined />} loading={aiRunning} onClick={openAiApproval}>AI 辅助预审</Button>}</Space>
      </Flex>
      <Flex align="center" gap={8} wrap className="quick-filter-row"><Typography.Text type="secondary"><FilterOutlined /> 快捷筛选</Typography.Text>{(['all', 'pending', 'deferred', 'context', 'low'] as const).map((filter) => <Button key={filter} type={query.quickFilter === filter ? 'primary' : 'default'} size="small" onClick={() => updateParam('quick_filter', filter === 'all' ? undefined : filter)}>{({ all: '当前批次', pending: '待审核', deferred: 'AI 已隔离', context: '有上下文差异', low: '低置信度' })[filter]}</Button>)}<Button size="small" type={query.sampleOnly ? 'primary' : 'default'} onClick={() => updateParam('sample_only', query.sampleOnly ? undefined : 'true')}>{query.sampleSize} 条高质量样本</Button>{query.sampleOnly && <InputNumber min={1} max={5000} value={query.sampleSize} onChange={(value) => { const next = new URLSearchParams(params); next.set('sample_size', String(value ?? 300)); next.set('page', '1'); setParams(next) }} style={{ width: 100 }} />}{query.search && <Tag closable onClose={() => updateParam('search')}>搜索：{query.search}</Tag>}{query.siteId && <Tag closable onClose={() => updateParam('site_id')}>电厂：{query.siteId}</Tag>}<Button type="link" size="small" icon={<ClearOutlined />} onClick={() => setParams({ page: '1' })}>清除筛选</Button></Flex>
      {selectedRowKeys.length > 0 && <Flex justify="space-between" align="center" className="selection-bar"><Typography.Text>已选 <strong>{selectedRowKeys.length}</strong> 条 · 批量操作仅作用于当前页</Typography.Text><Space><Button size="small" onClick={() => setSelectedRowKeys([])}>取消选择</Button><Button size="small" danger onClick={async () => { try { await Promise.all(selectedRowKeys.map((candidateId) => submitReview({ candidateId: String(candidateId), decision: 'deferred', note: '人工批量标记待补证据', idempotencyKey: `candidate-defer-${String(candidateId)}` }))); message.success(`已隔离 ${selectedRowKeys.length} 条，等待补充证据`); setSelectedRowKeys([]); await candidates.refetch() } catch { message.error('批量隔离失败，已保留未完成记录') } }}>标记待补证据</Button></Space></Flex>}
      <Table<CandidateRow> rowKey="candidateId" loading={candidates.isLoading} columns={columns} dataSource={candidates.data?.rows ?? []} size={compactRows ? 'small' : 'middle'} scroll={{ x: 1180 }} pagination={false} rowSelection={{ selectedRowKeys, onChange: setSelectedRowKeys, getCheckboxProps: (record) => ({ disabled: record.reviewState !== 'pending' }) }} onRow={(record) => ({ onClick: () => openDetail(record), className: 'clickable-row' })} />
      <Flex justify="space-between" align="center" className="table-footer"><Typography.Text type="secondary">显示 {candidates.data ? `${((query.page - 1) * pageSize) + 1}–${Math.min(query.page * pageSize, candidates.data.total)}` : '…'} / {candidates.data?.total.toLocaleString() ?? '…'} 条</Typography.Text><Pagination current={query.page} pageSize={pageSize} total={candidates.data?.total ?? 0} showSizeChanger={false} onChange={(page) => { const next = new URLSearchParams(params); next.set('page', String(page)); setParams(next) }} showQuickJumper /></Flex>
    </Card>
    <Drawer open={drawerOpen} onClose={() => setDrawerOpen(false)} width={520} destroyOnClose title={detail ? `${detail.assetNumber} · 设备详情` : '设备详情'}>{detail && <ReviewDrawer detail={detail} onCompleted={() => { setDrawerOpen(false); candidates.refetch() }} />}</Drawer>
  </div>
}
