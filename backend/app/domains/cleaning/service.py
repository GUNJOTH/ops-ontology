"""Cleaning workflow compatibility facade.

Responsibilities are separated into:
- task_queries: task state, queue and list/detail queries
- preview_replay: immutable preview generation and replay validation
- approval_publish: lifecycle orchestration, approval and local publication
- rule_registry: rule registration and registry queries

All modules operate on the local workflow database only; source systems remain
read-only. This facade preserves the legacy import surface.
"""
from .approval_publish import (
    advance_cleaning_task,
    approve_cleaning_task,
    cleaning_batch_approve,
    cleaning_publish,
    publish_cleaning_task,
)
from .preview_replay import (
    execute_cleaning_replay,
    expected_cleaning_description,
    gate_cleaning_replay,
    generate_cleaning_preview,
    hydrate_cleaning_preview,
    record_cleaning_preview,
    sha256_file,
)
from .rule_registry import cleaning_rules, register_cleaning_rule
from .task_queries import (
    cleaning_queue,
    cleaning_task,
    cleaning_task_next_action,
    cleaning_task_payload,
    cleaning_task_replay_ids,
    cleaning_task_row,
    cleaning_tasks,
)

__all__ = [
    "cleaning_task_row",
    "cleaning_task_next_action",
    "cleaning_task_payload",
    "hydrate_cleaning_preview",
    "sha256_file",
    "generate_cleaning_preview",
    "expected_cleaning_description",
    "execute_cleaning_replay",
    "cleaning_task_replay_ids",
    "cleaning_tasks",
    "cleaning_task",
    "record_cleaning_preview",
    "gate_cleaning_replay",
    "advance_cleaning_task",
    "cleaning_rules",
    "register_cleaning_rule",
    "cleaning_queue",
    "cleaning_batch_approve",
    "approve_cleaning_task",
    "cleaning_publish",
    "publish_cleaning_task",
]
