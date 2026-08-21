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
  reviewSample: ReviewSampleSummary
}

export interface WorldModelSummary {
  core: Array<{
    key: string
    label: string
    count: number
    status: 'active' | 'blocked' | 'governed' | 'pending' | string
    path: string
    description: string
  }>
  stages: Array<{
    key: string
    label: string
    status: 'completed' | 'in_progress' | 'pending' | string
    detail: string
  }>
  sourceWrite: boolean
  formalPublication: boolean
  sourceSystems: string[]
  latestStatusReplay: Record<string, string | number> | null
  latestStateTransition: Record<string, string | number> | null
  latestOntologyMetaModel: Record<string, string | number> | null
  nextAction: { title: string; description: string; path: string }
}

export interface SemanticCoverage {
  schemaVersion: string
  status: string
  run: Record<string, string | number> | null
  gaps: Array<{
    gap_id: string
    gap_type: string
    scope_key: string
    expected_count: number
    observed_count: number
    status: string
    evidence_required: string
    note: string
    created_at: string
  }>
  sourceWrite: boolean
  formalPublication: boolean
}

export interface SemanticRuntimeContract {
  schemaVersion: string
  status: string
  run: Record<string, string | number> | null
  authority: Array<Record<string, string | number>>
  counts: Record<string, number>
  violations: Array<Record<string, string | number>>
  sourceWrite: boolean
  formalPublication: boolean
}

export interface SemanticGovernanceContract {
  schemaVersion: string
  status: string
  run: Record<string, string | number> | null
  identity: {
    policy: Record<string, string | number> | null
    assertionCount: number
    autoCount: number
    manualCount: number
    reviewRequiredCount: number
    revokedCount: number
    conflictCount: number
  }
  relations: Array<Record<string, string | number>>
  objectSchema: Array<Record<string, string | number>>
  eventRelations: Array<Record<string, string | number>>
  rules: Array<Record<string, string | number>>
  conflicts: Array<Record<string, string | number>>
  violations: Array<Record<string, string | number>>
  sourceWrite: boolean
  formalPublication: boolean
}

export interface CanonicalSemanticSummary {
  schemaVersion: string
  status: string
  run: Record<string, string | number> | null
  standardBaseline: string[]
  graphs: Array<Record<string, string | number | null>>
  counts: { resources: number; statements: number; provenance: number; inferredStatements?: number }
  vocabulary?: Record<string, number>
  provenanceCoverage?: { eligibleStatements?: number; coveredStatements?: number; rate?: number }
  owlRlReplay?: { status?: string; inferred_statement_count?: number; rule_set_version?: string } | null
  artifacts: Record<string, string>
  sourceWrite: boolean
  formalPublication: boolean
}

export interface SemanticSourceTruth {
  schemaVersion: string
  status: 'active' | 'partial'
  authority: string
  canonicalRunId: string | null
  projectedDeviceCount: number
  identityDeviceCount: number
  coverage: number
  canonicalReadRoutes: string[]
  compatibilityReadRoutes: string[]
  controlPlane: string[]
  sourceWrite: boolean
  formalPublication: boolean
}

export interface SemanticIdentityReviewRow {
  review_id: string
  assertion_id: string
  queue_status: string
  reviewer: string | null
  review_note: string
  approval_receipt: string | null
  created_at: string
  reviewed_at: string | null
  source_system: string
  source_schema: string
  source_table_group: string
  source_table: string
  source_row_id: string
  source_key_type: string
  source_key: string
  canonical_object_type: string
  canonical_object_id: string
  assertion_type: string
  assertion_status: string
  confidence: number
  decision_mode: string
  review_required: number
  lifecycle_status: string
  policy_version: string | null
  source_snapshot_id: string
  hasConflict: boolean
  conflicts: Array<Record<string, string | number>>
}

export interface SemanticIdentityReviewPage {
  schemaVersion: string
  status: string
  rows: SemanticIdentityReviewRow[]
  total: number
  page: number
  pageSize: number
  counts: Record<string, number>
  sourceWrite: boolean
  formalPublication: boolean
}

export interface SemanticIdentityReviewDetail {
  schemaVersion: string
  review: SemanticIdentityReviewRow & Record<string, unknown>
  conflicts: Array<Record<string, string | number>>
  audits: Array<Record<string, string | number>>
  sourceWrite: boolean
  formalPublication: boolean
}

