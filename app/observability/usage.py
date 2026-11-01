"""One-turn token accounting from trace events, without persistence or UI policy."""

TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
AUXILIARY = {"memory": "memory.extraction", "summary": "context.summary"}


def _number(value):
    return value if type(value) is int and value >= 0 else None


def _sum_known(values):
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _normalize(usage):
    usage = usage or {}
    counts = {
        "input_tokens": _number(usage.get("input_tokens", usage.get("prompt_tokens"))),
        "output_tokens": _number(
            usage.get("output_tokens", usage.get("completion_tokens"))
        ),
        "total_tokens": _number(usage.get("total_tokens")),
    }
    source = usage.get("source")
    if source not in {"reported", "estimated", "mixed"}:
        estimated = _number(usage.get("estimated_tokens")) or 0
        reported = _number(usage.get("reported_tokens")) or 0
        source = (
            "mixed"
            if estimated and reported
            else "estimated"
            if estimated
            else "reported"
        )
    if all(value is None for value in counts.values()):
        source = "unknown"
    return {
        **counts,
        "source": source,
        "calls": _number(usage.get("request_count")) or 1,
        "reported_tokens": (
            counts["total_tokens"]
            if source == "reported"
            else 0
            if source == "estimated"
            else _number(usage.get("reported_tokens"))
        ),
        "estimated_tokens": (
            counts["total_tokens"]
            if source == "estimated"
            else 0
            if source == "reported"
            else _number(usage.get("estimated_tokens"))
        ),
    }


def _group(usages, *, required=TOKEN_FIELDS, unobserved=False):
    records = [_normalize(usage) for usage in usages]
    missing = sum(
        record["calls"]
        for record in records
        if any(record[field] is None for field in required)
    )
    counts = {
        field: _sum_known(record[field] for record in records) for field in TOKEN_FIELDS
    }
    sources = {record["source"] for record in records} - {"unknown"}
    source = (
        "mixed" if "mixed" in sources or len(sources) > 1 else next(iter(sources), None)
    )
    if not records:
        status = "unknown" if unobserved else "not_called"
    elif all(value is None for value in counts.values()):
        status = "unknown"
    else:
        status = "partial" if missing or unobserved else "complete"
    return {
        **counts,
        "calls": sum(record["calls"] for record in records),
        "missing_calls": missing,
        "source": source or ("unknown" if status == "unknown" else None),
        "status": status,
        "reported_tokens": _sum_known(record["reported_tokens"] for record in records),
        "estimated_tokens": _sum_known(
            record["estimated_tokens"] for record in records
        ),
    }


def _unique_events(events):
    # Only trace identities are unique: two identical payloads may be two paid calls.
    seen, runs, result = set(), set(), []
    for event in events:
        run_id, sequence = event.get("run_id"), event.get("sequence")
        if run_id is not None:
            runs.add(run_id)
        if sequence is not None:
            identity = (run_id, sequence)
            if identity in seen:
                continue
            seen.add(identity)
        result.append(event)
    if len(runs) > 1:
        raise ValueError("Token accounting requires events from a single run")
    return result


def _call_usage(event):
    usage = event.get("usage")
    failed = event.get("finish_reason") in {"error", "cancelled"} or event.get(
        "error_kind"
    )
    if failed and usage and not any((_number(usage.get(f)) or 0) for f in TOKEN_FIELDS):
        return None  # Upstream empty_request is a placeholder, not a reported zero.
    return usage


def summarize_usage(events, *, mode, stop_reason=None):
    """Return JSON-ready usage for one task; unknown fields remain None.

    Physical llm.call events take precedence over runner/run summaries. Retrieval
    metadata is a cumulative snapshot, so only its latest snapshot is counted.
    stop_reason may supply the final job status when a worker was terminated.
    """
    if mode not in {"live", "offline"}:
        raise ValueError("Usage mode must be live or offline")
    if mode == "offline":
        return {
            "schema_version": 1,
            "mode": mode,
            "status": "not_applicable",
            "conversation": None,
            "categories": {},
            "retrieval": {},
        }

    events = _unique_events(events)
    finish = next((e for e in reversed(events) if e["event"] == "run.finished"), {})
    reason = stop_reason or finish.get("stop_reason")
    successful = reason == "completed" and not finish.get("error")
    main = [_call_usage(e) for e in events if e["event"] == "llm.call"]
    if not main:
        fallback = next(
            (
                e
                for e in reversed(events)
                if e["event"] in {"run.finished", "runner.finished"}
                and e.get("usage") is not None
            ),
            None,
        )
        if fallback:
            main = [_call_usage({**fallback, "finish_reason": reason})]

    records = {"main": main}
    for name, prefix in AUXILIARY.items():
        finished = [e for e in events if e["event"] == prefix + ".finished"]
        started = sum(e["event"] == prefix + ".started" for e in events)
        failed = sum(e["event"] == prefix + ".failed" for e in events)
        # A failed event and its finally/finished event describe one attempt.
        records[name] = [e.get("usage") for e in finished]
        records[name].extend([None] * max(0, max(started, failed) - len(finished)))
    categories = {
        name: _group(usages, unobserved=name == "main" and not main)
        for name, usages in records.items()
    }
    conversation = _group(
        [usage for usages in records.values() for usage in usages],
        unobserved=not main,
    )

    retriever = "bm25"
    for event in events:
        if event["event"] == "run.started":
            retriever = (event.get("knowledge_options") or {}).get("retriever", "bm25")
        elif event["event"] == "knowledge.index_built":
            retriever = event.get("retriever", retriever)
    pending_search = sum(
        e["event"] == "tool.started" and e.get("name") == "search_knowledge"
        for e in events
    ) > sum(e["event"] == "knowledge.retrieval_completed" for e in events)
    retrieval = {}
    for name in ("embedding", "rerank"):
        snapshot = next(
            (
                e[name]
                for e in reversed(events)
                if e["event"] == "knowledge.retrieval_completed"
                and e.get(name) is not None
            ),
            {},
        )
        requests = snapshot.get("requests", [])
        attempts = max(_number(snapshot.get("attempts")) or 0, len(requests))
        usages = [request.get("usage") for request in requests]
        usages.extend([None] * (attempts - len(requests)))
        expected = (
            retriever in {"vector", "hybrid", "hybrid_rerank"}
            if name == "embedding"
            else retriever == "hybrid_rerank"
        )
        retrieval[name] = {
            **_group(
                usages,
                required=("total_tokens",),
                unobserved=pending_search and expected,
            ),
            "raw_usage": [request.get("usage") for request in requests],
        }

    complete = (
        successful
        and conversation["status"] == "complete"
        and all(
            group["status"] in {"complete", "not_called"}
            for group in retrieval.values()
        )
    )
    return {
        "schema_version": 1,
        "mode": mode,
        "status": "complete" if complete else "incomplete",
        "conversation": conversation,
        "categories": categories,
        "retrieval": retrieval,
    }
