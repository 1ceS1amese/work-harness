"""Keep ledger and receipt adapters on the selected blackboard backend."""

from work_harness.blackboard.execution import ActivationStore, MemoryActivationStore
from work_harness.blackboard.protocol import BlackboardError
from work_harness.blackboard.store import BlackboardStore
from work_harness.blackboard.verification import (
    MemoryVerificationReceiptStore,
    VerificationReceiptStore,
)
from work_harness.storage.blackboard_execution_postgres import (
    PostgresActivationStore,
    PostgresVerificationReceiptStore,
)
from work_harness.storage.blackboard_execution_sqlite import (
    SQLiteActivationStore,
    SQLiteVerificationReceiptStore,
)
from work_harness.storage.blackboard_memory import MemoryBlackboardStore
from work_harness.storage.blackboard_postgres import PostgresBlackboardStore
from work_harness.storage.blackboard_sqlite import SQLiteBlackboardStore


def build_activation_store(store: BlackboardStore) -> ActivationStore:
    if isinstance(store, PostgresBlackboardStore):
        return PostgresActivationStore(store)
    if isinstance(store, SQLiteBlackboardStore):
        return SQLiteActivationStore(store)
    if isinstance(store, MemoryBlackboardStore):
        return MemoryActivationStore(store)
    raise BlackboardError("unsupported blackboard ledger backend")


def build_verification_receipt_store(store: BlackboardStore) -> VerificationReceiptStore:
    if isinstance(store, PostgresBlackboardStore):
        return PostgresVerificationReceiptStore(store)
    if isinstance(store, SQLiteBlackboardStore):
        return SQLiteVerificationReceiptStore(store)
    if isinstance(store, MemoryBlackboardStore):
        return MemoryVerificationReceiptStore()
    raise BlackboardError("unsupported blackboard verification backend")