export interface OntologyMetaSummary {
  schemaVersion: string
  latestRun: Record<string, string | number> | null
  objectTypeCount: number
  propertyTypeCount: number
  relationTypeCount: number
  eventTypeCount: number
  stateMachineCount: number
  transitionRuleCount: number
  coreObjects: Array<{ object_type: string; display_name: string; kind: string; version: string; review_status: string; status: string }>
  missingCoreObjects: string[]
  relationReviewStatuses: Array<{ value: string; count: number }>
  eventReviewStatuses: Array<{ value: string; count: number }>
  unregisteredRelations: Array<{ predicate: string; subject_type: string; object_type: string }>
  unregisteredEvents: Array<{ event_type: string; subject_type: string }>
  sourceWrite: boolean
  formalPublication: boolean
  policy: string[]
}

export interface ReviewSampleSummary {
  sampleId: string
  sampleName: string
  batchId: string
  sourceSnapshotId: string
  targetCount: number
  selectedCount: number
  status: 'open' | 'completed' | 'cancelled'
  strategy: string
  ruleVersion: string
  validatorVersion: string
  pendingCount: number
  approvedCount: number
  modifiedCount: number
  rejectedCount: number
  deferredCount: number
  strata: Array<{ stratum: string; count: number }>
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
  quickFilter?: 'all' | 'pending' | 'context' | 'low' | 'deferred'
  sampleOnly?: boolean
  sampleSize?: number
}

export interface CandidatePage {
  rows: CandidateRow[]
  total: number
  page: number
  pageSize: number
}

export interface CandidateFacets {
  batchId: string
  sites: Array<{ value: string; count: number }>
  classifications: Array<{ value: string; count: number }>
  sourceWrite: boolean
  formalPublication: boolean
}

export interface ReviewPayload {
  candidateId: string
  decision: 'approved' | 'modified' | 'rejected' | 'deferred'
  reviewedDescription?: string
  note?: string
  idempotencyKey: string
}

export type AiReviewScope = 'sample' | 'batch'

export interface AiReviewPreview {
  scope: AiReviewScope
  batchId: string
  sampleId: string | null
  sampleSelectedCount: number | null
  eligibleCount: number
  pendingCount: number
  policyVersion: string
  engine: string
  rules: string[]
}

export interface AiApprovalResult {
  runId: string
  batchId: string
  scope: AiReviewScope
  policyVersion: string
  eligibleCount: number
  appliedCount: number
  skippedCount: number
  status: string
  replayed: boolean
}

export type AiSampleDecision = 'keep_original' | 'accept_candidate' | 'needs_review'

export interface AiSampleRow {
  sampleId: string
  candidateId: string
  siteId: string
  assetNumber: string
  originalDescription: string
  candidateDescription: string
  diffCategory: string
  diffSignature: string
  kks: string
  locationDescription: string
  locationParent: string
  classificationDescription: string
  confidence: string
  aiDecision: string
  aiConfidence: string
  aiReason: string
  decision: AiSampleDecision | null
  reviewNote: string
  reviewer: string
  reviewedAt: string
}

export interface AiSamplePage {
  rows: AiSampleRow[]
  total: number
  page: number
  pageSize: number
  summary: {
    sampleId: string
    total: number
    reviewed: number
    pending: number
    reviewedByDecision: Record<AiSampleDecision, number>
    aiRecommendation: Record<AiSampleDecision, number>
    pendingByRecommendation: Record<AiSampleDecision, number>
    sites: Array<{ siteId: string; count: number }>
    classifications: Array<{ value: string; count: number }>
  }
}

export interface AiClusterSite {
  siteId: string
  count: number
}

export interface AiClusterSample {
  candidateId: string
  siteId: string
  assetNumber: string
  originalDescription: string
  candidateDescription: string
  kks: string
  locationDescription: string
  locationParent: string
  classificationDescription: string
}

export interface AiClusterRow {
  clusterId: string
  clusterType: 'question_context' | 'leading_minus' | 'terminal_hyphen' | 'other'
  clusterLabel: string
  clusterPattern: string
  ruleSignature: string
  memberCount: number
  reviewedCount: number
  pendingCount: number
  siteIds: string[]
  sites: AiClusterSite[]
  aiDecision: AiSampleDecision
  aiConfidence: number
  aiReason: string
  decision: AiSampleDecision | null
  sample: AiClusterSample
}

