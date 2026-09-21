"""Replay a bounded, deterministic OWL 2 RL-compatible rule subset.

The project deliberately uses a small replayable rule set instead of an
opaque reasoner.  Every inferred statement is stored in a separate local
inference graph with the rule-set version and support summary.  The source
semantic database is opened read-only; only the local Canonical RDF store is
updated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from common import DEFAULT_TARGET
from common import utc_now as now
from pipeline.contracts import connect_local, resolve_artifact_path
from pipeline.entrypoint import PipelineStepError, add_pipeline_arguments, run_single_step
from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS
from semantic_namespaces import GRAPH_NAMESPACE, ONTOLOGY_NAMESPACE

ROOT = Path(__file__).resolve().parent
STANDARD_ROOT = ROOT.parent / "standards"
RUN_ROOT = ROOT / "canonical-runs"
INFERENCE_RULESET_VERSION = "owl2rl-replay-subset-v1"
INFERENCE_BASE = f"{GRAPH_NAMESPACE}inference/"
EX_BASE = ONTOLOGY_NAMESPACE


def load_jsonld_context() -> dict[str, object]:
    """Load the versioned project context instead of generating an ad-hoc one."""
    payload = json.loads((STANDARD_ROOT / "context.jsonld").read_text(encoding="utf-8"))
    context = payload.get("@context", payload)
    if not isinstance(context, dict):
        raise ValueError("standards/context.jsonld 必须包含对象形式的 @context")
    return context


def digest(*parts: object) -> str:
    raw = "|".join("" if value is None else str(value) for value in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS canonical_inference_run (
          run_id TEXT PRIMARY KEY,
          input_projection_run_id TEXT NOT NULL,
          ontology_version TEXT NOT NULL,
          rule_set_version TEXT NOT NULL,
          input_statement_count INTEGER NOT NULL,
          inferred_statement_count INTEGER NOT NULL,
          iteration_count INTEGER NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('completed','blocked','failed')),
          manifest_json TEXT NOT NULL,
          source_write INTEGER NOT NULL CHECK(source_write=0),
          formal_publication INTEGER NOT NULL CHECK(formal_publication=0),
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_canonical_inference_run_created
          ON canonical_inference_run(created_at);
        CREATE TABLE IF NOT EXISTS canonical_inferred_statement (
          statement_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          graph_iri TEXT NOT NULL,
          subject_iri TEXT NOT NULL,
          predicate_iri TEXT NOT NULL,
          object_kind TEXT NOT NULL CHECK(object_kind IN ('iri','literal')),
          object_iri TEXT,
          lexical_value TEXT,
          datatype_iri TEXT,
          language_tag TEXT,
          rule_id TEXT NOT NULL,
          support_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(run_id,subject_iri,predicate_iri,object_kind,object_iri,lexical_value,datatype_iri,language_tag)
        );
        CREATE INDEX IF NOT EXISTS ix_canonical_inferred_statement_subject
          ON canonical_inferred_statement(run_id,subject_iri,predicate_iri);
        """
    )


def union_graph(dataset: Dataset) -> Graph:
    graph = Graph()
    for subject, predicate, obj, _context in dataset.quads((None, None, None, None)):
        graph.add((subject, predicate, obj))
    return graph


def closure_pairs(graph: Graph, predicate: URIRef) -> set[tuple[URIRef, URIRef]]:
    pairs = {(s, o) for s, _, o in graph.triples((None, predicate, None)) if isinstance(s, URIRef) and isinstance(o, URIRef)}
    changed = True
    while changed:
        changed = False
        for left, middle in list(pairs):
            for candidate_left, right in list(pairs):
                if candidate_left == middle and (left, right) not in pairs:
                    pairs.add((left, right))
                    changed = True
    return pairs


