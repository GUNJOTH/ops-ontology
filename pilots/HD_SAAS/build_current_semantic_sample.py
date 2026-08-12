"""Build a deterministic stratified sample from the current semantic batch."""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
INPUT = ROOT / "semantic_candidates" / "hd_semantic_candidates.csv"
OUTPUT_DIR = ROOT / "semantic_candidates"
SAMPLE = OUTPUT_DIR / "semantic_review_sample_300.csv"
MANIFEST = OUTPUT_DIR / "semantic_review_sample_manifest.json"
OBSERVATIONS = OUTPUT_DIR / "semantic_rule_observations.csv"
TARGET = 300


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def context_group(row: dict[str, str]) -> str:
    try:
        context = json.loads(row.get("CONTEXT_JSON") or "{}")
    except (json.JSONDecodeError, TypeError):
        context = {}
    location = context.get("location") or {}
    hierarchy = context.get("location_hierarchy") or {}
    classification = context.get("classification") or {}
    parent = clean(row.get("LOCATION_PARENT")) or clean(hierarchy.get("PARENT"))
    class_name = clean(row.get("CLASSIFICATION_DESCRIPTION")) or clean(classification.get("DESCRIPTION"))
    return parent or class_name or "UNCONTEXTUALIZED"


def main() -> None:
    groups: defaultdict[str, list[tuple[str, dict[str, str]]]] = defaultdict(list)
    site_groups: defaultdict[str, defaultdict[str, list[tuple[str, dict[str, str]]]]] = defaultdict(lambda: defaultdict(list))
    site_rows: Counter[str] = Counter()
    site_counts: Counter[str] = Counter()
    group_counts: Counter[str] = Counter()
    input_rows = 0
    with INPUT.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            input_rows += 1
            site = clean(row.get("SITEID"))
            group = context_group(row)
            try:
                context = json.loads(row.get("CONTEXT_JSON") or "{}")
            except (json.JSONDecodeError, TypeError):
                context = {}
            row["LOCATION_CODE"] = clean((context.get("location") or {}).get("LOCATION"))
            stratum = f"{site}|{group}"
            digest = hashlib.sha256((clean(row.get("CANDIDATE_ID")) + "|semantic-review-sample-20260812").encode("utf-8")).hexdigest()
            groups[stratum].append((digest, row))
            site_groups[site][group].append((digest, row))
            site_rows[site] += 1
    for stratum in groups:
        groups[stratum].sort(key=lambda item: item[0])
        group_counts[stratum] = len(groups[stratum])

    for site in site_groups:
        for group in site_groups[site]:
            site_groups[site][group].sort(key=lambda item: item[0])

    sites = sorted(site_rows)
    if input_rows <= TARGET:
        site_targets = dict(site_rows)
    else:
        site_targets = {site: min(30, site_rows[site]) for site in sites}
        remaining = TARGET - sum(site_targets.values())
        while remaining > 0:
            eligible = [site for site in sites if site_targets[site] < site_rows[site]]
            if not eligible:
                break
            # Allocate remaining rows by largest proportional deficit, with
            # deterministic site-name tie breaking.
            site = max(
                eligible,
                key=lambda item: (site_rows[item] / max(site_targets[item], 1), -sites.index(item)),
            )
            site_targets[site] += 1
            remaining -= 1

    selected: list[tuple[str, dict[str, str]]] = []
    selected_site_counts: Counter[str] = Counter()
    for site in sites:
        site_strata = sorted(site_groups[site])
        rank = 0
        while selected_site_counts[site] < site_targets[site]:
            progressed = False
            for group in site_strata:
                items = site_groups[site][group]
                if rank < len(items) and selected_site_counts[site] < site_targets[site]:
                    selected.append((f"{site}|{group}", items[rank][1]))
                    selected_site_counts[site] += 1
                    progressed = True
            if not progressed:
                break
            rank += 1

    fields = [
        "SAMPLE_ID", "STRATUM", "SITEID", "ASSETNUM", "ASSETID", "CANDIDATE_ID",
        "ORIGINAL_DESCRIPTION", "CANDIDATE_DESCRIPTION", "UNIFIED_DESCRIPTION",
        "SEMANTIC_ACTION", "SEMANTIC_RESULT_STATUS", "SEMANTIC_REASON_CODES",
        "LOCATION_CODE", "LOCATION_DESCRIPTION", "LOCATION_PARENT", "LOCATION_STATUS",
        "CLASSSTRUCTURE_DESCRIPTION", "CLASSIFICATION_DESCRIPTION", "SPEC_COUNT",
        "FEATURE_COUNT", "PARENT_ASSET_COUNT", "RELATION_COUNT", "CONTEXT_EVIDENCE_LEVEL",
        "EXPECTED_UNIFIED_DESCRIPTION", "REVIEW_DECISION", "REVIEW_NOTES",
        "SOURCE_ROW_HASH", "CONTEXT_HASH", "SEMANTIC_CANDIDATE_HASH",
    ]
    with SAMPLE.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, (stratum, row) in enumerate(selected, start=1):
            output = {field: clean(row.get(field)) for field in fields}
            output["SAMPLE_ID"] = f"HD-SEMANTIC-SAMPLE-{index:04d}"
            output["STRATUM"] = stratum
            output["EXPECTED_UNIFIED_DESCRIPTION"] = ""
            output["REVIEW_DECISION"] = "pending"
            output["REVIEW_NOTES"] = ""
            writer.writerow(output)
            site_counts[clean(row.get("SITEID"))] += 1

    observation_fields = ["OBSERVATION_KIND", "OBSERVATION_VALUE", "ROW_COUNT", "SAMPLE_COUNT", "NOTE"]
    observations: list[dict[str, str]] = []
    observations.append({
        "OBSERVATION_KIND": "BATCH",
        "OBSERVATION_VALUE": "CURRENT_SEMANTIC_BATCH",
        "ROW_COUNT": str(input_rows),
        "SAMPLE_COUNT": str(len(selected)),
        "NOTE": "候选生成仅保留来源描述，未应用未确认术语规则",
    })
    observations.append({
        "OBSERVATION_KIND": "STRATUM_COUNT",
        "OBSERVATION_VALUE": str(len(groups)),
        "ROW_COUNT": str(len(groups)),
        "SAMPLE_COUNT": str(len(selected)),
        "NOTE": "分层键为SITEID + LOCATION_PARENT或CLASSIFICATION_DESCRIPTION",
    })
    for site, count in site_counts.most_common():
        observations.append({
            "OBSERVATION_KIND": "SAMPLE_SITE",
            "OBSERVATION_VALUE": site,
            "ROW_COUNT": str(sum(len(items) for key, items in groups.items() if key.startswith(site + "|"))),
            "SAMPLE_COUNT": str(count),
            "NOTE": "当前样本覆盖",
        })
    for stratum, count in sorted(group_counts.items(), key=lambda item: (-item[1], item[0]))[:100]:
        sample_count = sum(1 for selected_stratum, _ in selected if selected_stratum == stratum)
        observations.append({
            "OBSERVATION_KIND": "TOP_CONTEXT_GROUP",
            "OBSERVATION_VALUE": stratum,
            "ROW_COUNT": str(count),
            "SAMPLE_COUNT": str(sample_count),
            "NOTE": "仅作为上下文观察，不自动改写",
        })
    with OBSERVATIONS.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=observation_fields)
        writer.writeheader()
        writer.writerows(observations)

    manifest = {
        "sample_run_id": "hd-current-semantic-sample-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "input_file": str(INPUT),
        "input_sha256": hashlib.sha256(INPUT.read_bytes()).hexdigest(),
        "input_rows": input_rows,
        "sample_rows": len(selected),
        "target_rows": TARGET,
        "stratum_definition": "SITEID + LOCATION_PARENT, fallback CLASSIFICATION_DESCRIPTION",
        "site_counts": dict(site_counts),
        "site_targets": site_targets,
        "stratum_count": len(groups),
        "review_decision": "pending",
        "sample_file": str(SAMPLE),
        "observations_file": str(OBSERVATIONS),
        "source_write": False,
        "formal_publication": False,
        "status": "sample_only",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
