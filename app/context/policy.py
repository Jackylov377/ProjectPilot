"""Full-history and recent-turn baselines with exact pre-request budget checks."""

import json
from copy import deepcopy
from dataclasses import dataclass, replace

from nanobot.agent.context_governance import (
    SNIP_SAFETY_BUFFER,
    ContextGovernor,
    ContextWindowExceededError,
)
from nanobot.providers.base import ProviderCallContext
from nanobot.utils.helpers import estimate_prompt_tokens_chain

CURRENT_TURN = "_projectpilot_current_turn"


@dataclass(frozen=True)
class ContextOptions:
    strategy: str = "full"
    window_tokens: int = 16384
    recent_turns: int | None = None
    max_output_tokens: int | None = None

    def __post_init__(self):
        if self.recent_turns is None:
            object.__setattr__(self, "recent_turns", 2 if self.strategy == "summary" else 4)
        if self.strategy not in {"full", "window", "summary"}:
            raise ValueError("Context strategy must be full, window or summary")
        if self.window_tokens <= SNIP_SAFETY_BUFFER or self.recent_turns < 1:
            raise ValueError("Context window and retained turn count must be positive")
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("Context output reserve must be positive")


class PolicyGovernor(ContextGovernor):
    """Select only the provider-facing copy; leave raw Runner and Session history intact.

    Both upstream request paths (normal and tool-free finalization) call this boundary.
    It deliberately avoids upstream automatic summarization and opaque continuation.
    """

    def __init__(self, options: ContextOptions, trace):
        self.options, self.trace = options, trace
        self._protected_content = None
        self.window_fallback = False

    def _count(self, config, messages, definitions):
        return estimate_prompt_tokens_chain(config.provider, config.model, messages, definitions)

    def _mark_current_turn(self, messages):
        # Accepted request copies carry this marker across iterations. The first
        # request finds the fresh user input; tool-free finalization keeps its tail.
        marked = [i for i, message in enumerate(messages) if message.get(CURRENT_TURN)]
        if marked:
            return marked[-1]
        users = [
            i
            for i, message in enumerate(messages)
            if message.get("role") == "user"
            and (
                self._protected_content is None or message.get("content") == self._protected_content
            )
        ]
        if not users:
            raise ValueError("Context selection requires a current user turn")
        start = users[-1]
        # Upstream may merge adjacent user messages. Mark their first member so
        # the merged model copy still carries the protected-turn boundary.
        while start > 0 and messages[start - 1].get("role") == "user":
            start -= 1
        self._protected_content = deepcopy(messages[start].get("content"))
        messages[start][CURRENT_TURN] = True
        return start

    def _select(self, config, messages, definitions):
        copied = deepcopy(messages)
        self._mark_current_turn(copied)
        prepared = self.prepare_messages_for_model(config, copied)
        current = self._mark_current_turn(prepared)
        starts = [i for i, m in enumerate(prepared[:current]) if m.get("role") == "user"]
        groups = [
            list(range(start, starts[index + 1] if index + 1 < len(starts) else current))
            for index, start in enumerate(starts)
        ]
        omitted = set()

        def omit(group):
            # System instructions remain mandatory even if a historical turn is removed.
            omitted.update(i for i in group if prepared[i].get("role") != "system")

        def selected():
            return [m for i, m in enumerate(prepared) if i not in omitted]

        remaining = list(groups)
        if self.options.strategy == "window" or self.window_fallback:
            # The current, still-running turn counts as one of recent_turns.
            while len(remaining) > self.options.recent_turns - 1:
                omit(remaining.pop(0))
        budget = self.input_budget(config)
        if self.options.strategy in {"window", "summary"}:
            while remaining and self._count(config, selected(), definitions)[0] > budget:
                omit(remaining.pop(0))

        chosen = selected()
        dropped_chunks = 0
        if (
            self.options.strategy in {"window", "summary"}
            and self._count(config, chosen, definitions)[0] > budget
        ):
            # Never cut a JSON string or detach a citation from its source text.
            current = self._mark_current_turn(chosen)
            for message in reversed(chosen[current:]):
                if message.get("role") != "tool" or message.get("name") != "search_knowledge":
                    continue
                try:
                    payload = json.loads(message["content"])
                except (ValueError, TypeError):
                    continue
                if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                    continue
                while payload["results"] and self._count(config, chosen, definitions)[0] > budget:
                    payload["results"].pop()
                    payload["truncated"] = True
                    payload["omitted_count"] = payload.get("omitted_count", 0) + 1
                    payload["context_note"] = (
                        "部分片段因本次上下文预算省略；仅使用当前返回依据，必要时缩小问题再检索。"
                    )
                    message["content"] = json.dumps(payload, ensure_ascii=False)
                    dropped_chunks += 1
        return chosen, len(groups) - len(remaining), dropped_chunks

    async def prepare_request(self, state, messages, *, tool_definitions, transcript=None):
        config = state.config
        chosen, dropped_turns, dropped_chunks = self._select(config, messages, tool_definitions)
        estimated, source = self._count(config, chosen, tool_definitions)
        budget = self.input_budget(config)
        record = dict(
            purpose="agent",
            strategy=self.options.strategy,
            window_fallback=self.window_fallback,
            context_window_tokens=config.context_window_tokens,
            max_output_tokens=config.max_tokens,
            safety_tokens=SNIP_SAFETY_BUFFER,
            input_budget=budget,
            estimated_tokens=estimated,
            estimate_source=source,
            tool_schema_count=len(tool_definitions or []),
            tool_schema_marginal_tokens=max(0, estimated - self._count(config, chosen, None)[0]),
            raw_message_count=len(messages),
            selected_message_count=len(chosen),
            dropped_history_turns=dropped_turns,
            omitted_knowledge_chunks=dropped_chunks,
            tool_free=tool_definitions is None,
            messages=chosen,
            tool_definitions=tool_definitions,
        )
        if budget <= 0 or estimated > budget:
            self.trace.emit("context.rejected", **record)
            raise ContextWindowExceededError(
                session_key=config.session_key,
                estimated_tokens=estimated,
                input_budget=budget,
                source=source,
            )
        # Do not call the upstream auto-fit/summary path: the chosen baseline
        # must be observable, including a full-history failure at its cap.
        state.conversation.replace_transcript(transcript if transcript is not None else messages)
        provider_context = state.conversation.prepare_request(
            transcript if transcript is not None else messages,
            context_window_tokens=config.context_window_tokens,
            model_messages=chosen,
            resume_state=False,
        )
        if state.events.publish is not None:
            provider_context = replace(
                provider_context or ProviderCallContext(), events=state.events
            )
        state.messages = deepcopy(chosen)
        state.tool_definitions = deepcopy(tool_definitions)
        self.trace.emit("context.prepared", **record)
        return chosen, provider_context

    async def summarize_provider_compaction(self, state, response, **kwargs):
        if response.provider_compaction_applied or response.provider_compaction_state is not None:
            raise ValueError("Native provider compaction is not a full/window baseline")
