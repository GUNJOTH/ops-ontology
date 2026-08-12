PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS system_setting (
  setting_key TEXT PRIMARY KEY,
  setting_value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_connection (
  connection_id TEXT PRIMARY KEY,
  source_system TEXT NOT NULL,
  source_schema TEXT NOT NULL,
  host TEXT NOT NULL,
  port INTEGER NOT NULL,
  access_mode TEXT NOT NULL CHECK (access_mode = 'read_only'),
  secret_reference TEXT,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  created_at TEXT NOT NULL,
  UNIQUE (source_system, source_schema, host, port)
);

CREATE TABLE IF NOT EXISTS source_snapshot (
  source_snapshot_id TEXT PRIMARY KEY,
  connection_id TEXT REFERENCES source_connection(connection_id),
  source_table TEXT NOT NULL,
  source_filter TEXT,
  source_row_count INTEGER NOT NULL,
  distinct_identity_count INTEGER NOT NULL,
  snapshot_hash TEXT NOT NULL,
  snapshot_path TEXT,
  source_write INTEGER NOT NULL DEFAULT 0 CHECK (source_write = 0),
  captured_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('captured','verified','rejected'))
);

CREATE TABLE IF NOT EXISTS batch_run (
  batch_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL UNIQUE,
  source_snapshot_id TEXT NOT NULL REFERENCES source_snapshot(source_snapshot_id),
  batch_type TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  validator_version TEXT NOT NULL,
  input_count INTEGER NOT NULL,
  candidate_count INTEGER NOT NULL DEFAULT 0,
  needs_review_count INTEGER NOT NULL DEFAULT 0,
  blocked_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  approved_count INTEGER NOT NULL DEFAULT 0,
  published_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('created','running','completed','failed','superseded')),
  config_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  source_write INTEGER NOT NULL DEFAULT 0 CHECK (source_write = 0),
  formal_publication INTEGER NOT NULL DEFAULT 0 CHECK (formal_publication IN (0,1))
);

CREATE TABLE IF NOT EXISTS device_identity (
  device_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_snapshot_id TEXT NOT NULL REFERENCES source_snapshot(source_snapshot_id),
  source_schema TEXT NOT NULL,
  source_asset_id TEXT,
  site_id TEXT NOT NULL,
  asset_number TEXT NOT NULL,
  source_row_hash TEXT NOT NULL,
  original_description TEXT NOT NULL,
  location_code TEXT,
  location_description TEXT,
  location_parent TEXT,
  classification_description TEXT,
  class_structure_description TEXT,
  context_hash TEXT NOT NULL,
  analytics_row_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  UNIQUE (source_snapshot_id, source_schema, site_id, asset_number)
);

CREATE TABLE IF NOT EXISTS exclusion_record (
  exclusion_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_snapshot_id TEXT NOT NULL REFERENCES source_snapshot(source_snapshot_id),
  candidate_id TEXT,
  source_schema TEXT NOT NULL,
  site_id TEXT NOT NULL,
  asset_number TEXT NOT NULL,
  description TEXT,
  exclusion_stage TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  rule_version TEXT NOT NULL,
  excluded_at TEXT NOT NULL,
  UNIQUE (source_snapshot_id, source_schema, site_id, asset_number, exclusion_stage, reason_code)
);

