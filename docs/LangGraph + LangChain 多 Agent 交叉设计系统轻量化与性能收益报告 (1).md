# LangGraph + LangChain 多 Agent 交叉设计系统轻量化与性能收益报告

**报告日期：2026 年 8 月 19 日**

## 一、最终结论

针对以 LangGraph 和 LangChain 为核心的多 Agent 交叉生成、交叉评审、裁决与汇总系统，推荐的默认架构是：

```text
Starlette + Uvicorn + msgspec
            │
            ▼
     单个预编译 LangGraph
            │
    ┌───────┼───────┐
    ▼       ▼       ▼
 Agent A  Agent B  Agent C
    └───────┼───────┘
            ▼
    Cross Review / Judge
            │
            ▼
       Synthesizer
```

具体选择如下：

| 层级 | 默认方案 | 适用理由 |
|---|---|---|
| API 框架 | **Starlette** | 接口少、控制直接、去掉不必要的自动依赖注入与响应模型处理 |
| ASGI Server | **Uvicorn** | 生态成熟、诊断方便，Agent 请求中协议层通常不是主要瓶颈 |
| API 数据校验 | **msgspec** | 仅在 HTTP 输入输出边界使用，减少 JSON 编解码和校验负担 |
| Agent 编排 | **单个预编译 LangGraph** | 所有请求复用，不在 endpoint 内重新构图 |
| Agent 组织 | **进程内 node/subgraph** | 避免 Agent 之间额外的 HTTP、JSON、鉴权与超时开销 |
| 动态并发 | **LangGraph `Send`** | 根据任务动态生成 reviewer，并行扇出与汇合 |
| 内部状态 | **TypedDict/dataclass** | 避免每个图步骤进行复杂递归模型校验 |
| 流式输出 | **SSE + `updates`** | 只发送状态增量，避免反复序列化整个图状态 |
| 高连接候选 | **Granian** | 仅在 HTTP/2、大量 SSE/WebSocket 场景压测证明有收益后采用 |
| 分布式阶段 | **LangGraph Agent Server** | 用于队列、持久化 run、取消、恢复、A2A 和独立扩缩容 |

FastAPI 本身建立在 Starlette 之上，并增加 Pydantic 校验、响应序列化、依赖注入和 OpenAPI 等能力。因此，Starlette 的优势主要来自执行路径更薄，而不是底层网络能力完全不同。对于只有少量 Agent 接口的内部服务，这些额外能力可能没有被充分使用。

## 二、收益优先级

本项目的优化收益优先级应当是：

```text
Agent 拓扑与模型调用次数
        >
独立节点并行化
        >
减少重复上下文和 token
        >
消除 Agent 间 HTTP 调用
        >
状态大小和序列化优化
        >
FastAPI → Starlette
        >
Uvicorn → Granian
```

更换 FastAPI 主要影响：

$$
T_{api}
$$

完整任务耗时可以拆分为：

$$
T_{total}
=
T_{api}
+
T_{queue}
+
T_{graph}
+
T_{model}
+
T_{tool}
+
T_{persist}
+
T_{retry}
$$

假设 Web 层减少了 2 ms，但完整模型任务需要 4 s，则端到端收益只有：

$$
\frac{2}{4000}=0.05\%
$$

相反，3 个各需要 1 s 且互相独立的 Agent，由串行改成并行，理论耗时可以由约 3 s 降到约 1 s。

因此，本次工程同时提供：

- FastAPI/Uvicorn 基线；
- Starlette/Uvicorn 优化版；
- Starlette/Granian 候选版；
- LangGraph 串行图；
- LangGraph `Send` 并行图；
- SSE 首事件测试；
- 冷启动、RSS、p50、p95、p99 和吞吐统计。

LangGraph 官方将 subgraph 用于多 Agent 系统和模块复用，并支持将共享状态的已编译子图直接加入父图；`Send` 则可以动态创建 worker，每个 worker 使用独立状态，最后通过 reducer 汇合结果。

## 三、多 Agent 交叉设计的推荐拓扑

### 1. 不推荐的结构