export interface AiClusterMember extends AiClusterSample {
  clusterId: string
  clusterType: AiClusterRow['clusterType']
  clusterLabel: string
  clusterPattern: string
  ruleSignature: string
  aiDecision: AiSampleDecision
  aiConfidence: number
  aiReason: string
  memberDecision: AiSampleDecision | null
  confidence: string
  reviewState: string
  reasonCodes: string[]
  appliedRules: string[]
  ruleVersion: string
  validatorVersion: string
  createdAt: string
}

export interface AiClusterPage {
  rows: AiClusterRow[]
  total: number
  page: number
  pageSize: number
  summary: {
    candidateCount: number
    clusterCount: number
    pendingCandidateCount: number
    reviewedCandidateCount: number
    pendingClusterCount: number
    aiRecommendation: Record<AiSampleDecision, number>
    clusterDecision: Record<AiSampleDecision, number>
    sites: Array<{ siteId: string; count: number }>
    classifications: Array<{ value: string; count: number }>
  }
}

export interface AiClusterDetail extends AiClusterRow {
  members: AiClusterMember[]
  total: number
  page: number
  pageSize: number
}

export interface FormalApprovalRow {
  queueId: string
  candidateId: string
  clusterId: string
  replayId: string
  proposedDecision: CandidateReviewState
  proposedDescription: string
  status: CandidateReviewState
  note: string
  createdAt: string
  updatedAt: string
  batchId: string
  siteId: string
  assetNumber: string
  assetId: string
  originalDescription: string
  candidateDescription: string
  reviewState: CandidateReviewState
  publicationState: string
  kks: string
  locationDescription: string
  locationParent: string
  classificationDescription: string
  replayStatus: string
  replayEvaluationCount: number
  replayPassCount: number
  replayFailCount: number
  reviewId: string
  approvalReceipt: string
  reviewer: string
  reviewedAt: string
}

export interface FormalApprovalPage {
  rows: FormalApprovalRow[]
  total: number
  page: number
  pageSize: number
  summary: { pending: number; completed: number }
}

export interface CleaningRuleSummary {
  ruleKey: string
  ruleLabel: string
  total: number
  pending: number
  completed: number
  cleaning: boolean
}

export interface CleaningRow extends FormalApprovalRow {
  cleaningType: string
  ruleKey: string
  ruleLabel: string
  actionLabel: string
}

export interface CleaningPage {
  rows: CleaningRow[]
  total: number
  page: number
  pageSize: number
  summary: {
    totalPending: number
    cleaningPending: number
    cleaningPublished: number
    keepOriginalPending: number
    rules: CleaningRuleSummary[]
  }
}

export interface CleaningRuleRun {
  rule_key: string
  cleaning_type: string
  rule_label: string
  action_label: string
  is_cleaning: number
  replay_id: string
  rule_version: string
  enabled: number
  cleaning_run_id: string
  status: string
  stage: 'task' | 'previewed' | 'replayed' | 'approved' | 'published' | 'failed'
  candidate_count: number
  pending_count: number
  approved_count: number
  published_count: number
  preview_rows: number
  replay_rows: number
  preview_path: string | null
  sample_path: string | null
  preview_id: string | null
  preview_sha256: string | null
  approval_idempotency_key: string | null
  publication_run_id: string | null
  backup_path: string | null
  source_write: number
  formal_publication: number
  last_error: string | null
  updated_at: string
}

export interface CleaningTask {
  taskId: string
  ruleKey: string
  ruleLabel: string
  cleaningType: string
  actionLabel: string
  isCleaning: boolean
  replayId: string
  ruleVersion: string
  stage: 'task' | 'previewed' | 'replayed' | 'approved' | 'published' | 'failed'
  nextAction: 'preview' | 'replay' | 'approval' | 'publication' | 'completed' | string
  availableActions: string[]
  status: string
  candidateCount: number
  pendingCount: number
  approvedCount: number
  publishedCount: number
  previewRows: number
  replayRows: number
  replayStatus: string | null
  replayEvaluationCount: number
  replayPassCount: number
  replayFailCount: number
  previewId: string | null
  previewSha256: string | null
  previewPath: string | null
  samplePath: string | null
  approvalIdempotencyKey: string | null
  publicationRunId: string | null
  backupPath: string | null
  sourceWrite: boolean
  formalPublication: boolean
  lastError: string | null
  createdAt: string
  updatedAt: string
}

