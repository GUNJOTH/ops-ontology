"""Build a local source-scoped device identity result layer from read-only snapshots.

The source DM8 schemas are never opened by this script.  ASSET rows become
source-scoped seed devices; operational rows are mapped only when there is a
deterministic same-schema ASSETNUM match.  Location/description matches remain
candidate evidence and require review.  Cross-schema candidate generation is
optional diagnostics and is disabled by default; it is not part of the
source-scoped semantic-governance workflow.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[2]
ASSET_FIELDS = {
    "ASSETID", "SITEID", "ORGID", "ASSETNUM", "DESCRIPTION", "PARENT", "LOCATION", "STATUS",
    "SERIALNUM", "MANUFACTURER", "ITEMNUM", "CLASSSTRUCTUREID", "CLASSIFICATIONID",
    "PLUSCMODELNUM", "S_MODELNUM", "MODELNUM", "S_OLDASSETNUM", "C_OLDLOCATION", "CHANGEDATE",
}
DIRECT_KEY_FIELDS = ("ASSETNUM", "ASSETNUM1", "STDASSETNUM", "EQUIPMENT_ID", "EQUIPMENTID")
ROW_ID_FIELDS = (
    "ASSETID", "XJJLID", "PLUSCSPOTCHECKID", "TICKETID", "WORKORDERID", "WONUM",
    "C_FAULTID", "C_FAULTNO", "C_FAULTNUM",
)
DESCRIPTION_FIELDS = ("DESCRIPTION", "FAULT_DESC", "C_FAULTINFODES", "GZMS", "XQDESCRIPTION", "DEFECTSCONTENT")


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def norm_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", clean(value)).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def sha_id(prefix: str, *parts: str) -> str:
    raw = "|".join(clean(part) for part in parts)
    return prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def latest_dir(pattern: str) -> pathlib.Path:
    matches = sorted((ROOT / "snapshots").glob(pattern), reverse=True)
    if not matches:
        raise SystemExit(f"No snapshot matches {pattern}")
    return matches[0]


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def write_manifest(path: pathlib.Path, manifest: dict[str, object]) -> None:
    if path.exists():
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prior = {}
        if prior.get("run_id") and prior.get("run_id") != manifest.get("run_id"):
            raise RuntimeError(
                f"refusing to overwrite manifest {path} for run {prior.get('run_id')} "
                f"with run {manifest.get('run_id')}"
            )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_asset_rows(path: pathlib.Path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def init_db(path: pathlib.Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE unified_device (
            unified_device_id TEXT PRIMARY KEY,
            identity_scope TEXT NOT NULL,
            master_source_schema TEXT NOT NULL,
            site_id TEXT NOT NULL,
            asset_number TEXT NOT NULL,
            source_asset_id TEXT,
            canonical_name TEXT,
            description_norm TEXT,
            location_code TEXT,
            parent_asset_number TEXT,
            org_id TEXT,
            classstructure_id TEXT,
            classification_id TEXT,
            status TEXT,
            source_identity_key TEXT NOT NULL,
            seed_status TEXT NOT NULL,
            source_snapshot_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(master_source_schema, site_id, asset_number)
        );

        CREATE TABLE device_identity_map (
            map_id INTEGER PRIMARY KEY AUTOINCREMENT,
            unified_device_id TEXT,
            candidate_device_id TEXT,
            source_schema TEXT NOT NULL,
            source_table_group TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_row_id TEXT NOT NULL,
            source_key_type TEXT NOT NULL,
            source_key TEXT NOT NULL,
            site_id TEXT,
            raw_description TEXT,
            location_code TEXT,
            match_method TEXT NOT NULL,
            match_confidence REAL NOT NULL,
            status TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            source_snapshot_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE device_match_candidate (
            candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_schema TEXT NOT NULL,
            source_table_group TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_row_id TEXT NOT NULL,
            source_key_type TEXT NOT NULL,
            source_key TEXT NOT NULL,
            site_id TEXT,
            raw_description TEXT,
            location_code TEXT,
            kks_code TEXT,
            candidate_device_id TEXT,
            candidate_asset_number TEXT,
            score REAL NOT NULL,
            match_method TEXT NOT NULL,
            decision TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            source_snapshot_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE cross_system_match_candidate (
            candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
            left_device_id TEXT NOT NULL,
            right_device_id TEXT NOT NULL,
            site_id TEXT NOT NULL,
            asset_number TEXT NOT NULL,
            score REAL NOT NULL,
            match_method TEXT NOT NULL,
            decision TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(left_device_id, right_device_id)
        );

        CREATE TABLE identity_sample_200 (
            sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL UNIQUE,
            sample_stratum TEXT NOT NULL,
            sample_rank INTEGER NOT NULL,
            FOREIGN KEY(candidate_id) REFERENCES device_match_candidate(candidate_id)
        );

        CREATE TABLE identity_run (
            run_id TEXT PRIMARY KEY,
            asset_snapshot_id TEXT NOT NULL,
            operational_snapshot_id TEXT NOT NULL,
            read_only INTEGER NOT NULL,
            source_write INTEGER NOT NULL,
            formal_publication INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX idx_device_scope_key ON unified_device(master_source_schema, site_id, asset_number);
        CREATE INDEX idx_device_location ON unified_device(master_source_schema, site_id, location_code);
        CREATE INDEX idx_device_description ON unified_device(master_source_schema, site_id, description_norm);
        CREATE INDEX idx_map_source ON device_identity_map(source_schema, source_table_group, source_table, source_row_id);
        CREATE INDEX idx_candidate_stratum ON device_match_candidate(source_schema, source_table_group, site_id, source_table);
        """
    )
    return connection


