"""One local tool call over stdin/stdout; no model, shell or session lifecycle."""

import asyncio
import json
import sys
from pathlib import Path

from app.tools.list_files import ListFiles
from app.tools.read_file import ReadFile
from app.tools.search_code import SearchCode
from app.tools.write_report import WriteReport

TOOLS = {
    "list_files": ListFiles,
    "read_file": ReadFile,
    "search_code": SearchCode,
    "write_report": WriteReport,
}


def main():
    request = json.loads(sys.stdin.buffer.read())
    try:
        tool_type = TOOLS[request["name"]]
        options = {"append_to": request.get("append_report")} if tool_type is WriteReport else {}
        if tool_type is ListFiles:
            options = {"max_depth": request.get("list_depth", 1)}
        tool = tool_type(Path(request["root"]), **options)
        result = asyncio.run(tool.execute(**request["params"]))
        response = {"content": str(result), "is_error": bool(getattr(result, "is_error", False))}
    except Exception as exc:
        response = {"content": f"Error: {exc}", "is_error": True}
    sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
