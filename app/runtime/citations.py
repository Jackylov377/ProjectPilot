"""Check citation locations against returned evidence, never semantic entailment."""

import json
import re


def current_knowledge_evidence(messages):
    boundary = max(
        (i for i, message in enumerate(messages) if message.get("role") == "user"), default=-1
    )
    evidence = []
    for message in messages[boundary + 1 :]:
        if message.get("role") != "tool" or message.get("name") != "search_knowledge":
            continue
        try:
            payload = json.loads(message.get("content", ""))
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        for hit in payload.get("results", []):
            if isinstance(hit, dict) and isinstance(hit.get("source"), str):
                evidence.append(hit)
    return evidence


def covered(first, last, intervals):
    cursor = first
    for start, end in sorted(intervals):
        if start > cursor:
            break
        cursor = max(cursor, end + 1)
        if cursor > last:
            return True
    return False


def citation_violations(answer, evidence):
    sources = {hit["source"] for hit in evidence}
    # Known paths come first so a selected filename may include spaces. Unknown
    # ordinary path references are also recognized and cannot silently pass.
    filenames = r"[^\s`\"'<>\[\](){}（）【】《》“”‘’，。；：、]+?\.[A-Za-z][A-Za-z0-9_-]{0,11}"
    alternatives = [re.escape(source) for source in sorted(sources, key=len, reverse=True)]
    pattern = re.compile(
        r"(?P<source>"
        + "|".join([*alternatives, filenames])
        + r"):(?P<location>p?\d+(?:-p?\d+)?)(?![\w-])"
    )
    issues = []
    for match in pattern.finditer(answer):
        source, location = match["source"], match["location"]
        parts = location.split("-")
        page = parts[0].startswith("p")
        first, last = int(parts[0].removeprefix("p")), int(parts[-1].removeprefix("p"))
        intervals = []
        for hit in evidence:
            if hit["source"] != source:
                continue
            if page and type(hit.get("page")) is int:
                intervals.append((hit["page"], hit["page"]))
            elif (
                not page
                and hit.get("page") is None
                and all(type(hit.get(key)) is int for key in ("start_line", "end_line"))
            ):
                intervals.append((hit["start_line"], hit["end_line"]))
        if first < 1 or last < first or not covered(first, last, intervals):
            issues.append(
                f"引用“{match[0]}”不在本回合实际返回的证据范围内。"
                "只能使用本回合返回的来源和行段，PDF使用物理页码:pN；"
                "没有依据时明确无法确认，不使用旧引用或补造来源。"
            )
    return list(dict.fromkeys(issues))
