# LangGraph + LangChain 多 Agent 交叉设计系统轻量化与性能收益报告

**版本日期：2026-08-19**
**适用范围：Python、LangGraph、LangChain、多 Agent 交叉生成/评审/裁决、HTTP/SSE 服务**

---

## 1. 执行摘要

本项目的默认技术路线应确定为：

```text
Starlette + Uvicorn + msgspec（仅 API 边界）
                    │
                    ▼
          单个预编译 LangGraph 主图
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
   Agent 子图 A  Agent 子图 B  Agent 子图 C
        └───────────┼───────────┘
                    ▼
          Cross Review / Judge
                    ▼
              Synthesizer
```

核心结论如下。

| 决策项 | 建议 |
|---|---|
| Web 框架 | 用 Starlette 替代 FastAPI 的薄 API 层 |
| ASGI Server | 默认 Uvicorn；只有压测证明 HTTP/2、SSE、WebSocket 场景有收益时才切 Granian |
| Agent 部署 | 同一业务域内使用进程内 LangGraph subgraph，不给每个 Agent 单独套 HTTP 服务 |
| 状态模型 | 内部使用 `TypedDict`/`dataclass`；API 输入输出使用 msgspec |
| 图生命周期 | 进程启动时编译一次，所有请求复用 |
| 调度 | 独立 Agent 使用 `Send` 扇出和并行 superstep；设置全局并发上限 |
| 流式输出 | SSE 默认只发送 `updates`/`messages`/`custom` 中真正需要的一种或几种 |
| 持久化 | 简单服务保持无状态；需要断点、恢复、线程和长期任务时使用 Postgres checkpointer |
| 分布式运行 | 出现任务队列、取消、恢复、独立扩缩容或 A2A 需求时迁移到 LangGraph Agent Server |
| 淘汰项 | 新项目不采用 LangServe；不因微基准直接迁移到 Litestar、Sanic、Go 或 Rust |

**最重要的判断：更换 FastAPI 不是最大收益来源。** Web 框架优化只减少 $T_{api}$；多 Agent 系统的主要耗时通常来自模型、工具、检索、持久化以及错误重试。真正能形成数量级收益的是并行化独立 Agent、消除 Agent 间 HTTP 跳转、减少重复上下文、采用自适应 Agent 数量和降低无效模型调用。

---

## 2. 目标与边界

### 2.1 优化目标

本报告同时优化四个维度：

| 维度 | 目标指标 |
|---|---|
| 延迟 | 首事件 p95、完整运行 p95/p99、取消延迟 |
| 吞吐与资源 | RPS、并发会话数、空闲 RSS、峰值 RSS、冷启动 |
| 模型成本 | 调用次数、输入/输出 token、重试次数、单次任务成本 |
| 任务效用 | 正确率、覆盖率、交叉评审增益、失败率、成本调整后质量 |

### 2.2 不应混淆的两个问题

**问题 A：Web 层是否轻量。** 由 Starlette/FastAPI、Uvicorn/Granian、JSON 校验与日志决定。

**问题 B：多 Agent 工作流是否高效。** 由 Agent 拓扑、并行度、模型路由、上下文长度、持久化、缓存和失败策略决定。

只有在 `health` 和 `delay_ms=0` 的基准中，Web 框架差异才会充分显现；真实 LLM 调用持续数秒时，入口层节省的几毫秒会被稀释。因此，必须分层测量，不能只测端到端，也不能只测 Hello World。

---

## 3. 技术选型论证

### 3.1 Starlette 为什么是默认选择

FastAPI 构建在 Starlette 之上，并增加 Pydantic 参数验证、响应序列化、依赖注入和 OpenAPI 等能力。你的 Agent 服务通常只有 `/health`、`/invoke`、`/stream`、`/resume` 等少量接口，不需要完整 CRUD 框架，因此可以直接保留 Starlette 的 ASGI、路由、StreamingResponse、WebSocket、中间件和 lifespan，移除没有使用的上层功能。

收益主要体现为：

1. 更少的 API 层抽象和序列化路径；
2. 更少的框架依赖和启动工作；
3. 对 SSE、断线检测和请求生命周期拥有更直接的控制；
4. API 与 LangGraph 事件流之间不再经过额外响应模型转换。

代价是需要自己维护请求错误格式、认证、API 文档和输入限制。因此，本方案不是“Starlette 永远比 FastAPI 好”，而是“在少接口、内部 Agent 服务、无需自动 OpenAPI 的约束下更合适”。

