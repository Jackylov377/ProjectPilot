"""Bounded, strictly read-only history discovery and presentation."""

import hashlib
import json
import os
from pathlib import Path

from .inventory import MAX_FILES, MAX_FILE_BYTES
from .service import Problem, task_state_path
from .sources import local_identity


def read_object(path):
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise Problem("history_unavailable", "历史定位包含不支持的链接。")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("Read bound exceeded")
        return raw.decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        raise Problem("history_unavailable", "历史文件不可读或超过读取上限。", 500) from None


def history_files(root):
    if not root.exists():
        return []
    files = []
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise Problem("history_unavailable", "历史目录不能是链接。")

    def failure(_error):
        raise Problem("history_unavailable", "部分历史目录不可读。", 500)

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=failure):
        for name in dirs[:]:
            path = Path(directory) / name
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise Problem("history_unavailable", "历史目录包含未读取的链接。")
        dirs.sort()
        for name in sorted(names):
            path = Path(directory) / name
            if path.suffix == ".jsonl":
                if not path.resolve().is_relative_to(root.resolve()):
                    raise Problem("history_unavailable", "历史定位超出所属范围。")
                files.append(path)
                if len(files) > MAX_FILES:
                    raise Problem("history_unavailable", "历史文件数量超过读取上限。")
    return sorted(files)


def parse_history(path):
    header, messages = None, []
    try:
        for line in read_object(path).splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Nonobject record")
            if row.get("_type") == "metadata":
                if header is not None or messages:
                    raise ValueError("Duplicate or misplaced metadata")
                header = row
            elif row.get("_type") == "provider_state":
                continue
            elif row.get("role") in ("user", "assistant", "tool", "system"):
                if row.get("projectpilot_run_id") is not None and not isinstance(row["projectpilot_run_id"], str):
                    raise ValueError("Invalid run identity")
                messages.append(row)
            else:
                raise ValueError("Unknown history record")
        if not header or not isinstance(header.get("key"), str):
            raise ValueError("Missing history identity")
        return header, messages
    except (ValueError, TypeError):
        raise Problem("history_damaged", "历史记录损坏或格式不受支持，原文件保持不变。", 500) from None


def present(messages, jobs):
    """Join only explicit run identities. Never guess an untagged legacy turn's usage."""
    messages = [dict(row) for row in messages if row.get("role") in ("user", "assistant") and row.get("content")]
    represented = {row.get("projectpilot_run_id") for row in messages}
    # Untagged legacy transcripts cannot be reconciled by matching prompt text.
    # A missing request after the first explicitly linked run can still be recovered.
    known_times = [jobs[run_id]["created_at"] for run_id in represented if isinstance(run_id, str) and run_id in jobs]
    for run_id, job in sorted(jobs.items(), key=lambda pair: pair[1]["created_at"]):
        if not messages or all(row.get("projectpilot_run_id") for row in messages) or (known_times and job["created_at"] >= min(known_times)):
            prompt = job.get("request", {}).get("prompt")
            if run_id not in represented and prompt:
                index = next((i for i, row in enumerate(messages)
                              if jobs.get(row.get("projectpilot_run_id"), {}).get("created_at", "") > job["created_at"]), len(messages))
                messages.insert(index, {"role": "user", "content": prompt, "projectpilot_run_id": run_id})
    answered = {row.get("projectpilot_run_id") for row in messages
                if row["role"] == "assistant" and not row.get("tool_calls")}
    result = []
    for row in messages:
        entry = {"role": row["role"], "content": row["content"]}
        run_id = row.get("projectpilot_run_id")
        if isinstance(run_id, str):
            entry["run_id"] = run_id
        if row["role"] == "assistant":
            entry.update(usage=None, is_final_answer=not bool(row.get("tool_calls")))
        job = jobs.get(run_id) if isinstance(run_id, str) else None
        if job:
            entry.update(job_id=job["id"], turn_status=job["status"])
            if row["role"] == "assistant" or (run_id not in answered and job["status"] != "running"):
                entry["usage"] = job.get("result", {}).get("usage")
                if job["status"] != "completed":
                    for key in ("error", "error_code", "error_hint"):
                        if isinstance(job.get(key), str):
                            entry[key] = job[key]
        result.append(entry)
    return result


def task_history(service, task_id):
    task = service.store.get_task(task_id)
    info = service.task(task_id)
    runs = service.runs(task_id)
    environment = info["run_environment"]
    if environment is None:
        return []
    state = task_state_path(service.data_dir, service.store.workspace_id, task.project_id, task_id, environment)
    files = history_files(state / "sessions")
    if len(files) > 1:
        raise Problem("history_ambiguous", "任务关联多个历史文件，不能自动合并。")
    messages = []
    jobs = {run["id"]: run for run in runs}
    if files:
        header, messages = parse_history(files[0])
        if header["key"] != task.history_key:
            raise Problem("history_scope_mismatch", "历史身份与任务不匹配。")
        for row in messages:
            identity = row.get("projectpilot_run_id")
            if row.get("role") == "user" and identity is None:
                raise Problem("history_scope_mismatch", "任务历史中的用户消息缺少运行归属。")
            if identity is not None and identity not in jobs:
                raise Problem("history_scope_mismatch", "历史中出现不属于当前任务的运行。")
    elif any(run["status"] == "completed" for run in runs):
        raise Problem("history_missing", "已完成运行的历史文件缺失。", 500)
    result = present(messages, jobs)
    for message in result:
        context = jobs.get(message.get("run_id"), {}).get("request", {}).get("knowledge_context")
        if context:
            message["document_manifest"] = [{key: item[key] for key in (
                "document_id", "version_id", "revision", "name", "content_sha256")}
                for item in context["documents"]]
            message["retriever"] = context["retriever"]
    return result


