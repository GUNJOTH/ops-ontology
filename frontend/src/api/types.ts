export type CandidateReviewState = 'pending' | 'approved' | 'modified' | 'rejected' | 'deferred'
export type CandidateValidatorStatus = 'candidate' | 'needs_review' | 'blocked'

export interface DashboardSummary {
  batchId: string
  ruleVersion: string
  sourceSnapshot: string
  inputCount: number
  candidateCount: number
  pendingReviewCount: number
  approvedCount: number
  publishedCount: number
  blockedCount: number
  readOnlySource: boolean
  samplesReady: number
  sites: Array<{ siteId: string; count: number; share: number }>
  contextCoverage: Array<{ label: string; value: number }>
}

export interface CandidateRow {
  candidateId: string
  batchId: string
  siteId: string
  assetNumber: string
  originalDescription: string
  candidateDescription: string
  kks: string
  locationDescription: string
  locationParent: string
  classificationDescription: string
  confidence: 'high' | 'medium' | 'low'
  validatorStatus: CandidateValidatorStatus
  reviewState: CandidateReviewState
  reasonCodes: string[]
  evidenceLevel: string
  updatedAt: string
}

export interface CandidateDetail extends CandidateRow {
  assetId: string
  sourceRowHash: string
  contextHash: string
  specificationCount: number
  featureCount: number
  parentChildEvidence: string
  appliedRules: string[]
  validatorVersion: string
  ruleVersion: string
}

export interface CandidateQuery {
  page: number
  pageSize: number
  search?: string
  siteId?: string
  classification?: string
  quickFilter?: 'all' | 'pending' | 'context' | 'low'
}

export interface CandidatePage {
  rows: CandidateRow[]
  total: number
  page: number
  pageSize: number
}

export interface ReviewPayload {
  candidateId: string
  decision: 'approved' | 'modified' | 'rejected' | 'deferred'
  reviewedDescription?: string
  note?: string
  idempotencyKey: string
}
