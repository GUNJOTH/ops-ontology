import { useQuery } from '@tanstack/react-query'
import { ApartmentOutlined, BranchesOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { Alert, Card, Col, Row, Space, Statistic, Table, Tabs, Tag, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { getCanonicalSemanticSummary, getOntologyEventTypes, getOntologyMetaSummary, getOntologyObjectTypes, getOntologyRelationTypes, getSemanticCoverage, getSemanticGovernanceContract, getSemanticRuntimeContract, getSemanticSourceTruth } from '../api/client'
import type { OntologyMetaSummary } from '../api/types'
import { PageHeader, ReadOnlyTag } from '../components/PageHeader'

function number(value: number | undefined) {
  return (value ?? 0).toLocaleString()
}

function reviewColor(value: string) {
  if (value === 'approved') return 'green'
  if (value === 'needs_review') return 'orange'
  return 'blue'
}

export function OntologyRuntimePage() {
  const summary = useQuery({ queryKey: ['ontology-meta-summary'], queryFn: getOntologyMetaSummary })
  const objectTypes = useQuery({ queryKey: ['ontology-object-types'], queryFn: () => getOntologyObjectTypes() })
  const relationTypes = useQuery({ queryKey: ['ontology-relation-types'], queryFn: () => getOntologyRelationTypes() })
  const eventTypes = useQuery({ queryKey: ['ontology-event-types'], queryFn: () => getOntologyEventTypes() })
  const coverage = useQuery({ queryKey: ['semantic-coverage'], queryFn: getSemanticCoverage })
  const runtimeContract = useQuery({ queryKey: ['semantic-runtime-contract'], queryFn: getSemanticRuntimeContract })
  const governance = useQuery({ queryKey: ['semantic-governance-contract'], queryFn: getSemanticGovernanceContract })
  const canonical = useQuery({ queryKey: ['canonical-semantic-summary'], queryFn: getCanonicalSemanticSummary })
  const sourceTruth = useQuery({ queryKey: ['semantic-source-of-truth'], queryFn: getSemanticSourceTruth })
  const data = summary.data

  const objectColumns: TableColumnsType<OntologyMetaSummary['coreObjects'][number]> = [
    { title: '对象类型', dataIndex: 'object_type', width: 180 },
    { title: '业务名称', dataIndex: 'display_name', width: 150 },
    { title: '层级', dataIndex: 'kind', width: 100 },
    { title: '版本', dataIndex: 'version', width: 160 },
    { title: '审核状态', dataIndex: 'review_status', width: 110, render: (value: string) => <Tag color={reviewColor(value)}>{value}</Tag> },
  ]
  const relationColumns: TableColumnsType<Record<string, unknown>> = [
    { title: '关系谓词', dataIndex: 'predicate', width: 180 },
    { title: 'Domain', dataIndex: 'domain_type', width: 150 },
    { title: 'Range', dataIndex: 'range_type', width: 150 },
    { title: '审核状态', dataIndex: 'review_status', width: 110, render: (value: string) => <Tag color={reviewColor(value)}>{value}</Tag> },
  ]
  const eventColumns: TableColumnsType<Record<string, unknown>> = [
    { title: '事件类型', dataIndex: 'event_type', width: 210 },
    { title: '业务名称', dataIndex: 'display_name', width: 160 },
    { title: '主体类型', dataIndex: 'subject_type', width: 150 },
    { title: '影响状态', dataIndex: 'affects_state', width: 100, render: (value: number) => value ? <Tag color="blue">是</Tag> : <Tag>否</Tag> },
    { title: '审核状态', dataIndex: 'review_status', width: 110, render: (value: string) => <Tag color={reviewColor(value)}>{value}</Tag> },
  ]

  return <div>
    <PageHeader title="本体元模型" description="对象、属性、关系、事件和状态机的可验证注册表；Agent 通过语义契约读取，不直接拼接数据库。" extra={<Space><ReadOnlyTag /><Tag color="blue">ontology-runtime-v1</Tag></Space>} />
    {summary.isError && <Alert type="error" showIcon title="本体元模型尚未初始化" description="请运行 system/build_ontology_meta_model.py。该操作只写本地语义覆盖层。" style={{ marginBottom: 16 }} />}
    {data && <>
      <Alert type="info" showIcon icon={<SafetyCertificateOutlined />} title="当前是本地注册表，不是源表改造" description="新增对象、属性、关系或事件先进入注册表；未知关系和事件只标记 needs_review，不静默删除、不自动合并。" style={{ marginBottom: 16 }} />
      {coverage.data && <Alert
        type={coverage.data.status === 'completed' ? 'success' : 'warning'}
        showIcon
        title={`本地语义闭环：${coverage.data.status === 'completed' ? '已完成' : '已完成并隔离缺口'}`}
        description={`覆盖运行 ${String(coverage.data.run?.run_id ?? '—')} · 缺口 ${number(coverage.data.gaps.length)} 条；缺证据进入 needs_evidence/isolated，不自动补事实、不写源系统。`}
        style={{ marginBottom: 16 }}
      />}
      {canonical.data && <Card title="Canonical Semantic Model" variant="borderless" style={{ marginBottom: 16 }}>
        <Space wrap style={{ marginBottom: 10 }}>
          <Tag color={canonical.data.status === 'completed' ? 'green' : 'orange'}>{canonical.data.status}</Tag>
          <Typography.Text>资源 {number(canonical.data.counts.resources)}</Typography.Text>
          <Typography.Text>RDF 语句 {number(canonical.data.counts.statements)}</Typography.Text>
          <Typography.Text>来源记录 {number(canonical.data.counts.provenance)}</Typography.Text>
          <Typography.Text>RL 推理 {number(canonical.data.counts.inferredStatements)}</Typography.Text>
          <Typography.Text>来源覆盖 {Math.round((canonical.data.provenanceCoverage?.rate ?? 0) * 100)}%</Typography.Text>
          <Tag color="blue">RDF 1.1 / OWL 2 RL / SHACL / SPARQL 1.1</Tag>
        </Space>
        <Alert
          type={sourceTruth.data?.status === 'active' ? 'success' : 'warning'}
          showIcon
          title={sourceTruth.data?.status === 'active' ? 'Canonical RDF 已成为全部设备的语义读取权威' : 'Canonical RDF 已成为标准资产权威，但读取切换仍是部分覆盖'}
          description={`投影运行 ${String(canonical.data.run?.run_id ?? '—')} · 命名图 ${number(canonical.data.graphs.length)} 个 · SKOS 概念 ${number(canonical.data.vocabulary?.concepts)} · 状态 ${number(canonical.data.vocabulary?.states)} · 推理 ${canonical.data.owlRlReplay?.status ?? '未运行'} · 设备读取覆盖 ${number(sourceTruth.data?.projectedDeviceCount)} / ${number(sourceTruth.data?.identityDeviceCount)}（${Math.round((sourceTruth.data?.coverage ?? 0) * 10000) / 100}%）· 未覆盖对象保留兼容读取，不伪装为 Canonical RDF}`}
        />
      </Card>}
      {sourceTruth.data && <Alert
        type={sourceTruth.data.status === 'active' ? 'success' : 'warning'}
        showIcon
        title={`Semantic Source of Truth：${sourceTruth.data.status === 'active' ? '已完成切换' : '部分切换'}`}
        description={`Canonical RDF 读取 ${number(sourceTruth.data.projectedDeviceCount)} 台设备；关系运行层仅承担 ${sourceTruth.data.status === 'active' ? '映射、审批、回放和审计' : '未投影对象兼容读取，以及映射、审批、回放和审计'}。`}
        style={{ marginBottom: 16 }}
      />}
      {runtimeContract.data && <Card title="运行契约" variant="borderless" style={{ marginBottom: 16 }}>
        <Space wrap style={{ marginBottom: 12 }}>
          <Tag color="blue">{runtimeContract.data.status}</Tag>
          <Typography.Text>对象实例 {number(runtimeContract.data.counts.objectInstances)}</Typography.Text>
          <Typography.Text>身份断言 {number(runtimeContract.data.counts.identityAssertions)}</Typography.Text>
          <Typography.Text>事件追踪 {number(runtimeContract.data.counts.tracedEvents)}/{number(runtimeContract.data.counts.events)}</Typography.Text>
          <Typography.Text>约束隔离 {number(runtimeContract.data.counts.violations)}</Typography.Text>
        </Space>
        <Table
          rowKey="layer_key"
          size="small"
          pagination={false}
          columns={[
            { title: '层', dataIndex: 'layer_key' },
            { title: '职责', dataIndex: 'display_name' },
            { title: '优先级', dataIndex: 'precedence' },
            { title: '来源权威', dataIndex: 'source_of_record', render: (value: number) => value ? <Tag color="green">是</Tag> : <Tag>派生</Tag> },
            { title: '审批', dataIndex: 'requires_approval', render: (value: number) => value ? <Tag color="orange">需要</Tag> : <Tag>不需要</Tag> },
          ]}
          dataSource={runtimeContract.data.authority}
        />
      </Card>}
      {governance.data && <Card title="治理契约" variant="borderless" style={{ marginBottom: 16 }}>
        <Space wrap style={{ marginBottom: 12 }}>
          <Tag color={governance.data.status === 'completed' ? 'green' : 'orange'}>{governance.data.status}</Tag>
          <Typography.Text>身份自动 {number(governance.data.identity.autoCount)}</Typography.Text>
          <Typography.Text>身份待确认 {number(governance.data.identity.reviewRequiredCount)}</Typography.Text>
          <Typography.Text>身份冲突 {number(governance.data.identity.conflictCount)}</Typography.Text>
          <Typography.Text>关系契约 {number(governance.data.relations.length)}</Typography.Text>
          <Typography.Text>对象属性 {number(governance.data.objectSchema.length)}</Typography.Text>
          <Typography.Text>事件因果契约 {number(governance.data.eventRelations.length)}</Typography.Text>
          <Typography.Text>可执行规则 {number(governance.data.rules.length)}</Typography.Text>
        </Space>
        <Alert
          type={governance.data.status === 'completed' ? 'success' : 'warning'}
          showIcon
          title="自动映射只接受唯一精确证据；冲突、位置缺口和因果缺口保持隔离"
          description={`当前隔离 ${number(governance.data.violations.length)} 项；不自动合并跨系统设备、不凭空补事件因果、不写源系统。`}
          style={{ marginBottom: 12 }}
        />
        {governance.data.conflicts.length > 0 && <Table<Record<string, string | number>>
          rowKey="conflict_id"
          size="small"
          pagination={{ pageSize: 5, showSizeChanger: false }}
          columns={[
            { title: '冲突类型', dataIndex: 'conflict_type' },
            { title: '来源', dataIndex: 'source_schema' },
            { title: '键类型', dataIndex: 'source_key_type' },
            { title: '源键', dataIndex: 'source_key' },
            { title: '严重度', dataIndex: 'severity', render: (value: string) => <Tag color="red">{value}</Tag> },
            { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color="orange">{value}</Tag> },
          ]}
          dataSource={governance.data.conflicts}
        />}
      </Card>}
      <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
        <Col xs={12} md={4}><Card className="metric-card" variant="borderless"><Statistic title={<Space><ApartmentOutlined />对象类型</Space>} value={data.objectTypeCount} /></Card></Col>
        <Col xs={12} md={4}><Card className="metric-card" variant="borderless"><Statistic title="属性类型" value={data.propertyTypeCount} /></Card></Col>
        <Col xs={12} md={4}><Card className="metric-card" variant="borderless"><Statistic title={<Space><BranchesOutlined />关系类型</Space>} value={data.relationTypeCount} /></Card></Col>
        <Col xs={12} md={4}><Card className="metric-card" variant="borderless"><Statistic title="事件类型" value={data.eventTypeCount} /></Card></Col>
        <Col xs={12} md={4}><Card className="metric-card" variant="borderless"><Statistic title="状态机" value={data.stateMachineCount} /></Card></Col>
        <Col xs={12} md={4}><Card className="metric-card purple" variant="borderless"><Statistic title="待复核关系/事件" value={data.unregisteredRelations.length + data.unregisteredEvents.length} /></Card></Col>
      </Row>
      {(data.unregisteredRelations.length > 0 || data.unregisteredEvents.length > 0) && <Alert type="warning" showIcon title="仍有注册缺口，当前不会自动接受" description={`关系 ${number(data.unregisteredRelations.length)} 类，事件 ${number(data.unregisteredEvents.length)} 类；这些仅影响元模型完整性，不会修改现有运行事实。`} style={{ marginBottom: 16 }} />}
      <Card variant="borderless">
        <Tabs items={[
          { key: 'objects', label: '14 个核心业务对象', children: <Table rowKey="object_type" size="small" pagination={false} columns={objectColumns} dataSource={data.coreObjects} loading={objectTypes.isLoading} /> },
          { key: 'relations', label: `关系注册表 ${number(data.relationTypeCount)}`, children: <Table rowKey="predicate" size="small" pagination={{ pageSize: 12, showSizeChanger: false }} columns={relationColumns} dataSource={relationTypes.data?.items ?? []} loading={relationTypes.isLoading} /> },
          { key: 'events', label: `事件注册表 ${number(data.eventTypeCount)}`, children: <Table rowKey="event_type" size="small" pagination={{ pageSize: 12, showSizeChanger: false }} columns={eventColumns} dataSource={eventTypes.data?.items ?? []} loading={eventTypes.isLoading} /> },
        ]} />
        <Typography.Text type="secondary">最近元模型运行：{String(data.latestRun?.created_at ?? '—')} · 未写源系统：{String(data.sourceWrite)} · 正式发布：{String(data.formalPublication)}</Typography.Text>
      </Card>
    </>}
  </div>
}
