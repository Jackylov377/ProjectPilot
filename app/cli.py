"""Local smoke-demo entry point."""

import argparse
import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from loguru import logger
from nanobot.agent.context_governance import ContextWindowExceededError
from nanobot.providers.openai_compat_provider import OpenAICompatProvider

from app.context import ContextOptions
from app.memory.offline import OfflineMemoryProvider
from app.memory.service import MemoryOptions
from app.observability.trace import JsonlTrace
from app.runtime.answer import AnswerValidationError
from app.runtime.knowledge import KNOWLEDGE_PROMPT, KnowledgeOptions
from app.runtime.offline import (
    OfflineDemoProvider,
    OfflineKnowledgeProvider,
    OfflineRepositoryProvider,
)
from app.runtime.repository import REPOSITORY_PROMPT, RepositoryOptions, ToolExecutionLimit
from app.runtime.session import run_session, state_location
from app.runtime.smoke import DEFAULT_PROMPT, run_smoke


async def _run(args: argparse.Namespace) -> int:
    # Read only the explicitly named local file; do not discover parent .env files.
    load_dotenv(args.env_file, override=False)
    key = os.getenv("PROJECTPILOT_API_KEY", "").strip()
    if args.mode == "live":
        base = os.getenv("PROJECTPILOT_API_BASE", "").strip()
        model = os.getenv("PROJECTPILOT_MODEL", "").strip()
        if not all((key, base, model)):
            print("请先在本地 .env 配置 PROJECTPILOT_API_KEY、API_BASE 和 MODEL。")
            return 2
        url = urlsplit(base)
        if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
            print("API_BASE 必须是无用户名和密码的 HTTP(S) 地址。")
            return 2
        provider = OpenAICompatProvider(
            api_key=key,
            api_base=base,
            default_model=model,
            provider_name="projectpilot-compatible",
        )
    else:
        provider_type = {
            "repository": OfflineRepositoryProvider,
            "knowledge": OfflineKnowledgeProvider,
        }.get(args.engine, OfflineDemoProvider)
        if args.memory and args.engine == "session":
            provider_type = OfflineMemoryProvider
        provider = provider_type()
    trace = JsonlTrace(args.trace_dir, secrets=(key,))
    print(f"模式：{args.mode}；Trace：{trace.path}")
    try:
        session_engine = args.engine in {"session", "repository", "knowledge"}
        runner = run_session if session_engine else run_smoke
        session_options = {}
        if session_engine:
            session_options = {
                "state_dir": state_location(
                    args.state_dir / args.engine
                    if args.engine in {"repository", "knowledge"}
                    else args.state_dir,
                    args.workspace,
                    args.mode,
                ),
                "session_id": args.session,
            }
            if args.memory:
                session_options["memory_options"] = MemoryOptions(
                    args.memory_db or args.state_dir / "memory.sqlite3",
                    mode=args.mode,
                    owner_id=args.memory_owner,
                )
            if args.context_policy:
                session_options["context_options"] = ContextOptions(
                    strategy=args.context_policy,
                    window_tokens=args.context_window or 16384,
                    recent_turns=args.context_turns
                    or (2 if args.context_policy == "summary" else 4),
                    max_output_tokens=args.context_output_tokens,
                )
            if args.engine == "repository":
                session_options["repository_options"] = RepositoryOptions(
                    args.report_dir,
                    args.tool_timeout,
                    args.max_tool_calls,
                    args.max_repeated_calls,
                    mcp_enabled=args.mcp,
                    report_path_only=args.report_path_only,
                    append_report=args.append_report,
                )
            elif args.engine == "knowledge":
                session_options["knowledge_options"] = KnowledgeOptions(
                    paths=tuple(args.knowledge_files),
                    max_chars=args.chunk_chars,
                    overlap_chars=args.chunk_overlap,
                    max_tool_calls=args.max_tool_calls,
                    max_repeated_calls=args.max_repeated_calls,
                )
        result = await runner(
            provider,
            args.workspace,
            trace,
            prompt=args.prompt,
            max_iterations=args.max_iterations,
            timeout_seconds=args.timeout,
            **session_options,
        )
    except ContextWindowExceededError:
        print(
            "当前内容超过上下文预算。可选择 window 策略、缩小问题或减少当前工具返回内容；原始会话保留。"
        )
        return 1
    except TimeoutError:
        print("运行已达到时间上限，详情已记录到 Trace。")
        return 1
    except ToolExecutionLimit as exc:
        print(f"运行已停止：{exc.reason}；详情已记录到 Trace。")
        return 1
    except AnswerValidationError:
        print("回答经一次纠正仍未满足输出要求，已停止；失败原因已保留在 Trace 运行记录中。")
        return 1
    except Exception as exc:
        # Do not print an SDK exception body that might contain credentials.
        print(f"运行失败：{type(exc).__name__}，请查看 Trace。")
        if isinstance(exc, RuntimeError) and "分词缓存" in str(exc):
            print("请先运行 python scripts/prepare_tokenizer.py 准备分词缓存。")
        return 1
    finally:
        # The pinned provider has no public close method; its owned HTTP client does.
        client = getattr(provider, "_client", None)
        if client is not None:
            await client.close()
    print(trace.clean(result.final_content or "（没有最终回答）"))
    print(f"停止原因：{result.stop_reason}；成功工具调用：{result.tools_used}")
    if args.engine in {"session", "repository", "knowledge"}:
        print(
            f"会话：{args.session}；恢复 {result.restored_messages} 条，保存后 {result.saved_messages} 条"
        )
        print(f"会话文件：{result.session_path}")
        return 0 if result.stop_reason == "completed" else 1
    return (
        0 if result.stop_reason == "completed" and "read_project_note" in result.tools_used else 1
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="ProjectPilot 最小 Agent 闭环")
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument(
        "--engine", choices=["smoke", "session", "repository", "knowledge"], default="smoke"
    )
    parser.add_argument(
        "--session", default="demo", help="同一个名称续接历史，更换名称开始独立会话"
    )
    parser.add_argument("--state-dir", type=Path, default=Path("runs/state"))
    parser.add_argument(
        "--context-policy",
        choices=["full", "window", "summary"],
        help="历史策略：完整、近期窗口，或带任务状态的摘要",
    )
    parser.add_argument("--context-window", type=int, help="本地总 Token 窗口，包含输出及安全预留")
    parser.add_argument(
        "--context-turns",
        type=int,
        help="window 默认含当前共4回合；summary 默认保留最近2个完整回合",
    )
    parser.add_argument("--context-output-tokens", type=int, help="最大输出 Token 预留")
    parser.add_argument("--memory", action="store_true", help="启用跨会话项目决定与语言偏好记忆")
    parser.add_argument("--memory-db", type=Path, help="记忆数据库；默认位于 state-dir 下")
    parser.add_argument("--memory-owner", default="local", help="本地记忆作用域，不是多用户认证")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--report-dir", type=Path, default=Path("runs/reports"))
    parser.add_argument("--knowledge-files", nargs="+", help="知识问答使用的工作区相对文档路径")
    parser.add_argument("--chunk-chars", type=int, default=1000, help="知识片段最大字符数，1～1000")
    parser.add_argument(
        "--chunk-overlap", type=int, default=0, help="同段落区域或 PDF 页内的重叠字符数"
    )
    parser.add_argument(
        "--append-report", help="仅向报告目录内指定的已有 Markdown 文件追加本次报告，不新建文件"
    )
    parser.add_argument(
        "--mcp", action="store_true", help="仓库模式启用本地只读项目元数据 MCP 工具"
    )
    parser.add_argument(
        "--report-path-only",
        action="store_true",
        help="仓库报告保存成功后直接返回路径，省去最终模型调用",
    )
    parser.add_argument("--tool-timeout", type=float, default=15)
    parser.add_argument("--max-tool-calls", type=int, default=20)
    parser.add_argument("--max-repeated-calls", type=int, default=2)
    parser.add_argument("--trace-dir", type=Path, default=Path("runs"))
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--max-iterations", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    repository = args.engine == "repository"
    knowledge = args.engine == "knowledge"
    if args.context_policy and args.engine == "smoke":
        parser.error("--context-policy 需要 session、repository 或 knowledge 模式")
    if (
        args.context_window is not None or args.context_output_tokens is not None
    ) and not args.context_policy:
        parser.error("上下文窗口与输出预留需要 --context-policy")
    if args.context_turns is not None and args.context_turns < 1:
        parser.error("--context-turns 必须为正数")
    if args.memory and args.engine == "smoke":
        parser.error("--memory 需要 session、repository 或 knowledge 模式")
    if args.memory_db and not args.memory:
        parser.error("--memory-db 需要 --memory")
    if knowledge and not args.knowledge_files:
        parser.error("--engine knowledge 需要 --knowledge-files")
    if args.knowledge_files and not knowledge:
        parser.error("--knowledge-files 需要 --engine knowledge")
    if knowledge and not 1 <= args.chunk_chars <= 1000:
        parser.error("--chunk-chars 必须在 1～1000 之间")
    if knowledge and not 0 <= args.chunk_overlap < args.chunk_chars:
        parser.error("--chunk-overlap 必须非负且小于 --chunk-chars")
    if args.mcp and not repository:
        parser.error("--mcp 需要 --engine repository")
    if args.report_path_only and not repository:
        parser.error("--report-path-only 需要 --engine repository")
    if args.append_report and not repository:
        parser.error("--append-report 需要 --engine repository")
    if args.workspace is None:
        args.workspace = Path(".") if repository or knowledge else Path("examples/demo_project")
    if args.prompt is None:
        args.prompt = (
            REPOSITORY_PROMPT if repository else (KNOWLEDGE_PROMPT if knowledge else DEFAULT_PROMPT)
        )
    if args.max_iterations is None:
        args.max_iterations = 10 if repository else (6 if knowledge else 4)
    if (
        min(
            args.max_iterations,
            args.timeout,
            args.tool_timeout,
            args.max_tool_calls,
            args.max_repeated_calls,
        )
        <= 0
    ):
        parser.error("执行次数与超时必须为正数")
    # Upstream default logs can include raw tool arguments or provider errors.
    # Our application exposes only the explicit, redacted trace and concise console output.
    logger.remove()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
