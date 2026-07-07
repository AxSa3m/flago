<div align="center">

# Flago

**A local Feishu/Lark AI work assistant with configurable Gemini, Anthropic Claude and OpenAI-compatible models.**

**English** | [中文](README.md)

![version](https://img.shields.io/badge/version-v0.1.0-blue)
![license](https://img.shields.io/badge/license-Apache--2.0-green)
![python](https://img.shields.io/badge/Python-3.13+-3776AB)
![runtime](https://img.shields.io/badge/runtime-uv-4B32C3)
![Feishu](https://img.shields.io/badge/platform-Feishu%20%2F%20Lark-00A1E9)

</div>

Flago is a **Python 3.13 + uv local Feishu/Lark work assistant**. The name means
**Feishu/Lark link to Anthropic, Gemini and OpenAI**. The default Chinese assistant name is **飞灵**.

Flago receives private messages or group mentions through a Feishu/Lark bot, reads Feishu Docs, Sheets, Bitable records, Wiki pages, attachments and web links with user authorization, calls configured model providers, and sends the result back to Feishu/Lark.

Flago is designed for users who want a self-hosted assistant with their own Feishu/Lark app, model API keys and local configuration.

## Capabilities

- Local admin console, first-run setup wizard and SQLite storage.
- Feishu/Lark bot long-connection message handling, private chat and group mention replies.
- User OAuth authorization for reading Feishu/Lark resources.
- Reading Feishu Docs, Sheets, Bitable, Wiki, attachments and web pages.
- Configurable Gemini, Anthropic Claude and OpenAI-compatible model providers.
- Short-term chat context, long-term memory, assistant name and profile preferences.
- Confirmed writeback workflow for supported Feishu/Lark resources.

## Quick Start

```powershell
uv sync
Copy-Item .env.example .env
uv run flago serve
```

Start the local service and open the admin console:

```powershell
uv run flago service start --open-admin
```

Check, restart or stop the local service:

```powershell
uv run flago service status
uv run flago service restart
uv run flago service stop
```

Build portable packages:

```powershell
uv run flago package build
uv run flago package build --target all
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
```

Run tests:

```powershell
uv run pytest
```

Check local configuration and external credentials:

```powershell
uv run flago doctor
```

## Documentation

Most detailed documents are currently written in Chinese:

- [Beginner installation guide](docs/beginner-installation-guide.md)
- [User authorization and privacy guide](docs/user-privacy-operations.md)
- [Model provider configuration](docs/model-provider-configuration.md)
- [Local deployment guide](docs/deployment.md)
- [Portable package guide](docs/portable-packaging.md)

## Configuration

All configuration is provided through environment variables or `.env`.
Do not commit real secrets to the repository.

Common variables:

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_OAUTH_SCOPES`
- `FLAGO_OAUTH_ENABLE_OFFLINE_ACCESS`
- `FEISHU_HTTP_PROXY`
- `FEISHU_DOCS_BASE_URL`
- `FLAGO_DEFAULT_PROVIDER`
- `FLAGO_DEFAULT_MODEL`
- `GEMINI_API_KEY`
- `GEMINI_MODEL`
- `OPENAI_*`, `DEEPSEEK_*`, `QWEN_*`, `DOUBAO_*`, `MINIMAX_*`
- `FLAGO_BASE_URL`
- `FLAGO_SQLITE_PATH`
- `FLAGO_ASSISTANT_DEFAULT_NAME`
- `FLAGO_AGENT_MODE`
- `FLAGO_RESOURCE_SEARCH_ENABLED`
- `FLAGO_MEMORY_STORE_RAW_TEXT`
- `FLAGO_WRITEBACK_ENABLED`
- `FLAGO_WRITEBACK_CONFIRMATION_MODE`

OpenAI-compatible provider example:

```env
FLAGO_DEFAULT_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_BASE_URL=https://your-openai-compatible-endpoint/v1
DEEPSEEK_MODEL=your-model-name
```

If you do not want to change the deployment default model, use the Feishu/Lark bot custom menu to set a personal default model. Text chat does not switch models.

## Feishu/Lark Authorization

Users send `/授权` in Feishu/Lark. The bot returns a one-time OAuth link. After authorization, Flago stores the user token in local SQLite and uses it to read Feishu/Lark resources according to that user's permissions.

Resource access requires both:

- The user has permission to access the target Feishu/Lark resource.
- The Feishu/Lark app has the required API permissions and OAuth scopes enabled.

The OAuth callback URL in the Feishu/Lark developer console should be:

```text
{FLAGO_BASE_URL}/oauth/feishu/callback
```

## Privacy Defaults

- Flago does not persist full Feishu/Lark resource content by default.
- Flago does not save full chat logs as long-term memory by default.
- Logs and errors redact common credential fields.
- Resource readers have size limits and truncation notices.
- Feishu/Lark resource search is triggered only when needed and uses result limits.
- Writeback is disabled by default.

See [User authorization and privacy guide](docs/user-privacy-operations.md) for details.

## Bot Menu

Flago supports Feishu/Lark custom menu event keys such as:

```text
flago.assistant.name.view
flago.model.view
flago.model.default
flago.model.use.gemini
flago.model.use.deepseek
flago.auth.start
flago.auth.status
flago.writeback.status
flago.writeback.history
flago.writeback.undo
flago.memory.view
flago.memory.delete
flago.memory.disable
flago.memory.enable
flago.help
flago.admin.open
```

Recommended menu groups:

- Assistant
- Model
- Authorization
- Writeback
- Memory
- Help

## Writeback

When `FLAGO_WRITEBACK_ENABLED=true`, explicit write requests to supported Feishu/Lark resources generate a confirmation card. The model prepares the content; the program performs permission checks and executes only after confirmation unless a direct-write policy is explicitly enabled.

When writeback is disabled, Flago only returns copyable drafts or operation suggestions.

## Third-Party Services and Data Responsibility

Users are responsible for configuring and managing their own Feishu/Lark app, model API keys, media/workflow service endpoints and related third-party services.

Users are responsible for ensuring that:

- They have proper permissions to access and process relevant Feishu/Lark documents, messages, sheets, bitables, files and enterprise data.
- Their use of model providers, APIs, proxies and workflow services complies with the corresponding service terms.
- Input, upload, processing, storage and export of data comply with organizational, regional and industry requirements.
- API keys, app secrets, OAuth tokens, proxy addresses and other credentials are properly protected.

This project does not provide third-party services and is not responsible for third-party service configuration, permission scope, data processing behavior, fees, data leakage or compliance risks caused by user configuration or usage.

## License

Flago is open-sourced under the [Apache License 2.0](LICENSE).

Apache-2.0 allows use, copy, modification, distribution and commercial use, subject to the license requirements on copyright, patent, trademark and notice preservation. The project name, marks and official release notes do not automatically authorize others to impersonate an official version or official service.
