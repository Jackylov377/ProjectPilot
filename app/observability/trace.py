"""JSONL traces with run IDs, lifecycle events and configured-secret redaction."""

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from nanobot.agent.hook import AgentHook, AgentHookContext


class JsonlTrace:
    def __init__(self, directory: Path, secrets: tuple[str, ...] = ()):
        directory.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid4().hex
        self.path = directory / f"{self.run_id}.jsonl"
        self._secrets = tuple(secret for secret in secrets if secret)
        self._start = time.perf_counter()
        self._sequence = 0
        self.path.touch(exist_ok=False)

    def clean(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "[REDACTED]"
                if key.lower()
                in {
                    "api_key",
                    "authorization",
                    "password",
                    "token",
                    "secret",
                    "reasoning_content",
                    "thinking_blocks",
                    "provider_state",
                }
                else self.clean(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
        return value

    def emit(self, event: str, **data: Any) -> None:
        self._sequence += 1
        record = {
            "schema_version": 1,
            "run_id": self.run_id,
            "sequence": self._sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": round((time.perf_counter() - self._start) * 1000, 3),
            "event": event,
            **data,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self.clean(record), ensure_ascii=False) + "\n")


class TraceHook(AgentHook):
    def __init__(self, trace: JsonlTrace):
        super().__init__(reraise=True)
        self.trace = trace

    async def before_iteration(self, context: AgentHookContext) -> None:
        self.trace.emit("iteration.started", iteration=context.iteration, messages=context.messages)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        self.trace.emit(
            "tools.requested",
            iteration=context.iteration,
            calls=[call.to_openai_tool_call() for call in context.tool_calls],
        )

    async def before_execute_tool(self, context, tool_call, tool, params) -> None:
        self.trace.emit("tool.started", call_id=tool_call.id, name=tool_call.name, arguments=params)

    async def after_execute_tool(self, context, tool_call, tool, params, result) -> None:
        self.trace.emit(
            "tool.finished",
            call_id=tool_call.id,
            name=tool_call.name,
            result=str(result),
            is_error=bool(getattr(result, "is_error", False)),
        )

    async def on_execute_tool_error(self, context, tool_call, tool, params, error) -> None:
        self.trace.emit("tool.error", call_id=tool_call.id, name=tool_call.name, error=str(error))

    async def after_iteration(self, context: AgentHookContext) -> None:
        response = context.response
        self.trace.emit(
            "iteration.finished",
            iteration=context.iteration,
            content=response.content if response else None,
            finish_reason=response.finish_reason if response else None,
            usage=asdict(response.usage) if response and response.usage else None,
            tool_events=context.tool_events,
        )
