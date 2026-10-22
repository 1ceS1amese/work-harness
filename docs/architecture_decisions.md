# 架构决策记录

## ADR-001：唯一 Root Graph

以下固定 Worker/Reviewer 顺序适用于兼容旧引擎；黑板控制循环见 ADR-009。

采用 LangGraph `StateGraph` 作为唯一控制平面。动态 Worker / Reviewer 使用 `Send` 调用
独立状态的 Agent 子图，产物和调用记录通过 reducer 归并，Judge 之后仅通过显式条件边路由。
禁止 Provider、工具或外部 Agent 修改全局终止状态。

## ADR-002：TypedDict 运行状态 + Pydantic 边界合同

Graph 内部用 `TypedDict` 和 reducer 获得清晰的并行归并语义；每个节点入口/出口用 Pydantic 再验证。这样避免以 Pydantic State 承担高频归并的额外成本，同时保持 Checkpoint 可序列化和 API 类型稳定。

## ADR-003：默认等待真实 Provider，Mock 仅作为测试夹具

首次启动默认使用 `setup` Profile：禁用所有 Mock Provider，真实模型未就绪时健康状态降级并拒绝创建任务，避免模拟输出被误认为真实交付。确定性 Mock 只保留在显式 `local` 开发 Profile、自动化测试和框架基准中。真实模型通过项目自有 Provider 协议接入，兼容 Chat Completions 与 Responses；结构化返回仍需本地 Pydantic 校验。

## ADR-004：异步 SQLite 仅用于单机阶段

API 和图均为异步路径，因此本地持久化使用 `AsyncSqliteSaver`。SQLite 适合本地与轻量部署，不作为多副本生产方案；生产迁移到 PostgreSQL 前必须增加数据库迁移和并发恢复测试。

## ADR-005：先确定性检查，再调用 Reviewer

此阶段顺序属于旧引擎。黑板引擎使用知识源匹配和受信任验证器，不要求固定 Reviewer 阶段。

缺失产物、空内容、失败状态、依赖停滞和预算上限由程序检查。Reviewer 只承担事实、逻辑和约束等语义审查，避免把可测试规则交给模型。

## ADR-006：PostgreSQL 作为多实例队列与 Checkpoint 的共享事实源

多实例模式将 API 生产者和 Graph Worker 分离，以 PostgreSQL 事务原子保存 Run 与 Job，使用
`FOR UPDATE SKIP LOCKED`、带 fencing token 的可过期租约、有界退避和死信完成至少一次调度。
Redis/NOTIFY 只能作为唤醒加速，不能保存唯一任务事实。SQLite/Memory 继续服务于单机和桌面
模式。完整状态机、故障语义和验收矩阵见
[多实例可靠运行队列设计](distributed-run-queue.md)。

## ADR-007：协调图与每次调用独立的 Agent 子图

Worker 的模型与工具循环使用独立 StateGraph，Review 使用独立复核子图。共享编译后的图
定义，每次 Send 使用独立调用状态及继承父 Checkpointer 的命名空间。私有模型消息只保存在
子图中，父图通过明确的输出 Schema 接收产物和记录。交互合同与升级规则见
[LangGraph 多 Agent 协作核心设计](multi-agent-design.md)。

## ADR-008：版本化依赖与返工传播

派发任务时只传入声明的直接上游产物。返工使目标及所有传递依赖的下游任务失效，并提升它们的
所需产物版本；调度不能用旧成功产物满足新版本依赖。未受影响的产物及复核保留。产物身份、
复核路由和交互记录 ID 均由 Harness 生成。

## ADR-009：以黑板已提交状态驱动知识源协作

黑板只接受 Fact、Intent、Hint 三类对象；知识源可拥有不同内部逻辑和私有 LangGraph 子图。
知识源读取带版本的板面，提交增量提案，内核验证后原子提交。共享知识不通过 Agent 私有消息
传递；调度器根据新板面的目标缺口、事实和提示重新匹配知识源，不预设完整业务流程。

Fact 需要对应验证器的凭据，模型自报成功不能成为确认依据。黑板数据库保存业务事实、附件、
事件和激活账本；Checkpoint 保存控制循环的恢复位置和私有执行状态，两者共同恢复。
首次接纳固定根目标验收条件，CLI 与界面均要求明确选择模板或提供类型化条件。

旧引擎和黑板引擎分别固定版本；接纳默认值或禁止新黑板任务的开关不能重标已有 Run。
新 Run 默认使用黑板，需提供明确验收条件；legacy 继续支持显式选择及部署回退。费用按用户
决定不参与切换验收，质量对照与恢复演练已完成。实际交付与部署验证范围以
[黑板实施计划](blackboard-design.md)为准。

## 参考依据

- LangGraph 官方文档：State、Node、Edge、`Send`、`Command` 与 Checkpoint；
- OpenAI 官方 Structured Outputs 文档：JSON Schema 结构化响应；
- 原始设计输入：`agent.md`。
