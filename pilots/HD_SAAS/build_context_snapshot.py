"""Build compact HD_SAAS context from read-only source tables for active candidates."""
from __future__ import annotations

import csv
import json
import os
import pathlib
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[2]
sys.path.insert(0, str(REPO_ROOT / "equipment-description-harness" / "tools" / "dmPython-src"))
dmdbms_bin = os.environ.get("DMDBMS_BIN")
if dmdbms_bin and pathlib.Path(dmdbms_bin).is_dir():
    os.add_dll_directory(dmdbms_bin)
import dmPython  # noqa: E402


SNAPSHOT_MANIFEST = ROOT / "snapshot" / "manifest.json"
ACTIVE_CANDIDATES = ROOT / "candidates" / "equipment_description_candidates.csv"
CONTEXT_DIR = ROOT / "context"
CONTEXT_MANIFEST = CONTEXT_DIR / "manifest.json"

CONTEXT_FIELDS = {
    "LOCATIONS": [
        "LOCATION", "DESCRIPTION", "TYPE", "CLASSSTRUCTUREID", "SITEID", "ORGID", "STATUS",
        "STATUSDATE", "LOCATIONSID", "LEVEL1", "LEVEL2", "C_SYSTEM", "LOCATIONSCODE",
    ],
    "LOCHIERARCHY": ["LOCATION", "PARENT", "SITEID", "ORGID", "LOCHIERARCHYID"],
    "CLASSSTRUCTURE": ["CLASSSTRUCTUREID", "DESCRIPTION", "GENASSETDESC", "PARENT", "CLASSIFICATIONID", "ORGID", "SITEID", "HIERARCHYPATH"],
    "CLASSIFICATION": ["CLASSIFICATIONID", "DESCRIPTION", "ORGID", "SITEID"],
    "ASSETSPEC": [
        "ASSETNUM", "ASSETATTRID", "CLASSSTRUCTUREID", "NUMVALUE", "MEASUREUNITID", "ALNVALUE",
        "CHANGEDATE", "ES01", "ES02", "ES03", "ES04", "ES05", "SITEID", "ORGID", "SECTION",
        "ASSETSPECID", "MANDATORY", "TABLEVALUE",
    ],
    "ASSETFEATURE": ["ASSETFEATUREID", "ASSETNUM", "CLASSSTRUCTUREID", "FEATURE", "LABEL", "SITEID", "ORGID"],
    "ASSETFEATURESPEC": [
        "ASSETFEATURESPECID", "ASSETNUM", "ASSETATTRID", "CLASSSTRUCTUREID", "FEATURE", "ALNVALUE",
        "NUMVALUE", "TABLEVALUE", "SITEID", "ORGID", "CHANGEDATE", "MANDATORY",
    ],
    "ASSETMETER": ["ASSETNUM", "METERNAME", "ACTIVE", "MEASUREUNITID", "LASTREADING", "SITEID", "ORGID", "CHANGEDATE"],
    "ASSETHIERARCHY": ["ASSETNUM", "PARENT", "WONUM", "LOCATION", "SITEID", "ORGID", "ASSETHIERARCHYID"],
    "ASSETLOCRELATION": [
        "SOURCEASSETNUM", "SOURCELOCATION", "TARGETASSETNUM", "TARGETLOCATION", "SITEID", "ORGID",
        "ASSETLOCRELATIONID", "ASSETRELATIONNUM", "CREATEDDATE",
    ],
}


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def query_rows(cursor, table: str, fields: list[str], predicate: str) -> list[dict[str, str]]:
    select_fields = ", ".join('"%s"' % field for field in fields)
    cursor.execute("SELECT %s FROM %s WHERE %s" % (select_fields, table, predicate))
    return [dict(zip(fields, (clean(value) for value in row))) for row in cursor.fetchall()]