export interface CleaningTasksPage {
  tasks: CleaningTask[]
  workflow: string[]
  sourceWrite: boolean
}

export interface CleaningAdvanceResult {
  taskId: string
  action: string
  stage: string
  nextAction?: string
  status: string
  targetCount?: number
  appliedCount?: number
  publishedCount?: number
  previewRows?: number
  passCount?: number
  failCount?: number
  requiresConfirmation?: boolean
  idempotencyKey: string
  sourceWrite: boolean
  formalPublication: boolean
  backupPath?: string
}

export interface CleaningBatchResult {
  targetCount: number
  appliedCount: number
  pendingCount: number
  completedCount: number
  status: string
  idempotencyKey: string
  sourceWrite: boolean
  formalPublication: boolean
}

export interface CleaningPublishResult {
  publicationRunId: string
  targetCount: number
  publishedCount: number
  status: string
  idempotencyKey: string
  backupPath: string
  sourceWrite: boolean
  formalPublication: boolean
}

export interface PublishedRow {
  publicationId: string
  candidateId: string
  reviewId: string
  batchId: string
  sourceSnapshotId: string
  siteId: string
  assetNumber: string
  assetId: string
  originalDescription: string
  finalDescription: string
  kks: string
  locationDescription: string
  locationParent: string
  classificationDescription: string
  appliedRules: string[]
  ruleVersion: string
  validatorVersion: string
  replayId: string
  publishedBy: string
  publishedAt: string
  approvalReceipt: string
  reviewer: string
  reviewedAt: string
}

export interface PublishedPage {
  rows: PublishedRow[]
  total: number
  page: number
  pageSize: number
  sites: Array<{ siteId: string; count: number }>
  rules: Array<{ rule: string; count: number }>
}

export interface MetadataSemanticItem {
  semanticId: string
  dictionaryVersion: string
  conceptType: string
  semanticKey: string
  canonicalName: string
  semanticLabelCandidate: string
  description: string
  dataType: string
  length: string
  required: string
  domainId: string
  parentOrTable: string
  sourceSchemas: string
  crossSchemaStatus: string
  aiCategory: string
  aiConfidence: string
  aiReason: string
  semanticStatus: string
  evidence: string
  loadedAt: string
}

export interface MetadataSummary {
  resultVersion: string
  runId: string
  loadedAt: string
  total: number
  conceptTypes: Array<{ value: string; count: number }>
  aiCategories: Array<{ value: string; count: number }>
  semanticStatuses: Array<{ value: string; count: number }>
  validation: { findingCount: number; findingTypes: Array<{ value: string; count: number }> }
  sourceWrite: boolean
  formalPublication: boolean
  source: string
  duckdbAvailable: boolean
}

export interface MetadataCatalogPage {
  items: MetadataSemanticItem[]
  total: number
  page: number
  pageSize: number
  sourceWrite: boolean
  formalPublication: boolean
}

export interface MetadataCatalogDetail {
  item: MetadataSemanticItem
  related: MetadataSemanticItem[]
  sourceWrite: boolean
  formalPublication: boolean
}

export interface UnifiedDeviceSourceSystem {
  system_key: string
  display_name: string
  connection_kind: string
  snapshot_id: string
  read_only: number
  status: string
  created_at: string
}

export interface UnifiedDeviceSummary {
  runId: string
  sourceSnapshotId: string
  identityDatabase: string
  unifiedDeviceCount: number
  identityMapCount: number
  identityMapByStatus: Record<string, number>
  relationCount: number
  relationByStatus: Record<string, number>
  businessLinkCount: number
  acceptedBusinessLinkCount: number
  businessLinksByType: Record<string, Record<string, number>>
  businessEventCount: number
  businessEventsByTypeStatus: Record<string, Record<string, number>>
  crossSystemCandidateCount: number
  sourceSystems: UnifiedDeviceSourceSystem[]
  sourceWrite: boolean
  formalPublication: boolean
  policy: string[]
}

