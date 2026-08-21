import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Badge, Button, Card, Descriptions, Drawer, Flex, Input, Modal, Pagination, Select, Space, Table, Tag, Typography, message } from 'antd'
import type { TableColumnsType } from 'antd'
import { REVIEWER_NAME, decideSemanticIdentityReview, getSemanticIdentityReviewDetail, getSemanticIdentityReviewQueue } from '../api/client'
import type { SemanticIdentityReviewRow } from '../api/types'
import { PageHeader, ReadOnlyTag } from '../components/PageHeader'

const pageSize = 25

function statusLabel(value: string) {
  return ({ pending: '待确认', approved: '已确认', rejected: '已驳回', blocked: '已隔离' } as Record<string, string>)[value] ?? value
}

export function IdentityReviewPage() {
  const [page, setPage] = useState(1)
  const [status, setStatus] = useState('pending')
  const [selectedId, setSelectedId] = useState<string>()
  const [decideTarget, setDecideTarget] = useState<{ row: SemanticIdentityReviewRow; decision: 'approved' | 'rejected' }>()
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const queue = useQuery({
    queryKey: ['semantic-identity-review', page, status],
    queryFn: () => getSemanticIdentityReviewQueue({ page, pageSize, status }),
  })
  const detail = useQuery({
    queryKey: ['semantic-identity-review-detail', selectedId],
    queryFn: () => getSemanticIdentityReviewDetail(selectedId as string),
    enabled: Boolean(selectedId),
  })

  const openDecide = (row: SemanticIdentityReviewRow, decision: 'approved' | 'rejected') => {
    setNote('')
    setDecideTarget({ row, decision })
  }

  const submitDecision = async () => {
    if (!decideTarget) return
    const { row, decision } = decideTarget
    setSubmitting(true)
    try {
      await decideSemanticIdentityReview(row.review_id, { decision, reviewer: REVIEWER_NAME, note, evidence: { source: 'identity-review-page', reviewId: row.review_id }, idempotencyKey: `identity-review-${row.review_id}-${decision}` })
      message.success(`${row.source_key} 已${decision === 'approved' ? '确认' : '驳回'}`)
      setDecideTarget(undefined)
      await queue.refetch()
      if (selectedId === row.review_id) await detail.refetch()
    } catch {
      message.error('身份裁决提交失败，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  const columns: TableColumnsType<SemanticIdentityReviewRow> = [
    { title: '来源身份', key: 'source', width: 280, render: (_, row) => <Space direction="vertical" size={2}><Typography.Text strong>{row.source_key}</Typography.Text><Typography.Text type="secondary">{row.source_schema} · {row.source_table} · {row.source_row_id}</Typography.Text></Space> },
    { title: '候选统一对象', key: 'target', width: 230, render: (_, row) => <Space direction="vertical" size={2}><Typography.Text>{row.canonical_object_type}</Typography.Text><Typography.Text type="secondary">{row.canonical_object_id}</Typography.Text></Space> },
    { title: '证据', key: 'evidence', width: 160, render: (_, row) => <Space wrap><Tag color={row.confidence >= 0.9 ? 'green' : 'orange'}>{(row.confidence * 100).toFixed(0)}%</Tag>{row.hasConflict && <Tag color="red">冲突</Tag>}</Space> },
    { title: '状态', dataIndex: 'queue_status', width: 110, render: (value) => <Tag color={value === 'pending' ? 'orange' : value === 'approved' ? 'green' : 'default'}>{statusLabel(value)}</Tag> },
    { title: '操作', key: 'action', width: 220, render: (_, row) => <Space><Button size="small" onClick={() => setSelectedId(row.review_id)}>详情</Button><Button size="small" type="primary" disabled={row.queue_status !== 'pending'} onClick={() => openDecide(row, 'approved')}>确认</Button><Button size="small" danger disabled={row.queue_status !== 'pending'} onClick={() => openDecide(row, 'rejected')}>驳回</Button></Space> },
  ]

  return <div>
    <PageHeader title="身份映射审批" description="确认来源记录与统一业务对象之间的本地映射；不写入 DM8、MaxiEAM、HD_SAAS 或 XNY_SAAS。" extra={<ReadOnlyTag />} />
    <Alert type="info" showIcon message="身份审批是独立治理门禁" description="只有明确身份、位置/KKS/业务编号等证据充分且无冲突的映射才能确认；不确定关系继续隔离。" style={{ marginBottom: 14 }} />
    {queue.isError && <Alert type="error" showIcon message="身份审批队列加载失败" description="请确认后端服务和本地统一语义库已启动。" style={{ marginBottom: 14 }} />}
    <Card bordered={false}>
      <Flex gap={12} align="center" style={{ marginBottom: 14 }}><Select value={status} onChange={(value) => { setStatus(value); setPage(1) }} style={{ width: 150 }} options={[{ value: 'pending', label: '待确认' }, { value: 'blocked', label: '已隔离' }, { value: 'approved', label: '已确认' }, { value: 'rejected', label: '已驳回' }, { value: 'all', label: '全部状态' }]} />{queue.data && <Space><Badge status="processing" text={`当前 ${queue.data.total} 条`} />{Object.entries(queue.data.counts).map(([key, count]) => <Tag key={key}>{statusLabel(key)} {count}</Tag>)}</Space>}</Flex>
      <Table<SemanticIdentityReviewRow> rowKey="review_id" loading={queue.isLoading} columns={columns} dataSource={queue.data?.rows ?? []} pagination={false} scroll={{ x: 1050 }} />
      <Flex justify="space-between" align="center" style={{ marginTop: 14 }}><Typography.Text type="secondary">显示 {queue.data?.rows.length ?? 0} / {queue.data?.total ?? 0} 条</Typography.Text><Pagination current={page} pageSize={pageSize} total={queue.data?.total ?? 0} showSizeChanger={false} onChange={setPage} /></Flex>
    </Card>
    <Drawer title="身份映射详情" width={760} open={Boolean(selectedId)} onClose={() => setSelectedId(undefined)}>
      {detail.data && <Space direction="vertical" style={{ width: '100%' }}><Descriptions bordered size="small" column={2} items={Object.entries(detail.data.review).filter(([key]) => !['evidence_json', 'assertion_evidence_json'].includes(key)).slice(0, 18).map(([key, value]) => ({ key, label: key, children: String(value ?? '') }))} /><Typography.Title level={5}>冲突证据</Typography.Title>{detail.data.conflicts.length ? detail.data.conflicts.map((item, index) => <Alert key={index} type="warning" showIcon message={JSON.stringify(item)} />) : <Typography.Text type="secondary">未发现隔离冲突</Typography.Text>}<Typography.Title level={5}>审核审计</Typography.Title>{detail.data.audits.map((item, index) => <Typography.Paragraph key={index}>{JSON.stringify(item)}</Typography.Paragraph>)}</Space>}
    </Drawer>
    <Modal open={Boolean(decideTarget)} title={decideTarget ? (decideTarget.decision === 'approved' ? '确认身份映射' : '驳回身份映射') : ''} width={520} okText={decideTarget ? (decideTarget.decision === 'approved' ? '确认身份映射' : '驳回身份映射') : '确认'} cancelText="取消" confirmLoading={submitting} onOk={submitDecision} onCancel={() => setDecideTarget(undefined)}>
      {decideTarget && <Space direction="vertical" style={{ width: '100%' }}><Typography.Text>来源：{decideTarget.row.source_schema} / {decideTarget.row.source_table} / {decideTarget.row.source_row_id}</Typography.Text><Typography.Text>目标：{decideTarget.row.canonical_object_type} / {decideTarget.row.canonical_object_id}</Typography.Text>{decideTarget.row.hasConflict && <Alert type="warning" showIcon message="该断言属于冲突组，后端会阻止直接确认" />}<Input.TextArea rows={3} value={note} onChange={(event) => setNote(event.target.value)} placeholder="填写审核依据（建议说明 KKS、位置层级或业务编号证据）" /></Space>}
    </Modal>
  </div>
}
