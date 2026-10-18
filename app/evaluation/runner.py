"""Fresh-process task runner; reuse production adapters without changing their logic."""

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
from itertools import product
from pathlib import Path

from app.evaluation import grading, grading_v2
from app.evaluation.grading import (
    grade_turn,
    read_events,
    usage_from_events,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = {
    "full": {"strategy": "full", "memory": True},
    "window": {"strategy": "window", "memory": True},
    "summary": {"strategy": "summary", "memory": True},
    "memory-off": {"strategy": "full", "memory": False},
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def verify_sources(manifest):
    if any(digest(ROOT / name) != expected for name, expected in manifest["frozen_files"].items()):
        raise ValueError("Source changed during the batch; keep progress and start a new run")


def within(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Fixture path escapes task workspace")
    return path


def put_files(workspace, files):
    for name, content in files.items():
        target = within(workspace, name)
        if content is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")


def seed_history(state_dir, session_id, history):
    from nanobot.session.manager import SessionManager

    manager = SessionManager(state_dir / "workspace", sessions_root=state_dir / "sessions")
    session = manager.get_or_create(f"projectpilot:{session_id}")
    if session.messages:
        raise ValueError("Synthetic history requires an empty session")
    session.add_message("user", history["first_user"])
    session.add_message("assistant", "这是合成历史夹具，不代表执行过任务。")
    for index in range(history["turns"]):
        session.add_message("user", f"背景登记第{index + 1}轮。")
        session.add_message("assistant", history["filler"] * history["filler_repeats"])
    manager.save(session)
    return len(session.messages)


async def worker(job_path):
    from dotenv import load_dotenv
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider

    from app.context import ContextOptions
    from app.memory.offline import OfflineMemoryProvider
    from app.memory.service import MemoryOptions
    from app.observability.trace import JsonlTrace
    from app.runtime.knowledge import KnowledgeOptions
    from app.runtime.offline import (
        OfflineDemoProvider,
        OfflineKnowledgeProvider,
        OfflineRepositoryProvider,
    )
    from app.runtime.repository import RepositoryOptions
    from app.runtime.session import run_session
    from app.runtime.smoke import run_smoke

    job = read(job_path)
    root, config = job_path.parent, job["config"]
    provider = None
    if job["mode"] == "live":
        load_dotenv(ROOT / ".env", override=False)
    trace = JsonlTrace(root / "traces", secrets=(os.environ.get("PROJECTPILOT_API_KEY", ""),))
    actual = {"answer": "", "error": None, "trace": str(trace.path), "pid": os.getpid()}
    try:
        if job.get("history"):
            count = seed_history(Path(job["state_dir"]), job["session"], job["history"])
            trace.emit("evaluation.history_seeded", message_count=count, synthetic=True)
        if job["mode"] == "live":
            key = os.environ["PROJECTPILOT_API_KEY"]
            provider = OpenAICompatProvider(
                api_key=key,
                api_base=config["api_base"],
                default_model=config["model"],
                provider_name="projectpilot-compatible",
                extra_body={"enable_thinking": False},
            )
        else:
            kind = job["engine"]
            provider_type = {
                "repository": OfflineRepositoryProvider,
                "knowledge": OfflineKnowledgeProvider,
            }.get(kind, OfflineMemoryProvider if job["memory"] else OfflineDemoProvider)
            provider = provider_type()
        options = {
            "prompt": job["prompt"],
            "max_iterations": config["max_iterations"],
            "timeout_seconds": config["timeout_seconds"],
        }
        workspace = Path(job["workspace"])
        if job["engine"] == "smoke":
            result = await run_smoke(provider, workspace, trace, **options)
        else:
            options.update(
                state_dir=Path(job["state_dir"]),
                session_id=job["session"],
                context_options=ContextOptions(
                    config["strategy"],
                    window_tokens=config["window_tokens"],
                    recent_turns=config["recent_turns"],
                    max_output_tokens=config["max_output_tokens"],
                ),
            )
            if job["memory"]:
                options["memory_options"] = MemoryOptions(Path(job["memory_db"]), mode=job["mode"])
            if job["engine"] == "knowledge":
                options["knowledge_options"] = KnowledgeOptions(tuple(job["knowledge_files"]))
            if job["engine"] == "repository":
                options["repository_options"] = RepositoryOptions(Path(job["report_dir"]))
            result = await run_session(provider, workspace, trace, **options)
        actual.update(
            answer=result.final_content,
            stop_reason=result.stop_reason,
            restored_messages=getattr(result, "restored_messages", 0),
            reports=[
                {"path": str(path), "text": Path(path).read_text(encoding="utf-8")}
                for path in getattr(result, "saved_reports", [])
            ],
        )
    except Exception as exc:
        actual["error"] = type(exc).__name__
        events = read_events(trace.path) if trace.path.stat().st_size else []
        actual["stop_reason"] = next(
            (e.get("stop_reason") for e in reversed(events) if e["event"] == "run.finished"),
            "error",
        )
        loaded = next((event for event in events if event["event"] == "session.loaded"), None)
        if loaded:
            actual["restored_messages"] = loaded["message_count"]
    finally:
        if job["memory"] and Path(job["memory_db"]).exists():
            from app.memory import MemoryScope, MemoryStore

            records = MemoryStore(
                Path(job["memory_db"]),
                MemoryScope.for_workspace(
                    Path(job["workspace"]), mode=job["mode"], engine=job["engine"]
                ),
            ).list()
            actual["active_memory"] = [asdict(record) for record in records]
            events = read_events(trace.path) if trace.path.stat().st_size else []
            recalled = next(
                (e["selected"] for e in reversed(events) if e["event"] == "memory.recalled"), []
            )
            ids = {item["memory_id"] for item in recalled}
            actual["recalled_memory"] = [
                asdict(record) for record in records if record.memory_id in ids
            ]
        write(root / "actual.json", trace.clean(actual))
        client = getattr(provider, "_client", None)
        if client:
            await client.close()


def validate(dataset):
    if dataset["split"] != "development" or not dataset["cases"]:
        raise ValueError("This first runner accepts nonempty development datasets only")
    seen = set()
    allowed_checks = {
        "contains",
        "any_of",
        "excludes",
        "endswith",
        "required_tools",
        "tool_error",
        "min_restored_messages",
        "report_contains",
    } | grading_v2.EXTRA_CHECKS
    for case in dataset["cases"]:
        case_id = case["case_id"]
        if not re.fullmatch(r"[a-z0-9_-]+", case_id) or case_id in seen or not case["turns"]:
            raise ValueError("Case IDs must be unique safe names; turns must be nonempty")
        seen.add(case_id)
        if case["engine"] not in {"smoke", "session", "repository", "knowledge"}:
            raise ValueError("Unknown engine")
        if set(case.get("budget", {})) - set(dataset["budget"]):
            raise ValueError("Unknown case budget")
        if case.get("history"):
            history = case["history"]
            if (
                case["engine"] == "smoke"
                or not 1 <= history["turns"] <= 20
                or not 1 <= history["filler_repeats"] <= 1000
            ):
                raise ValueError("Invalid synthetic history size or engine")
        for turn in case["turns"]:
            if not turn["prompt"].strip() or set(turn.get("checks", {})) - allowed_checks:
                raise ValueError("Empty prompt or unsupported grader check")
            if case["engine"] == "knowledge" and not turn.get("knowledge_files"):
                raise ValueError("Knowledge turn needs explicit documents")
            checks = turn.get("checks", {})
            if "retrieval" in checks:
                annotation = checks["retrieval"]
                if not 1 <= annotation["k"] <= 5 or any(
                    set(target) != {"source", "start_line", "end_line"}
                    or not 1 <= target["start_line"] <= target["end_line"]
                    for target in annotation["relevant"]
                ):
                    raise ValueError("Invalid retrieval annotation")
            if any(
                item["kind"] not in {"contains", "excludes", "endswith"} or not item["text"]
                for item in checks.get("constraints", [])
            ):
                raise ValueError("Invalid response constraint")
            for name in turn.get("files", {}):
                within(ROOT / "fixture-validation", name)
        for name in case.get("files", {}):
            within(ROOT / "fixture-validation", name)


def run(dataset_path, directory, configs, repeats, live=False, case_ids=None):
    dataset = read(dataset_path)
    validate(dataset)
    if case_ids:
        if len(case_ids) != len(set(case_ids)) or set(case_ids) - {
            case["case_id"] for case in dataset["cases"]
        }:
            raise ValueError("Unknown or duplicate selected case IDs")
        dataset = {
            **dataset,
            "cases": [case for case in dataset["cases"] if case["case_id"] in case_ids],
        }
    base = dataset["budget"]
    mode = "live" if live else "offline"
    if live:
        from urllib.parse import urlsplit

        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
        base = {
            **base,
            "model": os.environ["PROJECTPILOT_MODEL"],
            "api_base": os.environ["PROJECTPILOT_API_BASE"],
        }
        url = urlsplit(base["api_base"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("Invalid API base")
        if not os.environ.get("PROJECTPILOT_API_KEY"):
            raise ValueError("Missing API key")
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "purpose": "development",
        "dataset": dataset,
        "dataset_sha256": digest(dataset_path),
        "grader_version": grading_v2.GRADER_VERSION,
        "grader_dependencies": ["app/evaluation/grading.py", "app/evaluation/grading_v2.py"],
        "selected_case_ids": case_ids,
        "python": sys.version,
        "nanobot_version": version("nanobot-ai"),
        "frozen_files": {
            str(p.relative_to(ROOT)): digest(p) for p in sorted((ROOT / "app").rglob("*.py"))
        },
        "configs": {name: {**base, **CONFIGS[name]} for name in configs},
        "repeats": repeats,
        "planned": len(dataset["cases"]) * len(configs) * repeats,
        "policy": "No selective retries. Preparation failures count. Review pending is not success.",
    }
    write(directory / "manifest.json", manifest)
    rows = []
    for name, config in manifest["configs"].items():
        for repeat in range(1, repeats + 1):
            for case in dataset["cases"]:
                verify_sources(manifest)
                case_config = {**config, **case.get("budget", {})}
                root = directory / name / str(repeat) / case["case_id"]
                root.mkdir(parents=True)
                workspace = root / "workspace"
                workspace.mkdir()
                put_files(workspace, case.get("files", {}))
                row = {"config": name, "repeat": repeat, "case_id": case["case_id"], "turns": []}
                started = time.perf_counter()
                for index, turn in enumerate(case["turns"]):
                    put_files(workspace, turn.get("files", {}))
                    turn_root = root / f"turn-{index + 1}"
                    turn_root.mkdir()
                    job = {
                        "mode": mode,
                        "config": case_config,
                        "engine": case["engine"],
                        "workspace": str(workspace),
                        "state_dir": str(root / "state"),
                        "memory_db": str(root / "memory.sqlite3"),
                        "report_dir": str(root / "reports"),
                        "memory": case.get("memory", False) and config["memory"],
                        "prompt": turn["prompt"],
                        "session": turn.get("session", "main"),
                        "knowledge_files": turn.get("knowledge_files", []),
                        "history": case.get("history") if index == 0 else None,
                    }
                    job_path = turn_root / "job.json"
                    write(job_path, job)
                    infrastructure_error = None
                    try:
                        result = subprocess.run(
                            [sys.executable, "-m", "app.evaluation", "--worker", str(job_path)],
                            cwd=ROOT,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=case_config["timeout_seconds"] + 20,
                        )
                        if result.returncode:
                            infrastructure_error = "worker_exit_" + str(result.returncode)
                    except subprocess.TimeoutExpired:
                        infrastructure_error = "worker_timeout"
                    actual_path = turn_root / "actual.json"
                    row["turns"].append(
                        {
                            "actual": str(actual_path) if actual_path.exists() else None,
                            "infrastructure_error": infrastructure_error,
                        }
                    )
                row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
                rows.append(row)
                print(
                    f"已执行 {len(rows)}/{manifest['planned']}：{name} / {case['case_id']}",
                    flush=True,
                )
                write(directory / "progress.json", rows)
    verify_sources(manifest)
    files = [
        directory / "manifest.json",
        *directory.rglob("actual.json"),
        *directory.rglob("*.jsonl"),
        *directory.rglob("job.json"),
    ]
    write(
        directory / "results.json",
        {
            "runs": rows,
            "evidence_sha256": {str(p.relative_to(directory)): digest(p) for p in files},
        },
    )
    return replay(directory, output=directory)


def replay(directory, output=None, *, include_tasks=False):
    directory = directory.resolve()
    results = read(directory / "results.json")
    for name, expected in results["evidence_sha256"].items():
        if digest(within(directory, name)) != expected:
            raise ValueError("Evidence changed: " + name)
    manifest = read(directory / "manifest.json")
    graders = {grading.GRADER_VERSION: grading, grading_v2.GRADER_VERSION: grading_v2}
    grader = graders[manifest["grader_version"]]
    for name in manifest.get("grader_dependencies", ["app/evaluation/grading.py"]):
        path = ROOT / name
        if digest(path) != manifest["frozen_files"][str(path.relative_to(ROOT))]:
            raise ValueError("Grader changed; original replay requires the frozen grader")
    cases = {case["case_id"]: case for case in manifest["dataset"]["cases"]}
    planned = set(product(manifest["configs"], range(1, manifest["repeats"] + 1), cases))
    observed = [(r["config"], r["repeat"], r["case_id"]) for r in results["runs"]]
    if len(observed) != len(planned) or set(observed) != planned:
        raise ValueError("Planned runs are missing or duplicated")
    rows = []
    for source in results["runs"]:
        case = cases[source["case_id"]]
        if len(source["turns"]) != len(case["turns"]):
            raise ValueError("Planned turns are missing or duplicated")
        failures, events = [], []
        measured = {}
        infra = False
        timeout = False
        for index, turn in enumerate(source["turns"]):
            actual, trace = {}, []
            if turn["infrastructure_error"] or not turn["actual"]:
                infra = True
                failures.append(
                    f"回合{index + 1}：{turn['infrastructure_error'] or 'missing_result'}"
                )
                timeout |= turn["infrastructure_error"] == "worker_timeout"
            if turn["actual"]:
                try:
                    actual = read(Path(turn["actual"]))
                    trace = read_events(Path(actual["trace"]))
                except (OSError, ValueError, KeyError) as exc:
                    infra = True
                    failures.append(
                        f"回合{index + 1}：无法读取完整运行证据（{type(exc).__name__}）"
                    )
                events.extend(trace)
                timeout |= actual.get("stop_reason") == "timeout" or any(
                    e.get("error") == "TimeoutError" for e in trace
                )
            expected = case["turns"][index].get("checks", {})
            if grader is grading_v2:
                problems, metrics = grader.assess(actual, expected, trace)
                for name, pair in metrics.items():
                    previous = measured.setdefault(name, [0, 0])
                    previous[0] += pair[0]
                    previous[1] += pair[1]
            else:
                problems = grade_turn(actual, expected, trace)
            failures.extend(f"回合{index + 1}：{f}" for f in problems)
        status = (
            "infrastructure_error"
            if infra
            else (
                "failed"
                if failures
                else ("review_required" if case.get("human_review") else "passed")
            )
        )
        if status == "review_required":
            failures.append(case["human_review"])
        if case.get("recovery"):
            measured["recovery_task_success"] = [int(status == "passed"), 1]
        rows.append(
            {
                **source,
                "status": status,
                "failures": failures,
                "timeout": timeout,
                "usage": usage_from_events(events),
                "metrics": measured,
            }
        )
    summary = grader.summarize(rows, manifest["mode"])
    if output:
        write(output / "report.json", {"summary": summary, "tasks": rows})
        (output / "report.txt").write_text(grader.readable_report(summary, rows), encoding="utf-8")
    return {"summary": summary, "tasks": rows} if include_tasks else summary
