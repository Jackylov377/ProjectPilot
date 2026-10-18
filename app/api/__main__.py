"""python -m app.api launches the local workbench with one server worker."""

import argparse
from pathlib import Path

import uvicorn

from app.api.jobs import ROOT
from app.api.server import create_app


def main():
    parser = argparse.ArgumentParser(description="ProjectPilot 本地工作台")
    parser.add_argument("--workspace", type=Path, default=ROOT / "examples/demo_project")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "runs/web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-live", action="store_true", help="允许在界面选择已配置的真实模型")
    args = parser.parse_args()
    app = create_app(workspace=args.workspace, data_dir=args.data_dir, allow_live=args.allow_live)
    uvicorn.run(app, host=args.host, port=args.port, workers=1, log_level="warning")


if __name__ == "__main__":
    main()
