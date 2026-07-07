# 多模型 Provider 配置模板

本文面向开源部署者，说明 Flago（FLAGO） 多模型配置、安全、成本和测试边界。

## 通用配置

```env
FLAGO_DEFAULT_PROVIDER=gemini
FLAGO_DEFAULT_MODEL=
FLAGO_MODEL_TEST_MAX_OUTPUT_TOKENS=1024
FLAGO_MODEL_PROVIDER_CONCURRENCY_LIMIT=4
```

- `FLAGO_DEFAULT_PROVIDER`：部署默认 Provider。
- `FLAGO_DEFAULT_MODEL`：可选，覆盖 Provider 默认模型。
- `FLAGO_MODEL_TEST_MAX_OUTPUT_TOKENS`：测试环境默认输出 token 上限。
- `FLAGO_MODEL_PROVIDER_CONCURRENCY_LIMIT`：每个 Provider 的并发调用上限，`0` 表示关闭路由层并发限制。

飞书中的“查看模型”只显示当前实际使用的模型、可用模型和未启用的 Provider 名称。
密钥缺失等部署细节不向普通用户展开；部署者可通过 `flago doctor` 和服务日志检查配置。

dev/prod 环境默认不设置硬性 `max_tokens`。如需成本保护，请设置对应 Provider 的 max token 配置。

## Gemini

```env
FLAGO_DEFAULT_PROVIDER=gemini
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
GEMINI_TIMEOUT_SECONDS=60
# GEMINI_MAX_OUTPUT_TOKENS=4096
# GEMINI_THINKING_BUDGET=0
# GEMINI_BASE_URL=
# GEMINI_HTTP_PROXY=http://127.0.0.1:7890
```

`GEMINI_MAX_OUTPUT_TOKENS` 不设置时，dev/prod 不硬限制输出 token；test 环境使用 `FLAGO_MODEL_TEST_MAX_OUTPUT_TOKENS`。

## OpenAI

```env
FLAGO_DEFAULT_PROVIDER=openai
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=
FLAGO_OPENAI_COMPATIBLE_TIMEOUT_SECONDS=60
# FLAGO_OPENAI_COMPATIBLE_MAX_OUTPUT_TOKENS=4096
# FLAGO_OPENAI_COMPATIBLE_HTTP_PROXY=http://127.0.0.1:7890
```

OpenAI 当前走 OpenAI-compatible Chat Completions 接口。只有 `API_KEY`、`BASE_URL` 和 `MODEL` 都存在时才注册该 Provider。

## DeepSeek

```env
FLAGO_DEFAULT_PROVIDER=deepseek
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
FLAGO_OPENAI_COMPATIBLE_TIMEOUT_SECONDS=60
# FLAGO_OPENAI_COMPATIBLE_MAX_OUTPUT_TOKENS=4096
```

## Qwen

```env
FLAGO_DEFAULT_PROVIDER=qwen
QWEN_API_KEY=
QWEN_BASE_URL=
QWEN_MODEL=
FLAGO_OPENAI_COMPATIBLE_TIMEOUT_SECONDS=60
# FLAGO_OPENAI_COMPATIBLE_MAX_OUTPUT_TOKENS=4096
```

## Doubao

```env
FLAGO_DEFAULT_PROVIDER=doubao
DOUBAO_API_KEY=
DOUBAO_BASE_URL=
DOUBAO_MODEL=
FLAGO_OPENAI_COMPATIBLE_TIMEOUT_SECONDS=60
# FLAGO_OPENAI_COMPATIBLE_MAX_OUTPUT_TOKENS=4096
```

## Minimax

```env
FLAGO_DEFAULT_PROVIDER=minimax
MINIMAX_API_KEY=
MINIMAX_BASE_URL=
MINIMAX_MODEL=
FLAGO_OPENAI_COMPATIBLE_TIMEOUT_SECONDS=60
# FLAGO_OPENAI_COMPATIBLE_MAX_OUTPUT_TOKENS=4096
```

## Claude

Claude Provider 通过 Anthropic Messages API 接入，当前启用文本聊天，不声明 Flago（FLAGO） 工具调用能力。

```env
FLAGO_DEFAULT_PROVIDER=claude
ANTHROPIC_API_KEY=
ANTHROPIC_BASE_URL=https://api.anthropic.com
ANTHROPIC_MODEL=claude-sonnet-4-5
ANTHROPIC_VERSION=2023-06-01
ANTHROPIC_TIMEOUT_SECONDS=60
ANTHROPIC_MAX_OUTPUT_TOKENS=4096
# ANTHROPIC_HTTP_PROXY=http://127.0.0.1:7890
```

## Seedance / ComfyUI（规划）

Seedance 和 ComfyUI 属于媒体或工作流 Provider，不应直接作为普通聊天模型接入。后续应通过显式工具、成本确认和异步任务状态查询接入。

```env
SEEDANCE_API_KEY=
SEEDANCE_BASE_URL=

COMFYUI_BASE_URL=
COMFYUI_API_KEY=
```

## 安全与审计

- API key、Authorization header、OAuth token 和 provider 错误会经过脱敏后再记录或展示。
- 模型审计只记录 `provider`、`model`、`latency_ms`、`usage`、消息数量、能力和 token 上限。
- 审计日志不记录用户消息正文、飞书正文、网页正文或完整 prompt。
- 测试使用 mock provider 或 mock HTTP，不依赖真实 API key 或真实额度。
