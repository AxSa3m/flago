# 多模型 Provider 架构规划

## 目标

FCGO 当前已经完成 Gemini 驱动的飞书消息闭环。下一阶段的目标是把模型层从
“Gemini 单一实现”升级为“可配置、可路由、可扩展的多模型 Provider 体系”，让开源
使用者可以按自己的网络、预算、账号和工作场景选择不同模型。

需要覆盖两类能力：

- 文本/多模态聊天模型：Gemini、OpenAI、Claude、DeepSeek、Qwen、Doubao、Minimax。
- 媒体与工作流 API：Seedance、ComfyUI API 等图像、视频或工作流执行能力。

## 当前扩展性评估

当前架构已经有一个好的基础：

- `Assistant` 只依赖 `ModelProvider` 协议。
- 飞书消息路由、资源读取、写回确认和模型调用已经基本分层。
- `AssistantRequest` 和 `AssistantResponse` 让业务层不用直接依赖 Gemini SDK。

当前限制逐步收敛中：

- 启动装配已迁移到 `ModelProviderRegistry` 和 `ModelRouter`。
- 已支持部署级默认 Provider/模型配置，以及飞书中的会话级/个人级模型偏好。
- prompt 构建已抽到 Provider 无关层。
- 模型能力已由 `ProviderCapability` 表达，当前用于路由前的能力校验。
- Seedance、ComfyUI 这类接口不是普通聊天模型，不能直接塞进 `ModelProvider.generate`。

结论：不需要重写飞书和资源读取层，但需要重构模型层，让 Gemini 成为标准 Provider
之一，而不是唯一入口。当前 Gemini 与 OpenAI-compatible Provider 已完成。

## 分层设计

建议把模型层拆成四层。

### 1. 业务编排层

`Assistant` 继续负责：

- 解析用户输入。
- 读取飞书资源。
- 构造统一业务请求。
- 调用模型路由器。
- 返回 `AssistantResponse`。

这一层不应该知道 OpenAI、Claude、Gemini 或 ComfyUI 的 SDK 细节。

### 2. 通用模型协议层

新增 provider-agnostic 数据结构：

- `ModelRequest`
- `ModelMessage`
- `ModelResponse`
- `ModelUsage`
- `ModelError`
- `ProviderConfig`
- `ProviderCapability`

`AssistantRequest` 仍是业务入口，通用模型协议负责把业务上下文转换成不同模型 SDK
可以消费的请求。

### 3. Provider 注册与路由层

新增 `ModelProviderRegistry` 和 `ModelRouter`：

- 根据配置加载可用 Provider。
- 根据默认配置选择 Provider 和模型。
- 根据能力需求选择 Provider，例如工具调用、长上下文、视觉输入、图像生成。
- 支持失败降级和清晰错误提示。
- 预留用户级、会话级模型选择。

### 4. Provider 实现层

每个 Provider 只负责一件事：把通用模型请求适配为目标 API 请求，再把响应转回统一
格式。

首批 Provider 建议：

- `GeminiProvider`
- `OpenAICompatibleProvider`
- `ClaudeProvider`
- `MediaWorkflowProvider` 抽象
- `SeedanceProvider`
- `ComfyUIProvider`

## Provider 类型

### Chat Provider

适用于文本或多模态聊天：

- Gemini
- OpenAI
- Claude
- DeepSeek
- Qwen
- Doubao
- Minimax

核心接口：

```text
generate(request: ModelRequest) -> ModelResponse
```

可选能力：

- tool calling
- JSON mode
- streaming
- vision input
- long context
- reasoning/thinking budget

### OpenAI-Compatible Provider

许多厂商提供 OpenAI 兼容接口。建议优先做一个通用适配器，通过配置覆盖：

- OpenAI 官方 API
- DeepSeek
- Qwen
- Doubao/火山方舟
- Minimax
- 其他兼容网关或中转服务

关键配置：

