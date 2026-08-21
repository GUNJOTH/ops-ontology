"""Semantic runtime, identity, action and query contracts."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SemanticExecutionPreviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    asset_id: str = Field(alias="assetId", min_length=1, max_length=200)
    asset_version_id: str | None = Field(default=None, alias="assetVersionId", max_length=200)
    target_scope: str = Field(default="local_semantic_layer", alias="targetScope", min_length=1, max_length=200)
    sample_size: int = Field(default=200, alias="sampleSize", ge=1, le=500)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)
    note: str = Field(default="", max_length=500)


class SemanticExecutionDispatchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    action_run_id: str | None = Field(default=None, alias="actionRunId", max_length=200)
    action_plan_id: str | None = Field(default=None, alias="actionPlanId", max_length=200)
    approval_receipt: str | None = Field(default=None, alias="approvalReceipt", max_length=300)
    adapter_id: str = Field(default="local-preview-adapter", alias="adapterId", max_length=200)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)
    request_payload: dict[str, Any] = Field(default_factory=dict, alias="requestPayload")


class SemanticActionApprovalRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    decision: Literal["approved", "rejected"]
    reviewer: str = Field(default="人工审核", max_length=100)
    comment: str = Field(default="", max_length=500)


class DefectStatusReviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    decision: Literal["approved", "rejected"]
    canonical_state: str | None = Field(default=None, alias="canonicalState", max_length=40)
    business_meaning: str | None = Field(default=None, alias="businessMeaning", max_length=200)
    notes: str = Field(default="", max_length=500)
    reviewer: str = Field(default="人工审核", max_length=100)


class SemanticStateReplayRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subject_type: str | None = Field(default=None, alias="subjectType", max_length=100)
    subject_key: str | None = Field(default=None, alias="subjectKey", max_length=200)


class SemanticIdentityRevokeRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    reviewer: str = Field(default="人工审核", max_length=100)
    reason: str = Field(min_length=3, max_length=500)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)


class SemanticIdentityReviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    decision: Literal["approved", "rejected"]
    reviewer: str = Field(default="人工审核", max_length=100)
    note: str = Field(default="", max_length=500)
    evidence: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)


class CanonicalSparqlRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20000)

