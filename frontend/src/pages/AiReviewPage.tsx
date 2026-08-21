import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Badge,
  Button,
  Card,
  Descriptions,
  Divider,
  Drawer,
  Flex,
  Input,
  Pagination,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { TableColumnsType } from 'antd'
import {
  getAiCluster,
  getAiClusters,
  getAiSample,
  runPendingAgentAudit,
  saveAiClusterDecision,
  saveAiSampleDecision,
} from '../api/client'
import type { AiClusterMember, AiClusterRow, AiSampleDecision, AiSampleRow, CandidateAgentAuditResult } from '../api/types'
import { PageHeader, ReadOnlyTag } from '../components/PageHeader'

const clusterPageSize = 25
const samplePageSize = 25
const decisionLabels: Record<AiSampleDecision, string> = {
  accept_candidate: '接受候选',
  keep_original: '保留原文',
  needs_review: '需要复核',
}
const clusterTypeLabels: Record<string, string> = {
  question_context: '问号上下文',
  leading_minus: '前导负号',
  terminal_hyphen: '末尾横线',
  other: '其他差异',
}
function decisionTag(decision: AiSampleDecision | null | undefined) {
  if (!decision) return <Tag>未决</Tag>
  const color = decision === 'accept_candidate' ? 'green' : decision === 'needs_review' ? 'orange' : 'blue'
  return <Tag color={color}>{decisionLabels[decision]}</Tag>
}

function shortId(value: string) {
  return value.replace(/^cluster-/, '').slice(0, 12)
}

function DiffText({ original, candidate }: { original: string; candidate: string }) {
  return (
    <Space direction="vertical" size={2}>
      <Typography.Text delete={false}>{original || '（空）'}</Typography.Text>
      <Typography.Text type="success">→ {candidate || '（空）'}</Typography.Text>
    </Space>
  )
}

