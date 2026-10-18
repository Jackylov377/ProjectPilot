"""python -m app.evaluation: run a fixed dataset or replay saved evidence."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.evaluation.runner import CONFIGS, ROOT, replay, run, worker


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="独立任务评测；默认离线，不请求模型")
    parser.add_argument(
        "--dataset", type=Path, default=ROOT / "evals/datasets/agent_dev/tasks_v2.json"
    )
    parser.add_argument("--output", type=Path, help="新的结果目录，不覆盖旧运行")
    parser.add_argument("--configs", nargs="+", choices=list(CONFIGS), default=["full", "window"])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--cases", nargs="+", help="运行前选择任务，选择结果写入冻结清单")
    parser.add_argument("--live", action="store_true", help="将合成场景发给 .env 配置的模型服务")
    parser.add_argument("--replay", type=Path, help="只读取原始结果和 Trace 重新判分，无模型调用")
    parser.add_argument("--review-template", type=Path, help="为原批次的待复核任务生成空白复核表")
    parser.add_argument("--review-batch", type=Path, help="待导入复核结论的原批次")
    parser.add_argument("--review-file", type=Path, help="已填写的复核JSON，不覆盖自动成绩")
    parser.add_argument("--review-output", type=Path, help="批次目录外的新JSON文件，拒绝覆盖")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.review_template or args.review_batch or args.review_file or args.review_output:
        from app.evaluation.review import reviewed_report, save_sidecar, template
        from app.evaluation.runner import read

        batch = args.review_template or args.review_batch
        if (
            not batch
            or not args.review_output
            or args.replay
            or args.live
            or args.output
            or args.worker
            or args.cases
            or args.repeats != 1
            or args.configs != ["full", "window"]
            or args.dataset.resolve() != ROOT / "evals/datasets/agent_dev/tasks_v2.json"
            or (args.review_template and (args.review_batch or args.review_file))
            or (args.review_batch and not args.review_file)
        ):
            parser.error(
                "复核需指定原批次及新 --review-output；导入时还需 --review-file，不能混合运行参数"
            )
        try:
            result = (
                template(batch)
                if args.review_template
                else reviewed_report(batch, read(args.review_file))
            )
            save_sidecar(batch, args.review_output, result)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(str(exc))
        print(f"独立复核文件已保存：{args.review_output.resolve()}；原始成绩不变。")
        return
    if args.worker:
        asyncio.run(worker(args.worker))
        return
    if args.replay:
        if args.live or args.output:
            parser.error("回放只读，不接受 --live 或 --output")
        result = replay(args.replay)
    else:
        if not args.output or args.repeats < 1 or len(args.configs) != len(set(args.configs)):
            parser.error("需要新 --output 目录、正数 repeats 和无重复 configs")
        result = run(
            args.dataset.resolve(), args.output, args.configs, args.repeats, args.live, args.cases
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