```text
Controller
   ├─ HTTP → Agent A
   ├─ HTTP → Agent B
   └─ HTTP → Agent C
```

同一业务域中的 Agent 若全部属于 Python/LangGraph 系统，这种结构会产生：

```text
重复 JSON 编解码
连接池等待
重复鉴权
HTTP 超时与重试
服务发现
错误码转换
重复 tracing
更多容器和进程
```

除非 Agent 需要独立 GPU、独立安全域、独立发布或独立扩缩容，否则不应拆成多个 HTTP 微服务。

### 2. 推荐结构

```text
                       ┌→ Designer A ─┐
Input → Planner → Send ├→ Designer B ─┼→ Review Aggregator
                       └→ Designer C ─┘          │
                                                ▼
                                         Judge/Synthesizer
                                                │
                                              Output
```

主图在进程启动时编译一次：

```python
GRAPH = build_graph().compile()
```

请求中只调用：

```python
result = await GRAPH.ainvoke(inputs, config=config)
```

禁止在请求内重复执行：

```python
graph = build_graph().compile()
```

### 3. 交叉评审复杂度

假设存在 $n$ 个候选 Agent：

| 评审方式 | 额外评审调用数 | 评价 |
|---|---:|---|
| 全连接互评 | $n(n-1)$ | 信息密度高，但成本呈二次增长 |
| 环形互评 | $n$ | 成本线性，适合作为默认轻量方案 |
| 每个候选随机 $k$ 个评审者 | $nk$ | 可以控制质量与成本 |
| 单个中央 Judge | $1$ | 最省调用，但存在单点评判偏差 |
| 自适应升级 | 随任务难度变化 | 综合收益最高 |

不建议默认使用全连接互评。推荐：

```text
2 个独立候选
       │
       ▼
轻量 Judge 判断争议度
       │
   ┌───┴───────────────┐
   │低争议              │高争议
   ▼                    ▼
直接合成          增加 1~2 个候选
                         │
                    环形或局部互评
                         │
                       合成
```

这种结构使简单任务不必固定启动大量 Agent，而困难任务仍能获得更深的交叉评审。

## 四、Starlette、Uvicorn 与 Granian 的定位

### Starlette

Starlette 已经提供：

```text
ASGI
路由
StreamingResponse
WebSocket
中间件
lifespan
后台任务
请求断线检测
```

因此足以承担 LangGraph 前面的薄 API 层。Starlette 的流式响应可以直接消费异步生成器，Request 也支持检查客户端是否断开，适合 SSE Agent 输出。

### Uvicorn

默认继续使用 Uvicorn，原因是：

```text
兼容性成熟
排障简单
部署资料多
真实 Agent 请求以模型 I/O 为主
HTTP Server 通常不是第一瓶颈
```

### Granian

Granian 基于 Rust、Hyper 和 Tokio，支持 ASGI、HTTP/1、HTTP/2、WebSocket、TLS、多 worker 和连接背压。它更适合：

```text
大量长期 SSE 连接
大量 WebSocket 会话
HTTP/2 是硬要求
吞吐与连接密度优先
协议层已被确认是瓶颈
```

Granian 官方同时指出，它并不适合需要纯 Python、复杂调试、Trio/Geven​​t 或某些尚未实现的 ASGI 扩展的项目。因此应通过目标负载测试，而不是直接根据微基准切换。

建议切换门槛：

```text
目标连接规模下，stream p95 改善约 15% 以上
或每连接 RSS 明显下降
或 Uvicorn 已经出现协议层/连接处理瓶颈
且取消、代理、TLS、日志和 ASGI 兼容测试全部通过
```

## 五、状态、序列化和流式优化

### 1. API 边界使用 msgspec

```python
class RunRequest(msgspec.Struct, forbid_unknown_fields=True):
    prompt: str
    agent_count: int = 3
    strategy: Literal["parallel", "sequential"] = "parallel"
```

### 2. LangGraph 内部使用 TypedDict/dataclass

```python
class GraphState(TypedDict, total=False):
    prompt: str
    agents: list[str]
    reviews: Annotated[list[Review], operator.add]
    final: str
```

