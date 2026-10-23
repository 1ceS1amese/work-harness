"""Product sources for document delivery, with the original draft contract preserved."""

from __future__ import annotations

import hashlib
from functools import partial
from typing import Any

from work_harness.blackboard.models import ContentRef, Evidence, FactContent, Hint, Link
from work_harness.blackboard.verification import VerificationRegistry, Verifier
from work_harness.domain.models import BlackboardTaskInput, TaskRequest, TaskSpec
from work_harness.knowledge_sources.adapters.agent import AgentKnowledgeSource
from work_harness.knowledge_sources.adapters.callback import (
    VerificationKnowledgeSource,
    VerifiedObservation,
)
from work_harness.knowledge_sources.base import (
    ActivationRule,
    BoardView,
    FactQuery,
    KnowledgeSourceSpec,
    SourceLimits,
)
from work_harness.knowledge_sources.deliverables import (
    DELIVERABLE_REVISION,
    DELIVERABLE_TEMPLATES,
    DeliverableTemplate,
    selected_deliverable,
)
from work_harness.knowledge_sources.execution import Invocation
from work_harness.knowledge_sources.registry import KnowledgeSourceRegistry
from work_harness.workflow.agent_services import AgentServices

DELIVERY_KEY = "draft.content"
DELIVERY_SCOPE = {"verification": "stored_content_only"}
DELIVERY_CHECK = "content_stored"
CATALOG_REVISION = "draft-delivery-v1"
DELIVERY_PREFIX = "已保存候选草稿；以下内容尚未核验其事实或质量：\n"


def draft_task_input() -> BlackboardTaskInput:
    """An explicitly selected delivery template; never infer it from a free-text goal."""
    return BlackboardTaskInput.model_validate(
        {
            "topic": "document",
            "acceptance": [
                {
                    "id": "saved-draft",
                    "description": "候选草稿已保存且可读取，不证明内容准确性或任务实际完成",
                    "claim_key": DELIVERY_KEY,
                    "claim_type": "delivery",
                    "scope": DELIVERY_SCOPE,
                    "required_checks": [DELIVERY_CHECK],
                }
            ],
        }
    )


def acceptance_task_input(template_id: str) -> BlackboardTaskInput:
    if template_id == "saved-draft":
        return draft_task_input()
    for template in DELIVERABLE_TEMPLATES:
        if template.id == template_id:
            return template.task_input()
    raise ValueError(f"unknown delivery template: {template_id}")


def catalog_revision(request: TaskRequest) -> str:
    return DELIVERABLE_REVISION if selected_deliverable(request.blackboard) else CATALOG_REVISION


def verify_delivery(
    content: FactContent, proof: Any, *, template: DeliverableTemplate | None = None
) -> tuple[str, ...]:
    """Attest bytes that the host read, never the truth of the candidate's prose."""
    ref = ContentRef.model_validate(proof["ref"])
    body = proof["body"]
    encoded = body.encode("utf-8")
    statement = (template.prefix if template else DELIVERY_PREFIX) + body[:19_000]
    if template:
        # FactContent normalizes whitespace; the attachment digest still binds raw bytes.
        statement = statement.strip()
    if (
        not body.strip()
        or hashlib.sha256(encoded).hexdigest() != ref.content_digest
        or len(encoded) != ref.size_bytes
        or content.claim_type != "delivery"
        or content.claim_key != (template.claim_key if template else DELIVERY_KEY)
        or content.scope != (template.scope if template else DELIVERY_SCOPE)
        or content.value != ref.model_dump(mode="json")
        or content.attachments != (ref,)
        or content.statement != statement
        or len(content.evidence) != 1
        or content.evidence[0].source != "delivery"
        or content.evidence[0].content_digest != ref.content_digest
        or content.premise_refs
    ):
        raise ValueError("delivery evidence does not match the stored candidate")
    if template:
        if ref.media_type != "text/markdown":
            raise ValueError("document deliverables must use Markdown")
        template.validate_body(body)
        return template.checks
    return (DELIVERY_CHECK,)


