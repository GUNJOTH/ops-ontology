"""Compatibility adapter for legacy top-level system commands.

The adapter gives an existing command a shared run manifest and safe-result
envelope while its domain implementation is migrated separately. It is meant
for low-risk verification and analysis commands first, not as a replacement
for explicit publication workflows.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from .entrypoint import PipelineStepError, add_pipeline_arguments, run_single_step


def run_legacy_main(
    *,
    pipeline_id: str,
    pipeline_version: str,
    root: Path,
    legacy_main: Callable[[], Any],
    argv: list[str] | None = None,
) -> int:
    """Run a legacy command under the shared PipelineContext contract."""
    parser = argparse.ArgumentParser(add_help=False)
    add_pipeline_arguments(parser)
    parsed, remaining = parser.parse_known_args(argv)
    original_argv = sys.argv[:]
    sys.argv = [original_argv[0], *remaining]

    def handler(_context: Any, _dependencies: Mapping[str, Any]) -> dict[str, Any]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = legacy_main()
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise PipelineStepError(
                    f"legacy command exited with code {exc.code}",
                    {"stdoutTail": stdout.getvalue()[-4000:], "stderrTail": stderr.getvalue()[-4000:]},
                ) from exc
            result = None
        if isinstance(result, int):
            if result != 0:
                raise PipelineStepError(
                    f"legacy command returned code {result}",
                    {"stdoutTail": stdout.getvalue()[-4000:], "stderrTail": stderr.getvalue()[-4000:]},
                )
            result = None
        payload = dict(result) if isinstance(result, Mapping) else {"status": "completed"}
        payload.setdefault("status", "completed")
        payload["legacyStdoutTail"] = stdout.getvalue()[-4000:]
        payload["legacyStderrTail"] = stderr.getvalue()[-4000:]
        payload["sourceWrite"] = False
        payload["formalPublication"] = False
        payload["legacyAdapter"] = True
        return payload

    try:
        result = run_single_step(
            pipeline_id=pipeline_id,
            pipeline_version=pipeline_version,
            step_id="legacy_command",
            root=root,
            parameters={"argv": remaining, "command": pipeline_id},
            handler=handler,
            manifest_path=parsed.pipeline_manifest,
            resume_manifest_path=parsed.resume_manifest,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "pipelineId": pipeline_id, "error": str(exc), "sourceWrite": False, "formalPublication": False}, ensure_ascii=False, indent=2))
        return 1
    finally:
        sys.argv = original_argv
