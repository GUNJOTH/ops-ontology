"""Stable API projections for candidate and publication rows.

These functions deliberately contain no database access.  Keeping the
projection contract separate from query and approval code makes it safe to
change persistence details without changing the API payload shape.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app.core.utils import parse_json_array


def row_to_candidate(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "candidateId": row["candidate_id"],
        "batchId": row["batch_id"],
        "siteId": row["site_id"],
        "assetNumber": row["asset_number"],
        "originalDescription": row["original_description"],
        "candidateDescription": row["candidate_description"],
        "kks": row["location_code"] or "",
        "locationDescription": row["location_description"] or "",
        "locationParent": row["location_parent"] or "",
        "classificationDescription": row["classification_description"] or "",
        "confidence": row["confidence"],
        "validatorStatus": row["validator_status"],
        "reviewState": row["review_state"],
        "reasonCodes": parse_json_array(row["reason_codes_json"]),
        "evidenceLevel": row["evidence_level"],
        "updatedAt": row["created_at"],
    }


def row_to_publication(row: sqlite3.Row) -> dict[str, Any]:
    """Map the formal-result row without exposing mutable source-table state."""
    applied_rules = parse_json_array(row["applied_rule_ids_json"])
    original = row["original_description"] or ""
    final = row["final_description"] or ""
    if (
        "format.fullwidth_parenthesis_to_ascii" not in applied_rules
        and final != original
        and any(mark in original for mark in ("（", "）"))
    ):
        applied_rules.append("format.fullwidth_parenthesis_to_ascii")
    if (
        "format.fullwidth_comma_to_ascii" not in applied_rules
        and final != original
        and "，" in original
    ):
        applied_rules.append("format.fullwidth_comma_to_ascii")
    return {
        "publicationId": row["publication_id"],
        "candidateId": row["candidate_id"],
        "reviewId": row["review_id"],
        "batchId": row["batch_id"],
        "sourceSnapshotId": row["source_snapshot_id"],
        "siteId": row["site_id"],
        "assetNumber": row["asset_number"],
        "assetId": row["source_asset_id"] or "",
        "originalDescription": row["original_description"] or "",
        "finalDescription": row["final_description"],
        "kks": row["location_code"] or "",
        "locationDescription": row["location_description"] or "",
        "locationParent": row["location_parent"] or "",
        "classificationDescription": row["classification_description"] or "",
        "appliedRules": applied_rules,
        "ruleVersion": row["rule_version"],
        "validatorVersion": row["validator_version"],
        "replayId": row["replay_id"],
        "publishedBy": row["published_by"],
        "publishedAt": row["published_at"],
        "approvalReceipt": row["approval_receipt"],
        "reviewer": row["reviewer"],
        "reviewedAt": row["reviewed_at"],
    }


PUBLISHED_SELECT = """
    SELECT p.publication_id,p.candidate_id,p.review_id,p.source_snapshot_id,
      p.site_id,p.asset_number,p.final_description,p.rule_version,
      p.validator_version,p.replay_id,p.published_by,p.published_at,
      c.batch_id,c.original_description,c.applied_rule_ids_json,
      d.source_asset_id,d.location_code,d.location_description,d.location_parent,
      d.classification_description,r.approval_receipt,r.reviewer,r.reviewed_at
    FROM published_description p
    JOIN semantic_candidate c ON c.candidate_id=p.candidate_id
    JOIN device_identity d ON d.device_id=c.device_id
    JOIN review_decision r ON r.review_id=p.review_id
"""