### 3.2 为什么默认仍使用 Uvicorn

Agent 请求通常是长耗时、I/O 密集、SSE 持续输出。此时 ASGI Server 的协议处理并不是主要瓶颈。Uvicorn 生态成熟、诊断简单，并且是公平基线。

Granian 基于 Rust、Hyper 和 Tokio，支持 ASGI、HTTP/1、HTTP/2 与 WebSocket。它在 HTTP/2、高连接密度和吞吐优先的场景值得测试，但不应仅凭第三方排行榜替换。采用门槛应是：目标并发和真实流式负载下，Granian 的 p95、连接密度或 RSS 有明确改善，而且不存在 ASGI 扩展、调试或部署兼容问题。

### 3.3 为什么不优先选择 Litestar、Sanic、Go 或 Rust

| 选项 | 不作为默认方案的原因 |
|---|---|
| Litestar | 能力更完整，但不是极简薄壳；迁移收益可能小于工程切换成本 |
| Sanic/Robyn | 与 LangGraph 主体仍在 Python 中，框架迁移不能解决模型与图调度瓶颈 |
| Go/Rust API | 需要保留 Python LangGraph 进程，跨语言 RPC 反而增加序列化、部署和故障面 |
| Bare ASGI | 可再减少少量抽象，但路由、中间件、错误处理和维护成本不划算 |

Go/Rust 更适合独立的边缘网关、CPU 密集工具或高性能协议服务，而不是把 LangGraph 控制面强行迁出 Python。

---

## 4. 推荐的多 Agent 交叉设计拓扑

### 4.1 不推荐：每个 Agent 一个 HTTP 服务

```text
Controller → HTTP → Agent A
           → HTTP → Agent B
           → HTTP → Agent C
```

这种结构会重复承担 JSON 编解码、连接池等待、超时、鉴权、追踪、错误映射和服务发现。只要 Agent 属于同一安全域、同一发布周期且不需要独立扩缩容，就应作为主图中的 subgraph 或 node 直接调用。

### 4.2 推荐：进程内扇出、汇合与裁决

```text
Input
  │
Planner / Router
  │
  ├─ Send → Designer A ─┐
  ├─ Send → Designer B ─┼─→ Review Aggregator
  └─ Send → Designer C ─┘          │
                                   ▼
                           Judge / Synthesizer
                                   │
                                 Output
```

独立 Agent 在同一 superstep 中并行运行；每个 worker 返回状态增量，利用 reducer 合并。主图和子图均在进程启动时编译一次。

### 4.3 交叉评审的调用复杂度

假设有 $n$ 个候选 Agent：

| 模式 | 评审调用数 | 特点 |
|---|---:|---|
| 全连接互评 | $n(n-1)$ | 信息最密，但成本和延迟呈二次增长 |
| 环形互评 | $n$ | 每个候选由另一个 Agent 评审，成本线性 |
| 每个候选随机 $k$ 个评审者 | $nk$ | 可调节质量与成本 |
| 中央 Judge | $1$ | 最省调用，但单点偏差较大 |
| 两阶段自适应 | 平均小于固定深度 | 简单任务快速结束，争议任务升级 |

默认推荐“**独立生成 + 环形互评或中央 Judge + 自适应升级**”，不要默认全连接互评。

### 4.4 自适应升级策略

```text
第 1 阶段：2 个便宜/快速 Agent 独立生成
          │
          ▼
轻量 Judge 计算一致性、覆盖率和风险
          │
      ┌───┴──────────────┐
      │低争议             │高争议
      ▼                   ▼
直接合成            再生成 1~2 个候选
                          │
                    深度交叉评审
                          │
                        合成
```

该策略通常比固定启动 5~8 个 Agent 更有经济性，因为平均调用数由任务难度决定。阈值应从离线评测集标定，而不是凭感觉设置。

---

## 5. 性能收益模型

### 5.1 总延迟分解

$$
T_{total}=T_{api}+T_{queue}+T_{graph}+T_{model}+T_{tool}+T_{persist}+T_{retry}
$$

FastAPI → Starlette 主要影响 $T_{api}$，偶尔影响一部分序列化成本；Agent 并行和拓扑优化影响 $T_{model}+T_{tool}$，因此收益上限更大。

例如，Web 层节省 2 ms，而真实任务耗时 4 s，则端到端改善仅为：

$$
\frac{2}{4000}=0.05\%
$$

但 3 个相互独立、各耗时 1 s 的 Agent 从串行改为并行，理想情况下可由约 3 s 降到约 1 s。实际速度提升受供应商限流、连接池、合并阶段和长尾 Agent 限制。

