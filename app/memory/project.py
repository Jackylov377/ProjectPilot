"""Project-only memory file; legacy path scopes are never imported or queried."""

from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from .store import MEMORY_SCHEMA, MemoryStore


class ProjectMemoryStore(MemoryStore):
    def __init__(self, path: Path, scope, trace=None, *, create=True):
        if scope.engine != "project":
            raise ValueError("Expected a project memory scope")
        self.path, self.scope, self.trace = Path(path).resolve(), scope, trace
        if not self.path.exists():
            if not create:
                raise FileNotFoundError(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("xb"):
                pass
            db = sqlite3.connect(self.path)
            try:
                db.executescript("BEGIN IMMEDIATE;\n" + MEMORY_SCHEMA + """
                    CREATE TABLE management_receipts (
                        request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, memory_id TEXT NOT NULL
                    );
                    PRAGMA user_version=1; COMMIT;
                """)
            finally:
                db.close()
        with self._connection() as db:
            db.execute("SELECT memory_id,workspace_id,engine FROM memories LIMIT 0")
            db.execute("SELECT request_id,payload,memory_id FROM management_receipts LIMIT 0")

    def revoke_checked(self, memory_id, *, expected_revision, request_id):
        """Commit withdrawal and its retry receipt in the same memory transaction."""
        from .store import MemoryConflict, _timestamp
        payload = json.dumps(dict(scope=asdict(self.scope), memory_id=memory_id,
                                  expected_revision=expected_revision), sort_keys=True)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            receipt = db.execute("SELECT payload,memory_id FROM management_receipts WHERE request_id=?", (request_id,)).fetchone()
            if receipt:
                if receipt['payload'] != payload:
                    raise MemoryConflict("Withdrawal request changed")
                return self._record(self._get(db, memory_id))
            memory = self._get(db, memory_id)
            if memory['revision'] != expected_revision or memory['status'] not in ('candidate', 'active'):
                raise MemoryConflict("Memory revision or state changed")
            db.execute("UPDATE memories SET status='revoked',revision=?,updated_at=? WHERE memory_id=?",
                       (self._revision(db, memory['key']) + 1, _timestamp(), memory_id))
            db.execute("INSERT INTO management_receipts VALUES(?,?,?)", (request_id, payload, memory_id))
            result = self._record(self._get(db, memory_id))
        self._emit('memory.revoked', result)
        return result

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)
        db.row_factory = sqlite3.Row
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("Unsupported project memory storage version")
            with db:
                yield db
        finally:
            db.close()
