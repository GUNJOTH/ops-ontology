"""Pure rule-agent execution primitives.

The router owns HTTP and persistence orchestration.  This module owns the
small, auditable rule vocabulary and deterministic sampling used by preview
and replay.  Keeping these functions pure makes normal execution, reruns and
dirty-input tests cheap and independent of the workflow database.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from fastapi import HTTPException


def rule_agent_scope_matches(row: Any, scope: dict[str, Any]) -> bool:
    """Apply explicit site/classification/KKS/location scope."""
    sites = scope.get("sites") or scope.get("siteIds") or []
    classifications = scope.get("classifications") or scope.get("classificationIds") or []
    location_parents = scope.get("locationParents") or scope.get("locationParent") or []
    location_codes = scope.get("locationCodes") or scope.get("locationCode") or []
    kks_prefixes = scope.get("kksPrefixes") or scope.get("kksPrefix") or []
    if isinstance(sites, str):
        sites = [sites]
    if isinstance(classifications, str):
        classifications = [classifications]
    if isinstance(location_parents, str):
        location_parents = [location_parents]
    if isinstance(location_codes, str):
        location_codes = [location_codes]
    if isinstance(kks_prefixes, str):
        kks_prefixes = [kks_prefixes]
    site_values = {str(value).strip() for value in sites if str(value).strip()}
    class_values = {str(value).strip() for value in classifications if str(value).strip()}
    parent_values = {str(value).strip() for value in location_parents if str(value).strip()}
    code_values = {str(value).strip() for value in location_codes if str(value).strip()}
    prefix_values = {str(value).strip() for value in kks_prefixes if str(value).strip()}
    location_parent = row["location_parent"] or ""
    location_code = row["location_code"] or ""
    return (
        (not site_values or (row["site_id"] or "") in site_values)
        and (not class_values or (row["classification_description"] or "") in class_values)
        and (not parent_values or location_parent in parent_values)
        and (not code_values or location_code in code_values)
        and (not prefix_values or any(location_code.startswith(prefix) for prefix in prefix_values))
    )


def rule_agent_transform(original: str, operation: str, condition: dict[str, Any], parameters: dict[str, Any]) -> tuple[bool, str, str]:
    """Execute the deliberately small, auditable rule vocabulary."""
    text = original or ""
    contains = condition.get("contains") or condition.get("descriptionContains")
    pattern = condition.get("descriptionPattern")
    prefix = condition.get("prefix")
    suffix = condition.get("suffix")
    if pattern is not None and contains is None and prefix is None and suffix is None:
        return False, text, "unsupported_description_pattern"
    if contains is not None and str(contains) not in text:
        return False, text, "condition_not_matched"
    if prefix is not None and not text.startswith(str(prefix)):
        return False, text, "prefix_not_matched"
    if suffix is not None and not text.endswith(str(suffix)):
        return False, text, "suffix_not_matched"
    normalized_operation = operation.strip().lower()
    if normalized_operation == "replace":
        source = str(parameters.get("from") or parameters.get("source") or "")
        target = str(parameters.get("to") if parameters.get("to") is not None else parameters.get("target") or "")
        if not source:
            raise HTTPException(status_code=409, detail="规则草案缺少 replace.from，不能安全执行")
        if source not in text:
            return False, text, "source_not_matched"
        return True, text.replace(source, target), "replace"
    if normalized_operation == "trim":
        transformed = text.strip()
        return transformed != text, transformed, "trim"
    if normalized_operation == "normalize":
        mode = str(parameters.get("mode") or "legacy").strip().lower()
        if mode == "nfkc":
            transformed = unicodedata.normalize("NFKC", text)
        elif mode == "whitespace":
            transformed = re.sub(r"[\s\u3000]+", " ", text).strip()
        elif mode in {"legacy", "nfkc_whitespace"}:
            transformed = unicodedata.normalize("NFKC", text)
            if parameters.get("whitespace", True):
                transformed = re.sub(r"[\s\u3000]+", " ", transformed).strip()
        else:
            raise HTTPException(status_code=409, detail=f"normalize mode={mode} is not supported")
        return transformed != text, transformed, "normalize"
    if normalized_operation in {"keep_original", "block"}:
        return True, text, normalized_operation
    raise HTTPException(status_code=409, detail=f"规则草案 operation={operation} 不在安全执行器白名单内")


def rule_agent_stratified_sample(rows: list[dict[str, Any]], sample_size: int = 200) -> list[dict[str, Any]]:
    """Select a deterministic, proportional sample across sites."""
    if len(rows) <= sample_size:
        return list(rows)
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, row in enumerate(rows):
        site = str(row.get("SITEID") or "").strip() or "__UNKNOWN__"
        grouped.setdefault(site, []).append((index, row))
    sites = sorted(grouped)
    if len(sites) > sample_size:
        return list(rows[:sample_size])
    population = len(rows)
    raw_quota = {site: sample_size * len(grouped[site]) / population for site in sites}
    quota = {site: min(len(grouped[site]), max(1, int(raw_quota[site]))) for site in sites}
    while sum(quota.values()) < sample_size:
        candidates = [site for site in sites if quota[site] < len(grouped[site])]
        if not candidates:
            break
        site = max(candidates, key=lambda value: (raw_quota[value] - quota[value], -sites.index(value)))
        quota[site] += 1
    while sum(quota.values()) > sample_size:
        candidates = [site for site in sites if quota[site] > 1]
        if not candidates:
            break
        site = max(candidates, key=lambda value: (quota[value] - raw_quota[value], -sites.index(value)))
        quota[site] -= 1
    selected: list[tuple[int, dict[str, Any]]] = []
    for site in sites:
        group = grouped[site]
        target = quota[site]
        if target >= len(group):
            selected.extend(group)
        elif target == 1:
            selected.append(group[len(group) // 2])
        else:
            indices = [round(index * (len(group) - 1) / (target - 1)) for index in range(target)]
            selected.extend(group[index] for index in indices)
    selected.sort(key=lambda item: item[0])
    return [row for _, row in selected[:sample_size]]
