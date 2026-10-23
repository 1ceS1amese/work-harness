from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from work_harness.config.loader import default_project_root, load_app_config
from work_harness.domain.models import BlackboardTaskInput, SkillSelection, TaskRequest
from work_harness.evaluation import prepare_comparison, run_evaluation, save_report
from work_harness.knowledge_sources.deliverables import DELIVERABLE_TEMPLATES
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
        "--engine", choices=["legacy", "blackboard"], help="Override the configured engine"
    )
    acceptance = run.add_mutually_exclusive_group()
    acceptance.add_argument(
        "--acceptance-template",
        choices=[*(template.id for template in DELIVERABLE_TEMPLATES), "saved-draft"],
        help="Choose a document deliverable; saved-draft preserves the older storage-only check",
    )
    acceptance.add_argument(
        "--blackboard-spec",
        type=Path,
        help="JSON file containing topic and typed acceptance criteria for a blackboard task",
    )
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
    subparsers.add_parser(
        "migrate",
        help="Create or upgrade PostgreSQL queue, blackboard and checkpoint tables",
    )

    evaluate = subparsers.add_parser("eval", help="Run the fixed offline evaluation set")
    evaluate.add_argument("--cases", type=Path)
    evaluate.add_argument("--output", type=Path)
    evaluation_mode = evaluate.add_mutually_exclusive_group()
    evaluation_mode.add_argument(
        "--prepare-comparison",
        action="store_true",
        help="Write an unexecuted comparison template without loading providers or credentials",
    )
    evaluation_mode.add_argument(
        "--compare-engines",
        action="store_true",
        help="Run the frozen real-provider comparison manifest for both engines",
    )
    evaluation_mode.add_argument(
        "--reassess-comparison",
        type=Path,
        metavar="REPORT",
        help="Apply the current cost-exclusion policy to a frozen report without model calls",
    )

    serve = subparsers.add_parser("serve", help="Start the Agent API")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    subparsers.add_parser("worker", help="Consume durable PostgreSQL run jobs")
    subparsers.add_parser(
        "worker-health",
        help="Check that this distributed Worker has a fresh database heartbeat",
    )
    return parser


async def _run_task(args: argparse.Namespace) -> int:
    config = load_app_config(args.project_root)
    if config.runtime.execution.role != "embedded":
        raise ValueError("the `run` command requires the embedded execution role")
    engine = args.engine or config.runtime.workflow.engine
    blackboard = None
    if args.acceptance_template:
        from work_harness.knowledge_sources.catalog import acceptance_task_input

        blackboard = acceptance_task_input(args.acceptance_template)
    elif args.blackboard_spec:
        blackboard = BlackboardTaskInput.model_validate_json(
            args.blackboard_spec.read_text(encoding="utf-8")
        )
    if engine == "blackboard" and blackboard is None:
        raise ValueError(
            "blackboard runs require --blackboard-spec or an explicit --acceptance-template; "
            "choose analysis-report, implementation-plan or action-checklist"
        )
    if engine == "legacy" and blackboard is not None:
        raise ValueError("blackboard acceptance requires --engine blackboard")
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
        try:
            run_id = await manager.start(
                TaskRequest(
                    goal=args.goal,
                    engine=args.engine,
                    blackboard=blackboard,
                    task_type=args.task_type,
                    risk_level=args.risk_level,
                    skills=selections,
                )
            )
            result = await manager.wait(run_id)
            print(result.model_dump_json(indent=2))
            return 0 if result.status in {"succeeded", "waiting_human"} else 1
        finally:
            await manager.close()


