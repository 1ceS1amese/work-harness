from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from work_harness.domain.enums import (
    AgentFunction,
    AllocationMode,
    ArtifactStatus,
    DecisionAction,
    RiskLevel,
    RunStatus,
    Severity,
)
from work_harness.domain.models import (
    AgentAssignment,
    Decision,
    ModelMessage,
    ModelRequest,
    Plan,
    RunError,
    SubtaskSpec,
    TaskRequest,
    TaskSpec,
)
from work_harness.workflow.agent_services import AgentServices
from work_harness.workflow.contracts import WORKFLOW_REVISION
from work_harness.workflow.state import GraphState, latest_artifacts, latest_reviews


class WorkflowNodes(AgentServices):
    async def normalize(self, state: GraphState) -> dict[str, Any]:
        run_id = state["run_id"]
        async with self.trace.span(run_id=run_id, node="normalize"):
            request = TaskRequest.model_validate(state["raw_input"])
            self.skills.validate_selections(request.skills)
            risk = request.risk_level
            lower_goal = request.goal.casefold()
            high_risk_markers = (
                "删除生产",
                "生产数据库",
                "delete production",
                "drop database",
                "send email",
                "publish externally",
            )
            if risk == RiskLevel.LOW and any(marker in lower_goal for marker in high_risk_markers):
                risk = RiskLevel.HIGH
            task = TaskSpec(
                goal=request.goal,
                context=request.context,
                constraints=request.constraints,
                expected_output=request.expected_output,
                task_type=request.task_type,
                risk_level=risk,
                assignment=request.assignment,
                skills=request.skills,
            )
            return {
                "task": task.model_dump(mode="json"),
                "workflow_revision": WORKFLOW_REVISION,
                "status": RunStatus.RUNNING.value,
                "repair_round": 0,
            }

    async def plan(self, state: GraphState) -> dict[str, Any]:
        async with self.trace.span(run_id=state["run_id"], node="plan"):
            role = self.config.agents.agents["planner"]
            plan, response = await self.structured_call(
                state=state,
                node_name="plan",
                agent_role="planner",
                prompt_name="planner",
                logical_model=role.logical_model,
                operation="plan",
                payload={
                    "task": state["task"],
                    "selected_skills": self.skills.planner_context(
                        TaskSpec.model_validate(state["task"]).skills
                    ),
                    "execution_limits": {
                        "token_accounting": "observed_usage_only",
                        "workers": {
                            name: {"max_output_tokens": worker.max_output_tokens}
                            for name, worker in self.config.agents.agents.items()
                            if worker.assignable
                        },
                    },
                },
                output_model=Plan,
                max_output_tokens=role.max_output_tokens,
            )
            if len(plan.subtasks) > self.config.runtime.max_planned_subtasks:
                raise ValueError(
                    f"plan has {len(plan.subtasks)} subtasks; limit is "
                    f"{self.config.runtime.max_planned_subtasks}"
                )
            plan = self._apply_assignment_policy(plan, TaskSpec.model_validate(state["task"]))
            return {
                "plan": plan.model_dump(mode="json"),
                "pending_subtasks": [item.model_dump(mode="json") for item in plan.subtasks],
                "required_versions": {item.subtask_id: 1 for item in plan.subtasks},
                "model_calls": [self.model_call("plan", response)],
            }

    def _apply_assignment_policy(self, plan: Plan, task: TaskSpec) -> Plan:
        assignment = AgentAssignment.model_validate(task.assignment)
        assignable = {
            name: role for name, role in self.config.agents.agents.items() if role.assignable
        }
        unknown = set(assignment.assignees) - set(assignable)
        if unknown:
            raise ValueError(f"task assignment references unknown agents: {sorted(unknown)}")

        selected = assignment.assignees or list(assignable)
        if not selected:
            raise ValueError("no assignable agents are configured")

        rewritten: list[SubtaskSpec] = []
        loads: dict[str, int] = {name: 0 for name in selected}
        for index, subtask in enumerate(plan.subtasks):
            needed_function = self._infer_function(subtask)
            if assignment.mode == AllocationMode.AUTO:
                owner = (
                    self._functional_owner(
                        needed_function,
                        selected,
                        assignment.functions_by_agent,
                        loads,
                    )
                    if assignment.functions_by_agent
                    else min(
                        selected,
                        key=lambda name: (loads[name], selected.index(name)),
                    )
                )
            elif assignment.mode == AllocationMode.DIRECT:
                owner = selected[index % len(selected)]
            else:
                owner = self._functional_owner(
                    needed_function,
                    selected,
                    assignment.functions_by_agent,
                    loads,
                )
            loads[owner] = loads.get(owner, 0) + 1
            role = assignable[owner]
            selected_functions = assignment.functions_by_agent.get(owner, [])
            assigned_function = (
                needed_function
                if not selected_functions or needed_function in selected_functions
                else selected_functions[0]
            )
            skill_context = self.skills.resolve(
                task.skills,
                subtask.skill_ids,
                function=assigned_function,
            )
            requested_tools = (
                list(skill_context.allowed_tools)
                if skill_context.skill_ids
                else subtask.allowed_tools
            )
            allowed_tools = [item for item in requested_tools if item in role.allowed_tools]
            rewritten.append(
                subtask.model_copy(
                    update={
                        "worker_role": owner,
                        "assigned_function": assigned_function,
                        "logical_model": skill_context.logical_model or role.logical_model,
                        "allowed_tools": allowed_tools,
                        "skill_ids": list(skill_context.skill_ids),
                    }
                )
            )
        return plan.model_copy(update={"subtasks": rewritten})

    @staticmethod
    def _functional_owner(
        needed_function: AgentFunction,
        assignees: list[str],
        functions_by_agent: dict[str, list[AgentFunction]],
        loads: dict[str, int],
    ) -> str:
        matching = [
            name
            for name in assignees
            if not functions_by_agent.get(name)
            or needed_function in functions_by_agent[name]
        ]
        candidates = matching or assignees
        return min(
            candidates,
            key=lambda name: (loads[name], assignees.index(name)),
        )

    @staticmethod
    def _infer_function(subtask: SubtaskSpec) -> AgentFunction:
        title = subtask.title.casefold()
        description = subtask.description.casefold()
        markers = {
            AgentFunction.ANALYSIS: ("分析", "需求", "假设", "比较", "compare", "analy"),
            AgentFunction.RESEARCH: (
                "研究",
                "证据",
                "来源",
                "调研",
                "research",
                "evidence",
                "source",
            ),
            AgentFunction.EXECUTION: (
                "交付",
                "实现",
                "方案",
                "执行",
                "清单",
                "draft",
                "deliver",
                "build",
                "create",
            ),
            AgentFunction.RISK: (
                "风险",
                "安全",
                "合规",
                "约束",
                "risk",
                "security",
                "compliance",
            ),
        }
        scores = {
            function: sum(marker in title for marker in function_markers) * 5
            + sum(marker in description for marker in function_markers)
            for function, function_markers in markers.items()
        }
        if not any(scores.values()):
            return AgentFunction.EXECUTION
        return max(scores, key=lambda function: scores[function])

    async def schedule(self, state: GraphState) -> dict[str, Any]:
        async with self.trace.span(run_id=state["run_id"], node="schedule"):
            latest = {item["subtask_id"]: item for item in latest_artifacts(state["artifacts"])}
            pending: list[dict[str, Any]] = []
            failed: list[str] = []
            for item in state.get("pending_subtasks", []):
                artifact = latest.get(item["subtask_id"])
                current = artifact is not None and artifact["version"] >= state.get(
                    "required_versions", {}
                ).get(item["subtask_id"], 1)
                if current and artifact["status"] == ArtifactStatus.SUCCESS:
                    continue
                if current and artifact["status"] in {
                    ArtifactStatus.FAILED, ArtifactStatus.PARTIAL,
                }:
                    failed.append(item["subtask_id"])
                pending.append(item)
            if failed:
                error = RunError(
                    code="worker_failed",
                    message=f"workers failed for subtasks: {sorted(failed)}",
                    node="schedule",
                )
                return {
                    "pending_subtasks": pending,
                    "status": RunStatus.FAILED.value,
                    "errors": [error.model_dump(mode="json")],
                }
            return {"pending_subtasks": pending}

    async def validate(self, state: GraphState) -> dict[str, Any]:
        async with self.trace.span(run_id=state["run_id"], node="validate"):
            issues: list[dict[str, Any]] = []
            plan = Plan.model_validate(state["plan"])
            latest = {item["subtask_id"]: item for item in latest_artifacts(state["artifacts"])}
            for subtask in plan.subtasks:
                artifact = latest.get(subtask.subtask_id)
                if artifact is None:
                    issues.append(
                        {
                            "subtask_id": subtask.subtask_id,
                            "code": "missing_artifact",
                            "severity": Severity.ERROR.value,
                            "message": "No artifact exists for planned subtask.",
                        }
                    )
                    continue
                if not artifact.get("content", "").strip():
                    issues.append(
                        {
                            "subtask_id": subtask.subtask_id,
                            "code": "empty_artifact",
                            "severity": Severity.ERROR.value,
                            "message": "Artifact content is empty.",
                        }
                    )
                if artifact["status"] == ArtifactStatus.FAILED:
                    issues.append(
                        {
                            "subtask_id": subtask.subtask_id,
                            "code": "failed_artifact",
                            "severity": Severity.ERROR.value,
                            "message": "Worker marked the artifact as failed.",
                        }
                    )
            return {"validation_issues": issues}

    async def judge(self, state: GraphState) -> dict[str, Any]:
        async with self.trace.span(run_id=state["run_id"], node="judge"):
            role = self.config.agents.agents["judge"]
            current_reviews = latest_reviews(state.get("reviews", []), state["artifacts"])
            decision, response = await self.structured_call(
                state=state,
                node_name="judge",
                agent_role="judge",
                prompt_name="judge",
                logical_model=role.logical_model,
                operation="judge",
                payload={
                    "task": state["task"],
                    "artifacts": latest_artifacts(state["artifacts"]),
                    "validation_issues": state.get("validation_issues", []),
                    "reviews": current_reviews,
                    "repair_round": state.get("repair_round", 0),
                },
                output_model=Decision,
                max_output_tokens=role.max_output_tokens,
            )
            update: dict[str, Any] = {
                "decision": decision.model_dump(mode="json"),
                "model_calls": [self.model_call("judge", response)],
            }
            if decision.action == DecisionAction.REQUEST_HUMAN:
                update["status"] = RunStatus.WAITING_HUMAN.value
            elif decision.action == DecisionAction.FAIL:
                update["status"] = RunStatus.FAILED.value
            return update

    async def repair_plan(self, state: GraphState) -> dict[str, Any]:
        async with self.trace.span(run_id=state["run_id"], node="repair_plan"):
            decision = Decision.model_validate(state["decision"])
            plan = Plan.model_validate(state["plan"])
            targets = set(decision.target_subtasks)
            known = {item.subtask_id for item in plan.subtasks}
            if targets - known:
                raise ValueError("repair targets must reference planned subtasks")
            if not targets:
                targets = known
            # Every descendant must be recomputed after its inputs change.
            while True:
                expanded = targets | {
                    item.subtask_id for item in plan.subtasks
                    if targets.intersection(item.dependencies)
                }
                if expanded == targets:
                    break
                targets = expanded
            latest = {item["subtask_id"]: item for item in latest_artifacts(state["artifacts"])}
            required = dict(state.get("required_versions", {}))
            required.update({key: int(latest[key]["version"]) + 1 for key in targets})
            selected = [
                item.model_dump(mode="json")
                for item in plan.subtasks
                if item.subtask_id in targets
            ]
            return {
                "pending_subtasks": selected,
                "required_versions": required,
                "repair_round": state.get("repair_round", 0) + 1,
                "status": RunStatus.RUNNING.value,
            }

    async def human_gate(self, state: GraphState) -> dict[str, Any]:
        from langgraph.types import interrupt

        decision = Decision.model_validate(state["decision"])
        response = interrupt(
            {
                "run_id": state["run_id"],
                "prompt": decision.human_prompt or "This run requires explicit approval.",
                "reasons": decision.reasons,
            }
        )
        approved = isinstance(response, dict) and bool(response.get("approved"))
        if approved:
            next_decision = Decision(
                action=DecisionAction.APPROVE,
                reasons=["Human approval received."],
            )
            return {
                "decision": next_decision.model_dump(mode="json"),
                "status": RunStatus.RUNNING.value,
            }
        next_decision = Decision(
            action=DecisionAction.FAIL,
            reasons=["Human approval was denied."],
        )
        return {
            "decision": next_decision.model_dump(mode="json"),
            "status": RunStatus.FAILED.value,
        }

    async def synthesize(self, state: GraphState) -> dict[str, Any]:
        async with self.trace.span(run_id=state["run_id"], node="synthesize"):
            role = self.config.agents.agents["synthesizer"]
            prompt = self.prompts.get("synthesizer")
            artifacts = latest_artifacts(state["artifacts"])
            request = ModelRequest(
                run_id=state["run_id"],
                trace_id=f"trace_{uuid4().hex}",
                node_name="synthesize",
                agent_role="synthesizer",
                logical_model=role.logical_model,
                messages=[
                    ModelMessage(role="system", content=prompt.body),
                    ModelMessage(
                        role="user",
                        content=json.dumps(
                            {"task": state["task"], "artifacts": artifacts},
                            ensure_ascii=False,
                            default=str,
                        ),
                    ),
                ],
                max_output_tokens=role.max_output_tokens,
                risk_level=state["task"]["risk_level"],
                metadata={
                    "operation": "synthesize",
                    "payload": {"task": state["task"], "artifacts": artifacts},
                    "prompt_name": prompt.name,
                    "prompt_version": prompt.version,
                },
            )
            response = await self.gateway.complete(request)
            return {
                "final_output": response.content,
                "status": RunStatus.SUCCEEDED.value,
                "model_calls": [self.model_call("synthesize", response)],
            }

    async def fail(self, state: GraphState) -> dict[str, Any]:
        if state.get("errors"):
            return {"status": RunStatus.FAILED.value}
        decision = state.get("decision", {})
        message = "; ".join(decision.get("reasons", [])) or "workflow failed"
        error = RunError(code="workflow_failed", message=message, node="fail")
        return {
            "status": RunStatus.FAILED.value,
            "errors": [error.model_dump(mode="json")],
        }