### 5.2 并行上限

串行：

$$
T_{seq}\approx\sum_{i=1}^{n}T_i+T_{merge}
$$

并行：

$$
T_{par}\approx\max_i T_i+T_{dispatch}+T_{merge}
$$

理想加速比：

$$
S=\frac{T_{seq}}{T_{par}}
$$

并行不是免费收益。它可能提高瞬时 QPS、触发 429、增加 token 成本，并把整体延迟锁定在最慢 Agent 上。因此应同时使用 `max_concurrency`、异步信号量、供应商限流器和超时。

建议的并发上限为：

$$
C_{effective}=\min(C_{provider},C_{tool},C_{db},C_{worker},C_{budget})
$$

### 5.3 成本调整后效用

只优化延迟可能导致更多模型调用。建议采用统一指标：

$$
U=\frac{Q\times P_{success}}{C_{model}+\lambda_t T_{95}+\lambda_m M_{peak}}
$$

其中 $Q$ 是离线质量分，$C_{model}$ 是单次调用成本，$T_{95}$ 是完整运行 p95，$M_{peak}$ 是峰值资源，系数由业务价值设定。最终方案应最大化 $U$，而不是单独最大化 RPS。

---

## 6. 可复现实验工程

### 6.1 版本快照

`pyproject.toml` 固定了 2026-08-19 对应的直接依赖：

```text
langgraph==1.2.11
langchain-core==1.5.6
starlette==1.6.0
uvicorn[standard]==0.52.4
msgspec==0.21.1
fastapi==0.141.1
granian==2.8.1
httpx==0.28.1
pytest==9.1.1
pytest-asyncio==1.4.0
```

首次联网执行 `uv sync --all-extras` 后会生成 `uv.lock`。必须将该锁文件纳入版本控制，直接依赖固定并不能完全锁定传递依赖。

### 6.2 模拟图为什么不需要 API Key

复现图包含：

1. Planner 根据 `agent_count` 动态生成 reviewer；
2. 并行图通过 `Send` 扇出；
3. 串行图依次执行同一批 reviewer；
4. reviewer 使用 `asyncio.sleep` 模拟外部 I/O；
5. 使用输入哈希生成确定性评分；
6. Synthesizer 汇总结果。

这样可先测出纯框架和编排开销，避免 LLM 网络波动掩盖差异。接入真实模型后再进行第二阶段端到端测试。

### 6.3 运行步骤

```bash
cd langgraph_starlette_repro
uv sync --all-extras
uv run pytest -q
make bench
```

完整基准默认在 `18080`、`18081`、`18082` 三个端口运行，避免常见的 8000/8001 端口冲突。可通过环境变量修改：

```bash
REPEATS=5 BASE_PORT=19080 ./bench/run_matrix.sh
```

### 6.4 基准矩阵

| 名称 | 请求 | 作用 |
|---|---|---|
| `health` | GET `/health` | 隔离 ASGI、路由和 JSON 响应 |
| `graph` | 3 Agent，0 ms | 测 API + LangGraph 调度和序列化 |
| `parallel` | 3 Agent × 100 ms，并行 | 测扇出/汇合 |
| `sequential` | 3 Agent × 100 ms，串行 | 作为并行收益基线 |
| `stream` | 并行 + SSE | 测首事件 p95 与完整流式延迟 |

每种服务器组合运行相同负载、单 worker、关闭访问日志并复用 HTTP 连接。脚本会记录多次重复实验的中位数、冷启动、空闲 RSS 和峰值 RSS，并生成 `summary.md`、`summary.csv` 和 `summary.json`。

### 6.5 公平实验要求

必须保持以下变量一致：

| 项目 | 要求 |
|---|---|
| Python | 同一解释器和同一锁文件 |
| worker | 每个方案均为 1；多实例测试另开一组实验 |
| 日志 | 访问日志关闭，业务日志级别一致 |
| 硬件 | 同一主机、CPU governor、容器限制和网络 |
| 负载 | 相同请求体、并发、预热、重复次数 |
| 模型 | 同一模型、区域、连接池、超时、重试策略 |
| 结果 | 至少报告 p50/p95/p99、失败率、RSS 和成本 |

真实模型测试应至少重复 5 轮，并优先使用每轮中位数，避免单次网络抖动误导结论。

---

## 7. 收益最大化优化路线

### 7.1 P0：先建立可观测基线

上线前记录：

