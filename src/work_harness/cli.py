from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from pathlib import Path

from work_harness.config.loader import default_project_root, load_app_config
from work_harness.domain.models import SkillSelection, TaskRequest
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
    run.add_argument(
        "--skill",
        action="append",
        default=[],
        metavar="SKILL[@PROVIDER]",
        help="Select a Skill and optionally pin its Provider; may be repeated",
    )

    subparsers.add_parser("check-config", help="Validate configuration and references")
    subparsers.add_parser("export-schemas", help="Export Pydantic contracts as JSON Schema")
    skills = subparsers.add_parser("skills", help="List or validate project Skills")
    skills.add_argument("action", choices=["list", "validate"])
    subparsers.add_parser(
        "check-compatibility",
        help="Validate Provider, Tool, and Skill compatibility",
    )

    evaluate = subparsers.add_parser("eval", help="Run the fixed offline evaluation set")
    evaluate.add_argument("--cases", type=Path)
    evaluate.add_argument("--output", type=Path)

    serve = subparsers.add_parser("serve", help="Start the Agent API")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    return parser


async def _run_task(args: argparse.Namespace) -> int:
    selections = []
    for raw in args.skill:
        skill_id, separator, provider = raw.partition("@")
        selections.append(
            SkillSelection(
                skill_id=skill_id,
                provider=provider if separator else None,
            )
        )
    async with build_runtime(args.project_root) as runtime:
        manager = RunManager(runtime)
        run_id = await manager.start(
            TaskRequest(
                goal=args.goal,
                task_type=args.task_type,
                risk_level=args.risk_level,
                skills=selections,
            )
        )
        result = await manager.wait(run_id)
        print(result.model_dump_json(indent=2))
        return 0 if result.status in {"succeeded", "waiting_human"} else 1


async def _run_eval(args: argparse.Namespace) -> int:
    root = args.project_root.resolve()
    cases = args.cases or root / "evaluation" / "eval_cases.jsonl"
    configured_output = os.getenv("WORK_HARNESS_EVALUATION_OUTPUT", "").strip()
    output = args.output or (
        Path(configured_output)
        if configured_output
        else root / "evaluation" / "reports" / "latest.json"
    )
    async with build_runtime(root, checkpoint_backend="memory") as runtime:
        report = await run_evaluation(runtime, cases)
    save_report(report, output)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["bad_case_count"] == 0 else 1


async def _inspect_compatibility(args: argparse.Namespace) -> int:
    async with build_runtime(args.project_root, checkpoint_backend="memory") as runtime:
        skills = [item.model_dump(mode="json") for item in runtime.skill_registry.descriptors()]
        if args.command == "skills" and args.action == "list":
            payload: object = skills
        else:
            payload = {
                "status": "ok",
                "providers": runtime.provider_compatibility.model_dump(mode="json"),
                "provider_adapters": runtime.provider_adapters.adapters,
                "tool_adapters": runtime.tool_adapters.adapters,
                "agent_routes": {
                    name: route.model_dump(mode="json")
                    for name, route in runtime.config.models.agent_routes.items()
                },
                "skills": skills,
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


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
                        "provider_store": config.runtime.provider_store.backend,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "export-schemas":
        from work_harness.schema_export import export_schemas

        configured_output = os.getenv("WORK_HARNESS_SCHEMA_OUTPUT", "").strip()
        output = Path(configured_output) if configured_output else args.project_root / "schemas"
        exported = export_schemas(output)
        print(json.dumps([str(path) for path in exported], ensure_ascii=False, indent=2))
        return 0
    if args.command == "run":
        return asyncio.run(_run_task(args))
    if args.command == "eval":
        return asyncio.run(_run_eval(args))
    if args.command in {"skills", "check-compatibility"}:
        return asyncio.run(_inspect_compatibility(args))
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
