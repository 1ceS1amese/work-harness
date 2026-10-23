"""Deliverable choices change generation and evidence, not merely the displayed label."""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest
from test_backup_restore import project as project

from work_harness import cli
from work_harness.api.app import create_app
from work_harness.blackboard.models import ContentRef, Evidence, FactContent
from work_harness.domain.models import TaskRequest
from work_harness.knowledge_sources.adapters.callback import VerifiedObservation
from work_harness.knowledge_sources.catalog import (
    acceptance_task_input,
    catalog_revision,
    draft_task_input,
    verify_delivery,
)
from work_harness.knowledge_sources.deliverables import DELIVERABLE_TEMPLATES
from work_harness.llm.mock import MockProvider

DOCUMENTS = {
    "analysis-report": """## 结论摘要
优先验证本地预览的完成条件。
## 分析依据
依据用户给出的界面截图；尚无真实使用统计。
## 风险与局限
截图不能反映所有用户习惯，需要实际试用。
## 建议行动
1. 先测试三种交付物的选择与提交。
2. 再收集试用反馈。
""",
    "implementation-plan": """## 目标与范围
为团队上线一个内部文档工作台，暂不接入部署工具。
## 实施步骤
第一阶段核对需求，第二阶段配置模型，第三阶段试用。
## 验收标准
每个阶段由负责人核对文档并记录待办事项。
## 风险与回退
渠道不可用时保留原有配置，并暂停新任务。
""",
    "action-checklist": """## 适用范围与前提
适用于本地预览，需要服务已启动。
## 操作步骤
- [ ] 打开主页，确认能看到完成条件选择框。
- [ ] 选择操作清单，提交目标并检查输出。
## 检查与确认
确认输出包含具体操作和检查方法；异常时记录错误信息。
""",
}


@pytest.fixture
def document_worker(monkeypatch):
    documents = dict(DOCUMENTS)
    calls = []
    dispatch = MockProvider._dispatch

    def complete(self, operation, payload, run_id):
        result = dispatch(self, operation, payload, run_id)
        if operation == "worker":
            board = payload["task"].get("context", {}).get("blackboard", {})
            for criterion in board.get("root_intent", {}).get("acceptance", []):
                name = criterion["claim_key"].removeprefix("deliverable.")
                if name in documents:
                    calls.append(payload)
                    result["content"] = documents[name]
        return result

    monkeypatch.setattr(MockProvider, "_dispatch", complete)
    return documents, calls


@pytest.mark.parametrize("template", DELIVERABLE_TEMPLATES, ids=lambda item: item.id)
async def test_selected_document_is_generated_stored_and_verified(
    project, document_worker, template
):
    app = create_app(project)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        choices = (await client.get("/v1/capabilities")).json()["workflow"]["blackboard_templates"]
        assert [choice["title"] for choice in choices] == ["分析报告", "实施方案", "操作清单"]
        choice = next(item for item in choices if item["id"] == template.id)
        accepted = await client.post(
            "/v1/runs",
            json={"goal": "为团队准备工作台试用材料", "blackboard": choice["input"]},
        )
        assert accepted.status_code == 202, accepted.text
        run_id = accepted.json()["run_id"]
        for _ in range(300):
            result = (await client.get(f"/v1/runs/{run_id}")).json()
            if result["status"] not in {"pending", "running"}:
                break
            await asyncio.sleep(0.01)
        assert result["status"] == "succeeded", result
        assert result["output"] == (template.prefix + DOCUMENTS[template.id]).strip()
        snapshot = (await client.get(f"/v1/runs/{run_id}/blackboard")).json()
        assert snapshot["root_intent"]["acceptance"] == choice["input"]["acceptance"]
        objects = (await client.get(f"/v1/runs/{run_id}/blackboard/objects")).json()["items"]
        facts = [item for item in objects if item["kind"] == "fact"]
        assert len(facts) == 1
        assert facts[0]["claim_key"] == template.claim_key
        assert facts[0]["scope"] == template.scope
        assert facts[0]["verification"]["checks"] == list(template.checks)
    _, calls = document_worker
    assert calls and all(template.instruction in call["task"]["constraints"] for call in calls)
    assert all(template.description in call["subtask"]["acceptance_criteria"][0] for call in calls)