```text
run_id / thread_id
图节点开始和结束时间
每个模型的首 token 与完整耗时
每个工具的耗时、错误和重试
输入/输出 token
checkpoint 大小
最终质量评分
```

没有节点级指标时，更换框架只是在猜测。

### 7.2 P1：优化图结构，收益最高

1. 主图和子图只编译一次；禁止在 endpoint 中 `build_graph().compile()`。
2. 独立 Agent 使用并行分支或 `Send`，有数据依赖的节点才串行。
3. Agent 同进程调用，不通过 HTTP 自我调用。
4. 所有 LLM、MCP、向量库、数据库和 HTTP 工具使用异步 API。
5. 为最慢 Agent 设置 deadline；超时后允许 Judge 使用已有候选继续。
6. 采用环形评审、随机 $k$ 邻居或自适应升级，避免全连接互评。
7. 汇合节点只消费必要摘要，不重复携带完整消息历史。

### 7.3 P2：减少状态与序列化

1. 内部状态使用 `TypedDict`/`dataclass`，避免每个 superstep 递归 Pydantic 校验。
2. msgspec 只处理 HTTP 边界，不强行改造 LangChain 的 `BaseMessage` 生态。
3. 状态更新只返回 delta，不复制整个 state。
4. 大文件、长工具输出和检索文档存对象存储，只在 state 中保存引用和摘要。
5. 生产流式模式优先 `updates`；不对普通用户发送 `values`、`debug` 或完整 checkpoint。
6. 输出结构固定，避免同一内容在消息、状态和 tracing 中重复序列化。

### 7.4 P3：降低模型调用与 token 成本

| 方法 | 直接收益 |
|---|---|
| 自适应 Agent 数量 | 简单任务减少调用 |
| 便宜模型生成/评审，强模型最终合成 | 降低平均单价 |
| Prompt 去重与共享上下文摘要 | 降低输入 token |
| 结构化短评审 | 降低输出 token 和解析失败 |
| 语义/工具结果缓存 | 消除重复外部调用 |
| 争议阈值与早停 | 低风险任务提前结束 |
| 模型路由 | 简单节点不调用最强模型 |
| 限制重试预算 | 防止故障时成本失控 |

建议默认从 2 个候选 Agent 起步，而不是固定 5 个以上。只有 Judge 判定置信度不足、方案冲突或高风险时才扩充。

### 7.5 P4：连接、背压与取消

1. 在 Starlette lifespan 中创建共享模型客户端、HTTP 连接池、MCP session 和数据库池。
2. endpoint 只获取已存在的 graph/client，不创建新对象。
3. 全局并发不得只由 `agent_count` 决定；需要进程级信号量。
4. 对模型、工具和整体 run 分别设置超时。
5. SSE 检查客户端断开，并把取消信号传播到图运行和外部请求。
6. 网关限制请求体、并发连接、每用户运行数和速率。
7. 对高峰负载实施 backpressure，而不是无限排队造成超时雪崩。

### 7.6 P5：持久化策略

无断点需求时保持无状态最轻。需要线程记忆、人机中断、恢复和长任务时，使用生产级 Postgres checkpointer。必须设置 checkpoint 保留策略，否则消息和状态会随运行持续增长。

建议只持久化：

```text
必要业务状态
节点完成标记
小型结构化结果
外部大对象引用
恢复所需的最小消息上下文
```

不要把完整文档、二进制文件、超长工具日志和每个 token 事件写入 checkpoint。

### 7.7 P6：CPU 密集工具隔离

LangGraph 和 Starlette 的异步事件循环适合 I/O，不适合长时间占用 GIL 的 CPU 任务。代码分析、密码计算、图像处理或本地推理应采用：

```text
释放 GIL 的原生库
进程池
独立 CPU/GPU worker
按需拆分的 Go/Rust/C++ 工具服务
```

这类拆分是为了计算隔离和独立扩缩容，不是为了替换 LangGraph 控制面。

---

## 8. 迁移决策门槛

以下是建议的工程验收门槛，不是框架官方标准。

### 8.1 FastAPI → Starlette

满足以下大部分条件再迁移：

```text
接口数量少且稳定
不依赖自动 OpenAPI/复杂依赖注入
route/graph-zero-I/O p95 改善达到约 10%
空闲 RSS 或冷启动有可见改善
错误处理与校验代码仍可控
真实 Agent 指标没有退化
```

若真实任务完全由 5~30 秒 LLM 调用主导，而 Starlette 仅节省不足 1% 的端到端时间，保留 FastAPI 也可能更经济。

