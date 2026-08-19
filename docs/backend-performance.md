# 后端性能设计与基准

本轮优化针对两份分析报告指出的高收益路径，保留 FastAPI、LangGraph、Pydantic 和现有
API 契约。当前瓶颈主要来自模型调用数量、运行调度和状态读取，并不来自 FastAPI 路由层，
因此没有为了框架体积而改写为 Starlette，也没有在缺少真实负载数据时引入 Granian。

## 已实施的优化

| 路径 | 实现 | 收益目标 |
| --- | --- | --- |
| 审查拓扑 | `adaptive` 默认让普通低/中风险产物执行一次综合审查，高/关键风险及研究/决策任务保留事实、逻辑、约束三维审查 | 默认两产物场景的审查调用从 6 次降至 2 次 |
| LangGraph 分发 | Worker/Reviewer 的 `Send` 只携带节点必需字段 | 避免并行分支重复复制完整工件和模型调用历史 |
| 运行调度 | `run_max_concurrency` 对完整 Graph Run 做背压 | 防止多个请求绕过节点级信号量压垮 Provider |
| Provider 调度 | 每个 Provider 独立 `max_concurrency` 信号量，共享 HTTP 连接池 | 隔离不同模型端点并限制排队压力 |
| SSE | 节点完成时发布合并信号，消费者按需读取快照；空闲时仅发送 keepalive | 移除每个连接每 250 ms 的完整 Checkpoint 轮询 |
| Tool | 对声明为 `idempotent` 的工具启用有界、运行级缓存 | 避免同一运行中相同只读调用重复执行 |
| Trace | `sample_rate` 按 `run_id` 确定性采样，生产 Profile 默认 10% | 降低高并发时的日志 I/O |
| API 边界 | ASGI 原生请求体上限与客户端令牌桶 | 让已有配置真正生效，避免无界输入和突发请求 |

默认 Mock 工作流包含 1 次 Plan、2 次 Worker、Review、1 次 Judge 和 1 次 Synthesize。
自适应审查把低风险场景的总模型调用从 11 次降至 7 次，减少 **36.4%**；其中审查调用
减少 **66.7%**。这是确定性调用计数，不是对真实模型质量或真实网络延迟的声明。

## 配置

关键参数位于 `config/runtime.yaml` 和 `config/models.yaml`：

```yaml
run_max_concurrency: 4
review_strategy: adaptive
deep_review_risk_levels: [high, critical]
deep_review_task_types: [research, decision]
tool_cache_max_entries: 256
sse_keepalive_seconds: 15
```

```yaml
providers:
  compatible:
    max_concurrency: 8
```

`review_strategy` 可选：

- `adaptive`：普通低/中风险综合审查，高/关键风险及研究/决策任务三维审查；
- `comprehensive`：所有启用交叉复核的任务只做一次综合审查；
- `three_dimensional`：所有启用交叉复核的任务均做事实、逻辑、约束三维审查。

关闭任务的 `cross_review` 后仍保留一次约束审查，避免完全绕过质量门禁。

## 可重复基准

在项目根目录执行：

```bash
uv run python scripts/benchmark_backend.py --runs 50 --concurrency 4
```

也可保存 JSON：

```bash
uv run python scripts/benchmark_backend.py \
  --runs 100 \
  --concurrency 8 \
  --output var/benchmarks/backend.json
```

输出包括吞吐、p50/p95/p99、平均模型调用、平均审查调用、节点调用分布和进程最大 RSS。
此基准固定使用 Mock Provider，用于比较框架开销和调用拓扑；`quality_claim` 永远为
`false`。接入真实 Provider 后应另建固定任务集，记录 Token、成本、结构化输出失败率、
限流重试和质量人工评分，不能把 Mock 数据外推为生产性能。

2026-08-19 在 WSL2、Python 3.12.3、AMD Ryzen 5 5600（分配 12 个逻辑 CPU）上，以
50 次运行、客户端并发 4 完成一次样例测量：50/50 成功，84.24 runs/s，p50 45.03 ms、
p95 65.62 ms、p99 76.15 ms，平均 7 次模型调用、2 次审查调用，进程最大 RSS
113.69 MiB。该结果只作为本次源码状态的 Mock 基准，不承诺其他机器或真实 Provider
具有相同表现。

固定 10 条 Mock 合同集与仓库内 `framework-002` 基线相比，仍为 10/10 通过；平均模型
调用由 10.8 降至 8.8，输入/输出 Token 代理分别由 6912/13824 降至 5632/11264，均减少
约 **18.5%**。样例完整延迟由平均 18.8 ms、p95 23 ms 变为 13.7 ms、p95 16 ms，
但毫秒级 Mock 测量易受机器噪声影响，调用数与 Token 代理才是这一对比中的确定性收益。

## 保留的边界

- SQLite、进程内信号量和 SSE 订阅表仍限定为单实例部署。
- SSE 当前推送完整运行快照，但只在状态更新时读取；跨实例回放需要持久事件表或消息总线。
- Tool 缓存限定在单次运行内，运行终止或取消后清理，不跨用户复用结果。
- Provider 等待时间计入调用超时，过载会快速失败而不是无限排队。
- 是否替换 ASGI Server、序列化库或 Web 框架，应以真实 p95/p99 与 CPU profile 为依据。
