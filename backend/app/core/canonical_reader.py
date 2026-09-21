"""Read-only adapter for the Canonical RDF Dataset.

The workflow and semantic overlay databases remain the control plane for
mapping, review, replay and approval.  This module is deliberately limited to
reading the latest completed Canonical RDF projection so runtime semantic
contexts do not silently fall back to relational tables as their authority.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any
from urllib.parse import quote, unquote

from .semantic_release import selected_canonical_run_id

EX = "https://semantic.local/ontology/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
PROV_PREFIX = "http://www.w3.org/ns/prov#"
HAS_SUBJECT = EX + "hasSubject"
HAS_FACT = EX + "hasFact"
HAS_CURRENT_STATE = EX + "hasCurrentState"
SKOS_PREF_LABEL = "http://www.w3.org/2004/02/skos/core#prefLabel"


def _local_name(iri: str | None) -> str:
    if not iri:
        return ""
    return unquote(str(iri).rstrip("/").rsplit("/", 1)[-1])


def _pascal_to_snake(value: str) -> str:
    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).lower()


def _path_key(iri: str | None) -> tuple[str, str]:
    parts = [unquote(part) for part in str(iri or "").split("/") if part]
    try:
        index = parts.index("object")
        return (parts[index + 1], "/".join(parts[index + 2:]))
    except (ValueError, IndexError):
        return ("business_object", _local_name(iri))


class CanonicalReader:
    """Bounded reader over one completed canonical projection run."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.run = canonical_run_row(connection)
        if self.run is None:
            raise RuntimeError("Canonical RDF Dataset 尚无已完成投影")
        self.run_id = str(self.run["run_id"])

    def _statements(self, subject_iri: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT statement_id,predicate_iri,object_kind,object_iri,lexical_value,
                   datatype_iri,language_tag,confidence,assertion_status
            FROM canonical_statement INDEXED BY ix_canonical_statement_subject
            WHERE run_id=? AND subject_iri=?
            ORDER BY predicate_iri,statement_id
            """,
            (self.run_id, subject_iri),
        ).fetchall()
        return [dict(row) for row in rows]

    def _provenance(self, statement_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT source_system,source_schema,source_table,source_row_id,
                   source_snapshot_id,activity_type,activity_id,evidence_json
            FROM canonical_provenance
            WHERE statement_id=? ORDER BY created_at LIMIT 1
            """,
            (statement_id,),
        ).fetchone()
        return dict(row) if row else {}

    def _property_map(self, subject_iri: str) -> dict[str, list[dict[str, Any]]]:
        values: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self._statements(subject_iri):
            values[str(row["predicate_iri"])].append(row)
        return values

    @staticmethod
    def _literal(properties: dict[str, list[dict[str, Any]]], predicate: str) -> str | None:
        rows = properties.get(predicate, [])
        return str(rows[0]["lexical_value"]) if rows and rows[0]["lexical_value"] is not None else None

    def _device_subject(self, unified_device_id: str) -> str | None:
        # Full identity projections use a stable IRI derived from the
        # canonical device id.  Resolve by the indexed subject key instead of
        # reverse-searching canonicalKey lexical values across millions of
        # source statements.
        subject_iri = f"{EX}object/device/{quote(str(unified_device_id), safe='-._~')}"
        row = self.connection.execute(
            """
            SELECT subject_iri
            FROM canonical_statement INDEXED BY ix_canonical_statement_subject
            WHERE run_id=?
              AND subject_iri=?
              AND predicate_iri=?
              AND object_iri=?
            LIMIT 1
            """,
            (self.run_id, subject_iri, RDF_TYPE, EX + "Device"),
        ).fetchone()
        return str(row["subject_iri"]) if row else None

    def _event_subjects(self, device_subject: str) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT subject_iri
            FROM canonical_statement INDEXED BY ix_canonical_statement_object
            WHERE run_id=? AND predicate_iri=? AND object_iri=?
            ORDER BY subject_iri
            """,
            (self.run_id, HAS_SUBJECT, device_subject),
        ).fetchall()
        return [str(row["subject_iri"]) for row in rows]

    def _fact_subjects(self, device_subject: str) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT object_iri
            FROM canonical_statement INDEXED BY ix_canonical_statement_subject
            WHERE run_id=? AND subject_iri=? AND predicate_iri=? AND object_kind='iri'
            ORDER BY object_iri
            """,
            (self.run_id, device_subject, HAS_FACT),
        ).fetchall()
        return [str(row["object_iri"]) for row in rows]

    def _state_subjects(self, device_subject: str) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT object_iri
            FROM canonical_statement INDEXED BY ix_canonical_statement_subject
            WHERE run_id=? AND subject_iri=? AND predicate_iri=? AND object_kind='iri'
            ORDER BY object_iri
            """,
            (self.run_id, device_subject, HAS_CURRENT_STATE),
        ).fetchall()
        return [str(row["object_iri"]) for row in rows]

    def _event(self, event_iri: str, device_id: str) -> dict[str, Any]:
        properties = self._property_map(event_iri)
        types = [
            _local_name(str(row["object_iri"]))
            for row in properties.get(RDF_TYPE, [])
            if _local_name(str(row["object_iri"])) not in {"BusinessEvent", "BusinessObject"}
        ]
        event_type = _pascal_to_snake(types[0] if types else "BusinessEvent")
        evidence = self._provenance((self._statements(event_iri)[0])["statement_id"])
        return {
            "event_id": _local_name(event_iri),
            "event_type": event_type,
            "subject_type": "device",
            "subject_key": device_id,
            "occurred_at": self._literal(properties, EX + "occurredAt"),
            "recorded_at": self._literal(properties, EX + "recordedAt"),
            "source_schema": self._literal(properties, EX + "sourceSchema"),
            "source_table": self._literal(properties, EX + "sourceTable"),
            "source_row_id": self._literal(properties, EX + "sourceRecordId"),
            "source_snapshot_id": self._literal(properties, EX + "sourceSnapshotId"),
            "raw_status": self._literal(properties, EX + "rawStatus"),
            "description": self._literal(properties, EX + "description"),
            "payload_json": json.dumps({"canonicalIri": event_iri}, ensure_ascii=False),
            "status": "accepted",
            "confidence": None,
            "canonical_iri": event_iri,
            "provenance": evidence,
        }

    def _fact(self, fact_iri: str, device_id: str) -> dict[str, Any]:
        properties = self._property_map(fact_iri)
        types = {_local_name(str(row["object_iri"])) for row in properties.get(RDF_TYPE, [])}
        status = "derived" if "DerivedFact" in types else "observed"
        rows = self._statements(fact_iri)
        evidence = self._provenance(rows[0]["statement_id"] if rows else "")
        return {
            "fact_id": _local_name(fact_iri),
            "fact_type": self._literal(properties, EX + "factType"),
            "predicate": self._literal(properties, EX + "predicateLabel"),
            "subject_type": "device",
            "subject_key": device_id,
            "value_json": self._literal(properties, EX + "valueJson"),
            "unit": self._literal(properties, EX + "unit"),
            "source_schema": self._literal(properties, EX + "sourceSchema"),
            "source_table": self._literal(properties, EX + "sourceTable"),
            "source_row_id": self._literal(properties, EX + "sourceRecordId"),
            "source_snapshot_id": self._literal(properties, EX + "sourceSnapshotId"),
            "status": status,
            "confidence": None,
            "canonical_iri": fact_iri,
            "provenance": evidence,
        }

    def _state(self, state_iri: str) -> dict[str, Any]:
        properties = self._property_map(state_iri)
        parts = [unquote(part) for part in state_iri.rstrip("/").split("/")]
        state_domain = parts[-2] if len(parts) >= 2 else ""
        state = parts[-1] if parts else ""
        return {
            "state_domain": state_domain,
            "current_state": state,
            "display_name": self._literal(properties, SKOS_PREF_LABEL) or state,
            "status": "current",
            "canonical_iri": state_iri,
        }

    def _relation_rows(self, device_subject: str, device_id: str) -> list[dict[str, Any]]:
        excluded = {RDF_TYPE, HAS_FACT, HAS_CURRENT_STATE, EX + "hasEvent"}
        rows = self.connection.execute(
            """
            SELECT statement_id,predicate_iri,object_kind,object_iri,confidence,assertion_status
            FROM canonical_statement INDEXED BY ix_canonical_statement_subject
            WHERE run_id=? AND subject_iri=? AND object_kind='iri'
            ORDER BY predicate_iri,statement_id
            """,
            (self.run_id, device_subject),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            predicate = str(row["predicate_iri"])
            if predicate in excluded or predicate.startswith(PROV_PREFIX):
                continue
            object_type, object_key = _path_key(row["object_iri"])
            result.append({
                "relation_id": f"RDF-{row['statement_id']}",
                "subject_type": "device",
                "subject_key": device_id,
                "predicate": _local_name(predicate),
                "object_type": object_type,
                "object_key": object_key,
                "object_iri": row["object_iri"],
                "status": row["assertion_status"] or "accepted",
                "confidence": row["confidence"],
                "source_schema": None,
                "source_table": None,
                "source_row_id": None,
                "source_snapshot_id": None,
                "evidence_json": json.dumps({"canonicalIri": row["object_iri"]}, ensure_ascii=False),
                "canonical_iri": row["object_iri"],
                "provenance": self._provenance(row["statement_id"]),
            })
        return result

    def device_context(self, unified_device_id: str, as_of: str | None = None) -> dict[str, Any] | None:
        subject = self._device_subject(unified_device_id)
        if subject is None:
            return None
        events = [self._event(iri, unified_device_id) for iri in self._event_subjects(subject)]
        if as_of:
            events = [row for row in events if not row.get("occurred_at") or row["occurred_at"] <= as_of]
        events.sort(key=lambda row: (row.get("occurred_at") or "", row["event_id"]), reverse=True)
        facts = [self._fact(iri, unified_device_id) for iri in self._fact_subjects(subject)]
        states = [self._state(iri) for iri in self._state_subjects(subject)]
        return {
            "runId": self.run_id,
            "subjectIri": subject,
            "relations": self._relation_rows(subject, unified_device_id),
            "events": events[:200],
            "facts": facts[:500],
            "states": states,
            "source": "canonical-rdf-dataset",
            "provenanceCoverage": self.run["manifest_json"],
        }


def canonical_run_row(connection: Any) -> Any:
    """Return the active release run, falling back to latest for development."""
    selected = selected_canonical_run_id()
    if selected:
        row = connection.execute(
            "SELECT * FROM canonical_projection_run WHERE run_id=? AND status='completed'",
            (selected,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"当前 Semantic Release 指向不存在或未完成的 Canonical run：{selected}")
        return row
    return connection.execute(
        "SELECT * FROM canonical_projection_run WHERE status='completed' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
