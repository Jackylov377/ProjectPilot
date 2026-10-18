"""Reuse nanobot's MCP discovery/wrapper lifecycle for a fixed local service."""

import asyncio
import math
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.mcp import connect_mcp_servers
from nanobot.config.schema import MCPServerConfig

MCP_TOOL_NAME = "mcp_repository_project_info"


class TracedMCPTool(Tool):
    def __init__(self, tool, trace, timeout):
        self.tool, self.trace, self.timeout = tool, trace, timeout

    @property
    def name(self):
        return self.tool.name

    @property
    def description(self):
        return self.tool.description

    @property
    def parameters(self):
        return {**self.tool.parameters, "additionalProperties": False}

    @property
    def exclusive(self):
        return True

    async def execute(self, **params):
        started = time.perf_counter()
        result = None
        self.trace.emit("mcp.call_started", name=self.name)
        try:
            async with asyncio.timeout(self.timeout):
                result = await self.tool.execute(**params)
                return result
        except TimeoutError:
            result = ToolResult.error("Error: MCP call timed out")
            return result
        finally:
            self.trace.emit(
                "mcp.call_finished",
                name=self.name,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                is_error=bool(getattr(result, "is_error", False)),
                result=str(result) if result is not None else None,
            )


@asynccontextmanager
async def repository_mcp(workspace, registry, trace, timeout=15, *, server_args=None):
    """Discover one allowlisted tool; always close its owning MCP connection."""
    args = server_args or ["-m", "app.mcp.server", "--workspace", str(workspace.resolve())]
    config = MCPServerConfig(
        type="stdio",
        command=sys.executable,
        args=args,
        cwd=str(Path(__file__).resolve().parents[2]),
        env={"PYTHONUTF8": "1"},
        tool_timeout=max(1, math.ceil(timeout)),
        enabled_tools=["project_info"],
    )
    connections = {}
    trace.emit("mcp.connecting", server="repository", transport="stdio")
    try:
        async with asyncio.timeout(15):
            connections = await connect_mcp_servers({"repository": config}, registry)
        if "repository" not in connections or not registry.has(MCP_TOOL_NAME):
            raise RuntimeError("Local repository MCP server failed to initialize")
        registry.register(TracedMCPTool(registry.get(MCP_TOOL_NAME), trace, timeout))
        trace.emit(
            "mcp.connected", server="repository", tools=[registry.get(MCP_TOOL_NAME).to_schema()]
        )
        yield
    finally:
        try:
            for connection in connections.values():
                await connection.aclose()
        finally:
            registry.unregister(MCP_TOOL_NAME)
            trace.emit("mcp.closed", server="repository", connections=len(connections))
