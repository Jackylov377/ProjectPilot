"""Persistent evaluation receipts and a RabbitMQ worker for offline batches."""

import json
import logging
import os
import re
import signal
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from threading import Event
from uuid import uuid4

from app.api.diagnostics import StderrCapture, failure_details
from app.api.jobs import evaluation_command, evaluation_result, now, start_process, stop_process

QUEUE = "projectpilot.evaluations.v1"
MAX_ATTEMPTS = 3
DATABASE = "evaluation-queue.sqlite3"
RABBITMQ_HOST = "PROJECTPILOT_RABBITMQ_HOST"
LOGGER = logging.getLogger(__name__)


class EvaluationJobs:
    def __init__(self, data_dir):
        self.path = data_dir / DATABASE
        self.attempts_dir = data_dir / "evaluation-attempts"
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS evaluation_jobs (
                    id TEXT PRIMARY KEY,
                    request TEXT NOT NULL,
                    timeout INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL,
                    first_started_at TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    published INTEGER NOT NULL DEFAULT 0,
                    output TEXT,
                    outcome TEXT NOT NULL DEFAULT '{}'
                )
            """)
            columns = {column["name"] for column in db.execute("PRAGMA table_info(evaluation_jobs)")}
            if "first_started_at" not in columns:
                db.execute("ALTER TABLE evaluation_jobs ADD COLUMN first_started_at TEXT")
                db.execute("UPDATE evaluation_jobs SET first_started_at=started_at WHERE attempt=1")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def receipt(row):
        job = dict(row)
        job.pop("published")
        job["request"] = json.loads(job["request"])
        if output := job.pop("output"):
            job["request"]["output"] = output
        job.update(json.loads(job.pop("outcome")), kind="evaluation", execution="queue")
        return job

    def submit(self, request, timeout):
        job_id = uuid4().hex
        with closing(self.connect()) as db, db:
            row = db.execute(
                "INSERT INTO evaluation_jobs (id, request, timeout, created_at) "
                "VALUES (?, ?, ?, ?) RETURNING *",
                (job_id, json.dumps(request), timeout, now()),
            ).fetchone()
        return self.receipt(row)

    def get(self, job_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM evaluation_jobs WHERE id=?", (job_id,)).fetchone()
        return self.receipt(row) if row else None

    def list(self):
        with closing(self.connect()) as db:
            return [self.receipt(row) for row in db.execute("SELECT * FROM evaluation_jobs")]

    def reports(self):
        with closing(self.connect()) as db:
            return [Path(row[0]) / "report.json" for row in db.execute(
                "SELECT output FROM evaluation_jobs WHERE status='completed' AND output IS NOT NULL"
            )]

    def unpublished(self):
        with closing(self.connect()) as db:
            return [row[0] for row in db.execute(
                "SELECT id FROM evaluation_jobs WHERE status='queued' AND published=0 "
                "ORDER BY created_at, id LIMIT 32"
            )]

    def confirm_published(self, job_id):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE evaluation_jobs SET published=1 WHERE id=?", (job_id,))

    def claim(self, job_id):
        with closing(self.connect()) as db, db:
            started_at = now()
            row = db.execute(
                "UPDATE evaluation_jobs SET status='running', attempt=attempt+1, started_at=?, "
                "first_started_at=CASE WHEN attempt=0 THEN ? ELSE first_started_at END "
                "WHERE id=? AND status IN ('queued','running') AND attempt<? RETURNING *",
                (started_at, started_at, job_id, MAX_ATTEMPTS),
            ).fetchone()
            if row is None:
                outcome = {
                    "error_code": "worker_interrupted",
                    "error": "评测 Worker 多次中断。",
                    "error_hint": "请查看 evaluation-worker 日志后重新提交。",
                }
                db.execute(
                    "UPDATE evaluation_jobs SET status='interrupted', finished_at=?, outcome=? "
                    "WHERE id=? AND status='running' AND attempt>=?",
                    (now(), json.dumps(outcome), job_id, MAX_ATTEMPTS),
                )
        return self.receipt(row) if row else None

    def owns(self, job_id, attempt):
        with closing(self.connect()) as db:
            return db.execute(
                "SELECT 1 FROM evaluation_jobs WHERE id=? AND status='running' AND attempt=?",
                (job_id, attempt),
            ).fetchone() is not None

    def finish(self, job_id, attempt, *, status, outcome, output=None):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE evaluation_jobs SET status=?, finished_at=?, outcome=?, output=? "
                "WHERE id=? AND status='running' AND attempt=?",
                (status, now(), json.dumps(outcome), output, job_id, attempt),
            )

    def cancel(self, job_id):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE evaluation_jobs SET status='cancelled', finished_at=? "
                "WHERE id=? AND status IN ('queued','running')",
                (now(), job_id),
            )
            row = db.execute("SELECT * FROM evaluation_jobs WHERE id=?", (job_id,)).fetchone()
        return self.receipt(row) if row else None


def broker_connection():
    import pika

    return pika.BlockingConnection(pika.ConnectionParameters(
        host=os.environ[RABBITMQ_HOST],
        credentials=pika.PlainCredentials(
            "projectpilot", os.getenv("PROJECTPILOT_RABBITMQ_PASSWORD", "projectpilot-local")
        ),
        heartbeat=30,
        socket_timeout=3,
        stack_timeout=5,
        blocked_connection_timeout=5,
    ))


def evaluation_channel(connection):
    channel = connection.channel()
    channel.queue_declare(
        queue=QUEUE,
        durable=True,
        arguments={"x-single-active-consumer": True, "x-consumer-timeout": 25200000},
    )
    return channel


def publish_pending(jobs):
    import pika

    pending = jobs.unpublished()
    if not pending:
        return
    with broker_connection() as connection:
        channel = evaluation_channel(connection)
        channel.confirm_delivery()
        for job_id in pending:
            channel.basic_publish(
                exchange="", routing_key=QUEUE, body=job_id.encode("ascii"), mandatory=True,
                properties=pika.BasicProperties(delivery_mode=2, message_id=job_id),
            )
            jobs.confirm_published(job_id)


def failed(jobs, job_id, attempt, reason):
    details = failure_details(stop_reason=reason)
    if reason == "process_error":
        details["error_hint"] = "请查看 evaluation-worker 日志后重新提交。"
    jobs.finish(job_id, attempt, status="failed", outcome=details)


def execute_delivery(jobs, job_id, connection, stopping):
    """Return whether the delivery can be acknowledged."""
    job = jobs.claim(job_id)
    if job is None:
        return True
    attempt = job["attempt"]
    # A replacement Worker gets its own directory. Only the current attempt can publish a report.
    output = jobs.attempts_dir / f"{job_id}-{attempt}"
    try:
        process = start_process(evaluation_command({**job["request"], "output": str(output)}))
    except OSError:
        failed(jobs, job_id, attempt, "start_failed")
        return True
    capture = StderrCapture(process.stderr)
    deadline = time.monotonic() + job["timeout"]
    try:
        while process.poll() is None:
            if stopping.is_set():
                return False
            if not jobs.owns(job_id, attempt):
                return True
            if time.monotonic() >= deadline:
                stop_process(process)
                failed(jobs, job_id, attempt, "timeout")
                return True
            connection.process_data_events(time_limit=0.25)
        if process.returncode:
            LOGGER.warning("Evaluation %s attempt %s failed: %s", job_id, attempt, capture.finish())
            failed(jobs, job_id, attempt, "process_error")
            return True
        try:
            result = evaluation_result(output)
        except (OSError, ValueError):
            failed(jobs, job_id, attempt, "result_invalid")
        else:
            jobs.finish(job_id, attempt, status="completed", outcome={"result": result}, output=str(output))
        return True
    finally:
        stop_process(process)
        capture.finish()


def main():
    import pika

    from app.api.jobs import ROOT

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("pika").setLevel(logging.WARNING)
    stopping = Event()

    def stop(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    data_dir = ROOT / "runs" / "web"
    data_dir.mkdir(parents=True, exist_ok=True)
    jobs = EvaluationJobs(data_dir)
    while not stopping.is_set():
        try:
            with broker_connection() as connection:
                channel = evaluation_channel(connection)
                channel.basic_qos(prefetch_count=1)
                LOGGER.info("Evaluation Worker is listening on %s", QUEUE)
                for method, _, body in channel.consume(QUEUE, inactivity_timeout=1):
                    if stopping.is_set():
                        break
                    if method is None:
                        continue
                    if not re.fullmatch(rb"[0-9a-f]{32}", body):
                        channel.basic_reject(method.delivery_tag, requeue=False)
                    elif execute_delivery(jobs, body.decode("ascii"), connection, stopping):
                        channel.basic_ack(method.delivery_tag)
                    else:
                        break
        except (pika.exceptions.AMQPError, OSError, sqlite3.Error) as exc:
            LOGGER.warning("Evaluation Worker is reconnecting (%s)", type(exc).__name__)
            stopping.wait(2)


if __name__ == "__main__":
    main()
