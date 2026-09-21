"""Local Semantic Release control plane for the Canonical RDF Dataset.

This module manages release manifests, approvals, immutable local backups and
an active-release pointer.  It never writes DM8, MaxiEAM, HD_SAAS or XNY_SAAS.
The active pointer is deliberately separate from the source snapshot and can
be rolled back without deleting any historical artifact.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_ROOT.parent
SYSTEM_ROOT = PROJECT_ROOT / "system"
DATA_DIR = SYSTEM_ROOT / "data"
CANONICAL_DB = DATA_DIR / "canonical_semantic.sqlite3"
RELEASE_ROOT = SYSTEM_ROOT / "releases"
BACKUP_ROOT = SYSTEM_ROOT / "backups" / "semantic-releases"
REGISTRY_PATH = DATA_DIR / "semantic_release_registry.json"
ACTIVE_POINTER_PATH = DATA_DIR / "semantic_release_active.json"
RELEASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,160}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_release_id(value: str) -> str:
    if not RELEASE_ID_PATTERN.fullmatch(value):
        raise ValueError(f"非法 release_id：{value}")
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_registry() -> dict[str, Any]:
    if not REGISTRY_PATH.exists():
        return {"schemaVersion": "semantic-release-registry-v1", "releases": [], "updatedAt": None}
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _save_registry(registry: dict[str, Any]) -> None:
    registry["schemaVersion"] = "semantic-release-registry-v1"
    registry["updatedAt"] = utc_now()
    _write_json(REGISTRY_PATH, registry)


def _release_path(release_id: str) -> Path:
    return RELEASE_ROOT / _safe_release_id(release_id) / "release.json"


def _load_release(release_id: str) -> dict[str, Any]:
    path = _release_path(release_id)
    if not path.exists():
        raise FileNotFoundError(f"Semantic Release 不存在：{release_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_release(release: dict[str, Any]) -> None:
    _write_json(_release_path(str(release["releaseId"])), release)
    registry = load_registry()
    releases = [item for item in registry.get("releases", []) if item.get("releaseId") != release["releaseId"]]
    releases.append({
        "releaseId": release["releaseId"],
        "ontologyVersion": release["ontologyVersion"],
        "canonicalRunId": release["canonicalRunId"],
        "status": release["status"],
        "approvalStatus": release["approval"]["status"],
        "backupStatus": release["backup"]["status"],
        "activatedAt": release.get("activatedAt"),
        "createdAt": release["createdAt"],
    })
    registry["releases"] = releases
    _save_registry(registry)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _latest_completed_run() -> tuple[dict[str, Any], dict[str, Any]]:
    if not CANONICAL_DB.exists():
        raise FileNotFoundError(CANONICAL_DB)
    connection = sqlite3.connect(f"file:{CANONICAL_DB.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM canonical_projection_run WHERE status='completed' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("没有已完成的 Canonical RDF 投影")
        manifest = json.loads(row["manifest_json"] or "{}")
        return dict(row), manifest
    finally:
        connection.close()


def _artifact_sources(run_manifest: dict[str, Any]) -> list[tuple[str, Path]]:
    artifacts = run_manifest.get("artifacts") if isinstance(run_manifest, dict) else {}
    sources: list[tuple[str, Path]] = []
    for key, raw_path in (artifacts or {}).items():
        if not raw_path:
            continue
        path = Path(str(raw_path))
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if path.exists() and path.is_file():
            sources.append((str(key), path.resolve()))
    for key, path in {
        "ontology": PROJECT_ROOT / "standards" / "ontology.ttl",
        "vocabularies": PROJECT_ROOT / "standards" / "vocabularies.ttl",
        "shacl": PROJECT_ROOT / "standards" / "enterprise-operations.shacl.ttl",
        "context": PROJECT_ROOT / "standards" / "context.jsonld",
        "rdfDatasetManifest": PROJECT_ROOT / "standards" / "rdf-dataset.json",
        "assetManifest": PROJECT_ROOT / "standards" / "semantic-asset-manifest.json",
        "versionSpec": PROJECT_ROOT / "standards" / "ontology-version.json",
    }.items():
        if path.exists() and path.is_file() and not any(existing == path.resolve() for _, existing in sources):
            sources.append((key, path.resolve()))
    if not sources:
        raise RuntimeError("Canonical RDF 没有可备份的标准资产")
    return sources


def prepare_release(verification_path: Path | None = None, actor: str = "local-user") -> dict[str, Any]:
    run, run_manifest = _latest_completed_run()
    if int(run.get("validation_error_count") or 0) != 0:
        raise RuntimeError("Canonical RDF 存在验证错误，禁止生成生产 Release")
    if bool(run_manifest.get("sourceWrite")) or bool(run_manifest.get("formalPublication")):
        raise RuntimeError("源写入或正式发布标志异常，禁止生成生产 Release")
    verification: dict[str, Any] = {"status": "not_supplied"}
    if verification_path is not None:
        verification = json.loads(verification_path.read_text(encoding="utf-8"))
        if verification.get("status") != "PASS":
            raise RuntimeError("标准验证报告不是 PASS，禁止生成生产 Release")
        reported_run = verification.get("canonicalRunId") or (verification.get("canonical", {}) or {}).get("latestRun", {}).get("run_id")
        if reported_run and reported_run != run["run_id"]:
            raise RuntimeError("验证报告与最新 Canonical 投影 run 不一致")
    identity_count = (run_manifest.get("canonicalScope") or {}).get("sourceIdentityDeviceCount")
    if identity_count is None or int(identity_count) <= 0:
        raise RuntimeError("Canonical Release 缺少完整身份投影计数")
    existing = [item for item in load_registry().get("releases", []) if item.get("canonicalRunId") == run["run_id"] and item.get("status") not in {"rejected", "rolled_back"}]
    if existing:
        return _load_release(str(existing[-1]["releaseId"]))
    release_id = f"semantic-release-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{hashlib.sha256(run['run_id'].encode()).hexdigest()[:10]}"
    sources = _artifact_sources(run_manifest)
    release = {
        "schemaVersion": "semantic-release-v1",
        "releaseId": release_id,
        "ontologyVersion": run["ontology_version"],
        "canonicalRunId": run["run_id"],
        "sourceSnapshotId": run["source_snapshot_id"],
        "createdAt": utc_now(),
        "createdBy": actor,
        "status": "pending_approval",
        "gates": {
            "verificationStatus": verification.get("status"),
            "verificationPath": str(verification_path) if verification_path else None,
            "canonicalValidationErrorCount": int(run.get("validation_error_count") or 0),
            "sourceIdentityDeviceCount": int(identity_count),
        },
        "artifacts": [
            {"key": key, "sourcePath": str(path), "size": path.stat().st_size, "sha256": _sha256(path)}
            for key, path in sources
        ],
        "approval": {"status": "pending", "approvals": []},
        "backup": {"status": "required", "manifestPath": None},
        "activatedAt": None,
        "sourceWrite": False,
        "formalPublication": False,
    }
    _save_release(release)
    return release


def approve_release(release_id: str, reviewer: str, receipt: str, note: str = "") -> dict[str, Any]:
    release = _load_release(release_id)
    if release["status"] not in {"pending_approval", "approved"}:
        raise RuntimeError(f"Release 当前状态不可审批：{release['status']}")
    if not reviewer.strip() or not receipt.strip():
        raise ValueError("审批人和审批凭据不能为空")
    approvals = release["approval"].setdefault("approvals", [])
    if not any(item.get("receipt") == receipt for item in approvals):
        approvals.append({"reviewer": reviewer.strip(), "receipt": receipt.strip(), "note": note, "approvedAt": utc_now()})
    release["approval"]["status"] = "approved"
    release["status"] = "approved"
    _save_release(release)
    return release


def backup_release(release_id: str) -> dict[str, Any]:
    release = _load_release(release_id)
    backup_dir = BACKUP_ROOT / _safe_release_id(release_id)
    if backup_dir.exists():
        existing = backup_dir / "backup.json"
        if existing.exists():
            return json.loads(existing.read_text(encoding="utf-8"))
        raise FileExistsError(f"备份目录已存在但缺少清单：{backup_dir}")
    backup_dir.mkdir(parents=True)
    canonical_backup = backup_dir / "canonical_semantic.sqlite3"
    source_connection = sqlite3.connect(f"file:{CANONICAL_DB.resolve()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(str(canonical_backup))
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    files = [{"key": "canonicalDatabase", "path": str(canonical_backup), "size": canonical_backup.stat().st_size, "sha256": _sha256(canonical_backup)}]
    for item in release["artifacts"]:
        source = Path(item["sourcePath"])
        target = backup_dir / "artifacts" / f"{item['key']}-{source.name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        files.append({"key": item["key"], "path": str(target), "size": target.stat().st_size, "sha256": _sha256(target)})
    manifest = {
        "schemaVersion": "semantic-release-backup-v1",
        "releaseId": release_id,
        "canonicalRunId": release["canonicalRunId"],
        "createdAt": utc_now(),
        "backupDirectory": str(backup_dir),
        "files": files,
        "sourceWrite": False,
        "formalPublication": False,
    }
    _write_json(backup_dir / "backup.json", manifest)
    release["backup"] = {"status": "verified", "manifestPath": str(backup_dir / "backup.json"), "createdAt": manifest["createdAt"]}
    _save_release(release)
    return manifest


def _verify_backup_manifest(manifest: dict[str, Any]) -> None:
    for item in manifest.get("files", []):
        path = Path(item["path"])
        if not path.exists() or path.stat().st_size != int(item["size"]):
            raise RuntimeError(f"备份文件缺失或大小不一致：{path}")
        if _sha256(path) != item["sha256"]:
            raise RuntimeError(f"备份文件校验失败：{path}")


def restore_backup(release_id: str, target_dir: Path) -> dict[str, Any]:
    release = _load_release(release_id)
    backup_path = Path(release["backup"].get("manifestPath") or "")
    if not backup_path.exists():
        raise FileNotFoundError(f"Release 没有已验证备份：{release_id}")
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    _verify_backup_manifest(backup)
    target = target_dir.resolve()
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"恢复目标必须为空目录：{target}")
    target.mkdir(parents=True, exist_ok=True)
    restored = []
    for item in backup["files"]:
        source = Path(item["path"])
        destination = target / source.name
        shutil.copy2(source, destination)
        restored.append({"source": str(source), "target": str(destination), "sha256": _sha256(destination)})
    return {"status": "restored_to_staging", "releaseId": release_id, "targetDir": str(target), "files": restored, "sourceWrite": False, "formalPublication": False}


def activate_release(release_id: str, actor: str = "local-user") -> dict[str, Any]:
    release = _load_release(release_id)
    if release["approval"]["status"] != "approved":
        raise RuntimeError("Release 尚未完成审批")
    if release["backup"]["status"] != "verified":
        raise RuntimeError("Release 尚未完成 RDF Dataset 备份校验")
    pointer = {"schemaVersion": "semantic-release-pointer-v1", "releaseId": release_id, "canonicalRunId": release["canonicalRunId"], "activatedAt": utc_now(), "activatedBy": actor, "sourceWrite": False, "formalPublication": False}
    _write_json(ACTIVE_POINTER_PATH, pointer)
    release["status"] = "active"
    release["activatedAt"] = pointer["activatedAt"]
    release["activatedBy"] = actor
    _save_release(release)
    return pointer


def rollback_release(release_id: str, actor: str = "local-user") -> dict[str, Any]:
    target = _load_release(release_id)
    if target["approval"]["status"] != "approved" or target["backup"]["status"] != "verified":
        raise RuntimeError("只能回滚到已审批且备份已校验的 Release")
    previous = load_active_release()
    pointer = {"schemaVersion": "semantic-release-pointer-v1", "releaseId": release_id, "canonicalRunId": target["canonicalRunId"], "activatedAt": utc_now(), "activatedBy": actor, "rollbackFrom": previous.get("releaseId") if previous else None, "sourceWrite": False, "formalPublication": False}
    _write_json(ACTIVE_POINTER_PATH, pointer)
    return pointer


def load_active_release() -> dict[str, Any] | None:
    if not ACTIVE_POINTER_PATH.exists():
        return None
    return json.loads(ACTIVE_POINTER_PATH.read_text(encoding="utf-8"))


def selected_canonical_run_id() -> str | None:
    pointer = load_active_release()
    return str(pointer["canonicalRunId"]) if pointer and pointer.get("canonicalRunId") else None
