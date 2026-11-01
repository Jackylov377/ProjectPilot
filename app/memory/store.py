"""Explicit candidates and transactional fact replacement using standard-library SQLite."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.observability.trace import JsonlTrace


MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
    mode TEXT NOT NULL, engine TEXT NOT NULL,
    kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('candidate','active','superseded','revoked','duplicate')),
    revision INTEGER NOT NULL, base_revision INTEGER NOT NULL,
    session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
    message_id TEXT NOT NULL, quote TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    expires_at TEXT, supersedes_id TEXT, duplicate_of_id TEXT,
    UNIQUE(owner_id, workspace_id, mode, engine, session_id, turn_id, key)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_memory
    ON memories(owner_id, workspace_id, mode, engine, key)
    WHERE status = 'active';
"""

def _timestamp(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("Memory timestamps must include a timezone")
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class MemoryScope:
    owner_id: str
    workspace_id: str
    mode: str
    engine: str

    @classmethod
    def for_workspace(cls, workspace: Path, *, mode="offline", engine="session", owner_id="local"):
        identity = os.path.normcase(str(workspace.resolve()))
        return cls(owner_id, hashlib.sha256(identity.encode()).hexdigest(), mode, engine)

    @classmethod
    def for_project(cls, workspace_id: str, project_id: str, *, mode="offline", owner_id="local"):
        if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) for value in (workspace_id, project_id)):
            raise ValueError("Stable workspace/project identities are required")
        return cls(owner_id, f"project:{workspace_id}:{project_id}", mode, "project")

    def __post_init__(self):
        if not self.owner_id.strip() or not self.workspace_id.strip():
            raise ValueError("Memory owner and workspace must be nonempty")
        if self.mode not in {"offline", "live"} or self.engine not in {
            "session",
            "repository",
            "knowledge",
            "project",
        }:
            raise ValueError("Unknown memory mode or engine")


@dataclass(frozen=True)
class Source:
    session_id: str
    turn_id: str
    message_id: str
    quote: str

    def __post_init__(self):
        if any(not value.strip() for value in asdict(self).values()):
            raise ValueError("Memory source requires session, turn, message locator and quote")


@dataclass(frozen=True)
class Memory:
    memory_id: str
    kind: str
    key: str
    value: str
    status: str
    revision: int
    base_revision: int
    source: Source
    created_at: str
    updated_at: str
    expires_at: str | None
    supersedes_id: str | None
    duplicate_of_id: str | None


class MemoryConflict(ValueError):
    """A candidate was staged before another accepted change or withdrawal."""


