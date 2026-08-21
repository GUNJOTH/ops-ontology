import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BookOutlined, SearchOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Col, Descriptions, Divider, Drawer, Flex, Input, Pagination, Row, Select, Space, Statistic, Table, Tag, Typography, message } from 'antd'
import type { TableColumnsType } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { getKnowledgeAsset, getKnowledgeAssetSummary, getKnowledgeAssets, previewSemanticExecution } from '../api/client'
import type { KnowledgeAssetDetail, KnowledgeAssetItem } from '../api/types'
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
  if (value === 'enabled' || value === 'approved') return 'green'
  if (value === 'blocked' || value === 'retired') return 'red'
  if (value === 'proposed' || value === 'needs_review') return 'gold'
  return 'blue'
}

function assetTypeLabel(value: string) {
  return ({ rule: '规则', terminology: '术语', evaluation_case: '评估用例', definition: '定义' } as Record<string, string>)[value] ?? value
}

function prettyJson(value: unknown) {
  if (typeof value !== 'string') return display(value)
  try { return JSON.stringify(JSON.parse(value), null, 2) } catch { return value }
}

export function KnowledgeAssetPage() {
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState(params.get('search') ?? '')
  const [selectedId, setSelectedId] = useState<string>()
  const [previewing, setPreviewing] = useState(false)
  const [executionPreview, setExecutionPreview] = useState<import('../api/types').SemanticExecutionPreview>()
  const page = Number(params.get('page') ?? 1)
  const assetType = params.get('asset_type') ?? 'all'
  const status = params.get('status') ?? 'all'
  const query = useMemo(() => ({ page, pageSize, search: params.get('search') ?? '', assetType, status }), [assetType, page, params, status])
  const summary = useQuery({ queryKey: ['knowledge-asset-summary'], queryFn: getKnowledgeAssetSummary })
  const assets = useQuery({ queryKey: ['knowledge-assets', query], queryFn: () => getKnowledgeAssets(query) })
  const detail = useQuery({ queryKey: ['knowledge-asset', selectedId], queryFn: () => getKnowledgeAsset(selectedId as string), enabled: Boolean(selectedId) })

  const updateParam = (key: string, value?: string) => {
    const next = new URLSearchParams(params)
    if (value && value !== 'all') next.set(key, value); else next.delete(key)
    next.set('page', '1')
    setParams(next)
  }

  const summaryData = summary.data
  const rows = assets.data?.items ?? []
  const columns: TableColumnsType<KnowledgeAssetItem> = [
    { title: '类型', dataIndex: 'assetType', width: 115, fixed: 'left', render: (value: string) => <Tag color="blue">{assetTypeLabel(value)}</Tag> },
    { title: '知识身份', dataIndex: 'assetKey', width: 300, ellipsis: true, render: (value: string) => <Typography.Text strong copyable={{ text: value }}>{value}</Typography.Text> },
    { title: '标题/规范定义', key: 'title', width: 280, ellipsis: true, render: (_, row) => <Space direction="vertical" size={0}><Typography.Text>{display(row.title)}</Typography.Text><Typography.Text type="secondary" ellipsis>{display(row.canonicalDefinition)}</Typography.Text></Space> },
    { title: '当前版本', dataIndex: 'currentVersion', width: 220, ellipsis: true },
    { title: '状态', dataIndex: 'status', width: 120, render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> },
    { title: '来源', dataIndex: 'sourceCount', width: 80, render: (value: number) => number(value) },
    { title: '问题', dataIndex: 'issueCount', width: 80, render: (value: number) => value ? <Tag color="orange">{value}</Tag> : <Typography.Text type="secondary">0</Typography.Text> },
  ]

  const d = detail.data
  const previewExecution = async () => {
    if (!d) return
    setPreviewing(true)
    try {
      const result = await previewSemanticExecution({
        assetId: d.asset.assetId,
        assetVersionId: d.machineContract?.asset_version_id ? String(d.machineContract.asset_version_id) : undefined,
        sampleSize: 200,
        idempotencyKey: `semantic-execution-preview-${d.asset.assetId}-${d.asset.currentVersion}`,
        note: '只生成机器语义执行预演，不执行源写入或正式发布',
      })
      setExecutionPreview(result)
      message.info(`执行预演状态：${result.status}`)
    } catch (error) {
      message.error(error instanceof Error ? error.message : '执行预演失败')
    } finally {
      setPreviewing(false)
    }
  }
  return <div>
    <PageHeader title="知识资产" description="统一查看规则、术语和评估用例的唯一身份、版本、来源、条件、Action、差异和审核状态；当前仅操作本地关系层。" extra={<Space><ReadOnlyTag /><Tag color="blue">知识身份 v1</Tag></Space>} />
    {summary.isError && <Alert type="error" showIcon title="无法读取知识资产层" description="请先运行本地知识身份构建脚本。" style={{ marginBottom: 16 }} />}
    {summaryData && <Alert type="info" showIcon icon={<SafetyCertificateOutlined />} title="知识整合规则" description={`同一规则或术语通过 asset_key 归并；来源版本分别保留；重复、冲突和缺失只标记，不静默覆盖，也不直接启用。当前已生成 ${number(summaryData.machineContract ? Number(summaryData.machineContract.contract_count ?? 0) : 0)} 份机器语义契约，约束门禁 ${number(summaryData.machineConstraintGates.reduce((sum, item) => sum + item.count, 0))} 条。`} style={{ marginBottom: 16 }} />}
    <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title={<Space><BookOutlined />知识资产</Space>} value={summaryData?.assetCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title="版本" value={summaryData?.versionCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card" variant="borderless"><Statistic title="来源证据" value={summaryData?.sourceCount ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card className="metric-card purple" variant="borderless"><Statistic title="开放问题" value={summaryData?.issueCount ?? 0} /></Card></Col>
    </Row>
    {summaryData && <Card size="small" style={{ marginBottom: 16 }} title={<Space><BookOutlined />业务对象目录</Space>} extra={<Typography.Text type="secondary">关系 {number(summaryData.businessObjectRelationCount)} 条</Typography.Text>}>
      <Space wrap>{summaryData.businessObjectTypes.map((item) => <Tag key={item.object_type} color={item.parent_object_type ? 'blue' : 'purple'}>{item.parent_object_type ? `${item.parent_object_type} → ` : ''}{item.display_name}</Tag>)}</Space>
    </Card>}
    <Card className="candidate-card" variant="borderless">
      <Flex justify="space-between" align="center" wrap gap={12} className="candidate-toolbar">
        <Input.Search allowClear prefix={<SearchOutlined />} placeholder="搜索资产身份、标题、版本或定义" value={search} onChange={(event) => setSearch(event.target.value)} onSearch={(value) => updateParam('search', value.trim() || undefined)} style={{ width: 360 }} />
        <Space wrap><Select value={assetType} onChange={(value) => updateParam('asset_type', value)} options={[{ value: 'all', label: '全部类型' }, { value: 'rule', label: '规则' }, { value: 'terminology', label: '术语' }, { value: 'evaluation_case', label: '评估用例' }]} style={{ width: 150 }} /><Select value={status} onChange={(value) => updateParam('status', value)} options={[{ value: 'all', label: '全部状态' }, ...(summaryData?.statuses.map((item) => ({ value: item.value, label: item.value })) ?? [])]} style={{ width: 150 }} /></Space>
      </Flex>
      <Table<KnowledgeAssetItem> rowKey="assetId" loading={assets.isLoading} columns={columns} dataSource={rows} size="middle" scroll={{ x: 1250 }} pagination={false} onRow={(record) => ({ onClick: () => setSelectedId(record.assetId), className: 'clickable-row' })} />
      <Flex justify="space-between" align="center" className="table-footer"><Typography.Text type="secondary">显示 {assets.data ? `${((page - 1) * pageSize) + (assets.data.total ? 1 : 0)}–${Math.min(page * pageSize, assets.data.total)}` : '—'} / {number(assets.data?.total)} 条</Typography.Text><Pagination current={page} pageSize={pageSize} total={assets.data?.total ?? 0} showSizeChanger={false} showQuickJumper onChange={(nextPage) => updateParam('page', String(nextPage))} /></Flex>
    </Card>
    <Drawer open={Boolean(selectedId)} onClose={() => setSelectedId(undefined)} size="large" destroyOnClose title={d ? `${assetTypeLabel(d.asset.assetType)} · ${d.asset.assetKey}` : '知识资产详情'}>
      {detail.isLoading && <Typography.Text type="secondary">正在读取版本、来源、部件和问题…</Typography.Text>}
      {d && <Space orientation="vertical" size={18} style={{ width: '100%' }}>
        <Descriptions column={2} size="small" bordered items={[
          { label: '知识身份', children: <Typography.Text copyable>{d.asset.assetKey}</Typography.Text> },
          { label: '资产类型', children: assetTypeLabel(d.asset.assetType) },
          { label: '标题', children: display(d.asset.title) },
          { label: '当前版本', children: display(d.asset.currentVersion) },
          { label: '状态', children: <Tag color={statusColor(d.asset.status)}>{d.asset.status}</Tag> },
          { label: '适用范围', children: display(d.asset.sourceScope) },
          { label: '来源数', children: number(d.asset.sourceCount) },
          { label: '开放问题', children: number(d.asset.issueCount) },
        ]} />
        {d.machineContract && <Flex align="center" gap={12}><Alert style={{ flex: 1 }} type="success" showIcon title="机器可执行契约已生成" description={`可判断、可计算、可约束、可行动；确定性 ${d.machineContract.deterministic ? '是' : '否'}；需要审批 ${d.machineContract.requires_approval ? '是' : '否'}。当前动作只允许写本地候选/回放结果，不允许写源表。`} /><Button type="primary" loading={previewing} onClick={() => void previewExecution()}>生成执行预演</Button></Flex>}
        {executionPreview && <Alert type={executionPreview.status === 'ready' ? 'success' : executionPreview.status === 'blocked' ? 'error' : 'warning'} showIcon title={`执行预演：${executionPreview.status}`} description={<Space direction="vertical" size={4}>{executionPreview.gates.map((gate) => <Typography.Text key={gate.key} type={gate.status === 'pass' ? 'success' : gate.status === 'blocked' ? 'danger' : 'warning'}>{gate.key}：{gate.message}</Typography.Text>)}</Space>} />}
        <Divider titlePlacement="start">机器判断与计算</Divider>
        <Table rowKey="decision_id" size="small" dataSource={d.machineDecisions} pagination={{ pageSize: 5, showSizeChanger: false }} columns={[{ title: '判断键', dataIndex: 'decision_key', width: 150 }, { title: '输出值', dataIndex: 'output_values_json', width: 280, render: prettyJson }, { title: '置信度门槛', dataIndex: 'confidence_required', width: 110 }, { title: '表达式', dataIndex: 'decision_expression_json', ellipsis: true, render: prettyJson }]} scroll={{ x: 750 }} />
        <Table rowKey="calculation_id" size="small" dataSource={d.machineCalculations} pagination={{ pageSize: 5, showSizeChanger: false }} columns={[{ title: '计算键', dataIndex: 'calculation_key', width: 170 }, { title: '操作', dataIndex: 'operation', width: 190 }, { title: '输入字段', dataIndex: 'input_fields_json', width: 220, render: prettyJson }, { title: '输出字段', dataIndex: 'output_field', width: 150 }, { title: '参数', dataIndex: 'parameters_json', ellipsis: true, render: prettyJson }]} scroll={{ x: 950 }} />
        <Divider titlePlacement="start">机器约束与行动</Divider>
        <Table rowKey="constraint_id" size="small" dataSource={d.machineConstraints} pagination={{ pageSize: 10, showSizeChanger: false }} columns={[{ title: '约束', dataIndex: 'constraint_key', width: 180 }, { title: '类型', dataIndex: 'constraint_type', width: 110 }, { title: '严重级别', dataIndex: 'severity', width: 100 }, { title: '失败动作', dataIndex: 'on_fail', width: 110 }, { title: '表达式', dataIndex: 'expression_json', ellipsis: true, render: prettyJson }]} scroll={{ x: 800 }} />
        <Table rowKey="action_id" size="small" dataSource={d.machineActions} pagination={false} columns={[{ title: '行动', dataIndex: 'action_key', width: 130 }, { title: '行动类型', dataIndex: 'action_type', width: 160 }, { title: '目标', dataIndex: 'target_type', width: 160 }, { title: '需要审批', dataIndex: 'requires_approval', width: 100, render: (value: number) => <Tag color={value ? 'gold' : 'green'}>{value ? '是' : '否'}</Tag> }, { title: '幂等键模板', dataIndex: 'idempotency_key_template', ellipsis: true }]} scroll={{ x: 750 }} />
        <Divider titlePlacement="start">条件 / 规则 / Action</Divider>
        <Table rowKey="part_id" size="small" dataSource={d.parts} pagination={{ pageSize: 10, showSizeChanger: false }} columns={[{ title: '版本', dataIndex: 'asset_version_id', width: 220, ellipsis: true }, { title: '部件', dataIndex: 'part_type', width: 100 }, { title: '标签', dataIndex: 'label', width: 180 }, { title: '表达式', dataIndex: 'expression_json', ellipsis: true, render: prettyJson }]} scroll={{ x: 850 }} />
        <Divider titlePlacement="start">版本与回放</Divider>
        <Table rowKey="asset_version_id" size="small" dataSource={d.versions} pagination={{ pageSize: 8, showSizeChanger: false }} columns={[{ title: '版本', dataIndex: 'version', width: 220 }, { title: '状态', dataIndex: 'status', width: 110, render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> }, { title: '预览', dataIndex: 'preview_count', width: 80 }, { title: '回放', dataIndex: 'replay_count', width: 80 }, { title: '通过', dataIndex: 'replay_pass_count', width: 80 }, { title: '失败', dataIndex: 'replay_fail_count', width: 80 }, { title: '内容哈希', dataIndex: 'content_hash', ellipsis: true }]} scroll={{ x: 900 }} />
        <Divider titlePlacement="start">来源与审核问题</Divider>
        <Table rowKey="source_id" size="small" dataSource={d.sources} pagination={{ pageSize: 8, showSizeChanger: false }} columns={[{ title: '来源类型', dataIndex: 'source_kind', width: 160 }, { title: '来源记录', dataIndex: 'source_record_id', width: 260, ellipsis: true }, { title: '源表', dataIndex: 'source_table', width: 180 }, { title: '状态', dataIndex: 'source_status', width: 110 }, { title: '快照', dataIndex: 'source_snapshot_id', ellipsis: true }]} scroll={{ x: 900 }} />
        {d.issues.length > 0 && <Table rowKey="issue_id" size="small" dataSource={d.issues} pagination={false} columns={[{ title: '问题类型', dataIndex: 'issue_type', width: 130 }, { title: '严重级别', dataIndex: 'severity', width: 100 }, { title: '状态', dataIndex: 'status', width: 100 }, { title: '详情', dataIndex: 'details_json', ellipsis: true, render: prettyJson }]} scroll={{ x: 750 }} />}
      </Space>}
    </Drawer>
  </div>
}
