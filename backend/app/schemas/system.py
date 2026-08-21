"""System domain request contracts (health, metrics, Semantic Release)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class SemanticReleaseApprovalRequest(BaseModel):
    reviewer: str = Field(default="人工审核", min_length=1, max_length=100)
    receipt: str = Field(min_length=3, max_length=300)
    note: str = Field(default="", max_length=500)