export interface UnifiedDeviceRow {
  unifiedDeviceId: string
  sourceSchema: string
  siteId: string
  assetNumber: string
  sourceAssetId: string
  canonicalName: string
  locationCode: string
  parentAssetNumber: string
  organization: string
  classificationId: string
  status: string
  seedStatus: string
  mappingCount: number
  businessLinkCount: number
  acceptedBusinessLinkCount: number
  businessReviewCount: number
  relationCount: number
  mappingStatus: string
  sourceSnapshotId: string
}

export interface UnifiedDevicePage {
  rows: UnifiedDeviceRow[]
  total: number
  page: number
  pageSize: number
}

export interface UnifiedDeviceMapping {
  source_schema: string
  source_table_group: string
  source_table: string
  source_row_id: string
  source_key_type: string
  source_key: string
  site_id: string | null
  raw_description: string | null
  location_code: string | null
  match_method: string | null
  match_confidence: number | null
  status: string
  evidence_json: string
  source_snapshot_id: string
}

export interface UnifiedBusinessLink {
  link_id: string
  source_schema: string
  source_table_group: string
  source_table: string
  source_row_id: string
  business_type: string
  source_key_type: string
  source_key: string
  status: string
  confidence: number
  evidence_json: string
  source_snapshot_id: string
}

export interface UnifiedDeviceRelation {
  relation_id: string
  subject_unified_device_id: string
  predicate: string
  object_unified_device_id: string | null
  source_schema: string
  source_table: string
  source_row_id: string
  confidence: number
  status: string
  evidence_json: string
  source_snapshot_id: string
}

export interface UnifiedDeviceDetail {
  device: UnifiedDeviceRow
  sourceIdentity: { sourceIdentityKey: string; sourceAssetId: string; masterSourceSchema: string }
  mappings: UnifiedDeviceMapping[]
  businessLinks: UnifiedBusinessLink[]
  relations: UnifiedDeviceRelation[]
  relatedDevices: Array<{ unified_device_id: string; master_source_schema: string; site_id: string; asset_number: string; canonical_name: string }>
  location: UnifiedLocation | null
  locationHierarchy: Array<Record<string, string | null>>
  businessRecordEvidence: UnifiedBusinessRecordEvidence[]
  sourceWrite: boolean
  formalPublication: boolean
}

export interface UnifiedLocation {
  locationRecordId: string
  sourceSchema: string
  siteId: string
  locationCode: string
  sourceLocationId: string
  description: string
  parentLocation: string
  status: string
  classificationId: string
  sourceSnapshotId: string
  deviceCount?: number
  businessRecordCount?: number
}

export interface UnifiedLocationPage {
  rows: UnifiedLocation[]
  total: number
  page: number
  pageSize: number
  sourceWrite: boolean
  formalPublication: boolean
}

export interface UnifiedLocationSummary {
  locationCount: number
  hierarchyCount: number
  sourceCounts: Array<{ sourceSchema: string; count: number }>
  hierarchyBySource: Array<{ sourceSchema: string; count: number }>
  sourceWrite: boolean
  formalPublication: boolean
  policy: string[]
}

export interface UnifiedBusinessRecordEvidence {
  event_record_id: string
  unified_device_id: string | null
  source_schema: string
  event_type: string
  source_table: string
  source_row_id: string
  site_id: string | null
  location_code: string | null
  event_time: string | null
  status: string | null
  description: string | null
  source_snapshot_id?: string
  link_status: string
  evidence_json: string
}

export interface UnifiedLocationDetail {
  location: UnifiedLocation
  hierarchy: Array<Record<string, string | null>>
  devices: Array<Record<string, string | null>>
  businessRecords: UnifiedBusinessRecordEvidence[]
  sourceWrite: boolean
  formalPublication: boolean
}

export interface KnowledgeAssetItem {
  assetId: string
  assetKey: string
  assetType: string
  title: string
  canonicalDefinition: string
  currentVersion: string
  status: string
  sourceScope: string
  sourceCount: number
  issueCount: number
  createdAt: string
  updatedAt: string
}