async def test_plain_saved_text_cannot_complete_a_report(project, document_worker):
    documents, _ = document_worker
    documents["analysis-report"] = "文档已保存。所有工作都已完成。"
    app = create_app(project)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        accepted = await client.post(
            "/v1/runs",
            json={
                "goal": "提供分析报告",
                "blackboard": acceptance_task_input("analysis-report").model_dump(mode="json"),
            },
        )
        run_id = accepted.json()["run_id"]
        for _ in range(300):
            result = (await client.get(f"/v1/runs/{run_id}")).json()
            if result["status"] not in {"pending", "running"}:
                break
            await asyncio.sleep(0.01)
        assert result["status"] in {"failed", "waiting_human"}, result
        objects = (await client.get(f"/v1/runs/{run_id}/blackboard/objects")).json()["items"]
        assert not any(item["kind"] == "fact" for item in objects)


def delivery_fact(template, body):
    encoded = body.encode()
    ref = ContentRef(
        content_digest=hashlib.sha256(encoded).hexdigest(),
        size_bytes=len(encoded),
        media_type="text/markdown",
    )
    content = FactContent(
        topic="document",
        statement=template.prefix + body[:19_000],
        claim_key=template.claim_key,
        claim_type="delivery",
        scope=template.scope,
        value=ref.model_dump(mode="json"),
        attachments=(ref,),
        evidence=(
            Evidence(
                source="delivery",
                locator=f"content:{ref.content_digest}",
                content_digest=ref.content_digest,
                description="Checked stored bytes and sections.",
            ),
        ),
    )
    return content, {"body": body, "ref": ref.model_dump(mode="json")}


@pytest.mark.parametrize("template", DELIVERABLE_TEMPLATES, ids=lambda item: item.id)
def test_receipt_binds_document_bytes_type_and_checks(template):
    content, proof = delivery_fact(template, DOCUMENTS[template.id])
    observation = VerifiedObservation(
        local_id=template.id, content=content, verifier_id=f"stored-{template.id}", proof=proof
    )
    assert observation.proof == proof
    assert (
        verify_delivery(observation.content, observation.proof, template=template)
        == template.checks
    )
    assert verify_delivery(content, proof, template=template) == template.checks
    with pytest.raises(ValueError, match="does not match"):
        verify_delivery(content, {**proof, "body": proof["body"] + "篡改"}, template=template)
    with pytest.raises(ValueError, match="does not match"):
        verify_delivery(content.model_copy(update={"scope": {}}), proof, template=template)
    other = next(item for item in DELIVERABLE_TEMPLATES if item.id != template.id)
    with pytest.raises(ValueError, match="does not match"):
        verify_delivery(content, proof, template=other)


@pytest.mark.parametrize(
    "body",
    [
        "已完成",
        "## 结论摘要\n## 分析依据\n## 风险与局限\n## 建议行动",
        "## 结论摘要\nTODO\n## 分析依据\n待补充\n## 风险与局限\n...\n## 建议行动\n---",
        "```markdown\n" + DOCUMENTS["analysis-report"] + "```",
        "~~~markdown\n" + DOCUMENTS["analysis-report"] + "~~~",
        DOCUMENTS["analysis-report"].replace("## 风险与局限", "## 其他事项"),
    ],
)
def test_incomplete_or_example_only_document_is_rejected(body):
    template = DELIVERABLE_TEMPLATES[0]
    content, proof = delivery_fact(template, body)
    with pytest.raises(ValueError, match="缺少有效章节"):
        verify_delivery(content, proof, template=template)


@pytest.mark.parametrize("steps", ["按要求完成即可。", "- [ ] TODO", "```\n- [ ] 打开主页\n```"])
def test_checklist_requires_actual_list_items(steps):
    body = (
        "## 适用范围与前提\n服务已启动。\n## 操作步骤\n" + steps + "\n## 检查与确认\n检查服务响应。"
    )
    with pytest.raises(ValueError):
        DELIVERABLE_TEMPLATES[2].validate_body(body)


@pytest.mark.parametrize("template", DELIVERABLE_TEMPLATES, ids=lambda item: item.id)
def test_cli_accepts_each_document_contract(project, document_worker, capsys, template):
    assert (
        cli.main(
            [
                "--project-root",
                str(project),
                "run",
                "--goal",
                "准备工作台试用材料",
                "--acceptance-template",
                template.id,
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "succeeded"
    assert result["output"] == (template.prefix + DOCUMENTS[template.id]).strip()


def test_original_draft_contract_keeps_its_policy_identity():
    assert acceptance_task_input("saved-draft") == draft_task_input()
    assert (
        catalog_revision(TaskRequest(goal="旧任务", blackboard=draft_task_input()))
        == "draft-delivery-v1"
    )
    assert (
        catalog_revision(
            TaskRequest(goal="分析报告", blackboard=acceptance_task_input("analysis-report"))
        )
        == "document-delivery-v1"
    )
