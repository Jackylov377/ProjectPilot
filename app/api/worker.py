"""Execute one application turn outside the HTTP server's process-global state."""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from nanobot.providers.openai_compat_provider import OpenAICompatProvider

from app.api.diagnostics import failure_details
from app.api.jobs import ROOT, read, usage_from_trace, write
from app.api.streaming import AnswerPreview, PreviewHook, StreamingTrace
from app.context import ContextOptions
from app.memory.offline import OfflineMemoryProvider
from app.memory.service import MemoryOptions
from app.memory.store import MemoryScope
from app.rag.pipeline import retrieval_secrets
from app.runtime.knowledge import KnowledgeOptions
from app.runtime.offline import (
    OfflineDemoProvider,
    OfflineKnowledgeProvider,
    OfflineRepositoryProvider,
)
from app.runtime.repository import RepositoryOptions
from app.runtime.session import run_session, state_location
from app.workspace import WorkspaceStore
from app.workspace.service import task_state_path


def failure_events(trace):
    events = []
    try:
        lines = trace.path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}, {}
    for line in lines:
        try:
            record = json.loads(line)
            if isinstance(record, dict):
                events.append(record)
        except ValueError:
            continue
    finished = next(
        (event for event in reversed(events) if event.get("event") == "run.finished"),
        {},
    )
    call = next(
        (event for event in reversed(events) if event.get("event") == "llm.call"), {}
    )
    if call.get("finish_reason") != "error":
        call = {}
    return finished, call