不建议把全部 `BaseMessage`、ToolMessage 和 LangGraph 状态强制转换成 msgspec 对象。msgspec 应作为 HTTP 边界工具，而不是替换整个 LangChain 消息体系。

LangGraph 文档明确提醒，Pydantic 的递归验证在性能敏感的图中可能较慢，并建议考虑 dataclass 等状态结构。

### 3. 只返回状态增量

节点应返回：

```python
return {"reviews": [review]}
```

而不是复制整个 state：

```python
return {
    "prompt": state["prompt"],
    "agents": state["agents"],
    "reviews": state["reviews"] + [review],
    ...
}
```

### 4. 生产环境精简流模式

LangGraph 支持 `updates`、`values`、`messages`、`custom`、`checkpoints`、`tasks` 和 `debug` 等流模式。生产接口通常只应启用实际需要的模式。

建议：

```text
普通状态进度：updates
模型 token：messages
自定义业务事件：custom
故障排查：debug，仅非生产或抽样开启
```

不要默认向客户端发送完整 `values` 或 checkpoint，否则会不断重复序列化整个状态。

## 六、降低模型成本的高收益措施

| 优化方法 | 主要收益 |
|---|---|
| 默认从 2 个候选 Agent 开始 | 降低固定调用数 |
| 争议检测后再增加 Agent | 困难任务才升级 |
| 便宜模型评审，强模型最终合成 | 降低平均单价 |
| 环形互评替代全连接互评 | 从 $O(n^2)$ 降为 $O(n)$ |
| Prompt 去重与共享摘要 | 降低输入 token |
| 结构化短评审 | 降低输出 token 与解析失败 |
| 检索和工具结果缓存 | 消除重复调用 |
| 对低风险任务早停 | 降低延迟和成本 |
| 限制重试预算 | 防止异常时成本雪崩 |
| 最慢 Agent deadline | 避免整个 run 被单个长尾锁死 |

建议采用成本调整后的统一评价指标：

$$
U=
\frac{
Q\times P_{success}
}{
C_{model}+\lambda_tT_{95}+\lambda_mM_{peak}
}
$$

其中：

- $Q$：离线质量评分；
- $P_{success}$：任务成功率；
- $C_{model}$：单次模型成本；
- $T_{95}$：完整运行 p95；
- $M_{peak}$：峰值内存；
- $\lambda_t,\lambda_m$：业务权重。

最终目标不是单独获得最高 RPS，而是在质量、成本、延迟和可靠性之间获得最高综合效用。

## 七、并发、背压与取消

建议有效并发上限按照以下约束确定：

$$
C_{effective}
=
\min(
C_{provider},
C_{tool},
C_{db},
C_{worker},
C_{budget}
)
$$

不能直接令：

```python
max_concurrency = agent_count
```

生产环境需要同时存在：

```text
LangGraph max_concurrency
进程级 asyncio.Semaphore
模型供应商限流器
HTTP 连接池上限
数据库池上限
任务级 token/费用预算
```

还应分别设置：

```text
单模型调用超时
单工具调用超时
单节点 deadline
完整 run deadline
SSE 断线取消
最大重试次数
```

模型客户端、HTTP Client、MCP session 和数据库连接池应在 Starlette lifespan 中创建并复用，不应每个请求重新建立连接。

## 八、持久化与 Agent Server

无断点恢复需求时，无状态 Starlette 服务最轻。

需要以下功能时，再增加 Postgres checkpointer：

```text
thread 状态
长期记忆
Human-in-the-loop
中断与恢复
节点失败后续跑
```

内存 checkpointer 在进程重启后会丢失状态；生产场景应使用持久化 saver，并设置 checkpoint 保留策略，避免状态无限增长。

出现以下需求时，应进一步迁移到 LangGraph Agent Server：

```text
运行时间超过 HTTP 请求生命周期
后台任务队列
run 取消与恢复
持久化 thread/run
API 与 worker 独立扩缩容
跨服务 Agent 通信
A2A
生产级任务状态查询
```

Agent Server 会复用已编译图，并提供任务队列、持久化和流式运行；其生产架构通常引入 PostgreSQL 和 Redis，因此不属于“最少依赖”的第一阶段方案。