class MemoryStore:
    """One trusted scope per instance; session_id is provenance, not a recall boundary.

    This layer accepts explicit structured facts. It does not infer whether a quote
    semantically supports a fact; extraction and source verification belong to the caller.
    """

    def __init__(self, path: Path, scope: MemoryScope, trace: JsonlTrace | None = None):
        self.path, self.scope, self.trace = path.resolve(), scope, trace
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript(MEMORY_SCHEMA)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @property
    def _scope_values(self):
        return tuple(asdict(self.scope).values())

    _WHERE = "owner_id=? AND workspace_id=? AND mode=? AND engine=?"

    def _get(self, db, memory_id):
        row = db.execute(
            f"SELECT * FROM memories WHERE {self._WHERE} AND memory_id=?",
            (*self._scope_values, memory_id),
        ).fetchone()
        if row is None:
            raise KeyError("Memory not found in this scope")
        return row

    @staticmethod
    def _record(row):
        data = dict(row)
        source = Source(**{name: data.pop(name) for name in Source.__dataclass_fields__})
        for name in MemoryScope.__dataclass_fields__:
            data.pop(name)
        return Memory(**data, source=source)

    def _revision(self, db, key):
        return db.execute(
            f"SELECT COALESCE(MAX(revision),0) FROM memories WHERE {self._WHERE} AND key=?",
            (*self._scope_values, key),
        ).fetchone()[0]

    def _emit(self, event, memory, **details):
        if self.trace:
            self.trace.emit(event, scope=asdict(self.scope), memory=asdict(memory), **details)

    def get(self, memory_id: str) -> Memory:
        with self._connection() as db:
            return self._record(self._get(db, memory_id))

    def propose(
        self,
        *,
        key: str,
        value: str,
        source: Source,
        kind="decision",
        expires_at: datetime | None = None,
    ) -> Memory:
        if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", key):
            raise ValueError("Use a canonical dotted key, e.g. project.model")
        if kind not in {"decision", "preference", "fact"} or not value.strip():
            raise ValueError("Memory requires an allowed kind and nonempty value")
        now = _timestamp()
        expiry = _timestamp(expires_at) if expires_at else None
        if expiry is not None and expiry <= now:
            raise ValueError("A new candidate must not already be expired")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                f"SELECT * FROM memories WHERE {self._WHERE} AND session_id=? AND turn_id=? AND key=?",
                (*self._scope_values, source.session_id, source.turn_id, key),
            ).fetchone()
            if existing:
                memory = self._record(existing)
                if (memory.kind, memory.value, memory.source, memory.expires_at) != (
                    kind,
                    value,
                    source,
                    expiry,
                ):
                    raise ValueError("The same source turn/key cannot propose different content")
                return memory
            data = {
                "memory_id": uuid4().hex,
                **asdict(self.scope),
                "kind": kind,
                "key": key,
                "value": value,
                "status": "candidate",
                "revision": 0,
                "base_revision": self._revision(db, key),
                **asdict(source),
                "created_at": now,
                "updated_at": now,
                "expires_at": expiry,
                "supersedes_id": None,
                "duplicate_of_id": None,
            }
            db.execute(
                f"INSERT INTO memories ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",
                tuple(data.values()),
            )
            memory = self._record(self._get(db, data["memory_id"]))
        self._emit("memory.proposed", memory)
        return memory

    def accept(self, memory_id: str) -> Memory:
        now = _timestamp()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            candidate = self._get(db, memory_id)
            if candidate["status"] == "duplicate":
                return self._record(self._get(db, candidate["duplicate_of_id"]))
            if candidate["status"] in {"active", "superseded"}:
                return self._record(candidate)  # Retrying acceptance never reactivates an old fact.
            if candidate["status"] != "candidate":
                raise ValueError("Only pending candidates can be accepted")
            if candidate["expires_at"] is not None and candidate["expires_at"] <= now:
                raise ValueError("Candidate expired before acceptance")
            revision = self._revision(db, candidate["key"])
            if candidate["base_revision"] != revision:
                raise MemoryConflict("Decision changed since proposal; review a new candidate")
            current = db.execute(
                f"SELECT * FROM memories WHERE {self._WHERE} AND key=? AND status='active'",
                (*self._scope_values, candidate["key"]),
            ).fetchone()
            if current and (current["kind"], current["value"], current["expires_at"]) == (
                candidate["kind"],
                candidate["value"],
                candidate["expires_at"],
            ):
                db.execute(
                    "UPDATE memories SET status='duplicate', duplicate_of_id=?, updated_at=? WHERE memory_id=?",
                    (current["memory_id"], now, memory_id),
                )
                accepted = self._record(current)
                action = "deduplicated"
            else:
                if current:
                    db.execute(
                        "UPDATE memories SET status='superseded', updated_at=? WHERE memory_id=?",
                        (now, current["memory_id"]),
                    )
                db.execute(
                    "UPDATE memories SET status='active', revision=?, supersedes_id=?, updated_at=? WHERE memory_id=?",
                    (revision + 1, current["memory_id"] if current else None, now, memory_id),
                )
                accepted = self._record(self._get(db, memory_id))
                action = "accepted"
        self._emit("memory.updated", accepted, action=action, candidate_id=memory_id)
        return accepted

    def revoke(self, memory_id: str) -> Memory:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            memory = self._get(db, memory_id)
            if memory["status"] == "revoked":
                return self._record(memory)
            if memory["status"] not in {"candidate", "active"}:
                raise ValueError("Only pending or active memories can be revoked")
            revision = self._revision(db, memory["key"]) + 1
            db.execute(
                "UPDATE memories SET status='revoked', revision=?, updated_at=? WHERE memory_id=?",
                (revision, _timestamp(), memory_id),
            )
            result = self._record(self._get(db, memory_id))
        self._emit("memory.revoked", result)
        return result

    def list(self, *, include_history=False, key: str | None = None, now: datetime | None = None):
        query = f"SELECT * FROM memories WHERE {self._WHERE}"
        args = list(self._scope_values)
        if not include_history:
            query += " AND status='active' AND (expires_at IS NULL OR expires_at>?)"
            args.append(_timestamp(now))
        if key is not None:
            query += " AND key=?"
            args.append(key)
        with self._connection() as db:
            return [
                self._record(row)
                for row in db.execute(query + " ORDER BY key, created_at, memory_id", args)
            ]

    def recall(self, key: str | None = None, *, now: datetime | None = None) -> list[Memory]:
        """Exact-key recall only; query ranking and token budgets are added at Agent integration."""
        records = self.list(key=key, now=now)
        if self.trace:
            self.trace.emit(
                "memory.recalled",
                scope=asdict(self.scope),
                key=key,
                memories=[{"memory_id": m.memory_id, "revision": m.revision} for m in records],
                selection="active_unexpired_exact_key" if key else "active_unexpired",
            )
        return records