async def _run_eval(args: argparse.Namespace) -> int:
    root = args.project_root.resolve()
    if args.reassess_comparison:
        from work_harness.evaluation_comparison import ComparisonSpec, reassess_comparison

        cases = args.cases or root / "evaluation" / "comparison.json"
        spec = ComparisonSpec.model_validate_json(cases.read_text(encoding="utf-8"))
        output = args.output or args.reassess_comparison.with_name(
            args.reassess_comparison.stem + "-reassessed.json"
        )
        report = reassess_comparison(args.reassess_comparison, spec, output)
        print(
            json.dumps(
                {
                    "gates": report["gates"],
                    "excluded_gates": report["excluded_gates"],
                    "output": str(output),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if report["comparison_gates_passed"] else 1
    if args.compare_engines:
        from datetime import UTC, datetime

        from work_harness.evaluation_comparison import ComparisonSpec, run_comparison

        cases = args.cases or root / "evaluation" / "comparison.json"
        spec = ComparisonSpec.model_validate_json(cases.read_text(encoding="utf-8"))
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        output = args.output or root / "evaluation" / "reports" / f"comparison-{timestamp}.json"
        async with build_runtime(
            root, checkpoint_backend="memory", provider_store_backend="sqlite"
        ) as runtime:
            report = await run_comparison(runtime, spec, output)
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "results": report["results"],
                    "gates": report["gates"],
                    "output": str(output),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if report["comparison_gates_passed"] else 1
    if args.prepare_comparison:
        if args.cases is not None:
            raise ValueError(
                "--cases is for execution; comparison preparation only creates a template"
            )
        report = prepare_comparison()
        output = args.output or root / "evaluation" / "reports" / "comparison.pending.json"
        # A dedicated default path keeps preparation separate from actual evaluation results.
        save_report(report, output, overwrite=False)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
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


async def _migrate(args: argparse.Namespace) -> int:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from work_harness.storage import PostgresRunStore
    from work_harness.storage.blackboard_postgres import PostgresBlackboardStore
    from work_harness.storage.factory import build_run_store

    config = load_app_config(args.project_root)
    if (
        config.runtime.run_store.backend != "postgres"
        or config.runtime.checkpoint.backend != "postgres"
    ):
        raise ValueError("migrate requires PostgreSQL run store and checkpoint backends")
    run_store = build_run_store(config.runtime.run_store, config.project_root)
    if not isinstance(run_store, PostgresRunStore):
        raise TypeError("configured run store is not PostgreSQL")
    try:
        await run_store.setup()
    finally:
        await run_store.close()
    blackboard = PostgresBlackboardStore(os.environ[config.runtime.run_store.dsn_env].strip())
    try:
        await blackboard.setup()
    finally:
        await blackboard.close()
    dsn_env = config.runtime.checkpoint.dsn_env
    dsn = os.getenv(dsn_env, "").strip()
    if not dsn:
        raise ValueError(f"PostgreSQL checkpoint requires {dsn_env} to be configured")
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
    print(
        json.dumps(
            {
                "status": "ok",
                "run_queue_schema": PostgresRunStore.schema_version,
                "blackboard_schema": PostgresBlackboardStore.schema_version,
                "checkpoint": "postgres",
            },
            ensure_ascii=False,
        )
    )
    return 0


async def _run_worker(args: argparse.Namespace) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered_signals: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
            registered_signals.append(signum)
    try:
        async with build_runtime(args.project_root) as runtime:
            if runtime.config.runtime.execution.role != "worker":
                raise ValueError("worker command requires WORK_HARNESS_EXECUTION_ROLE=worker")
            manager = RunManager(runtime)
            await manager.initialize()
            consumer = asyncio.create_task(
                manager.wait_worker(),
                name="work-harness:worker-supervisor",
            )
            stopper = asyncio.create_task(stop.wait(), name="work-harness:worker-stop")
            try:
                done, _ = await asyncio.wait(
                    {consumer, stopper},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if consumer in done:
                    await consumer
            finally:
                stopper.cancel()
                consumer.cancel()
                await asyncio.gather(stopper, consumer, return_exceptions=True)
                await manager.close()
    finally:
        for signum in registered_signals:
            loop.remove_signal_handler(signum)
    return 0


async def _worker_health(args: argparse.Namespace) -> int:
    from work_harness.storage.factory import build_run_store

    config = load_app_config(args.project_root)
    if config.runtime.execution.role != "worker" or config.runtime.run_store.backend != "postgres":
        raise ValueError("worker-health requires the PostgreSQL worker execution role")
    store = build_run_store(config.runtime.run_store, config.project_root)
    instance_id = (
        os.getenv("WORK_HARNESS_INSTANCE_ID", "").strip() or os.getenv("HOSTNAME", "").strip()
    )
    if not instance_id:
        raise ValueError("worker health requires WORK_HARNESS_INSTANCE_ID or HOSTNAME")
    try:
        await store.initialize()
        workers = await store.list_live_workers(
            stale_seconds=config.runtime.execution.worker_stale_seconds
        )
    finally:
        await store.close()
    worker = next((item for item in workers if item.instance_id == instance_id), None)
    if worker is None:
        raise RuntimeError("worker heartbeat is missing or stale")
    print(worker.model_dump_json())
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
                    "execution_role": config.runtime.execution.role,
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
    if args.command == "migrate":
        return asyncio.run(_migrate(args))
    if args.command == "worker":
        return asyncio.run(_run_worker(args))
    if args.command == "worker-health":
        return asyncio.run(_worker_health(args))
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
