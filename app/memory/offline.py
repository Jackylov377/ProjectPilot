"""Scripted memory fixtures, strictly for offline integration checks."""

import json
import re

from nanobot.providers.base import LLMResponse

from app.memory.service import EXTRACTION_PROMPT, SNAPSHOT_END, SNAPSHOT_START
from app.runtime.offline import OfflineDemoProvider, offline_checkpoint


class OfflineMemoryProvider(OfflineDemoProvider):
    def get_default_model(self):
        return "scripted-memory-demo"

    async def chat(self, messages, **kwargs):
        if checkpoint := offline_checkpoint(messages):
            return checkpoint
        if messages[0]["content"] == EXTRACTION_PROMPT:
            candidates = []
            for statement in json.loads(messages[-1]["content"]):
                index, text = statement["id"], statement["text"]
                match = re.search(r"(?:使用|改用|选用)(模型\s*[A-Z])", text)
                if match:
                    candidates.append(
                        {
                            "statement": index,
                            "key": "project.model",
                            "value": match.group(1),
                            "explicit": True,
                        }
                    )
                language = re.search(r"(?:用|使用)(中文|英文)(?:回答|回复)", text)
                if language:
                    candidates.append(
                        {
                            "statement": index,
                            "key": "preference.answer_language",
                            "value": language.group(1),
                            "explicit": True,
                        }
                    )
            return LLMResponse(content=json.dumps({"candidates": candidates}, ensure_ascii=False))
        current = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        payload = json.loads(current.split(SNAPSHOT_START, 1)[1].split(SNAPSHOT_END, 1)[0])
        records = payload["records"]
        if payload["extraction_status"] in {"failed", "pending"}:
            answer = "本次未保存长期记忆。"
        elif records:
            answer = "；".join(
                f"{m['value']}（来源：会话 {m['source']['session_id']}）" for m in records
            )
        else:
            answer = "本次没有召回相关有效记忆，无法确认。"
        return LLMResponse(content="离线预设响应：" + answer)
