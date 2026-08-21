"""Manage version registration, local snapshots and pointer rollback.

This manager never changes DM8/MaxiEAM/HD_SAAS/XNY_SAAS.  It only manages the
local Canonical store's version registry and immutable local snapshots.  A
future ontology version must pass the same projection, SHACL, JSON-LD,
SPARQL and OWL-RL gates before its pointer can be activated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
STANDARD_ROOT = PROJECT_ROOT / "standards"
TARGET = ROOT / "data" / "canonical_semantic.sqlite3"
REGISTRY = ROOT / "data" / "ontology_version_registry.json"
SNAPSHOT_ROOT = ROOT / "data" / "ontology-snapshots"
VERSION_SPEC = STANDARD_ROOT / "ontology-version.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_registry() -> dict[str, object]:
    if REGISTRY.exists():
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    return {"schemaVersion": "ontology-version-registry-v1", "activeVersion": None, "versions": [], "migrations": [], "snapshots": []}


def save_registry(registry: dict[str, object]) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")


def version_record(spec: dict[str, object], status: str) -> dict[str, object]:
    required = ["ontologyFile", "vocabularyFile", "shaclFile", "contextFile", "rdfDatasetManifest", "assetManifest", "sparqlManifest"]
    for key in required:
        path = STANDARD_ROOT / str(spec[key])
        if not path.exists():
            raise FileNotFoundError(path)
    return {
        "version": str(spec["version"]),
        "versionIri": spec["versionIri"],
        "ontologyHash": sha256(STANDARD_ROOT / str(spec["ontologyFile"])),
        "vocabularyHash": sha256(STANDARD_ROOT / str(spec["vocabularyFile"])),
        "shaclHash": sha256(STANDARD_ROOT / str(spec["shaclFile"])),
        "contextHash": sha256(STANDARD_ROOT / str(spec["contextFile"])),
        "rdfDatasetManifestHash": sha256(STANDARD_ROOT / str(spec["rdfDatasetManifest"])),
        "assetManifestHash": sha256(STANDARD_ROOT / str(spec["assetManifest"])),
        "sparqlManifestHash": sha256(PROJECT_ROOT / "sparql" / Path(str(spec["sparqlManifest"])).name),
        "parentVersion": spec.get("parentVersion"),
        "status": status,
        "registeredAt": now(),
        "activatedAt": now() if status == "active" else None,
        "sourceWrite": False,
        "formalPublication": False,
    }


def register_spec(spec_path: Path, activate: bool = False) -> dict[str, object]:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    registry = load_registry()
    version = str(spec["version"])
    record = version_record(spec, "active" if activate else "available")
    registry["versions"] = [item for item in registry.get("versions", []) if item["version"] != version] + [record]
    if activate:
        registry["activeVersion"] = version
    registry["updatedAt"] = now()
    save_registry(registry)
    return {"status": "registered", "record": record, "activeVersion": registry.get("activeVersion"), "sourceWrite": False, "formalPublication": False}


def register_current() -> dict[str, object]:
    spec = json.loads(VERSION_SPEC.read_text(encoding="utf-8"))
    registry = load_registry()
    version = str(spec["version"])
    result = register_spec(VERSION_SPEC, activate=True)
    result["status"] = "registered"
    return result


def snapshot(version: str | None = None) -> dict[str, object]:
    registry = load_registry()
    active = version or registry.get("activeVersion")
    if not active:
        raise RuntimeError("尚未注册 active ontology version")
    if not TARGET.exists():
        raise FileNotFoundError(TARGET)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = SNAPSHOT_ROOT / str(active) / stamp
    destination.mkdir(parents=True, exist_ok=False)
    db_copy = destination / "canonical_semantic.sqlite3"
    shutil.copy2(TARGET, db_copy)
    manifest = {
        "snapshotId": f"ontology-snapshot-{stamp}",
        "version": active,
        "createdAt": now(),
        "canonicalDb": str(db_copy),
        "sourceWrite": False,
        "formalPublication": False,
    }
    (destination / "snapshot.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    registry.setdefault("snapshots", []).append(manifest)
    registry["updatedAt"] = now()
    save_registry(registry)
    return {"status": "snapshotted", **manifest}


def prepare_migration(manifest_path: Path) -> dict[str, object]:
    registry = load_registry()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    from_version = str(manifest["fromVersion"])
    to_version = str(manifest["toVersion"])
    if registry.get("activeVersion") != from_version:
        raise RuntimeError(f"当前 active 版本不是 {from_version}")
    record = {
        "migrationId": f"migration-{from_version}-to-{to_version}",
        "fromVersion": from_version,
        "toVersion": to_version,
        "status": "prepared",
        "manifest": manifest,
        "preparedAt": now(),
        "sourceWrite": False,
        "formalPublication": False,
    }
    registry["migrations"] = [item for item in registry.get("migrations", []) if item["migrationId"] != record["migrationId"]] + [record]
    registry["updatedAt"] = now()
    save_registry(registry)
    return record


def rollback(version: str) -> dict[str, object]:
    registry = load_registry()
    known = next((item for item in registry.get("versions", []) if item["version"] == version), None)
    if known is None:
        raise RuntimeError(f"版本未注册：{version}")
    previous = registry.get("activeVersion")
    registry["activeVersion"] = version
    for item in registry.get("versions", []):
        item["status"] = "active" if item["version"] == version else "available"
    registry["updatedAt"] = now()
    save_registry(registry)
    return {"status": "rolled_back_pointer", "fromVersion": previous, "activeVersion": version, "sourceWrite": False, "formalPublication": False}


def activate(version: str, verification_path: Path) -> dict[str, object]:
    registry = load_registry()
    known = next((item for item in registry.get("versions", []) if item["version"] == version), None)
    if known is None:
        raise RuntimeError(f"版本未注册：{version}")
    report = json.loads(verification_path.read_text(encoding="utf-8"))
    if report.get("status") != "PASS":
        raise RuntimeError("版本验证报告不是 PASS，禁止激活")
    previous = registry.get("activeVersion")
    registry["activeVersion"] = version
    for item in registry.get("versions", []):
        item["status"] = "active" if item["version"] == version else "available"
        if item["version"] == version:
            item["activatedAt"] = now()
    registry["updatedAt"] = now()
    save_registry(registry)
    return {"status": "activated", "fromVersion": previous, "activeVersion": version, "verification": str(verification_path), "sourceWrite": False, "formalPublication": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage local ontology version registry and rollback pointer")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("register-current")
    reg = sub.add_parser("register-version")
    reg.add_argument("spec", type=Path)
    reg.add_argument("--activate", action="store_true")
    snap = sub.add_parser("snapshot")
    snap.add_argument("--version")
    mig = sub.add_parser("prepare-migration")
    mig.add_argument("manifest", type=Path)
    rb = sub.add_parser("rollback")
    rb.add_argument("version")
    act = sub.add_parser("activate")
    act.add_argument("version")
    act.add_argument("verification", type=Path)
    args = parser.parse_args()
    if args.command == "register-current":
        result = register_current()
    elif args.command == "register-version":
        result = register_spec(args.spec.resolve(), args.activate)
    elif args.command == "snapshot":
        result = snapshot(args.version)
    elif args.command == "prepare-migration":
        result = prepare_migration(args.manifest.resolve())
    elif args.command == "rollback":
        result = rollback(args.version)
    else:
        result = activate(args.version, args.verification.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
