# 分阶段迁移与后续计划

## 已完成：阶段 0 / 框架架构

- 冻结目录、配置、Prompt 和核心数据合同；
- 建立唯一 Root Graph、并行归并、确定性验证、审查、裁决、返工和人工恢复；
- 建立 Mock/兼容 Provider、Tool Gateway、Checkpoint、CLI、API、测试和最小评估集。

验证：`uv run pytest`、`uv run ruff check .`、`uv run work-harness eval`。

回滚：本阶段仅新增文件；删除新增骨架即可恢复到仅有 `agent.md` 的初始状态。

## 下一阶段：真实单 Agent baseline

目标：选择一个真实 Provider，完成固定评估集的单 Agent 结果、成本和延迟记录。保留当前 Mock 作为回归测试，不直接用多 Agent 结果宣称效果提升。

涉及：`config/models.yaml`、Provider Adapter、评估评分器和报告。风险是结构化输出能力、价格和数据策略与声明不一致。回滚方式是关闭真实 Provider 并恢复 Mock Profile。

## 第三阶段：质量评估与 bad case 回放

目标：补充人工评分、semantic match、tool call accuracy、per-role token/cost、P50/P95 和失败分类；为每个 bad case 保存脱敏输入、配置摘要、Prompt 版本、产物、审查和裁决引用。

## 第四阶段：生产可靠性（调度核心已完成）

已完成：PostgreSQL Checkpoint、可靠任务队列、多副本取消/恢复、租约 fencing、死信重放、Worker
readiness 和真实数据库并发/迁移测试。

剩余目标：组织审批身份、租户隔离、分布式限流、共享可编辑配置、持久事件、OpenTelemetry、
告警、外部数据库权限拆分与目标环境压力/故障注入。完成这些验收后才允许真实多用户生产流量。

## 新增扩展的标准路径

- 新工具：YAML 声明 → Pydantic 输入/输出 → Handler → Registry → 权限/审批测试；
- 新 Prompt：独立 Markdown → 提升版本号 → 固定评估对比；
- 新模型：Provider 白名单 → 能力声明 → 逻辑模型候选 → 健康检查 → 评估；
- 新 Agent：明确单一职责和合同 → 作为 Root Graph 节点/子图 → 设置预算、超时和终止条件。
