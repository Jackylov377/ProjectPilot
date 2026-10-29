"""Execute one application turn outside the HTTP server's process-global state."""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from nanobot.providers.openai_compat_provider import OpenAICompatProvider

from app.api.diagnostics import failure_details
from app.api.jobs import ROOT, read, write
from app.api.streaming import AnswerPreview, PreviewHook, StreamingTrace
from app.context import ContextOptions
from app.memory.offline import OfflineMemoryProvider
from app.memory.service import MemoryOptions
from app.rag.pipeline import retrieval_secrets
from app.runtime.knowledge import KnowledgeOptions
from app.runtime.offline import (
    OfflineDemoProvider,
    OfflineKnowledgeProvider,
    OfflineRepositoryProvider,
)
from app.runtime.repository import RepositoryOptions
from app.runtime.session import run_session, state_location


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
    trace = StreamingTrace(directory / "traces", preview, secrets=retrieval_secrets())
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
        turn = await run_session(
            provider,
            workspace,
            trace,
            state_dir=state_location(
                Path(options["data_dir"]) / "sessions" / engine,
                workspace,
                options["mode"],
            ),
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
        write(directory / "result.json", trace.clean(result))
        client = getattr(provider, "_client", None)
        if client:
            await client.close()


if __name__ == "__main__":
    asyncio.run(execute(Path(sys.argv[1]).resolve()))
