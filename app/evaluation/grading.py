"""Versioned, deliberately limited deterministic checks and honest aggregation."""

import json
import math
from collections import Counter
from statistics import mean, pstdev

GRADER_VERSION = "task-checks-v1"


def read_events(path):
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if not events or [e["sequence"] for e in events] != list(range(1, len(events) + 1)):
        raise ValueError("Incomplete trace sequence")
    if len({e["run_id"] for e in events}) != 1:
        raise ValueError("Mixed trace run IDs")
    return events


def grade_turn(actual, expected, events):
    failures = []
    if actual.get("error") or actual.get("stop_reason") != "completed":
        failures.append("运行未正常完成：" + str(actual.get("error") or actual.get("stop_reason")))
    if not events or events[-1]["event"] != "run.finished":
        failures.append("缺少完整结束记录")
    for event in events:
        if event["event"] in {"memory.extraction.failed", "context.summary.failed"}:
            failures.append("准备链路失败：" + event["event"] + ":" + event.get("error", ""))
        if event["event"] == "llm.call" and event.get("error_kind"):
            failures.append("模型调用失败：" + event["error_kind"])
    text = actual.get("answer", "")
    for term in expected.get("contains", []):
        if term not in text:
            failures.append("回答缺少：" + term)
    for alternatives in expected.get("any_of", []):
        if not any(term in text for term in alternatives):
            failures.append("回答需包含其中一项：" + " / ".join(alternatives))
    for term in expected.get("excludes", []):
        if term in text:
            failures.append("回答出现禁止内容：" + term)
    if expected.get("endswith") and not text.rstrip().endswith(expected["endswith"]):
        failures.append("结尾不符合要求")
    successful = {
        e["name"] for e in events if e["event"] == "tool.finished" and not e.get("is_error")
    }
    for name in expected.get("required_tools", []):
        if name not in successful:
            failures.append("缺少成功工具调用：" + name)
    if expected.get("tool_error") and not any(
        e["event"] == "tool.error" or (e["event"] == "tool.finished" and e.get("is_error"))
        for e in events
    ):
        failures.append("未观察到预定工具故障")
    if actual.get("restored_messages", 0) < expected.get("min_restored_messages", 0):
        failures.append("未恢复足够的会话消息")
    for term in expected.get("report_contains", []):
        if not any(term in report["text"] for report in actual.get("reports", [])):
            failures.append("保存的报告缺少：" + term)
    return failures


def usage_from_events(events):
    # Main requests use the retry observer; extraction and summary call chat()
    # directly and have separate usage events. Never add run.finished totals again.
    calls = [e for e in events if e["event"] == "llm.call"]
    auxiliary = [
        e
        for e in events
        if e["event"] in {"memory.extraction.finished", "context.summary.finished"}
    ]
    records = calls + auxiliary
    return {
        "observed_main_calls": len(calls),
        "auxiliary_records": len(auxiliary),
        "reported_tokens": sum(
            (e.get("usage") or {}).get("reported_tokens", 0) or 0 for e in records
        ),
        "estimated_tokens": sum(
            (e.get("usage") or {}).get("estimated_tokens", 0) or 0 for e in records
        ),
        "missing_usage_records": sum(not e.get("usage") for e in records),
        "auxiliary_failures": sum(
            e["event"] in {"memory.extraction.failed", "context.summary.failed"} for e in events
        ),
    }


def summarize(rows, mode):
    groups = []
    for config in dict.fromkeys(row["config"] for row in rows):
        selected = [r for r in rows if r["config"] == config]
        counts = Counter(r["status"] for r in selected)
        durations = sorted(r["elapsed_ms"] for r in selected)
        repeat_rates = []
        for repeat in sorted({r["repeat"] for r in selected}):
            batch = [r for r in selected if r["repeat"] == repeat]
            repeat_rates.append(sum(r["status"] == "passed" for r in batch) / len(batch))
        groups.append(
            {
                "config": config,
                "planned": len(selected),
                "passed": counts["passed"],
                "failed": counts["failed"],
                "review_required": counts["review_required"],
                "infrastructure_errors": counts["infrastructure_error"],
                "timeouts": sum(r["timeout"] for r in selected),
                "strict_success_rate": counts["passed"] / len(selected),
                "repeat_rates": repeat_rates,
                "repeat_rate_mean": mean(repeat_rates),
                "repeat_rate_stddev": pstdev(repeat_rates) if len(repeat_rates) > 1 else None,
                "p50_ms": durations[math.ceil(len(durations) * 0.50) - 1],
                "p95_ms": durations[math.ceil(len(durations) * 0.95) - 1],
                "usage": {
                    key: sum(r["usage"][key] for r in selected)
                    for key in (
                        "observed_main_calls",
                        "auxiliary_records",
                        "reported_tokens",
                        "estimated_tokens",
                        "missing_usage_records",
                        "auxiliary_failures",
                    )
                },
                "usage_incomplete": any(
                    r["status"] == "infrastructure_error" or r["usage"]["missing_usage_records"]
                    for r in selected
                ),
                "cost": None,
            }
        )
    return {
        "mode": mode,
        "interpretation": "离线仅验证评测流程，不代表真实模型能力。"
        if mode == "offline"
        else "开发集结果，不是独立保留集验收。",
        "grader_version": GRADER_VERSION,
        "latency_method": "nearest-rank; all planned tasks, including preparation and process startup",
        "success_denominator": "all planned tasks; pending review and infrastructure errors are not successes",
        "groups": groups,
        "limitations": [
            "关键词、工具回执和来源字符串只能做初筛，不能证明引用支持结论。",
            "人工复核未实现回填；需要复核的任务保持待复核，不计成功。",
            "费用未核算；缺失用量不能按免费处理；单次运行没有波动结论。",
        ],
    }


def readable_report(summary, rows):
    lines = ["ProjectPilot Phase 5 开发评测", summary["interpretation"], ""]
    for group in summary["groups"]:
        lines.append(
            f"{group['config']}：共 {group['planned']} 项，通过 {group['passed']}，"
            f"失败 {group['failed']}，待复核 {group['review_required']}，"
            f"执行设施错误 {group['infrastructure_errors']}；超时 {group['timeouts']}。"
        )
    lines += ["", "需要关注的任务："]
    for row in rows:
        if row["status"] != "passed":
            lines.append(
                f"- {row['config']} / {row['case_id']} / 第{row['repeat']}次：{row['status']}"
            )
            lines.extend("  " + reason for reason in row["failures"])
    lines += ["", *summary["limitations"]]
    return "\n".join(lines) + "\n"
