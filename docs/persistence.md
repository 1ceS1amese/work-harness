# 持久化设计

## 目标

持久化层解决六个相互独立的问题：

1. LangGraph 执行状态可以在进程重启后恢复；
2. run_id、状态和幂等关系可以被可靠查询；
3. 前端群聊在刷新页面后可以继续展示。
4. Provider 配置、模型映射及可选的页面托管凭据可以在进程重启后恢复；
5. Agent 提示词和允许页面修改的运行策略可以持久化，并在重启后装载。
6. 多实例 API 返回后，原始任务仍可由任意兼容 Worker 领取、恢复、取消或进入死信。

这些状态不能写进同一张业务表，也不能由 `RunManager` 直接操作具体数据库。

## 分层

| 层 | 接口/实现 | 保存内容 | 默认位置 |
|---|---|---|---|
| Workflow Checkpoint | LangGraph Checkpointer | Graph State、任务、计划、产物、复核、中断点 | `var/checkpoints.sqlite` |
| Run Index | `RunStore` | run_id、状态、时间、幂等哈希、请求指纹 | `var/runs.sqlite` |
| Durable Run Queue | `ReliableRunStore` | Job payload、Graph revision、优先级、租约 token、尝试与错误 | 分布式模式的 PostgreSQL |
| Provider Profile | `ProviderProfileStore` | Adapter、协议变体、端点、凭据通道、模型映射、逐 Agent 路由及可选的凭据密文 | `var/providers.sqlite` + `var/providers.sqlite.key` |
| Application Settings | `ApplicationSettingsStore` | Agent Prompt 覆盖，以及经过白名单和范围校验的并发、检查、输出长度、超时参数 | `var/settings.json` |
| UI Conversation | `ConversationStorage` | 群聊、消息、选择项、活动会话 | 浏览器 localStorage |

RunStore 不保存原始幂等键，只保存 SHA-256 哈希。内嵌模式也不保存完整请求；分布式
`ReliableRunStore` 必须把已经过 Pydantic 校验的请求 JSON 原子写入 Job payload，否则 API 在首次
Checkpoint 前退出时任务会丢失。Job payload 不包含原始幂等键或任何 Provider 凭据。

ProviderProfileStore 支持 `environment` 和 `stored` 两个互斥凭据通道。前者只保存
`api_key_env`；后者把 API Key 使用 Fernet 认证加密后写入 `credential_ciphertext`，主密钥保存在
数据库旁的 `.key` 文件。API 列表只返回 `credential_configured` 布尔值，运行时解密值放在
Pydantic private attribute 中，不参与序列化、日志或配置比较。`options` 中的
`authorization`、`api_key`、`token`、`secret`、`password` 等旁路字段仍会被 Schema 拒绝。

ApplicationSettingsStore 不接受任意 YAML、路径或额外字段。Prompt 名称限制为内置工作阶段，
单项正文限制为 50,000 字符；运行配置只允许修改并发、复核策略、返工、超时和结果大小。
数据库后端、数据路径、安全开关和凭据不属于页面设置。保存采用同目录临时文件原子替换并将权限
收紧为 `0600`，运行时只在启动阶段读取，避免进行中的任务中途切换 Prompt 或策略。

## Agent 子图持久化

Root Graph 的 Worker/Review 节点现在是独立子图。每次 `Send` 调用继承父 Checkpointer 并
使用独立命名空间，私有模型消息、工具游标和局部用量可以在调用内恢复；子图完成后才将允许的
产物及记录合并到父图。Checkpoint 数据访问和保留策略应覆盖子图私有消息。

`WORKFLOW_REVISION` 与实际 Prompt 内容参与分布式执行指纹，父状态同时记录工作流版本。
不兼容的旧执行状态不能直接恢复，升级前需排空旧任务或使用原版本完成它们。历史结果查询
保持可用。详细时序和至少一次工具执行的边界见[多 Agent 核心设计](multi-agent-design.md)。

## ProviderProfileStore 合同

```text
initialize()                         初始化表或连接
upsert(name, profile, api_key?, clear_api_key?)
                                      原子保存 Profile，并按需保留、替换或清除密文
get(name)                            查询单个持久化 Profile
get_api_key(name)                    仅供运行时解密读取页面托管凭据
list()                               按名称稳定排序
delete(name)                         删除覆盖；不存在时明确报错
close()                              释放资源
```

