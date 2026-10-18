"""Explicit local memory administration, without invoking an LLM."""

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from app.memory import MemoryScope, MemoryStore, Source
from app.observability.trace import JsonlTrace


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--db", type=Path, default=Path("runs/state/memory.sqlite3"))
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument(
        "--engine", choices=["session", "repository", "knowledge"], default="session"
    )
    parser.add_argument("--owner", default="local")
    parser.add_argument("--trace-dir", type=Path, default=Path("runs/memory-traces"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("propose").add_argument("--input", type=Path, required=True)
    for name in ("accept", "revoke"):
        commands.add_parser(name).add_argument("memory_id")
    commands.add_parser("recall").add_argument("--key")
    commands.add_parser("list").add_argument("--history", action="store_true")
    args = parser.parse_args()
    scope = MemoryScope.for_workspace(
        args.workspace, mode=args.mode, engine=args.engine, owner_id=args.owner
    )
    store = MemoryStore(args.db, scope, JsonlTrace(args.trace_dir))
    try:
        if args.command == "propose":
            data = json.loads(args.input.read_text(encoding="utf-8-sig"))
            data["source"] = Source(**data["source"])
            if data.get("expires_at"):
                data["expires_at"] = datetime.fromisoformat(data["expires_at"])
            result = store.propose(**data)
        elif args.command in {"accept", "revoke"}:
            result = getattr(store, args.command)(args.memory_id)
        elif args.command == "recall":
            result = store.recall(args.key)
        else:
            result = store.list(include_history=args.history)
    except (ValueError, KeyError, OSError, TypeError) as exc:
        parser.exit(1, f"Memory operation failed: {exc}\n")
    output = [asdict(item) for item in result] if isinstance(result, list) else asdict(result)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