async def execute(directory):
    options = read(directory / "job.json")["request"]
    load_dotenv(ROOT / ".env", override=False)
    key = os.environ.get("PROJECTPILOT_API_KEY", "")
    preview = AnswerPreview(directory / "answer-preview.jsonl", secrets=retrieval_secrets())
    trace = StreamingTrace(
        directory / "traces",
        preview,
        secrets=retrieval_secrets(),
        run_id=directory.name,
    )
    provider = None
    result = {"answer": "", "stop_reason": "error", "trace_id": trace.run_id}
    try:
        if options["mode"] == "live":
            if not all(
                os.environ.get(name)
                for name in (
                    "PROJECTPILOT_API_KEY",
                    "PROJECTPILOT_MODEL",
                    "PROJECTPILOT_API_BASE",
                )
            ):
                raise ValueError("模型配置不完整")
            provider = OpenAICompatProvider(
                api_key=key,
                api_base=os.environ["PROJECTPILOT_API_BASE"],
                default_model=os.environ["PROJECTPILOT_MODEL"],
                provider_name="projectpilot-compatible",
                extra_body={"enable_thinking": False},
            )
        else:
            provider_class = {
                "repository": OfflineRepositoryProvider,
                "knowledge": OfflineKnowledgeProvider,
            }.get(
                options["engine"],
                OfflineMemoryProvider if options["memory"] else OfflineDemoProvider,
            )
            provider = provider_class()
        workspace = Path(options["workspace"])
        engine = options["engine"]
        kwargs = {}
        if engine == "repository":
            kwargs["repository_options"] = RepositoryOptions(directory / "reports")
        elif engine == "knowledge":
            kwargs["knowledge_options"] = KnowledgeOptions(
                tuple(options["documents"]),
                retriever=options.get("retriever", "bm25"),
                allow_remote=options["mode"] == "live",
            )
        if options["memory"]:
            kwargs["memory_options"] = MemoryOptions(
                Path(options["data_dir"]) / "memory.sqlite3", mode=options["mode"]
            )
        state = state_location(Path(options["data_dir"]) / "sessions" / engine, workspace, options["mode"])
        if options.get("task_id"):
            metadata = WorkspaceStore(Path(options["data_dir"]) / "workspace.sqlite3")
            task = metadata.get_task(options["task_id"])
            with metadata._connection(write=False) as db:
                admission = db.execute("SELECT envelope_json FROM task_runs WHERE run_id=?", (directory.name,)).fetchone()
                previous = [json.loads(row[0])["workspace"] for row in db.execute(
                    "SELECT envelope_json FROM task_runs WHERE task_id=?", (task.task_id,)).fetchall()]
            if (
                not admission or json.loads(admission[0]) != options
                or options["engine"] != task.task_mode or options["memory"] is not task.memory_enabled
                or options["context_policy"] != task.context_policy
                or options["project_id"] != task.project_id or options["workspace_id"] != metadata.workspace_id
                or options["session"] != task.task_id or options["history_key"] != task.history_key
            ):
                raise ValueError("Task execution does not match durable admission")
            state = task_state_path(options["data_dir"], metadata.workspace_id,
                                    task.project_id, task.task_id, options["mode"])
            identity = f"project:{metadata.workspace_id}:{task.project_id}"
            kwargs["context_identity"] = identity
            kwargs["previous_workspaces"] = tuple(previous)
            if task.task_mode == "knowledge":
                context = options.get("knowledge_context")
                if not context or not context.get("documents"):
                    raise ValueError("Knowledge execution requires a pinned project manifest")
                with metadata._connection(write=False) as db:
                    for item in context["documents"]:
                        row = db.execute("""SELECT v.*,d.name,d.media_type FROM document_versions v
                            JOIN documents d ON v.document_id=d.document_id
                            JOIN run_documents r ON r.version_id=v.version_id AND r.document_id=v.document_id
                            WHERE r.run_id=? AND v.project_id=? AND v.document_id=? AND v.version_id=?""",
                            (directory.name, task.project_id, item["document_id"], item["version_id"])).fetchone()
                        if row is None or any(row[key] != value for key, value in item.items()):
                            raise ValueError("Document manifest does not match durable publication")
                kwargs["knowledge_options"] = KnowledgeOptions(
                    tuple(item["name"] for item in context["documents"]), retriever=context["retriever"],
                    allow_remote=options["mode"] == "live", manifest=tuple(context["documents"]),
                    data_dir=Path(options["data_dir"]), project_workspace_id=identity,
                )
            if task.memory_enabled:
                kwargs["memory_options"] = MemoryOptions(
                    Path(options["data_dir"]) / "project-memory.sqlite3", mode=options["mode"],
                    project_scope=MemoryScope.for_project(metadata.workspace_id, task.project_id, mode=options["mode"]),
                )
        turn = await run_session(
            provider,
            workspace,
            trace,
            state_dir=state,
            session_id=options["session"],
            prompt=options["prompt"],
            max_iterations=10 if engine == "repository" else 6,
            timeout_seconds=90,
            context_options=ContextOptions(options["context_policy"]),
            extra_hooks=(PreviewHook(preview),),
            **kwargs,
        )
        result.update(
            answer=turn.final_content,
            stop_reason=turn.stop_reason,
            restored_messages=turn.restored_messages,
            saved_messages=turn.saved_messages,
            tools_used=turn.tools_used,
            retrieval_warnings=turn.retrieval_warnings,
            reports=[
                {
                    "name": Path(path).name,
                    "text": Path(path).read_text(encoding="utf-8"),
                }
                for path in turn.saved_reports
            ],
        )
        if turn.stop_reason != "completed":
            _, call = failure_events(trace)
            result.update(
                failure_details(
                    stop_reason=turn.stop_reason,
                    error_kind=call.get("error_kind"),
                    status_code=call.get("error_status_code"),
                )
            )
            # Provider error bodies are not final answers and may contain request details.
            result["answer"] = ""
    except Exception as exc:
        finished, call = failure_events(trace)
        stop = finished.get("stop_reason", "error")
        result.update(
            stop_reason=stop,
            exception_type=type(exc).__name__,
            **failure_details(
                stop_reason=stop,
                exception=exc,
                error_kind=call.get("error_kind"),
                status_code=call.get("error_status_code"),
            ),
        )
        trace.emit("worker.failed", **result)
    finally:
        result["usage"] = usage_from_trace(
            directory,
            run_id=trace.run_id,
            mode=options["mode"],
            stop_reason=result["stop_reason"],
        )
        write(directory / "result.json", trace.clean(result))
        client = getattr(provider, "_client", None)
        if client:
            await client.close()


if __name__ == "__main__":
    asyncio.run(execute(Path(sys.argv[1]).resolve()))