function ClusterPanel() {
  const queryClient = useQueryClient()
  const [page, setPage] = useState(1)
  const [clusterType, setClusterType] = useState('all')
  const [siteId, setSiteId] = useState<string>()
  const [aiDecision, setAiDecision] = useState('all')
  const [decision, setDecision] = useState('all')
  const [selectedId, setSelectedId] = useState<string>()
  const [note, setNote] = useState('')

  const query = useQuery({
    queryKey: ['ai-clusters', page, clusterType, siteId, aiDecision, decision],
    queryFn: () => getAiClusters({ page, pageSize: clusterPageSize, clusterType, siteId, aiDecision, decision }),
  })
  const detailQuery = useQuery({
    queryKey: ['ai-cluster', selectedId],
    queryFn: () => getAiCluster(selectedId as string),
    enabled: Boolean(selectedId),
  })
  const mutation = useMutation({
    mutationFn: (value: AiSampleDecision) => saveAiClusterDecision({ clusterId: selectedId as string, decision: value, note }),
    onSuccess: (result) => {
      message.success(`已记录整簇决策：${result.memberCount} 条，实际新增 ${result.appliedCount} 条`)
      setNote('')
      void queryClient.invalidateQueries({ queryKey: ['ai-clusters'] })
      void queryClient.invalidateQueries({ queryKey: ['ai-cluster', selectedId] })
    },
    onError: () => message.error('整簇决策保存失败，未改变正式结果层'),
  })

  const resetPage = () => setPage(1)
  const submitClusterDecision = (value: AiSampleDecision) => {
    if (!selectedId || !detailQuery.data) return
    mutation.mutate(value)
  }

  const columns: TableColumnsType<AiClusterRow> = [
    {
      title: '语义簇',
      key: 'cluster',
      width: 220,
      fixed: 'left',
      render: (_, row) => (
        <Space direction="vertical" size={2}>
          <Space size={6}><Tag color="purple">{row.clusterLabel}</Tag><Typography.Text strong>{row.memberCount} 条</Typography.Text></Space>
          <Typography.Text type="secondary" copyable={{ text: row.clusterId }}>#{shortId(row.clusterId)}</Typography.Text>
          <Typography.Text type="secondary">{row.clusterPattern}</Typography.Text>
        </Space>
      ),
    },
    {
      title: 'AI 建议',
      key: 'ai',
      width: 220,
      render: (_, row) => (
        <Space direction="vertical" size={3}>
          {decisionTag(row.aiDecision)}
          <Typography.Text type="secondary">置信度 {(row.aiConfidence * 100).toFixed(0)}%</Typography.Text>
          <Typography.Text type="secondary" ellipsis={{ tooltip: row.aiReason }}>{row.aiReason}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '代表样本',
      key: 'sample',
      width: 360,
      render: (_, row) => <DiffText original={row.sample.originalDescription} candidate={row.sample.candidateDescription} />,
    },
    {
      title: '电厂 / 上下文',
      key: 'context',
      width: 260,
      render: (_, row) => (
        <Space direction="vertical" size={2}>
          <Space wrap size={[4, 4]}>{row.sites.map((site) => <Tag key={site.siteId}>{site.siteId} · {site.count}</Tag>)}</Space>
          <Typography.Text type="secondary">{row.sample.kks || '无位置编码'}</Typography.Text>
          <Typography.Text type="secondary">{row.sample.locationDescription || '无位置描述'} · {row.sample.classificationDescription || '无分类'}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '簇状态',
      key: 'status',
      width: 150,
      render: (_, row) => <Space direction="vertical" size={3}>{decisionTag(row.decision)}<Typography.Text type="secondary">待处理 {row.pendingCount} 条</Typography.Text></Space>,
    },
    {
      title: '操作',
      key: 'action',
      width: 110,
      fixed: 'right',
      render: (_, row) => <Button type="link" onClick={() => setSelectedId(row.clusterId)}>查看整簇</Button>,
    },
  ]

  const summary = query.data?.summary
  const detail = detailQuery.data
  return (
    <>
      <Alert
        type="info"
        showIcon
        message="先确认模式，再处理成员"
        description="同一规则簇只需确认一次。整簇决策会写入本地 AI 复核记录，并保留成员级审计；仍需回放测试和正式审批后才能发布。"
        style={{ marginBottom: 14 }}
      />
      <Flex gap={10} wrap style={{ marginBottom: 14 }}>
        <Select value={clusterType} onChange={(value) => { setClusterType(value); resetPage() }} style={{ width: 150 }} options={[{ value: 'all', label: '全部语义簇' }, ...Object.entries(clusterTypeLabels).map(([value, label]) => ({ value, label }))]} />
        <Select allowClear value={siteId} onChange={(value) => { setSiteId(value); resetPage() }} placeholder="全部电厂" style={{ width: 180 }} options={(query.data?.summary.sites ?? []).map((item) => ({ value: item.siteId, label: `${item.siteId} · ${item.count}` }))} />
        <Select value={aiDecision} onChange={(value) => { setAiDecision(value); resetPage() }} style={{ width: 160 }} options={[{ value: 'all', label: '全部 AI 建议' }, ...Object.entries(decisionLabels).map(([value, label]) => ({ value, label }))]} />
        <Select value={decision} onChange={(value) => { setDecision(value); resetPage() }} style={{ width: 150 }} options={[{ value: 'all', label: '全部状态' }, { value: 'pending', label: '待确认簇' }, ...Object.entries(decisionLabels).map(([value, label]) => ({ value, label }))]} />
        {summary && <Space wrap><Badge status="processing" text={`待处理 ${summary.pendingCandidateCount} 条`} /><Badge status="warning" text={`待确认 ${summary.pendingClusterCount} 簇`} /><Tag color="purple">共 {summary.clusterCount} 个模式</Tag><Tag color="green">接受建议 {summary.aiRecommendation.accept_candidate} 簇</Tag><Tag>保留原文 {summary.aiRecommendation.keep_original} 簇</Tag><Tag color="orange">需复核 {summary.aiRecommendation.needs_review} 簇</Tag></Space>}
      </Flex>
      {query.isError && <Alert type="error" showIcon message="语义簇加载失败" description="请重试；源库不会被修改。" style={{ marginBottom: 14 }} />}
      {query.isLoading && <Alert type="info" showIcon message="正在分析当前批次的语义簇" description="首次扫描会读取当前高质量候选并按描述差异聚类，完成后结果会缓存，后续筛选无需重复全量扫描。" style={{ marginBottom: 14 }} />}
      <Card bordered={false} bodyStyle={{ padding: 0 }}>
        <Table<AiClusterRow> rowKey="clusterId" loading={query.isLoading} locale={{ emptyText: query.isLoading ? '正在分析语义簇…' : query.isError ? '加载失败，请重试' : '当前没有待处理语义簇' }} columns={columns} dataSource={query.data?.rows ?? []} pagination={false} scroll={{ x: 1450 }} size="middle" />
        <Flex justify="space-between" align="center" style={{ padding: 14 }}><Typography.Text type="secondary">当前显示 {query.data?.rows.length ?? 0} / {query.data?.total ?? 0} 个簇</Typography.Text><Pagination current={page} pageSize={clusterPageSize} total={query.data?.total ?? 0} showSizeChanger={false} onChange={setPage} /></Flex>
      </Card>
      <Drawer title={detail ? `${detail.clusterLabel} · ${detail.memberCount} 条` : '语义簇详情'} width={920} open={Boolean(selectedId)} onClose={() => setSelectedId(undefined)}>
        {detail && <>
          <Space wrap style={{ marginBottom: 10 }}>{decisionTag(detail.aiDecision)}<Tag>置信度 {(detail.aiConfidence * 100).toFixed(0)}%</Tag>{decisionTag(detail.decision)}</Space>
          <Alert type="warning" showIcon message={detail.aiReason} description={`规则签名：${detail.ruleSignature}。当前操作只记录本地审核决策，不直接发布。`} />
          <Descriptions size="small" column={2} bordered style={{ marginTop: 14 }} items={[{ key: 'pattern', label: '簇模式', children: detail.clusterPattern }, { key: 'sites', label: '涉及电厂', children: detail.sites.map((site) => `${site.siteId} (${site.count})`).join('、') }, { key: 'sample', label: '代表资产', children: `${detail.sample.siteId} / ${detail.sample.assetNumber}` }, { key: 'context', label: '位置 / 分类', children: `${detail.sample.kks || '无'} / ${detail.sample.classificationDescription || '无'}` }]} />
          <Divider>改写前后样本</Divider>
          <Card size="small"><DiffText original={detail.sample.originalDescription} candidate={detail.sample.candidateDescription} /></Card>
          <Divider>成员（当前页 {detail.members.length} / {detail.total}）</Divider>
          <Table<AiClusterMember> rowKey="candidateId" size="small" pagination={false} scroll={{ x: 760 }} dataSource={detail.members} columns={[{ title: '设备', key: 'asset', render: (_, row) => <Space direction="vertical" size={0}><Typography.Text strong>{row.assetNumber}</Typography.Text><Typography.Text type="secondary">{row.siteId}</Typography.Text></Space> }, { title: '前后描述', key: 'diff', render: (_, row) => <DiffText original={row.originalDescription} candidate={row.candidateDescription} /> }, { title: 'KKS / 位置', key: 'context', render: (_, row) => <Typography.Text type="secondary">{row.kks || '无'} / {row.locationDescription || '无'}</Typography.Text> }, { title: '成员决策', dataIndex: 'memberDecision', render: (value) => decisionTag(value) }]} />
          <Input.TextArea value={note} onChange={(event) => setNote(event.target.value)} placeholder="可选：填写本次整簇确认说明" rows={2} style={{ marginTop: 14 }} />
          <Flex gap={8} wrap style={{ marginTop: 12 }}>
            <Button type="primary" loading={mutation.isPending && mutation.variables === 'accept_candidate'} disabled={Boolean(detail.decision)} onClick={() => submitClusterDecision('accept_candidate')}>整簇确认候选</Button>
            <Button loading={mutation.isPending && mutation.variables === 'keep_original'} disabled={Boolean(detail.decision)} onClick={() => submitClusterDecision('keep_original')}>整簇驳回候选，保留原文</Button>
            <Button loading={mutation.isPending && mutation.variables === 'needs_review'} disabled={Boolean(detail.decision)} onClick={() => submitClusterDecision('needs_review')}>整簇标记需复核</Button>
          </Flex>
        </>}
      </Drawer>
    </>
  )
}

function SamplePanel() {
  const [page, setPage] = useState(1)
  const [decision, setDecision] = useState('all')
  const [siteId, setSiteId] = useState<string>()
  const [saving, setSaving] = useState<string>()
  const query = useQuery({ queryKey: ['ai-sample', page, decision, siteId], queryFn: () => getAiSample({ page, pageSize: samplePageSize, decision, siteId }) })
  const submit = async (row: AiSampleRow, value: AiSampleDecision) => {
    setSaving(row.candidateId)
    try {
      await saveAiSampleDecision({ sampleId: row.sampleId, candidateId: row.candidateId, decision: value })
      message.success('已保存本地样本决策')
      await query.refetch()
    } catch { message.error('保存失败，未改变正式结果层') } finally { setSaving(undefined) }
  }
  const columns: TableColumnsType<AiSampleRow> = [
    { title: '设备', key: 'asset', width: 150, render: (_, row) => <Space direction="vertical" size={0}><Typography.Text strong>{row.assetNumber}</Typography.Text><Typography.Text type="secondary">{row.siteId}</Typography.Text></Space> },
    { title: '前后描述', key: 'diff', width: 360, render: (_, row) => <DiffText original={row.originalDescription} candidate={row.candidateDescription} /> },
    { title: 'AI 建议', key: 'ai', width: 140, render: (_, row) => <Space direction="vertical" size={2}><Typography.Text>{row.aiDecision || '未分类'}</Typography.Text><Typography.Text type="secondary">置信度 {row.aiConfidence}</Typography.Text></Space> },
    { title: '决策', key: 'decision', width: 330, render: (_, row) => <Space wrap>{(Object.keys(decisionLabels) as AiSampleDecision[]).map((value) => <Button key={value} size="small" type={row.decision === value ? 'primary' : 'default'} loading={saving === row.candidateId} onClick={() => submit(row, value)}>{decisionLabels[value]}</Button>)}</Space> },
  ]
  return <><Flex gap={10} wrap style={{ marginBottom: 14 }}><Select value={decision} onChange={(value) => { setDecision(value); setPage(1) }} style={{ width: 150 }} options={[{ value: 'all', label: '全部建议' }, ...Object.entries(decisionLabels).map(([value, label]) => ({ value, label }))]} /><Select allowClear value={siteId} onChange={(value) => { setSiteId(value); setPage(1) }} placeholder="全部电厂" style={{ width: 180 }} options={(query.data?.summary.sites ?? []).map((item) => ({ value: item.siteId, label: `${item.siteId} · ${item.count}` }))} /></Flex><Card bordered={false} bodyStyle={{ padding: 0 }}><Table<AiSampleRow> rowKey="candidateId" loading={query.isLoading} columns={columns} dataSource={query.data?.rows ?? []} pagination={false} scroll={{ x: 1050 }} /><Flex justify="space-between" align="center" style={{ padding: 14 }}><Typography.Text type="secondary">当前显示 {query.data?.rows.length ?? 0} / {query.data?.total ?? 0} 条样本</Typography.Text><Pagination current={page} pageSize={samplePageSize} total={query.data?.total ?? 0} showSizeChanger={false} onChange={setPage} /></Flex></Card></>
}

export function AiReviewPage() {
  const [view, setView] = useState<'clusters' | 'samples'>('clusters')
  const [agentResult, setAgentResult] = useState<CandidateAgentAuditResult>()
  const agentAuditMutation = useMutation({
    mutationFn: () => runPendingAgentAudit({ idempotencyKey: 'candidate-agent-review-current-batch', note: 'AI 辅助预审待处理记录；无风险保留原文建议通过，其余保持待复核' }),
    onSuccess: (result) => { setAgentResult(result); message.success(`智能体审核完成：通过 ${result.approvedCount} 条，待复核 ${result.needsReviewCount} 条`) },
    onError: (error) => message.error(error instanceof Error ? error.message : '智能体审核失败'),
  })
  return <div>
    <PageHeader title="AI 语义复核" description="从逐条确认升级为语义簇确认：先判断规则模式，再对成员回放；所有操作先落本地审核记录，不直接发布或回写 MaxiEAM。" extra={<><Tag color="purple">簇优先</Tag><ReadOnlyTag /></>} />
    <Segmented block value={view} onChange={(value) => setView(value as 'clusters' | 'samples')} options={[{ value: 'clusters', label: '按语义簇查看' }, { value: 'samples', label: '按样本行查看' }]} style={{ marginBottom: 14 }} />
    <Space style={{ marginBottom: 14 }}>
      <Button loading={agentAuditMutation.isPending} onClick={() => agentAuditMutation.mutate()}>智能体审核待处理</Button>
      {agentResult && <Tag color="green">最近通过 {agentResult.approvedCount} 条，待复核 {agentResult.needsReviewCount} 条</Tag>}
    </Space>
    {view === 'clusters' ? <ClusterPanel /> : <SamplePanel />}
  </div>
}
