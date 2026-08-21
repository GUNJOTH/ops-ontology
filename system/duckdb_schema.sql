CREATE TABLE IF NOT EXISTS analytics_metadata (
  metadata_key VARCHAR PRIMARY KEY,
  metadata_value VARCHAR NOT NULL,
  updated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_quality_metric (
  batch_id VARCHAR NOT NULL,
  metric_group VARCHAR NOT NULL,
  metric_name VARCHAR NOT NULL,
  metric_value DOUBLE NOT NULL,
  dimension_json JSON,
  measured_at TIMESTAMP NOT NULL,
  PRIMARY KEY (batch_id, metric_group, metric_name)
);

CREATE OR REPLACE VIEW v_candidate_quality AS
SELECT
  SITEID AS site_id,
  count(*) AS candidate_count,
  count(DISTINCT ASSETNUM) AS distinct_asset_count,
  count(*) FILTER (WHERE json_extract_string(try_cast(CONTEXT_JSON AS JSON), '$.location.LOCATION') IS NOT NULL AND trim(json_extract_string(try_cast(CONTEXT_JSON AS JSON), '$.location.LOCATION')) <> '') AS location_count,
  count(*) FILTER (WHERE CLASSIFICATION_DESCRIPTION IS NOT NULL AND trim(CLASSIFICATION_DESCRIPTION) <> '') AS classification_count,
  count(*) FILTER (WHERE SEMANTIC_RESULT_STATUS = 'candidate') AS auto_candidate_count,
  count(*) FILTER (WHERE SEMANTIC_RESULT_STATUS = 'needs_review') AS needs_review_count,
  count(*) FILTER (WHERE SEMANTIC_RESULT_STATUS = 'blocked') AS blocked_count,
  count(*) FILTER (WHERE UNIFIED_DESCRIPTION <> ORIGINAL_DESCRIPTION) AS changed_count
FROM semantic_candidate_fact
GROUP BY SITEID;

CREATE OR REPLACE VIEW v_reason_code_distribution AS
SELECT SEMANTIC_REASON_CODES AS reason_codes, count(*) AS row_count
FROM semantic_candidate_fact
GROUP BY SEMANTIC_REASON_CODES
ORDER BY row_count DESC;

CREATE OR REPLACE VIEW v_context_coverage AS
SELECT
  count(*) AS row_count,
  count(*) FILTER (WHERE json_extract_string(try_cast(CONTEXT_JSON AS JSON), '$.location.LOCATION') IS NOT NULL AND trim(json_extract_string(try_cast(CONTEXT_JSON AS JSON), '$.location.LOCATION')) <> '') AS location_code_rows,
  count(*) FILTER (WHERE LOCATION_PARENT IS NOT NULL AND trim(LOCATION_PARENT) <> '') AS location_parent_rows,
  count(*) FILTER (WHERE LOCATION_DESCRIPTION IS NOT NULL AND trim(LOCATION_DESCRIPTION) <> '') AS location_description_rows,
  count(*) FILTER (WHERE CLASSIFICATION_DESCRIPTION IS NOT NULL AND trim(CLASSIFICATION_DESCRIPTION) <> '') AS classification_rows,
  count(*) FILTER (WHERE TRY_CAST(SPEC_COUNT AS BIGINT) > 0) AS specification_rows,
  count(*) FILTER (WHERE TRY_CAST(FEATURE_COUNT AS BIGINT) > 0) AS feature_rows,
  count(*) FILTER (WHERE TRY_CAST(PARENT_ASSET_COUNT AS BIGINT) > 0) AS parent_asset_rows,
  count(*) FILTER (WHERE TRY_CAST(RELATION_COUNT AS BIGINT) > 0) AS relation_rows
FROM semantic_candidate_fact;

CREATE OR REPLACE VIEW v_description_frequency AS
SELECT UNIFIED_DESCRIPTION AS unified_description, count(*) AS row_count
FROM semantic_candidate_fact
GROUP BY UNIFIED_DESCRIPTION
ORDER BY row_count DESC;