def make_asset_device(schema: str, row: dict[str, str], snapshot_id: str, created_at: str) -> tuple[str, tuple[object, ...]] | None:
    site = clean(row.get("SITEID"))
    asset_number = clean(row.get("ASSETNUM"))
    if not site or not asset_number:
        return None
    device_id = sha_id("UD-", schema, site, asset_number)
    name = clean(row.get("DESCRIPTION"))
    return device_id, (
        device_id, "source_seed", schema, site, asset_number, clean(row.get("ASSETID")), name,
        norm_text(name), clean(row.get("LOCATION")), clean(row.get("PARENT")), clean(row.get("ORGID")),
        clean(row.get("CLASSSTRUCTUREID")), clean(row.get("CLASSIFICATIONID")), clean(row.get("STATUS")),
        f"{site}|{asset_number}", "seed", snapshot_id, created_at,
    )


def event_row_id(row: dict[str, str], ordinal: int) -> str:
    for field in ROW_ID_FIELDS:
        value = clean(row.get(field))
        if value:
            return f"{field}:{value}"
    return f"ROW:{ordinal}"


def event_description(row: dict[str, str]) -> str:
    for field in DESCRIPTION_FIELDS:
        value = clean(row.get(field))
        if value:
            return value
    return ""


def direct_key(row: dict[str, str]) -> tuple[str, str] | None:
    for field in DIRECT_KEY_FIELDS:
        value = clean(row.get(field))
        if value:
            return field, value
    return None


def read_operational_files(operational_root: pathlib.Path):
    for path in sorted(operational_root.glob("*.csv")):
        if path.name.startswith("manifest"):
            continue
        parts = path.stem.split("__", 2)
        if len(parts) != 3:
            continue
        schema, group, table = parts
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for ordinal, row in enumerate(csv.DictReader(handle), start=1):
                yield schema.upper(), group, table.upper(), ordinal, {key.upper(): clean(value) for key, value in row.items()}


