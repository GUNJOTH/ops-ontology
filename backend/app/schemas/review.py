"""Candidate review and formal approval contracts."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate_id: str = Field(alias="candidateId", min_length=1)
    decision: Literal["approved", "modified", "rejected", "deferred"]
    reviewed_description: str | None = Field(default=None, alias="reviewedDescription")
    note: str | None = None
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class ReviewResponse(BaseModel):
    review_id: str = Field(alias="reviewId")
    candidate_id: str = Field(alias="candidateId")
    decision: str
    review_state: str = Field(alias="reviewState")
    approval_receipt: str = Field(alias="approvalReceipt")
    reviewed_at: str = Field(alias="reviewedAt")


class FormalBatchApprovalRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    replay_id: str = Field(alias="replayId", min_length=1, max_length=200)
    note: str = Field(default="", max_length=500)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)

