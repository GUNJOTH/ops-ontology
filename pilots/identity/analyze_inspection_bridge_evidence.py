"""Build a read-only evidence report for blocked inspection identity links."""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import sqlite3
from collections import Counter, defaultdict

ROOT = pathlib.Path(__file__).resolve().parent


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def kks_like(value: object) -> bool:
    text = clean(value).upper()
    return bool(text and len(text) >= 6 and re.fullmatch(r"[A-Z0-9][A-Z0-9._/-]*", text) and re.search(r"[A-Z]", text) and re.search(r"\d", text))


def rows(path: pathlib.Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{str(k).upper(): clean(v) for k, v in row.items()} for row in csv.DictReader(handle)]


def latest(path: pathlib.Path, pattern: str) -> pathlib.Path:
    matches = sorted(path.glob(pattern), reverse=True)
    if not matches:
        raise SystemExit(f"no match: {pattern}")
    return matches[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-root", required=True)
    parser.add_argument("--operational-root", required=True)
    parser.add_argument("--asset-root", required=True)
    parser.add_argument("--fresh-operational-root", required=True)
    parser.add_argument("--context-root", required=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    result_root = pathlib.Path(args.result_root).resolve()
    old_root = pathlib.Path(args.operational_root).resolve()
    fresh_root = pathlib.Path(args.fresh_operational_root).resolve()
    asset_root = pathlib.Path(args.asset_root).resolve()
    context_root = pathlib.Path(args.context_root).resolve()

    assets_by_key: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    assets_by_location: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for path in sorted(asset_root.glob("*_saas__asset_master.csv")):
        schema = path.name.split("__", 1)[0].upper()
        for asset in rows(path):
            site = clean(asset.get("SITEID"))
            asset_number = clean(asset.get("ASSETNUM"))
            itemnum = clean(asset.get("ITEMNUM"))
            location = clean(asset.get("LOCATION"))
            record = {
                "assetnum": asset_number,
                "itemnum": itemnum,
                "description": clean(asset.get("DESCRIPTION")),
                "location": location,
                "parent": clean(asset.get("PARENT")),
                "status": clean(asset.get("STATUS")),
                "assetid": clean(asset.get("ASSETID")),
            }
            if site and itemnum:
                assets_by_key[(schema, site, itemnum)].append(record)
            if site and location:
                assets_by_location[(schema, site, location)].append(record)

    fresh_by_file: dict[str, list[dict[str, str]]] = {}
    for path in sorted(fresh_root.glob("*inspection*.csv")):
        fresh_by_file[path.name] = rows(path)
    old_by_file: dict[str, list[dict[str, str]]] = {}
    for path in sorted(old_root.glob("*inspection*.csv")):
        old_by_file[path.name] = rows(path)
    fresh_xjjlid_by_file: dict[str, dict[str, dict[str, str]]] = {
        name: {clean(r.get("XJJLID")): r for r in file_rows if clean(r.get("XJJLID"))}
        for name, file_rows in fresh_by_file.items()
    }
    old_xjjlid_by_file: dict[str, dict[str, dict[str, str]]] = {
        name: {clean(r.get("XJJLID")): r for r in file_rows if clean(r.get("XJJLID"))}
        for name, file_rows in old_by_file.items()
    }

    con = sqlite3.connect(result_root / "identity_semantics.sqlite3")
    con.row_factory = sqlite3.Row
    blocked = con.execute(
        """SELECT e.event_record_id, e.source_schema, e.source_table, e.source_row_id,
                  e.site_id, e.location_code, e.event_time, e.status, e.description,
                  c.source_key_type, c.source_key, c.kks_code, c.candidate_asset_number,
                  c.score, c.match_method, c.evidence_json
             FROM device_event e
             LEFT JOIN device_match_candidate c
               ON c.source_schema=e.source_schema AND c.source_table_group='inspection'
              AND c.source_table=e.source_table AND c.source_row_id=e.source_row_id
            WHERE e.event_type='inspection' AND e.link_status='blocked'
            ORDER BY e.source_schema, e.source_table, e.source_row_id"""
    ).fetchall()

    needed_locations = {
        (clean(row["source_schema"]), clean(row["site_id"]), clean(row["location_code"]))
        for row in blocked
        if clean(row["site_id"]) and clean(row["location_code"])
    }
    raw_location_rows: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    raw_parents: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for path in sorted(context_root.glob("*_saas__context__lochierarchy.csv")):
        schema = path.name.split("__", 1)[0].upper()
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                row = {str(k).upper(): clean(v) for k, v in raw.items()}
                key = (schema, row.get("SITEID", ""), row.get("LOCATION", ""))
                if key in needed_locations:
                    parent = row.get("PARENT", "")
                    if parent:
                        raw_parents[key].add(parent)
    parent_locations = {
        (schema, site, parent)
        for (schema, site, _), parents in raw_parents.items()
        for parent in parents
    }
    for path in sorted(context_root.glob("*_saas__context__locations.csv")):
        schema = path.name.split("__", 1)[0].upper()
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                row = {str(k).upper(): clean(v) for k, v in raw.items()}
                key = (schema, row.get("SITEID", ""), row.get("LOCATION", ""))
                if key in needed_locations or key in parent_locations:
                    raw_location_rows[key].append(row)

    prefix_asset_candidates: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    assets_by_scope: dict[tuple[str, str], list[tuple[str, list[dict[str, str]]]]] = defaultdict(list)
    for (asset_schema, asset_site, asset_location), asset_rows in assets_by_location.items():
        assets_by_scope[(asset_schema, asset_site)].append((asset_location, asset_rows))
    for schema, site, location in needed_locations:
        if not location:
            continue
        matches: list[dict[str, str]] = []
        for asset_location, asset_rows in assets_by_scope.get((schema, site), []):
            if asset_location.startswith(location):
                matches.extend(asset_rows)
        prefix_asset_candidates[(schema, site, location)] = matches

    report_rows: list[dict[str, object]] = []
    summary = Counter()
    for source in blocked:
        item = dict(source)
        schema = clean(item["source_schema"])
        table = clean(item["source_table"])
        file_name = f"{schema.lower()}__inspection__{table.lower()}.csv"
        old = old_by_file.get(file_name, [])
        fresh = fresh_by_file.get(file_name, [])
        source_row_id = clean(item["source_row_id"])
        ordinal_match = re.fullmatch(r"ROW:(\d+)", source_row_id)
        old_row = old[int(ordinal_match.group(1)) - 1] if ordinal_match and int(ordinal_match.group(1)) <= len(old) else None
        fresh_row = fresh[int(ordinal_match.group(1)) - 1] if ordinal_match and int(ordinal_match.group(1)) <= len(fresh) else None
        if source_row_id.startswith("XJJLID:"):
            value = source_row_id.split(":", 1)[1]
            fresh_row = fresh_xjjlid_by_file.get(file_name, {}).get(value)
            old_row = old_xjjlid_by_file.get(file_name, {}).get(value)
        aligned = False
        if old_row and fresh_row:
            aligned = all(clean(old_row.get(field)) == clean(fresh_row.get(field)) for field in ("SITEID", "LOCATION", "CREATEDATE"))
        source_row = fresh_row if aligned else None
        if source_row is None and fresh_row and not old_row:
            source_row = fresh_row
            aligned = True
        site = clean(item.get("site_id"))
        location = clean(item.get("location_code"))
        itemnum = clean((source_row or {}).get("ITEMNUM"))
        direct_assetnum = clean((source_row or {}).get("ASSETNUM")) or clean((source_row or {}).get("STDASSETNUM"))
        location_assets = assets_by_location.get((schema, site, location), []) if site and location else []
        item_assets = assets_by_key.get((schema, site, itemnum), []) if site and itemnum else []
        direct_assets = [a for a in assets_by_location.get((schema, site, location), []) if a["assetnum"] == direct_assetnum] if direct_assetnum else []
        hierarchy = con.execute(
            """SELECT parent_location FROM location_hierarchy
                WHERE source_schema=? AND site_id=? AND location_code=?
                ORDER BY parent_location""",
            (schema, site, location),
        ).fetchall() if site and location else []
        parent_descriptions = []
        for h in hierarchy:
            parent = clean(h["parent_location"])
            parent_rows = con.execute(
                """SELECT location_code, description, parent_location FROM function_location
                    WHERE source_schema=? AND site_id=? AND location_code=?
                    ORDER BY location_code LIMIT 5""",
                (schema, site, parent),
            ).fetchall()
            parent_descriptions.extend(dict(p) for p in parent_rows)
        raw_location = raw_location_rows.get((schema, site, location), [])
        raw_parent_values = sorted(raw_parents.get((schema, site, location), set()))
        raw_parent_rows = [
            row for parent in raw_parent_values
            for row in raw_location_rows.get((schema, site, parent), [])
        ]
        prefix_assets = prefix_asset_candidates.get((schema, site, location), [])
        evidence = {
            "fresh_source_row": source_row or {},
            "old_fresh_row_aligned": aligned,
            "business_record_fields": {key: value for key, value in (source_row or {}).items() if key.endswith("ID") or key.endswith("NUM") or key in {"WONUM", "CHECKNUM", "INVCHECKNUM", "ST_USECURITYCHECKNUM", "XJJLNUM"} if value},
            "direct_asset_number": direct_assetnum,
            "direct_asset_count": len(direct_assets),
            "direct_asset_candidates": direct_assets,
            "itemnum": itemnum,
            "itemnum_asset_count": len(item_assets),
            "itemnum_asset_candidates": item_assets[:20],
            "location_asset_count": len(location_assets),
            "location_asset_candidates": location_assets[:20],
            "location_kks_format_signal": kks_like(location),
            "location_parent_count": len(hierarchy),
            "location_parent_descriptions": parent_descriptions[:20],
            "raw_location_rows": raw_location[:10],
            "raw_parent_locations": raw_parent_values,
            "raw_parent_rows": raw_parent_rows[:20],
            "prefix_asset_count": len(prefix_assets),
            "prefix_asset_candidates": prefix_assets[:20],
        }
        if direct_assets and len(direct_assets) == 1:
            action = "candidate_direct_asset_number_bridge"
        elif item_assets and len(item_assets) == 1:
            action = "candidate_unique_itemnum_bridge_review"
        elif location_assets and len(location_assets) == 1 and len(hierarchy) >= 1 and kks_like(location):
            action = "candidate_unique_kks_location_parent_review"
        elif item_assets:
            action = "blocked_itemnum_not_unique"
        elif location_assets:
            action = "blocked_location_not_unique"
        elif raw_location and raw_parent_values and prefix_assets:
            action = "needs_review_location_scope_not_device"
        elif source_row and any(evidence["business_record_fields"].values()):
            action = "blocked_business_record_without_device_bridge"
        elif location and kks_like(location):
            action = "blocked_kks_not_found_in_asset"
        elif not location:
            action = "blocked_missing_location"
        else:
            action = "blocked_no_device_bridge"
        item.update({"evidence": evidence, "recommended_action": action})
        summary[action] += 1
        report_rows.append(item)
    con.close()

    output = pathlib.Path(args.output).resolve() if args.output else result_root / "inspection-bridge-evidence.json"
    payload = {
        "count": len(report_rows),
        "summary": dict(sorted(summary.items())),
        "asset_location_key_count": len(assets_by_location),
        "asset_itemnum_key_count": len(assets_by_key),
        "policy": {
            "source_write": False,
            "formal_publication": False,
            "record_numbers_are_traceability_only": True,
            "itemnum_requires_unique_site_scoped_asset": True,
            "kks_location_requires_unique_asset_and_parent_context": True,
        },
        "rows": report_rows,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"count": len(report_rows), "summary": dict(sorted(summary.items())), "output": str(output), "read_only": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