新项目不建议采用 LangServe。LangServe 已被标记为 deprecated，其代码仓库也已经归档，官方方向转向 LangGraph Platform/Agent Server。

## 九、复现实验矩阵

工程默认包含五类工作负载：

| 工作负载 | 配置 | 测量目的 |
|---|---|---|
| `health` | GET `/health` | 隔离 ASGI、路由和 JSON 响应 |
| `graph` | 3 Agent，0 ms | API + LangGraph 纯调度开销 |
| `parallel` | 3 Agent × 100 ms | 并行扇出与汇合 |
| `sequential` | 3 Agent × 100 ms | 并行收益基线 |
| `stream` | 并行 + SSE | 首事件和完整流式延迟 |

对比组合：

```text
FastAPI + Uvicorn
Starlette + Uvicorn
Starlette + Granian
```

输出指标：

```text
RPS
p50/p95/p99
SSE first-event p95
失败数
冷启动时间
空闲 RSS
峰值 RSS
并行加速比
相对 FastAPI 的吞吐和 p95 改善
```

默认执行：

```bash
cd langgraph_starlette_repro
uv sync --all-extras
uv run pytest -q
make bench
```

增加重复次数：

```bash
REPEATS=5 BASE_PORT=19080 ./bench/run_matrix.sh
```

结果自动生成到：

```text
benchmark-results/
├── environment.json
├── *.server.json
├── *.rN.<workload>.json
├── summary.md
├── summary.csv
└── summary.json
```

## 十、迁移验收标准

### FastAPI → Starlette

建议满足以下多数条件：

```text
接口数量较少且稳定
不依赖自动 OpenAPI
不依赖复杂 FastAPI Depends
health 或 graph p95 改善约 10% 以上
空闲 RSS或冷启动有明显改善
错误处理代码仍然可控
真实模型任务没有质量或可靠性退化
```

如果真实任务耗时 5～30 秒，而 Starlette 只减少不足 1% 的端到端时间，保留 FastAPI 可能更经济。

### Uvicorn → Granian

建议满足：

```text
存在大量 HTTP/2、SSE 或 WebSocket 连接
目标负载下 p95 或连接密度改善约 15% 以上
不存在所需 ASGI 扩展兼容问题
断线、取消、代理和 TLS 测试通过
```

### 单体 LangGraph → Agent Server

在出现持久化任务、队列、恢复、取消、A2A 或独立扩缩容需求后迁移，而不是项目一开始就引入 PostgreSQL、Redis 和分布式 worker。

## 十一、实施顺序

```text
阶段 1：运行复现工程，建立 FastAPI 基线
阶段 2：切换 Starlette，保持图和模型完全相同
阶段 3：并行独立 Agent，去除 Agent 间 HTTP
阶段 4：加入自适应 Agent 数量、环形评审和早停
阶段 5：压缩状态、上下文和流式事件
阶段 6：使用真实模型评估质量、成本和 p95
阶段 7：确有需求时加入 Postgres checkpoint
阶段 8：进入分布式阶段后采用 Agent Server
阶段 9：只有连接压测证明收益后切 Granian
```

## 十二、最终技术方案

```text
反向代理/API Gateway
          │
          ▼
Starlette + Uvicorn + msgspec
          │
          ▼
单个预编译 LangGraph 主图
          │
          ├─ Planner/Router
          ├─ Send 动态并行 Agent
          ├─ 环形或局部 Cross Review
          ├─ 自适应升级
          ├─ Judge
          └─ Synthesizer
```

关键原则：

```text
API 层保持薄
图只编译一次
连接池全局复用
独立 Agent 并行
内部 Agent 不走 HTTP
交叉评审避免 O(n²)
默认少 Agent，争议时升级
内部状态保持小
流式只传增量
持久化按需求引入
以质量/成本/延迟综合收益为准
```

工程中的直接依赖已经按 2026-08-19 的版本快照固定，包括 LangGraph 1.2.11、LangChain Core 1.5.6、Starlette 1.6.0、Uvicorn 0.52.4、msgspec 0.21.1、FastAPI 0.141.1 和 Granian 2.8.1。