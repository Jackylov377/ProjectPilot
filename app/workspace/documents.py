"""Project-owned immutable original versions and optimistic task selections."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
from uuid import uuid4

from app.rag.ingestion import Document, parse_document
from .errors import Problem
from .store import StorageError, timestamp

MAX_UPLOAD = 4 * 1024 * 1024
RETRIEVERS = ("bm25", "vector", "hybrid", "hybrid_rerank")
ROOT = Path(__file__).resolve().parents[2]


def filename(name):
    if (not isinstance(name, str) or not re.fullmatch(r'[^/\\:<>"|?*\x00-\x1f\x7f]{1,100}\.(?:txt|md|pdf)', name, re.I)
            or name.startswith(".") or ".." in name or name != name.strip()):
        raise Problem("invalid_document_name", "请使用普通 TXT、Markdown 或 PDF 文件名。", 422)
    return unicodedata.normalize("NFC", name)


def content_path(data_dir, locator):
    root = Path(data_dir).resolve()
    relative = Path(locator)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "project-documents":
        raise Problem("document_unavailable", "资料定位无效。")
    path = root / relative
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise Problem("document_unavailable", "资料定位包含链接。")
    if not path.resolve().is_relative_to(root):
        raise Problem("document_unavailable", "资料超出项目存储范围。")
    return path


def parsed_manifest_document(data_dir, item, workspace_id):
    """Read the pinned original, replacing physical-path identities with stable ones."""
    path = content_path(data_dir, item["locator"])
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_UPLOAD + 1)
        if len(raw) > MAX_UPLOAD or hashlib.sha256(raw).hexdigest() != item["content_sha256"]:
            raise ValueError("Content changed")
        parsed = parse_document(path.parent, path.name)
    except (OSError, ValueError):
        raise Problem("document_unavailable", "运行固定的资料版本缺失或已损坏。") from None
    return Document(workspace_id, item["document_id"], parsed.version, item["name"], parsed.text, parsed.pages)


class ProjectDocuments:
    def __init__(self, service):
        self.service, self.store, self.data_dir = service, service.store, service.data_dir

    def _document(self, db, project_id, document_id):
        self.store._project(db, project_id)
        row = db.execute("SELECT * FROM documents WHERE project_id=? AND document_id=?", (project_id, document_id)).fetchone()
        if row is None:
            raise Problem("document_not_found", "未找到本项目中的资料。", 404)
        return dict(row)

    def _version(self, db, project_id, document_id, version_id):
        row = db.execute("""SELECT v.*,d.name,d.media_type FROM document_versions v
            JOIN documents d ON d.document_id=v.document_id
            WHERE v.project_id=? AND v.document_id=? AND v.version_id=?""",
                         (project_id, document_id, version_id)).fetchone()
        if row is None:
            raise Problem("version_not_found", "未找到本项目资料的该版本。", 404)
        result = dict(row)
        result["preview"] = json.loads(result.pop("preview_json"))
        return result

    def version(self, project_id, document_id, version_id):
        with self.store._connection(write=False) as db:
            self._document(db, project_id, document_id)
            return self._version(db, project_id, document_id, version_id)

    def document(self, project_id, document_id):
        with self.store._connection(write=False) as db:
            result = self._document(db, project_id, document_id)
            result["current_version"] = self._version(db, project_id, document_id, result["current_version_id"])
            return result

    def list(self, project_id):
        self.store.get_project(project_id)
        with self.store._connection(write=False) as db:
            ids = [row[0] for row in db.execute("SELECT document_id FROM documents WHERE project_id=? ORDER BY created_at,document_id", (project_id,))]
        return [self.document(project_id, identifier) for identifier in ids]

    def versions(self, project_id, document_id):
        with self.store._connection(write=False) as db:
            self._document(db, project_id, document_id)
            return [self._version(db, project_id, document_id, row[0]) for row in db.execute(
                "SELECT version_id FROM document_versions WHERE document_id=? ORDER BY revision", (document_id,)).fetchall()]

    def publish(self, project_id, *, request_id, content, name=None, document_id=None, expected_revision=None):
        try:
            return self._publish(project_id, request_id=request_id, content=content, name=name,
                                 document_id=document_id, expected_revision=expected_revision)
        except (OSError, sqlite3.Error, StorageError):
            raise Problem("document_storage_failed", "资料发布失败，原可用版本未改变。", 503) from None

    def _publish(self, project_id, *, request_id, content, name=None, document_id=None, expected_revision=None):
        self.store.get_project(project_id)
        if not content or len(content) > MAX_UPLOAD:
            raise Problem("document_size", "资料不能为空，且单份最多 4 MiB。", 413 if content else 422)
        if document_id is not None:
            name = self.document(project_id, document_id)["name"]
        name = filename(name)
        sha = hashlib.sha256(content).hexdigest()
        payload = dict(project_id=project_id, document_id=document_id, name=name,
                       content_sha256=sha, expected_revision=expected_revision)
        with self.store._connection(write=False) as db:
            prior = self.service._prior(db, "publish_document", request_id, payload)
            if prior:
                row = db.execute("SELECT document_id FROM document_versions WHERE version_id=?", (prior,)).fetchone()
                return self._version(db, project_id, row[0], prior)
        staging = self.data_dir / "document-staging"
        staging.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="upload-", dir=staging) as temporary:
            temporary = Path(temporary)
            raw = temporary / ("source" + Path(name).suffix.lower())
            raw.write_bytes(content)
            metadata = temporary / "validation.json"
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "app.workspace.document_validation", str(temporary), raw.name, str(metadata)],
                    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if result.returncode or not metadata.is_file() or metadata.stat().st_size > 256 * 1024:
                    raise ValueError("Invalid document")
                preview = json.loads(metadata.read_text(encoding="utf-8"))
            except (OSError, ValueError, subprocess.TimeoutExpired):
                raise Problem("document_invalid", "资料解析失败或超时，原可用版本未改变。", 422) from None
            try:
                with self.store._connection(write=True) as db:
                    prior = self.service._prior(db, "publish_document", request_id, payload)
                    if prior:
                        doc = db.execute("SELECT document_id FROM document_versions WHERE version_id=?", (prior,)).fetchone()[0]
                        return self._version(db, project_id, doc, prior)
                    now = timestamp()
                    if document_id is None:
                        if db.execute("SELECT 1 FROM documents WHERE project_id=? AND name_key=?", (project_id, name.casefold())).fetchone():
                            raise Problem("document_name_conflict", "同项目已有同名资料，请显式修订原文档。")
                        doc = uuid4().hex
                        db.execute("INSERT INTO documents VALUES(?,?,?,?,?,NULL,0,?,?)", (
                            doc, project_id, name, name.casefold(), Path(name).suffix.lower(), now, now,
                        ))
                        revision = 1
                    else:
                        doc = document_id
                        current = self._document(db, project_id, doc)
                        if type(expected_revision) is not int or expected_revision != current["revision"]:
                            raise Problem("revision_conflict", "资料已被更新，请刷新后再修订。")
                        old = self._version(db, project_id, doc, current["current_version_id"])
                        if old["content_sha256"] == sha:
                            self.service._remember(db, "publish_document", request_id, payload, old["version_id"])
                            return old
                        revision = current["revision"] + 1
                    version = uuid4().hex
                    locator = f"project-documents/{project_id}/{doc}/{version}/{raw.name}"
                    target = content_path(self.data_dir, locator)
                    target.parent.mkdir(parents=True, exist_ok=False)
                    shutil.move(str(raw), str(target))  # Fresh UUID directory, never overwrite a published file.
                    db.execute("INSERT INTO document_versions VALUES(?,?,?,?,?,?,?,?,?)", (
                        version, project_id, doc, revision, sha, len(content), locator, json.dumps(preview), now,
                    ))
                    db.execute("UPDATE documents SET current_version_id=?,revision=?,updated_at=? WHERE document_id=?", (version, revision, now, doc))
                    self.service._remember(db, "publish_document", request_id, payload, version)
                    return self._version(db, project_id, doc, version)
            except sqlite3.Error as exc:
                raise Problem("document_storage_failed", "资料发布失败，未替换原版本。", 503) from exc

    def settings(self, task_id, db=None):
        if db is None:
            with self.store._connection(write=False) as connection:
                return self.settings(task_id, connection)
        task = self.store._task(db, task_id)
        if task.task_mode != "knowledge":
            raise Problem("not_knowledge_task", "仅知识资料问答任务可以选择资料。", 422)
        row = db.execute("SELECT * FROM task_knowledge WHERE task_id=?", (task_id,)).fetchone()
        return {"task_id": task_id, "project_id": task.project_id, "revision": row["revision"],
                "retriever": row["retriever"], "document_ids": [r[0] for r in db.execute(
                    "SELECT document_id FROM task_documents WHERE task_id=? ORDER BY document_id", (task_id,))]}

    def select(self, task_id, *, request_id, expected_revision, document_ids, retriever):
        if retriever not in RETRIEVERS or len(document_ids) > 20 or len(set(document_ids)) != len(document_ids):
            raise Problem("invalid_selection", "请选择最多 20 份不重复资料和有效检索方式。", 422)
        payload = dict(task_id=task_id, expected_revision=expected_revision,
                       document_ids=sorted(document_ids), retriever=retriever)
        with self.store._connection(write=True) as db:
            prior = self.service._prior(db, "select_documents", request_id, payload)
            if prior:
                return json.loads(prior)  # Original result/revision, not a second replacement.
            current = self.settings(task_id, db)
            if type(expected_revision) is not int or expected_revision != current["revision"]:
                raise Problem("revision_conflict", "任务资料设置已改变，请刷新后再修改。")
            runtime = db.execute("SELECT environment FROM task_runtime WHERE task_id=?", (task_id,)).fetchone()
            if runtime and runtime[0] == "offline" and retriever != "bm25":
                raise Problem("retriever_requires_live", "离线任务不能使用高级检索。")
            for identifier in document_ids:
                self._document(db, current["project_id"], identifier)
            db.execute("DELETE FROM task_documents WHERE task_id=?", (task_id,))
            db.executemany("INSERT INTO task_documents VALUES(?,?,?)", [(task_id, current["project_id"], identifier) for identifier in sorted(document_ids)])
            db.execute("UPDATE task_knowledge SET retriever=?,revision=revision+1 WHERE task_id=?", (retriever, task_id))
            selected = self.settings(task_id, db)
            self.service._remember(db, "select_documents", request_id, payload, json.dumps(selected))
            return selected

    def manifest(self, task_id, db):
        settings = self.settings(task_id, db)
        if not settings["document_ids"]:
            raise Problem("documents_required", "请先为任务选择至少一份可用项目资料。")
        versions = []
        for identifier in settings["document_ids"]:
            doc = self._document(db, settings["project_id"], identifier)
            version = self._version(db, settings["project_id"], identifier, doc["current_version_id"])
            path = content_path(self.data_dir, version["locator"])
            try:
                with path.open("rb") as handle:
                    raw = handle.read(MAX_UPLOAD + 1)
                if len(raw) > MAX_UPLOAD or hashlib.sha256(raw).hexdigest() != version["content_sha256"]:
                    raise ValueError("Content damaged")
            except (OSError, ValueError):
                raise Problem("document_unavailable", "所选资料版本缺失或损坏，未接收运行。") from None
            versions.append({key: version[key] for key in ("project_id", "document_id", "version_id", "revision", "content_sha256", "locator", "name", "media_type")})
        return {"revision": settings["revision"], "retriever": settings["retriever"], "documents": versions}