class LegacyHistory:
    def __init__(self, data_dir, workspace):
        self.data_dir, self.workspace = Path(data_dir).resolve(), Path(workspace).resolve()

    def _descriptors(self):
        root = self.data_dir / "sessions"
        result = []
        for path in history_files(root):
            parts = path.relative_to(root).parts
            identity = hashlib.sha256((local_identity(self.data_dir) + ":" + path.relative_to(root).as_posix()).encode()).hexdigest()
            item = {"legacy_id": identity, "session": None, "engine": parts[0] if parts else None,
                    "mode": parts[1] if len(parts) > 1 else None,
                    "workspace_hash": parts[2] if len(parts) > 2 else None,
                    "read_only": True, "updated_at": "", "_path": path}
            try:
                header, _messages = parse_history(path)
                if not header["key"].startswith("projectpilot:"):
                    raise Problem("history_damaged", "历史身份格式不受支持。", 500)
                item.update(session=header["key"].removeprefix("projectpilot:"), updated_at=header.get("updated_at", ""))
            except Problem as exc:
                item.update(error_code=exc.code, error=exc.message)
            result.append(item)
        known = {(item["engine"], item["mode"], item["workspace_hash"], item["session"]) for item in result}
        for record in self._receipt_records():
            body = record["request"]
            scope = hashlib.sha256(local_identity(body["workspace"]).encode()).hexdigest()[:16]
            identity = (body["engine"], body["mode"], scope, body["session"])
            if identity not in known:
                known.add(identity)
                result.append({"legacy_id": hashlib.sha256((local_identity(self.data_dir) + ":receipt-scope:" + json.dumps(identity)).encode()).hexdigest(),
                               "session": body["session"], "engine": body["engine"], "mode": body["mode"],
                               "workspace_hash": scope, "read_only": True, "history_missing": True,
                               "updated_at": record["created_at"], "_path": None})
        return result

    def list(self):
        return [{key: value for key, value in item.items() if key != "_path"} for item in self._descriptors()]

    def _receipt_records(self):
        records = []
        for index, path in enumerate(sorted((self.data_dir / "jobs").glob("*/job.json"))):
            if index >= MAX_FILES:
                raise Problem("history_unavailable", "运行回执数量超出读取上限。")
            try:
                if not path.resolve().is_relative_to(self.data_dir):
                    continue
                if any(parent.is_symlink() or getattr(parent, "is_junction", lambda: False)()
                       for parent in (path.parent, path.parent.parent)):
                    continue
                record = json.loads(read_object(path))
                if not isinstance(record, dict) or not isinstance(record.get("request"), dict):
                    continue
                body = record["request"]
                if (record.get("kind") != "agent" or body.get("task_id")
                        or not isinstance(body.get("workspace"), str)
                        or not Path(body["workspace"]).is_absolute()
                        or body.get("engine") not in ("session", "repository", "knowledge")
                        or body.get("mode") not in ("offline", "live")
                        or not isinstance(body.get("session"), str)
                        or not isinstance(record.get("created_at"), str)
                        or ("result" in record and not isinstance(record["result"], dict))
                        or record.get("status") not in ("running", "completed", "failed", "cancelled", "interrupted")):
                    continue
                if record.get("id") != path.parent.name:
                    continue
                records.append(record)
            except (Problem, ValueError, TypeError, KeyError):
                continue
        return records

    def _receipts(self, item):
        jobs = {}
        for record in self._receipt_records():
            body = record["request"]
            try:
                scope = hashlib.sha256(local_identity(body["workspace"]).encode()).hexdigest()[:16]
                if (body.get("session"), body.get("engine"), body.get("mode"), scope) != (
                    item["session"], item["engine"], item["mode"], item["workspace_hash"]
                ):
                    continue
                run_id = record.get("run_id")
                if isinstance(run_id, str) and run_id == record["id"]:
                    jobs[run_id] = record
            except (Problem, ValueError, TypeError, KeyError):
                continue  # Cannot attach unverified receipt metadata to a transcript.
        return jobs

    def read(self, legacy_id):
        item = next((item for item in self._descriptors() if item["legacy_id"] == legacy_id), None)
        if item is None:
            raise Problem("history_not_found", "未找到旧历史。", 404)
        if "error_code" in item:
            raise Problem(item["error_code"], item["error"], 500)
        if item["_path"] is None:
            jobs = self._receipts(item)
            if any(job["status"] == "completed" for job in jobs.values()):
                raise Problem("history_missing", "已完成旧运行的历史文件缺失，未创建空会话。", 500)
            return present([], jobs)
        _header, messages = parse_history(item["_path"])
        return present(messages, self._receipts(item))

    def scope(self, engine, environment, repository_id=None):
        if repository_id:
            import re
            if engine != "repository" or not re.fullmatch(r"[0-9a-f]{32}", repository_id):
                raise Problem("invalid_source", "旧来源标识无效。", 422)
            # The source may no longer exist; reading a transcript must not re-import it.
            source = self.data_dir / "jobs" / repository_id / "source"
        else:
            source = self.data_dir / "knowledge" if engine == "knowledge" else self.workspace
        scope = hashlib.sha256(local_identity(source).encode()).hexdigest()[:16]
        return [item for item in self.list() if (item["engine"], item["mode"], item["workspace_hash"]) == (engine, environment, scope)]

    def read_scoped(self, session, engine, environment, repository_id=None):
        matched = [item for item in self.scope(engine, environment, repository_id) if item["session"] == session]
        if not matched:
            raise Problem("history_not_found", "未找到旧历史；不会创建空会话。", 404)
        if len(matched) > 1:
            raise Problem("history_ambiguous", "同一旧身份存在多个历史文件，请使用明确的历史标识。")
        return self.read(matched[0]["legacy_id"])