def insert_asset_seeds(connection: sqlite3.Connection, asset_root: pathlib.Path, created_at: str) -> tuple[dict[str, int], dict[str, str]]:
    counts: dict[str, int] = Counter()
    snapshot_ids: dict[str, str] = {}
    for asset_file in sorted(asset_root.glob("*_saas__asset_master.csv")):
        schema = asset_file.name.split("__", 1)[0].upper()
        snapshot_ids[schema] = asset_root.name
        batch: list[tuple[object, ...]] = []
        raw = 0
        blank = 0
        duplicate = 0
        for row in read_asset_rows(asset_file):
            raw += 1
            made = make_asset_device(schema, row, asset_root.name, created_at)
            if made is None:
                blank += 1
                continue
            batch.append(made[1])
            if len(batch) >= 5000:
                before = connection.total_changes
                connection.executemany(
                    "INSERT OR IGNORE INTO unified_device VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch
                )
                duplicate += len(batch) - (connection.total_changes - before)
                batch.clear()
        if batch:
            before = connection.total_changes
            connection.executemany(
                "INSERT OR IGNORE INTO unified_device VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch
            )
            duplicate += len(batch) - (connection.total_changes - before)
        counts[f"{schema}:asset_raw"] = raw
        counts[f"{schema}:asset_blank_identity"] = blank
        counts[f"{schema}:asset_duplicate_identity"] = duplicate
        connection.commit()
    return dict(counts), snapshot_ids


def find_candidate(connection: sqlite3.Connection, schema: str, site: str, asset_key: tuple[str, str] | None,
                   location: str, description: str) -> tuple[str | None, str | None, float, str, str, dict[str, object]]:
    if asset_key and site:
        key_type, key_value = asset_key
        row = connection.execute(
            "SELECT unified_device_id, asset_number FROM unified_device WHERE master_source_schema=? AND site_id=? AND asset_number=?",
            (schema, site, asset_value := asset_key[1]),
        ).fetchone()
        if row:
            return row[0], row[1], 1.0, "exact_assetnum", "accepted", {"key_type": key_type, "asset_number": asset_value}
        return None, None, 0.0, "asset_key_not_found", "needs_review", {"key_type": key_type, "asset_number": asset_value}

    location = clean(location)
    description_norm = norm_text(description)
    evidence: dict[str, object] = {"direct_key_present": False}
    location_rows = []
    if site and location:
        location_rows = connection.execute(
            "SELECT unified_device_id, asset_number, canonical_name FROM unified_device WHERE master_source_schema=? AND site_id=? AND location_code=? LIMIT 3",
            (schema, site, location),
        ).fetchall()
    description_rows = []
    if site and description_norm:
        description_rows = connection.execute(
            "SELECT unified_device_id, asset_number, canonical_name FROM unified_device WHERE master_source_schema=? AND site_id=? AND description_norm=? LIMIT 3",
            (schema, site, description_norm),
        ).fetchall()
    if len(location_rows) == 1 and len(description_rows) == 1 and location_rows[0][0] == description_rows[0][0]:
        row = location_rows[0]
        evidence.update({"location_exact": True, "description_exact": True, "candidate_count": 1})
        return row[0], row[1], 0.95, "exact_location_and_description", "needs_review", evidence
    if len(location_rows) == 1:
        row = location_rows[0]
        evidence.update({"location_exact": True, "description_exact": False, "candidate_count": len(location_rows)})
        return row[0], row[1], 0.86, "exact_location", "needs_review", evidence
    if len(description_rows) == 1:
        row = description_rows[0]
        evidence.update({"location_exact": False, "description_exact": True, "candidate_count": len(description_rows)})
        return row[0], row[1], 0.78, "exact_description_same_site", "needs_review", evidence
    evidence.update({"location_exact": False, "description_exact": False, "location_candidate_count": len(location_rows), "description_candidate_count": len(description_rows)})
    return None, None, 0.0, "no_deterministic_identity_evidence", "blocked", evidence


