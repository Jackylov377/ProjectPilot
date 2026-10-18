"""A deliberately small, read-only tool for the first runtime milestone."""

import json
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult


class ReadProjectNote(Tool):
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    @property
    def name(self) -> str:
        return "read_project_note"

    @property
    def description(self) -> str:
        return "Read a trusted demo project's overview or stack note. Returns source and text."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"section": {"type": "string", "enum": ["overview", "stack"]}},
            "required": ["section"],
            "additionalProperties": False,
        }

    async def execute(self, section: str, **kwargs: Any) -> str:
        if section not in {"overview", "stack"}:
            return ToolResult.error("Error: section must be overview or stack")
        path = (self.workspace / f"{section}.md").resolve()
        if not path.is_relative_to(self.workspace):
            return ToolResult.error("Error: path outside allowed directory")
        try:
            # Bound the observation before decoding; the model cannot pick arbitrary files.
            with path.open("rb") as handle:
                raw = handle.read(8193)
            if len(raw) > 8192:
                return ToolResult.error("Error: demo note exceeds 8192 bytes")
            text = raw.decode("utf-8")
        except (OSError, UnicodeError):
            return ToolResult.error("Error: demo note is unavailable or is not UTF-8")
        return json.dumps({"source": path.name, "text": text}, ensure_ascii=False)
