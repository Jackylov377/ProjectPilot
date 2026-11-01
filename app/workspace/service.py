"""Application orchestration: durable admission before process startup, never replay."""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from .sources import github_identity, local_identity
from .store import WorkspaceStore, checked_name, timestamp
from .errors import Problem
from .documents import ProjectDocuments
from .memories import ProjectMemories
from app.rag.pipeline import configured_clients


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def task_state_path(data_dir, workspace_id, project_id, task_id, environment):
    if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value)
               for value in (workspace_id, project_id, task_id)) or environment not in ("offline", "live"):
        raise ValueError("Invalid task history identity")
    return Path(data_dir).resolve() / "task-sessions" / workspace_id / project_id / task_id / environment


class WorkspaceService:
    def __init__(self, store: WorkspaceStore, manager, workspace: Path, data_dir: Path, *, live_enabled=False):
        self.store, self.manager = store, manager
        self.workspace, self.data_dir = workspace.resolve(), data_dir.resolve()
        self.live_enabled = live_enabled
        self.documents = ProjectDocuments(self)
        self.memories = ProjectMemories(self)
        # An accepted request with no process receipt is an interrupted attempt, not a queue.
        with self.manager.lock, self.store._connection(write=True) as db:
            for row in db.execute("SELECT run_id FROM task_runs WHERE admission_state='accepted'").fetchall():
                if not (self.manager.directory / row[0] / "job.json").is_file():
                    db.execute("UPDATE task_runs SET admission_state='interrupted' WHERE run_id=?", (row[0],))

    def _prior(self, db, operation, request_id, payload):
        row = db.execute("SELECT fingerprint,result_id FROM request_receipts WHERE operation=? AND request_id=?",
                         (operation, request_id)).fetchone()
        if row and row["fingerprint"] != fingerprint(payload):
            raise Problem("request_conflict", "同一次请求的内容或目标发生变化，请使用新的请求标识。")
        return row["result_id"] if row else None

    def _remember(self, db, operation, request_id, payload, result_id):
        db.execute("INSERT INTO request_receipts VALUES(?,?,?,?,?)",
                   (operation, request_id, fingerprint(payload), result_id, timestamp()))

    def source(self, source_kind, repository_id=None):
        if source_kind == "collection":
            if repository_id is not None:
                raise Problem("invalid_source", "纯资料项目不能绑定仓库。", 422)
            return None
        if source_kind == "local":
            if repository_id not in (None, "startup"):
                raise Problem("invalid_source", "仅支持服务启动时配置的本地仓库。", 422)
            if not self.workspace.is_dir():
                raise Problem("source_unavailable", "配置的本地仓库不可用。")
            locator = local_identity(self.workspace)
            return {"kind": "local", "source_key": "local:" + locator,
                    "source_locator": locator, "workspace": str(self.workspace), "repository": None}
        if source_kind != "github" or not isinstance(repository_id, str) or not re.fullmatch(r"[0-9a-f]{32}", repository_id):
            raise Problem("invalid_source", "请选择当前应用成功导入的 GitHub 仓库。", 422)
        try:
            record = self.manager.get(repository_id)
            info = record.get("result", {}).get("repository", {})
            if not isinstance(info, dict):
                raise ValueError("Invalid import metadata")
            if record["kind"] != "repository_import" or record["status"] != "completed" or info.get("id") != repository_id:
                raise ValueError("Incomplete import")
            path = self.manager.directory / repository_id / "source"
            if (not path.is_dir() or path.is_symlink() or getattr(path, "is_junction", lambda: False)()
                    or not path.resolve().is_relative_to(self.manager.directory.resolve())):
                raise ValueError("Invalid source path")
            if not isinstance(info.get("url"), str):
                raise ValueError("Missing import URL")
            key, url = github_identity(info["url"])
            archive = info.get("archive_sha256")
            if not isinstance(archive, str) or not re.fullmatch(r"[0-9a-f]{64}", archive):
                raise ValueError("Missing archive version")
            return {"kind": "github", "source_key": key, "source_locator": url,
                    "workspace": str(path.resolve()), "repository": repository_id,
                    "archive_sha256": archive, "commit": info.get("commit")}
        except (KeyError, ValueError, TypeError, OSError):
            raise Problem("source_unavailable", "导入尚未完成、来源不可用或缺少版本证据。") from None

    def project(self, project_id):
        result = asdict(self.store.get_project(project_id))
        with self.store._connection(write=False) as db:
            row = db.execute("SELECT snapshot_json FROM project_bindings WHERE project_id=?", (project_id,)).fetchone()
        result["repository_binding"] = json.loads(row[0]) if row else None
        return result

    def task(self, task_id):
        task = self.store.get_task(task_id)
        result = asdict(task)
        with self.store._connection(write=False) as db:
            row = db.execute("SELECT environment FROM task_runtime WHERE task_id=?", (task_id,)).fetchone()
            bound = db.execute("SELECT snapshot_json FROM project_bindings WHERE project_id=?", (task.project_id,)).fetchone()
        reason = None
        if task.task_mode == "knowledge":
            try:
                with self.store._connection(write=False) as db:
                    context = self.documents.manifest(task_id, db)
                self._retriever(context["retriever"], row[0] if row else None)
            except Problem as exc:
                reason = exc.code
        elif not bound:
            reason = "source_binding_required"
        else:
            snapshot = json.loads(bound[0])
            project = self.store.get_project(task.project_id)
            try:
                current = self.source(project.source_kind, snapshot["repository"])
                if current != snapshot or current["source_key"] != project.source_key:
                    reason = "source_mismatch"
            except Problem as exc:
                reason = exc.code
        if reason is None and row and row[0] == "live" and not self.live_enabled:
            reason = "live_unavailable"
        if reason is None and task.memory_enabled and self.memories.path.exists():
            try:
                self.memories.open(task.project_id, row[0] if row else "offline")
            except Problem as exc:
                reason = exc.code
        result.update(run_environment=row[0] if row else None, can_run=reason is None, blocked_reason=reason)
        return result

    def _retriever(self, retriever, environment):
        if retriever != "bm25" and (environment == "offline" or not self.live_enabled):
            raise Problem("retriever_requires_live", "高级检索需要可用的真实运行环境。")
        try:
            configured_clients(retriever, allow_remote=environment != "offline")
        except ValueError:
            raise Problem("retriever_unavailable", "检索服务配置尚未就绪。") from None

    def create_project(self, *, request_id, name, source_kind, repository_id=None, create_independent=False):
        payload = dict(name=checked_name(name), source_kind=source_kind,
                       repository_id=repository_id, create_independent=create_independent)
        with self.manager.lock, self.store._connection(write=True) as db:
            prior = self._prior(db, "create_project", request_id, payload)
            if prior:
                result_id = prior
            else:
                source = self.source(source_kind, repository_id)
                if source and not create_independent and db.execute(
                    "SELECT 1 FROM projects WHERE source_key=?", (source["source_key"],)
                ).fetchone():
                    raise Problem("project_selection_required", "同来源已有项目，请选择已有项目或明确新建独立项目。")
                project = self.store._create_project(db, payload["name"], source_kind=source_kind,
                                                     source_locator=source["source_locator"] if source else None)
                result_id = project.project_id
                if source:
                    db.execute("INSERT INTO project_bindings VALUES(?,?,?)", (result_id, json.dumps(source), timestamp()))
                self._remember(db, "create_project", request_id, payload, result_id)
        return self.project(result_id)

    def create_task(self, project_id, *, request_id, name, task_mode, memory_enabled, context_policy):
        payload = dict(project_id=project_id, name=checked_name(name), task_mode=task_mode,
                       memory_enabled=memory_enabled, context_policy=context_policy)
        with self.store._connection(write=True) as db:
            prior = self._prior(db, "create_task", request_id, payload)
            if prior:
                result_id = prior
            else:
                task = self.store._create_task(db, **payload)
                result_id = task.task_id
                self._remember(db, "create_task", request_id, payload, result_id)
        return self.task(result_id)

    def bind_repository(self, project_id, repository_id):
        with self.manager.lock, self.store._connection(write=True) as db:
            project = self.store._project(db, project_id)
            source = self.source(project.source_kind, repository_id)
            if source is None or source["source_key"] != project.source_key:
                raise Problem("source_mismatch", "只能为项目绑定原有仓库来源的内容。")
            db.execute("INSERT INTO project_bindings VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET snapshot_json=excluded.snapshot_json,updated_at=excluded.updated_at",
                       (project_id, json.dumps(source), timestamp()))
        return self.project(project_id)

    def has_run(self, run_id):
        with self.store._connection(write=False) as db:
            return db.execute("SELECT 1 FROM task_runs WHERE run_id=?", (run_id,)).fetchone() is not None

    def run(self, run_id):
        with self.manager.lock, self.store._connection(write=False) as db:
            row = db.execute("SELECT * FROM task_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise Problem("run_not_found", "未找到任务运行。", 404)
            envelope = json.loads(row["envelope_json"])
            if row["admission_state"] != "interrupted" and (self.manager.directory / run_id / "job.json").is_file():
                job = self.manager.get(run_id)
                request = job.get("request", {})
                if job.get("kind") == "agent" and request == envelope:
                    return job
            return {"id": run_id, "run_id": run_id, "kind": "agent", "status": "interrupted",
                    "created_at": row["created_at"], "timeout": 115, "request": envelope,
                    "error_code": "admission_interrupted", "error": "运行已接收，但执行回执缺失、损坏或启动未完成。",
                    "error_hint": "本次请求不会自动重放；核对已有结果后，如需重试请发送新的请求标识。"}

    def runs(self, task_id=None):
        with self.store._connection(write=False) as db:
            if task_id is not None:
                self.store._task(db, task_id)
            rows = db.execute("SELECT run_id FROM task_runs" + (" WHERE task_id=?" if task_id else "") + " ORDER BY created_at,run_id",
                              (task_id,) if task_id else ()).fetchall()
        return [self.run(row[0]) for row in rows]

    def submit(self, task_id, *, request_id, prompt, environment=None):
        payload = dict(task_id=task_id, prompt=prompt, environment=environment)
        # Same lock as import/evaluation submission. No second request can pass admission
        # between the metadata commit and the process receipt becoming visible.
        with self.manager.lock:
            with self.store._connection(write=True) as db:
                prior = self._prior(db, "send", request_id, payload)
                if prior:
                    run_id = prior
                    replay = True
                else:
                    replay = False
                    task = self.store._task(db, task_id)
                    runtime = db.execute("SELECT environment FROM task_runtime WHERE task_id=?", (task_id,)).fetchone()
                    if runtime:
                        if environment is not None and runtime[0] != environment:
                            raise Problem("environment_locked", "任务运行环境已固定，需要其他环境请新建任务。")
                        selected = runtime[0]
                    else:
                        if environment is None:
                            raise Problem("environment_required", "首次发送必须明确选择本地演示或真实模型。", 422)
                        selected = environment
                    if selected not in ("offline", "live"):
                        raise Problem("invalid_environment", "运行环境无效。", 422)
                    if selected == "live" and not self.live_enabled:
                        raise Problem("live_unavailable", "当前服务未启用真实模型。")
                    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
                        raise Problem("invalid_prompt", "请输入有效问题。", 422)
                    knowledge = None
                    snapshot = None
                    if task.task_mode == "knowledge":
                        knowledge = self.documents.manifest(task_id, db)
                        self._retriever(knowledge["retriever"], selected)
                        work = str(self.data_dir / "project-documents" / task.project_id)
                    else:
                        row = db.execute("SELECT snapshot_json FROM project_bindings WHERE project_id=?", (task.project_id,)).fetchone()
                        if row is None:
                            raise Problem("source_binding_required", "项目尚未绑定可用仓库。")
                        snapshot = json.loads(row[0])
                        project = self.store._project(db, task.project_id)
                        current = self.source(project.source_kind, snapshot["repository"])
                        if current != snapshot or current["source_key"] != project.source_key:
                            raise Problem("source_mismatch", "来源证据发生变化，请重新核对绑定。")
                        work = snapshot["workspace"]
                    if task.memory_enabled and self.memories.path.exists():
                        self.memories.open(task.project_id, selected)
                    if any(job["status"] == "running" for job in self.manager.list()):
                        raise Problem("busy", "当前已有运行，请等待结束或停止后再发送。")
                    run_id, now = uuid4().hex, timestamp()
                    envelope = dict(prompt=prompt, session=task.task_id, engine=task.task_mode,
                                    mode=selected, context_policy=task.context_policy, memory=task.memory_enabled,
                                    documents=[item["name"] for item in knowledge["documents"]] if knowledge else [],
                                    retriever=knowledge["retriever"] if knowledge else "bm25",
                                    repository=snapshot["repository"] if snapshot else None,
                                    workspace=work, data_dir=str(self.data_dir),
                                    workspace_id=self.store.workspace_id, project_id=task.project_id,
                                    task_id=task.task_id, history_key=task.history_key,
                                    request_id=request_id, repository_snapshot=snapshot,
                                    knowledge_context=knowledge)
                    if not runtime:
                        db.execute("INSERT INTO task_runtime VALUES(?,?,?)", (task_id, selected, now))
                    db.execute("INSERT INTO task_runs VALUES(?,?,?,?,?,?,?)", (
                        run_id, task_id, task.project_id, request_id, json.dumps(envelope), "accepted", now,
                    ))
                    if knowledge:
                        db.executemany("INSERT INTO run_documents VALUES(?,?,?,?)", [
                            (run_id, task.project_id, item["document_id"], item["version_id"]) for item in knowledge["documents"]
                        ])
                    self._remember(db, "send", request_id, payload, run_id)
            if not replay:
                try:
                    self.manager.submit("agent", envelope, timeout=115, job_id=run_id)
                except (OSError, ValueError):
                    with self.store._connection(write=True) as db:
                        db.execute("UPDATE task_runs SET admission_state='interrupted' WHERE run_id=?", (run_id,))
            return self.run(run_id)
