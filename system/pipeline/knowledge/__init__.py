"""Knowledge ingestion, lifecycle, conflict and retrieval helpers.

The package writes only the local semantic overlay.  Source documents are
read-only inputs and an agent result is always a candidate until an explicit
review and knowledge-release decision is recorded.
"""

from .documents import import_document, parse_document
from .lifecycle import approve_knowledge, release_approved_knowledge
from .quality import detect_conflicts, replay_knowledge

__all__ = [
    "approve_knowledge",
    "detect_conflicts",
    "import_document",
    "parse_document",
    "release_approved_knowledge",
    "replay_knowledge",
]