export interface KnowledgeAssetSummary {
  runId: string
  assetCount: number
  versionCount: number
  sourceCount: number
  bindingCount: number
  issueCount: number
  assetTypes: Array<{ value: string; count: number }>
  statuses: Array<{ value: string; count: number }>
  openIssues: Array<{ value: string; count: number }>
  machineContract: Record<string, string | number> | null
  machineConstraintGates: Array<{ value: string; count: number }>
  businessObjectTypes: Array<{ object_type: string; display_name: string; description: string; parent_object_type: string | null; version: string; status: string }>
  businessObjectRelationCount: number
  businessObjectRelationStatuses: Array<{ value: string; count: number }>
  sourceWrite: boolean
  formalPublication: boolean
  policy: string[]
}

export interface KnowledgeAssetPage {
  items: KnowledgeAssetItem[]
  total: number
  page: number
  pageSize: number
  sourceWrite: boolean
  formalPublication: boolean
}

export interface KnowledgeAssetDetail {
  asset: KnowledgeAssetItem
  versions: Array<Record<string, string | number | null>>
  parts: Array<Record<string, string | number | null>>
  sources: Array<Record<string, string | null>>
  bindings: Array<Record<string, string | null>>
  issues: Array<Record<string, string | null>>
  machineContract: Record<string, string | number | null> | null
  machineDecisions: Array<Record<string, string | number | null>>
  machineCalculations: Array<Record<string, string | number | null>>
  machineConstraints: Array<Record<string, string | number | null>>
  machineActions: Array<Record<string, string | number | null>>
  sourceWrite: boolean
  formalPublication: boolean
}

export interface SemanticExecutionPreview {
  runId: string
  assetId: string
  assetVersionId: string
  mode: string
  status: string
  targetCount: number
  gates: Array<{ key: string; status: string; severity: string; message: string }>
  actionPlan: Record<string, unknown>
  idempotentReplay: boolean
  sourceWrite: boolean
  formalPublication: boolean
}

export interface SemanticFactItem {
  fact_id: string
  fact_type: string
  subject_type: string
  subject_key: string
  predicate: string
  value_json: string
  unit: string | null
  source_schema: string
  source_table: string
  source_row_id: string
  source_snapshot_id: string | null
  status: string
  confidence: number
  observed_at: string | null
  created_at: string
}

export interface SemanticFactSummary {
  latestRun: Record<string, string | number>
  factCount: number
  observedFactCount: number
  derivedFactCount: number
  derivationCount: number
  decisionCount: number
  actionCount: number
  logicRuleCount: number
  logicReadyCount: number
  logicActionSpecCount: number
  deterministicRuleCount: number
  latestReasoningRun: Record<string, string | number> | null
  factStatuses: Array<{ value: string; count: number }>
  factTypes: Array<{ value: string; count: number }>
  decisionStatuses: Array<{ value: string; count: number }>
  actionStatuses: Array<{ value: string; count: number }>
  sourceWrite: boolean
  formalPublication: boolean
  policy: string[]
}

export interface SemanticFactPage {
  items: SemanticFactItem[]
  total: number
  page: number
  pageSize: number
  sourceWrite: boolean
  formalPublication: boolean
}

export interface SemanticFactDetail {
  fact: SemanticFactItem
  derivations: Array<Record<string, string | number | null>>
  decisions: Array<Record<string, string | number | null>>
  actions: Array<Record<string, string | number | null>>
  sourceWrite: boolean
  formalPublication: boolean
}

export interface SemanticStatusDictionaryItem {
  status_id: string
  source_schema: string
  source_table: string
  raw_status: string
  status_present: number
  evidence_count: number
  linked_device_evidence_count: number
  example_records_json: string
  source_snapshot_id: string
  mapping_status: 'pending' | 'approved' | 'rejected' | string
  canonical_state: string | null
  mapping_version: number
  business_meaning: string | null
  mapping_notes: string | null
  reviewer: string | null
  reviewed_at: string | null
  created_at: string
  updated_at: string
  examples?: Array<Record<string, string | number | null>>
}

export interface SemanticStatusDictionarySummary {
  latestRun: Record<string, string | number>
  candidateCount: number
  pendingCount: number
  approvedCount: number
  rejectedCount: number
  evidenceRowCount: number
  systems: Array<{ value: string; count: number }>
  mappingStatuses: Array<{ value: string; count: number }>
  canonicalStates: Array<{ value: string; display_name: string; description: string; is_terminal: number; sort_order: number }>
  latestReplay: Record<string, string | number> | null
  sourceWrite: boolean
  formalPublication: boolean
  policy: string[]
}

