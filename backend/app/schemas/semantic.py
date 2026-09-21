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


class SemanticContextBuildRequest(BaseModel):
    """Bounded request for an ontology-guided Agent context."""

    model_config = ConfigDict(populate_by_name=True)

    object_type: str = Field(alias="objectType", min_length=1, max_length=100)
    canonical_key: str = Field(alias="canonicalKey", min_length=1, max_length=300)
    task: str = Field(default="", max_length=300)
    question: str = Field(default="", max_length=2000)
    time_from: str | None = Field(default=None, alias="timeFrom", max_length=80)
    time_to: str | None = Field(default=None, alias="timeTo", max_length=80)
    limit: int = Field(default=50, ge=1, le=200)


class KnowledgeImportRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source_path: str = Field(alias="sourcePath", min_length=1, max_length=1000)
    package_id: str = Field(default="platform-core", alias="packageId", max_length=120)
    domain: str = Field(default="enterprise-operations", max_length=200)
    source_snapshot_id: str = Field(default="local-document-import", alias="sourceSnapshotId", max_length=200)
    owner: str = Field(default="", max_length=100)


class KnowledgeExtractionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    fragment_id: str = Field(alias="fragmentId", min_length=1, max_length=200)
    use_ai: bool = Field(default=True, alias="useAi")
    prompt_version: str = Field(default="knowledge-extraction-v1", alias="promptVersion", max_length=100)


class KnowledgeCaseRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str = Field(min_length=1, max_length=500)
    source_fragment_id: str = Field(alias="sourceFragmentId", min_length=1, max_length=200)
    definition: dict[str, Any] = Field(min_length=1)
    package_id: str = Field(default="platform-core", alias="packageId", max_length=120)
    domain: str = Field(default="enterprise-operations", max_length=200)


class KnowledgeReviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    decision: Literal["approved", "rejected"]
    reviewer: str = Field(default="人工审核", max_length=100)
    note: str = Field(default="", max_length=1000)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)


class KnowledgeReleaseRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    release_id: str = Field(alias="releaseId", min_length=3, max_length=200)
    reviewer: str = Field(default="人工审核", max_length=100)
    note: str = Field(default="", max_length=1000)


class KnowledgeReplayRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    cases: list[dict[str, Any]] = Field(default_factory=list, max_length=500)


class AgentSemanticValidationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    context: dict[str, Any]
    result: dict[str, Any]