def build_product_sources(
    services: AgentServices,
    request: TaskRequest,
    verification: VerificationRegistry,
) -> KnowledgeSourceRegistry:
    if request.blackboard is None:
        raise ValueError("blackboard runs require explicit acceptance criteria")
    config = services.config
    topic = request.blackboard.topic
    roles = request.assignment.assignees or config.runtime.workflow.blackboard.agent_roles
    if any(len(items) > 1 for items in request.assignment.functions_by_agent.values()):
        raise ValueError("blackboard sources currently accept one assigned function per agent")
    task = TaskSpec.model_validate(
        request.model_dump(exclude={"engine", "blackboard", "idempotency_key"})
    )
    template = selected_deliverable(request.blackboard)
    revision = catalog_revision(request)
    if template:
        task.constraints = [*task.constraints, template.instruction]
    claim_key = template.claim_key if template else DELIVERY_KEY
    scope = template.scope if template else DELIVERY_SCOPE
    prefix = template.prefix if template else DELIVERY_PREFIX
    verifier_id = f"stored-{template.id}" if template else "stored-draft"
    registry = KnowledgeSourceRegistry()
    for role_name in roles:
        role = config.agents.agents.get(role_name)
        if role is None or not role.assignable:
            raise ValueError(f"blackboard source requires an assignable agent: {role_name}")
        tools = set(role.allowed_tools)
        for selection in request.skills:
            tools.update(services.skills.get(selection.skill_id).manifest.allowed_tools)
        source = KnowledgeSourceSpec(
            id=f"agent-{role_name}",
            version=role.version,
            emitted_kinds=("hint",),
            activation_rule=ActivationRule(
                intent_topics=(topic,),
                absent_hint_topics=(topic,),
                reason=(
                    f"当前目标尚无候选内容，可结合板面生成{template.title}。"
                    if template
                    else "当前目标尚无候选内容，可结合板面生成草稿。"
                ),
            ),
            allowed_tools=tuple(sorted(tools)),
            model_route=role.logical_model,
            limits=SourceLimits(
                max_output_tokens=role.max_output_tokens,
                timeout_seconds=min(
                    role.timeout_seconds, config.runtime.node_timeout_seconds, 3600
                ),
            ),
            adapter_type="agent",
            strategy_version=revision,
        )
        registry.register(AgentKnowledgeSource(source, services, worker_role=role_name, task=task))

    verification.register(
        Verifier(
            verifier_id,
            "1",
            "stored-markdown-structure" if template else "stored-content-digest",
            ("delivery",),
            partial(verify_delivery, template=template),
        )
    )

    async def observe(invocation: Invocation, view: BoardView) -> tuple[VerifiedObservation, ...]:
        for item in view.objects:
            if not isinstance(item, Hint) or item.topic != topic or item.status != "active":
                continue
            for ref in item.attachments:
                stored = await invocation.execution.board_store.content(view.board_id, ref)
                if not stored.body.strip():
                    continue
                if template:
                    try:
                        template.validate_body(stored.body)
                    except ValueError:
                        # Leave the goal unsatisfied; another contribution can supply the document.
                        continue
                content = FactContent(
                    topic=topic,
                    statement=prefix + stored.body[:19_000],
                    claim_type="delivery",
                    claim_key=claim_key,
                    scope=scope,
                    value=ref.model_dump(mode="json"),
                    derived_from=(item.ref,),
                    links=(Link(relation="contributes_to", target=view.intent.ref),),
                    attachments=(ref,),
                    evidence=(
                        Evidence(
                            source="delivery",
                            locator=f"content:{ref.content_digest}",
                            content_digest=ref.content_digest,
                            description=(
                                "宿主核对附件摘要、长度、必要章节正文和要求的待办项；"
                                "不证明事实准确性或文档中的操作已执行。"
                                if template
                                else "宿主读取已保存内容并核对摘要、长度和非空性；不证明内容质量。"
                            ),
                        ),
                    ),
                )
                return (
                    VerifiedObservation(
                        local_id=template.id if template else "draft",
                        content=content,
                        verifier_id=verifier_id,
                        proof={"ref": ref.model_dump(mode="json"), "body": stored.body},
                    ),
                )
        return ()

    registry.register(
        VerificationKnowledgeSource(
            KnowledgeSourceSpec(
                id=f"verify-{template.id}" if template else "verify-draft-delivery",
                version="1",
                emitted_kinds=("fact",),
                activation_rule=ActivationRule(
                    intent_topics=(topic,),
                    hint_topics=(topic,),
                    absent_facts=(
                        FactQuery(claim_key=claim_key, claim_type="delivery", scope=scope),
                    ),
                    reason=(
                        f"核对{template.title}的保存状态与章节结构。"
                        if template
                        else "核对候选草稿是否已保存且可读取。"
                    ),
                ),
                adapter_type="verification",
                strategy_version=revision,
            ),
            observe,
        )
    )
    return registry