启动时先加载 YAML，再按名称叠加 SQLite Profile，并重建逻辑模型候选与 Agent 路由；随后只为
选择 `stored` 通道的 Provider 解密运行时 Key。`agent_bindings.main` 展开到所有非 assignable 的
主协调角色，具体 `worker-*` 绑定只影响对应子 Agent。叠加后的完整 `AppConfig` 必须再次通过
模型白名单、能力全等、逻辑模型候选、Agent 引用和 Skill 兼容校验，否则启动失败。管理 API 的
写入不会热切换正在使用的 Provider；`PUT`/`DELETE` 返回后需重启，防止进行中的模型调用被关闭
或改路由。

`.key` 文件与 SQLite 密文必须作为一组备份和恢复。分离密钥只防止数据库文件单独泄露时直接
暴露明文，不抵御数据卷、运行中进程或完整备份被同时读取。

## RunStore 合同

```text
initialize()                         初始化表或连接
reserve(record)                      原子创建；幂等键已存在时返回原记录
get(run_id)                          查询单条运行索引
get_by_idempotency_key_hash(hash)    按哈希查询已保留运行，不读取或保存原始幂等键
update_status(run_id, status)        同步状态与更新时间
list(limit, offset)                  按创建时间倒序分页
claim(run_id, owner, ttl)            原子获取或续接过期运行的唯一执行权
heartbeat(run_id, owner, ttl)        当前 owner 延长租约
release(run_id, owner)               释放当前 owner 的租约
list_recoverable(limit)              扫描无租约或租约过期的未完成运行
close()                              释放资源
```

Memory、SQLite 和 PostgreSQL 的索引部分遵循同一合同。`ReliableRunStore` 在此基础上增加
`submit/enqueue_resume/claim_job/heartbeat_job/complete_job/retry_job/cancel_jobs`、死信查询/重放、
Worker 心跳和跨实例通知；Memory 用于快速状态机测试，PostgreSQL 用于真实多实例运行。SQLite
不实现可靠共享队列。

## 内嵌模式保存时序

```text
POST /v1/runs
  → 规范化请求并计算 request_fingerprint
  → 对 idempotency_key 计算哈希
  → 检查进程内接纳容量
     → 队列已满但幂等记录存在：校验请求后返回原 run_id
     → 队列已满且是新请求：HTTP 429 + Retry-After，不创建 RunStore 记录
  → RunStore.reserve(PENDING)
     → 已存在且请求一致：返回原 run_id
     → 已存在但请求不同：HTTP 409
     → 新记录：继续执行
  → RunStore.update_status(RUNNING)
  → RunStore.claim(owner, lease)
  → 执行期间按 heartbeat_interval 续租
  → LangGraph 执行并持续写 Checkpoint
  → RunStore.update_status(最终状态或 WAITING_HUMAN)
  → RunStore.release(owner)
```

查询完整结果时，以 Checkpoint 中的 Graph State 为事实来源，同时把最新状态回写到 RunStore。对于升级前已经存在、但没有 RunStore 索引的旧 Checkpoint，首次查询会自动补建索引。

## 分布式模式保存时序

```text
POST /v1/runs on API-A
  → 校验请求，计算幂等哈希、请求指纹和 graph_revision
  → PostgreSQL advisory transaction lock
  → 同一事务检查全局 available 容量
  → 同一事务写 run_index(PENDING) + run_jobs(START, AVAILABLE, payload)
  → 提交成功后才返回 202

Worker-B
  → 按 graph_revision 使用 FOR UPDATE SKIP LOCKED 原子领取
  → 生成 UUID fencing token，更新 run_index(RUNNING)
  → 从共享 PostgreSQL Checkpoint 首次执行或 ainvoke(None) 续跑
  → 周期续租；旧 token 无法 heartbeat/ack/nack
  → 业务终态：原子完成 Job 并同步 Run Index
  → 基础设施错误：有界退避后重新 available，耗尽进入 dead
```

人工批准会在 `waiting_human` Run 下创建唯一 `RESUME` Job，并在同一事务把 Run 改成 `pending`，
使 SSE 在等待 Worker 时保持活动；Worker 使用 `Command(resume=...)` 从共享 Checkpoint 继续。取消
会在事务中使所有 active Job 失效；Worker 最迟在下一次心跳观察到 token 失效。调度语义为
at-least-once，外部副作用仍依赖 Tool 幂等合同。

## SQLite 实现

SQLiteRunStore 使用独立的 `run_index` 表：

```text
run_id                  PRIMARY KEY
status                  当前运行状态
idempotency_key_hash    可空，唯一索引
request_fingerprint     可空，请求一致性校验
lease_owner             当前执行 owner，可空
lease_expires_at        UTC 租约截止时间，可空
attempt_count           成功获取执行权的累计次数
created_at              UTC ISO 8601
updated_at              UTC ISO 8601
```

