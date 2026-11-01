"""Versioned project/task metadata, deliberately independent of chat and memory."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from uuid import uuid4

from .sources import github_identity, local_identity
from .schema import RESOURCE_SCHEMA, SERVICE_SCHEMA

SCHEMA_VERSION = 3


class StorageError(RuntimeError):
    """The metadata file cannot be safely used."""


class UnsupportedVersion(StorageError):
    pass


class NameConflict(ValueError):
    pass


class NotFound(LookupError):
    pass


class InvalidConfiguration(ValueError):
    pass


class ImmutableField(ValueError):
    pass


@dataclass(frozen=True)
class Project:
    project_id: str
    workspace_id: str
    name: str
    project_kind: str
    source_kind: str
    source_key: str
    source_locator: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Task:
    task_id: str
    project_id: str
    name: str
    task_mode: str
    memory_enabled: bool
    context_policy: str
    history_key: str
    created_at: str
    updated_at: str


SCHEMA = """
CREATE TABLE workspace_info (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    workspace_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspace_info(workspace_id),
    name TEXT NOT NULL CHECK(length(trim(name))>0),
    project_kind TEXT NOT NULL CHECK(project_kind IN ('repository','knowledge')),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('github','local','collection')),
    source_key TEXT NOT NULL CHECK(length(source_key)>0), source_locator TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(workspace_id,name),
    CHECK((project_kind='knowledge' AND source_kind='collection') OR
          (project_kind='repository' AND source_kind IN ('github','local')))
);
CREATE INDEX projects_by_source ON projects(source_key);
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
    name TEXT NOT NULL CHECK(length(trim(name))>0),
    task_mode TEXT NOT NULL CHECK(task_mode IN ('repository','knowledge')),
    memory_enabled INTEGER NOT NULL CHECK(memory_enabled IN (0,1)),
    context_policy TEXT NOT NULL CHECK(context_policy IN ('window','full','summary')),
    history_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(project_id,name)
);
CREATE TRIGGER compatible_task BEFORE INSERT ON tasks
WHEN NEW.task_mode='repository' AND
    (SELECT project_kind FROM projects WHERE project_id=NEW.project_id)!='repository'
BEGIN SELECT RAISE(ABORT,'incompatible_task'); END;
CREATE TRIGGER fixed_project BEFORE UPDATE ON projects
WHEN NEW.project_id IS NOT OLD.project_id OR NEW.workspace_id IS NOT OLD.workspace_id OR
    NEW.project_kind IS NOT OLD.project_kind OR NEW.source_kind IS NOT OLD.source_kind OR
    NEW.source_key IS NOT OLD.source_key OR NEW.source_locator IS NOT OLD.source_locator OR
    NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'immutable_field'); END;
CREATE TRIGGER fixed_task BEFORE UPDATE ON tasks
WHEN NEW.task_id IS NOT OLD.task_id OR NEW.project_id IS NOT OLD.project_id OR
    NEW.task_mode IS NOT OLD.task_mode OR NEW.memory_enabled IS NOT OLD.memory_enabled OR
    NEW.context_policy IS NOT OLD.context_policy OR NEW.history_key IS NOT OLD.history_key OR
    NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'immutable_field'); END;
