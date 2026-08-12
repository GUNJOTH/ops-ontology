"""Extract HD GRP attribute, classification and domain terminology candidates read-only."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import pathlib
import sys
import unicodedata
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[2]
sys.path.insert(0, str(REPO_ROOT / "equipment-description-harness" / "tools" / "dmPython-src"))
dmdbms_bin = os.environ.get("DMDBMS_BIN")
if dmdbms_bin and pathlib.Path(dmdbms_bin).is_dir():
    os.add_dll_directory(dmdbms_bin)
import dmPython  # noqa: E402

OUTPUT_DIR = ROOT / "terminology"
OBJECTS = [
    "ASSET", "LOCATIONS", "LOCHIERARCHY", "CLASSIFICATION", "CLASSSTRUCTURE", "ASSETATTRIBUTE",
    "ASSETSPEC", "ASSETFEATURE", "ASSETFEATURESPEC", "ASSETMETER", "ASSETHIERARCHY", "ASSETLOCRELATION",
]


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", clean(value))
    return " ".join(text.split())


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def write_csv(path: pathlib.Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def term_id(kind: str, source_table: str, key: str, source_term: str) -> str:
    return hashlib.sha256(f"{kind}|{source_table}|{key}|{source_term}".encode("utf-8")).hexdigest()


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_id = json.loads((ROOT / "snapshot" / "manifest.json").read_text(encoding="utf-8"))["source_snapshot_id"]
    dsn = os.environ.get("HD_DM_DSN")
    if not dsn:
        raise SystemExit("请通过 HD_DM_DSN 环境变量提供 DM8 连接地址")
    conn = dmPython.connect(
        user=os.environ.get("HD_DM_USER", "HD_SAAS"), password=os.environ["HD_DM_PASSWORD"],
        dsn=dsn, schema="HD_SAAS", access_mode=dmPython.DSQL_MODE_READ_ONLY, autoCommit=True,
    )
    cursor = conn.cursor()
    object_literals = ",".join(sql_literal(value) for value in OBJECTS)
    attr_fields = [
        "OBJECTNAME", "ENTITYNAME", "ATTRIBUTENAME", "ALIAS", "COLUMNNAME", "TITLE", "REMARKS", "DOMAINID",
        "SAMEASATTRIBUTE", "SAMEASOBJECT", "TYPE", "LENGTH", "SCALE", "REQUIRED", "MUSTBE", "PERSISTENT",
        "PRIMARYKEYCOLSEQ", "USERDEFINED", "ISVISIBLE",
    ]
    cursor.execute(
        "SELECT %s FROM GRPATTRIBUTE WHERE UPPER(OBJECTNAME) IN (%s) ORDER BY OBJECTNAME, ATTRIBUTENAME"
        % (", ".join('"%s"' % field for field in attr_fields), object_literals)
    )
    attribute_rows = [dict(zip(attr_fields, (clean(value) for value in row))) for row in cursor.fetchall()]
    write_csv(OUTPUT_DIR / "grp_attribute_candidates.csv", attr_fields, attribute_rows)

    domain_ids = sorted({row["DOMAINID"] for row in attribute_rows if row["DOMAINID"]})
    domain_rows: list[dict[str, str]] = []
    for table, fields in [
        ("ALNDOMAIN", ["DOMAINID", "VALUE", "DESCRIPTION", "SITEID", "ORGID", "VALUEID", "SEQNUM", "PARENTVALUE"]),
        ("SYNONYMDOMAIN", ["DOMAINID", "MAXVALUE", "VALUE", "DESCRIPTION", "DEFAULTS", "SITEID", "ORGID", "VALUEID"]),
    ]:
        for offset in range(0, len(domain_ids), 500):
            chunk = domain_ids[offset : offset + 500]
            if not chunk:
                continue
            cursor.execute(
                "SELECT %s FROM %s WHERE DOMAINID IN (%s)"
                % (", ".join('"%s"' % field for field in fields), table, ",".join(sql_literal(value) for value in chunk))
            )
            for row in cursor.fetchall():
                domain_rows.append({"SOURCE_TABLE": table, **dict(zip(fields, (clean(value) for value in row)))})
    domain_fields = ["SOURCE_TABLE", "DOMAINID", "VALUE", "MAXVALUE", "DESCRIPTION", "DEFAULTS", "SITEID", "ORGID", "VALUEID", "SEQNUM", "PARENTVALUE"]
    write_csv(OUTPUT_DIR / "domain_value_candidates.csv", domain_fields, domain_rows)

    class_rows: list[dict[str, str]] = []
    for table_name, file_name, fields in [
        ("CLASSSTRUCTURE", "classstructure_terms.csv", ["CLASSSTRUCTUREID", "DESCRIPTION", "PARENT", "CLASSIFICATIONID", "HIERARCHYPATH", "SITEID", "ORGID"]),
        ("CLASSIFICATION", "classification_terms.csv", ["CLASSIFICATIONID", "DESCRIPTION", "SITEID", "ORGID"]),
    ]:
        source_file = ROOT / "context" / ("classstructure.csv" if table_name == "CLASSSTRUCTURE" else "classification.csv")
        with source_file.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        table_rows = [{"SOURCE_TABLE": table_name, **{field: clean(row.get(field)) for field in fields}} for row in rows]
        write_csv(OUTPUT_DIR / file_name, ["SOURCE_TABLE"] + fields, table_rows)
        class_rows.extend(table_rows)

    candidates: list[dict[str, str]] = []
    candidate_fields = ["TERM_ID", "TERM_KIND", "SOURCE_TABLE", "SOURCE_OBJECT", "SOURCE_KEY", "SOURCE_TERM", "CANDIDATE_STANDARD_TERM", "DOMAIN_ID", "SOURCE_EVIDENCE_JSON", "STATUS", "REVIEW_NOTE"]
    for row in attribute_rows:
        source_term = normalize(row.get("TITLE")) or normalize(row.get("ALIAS")) or normalize(row.get("ATTRIBUTENAME")) or normalize(row.get("COLUMNNAME"))
        if not source_term:
            continue
        evidence = {field: row.get(field, "") for field in ["OBJECTNAME", "ATTRIBUTENAME", "COLUMNNAME", "TITLE", "REMARKS", "DOMAINID", "SAMEASATTRIBUTE", "SAMEASOBJECT", "TYPE", "REQUIRED"]}
        candidates.append({
            "TERM_ID": term_id("attribute", "GRPATTRIBUTE", row.get("OBJECTNAME", "") + "." + row.get("ATTRIBUTENAME", ""), source_term),
            "TERM_KIND": "attribute",
            "SOURCE_TABLE": "GRPATTRIBUTE",
            "SOURCE_OBJECT": row.get("OBJECTNAME", ""),
            "SOURCE_KEY": row.get("ATTRIBUTENAME", ""),
            "SOURCE_TERM": source_term,
            "CANDIDATE_STANDARD_TERM": "",
            "DOMAIN_ID": row.get("DOMAINID", ""),
            "SOURCE_EVIDENCE_JSON": json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
            "STATUS": "candidate",
            "REVIEW_NOTE": "需人工确认标准术语及是否允许替换",
        })
    for row in class_rows:
        source_term = normalize(row.get("DESCRIPTION"))
        if not source_term:
            continue
        key = row.get("CLASSSTRUCTUREID") or row.get("CLASSIFICATIONID") or ""
        candidates.append({
            "TERM_ID": term_id("classification", row.get("SOURCE_TABLE", ""), key, source_term),
            "TERM_KIND": "classification",
            "SOURCE_TABLE": row.get("SOURCE_TABLE", ""),
            "SOURCE_OBJECT": "",
            "SOURCE_KEY": key,
            "SOURCE_TERM": source_term,
            "CANDIDATE_STANDARD_TERM": source_term,
            "DOMAIN_ID": "",
            "SOURCE_EVIDENCE_JSON": json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            "STATUS": "candidate",
            "REVIEW_NOTE": "分类名称作为设备类型证据，需确认标准写法",
        })
    for row in domain_rows:
        source_term = normalize(row.get("VALUE"))
        if not source_term:
            continue
        standard = normalize(row.get("MAXVALUE")) if row.get("SOURCE_TABLE") == "SYNONYMDOMAIN" else normalize(row.get("DESCRIPTION"))
        candidates.append({
            "TERM_ID": term_id("domain", row.get("SOURCE_TABLE", ""), row.get("DOMAINID", "") + "." + row.get("VALUE", ""), source_term),
            "TERM_KIND": "domain",
            "SOURCE_TABLE": row.get("SOURCE_TABLE", ""),
            "SOURCE_OBJECT": "",
            "SOURCE_KEY": row.get("DOMAINID", ""),
            "SOURCE_TERM": source_term,
            "CANDIDATE_STANDARD_TERM": standard,
            "DOMAIN_ID": row.get("DOMAINID", ""),
            "SOURCE_EVIDENCE_JSON": json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            "STATUS": "candidate",
            "REVIEW_NOTE": "域值标准写法候选，需确认业务展示和单位规则",
        })
    write_csv(OUTPUT_DIR / "terminology_candidates.csv", candidate_fields, candidates)
    cursor.close()
    conn.close()
    manifest = {
        "terminology_run_id": "hd-terminology-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "source_snapshot_id": snapshot_id,
        "source_schema": "HD_SAAS",
        "source_write": False,
        "grp_attribute_rows": len(attribute_rows),
        "domain_value_rows": len(domain_rows),
        "classification_term_rows": len(class_rows),
        "terminology_candidate_rows": len(candidates),
        "terminology_version": "0.1.0",
        "status": "candidate_only",
        "approval_required": True,
    }
    (OUTPUT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(manifest)


if __name__ == "__main__":
    main()
