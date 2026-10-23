import type { WorkflowCapabilities } from '../types'

export const deliverableTemplates: WorkflowCapabilities['blackboard_templates'] = [
  {
    id: 'analysis-report',
    title: '分析报告',
    sections: '结论摘要、分析依据、风险与局限、建议行动',
  },
  {
    id: 'implementation-plan',
    title: '实施方案',
    sections: '目标与范围、实施步骤、验收标准、风险与回退',
  },
  { id: 'action-checklist', title: '操作清单', sections: '适用范围与前提、操作步骤、检查与确认' },
].map(({ id, title, sections }) => ({
  id,
  title,
  description: `交付包含${sections}的${title}，各章节须有正文。`,
  input: {
    topic: 'document',
    acceptance: [
      {
        id,
        description: `交付${title}`,
        claim_key: `deliverable.${id}`,
        claim_type: 'delivery',
        scope: {
          verification: 'stored_content_and_structure',
          deliverable: id,
          format_version: '1',
        },
        operator: 'exists',
        expected: null,
        required_checks: [
          'content_stored',
          'required_sections_present',
          ...(id === 'action-checklist' ? ['checklist_items_present'] : []),
        ],
      },
    ],
  },
}))