def apply_rules(base: Graph) -> tuple[set[tuple[object, object, object]], dict[tuple[object, object, object], dict[str, object]], int]:
    """Apply the replayable RL subset until a fixed point is reached."""
    triples = set(base)
    support: dict[tuple[object, object, object], dict[str, object]] = {}
    iterations = 0

    while iterations < 32:
        iterations += 1
        current = Graph()
        for triple in triples:
            current.add(triple)
        additions: set[tuple[object, object, object]] = set()

        subclass = closure_pairs(current, RDFS.subClassOf)
        subproperty = closure_pairs(current, RDFS.subPropertyOf)
        equivalent_classes = {(s, o) for s, _, o in current.triples((None, OWL.equivalentClass, None)) if isinstance(s, URIRef) and isinstance(o, URIRef)}
        equivalent_properties = {(s, o) for s, _, o in current.triples((None, OWL.equivalentProperty, None)) if isinstance(s, URIRef) and isinstance(o, URIRef)}
        subclass |= equivalent_classes | {(o, s) for s, o in equivalent_classes}
        subproperty |= equivalent_properties | {(o, s) for s, o in equivalent_properties}

        for subject, predicate, obj in list(triples):
            if predicate == RDF.type and isinstance(obj, URIRef):
                for parent, child in subclass:
                    if parent == obj:
                        additions.add((subject, RDF.type, child))
            for child_predicate, parent_predicate in subproperty:
                if predicate == child_predicate:
                    additions.add((subject, parent_predicate, obj))

        for subject, predicate, obj in list(triples):
            for domain in current.objects(predicate, RDFS.domain):
                additions.add((subject, RDF.type, domain))
            for range_class in current.objects(predicate, RDFS.range):
                if isinstance(obj, (URIRef,)):
                    additions.add((obj, RDF.type, range_class))

            for inverse in current.objects(predicate, OWL.inverseOf):
                additions.add((obj, inverse, subject))
            for inverse_subject, inverse_object in current.subject_objects(OWL.inverseOf):
                if predicate == inverse_subject:
                    additions.add((obj, inverse_object, subject))

        symmetric = {p for p in current.subjects(RDF.type, OWL.SymmetricProperty) if isinstance(p, URIRef)}
        for subject, predicate, obj in list(triples):
            if predicate in symmetric:
                additions.add((obj, predicate, subject))

        transitive = {p for p in current.subjects(RDF.type, OWL.TransitiveProperty) if isinstance(p, URIRef)}
        for predicate in transitive:
            for left, middle in current.subject_objects(predicate):
                for candidate_middle, right in current.subject_objects(predicate):
                    if middle == candidate_middle:
                        additions.add((left, predicate, right))

        new = additions - triples
        if not new:
            break
        for triple in new:
            support[triple] = {
                "ruleSet": INFERENCE_RULESET_VERSION,
                "iteration": iterations,
                "supportPredicates": [str(triple[1])],
            }
        triples.update(new)

    inferred = triples - set(base)
    return inferred, {triple: support.get(triple, {"ruleSet": INFERENCE_RULESET_VERSION}) for triple in inferred}, iterations


