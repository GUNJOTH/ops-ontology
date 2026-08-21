"""Refresh the deterministic cross-plant sample without rebuilding source seeds."""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone

from build_identity_result_layer import export_sample, query_counts, select_samples


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: refresh_identity_sample.py RESULT_DIR")
    root = pathlib.Path(sys.argv[1]).resolve()
    db_path = root / "identity_semantics.sqlite3"
    manifest_path = root / "manifest.json"
    connection = sqlite3.connect(db_path)
    count = select_samples(connection, target=200)
    export_sample(connection, root / "identity_sample_200.csv")
    connection.close()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary_connection = sqlite3.connect(db_path)
    manifest["summary"] = query_counts(summary_connection)
    summary_connection.close()
    manifest["summary"]["sample_count"] = count
    manifest["sample_refresh_at"] = datetime.now(timezone.utc).isoformat()
    manifest["sample_policy"] = "prefer nonblank SITEID strata; blank SITEID only fallback"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"sample_count": count, "summary": manifest["summary"], "read_only": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
