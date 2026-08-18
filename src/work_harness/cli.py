from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from work_harness.config.loader import default_project_root, load_app_config
from work_harness.domain.models import TaskRequest
from work_harness.evaluation import run_evaluation, save_report
from work_harness.runtime import build_runtime
from work_harness.service import RunManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="work-harness")
    parser.add_argument("--project-root", type=Path, default=default_project_root())
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run one task through the root graph")
    run.add_argument("--goal", required=True)
    run.add_argument("--task-type", default="general")
    run.add_argument("--risk-level", choices=["low", "medium", "high", "critical"], default="low")

    subparsers.add_parser("check-config", help="Validate configuration and references")
    subparsers.add_parser("export-schemas", help="Export Pydantic contracts as JSON Schema")

    evaluate = subparsers.add_parser("eval", help="Run the fixed offline evaluation set")
    evaluate.add_argument("--cases", type=Path)
    evaluate.add_argument("--output", type=Path)

    serve = subparsers.add_parser("serve", help="Start the Agent API")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    return parser


async def _run_task(args: argparse.Namespace) -> int:
    async with build_runtime(args.project_root) as runtime:
        manager = RunManager(runtime)
        run_id = await manager.start(
            TaskRequest(goal=args.goal, task_type=args.task_type, risk_level=args.risk_level)
        )
        result = await manager.wait(run_id)
        print(result.model_dump_json(indent=2))
        return 0 if result.status in {"succeeded", "waiting_human"} else 1


async def _run_eval(args: argparse.Namespace) -> int:
    root = args.project_root.resolve()
    cases = args.cases or root / "evaluation" / "eval_cases.jsonl"
    output = args.output or root / "evaluation" / "reports" / "latest.json"
    async with build_runtime(root, checkpoint_backend="memory") as runtime:
        report = await run_evaluation(runtime, cases)
    save_report(report, output)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["bad_case_count"] == 0 else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check-config":
        config = load_app_config(args.project_root)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "profile": config.models.profile,
                    "agents": sorted(config.agents.agents),
                    "logical_models": sorted(config.models.logical_models),
                    "tools": sorted(config.tools.tools),
                    "persistence": {
                        "checkpoint": config.runtime.checkpoint.backend,
                        "run_store": config.runtime.run_store.backend,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "export-schemas":
        from work_harness.schema_export import export_schemas

        exported = export_schemas(args.project_root / "schemas")
        print(json.dumps([str(path) for path in exported], ensure_ascii=False, indent=2))
        return 0
    if args.command == "run":
        return asyncio.run(_run_task(args))
    if args.command == "eval":
        return asyncio.run(_run_eval(args))
    if args.command == "serve":
        import uvicorn

        from work_harness.api.app import create_app

        config = load_app_config(args.project_root)
        uvicorn.run(
            create_app(args.project_root),
            host=args.host or config.api.host,
            port=args.port or config.api.port,
        )
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
