import type {
  CandidateDetail,
  CandidatePage,
  CandidateQuery,
  DashboardSummary,
  ReviewPayload,
} from './types'

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'
const MOCK_ENABLED = import.meta.env.VITE_ENABLE_MOCK === 'true'

const mockDashboard: DashboardSummary = {
  batchId: 'hd-semantic-20260812',
  ruleVersion: '0.2.0',
  sourceSnapshot: 'HD_SAAS / 20260812',
  inputCount: 382785,
  candidateCount: 382785,
  pendingReviewCount: 300,
  approvedCount: 0,
  publishedCount: 0,
  blockedCount: 0,
  readOnlySource: true,
  samplesReady: 300,
  sites: [
    { siteId: 'ZJFD000', count: 105110, share: 27.5 },
    { siteId: 'XZHR200', count: 100657, share: 26.3 },
    { siteId: 'HRCS000', count: 71284, share: 18.6 },
  ],
  contextCoverage: [
    { label: 'KKS / 位置', value: 100 },
    { label: '位置父级', value: 100 },
    { label: '分类', value: 30 },
    { label: '规格 / 特征', value: 18 },
  ],
  reviewSample: {
    sampleId: 'sample-mock', sampleName: 'high-quality-300-v1', batchId: 'hd-semantic-20260812', sourceSnapshotId: 'mock',
    targetCount: 300, selectedCount: 300, status: 'open', strategy: 'round_robin_by_SITEID_and_CLASSIFICATION_DESCRIPTION',
    ruleVersion: '0.2.0', validatorVersion: 'validator-0.1.0', pendingCount: 300, approvedCount: 0, modifiedCount: 0, rejectedCount: 0, deferredCount: 0, strata: [],
  },
}

const mockRows: CandidateDetail[] = [
  {
    candidateId: 'cand-p-1101', batchId: mockDashboard.batchId, siteId: 'ZJFD000', assetNumber: 'P-1101', assetId: '1038821',
    originalDescription: '冷却水泵', candidateDescription: '冷却水泵', kks: '10CJA01 AP01', locationDescription: '冷却水系统', locationParent: '循环水泵房',
    classificationDescription: '泵', confidence: 'high', validatorStatus: 'candidate', reviewState: 'pending', reasonCodes: ['DESC_BASE_001'], evidenceLevel: '充分',
    updatedAt: '2026-08-12 09:12', sourceRowHash: 'sha256:8d2a...a91c', contextHash: 'sha256:41f0...1d82', specificationCount: 4, featureCount: 8,
    parentChildEvidence: '父级位置与分类一致，未发现设备身份冲突', appliedRules: ['DESC_BASE_001'], validatorVersion: 'validator-0.1.0', ruleVersion: '0.2.0',
  },
  {
    candidateId: 'cand-v-0203', batchId: mockDashboard.batchId, siteId: 'XZHR200', assetNumber: 'V-203', assetId: '2031448',
    originalDescription: '主给水调节阀', candidateDescription: '给水调节阀', kks: '20LBA10 AA01', locationDescription: '给水系统', locationParent: '汽机房',
    classificationDescription: '阀门', confidence: 'high', validatorStatus: 'candidate', reviewState: 'pending', reasonCodes: ['TERM_SYNONYM_002'], evidenceLevel: '充分',
    updatedAt: '2026-08-12 09:10', sourceRowHash: 'sha256:1aa8...5c3e', contextHash: 'sha256:7bb1...3f20', specificationCount: 5, featureCount: 9,
    parentChildEvidence: '分类为阀门，系统上下文支持“给水”术语', appliedRules: ['TERM_SYNONYM_002'], validatorVersion: 'validator-0.1.0', ruleVersion: '0.2.0',
  },
  {
    candidateId: 'cand-m-0301', batchId: mockDashboard.batchId, siteId: 'HRCS000', assetNumber: 'M-301', assetId: '3020991',
    originalDescription: '风机电机', candidateDescription: '风机电动机', kks: '30HFA20 AP01', locationDescription: '送风系统', locationParent: '风机房',
    classificationDescription: '电动机', confidence: 'medium', validatorStatus: 'needs_review', reviewState: 'pending', reasonCodes: ['TERM_CONFLICT_001'], evidenceLevel: '部分',
    updatedAt: '2026-08-12 09:07', sourceRowHash: 'sha256:772b...90ae', contextHash: 'sha256:9e4d...7bc2', specificationCount: 2, featureCount: 4,
    parentChildEvidence: '分类支持电动机，但同类设备存在两种术语写法', appliedRules: [], validatorVersion: 'validator-0.1.0', ruleVersion: '0.2.0',
  },
  {
    candidateId: 'cand-p-1102', batchId: mockDashboard.batchId, siteId: 'ZJFD000', assetNumber: 'P-1102', assetId: '1038822',
    originalDescription: '冷却水泵', candidateDescription: '冷却水泵', kks: '10CJA01 AP02', locationDescription: '循环水泵房', locationParent: '循环水系统',
    classificationDescription: '泵', confidence: 'medium', validatorStatus: 'needs_review', reviewState: 'pending', reasonCodes: ['LOCATION_CONTEXT_002'], evidenceLevel: '部分',
    updatedAt: '2026-08-12 09:02', sourceRowHash: 'sha256:92d1...2a10', contextHash: 'sha256:aa2d...0c8f', specificationCount: 4, featureCount: 8,
    parentChildEvidence: '位置描述与位置父级存在差异，仅作为上下文提示', appliedRules: ['DESC_BASE_001'], validatorVersion: 'validator-0.1.0', ruleVersion: '0.2.0',
  },
  {
    candidateId: 'cand-f-0410', batchId: mockDashboard.batchId, siteId: 'XZHR200', assetNumber: 'F-410', assetId: '2032770',
    originalDescription: '入口阀', candidateDescription: '入口隔离阀', kks: '40LBA11 AA01', locationDescription: '给水入口', locationParent: '给水系统',
    classificationDescription: '阀门', confidence: 'high', validatorStatus: 'candidate', reviewState: 'pending', reasonCodes: ['TERM_CONTEXT_004'], evidenceLevel: '充分',
    updatedAt: '2026-08-12 08:59', sourceRowHash: 'sha256:3e1f...6ca1', contextHash: 'sha256:4c3d...aa10', specificationCount: 6, featureCount: 10,
    parentChildEvidence: 'KKS 与阀门分类一致，父级系统支持入口语义', appliedRules: ['TERM_CONTEXT_004'], validatorVersion: 'validator-0.1.0', ruleVersion: '0.2.0',
  },
]

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { headers: { 'Content-Type': 'application/json' }, ...init })
  if (!response.ok) throw new Error(`API ${response.status}`)
  return response.json() as Promise<T>
}

