import pytest
from pydantic import ValidationError

from work_harness.domain.enums import AllocationMode
from work_harness.domain.models import AgentAssignment, Plan, SubtaskSpec


def test_plan_rejects_dependency_cycle() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        Plan(
            subtasks=[
                SubtaskSpec(
                    subtask_id="a",
                    title="A",
                    description="A",
                    dependencies=["b"],
                    acceptance_criteria=["done"],
                ),
                SubtaskSpec(
                    subtask_id="b",
                    title="B",
                    description="B",
                    dependencies=["a"],
                    acceptance_criteria=["done"],
                ),
            ]
        )


def test_plan_accepts_dependency_dag() -> None:
    plan = Plan(
        subtasks=[
            SubtaskSpec(
                subtask_id="a",
                title="A",
                description="A",
                acceptance_criteria=["done"],
            ),
            SubtaskSpec(
                subtask_id="b",
                title="B",
                description="B",
                dependencies=["a"],
                acceptance_criteria=["done"],
            ),
        ]
    )
    assert [item.subtask_id for item in plan.subtasks] == ["a", "b"]


def test_direct_assignment_requires_an_agent() -> None:
    with pytest.raises(ValidationError, match="requires at least one assignee"):
        AgentAssignment(mode=AllocationMode.DIRECT)


def test_function_assignment_only_references_selected_agents() -> None:
    with pytest.raises(ValidationError, match="selected assignees"):
        AgentAssignment(
            mode=AllocationMode.CROSS_FUNCTIONAL,
            assignees=["worker-01"],
            functions_by_agent={"worker-02": ["analysis"]},
        )


def test_function_assignment_requires_a_non_empty_function_set() -> None:
    with pytest.raises(ValidationError, match="requires at least one function"):
        AgentAssignment(
            mode=AllocationMode.CROSS_FUNCTIONAL,
            assignees=["worker-01"],
            functions_by_agent={"worker-01": []},
        )


def test_legacy_specialist_ids_migrate_to_homogeneous_agents() -> None:
    assignment = AgentAssignment(
        mode=AllocationMode.CROSS_FUNCTIONAL,
        assignees=["worker-analysis", "worker-delivery", "worker-risk"],
    )
    assert assignment.assignees == ["worker-01", "worker-03", "worker-04"]
    assert assignment.functions_by_agent == {
        "worker-01": ["analysis"],
        "worker-03": ["execution"],
        "worker-04": ["risk"],
    }
