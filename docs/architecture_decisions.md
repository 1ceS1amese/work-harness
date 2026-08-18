# 架构决策记录

## ADR-001：唯一 Root Graph

采用 LangGraph `StateGraph` 作为唯一控制平面。动态 Worker / Reviewer 使用 `Send`，列表字段使用 reducer 追加，Judge 之后仅通过显式条件边路由。禁止 Provider、工具或外部 Agent 修改全局终止状态。

## ADR-002：TypedDict 运行状态 + Pydantic 边界合同

Graph 内部用 `TypedDict` 和 reducer 获得清晰的并行归并语义；每个节点入口/出口用 Pydantic 再验证。这样避免以 Pydantic State 承担高频归并的额外成本，同时保持 Checkpoint 可序列化和 API 类型稳定。

## ADR-003：默认 Mock，真实 Provider 可替换

首版默认 Mock Provider，确保没有密钥时也能验证图拓扑、状态、并行、恢复和 Schema。真实模型通过项目自有 Provider 协议接入。OpenAI 兼容 Adapter 使用 Chat Completions 的 `/chat/completions` 边界，并在声明结构化输出时提交 JSON Schema；返回结果仍需本地 Pydantic 校验。

## ADR-004：异步 SQLite 仅用于单机阶段

API 和图均为异步路径，因此本地持久化使用 `AsyncSqliteSaver`。SQLite 适合本地与轻量部署，不作为多副本生产方案；生产迁移到 PostgreSQL 前必须增加数据库迁移和并发恢复测试。

## ADR-005：先确定性检查，再调用 Reviewer

缺失产物、空内容、失败状态、依赖停滞和预算上限由程序检查。Reviewer 只承担事实、逻辑和约束等语义审查，避免把可测试规则交给模型。

## 参考依据

- LangGraph 官方文档：State、Node、Edge、`Send`、`Command` 与 Checkpoint；
- OpenAI 官方 Structured Outputs 文档：JSON Schema 结构化响应；
- 原始设计输入：`agent.md`。

