"""Local FastAPI application: existing agent, knowledge, traces, evals and memory."""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Literal
from zipfile import ZIP_DEFLATED, ZipFile

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.github import github_repository
from app.api.jobs import ROOT, TERMINAL, BusyError, JobManager, read
from app.api.workspace import install_workspace_api
from app.api.streaming import latest_preview
from app.memory import MemoryScope, MemoryStore
from app.observability.trace import JsonlTrace
from app.rag.ingestion import parse_document
from app.rag.pipeline import Retriever, retrieval_secrets

STATIC = Path(__file__).with_name("static")
Engine = Literal["session", "repository", "knowledge"]
Mode = Literal["offline", "live"]


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=12000)
    session: str = Field(default="demo", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    engine: Engine = "session"
    mode: Mode = "offline"
    context_policy: Literal["full", "window", "summary"] = "window"
    memory: bool = False
    documents: list[str] = Field(default_factory=list, max_length=20)
    retriever: Retriever = "bm25"
    repository: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class RepositoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=300)


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    configs: list[Literal["full", "window", "summary", "memory-off"]] = Field(
        default=["full"], min_length=1, max_length=4
    )
    cases: list[str] = Field(default_factory=list, max_length=30)
    repeats: int = Field(default=1, ge=1, le=3)
    mode: Mode = "offline"


