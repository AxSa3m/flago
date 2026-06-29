# FCGO 架构说明

## 模块

- `fcgo.config`：配置加载和环境变量管理。
- `fcgo.logging`：日志初始化和密钥脱敏。
- `fcgo.models`：核心 Pydantic 数据模型。
- `fcgo.storage`：SQLite 存储 token、幂等键、模型偏好和审计日志。
- `fcgo.feishu`：飞书客户端、OAuth、长连接 worker、OpenAPI 封装和消息路由。
- `fcgo.gemini`：Gemini 模型提供方，是当前默认真实 Provider。
- `fcgo.model_providers`：通用模型请求/响应协议、Provider 无关的提示词构建器、注册表和运行时路由。
- `fcgo.agent`：助手编排层，连接消息、资源、工具和模型；当前同时保留 legacy
  Assistant 与新 AgentOrchestrator。
- `fcgo.resources`：飞书资源和网页链接解析/读取。
- `fcgo.writeback`：确认式写回、待确认动作、执行与撤回记录。
- `fcgo.server`：FastAPI 服务入口。

上下文读取、用户授权和长期记忆的隐私边界见
[上下文读取、用户授权和长期记忆隐私规格](context-privacy-memory.md)。相关实现默认开启当前会话上下文读取，并禁止长期保存完整正文。

## 数据流

1. 飞书长连接收到消息事件。
2. `FeishuMessageRouter` 做消息幂等检查并构造 `AssistantRequest`。
3. 路由根据 `FCGO_AGENT_MODE` 选择编排层：
   - `legacy`：使用旧 `Assistant`，保持当前线上可用行为。
   - `agent`：使用 `AgentOrchestrator`，要求模型输出 `AgentDecision` JSON。
4. `ModelRouter` 根据默认 Provider、用户模型偏好和请求模型配置选择具体 Provider。
5. Agent 模式下，模型只能输出 `final_response`、`tool_calls` 或 `writeback_drafts`。
6. 所有工具调用进入 `ToolRegistry`，先做参数校验、群聊限制、只读/写入策略和超时控制，再执行具体 Skill/资源逻辑。
7. `FeishuClient` 将最终文本或确认卡片发送回原飞书会话。

## Agent + Tools 双轨协议

内部协议由 `fcgo.models` 定义：

- `AgentDecision`：模型每轮只允许输出 `final_response`、`tool_calls`、`writeback_drafts`。
- `ToolCall`：统一工具名、参数和 call id。
- `ToolResult`：统一返回 `ok`、`content`、`error`、`user_message` 和审计元数据。
- `ActionProposalDraft`：写回确认卡片的草案输入。

第一阶段默认使用模型无关 JSON parser：所有 Provider 仍按普通聊天能力调用，模型在文本中返回 JSON。
如果 JSON 解析或 Pydantic 校验失败，系统只允许一次 repair prompt；仍失败则返回用户可读错误，不执行工具。

第二阶段可以为支持原生 function calling 的 Provider 添加 adapter。adapter 只负责把原生 tool call 转换成内部
`ToolCall`，并把内部 `ToolResult` 转回 Provider tool result message；Agent Core、Tool Registry 和安全策略不变。

当前 Tool Registry 暴露：

- `search_resources`
- `read_resource`
- `inspect_doc_structure`
- `prepare_writeback`
- `get_writeback_policy`
- `list_memory`
- `upsert_memory_draft`
- `get_model_status`
- `web_read`

每个工具声明参数 schema、是否只读、所需权限、是否允许群聊、超时和审计类型。未知工具、参数不合法、
群聊禁用工具，以及只读模式下的写入工具都会被拒绝。

## 多模型 Provider

当前运行时代码通过 `ModelProviderRegistry` 和 `ModelRouter` 装配 Provider。默认 Provider
由 `FCGO_DEFAULT_PROVIDER` 控制，默认模型可通过 `FCGO_DEFAULT_MODEL` 覆盖；目前已实现
`gemini`、`openai`、`deepseek`、`qwen`、`doubao`、`minimax`、`claude` 和本地开发用 `echo`。
后续多模型接入规划见
[多模型 Provider 架构规划](multi-model-provider-architecture.md)，目标是支持
Seedance、ComfyUI API 等不同类型 Provider，并通过统一能力矩阵、配置和路由层管理。

## 写回策略

写回仍由程序执行安全校验、确认卡片、待确认动作、审计和撤回记录。Agent 只能准备草案，不能绕过执行层。

环境变量：

- `FCGO_WRITEBACK_ENABLED=false|true`：关闭时不会保存或执行任何写回。
- `FCGO_WRITEBACK_AUTO_EXECUTE_ENABLED=false|true`：控制用户是否可以通过 `/写回 自动开启`
  启用个人低风险自动写回；默认关闭。
- `FCGO_WRITEBACK_CONFIRMATION_MODE=always|low_risk_direct|draft_only`
  - `always`：默认值；所有写入、修改、删除都生成确认卡片。
  - `low_risk_direct`：明确目标的文档开头/末尾追加可直接执行；修改、删除、表格和多维表仍需确认。
  - `draft_only`：只返回草稿，不保存 pending action，不执行。

用户级自动写回偏好只影响该用户。开启后仍不会绕过权限、幂等、审计和可撤回记录；高风险动作仍强制确认。

Agent v1 只稳定支持文档开头、文档末尾、表格范围、多维表新增/更新/删除这类结构化写回草案。
文档中间位置、附件前后位置会返回“不支持稳定写入中间位置”，不会生成可执行卡片。

## 本地存储

SQLite 默认位置为 `data/fcgo.sqlite3`，包含：

- `oauth_tokens`
- `idempotency_keys`
- `pending_actions`（实验写回遗留表，当前默认不写入）
- `audit_events`

隐私默认配置：

- `FCGO_MEMORY_STORE_RAW_TEXT=false`