def build_candidates(connection: sqlite3.Connection, operational_root: pathlib.Path, created_at: str, snapshot_id: str) -> dict[str, int]:
    counts: dict[str, int] = Counter()
    for schema, group, table, ordinal, row in read_operational_files(operational_root):
        site = clean(row.get("SITEID")) or clean(row.get("ASSETSITEID"))
        location = clean(row.get("LOCATION")) or clean(row.get("WORKLOCATION"))
        description = event_description(row)
        key = direct_key(row)
        source_row_id = event_row_id(row, ordinal)
        if key:
            source_key_type, source_key = key
        else:
            source_key_type, source_key = "EVENT_ID", source_row_id
        candidate_device_id, candidate_asset_number, score, method, decision, evidence = find_candidate(
            connection, schema, site, key, location, description
        )
        kks_code = clean(row.get("KKS")) or clean(row.get("C_FAULTKKS"))
        evidence.update({"source_fields": sorted(row.keys()), "kks_code_present": bool(kks_code), "kks_indexed": False})
        cursor = connection.execute(
            """INSERT INTO device_match_candidate
               (source_schema, source_table_group, source_table, source_row_id, source_key_type, source_key,
                site_id, raw_description, location_code, kks_code, candidate_device_id, candidate_asset_number,
                score, match_method, decision, evidence_json, source_snapshot_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (schema, group, table, source_row_id, source_key_type, source_key, site, description, location, kks_code,
             candidate_device_id, candidate_asset_number, score, method, decision, json_text(evidence), snapshot_id, created_at),
        )
        candidate_id = cursor.lastrowid
        connection.execute(
            """INSERT INTO device_identity_map
               (unified_device_id, candidate_device_id, source_schema, source_table_group, source_table, source_row_id,
                source_key_type, source_key, site_id, raw_description, location_code, match_method, match_confidence,
                status, evidence_json, source_snapshot_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (candidate_device_id if decision == "accepted" else None, str(candidate_id), schema, group, table, source_row_id,
             source_key_type, source_key, site, description, location, method, score, decision, json_text(evidence), snapshot_id, created_at),
        )
        counts[f"candidate:{decision}"] += 1
        counts[f"candidate:{schema}:{group}"] += 1
    connection.commit()
    return dict(counts)


def build_cross_system_candidates(connection: sqlite3.Connection, created_at: str) -> int:
    rows = connection.execute(
        """SELECT l.unified_device_id, r.unified_device_id, l.site_id, l.asset_number,
                  l.canonical_name, r.canonical_name
           FROM unified_device l JOIN unified_device r
             ON l.site_id=r.site_id AND l.asset_number=r.asset_number
            AND l.master_source_schema < r.master_source_schema"""
    ).fetchall()
    for left_id, right_id, site, asset_number, left_name, right_name in rows:
        connection.execute(
            """INSERT OR IGNORE INTO cross_system_match_candidate
               (left_device_id, right_device_id, site_id, asset_number, score, match_method, decision, evidence_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (left_id, right_id, site, asset_number, 0.98, "same_site_assetnum_cross_schema", "needs_review",
             json_text({"left_name": left_name, "right_name": right_name, "automatic_merge": False}), created_at),
        )
    connection.commit()
    return len(rows)


def select_samples(connection: sqlite3.Connection, target: int = 200) -> int:
    connection.execute("DELETE FROM identity_sample_200")
    rows = list(connection.execute(
        """SELECT candidate_id, source_schema, source_table_group, source_table, site_id
           FROM device_match_candidate
          ORDER BY source_schema, source_table_group, site_id, source_table, candidate_id"""
    ))
    by_group: dict[tuple[str, str], list[tuple[object, ...]]] = defaultdict(list)
    for row in rows:
        if row[4]:
            by_group[(str(row[1]), str(row[2]))].append(row)
    groups = sorted(by_group)
    if not groups:
        return 0
    base, remainder = divmod(target, len(groups))
    quotas = {group_key: base + (1 if index < remainder else 0) for index, group_key in enumerate(groups)}
    chosen: list[tuple[int, str]] = []
    selected: set[int] = set()
    group_counts = {group_key: 0 for group_key in groups}
    for group_key in groups:
        quota = quotas[group_key]
        group_rows = by_group[group_key]
        seen_sites: set[str] = set()
        for candidate_id, schema, group, table, site in group_rows:
            if group_counts[group_key] >= quota:
                break
            if str(site) in seen_sites:
                continue
            seen_sites.add(str(site))
            selected.add(int(candidate_id))
            chosen.append((int(candidate_id), f"{schema}|{group}|{site}|site"))
            group_counts[group_key] += 1
        if group_counts[group_key] < quota:
            for candidate_id, schema, group, table, site in group_rows:
                if int(candidate_id) in selected:
                    continue
                selected.add(int(candidate_id))
                chosen.append((int(candidate_id), f"{schema}|{group}|{site}|fill"))
                group_counts[group_key] += 1
                if group_counts[group_key] >= quota:
                    break
    if len(chosen) < target:
        for candidate_id, schema, group, table, site in rows:
            if int(candidate_id) in selected:
                continue
            selected.add(int(candidate_id))
            chosen.append((int(candidate_id), f"{schema}|{group}|{site or '(blank)'}|fallback"))
            if len(chosen) >= target:
                break
    for rank, (candidate_id, stratum) in enumerate(chosen, start=1):
        connection.execute(
            "INSERT INTO identity_sample_200(candidate_id, sample_stratum, sample_rank) VALUES (?,?,?)",
            (candidate_id, stratum, rank),
        )
    connection.commit()
    return len(chosen)


def query_counts(connection: sqlite3.Connection) -> dict[str, object]:
    def scalar(sql: str) -> int:
        return int(connection.execute(sql).fetchone()[0])

    by_decision = {
        row[0]: row[1] for row in connection.execute("SELECT decision, COUNT(*) FROM device_match_candidate GROUP BY decision")
    }
    by_schema = {
        f"{row[0]}:{row[1]}": row[2]
        for row in connection.execute(
            "SELECT source_schema, source_table_group, COUNT(*) FROM device_match_candidate GROUP BY source_schema, source_table_group"
        )
    }
    return {
        "unified_device_count": scalar("SELECT COUNT(*) FROM unified_device"),
        "identity_map_count": scalar("SELECT COUNT(*) FROM device_identity_map"),
        "candidate_count": scalar("SELECT COUNT(*) FROM device_match_candidate"),
        "candidate_by_decision": by_decision,
        "candidate_by_schema_group": by_schema,
        "cross_system_candidate_count": scalar("SELECT COUNT(*) FROM cross_system_match_candidate"),
        "sample_count": scalar("SELECT COUNT(*) FROM identity_sample_200"),
        "duplicate_source_identity_count": scalar(
            "SELECT COUNT(*) FROM (SELECT master_source_schema, site_id, asset_number FROM unified_device GROUP BY 1,2,3 HAVING COUNT(*)>1)"
        ),
        "cross_site_asset_key_collision_count": scalar(
            "SELECT COUNT(*) FROM (SELECT master_source_schema, asset_number FROM unified_device GROUP BY 1,2 HAVING COUNT(DISTINCT site_id)>1)"
        ),
    }


def export_sample(connection: sqlite3.Connection, output: pathlib.Path) -> None:
    cursor = connection.execute(
        """SELECT s.sample_rank, s.sample_stratum, c.source_schema, c.source_table_group, c.source_table,
                  c.source_row_id, c.site_id, c.source_key_type, c.source_key, c.raw_description, c.location_code,
                  c.candidate_asset_number, c.score, c.match_method, c.decision, c.evidence_json
             FROM identity_sample_200 s JOIN device_match_candidate c ON c.candidate_id=s.candidate_id
            ORDER BY s.sample_rank"""
    )
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([item[0] for item in cursor.description])
        writer.writerows(cursor.fetchall())


def load_frozen_baseline() -> dict[str, object] | None:
    baseline_path_value = os.environ.get("IDENTITY_BASELINE_FILE")
    baseline_path = pathlib.Path(baseline_path_value) if baseline_path_value else ROOT / "config" / "identity_baseline.json"
    if not baseline_path.is_absolute():
        baseline_path = (REPO_ROOT / baseline_path).resolve()
    if not baseline_path.exists():
        return None
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["baseline_file"] = str(baseline_path)
    return baseline


def main() -> None:
    parser = argparse.ArgumentParser(description="Build source-scoped device identity results from read-only snapshots")
    parser.add_argument(
        "--enable-cross-system-candidates",
        action="store_true",
        help="Optionally generate cross-schema diagnostic candidates; never auto-merges devices.",
    )
    args = parser.parse_args()
    baseline = load_frozen_baseline()
    asset_root_value = os.environ.get("IDENTITY_ASSET_SNAPSHOT")
    if not asset_root_value and baseline:
        asset_root_value = str(baseline["asset_snapshot"]["relative_path"])
    asset_root = pathlib.Path(asset_root_value) if asset_root_value else latest_dir("identity-source-snapshot-*/")
    operational_root_value = os.environ.get("IDENTITY_OPERATIONAL_SNAPSHOT")
    if not operational_root_value and baseline:
        operational_root_value = str(baseline["operational_snapshot"]["relative_path"])
    operational_root = pathlib.Path(operational_root_value) if operational_root_value else latest_dir("identity-operational-snapshot-*/")
    if not asset_root.is_absolute():
        asset_root = (REPO_ROOT / asset_root).resolve()
    if not operational_root.is_absolute():
        operational_root = (REPO_ROOT / operational_root).resolve()
    run_time = datetime.now(timezone.utc)
    run_id = f"identity-layer-v1-{run_time.strftime('%Y%m%dT%H%M%SZ')}"
    output_root = ROOT / "results" / run_id
    output_root.mkdir(parents=True, exist_ok=False)
    db_path = output_root / "identity_semantics.sqlite3"
    connection = init_db(db_path)
    created_at = run_time.isoformat()
    counts, source_snapshot_ids = insert_asset_seeds(connection, asset_root, created_at)
    candidate_counts = build_candidates(connection, operational_root, created_at, operational_root.name)
    cross_system_count = build_cross_system_candidates(connection, created_at) if args.enable_cross_system_candidates else 0
    sample_count = select_samples(connection, target=200)
    summary = query_counts(connection)
    connection.execute(
        "INSERT INTO identity_run VALUES (?,?,?,?,?,?,?)",
        (run_id, asset_root.name, operational_root.name, 1, 0, 0, created_at),
    )
    connection.commit()
    sample_path = output_root / "identity_sample_200.csv"
    export_sample(connection, sample_path)
    connection.close()
    manifest = {
        "run_id": run_id,
        "baseline_id": baseline.get("baseline_id") if baseline else None,
        "baseline_file": baseline.get("baseline_file") if baseline else None,
        "asset_snapshot_id": asset_root.name,
        "operational_snapshot_id": operational_root.name,
        "source_snapshots": source_snapshot_ids,
        "source_asset_counts": counts,
        "candidate_counts": candidate_counts,
        "cross_system_candidate_count": cross_system_count,
        "summary": summary,
        "policy": {
            "source_write": False,
            "formal_publication": False,
            "cross_system_merge_required": False,
            "cross_system_candidate_generation": "disabled_by_default",
            "cross_system_candidates_enabled": bool(args.enable_cross_system_candidates),
            "cross_schema_auto_merge": False,
            "exact_same_schema_assetnum_auto_accept": True,
            "location_or_description_only": "needs_review",
            "kks": "captured as evidence when present; no ASSET KKS index in this snapshot",
        },
        "artifacts": {"sqlite": db_path.name, "sample_csv": sample_path.name},
        "created_at": created_at,
    }
    write_manifest(output_root / "manifest.json", manifest)
    print(json.dumps({"run_id": run_id, "output": str(output_root), "summary": summary, "read_only": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
