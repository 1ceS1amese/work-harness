from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from work_harness.domain.enums import RiskLevel
from work_harness.domain.models import TaskRequest
from work_harness.runtime import build_runtime
from work_harness.service import RunManager


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _max_rss_mb() -> float | None:
    try:
        import resource
    except ImportError:
        return None
    rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        rss /= 1024
    return round(rss / 1024, 2)


async def _run_once(
    manager: RunManager,
    index: int,
    risk_level: RiskLevel,
) -> dict[str, Any]:
    started = time.perf_counter()
    run_id = await manager.start(
        TaskRequest(
            goal=f"Benchmark deterministic backend run {index}",
            engine="legacy",
            risk_level=risk_level,
        )
    )
    result = await manager.wait(run_id)
    return {
        "status": getattr(result.status, "value", result.status),
        "latency_ms": (time.perf_counter() - started) * 1000,
        "model_calls": len(result.model_calls),
        "review_calls": len(result.reviews),
        "model_calls_by_node": Counter(call.node for call in result.model_calls),
    }


async def benchmark(args: argparse.Namespace) -> dict[str, Any]:
    project_root = args.project_root.resolve()
    async with build_runtime(project_root, checkpoint_backend="memory") as runtime:
        manager = RunManager(runtime)
        limiter = asyncio.Semaphore(args.concurrency)

        async def limited_run(index: int) -> dict[str, Any]:
            async with limiter:
                return await _run_once(manager, index, RiskLevel(args.risk_level))

        try:
            for index in range(args.warmup):
                await _run_once(manager, -index - 1, RiskLevel(args.risk_level))
            started = time.perf_counter()
            samples = await asyncio.gather(
                *(limited_run(index) for index in range(args.runs))
            )
            elapsed = time.perf_counter() - started
        finally:
            await manager.close()

        latencies = [float(sample["latency_ms"]) for sample in samples]
        node_counts: Counter[str] = Counter()
        for sample in samples:
            node_counts.update(sample["model_calls_by_node"])
        return {
            "profile": runtime.config.models.profile,
            "provider": "mock",
            "review_strategy": runtime.config.runtime.review_strategy,
            "risk_level": args.risk_level,
            "runs": args.runs,
            "client_concurrency": args.concurrency,
            "run_max_concurrency": runtime.config.runtime.run_max_concurrency,
            "successes": sum(sample["status"] == "succeeded" for sample in samples),
            "elapsed_seconds": round(elapsed, 4),
            "throughput_runs_per_second": round(args.runs / elapsed, 2),
            "latency_ms": {
                "mean": round(statistics.fmean(latencies), 2),
                "p50": round(_percentile(latencies, 0.50), 2),
                "p95": round(_percentile(latencies, 0.95), 2),
                "p99": round(_percentile(latencies, 0.99), 2),
            },
            "average_model_calls": round(
                statistics.fmean(sample["model_calls"] for sample in samples), 2
            ),
            "average_review_calls": round(
                statistics.fmean(sample["review_calls"] for sample in samples), 2
            ),
            "model_calls_by_node": dict(sorted(node_counts.items())),
            "max_rss_mb": _max_rss_mb(),
            "quality_claim": False,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure deterministic Work Harness backend scheduling overhead."
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--risk-level",
        choices=[item.value for item in RiskLevel],
        default=RiskLevel.LOW.value,
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.runs < 1 or args.warmup < 0 or args.concurrency < 1:
        parser.error("runs/concurrency must be positive and warmup must be non-negative")
    return args


def main() -> None:
    os.environ.setdefault("WORK_HARNESS_LOG_LEVEL", "WARNING")
    args = parse_args()
    report = asyncio.run(benchmark(args))
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
