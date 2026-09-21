"""Fast tests for the shared pipeline contract; no project database is needed."""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from pipeline.contracts import PipelineContext, connect_readonly, content_hash, manifest_path, resolve_artifact_path
from pipeline.dag import PipelineRunner, load_spec
from pipeline.entrypoint import run_single_step
from pipeline.legacy import run_legacy_main
from pipeline.run_assets import default_asset_index, load_asset_index, register_run


def _test_root() -> Path:
    root = Path(__file__).resolve().parent / ".test-tmp" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=False)
    return root


def test_dag_is_deterministic_and_records_provenance() -> None:
    tmp_path = _test_root()
    spec = {
        "pipelineId": "test-pipeline",
        "version": "v1",
        "steps": [
            {"id": "source", "handler": "source"},
            {"id": "derived", "handler": "derived", "dependsOn": ["source"]},
        ],
    }
    spec_path = tmp_path / "pipeline.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    context = PipelineContext("test-pipeline", "v1", "run-1", tmp_path, {"sourceWrite": False}, tmp_path / "manifest.json")
    calls: list[str] = []

    def source(_context, _dependencies):
        calls.append("source")
        return {"value": 2, "sourceWrite": False, "formalPublication": False}

    def derived(_context, dependencies):
        calls.append("derived")
        return {"value": dependencies["source"]["value"] * 2, "sourceWrite": False, "formalPublication": False}

    try:
        result = PipelineRunner(load_spec(spec_path), context).run({"source": source, "derived": derived})
        assert result["status"] == "completed"
        assert calls == ["source", "derived"]
        assert result["outputs"]["derived"]["value"] == 4
        assert result["steps"][0]["idempotencyKey"]
        assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))["sourceWrite"] is False
        resumed_context = PipelineContext(
            "test-pipeline", "v1", "run-2", tmp_path,
            {"sourceWrite": False}, tmp_path / "manifest-2.json", result,
        )
        resumed = PipelineRunner(load_spec(spec_path), resumed_context).run({
            "source": lambda _context, _dependencies: (_ for _ in ()).throw(AssertionError("source was rerun")),
            "derived": lambda _context, _dependencies: (_ for _ in ()).throw(AssertionError("derived was rerun")),
        })
        assert all(step["resumed"] for step in resumed["steps"])
        assert resumed["outputs"]["derived"]["value"] == 4
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_content_hash_ignores_run_metadata() -> None:
    left = {"run_id": "one", "created_at": "2026-01-01", "count": 3}
    right = {"run_id": "two", "created_at": "2026-01-02", "count": 3}
    assert content_hash(left) == content_hash(right)


def test_resolve_artifact_path_falls_back_to_run_dir() -> None:
    tmp_path = _test_root()
    try:
        run_dir = tmp_path / "run-1"
        run_dir.mkdir()
        artifact = run_dir / "canonical.reasoning.trig"
        artifact.write_text("{}", encoding="utf-8")
        stale = tmp_path / "elsewhere" / "canonical.reasoning.trig"
        assert resolve_artifact_path(str(stale), run_dir) == artifact
        assert resolve_artifact_path("canonical.reasoning.trig", run_dir) == artifact
        assert resolve_artifact_path(artifact, run_dir) == artifact
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_manifest_path_is_relative_to_run_dir() -> None:
    tmp_path = _test_root()
    try:
        run_dir = tmp_path / "canonical-runs" / "run-1"
        run_dir.mkdir(parents=True)
        artifact = run_dir / "canonical.trig"
        assert manifest_path(artifact, run_dir) == "canonical.trig"
        standard = tmp_path / "standards" / "rdf-dataset.json"
        assert manifest_path(standard, run_dir) == "../../standards/rdf-dataset.json"
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_single_step_entrypoint_records_local_publication_capability() -> None:
    tmp_path = _test_root()
    try:
        result = run_single_step(
            pipeline_id="test-publication",
            pipeline_version="v1",
            step_id="publish",
            root=tmp_path,
            parameters={"targetDb": str(tmp_path / "workflow.sqlite3")},
            handler=lambda _context, _dependencies: {
                "status": "published",
                "source_write": False,
                "formal_publication": True,
            },
            allow_formal_publication=True,
        )
        assert result["status"] == "completed"
        assert result["formalPublication"] is True
        manifest = Path(result["manifestPath"])
        assert manifest.is_file()
        stored = json.loads(manifest.read_text(encoding="utf-8"))
        assert stored["formalPublication"] is True
        assert stored["sourceWrite"] is False
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_single_step_entrypoint_rejects_publication_without_capability() -> None:
    tmp_path = _test_root()
    try:
        try:
            run_single_step(
                pipeline_id="test-publication-blocked",
                pipeline_version="v1",
                step_id="publish",
                root=tmp_path,
                parameters={},
                handler=lambda _context, _dependencies: {
                    "status": "published",
                    "source_write": False,
                    "formal_publication": True,
                },
            )
        except RuntimeError as exc:
            assert "formal_publication" in str(exc)
        else:
            raise AssertionError("formal publication bypassed the pipeline gate")
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)




