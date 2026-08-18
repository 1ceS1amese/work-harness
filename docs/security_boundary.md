# 安全边界

## 密钥与模型

- 配置文件只保存环境变量名，不保存真实密钥；
- Provider 白名单同时限制 Provider 和实际模型；
- API 请求不能覆盖模型端点、密钥、系统 Prompt 或工具注册表；
- OpenAI 兼容 Adapter 默认只允许 HTTPS，阻止显式 localhost、私有、保留和链路本地 IP；
- 认证、参数和结构错误不重试，只有超时、传输、429 和部分 5xx 有界重试；
- 日志不记录请求正文、工具参数、Cookie、Token、密码和私钥。

当前 URL 检查不能独立防御 DNS rebinding。生产环境还必须在网络出口层使用域名/IP allowlist，并阻断云元数据地址和内部管理网段。

## 工具

- 每个工具必须同时有 YAML 元数据和注册的类型化 Handler；
- 调用前检查 Agent 角色、Pydantic 参数、审批状态和超时；
- 模型只收到当前子任务与角色共同允许的工具 Schema；未知工具和非法 JSON 参数直接失败；
- `workspace_read` 将路径固定在项目根目录，限制文件/行/扫描/输出大小，并拒绝 `.env`、私钥、依赖目录、运行数据库和越界符号链接；
- Checkpoint 中的工具审计只保存参数 SHA-256 与有界输出摘要，不复制原始参数；
- Write、Execute、External Communication 配置若未要求审批，启动校验直接失败；
- 副作用工具必须补充幂等键、审计事件和恢复测试后才能接入。

## 状态与日志

- Blackboard / Checkpoint 只保存标准合同和受控摘要；
- 不保存模型供应商私有响应、隐藏推理或凭据；
- SQLite Checkpoint 当前未加密，不能存储敏感生产任务；生产启用加密 Serializer 或数据库层加密；
- JSON 日志使用字段名脱敏，但业务内容中的自由文本秘密仍需在入口和工具 Adapter 增加内容级 DLP。
