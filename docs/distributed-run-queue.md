# 多实例可靠运行队列设计

状态：核心实现与真实 PostgreSQL 功能验收完成；目标环境容量压测待部署方执行

适用版本：当前开发分支，计划随 `0.3.x` 发布

范围：Work Harness 的完整 Graph Run，不替代 LangGraph 节点内的 Worker/Reviewer 并发

## 1. 设计目标

多实例模式必须同时满足以下条件：

1. API 与执行 Worker 可以独立扩缩容，API 进程不创建 Graph 后台 Task；
2. API 返回 `202` 前，Run 索引与待执行 Job 已在同一 PostgreSQL 事务中持久化；
3. 多个 Worker 竞争任务时，一个租约周期内只能有一个有效领取者；
4. Worker 进程退出、失联或节点重启后，租约到期的 Job 可以自动重试；
5. 有界重试耗尽后进入死信状态，不进行无限重放；
6. 幂等提交、人工恢复、取消、超时、退避和死信都有明确状态转换；
7. API、Worker 和 LangGraph Checkpoint 共享同一个 PostgreSQL 故障域；
8. SSE 即使连接到非执行实例，也能观察另一个实例写入的状态；
9. 保留当前 Memory/SQLite 单机模式，桌面 EXE 不被强制引入 PostgreSQL；
10. 对外只承诺 **至少一次调度**。框架通过租约令牌和幂等 Tool 合同降低重复副作用，
    不虚假承诺分布式环境中的端到端 exactly-once。

## 2. 非目标

- 不使用 Redis 保存唯一任务事实；Redis 可在未来作为通知加速层，但不能决定任务是否存在；
- 不允许 API 在数据库提交前把任务放进内存队列；
- 不依赖同一容器内增加多个 Uvicorn Worker 来获得可靠性；
- 不把 LLM 调用本身视为可事务回滚操作；
- 不在第一版实现跨区域主动—主动或自动分片。

## 3. 部署拓扑

```text
                      ┌─────────────────────┐
客户端 ──HTTPS──► LB ─┤ API × N             │
                      │ 认证 / 校验 / 查询   │
                      └──────────┬──────────┘
                                 │ 原子 submit / query
                                 ▼
                    ┌──────────────────────────┐
                    │ PostgreSQL               │
                    │ run_index                │
                    │ run_jobs                 │
                    │ worker_heartbeats        │
                    │ LangGraph checkpoints    │
                    └──────────┬───────────────┘
                               │ SKIP LOCKED claim
                    ┌──────────┴──────────┐
                    ▼                     ▼
              Worker × M            Worker × M
              Graph + Provider      Graph + Provider
```

每个容器只承担一种角色：

- `api`：生产者和查询端，不执行 Graph；
- `worker`：消费者，不暴露业务 API；
- `migrate`：一次性建表/迁移，不持续运行；
- `embedded`：仅用于本地 SQLite/Memory 兼容模式。

容器内部保持单 Python 进程。横向容量通过增加容器实现，避免每个进程重复创建不可控的模型
连接池。

