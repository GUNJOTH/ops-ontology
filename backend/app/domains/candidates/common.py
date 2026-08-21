"""Shared constants and durable file-backed helpers for candidate AI review."""
from __future__ import annotations

import csv
import json
from typing import Any

from fastapi import HTTPException

from app.core.config import AI_REVIEW_MANIFEST, AI_REVIEW_SAMPLE_CSV

CANDIDATE_AGENT_MAX_BATCH_SIZE = 500
CANDIDATE_AGENT_EVIDENCE_LEVELS = ("strong", "source_preview_and_replay")
RULE_AGENT_EVIDENCE_SQL = "c.evidence_level IN ('strong', 'source_preview_and_replay')"
AI_CLUSTER_CACHE_TTL_SECONDS = 300
_AI_CLUSTER_ROWS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_AI_CLUSTER_SUMMARY_CACHE: dict[str, tuple[float, list[dict[str, Any]], dict[str, Any]]] = {}
CANDIDATE_AGENT_REVIEW_VERSION = "candidate-keep-original-agent-20260817-v2"
CANDIDATE_AGENT_DEFAULT_BATCH_SIZE = 100
AI_SAMPLE_ID = "next-ai-review-20260812T085842Z"
AUTO_APPROVAL_POLICY_VERSION = "conservative-equivalence-v1"


def load_ai_review_sample() -> tuple[str, list[dict[str, str]]]:
    if not AI_REVIEW_SAMPLE_CSV.exists():
        raise HTTPException(status_code=503, detail="AI 语义样本尚未生成")
    sample_id = AI_SAMPLE_ID
    if AI_REVIEW_MANIFEST.exists():
        try:
            sample_id = str(json.loads(AI_REVIEW_MANIFEST.read_text(encoding="utf-8")).get("sample_id") or AI_SAMPLE_ID)
        except (OSError, json.JSONDecodeError):
            pass
    with AI_REVIEW_SAMPLE_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return sample_id, rows


def invalidate_ai_cluster_cache() -> None:
    _AI_CLUSTER_ROWS_CACHE.clear()
    _AI_CLUSTER_SUMMARY_CACHE.clear()

