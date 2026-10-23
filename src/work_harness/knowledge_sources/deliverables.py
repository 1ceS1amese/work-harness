"""Versioned document deliverables shared by discovery, generation and verification."""

from __future__ import annotations

import re
from dataclasses import dataclass

from work_harness.domain.models import BlackboardTaskInput

DELIVERABLE_REVISION = "document-delivery-v1"


def _sections(body: str) -> dict[str, str]:
    """Read level-two Markdown sections without treating fenced examples as headings."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    fence: str | None = None
    for line in body.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*", line):
                fence = None
            continue
        if marker:
            fence = marker[1]
            continue
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*$", line)
        if heading and len(heading[1]) <= 2:
            current = re.sub(r"\s+#+$", "", heading[2]) if len(heading[1]) == 2 else None
            if current is not None:
                sections.setdefault(current, [])
        elif current is not None and not heading:
            sections[current].append(line)
    return {title: "\n".join(lines) for title, lines in sections.items()}


def _has_content(text: str) -> bool:
    words = re.sub(r"[^\w]+", "", text).casefold()
    return bool(words) and words not in {"todo", "tbd", "待补充", "待填写", "待完善"}


@dataclass(frozen=True)
class DeliverableTemplate:
    id: str
    title: str
    sections: tuple[str, ...]
    guidance: str
    checklist_section: str | None = None

    @property
    def claim_key(self) -> str:
        return f"deliverable.{self.id}"

    @property
    def scope(self) -> dict[str, str]:
        return {
            "verification": "stored_content_and_structure",
            "deliverable": self.id,
            "format_version": "1",
        }

    @property
    def checks(self) -> tuple[str, ...]:
        checks = ("content_stored", "required_sections_present")
        return (*checks, "checklist_items_present") if self.checklist_section else checks

    @property
    def prefix(self) -> str:
        return f"{self.title}已交付（已校验存储和结构）。\n\n"

    @property
    def description(self) -> str:
        return f"交付包含{'、'.join(self.sections)}的{self.title}，各章节须有正文。"

    @property
    def instruction(self) -> str:
        headings = "\n".join(f"## {section}" for section in self.sections)
        return (
            f"交付物为{self.title}。在 Artifact.content 中输出 Markdown 正文，"
            f"使用以下二级标题（标题文字须一致），每节写明与用户目标相关的实际内容：\n{headings}\n"
            f"{self.guidance} 缺少资料时明确列出假设、未知项及核实方法，不编造依据。"
            "不要使用空白章节或 TODO 占位，不要将整份文档包在代码块内。"
            "文档交付不代表其中建议的操作、代码、测试或部署已经执行。"
        )

    def task_input(self) -> BlackboardTaskInput:
        return BlackboardTaskInput.model_validate(
            {
                "topic": "document",
                "acceptance": [
                    {
                        "id": self.id,
                        "description": self.description + " " + self.guidance,
                        "claim_key": self.claim_key,
                        "claim_type": "delivery",
                        "scope": self.scope,
                        "required_checks": self.checks,
                    }
                ],
            }
        )

    def validate_body(self, body: str) -> None:
        sections = _sections(body)
        missing = [name for name in self.sections if not _has_content(sections.get(name, ""))]
        if missing:
            raise ValueError("交付物缺少有效章节正文：" + "、".join(missing))
        if self.checklist_section:
            items = re.findall(
                r"^\s*[-*+]\s+\[[ xX]\]\s+(.+)$",
                sections[self.checklist_section],
                flags=re.MULTILINE,
            )
            if not any(_has_content(item) for item in items):
                raise ValueError("操作步骤须包含 Markdown 待办项（- [ ] 具体操作）")


DELIVERABLE_TEMPLATES = (
    DeliverableTemplate(
        id="analysis-report",
        title="分析报告",
        sections=("结论摘要", "分析依据", "风险与局限", "建议行动"),
        guidance="区分结论与假设，注明依据及其来源，说明局限，并给出有优先级的行动建议。",
    ),
    DeliverableTemplate(
        id="implementation-plan",
        title="实施方案",
        sections=("目标与范围", "实施步骤", "验收标准", "风险与回退"),
        guidance="明确实施范围、依赖和分阶段步骤，为每阶段给出可检查的验收标准及失败回退方法。",
    ),
    DeliverableTemplate(
        id="action-checklist",
        title="操作清单",
        sections=("适用范围与前提", "操作步骤", "检查与确认"),
        guidance=(
            "写清适用条件和准备事项；操作步骤使用 - [ ] 待办项，按顺序给出具体操作及检查方法。"
        ),
        checklist_section="操作步骤",
    ),
)


def deliverable_templates() -> list[dict]:
    return [
        {
            "id": template.id,
            "title": template.title,
            "description": template.description,
            "input": template.task_input().model_dump(mode="json"),
        }
        for template in DELIVERABLE_TEMPLATES
    ]


def selected_deliverable(blackboard: BlackboardTaskInput | None) -> DeliverableTemplate | None:
    if blackboard is None:
        return None
    selected = [
        template
        for template in DELIVERABLE_TEMPLATES
        if any(
            criterion.claim_type == "delivery"
            and criterion.claim_key == template.claim_key
            and criterion.scope == template.scope
            for criterion in blackboard.acceptance
        )
    ]
    if len(selected) > 1:
        raise ValueError("每个任务请选择一种交付物；多份成果请分别创建任务")
    return selected[0] if selected else None