分布式入口支持显式 `engine: "blackboard"`，并要求请求包含黑板目标验收条件；默认引擎
仍为 `legacy`。提交时固定 Run 的引擎和执行版本，Worker 按该版本恢复；共享数据库还
保存黑板对象、账本、附件、回执和持久事件。人工输入使用黑板类型化输入接口，和恢复 Job
原子保存。完成及取消由黑板终态驱动对账；丢失通知后仍可轮询恢复或按事件游标补读。
部署前执行 `work-harness migrate`，安装队列 Schema 5、黑板 Schema 3 和 Checkpoint 表。
租约与故障恢复细节见[持久化说明](persistence.md#postgresql-实现)。

## 4. 共享事实源

### 4.1 为什么必须保存 Job 载荷

当前单机实现的 `RunStore` 只保存请求指纹，不保存原始任务；初始 Graph 输入仅存在于本地
`asyncio.Task`。如果 API 在首次 Checkpoint 前退出，任务无法安全恢复。分布式模式必须把经过
Pydantic 校验、排除原始幂等键后的请求 JSON 写入 `run_jobs.payload`，并与 Run 索引一起提交。

### 4.2 PostgreSQL 表

`run_index` 保存面向 API 的当前状态：

```text
run_id                    text primary key
status                    pending | running | waiting_human | succeeded |
                          partial | failed | cancelled
idempotency_key_hash      char(64) unique nulls distinct
request_fingerprint       char(64)
cancel_requested_at       timestamptz null
created_at                timestamptz
updated_at                timestamptz
```

`run_jobs` 保存每一次可执行动作，人工恢复会创建同一 `run_id` 的新 Job：

```text
job_id                    uuid primary key
run_id                    text references run_index(run_id)
kind                      start | resume
status                    available | leased | completed | cancelled | dead
payload                   jsonb
graph_revision            char(64)
priority                  smallint default 0
available_at              timestamptz
lease_owner               text null
lease_token               uuid null
lease_expires_at          timestamptz null
attempt_count             integer default 0
max_attempts              integer
last_error_code           text null
last_error_message        text null
created_at                timestamptz
updated_at                timestamptz
completed_at              timestamptz null
```

约束与索引：

```sql
create unique index ux_run_jobs_active_run
on run_jobs(run_id)
where status in ('available', 'leased');

create index ix_run_jobs_claim
on run_jobs(priority desc, available_at, created_at)
where status in ('available', 'leased');
```

`worker_heartbeats` 只用于可观测性和 readiness，不参与 Job 正确性：

```text
worker_id                 text primary key
instance_id               text
capacity                  integer
active_jobs               integer
started_at                timestamptz
last_seen_at              timestamptz
build_version             text
config_fingerprint        char(64)
```

即使该表短暂写入失败，Job 租约仍是执行权的事实来源。

## 5. Job 状态机

```text
                         Worker claim
available ─────────────────────────────────► leased
    ▲                                            │
    │ retryable failure / lease expired          │ success or graph terminal
    └────────────────────────────────────────────┤
                                                 ▼
                                             completed

leased/available ── cancel request ─────────► cancelled
leased ── attempts exhausted ────────────────► dead
```

规则：

- `available`：`available_at <= database_now()` 时可以领取；
- `leased`：只有匹配 `lease_token` 的 Worker 可以 heartbeat、ack 或 nack；
- 租约到期的 `leased` Job 可由另一个 Worker 原子接管，并生成新 token；达到最大尝试数时该
  token 只能读取 Checkpoint 对账，不能再次推进 Graph；
- `completed/cancelled/dead` 均为 Job 终态；
- Graph 进入 `waiting_human` 时，当前 Job 为 `completed`，后续批准创建 `resume` Job；
- `dead` 同步把 Run 标记为 `failed`，并附加稳定错误码 `queue_attempts_exhausted`；
- Run 取消会在事务中标记 `cancel_requested_at` 并取消未完成 Job。

## 6. 核心事务

### 6.1 新任务提交

单个数据库事务内执行：

1. 按 `idempotency_key_hash` 查找现有 Run；
2. 存在时比较 `request_fingerprint`：一致返回原 `run_id`，不一致返回 `409`；
3. 获取队列接纳 advisory transaction lock；
4. 统计 `available` Job；达到 `run_queue_capacity` 时回滚并返回 `429 + Retry-After`；
5. 插入 `run_index(PENDING)`；
6. 插入 `run_jobs(START, AVAILABLE, payload)`；
7. 提交后 API 才返回 `202`。

容量只限制等待队列，不把已经由 Worker 领取的 Job 计算在内。Worker 总执行容量等于所有存活
Worker 的 `run_max_concurrency` 之和。分布式 Profile 中 `run_queue_capacity` 必须大于零。

### 6.2 原子领取

领取必须使用数据库时间和行锁：

```sql
with candidate as (
  select job_id
  from run_jobs
  where (
      status = 'available' and available_at <= clock_timestamp()
    ) or (
      status = 'leased' and lease_expires_at <= clock_timestamp()
    )
  order by priority desc, available_at asc, created_at asc
  for update skip locked
  limit 1
)
update run_jobs as job
set status = 'leased',
    lease_owner = :worker_id,
    lease_token = :new_token,
    lease_expires_at = clock_timestamp() + :lease_interval,
    attempt_count = attempt_count + 1,
    updated_at = clock_timestamp()
from candidate
where job.job_id = candidate.job_id
returning job.*;
```

同一事务把对应 Run 更新为 `RUNNING`。`SKIP LOCKED` 让多个 Worker 不必串行等待同一候选行。

### 6.3 心跳与 fencing token

心跳条件必须同时匹配：

```text
job_id + status=leased + lease_owner + lease_token + run 未请求取消
```

旧 Worker 的 token 在重新领取后立即失效。heartbeat 返回 false 时，Worker 取消本地 Graph
调用，不再 ack。token 能阻止旧 Worker提交队列终态，但不能让已经发出的外部 HTTP/Tool
副作用回滚，因此带副作用 Tool 必须接受 `(run_id, tool_call_id)` 幂等键。

### 6.4 成功确认

Graph 达到 `succeeded/partial/failed/waiting_human` 任一业务终态后，Worker 在事务中：

1. 使用 token 条件把 Job 更新为 `completed`；
2. 把 Checkpoint 中的 Run 状态同步到 `run_index`；
3. 清空租约字段并写 `completed_at`。

如果 token 已失效，ack 返回 false，旧 Worker不得覆盖新领取者的队列状态。

### 6.5 失败重试与死信

仅基础设施错误进入队列重试：数据库瞬断、Worker 进程终止、无法读取 Checkpoint 等。模型业务
失败已经由 Graph 的 Judge/错误状态处理，不重复整个 Run。

退避：

```text
delay = min(retry_max_seconds,
            retry_base_seconds * 2 ** (attempt_count - 1))
        + deterministic_jitter(job_id, attempt_count)
```

`attempt_count < max_attempts` 时 nack 回到 `available` 并设置 `available_at`。最后一次执行如果在
Graph 写入 Checkpoint 后、Job ack 前崩溃，接管者先读取 Checkpoint：业务终态直接完成 Job；仍为
活动态或没有 Checkpoint 才转 `dead`。管理 API 支持显式重放死信，但会创建新 Job 并保留旧 Job
审计记录，不把旧 attempt 归零。

## 7. 人工恢复、取消与超时

### 人工恢复

- 只允许 `run_index.status = waiting_human`；
- 在事务中确认没有 active Job；
- 插入 `kind=resume`、payload 为 `{approved, note}` 的 Job；
- Run 与 RESUME Job 在同一事务中改为 `pending/available`，使 SSE 保持订阅并明确表示恢复已排队；
- Worker 领取后改为 `running` 并调用 `Command(resume=payload)`。

### 取消

- API 事务写 `cancel_requested_at`，取消 `available` Job，并使 `leased` Job 的后续 heartbeat
  失败；
- 当前 Worker最迟在一个 heartbeat 周期内取消 Graph；
- Worker 或取消 API 把 Checkpoint 和 Run 索引同步为 `cancelled`；
- 取消是幂等操作，对终态 Run 重复调用不产生新 Job。

### 超时

- `run_timeout_seconds` 仍由执行 Worker 计时；
- 超时属于业务终态 `failed/run_timeout`，ack 当前 Job，不进行整个 Run 队列重试；
- 单个 Provider/Tool 的重试继续在各自边界内完成，不能绕过 Run deadline。

## 8. Checkpoint 与跨实例事件

分布式 Profile 强制以下组合，禁止混用：

```text
run_store.backend = postgres
checkpoint.backend = postgres
execution_role in {api, worker}
```

`AsyncPostgresSaver` 在 `migrate` 阶段调用一次 `setup()`；运行容器使用最小 DML 权限，不在每次
启动时执行 DDL。Checkpoint 反序列化启用严格 msgpack 允许列表。

SSE 第一版采用“通知加速 + 周期校准”策略：

1. Worker 提交状态后执行 `pg_notify('work_harness_run_events', run_id)`；
2. API 实例收到通知后唤醒本地订阅者；
3. 为防通知丢失，SSE 每个 keepalive 周期都重新读取 Run/Checkpoint；
4. 客户端事件 ID 只是单连接序号，不宣称持久事件回放。

未来需要断线续传时增加 append-only `run_events` 表，不能依赖 PostgreSQL NOTIFY 作为事件存档。

## 9. Worker 生命周期

Worker 启动顺序：

1. 校验配置和 Provider 能力全等合同；
2. 连接 PostgreSQL，确认 schema version；
3. 连接共享 Checkpointer；
4. 注册 heartbeat；
5. 启动不超过 `run_max_concurrency` 个消费槽；
6. 空队列时使用短轮询并带抖动，后续可用 NOTIFY 唤醒降低空转。

收到 `SIGTERM` 后：

1. 停止领取新 Job；
2. readiness 立即变为 false；
3. 在 `stop_grace_period` 内等待活动 Graph；
4. 到期后取消本地调用并 nack；若数据库不可用则让租约自然到期；
5. 关闭 Provider 连接池和数据库连接。

## 10. 健康检查与指标

API readiness 同时要求：

- PostgreSQL 可读写；
- Provider 配置合同有效；
- 至少一个 Worker 的 `last_seen_at` 在 `worker_stale_seconds` 内；
- 不存在与当前 API `graph_revision` 不兼容的 available/leased Job；
- 数据库 schema version 与应用兼容。

建议指标：

```text
queue_available_jobs
queue_leased_jobs
queue_dead_jobs
queue_incompatible_active_jobs
queue_oldest_available_seconds
queue_claim_total
queue_retry_total
queue_lease_expired_total
queue_dead_total
queue_submit_rejected_total
workers_live
workers_capacity
workers_active_jobs
job_queue_latency_seconds
job_attempts
```

日志必须包含 `run_id/job_id/worker_id/lease_token_hash/attempt`，不得记录 payload 中的完整用户
内容、原始幂等键、Provider Key 或数据库口令。

## 11. 配置合同

目标配置：

```yaml
execution:
  role: api                 # embedded | api | worker
  poll_interval_seconds: 0.5
  worker_stale_seconds: 30
  job_max_attempts: 3
  retry_base_seconds: 2
  retry_max_seconds: 60

checkpoint:
  backend: postgres
  dsn_env: WORK_HARNESS_DATABASE_URL

run_store:
  backend: postgres
  dsn_env: WORK_HARNESS_DATABASE_URL
```

数据库 URL 只通过部署 Secret/环境注入，不写入设置页面、SQLite Profile、日志或健康响应。
当前分布式 Profile 禁止页面修改 Provider、Prompt 和业务运行参数；所有实例必须滚动装载同一
不可变配置。应用版本与有效配置共同生成 `config_fingerprint/graph_revision`，摘要不一致的
Worker 不进入 ready，也不会领取不匹配 Job。

Compose 允许通过 `WORK_HARNESS_RUN_MAX_CONCURRENCY`、`WORK_HARNESS_RUN_QUEUE_CAPACITY`、
运行/租约/心跳时间、poll/stale、最大尝试次数及 retry base/max 环境变量调整同一部署；这些值
必须一致地注入全部 API/Worker，并继续接受类型、范围和交叉约束校验。

## 12. 安全与数据库权限

- 推荐由 `migrate` 角色拥有 DDL 权限，`api` 和 `worker` 只拥有所需表的 DML 权限；随仓库提供的
  单机 Compose 为降低首次部署复杂度暂时共用 PostgreSQL 初始化角色，外部生产数据库应拆分；
- 强制 TLS，生产不允许 `sslmode=disable`；
- PostgreSQL 不映射公网端口；
- payload 最大值受 API 请求体限制，JSONB 不保存原始 API Key；
- 多租户版本必须在所有主键/索引/查询条件加入 `tenant_id`，在此之前不能宣称租户隔离；
- 目标权限模型要求死信、重放和查看错误使用独立管理权限；当前版本仍由部署级共享 API Key
  保护，因此在组织身份接入前不能宣称细粒度管理授权。

## 13. 迁移与回滚

迁移顺序：

1. 部署 PostgreSQL 和 `migrate` Job；
2. 以只读方式复制 Provider/Settings 配置，冻结页面写入；
3. 停止旧实例接纳新任务并等待活动任务结束；
4. 导入需要保留的 Run 索引；旧 SQLite Checkpoint 不自动跨后端续跑；
5. 启动 Worker，确认 heartbeat；
6. 启动 API，开放流量；
7. 保留旧卷只读备份至回滚窗口结束。

不能在同一队列中同时运行不同 Graph schema 的 Worker。滚动升级需要 `graph_revision`：旧 Job
仅由兼容 revision 消费；不兼容升级应先排空队列或提供显式状态迁移。

## 14. 验收矩阵

以下测试全部通过才允许把多实例模式标记为可部署：

| 类别 | 必须证明的行为 |
| --- | --- |
| 原子提交 | 在 Run 插入与 Job 插入之间注入故障，不出现孤立 Run 或丢失 Job |
| 幂等竞争 | 50 个并发相同幂等键只产生一个 Run/Job；不同指纹稳定返回冲突 |
| 多 Worker | 至少两个独立连接并发领取 100 个 Job，每个租约代次只被领取一次 |
| Worker 崩溃 | 领取后强制终止连接，租约到期后由另一 Worker 接管 |
| fencing | 旧 token 的 heartbeat/ack/nack 全部失败，不能覆盖新 Worker |
| 重试 | 退避时间单调、有上限；达到 max attempts 后进入 dead |
| 取消 | available 和 leased 两种状态都能最终取消，且不会再次领取 |
| 人工恢复 | waiting Run 只创建一个 resume Job，并从共享 Checkpoint 继续 |
| 容量 | 多 API 实例并发提交时，全局 available 数不超过配置上限 |
| SSE | API-A 上的连接能观察 Worker-B 的状态，通知丢失时周期校准仍收敛 |
| 优雅退出 | Worker 停止领取，活动任务完成或安全释放，进程在 grace period 内退出 |
| 迁移 | 空库升级、已有 schema 升级和版本过新拒绝启动均有测试 |
| 性能 | 目标并发下记录 submit/claim p95、队列延迟和数据库连接数，无锁队头阻塞 |

PostgreSQL 集成测试必须使用真实数据库容器，不能用 SQLite 测试结果替代 `SKIP LOCKED`、事务
隔离和连接中断行为。Memory 实现仅用于快速验证状态机合同。

当前自动化证据包括：Run/Job 插入故障回滚、50 路幂等/容量竞争、两个独立 Store 领取 100 个
Job、租约接管与旧 token 拒绝、崩溃尝试耗尽自动死信、取消与重放、LISTEN 断线重连、旧 schema
迁移与新 schema 拒绝，以及一个 API Runtime 加两个 Worker Runtime 共享 PostgreSQL Checkpoint
完成 7 个完整 Graph Run（含人工门控恢复），并验证尝试耗尽时优先对账终态 Checkpoint。完整后端套件共 116 项；
CI 通过 PostgreSQL 17 service 运行集成组。

第 14 节中的功能与故障语义已经进入自动化基线。性能行是部署环境验收项：仓库的 Mock 基准不能
替代目标 PostgreSQL、真实 Provider、预期 API/Worker 数量下的 submit/claim p95、队列延迟、
连接数与资源曲线，因此上线生产流量前仍必须单独记录。

## 15. 已接受的关键决策

1. PostgreSQL 是队列和 Checkpoint 的唯一共享事实源；
2. API/Worker 分离，默认单机模式继续使用 embedded；
3. 使用数据库行租约、`SKIP LOCKED` 和 fencing token，不引入 Redis 双写；
4. 调度语义为 at-least-once，副作用通过 Tool 幂等合同治理；
5. 死信是显式终态且可审计，重放创建新 Job；
6. NOTIFY 只用于降低事件延迟，周期读取保证最终可见；
7. 迁移由独立命令执行，运行角色不默认持有 DDL 权限；
8. 任何多实例发布声明都必须以第 14 节的真实 PostgreSQL 测试为证据。
