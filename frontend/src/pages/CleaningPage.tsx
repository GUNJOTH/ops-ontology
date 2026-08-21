import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Badge, Button, Card, Col, Flex, Modal, Pagination, Row, Select, Space, Table, Tag, Typography, message } from 'antd'
import type { TableColumnsType } from 'antd'
import { CheckCircleOutlined, ClearOutlined, PlayCircleOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { advanceCleaningTask, getCleaning, getCleaningTasks, getDashboard } from '../api/client'
import type { CleaningRow, CleaningTask } from '../api/types'
import { PageHeader, ReadOnlyTag } from '../components/PageHeader'
import { useNavigate } from 'react-router-dom'

const pageSize = 25

const stages: Array<{ key: CleaningTask['stage']; label: string }> = [
  { key: 'task', label: '任务' },
  { key: 'previewed', label: '预览' },
  { key: 'replayed', label: '回放' },
  { key: 'approved', label: '审批' },
  { key: 'published', label: '发布' },
]

function stageLabel(stage: string) {
  return stages.find((item) => item.key === stage)?.label ?? stage
}

function statusLabel(value: string) {
  if (value === 'pending') return '待审批'
  if (value === 'modified' || value === 'approved') return '已审批'
  if (value === 'rejected') return '已驳回'
  return value
}

function nextAction(task: CleaningTask | undefined) {
  if (task?.nextAction === 'completed') return '已完成发布'
  if (task?.nextAction === 'preview') return '生成预览'
  if (task?.nextAction === 'replay') return '执行回放'
  if (task?.nextAction === 'approval') return '批量审批'
  if (task?.nextAction === 'publication') return '发布任务'
  if (!task) return '暂无任务'
  if (task.stage === 'task') return '生成预览'
  if (task.stage === 'previewed' && task.replayStatus === 'passed' && task.replayFailCount === 0) return '确认回放'
  if (task.stage === 'replayed' || (task.stage === 'previewed' && task.pendingCount > 0)) return '审批任务'
  if (task.stage === 'approved') return '发布任务'
  if (task.stage === 'published') return '已发布'
  return '等待预览'
}

export function CleaningPage() {
  const navigate = useNavigate()
  const [page, setPage] = useState(1)
  const [status, setStatus] = useState('all')
  const [selectedTaskId, setSelectedTaskId] = useState<string>()
  const [saving, setSaving] = useState(false)
  const dashboardQuery = useQuery({ queryKey: ['dashboard'], queryFn: getDashboard })
  const tasksQuery = useQuery({ queryKey: ['cleaning-tasks'], queryFn: getCleaningTasks })
  const tasks = tasksQuery.data?.tasks ?? []
  const selected = tasks.find((task) => task.taskId === selectedTaskId) ?? tasks.find((task) => task.isCleaning) ?? tasks[0]
  const query = useQuery({
    queryKey: ['cleaning', selected?.taskId, page, status],
    queryFn: () => getCleaning({ page, pageSize, scope: 'all', status, taskId: selected?.taskId }),
    enabled: Boolean(selected),
  })
  const data = query.data
  const rows = data?.rows ?? []
  const cleaningTasks = tasks.filter((task) => task.isCleaning)
  const pending = cleaningTasks.reduce((sum, task) => sum + task.pendingCount, 0)
  const published = cleaningTasks.reduce((sum, task) => sum + task.publishedCount, 0)
  const target = cleaningTasks.reduce((sum, task) => sum + task.candidateCount, 0)
  const pendingDeviceCandidates = dashboardQuery.data?.pendingReviewCount
  const taskStatsLoading = tasksQuery.isLoading

  const refresh = async () => {
    await Promise.all([tasksQuery.refetch(), query.refetch()])
  }

  const advance = async (confirmPublication = false) => {
    if (!selected || selected.stage === 'published') return
    setSaving(true)
    try {
      const action = selected.nextAction || selected.stage
      const result = await advanceCleaningTask(selected.taskId, {
        idempotencyKey: `cleaning-${selected.taskId}-${action}-v1`,
        confirmPublication,
        note: '统一清洗任务推进：仅写入本地结果层，不写源库',
      })
      if (result.requiresConfirmation) {
        Modal.confirm({
          title: '确认正式发布',
          content: `将发布 ${result.targetCount ?? selected.candidateCount} 条到本地正式结果层，源库不会写入。`,
          okText: '确认发布',
          cancelText: '取消',
          onOk: () => advance(true),
        })
        return
      }
      if (result.status === 'failed' || (result.failCount ?? 0) > 0) {
        throw new Error(`回放未通过：${result.failCount ?? 0} 条异常`)
      }
      message.success(result.action === 'publication' || result.action === 'completed' ? `已发布 ${result.publishedCount ?? 0} 条` : `已完成：${result.action}`)
      await refresh()
    } catch (error) {
      message.error(error instanceof Error ? error.message : '任务推进失败，未改变正式结果')
    } finally {
      setSaving(false)
    }
  }

  const act = () => { void advance(false) }

  const columns: TableColumnsType<CleaningRow> = [
    { title: '电厂', dataIndex: 'siteId', width: 110 },
    { title: '设备编码', dataIndex: 'assetNumber', width: 150 },
    { title: '改写前 → 改写后', key: 'description', render: (_, row) => <div><Typography.Text>{row.originalDescription}</Typography.Text><Typography.Text type="secondary"> → </Typography.Text><Typography.Text strong>{row.proposedDescription}</Typography.Text></div> },
    { title: '规则', dataIndex: 'ruleLabel', width: 180, render: (value: string) => <Tag color="blue">{value}</Tag> },
    { title: '回放', dataIndex: 'replayStatus', width: 100, render: (value: string) => <Badge status={value === 'passed' ? 'success' : 'warning'} text={value === 'passed' ? '通过' : value ?? '未执行'} /> },
    { title: '审批状态', dataIndex: 'status', width: 100, render: (value: string) => <Tag>{statusLabel(value)}</Tag> },
  ]

  return <div>
    <PageHeader title="清洗任务" description="统一管理：预览 → 回放 → 审批 → 发布。每个规则是一项独立任务，源 MaxiEAM 始终只读。" extra={<Space><ReadOnlyTag /><Button type="primary" icon={selected?.stage === 'published' ? <CheckCircleOutlined /> : <PlayCircleOutlined />} loading={saving} disabled={!selected || selected.stage === 'published' || (!selected.isCleaning && selected.stage !== 'previewed')} onClick={act}>{nextAction(selected)}</Button></Space>} />
    <Alert type="info" showIcon icon={<SafetyCertificateOutlined />} message="任务状态是唯一入口" description="预览只登记结果文件，回放通过后才允许审批；审批和正式发布分开记录，正式发布只写本地正式结果层，不回写源库。" style={{ marginBottom: 16 }} />
    <Card style={{ marginBottom: 16, borderColor: '#93c5fd', background: '#f8fbff' }}>
      <Flex justify="space-between" align="center" gap={16} wrap>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>设备候选审核不在清洗任务表内</Typography.Title>
          <Typography.Text type="secondary">当前有 <Typography.Text strong>{pendingDeviceCandidates === undefined ? '加载中…' : pendingDeviceCandidates.toLocaleString()}</Typography.Text> 条设备候选待审核。进入设备审核后，可查看原文、候选描述、KKS、位置、分类、规则证据，并逐条通过、修改、驳回或待补证据。</Typography.Text>
        </div>
        <Space wrap>
          <Button type="primary" onClick={() => navigate('/candidates?quick_filter=pending')}>进入设备审核</Button>
          <Button onClick={() => navigate('/ai-review')}>打开 AI 样本复核</Button>
        </Space>
      </Flex>
    </Card>
    <Row gutter={16} style={{ marginBottom: 16 }}>
      <Col xs={24} md={8}><Card><Typography.Text type="secondary">清洗候选</Typography.Text><Typography.Title level={2} style={{ margin: '6px 0 0' }}>{taskStatsLoading ? '—' : target.toLocaleString()}</Typography.Title><Typography.Text type="secondary">{taskStatsLoading ? '正在加载' : '条'}</Typography.Text></Card></Col>
      <Col xs={24} md={8}><Card><Typography.Text type="secondary">待审批</Typography.Text><Typography.Title level={2} style={{ margin: '6px 0 0' }}>{taskStatsLoading ? '—' : pending.toLocaleString()}</Typography.Title><Typography.Text type="secondary">{taskStatsLoading ? '正在加载' : '条'}</Typography.Text></Card></Col>
      <Col xs={24} md={8}><Card><Typography.Text type="secondary">已发布</Typography.Text><Typography.Title level={2} style={{ margin: '6px 0 0' }}>{taskStatsLoading ? '—' : published.toLocaleString()}</Typography.Title><Typography.Text type="secondary">{taskStatsLoading ? '正在加载' : `/ ${target.toLocaleString()} 条`}</Typography.Text></Card></Col>
    </Row>
    <Card title="清洗任务" style={{ marginBottom: 16 }}>
      <Flex gap={8} wrap="wrap">{tasksQuery.isLoading ? <Typography.Text type="secondary">正在读取清洗任务…</Typography.Text> : tasks.filter((task) => task.isCleaning).map((task) => <Button key={task.taskId} type={task.taskId === selected?.taskId ? 'primary' : 'default'} onClick={() => { setSelectedTaskId(task.taskId); setPage(1) }}>{task.ruleLabel} · {stageLabel(task.stage)} · 预览 {task.previewRows || 0} / 回放 {task.replayRows || 0}</Button>)}</Flex>
      {selected && <><Flex gap={4} align="center" style={{ marginTop: 16, marginBottom: 8 }}>{stages.slice(1).map((item) => <Tag key={item.key} color={stages.findIndex((stage) => stage.key === selected.stage) >= stages.findIndex((stage) => stage.key === item.key) ? 'blue' : 'default'}>{item.label}</Tag>)}</Flex><Typography.Text type="secondary">当前动作：{nextAction(selected)}。回放 {selected.replayPassCount}/{selected.replayEvaluationCount} 通过；源写入 {selected.sourceWrite ? '是' : '否'}。</Typography.Text></>}
    </Card>
    <Card title="任务结果" extra={<Select value={status} onChange={(value) => { setStatus(value); setPage(1) }} options={[{ value: 'all', label: '全部' }, { value: 'pending', label: '待审批' }, { value: 'modified', label: '已审批' }]} style={{ width: 120 }} />}>
      <Table<CleaningRow> rowKey="queueId" loading={query.isLoading} columns={columns} dataSource={rows} pagination={false} scroll={{ x: 980 }} size="middle" />
      <Flex justify="space-between" align="center" style={{ marginTop: 16 }}><Typography.Text type="secondary">正式结果保留原始描述、候选描述、规则版本、回放和审批凭据。</Typography.Text><Pagination current={page} pageSize={pageSize} total={data?.total ?? 0} showSizeChanger={false} onChange={setPage} /></Flex>
    </Card>
  </div>
}
