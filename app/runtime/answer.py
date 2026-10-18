"""Narrow, source-bound output checks and one budgeted rewrite before persistence."""

import re
from dataclasses import asdict, dataclass, replace

from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.registry import ToolRegistry

from app.runtime.citations import citation_violations, current_knowledge_evidence


class AnswerValidationError(RuntimeError):
    pass


@dataclass
class AnswerContract:
    sections: tuple[str, str] | None = None
    ending: str | None = None
    knowledge: bool = False

    @classmethod
    def from_sources(cls, texts, *, knowledge=False):
        contract = cls(knowledge=knowledge)
        for text in texts:
            for sentence in re.split(r"[。！？\n]", text):
                # This is deliberately a literal grammar, not a semantic instruction extractor.
                if re.search(r"如果|假设|示例|引用|原文|资料说|文档说|他说", sentence):
                    continue
                if re.search(r"取消.*格式|不再.*(?:格式|两段)|不(?:要|必|需要).*按", sentence):
                    contract.sections = None
                    continue
                match = re.search(
                    r'按[“"]([^”"\n]{1,24})[”"]\s*[、和与]?\s*[“"]([^”"\n]{1,24})[”"]两段(?:作答|回答)',
                    sentence,
                )
                if match and match[1] != match[2]:
                    contract.sections = (match[1], match[2])
                if re.search(r"取消.*结尾|不再.*(?:结尾|标记)", sentence):
                    contract.ending = None
                match = re.search(r'最后必须(?:写|加)[“"]([^”"\n]{1,80})[”"]', sentence)
                if match:
                    contract.ending = match[1]
        return contract

    def violations(self, answer):
        issues = []
        if self.sections:
            headings = []
            for label in self.sections:
                pattern = (
                    rf"(?m)^\s*(?:#{{1,6}}\s*)?(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*(?:[：:]|$)"
                )
                headings.append(list(re.finditer(pattern, answer)))
            if (
                any(len(matches) != 1 for matches in headings)
                or headings[0][0].start() >= headings[1][0].start()
            ):
                issues.append("按要求依次保留两个命名部分，不能缺失或重复标签。")
            else:
                prefix = answer[: headings[0][0].start()].strip()
                # One short standalone title is allowed, substantive prefaces are not.
                if prefix and (
                    "\n" in prefix or len(prefix) > 40 or re.search(r"[。！？：:]", prefix)
                ):
                    issues.append("删除两部分之前的说明正文；必要内容放入指定部分。")
        if self.ending:
            ends_correctly = answer.rstrip().endswith(self.ending)
            prefix = answer.rstrip().splitlines()[-1][: -len(self.ending)] if ends_correctly else ""
            promised = re.search(
                r"(?:需|须|应|要求|末尾|结尾).{0,16}(?:写|添加|加上|附上|保留)", prefix
            )
            if not ends_correctly or promised:
                issues.append(
                    f"本次答案末尾直接写出原文：{self.ending}；不能只说以后需要写这个标记。"
                )
        if self.knowledge:
            unsupported = re.search(
                r"(?:唯一|最新)(?:的)?(?:相关)?(?:的)?(?:资料|文档|片段|原文|来源|版本)", answer
            )
            if unsupported:
                issues.append(
                    f"原回答中的“{unsupported[0]}”没有唯一性或时效性依据。"
                    "请改成中性表述“当前检索到的相关资料”，不再保留“唯一”或“最新”的评价；"
                    "保留原本有依据的事实、无法确认的结论和引用。"
                )
        return issues


class AnswerRunner(AgentRunner):
    """Keep the upstream request, usage and save path; do not trim model answers."""

    def __init__(self, trace, *, knowledge=False):
        super().__init__()
        self.trace, self.knowledge = trace, knowledge
        self.user_sources = []
        self.summary_sources = []
        self.correction_used = False
        self.observed_usage = None

    async def _request_model(self, spec, messages, hook, context, *, request_state, **kwargs):
        response, usage = await super()._request_model(
            spec, messages, hook, context, request_state=request_state, **kwargs
        )
        self.observed_usage = self._merge_usage(self.observed_usage, usage)
        if response.tool_calls or response.finish_reason != "stop":
            return response, usage
        sent = request_state.messages
        # Only rules actually visible to this request are checked. A window baseline
        # must not recover discarded constraints through this separate path.
        sources = [s for s in self.summary_sources if s in sent[0].get("content", "")]
        sources += [
            s
            for s in self.user_sources
            if any(m.get("role") == "user" and s in str(m.get("content", "")) for m in sent)
        ]
        contract = AnswerContract.from_sources(sources, knowledge=self.knowledge)
        evidence = current_knowledge_evidence(sent) if self.knowledge else []
        issues = contract.violations(response.content or "")
        if self.knowledge:
            issues.extend(citation_violations(response.content or "", evidence))
        if not issues:
            return response, usage
        self.trace.emit(
            "answer.rejected", draft=response.content, issues=issues, contract=asdict(contract)
        )
        if self.correction_used:
            raise AnswerValidationError("Output still violates the bounded answer contract")
        self.correction_used = True
        repair_messages = [
            *sent,
            {"role": "assistant", "content": response.content or ""},
            {
                "role": "system",
                "content": "本回合仅允许一次最终回答纠正。\n"
                + "\n".join(issues)
                + "\n保留有依据的结论和引用，不增加事实，不复述规则，不使用工具。",
            },
            {"role": "user", "content": "请根据原始任务和已有证据，直接给出纠正后的完整回答。"},
        ]
        self.trace.emit("answer.correction.started", max_attempts=1)
        corrected, repair_usage = await super()._request_model(
            replace(spec, tools=ToolRegistry()),
            repair_messages,
            hook,
            context,
            request_state=request_state,
            **kwargs,
        )
        self.observed_usage = self._merge_usage(self.observed_usage, repair_usage)
        combined = self._merge_usage(usage, repair_usage)
        remaining = contract.violations(corrected.content or "")
        if self.knowledge:
            remaining.extend(citation_violations(corrected.content or "", evidence))
        self.trace.emit(
            "answer.correction",
            content=corrected.content,
            issues=remaining,
            finish_reason=corrected.finish_reason,
            usage=asdict(repair_usage),
        )
        if corrected.finish_reason == "error":
            return corrected, combined
        if corrected.tool_calls or corrected.finish_reason != "stop" or remaining:
            raise AnswerValidationError("One correction did not produce a valid final answer")
        return corrected, combined
