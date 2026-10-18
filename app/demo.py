"""Prepare isolated offline demo receipts using the actual application job workers."""

import argparse
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app.api.jobs import ROOT, TERMINAL, JobManager, write
from app.doctor import inspect_environment

PROJECT_FILES = {
    "README.md": "# CedarLab（虚构演示夹具）\n\n用于演示实验分数的整理。\n"
    "pipeline.py 提供分数归一化与汇总函数。\n这里只用于离线流程展示。\n",
    "overview.md": "CedarLab 是虚构实验记录项目，整理分数和结果；仅作为离线工具演示夹具。\n",
    "stack.md": "Python处理实验分数，本地文本保存说明；这是测试资料。\n",
    "pipeline.py": "def normalize(scores):\n    total = sum(scores)\n"
    "    return [score / total for score in scores] if total else []\n\n\n"
    "def summarize(scores):\n    return {'count': len(scores), 'total': sum(scores)}\n",
}


def prepare_demo(output, progress=print):
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError("演示目录已存在，请换一个新目录；已有数据不会覆盖。")
    if not inspect_environment()["ready"]:
        raise RuntimeError("本地环境尚未就绪，请先运行 python -m app.doctor。")
    output.mkdir(parents=True)
    workspace, data = output / "project", output / "web"
    workspace.mkdir()
    for name, content in PROJECT_FILES.items():
        (workspace / name).write_text(content, encoding="utf-8")
    knowledge = data / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "demo-guide.txt").write_text(
        "离线演示资料：单个工具执行超时为15秒，整个回合执行超时为90秒。\n",
        encoding="utf-8",
    )
    manifest = {
        "mode": "offline",
        "interpretation": "真实执行预设Provider与应用任务链，不代表真实模型能力或阶段验收。",
        "workspace": str(workspace),
        "data_dir": str(data),
        "status": "preparing",
        "jobs": [],
        "launch_args": [
            sys.executable,
            "-m",
            "app.api",
            "--workspace",
            str(workspace),
            "--data-dir",
            str(data),
            "--port",
            "8766",
        ],
    }
    receipt = output / "demo.json"
    write(receipt, manifest)
    manager = JobManager(data / "jobs")

    def run(label, kind, request, timeout):
        progress(f"正在准备：{label}")
        job = manager.submit(kind, request, timeout=timeout)
        while job["status"] not in TERMINAL:
            time.sleep(0.1)
            job = manager.get(job["id"])
        manifest["jobs"].append({"label": label, "id": job["id"], "status": job["status"]})
        write(receipt, manifest)
        if job["status"] != "completed":
            raise RuntimeError(f"{label} 未完成；已保留任务状态和运行证据。")
        return job

    def agent(label, prompt, session, *, engine="session", memory=False):
        return run(
            label,
            "agent",
            {
                "prompt": prompt,
                "session": session,
                "engine": engine,
                "mode": "offline",
                "context_policy": "window",
                "memory": memory,
                "documents": ["demo-guide.txt"] if engine == "knowledge" else [],
                "workspace": str(knowledge if engine == "knowledge" else workspace),
                "data_dir": str(data),
            },
            115,
        )

    try:
        agent("项目笔记与工具过程", "读取 overview，说明项目用途并引用来源。", "demo-notes")
        agent("保存模型决定", "我们决定使用模型 A。", "demo-memory", memory=True)
        agent("更新模型决定", "我们决定改用模型 B。", "demo-memory", memory=True)
        agent("新会话召回当前决定", "当前项目模型是什么？请说明来源。", "demo-recall", memory=True)
        agent(
            "知识检索与引用",
            "单个工具执行超时和整个回合执行超时有什么区别？",
            "demo-knowledge",
            engine="knowledge",
        )
        agent(
            "仓库工具与报告落盘",
            "分析演示仓库并保存报告，引用文件行号。",
            "demo-repository",
            engine="repository",
        )
        evaluation = run(
            "三题开发评测",
            "evaluation",
            {
                "configs": ["full"],
                "cases": ["note-read", "knowledge-fact", "memory-model-change"],
                "repeats": 1,
                "mode": "offline",
                "output": str(data / "evaluations/demo-baseline"),
            },
            720,
        )
        manifest["evaluation_summary"] = evaluation["result"]["summary"]
        manifest["status"] = "ready"
    except Exception:
        manifest["status"] = "failed"
        raise
    finally:
        manager.close()
        write(receipt, manifest)
    return manifest


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="生成独立的离线演示，不覆盖当前工作台")
    parser.add_argument("--output", type=Path, help="必须是新目录；默认在runs下生成唯一目录")
    args = parser.parse_args()
    output = args.output or ROOT / "runs" / (
        "demo-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:6]
    )
    try:
        result = prepare_demo(output)
    except (OSError, RuntimeError) as exc:
        print(f"演示准备未完成：{exc}")
        return 1
    print(f"演示已准备：{output.resolve() / 'demo.json'}")
    print("全部为离线预设响应，不是模型能力成绩。按以下命令打开独立工作台：")
    command = (subprocess.list2cmdline if os.name == "nt" else shlex.join)(result["launch_args"])
    print(("& " if os.name == "nt" else "") + command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
