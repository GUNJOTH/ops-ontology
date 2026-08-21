"""Single command-line entry point for ops-ontology system scripts.

This is the convergence point for the historical top-level scripts.  Existing
scripts remain runnable directly, but new automation should use:

    python system/cli.py build build_semantic_fact_layer --help
    python system/cli.py verify verify_ontology_runtime.py
    python system/cli.py run verify_maintainability.py --strict
    python system/cli.py list
    python system/cli.py pipeline --help
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEPENDENCY_DIR = PROJECT_ROOT / "backend" / ".deps"
if DEPENDENCY_DIR.exists() and str(DEPENDENCY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPENDENCY_DIR))

import click  # noqa: E402 - 依赖目录需在导入第三方 CLI 库前注入

ROOT = Path(__file__).resolve().parent
SCRIPT_PREFIXES = ("acceptance", "activate", "archive", "backfill", "build", "confirm", "create", "diagnose", "enqueue", "execute", "generate", "init", "ontology", "promote", "publish", "replay", "run", "semantic", "update", "verify")

# All top-level script prefixes are exposed as CLI commands.  Keeping this list
# data-driven means adding a new script family does not require a new hand-written
# click command.
PREFIXES = [
    "acceptance",
    "activate",
    "ai",
    "archive",
    "backfill",
    "build",
    "confirm",
    "create",
    "diagnose",
    "enqueue",
    "execute",
    "generate",
    "init",
    "ontology",
    "promote",
    "publish",
    "replay",
    "run",
    "semantic",
    "test",
    "update",
    "verify",
]


def _is_runnable_script(path: Path) -> bool:
    """A CLI script should be executable from the command line.

    Library modules such as semantic_registry.py / semantic_namespaces.py are
    imported by other scripts and should not be exposed as CLI commands.
    """
    if path.name == "cli.py":
        return False
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return 'if __name__ == "__main__":' in content or "if __name__ == '__main__':" in content


def _available_scripts(prefix: str) -> list[str]:
    return sorted(
        path.name
        for path in ROOT.glob(f"{prefix}_*.py")
        if path.is_file() and _is_runnable_script(path)
    )


def _all_scripts() -> list[str]:
    scripts: set[str] = set()
    for prefix in SCRIPT_PREFIXES:
        scripts.update(_available_scripts(prefix))
    return sorted(scripts)


def _run(script: str, args: tuple[str, ...]) -> None:
    subprocess.run([sys.executable, str(ROOT / script), *args], check=True)


def _print_available(prefix: str) -> None:
    scripts = _available_scripts(prefix)
    if not scripts:
        click.echo(f"没有可用的 {prefix}_*.py 脚本。")
        return
    click.echo(f"可用 {prefix} 脚本：")
    for name in scripts:
        click.echo(f"  {name}")


def _doctor_report() -> dict[str, Any]:
    """Run dependency, database, manifest and maintainability health checks."""
    from pipeline.contracts import connect_readonly
    from verify_maintainability import audit_repository

    report: dict[str, Any] = {}

    report["dependencies"] = {
        "ok": DEPENDENCY_DIR.exists(),
        "path": str(DEPENDENCY_DIR),
    }

    databases: dict[str, Any] = {}
    candidates = {
        "workflow_sqlite": ROOT / "data" / "semantic_workflow.sqlite3",
        "unified_semantics_sqlite": ROOT / "data" / "unified_semantics.sqlite3",
        "metadata_semantics_sqlite": PROJECT_ROOT / "pilots" / "metadata" / "results" / "metadata-semantic-v1-20260815T-v2" / "metadata_semantics.sqlite3",
        "duckdb_analytics": ROOT / "data" / "semantic_analytics_v155.duckdb",
    }
    for name, path in candidates.items():
        if not path.exists():
            if name == "metadata_semantics_sqlite":
                databases[name] = {"ok": True, "optional": True, "reason": "missing_optional", "path": str(path)}
            else:
                databases[name] = {"ok": False, "reason": "missing", "path": str(path)}
            continue
        try:
            if path.suffix == ".duckdb":
                databases[name] = {"ok": True, "path": str(path)}
                continue
            connection = connect_readonly(path, timeout=5)
            try:
                connection.execute("SELECT 1").fetchone()
            finally:
                connection.close()
            databases[name] = {"ok": True, "path": str(path)}
        except Exception as exc:  # pragma: no cover - depends on local filesystem
            databases[name] = {"ok": False, "reason": str(exc), "path": str(path)}
    report["databases"] = databases

    manifests = {
        "sparql": PROJECT_ROOT / "sparql" / "manifest.json",
        "semantic_asset": PROJECT_ROOT / "standards" / "semantic-asset-manifest.json",
        "ontology_version": PROJECT_ROOT / "standards" / "ontology-version.json",
        "semantic_closure": ROOT / "pipelines" / "semantic_closure.json",
    }
    report["manifests"] = {
        name: {"ok": path.exists(), "path": str(path)}
        for name, path in manifests.items()
    }

    budget = audit_repository()
    report["maintainability"] = {
        "ok": budget["status"] == "PASS",
        "status": budget["status"],
        "violations": budget["violations"],
    }

    report["ok"] = (
        report["dependencies"]["ok"]
        and all(item["ok"] for item in report["databases"].values())
        and all(item["ok"] for item in report["manifests"].values())
        and report["maintainability"]["ok"]
    )
    return report


@click.group()
def cli() -> None:
    """Semantic engineering system command line."""


def _register_prefix_command(prefix: str) -> None:
    """Register one ``prefix_*.py`` family as a top-level CLI command."""

    @cli.command(name=prefix)
    @click.argument("script", type=click.Choice(_available_scripts(prefix)), required=False)
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    def command(script: str | None, args: tuple[str, ...]) -> None:
        if script is None:
            _print_available(prefix)
            return
        _run(script, args)


for _prefix in PREFIXES:
    _register_prefix_command(_prefix)


@cli.command()
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def pipeline(args: tuple[str, ...]) -> None:
    """Run the declarative semantic closure pipeline."""
    _run("run_semantic_closure.py", args)


@cli.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@click.argument("script", type=click.Choice(_all_scripts()))
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def run(script: str, args: tuple[str, ...]) -> None:
    """Run any registered top-level system script through one safe entrypoint."""
    _run(script, args)


@cli.command(name="list")
def list_scripts() -> None:
    """List all registered top-level system scripts."""
    for script in _all_scripts():
        click.echo(script)


@cli.command(name="doctor")
def doctor() -> None:
    """Run local environment and repository health checks."""
    report = _doctor_report()
    click.echo(f"doctor: {'PASS' if report['ok'] else 'FAIL'}")
    for group in ("dependencies", "databases", "manifests", "maintainability"):
        value = report[group]
        if isinstance(value, dict) and "ok" in value:
            click.echo(f"  {group}: {'ok' if value['ok'] else 'FAIL'}")
        elif isinstance(value, dict):
            for name, item in value.items():
                ok = item.get("ok", False)
                click.echo(f"  {group}.{name}: {'ok' if ok else 'FAIL'}")
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
