import type {
  CandidateDetail,
  CandidatePage,
  CandidateFacets,
  CandidateQuery,
  DashboardSummary,
  WorldModelSummary,
  SemanticCoverage,
  SemanticRuntimeContract,
  SemanticGovernanceContract,
  CanonicalSemanticSummary,
  SemanticSourceTruth,
  SemanticIdentityReviewDetail,
  SemanticIdentityReviewPage,
  OntologyMetaSummary,
  MetadataCatalogDetail,
  MetadataCatalogPage,
  MetadataSummary,
  ReviewPayload,
  PublishedPage,
  PublishedRow,
  UnifiedDeviceDetail,
  UnifiedDevicePage,
  UnifiedDeviceSummary,
  UnifiedLocationDetail,
  UnifiedLocationPage,
  UnifiedLocationSummary,
  KnowledgeAssetDetail,
  KnowledgeAssetPage,
  KnowledgeAssetSummary,
  SemanticFactDetail,
  SemanticFactPage,
  SemanticFactSummary,
  SemanticEventDetail,
  SemanticEventPage,
  SemanticEventSummary,
  SemanticStatusDictionaryDetail,
  SemanticStatusDictionaryPage,
  SemanticStatusDictionarySummary,
  SemanticStatusDictionaryReplay,
  SemanticDecisionSummary,
  SemanticDecisionPage,
  SemanticActionPlanPage,
  SemanticActionPlanDetail,
} from './types'

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

// Acting reviewer sent on every request; the backend resolves the audit actor from
// the X-Reviewer header (falling back to SEMANTIC_REVIEWER, then "local-user").
// Configure this single constant to change the reviewer across the whole frontend.
export const REVIEWER_NAME = '人工审核'

// Optional Bearer token for the backend's opt-in SEMANTIC_API_TOKEN write guard.
// Empty by default so local (no-token) deployments keep working unchanged.
export const API_AUTH_TOKEN = ''

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json', 'X-Reviewer': REVIEWER_NAME }
  if (API_AUTH_TOKEN) headers.Authorization = `Bearer ${API_AUTH_TOKEN}`
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers: { ...headers, ...(init?.headers as Record<string, string> | undefined) } })
  if (!response.ok) {
    let detail = ''
    try {
      const payload = await response.json() as { detail?: string }
      detail = typeof payload.detail === 'string' ? payload.detail : ''
    } catch {
      // Keep the status fallback when the server does not return JSON.
    }
    throw new Error(detail || `API ${response.status}`)
  }
  return response.json() as Promise<T>
}

export async function getDashboard(): Promise<DashboardSummary> {
  return request<DashboardSummary>('/dashboard')
}

export async function getWorldModelSummary(): Promise<WorldModelSummary> {
  return request<WorldModelSummary>('/world-model/summary')
}

export async function getSemanticCoverage(): Promise<SemanticCoverage> {
  return request<SemanticCoverage>('/world-model/coverage')
}

export async function getSemanticRuntimeContract(): Promise<SemanticRuntimeContract> {
  return request<SemanticRuntimeContract>('/world-model/runtime-contract')
}

export async function getSemanticGovernanceContract(): Promise<SemanticGovernanceContract> {
  return request<SemanticGovernanceContract>('/world-model/governance-contract')
}

export async function getCanonicalSemanticSummary(): Promise<CanonicalSemanticSummary> {
  return request<CanonicalSemanticSummary>('/semantic/canonical/summary')
}

export async function getSemanticSourceTruth(): Promise<SemanticSourceTruth> {
  return request<SemanticSourceTruth>('/semantic/source-of-truth')
}

export async function getSemanticIdentityReviewQueue(query: { page: number; pageSize: number; status?: string }): Promise<SemanticIdentityReviewPage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.status && query.status !== 'all') params.set('status', query.status)
  return request<SemanticIdentityReviewPage>(`/world-model/identity-review?${params.toString()}`)
}

export async function getSemanticIdentityReviewDetail(reviewId: string): Promise<SemanticIdentityReviewDetail> {
  return request<SemanticIdentityReviewDetail>(`/world-model/identity-review/${encodeURIComponent(reviewId)}`)
}