初始化时自动建表并迁移租约列、启用 WAL，并设置 busy timeout。所有 SQL 参数均采用参数绑定；写操作由进程内异步锁串行化，数据库唯一索引负责跨连接的最终幂等约束，条件更新负责租约互斥。

## PostgreSQL 实现

`PostgresRunStore` 使用 schema version 5 管理以下表：

| 表 | 作用 |
| --- | --- |
| `work_harness_run_index` | API 状态、幂等关系、取消标记及可选执行版本绑定 |
| `work_harness_run_inputs` | 接纳时的私有原始输入，与索引、初始 Job 同事务保存 |
| `work_harness_run_jobs` | START/RESUME Job、JSONB payload、Graph revision、租约与死信审计 |
| `work_harness_worker_heartbeats` | Worker 容量、活动数、版本和配置摘要；只用于可观测性/readiness |

`work-harness migrate` 是部署时执行 DDL 的入口，依次升级队列、黑板并调用 LangGraph
`AsyncPostgresSaver.setup()`；API 和 Worker 启动只检查精确 schema version，空库、旧库或比应用
更新的 schema 都不会被静默使用。队列接纳通过数据库级 advisory transaction lock 保证多 API
实例下的精确上限；领取依赖数据库时间、行锁与 `SKIP LOCKED`，不依赖进程时钟。

租约到期后可以由另一 Worker 接管并生成新 token；如果崩溃次数已经达到 `max_attempts`，下一次
领取只授予一次“Checkpoint 对账租约”：共享 Checkpoint 已是业务终态则直接确认，仍是活动态或
不存在才原子转入 dead。这样既不会形成无限崩溃循环，也不会把“Graph 已成功、确认前崩溃”误判
为失败。死信重放创建新 Job 并保留旧记录。`graph_revision` 由应用版本和有效配置摘要生成，避免
不兼容 Worker 误领旧任务。

本次执行指纹加入 Prompt 版本、Skill 和已解析 Provider 目标。升级前先用原 Worker 排空旧
执行指纹的 Job，再迁移 Schema 并重启服务；新 Worker 不会为继续执行而重标历史任务。

索引的 `execution` 保存引擎名称、控制图 revision、状态 Schema 版本和执行配置指纹，
与 Run 接纳在同一次插入中写入；它不保存 Prompt、任务正文或凭据。重复接纳返回原绑定，
状态及租约更新保留该绑定。SQLite 同样增量增加此字段；历史记录的空值保留，按旧引擎
解释，不补造版本。SQLite 使用同库的 `run_inputs` 保存原始输入；它不随运行索引列表返回。
两种存储均原子保存索引与输入，首次 Checkpoint 前中断不丢失已接纳的新请求。旧记录没有
原始输入时仍依靠原 Checkpoint 恢复，不伪造缺失输入。

Memory/SQLite 的嵌入式入口已支持选择黑板引擎。黑板对象、附件、激活账本及确认凭据位于
独立黑板存储（SQLite Schema 9），LangGraph Checkpoint 保留控制图位置；Run 的对象 Schema
版本为 1，控制图 revision 为 `blackboard-v7`，这些版本与 PostgreSQL 索引迁移版本分别管理。
配置指纹包括有效 Prompt 及版本、Skill、模型和工具配置、初始化时解析的 Provider 目标、
知识源注册和确认/调度策略；不包含凭据值。配置不匹配时不继续推进活动黑板，完成历史仍可读。
PostgreSQL 黑板存储已在 5.1 实现，使用 `work_harness_schema_version` 中独立的
`blackboard` 组件当前版本 **3**，不改变队列组件的 Schema 5。黑板迁移使用 RunStore 的 DSN，
Checkpoint 仍使用自己的配置；表名以 `work_harness_boards` / `work_harness_board_` 开头。
迁移步骤 1 建立知识、历史、事件、回执、附件和控制表，步骤 2 加入激活账本、用量、核验
凭据和持久对账标记；步骤 3 增加队列托管标记和写入代次绑定的 Job/token。DDL 与版本
记录同事务提交；重复升级幂等，未来版本被拒绝。

