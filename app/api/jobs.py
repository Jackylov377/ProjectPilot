"""One owned subprocess at a time, with durable job receipts and bounded lifetime."""

import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.api.diagnostics import StderrCapture, clean_text, clean_value, failure_details

ROOT = Path(__file__).resolve().parents[2]
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


class BusyError(ValueError):
    pass


class JobManager:
    def __init__(self, directory: Path, *, secrets=()):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.processes = {}
        self.captures = {}
        self.invalid_records = set()
        self.pending_saves = {}
        self.secrets = tuple(
            filter(None, (*secrets, os.getenv("PROJECTPILOT_API_KEY", "")))
        )
        self.lock = threading.RLock()
        for job_id in self._job_ids():
            job = self._load(job_id)
            if job["status"] == "running":
                job.update(
                    status="interrupted",
                    finished_at=now(),
                    error="服务曾中断；任务未自动重放。",
                    error_hint="请先查看已保存的结果，再决定是否重新提交。",
                )
                self._save(job_id, job)

    @staticmethod
    def _valid_id(job_id):
        return (
            isinstance(job_id, str)
            and len(job_id) == 32
            and all(c in "0123456789abcdef" for c in job_id)
        )

    def _job_ids(self):
        return [
            path.name
            for path in self.directory.iterdir()
            if self._valid_id(path.name) and path.is_dir() and not path.is_symlink()
        ]

    def _diagnose(self, job_id, **data):
        path = self.directory / job_id / "diagnostic.json"
        try:
            write(
                path,
                {
                    "job_id": job_id,
                    "recorded_at": now(),
                    **clean_value(data, self.secrets),
                },
            )
        except OSError:
            # An unwritable historical directory must not stop other jobs.
            pass

    def _load(self, job_id):
        if job_id in self.pending_saves:
            return dict(self.pending_saves[job_id])
        try:
            job = read(self.directory / job_id / "job.json")
            if (
                not isinstance(job, dict)
                or job.get("id") != job_id
                or job.get("status") not in TERMINAL | {"running"}
                or job.get("kind") not in {"agent", "evaluation", "repository_import"}
                or not isinstance(job.get("request"), dict)
                or not isinstance(job.get("created_at"), str)
                or type(job.get("timeout")) not in (int, float)
                or not math.isfinite(job["timeout"])
                or job["timeout"] <= 0
                or ("result" in job and not isinstance(job["result"], dict))
            ):
                raise ValueError("Invalid job receipt schema")
            datetime.fromisoformat(job["created_at"])
            if job["kind"] == "evaluation" and not isinstance(
                job["request"].get("output"), str
            ):
                raise ValueError("Missing evaluation output")
            return job
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            # Leave the original bytes in place for recovery, never overwrite a bad receipt.
            if job_id not in self.invalid_records:
                self._diagnose(
                    job_id,
                    error_code="record_invalid",
                    exception_type=type(exc).__name__,
                )
                self.invalid_records.add(job_id)
            return {
                "id": job_id,
                "kind": "invalid",
                "status": "failed",
                "request": {},
                "created_at": "",
                "record_invalid": True,
                "exception_type": type(exc).__name__,
                **failure_details(stop_reason="record_invalid"),
            }

    def _save(self, job_id, job):
        try:
            write(self.directory / job_id / "job.json", job)
            self.pending_saves.pop(job_id, None)
        except OSError as exc:
            job["persistence_warning"] = (
                "任务状态暂时无法写入本地文件；请检查目录权限和磁盘空间。"
            )
            self.pending_saves[job_id] = dict(job)
            self._diagnose(
                job_id,
                error_code="record_write_failed",
                exception_type=type(exc).__name__,
            )

    def _finish(self, job):
        job_id = job["id"]
        capture = self.captures.pop(job_id, None)
        stderr = capture.finish() if capture else ""
        self._diagnose(
            job_id,
            status=job["status"],
            error_code=job.get("error_code"),
            error=job.get("error"),
            error_hint=job.get("error_hint"),
            exit_code=self.processes[job_id][0].poll(),
            stderr_tail=stderr,
        )
        if not job.get("record_invalid"):
            self._save(job_id, job)
        del self.processes[job_id]

    def path(self, job_id):
        if not self._valid_id(job_id):
            raise KeyError(job_id)
        path = self.directory / job_id
        if not path.is_dir() or path.is_symlink():
            raise KeyError(job_id)
        return path

    def submit(self, kind, request, *, timeout):
        with self.lock:
            for job in self.list():
                if job["status"] == "running":
                    raise BusyError("当前已有任务运行，请等待完成或先停止它。")
            job_id = uuid4().hex
            directory = self.directory / job_id
            directory.mkdir()
            job = {
                "id": job_id,
                "kind": kind,
                "status": "running",
                "created_at": now(),
                "request": request,
                "timeout": timeout,
            }
            write(directory / "job.json", job)
            if kind == "agent":
                command = [sys.executable, "-m", "app.api.worker", str(directory)]
            elif kind == "repository_import":
                command = [sys.executable, "-m", "app.api.github", str(directory)]
            else:
                command = [
                    sys.executable,
                    "-m",
                    "app.evaluation",
                    "--output",
                    request["output"],
                    "--configs",
                    *request["configs"],
                    "--repeats",
                    str(request["repeats"]),
                ]
                if request.get("cases"):
                    command += ["--cases", *request["cases"]]
                if request["mode"] == "live":
                    command += ["--live"]
            try:
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    start_new_session=os.name != "nt",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except OSError as exc:
                job.update(
                    status="failed",
                    finished_at=now(),
                    **failure_details(stop_reason="start_failed"),
                )
                self._diagnose(
                    job_id, error_code="start_failed", exception_type=type(exc).__name__
                )
                self._save(job_id, job)
                return job
            self.processes[job_id] = (process, time.monotonic())
            self.captures[job_id] = StderrCapture(process.stderr, self.secrets)
            return job

    def _stop(self, process):
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)

    def get(self, job_id):
        with self.lock:
            directory = self.path(job_id)
            job = self._load(job_id)
            owned = self.processes.get(job_id)
            if owned:
                process, started = owned
                if job.get("record_invalid"):
                    self._stop(process)
                elif (
                    process.poll() is None
                    and time.monotonic() - started > job["timeout"]
                ):
                    self._stop(process)
                    job.update(
                        status="failed",
                        **failure_details(stop_reason="timeout"),
                        stop_reason="timeout",
                        finished_at=now(),
                    )
                elif process.poll() is not None:
                    actual = directory / "result.json"
                    valid_result = True
                    if job["kind"] == "evaluation":
                        actual = Path(job["request"]["output"]) / "report.json"
                    if actual.is_file():
                        try:
                            result = read(actual)
                            if not isinstance(result, dict):
                                raise ValueError("Result must be an object")
                            if job["kind"] == "evaluation":
                                summary = result.get("summary")
                                if not isinstance(summary, dict) or not isinstance(
                                    summary.get("groups"), list
                                ):
                                    raise ValueError("Missing evaluation summary")
                                result = {"summary": summary}
                            elif not isinstance(result.get("stop_reason"), str):
                                raise ValueError("Missing task stop reason")
                            job["result"] = result
                        except (OSError, ValueError, TypeError):
                            valid_result = False
                            result = failure_details(stop_reason="result_invalid")
                    else:
                        result = {}
                    ok = (
                        process.returncode == 0
                        and valid_result
                        and bool(result)
                        and (
                            job["kind"] == "evaluation"
                            or result.get("stop_reason") == "completed"
                        )
                    )
                    job.update(
                        status="completed" if ok else "failed", finished_at=now()
                    )
                    if not ok:
                        job.update(
                            failure_details(stop_reason=result.get("stop_reason", ""))
                        )
                        for key in ("error", "error_code", "error_hint"):
                            if isinstance(result.get(key), str):
                                job[key] = clean_text(result[key], self.secrets)
                if job["status"] in TERMINAL:
                    self._finish(job)
            return job

    def list(self):
        with self.lock:
            return sorted(
                (self.get(job_id) for job_id in self._job_ids()),
                key=lambda job: job["created_at"],
                reverse=True,
            )

    def cancel(self, job_id):
        with self.lock:
            job = self.get(job_id)
            if job["status"] == "running":
                self._stop(self.processes[job_id][0])
                job.update(status="cancelled", finished_at=now())
                self._finish(job)
            return job

    def close(self):
        for job_id in list(self.processes):
            self.cancel(job_id)