export async function decideSemanticIdentityReview(reviewId: string, payload: { decision: 'approved' | 'rejected'; reviewer: string; note: string; evidence: Record<string, unknown>; idempotencyKey: string }): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/world-model/identity-review/${encodeURIComponent(reviewId)}`, { method: 'POST', body: JSON.stringify(payload) })
}

export async function getOntologyMetaSummary(): Promise<OntologyMetaSummary> {
  return request<OntologyMetaSummary>('/ontology/meta-summary')
}

export async function getOntologyObjectTypes(query: { kind?: string; reviewStatus?: string } = {}): Promise<{ items: Array<Record<string, unknown>>; sourceWrite: boolean; formalPublication: boolean }> {
  const params = new URLSearchParams()
  if (query.kind && query.kind !== 'all') params.set('kind', query.kind)
  if (query.reviewStatus && query.reviewStatus !== 'all') params.set('review_status', query.reviewStatus)
  const suffix = params.toString()
  return request(`/ontology/object-types${suffix ? `?${suffix}` : ''}`)
}

export async function getOntologyRelationTypes(reviewStatus = 'all'): Promise<{ items: Array<Record<string, unknown>>; sourceWrite: boolean; formalPublication: boolean }> {
  const suffix = reviewStatus !== 'all' ? `?review_status=${encodeURIComponent(reviewStatus)}` : ''
  return request(`/ontology/relation-types${suffix}`)
}

export async function getOntologyEventTypes(reviewStatus = 'all'): Promise<{ items: Array<Record<string, unknown>>; sourceWrite: boolean; formalPublication: boolean }> {
  const suffix = reviewStatus !== 'all' ? `?review_status=${encodeURIComponent(reviewStatus)}` : ''
  return request(`/ontology/event-types${suffix}`)
}

export async function getCandidates(query: CandidateQuery): Promise<CandidatePage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.search) params.set('search', query.search)
  if (query.siteId) params.set('site_id', query.siteId)
  if (query.classification) params.set('classification', query.classification)
  if (query.quickFilter && query.quickFilter !== 'all') params.set('quick_filter', query.quickFilter)
  if (query.sampleOnly) params.set('sample_only', 'true')
  if (query.sampleSize) params.set('sample_size', String(query.sampleSize))
  return request<CandidatePage>(`/candidates?${params.toString()}`)
}

export async function getCandidateFacets(query: Pick<CandidateQuery, 'quickFilter' | 'sampleOnly' | 'sampleSize'>): Promise<CandidateFacets> {
  const params = new URLSearchParams({
    quick_filter: query.quickFilter ?? 'all',
    sample_only: String(Boolean(query.sampleOnly)),
    sample_size: String(query.sampleSize ?? 300),
  })
  return request<CandidateFacets>(`/candidates/facets?${params.toString()}`)
}

export async function getCandidate(candidateId: string): Promise<CandidateDetail> {
  return request<CandidateDetail>(`/candidates/${encodeURIComponent(candidateId)}`)
}

export async function getReviewSample(): Promise<import('./types').ReviewSampleSummary> {
  return request<import('./types').ReviewSampleSummary>('/review-sample')
}

export async function submitReview(payload: ReviewPayload): Promise<void> {
  await request<void>('/reviews', { method: 'POST', body: JSON.stringify(payload) })
}

export async function getAiReviewPreview(scope: import('./types').AiReviewScope = 'sample'): Promise<import('./types').AiReviewPreview> {
  return request<import('./types').AiReviewPreview>(`/ai-review/preview?scope=${scope}`)
}

export async function getAgentAuditPreview(batchSize = 100): Promise<import('./types').AiAgentAuditPreview> {
  return request<import('./types').AiAgentAuditPreview>(`/ai-review/agent-preview?batch_size=${batchSize}`)
}

export async function runAiAutoApproval(scope: import('./types').AiReviewScope = 'sample', batchId?: string): Promise<import('./types').AiApprovalResult> {
  return request<import('./types').AiApprovalResult>('/ai-review/auto-approve', {
    method: 'POST', body: JSON.stringify({ scope, idempotencyKey: `ai-assisted-review-${scope}-${batchId ?? 'current'}` }),
  })
}

export async function getAiSample(query: { page: number; pageSize: number; decision?: string; siteId?: string }): Promise<import('./types').AiSamplePage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.decision && query.decision !== 'all') params.set('ai_decision', query.decision)
  if (query.siteId) params.set('site_id', query.siteId)
  return request<import('./types').AiSamplePage>(`/ai-review/sample?${params.toString()}`)
}

export async function saveAiSampleDecision(payload: { sampleId: string; candidateId: string; decision: import('./types').AiSampleDecision; note?: string }): Promise<void> {
  await request('/ai-review/decision', { method: 'POST', body: JSON.stringify({ ...payload, idempotencyKey: `ai-sample-${payload.sampleId}-${payload.candidateId}-${payload.decision}` }) })
}

export async function saveAiBulkDecision(payload: { sampleId: string; decision: 'keep_original' | 'accept_candidate' }): Promise<{ targetCount: number; appliedCount: number; replayedCount: number }> {
  return request('/ai-review/bulk-decision', { method: 'POST', body: JSON.stringify({ ...payload, idempotencyKey: `ai-bulk-${payload.sampleId}-${payload.decision}` }) })
}

export async function getAiClusters(query: { page: number; pageSize: number; clusterType?: string; siteId?: string; aiDecision?: string; decision?: string }): Promise<import('./types').AiClusterPage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.clusterType && query.clusterType !== 'all') params.set('cluster_type', query.clusterType)
  if (query.siteId) params.set('site_id', query.siteId)
  if (query.aiDecision && query.aiDecision !== 'all') params.set('ai_decision', query.aiDecision)
  if (query.decision && query.decision !== 'all') params.set('decision', query.decision)
  return request<import('./types').AiClusterPage>(`/ai-review/clusters?${params.toString()}`)
}

export async function getAiCluster(clusterId: string): Promise<import('./types').AiClusterDetail> {
  return request<import('./types').AiClusterDetail>(`/ai-review/clusters/${encodeURIComponent(clusterId)}`)
}

export async function saveAiClusterDecision(payload: { clusterId: string; decision: import('./types').AiSampleDecision; note?: string }): Promise<{ clusterId: string; decision: import('./types').AiSampleDecision; memberCount: number; appliedCount: number; replayedCount: number; replayed: boolean; sourceWrite: boolean; formalPublication: boolean }> {
  return request('/ai-review/clusters/decision', {
    method: 'POST',
    body: JSON.stringify({ ...payload, idempotencyKey: `ai-cluster-${payload.clusterId}-${payload.decision}` }),
  })
}

export async function getFormalApprovalQueue(query: { page: number; pageSize: number; status?: string; replayId?: string }): Promise<import('./types').FormalApprovalPage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.status) params.set('status', query.status)
  if (query.replayId) params.set('replay_id', query.replayId)
  return request<import('./types').FormalApprovalPage>(`/formal-approval-queue?${params.toString()}`)
}

export async function batchApproveFormalQueue(payload: { replayId: string; note: string; idempotencyKey: string }): Promise<{ replayId: string; targetCount: number; appliedCount: number; pendingCount: number; completedCount: number; status: string; sourceWrite: boolean; formalPublication: boolean }> {
  return request('/formal-approval-queue/batch-approve', { method: 'POST', body: JSON.stringify({ ...payload }) })
}

export async function getCleaning(query: { page: number; pageSize: number; scope?: 'cleaning' | 'keep_original' | 'all'; status?: string; taskId?: string }): Promise<import('./types').CleaningPage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.scope) params.set('scope', query.scope)
  if (query.status) params.set('status', query.status)
  if (query.taskId) params.set('task_id', query.taskId)
  return request<import('./types').CleaningPage>(`/cleaning?${params.toString()}`)
}

export async function getCleaningTasks(): Promise<import('./types').CleaningTasksPage> {
  return request<import('./types').CleaningTasksPage>('/cleaning/tasks')
}

export async function advanceCleaningTask(taskId: string, payload: { idempotencyKey: string; note?: string; confirmPublication?: boolean }): Promise<import('./types').CleaningAdvanceResult> {
  return request<import('./types').CleaningAdvanceResult>(`/cleaning/tasks/${encodeURIComponent(taskId)}/advance`, { method: 'POST', body: JSON.stringify(payload) })
}

export async function getPublished(query: { page: number; pageSize: number; search?: string; siteId?: string; rule?: string }): Promise<PublishedPage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.search) params.set('search', query.search)
  if (query.siteId) params.set('site_id', query.siteId)
  if (query.rule) params.set('rule', query.rule)
  return request<PublishedPage>(`/published?${params.toString()}`)
}

export async function getMetadataSummary(): Promise<MetadataSummary> {
  return request<MetadataSummary>('/metadata/summary')
}

export async function getMetadataCatalog(query: { page: number; pageSize: number; search?: string; conceptType?: string; aiCategory?: string; semanticStatus?: string; sourceSchema?: string }): Promise<MetadataCatalogPage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.search) params.set('search', query.search)
  if (query.conceptType && query.conceptType !== 'all') params.set('concept_type', query.conceptType)
  if (query.aiCategory && query.aiCategory !== 'all') params.set('ai_category', query.aiCategory)
  if (query.semanticStatus && query.semanticStatus !== 'all') params.set('semantic_status', query.semanticStatus)
  if (query.sourceSchema && query.sourceSchema !== 'all') params.set('source_schema', query.sourceSchema)
  return request<MetadataCatalogPage>(`/metadata/catalog?${params.toString()}`)
}

export async function getMetadataCatalogDetail(semanticId: string): Promise<MetadataCatalogDetail> {
  return request<MetadataCatalogDetail>(`/metadata/catalog/${encodeURIComponent(semanticId)}`)
}

export async function getUnifiedDeviceSummary(): Promise<UnifiedDeviceSummary> {
  return request<UnifiedDeviceSummary>('/unified-devices/summary')
}

export async function getUnifiedDevices(query: { page: number; pageSize: number; search?: string; sourceSchema?: string; siteId?: string }): Promise<UnifiedDevicePage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.search) params.set('search', query.search)
  if (query.sourceSchema && query.sourceSchema !== 'all') params.set('source_schema', query.sourceSchema)
  if (query.siteId) params.set('site_id', query.siteId)
  return request<UnifiedDevicePage>(`/unified-devices?${params.toString()}`)
}

export async function getUnifiedDevice(unifiedDeviceId: string): Promise<UnifiedDeviceDetail> {
  return request<UnifiedDeviceDetail>(`/unified-devices/${encodeURIComponent(unifiedDeviceId)}`)
}

export async function getUnifiedLocationSummary(): Promise<UnifiedLocationSummary> {
  return request<UnifiedLocationSummary>('/unified-locations/summary')
}

export async function getUnifiedLocations(query: { page: number; pageSize: number; search?: string; sourceSchema?: string; siteId?: string }): Promise<UnifiedLocationPage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.search) params.set('search', query.search)
  if (query.sourceSchema && query.sourceSchema !== 'all') params.set('source_schema', query.sourceSchema)
  if (query.siteId) params.set('site_id', query.siteId)
  return request<UnifiedLocationPage>(`/unified-locations?${params.toString()}`)
}

export async function getUnifiedLocation(locationRecordId: string): Promise<UnifiedLocationDetail> {
  return request<UnifiedLocationDetail>(`/unified-locations/${encodeURIComponent(locationRecordId)}`)
}

export async function getKnowledgeAssetSummary(): Promise<KnowledgeAssetSummary> {
  return request<KnowledgeAssetSummary>('/knowledge-assets/summary')
}

export async function getKnowledgeAssets(query: { page: number; pageSize: number; search?: string; assetType?: string; status?: string }): Promise<KnowledgeAssetPage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.search) params.set('search', query.search)
  if (query.assetType && query.assetType !== 'all') params.set('asset_type', query.assetType)
  if (query.status && query.status !== 'all') params.set('status', query.status)
  return request<KnowledgeAssetPage>(`/knowledge-assets?${params.toString()}`)
}

export async function getKnowledgeAsset(assetId: string): Promise<KnowledgeAssetDetail> {
  return request<KnowledgeAssetDetail>(`/knowledge-assets/${encodeURIComponent(assetId)}`)
}

export async function getSemanticFactSummary(): Promise<SemanticFactSummary> {
  return request<SemanticFactSummary>('/semantic-facts/summary')
}

export async function getSemanticFacts(query: { page: number; pageSize: number; search?: string; factType?: string; status?: string }): Promise<SemanticFactPage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.search) params.set('search', query.search)
  if (query.factType && query.factType !== 'all') params.set('fact_type', query.factType)
  if (query.status && query.status !== 'all') params.set('status', query.status)
  return request<SemanticFactPage>(`/semantic-facts?${params.toString()}`)
}

export async function getSemanticFact(factId: string): Promise<SemanticFactDetail> {
  return request<SemanticFactDetail>(`/semantic-facts/${encodeURIComponent(factId)}`)
}

export async function getSemanticStatusDictionarySummary(): Promise<SemanticStatusDictionarySummary> {
  return request<SemanticStatusDictionarySummary>('/semantic-status-dictionary/summary')
}

export async function getSemanticStatusDictionary(query: { page: number; pageSize: number; search?: string; sourceSchema?: string; sourceTable?: string; mappingStatus?: string }): Promise<SemanticStatusDictionaryPage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.search) params.set('search', query.search)
  if (query.sourceSchema && query.sourceSchema !== 'all') params.set('source_schema', query.sourceSchema)
  if (query.sourceTable && query.sourceTable !== 'all') params.set('source_table', query.sourceTable)
  if (query.mappingStatus && query.mappingStatus !== 'all') params.set('mapping_status', query.mappingStatus)
  return request<SemanticStatusDictionaryPage>(`/semantic-status-dictionary?${params.toString()}`)
}

export async function getSemanticStatusDictionaryItem(statusId: string): Promise<SemanticStatusDictionaryDetail> {
  return request<SemanticStatusDictionaryDetail>(`/semantic-status-dictionary/${encodeURIComponent(statusId)}`)
}

export async function reviewSemanticStatusDictionary(statusId: string, payload: { decision: 'approved' | 'rejected'; canonicalState?: string; businessMeaning?: string; notes?: string; reviewer?: string }): Promise<SemanticStatusDictionaryDetail> {
  return request<SemanticStatusDictionaryDetail>(`/semantic-status-dictionary/${encodeURIComponent(statusId)}/review`, { method: 'POST', body: JSON.stringify({ ...payload, businessMeaning: payload.businessMeaning ?? '', notes: payload.notes ?? '', reviewer: payload.reviewer ?? REVIEWER_NAME }) })
}

export async function replaySemanticStatusDictionary(): Promise<SemanticStatusDictionaryReplay> {
  return request<SemanticStatusDictionaryReplay>('/semantic-status-dictionary/replay', { method: 'POST' })
}

export async function getSemanticStatesSummary(): Promise<import('./types').SemanticStatesSummary> {
  return request('/semantic-states/summary')
}

export async function getSemanticEventSummary(): Promise<SemanticEventSummary> {
  return request<SemanticEventSummary>('/semantic-events/summary')
}

export async function getSemanticEvents(query: { page: number; pageSize: number; eventType?: string; status?: string; siteId?: string; search?: string }): Promise<SemanticEventPage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.eventType && query.eventType !== 'all') params.set('event_type', query.eventType)
  if (query.status && query.status !== 'all') params.set('status', query.status)
  if (query.siteId) params.set('site_id', query.siteId)
  if (query.search) params.set('search', query.search)
  return request<SemanticEventPage>(`/semantic-events?${params.toString()}`)
}

export async function getSemanticEvent(eventId: string): Promise<SemanticEventDetail> {
  return request<SemanticEventDetail>(`/semantic-events/${encodeURIComponent(eventId)}`)
}

export async function getSemanticDecisionSummary(): Promise<SemanticDecisionSummary> {
  return request<SemanticDecisionSummary>('/semantic-decisions/summary')
}

export async function getSemanticDecisions(query: { page: number; pageSize: number; status?: string; requiresAction?: string; search?: string }): Promise<SemanticDecisionPage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.status && query.status !== 'all') params.set('status', query.status)
  if (query.requiresAction && query.requiresAction !== 'all') params.set('requires_action', query.requiresAction)
  if (query.search) params.set('search', query.search)
  return request<SemanticDecisionPage>(`/semantic-decisions?${params.toString()}`)
}

export async function getSemanticActionPlans(query: { page: number; pageSize: number; status?: string; search?: string }): Promise<SemanticActionPlanPage> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) })
  if (query.status && query.status !== 'all') params.set('status', query.status)
  if (query.search) params.set('search', query.search)
  return request<SemanticActionPlanPage>(`/semantic-action-plans?${params.toString()}`)
}

export async function getSemanticActionPlan(planId: string): Promise<SemanticActionPlanDetail> {
  return request<SemanticActionPlanDetail>(`/semantic-action-plans/${encodeURIComponent(planId)}`)
}

export async function reviewSemanticActionPlan(planId: string, payload: { decision: 'approved' | 'rejected'; reviewer?: string; comment?: string }): Promise<{ planId: string; status: string; approvalReceipt: string; sourceWrite: boolean; formalPublication: boolean; note: string }> {
  return request(`/semantic-action-plans/${encodeURIComponent(planId)}/approval`, { method: 'POST', body: JSON.stringify({ ...payload, reviewer: payload.reviewer ?? REVIEWER_NAME, comment: payload.comment ?? '' }) })
}

export async function previewSemanticExecution(payload: { assetId: string; assetVersionId?: string; targetScope?: string; sampleSize?: number; idempotencyKey: string; note?: string }): Promise<import('./types').SemanticExecutionPreview> {
  return request('/semantic-execution/preview', { method: 'POST', body: JSON.stringify({ ...payload, targetScope: payload.targetScope ?? 'local_semantic_layer', sampleSize: payload.sampleSize ?? 200 }) })
}

export function metadataExportUrl(query: { search?: string; conceptType?: string; aiCategory?: string; semanticStatus?: string; sourceSchema?: string }): string {
  const params = new URLSearchParams()
  if (query.search) params.set('search', query.search)
  if (query.conceptType && query.conceptType !== 'all') params.set('concept_type', query.conceptType)
  if (query.aiCategory && query.aiCategory !== 'all') params.set('ai_category', query.aiCategory)
  if (query.semanticStatus && query.semanticStatus !== 'all') params.set('semantic_status', query.semanticStatus)
  if (query.sourceSchema && query.sourceSchema !== 'all') params.set('source_schema', query.sourceSchema)
  const suffix = params.toString()
  return `${API_BASE}/metadata/export${suffix ? `?${suffix}` : ''}`
}

export async function getRuleAgentProfile(sampleSize = 120): Promise<{ profile: import('./types').RuleAgentProfile; configured: boolean; sourceWrite: boolean }> {
  return request(`/rule-agent/profile?sample_size=${sampleSize}`)
}

export async function getRuleAgentStatus(): Promise<{ configured: boolean; model: string; baseUrlConfigured: boolean; apiKeyConfigured: boolean; timeoutSeconds: number; maxAttempts: number; failedRunCount: number; latestRun: { run_id: string; status: string; error_code: string | null; retryable: number; attempt_count: number; error_message: string | null; created_at: string; finished_at: string | null } | null; fallbackPolicy: string; sourceWrite: boolean; formalPublication: boolean }> {
  return request('/rule-agent/status')
}

export async function runPendingAgentAudit(payload: { idempotencyKey: string; candidateIds?: string[]; batchSize?: number; note?: string }): Promise<import('./types').CandidateAgentAuditResult> {
  return request('/ai-review/agent-audit', { method: 'POST', body: JSON.stringify({ ...payload, candidateIds: payload.candidateIds ?? [], batchSize: payload.batchSize ?? 100 }) })
}

export async function runSemanticReasoning(payload: { idempotencyKey: string; sampleSize?: number; clusterLimit?: number; clusterKeys?: string[]; note?: string }): Promise<import('./types').SemanticReasoningResult> {
  return request('/semantic-reasoning/analyze', { method: 'POST', body: JSON.stringify({ ...payload, clusterKeys: payload.clusterKeys ?? [] }) })
}

export async function getLatestSemanticReasoning(): Promise<{ run: { runId: string; status: string; clusterCount: number; sampleCount: number; model: string; createdAt: string; finishedAt: string | null } | null; items: import('./types').SemanticReasoningItem[]; sourceWrite: boolean; formalPublication: boolean }> {
  return request('/semantic-reasoning/latest')
}

export async function getRuleAgentProposals(status = 'all'): Promise<import('./types').RuleAgentPage> {
  return request<import('./types').RuleAgentPage>(`/rule-agent/proposals?status=${encodeURIComponent(status)}`)
}

export async function aiReviewRuleAgentProposals(payload: { idempotencyKey: string; proposalIds?: string[]; note?: string }): Promise<{ status: string; reviewedCount: number; acceptedCount: number; needsReviewCount: number; rejectedCount: number; autoProcessedCount: number; proposals: import('./types').RuleAgentProposal[]; sourceWrite: boolean; formalPublication: boolean }> {
  return request('/rule-agent/ai-review', { method: 'POST', body: JSON.stringify({ ...payload, proposalIds: payload.proposalIds ?? [] }) })
}

export async function discoverRules(payload: { idempotencyKey: string; sampleSize?: number; note?: string }): Promise<{ runId: string; status: string; profile?: import('./types').RuleAgentProfile; proposals: import('./types').RuleAgentProposal[]; proposalCount?: number; filterStats?: { version: string; modelProposalCount: number; eligibleProposalCount: number; filteredProposalCount: number; minMatchedCount: number; minEvidenceSamples: number }; sourceWrite: boolean; formalPublication: boolean }> {
  return request('/rule-agent/discover', { method: 'POST', body: JSON.stringify(payload) })
}

export async function previewRuleAgentProposal(proposalId: string, payload: { idempotencyKey: string; note?: string }): Promise<{ proposal: import('./types').RuleAgentProposal; sourceWrite: boolean; formalPublication: boolean }> {
  return request(`/rule-agent/proposals/${encodeURIComponent(proposalId)}/preview`, { method: 'POST', body: JSON.stringify(payload) })
}

export async function replayRuleAgentProposal(proposalId: string, payload: { idempotencyKey: string; note?: string }): Promise<{ proposal: import('./types').RuleAgentProposal; status: string; failures: Array<{ candidateId: string; reason: string }>; sourceWrite: boolean; formalPublication: boolean }> {
  return request(`/rule-agent/proposals/${encodeURIComponent(proposalId)}/replay`, { method: 'POST', body: JSON.stringify(payload) })
}

export async function confirmRuleAgentProposal(proposalId: string, payload: { idempotencyKey: string; note?: string }): Promise<{ proposal: import('./types').RuleAgentProposal; status: string; sourceWrite: boolean; formalPublication: boolean }> {
  return request(`/rule-agent/proposals/${encodeURIComponent(proposalId)}/confirm`, { method: 'POST', body: JSON.stringify(payload) })
}

export async function enableRuleAgentProposal(proposalId: string, payload: { idempotencyKey: string; note?: string }): Promise<{ proposal: import('./types').RuleAgentProposal; status: string; replayId: string; sourceWrite: boolean; formalPublication: boolean }> {
  return request(`/rule-agent/proposals/${encodeURIComponent(proposalId)}/enable`, { method: 'POST', body: JSON.stringify(payload) })
}

export async function queueRuleAgentProposal(proposalId: string, payload: { idempotencyKey: string; note?: string }): Promise<{ proposalId: string; replayId: string; targetCount: number; appliedCount: number; skippedCount: number; status: string; sourceWrite: boolean; formalPublication: boolean }> {
  return request(`/rule-agent/proposals/${encodeURIComponent(proposalId)}/queue`, { method: 'POST', body: JSON.stringify(payload) })
}

export async function getPublishedDetail(publicationId: string): Promise<PublishedRow> {
  return request<PublishedRow>(`/published/${encodeURIComponent(publicationId)}`)
}

export function publishedExportUrl(query: { search?: string; siteId?: string; rule?: string }): string {
  const params = new URLSearchParams()
  if (query.search) params.set('search', query.search)
  if (query.siteId) params.set('site_id', query.siteId)
  if (query.rule) params.set('rule', query.rule)
  const suffix = params.toString()
  return `${API_BASE}/published/export${suffix ? `?${suffix}` : ''}`
}