export async function getDashboard(): Promise<DashboardSummary> {
  if (MOCK_ENABLED) return mockDashboard
  return request<DashboardSummary>('/dashboard')
}

export async function getCandidates(query: CandidateQuery): Promise<CandidatePage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.search) params.set('search', query.search)
  if (query.siteId) params.set('site_id', query.siteId)
  if (query.classification) params.set('classification', query.classification)
  if (query.quickFilter && query.quickFilter !== 'all') params.set('quick_filter', query.quickFilter)
  if (query.sampleOnly) params.set('sample_only', 'true')
  if (!MOCK_ENABLED) return request<CandidatePage>(`/candidates?${params.toString()}`)
  {
    const normalized = (query.search ?? '').trim().toLowerCase()
    const filtered = mockRows.filter((row) => {
      const text = `${row.siteId} ${row.assetNumber} ${row.originalDescription} ${row.candidateDescription} ${row.kks}`.toLowerCase()
      const matchesSearch = !normalized || text.includes(normalized)
      const matchesSite = !query.siteId || row.siteId === query.siteId
      const matchesClass = !query.classification || row.classificationDescription === query.classification
      const matchesQuick = query.quickFilter === 'pending' ? row.reviewState === 'pending'
        : query.quickFilter === 'context' ? row.reasonCodes.some((code) => code.includes('LOCATION') || code.includes('CONTEXT'))
          : query.quickFilter === 'low' ? row.confidence === 'low'
            : true
      return matchesSearch && matchesSite && matchesClass && matchesQuick
    })
    const start = (query.page - 1) * query.pageSize
    return { rows: filtered.slice(start, start + query.pageSize), total: 300, page: query.page, pageSize: query.pageSize }
  }
}

export async function getCandidate(candidateId: string): Promise<CandidateDetail> {
  if (MOCK_ENABLED) return mockRows.find((row) => row.candidateId === candidateId) ?? mockRows[0]
  return request<CandidateDetail>(`/candidates/${encodeURIComponent(candidateId)}`)
}

export async function getReviewSample(): Promise<import('./types').ReviewSampleSummary> {
  if (MOCK_ENABLED) return mockDashboard.reviewSample
  return request<import('./types').ReviewSampleSummary>('/review-sample')
}

export async function submitReview(payload: ReviewPayload): Promise<void> {
  await request<void>('/reviews', { method: 'POST', body: JSON.stringify(payload) })
}

export { mockDashboard, mockRows }