def test_readonly_connection_cannot_write() -> None:
    tmp_path = _test_root()
    path = tmp_path / "source.sqlite3"
    import sqlite3
    try:
        db = sqlite3.connect(path)
        db.execute("create table sample(value integer)")
        db.commit()
        db.close()
        connection = connect_readonly(path)
        try:
            assert connection.execute("select count(*) from sample").fetchone()[0] == 0
            try:
                connection.execute("insert into sample values (1)")
            except sqlite3.OperationalError:
                pass
            else:
                raise AssertionError("read-only source accepted a write")
        finally:
            connection.close()
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_register_run_writes_shared_asset_index() -> None:
    tmp_path = _test_root()
    try:
        record = register_run(
            tmp_path,
            run_id="run-1",
            asset_type="canonical-rdf-build",
            manifest_path=tmp_path / "canonical-runs" / "run-1" / "manifest.json",
            idempotency_key="pipeline:v1:step:hash",
            inputs={"sourceSnapshotId": "snap-1"},
            outputs={"status": "passed", "sourceWrite": False, "formalPublication": False},
            source_snapshot_id="snap-1",
            replayable=True,
        )
        assert record["runId"] == "run-1"
        index = load_asset_index(default_asset_index(tmp_path))
        assert index["run-1"]["assetType"] == "canonical-rdf-build"
        assert index["run-1"]["safeBoundary"] is True
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_register_run_rejects_unsafe_output() -> None:
    tmp_path = _test_root()
    try:
        try:
            register_run(
                tmp_path,
                run_id="run-unsafe",
                asset_type="test",
                manifest_path=tmp_path / "manifest.json",
                idempotency_key="k",
                inputs={},
                outputs={"sourceWrite": True},
            )
        except ValueError as exc:
            assert "sourceWrite" in str(exc)
        else:
            raise AssertionError("unsafe run was registered")
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_legacy_adapter_preserves_arguments_and_manifest() -> None:
    tmp_path = _test_root()
    seen: list[str] = []
    try:
        result = run_legacy_main(
            pipeline_id="legacy-test",
            pipeline_version="v1",
            root=tmp_path,
            legacy_main=lambda: seen.extend(__import__("sys").argv[1:]) or {"status": "checked"},
            argv=["--pipeline-manifest", str(tmp_path / "manifest.json"), "--sample"],
        )
        assert result == 0
        assert seen == ["--sample"]
        payload = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        assert payload["status"] == "completed"
        assert payload["outputs"]["legacy_command"]["status"] == "checked"
        assert payload["sourceWrite"] is False
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_legacy_adapter_fails_on_nonzero_return() -> None:
    tmp_path = _test_root()
    try:
        assert run_legacy_main(
            pipeline_id="legacy-failure-test",
            pipeline_version="v1",
            root=tmp_path,
            legacy_main=lambda: 1,
        ) == 1
        manifests = list((tmp_path / "reports" / "pipeline-runs").glob("*.json"))
        assert manifests
        assert json.loads(manifests[0].read_text(encoding="utf-8"))["status"] == "failed"
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)