### 8.2 Uvicorn → Granian

仅在目标流量下达到以下条件之一时采用：

```text
大量 HTTP/2 并发流
大量长期 SSE/WebSocket 连接
p95 或每连接 RSS 改善约 15% 以上
Uvicorn 的协议层或连接处理已被证实是瓶颈
```

同时必须完成断线取消、代理转发、TLS、日志、指标和 ASGI 兼容测试。

### 8.3 单服务 → LangGraph Agent Server

出现任意关键需求时优先评估 Agent Server：

```text
运行时长超过 HTTP 请求生命周期
任务排队、取消与恢复
持久化 thread/run
Human-in-the-loop
多个 API 实例与 worker 独立扩缩容
跨服务 A2A
生产级任务状态查询
```

Agent Server 提供持久化和任务队列，但会引入 PostgreSQL、Redis 与运维复杂度，因此不属于“最少依赖”的第一阶段方案。

---

## 9. 生产部署建议

### 9.1 初始部署

```text
反向代理 / API Gateway
          │
          ▼
Starlette + Uvicorn
1 worker / container
          │
          ▼
预编译 LangGraph + 共享异步客户端
```

容器内部优先单 worker，由容器编排器水平扩容。每个 worker 都会各自持有模型客户端、图和缓存；盲目增加 worker 会线性增加内存和连接池数量。

### 9.2 安全与可靠性

必须补充：认证、授权、租户隔离、请求体限制、速率限制、运行数限制、超时、审计日志、敏感数据脱敏和工具权限白名单。Agent 工具调用应遵循最小权限，不能仅依靠 Prompt 约束。

### 9.3 观测指标

| 层级 | 指标 |
|---|---|
| HTTP | active connections、RPS、p95/p99、4xx/5xx、断开数 |
| Graph | run duration、node duration、fan-out、retry、cancel |
| Model | TTFT、tokens/s、429、token、cost |
| Tool | duration、timeout、cache hit、error |
| State | checkpoint bytes、state bytes、thread growth |
| Quality | task pass rate、judge score、人工复核率 |

---

## 10. 分阶段实施计划

### 阶段 1：复现与基线

执行本工程全部测试和基准，记录 FastAPI/Uvicorn 基线，不改业务逻辑。

### 阶段 2：薄 API 层迁移

将入口切到 Starlette + msgspec，保持同一个 LangGraph、同一模型和同一部署资源，做 A/B 对照。

### 阶段 3：图级高收益优化

并行独立节点、移除 Agent 间 HTTP、自适应 Agent 数量、上下文压缩、连接池复用和早停。这一阶段预计比框架替换更值得投入。

### 阶段 4：真实模型和成本评测

建立代表性任务集，比较质量、p95、token 成本和失败率，计算成本调整后效用 $U$。

### 阶段 5：持久化与分布式

只有业务明确需要长期任务、恢复、队列和水平扩缩容时，增加 Postgres checkpointer 或迁移 Agent Server。

---

## 11. 最终建议

当前项目的最佳起点不是寻找一个“理论 RPS 最高”的 Python Web 框架，而是构建一个薄、可测、不会干扰 LangGraph 的控制面：

```text
Starlette + Uvicorn + msgspec
        +
单个预编译 LangGraph
        +
进程内 subgraph / Send 并行
        +
自适应 Agent 数量
        +
最小状态与共享异步客户端
```

Granian是有条件的第二方案；Agent Server 是进入持久化和分布式阶段后的平台方案。真正的收益优先级应为：

```text
Agent 拓扑与调用次数
    > 并行和异步 I/O
    > 上下文/token 优化
    > 状态与序列化
    > HTTP 框架替换
```

因此，先用随附工程测量 FastAPI/Starlette/Granian 的边界开销，再把主要工程资源投入图结构、模型路由、早停和状态压缩，才能同时获得速度、成本和质量收益。

---

## 12. 官方资料

- Starlette: https://www.starlette.io/
- FastAPI benchmarks: https://fastapi.tiangolo.com/benchmarks/
- LangGraph subgraphs: https://docs.langchain.com/oss/python/langgraph/use-subgraphs
- LangGraph workflows and `Send`: https://docs.langchain.com/oss/python/langgraph/workflows-agents
- LangGraph streaming: https://docs.langchain.com/oss/python/langgraph/streaming
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- LangGraph Agent Server: https://docs.langchain.com/langsmith/agent-server
- Granian: https://github.com/emmett-framework/granian
- LangServe repository/deprecation notice: https://github.com/langchain-ai/langserve