`PostgresBlackboardStore` 与 `PostgresActivationStore` 共享异步连接池和事务；提交对象、
事件、回执与结算账本要么全部成功，要么全部回滚。同板写入使用事务级 advisory lock，
读取使用 REPEATABLE READ，防止在多条查询间混入另一提交的对象或控制状态。事务规则
参照 [PostgreSQL 隔离级别](https://www.postgresql.org/docs/current/transaction-iso.html)
和[事务锁文档](https://www.postgresql.org/docs/current/explicit-locking.html)。
写入令牌按已持久化的 owner、session 与 generation 验证，另一客户端必须显式接管；
连接池关闭不表示旧 owner 已失效。账本适配器必须绑定同一个黑板存储实例，确保结算共用
事务。队列执行使用 `store.for_lease(job.lease_ref())` 返回的独立句柄；它保留固定租约，
复用连接池但不负责关闭根连接池。晚到回调继续携带原 token，不能自动借用接管者身份。

5.2 的事务边界先锁定 Job，再锁 Run，最后锁黑板；验证 run/job 身份、owner/token、
数据库时间、执行引擎和只读对账限制，以及 Run 取消与板面封板状态。在事务结束前再次
检查租约到期时间。托管标记持久保存，不能通过未绑定租约的存储句柄绕过校验；新 Job
或接管后的 token 必须重新取得板面写入权。用量记账单独允许撤销租约后的旧尝试补记，
其入口不能保存提案、分配执行名额或推进执行。

队列取消遵循同一锁顺序，等待在途发布完成；若黑板已封板，则保留已确定结果和用于
对账的租约。宿主封取消板前须在事务中确认队列取消已持久化，避免绕过队列取消屏障。
API/Worker 新请求默认使用黑板引擎，须提供验收条件，旧引擎仍可显式选择。每次执行建立携带固定 Job 租约的
独立引擎，根图和子图的 Checkpoint 写入也在同一数据库连接上检查租约。过期写入连同
Checkpoint SQL 回滚，旧 Worker 不能覆盖接管者的位置。已保存的知识源输出由新 Worker
继续提交，已发生用量保留；未保存的外部调用结果仍受至少一次执行边界约束。

PostgreSQL 的类型化人工输入与唤醒 Job 共用事务，包括确认凭据、对象、回执和事件。
活跃 Run 借用当前写入权提交宿主命令，保持 Worker 的 owner/token/generation；等待态
使用事务内短租约，提交时释放为可领取的恢复 Job。Worker 确认人工等待时同时检查板面
唤醒版本，检查点之后到达的输入会继续执行，不能被确认成已覆盖。

Worker 扫描持久终态标记和未封板的取消记录，将封板、账本清理、Run 状态与 Job 完成
对齐。取消发生在初始化之前时，从已接受的原始目标建立取消黑板。扫描器只对账已保存
的终态或取消决定，不调用知识源；通知丢失由队列轮询、持久标记扫描和 SSE 事件补读覆盖。
读接口不负责清除待对账标记。存储和运行合同测试
通过显式 `WORK_HARNESS_TEST_DATABASE_URL` 连接真实 PostgreSQL，每项黑板测试建立并清理
独立 schema；未配置数据库时跳过 PostgreSQL 参数，Memory/SQLite 测试照常执行。

Schema 9 增加控制面的 `wakeup_revision` 和 `status_pending`：前者与人工输入的对象、
事件和回执原子保存，后者与封板原子保存。暂停检查点覆盖输入后、或 Run Index 已同步
终态后分别清除；扫描器据此补偿通知丢失及跨存储写入中断。API 的等待态输入持有短期
Run 租约和板面写入权，完成后撤销其写入代次；活跃态使用当前控制器的写入权。
人工确认只形成范围明确的 `user_statement` Fact。旧黑板运行的活动执行合同不会自动
改为 v7：升级前应使用原应用版本结束这些运行，再备份并迁移；旧版本不能写入 Schema 9。

黑板 SSE 使用持久事件游标，可跨服务重启续读；日志缺失会要求重新取快照。旧引擎的
跨实例 SSE 使用 PostgreSQL `LISTEN/NOTIFY` 唤醒本地订阅者，监听连接断开后自动重连；SSE
keepalive 周期仍会重新读取共享状态，因此丢失通知只增加延迟，不改变最终结果。NOTIFY 不是事件
存档，旧引擎仍不提供持久事件回放。

## 启动恢复

内嵌 API 生命周期会启动恢复扫描器。黑板先扫描持久控制标记，补偿待唤醒输入及待同步
终态，包括索引仍为 `waiting_human` 的运行。普通执行恢复认领 `pending/running` 且租约为空或已过期的记录，并且
每轮只装入当前接纳队列可以容纳的数量；其余记录保持持久状态，由后续扫描继续处理：

1. Checkpoint 已是终态或人工等待态：把事实状态同步回 RunStore；
2. Checkpoint 是活动态：以 `ainvoke(None, thread_id)` 从最近 Checkpoint 续跑；
3. 完全没有 Checkpoint：把孤立索引标记为 `failed`，避免重新提交未知原始请求；
4. 活跃运行由心跳续租；失去租约时取消本地调用，避免两个 owner 同时推进。

默认租约 30 秒、心跳和扫描间隔 10 秒，可在 `config/runtime.yaml` 调整。心跳必须短于租约。

## 一致性边界

- Checkpoint 是完整执行状态的事实来源；
- RunStore 是快速列表和幂等关系的事实来源；
- 每次执行结束、人工中断、取消和结果查询都会同步 RunStore 状态；
- 如果 RunStore 写入成功但进程在首次 Checkpoint 前退出，启动扫描会将孤立索引安全标记失败；因为 RunStore 不保存原始请求，所以不会猜测性重放；
- 分布式模式在一个 PostgreSQL 事务中保存 Run 与原始 Job，因此首次 Checkpoint 前的 API/Worker
  崩溃仍可重新领取；
- 分布式模式必须同时使用 PostgreSQL RunStore 与 PostgreSQL LangGraph Checkpoint，禁止混搭；
- Run Index 是分布式 API 当前状态的事实来源，Checkpoint 是完整 Graph State 的事实来源，Job
  表是待执行动作与租约的事实来源。

## 模式切换

```yaml
checkpoint:
  backend: memory | sqlite | postgres
  path: var/checkpoints.sqlite
  dsn_env: WORK_HARNESS_DATABASE_URL
run_store:
  backend: memory | sqlite | postgres
  path: var/runs.sqlite
  dsn_env: WORK_HARNESS_DATABASE_URL
provider_store:
  backend: memory | sqlite
  path: var/providers.sqlite
execution:
  role: embedded | api | worker
```

测试调用 `build_runtime(..., checkpoint_backend="memory")` 时，RunStore 和 ProviderProfileStore 默认同时切换到 memory，防止测试污染本地持久化数据。也可以通过 `run_store_backend`、`provider_store_backend` 单独覆盖。

同一场景下 ApplicationSettingsStore 也切换为 memory。文件部署可通过
`WORK_HARNESS_SETTINGS_PATH` 覆盖 `settings.json` 的位置。

`api/worker` 角色强制 PostgreSQL Checkpoint 与 RunStore 使用同一个 DSN 环境变量，并要求队列
容量大于零。分布式 Profile 当前把 ProviderStore/ApplicationSettingsStore 固定为 memory，并禁用
页面写接口；所有实例通过同一部署配置启动，以保证执行摘要一致。

## 扩展原则

新增存储实现时不要修改 `RunManager` 和 Graph 节点，只需：

1. 实现 RunStore 合同；
2. 在存储工厂注册；
3. 扩展配置枚举；
4. 保证 `reserve` 原子性；可靠队列还必须保证 Run 与 Job 原子提交；
5. 覆盖重启恢复、幂等冲突、状态同步和分页测试；
6. 多实例实现必须在真实数据库上覆盖竞争领取、租约接管、fencing、容量、迁移、死信和通知，
   不能用 SQLite 模拟结果代替。

服务端会话同步属于独立的 Conversation Repository，不应把前端展示数据写入 RunStore。


## 黑板备份与接纳回退

步骤 6.3 的备份覆盖 RunStore、Checkpoint、黑板全部表、Provider 密文与密钥、页面设置及
匹配的配置、Prompt、Skill。黑板附件和账本随黑板数据库保存。SQLite 备份要求停止应用，
使用运行时进程锁防止活动部署被跨库取样；PostgreSQL 备份在停止 API/Worker 后导出整个数据库。
恢复至新目录或新数据库，不能只恢复其中一部分。标准命令与范围见
[备份及恢复](docker-deployment.md#8-内嵌模式-sqlite-备份与恢复)。

`WORK_HARNESS_WORKFLOW_ENGINE=legacy` 配合 `WORK_HARNESS_ACCEPT_NEW_BLACKBOARD_RUNS=false`
可将新任务回退至旧引擎。接纳开关不进入已有运行的执行指纹，不改写历史引擎；幂等重试、
原黑板的人工输入和恢复仍使用原执行版本。恢复演练包含 SQLite 与真实 PostgreSQL 16，
详情及证据见[黑板实施计划](blackboard-design.md#63-备份与回退结果)。
