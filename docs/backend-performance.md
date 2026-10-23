# 后端性能设计与基准

## 2026-10-22：读取路径与异常稳定性

本轮沿用现有架构，对设置读取、运行历史和模型网关做定向优化；同时用故障注入确认并修复持久化竞态。未改变 Fact / Intent / Hint 协议、执行图和费用策略，也未增加生产缓存。

- 配置解析优先使用 PyYAML 的 `CSafeLoader`（不可用时回退到 `SafeLoader`）。设置 API 的 YAML、Prompt 文件读取及配置合并移到线程，避免阻塞请求事件循环；每次仍读取当前文件，不引入过期缓存。
- SQLite 自动补建 `created_at DESC, run_id DESC` 组合索引，历史页查询不再使用临时排序树；现有数据库在初始化时自动补齐，无需数据迁移操作。
- Provider 保存和删除在同一应用实例内串行执行「读取 → 整体校验 → 写入」，避免同时为同一 Agent 绑定两个不同服务后使配置不可加载。多实例配置仍遵守原有的集中部署限制。
- 设置先写入独立的 0600 临时文件、flush / fsync、原子替换，成功后才发布内存状态；失败清理临时文件并保留旧值。取消请求时等底层写入完成后再释放写锁，并保证内存与文件一致。SQLite RunStore、Provider Store 和黑板共用这套取消处理；不改变数据库事务与 fencing 规则。
- `ModelRequest.timeout_seconds` 现在限制整次逻辑调用，包括排队、重试等待和候选 Provider 切换；Provider 自身的超时仍限制每次尝试。超时与主动取消都会释放并发槽。重试带随机退避并遵守 `Retry-After`（秒数或 HTTP 日期）；等待将超过剩余时限时尝试其他允许的候选。健康检查单个 Provider 最多等待 2 秒（或其更短的配置时限）。

### 读取基准

同一环境、相同命令，50,000 条 SQLite 历史记录、40 个样本、设置 API 客户端并发 4。API 使用内存 ASGI 传输，数据库和配置位于独立临时目录，未访问真实模型。原始结果见 [对照 JSON](benchmarks/backend-reads-2026-10-22.json)。

| 路径 | 优化前 p95 | 优化后 p95 | 降幅 |
| --- | ---: | ---: | ---: |
| 配置加载 | 16.62 ms | 3.87 ms | 76.7% |
| 历史首页（50 条） | 31.52 ms | 0.71 ms | 97.8% |
| 设置 API（Provider / 应用设置混合读取） | 110.77 ms | 52.23 ms | 52.8% |
| 事件循环延迟（5 ms 采样） | 130.61 ms | 4.41 ms | 96.6% |

```bash
.venv/bin/python scripts/benchmark_backend_reads.py --records 50000 --samples 40 --concurrency 4
```

也可用 `uv run python` 执行。基准强制使用 local Profile 和临时目录，忽略部署环境的 `WORK_HARNESS_*` 覆盖；支持 `--output` 保存结果。以上是这次本地测量，不代表真实模型生成速度、生产吞吐或网络延迟。基准没有通过降低 SQLite 同步级别换取速度。

四个故障回归在修改前均失败：设置写盘失败却提前发布内存值、取消保存提前释放锁、并发 Provider 配置落盘冲突、逻辑调用超时重复叠加。修改后已通过；另覆盖 fsync / rename 失败、重复取消、SQLite 提交顺序、排队取消、限流退避和挂起的健康检查。本轮全量后端 **850 项全部通过**（135.22 秒），包括独立 PostgreSQL 16 的队列、黑板和备份恢复；Ruff、差异检查及发布元数据检查通过。临时测试数据库已关闭并移除。本轮没有重新测量真实 Provider 推理时延，也没有重做 Windows / Docker 发布产物验收。

## 历史优化背景（2026-08-19）

本轮优化针对两份分析报告指出的高收益路径，保留 FastAPI、LangGraph、Pydantic 和现有
API 契约。当前瓶颈主要来自模型调用数量、运行调度和状态读取，并不来自 FastAPI 路由层，
因此没有为了框架体积而改写为 Starlette，也没有在缺少真实负载数据时引入 Granian。

## 已实施的优化

