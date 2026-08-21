"""Central configuration and filesystem locations for the backend."""
from __future__ import annotations

import os
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_ROOT.parent
SYSTEM_ROOT = PROJECT_ROOT / "system"
DATA_DIR = SYSTEM_ROOT / "data"
DEPENDENCY_DIR = BACKEND_ROOT / ".deps"
IDENTITY_RESULT_ROOT = PROJECT_ROOT / "pilots" / "identity" / "results"


def load_local_env(path: Path) -> None:
    """Load allow-listed local development settings without overriding env."""
    if not path.exists():
        return
    allowed = {
        "RULE_AGENT_API_KEY",
        "RULE_AGENT_BASE_URL",
        "RULE_AGENT_MODEL",
        "RULE_AGENT_TIMEOUT",
        "RULE_AGENT_MAX_TOKENS",
        "RULE_AGENT_MAX_PROPOSALS",
        "RULE_AGENT_THINKING",
        "RULE_AGENT_MAX_ATTEMPTS",
        "RULE_AGENT_RETRY_BACKOFF",
        "METADATA_RESULT_DIR",
        "IDENTITY_RESULT_DB",
    }
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#") or "=" not in value:
            continue
        name, raw = value.split("=", 1)
        name = name.strip()
        if name not in allowed or os.environ.get(name):
            continue
        os.environ[name] = raw.strip().strip('"').strip("'")


load_local_env(BACKEND_ROOT / ".env")

UNIFIED_SEMANTICS_DB = DATA_DIR / "unified_semantics.sqlite3"
CANONICAL_SEMANTICS_DB = DATA_DIR / "canonical_semantic.sqlite3"
SEMANTIC_CONTEXT_FILE = PROJECT_ROOT / "standards" / "context.jsonld"
SQLITE_DB = DATA_DIR / "semantic_workflow.sqlite3"
DUCKDB_DB = DATA_DIR / "semantic_analytics_v155.duckdb"
METADATA_RESULT_DIR = Path(
    os.getenv(
        "METADATA_RESULT_DIR",
        str(PROJECT_ROOT / "pilots" / "metadata" / "results" / "metadata-semantic-v1-20260815T-v2"),
    )
)
METADATA_SQLITE_DB = METADATA_RESULT_DIR / "metadata_semantics.sqlite3"
METADATA_DUCKDB_DB = METADATA_RESULT_DIR / "metadata_semantics.duckdb"
BACKUP_DIR = SYSTEM_ROOT / "backups"
CLEANING_TASK_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "cleaning_tasks"
AI_REVIEW_SAMPLE_CSV = PROJECT_ROOT / "pilots" / "HD_SAAS" / "next_ai_review" / "sample_200_judgment.csv"
AI_REVIEW_MANIFEST = PROJECT_ROOT / "pilots" / "HD_SAAS" / "next_ai_review" / "manifest.json"
RULE_AGENT_DIR = PROJECT_ROOT / "pilots" / "HD_SAAS" / "rule_agent"

RULE_AGENT_BASE_URL = os.getenv("RULE_AGENT_BASE_URL", os.getenv("ANTHROPIC_BASE_URL", "")).rstrip("/")
RULE_AGENT_API_KEY = os.getenv("RULE_AGENT_API_KEY", os.getenv("ANTHROPIC_AUTH_TOKEN", ""))
RULE_AGENT_MODEL = os.getenv("RULE_AGENT_MODEL", os.getenv("ANTHROPIC_MODEL", "deepseek-v4-pro"))
RULE_AGENT_TIMEOUT = float(os.getenv("RULE_AGENT_TIMEOUT", "60"))
RULE_AGENT_MAX_TOKENS = int(os.getenv("RULE_AGENT_MAX_TOKENS", "4096"))
RULE_AGENT_MAX_PROPOSALS = int(os.getenv("RULE_AGENT_MAX_PROPOSALS", "3"))
RULE_AGENT_THINKING = os.getenv("RULE_AGENT_THINKING", "disabled").strip().lower()
RULE_AGENT_MAX_ATTEMPTS = max(1, min(3, int(os.getenv("RULE_AGENT_MAX_ATTEMPTS", "2"))))
RULE_AGENT_RETRY_BACKOFF = max(0.0, min(5.0, float(os.getenv("RULE_AGENT_RETRY_BACKOFF", "1"))))
RULE_AGENT_CATALOG_VERSION = "rule-agent-local-catalog-20260814-v1"