- provider name
- base URL
- API key
- model name
- timeout
- max output tokens
- proxy
- capability overrides

### Claude Provider

Claude 的消息格式、system prompt、tool use 和错误结构与 OpenAI-compatible 不完全相同，
应单独实现 Provider，避免在兼容适配器中堆积例外。

### Media/Workflow Provider

Seedance、ComfyUI API 不是普通聊天模型。它们应该作为媒体或工作流能力接入：

- `image_generation`
- `video_generation`
- `workflow_execution`
- `asset_upload`
- `job_polling`

这类任务通常有更高成本和更长耗时，默认应要求用户显式触发，必要时用飞书卡片确认。

## 能力矩阵草案

| Provider 类型 | 示例 | 聊天 | 工具调用 | JSON 输出 | 视觉输入 | 媒体生成 | 异步任务 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Gemini | Gemini API | 是 | 可选 | 可选 | 可选 | 部分模型可选 | 否 |
| OpenAI-compatible | OpenAI/DeepSeek/Qwen/Doubao/Minimax | 是 | 取决于厂商 | 取决于厂商 | 取决于模型 | 否 | 否 |
| Claude | Anthropic Claude | 是 | 可选 | 可选 | 取决于模型 | 否 | 否 |
| Seedance | 视频生成 API | 否 | 否 | 不适用 | 可选 | 视频 | 是 |
| ComfyUI | 工作流 API | 否 | 否 | 不适用 | 可选 | 图像/视频/工作流 | 是 |

能力必须由配置或 Provider 自声明，不能仅根据 provider 名称硬编码判断。

## 配置草案

短期继续使用 `.env`。当前已经实现 `FCGO_DEFAULT_PROVIDER` 和
`FCGO_DEFAULT_MODEL`，用于部署级默认 Provider/模型选择；OpenAI-compatible Provider
会在同一组 `API_KEY`、`BASE_URL`、`MODEL` 都存在时注册。

```env
FCGO_DEFAULT_PROVIDER=gemini
FCGO_DEFAULT_MODEL=
FCGO_MODEL_TEST_MAX_OUTPUT_TOKENS=1024
FCGO_MODEL_PROVIDER_CONCURRENCY_LIMIT=4

GEMINI_API_KEY=
GEMINI_BASE_URL=
GEMINI_MODEL=gemini-2.5-flash
GEMINI_TIMEOUT_SECONDS=60
GEMINI_MAX_OUTPUT_TOKENS=
GEMINI_THINKING_BUDGET=

FCGO_OPENAI_COMPATIBLE_TIMEOUT_SECONDS=60
FCGO_OPENAI_COMPATIBLE_MAX_OUTPUT_TOKENS=
FCGO_OPENAI_COMPATIBLE_HTTP_PROXY=

OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=

DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=
DEEPSEEK_MODEL=

QWEN_API_KEY=
QWEN_BASE_URL=
QWEN_MODEL=

DOUBAO_API_KEY=
DOUBAO_BASE_URL=
DOUBAO_MODEL=

MINIMAX_API_KEY=
MINIMAX_BASE_URL=
MINIMAX_MODEL=

ANTHROPIC_API_KEY=
ANTHROPIC_BASE_URL=
ANTHROPIC_MODEL=
ANTHROPIC_VERSION=2023-06-01
ANTHROPIC_TIMEOUT_SECONDS=60
ANTHROPIC_MAX_OUTPUT_TOKENS=4096
ANTHROPIC_HTTP_PROXY=

SEEDANCE_API_KEY=
SEEDANCE_BASE_URL=

COMFYUI_BASE_URL=
COMFYUI_API_KEY=
```

中期可以增加一个结构化配置文件，例如 `providers.toml`，用于描述多个 Provider、模型列表、
能力覆盖和 fallback 规则。