export interface SemanticStatusDictionaryPage {
  items: SemanticStatusDictionaryItem[]
  total: number
  page: number
  pageSize: number
  sourceWrite: boolean
  formalPublication: boolean
}

export interface SemanticStatusDictionaryDetail {
  item: SemanticStatusDictionaryItem
  reviews?: Array<Record<string, string | number | null>>
  sourceWrite: boolean
  formalPublication: boolean
}

export interface SemanticStatusDictionaryReplay {
  run_id: string
  approved_mapping_count: number
  input_fact_count: number
  matched_fact_count: number
  decision_count: number
  derived_fact_count: number
  action_count: number
  status: string
  note: string
  source_write: boolean
  formal_publication: boolean
}

export interface SemanticStatesSummary {
  latestRun: Record<string, string | number> | null
  currentStateCount: number
  transitionCount: number
  reviewTransitionCount: number
  states: Array<{ value: string; display_name: string; count: number }>
  sourceWrite: boolean
  formalPublication: boolean
  policy: string[]
}

export interface SemanticEventItem {
  event_id: string
  event_type: 'InspectionEvent' | 'DefectEvent' | 'WorkOrderEvent' | 'BusinessEvent' | string
  subject_type: string
  subject_key: string
  source_event_id: string | null
  source_schema: string
  source_table: string
  source_row_id: string
  source_snapshot_id: string
  site_id: string | null
  location_code: string | null
  occurred_at: string | null
  recorded_at: string | null
  raw_status: string | null
  description: string | null
  identity_status: string
  payload_json: string
  confidence: number
  status: string
  current_state?: string | null
  current_state_display?: string | null
}

export interface SemanticEventSummary {
  latestRun: Record<string, string | number>
  eventCount: number
  reviewEventCount: number
  eventTypes: Array<{ value: string; count: number }>
  sources: Array<{ value: string; count: number }>
  sourceWrite: boolean
  formalPublication: boolean
  policy: string[]
}

export interface SemanticEventPage {
  items: SemanticEventItem[]
  total: number
  page: number
  pageSize: number
  sourceWrite: boolean
  formalPublication: boolean
}

export interface SemanticEventDetail {
  event: SemanticEventItem & { payload: Record<string, unknown> }
  sourceFacts: Array<Record<string, string | number | null>>
  transitions: Array<Record<string, string | number | null>>
  currentState: Record<string, string | number | null> | null
  sourceWrite: boolean
  formalPublication: boolean
}

export interface SemanticDecisionItem {
  decision_id: string
  subject_type: string
  subject_key: string
  rule_asset_id: string
  rule_version_id: string
  input_fact_ids_json: string
  input_context_json: string
  decision: string
  confidence: number
  explanation: string
  status: string
  requires_action: number
  created_at: string
  plan_id?: string | null
  action_type?: string | null
  plan_status?: string | null
  plan_requires_approval?: number | null
}

export interface SemanticActionPlanItem {
  plan_id: string
  decision_id: string
  action_id: string | null
  action_key: string
  action_type: string
  target_type: string
  target_key: string | null
  payload_json: string
  reason: string
  risk_level: string
  requires_approval: number
  status: string
  idempotency_key: string
  source_write: number
  formal_publication: number
  created_at: string
  updated_at: string
  approval_status?: string | null
  reviewer?: string | null
  comment?: string | null
  approval_receipt?: string | null
}

export interface SemanticDecisionSummary {
  latestRun: Record<string, string | number>
  decisionCount: number
  actionRuleCount: number
  actionRuleMatchCount: number
  riskEvidenceCount: number
  actionPlanCount: number
  pendingApprovalCount: number
  approvalCount: number
  decisionStatuses: Array<{ value: string; count: number }>
  planStatuses: Array<{ value: string; count: number }>
  approvalStatuses: Array<{ value: string; count: number }>
  sourceWrite: boolean
  formalPublication: boolean
  policy: string[]
}

export interface SemanticDecisionPage {
  items: SemanticDecisionItem[]
  total: number
  page: number
  pageSize: number
  sourceWrite: boolean
  formalPublication: boolean
}

export interface SemanticActionPlanPage {
  items: SemanticActionPlanItem[]
  total: number
  page: number
  pageSize: number
  sourceWrite: boolean
  formalPublication: boolean
}

