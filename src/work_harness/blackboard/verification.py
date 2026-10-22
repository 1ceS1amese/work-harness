"""Host-owned verification receipts, bound to the exact claim and evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from pydantic import JsonValue

from work_harness.blackboard.models import ClaimType, FactContent, Verification, digest
from work_harness.blackboard.protocol import VerificationError

EvidenceCheck = Callable[[FactContent, JsonValue], tuple[str, ...]]


def claim_digest(content: FactContent) -> str:
    # Topic, causal inspiration and goal routing do not prove the claim itself.
    return digest(
        content.model_dump(mode="json", exclude={"topic", "tags", "derived_from", "links"})
    )


@dataclass(frozen=True)
class Verifier:
    id: str
    version: str
    method: str
    claim_types: tuple[ClaimType, ...]
    check: EvidenceCheck


class VerificationReceiptStore(Protocol):
    async def put(self, board_id: str, receipt: Verification) -> None: ...

    async def get(self, board_id: str, receipt_id: str) -> Verification | None: ...


class MemoryVerificationReceiptStore:
    def __init__(self) -> None:
        self._receipts: dict[tuple[str, str], Verification] = {}
        self._lock = asyncio.Lock()

    async def put(self, board_id: str, receipt: Verification) -> None:
        async with self._lock:
            key = (board_id, receipt.receipt_id)
            if key in self._receipts and self._receipts[key] != receipt:
                raise VerificationError("verification receipt is immutable")
            self._receipts[key] = receipt.model_copy(deep=True)

    async def get(self, board_id: str, receipt_id: str) -> Verification | None:
        async with self._lock:
            receipt = self._receipts.get((board_id, receipt_id))
            return receipt.model_copy(deep=True) if receipt else None


class VerificationRegistry:
    """Only trusted host adapters receive this capability; agents receive receipt IDs."""

    def __init__(self, receipts: VerificationReceiptStore | None = None) -> None:
        self._verifiers: dict[str, Verifier] = {}
        self.receipts = receipts if receipts is not None else MemoryVerificationReceiptStore()

    def register(self, verifier: Verifier) -> None:
        if (
            not verifier.id
            or not verifier.version
            or not verifier.method
            or not verifier.claim_types
        ):
            raise ValueError("verifier identity, version, method and claim types are required")
        if verifier.id in self._verifiers:
            raise ValueError(f"verifier already registered: {verifier.id}")
        self._verifiers[verifier.id] = verifier

    async def attest(
        self,
        board_id: str,
        content: FactContent,
        *,
        verifier_id: str,
        evidence: JsonValue,
    ) -> Verification:
        """Check evidence outside the commit transaction and issue an opaque receipt."""
        content = FactContent.model_validate(content.model_dump())
        verifier = self._verifiers.get(verifier_id)
        if verifier is None or content.claim_type not in verifier.claim_types:
            raise VerificationError("verifier is not authorized for this claim type")
        if any(ref.object_id.startswith("local:") for ref in content.premise_refs):
            raise VerificationError("verification premises must reference committed facts")
        try:
            checks = verifier.check(content.model_copy(deep=True), evidence)
        except (ValueError, TypeError, KeyError) as exc:
            raise VerificationError(f"evidence rejected by {verifier.id}: {exc}") from exc
        receipt = Verification(
            receipt_id=f"verification_{uuid4().hex}",
            verifier_id=verifier.id,
            verifier_version=verifier.version,
            method=verifier.method,
            verified_at=datetime.now(UTC),
            content_digest=claim_digest(content),
            checks=checks,
            premise_refs=content.premise_refs,
        )
        await self.receipts.put(board_id, receipt)
        return receipt.model_copy(deep=True)

    async def verify(self, board_id: str, content: FactContent, receipt_id: str) -> Verification:
        receipt = await self.receipts.get(board_id, receipt_id)
        if receipt is None:
            raise VerificationError("unknown verification receipt for this board")
        if receipt.content_digest != claim_digest(content):
            raise VerificationError("verification receipt does not cover this claim or evidence")
        return receipt.model_copy(deep=True)
