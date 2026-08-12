import { useState } from 'react'
import { Alert, Button, Divider, Flex, Form, Input, Space, Statistic, Tag, Typography } from 'antd'
import type { CandidateDetail, ReviewPayload } from '../api/types'
import { submitReview } from '../api/client'
import { StatusTag } from '../components/StatusTag'

export function ReviewDrawer({ detail, onCompleted }: { detail: CandidateDetail; onCompleted: () => void }) {
  const [description, setDescription] = useState(detail.candidateDescription)
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [message, setMessage] = useState<string>()

  const review = async (decision: ReviewPayload['decision']) => {
    if ((decision !== 'approved' && !note.trim()) || (decision === 'modified' && !description.trim())) {
      setMessage(decision === 'modified' ? '修改后通过需要填写统一描述；拒绝或待补证据需要填写说明。' : '请先填写审核说明。')
      return
    }
    setSubmitting(true); setMessage(undefined)
    try {
      await submitReview({ candidateId: detail.candidateId, decision, reviewedDescription: description, note, idempotencyKey: `${detail.candidateId}-${Date.now()}` })
      onCompleted()
    } catch {
      setMessage('审核接口暂不可用，当前未改变记录状态。请稍后重试。')
    } finally { setSubmitting(false) }
  }

  return <div className="review-drawer">
    <Flex justify="space-between" align="center"><Typography.Text type="secondary">当前审核状态</Typography.Text><StatusTag value={detail.reviewState} /></Flex>
    <Divider />
    <Typography.Title level={5}>设备身份</Typography.Title>
    <Flex gap={24} wrap><Statistic title="SITEID" value={detail.siteId} /><Statistic title="ASSETNUM" value={detail.assetNumber} /><Statistic title="ASSETID" value={detail.assetId} /></Flex>
    <Divider />
    <Typography.Title level={5}>描述对比</Typography.Title>
    <Typography.Text type="secondary">原始描述</Typography.Text><div className="source-description">{detail.originalDescription}</div>
    <Form layout="vertical"><Form.Item label="统一描述候选"><Input value={description} onChange={(event) => setDescription(event.target.value)} /></Form.Item><Form.Item label="审核说明"><Input.TextArea rows={3} value={note} onChange={(event) => setNote(event.target.value)} placeholder="修改、拒绝或待补证据时必填" /></Form.Item></Form>
    <Divider />
    <Typography.Title level={5}>上下文证据</Typography.Title>
    <div className="evidence-grid"><div><Typography.Text type="secondary">KKS</Typography.Text><div>{detail.kks}</div></div><div><Typography.Text type="secondary">位置</Typography.Text><div>{detail.locationDescription}</div></div><div><Typography.Text type="secondary">位置父级</Typography.Text><div>{detail.locationParent}</div></div><div><Typography.Text type="secondary">分类</Typography.Text><div>{detail.classificationDescription}</div></div><div><Typography.Text type="secondary">规格 / 特征</Typography.Text><div>{detail.specificationCount} / {detail.featureCount}</div></div><div><Typography.Text type="secondary">父子关系</Typography.Text><div>{detail.parentChildEvidence}</div></div></div>
    <Divider />
    <Typography.Title level={5}>规则审计</Typography.Title>
    <Space wrap>{detail.appliedRules.length ? detail.appliedRules.map((rule) => <Tag key={rule} color="blue">{rule}</Tag>) : <Tag>无自动规则</Tag>}<Tag>规则 {detail.ruleVersion}</Tag><Tag>验证器 {detail.validatorVersion}</Tag></Space>
    <Typography.Paragraph className="audit-code">源行哈希：{detail.sourceRowHash}{'\n'}上下文哈希：{detail.contextHash}{'\n'}证据级别：{detail.evidenceLevel}{'\n'}原因码：{detail.reasonCodes.join(', ')}</Typography.Paragraph>
    <Alert type="info" showIcon message="确认不会回写 MaxiEAM，仅写入审核记录。" />
    {message && <Alert type="warning" showIcon message={message} style={{ marginTop: 12 }} />}
    <Flex gap={8} wrap className="review-actions"><Button onClick={() => review('rejected')} disabled={submitting}>拒绝</Button><Button onClick={() => review('deferred')} disabled={submitting}>待补证据</Button><Button onClick={() => review('modified')} disabled={submitting}>修改后通过</Button><Button type="primary" onClick={() => review('approved')} loading={submitting}>通过并下一条 <span className="shortcut">Enter</span></Button></Flex>
  </div>
}
