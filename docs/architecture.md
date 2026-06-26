# FCGO 架构说明

## 模块

- `fcgo.config`：配置加载和环境变量管理。
- `fcgo.logging`：日志初始化和密钥脱敏。
- `fcgo.models`：核心 Pydantic 数据模型。
- `fcgo.storage`：SQLite 存储 token、幂等键、模型偏好和审计日志。
- `fcgo.feishu`：飞书客户端、OAuth、长连接 worker、OpenAPI 封装和消息路由。
- `fcgo.gemini`：Gemini 模型提供方，是当前默认真实 Provider。
- `fcgo.model_providers`：通用模型请求/响应协议、Provider 无关的提示词构建器、注册表和运行时路由。
- `fcgo.agent`：助手编排层，连接消息、资源和模型。
- `fcgo.resources`：飞书资源和网页链接解析/读取。
- `fcgo.writeback`：实验性写回代码，当前分支默认关闭。
- `fcgo.server`：FastAPI 服务入口。

上下文读取、用户授权和长期记忆的隐私边界见
[上下文读取、用户授权和长期记忆隐私规格](context-privacy-memory.md)。相关实现默认开启当前会话上下文读取，并禁止长期保存完整正文。

## 数据流

1. 飞书长连接收到消息事件。
2. `FeishuMessageRouter` 做消息幂等检查并构造 `AssistantRequest`。
3. `Assistant` 提取链接并调用 `ModelRouter`。
4. `ModelRouter` 根据默认 Provider/模型配置选择具体 Provider。
5. `GeminiProvider` 或其他 Provider 返回统一模型响应。
6. `FeishuClient` 将模型回复发送回原飞书会话。
7. 当前分支写入功能默认关闭；即使旧 Provider 误返回写回提案，路由也会丢弃提案并只返回文本提醒。

## 模型工具协议

- `read_resource`：模型请求读取用户授权范围内的飞书或网页资源，返回 `ResourceReadResult`。
- `propose_writeback` 工具暂不暴露给模型；本分支先聚焦读取和上下文。

## 多模型 Provider

当前运行时代码通过 `ModelProviderRegistry` 和 `ModelRouter` 装配 Provider。默认 Provider
由 `FCGO_DEFAULT_PROVIDER` 控制，默认模型可通过 `FCGO_DEFAULT_MODEL` 覆盖；目前已实现
`gemini`、`openai`、`deepseek`、`qwen`、`doubao`、`minimax`、`claude` 和本地开发用 `echo`。
后续多模型接入规划见
[多模型 Provider 架构规划](multi-model-provider-architecture.md)，目标是支持
Seedance、ComfyUI API 等不同类型 Provider，并通过统一能力矩阵、配置和路由层管理。

## 写入暂停

当前分支默认 `FCGO_WRITEBACK_ENABLED=false`。运行时不会生成写回卡片、保存待确认写回动作或执行写入。
旧卡片回调会返回“写入功能当前已暂停”。后续如果重新设计写入能力，应从清晰的 Agent 工具调用协议开始，
而不是继续依赖关键词和兜底解析。

## 本地存储

SQLite 默认位置为 `data/fcgo.sqlite3`，包含：

- `oauth_tokens`
- `idempotency_keys`
- `pending_actions`（实验写回遗留表，当前默认不写入）
- `audit_events`

隐私默认配置：

- `FCGO_MEMORY_STORE_RAW_TEXT=false`
