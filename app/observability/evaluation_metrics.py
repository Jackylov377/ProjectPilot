"""Prometheus metrics derived from the durable offline evaluation receipts."""

from contextlib import closing

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, HistogramMetricFamily

BUCKETS = (0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 900, 3600, 21600)
FINISHED = ("completed", "failed", "cancelled", "interrupted")


def duration_histogram(db, name, description, query):
    bucket_counts = ", ".join("COUNT(CASE WHEN seconds <= ? THEN 1 END)" for _ in BUCKETS)
    *counts, count, total = db.execute(
        f"WITH durations AS ({query}) "
        f"SELECT {bucket_counts}, COUNT(seconds), COALESCE(SUM(seconds), 0) FROM durations",
        BUCKETS,
    ).fetchone()
    buckets = [(str(bound), count) for bound, count in zip(BUCKETS, counts)]
    buckets.append(("+Inf", count))
    return HistogramMetricFamily(name, description, buckets=buckets, sum_value=total)


class EvaluationMetrics:
    def __init__(self, jobs):
        self.jobs = jobs

    def collect(self):
        active = GaugeMetricFamily(
            "projectpilot_evaluation_jobs", "Offline evaluation jobs currently queued or running.", labels=["status"]
        )
        finished = CounterMetricFamily(
            "projectpilot_evaluation_jobs_finished", "Offline evaluation jobs by terminal status.", labels=["status"]
        )
        with closing(self.jobs.connect()) as db:
            db.execute("BEGIN")
            totals = db.execute(
                "SELECT status, COUNT(*), SUM(MAX(0, attempt - 1)) FROM evaluation_jobs GROUP BY status"
            ).fetchall()
            wait = duration_histogram(
                db, "projectpilot_evaluation_queue_wait_seconds",
                "Time from offline evaluation submission to its first Worker claim.",
                "SELECT MAX(0, ROUND((julianday(first_started_at) - julianday(created_at)) * 86400, 3)) "
                "AS seconds FROM evaluation_jobs WHERE first_started_at IS NOT NULL",
            )
            execution = duration_histogram(
                db, "projectpilot_evaluation_execution_seconds",
                "Time from the final Worker claim to a completed or failed offline evaluation.",
                "SELECT MAX(0, ROUND((julianday(finished_at) - julianday(started_at)) * 86400, 3)) "
                "AS seconds FROM evaluation_jobs WHERE status IN ('completed', 'failed')",
            )
        counts = {status: count for status, count, _ in totals}
        for status in ("queued", "running"):
            active.add_metric([status], counts.get(status, 0))
        for status in FINISHED:
            finished.add_metric([status], counts.get(status, 0))
        yield active
        yield finished
        yield CounterMetricFamily(
            "projectpilot_evaluation_worker_retries", "Additional Worker attempts after the first claim.",
            value=sum(retries for _, _, retries in totals),
        )
        yield wait
        yield execution