def write_rows(table: str, rows: list[dict[str, str]]) -> None:
    path = CONTEXT_DIR / (table.lower() + ".csv")
    fields = CONTEXT_FIELDS[table]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    snapshot = json.loads(SNAPSHOT_MANIFEST.read_text(encoding="utf-8"))
    active_keys: set[tuple[str, str]] = set()
    active_sites: set[str] = set()
    location_keys: set[tuple[str, str]] = set()
    classstructure_ids: set[str] = set()
    with ACTIVE_CANDIDATES.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            site = clean(row.get("SITEID"))
            asset = clean(row.get("ASSETNUM"))
            active_keys.add((site, asset))
            active_sites.add(site)
            if clean(row.get("LOCATION")):
                location_keys.add((site, clean(row.get("LOCATION"))))
            if clean(row.get("CLASSSTRUCTUREID")):
                classstructure_ids.add(clean(row.get("CLASSSTRUCTUREID")))

    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    dsn = os.environ.get("HD_DM_DSN")
    if not dsn:
        raise SystemExit("请通过 HD_DM_DSN 环境变量提供 DM8 连接地址")
    connection = dmPython.connect(
        user=os.environ.get("HD_DM_USER", "HD_SAAS"),
        password=os.environ["HD_DM_PASSWORD"],
        dsn=dsn,
        schema="HD_SAAS",
        access_mode=dmPython.DSQL_MODE_READ_ONLY,
        autoCommit=True,
    )
    context_counts: dict[str, int] = {}
    missing: Counter[str] = Counter()
    try:
        cursor = connection.cursor()

        # Locations are restricted to locations referenced by active devices.
        locations: list[dict[str, str]] = []
        for site in sorted(active_sites):
            site_locations = sorted(location for location_site, location in location_keys if location_site == site)
            for offset in range(0, len(site_locations), 500):
                chunk = site_locations[offset : offset + 500]
                if not chunk:
                    continue
                predicate = "SITEID = %s AND LOCATION IN (%s)" % (
                    sql_literal(site), ",".join(sql_literal(value) for value in chunk)
                )
                locations.extend(query_rows(cursor, "LOCATIONS", CONTEXT_FIELDS["LOCATIONS"], predicate))
        write_rows("LOCATIONS", locations)
        context_counts["LOCATIONS"] = len(locations)
        location_index = {(row["SITEID"], row["LOCATION"]): row for row in locations}

        hierarchies: list[dict[str, str]] = []
        for site in sorted(active_sites):
            hierarchies.extend(query_rows(cursor, "LOCHIERARCHY", CONTEXT_FIELDS["LOCHIERARCHY"], "SITEID = %s" % sql_literal(site)))
        write_rows("LOCHIERARCHY", hierarchies)
        context_counts["LOCHIERARCHY"] = len(hierarchies)
        hierarchy_index = {(row["SITEID"], row["LOCATION"]): row for row in hierarchies}

        classstructures: list[dict[str, str]] = []
        ids = sorted(classstructure_ids)
        for offset in range(0, len(ids), 500):
            chunk = ids[offset : offset + 500]
            if chunk:
                classstructures.extend(query_rows(cursor, "CLASSSTRUCTURE", CONTEXT_FIELDS["CLASSSTRUCTURE"], "CLASSSTRUCTUREID IN (%s)" % ",".join(sql_literal(value) for value in chunk)))
        write_rows("CLASSSTRUCTURE", classstructures)
        context_counts["CLASSSTRUCTURE"] = len(classstructures)
        classstructure_index = {row["CLASSSTRUCTUREID"]: row for row in classstructures}

        classification_ids = sorted({row["CLASSIFICATIONID"] for row in classstructures if row["CLASSIFICATIONID"]})
        classifications: list[dict[str, str]] = []
        for offset in range(0, len(classification_ids), 500):
            chunk = classification_ids[offset : offset + 500]
            if chunk:
                classifications.extend(query_rows(cursor, "CLASSIFICATION", CONTEXT_FIELDS["CLASSIFICATION"], "CLASSIFICATIONID IN (%s)" % ",".join(sql_literal(value) for value in chunk)))
        write_rows("CLASSIFICATION", classifications)
        context_counts["CLASSIFICATION"] = len(classifications)
        classification_index = {row["CLASSIFICATIONID"]: row for row in classifications}

        # The remaining device-context tables are read by site and filtered by the active identity.
        table_rows: dict[str, list[dict[str, str]]] = {table: [] for table in CONTEXT_FIELDS if table not in {"LOCATIONS", "LOCHIERARCHY", "CLASSSTRUCTURE", "CLASSIFICATION"}}
        for site in sorted(active_sites):
            site_predicate = "SITEID = %s" % sql_literal(site)
            for table in table_rows:
                rows = query_rows(cursor, table, CONTEXT_FIELDS[table], site_predicate)
                if table == "ASSETLOCRELATION":
                    table_rows[table].extend(rows)
                else:
                    table_rows[table].extend(row for row in rows if (row.get("SITEID", site), row.get("ASSETNUM", "")) in active_keys)
        for table, rows in table_rows.items():
            write_rows(table, rows)
            context_counts[table] = len(rows)

        for site, location in location_keys:
            if (site, location) not in location_index:
                missing["MISSING_LOCATION"] += 1
            if (site, location) not in hierarchy_index:
                missing["MISSING_LOCATION_HIERARCHY"] += 1
        for class_id in classstructure_ids:
            if class_id not in classstructure_index:
                missing["MISSING_CLASSSTRUCTURE"] += 1
        for row in table_rows["ASSETLOCRELATION"]:
            for endpoint_field in ("SOURCEASSETNUM", "TARGETASSETNUM"):
                endpoint = row.get(endpoint_field, "")
                if endpoint and (row.get("SITEID", ""), endpoint) not in active_keys:
                    missing["RELATION_ENDPOINT_OUTSIDE_ACTIVE_SCOPE"] += 1
        cursor.close()
    finally:
        connection.close()

    manifest = {
        "context_run_id": "hd-context-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "source_snapshot_id": snapshot["source_snapshot_id"],
        "source_schema": "HD_SAAS",
        "source_host": dsn,
        "active_candidate_rows": len(active_keys),
        "active_sites": sorted(active_sites),
        "context_tables": context_counts,
        "consistency_warnings": dict(missing),
        "rule_version": "1.0.5",
        "validator_version": "hd-context-validator-1.0.0",
        "source_write": False,
        "formal_publication": False,
    }
    CONTEXT_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(manifest)


if __name__ == "__main__":
    main()
