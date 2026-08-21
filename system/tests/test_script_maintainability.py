"""Regression tests for shared script helpers and pure publish/replay/verify functions.

These tests intentionally avoid project databases and preview artifacts.  They
cover the pure logic that was historically duplicated across one-off scripts so
refactors can keep the behavior stable.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path

import cli
import common
import execute_next_deterministic_batch
import publish_ai_cluster_preview
import publish_format_preview
import replay_approve_publish_nfkc_safe_batch
import verify_release_gates
import verify_standard_semantic_ci


def _tmp_path() -> Path:
    root = Path(__file__).resolve().parent / ".test-tmp" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=False)
    return root


def test_sha256_file_and_bytes_are_stable() -> None:
    tmp = _tmp_path()
    try:
        path = tmp / "sample.txt"
        path.write_bytes(b"abc")
        assert common.sha256_file(path) == common.sha256_bytes(b"abc")
        assert common.sha256_file(path) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sid_is_deterministic() -> None:
    assert common.sid("TEST", "a", "b") == common.sid("TEST", "a", "b")
    assert common.sid("TEST", "a", "b") != common.sid("TEST", "a", "c")
    assert common.sid("TEST", None, "x").startswith("TEST-")


def test_utc_now_is_iso() -> None:
    value = common.utc_now()
    assert "T" in value
    assert value.endswith("+00:00") or value.endswith("Z")


def test_backup_before_publish_creates_manifest_and_sidecars() -> None:
    tmp = _tmp_path()
    try:
        db_path = tmp / "work.sqlite3"
        connection = sqlite3.connect(db_path)
        connection.execute("CREATE TABLE sample(value INTEGER)")
        connection.commit()
        sidecar = tmp / "analytics.duckdb"
        sidecar.write_bytes(b"sidecar")
        backup_dir, files = common.backup_before_publish(
            connection,
            "test-publication",
            "20260101T000000Z",
            (sidecar,),
            tmp,
        )
        try:
            assert backup_dir.name == "test-publication-pre-20260101T000000Z"
            assert (backup_dir / "semantic_workflow.sqlite3").exists()
            assert (backup_dir / "analytics.duckdb").exists()
            manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
            assert manifest["source_write"] is False
            assert manifest["formal_publication"] is False
            assert len(files) == 2
        finally:
            connection.close()
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_execute_next_deterministic_batch_transform_and_rule_keys() -> None:
    original = " （ 测试 ／ 内容 － ） "
    transformed = execute_next_deterministic_batch.transform(original)
    assert transformed == "( 测试 / 内容 - )"
    assert execute_next_deterministic_batch.rule_keys_for(original) == [
        "format.trim_description_space",
        "format.fullwidth_solidus_to_ascii",
        "format.fullwidth_hyphen_minus_to_ascii",
    ]


def test_publish_ai_cluster_preview_transforms() -> None:
    assert publish_ai_cluster_preview.width_transform("１２３＃") == "123#"
    assert publish_ai_cluster_preview.new_rules_transform("１２３") == "123"
    assert publish_ai_cluster_preview.new_rules_transform("ABC:") == "ABC"
    assert publish_ai_cluster_preview.new_rules_transform("ABC\u00b7") == "ABC"
    assert publish_ai_cluster_preview.replay_transform("（１２３）／Ｂ－") == "(123)/Ｂ-"


def test_replay_approve_publish_nfkc_safe_batch_append_reason() -> None:
    assert replay_approve_publish_nfkc_safe_batch.append_reason("", "r1") == '["r1"]'
    assert replay_approve_publish_nfkc_safe_batch.append_reason('["r1"]', "r1") == '["r1"]'
    assert replay_approve_publish_nfkc_safe_batch.append_reason('["r1"]', "r2") == '["r1", "r2"]'


def test_publish_format_preview_chunked() -> None:
    chunks = list(publish_format_preview.chunked(["a", "b", "c", "d"], size=2))
    assert chunks == [["a", "b"], ["c", "d"]]


def test_verify_standard_semantic_ci_load_json() -> None:
    tmp = _tmp_path()
    try:
        path = tmp / "manifest.json"
        path.write_text('{"ok": true}', encoding="utf-8")
        assert verify_standard_semantic_ci.load_json(path) == {"ok": True}
        bad = tmp / "bad.json"
        bad.write_text("[1, 2]", encoding="utf-8")
        try:
            verify_standard_semantic_ci.load_json(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("load_json should reject non-object JSON")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_verify_release_gates_run_gate_parses_json(monkeypatch) -> None:
    class FakeCompleted:
        returncode = 0
        stdout = '{"status": "PASS", "sourceWrite": false}'
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: FakeCompleted())
    result = verify_release_gates.run_gate("fake_gate.py")
    assert result == {"status": "PASS", "sourceWrite": False}


def test_cli_exposes_all_script_families() -> None:
    assert cli.PREFIXES
    assert "build" in cli.PREFIXES
    assert "pipeline" not in cli.PREFIXES
    for prefix in cli.PREFIXES:
        assert cli._available_scripts(prefix), f"CLI prefix without scripts: {prefix}"
    assert "build_business_semantics_layer.py" in cli._available_scripts("build")
    assert "semantic_registry.py" not in cli._all_scripts()
    assert "semantic_namespaces.py" not in cli._all_scripts()
    assert all("__main__" in (cli.ROOT / name).read_text(encoding="utf-8", errors="replace") for name in cli._all_scripts())


