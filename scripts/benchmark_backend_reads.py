"""Isolated configuration/API/SQLite read benchmark; never uses real model credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import sqlite3
import statistics
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from work_harness.api.app import create_app
from work_harness.config.loader import load_app_config
from work_harness.storage.sqlite import SQLiteRunStore


def distribution(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "mean_ms": round(statistics.fmean(values), 3),
        "p50_ms": round(ordered[math.ceil(len(values) * 0.5) - 1], 3),
        "p95_ms": round(ordered[math.ceil(len(values) * 0.95) - 1], 3),
    }


async def measure(args, root: Path) -> dict:
    config_times = []
    for _ in range(args.samples):
        started = time.perf_counter()
        load_app_config(root)
        config_times.append((time.perf_counter() - started) * 1000)
    store = SQLiteRunStore(root / "history.sqlite")
    await store.initialize()
    with closing(sqlite3.connect(store.path)) as connection:
        stamp = datetime(2026, 1, 1, tzinfo=UTC)
        connection.executemany(
            "INSERT INTO run_index (run_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (
                (
                    f"run_{i:08d}",
                    "succeeded",
                    (stamp + timedelta(seconds=i)).isoformat(),
                    stamp.isoformat(),
                )
                for i in range(args.records)
            ),
        )
        connection.commit()
        plan = [
            row[3]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM run_index "
                "ORDER BY created_at DESC, run_id DESC LIMIT 50 OFFSET 0",
            )
        ]
    history_times = []
    for _ in range(args.samples):
        started = time.perf_counter()
        rows = await store.list(limit=50)
        assert len(rows) == min(50, args.records)
        history_times.append((time.perf_counter() - started) * 1000)
    await store.close()

    app = create_app(root)
    api_times, lag = [], []
    stop = asyncio.Event()

    async def monitor():
        while not stop.is_set():
            target = asyncio.get_running_loop().time() + 0.005
            await asyncio.sleep(0.005)
            lag.append(max(0, asyncio.get_running_loop().time() - target) * 1000)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        slots = asyncio.Semaphore(args.concurrency)

        async def read(index):
            async with slots:
                started = time.perf_counter()
                response = await client.get("/v1/providers" if index % 2 else "/v1/settings")
                response.raise_for_status()
                api_times.append((time.perf_counter() - started) * 1000)

        monitoring = asyncio.create_task(monitor())
        try:
            await asyncio.gather(*(read(index) for index in range(args.samples)))
        finally:
            stop.set()
            await monitoring
    return {
        "samples": args.samples,
        "history_records": args.records,
        "api_concurrency": args.concurrency,
        "config_load": distribution(config_times),
        "history_first_page": distribution(history_times),
        "history_query_plan": plan,
        "settings_api": distribution(api_times),
        "event_loop_lag": distribution(lag),
        "real_model_calls": 0,
        "quality_claim": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--records", type=int, default=50_000)
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.records, args.samples, args.concurrency) < 1:
        parser.error("records, samples and concurrency must be positive")
    # Run in a fresh process and workspace, ignoring deployment and secret environment overrides.
    for name in list(os.environ):
        if name.startswith("WORK_HARNESS_"):
            del os.environ[name]
    os.environ.update(WORK_HARNESS_PROFILE="local", WORK_HARNESS_LOG_LEVEL="WARNING")
    with tempfile.TemporaryDirectory(prefix="work-harness-reads-") as directory:
        root = Path(directory)
        for name in ("config", "prompts", "skills"):
            shutil.copytree(args.project_root / name, root / name)
        report = asyncio.run(measure(args, root))
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
