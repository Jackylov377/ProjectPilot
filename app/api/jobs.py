"""One owned subprocess at a time, with durable job receipts and bounded lifetime."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


class BusyError(ValueError):
    pass


class JobManager:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.processes = {}
        self.lock = threading.RLock()
        for path in self.directory.glob("*/job.json"):
            job = read(path)
            if job["status"] == "running":
                job.update(
                    status="interrupted", finished_at=now(), error="服务曾中断；任务未自动重放。"
                )
                write(path, job)

    def path(self, job_id):
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise KeyError(job_id)
        path = self.directory / job_id
        if not (path / "job.json").is_file():
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
                    stderr=subprocess.DEVNULL,
                    start_new_session=os.name != "nt",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except OSError:
                job.update(status="failed", error="任务进程未能启动。", finished_at=now())
                write(directory / "job.json", job)
                return job
            self.processes[job_id] = (process, time.monotonic())
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
            job = read(directory / "job.json")
            owned = self.processes.get(job_id)
            if owned:
                process, started = owned
                if process.poll() is None and time.monotonic() - started > job["timeout"]:
                    self._stop(process)
                    job.update(
                        status="failed",
                        error="任务达到时间上限。",
                        stop_reason="timeout",
                        finished_at=now(),
                    )
                elif process.poll() is not None:
                    actual = directory / "result.json"
                    if job["kind"] == "evaluation":
                        actual = Path(job["request"]["output"]) / "report.json"
                    if actual.is_file():
                        result = read(actual)
                        if job["kind"] == "evaluation":
                            result = {"summary": result["summary"]}
                        job["result"] = result
                    else:
                        result = {}
                    ok = (
                        process.returncode == 0
                        and bool(result)
                        and (
                            job["kind"] == "evaluation" or result.get("stop_reason") == "completed"
                        )
                    )
                    job.update(status="completed" if ok else "failed", finished_at=now())
                    if not ok:
                        job["error"] = result.get("error", "任务未正常完成，已保留原始运行记录。")
                if job["status"] in TERMINAL:
                    write(directory / "job.json", job)
                    del self.processes[job_id]
            return job

    def list(self):
        with self.lock:
            return sorted(
                (self.get(path.parent.name) for path in self.directory.glob("*/job.json")),
                key=lambda job: job["created_at"],
                reverse=True,
            )

    def cancel(self, job_id):
        with self.lock:
            job = self.get(job_id)
            if job["status"] == "running":
                self._stop(self.processes.pop(job_id)[0])
                job.update(status="cancelled", finished_at=now())
                write(self.path(job_id) / "job.json", job)
            return job

    def close(self):
        for job_id in list(self.processes):
            self.cancel(job_id)