```toml
[defaults]
provider = "gemini"
model = "gemini-2.5-flash"

[[providers]]
name = "deepseek"
kind = "openai_compatible"
base_url_env = "DEEPSEEK_BASE_URL"
api_key_env = "DEEPSEEK_API_KEY"
default_model_env = "DEEPSEEK_MODEL"
capabilities = ["chat", "json"]

[[fallbacks]]
from = "gemini"
to = "deepseek"
on = ["rate_limit", "temporarily_unavailable"]
```

## 路由规则草案

默认路由：

1. 如果用户或会话指定模型，优先使用指定模型（UXS-39 已实现基础指令和 SQLite 偏好）。
2. 否则使用部署配置中的默认 Provider 和模型（UXS-33 已实现）。
3. 如果请求需要特定能力，例如工具调用、JSON 输出、图像生成或视频生成，只选择声明支持该能力的 Provider（UXS-33 已实现能力校验）。
4. 如果默认 Provider 不可用，按 fallback 配置降级；当前本地 dev/test 缺少 Gemini Key 时会降级到 `echo`。
5. 如果没有可用 Provider，返回可操作错误，例如缺少 API key、网络不可达、模型不支持该能力。

用户指令：

- `/模型 查看`
- `/模型 使用 gemini/gemini-2.5-flash`
- `/模型 使用 openai/gpt-...`
- `/模型 默认`
- `/模型 我的 使用 gemini/gemini-2.5-flash`
- `/模型 我的 默认`

群聊中应默认使用会话级模型配置，避免某个用户的个人偏好影响整个群。

## 安全与隐私要求

多模型接入不得削弱现有隐私策略：

- 不持久化飞书正文。
- 不在日志中输出 API key、Authorization header、完整资源正文。
- Provider 错误必须脱敏后再展示给飞书用户。
- 成本和额度错误要给出明确提示，但不泄露密钥、账户信息或完整请求体。
- 生成类任务默认需要显式触发；高成本媒体生成任务建议增加确认卡片。
- usage、latency、provider、model 可以写入审计日志；用户正文不写入审计日志。
- 当前实现由 `ModelRouter` 统一记录模型审计元数据，并按 provider 执行并发限制和超时保护。
- test 环境默认使用 `FCGO_MODEL_TEST_MAX_OUTPUT_TOKENS` 限制输出；dev/prod 默认不硬限制，除非显式设置 Provider 的 max token。

## 测试策略

必须优先使用 mock 集成测试，不依赖真实 API key：

- Provider 配置加载测试。
- Registry 和 Router 选择测试。
- OpenAI-compatible 请求格式测试。
- Claude 请求格式测试。
- Gemini 重构兼容性测试。
- Provider 错误脱敏测试。
- fallback 路由测试。
- 媒体任务 job polling mock 测试。

真实 API 测试只作为本地手工验证或可选 smoke test，避免开源 CI 消耗额度。

## 实施顺序

1. `UXS-32`：定义多模型 Provider 架构、能力矩阵和配置规范。
2. `UXS-34`：抽取通用模型请求响应协议与提示词构建器。
3. `UXS-37`：将 Gemini Provider 重构为统一 Provider 接口的默认实现。
4. `UXS-33`：实现 Provider 注册表和运行时路由，并支持部署配置默认 provider/model。
5. `UXS-39`：实现模型选择指令、用户偏好和会话级覆盖。
6. `UXS-35`：接入 OpenAI-compatible Provider。已支持 OpenAI、DeepSeek、Qwen、Doubao、Minimax 配置注册。
7. `UXS-36`：接入 Claude Provider。
8. `UXS-38`：设计并接入 Seedance、ComfyUI 等媒体和工作流 Provider。
9. `UXS-40`：补齐安全、成本、限流、测试和开源文档。

## 非目标

当前阶段不做：

- 多租户 SaaS 管理后台。
- 统一计费系统。
- 自动选择最便宜模型的复杂策略。
- 把所有 Provider 都一次性真实接入。
- 在本地长期保存用户完整聊天原文或飞书正文。