def create_app(
    *, workspace=ROOT / "examples/demo_project", data_dir=ROOT / "runs/web", allow_live=False
):
    workspace, data_dir = Path(workspace).resolve(strict=True), Path(data_dir).resolve()
    if not workspace.is_dir():
        raise ValueError("Workspace must be an existing directory")
    data_dir.mkdir(parents=True, exist_ok=True)
    knowledge = data_dir / "knowledge"
    knowledge.mkdir(exist_ok=True)
    sample = knowledge / "demo-guide.txt"
    if not sample.exists():
        sample.write_text(
            "示例资料：单个工具执行超时为15秒，整个回合执行超时为90秒。\n知识问答应引用资料来源，没有依据时说明无法确认。\n",
            encoding="utf-8",
        )
    load_dotenv(ROOT / ".env", override=False)
    cleaner = JsonlTrace(data_dir / "service-traces", secrets=retrieval_secrets())
    manager = JobManager(data_dir / "jobs", secrets=retrieval_secrets())
    live_enabled = allow_live and all(
        os.environ.get(name)
        for name in ("PROJECTPILOT_API_KEY", "PROJECTPILOT_MODEL", "PROJECTPILOT_API_BASE")
    )

    @asynccontextmanager
    async def lifespan(app):
        async def supervise():
            while True:
                try:
                    await asyncio.to_thread(manager.list)
                except OSError as exc:
                    cleaner.emit("jobs.supervision.failed", error=type(exc).__name__)
                    await asyncio.sleep(1)
                await asyncio.sleep(0.25)

        watcher = asyncio.create_task(supervise())
        try:
            yield
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            await asyncio.to_thread(manager.close)

    app = FastAPI(title="ProjectPilot 本地工作台", lifespan=lifespan)
    app.state.jobs = manager
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver", "[::1]"]
    )

    @app.middleware("http")
    async def local_origin(request, call_next):
        origin = request.headers.get("origin")
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and origin
            and origin != str(request.base_url).rstrip("/")
        ):
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "请从本地工作台提交操作。"}, status_code=403)
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    access, legacy = install_workspace_api(app, manager, workspace, data_dir, live_enabled, cleaner)

    def mode_check(mode):
        if mode == "live" and not live_enabled:
            raise HTTPException(400, "当前服务未启用真实模型；请使用本地演示模式。")

    def get_job(job_id):
        service = access.get()
        if service is not None and service.has_run(job_id):
            return service.run(job_id)
        try:
            return manager.get(job_id)
        except KeyError:
            raise HTTPException(404, "未找到这次运行。") from None

    def submit(kind, request, timeout):
        try:
            return manager.submit(kind, request, timeout=timeout)
        except BusyError as exc:
            raise HTTPException(409, str(exc)) from None

    def selected_workspace(engine, repository=None):
        if not repository:
            return knowledge if engine == "knowledge" else workspace
        if engine != "repository":
            raise HTTPException(422, "导入仓库请使用仓库分析任务类型。")
        job = get_job(repository)
        if job["kind"] != "repository_import" or job["status"] != "completed":
            raise HTTPException(422, "仓库尚未成功导入，请先获取源码。")
        source = manager.path(repository) / "source"
        if not source.is_dir() or source.is_symlink():
            raise HTTPException(422, "已导入的源码目录不可用。")
        return source.resolve()

    def memory_store(engine, mode, repository=None):
        return MemoryStore(
            data_dir / "memory.sqlite3",
            MemoryScope.for_workspace(
                selected_workspace(engine, repository), engine=engine, mode=mode
            ),
            cleaner,
        )

    def report_paths():
        paths = {}
        for root in {ROOT / "runs", data_dir / "evaluations"}:
            for path in sorted(root.glob("*/report.json")):
                identifier = sha256(str(path.resolve()).encode()).hexdigest()[:20]
                paths[identifier] = path
        return paths

    @app.get("/api/config")
    def config():
        return {
            "workspace": str(workspace),
            "workspace_name": workspace.name,
            "workspace_id": access.get().store.workspace_id if access.get() else None,
            "storage_namespace": sha256(str(data_dir.resolve()).encode()).hexdigest(),
            "live_enabled": bool(live_enabled),
            "default_mode": "offline",
            "concurrency": 1,
            "github_import": True,
            "task_api": True,
            "legacy_write_enabled": False,
            "task_execution": {"repository_memory_off": True, "knowledge": True, "memory": True},
        }

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/repositories")
    def repositories():
        imported = [
            item["result"]["repository"]
            for item in manager.list()
            if item["kind"] == "repository_import"
            and item["status"] == "completed"
            and (manager.path(item["id"]) / "source").is_dir()
        ]
        return cleaner.clean([{"id": "", "name": workspace.name, "url": None}, *imported])

    @app.post("/api/repositories/import", status_code=202)
    def import_github(body: RepositoryRequest):
        try:
            _, _, url = github_repository(body.url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return submit("repository_import", {"url": url}, 90)

    @app.post("/api/chat")
    def chat():
        from app.workspace.service import Problem
        raise Problem("legacy_write_disabled", "旧发送入口已停止写入，请改用任务入口；旧历史仍可只读查看。", 410)

    @app.get("/api/jobs")
    def jobs():
        found = {job["id"]: job for job in manager.list()}
        service = access.get()
        if service is not None:
            found.update({run["id"]: run for run in service.runs()})
        return cleaner.clean(sorted(found.values(), key=lambda job: job["created_at"], reverse=True))

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return cleaner.clean(get_job(job_id))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        current = get_job(job_id)
        if current["status"] != "running":
            return cleaner.clean(current)
        return manager.cancel(job_id)

    def trace_records(job_id):
        get_job(job_id)
        records = []
        for path in sorted((manager.directory / job_id / "traces").glob("*.jsonl")):
            with path.open("rb") as handle:
                for line in handle:
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        # Preserve complete lines even if the final UTF-8 character is partial.
                        break
        return records

    @app.get("/api/jobs/{job_id}/trace")
    def trace(job_id: str):
        return cleaner.clean(trace_records(job_id))

    @app.get("/api/jobs/{job_id}/export")
    def export(job_id: str):
        job = get_job(job_id)
        if job["status"] not in TERMINAL:
            raise HTTPException(409, "请在运行结束或停止后导出结果。")
        directory = manager.directory / job_id
        bundle = BytesIO()
        with ZipFile(bundle, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "run.json", json.dumps(cleaner.clean(job), ensure_ascii=False, indent=2)
            )
            if job["kind"] == "agent":
                result = job.get("result", {})
                archive.writestr("answer.txt", cleaner.clean(result.get("answer", "")))
                archive.writestr(
                    "status.txt",
                    "状态："
                    + job["status"]
                    + "\n停止原因："
                    + result.get("stop_reason", job.get("stop_reason", "unknown"))
                    + "\n生成预览不代表已确认的最终回答。\n",
                )
                for pattern in ("traces/*.jsonl", "reports/*.md", "answer-preview.jsonl"):
                    for path in directory.glob(pattern):
                        if path.is_file() and not path.is_symlink():
                            archive.writestr(
                                path.relative_to(directory).as_posix(),
                                cleaner.clean(path.read_text(encoding="utf-8")),
                            )
            elif job["kind"] == "evaluation":
                output = Path(job["request"]["output"])
                for name in ("manifest.json", "report.json", "report.txt"):
                    path = output / name
                    if path.is_file():
                        archive.writestr(name, cleaner.clean(path.read_text(encoding="utf-8")))
        return Response(
            bundle.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="projectpilot-{job_id[:8]}.zip"'
            },
        )

    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: str, request: Request):
        get_job(job_id)

        async def stream():
            seen, previous = 0, None
            last_answer = request.headers.get("last-event-id", "").removeprefix("answer-")
            answer_sequence = int(last_answer) if last_answer.isdigit() else 0
            while not await request.is_disconnected():
                current = get_job(job_id)
                records = trace_records(job_id)
                for record in records[seen:]:
                    payload = {
                        key: record[key]
                        for key in ("event", "name", "iteration", "elapsed_ms", "stop_reason")
                        if key in record
                    }
                    yield (
                        "event: progress\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                    )
                seen = len(records)
                preview = latest_preview(manager.directory / job_id / "answer-preview.jsonl")
                if preview and preview["sequence"] > answer_sequence:
                    answer_sequence = preview["sequence"]
                    yield (
                        f"id: answer-{answer_sequence}\nevent: answer\ndata: "
                        + json.dumps(cleaner.clean(preview), ensure_ascii=False)
                        + "\n\n"
                    )
                fingerprint = current["status"]
                if fingerprint != previous:
                    yield (
                        "event: status\ndata: "
                        + json.dumps(cleaner.clean(current), ensure_ascii=False)
                        + "\n\n"
                    )
                    previous = fingerprint
                if current["status"] in TERMINAL:
                    yield (
                        "event: done\ndata: "
                        + json.dumps(cleaner.clean(current), ensure_ascii=False)
                        + "\n\n"
                    )
                    break
                yield ": keepalive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/sessions")
    def sessions(engine: Engine = "session", mode: Mode = "offline", repository: str | None = None):
        return cleaner.clean(legacy.scope(engine, mode, repository))

    @app.get("/api/sessions/{session_id}")
    def session(session_id: str, engine: Engine = "session", mode: Mode = "offline", repository: str | None = None):
        return cleaner.clean(legacy.read_scoped(session_id, engine, mode, repository))

    @app.get("/api/documents")
    def documents():
        return [
            {"name": path.name, "bytes": path.stat().st_size}
            for path in sorted(knowledge.iterdir())
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.lower() in {".md", ".txt", ".pdf"}
        ]

    @app.put("/api/documents/{name}", status_code=201)
    async def upload(name: str, request: Request):
        if (
            not re.fullmatch(r"[^/\\:\x00]{1,100}\.(?:txt|md|pdf)", name, re.I)
            or name.startswith(".")
            or ".." in name
        ):
            raise HTTPException(422, "请使用普通文件名，支持 TXT、Markdown、PDF。")
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 4 * 1024 * 1024:
                raise HTTPException(413, "单份资料最多4 MB。")
        if not content:
            raise HTTPException(422, "文件没有内容。")
        if Path(name).suffix.lower() in {".md", ".txt"}:
            try:
                content.decode("utf-8-sig")
            except UnicodeError:
                raise HTTPException(422, "文本文件需要使用UTF-8编码。") from None
        try:
            with (knowledge / name).open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            raise HTTPException(409, "已有同名资料，请改名后导入。") from None
        return {"name": name, "bytes": len(content)}

    @app.get("/api/documents/{name}")
    def document(name: str):
        try:
            parsed = parse_document(knowledge, name)
        except (OSError, ValueError) as exc:
            raise HTTPException(422, "资料暂时无法预览：" + type(exc).__name__) from None
        return {
            "name": parsed.source,
            "text": parsed.text[:20000],
            "truncated": len(parsed.text) > 20000,
            "pages_without_text": parsed.pages_without_text,
        }

    @app.get("/api/evaluation-cases")
    def evaluation_cases():
        data = read(ROOT / "evals/datasets/agent_dev/tasks_v2.json")
        return [
            {"id": case["case_id"], "objective": case["objective"], "tags": case["tags"]}
            for case in data["cases"]
        ]

    @app.post("/api/evaluations/run", status_code=202)
    def run_evaluation(body: EvaluationRequest):
        mode_check(body.mode)
        valid = {case["id"] for case in evaluation_cases()}
        if (
            set(body.cases) - valid
            or len(body.cases) != len(set(body.cases))
            or len(body.configs) != len(set(body.configs))
        ):
            raise HTTPException(422, "请选择有效且不重复的题目和配置。")
        output = data_dir / "evaluations" / os.urandom(12).hex()
        return submit(
            "evaluation",
            {**body.model_dump(), "output": str(output)},
            min(21600, (len(body.cases) or len(valid)) * len(body.configs) * body.repeats * 240),
        )

    @app.get("/api/evaluations")
    def evaluations():
        result = []
        for identifier, path in report_paths().items():
            report = read(path)
            if "summary" in report:
                result.append(
                    {"id": identifier, "name": path.parent.name, "summary": report["summary"]}
                )
        return result

    @app.get("/api/evaluations/{identifier}")
    def evaluation(identifier: str):
        path = report_paths().get(identifier)
        if path is None:
            raise HTTPException(404, "未找到评测报告。")
        return cleaner.clean(read(path))

    @app.get("/api/memories")
    def memories(
        engine: Engine = "session",
        mode: Mode = "offline",
        history: bool = False,
        repository: str | None = None,
    ):
        return cleaner.clean(
            [
                asdict(record)
                for record in memory_store(engine, mode, repository).list(include_history=history)
            ]
        )

    @app.post("/api/memories/{memory_id}/revoke")
    def revoke(
        memory_id: str,
        engine: Engine = "session",
        mode: Mode = "offline",
        repository: str | None = None,
    ):
        if any(job["status"] == "running" for job in manager.list()):
            raise HTTPException(409, "请在当前运行结束后撤回记忆。")
        try:
            return asdict(memory_store(engine, mode, repository).revoke(memory_id))
        except (ValueError, KeyError):
            raise HTTPException(404, "未找到当前作用域中的这条记忆。") from None

    @app.get("/api/backlog")
    def backlog():
        return read(Path(__file__).with_name("backlog.json"))

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