| 路径 | 实现 | 收益目标 |
| --- | --- | --- |
| 审查拓扑 | `adaptive` 默认让普通低/中风险产物执行一次综合审查，高/关键风险及研究/决策任务保留事实、逻辑、约束三维审查 | 默认两产物场景的审查调用从 6 次降至 2 次 |
| LangGraph 分发 | Worker/Reviewer 的 `Send` 只携带节点必需字段 | 避免并行分支重复复制完整工件和模型调用历史 |
| 运行调度 | `run_max_concurrency` 限制执行数，`run_queue_capacity` 限制等待数 | 防止请求被无界创建为后台 Task 并压垮进程与 Provider |
| 多实例队列 | PostgreSQL 原子提交、全局容量锁、`SKIP LOCKED` 领取、租约/fencing 和按 Graph revision 路由 | API 与执行解耦，多个 Worker 无锁队头串行且可接管崩溃任务 |
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
run_queue_capacity: 8
run_queue_retry_after_seconds: 5
review_strategy: adaptive
deep_review_risk_levels: [high, critical]
deep_review_task_types: [research, decision]
tool_cache_max_entries: 256
sse_keepalive_seconds: 15
execution:
  role: embedded  # distributed profile uses api or worker
  poll_interval_seconds: 0.5
  job_max_attempts: 3
  retry_base_seconds: 2
  retry_max_seconds: 60
```

```yaml
providers:
  compatible:
    max_concurrency: 8
```

单进程最多接纳 `run_max_concurrency + run_queue_capacity` 个未完成运行。容量耗尽时，
`POST /v1/runs` 和人工恢复接口返回 `429` 与 `Retry-After`；已成功保留的幂等请求即使此时
重试，仍返回原 `run_id`，不会被误报为过载。`/health` 的 `scheduler` 同时公开活动数、
等待数、剩余接纳槽、累计接纳/拒绝/恢复数和本次恢复扫描观察到的积压。累计值属于当前
进程生命周期，不是跨重启的业务计数。

分布式模式中，`run_queue_capacity` 是 PostgreSQL 内所有 API 共享的 available Job 上限；Worker
总执行容量是所有存活 Worker 的 `run_max_concurrency` 之和。`/health.scheduler` 额外公开共享队列
各状态数量、不兼容 active Job、最老等待时间、存活 Worker 数/容量/活动数，以及配置摘要是否
一致。不兼容 active Job 非零会使 readiness 失败，避免滚动升级后静默搁置旧任务。队列统计来自
数据库，进程内累计接纳/拒绝计数仍只代表当前 API 实例。

领取事务用 `SKIP LOCKED` 选择同一 `graph_revision` 的候选行。已经耗尽尝试次数的过期租约只
获得一次 Checkpoint 对账权：终态直接确认，活动态才进入 dead，避免确认窗口崩溃把成功结果误判
为失败。短轮询间隔只影响空队列领取延迟，不影响任务是否存在；任务事实始终在 PostgreSQL，
跨实例 SSE 通知丢失时还会通过 keepalive 周期读取收敛。

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
`false`。接入真实 Provider 后应另建固定任务集，记录 Token、结构化输出失败率、
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

- SQLite、进程内接纳队列和本地 SSE 订阅表仍限定为 embedded 单实例；多实例必须同时启用
  PostgreSQL RunStore、可靠队列和 PostgreSQL Checkpointer。
- PostgreSQL 队列只承诺 at-least-once 调度；fencing 阻止旧 Worker 写队列终态，但不能撤销已经
  发出的模型请求或外部 Tool 副作用。
- SSE 当前推送完整运行快照，并用 PostgreSQL NOTIFY 加速跨实例唤醒；断线事件回放仍需要持久
  event 表，NOTIFY 不是审计日志。
- Tool 缓存限定在单次运行内，运行终止或取消后清理，不跨用户复用结果。
- Provider 等待时间计入整次逻辑调用超时，过载会在时限内失败，不无限排队或因重试倍增超时。
- Mock 基准没有测量 PostgreSQL submit/claim p95、队列延迟或连接池上限；正式容量规划必须在
  目标数据库、真实 Provider 和预期 Worker 数量下另做负载测试。
- 是否替换 ASGI Server、序列化库或 Web 框架，应以真实 p95/p99 与 CPU profile 为依据。
