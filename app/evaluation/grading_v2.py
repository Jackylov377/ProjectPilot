"""Evidence-based development checks; preserve v1 for historical replay."""

import json

from app.evaluation import grading as v1

GRADER_VERSION = "task-checks-v2"
EXTRA_CHECKS = {"retrieval", "recalled_memory", "active_memory", "context_contains", "constraints"}


def assess(actual, expected, events):
    failures = v1.grade_turn(actual, expected, events)
    metrics = {}

    def measure(name, correct, total):
        if total:
            metrics[name] = [correct, total]

    required = expected.get("required_tools", [])
    successful = {
        e["name"] for e in events if e["event"] == "tool.finished" and not e.get("is_error")
    }
    measure("required_tool_coverage", len(set(required) & successful), len(set(required)))
    if "retrieval" in expected:
        annotation = expected["retrieval"]
        hits = []
        for event in events:
            if (
                event["event"] == "tool.finished"
                and event.get("name") == "search_knowledge"
                and not event.get("is_error")
            ):
                hits = json.loads(event["result"])["results"][: annotation["k"]]
                break
        relevant = annotation["relevant"]

        def supports(hit, target):
            return (
                hit["source"] == target["source"]
                and hit["start_line"] <= target["start_line"]
                and hit["end_line"] >= target["end_line"]
            )

        if relevant:
            found = sum(any(supports(hit, target) for hit in hits) for target in relevant)
            rank = next(
                (
                    i
                    for i, hit in enumerate(hits, 1)
                    if any(supports(hit, target) for target in relevant)
                ),
                None,
            )
            # Macro-average across annotated queries, not across chunks.
            measure(f"first_search_recall_at_{annotation['k']}", found / len(relevant), 1)
            measure(f"first_search_mrr_at_{annotation['k']}", 1 / rank if rank else 0, 1)
            if found != len(relevant):
                failures.append("首轮检索未覆盖全部标注证据")
        else:
            searched = any(
                e["event"] == "tool.finished"
                and e.get("name") == "search_knowledge"
                and not e.get("is_error")
                for e in events
            )
            measure("unanswerable_empty_retrieval", int(searched and not hits), 1)
            # An unanswerable question can retrieve related, unsupported passages.
            # Empty retrieval is a diagnostic, never a mandatory refusal condition.
    for field, key in (("active_memory", "active_memory"), ("recalled_memory", "recalled_memory")):
        if field in expected:
            wanted = expected[field]
            observed = {record["key"]: record["value"] for record in actual.get(key, [])}
            matched = sum(observed.get(k) == value for k, value in wanted.items())
            if field == "recalled_memory":
                measure("annotated_memory_recall", matched, len(wanted))
            if (wanted and matched != len(wanted)) or (not wanted and observed):
                failures.append("记忆证据不符：" + field)
    if "context_contains" in expected:
        prepared = [
            e for e in events if e["event"] == "context.prepared" and e.get("purpose") == "agent"
        ]
        text = json.dumps(prepared[-1]["messages"], ensure_ascii=False) if prepared else ""
        required_context = expected["context_contains"]
        matched = sum(term in text for term in required_context)
        measure("annotated_context_retention", matched, len(required_context))
        for term in required_context:
            if term not in text:
                failures.append("实际请求未保留：" + term)
    constraints = expected.get("constraints", [])
    matched = 0
    for item in constraints:
        text, term = actual.get("answer", ""), item["text"]
        passed = (
            actual.get("stop_reason") == "completed"
            and {
                "contains": term in text,
                "excludes": term not in text,
                "endswith": text.rstrip().endswith(term),
            }[item["kind"]]
        )
        matched += passed
        if not passed:
            failures.append("回答约束不符：" + item["kind"] + " / " + term)
    measure("annotated_response_constraints", matched, len(constraints))
    return failures, metrics


def summarize(rows, mode):
    summary = v1.summarize(rows, mode)
    summary["grader_version"] = GRADER_VERSION
    summary["metric_definitions"] = {
        "required_tool_coverage": "Required successful tool names / annotated required names; not argument accuracy.",
        "first_search_recall_at_K": "Macro-average relevant source+line spans covered by the first successful search, cropped at K.",
        "first_search_mrr_at_K": "Macro-average reciprocal rank of first relevant hit; missing search is zero.",
        "unanswerable_empty_retrieval": "Unanswerable turns with an executed, empty search / annotated unanswerable turns; diagnostic only.",
        "annotated_memory_recall": "Correct annotated key+value pairs in the last recalled snapshot / expected pairs.",
        "annotated_context_retention": "Annotated strings present in the final prepared agent request / expected strings; not semantic retention.",
        "annotated_response_constraints": "Satisfied deterministic answer constraints / annotated answer constraints.",
        "recovery_task_success": "Strictly passed annotated recovery tasks / all annotated recovery tasks; includes explicit user repair turns.",
    }
    for group in summary["groups"]:
        selected = [r for r in rows if r["config"] == group["config"]]
        names = dict.fromkeys(name for r in selected for name in r.get("metrics", {}))
        group["metrics"] = {}
        for name in names:
            numerator = sum(r.get("metrics", {}).get(name, [0, 0])[0] for r in selected)
            denominator = sum(r.get("metrics", {}).get(name, [0, 0])[1] for r in selected)
            group["metrics"][name] = {
                "numerator": numerator,
                "denominator": denominator,
                "value": numerator / denominator if denominator else None,
            }
    return summary


def readable_report(summary, rows):
    text = v1.readable_report(summary, rows)
    labels = {
        "passed": "通过",
        "failed": "失败",
        "review_required": "待复核",
        "infrastructure_error": "执行设施错误",
    }
    for status, label in labels.items():
        text = text.replace(":" + status, ":" + label).replace("：" + status, "：" + label)
    return (
        text + "\n证据指标与分母见 report.json；检索命中、记忆召回、请求保留和最终答对分别计算。\n"
    )
