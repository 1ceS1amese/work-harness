from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from work_harness.domain.models import utc_now
from work_harness.storage.async_utils import write_in_thread
from work_harness.storage.provider_base import (
    ProviderProfileRecord,
    ProviderProfileSpec,
    ProviderProfileStoreNotFoundError,
)
from work_harness.storage.secrets import ProviderSecretCipher


class SQLiteProviderProfileStore:
    backend = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.key_path = path.with_name(f"{path.name}.key")
        self._cipher = ProviderSecretCipher(self.key_path)
        self._write_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._cipher.initialize)
        await write_in_thread(self._initialize_sync)

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
                    credential_ciphertext BLOB,
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
            if "credential_ciphertext" not in columns:
                connection.execute(
                    "ALTER TABLE provider_profiles ADD COLUMN credential_ciphertext BLOB"
                )
            connection.commit()

    async def close(self) -> None:
        async with self._write_lock:
            pass

    async def upsert(
        self,
        name: str,
        profile: ProviderProfileSpec,
        *,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> ProviderProfileRecord:
        async with self._write_lock:
            return await write_in_thread(
                self._upsert_sync,
                name,
                profile,
                api_key,
                clear_api_key,
            )

    def _upsert_sync(
        self,
        name: str,
        profile: ProviderProfileSpec,
        api_key: str | None,
        clear_api_key: bool,
    ) -> ProviderProfileRecord:
        if api_key is not None and clear_api_key:
            raise ValueError("cannot set and clear a Provider credential together")
        # Validate the path identifier before it reaches SQL or durable state.
        pending = ProviderProfileRecord(name=name, **profile.model_dump(mode="python"))
        now = utc_now()
        with closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT created_at, credential_ciphertext FROM provider_profiles "
                "WHERE name = ?",
                (name,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now.isoformat()
            credential_ciphertext = (
                existing["credential_ciphertext"] if existing else None
            )
            if clear_api_key:
                credential_ciphertext = None
            elif api_key is not None:
                credential_ciphertext = self._cipher.encrypt(api_key)
            connection.execute(
                """
                INSERT INTO provider_profiles (
                    name, config_json, model_bindings_json, agent_bindings_json,
                    credential_ciphertext, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    config_json = excluded.config_json,
                    model_bindings_json = excluded.model_bindings_json,
                    agent_bindings_json = excluded.agent_bindings_json,
                    credential_ciphertext = excluded.credential_ciphertext,
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
                    credential_ciphertext,
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

    async def get_api_key(self, name: str) -> str | None:
        return await asyncio.to_thread(self._get_api_key_sync, name)

    def _get_api_key_sync(self, name: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT credential_ciphertext FROM provider_profiles WHERE name = ?",
                (name,),
            ).fetchone()
            if row is None or row["credential_ciphertext"] is None:
                return None
            return self._cipher.decrypt(row["credential_ciphertext"])

    async def delete(self, name: str) -> None:
        async with self._write_lock:
            await write_in_thread(self._delete_sync, name)

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
            credential_configured=row["credential_ciphertext"] is not None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
