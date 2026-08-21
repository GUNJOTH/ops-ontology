"""Compatibility facade for AI decision endpoints.

The implementation is split by policy, preview, agent review, automatic
approval, and human-confirmed sample writes. Existing imports remain stable.
"""
from .agent_review_service import agent_audit_pending_candidates
from .auto_approval_service import ai_auto_approve
from .decision_policy import ai_sample_summary, auto_approval_filter, auto_approval_preview
from .preview_service import ai_agent_review_preview, ai_review_preview, ai_review_sample
from .sample_decision_service import save_ai_bulk_decision, save_ai_review_decision

__all__ = [
    "ai_sample_summary",
    "auto_approval_filter",
    "auto_approval_preview",
    "ai_review_preview",
    "ai_agent_review_preview",
    "ai_auto_approve",
    "agent_audit_pending_candidates",
    "ai_review_sample",
    "save_ai_review_decision",
    "save_ai_bulk_decision",
]