export interface SemanticActionPlanDetail {
  plan: SemanticActionPlanItem & { payload: Record<string, unknown> }
  decision: SemanticDecisionItem | null
  approval: Record<string, string | number | null> | null
  sourceWrite: boolean
  formalPublication: boolean
}

export interface RuleAgentProfile {
  batchId: string
  sourceSnapshotId: string
  eligibleCount: number
  changedCount: number
  sampleCount: number
  sites: Array<{ siteId: string; count: number }>
  classifications: Array<{ value: string; count: number }>
  examples: Array<{ candidateId: string; siteId: string; assetNumber: string; originalDescription: string; candidateDescription: string; kks: string; locationDescription: string; locationParent: string; classificationDescription: string }>
  localRuleCatalog?: Array<{ patternKey: string; ruleKey: string; operation: string; matchedCount: number; sampleCount: number; evaluationCount: number; evaluationFailCount: number }>
  blockedRuleCatalog?: Array<{ patternKey: string; matchedCount: number; sampleCount: number; reason: string; evaluationCount: number; evaluationFailCount: number }>
}

export interface SemanticReasoningItem {
  reasoningItemId: string
  runId: string
  clusterKey: string
  decision: 'propose_rule' | 'keep_original' | 'needs_review' | string
  hypothesis: string
  evidence: Record<string, unknown>
  counterexamples: unknown[]
  candidateRule: Record<string, unknown>
  confidence: number
  riskLevel: 'low' | 'medium' | 'high' | string
  requiredChecks: string[]
  createdAt: string
}

export interface SemanticReasoningResult {
  runId: string
  status: string
  clusterCount: number
  sampleCount: number
  items: SemanticReasoningItem[]
  sourceWrite: boolean
  formalPublication: boolean
}

export interface CandidateAgentAuditResult {
  runId: string
  batchId: string
  policyVersion: string
  candidateCount: number
  approvedCount: number
  needsReviewCount: number
  rejectedCount: number
  isolatedCount: number
  skippedCount: number
  remainingEligibleCount: number
  remainingPendingCount: number
  isolatedTotalCount: number
  status: string
  decisions: Array<{ candidateId: string; agentDecision: string; confidence: number; reason: string; decision: string; localGate: string }>
  sourceWrite: boolean
  formalPublication: boolean
}

export interface AiAgentAuditPreview {
  batchId: string
  pendingCount: number
  eligibleCount: number
  nextBatchSize: number
  isolatedCount: number
  auditedCount: number
  siteCounts: Array<{ siteId: string; count: number }>
  batchSize: number
  batchSizeOptions: number[]
  selectionStrategy: string
  policyVersion: string
  model: string
  configured: boolean
  sourceWrite: boolean
  formalPublication: boolean
  workflow: string[]
  gates: string[]
}

export interface RuleAgentProposal {
  proposalId: string
  runId: string
  ruleKey: string
  ruleVersion: string
  title: string
  objective: string
  operation: string
  condition: Record<string, unknown>
  parameters: Record<string, unknown>
  scope: Record<string, unknown>
  evidence: Record<string, unknown>
  examples: Array<Record<string, string>>
  expectedCount: number
  confidence: number
  riskLevel: 'low' | 'medium' | 'high'
  status: string
  discoveryFilterStatus: 'eligible' | 'filtered'
  discoveryFilterReason: string | null
  discoveryFilteredAt: string | null
  previewPath: string | null
  samplePath: string | null
  previewCount: number
  replayCount: number
  replayPassCount: number
  replayFailCount: number
  evaluationReplayId: string | null
  evaluationCount: number
  evaluationPassCount: number
  evaluationFailCount: number
  agentReviewDecision: string | null
  agentReviewConfidence: number
  agentReviewReason: string | null
  agentReviewVersion: string | null
  agentReviewedAt: string | null
  replayMessage: string | null
  createdAt: string
  updatedAt: string
}

export interface RuleAgentPage {
  proposals: RuleAgentProposal[]
  filteredProposalCount: number
  runs: Array<{ run_id: string; batch_id: string; eligible_count: number; sampled_count: number; model: string; status: string; created_at: string; finished_at: string | null; error_message: string | null; error_code?: string | null; retryable?: number; attempt_count?: number; fallback_used?: number }>
  sourceWrite: boolean
}