CREATE TABLE IF NOT EXISTS terminology_rule (
  term_rule_id TEXT PRIMARY KEY,
  rule_key TEXT NOT NULL UNIQUE,
  rule_type TEXT NOT NULL CHECK (rule_type IN ('format','synonym','abbreviation','unit','classification','kks','validation')),
  source_term TEXT NOT NULL,
  target_term TEXT NOT NULL,
  site_scope TEXT,
  classification_scope TEXT,
  context_condition_json TEXT NOT NULL DEFAULT '{}',
  priority INTEGER NOT NULL DEFAULT 100,
  status TEXT NOT NULL CHECK (status IN ('draft','candidate','confirmed','active','disabled','retired')),
  version TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  confirmed_by TEXT,
  confirmed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS semantic_candidate (
  candidate_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES batch_run(batch_id),
  device_id INTEGER NOT NULL REFERENCES device_identity(device_id),
  original_description TEXT NOT NULL,
  candidate_description TEXT NOT NULL,
  semantic_action TEXT NOT NULL,
  confidence TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
  validator_status TEXT NOT NULL CHECK (validator_status IN ('candidate','needs_review','blocked')),
  reason_codes_json TEXT NOT NULL DEFAULT '[]',
  evidence_level TEXT NOT NULL,
  applied_rule_ids_json TEXT NOT NULL DEFAULT '[]',
  candidate_hash TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  validator_version TEXT NOT NULL,
  review_state TEXT NOT NULL DEFAULT 'pending' CHECK (review_state IN ('pending','approved','modified','rejected','deferred')),
  publication_state TEXT NOT NULL DEFAULT 'unpublished' CHECK (publication_state IN ('unpublished','published','excluded','superseded')),
  created_at TEXT NOT NULL,
  UNIQUE (batch_id, device_id)
);

CREATE TABLE IF NOT EXISTS validation_result (
  validation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id TEXT NOT NULL REFERENCES semantic_candidate(candidate_id),
  validator_type TEXT NOT NULL CHECK (validator_type IN ('contract','identity','evidence','semantic','consistency')),
  severity TEXT NOT NULL CHECK (severity IN ('info','warning','hard_failure')),
  outcome TEXT NOT NULL CHECK (outcome IN ('pass','fail')),
  reason_code TEXT NOT NULL,
  message TEXT,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  validator_version TEXT NOT NULL,
  validated_at TEXT NOT NULL,
  UNIQUE (candidate_id, validator_type, reason_code)
);

CREATE TABLE IF NOT EXISTS review_decision (
  review_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL UNIQUE REFERENCES semantic_candidate(candidate_id),
  decision TEXT NOT NULL CHECK (decision IN ('approved','modified','rejected','deferred')),
  reviewed_description TEXT,
  reason_code TEXT NOT NULL,
  review_note TEXT,
  reviewer TEXT NOT NULL,
  approval_receipt TEXT NOT NULL UNIQUE,
  reviewed_at TEXT NOT NULL,
  CHECK (decision NOT IN ('approved','modified') OR length(trim(reviewed_description)) > 0),
  CHECK (decision = 'approved' OR length(trim(COALESCE(review_note,''))) > 0)
);

CREATE TABLE IF NOT EXISTS evaluation_case (
  case_id TEXT PRIMARY KEY,
  source_review_id TEXT REFERENCES review_decision(review_id),
  source_snapshot_id TEXT NOT NULL REFERENCES source_snapshot(source_snapshot_id),
  source_schema TEXT NOT NULL,
  site_id TEXT NOT NULL,
  asset_number TEXT NOT NULL,
  input_description TEXT NOT NULL,
  context_json TEXT NOT NULL DEFAULT '{}',
  expected_decision TEXT NOT NULL,
  expected_description TEXT,
  failure_type TEXT,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  introduced_rule_version TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replay_run (
  replay_id TEXT PRIMARY KEY,
  rule_version TEXT NOT NULL,
  validator_version TEXT NOT NULL,
  evaluation_count INTEGER NOT NULL DEFAULT 0,
  pass_count INTEGER NOT NULL DEFAULT 0,
  fail_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('running','passed','failed')),
  started_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS replay_result (
  replay_id TEXT NOT NULL REFERENCES replay_run(replay_id),
  case_id TEXT NOT NULL REFERENCES evaluation_case(case_id),
  actual_decision TEXT NOT NULL,
  actual_description TEXT,
  outcome TEXT NOT NULL CHECK (outcome IN ('pass','fail')),
  message TEXT,
  PRIMARY KEY (replay_id, case_id)
);

CREATE TABLE IF NOT EXISTS published_description (
  publication_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL UNIQUE REFERENCES semantic_candidate(candidate_id),
  review_id TEXT NOT NULL UNIQUE REFERENCES review_decision(review_id),
  source_snapshot_id TEXT NOT NULL REFERENCES source_snapshot(source_snapshot_id),
  source_schema TEXT NOT NULL,
  site_id TEXT NOT NULL,
  asset_number TEXT NOT NULL,
  final_description TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  validator_version TEXT NOT NULL,
  replay_id TEXT NOT NULL REFERENCES replay_run(replay_id),
  published_by TEXT NOT NULL,
  published_at TEXT NOT NULL,
  publication_hash TEXT NOT NULL UNIQUE,
  UNIQUE (source_schema, site_id, asset_number)
);

CREATE TABLE IF NOT EXISTS audit_event (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  event_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_sample (
  sample_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES batch_run(batch_id),
  source_snapshot_id TEXT NOT NULL REFERENCES source_snapshot(source_snapshot_id),
  sample_name TEXT NOT NULL,
  target_count INTEGER NOT NULL,
  selected_count INTEGER NOT NULL DEFAULT 0,
  strategy TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','completed','cancelled')),
  rule_version TEXT NOT NULL,
  validator_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (batch_id, sample_name)
);

CREATE TABLE IF NOT EXISTS review_sample_item (
  sample_id TEXT NOT NULL REFERENCES review_sample(sample_id),
  candidate_id TEXT NOT NULL REFERENCES semantic_candidate(candidate_id),
  ordinal INTEGER NOT NULL,
  stratum TEXT NOT NULL,
  selected_at TEXT NOT NULL,
  PRIMARY KEY (sample_id, candidate_id),
  UNIQUE (sample_id, ordinal)
);

CREATE INDEX IF NOT EXISTS ix_device_site_asset ON device_identity(site_id, asset_number);
CREATE INDEX IF NOT EXISTS ix_device_location ON device_identity(site_id, location_code);
CREATE INDEX IF NOT EXISTS ix_candidate_batch_status ON semantic_candidate(batch_id, validator_status, review_state);
CREATE INDEX IF NOT EXISTS ix_candidate_device ON semantic_candidate(device_id);
CREATE INDEX IF NOT EXISTS ix_rule_status_scope ON terminology_rule(status, site_scope, classification_scope);
CREATE INDEX IF NOT EXISTS ix_validation_candidate_outcome ON validation_result(candidate_id, outcome, severity);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_event(entity_type, entity_id, event_at);
CREATE INDEX IF NOT EXISTS ix_review_sample_batch ON review_sample(batch_id, status);
CREATE INDEX IF NOT EXISTS ix_review_sample_item_candidate ON review_sample_item(candidate_id);

CREATE VIEW IF NOT EXISTS v_review_queue AS
SELECT
  c.candidate_id,
  c.batch_id,
  d.source_schema,
  d.site_id,
  d.asset_number,
  d.original_description,
  c.candidate_description,
  d.location_code,
  d.location_description,
  d.location_parent,
  d.classification_description,
  d.class_structure_description,
  c.confidence,
  c.validator_status,
  c.reason_codes_json,
  c.evidence_level,
  c.rule_version,
  c.validator_version
FROM semantic_candidate c
JOIN device_identity d ON d.device_id = c.device_id
WHERE c.review_state = 'pending' AND c.publication_state = 'unpublished';

CREATE VIEW IF NOT EXISTS v_publishable_candidate AS
SELECT
  c.candidate_id,
  r.review_id,
  r.approval_receipt,
  COALESCE(r.reviewed_description, c.candidate_description) AS final_description,
  d.source_snapshot_id,
  d.source_schema,
  d.site_id,
  d.asset_number,
  c.rule_version,
  c.validator_version
FROM semantic_candidate c
JOIN device_identity d ON d.device_id = c.device_id
JOIN review_decision r ON r.candidate_id = c.candidate_id
WHERE r.decision IN ('approved','modified')
  AND c.validator_status = 'candidate'
  AND c.publication_state = 'unpublished';
