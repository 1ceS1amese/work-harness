# 持久化设计

## 目标

持久化层解决三个相互独立的问题：

1. LangGraph 执行状态可以在进程重启后恢复；
2. run_id、状态和幂等关系可以被可靠查询；
3. 前端群聊在刷新页面后可以继续展示。

这些状态不能写进同一张业务表，也不能由 `RunManager` 直接操作具体数据库。

## 分层

| 层 | 接口/实现 | 保存内容 | 默认位置 |
|---|---|---|---|
| Workflow Checkpoint | LangGraph Checkpointer | Graph State、任务、计划、产物、复核、中断点 | `var/checkpoints.sqlite` |
| Run Index | `RunStore` | run_id、状态、时间、幂等哈希、请求指纹 | `var/runs.sqlite` |
| UI Conversation | `ConversationStorage` | 群聊、消息、选择项、活动会话 | 浏览器 localStorage |

RunStore 不保存原始幂等键和完整请求，只保存 SHA-256 哈希。这样既支持幂等校验，也减少运行列表数据库中的敏感数据复制。

## RunStore 合同

```text
initialize()                         初始化表或连接
reserve(record)                      原子创建；幂等键已存在时返回原记录
get(run_id)                          查询单条运行索引
update_status(run_id, status)        同步状态与更新时间
list(limit, offset)                  按创建时间倒序分页
claim(run_id, owner, ttl)            原子获取或续接过期运行的唯一执行权
heartbeat(run_id, owner, ttl)        当前 owner 延长租约
release(run_id, owner)               释放当前 owner 的租约
list_recoverable(limit)              扫描无租约或租约过期的未完成运行
close()                              释放资源
```

Memory 和 SQLite 实现遵循同一合同。`RunManager` 只依赖该合同，通过 `HarnessRuntime` 注入实例。

## 保存时序

```text
POST /v1/runs
  → 规范化请求并计算 request_fingerprint
  → 对 idempotency_key 计算哈希
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

## 启动恢复

API 生命周期会启动恢复扫描器。它只认领 `pending/running` 且租约为空或已过期的记录：

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
- 多实例生产部署应使用支持事务和行级并发控制的共享数据库实现 RunStore，并使用生产级 LangGraph Checkpoint。

## 模式切换

```yaml
checkpoint:
  backend: memory | sqlite
  path: var/checkpoints.sqlite
run_store:
  backend: memory | sqlite
  path: var/runs.sqlite
```

测试调用 `build_runtime(..., checkpoint_backend="memory")` 时，RunStore 默认同时切换到 memory，防止测试污染本地持久化数据。也可以通过 `run_store_backend` 单独覆盖。

## 扩展原则

新增存储实现时不要修改 `RunManager` 和 Graph 节点，只需：

1. 实现 RunStore 合同；
2. 在存储工厂注册；
3. 扩展配置枚举；
4. 保证 `reserve` 原子性；
5. 覆盖重启恢复、幂等冲突、状态同步和分页测试。

服务端会话同步属于独立的 Conversation Repository，不应把前端展示数据写入 RunStore。
