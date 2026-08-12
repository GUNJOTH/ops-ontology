"""Read-only HD_SAAS ASSET snapshot for the single-schema semantic pilot."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[2]
sys.path.insert(0, str(REPO_ROOT / "equipment-description-harness" / "tools" / "dmPython-src"))
dmdbms_bin = os.environ.get("DMDBMS_BIN")
if dmdbms_bin and pathlib.Path(dmdbms_bin).is_dir():
    os.add_dll_directory(dmdbms_bin)
import dmPython  # noqa: E402


FIELDS = [
    "ASSETID", "SITEID", "ORGID", "ASSETNUM", "DESCRIPTION", "PARENT", "LOCATION", "STATUS",
    "SERIALNUM", "MANUFACTURER", "ITEMNUM", "CLASSSTRUCTUREID", "ASSETTYPE",
    "PLUSCMODELNUM", "S_MODELNUM", "CHANGEDATE",
]


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def main() -> None:
    user = os.environ.get("HD_DM_USER", "HD_SAAS")
    password = os.environ.get("HD_DM_PASSWORD")
    if not password:
        raise SystemExit("请通过 HD_DM_PASSWORD 环境变量提供 HD_SAAS 只读账号密码")
    dsn = os.environ.get("HD_DM_DSN")
    if not dsn:
        raise SystemExit("请通过 HD_DM_DSN 环境变量提供 DM8 连接地址")

    output_dir = ROOT / "snapshot"
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = "hd-saas-snapshot-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    connection = dmPython.connect(
        user=user,
        password=password,
        dsn=dsn,
        schema="HD_SAAS",
        access_mode=dmPython.DSQL_MODE_READ_ONLY,
        autoCommit=True,
    )
    site_metrics: list[dict[str, object]] = []
    all_keys: set[tuple[str, str]] = set()
    duplicate_keys: list[tuple[str, str]] = []
    total_rows = 0
    invalid_identity_count = 0
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT DISTINCT SITEID FROM ASSET WHERE SITEID IS NOT NULL ORDER BY SITEID")
        sites = [row[0] for row in cursor.fetchall()]
        cursor.close()
        for site_value in sites:
            site = clean(site_value)
            safe_site = site.replace("'", "''")
            predicate = "SITEID IS NULL" if site_value is None else f"SITEID = '{safe_site}'"
            file_stem = "__NULL_SITE__" if site_value is None else site.replace("\\", "_").replace("/", "_")
            output = output_dir / (file_stem + ".csv")
            sql = (
                "SELECT " + ", ".join(FIELDS) + " FROM ASSET "
                f"WHERE {predicate} ORDER BY ASSETNUM"
            )
            cursor = connection.cursor()
            cursor.execute(sql)
            rows = 0
            sha = hashlib.sha256()
            with output.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(FIELDS)
                for row in iter(lambda: cursor.fetchmany(5000), []):
                    writer.writerows(row)
                    for values in row:
                        encoded = ("\x1f".join(clean(value) for value in values) + "\n").encode("utf-8")
                        sha.update(encoded)
                        rows += 1
                        total_rows += 1
                        identity = (site, clean(values[3]))
                        if not identity[0] or not identity[1]:
                            invalid_identity_count += 1
                        elif identity in all_keys:
                            duplicate_keys.append(identity)
                        else:
                            all_keys.add(identity)
            cursor.close()
            site_metrics.append({"site": site, "rows": rows, "file": output.name, "sha256": sha.hexdigest()})
            print({"site": site, "rows": rows, "file": str(output)})
    finally:
        connection.close()

    source_snapshot_id = hashlib.sha256(
        json.dumps(site_metrics, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    manifest = {
        "run_id": run_id,
        "source_snapshot_id": source_snapshot_id,
        "source_schema": "HD_SAAS",
        "source_host": dsn,
        "source_table": "ASSET",
        "read_only": True,
        "filter": "none",
        "identity_key": ["source_schema", "SITEID", "ASSETNUM"],
        "source_surrogate": "ASSETID",
        "source_row_count": total_rows,
        "distinct_identity_count": len(all_keys),
        "duplicate_identity_count": len(duplicate_keys),
        "invalid_identity_count": invalid_identity_count,
        "site_count": len(site_metrics),
        "sites": site_metrics,
        "rule_version": "1.0.5",
        "contract_version": "1.0.3",
        "formal_publication": False,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print({"source_snapshot_id": source_snapshot_id, "rows": manifest["source_row_count"], "distinct": manifest["distinct_identity_count"], "duplicates": manifest["duplicate_identity_count"], "invalid": manifest["invalid_identity_count"]})


if __name__ == "__main__":
    main()
