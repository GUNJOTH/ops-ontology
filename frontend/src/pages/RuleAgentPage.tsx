import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Badge, Button, Card, Col, Descriptions, Empty, List, Row, Space, Statistic, Tag, Typography, message } from 'antd'
import { BulbOutlined, PlayCircleOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { aiReviewRuleAgentProposals, confirmRuleAgentProposal, discoverRules, enableRuleAgentProposal, getLatestSemanticReasoning, getRuleAgentProfile, getRuleAgentProposals, getRuleAgentStatus, previewRuleAgentProposal, queueRuleAgentProposal, replayRuleAgentProposal, runSemanticReasoning } from '../api/client'
import type { RuleAgentProposal, SemanticReasoningItem } from '../api/types'
import { PageHeader, ReadOnlyTag } from '../components/PageHeader'

function riskTag(value: RuleAgentProposal['riskLevel']) {
  return <Tag color={value === 'low' ? 'green' : value === 'medium' ? 'orange' : 'red'}>{{ low: '低风险', medium: '中风险', high: '高风险' }[value]}</Tag>
}

const statusLabel: Record<string, string> = { draft: '草案', previewed: '已预览', replayed: '回放通过', confirmed: '已确认', enabled: '已启用', failed: '回放失败' }

function confirmationHint(status: RuleAgentProposal['status']) {
  if (status === 'enabled') return '已启用：人工确认已完成，下一步进入统一审批队列'
  if (status === 'confirmed') return '已人工确认：下一步启用规则'
  if (status === 'replayed') return '回放通过：现在可以人工确认'
  if (status === 'previewed') return '已生成预览：必须先完成回放'
  if (status === 'failed') return '回放失败：修正规则或重新生成预览'
  return '草案状态：先生成预览'
}

const reasoningDecisionLabel: Record<string, string> = { propose_rule: '建议生成规则', keep_original: '保留原文', needs_review: '需要复核' }

function reasoningDecisionTag(item: SemanticReasoningItem) {
  const color = item.decision === 'propose_rule' ? 'blue' : item.decision === 'keep_original' ? 'green' : 'orange'
  return <Tag color={color}>{reasoningDecisionLabel[item.decision] ?? item.decision}</Tag>
}

export function RuleAgentPage() {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<RuleAgentProposal>()
  const statusQuery = useQuery({ queryKey: ['rule-agent-status'], queryFn: getRuleAgentStatus, staleTime: 60_000 })
  const profileQuery = useQuery({ queryKey: ['rule-agent-profile'], queryFn: () => getRuleAgentProfile(120) })
  const proposalsQuery = useQuery({ queryKey: ['rule-agent-proposals'], queryFn: () => getRuleAgentProposals() })
  const reasoningQuery = useQuery({ queryKey: ['semantic-reasoning-latest'], queryFn: getLatestSemanticReasoning })
  const mutation = useMutation({
    mutationFn: () => discoverRules({ idempotencyKey: `rule-agent-discover-${profile?.batchId ?? 'current'}-${profile?.eligibleCount ?? 0}`, sampleSize: 120, note: '仅生成规则草案，不启用、不发布' }),
    onSuccess: (result) => { message.success(`规则发现完成：保留 ${result.proposalCount ?? result.proposals.length} 条，过滤 ${result.filterStats?.filteredProposalCount ?? 0} 条无本地命中或样本证据的草案`); void queryClient.invalidateQueries({ queryKey: ['rule-agent-proposals'] }); setSelected(result.proposals[0]) },
    onError: (error) => message.error(error instanceof Error ? error.message : '规则发现失败'),
  })
  const reasoningMutation = useMutation({
    mutationFn: () => runSemanticReasoning({ idempotencyKey: `semantic-reasoning-${profile?.batchId ?? 'current'}-${profile?.sampleCount ?? 200}`, sampleSize: 200, clusterLimit: 12, note: '只生成语义推理结果和规则草案，不自动启用或发布' }),
    onSuccess: (result) => { message.success(`语义推理完成：${result.items.length} 个簇有判断，未写源数据、未发布`); void queryClient.invalidateQueries({ queryKey: ['semantic-reasoning-latest'] }) },
    onError: (error) => message.error(error instanceof Error ? error.message : '语义推理失败'),
  })
  const actionMutation = useMutation({
    mutationFn: async ({ action, proposalId }: { action: 'preview' | 'replay' | 'confirm' | 'enable'; proposalId: string }) => {
      const payload = { idempotencyKey: `rule-agent-${action}-${proposalId}`, note: '规则发现智能体工作流操作' }
      if (action === 'preview') return previewRuleAgentProposal(proposalId, payload)
      if (action === 'replay') return replayRuleAgentProposal(proposalId, payload)
      if (action === 'confirm') return confirmRuleAgentProposal(proposalId, payload)
      return enableRuleAgentProposal(proposalId, payload)
    },
    onSuccess: (result) => { message.success(`操作完成：${statusLabel[result.proposal.status] ?? result.proposal.status}`); void queryClient.invalidateQueries({ queryKey: ['rule-agent-proposals'] }); setSelected(result.proposal) },
    onError: (error) => message.error(error instanceof Error ? error.message : '操作失败'),
  })
  const aiReviewMutation = useMutation({
    mutationFn: () => aiReviewRuleAgentProposals({ idempotencyKey: `rule-agent-ai-review-${profile?.batchId ?? 'current'}-${proposals.length}`, note: '智能体批量审核规则草案，仅输出建议，不直接启用或发布' }),
    onSuccess: (result) => { message.success(`AI审核完成：${result.acceptedCount} 条建议通过，${result.needsReviewCount} 条需复核`); void queryClient.invalidateQueries({ queryKey: ['rule-agent-proposals'] }) },
    onError: (error) => message.error(error instanceof Error ? error.message : 'AI审核失败'),
  })
  const queueMutation = useMutation({
    mutationFn: (proposalId: string) => queueRuleAgentProposal(proposalId, { idempotencyKey: `rule-agent-queue-${proposalId}`, note: '规则确认后进入统一审批队列' }),
    onSuccess: (result) => { message.success(`已进入统一审批队列：${result.appliedCount} 条`); void queryClient.invalidateQueries({ queryKey: ['rule-agent-proposals'] }) },
    onError: (error) => message.error(error instanceof Error ? error.message : '进入审批队列失败'),
  })
  const profile = profileQuery.data?.profile
  const agentConfigured = statusQuery.data?.configured === true
  const proposals = proposalsQuery.data?.proposals ?? []
  useEffect(() => {
    if (!selected && proposals.length) setSelected(proposals[0])
  }, [proposals, selected])

  return <div>
    <PageHeader title="规则发现智能体" description="分析当前高质量未发布数据，生成可回放的规则草案。智能体不启用规则、不修改源库、不发布结果。" extra={<Space><ReadOnlyTag /><Button type="primary" icon={<PlayCircleOutlined />} disabled={!agentConfigured} loading={mutation.isPending} onClick={() => mutation.mutate()}>开始发现规则</Button><Button disabled={!agentConfigured} loading={aiReviewMutation.isPending} onClick={() => aiReviewMutation.mutate()}>AI批量审核</Button></Space>} />
    {statusQuery.isLoading && <Alert type="info" showIcon message="正在检查智能体配置" description="配置检查与全量数据统计分开进行；检查完成后操作按钮即可使用。" style={{ marginBottom: 16 }} />}
    {statusQuery.isError && <Alert type="error" showIcon message="无法读取智能体运行状态" description="请确认后端已启动；操作未执行。" style={{ marginBottom: 16 }} />}
    {statusQuery.data && !agentConfigured && <Alert type="warning" showIcon message="尚未配置规则发现模型" description="请在后端进程环境变量中设置 RULE_AGENT_API_KEY、RULE_AGENT_BASE_URL 和 RULE_AGENT_MODEL；密钥不会写入项目文件。" style={{ marginBottom: 16 }} />}
    {statusQuery.data && agentConfigured && statusQuery.data.failedRunCount > 0 && <Alert type="warning" showIcon message={`智能体有 ${statusQuery.data.failedRunCount} 次历史失败，当前失败不会自动生成规则`} description={statusQuery.data.latestRun?.status === 'failed' ? `最近失败：${statusQuery.data.latestRun.error_code || 'unknown_agent_error'}；${statusQuery.data.latestRun.retryable ? '可重试' : '需先检查配置或模型返回'}。系统最多尝试 ${statusQuery.data.maxAttempts} 次，失败记录会保留。` : `当前调用超时 ${statusQuery.data.timeoutSeconds} 秒，最多 ${statusQuery.data.maxAttempts} 次尝试。`} style={{ marginBottom: 16 }} />}
    {agentConfigured && profileQuery.isLoading && <Alert type="info" showIcon message="正在读取当前批次证据" description="统计当前批次候选证据可能需要一些时间；不影响开始发现规则、AI 批量审核和语义推理按钮执行，执行期间会显示进度。" style={{ marginBottom: 16 }} />}
    <Alert type="info" showIcon icon={<SafetyCertificateOutlined />} message="规则发现与规则执行分离" description="智能体只负责发现和解释规则；规则草案必须经过预览、回放和人工确认后，才能注册并启用。" style={{ marginBottom: 16 }} />
    {profile && <Row gutter={16} style={{ marginBottom: 16 }}><Col xs={24} md={6}><Card><Statistic title="分析范围" value={profile.eligibleCount} suffix="条" /></Card></Col><Col xs={24} md={6}><Card><Statistic title="已有差异" value={profile.changedCount} suffix="条" /></Card></Col><Col xs={24} md={6}><Card><Statistic title="样本" value={profile.sampleCount} suffix="条" /></Card></Col><Col xs={24} md={6}><Card><Statistic title="涉及电厂" value={profile.sites.length} suffix="个" /></Card></Col></Row>}
    <Card
      title="语义推理智能体"
      style={{ marginBottom: 16 }}
      extra={<Button icon={<BulbOutlined />} loading={reasoningMutation.isPending} disabled={!agentConfigured} onClick={() => reasoningMutation.mutate()}>运行 200 条分层样本</Button>}
    >
      <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>智能体读取语义簇及 KKS、位置、分类上下文，只输出“建议生成规则 / 保留原文 / 需要复核”。结果不会自动进入规则草案、不会启用、不会发布。</Typography.Paragraph>
      {reasoningQuery.data?.run && <Typography.Text type="secondary">最近运行：{reasoningQuery.data.run.runId}，判断 {reasoningQuery.data.items.length} 个簇</Typography.Text>}
      {!!reasoningQuery.data?.items.length && <List size="small" dataSource={reasoningQuery.data.items.slice(0, 12)} renderItem={(item) => <List.Item><List.Item.Meta title={<Space>{item.clusterKey}{reasoningDecisionTag(item)}<Tag>{(item.confidence * 100).toFixed(0)}%</Tag></Space>} description={<Typography.Text type="secondary">{item.hypothesis || '无推理说明'}{item.candidateRule && Object.keys(item.candidateRule).length ? ` · 草案 ${JSON.stringify(item.candidateRule)}` : ''}</Typography.Text>} /></List.Item>} />}
      {!reasoningQuery.data?.items.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未运行语义推理" />}
    </Card>
    <Row gutter={16}>
      <Col xs={24} lg={10}><Card title={<Space><BulbOutlined />规则草案</Space>} extra={<Badge count={proposals.length} showZero />}>
        {proposals.length ? <List dataSource={proposals} renderItem={(item) => <List.Item onClick={() => setSelected(item)} style={{ cursor: 'pointer', background: selected?.proposalId === item.proposalId ? '#eff6ff' : undefined, padding: 12 }}><List.Item.Meta title={<Space>{item.title}{riskTag(item.riskLevel)}</Space>} description={<Space direction="vertical" size={2}><Typography.Text type="secondary">{item.ruleKey}</Typography.Text><Typography.Text type="secondary">预计影响 {item.expectedCount} 条 · 置信度 {(item.confidence * 100).toFixed(0)}%</Typography.Text></Space>} /></List.Item>} /> : <Empty description="尚未生成规则草案" />}
      </Card></Col>
      <Col xs={24} lg={14}><Card title="规则详情" extra={selected && <Space wrap>
        <Tag color={selected.status === 'enabled' ? 'green' : selected.status === 'replayed' ? 'blue' : selected.status === 'failed' ? 'red' : 'default'}>{confirmationHint(selected.status)}</Tag>
        <Button size="small" onClick={() => actionMutation.mutate({ action: 'preview', proposalId: selected.proposalId })} loading={actionMutation.isPending}>生成预览</Button>
        <Button size="small" disabled={!['previewed', 'replayed'].includes(selected.status)} onClick={() => actionMutation.mutate({ action: 'replay', proposalId: selected.proposalId })} loading={actionMutation.isPending}>回放</Button>
        <Button size="small" title={selected.status === 'enabled' ? '该规则已启用，人工确认已完成' : confirmationHint(selected.status)} disabled={selected.status !== 'replayed'} onClick={() => actionMutation.mutate({ action: 'confirm', proposalId: selected.proposalId })} loading={actionMutation.isPending}>人工确认</Button>
        <Button size="small" type="primary" disabled={selected.status !== 'confirmed'} onClick={() => actionMutation.mutate({ action: 'enable', proposalId: selected.proposalId })} loading={actionMutation.isPending}>启用规则</Button>
        <Button size="small" disabled={selected.status !== 'enabled'} onClick={() => queueMutation.mutate(selected.proposalId)} loading={queueMutation.isPending}>进入统一审批队列</Button>
      </Space>}>{selected ? <><Descriptions column={1} bordered size="small" items={[{ key: 'key', label: '规则键', children: selected.ruleKey }, { key: 'objective', label: '目标', children: selected.objective || '未说明' }, { key: 'operation', label: '操作', children: selected.operation }, { key: 'scope', label: '范围', children: JSON.stringify(selected.scope) }, { key: 'evidence', label: '证据', children: JSON.stringify(selected.evidence) }, { key: 'status', label: '状态', children: <Tag color={selected.status === 'failed' ? 'red' : 'blue'}>{statusLabel[selected.status] ?? selected.status}</Tag> }, { key: 'counts', label: '预览回放', children: `预览 ${selected.previewCount} 条；当前回放 ${selected.replayPassCount}/${selected.replayCount} 通过；历史评价 ${selected.evaluationPassCount}/${selected.evaluationCount} 通过` }]} /><Typography.Title level={5} style={{ marginTop: 16 }}>示例</Typography.Title><List size="small" dataSource={selected.examples} renderItem={(example) => <List.Item><Typography.Text>{example.before || '—'} → {example.after || '—'}</Typography.Text></List.Item>} /></> : <Empty description="选择一条规则查看详情" />}</Card></Col>
    </Row>
  </div>
}
