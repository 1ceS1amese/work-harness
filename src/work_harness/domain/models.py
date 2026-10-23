from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from work_harness.blackboard.models import AcceptanceCriterion, ObjectRef
from work_harness.domain.enums import (
    AgentFunction,
    AllocationMode,
    ArtifactStatus,
    DecisionAction,
    MessageRole,
    ReviewVerdict,
    RiskLevel,
    RunStatus,
    Severity,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


LEGACY_AGENT_ALIASES: dict[str, tuple[str, AgentFunction]] = {
    "worker-general": ("worker-01", AgentFunction.EXECUTION),
    "worker-analysis": ("worker-01", AgentFunction.ANALYSIS),
    "worker-research": ("worker-02", AgentFunction.RESEARCH),
    "worker-delivery": ("worker-03", AgentFunction.EXECUTION),
    "worker-risk": ("worker-04", AgentFunction.RISK),
}


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class AgentAssignment(ContractModel):
    mode: AllocationMode = AllocationMode.AUTO
    assignees: list[str] = Field(default_factory=list, max_length=8)
    functions_by_agent: dict[str, list[AgentFunction]] = Field(default_factory=dict)
    cross_review: bool = True

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_agent_ids(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not isinstance(value.get("assignees"), list):
            return value

        migrated = dict(value)
        assignees: list[Any] = []
        for raw_agent_id in value["assignees"]:
            agent_id = (
                LEGACY_AGENT_ALIASES.get(raw_agent_id, (raw_agent_id, None))[0]
                if isinstance(raw_agent_id, str)
                else raw_agent_id
            )
            if agent_id not in assignees:
                assignees.append(agent_id)

        raw_functions = value.get("functions_by_agent", {})
        migrated_functions: dict[str, list[Any]] = {}
        if isinstance(raw_functions, dict):
            for raw_agent_id, functions in raw_functions.items():
                agent_id = LEGACY_AGENT_ALIASES.get(
                    raw_agent_id, (raw_agent_id, AgentFunction.EXECUTION)
                )[0]
                target = migrated_functions.setdefault(agent_id, [])
                if isinstance(functions, list):
                    target.extend(item for item in functions if item not in target)

        for raw_agent_id in value["assignees"]:
            if not isinstance(raw_agent_id, str) or raw_agent_id not in LEGACY_AGENT_ALIASES:
                continue
            agent_id, legacy_function = LEGACY_AGENT_ALIASES[raw_agent_id]
            migrated_functions.setdefault(agent_id, [legacy_function])

        migrated["assignees"] = assignees
        migrated["functions_by_agent"] = migrated_functions
        return migrated

    @field_validator("assignees")
    @classmethod
    def validate_assignees(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("assignee names cannot be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("assignees must be unique")
        return normalized

    @field_validator("functions_by_agent")
    @classmethod
    def validate_functions_by_agent(
        cls, value: dict[str, list[AgentFunction]]
    ) -> dict[str, list[AgentFunction]]:
        normalized: dict[str, list[AgentFunction]] = {}
        for raw_agent_id, functions in value.items():
            agent_id = raw_agent_id.strip()
            if not agent_id:
                raise ValueError("function assignment agent names cannot be blank")
            if not functions:
                raise ValueError(f"agent {agent_id} requires at least one function")
            if len(functions) != len(set(functions)):
                raise ValueError(f"agent {agent_id} functions must be unique")
            normalized[agent_id] = functions
        return normalized

    @model_validator(mode="after")
    def validate_direct_assignment(self) -> AgentAssignment:
        if self.mode in {AllocationMode.DIRECT, AllocationMode.CROSS_FUNCTIONAL}:
            if not self.assignees:
                raise ValueError(f"{self.mode} allocation requires at least one assignee")
        unknown_function_agents = set(self.functions_by_agent) - set(self.assignees)
        if unknown_function_agents:
            raise ValueError(
                "function assignments must reference selected assignees: "
                f"{sorted(unknown_function_agents)}"
            )
        return self


class SkillSelection(ContractModel):
    skill_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    provider: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{1,63}$",
    )


class BlackboardTaskInput(ContractModel):
    """User-supplied acceptance, kept separate from an Agent's proposed solution."""

    topic: str = Field(default="task", min_length=1, max_length=500)
    acceptance: tuple[AcceptanceCriterion, ...] = Field(min_length=1, max_length=100)

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("blackboard topic cannot be blank")
        if value == "user-input":
            raise ValueError("user-input is reserved for the original request")
        return value

    @field_validator("acceptance")
    @classmethod
    def unique_criteria(cls, value: tuple[AcceptanceCriterion, ...]):
        if len({item.id for item in value}) != len(value):
            raise ValueError("acceptance criterion ids must be unique")
        return value


class ModelSelection(ContractModel):
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    model: str = Field(min_length=1, max_length=512)
    reasoning_effort: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,31}$")


class TaskRequest(ContractModel):
    goal: str = Field(min_length=1, max_length=20_000)
    model_selection: ModelSelection | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list, max_length=50)
    expected_output: str | None = Field(default=None, max_length=4_000)
    task_type: str = Field(default="general", min_length=1, max_length=100)
    risk_level: RiskLevel = RiskLevel.LOW
    assignment: AgentAssignment = Field(default_factory=AgentAssignment)
    skills: list[SkillSelection] = Field(default_factory=list, max_length=8)
    idempotency_key: str | None = Field(default=None, max_length=200)
    engine: Literal["legacy", "blackboard"] | None = None
    blackboard: BlackboardTaskInput | None = None

    @model_validator(mode="after")
    def validate_engine_input(self) -> TaskRequest:
        if self.engine == "legacy" and self.blackboard is not None:
            raise ValueError("legacy runs do not accept blackboard acceptance criteria")
        if self.engine == "blackboard" and self.blackboard is None:
            raise ValueError("blackboard runs require explicit acceptance criteria")
        return self

    @field_validator("skills")
    @classmethod
    def validate_unique_skills(cls, value: list[SkillSelection]) -> list[SkillSelection]:
        ids = [item.skill_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("skills must be unique")
        return value


class TaskSpec(ContractModel):
    task_id: str = Field(default_factory=lambda: f"task_{uuid4().hex}")
    goal: str = Field(min_length=1, max_length=20_000)
    model_selection: ModelSelection | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    expected_output: str | None = None
    task_type: str = "general"
    risk_level: RiskLevel = RiskLevel.LOW
    assignment: AgentAssignment = Field(default_factory=AgentAssignment)
    skills: list[SkillSelection] = Field(default_factory=list, max_length=8)
    ambiguities: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class SubtaskSpec(ContractModel):
    subtask_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=10_000)
    dependencies: list[str] = Field(default_factory=list)
    worker_role: str = "worker-01"
    assigned_function: AgentFunction | None = None
    logical_model: str = "worker-fast"
    allowed_tools: list[str] = Field(default_factory=list)
    skill_ids: list[str] = Field(default_factory=list, max_length=8)
    acceptance_criteria: list[str] = Field(min_length=1)
    max_output_tokens: int = Field(
        default=5_000,
        ge=1,
        validation_alias=AliasChoices("max_output_tokens", "token_budget"),
        description="Maximum output tokens per model call.",
    )
    cost_budget_usd: float = Field(default=0, ge=0, deprecated=True)

    @field_validator("skill_ids")
    @classmethod
    def validate_unique_skill_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("subtask skill_ids must be unique")
        return value


class Plan(ContractModel):
    plan_id: str = Field(default_factory=lambda: f"plan_{uuid4().hex}")
    version: int = Field(default=1, ge=1)
    subtasks: list[SubtaskSpec] = Field(min_length=1)
    rationale: str = Field(default="", max_length=5_000)

    @model_validator(mode="after")
    def validate_dependency_graph(self) -> Plan:
        ids = [item.subtask_id for item in self.subtasks]
        if len(ids) != len(set(ids)):
            raise ValueError("subtask_id must be unique")
        known = set(ids)
        edges = {item.subtask_id: set(item.dependencies) for item in self.subtasks}
        for subtask_id, dependencies in edges.items():
            if subtask_id in dependencies:
                raise ValueError(f"subtask {subtask_id} cannot depend on itself")
            unknown = dependencies - known
            if unknown:
                raise ValueError(f"subtask {subtask_id} has unknown dependencies: {unknown}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("subtask dependency graph contains a cycle")
            if node in visited:
                return
            visiting.add(node)
            for dependency in edges[node]:
                visit(dependency)
            visiting.remove(node)
            visited.add(node)

        for subtask_id in ids:
            visit(subtask_id)
        return self


class Evidence(ContractModel):
    source: str = Field(min_length=1, max_length=500)
    locator: str | None = Field(default=None, max_length=2_000)
    summary: str = Field(min_length=1, max_length=4_000)
    tool_call_id: str | None = Field(default=None, max_length=200)


class Artifact(ContractModel):
    artifact_id: str = Field(default_factory=lambda: f"artifact_{uuid4().hex}")
    run_id: str
    subtask_id: str
    producer: str
    version: int = Field(default=1, ge=1)
    # The absolute contract ceiling matches RuntimeConfig's configurable
    # ceiling; WorkerAgent applies the deployment's usually smaller limit.
    content: str = Field(min_length=1, max_length=200_000)
    evidence: list[Evidence] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    status: ArtifactStatus = ArtifactStatus.SUCCESS
    created_at: datetime = Field(default_factory=utc_now)
    duration_ms: int = Field(default=0, ge=0)


class ReviewIssue(ContractModel):
    code: str = Field(min_length=1, max_length=120)
    severity: Severity
    message: str = Field(min_length=1, max_length=4_000)
    evidence: str | None = Field(default=None, max_length=4_000)
    recommendation: str | None = Field(default=None, max_length=4_000)


class Review(ContractModel):
    review_id: str = Field(default_factory=lambda: f"review_{uuid4().hex}")
    artifact_id: str
    artifact_version: int = Field(ge=1)
    reviewer: str
    dimension: str
    verdict: ReviewVerdict
    issues: list[ReviewIssue] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    created_at: datetime = Field(default_factory=utc_now)


class Decision(ContractModel):
    decision_id: str = Field(default_factory=lambda: f"decision_{uuid4().hex}")
    action: DecisionAction
    reasons: list[str] = Field(min_length=1)
    target_subtasks: list[str] = Field(default_factory=list)
    human_prompt: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Usage(ContractModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    usage_complete: bool = True
    estimated_cost_usd: float = Field(default=0, ge=0)


class ModelMessage(ContractModel):
    role: MessageRole
    content: str = ""
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    reasoning_content: str | None = None
    response_items: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def validate_tool_message(self) -> ModelMessage:
        if self.role == MessageRole.TOOL and not self.tool_call_id:
            raise ValueError("tool messages require tool_call_id")
        if self.tool_calls and self.role != MessageRole.ASSISTANT:
            raise ValueError("tool_calls are only valid on assistant messages")
        if self.reasoning_content and self.role != MessageRole.ASSISTANT:
            raise ValueError("reasoning_content is only valid on assistant messages")
        if self.response_items and self.role != MessageRole.ASSISTANT:
            raise ValueError("response_items are only valid on assistant messages")
        return self


class ModelRequest(ContractModel):
    run_id: str
    trace_id: str
    node_name: str
    agent_role: str
    logical_model: str
    model_selection: ModelSelection | None = None
    reasoning_effort: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    messages: list[ModelMessage] = Field(min_length=1)
    required_capabilities: set[str] = Field(default_factory=lambda: {"chat"})
    provider_preferences: list[str] = Field(default_factory=list)
    provider_allowlist: set[str] = Field(default_factory=set)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    response_schema: dict[str, Any] | None = None
    timeout_seconds: float = Field(default=60, gt=0)
    max_output_tokens: int = Field(
        default=4_000,
        ge=1,
        validation_alias=AliasChoices("max_output_tokens", "token_budget"),
        description="Maximum output tokens per model call.",
    )
    cost_budget_usd: float = Field(default=0, ge=0, deprecated=True)
    risk_level: RiskLevel = RiskLevel.LOW
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider_preferences")
    @classmethod
    def validate_provider_preferences(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("provider preferences must be unique")
        return value


class ModelResponse(ContractModel):
    content: str = ""
    structured_output: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    reasoning_content: str | None = None
    response_items: list[dict[str, Any]] = Field(default_factory=list)
    finish_reason: str = "stop"
    usage: Usage = Field(default_factory=Usage)
    latency_ms: int = Field(default=0, ge=0)
    first_token_latency_ms: int | None = Field(default=None, ge=0)
    provider: str
    model: str
    provider_request_id: str | None = None
    retries: int = Field(default=0, ge=0)
    fallback_from: str | None = None
    error_summary: str | None = None


class ModelCallRecord(ContractModel):
    node: str
    provider: str
    model: str
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    usage_complete: bool = True
    estimated_cost_usd: float = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    fallback_from: str | None = None
    skill_ids: list[str] = Field(default_factory=list)


class ToolCallRecord(ContractModel):
    node: str
    role: str
    tool_name: str
    call_id: str
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_summary: str
    truncated: bool = False
    duration_ms: int = Field(default=0, ge=0)
    cache_hit: bool = False


class RunError(ContractModel):
    code: str
    message: str
    node: str | None = None
    retryable: bool = False
    occurred_at: datetime = Field(default_factory=utc_now)


class AgentInteraction(ContractModel):
    """Public provenance for completed Agent exchanges; contains no private messages."""

    interaction_id: str
    kind: Literal["assignment", "dependency", "repair", "result", "review"]
    sender: str
    recipient: str
    subtask_id: str
    artifact_id: str | None = None
    artifact_version: int = Field(ge=1)
    review_id: str | None = None
    repair_round: int = Field(default=0, ge=0)


class RunResult(ContractModel):
    pending_intents: list[ObjectRef] = Field(default_factory=list)
    engine: Literal["legacy", "blackboard"] = "legacy"
    run_id: str
    status: RunStatus
    output: str = ""
    task: TaskSpec | None = None
    plan: Plan | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    reviews: list[Review] = Field(default_factory=list)
    decision: Decision | None = None
    errors: list[RunError] = Field(default_factory=list)
    repair_rounds: int = Field(default=0, ge=0)
    usage: Usage = Field(default_factory=Usage)
    model_calls: list[ModelCallRecord] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    agent_interactions: list[AgentInteraction] = Field(default_factory=list)
