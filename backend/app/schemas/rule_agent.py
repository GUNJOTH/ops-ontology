"""Rule discovery and semantic reasoning contracts."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RuleAgentRunRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)
    sample_size: int = Field(default=120, alias="sampleSize", ge=20, le=500)
    note: str = Field(default="", max_length=500)


class SemanticReasoningRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)
    sample_size: int = Field(default=200, alias="sampleSize", ge=50, le=500)
    cluster_limit: int = Field(default=12, alias="clusterLimit", ge=1, le=30)
    cluster_keys: list[str] = Field(default_factory=list, alias="clusterKeys", max_length=30)
    note: str = Field(default="", max_length=500)


class RuleAgentProposalActionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)
    note: str = Field(default="", max_length=500)


class RuleAgentReviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    idempotency_key: str = Field(alias="idempotencyKey", min_length=3, max_length=200)
    proposal_ids: list[str] = Field(default_factory=list, alias="proposalIds", max_length=20)
    note: str = Field(default="", max_length=500)

