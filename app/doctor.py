"""Read-only local readiness checks; no model calls, downloads or config changes."""

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit

from app.runtime.tokenizer import SHA256, URL

ROOT = Path(__file__).resolve().parents[1]


def inspect_environment(*, workspace=ROOT / "examples/demo_project", port=None, live=False):
    checks = []

    def add(name, ok, detail, required=True):
        checks.append({"name": name, "ok": ok, "detail": detail, "required": required})

    add("Python", sys.version_info >= (3, 11), sys.version.split()[0])
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for requirement in project["project"]["dependencies"]:
        name = re.match(r"[A-Za-z0-9_-]+", requirement).group()
        try:
            installed = version(name)
            pinned = requirement.split("==", 1)[1] if "==" in requirement else None
            add(name, not pinned or installed == pinned, f"已安装 {installed}；要求 {requirement}")
        except PackageNotFoundError:
            add(name, False, "缺少依赖，请按README安装项目依赖。")

    cache = Path(os.environ.get("TIKTOKEN_CACHE_DIR") or ROOT / ".cache/tiktoken")
    target = cache / hashlib.sha1(URL.encode()).hexdigest()
    try:
        valid = target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == SHA256
    except OSError:
        valid = False
    add("分词缓存", valid, "校验通过" if valid else "请运行 python scripts/prepare_tokenizer.py。")
    add("项目目录", Path(workspace).is_dir(), str(Path(workspace).resolve()))
    assets = [
        ROOT / "app/api/static/index.html",
        ROOT / "app/api/backlog.json",
        ROOT / "evals/datasets/agent_dev/tasks_v2.json",
    ]
    add("应用与评测资源", all(path.is_file() for path in assets), "网页、待办和开发题集")
    if port is not None:
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
            add("本地端口", True, f"{port} 当前可绑定；启动时仍可能被其他进程占用。")
        except OSError:
            add("本地端口", False, f"{port} 不可绑定；如已有工作台运行，可直接打开或另选端口。")

    values = dict(os.environ)
    try:
        from dotenv import dotenv_values

        values = {**dotenv_values(ROOT / ".env"), **values}
    except ImportError:
        pass
    configured = all(
        str(values.get(name) or "").strip()
        for name in ("PROJECTPILOT_API_KEY", "PROJECTPILOT_API_BASE", "PROJECTPILOT_MODEL")
    )
    try:
        url = urlsplit(values.get("PROJECTPILOT_API_BASE") or "")
        configured = bool(
            configured
            and url.scheme in {"http", "https"}
            and url.hostname
            and not url.username
            and not url.password
        )
    except ValueError:
        configured = False
    add(
        "真实模型配置",
        configured,
        "配置项齐全，未请求服务验证。"
        if configured
        else "配置不完整或地址格式有误；离线演示不需要密钥。",
        required=live,
    )
    return {"ready": all(item["ok"] for item in checks if item["required"]), "checks": checks}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="ProjectPilot 启动前检查，不修改配置或调用模型")
    parser.add_argument("--workspace", type=Path, default=ROOT / "examples/demo_project")
    parser.add_argument("--port", type=int, metavar="PORT")
    parser.add_argument("--live", action="store_true", help="将真实模型配置作为必需项")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果，不含密钥值")
    args = parser.parse_args()
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("端口必须在1到65535之间。")
    result = inspect_environment(workspace=args.workspace, port=args.port, live=args.live)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in result["checks"]:
            label = "通过" if item["ok"] else ("待处理" if item["required"] else "可选")
            print(f"[{label}] {item['name']}：{item['detail']}")
        print("本地运行准备就绪。" if result["ready"] else "请先处理以上必需项。")
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
