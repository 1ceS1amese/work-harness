"""Host commands for human information, attestation, goal changes and scoped consent."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, JsonValue

from work_harness.blackboard.kernel import BlackboardKernel
from work_harness.blackboard.models import (
    AcceptanceCriterion,
    BoardModel,
    Evidence,
    FactContent,
    FactProposal,
    HintProposal,
    Intent,
    Link,
    NonEmpty,
    ObjectRef,
    digest,
)
from work_harness.blackboard.protocol import (
    BlackboardDelta,
    BlackboardError,
    CommitContext,
    CommitReceipt,
    GoalRevision,
    IdempotencyConflictError,
    ObjectChange,
    StaleSnapshotError,
    WriterToken,
)
from work_harness.blackboard.verification import Verifier

INPUT_POLICY_REVISION = "human-input-v1"


class AddInformation(BoardModel):
    kind: Literal["add_information"] = "add_information"
    text: str = Field(min_length=1, max_length=20000)


class ConfirmStatement(BoardModel):
    kind: Literal["confirm_statement"] = "confirm_statement"
    claim_key: NonEmpty
    statement: str = Field(min_length=1, max_length=19000)
    value: JsonValue
    scope: dict[str, str] = Field(default_factory=dict)


class ReviseGoal(BoardModel):
    kind: Literal["revise_goal"] = "revise_goal"
    objective: str = Field(min_length=1, max_length=20000)
    acceptance: tuple[AcceptanceCriterion, ...] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=4000)


class AuthorizeOperation(BoardModel):
    kind: Literal["authorize_operation"] = "authorize_operation"
    tool_name: NonEmpty
    arguments: dict[str, JsonValue]
    approved: bool = Field(strict=True)
    expires_at: AwareDatetime


HumanCommand = Annotated[
    AddInformation | ConfirmStatement | ReviseGoal | AuthorizeOperation,
    Field(discriminator="kind"),
]


class HumanInput(BoardModel):
    idempotency_key: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=1, strict=True)
    target: ObjectRef
    command: HumanCommand

    @property
    def storage_key(self) -> str:
        return "human:" + digest(self.idempotency_key)

    @property
    def request_digest(self) -> str:
        return digest(self)


class HumanInputResult(BoardModel):
    receipt: CommitReceipt
    wakeup: Literal["active", "scheduled", "pending", "settled", "closed"]


def register_human_verifier(kernel: BlackboardKernel) -> None:
    def check(content: FactContent, proof: JsonValue) -> tuple[str, ...]:
        # The adapter supplies the exact authenticated command and the host-built claim.
        if content.claim_type != "user_statement" or proof != content.model_dump(mode="json"):
            raise ValueError("human attestation must cover this exact user statement")
        if content.scope.get("confirmation") == "operation_authorization":
            return ("operation_authorized" if content.value["approved"] else "operation_denied",)
        if content.scope.get("confirmation") != "user_statement":
            raise ValueError("unsupported human confirmation scope")
        return ("user_confirmed",)

    kernel.verification.register(
        Verifier(
            "authenticated-user",
            "1",
            "explicit-user-command",
            ("user_statement",),
            check,
        )
    )


async def prior_input(
    kernel: BlackboardKernel, board_id: str, request: HumanInput
) -> CommitReceipt | None:
    prior = await kernel.store.receipt(board_id, request.storage_key)
    if prior and prior.request_digest != request.request_digest:
        raise IdempotencyConflictError("human input key was reused with a different command")
    return prior


async def submit_input(
    kernel: BlackboardKernel,
    board_id: str,
    request: HumanInput,
    *,
    writer: WriterToken,
    max_objects: int,
    max_child_intents: int,
) -> CommitReceipt:
    request = HumanInput.model_validate(request.model_dump())
    prior = await prior_input(kernel, board_id, request)
    if prior is not None:
        return prior
    await kernel.store.check_writer(writer)
    snapshot = await kernel.store.snapshot(board_id)
    if snapshot.revision != request.expected_revision:
        raise StaleSnapshotError("human input requires the expected board revision")
    target = snapshot.by_id().get(request.target.object_id)
    if not isinstance(target, Intent) or target.ref != request.target:
        raise StaleSnapshotError("human input requires a current Intent reference")
    if target.status == "abandoned":
        raise BlackboardError("cannot supplement an abandoned Intent")
    command = request.command
    if isinstance(command, ReviseGoal) and target.id != snapshot.root_intent_id:
        raise BlackboardError("revise_goal addresses only the root Intent")
    if isinstance(command, AuthorizeOperation) and command.expires_at <= kernel.clock():
        raise BlackboardError("operation authorization must expire in the future")
    # The immutable input text is public evidence only after its proposal is accepted.
    attachment = await kernel.put_content(
        board_id,
        request.model_dump_json(),
        media_type="application/json",
        writer=writer,
    )
    common = {
        "topic": "user-input",
        "links": (Link(relation="contributes_to", target=target.ref),),
        "attachments": (attachment,),
    }
    proposals = []
    goal_revision = None
    if isinstance(command, (AddInformation, ReviseGoal)):
        proposals.append(
            HintProposal(
                local_id="input",
                **common,
                suggestion=command.text if isinstance(command, AddInformation) else command.reason,
                applies_when="继续处理指定目标时。",
                rationale="用户补充或目标修订；其中的外部主张尚未独立核验。",
            )
        )
        if isinstance(command, ReviseGoal):
            goal_revision = GoalRevision(target=target.ref, **command.model_dump(exclude={"kind"}))
    else:
        content = FactContent(
            **common,
            claim_type="user_statement",
            claim_key=command.claim_key
            if isinstance(command, ConfirmStatement)
            else "operation.authorization",
            statement=("用户确认的陈述（未独立核验）：" + command.statement)
            if isinstance(command, ConfirmStatement)
            else (
                f"用户{'授权' if command.approved else '拒绝'}了绑定参数的 "
                f"{command.tool_name} 操作；不证明操作已执行。"
            ),
            value=command.value
            if isinstance(command, ConfirmStatement)
            else {
                "approved": command.approved,
                "tool_name": command.tool_name,
                "arguments": command.arguments,
                "target": target.ref.model_dump(mode="json"),
            },
            scope={**command.scope, "confirmation": "user_statement"}
            if isinstance(command, ConfirmStatement)
            else {
                "confirmation": "operation_authorization",
                "tool_name": command.tool_name,
                "arguments_digest": digest(command.arguments),
                "intent_id": target.id,
                "intent_revision": str(target.revision),
                "intent_content_digest": target.content_digest,
            },
            valid_until=command.expires_at if isinstance(command, AuthorizeOperation) else None,
            evidence=(
                Evidence(
                    source="user_input",
                    locator=f"input:{request.storage_key}",
                    content_digest=attachment.content_digest,
                    description="已认证 API 接受的明确用户输入；仅确认陈述或指定操作授权。",
                ),
            ),
        )
        proof = await kernel.verification.attest(
            board_id,
            content,
            verifier_id="authenticated-user",
            evidence=content.model_dump(mode="json"),
        )
        proposals.append(
            FactProposal(
                local_id="input",
                **content.model_dump(),
                verification_receipt_id=proof.receipt_id,
            )
        )
    changes = ()
    if target.status == "blocked" and goal_revision is None:
        changes = (
            ObjectChange(target=target.ref, action="reopen_intent", reason="用户补充后重新评估"),
        )
    return await kernel.commit(
        board_id,
        BlackboardDelta(
            expected_revision=request.expected_revision,
            idempotency_key=request.storage_key,
            proposals=tuple(proposals),
            changes=changes,
            goal_revision=goal_revision,
        ),
        context=CommitContext(
            source_id="user",
            is_user=True,
            max_objects=max_objects,
            max_child_intents=max_child_intents,
        ),
        writer=writer,
        user_request_digest=request.request_digest,
    )
