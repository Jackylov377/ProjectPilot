"""Deterministic responses for plumbing checks, NOT a real model or benchmark."""

import json
from typing import Any
from uuid import uuid4

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


def offline_checkpoint(messages):
    """Explicit-label fixture only; it does not evaluate real summarization quality."""
    if not str(messages[0].get("content", "")).startswith("PROJECTPILOT_TASK_CHECKPOINT_V1"):
        return None
    labels = {
        "任务目标：": "goal",
        "已确认：": "decisions",
        "已完成：": "completed",
        "待办：": "pending",
        "未解决：": "unresolved",
    }
    items = []
    for statement in json.loads(messages[-1]["content"]):
        for label, category in labels.items():
            if statement["quote"].startswith(label):
                items.append({"id": statement["id"], "category": category})
                break
    return LLMResponse(content=json.dumps({"items": items}, ensure_ascii=False))


class OfflineDemoProvider(LLMProvider):
    def __init__(self):
        super().__init__(provider_name="offline-demo")

    def get_default_model(self) -> str:
        return "scripted-offline-demo"

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        if checkpoint := offline_checkpoint(messages):
            return checkpoint
        # Only observe tools from the current turn; old tool results are session history.
        last_user = max(
            (i for i, message in enumerate(messages) if message.get("role") == "user"), default=-1
        )
        observations = [
            message for message in messages[last_user + 1 :] if message.get("role") == "tool"
        ]
        if not observations:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id=f"demo-call-{sum(message.get('role') == 'user' for message in messages)}",
                        name="read_project_note",
                        arguments={"section": "overview"},
                    )
                ],
            )
        payload = observations[-1]["content"]
        try:
            note = json.loads(payload)
        except (ValueError, TypeError):
            return LLMResponse(content="离线演示：工具未能读取资料，无法确认项目内容。")
        return LLMResponse(
            content=f"离线演示（预设响应）：{note['text'].strip()}\n来源：{note['source']}"
        )


class OfflineKnowledgeProvider(OfflineDemoProvider):
    """Retrieve once, then quote observations; never present this as model reasoning."""

    def get_default_model(self):
        return "scripted-knowledge-demo"

    async def chat(self, messages, tools=None, **kwargs):
        if checkpoint := offline_checkpoint(messages):
            return checkpoint
        last_user = max(i for i, message in enumerate(messages) if message.get("role") == "user")
        observations = [
            message for message in messages[last_user + 1 :] if message.get("role") == "tool"
        ]
        if not observations:
            if not tools:
                return LLMResponse(content="离线演示：预算耗尽，当前资料不足，无法确认。")
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id=f"knowledge-{last_user}",
                        name="search_knowledge",
                        arguments={"query": messages[last_user]["content"][:512], "top_k": 3},
                    )
                ],
            )
        payload = json.loads(observations[-1]["content"])
        extraction_note = payload.get("extraction_note", "")
        if not payload["results"]:
            return LLMResponse(
                content="离线演示（预设响应）：当前资料不足，无法确认。未发现词面匹配片段。"
                + extraction_note
            )
        excerpts = [f"{hit['text'].strip()}\n来源：{hit['citation']}" for hit in payload["results"]]
        return LLMResponse(
            content="离线演示（预设摘录，不是模型回答质量评测）：\n"
            + "\n\n".join(excerpts)
            + "\n"
            + extraction_note
        )


class OfflineRepositoryProvider(OfflineDemoProvider):
    """A fixed repository workflow, using real observations; not model reasoning."""

    def get_default_model(self) -> str:
        return "scripted-repository-demo"

    async def chat(self, messages, tools=None, **kwargs):
        if checkpoint := offline_checkpoint(messages):
            return checkpoint
        if tools is None:
            return LLMResponse(content="执行预算已耗尽，仓库分析未完成。")
        last_user = max(i for i, message in enumerate(messages) if message.get("role") == "user")
        observations = [
            message for message in messages[last_user + 1 :] if message.get("role") == "tool"
        ]
        step = len(observations)
        if observations:
            try:
                latest = json.loads(observations[-1]["content"])
            except ValueError:
                return LLMResponse(content="离线预设流程收到工具错误，任务未完成；请检查 Trace。")
        if step == 0:
            name, arguments = "list_files", {"path": "."}
        elif step == 1:
            name, arguments = "read_file", {"path": "README.md", "max_lines": 12}
        elif step == 2:
            name, arguments = "search_code", {"query": "def ", "max_matches": 3}
        elif step == 3:
            if not latest["matches"]:
                return LLMResponse(content="离线预设流程未找到 Python 函数，任务未完成。")
            match = latest["matches"][0]
            name, arguments = (
                "read_file",
                {"path": match["source"], "start_line": max(1, match["line"] - 2), "max_lines": 8},
            )
        elif step == 4:
            note = json.loads(observations[1]["content"])
            content = (
                "# 离线链路验证报告（预设响应）\n\n"
                "本报告由固定流程整理真实工具观察，不代表模型完成了项目分析。\n\n"
                f"## README 摘录\n来源：{note['source']}:{note['start_line']}\n\n```text\n{note['content']}\n```\n\n"
                f"## 代码摘录\n来源：{latest['source']}:{latest['start_line']}\n\n```text\n{latest['content']}\n```\n"
            )
            name, arguments = (
                "write_report",
                {"filename": f"offline-{uuid4().hex[:12]}.md", "content": content},
            )
        else:
            return LLMResponse(
                content=f"离线预设工具链已完成，报告已保存：{latest['path']}。这不是模型能力评测。"
            )
        turn = sum(message.get("role") == "user" for message in messages)
        return LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(id=f"repo-{turn}-{step}", name=name, arguments=arguments)],
        )