"""


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def checked_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip() or "\x00" in name:
        raise InvalidConfiguration("Name must be nonblank text without NUL characters")
    return name.strip()


class WorkspaceStore:
    """Opening requires an existing, recognized DB. Only initialize creates one.

    Connections are short-lived, so objects do not hold locks between operations.
    Rename is the only supported update; fixed fields are also guarded by triggers.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        with self._connection(write=False) as db:
            self.workspace_id = db.execute(
                "SELECT workspace_id FROM workspace_info WHERE singleton=1"
            ).fetchone()[0]

    @classmethod
    def initialize(cls, path: str | Path):
        path = Path(path).resolve()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Reserve exactly once: never initialize over an existing or corrupt file.
            with path.open("xb"):
                pass
        except OSError as exc:
            raise StorageError("Cannot initialize metadata; target must not exist") from exc
        try:
            db = sqlite3.connect(path)
            try:
                db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + SERVICE_SCHEMA + RESOURCE_SCHEMA)
                db.execute("INSERT INTO workspace_info VALUES(1,?,?)", (uuid4().hex, timestamp()))
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                db.commit()
            finally:
                db.close()
            return cls(path)
        except (sqlite3.Error, StorageError) as exc:
            # Retain a failed initialization for diagnosis; never replace an old DB.
            raise StorageError("Metadata initialization failed; file retained") from exc

    @classmethod
    def upgrade(cls, path: str | Path):
        """Explicit v1/v2 -> v3 upgrade; preserve a SQLite backup before DDL."""
        path = Path(path).resolve()
        db = None
        try:
            db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return cls(path)
            if version not in (1, 2):
                raise UnsupportedVersion("Unsupported workspace metadata version")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StorageError("Corrupt metadata cannot be upgraded")
            for table in ("workspace_info", "projects", "tasks"):
                db.execute(f"SELECT * FROM {table} LIMIT 0")
            backup = path.with_name(path.name + f".v{version}-backup-{uuid4().hex}.sqlite3")
            target = sqlite3.connect(backup)
            try:
                db.backup(target)
            finally:
                target.close()
            additions = (SERVICE_SCHEMA if version == 1 else "") + RESOURCE_SCHEMA
            db.executescript("BEGIN IMMEDIATE;\n" + additions + f"\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;")
            return cls(path)
        except sqlite3.Error as exc:
            raise StorageError("Metadata upgrade failed; original records retained") from exc
        finally:
            if db is not None:
                db.close()

    @contextmanager
    def _connection(self, *, write: bool):
        db = None
        try:
            db = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise UnsupportedVersion("Unsupported workspace metadata version; no changes made")
            info = db.execute("SELECT workspace_id FROM workspace_info WHERE singleton=1").fetchone()
            if not info or not info[0]:
                raise StorageError("Missing workspace identity")
            if write:
                db.execute("BEGIN IMMEDIATE")
            else:
                db.execute("PRAGMA query_only=ON")
            yield db
            if write:
                db.commit()
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "immutable_field" in message:
                raise ImmutableField("Project source and task configuration cannot be changed") from exc
            if "UNIQUE constraint failed" in message and ".name" in message:
                raise NameConflict("Name already exists in this scope") from exc
            raise InvalidConfiguration("Metadata ownership or configuration constraint failed") from exc
        except sqlite3.Error as exc:
            raise StorageError("Cannot read or write workspace metadata") from exc
        finally:
            if db is not None:
                db.close()  # Rolls back uncommitted failed operations.

    @staticmethod
    def _project(db, project_id):
        row = db.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise NotFound("Project not found")
        return Project(**dict(row))

    @staticmethod
    def _task(db, task_id):
        row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFound("Task not found")
        values = dict(row)
        values["memory_enabled"] = bool(values["memory_enabled"])
        return Task(**values)

    def create_project(self, name: str, *, source_kind: str, source_locator: str | None = None) -> Project:
        with self._connection(write=True) as db:
            return self._create_project(db, name, source_kind=source_kind, source_locator=source_locator)

    def _create_project(self, db, name: str, *, source_kind: str, source_locator: str | None = None) -> Project:
        name = checked_name(name)
        if source_kind == "github":
            if not isinstance(source_locator, str):
                raise InvalidConfiguration("GitHub source requires a URL")
            try:
                key, locator = github_identity(source_locator)
            except ValueError as exc:
                raise InvalidConfiguration(str(exc)) from exc
        elif source_kind == "local":
            if not isinstance(source_locator, str) or not source_locator.strip():
                raise InvalidConfiguration("Local source requires an absolute directory path")
            if not Path(source_locator).is_absolute():
                raise InvalidConfiguration("Local source path must be absolute")
            locator = local_identity(source_locator)
            key = "local:" + locator
        elif source_kind == "collection":
            if source_locator is not None:
                raise InvalidConfiguration("A new collection gets its own identity")
            locator = uuid4().hex
            key = "collection:" + locator
        else:
            raise InvalidConfiguration("Unknown source kind")
        now, project_id = timestamp(), uuid4().hex
        kind = "knowledge" if source_kind == "collection" else "repository"
        db.execute("INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?)", (
            project_id, self.workspace_id, name, kind, source_kind, key, locator, now, now,
        ))
        return self._project(db, project_id)

    def get_project(self, project_id: str) -> Project:
        with self._connection(write=False) as db:
            return self._project(db, project_id)

    def list_projects(self, *, source_key: str | None = None) -> list[Project]:
        with self._connection(write=False) as db:
            query, args = "SELECT * FROM projects", ()
            if source_key is not None:
                query += " WHERE source_key=?"
                args = (source_key,)
            return [Project(**dict(row)) for row in db.execute(query + " ORDER BY created_at,project_id", args)]

    def rename_project(self, project_id: str, name: str) -> Project:
        name = checked_name(name)
        with self._connection(write=True) as db:
            self._project(db, project_id)
            db.execute("UPDATE projects SET name=?,updated_at=? WHERE project_id=?", (name, timestamp(), project_id))
            return self._project(db, project_id)

    def create_task(self, project_id: str, name: str, *, task_mode: str,
                    memory_enabled: bool, context_policy: str) -> Task:
        with self._connection(write=True) as db:
            return self._create_task(db, project_id, name, task_mode=task_mode,
                                     memory_enabled=memory_enabled, context_policy=context_policy)

    def _create_task(self, db, project_id: str, name: str, *, task_mode: str,
                     memory_enabled: bool, context_policy: str) -> Task:
        name = checked_name(name)
        if task_mode not in ("repository", "knowledge") or context_policy not in ("window", "full", "summary"):
            raise InvalidConfiguration("Task mode and context policy must be explicit supported choices")
        if type(memory_enabled) is not bool:
            raise InvalidConfiguration("Memory choice must be an explicit boolean")
        task_id, now = uuid4().hex, timestamp()
        project = self._project(db, project_id)
        if task_mode == "repository" and project.project_kind != "repository":
            raise InvalidConfiguration("Repository analysis requires a repository project")
        db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)", (
            task_id, project_id, name, task_mode, int(memory_enabled), context_policy,
            "projectpilot:" + task_id, now, now,
        ))
        if task_mode == "knowledge":
            db.execute("INSERT INTO task_knowledge VALUES(?,?,'bm25',0)", (task_id, project_id))
        return self._task(db, task_id)

    def get_task(self, task_id: str) -> Task:
        with self._connection(write=False) as db:
            return self._task(db, task_id)

    def list_tasks(self, project_id: str) -> list[Task]:
        with self._connection(write=False) as db:
            self._project(db, project_id)
            ids = db.execute("SELECT task_id FROM tasks WHERE project_id=? ORDER BY created_at,task_id", (project_id,))
            return [self._task(db, row[0]) for row in ids.fetchall()]

    def rename_task(self, task_id: str, name: str) -> Task:
        name = checked_name(name)
        with self._connection(write=True) as db:
            self._task(db, task_id)
            db.execute("UPDATE tasks SET name=?,updated_at=? WHERE task_id=?", (name, timestamp(), task_id))
            return self._task(db, task_id)
