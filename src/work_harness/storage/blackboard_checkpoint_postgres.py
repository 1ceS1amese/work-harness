"""Fence root and child checkpoint writes with their blackboard Job lease."""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from work_harness.blackboard.protocol import StaleWriterError
from work_harness.storage.blackboard_postgres import PostgresBlackboardStore


class LeasedPostgresSaver(BaseCheckpointSaver):
    def __init__(self, database: PostgresBlackboardStore, reader: AsyncPostgresSaver) -> None:
        super().__init__(serde=reader.serde)
        self.database = database
        self.reader = reader

    @property
    def config_specs(self):
        return self.reader.config_specs

    def get_next_version(self, current, channel):
        return self.reader.get_next_version(current, channel)

    def _check_thread(self, config):
        lease = self.database._lease
        if lease is None or config.get("configurable", {}).get("thread_id") != lease.run_id:
            raise StaleWriterError("checkpoint belongs to a different leased run")

    async def aget_tuple(self, config):
        self._check_thread(config)
        return await self.reader.aget_tuple(config)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        self._check_thread(config)
        async for checkpoint in self.reader.alist(
            config, filter=filter, before=before, limit=limit
        ):
            yield checkpoint

    async def aput(self, config, checkpoint, metadata, new_versions):
        self._check_thread(config)

        async def put(connection):
            saver = AsyncPostgresSaver(connection, serde=self.serde)
            return await saver.aput(config, checkpoint, metadata, new_versions)

        # The Saver uses this existing connection; its SQL and the queue lease
        # checks commit or roll back together, including expiry during a write.
        return await self.database.transaction(put, write=True)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        self._check_thread(config)

        async def put(connection):
            saver = AsyncPostgresSaver(connection, serde=self.serde)
            await saver.aput_writes(config, writes, task_id, task_path)

        await self.database.transaction(put, write=True)
