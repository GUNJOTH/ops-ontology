"""Cleaning task and rule contracts."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CleaningBatchApprovalRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scope: Literal["cleaning"] = "cleaning"
    task_id: str | None = Field(default=None, alias="taskId", max_length=200)
    note: str = Field(default="", max_length=500)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class CleaningBatchPublishRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scope: Literal["cleaning"] = "cleaning"
    task_id: str | None = Field(default=None, alias="taskId", max_length=200)
    note: str = Field(default="", max_length=500)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class CleaningRuleRegistrationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    rule_key: str = Field(alias="ruleKey", min_length=3, max_length=200)
    cleaning_type: str = Field(alias="cleaningType", min_length=1, max_length=80)
    rule_label: str = Field(alias="ruleLabel", min_length=1, max_length=200)
    action_label: str = Field(default="清洗", alias="actionLabel", max_length=80)
    is_cleaning: bool = Field(default=True, alias="isCleaning")
    replay_id: str = Field(alias="replayId", min_length=3, max_length=200)
    rule_version: str = Field(alias="ruleVersion", min_length=1, max_length=200)


class CleaningTaskActionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    note: str = Field(default="", max_length=500)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)


class CleaningTaskAdvanceRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    note: str = Field(default="", max_length=500)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=200)
    confirm_publication: bool = Field(default=False, alias="confirmPublication")


class CleaningTaskPreviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    preview_id: str | None = Field(default=None, alias="previewId", max_length=200)
    preview_sha256: str | None = Field(default=None, alias="previewSha256", min_length=8, max_length=128)
    preview_path: str | None = Field(default=None, alias="previewPath", max_length=500)
    sample_path: str | None = Field(default=None, alias="samplePath", max_length=500)
    note: str = Field(default="", max_length=500)

