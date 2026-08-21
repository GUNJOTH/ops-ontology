"""AI-assisted candidate review contracts."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AiDecisionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    sample_id: str = Field(alias="sampleId", min_length=1)
    candidate_id: str = Field(alias="candidateId", min_length=1)
    decision: Literal["keep_original", "accept_candidate", "needs_review"]
    note: str = ""
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class AiBulkDecisionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    sample_id: str = Field(alias="sampleId", min_length=1)
    decision: Literal["keep_original", "accept_candidate"]
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class AiClusterDecisionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    cluster_id: str = Field(alias="clusterId", min_length=1, max_length=100)
    decision: Literal["keep_original", "accept_candidate", "needs_review"]
    note: str = ""
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class AutoApprovalRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scope: Literal["sample", "batch"] = "sample"
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class AgentCandidateReviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)
    candidate_ids: list[str] = Field(default_factory=list, alias="candidateIds", max_length=500)
    batch_size: int = Field(default=100, alias="batchSize", ge=10, le=500)
    note: str = Field(default="", max_length=500)

