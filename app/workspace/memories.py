"""Project/environment memory management, separate from legacy memory endpoints."""

from dataclasses import asdict
import json
import sqlite3

from app.memory.project import ProjectMemoryStore
from app.memory.store import MemoryScope
from .errors import Problem


class ProjectMemories:
    def __init__(self, service):
        self.service, self.store = service, service.store
        self.path = service.data_dir / "project-memory.sqlite3"

    def scope(self, project_id, environment):
        self.store.get_project(project_id)
        if environment not in ("offline", "live"):
            raise Problem("invalid_environment", "请明确选择记忆的运行环境。", 422)
        return MemoryScope.for_project(self.store.workspace_id, project_id, mode=environment)

    def open(self, project_id, environment):
        scope = self.scope(project_id, environment)
        try:
            return ProjectMemoryStore(self.path, scope, create=False)
        except (OSError, ValueError, sqlite3.Error):
            raise Problem("memory_unavailable", "项目记忆存储不可用，未改用旧范围或关闭记忆。", 503) from None

    def list(self, project_id, environment, *, history=False):
        self.scope(project_id, environment)
        if not self.path.exists():
            return []
        try:
            return [asdict(item) for item in self.open(project_id, environment).list(include_history=history)]
        except sqlite3.Error:
            raise Problem("memory_unavailable", "项目记忆无法读取。", 503) from None

    def revoke(self, project_id, memory_id, *, environment, request_id, expected_revision):
        payload = dict(project_id=project_id, memory_id=memory_id, environment=environment,
                       expected_revision=expected_revision)
        self.scope(project_id, environment)
        with self.service.manager.lock, self.store._connection(write=True) as db:
            prior = self.service._prior(db, "revoke_memory", request_id, payload)
            if prior:
                return json.loads(prior)
            if any(run["status"] == "running" and run["request"]["project_id"] == project_id for run in self.service.runs()):
                raise Problem("project_busy", "请在该项目当前运行结束后修改记忆。")
            if not self.path.exists():
                raise Problem("memory_not_found", "未找到本项目该环境的记忆。", 404)
            store = self.open(project_id, environment)
            try:
                revoked = asdict(store.revoke_checked(memory_id, expected_revision=expected_revision,
                                                      request_id=request_id))
            except KeyError:
                raise Problem("memory_not_found", "未找到本项目该环境的记忆。", 404) from None
            except ValueError:
                raise Problem("revision_conflict", "该记忆已被替代或不允许撤回。") from None
            except sqlite3.Error:
                raise Problem("memory_unavailable", "项目记忆无法写入。", 503) from None
            self.service._remember(db, "revoke_memory", request_id, payload, json.dumps(revoked))
            return revoked