def latest_projection(db: sqlite3.Connection) -> sqlite3.Row:
    row = db.execute("SELECT * FROM canonical_projection_run WHERE status='completed' ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("没有可用于 OWL 2 RL 回放的 Canonical 投影")
    return row


def persist_inference(target: Path = DEFAULT_TARGET) -> dict[str, object]:
    created = now()
    db = connect_local(target, timeout=30)
    ensure_schema(db)
    projection = latest_projection(db)
    existing = db.execute(
        "SELECT manifest_json FROM canonical_inference_run WHERE input_projection_run_id=? AND rule_set_version=? AND status='completed' ORDER BY created_at DESC LIMIT 1",
        (projection["run_id"], INFERENCE_RULESET_VERSION),
    ).fetchone()
    if existing:
        # Keep an idempotent replay from leaving an old ad-hoc JSON-LD context
        # behind after the project context contract changes.
        existing_manifest = json.loads(existing["manifest_json"])
        jsonld_value = existing_manifest.get("artifacts", {}).get("jsonld")
        existing_run_dir = ROOT / "canonical-runs" / str(existing_manifest.get("inputProjectionRunId") or "")
        existing_jsonld = resolve_artifact_path(jsonld_value or "", existing_run_dir) if jsonld_value else None
        if existing_jsonld and existing_jsonld.is_file():
            refreshed = Dataset()
            refreshed.parse(existing_jsonld, format="json-ld")
            refreshed.serialize(
                destination=str(existing_jsonld),
                format="json-ld",
                context=load_jsonld_context(),
                auto_compact=True,
            )
        db.close()
        return existing_manifest
    manifest = json.loads(projection["manifest_json"])
    # Full Canonical RDF includes a streamed source-identity graph with
    # millions of Device seed rows.  Identity seeds are source facts, not
    # relation/state evidence; replay the bounded semantic reasoning graph so
    # OWL 2 RL does not materialise millions of meaningless superclass triples.
    trig_value = manifest["artifacts"].get("reasoningTrig") or manifest["artifacts"]["trig"]
    run_dir = ROOT / "canonical-runs" / str(projection["run_id"])
    trig_path = resolve_artifact_path(trig_value, run_dir)
    if not trig_path.exists():
        raise FileNotFoundError(trig_path)

    dataset = Dataset()
    dataset.parse(trig_path, format="trig")
    base = union_graph(dataset)
    inferred, support, iterations = apply_rules(base)
    run_id = f"owl-rl-replay-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{digest(projection['run_id'], created)[:10]}"
    graph_iri = f"{INFERENCE_BASE}{run_id}"
    run_dir = trig_path.parent
    trig_out = run_dir / "owl_rl_closure.trig"
    jsonld_out = run_dir / "owl_rl_closure.jsonld"
    inference_dataset = Dataset()
    inference_graph = inference_dataset.graph(URIRef(graph_iri))
    for subject, predicate, obj in inferred:
        inference_graph.add((subject, predicate, obj))
    inference_dataset.serialize(destination=str(trig_out), format="trig")
    inference_dataset.serialize(
        destination=str(jsonld_out),
        format="json-ld",
        context=load_jsonld_context(),
        auto_compact=True,
    )

    db.execute("DELETE FROM canonical_inferred_statement WHERE run_id=?", (run_id,))
    for subject, predicate, obj in sorted(inferred, key=lambda item: tuple(str(value) for value in item)):
        kind = "iri" if isinstance(obj, URIRef) else "literal"
        object_iri = str(obj) if kind == "iri" else None
        lexical = None if kind == "iri" else str(obj)
        datatype = str(obj.datatype) if isinstance(obj, Literal) and obj.datatype else None
        language = obj.language if isinstance(obj, Literal) else None
        statement_id = f"inferred-{digest(run_id, subject, predicate, kind, object_iri, lexical, datatype, language)}"
        db.execute(
            """INSERT INTO canonical_inferred_statement(
              statement_id,run_id,graph_iri,subject_iri,predicate_iri,object_kind,object_iri,
              lexical_value,datatype_iri,language_tag,rule_id,support_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (statement_id, run_id, graph_iri, str(subject), str(predicate), kind, object_iri, lexical,
             datatype, language, INFERENCE_RULESET_VERSION, json.dumps(support[(subject, predicate, obj)], ensure_ascii=False, sort_keys=True), created),
        )

    inference_manifest = {
        "schemaVersion": "canonical-inference-manifest-v1",
        "runId": run_id,
        "inputProjectionRunId": projection["run_id"],
        "ontologyVersion": projection["ontology_version"],
        "ruleSetVersion": INFERENCE_RULESET_VERSION,
        "inputStatementCount": len(base),
        "inferredStatementCount": len(inferred),
        "iterationCount": iterations,
        "reasoningScope": "bounded semantic graph; streamed source-identity seed graph excluded from materialisation",
        "sourceWrite": False,
        "formalPublication": False,
        "artifacts": {"trig": trig_out.name, "jsonld": jsonld_out.name},
    }
    db.execute(
        """INSERT INTO canonical_inference_run(
          run_id,input_projection_run_id,ontology_version,rule_set_version,input_statement_count,
          inferred_statement_count,iteration_count,status,manifest_json,source_write,formal_publication,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, projection["run_id"], projection["ontology_version"], INFERENCE_RULESET_VERSION,
         len(base), len(inferred), iterations, "completed", json.dumps(inference_manifest, ensure_ascii=False), 0, 0, created),
    )
    db.commit()
    db.close()
    return inference_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay the bounded OWL 2 RL subset over the latest Canonical RDF Dataset")
    parser.add_argument("--target-db", type=Path, default=DEFAULT_TARGET)
    add_pipeline_arguments(parser)
    args = parser.parse_args()
    try:
        pipeline = run_single_step(
            pipeline_id="owl-rl-replay",
            pipeline_version="owl-rl-replay-v1",
            step_id="owl_rl_replay",
            root=ROOT,
            parameters={"targetDb": str(args.target_db.resolve()), "ruleSetVersion": INFERENCE_RULESET_VERSION},
            handler=lambda _context, _dependencies: _replay_or_fail(args.target_db.resolve()),
            manifest_path=args.pipeline_manifest,
            resume_manifest_path=args.resume_manifest,
        )
    except PipelineStepError as exc:
        print(json.dumps(exc.payload, ensure_ascii=False, indent=2))
        raise SystemExit(1) from exc
    print(json.dumps(pipeline["outputs"]["owl_rl_replay"], ensure_ascii=False, indent=2))


def _replay_or_fail(target: Path) -> dict[str, object]:
    manifest = persist_inference(target)
    if manifest.get("status") != "completed":
        raise PipelineStepError("OWL 2 RL replay did not complete", manifest)
    return manifest


if __name__ == "__main__":
    main()
