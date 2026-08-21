"""CLI for Canonical RDF Semantic Release, backup, approval and rollback."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.semantic_release import (  # noqa: E402
    approve_release,
    activate_release,
    backup_release,
    prepare_release,
    restore_backup,
    rollback_release,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Canonical RDF Semantic Release control plane")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="为最新已验证 Canonical RDF 生成待审批 Release")
    prepare.add_argument("--verification", type=Path, required=True, help="标准 CI PASS JSON 报告")
    prepare.add_argument("--actor", default="local-user")
    approve = sub.add_parser("approve", help="记录本地生产发布审批")
    approve.add_argument("release_id")
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--receipt", required=True)
    approve.add_argument("--note", default="")
    backup = sub.add_parser("backup", help="备份并校验 Canonical SQLite 与 RDF 标准资产")
    backup.add_argument("release_id")
    activate = sub.add_parser("activate", help="激活已审批且已备份的 Release")
    activate.add_argument("release_id")
    activate.add_argument("--actor", default="local-user")
    rollback = sub.add_parser("rollback", help="切换到已审批且有备份的历史 Release")
    rollback.add_argument("release_id")
    rollback.add_argument("--actor", default="local-user")
    restore = sub.add_parser("restore", help="恢复到新的本地 staging 目录，不覆盖当前运行库")
    restore.add_argument("release_id")
    restore.add_argument("target_dir", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare_release(args.verification.resolve(), args.actor)
    elif args.command == "approve":
        result = approve_release(args.release_id, args.reviewer, args.receipt, args.note)
    elif args.command == "backup":
        result = backup_release(args.release_id)
    elif args.command == "activate":
        result = activate_release(args.release_id, args.actor)
    elif args.command == "rollback":
        result = rollback_release(args.release_id, args.actor)
    else:
        result = restore_backup(args.release_id, args.target_dir.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, FileExistsError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "detail": str(exc), "sourceWrite": False, "formalPublication": False}, ensure_ascii=False, indent=2))
        raise SystemExit(2) from exc
