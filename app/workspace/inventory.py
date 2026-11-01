"""Read-only legacy inventory. No runtime managers, providers or migrations.

Evidence is deliberately conservative: a receipt alone does not prove the policy
of every message in a JSONL. Unknowns remain unknown, even on a successful scan.
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from uuid import uuid4

from .sources import github_identity, local_identity

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_FILES = 20000
MAX_TOTAL_BYTES = 512 * 1024 * 1024
KINDS = ("candidate", "legacy_read_only", "needs_confirmation", "abnormal")
LABELS = dict(zip(KINDS, ("可迁移候选", "旧项目对话只读", "待确认", "异常")))
REASONS = {
    "configuration_missing": "固定配置缺失或格式无效，不补默认值",
    "configuration_conflict": "同一旧会话的固定配置不一致",
    "source_unresolved": "无法可靠确定仓库或新项目来源",
    "source_missing": "来源目录或导入快照不可用",
    "import_conflict": "导入引用与实际来源不一致",
    "history_missing": "缺少关联的聊天历史",
    "history_ambiguous": "同一旧身份关联多个历史文件",
    "history_damaged": "历史文件或必要字段损坏",
    "message_unlinked": "部分用户消息或最终回答没有可靠运行关联",
    "receipt_unlinked": "部分历史运行缺少回执或回执与本会话不符",
    "receipt_without_message": "部分运行回执没有对应的历史消息",
    "receipt_damaged": "运行回执损坏或身份无效",
    "document_missing": "所选资料不存在或越出已知资料目录",
    "document_version_unknown": "旧资料未持久保存完整版本，不能恢复全部历史依据",
    "running_job": "存在尚未结束的运行，数据稳定性不能确认",
    "orphan_history": "发现没有可靠运行回执关联的历史",
    "scan_unstable": "扫描未基于已确认的稳定数据，可靠候选结论已撤销",
}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def file_manifest(root: Path) -> tuple[dict, list]:
    """Bounded fingerprints of the explicitly selected root, never following links."""
    result, errors, total, visited = {}, [], 0, 0
    if not root.is_dir() or _is_link(root):
        return {}, [{"path": str(root), "code": "root_unavailable"}]

    def walk_error(error):
        errors.append({"path": str(error.filename), "code": "directory_unreadable"})

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        base = Path(directory)
        for name in sorted(dirs[:]):
            if _is_link(base / name):
                dirs.remove(name)
                errors.append({"path": str((base / name).relative_to(root)), "code": "linked_directory_skipped"})
        dirs.sort()
        for name in sorted(files):
            path = base / name
            relative = path.relative_to(root).as_posix()
            visited += 1
            if visited > MAX_FILES:
                errors.append({"path": relative, "code": "file_count_bound_exceeded"})
                return result, sorted(errors, key=lambda error: (error["path"], error["code"]))
            try:
                if _is_link(path) or not path.is_file():
                    raise ValueError("Nonregular file")
                stat = path.stat()
                if stat.st_size > MAX_FILE_BYTES or total + stat.st_size > MAX_TOTAL_BYTES:
                    raise ValueError("Read bound exceeded")
                hasher = hashlib.sha256()
                with path.open("rb") as handle:
                    remaining = MAX_FILE_BYTES + 1
                    while block := handle.read(min(1024 * 1024, remaining)):
                        hasher.update(block)
                        remaining -= len(block)
                        if remaining == 0:
                            raise ValueError("File grew beyond read bound")
                after = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("File changed during fingerprinting")
                result[relative] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": hasher.hexdigest()}
                total += stat.st_size
            except (OSError, ValueError):
                errors.append({"path": relative, "code": "file_unreadable_or_unstable"})
    return result, sorted(errors, key=lambda error: (error["path"], error["code"]))


def _inside(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


class Reader:
    def __init__(self, root: Path, original_root: Path):
        self.root, self.original_root = root.resolve(), original_root.resolve()
        self.gaps = []

    def original(self, path: Path) -> str:
        return str(self.original_root / path.relative_to(self.root))

    def translated(self, value: str) -> Path:
        original = Path(value).resolve()
        if _inside(original, self.original_root):
            return self.root / original.relative_to(self.original_root)
        # External sources are only checked for availability, never recursed into.
        return original

    def evidence(self, path: Path, *, line=None, run_id=None):
        value = {"path": self.original(path)}
        if line is not None:
            value["line"] = line
        if run_id is not None:
            value["run_id"] = run_id
        return value

    def raw(self, path: Path) -> bytes:
        resolved = path.resolve()
        if not _inside(resolved, self.root) or _is_link(path):
            raise ValueError("Read outside selected data root")
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Read bound exceeded")
        return data

    def json(self, path: Path) -> dict:
        value = json.loads(self.raw(path).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected object")
        return value


def _new_entry(identity: dict):
    return {
        "entry_id": digest(identity), "identity": identity, "classification": None,
        "configuration_evidence": [], "sources": [], "evidence": [],
        "run_ids": [], "history_paths": [], "reason_codes": [], "issues": [],
    }


def _identity(root: Path, request: dict) -> dict:
    return {
        "data_root": str(root), "workspace": local_identity(request["workspace"]),
        "engine": request["engine"], "environment": request["mode"], "session": request["session"],
    }


def _bucket(identity):
    workspace_hash = hashlib.sha256(identity["workspace"].encode()).hexdigest()[:16]
    return identity["engine"], identity["environment"], workspace_hash, identity["session"]


def _add_reason(entry, code):
    if code not in entry["reason_codes"]:
        entry["reason_codes"].append(code)


def _read_history(reader, path):
    messages, header, issues = [], None, []
    try:
        lines = reader.raw(path).decode("utf-8").splitlines()
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("Not object")
                if value.get("_type") == "metadata":
                    if header is not None or messages:
                        raise ValueError("Misplaced metadata")
                    header = value
                elif value.get("_type") == "provider_state":
                    continue
                elif value.get("role") in ("user", "assistant", "tool", "system"):
                    messages.append({
                        "role": value["role"], "run_id": value.get("projectpilot_run_id"),
                        "final": value["role"] == "user" or (value["role"] == "assistant" and not value.get("tool_calls")),
                        "line": number,
                    })
                else:
                    raise ValueError("Unknown record")
            except (ValueError, TypeError):
                issues.append({**reader.evidence(path, line=number), "code": "invalid_jsonl_record"})
    except (OSError, ValueError, UnicodeError):
        issues.append({**reader.evidence(path), "code": "history_unreadable"})
    if not header or not isinstance(header.get("key"), str) or not header["key"].startswith("projectpilot:"):
        issues.append({**reader.evidence(path), "code": "invalid_history_header"})
    return header, messages, issues


def _source(reader, request, imports):
    if request["engine"] == "knowledge":
        return None, ["source_unresolved"]
    if request["engine"] == "session":
        return {"kind": "legacy_local", "locator": local_identity(request["workspace"])}, []
    workspace = reader.translated(request["workspace"])
    reasons = [] if workspace.is_dir() and not _is_link(workspace) else ["source_missing"]
    reference = request.get("repository")
    if reference is not None:
        if not isinstance(reference, str) or not re.fullmatch(r"[0-9a-f]{32}", reference):
            return None, reasons + ["source_unresolved"]
        imported = imports.get(reference)
        try:
            info = imported["result"]["repository"]
            if imported["status"] != "completed" or info["id"] != reference:
                raise ValueError("Import incomplete")
            expected = reader.original_root / "jobs" / reference / "source"
            if local_identity(expected) != local_identity(request["workspace"]):
                reasons.append("import_conflict")
            if not isinstance(info["url"], str):
                raise ValueError("Import URL missing")
            key, locator = github_identity(info["url"])
            return {"kind": "github", "source_key": key, "locator": locator,
                    "import_id": reference, "commit": info.get("commit"),
                    "archive_sha256": info.get("archive_sha256")}, reasons
        except (KeyError, TypeError, ValueError):
            return None, reasons + ["source_unresolved"]
    locator = local_identity(request["workspace"])
    return {"kind": "local", "source_key": "local:" + locator, "locator": locator}, reasons


def _documents(reader, request, entry, assets):
    if request["engine"] != "knowledge":
        return
    selected = request.get("documents")
    if not isinstance(selected, list) or not selected or len(selected) > 20:
        _add_reason(entry, "document_missing")
        return
    directory = reader.root / "knowledge"
    for name in selected:
        if not isinstance(name, str):
            _add_reason(entry, "document_missing")
            continue
        path = (directory / name).resolve()
        if not _inside(path, directory.resolve()) or not path.is_file() or _is_link(path):
            _add_reason(entry, "document_missing")
        assets.append({"kind": "document_reference", "entry_id": entry["entry_id"],
                       "name": name, "historical_version": "unknown"})
    _add_reason(entry, "document_version_unknown")


def _memory_assets(reader):
    path = reader.root / "memory.sqlite3"
    if not path.exists():
        return [], []
    assets, issues = [], []
    try:
        # Always copy SQLite + WAL first. Even read-only SQLite can create SHM files.
        with tempfile.TemporaryDirectory(prefix="projectpilot-memory-") as temporary:
            copy = Path(temporary) / "memory.sqlite3"
            for suffix in ("", "-wal", "-shm"):
                source = path.with_name(path.name + suffix)
                if source.exists():
                    (Path(temporary) / source.name).write_bytes(reader.raw(source))
            db = sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)
            try:
                db.execute("PRAGMA query_only=ON")
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Invalid SQLite snapshot")
                rows = db.execute("""SELECT owner_id,workspace_id,mode,engine,status,COUNT(*)
                    FROM memories GROUP BY owner_id,workspace_id,mode,engine,status
                    ORDER BY owner_id,workspace_id,mode,engine,status""")
                for owner, workspace, environment, engine, status, count in rows:
                    assets.append({"kind": "memory_scope", "path": reader.original(path),
                                   "owner_id": owner, "workspace_id": workspace, "environment": environment,
                                   "engine": engine, "status": status, "count": count,
                                   "project_mapping": "unconfirmed"})
            finally:
                db.close()
    except (OSError, ValueError, sqlite3.Error):
        issues.append({"path": reader.original(path), "code": "memory_unreadable"})
    return assets, issues


def inventory(data_root: str | Path, *, stable_source: str | None = None,
              original_root: str | Path | None = None) -> dict:
    """Read a root; without explicit stability evidence the result is diagnostic only."""
    root = Path(data_root).resolve()
    origin = Path(original_root).resolve() if original_root is not None else root
    reader = Reader(root, origin)
    before, gaps = file_manifest(root)
    entries, imports, receipts, assets = {}, {}, {}, []
    auxiliary_counts = Counter()
    invalid_entries = []
    running = False
    for relative in sorted(before):
        parts = Path(relative).parts
        if len(parts) != 3 or parts[0] != "jobs" or parts[2] != "job.json":
            continue
        path, job_id = root / relative, parts[1]
        try:
            record = reader.json(path)
            if not re.fullmatch(r"[0-9a-f]{32}", job_id) or record.get("id") != job_id:
                raise ValueError("Invalid receipt identity")
            if record.get("status") in ("queued", "running"):
                running = True
            if record.get("kind") != "agent":
                auxiliary_counts[str(record.get("kind", "unknown"))] += 1
                if record.get("kind") == "repository_import":
                    imports[job_id] = record
                continue
            request = record["request"]
            if not isinstance(request, dict) or not all(isinstance(request.get(k), str) and request[k] for k in ("workspace", "session", "engine", "mode")):
                raise ValueError("Invalid scope")
            if request["engine"] not in ("session", "repository", "knowledge") or request["mode"] not in ("offline", "live") or not Path(request["workspace"]).is_absolute():
                raise ValueError("Invalid scope")
            identity = _identity(origin, request)
            key = digest(identity)
            entry = entries.setdefault(key, _new_entry(identity))
            entry["run_ids"].append(job_id)
            entry["evidence"].append(reader.evidence(path, run_id=job_id))
            entry["configuration_evidence"].append({
                "run_id": job_id, "task_mode": request["engine"],
                "memory_enabled": request.get("memory"), "context_policy": request.get("context_policy"),
            })
            if type(request.get("memory")) is not bool or request.get("context_policy") not in ("window", "full", "summary"):
                _add_reason(entry, "configuration_missing")
            if record.get("status") in ("queued", "running"):
                _add_reason(entry, "running_job")
            receipts[job_id] = (record, entry)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            entry = _new_entry({"data_root": str(origin), "receipt_path": reader.original(path)})
            _add_reason(entry, "receipt_damaged")
            entry["issues"].append(reader.evidence(path))
            invalid_entries.append(entry)

    buckets = defaultdict(list)
    for entry in entries.values():
        buckets[_bucket(entry["identity"])].append(entry)
        configs = {digest(c | {"run_id": None}) for c in entry["configuration_evidence"]}
        if len(configs) > 1:
            _add_reason(entry, "configuration_conflict")
        for run_id in entry["run_ids"]:
            record, _ = receipts[run_id]
            source, reasons = _source(reader, record["request"], imports)
            if source is not None and source not in entry["sources"]:
                entry["sources"].append(source)
            for reason in reasons:
                _add_reason(entry, reason)
            _documents(reader, record["request"], entry, assets)

    for relative, fingerprint in sorted(before.items()):
        parts = Path(relative).parts
        if parts[0] == "knowledge":
            assets.append({"kind": "document_file", "path": str(origin / relative),
                           "current_sha256": fingerprint["sha256"], "project_mapping": "unconfirmed",
                           "historical_version_archive": "unknown"})
        elif len(parts) >= 4 and parts[0] == "jobs" and parts[2] == "reports":
            run_id = parts[1]
            entry_id = receipts[run_id][1]["entry_id"] if run_id in receipts else None
            assets.append({"kind": "report_artifact", "entry_id": entry_id, "run_id": run_id,
                           "path": str(origin / relative), "sha256": fingerprint["sha256"],
                           "user_save_intent": "unknown"})

    for relative in sorted(before):
        path = root / relative
        parts = Path(relative).parts
        if parts[0] != "sessions" or path.suffix != ".jsonl":
            continue
        header, messages, issues = _read_history(reader, path)
        session = header["key"].removeprefix("projectpilot:") if header and isinstance(header.get("key"), str) else None
        supported_layout = len(parts) in (6, 7) and parts[4] == "sessions"
        bucket = (parts[1], parts[2], parts[3], session) if supported_layout else None
        if not supported_layout:
            gaps.append({"path": reader.original(path), "code": "unsupported_history_layout"})
        matched = buckets.get(bucket, [])
        if len(matched) == 1:
            entry = matched[0]
        else:
            identity = {"data_root": str(origin), "history_path": reader.original(path),
                        "engine": parts[1] if len(parts) > 1 else None, "session": session}
            entry = _new_entry(identity)
            entries[entry["entry_id"]] = entry
            _add_reason(entry, "orphan_history")
        entry["history_paths"].append(reader.original(path))
        entry["evidence"].append(reader.evidence(path, line=1))
        entry["issues"].extend(issues)
        if issues:
            _add_reason(entry, "history_damaged")
        linked = defaultdict(set)
        for message in messages:
            if not message["final"]:
                continue
            run_id = message["run_id"]
            if not isinstance(run_id, str) or not run_id:
                _add_reason(entry, "message_unlinked")
            elif run_id not in entry["run_ids"]:
                _add_reason(entry, "receipt_unlinked")
            else:
                linked[run_id].add(message["role"])
                entry["evidence"].append(reader.evidence(path, line=message["line"], run_id=run_id))
        for run_id in entry["run_ids"]:
            required = {"user", "assistant"} if receipts[run_id][0].get("status") == "completed" else {"user"}
            if not required <= linked[run_id]:
                _add_reason(entry, "receipt_without_message")

    memories, memory_gaps = _memory_assets(reader)
    assets.extend(memories)
    gaps.extend(memory_gaps)
    after, after_errors = file_manifest(root)
    gaps.extend(after_errors)
    unchanged = before == after
    stable = bool(stable_source) and unchanged and not running and not gaps
    if not unchanged:
        gaps.append({"path": str(origin), "code": "source_changed"})
    if running:
        gaps.append({"path": str(origin), "code": "running_jobs_present"})
    if not stable_source:
        gaps.append({"path": str(origin), "code": "stability_not_confirmed"})

    for entry in list(entries.values()) + invalid_entries:
        if not entry["history_paths"] and "receipt_damaged" not in entry["reason_codes"]:
            _add_reason(entry, "history_missing")
        if len(entry["history_paths"]) > 1:
            _add_reason(entry, "history_ambiguous")
        damaged = {"receipt_damaged", "history_damaged", "history_missing", "document_missing", "source_missing"} & set(entry["reason_codes"])
        if damaged:
            category = "abnormal"
        elif entry["identity"].get("engine") == "session":
            category = "legacy_read_only"
        elif entry["reason_codes"]:
            category = "needs_confirmation"
        elif stable:
            category = "candidate"
        else:
            _add_reason(entry, "scan_unstable")
            category = "needs_confirmation"
        entry["classification"] = category
        entry["reason_codes"].sort()
        entry["reasons"] = [{"code": code, "description": REASONS[code]} for code in entry["reason_codes"]]
        entry["run_ids"].sort()
        entry["history_paths"].sort()
    all_entries = sorted(list(entries.values()) + invalid_entries, key=lambda entry: entry["entry_id"])
    counts = Counter(entry["classification"] for entry in all_entries)
    # Translate any fingerprinting gaps back to original input paths for durable evidence.
    for gap in gaps:
        value = gap.get("path", "")
        if value.startswith(str(root)):
            gap["path"] = str(origin) + value[len(str(root)):]
    return {
        "schema_version": 1, "tool_version": "workspace-inventory/1", "inventory_id": uuid4().hex,
        "generated_at": datetime.now(timezone.utc).isoformat(), "data_root": str(origin),
        "scan_status": "complete" if stable else ("partial" if before else "failed"),
        "stability": {"basis": stable_source, "unchanged": unchanged, "running_jobs": running,
                      "manifest_sha256": digest(before), "file_count": len(before)},
        "counts": {kind: counts[kind] for kind in KINDS},
        "excluded": ["Other application roots unless explicitly selected", "Evaluation runs are auxiliary, not conversations"],
        "auxiliary_runs": dict(sorted(auxiliary_counts.items())), "incomplete_ranges": gaps,
        "file_manifest": before, "entries": all_entries,
        "assets": sorted(assets, key=lambda asset: json.dumps(asset, sort_keys=True, ensure_ascii=False)),
    }


def stable_snapshot(source: Path, destination: Path) -> dict:
    """Copy a bounded root and verify every copied byte against two origin manifests.

    No process is stopped. Active receipts prevent the snapshot being certified.
    The caller owns and disposes the destination; the original is never opened writable.
    """
    source, destination = source.resolve(), destination.resolve()
    if _inside(destination, source) or _inside(source, destination):
        raise ValueError("Snapshot and input directories must not overlap")
    before, errors = file_manifest(source)
    if errors:
        raise ValueError("Cannot snapshot an unreadable or unstable source")
    destination.mkdir(parents=True, exist_ok=False)
    for relative in sorted(before):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target, follow_symlinks=False)
    copied, copy_errors = file_manifest(destination)
    after, after_errors = file_manifest(source)
    if copy_errors or after_errors or before != after or before != copied:
        raise ValueError("Source changed while copying; snapshot not certified")
    for relative in before:
        if re.fullmatch(r"jobs/[0-9a-f]{32}/job.json", relative):
            try:
                record = json.loads((destination / relative).read_text(encoding="utf-8"))
                if record.get("status") in ("queued", "running"):
                    raise ValueError("Active receipt prevents a stable snapshot")
            except (UnicodeError, json.JSONDecodeError, AttributeError):
                pass  # Malformed evidence remains an inventory finding.
    return {"original_root": str(source), "manifest_sha256": digest(before),
            "file_count": len(before), "origin_before_after_equal": True, "copy_equal": True}


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def markdown(result: dict) -> str:
    lines = ["# 旧数据只读清点", "", f"数据根：`{result['data_root']}`", "",
             f"扫描状态：**{result['scan_status']}**；该状态不表示所有记录可以迁移。", "",
             "| 分类 | 会话数 |", "| --- | ---: |"]
    lines.extend(f"| {LABELS[kind]} | {result['counts'][kind]} |" for kind in KINDS)
    lines += ["", "## 稳定性与范围", "", "```json", json.dumps(result["stability"], ensure_ascii=False, indent=2), "```", "",
              "未完成范围：", ""]
    lines.extend(f"- {_cell(gap['path'])}：{gap['code']}" for gap in result["incomplete_ranges"])
    if not result["incomplete_ranges"]:
        lines.append("- 无已发现的读取缺口。")
    lines += ["", "其他应用目录不在默认范围；评测、导入属于辅助运行，不计作用户任务。", "",
              "## 逐条结论", "", "| 清点身份 | 旧身份 | 分类 | 原因 | 证据位置 |", "| --- | --- | --- | --- | --- |"]
    for entry in result["entries"]:
        identity = json.dumps(entry["identity"], ensure_ascii=False, sort_keys=True)
        reasons = "; ".join(reason["description"] for reason in entry["reasons"]) or "来源与配置证据一致；仅为候选，尚未迁移"
        evidence = "; ".join(item["path"] + (f":{item['line']}" if "line" in item else "") for item in entry["evidence"])
        lines.append("| " + " | ".join(_cell(value) for value in (entry["entry_id"], identity, LABELS[entry["classification"]], reasons, evidence)) + " |")
    lines += ["", "## 资产与限制", "", f"辅助资产条目：{len(result['assets'])}；明细见 inventory.json。",
              "", "旧报告仅视为生成产物，用户主动保存意图未知。旧资料版本和新项目归属不足的记录不推定为可迁移。",
              "", "本工具未创建项目、任务，未执行迁移，未修改原聊天、记忆或报告。", ""]
    return "\n".join(lines)


def write_inventory(result: dict, output: str | Path):
    output = Path(output).resolve()
    source = Path(result["data_root"]).resolve()
    if _inside(output, source) or _inside(source, output):
        raise ValueError("Output must be separate from the input data root")
    output.mkdir(parents=True, exist_ok=False)
    # A fresh directory and exclusive writes prevent replacing earlier evidence.
    with (output / "inventory.json").open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    with (output / "inventory.md").open("x", encoding="utf-8") as handle:
        handle.write(markdown(result))
