"""Evidence-bound review sidecars; never change original grades or source evidence."""

import json
from copy import deepcopy
from pathlib import Path

from app.evaluation import grading, grading_v2
from app.evaluation.runner import digest, read, replay

SCHEMA = "projectpilot-review-v1"
DECISIONS = {"passed": "passed", "failed": "failed", "insufficient_evidence": "review_required"}


def identity(row):
    return row["config"], row["repeat"], row["case_id"]


def load_batch(directory):
    directory = Path(directory).resolve()
    fresh = replay(directory, include_tasks=True)
    saved = read(directory / "report.json")
    # Early v1 reports predate the optional, empty per-task metrics field.
    for row in saved["tasks"]:
        row.setdefault("metrics", {})
    if fresh != saved:
        raise ValueError(
            "Saved report differs from verified replay; original report was not changed"
        )
    binding = {
        name: digest(directory / name) for name in ("manifest.json", "results.json", "report.json")
    }
    return directory, fresh, binding


def evidence_paths(directory, row):
    paths = []
    for turn in row["turns"]:
        if not turn.get("actual"):
            continue
        actual = Path(turn["actual"]).resolve()
        candidates = [actual, Path(read(actual)["trace"]).resolve()]
        for path in candidates:
            relative = path.relative_to(directory).as_posix()
            if relative not in paths:
                paths.append(relative)
    return paths


def template(directory):
    directory, report, binding = load_batch(directory)
    manifest = read(directory / "manifest.json")
    cases = {case["case_id"]: case for case in manifest["dataset"]["cases"]}
    return {
        "schema": SCHEMA,
        "batch_sha256": binding,
        "reviewer": "",
        "interpretation": "Separate review of this saved batch. Reviewer identity is self-reported; offline remains offline.",
        "items": [
            {
                "config": row["config"],
                "repeat": row["repeat"],
                "case_id": row["case_id"],
                "question": cases[row["case_id"]]["human_review"],
                "available_evidence": evidence_paths(directory, row),
                "decision": None,
                "reason": "",
                "evidence": [],
            }
            for row in report["tasks"]
            if row["status"] == "review_required"
        ],
    }


def reviewed_report(directory, review):
    directory, automatic, binding = load_batch(directory)
    if review.get("schema") != SCHEMA or review.get("batch_sha256") != binding:
        raise ValueError("Review does not match this original batch")
    report = deepcopy(automatic)
    targets = {identity(row): row for row in report["tasks"] if row["status"] == "review_required"}
    items = review.get("items")
    if not isinstance(items, list):
        raise ValueError("Review items must be a list")
    seen = set()
    applied = []
    for item in items:
        key = identity(item)
        if key not in targets or key in seen:
            raise ValueError("Only unique originally pending tasks can be reviewed")
        seen.add(key)
        decision = item.get("decision")
        if decision is None:
            continue
        if not isinstance(decision, str) or decision not in DECISIONS:
            raise ValueError("Decision must be passed, failed or insufficient_evidence")
        reviewer, reason, evidence = (
            review.get("reviewer"),
            item.get("reason"),
            item.get("evidence"),
        )
        allowed = evidence_paths(directory, targets[key])
        if (
            not isinstance(reviewer, str)
            or not reviewer.strip()
            or not isinstance(reason, str)
            or not reason.strip()
            or not isinstance(evidence, list)
            or not evidence
            or any(not isinstance(path, str) or path not in allowed for path in evidence)
        ):
            raise ValueError("A decision needs reviewer, reason and this task's original evidence")
        row = targets[key]
        row["automatic_status"] = row["status"]
        row["automatic_failures"] = list(row["failures"])
        row["status"] = DECISIONS[decision]
        row["failures"] = [] if decision == "passed" else [reason]
        if "recovery_task_success" in row.get("metrics", {}):
            row["metrics"]["recovery_task_success"] = [int(decision == "passed"), 1]
        row["review"] = {
            "decision": decision,
            "reason": reason,
            "evidence": evidence,
            "reviewer": reviewer,
        }
        applied.append({"config": key[0], "repeat": key[1], "case_id": key[2], **row["review"]})
    grader = {grading.GRADER_VERSION: grading, grading_v2.GRADER_VERSION: grading_v2}[
        automatic["summary"]["grader_version"]
    ]
    report["summary"] = grader.summarize(report["tasks"], automatic["summary"]["mode"])
    report["summary"]["limitations"] = [
        "复核汇总使用独立复核表；未填写或证据不足的项目仍待复核，复核者身份为自行填写。"
        if limitation.startswith("人工复核未实现回填")
        else limitation
        for limitation in report["summary"]["limitations"]
    ]
    report["automatic_summary"] = automatic["summary"]
    report["review_metadata"] = {"schema": SCHEMA, "batch_sha256": binding, "applied": applied}
    return report


def save_sidecar(directory, output, content):
    directory, output = Path(directory).resolve(), Path(output).resolve()
    if output.is_relative_to(directory):
        raise ValueError("Review output must be outside the original batch directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also protects a completed review from accidental replacement.
    with output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(content, ensure_ascii=False, indent=2) + "\n")
