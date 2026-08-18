from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from work_harness.domain.models import utc_now
from work_harness.storage.provider_base import (
    ProviderProfileRecord,
    ProviderProfileSpec,
    ProviderProfileStoreNotFoundError,
)


class SQLiteProviderProfileStore:
    backend = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = path
        self._write_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_profiles (
                    name TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    model_bindings_json TEXT NOT NULL,
                    agent_bindings_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(provider_profiles)")
            }
            if "agent_bindings_json" not in columns:
                connection.execute(
                    "ALTER TABLE provider_profiles "
                    "ADD COLUMN agent_bindings_json TEXT NOT NULL DEFAULT '{}'"
                )
            connection.commit()

    async def close(self) -> None:
        return None

    async def upsert(
        self, name: str, profile: ProviderProfileSpec
    ) -> ProviderProfileRecord:
        async with self._write_lock:
            return await asyncio.to_thread(self._upsert_sync, name, profile)

    def _upsert_sync(
        self, name: str, profile: ProviderProfileSpec
    ) -> ProviderProfileRecord:
        # Validate the path identifier before it reaches SQL or durable state.
        pending = ProviderProfileRecord(name=name, **profile.model_dump(mode="python"))
        now = utc_now()
        with closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT created_at FROM provider_profiles WHERE name = ?", (name,)
            ).fetchone()
            created_at = existing["created_at"] if existing else now.isoformat()
            connection.execute(
                """
                INSERT INTO provider_profiles (
                    name, config_json, model_bindings_json, agent_bindings_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    config_json = excluded.config_json,
                    model_bindings_json = excluded.model_bindings_json,
                    agent_bindings_json = excluded.agent_bindings_json,
                    updated_at = excluded.updated_at
                """,
                (
                    pending.name,
                    profile.config.model_dump_json(),
                    json.dumps(
                        profile.model_bindings,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        profile.agent_bindings,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    created_at,
                    now.isoformat(),
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM provider_profiles WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                raise RuntimeError("provider profile was not persisted")
            return self._record(row)

    async def get(self, name: str) -> ProviderProfileRecord | None:
        return await asyncio.to_thread(self._get_sync, name)

    def _get_sync(self, name: str) -> ProviderProfileRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM provider_profiles WHERE name = ?", (name,)
            ).fetchone()
            return self._record(row) if row else None

    async def list(self) -> list[ProviderProfileRecord]:
        return await asyncio.to_thread(self._list_sync)

    def _list_sync(self) -> list[ProviderProfileRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM provider_profiles ORDER BY name ASC"
            ).fetchall()
            return [self._record(row) for row in rows]

    async def delete(self, name: str) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._delete_sync, name)

    def _delete_sync(self, name: str) -> None:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM provider_profiles WHERE name = ?", (name,)
            )
            connection.commit()
            if cursor.rowcount == 0:
                raise ProviderProfileStoreNotFoundError(name)

    @staticmethod
    def _record(row: sqlite3.Row) -> ProviderProfileRecord:
        return ProviderProfileRecord(
            name=row["name"],
            config=json.loads(row["config_json"]),
            model_bindings=json.loads(row["model_bindings_json"]),
            agent_bindings=json.loads(row["agent_bindings_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
