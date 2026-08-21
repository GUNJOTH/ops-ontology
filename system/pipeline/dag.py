"""Small declarative DAG runner used by the semantic closure command."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import (
    PipelineContext,
    assert_safe_result,
    content_hash,
    idempotency_key,
    utc_now,
)

Handler = Callable[[PipelineContext, Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class DagStep:
    step_id: str
    handler: str
    depends_on: tuple[str, ...] = ()
    retries: int = 0


def load_spec(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("steps"), list):
        raise ValueError(f"invalid pipeline spec: {path}")
    return payload


def _steps(spec: Mapping[str, Any]) -> list[DagStep]:
    values: list[DagStep] = []
    seen: set[str] = set()
    for item in spec["steps"]:
        if not isinstance(item, Mapping):
            raise ValueError("pipeline step must be an object")
        step_id = str(item.get("id") or "").strip()
        handler = str(item.get("handler") or step_id).strip()
        if not step_id or step_id in seen:
            raise ValueError(f"duplicate or empty pipeline step: {step_id!r}")
        depends = tuple(str(value) for value in item.get("dependsOn", []))
        if int(item.get("retries", 0)) < 0:
            raise ValueError(f"negative retries: {step_id}")
        seen.add(step_id)
        values.append(DagStep(step_id, handler, depends, int(item.get("retries", 0))))
    known = {item.step_id for item in values}
    for item in values:
        missing = sorted(set(item.depends_on) - known)
        if missing:
            raise ValueError(f"missing dependencies for {item.step_id}: {missing}")
    return values


def _topological(steps: list[DagStep]) -> list[DagStep]:
    remaining = {item.step_id: item for item in steps}
    ordered: list[DagStep] = []
    while remaining:
        ready = [item for item in remaining.values() if all(dep not in remaining for dep in item.depends_on)]
        if not ready:
            raise ValueError("pipeline DAG contains a cycle")
        ready.sort(key=lambda item: item.step_id)
        ordered.extend(ready)
        for item in ready:
            remaining.pop(item.step_id)
    return ordered


class PipelineRunner:
    """Execute named handlers according to a JSON DAG and emit one manifest."""

    def __init__(self, spec: Mapping[str, Any], context: PipelineContext):
        self.spec = spec
        self.context = context
        self.steps = _topological(_steps(spec))

    def _resume_record(self, step_id: str, key: str) -> Mapping[str, Any] | None:
        manifest = self.context.resume_manifest or {}
        records = manifest.get("steps", [])
        if not isinstance(records, list):
            return None
        for record in records:
            if isinstance(record, Mapping) and record.get("stepId") == step_id and record.get("status") == "completed" and record.get("idempotencyKey") == key:
                return record
        return None

    def run(self, handlers: Mapping[str, Handler]) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        records: list[dict[str, Any]] = []
        started = utc_now()
        for step in self.steps:
            if step.handler not in handlers:
                raise KeyError(f"pipeline handler not registered: {step.handler}")
            dependencies = {name: outputs[name] for name in step.depends_on}
            key = idempotency_key(
                str(self.spec.get("pipelineId") or self.context.pipeline_id),
                str(self.spec.get("version") or self.context.pipeline_version),
                step.step_id,
                dependencies,
                {
                    **dict(self.context.parameters),
                    "_pipelineSpecHash": content_hash(self.spec),
                },
            )
            resumed = self._resume_record(step.step_id, key)
            if resumed is not None:
                output = resumed.get("output") if isinstance(resumed.get("output"), Mapping) else {}
                outputs[step.step_id] = output
                records.append({**dict(resumed), "resumed": True})
                continue

            step_started = utc_now()
            attempt = 0
            while True:
                attempt += 1
                try:
                    output = dict(handlers[step.handler](self.context, dependencies))
                    assert_safe_result(output)
                    step_finished = utc_now()
                    record = {
                        "stepId": step.step_id,
                        "handler": step.handler,
                        "status": "completed",
                        "attempts": attempt,
                        "idempotencyKey": key,
                        "startedAt": step_started,
                        "finishedAt": step_finished,
                        "outputHash": content_hash(output),
                        "output": output,
                        "resumed": False,
                    }
                    outputs[step.step_id] = output
                    records.append(record)
                    break
                except Exception as exc:
                    if attempt <= step.retries:
                        continue
                    failed = {
                        "stepId": step.step_id,
                        "handler": step.handler,
                        "status": "failed",
                        "attempts": attempt,
                        "idempotencyKey": key,
                        "startedAt": step_started,
                        "finishedAt": utc_now(),
                        "error": str(exc),
                    }
                    records.append(failed)
                    self._write_manifest(records, outputs, started, "failed")
                    raise
            self._write_manifest(records, outputs, started, "running")

        payload = self._write_manifest(records, outputs, started, "completed")
        return payload

    def _write_manifest(self, records: list[dict[str, Any]], outputs: Mapping[str, Any], started: str, status: str) -> dict[str, Any]:
        payload = {
            "pipelineId": str(self.spec.get("pipelineId") or self.context.pipeline_id),
            "pipelineVersion": str(self.spec.get("version") or self.context.pipeline_version),
            "runId": self.context.run_id,
            "status": status,
            "startedAt": started,
            "updatedAt": utc_now(),
            "sourceWrite": False,
            "formalPublication": False,
            "parameters": dict(self.context.parameters),
            "steps": records,
            "outputs": dict(outputs),
        }
        self.context.with_manifest(payload)
        return payload
