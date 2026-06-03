# FCGO 架构说明

## 模块

- `fcgo.config`：配置加载和环境变量管理。
- `fcgo.logging`：日志初始化和密钥脱敏。
- `fcgo.models`：核心 Pydantic 数据模型。
- `fcgo.storage`：SQLite 存储 token、幂等键、待确认动作和审计日志。
- `fcgo.feishu`：飞书客户端、OAuth、长连接 worker、OpenAPI 封装和消息路由。
- `fcgo.gemini`：Gemini 模型提供方，是当前默认真实 Provider。
- `fcgo.model_providers`：通用模型请求/响应协议、Provider 无关的提示词构建器、注册表和运行时路由。
- `fcgo.agent`：助手编排层，连接消息、资源和模型。
- `fcgo.resources`：飞书资源和网页链接解析/读取。
- `fcgo.writeback`：写回预览卡片、待确认动作确认和执行器。
- `fcgo.server`：FastAPI 服务入口。

## 数据流

1. 飞书长连接收到消息事件。
2. `FeishuMessageRouter` 做消息幂等检查并构造 `AssistantRequest`。
3. `Assistant` 提取链接并调用 `ModelRouter`。
4. `ModelRouter` 根据默认 Provider/模型配置选择具体 Provider。
5. `GeminiProvider` 或其他 Provider 返回统一模型响应。
6. 如果模型只返回普通回复，`FeishuClient` 将文本发送回原飞书会话。
7. 如果模型返回写回提案，并且用户消息有明确写回意图，路由先保存 `pending_actions`，再发送飞书交互卡片展示回复和写回预览。
8. 如果用户只是问答、总结或分析，即使模型误产出写回提案，路由也会抑制卡片并仅返回普通文本。

## 模型工具协议

- `read_resource`：模型请求读取用户授权范围内的飞书或网页资源，返回 `ResourceReadResult`。
- `propose_writeback`：模型只能创建写回草案，服务端把草案包装为待确认 `ActionProposal`。
- 模型工具不会直接执行写入；所有写入都必须先保存 pending action，再由飞书卡片确认。

## 多模型 Provider

当前运行时代码通过 `ModelProviderRegistry` 和 `ModelRouter` 装配 Provider。默认 Provider
由 `FCGO_DEFAULT_PROVIDER` 控制，默认模型可通过 `FCGO_DEFAULT_MODEL` 覆盖；目前已实现
`gemini`、`openai`、`deepseek`、`qwen`、`doubao`、`minimax` 和本地开发用 `echo`。
后续多模型接入规划见
[多模型 Provider 架构规划](multi-model-provider-architecture.md)，目标是支持
Claude、Seedance、ComfyUI API 等不同类型 Provider，并通过统一能力矩阵、配置和路由层管理。

## 写回流

1. 模型只能生成 `ActionProposal`。
2. 路由根据用户消息判断是否有明确写回意图；普通问答、总结、分析不触发卡片。
3. 通过意图判断后，proposal 保存到 `pending_actions`。
4. 飞书交互卡片展示模型回复、目标资源、写回动作、预览内容、动作 ID 和过期时间。
5. 卡片提供“确认执行”和“取消”按钮。
6. 飞书回调 `/callbacks/feishu/card` 解析按钮 value、操作者和事件 ID，并用事件 ID 做回调幂等。
7. 用户确认后调用 `WritebackService.confirm`，取消时调用 `WritebackService.cancel`。
8. 服务校验操作者、状态、过期时间和幂等性。
9. `FeishuWriteExecutor` 执行实际写入并记录审计日志。当前支持文档创建/追加、电子表格范围写入、多维表格记录创建/更新，以及飞书消息发送。

## 本地存储

SQLite 默认位置为 `data/fcgo.sqlite3`，包含：

- `oauth_tokens`
- `idempotency_keys`
- `pending_actions`
- `audit_events`
