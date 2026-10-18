"""Save Markdown reports in an application-selected output directory."""

import json
import re
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult


class WriteReport(Tool):
    def __init__(self, report_dir: Path, append_to: str | None = None):
        self.report_dir = report_dir.resolve()
        self.append_to = append_to

    @property
    def name(self) -> str:
        return "write_report"

    @property
    def description(self) -> str:
        if self.append_to:
            return (
                f"Append a Markdown section to the existing document {self.append_to}; "
                "filename must equal this application-selected name. Preserve existing content. "
                "Include source paths and line numbers. Saving does not verify factual claims."
            )
        return (
            "Save a Markdown report in the application-selected report directory. "
            "Filename must be a simple name such as project-summary.md, using ASCII letters, "
            "digits, hyphens or underscores, starting with a letter or digit. "
            "Existing reports are never overwritten; choose a new name instead. "
            "Include source file paths and line numbers for factual claims. "
            "This tool saves content; it does not verify claims or citations."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "minLength": 4, "maxLength": 83},
                "content": {"type": "string", "minLength": 1, "maxLength": 50000},
            },
            "required": ["filename", "content"],
            "additionalProperties": False,
        }

    async def execute(self, filename: str, content: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.md", filename):
            return ToolResult.error("Error: use a simple Markdown filename such as summary.md")
        if self.append_to and filename != self.append_to:
            return ToolResult.error(f"Error: append target is fixed to {self.append_to}")
        try:
            target = self.report_dir / filename
            if self.append_to:
                content = "\n\n" + content
                # r+ requires an existing document; append mode never creates a new report.
                with target.open("r+", encoding="utf-8", newline="\n") as handle:
                    handle.seek(0, 2)
                    handle.write(content)
            else:
                self.report_dir.mkdir(parents=True, exist_ok=True)
                with target.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
            return json.dumps(
                {
                    "filename": filename,
                    "path": str(target),
                    "bytes_written": len(content.encode("utf-8")),
                    "mode": "append" if self.append_to else "create",
                },
                ensure_ascii=False,
            )
        except FileExistsError:
            return ToolResult.error("Error: report already exists; choose a new filename")
        except OSError as exc:
            return ToolResult.error(f"Error: {exc}")


def main() -> int:
    import argparse
    import asyncio

    from nanobot.agent.tools.registry import ToolRegistry, is_tool_error_result

    parser = argparse.ArgumentParser(description="把 Markdown 内容保存到指定报告目录")
    parser.add_argument("filename")
    parser.add_argument("--content-file", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, default=Path("runs/reports"))
    args = parser.parse_args()
    tools = ToolRegistry()
    tools.register(WriteReport(args.report_dir))
    result = asyncio.run(
        tools.execute(
            "write_report",
            {"filename": args.filename, "content": args.content_file.read_text(encoding="utf-8")},
        )
    )
    print(
        result
        if is_tool_error_result(result)
        else json.dumps(json.loads(result), ensure_ascii=False, indent=2)
    )
    return 1 if is_tool_error_result(result) else 0


if __name__ == "__main__":
    raise SystemExit(main())
