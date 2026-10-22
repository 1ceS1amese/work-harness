"""Ordered, transactional migrations for the PostgreSQL blackboard component."""

from psycopg import AsyncConnection

from work_harness.storage.postgres import QUEUE_ADVISORY_LOCK_ID, PostgresSchemaError

# Share the bootstrap lock with the queue: both create the schema-version table.
MIGRATIONS = (
    (
        """CREATE TABLE work_harness_boards (
            board_id TEXT PRIMARY KEY,
            revision BIGINT NOT NULL CHECK (revision >= 1),
            root_intent_id TEXT NOT NULL
        )""",
        """CREATE TABLE work_harness_board_object_revisions (
            board_id TEXT NOT NULL REFERENCES work_harness_boards(board_id),
            object_id TEXT NOT NULL,
            revision BIGINT NOT NULL CHECK (revision >= 1),
            board_revision BIGINT NOT NULL CHECK (board_revision >= 1),
            payload JSONB NOT NULL,
            PRIMARY KEY (board_id, object_id, revision)
        )""",
        """CREATE TABLE work_harness_board_objects_current (
            board_id TEXT NOT NULL,
            object_id TEXT NOT NULL,
            revision BIGINT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('fact', 'intent', 'hint')),
            topic TEXT NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY (board_id, object_id),
            FOREIGN KEY (board_id, object_id, revision)
                REFERENCES work_harness_board_object_revisions(board_id, object_id, revision)
        )""",
        """CREATE TABLE work_harness_board_events (
            board_id TEXT NOT NULL REFERENCES work_harness_boards(board_id),
            sequence BIGINT NOT NULL CHECK (sequence >= 1),
            payload JSONB NOT NULL,
            PRIMARY KEY (board_id, sequence)
        )""",
        """CREATE TABLE work_harness_board_commits (
            board_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            board_revision BIGINT NOT NULL,
            payload JSONB NOT NULL,
            PRIMARY KEY (board_id, idempotency_key),
            UNIQUE (board_id, board_revision),
            FOREIGN KEY (board_id, board_revision)
                REFERENCES work_harness_board_events(board_id, sequence)
        )""",
        """CREATE TABLE work_harness_board_contents (
            board_id TEXT NOT NULL REFERENCES work_harness_boards(board_id),
            content_digest TEXT NOT NULL,
            payload JSONB NOT NULL,
            PRIMARY KEY (board_id, content_digest)
        )""",
        """CREATE TABLE work_harness_board_control (
            board_id TEXT PRIMARY KEY REFERENCES work_harness_boards(board_id),
            generation BIGINT NOT NULL DEFAULT 0,
            owner_id TEXT,
            session_id TEXT,
            cancelled BOOLEAN NOT NULL DEFAULT FALSE,
            finalization JSONB
        )""",
        """CREATE INDEX ix_work_harness_board_objects_query
            ON work_harness_board_objects_current(board_id, kind, topic, status)""",
        """CREATE INDEX ix_work_harness_board_history_snapshot
            ON work_harness_board_object_revisions(board_id, board_revision)""",
    ),
    (
        """ALTER TABLE work_harness_board_control
            ADD COLUMN payload JSONB,
            ADD COLUMN wakeup_revision BIGINT NOT NULL DEFAULT 0,
            ADD COLUMN status_pending BOOLEAN NOT NULL DEFAULT FALSE""",
        """CREATE TABLE work_harness_board_activations (
            activation_id TEXT PRIMARY KEY,
            board_id TEXT NOT NULL REFERENCES work_harness_boards(board_id),
            UNIQUE (board_id, activation_id)
        )""",
        """CREATE TABLE work_harness_board_activation_attempts (
            activation_id TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK (attempt >= 1),
            board_id TEXT NOT NULL,
            ordinal BIGINT NOT NULL CHECK (ordinal >= 1),
            payload JSONB NOT NULL,
            PRIMARY KEY (activation_id, attempt),
            UNIQUE (board_id, ordinal),
            FOREIGN KEY (board_id, activation_id)
                REFERENCES work_harness_board_activations(board_id, activation_id)
        )""",
        """CREATE TABLE work_harness_board_usage (
            activation_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            call_id TEXT NOT NULL,
            payload JSONB NOT NULL,
            PRIMARY KEY (activation_id, attempt, call_id),
            FOREIGN KEY (activation_id, attempt)
                REFERENCES work_harness_board_activation_attempts(activation_id, attempt)
        )""",
        """CREATE TABLE work_harness_board_verification_receipts (
            board_id TEXT NOT NULL REFERENCES work_harness_boards(board_id),
            receipt_id TEXT NOT NULL,
            payload JSONB NOT NULL,
            PRIMARY KEY (board_id, receipt_id)
        )""",
        """CREATE INDEX ix_work_harness_board_pending
            ON work_harness_board_control(board_id)
            WHERE wakeup_revision > 0 OR status_pending""",
    ),
    (
        """ALTER TABLE work_harness_boards
            ADD COLUMN queue_managed BOOLEAN NOT NULL DEFAULT FALSE""",
        """ALTER TABLE work_harness_board_control
            ADD COLUMN queue_job_id UUID,
            ADD COLUMN queue_lease_token UUID""",
    ),
)
SCHEMA_VERSION = len(MIGRATIONS)


async def schema_version(connection: AsyncConnection) -> int:
    row = await (
        await connection.execute("SELECT to_regclass('work_harness_schema_version') AS name")
    ).fetchone()
    if row["name"] is None:
        return 0
    row = await (
        await connection.execute(
            "SELECT version FROM work_harness_schema_version WHERE component = 'blackboard'"
        )
    ).fetchone()
    return row["version"] if row else 0


async def migrate(connection: AsyncConnection) -> None:
    """Caller owns the transaction; failed DDL and its version stamp roll back together."""
    await connection.execute("SELECT pg_advisory_xact_lock(%s)", (QUEUE_ADVISORY_LOCK_ID,))
    await connection.execute(
        """CREATE TABLE IF NOT EXISTS work_harness_schema_version (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        )"""
    )
    current = await schema_version(connection)
    if not 0 <= current <= SCHEMA_VERSION:
        raise PostgresSchemaError(f"unsupported blackboard schema version: {current}")
    for index in range(current, SCHEMA_VERSION):
        for statement in MIGRATIONS[index]:
            await connection.execute(statement)
        await connection.execute(
            """INSERT INTO work_harness_schema_version(component, version)
                VALUES ('blackboard', %s) ON CONFLICT(component) DO UPDATE
                SET version = excluded.version, updated_at = clock_timestamp()""",
            (index + 1,),
        )
