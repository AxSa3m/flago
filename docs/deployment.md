# 本地部署指南

本指南以 Windows PowerShell、Python 3.13 和 uv 为基准。首次部署建议先完成最小直连配置，再逐项开启代理、兼容网关、其他模型或自动续期。

## 1. 环境准备

检查 Python：

```powershell
py -3.13 --version
```

预期输出为 `Python 3.13.x`。如果没有 Python 3.13，请先通过
[Python 官方 Windows 下载页](https://www.python.org/downloads/windows/) 安装。

安装或更新 uv：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv --version
```

安装命令来自 [uv 官方安装文档](https://docs.astral.sh/uv/getting-started/installation/)。

进入项目目录并安装依赖：

```powershell
cd D:\CodingSpace\feishuGemini\fcgo
uv sync
```

## 2. 准备配置

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，填入：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_OAUTH_SCOPES`
- `FEISHU_HTTP_PROXY`：可选；飞书 OpenAPI 调用遇到本地网络或 TLS 问题时填写本地代理，例如 `http://127.0.0.1:7890`
- `GEMINI_API_KEY`
- `GEMINI_MODEL`
- `GEMINI_HTTP_PROXY`：可选；当前网络或地区无法访问 Google AI API 时填写本地代理，例如 `http://127.0.0.1:7890`
- `GEMINI_BASE_URL`：可选；使用 Gemini 兼容网关时填写
- `FCGO_DEFAULT_PROVIDER` / `FCGO_DEFAULT_MODEL`：可选；用于选择部署默认 Provider 和模型
- OpenAI 兼容 Provider：如需使用 OpenAI、DeepSeek、Qwen、Doubao 或 Minimax，填入对应
  `API_KEY`、`BASE_URL`、`MODEL`
- `FCGO_BASE_URL`
- `FCGO_START_LONG_CONNECTION=true`
- `FCGO_AGENT_MODE=legacy`：默认保持当前稳定编排；测试 Agent + Tools 时改为 `agent`
- `FCGO_WRITEBACK_ENABLED=false`：默认关闭写回；测试确认式写回时改为 `true`
- `FCGO_WRITEBACK_AUTO_EXECUTE_ENABLED=false`：默认不允许用户开启低风险自动写入；需要该能力时改为 `true`
- `FCGO_WRITEBACK_CONFIRMATION_MODE=always`：写回确认策略，可选 `always`、`low_risk_direct`、`draft_only`

最小 Gemini 直连配置示例：

```env
FCGO_ENV=prod
FCGO_BASE_URL=https://fcgo.example.com
FCGO_START_LONG_CONNECTION=true
FCGO_DEFAULT_PROVIDER=gemini
FCGO_AGENT_MODE=legacy

FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx

GEMINI_API_KEY=xxx
GEMINI_MODEL=gemini-2.5-flash
GEMINI_HTTP_PROXY=
GEMINI_BASE_URL=

FCGO_WRITEBACK_ENABLED=false
FCGO_WRITEBACK_AUTO_EXECUTE_ENABLED=false
FCGO_WRITEBACK_CONFIRMATION_MODE=always
```

`FCGO_BASE_URL` 必须填写部署服务的外部访问根地址，不要包含
`/oauth/feishu/callback` 路径。OAuth 完整回调地址由程序自动拼接。

### Gemini 网络路径

Gemini 支持三种部署路径。选择顺序如下：

1. **默认直连**：`GEMINI_HTTP_PROXY` 和 `GEMINI_BASE_URL` 都留空。首次部署必须先尝试此路径。
2. **HTTP 代理**：只有直连出现地区、TLS 或网络不可达错误时，才设置 `GEMINI_HTTP_PROXY`。
3. **兼容网关**：只有明确使用 Gemini 兼容转发服务时，才设置 `GEMINI_BASE_URL`。

推荐把三种方式视为互斥配置，不要同时设置 `GEMINI_HTTP_PROXY` 和
`GEMINI_BASE_URL`。如果两者同时存在，程序会访问自定义 base URL，并让请求继续经过代理；
这种组合只适用于网关本身也必须通过代理访问的特殊网络，普通部署不建议使用。

默认直连：

```env
GEMINI_HTTP_PROXY=
GEMINI_BASE_URL=
```

代理路径：

```env
GEMINI_HTTP_PROXY=http://127.0.0.1:7890
GEMINI_BASE_URL=
```

兼容网关路径：

```env
GEMINI_HTTP_PROXY=
GEMINI_BASE_URL=https://your-gemini-compatible-gateway.example.com
```

每次修改后先运行：

```powershell
uv run fcgo doctor
```

当 `FCGO_DEFAULT_PROVIDER=gemini` 时，doctor 会执行一个轻量 Gemini 请求。只有默认直连报告
网络、地区或 TLS 错误时，才切换到代理或兼容网关。API Key、额度或权限错误不能通过代理解决。
doctor 通过后再重启正式服务。

OpenAI 兼容 Provider 只有在同一组 `API_KEY`、`BASE_URL`、`MODEL` 都填写时才会注册。
例如：

```env
FCGO_DEFAULT_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_BASE_URL=https://your-openai-compatible-endpoint/v1
DEEPSEEK_MODEL=your-model-name
```

如果你所在网络访问某个 OpenAI 兼容接口也需要代理，可以配置：

```env
FCGO_OPENAI_COMPATIBLE_HTTP_PROXY=http://127.0.0.1:7890
```

## 3. 飞书应用配置

在飞书开放平台创建自建应用，并启用机器人能力。

需要配置：

- 在事件与回调中选择“使用长连接接收事件”。
- 订阅消息接收事件 `im.message.receive_v1`。
- 订阅机器人自定义菜单事件 `application.bot.menu_v6`。
- 记忆保存/删除确认卡片需要启用卡片交互回调；当前 worker 通过长连接处理卡片操作。
- 开通聊天历史读取权限 `im:message:readonly`，用于当前会话近期上下文。
- 应用权限管理中开通 OAuth 读取权限，例如 `docx:document:readonly`、`docs:document.media:download`、`wiki:node:read`、`sheets:spreadsheet:readonly`、`bitable:app:readonly`、`base:table:read`、`base:record:read`、`base:field:read`、`base:view:read`。
- 如需自动刷新用户 access token，需要在飞书开发者后台开通 `offline_access`，然后设置 `FCGO_OAUTH_ENABLE_OFFLINE_ACCESS=true`。未开通时不要开启，否则飞书授权页会报 `20027` 应用权限不足。
- OAuth 回调地址：`{FCGO_BASE_URL}/oauth/feishu/callback`

写回和记忆确认卡片均通过长连接接收卡片操作。启用写回时设置
`FCGO_WRITEBACK_ENABLED=true`；关闭时不会创建或执行写回。

### 暴露 OAuth 回调

飞书 OAuth 浏览器回调不能访问 `127.0.0.1` 或仅局域网可见的地址。即使机器人消息使用长连接，
OAuth 仍需要一个浏览器能够访问的公网 HTTPS 地址。

部署时需要使用以下任一方式：

- 将 FCGO 部署到带 HTTPS 域名的服务器。
- 使用反向代理把公网 HTTPS 域名转发到 `127.0.0.1:8000`。
- 本地测试时使用可信的 HTTPS 隧道服务转发到 `127.0.0.1:8000`。

例如外部地址是 `https://fcgo.example.com`：

```env
FCGO_BASE_URL=https://fcgo.example.com
```

飞书开发者后台登记的回调地址必须完全一致：

```text
https://fcgo.example.com/oauth/feishu/callback
```

更换域名、端口或协议后，需要同时更新 `.env` 和飞书开发者后台配置，并重启服务。

### 机器人自定义菜单

在飞书开放平台的机器人能力中开启自定义菜单。菜单项选择事件类型，并按下面的事件 key
配置：

```text
fcgo.assistant.name.view 查看助手信息
fcgo.model.view          查看模型状态
fcgo.model.default       恢复个人默认模型
fcgo.model.use.gemini    使用 Gemini
fcgo.model.use.deepseek  使用 DeepSeek
fcgo.model.use.openai    使用 OpenAI
fcgo.model.use.qwen      使用 Qwen
fcgo.model.use.doubao    使用 Doubao
fcgo.model.use.minimax   使用 Minimax
fcgo.model.use.claude    使用 Claude
fcgo.auth.start          发起飞书授权
fcgo.auth.status         查看授权状态
fcgo.context.view        查看上下文策略
fcgo.writeback.status    查看写入策略
fcgo.writeback.auto.enable   开启个人自动写入
fcgo.writeback.auto.disable  关闭个人自动写入
fcgo.writeback.auto.clear    清除个人自动写入偏好
fcgo.writeback.history       查看最近写入
fcgo.writeback.undo          撤回最近写入，需要确认
fcgo.memory.view         查看长期记忆
fcgo.memory.delete       清空长期记忆，需要确认
fcgo.memory.disable      关闭长期记忆
fcgo.memory.enable       开启长期记忆
fcgo.help                查看帮助
```

菜单事件需要订阅 `application.bot.menu_v6`，并确保长连接事件接收已启用。菜单点击后，
FCGO 会通过 open_id 给操作者发送结果；模型菜单设置的是该用户的个人默认模型。
文本对话不会触发模型切换；`/模型 查看` 仅用于查看当前配置。
记忆菜单作用于点击菜单的用户，不会查看或修改其他用户的长期记忆。
菜单里的 `fcgo.memory.delete` 会发送清空全部长期记忆的确认卡片；按 key 删除单条记忆请使用文本命令。
写入菜单作用于点击菜单的用户；自动写入只影响该用户个人偏好，不会影响其他用户。

用户授权入口：

- 用户在飞书中向机器人发送 `/授权`。
- 机器人返回一次性 OAuth 链接。
- 用户在浏览器完成授权后，回调会保存该用户的 token。
- 用户可发送 `/授权 状态` 或点击授权状态菜单，检查 token 是否可用、是否缺少 scope。
- 授权 state 默认 10 分钟过期，可通过 `FCGO_OAUTH_STATE_TTL_SECONDS` 调整。

飞书资源读取以用户授权为准，但不是“只要用户能看就一定能读”。它需要同时满足：

- 用户本人对目标文档、知识库、表格或多维表格有访问权限。
- 自建应用在开发者后台已经开通对应 API 权限。
- 用户最近一次 `/授权` 已授予 `FEISHU_OAUTH_SCOPES` 中配置的 scope。

如果之前已经授权过，但当时缺少新加的读取 scope，需要重新发送 `/授权` 获取新的 scope。默认配置：

```env
FEISHU_OAUTH_SCOPES=auth:user.id:read drive:drive.search:readonly search:docs:read docx:document:readonly docx:document docs:document.media:download wiki:node:read wiki:wiki:readonly sheets:spreadsheet:readonly sheets:spreadsheet bitable:app:readonly bitable:app base:table:read base:record:read base:record:create base:record:update base:record:delete base:field:read base:view:read
FCGO_OAUTH_ENABLE_OFFLINE_ACCESS=false
```

资料搜索和表格读取默认按安全上限执行，避免把过多飞书内容一次性塞进模型上下文：

```env
FEISHU_DOCS_BASE_URL=https://my.feishu.cn
FCGO_RESOURCE_SEARCH_ENABLED=true
FCGO_RESOURCE_SEARCH_RESULT_LIMIT=5
FCGO_RESOURCE_SEARCH_READ_LIMIT=3
FCGO_MAX_SHEET_ROWS=200
FCGO_DOC_BLOCK_SCAN_LIMIT=1000
FCGO_EMBEDDED_FILE_LIMIT=3
FCGO_EMBEDDED_FILE_MAX_BYTES=20971520
FCGO_EMBEDDED_FILE_MAX_CHARS=40000
FCGO_ATTACHMENT_OCR_ENABLED=false
FCGO_ATTACHMENT_OCR_COMMAND=tesseract
FCGO_ATTACHMENT_OCR_LANGUAGES=chi_sim+eng
FCGO_ATTACHMENT_OCR_TIMEOUT_SECONDS=15
FCGO_ATTACHMENT_OCR_MAX_PIXELS=20000000
FCGO_EMBEDDED_LINK_LIMIT=20
FCGO_PDF_DEFAULT_PAGES=2
FCGO_PDF_MAX_PAGES=10
FCGO_PDF_EXTRACT_TIMEOUT_SECONDS=20
FCGO_MAX_SHEET_COLUMNS=26
FCGO_WEB_TIMEOUT_SECONDS=20
```

内嵌对象读取说明：

- `FCGO_EMBEDDED_FILE_LIMIT` 限制单篇文档下载和检查的文件/图片数量。
- `FCGO_EMBEDDED_FILE_MAX_BYTES` 限制单个素材下载体积。
- `FCGO_EMBEDDED_FILE_MAX_CHARS` 限制单个附件注入模型上下文的文本长度。
- `FCGO_ATTACHMENT_OCR_ENABLED` 默认关闭。设为 `true` 后，图片附件会调用本机
  `FCGO_ATTACHMENT_OCR_COMMAND` 指向的 Tesseract 命令做 OCR；如果本机未安装或超时，会保留图片元数据并返回原因。
- `FCGO_ATTACHMENT_OCR_LANGUAGES` 默认 `chi_sim+eng`，需要本机 Tesseract 已安装对应语言包。
- `FCGO_ATTACHMENT_OCR_TIMEOUT_SECONDS` 和 `FCGO_ATTACHMENT_OCR_MAX_PIXELS` 用于限制 OCR 成本和大图风险。
- `FCGO_EMBEDDED_LINK_LIMIT` 限制单篇文档列出的超链接、`@文档` 和内嵌网页数量。
- Office Open XML 文件在解析前检查压缩包条目数和解压后体积，避免压缩炸弹。
- 可执行文件不会执行，压缩包不会解压，脚本文件只按纯文本读取。

私聊中没有显式链接、且用户明确要求搜索/读取飞书文档、表格、多维表、知识库或资料时，FCGO 会按用户 OAuth
搜索本人可见云文档和 Wiki，并只读取前几个支持的命中项。搜索不会全量扫描云空间，也不会把资源正文保存到
SQLite 或审计 detail。如果搜索响应包含可打开 URL，会直接使用飞书返回的地址；否则使用
`FEISHU_DOCS_BASE_URL` 和资源 token 生成链接。

普通网页链接会按 `FCGO_WEB_TIMEOUT_SECONDS` 抓取，并只读取 `text/html`、`text/plain`
和 `application/xhtml+xml` 这类可读文本内容；二进制或过大的内容会被安全跳过。

上下文与记忆隐私默认值：

```env
FCGO_CONTEXT_RECENT_MESSAGE_LIMIT=50
FCGO_CONTEXT_RECENT_TIME_WINDOW_HOURS=24
FCGO_CONTEXT_CACHE_TTL_HOURS=24
FCGO_CONTEXT_CACHE_REFRESH_SECONDS=60
FCGO_CONTEXT_INJECT_MESSAGE_LIMIT=8
FCGO_CONTEXT_MAX_CHARS=6000
FCGO_MEMORY_STORE_RAW_TEXT=false
FCGO_MEMORY_ITEM_MAX_CHARS=2000
FCGO_MEMORY_CONTEXT_MAX_CHARS=4000
```

默认开启当前会话上下文读取；未授权或读取失败时会降级为无历史上下文继续回答。系统不会把完整聊天、飞书资源、网页或附件正文保存为长期记忆。详细边界见
[上下文读取、用户授权和长期记忆隐私规格](context-privacy-memory.md)。

当前会话聊天历史读取使用应用权限 `im:message:readonly` 和 tenant token，不依赖用户 OAuth。
FCGO 会短期缓存最近聊天原文，用于减少重复飞书 API 调用；缓存按 TTL 自动清理。
每次模型请求会优先注入近期全文消息，并受
`FCGO_CONTEXT_INJECT_MESSAGE_LIMIT` 和 `FCGO_CONTEXT_MAX_CHARS` 限制。
超过近期窗口或字符预算的旧消息会压缩成当前会话滚动摘要。长期记忆开启时，私聊会话摘要会作为用户可查看、可删除的 `会话摘要` 记忆保存；聊天缓存不会写入长期记忆，系统也不会保存完整聊天原文。
单条长期记忆和每次注入模型上下文的长期记忆总量分别受 `FCGO_MEMORY_ITEM_MAX_CHARS` 和 `FCGO_MEMORY_CONTEXT_MAX_CHARS` 限制。

用户可在飞书中使用以下隐私控制指令：

```text
/帮助
/助手 名称
/助手 命名 小飞
/助手 默认名称
/授权
/上下文 查看
/记忆 查看
/记忆 记住 输出格式=优先表格
/记忆 记住 输出尽量用表格
/记忆 修改 语言风格=简洁中文
/记忆 删除 语言风格
/记忆 删除
/记忆 清空
/记忆 关闭
/记忆 开启
```

`/记忆 关闭` 只暂停长期记忆，不清空已有记忆；`/记忆 删除` 或 `/记忆 清空`
会通过确认卡片清空长期记忆。`/记忆 删除 语言风格` 这类带 key 的命令只删除单条记忆。
记忆管理命令只允许在私聊中操作，避免个人记忆展示到群聊。
普通聊天中识别到“记住我叫…”“我的项目代号是…”这类候选记忆时，会先发送确认卡片；用户点击“保存”后才写入长期记忆。

## 4. 启动前检查

运行：

```powershell
uv run fcgo doctor
```

doctor 会检查：

- 飞书 App ID 和 App Secret 是否存在。
- 是否可以获取飞书 tenant token。
- 默认模型 Provider 是否已注册。
- 默认 Provider 为 Gemini 时，是否可以完成轻量 Gemini 请求。

如果 doctor 失败，先修复配置再启动长连接。doctor 不会修改 `.env`、飞书后台配置或用户授权。

## 5. 本地启动

仅启动 HTTP 服务：

```powershell
uv run fcgo serve
```

启动 HTTP 服务并同时启动飞书长连接 worker：

```powershell
uv run fcgo serve
```

前提是 `.env` 已设置：

```env
FCGO_START_LONG_CONNECTION=true
```

只启动长连接 worker：

```powershell
uv run fcgo worker
```

不要同时运行多个 `fcgo worker` 或多个启用了长连接的 `fcgo serve`，否则可能重复消费飞书事件。

## 6. 健康检查

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
```

预期：

```json
{"status":"ok"}
```

检查端口：

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen
```

如果通过重定向日志的后台命令启动，可以查看：

```powershell
Get-Content .\server.err.log -Tail 80
```

否则直接查看运行 `uv run fcgo serve` 的终端输出。

## 7. 部署后验证

按以下顺序验证：

1. 飞书私聊机器人发送 `/帮助`，确认长连接消息可达。
2. 发送 `/授权 状态`，确认机器人返回授权状态。
3. 发送 `/授权`，点击按钮并完成 OAuth；成功页应提示 5 秒后自动关闭。
4. 发送 `/模型 查看`，确认默认模型显示为可用。
5. 发送普通问题，确认模型可以回复。
6. 发送一个本人有权限的飞书文档链接，确认可以读取。
7. 在群聊中不 `@机器人` 发送消息，应无回复；`@机器人` 后应正常回复。

## 8. 测试

```powershell
uv run pytest -q
uv run ruff check .
uv run mypy
```

## 9. 常见问题

- `uv` 找不到：关闭并重新打开 PowerShell，或确认 uv 安装目录已经加入 `PATH`。
- Python 版本错误：运行 `py -3.13 --version`，确认项目使用 Python 3.13。
- doctor 报 Gemini 地区或网络错误：先保持 `GEMINI_BASE_URL` 为空，只设置 `GEMINI_HTTP_PROXY` 后重试。
- doctor 报 Gemini API Key、额度或权限错误：检查 Key 和额度；不要通过切换代理掩盖认证问题。
- 兼容网关无法访问：清空 `GEMINI_HTTP_PROXY`，确认 `GEMINI_BASE_URL` 是网关要求的 API 根地址。
- OAuth 回调打不开：确认 `FCGO_BASE_URL` 是公网 HTTPS 根地址，且飞书后台登记了完全相同的回调 URL。
- OAuth 成功后仍缺权限：确认飞书后台已开通并发布新 scope，然后让用户重新发送 `/授权`。
- 模型无法访问：先确认当前 `FCGO_DEFAULT_PROVIDER` 是否已注册；OpenAI 兼容 Provider 检查对应 `API_KEY`、`BASE_URL`、`MODEL` 以及 `FCGO_OPENAI_COMPATIBLE_HTTP_PROXY`；Claude 检查 `ANTHROPIC_*` 配置。
- 飞书消息收不到：检查应用是否启用机器人和长连接事件订阅。
- 私有文档无法读取：先确认用户本人能打开文档；再确认开发者后台已开通对应 API 权限；最后让用户重新发送 `/授权`，确保 token 包含文档读取 scope。旧 token 只包含 `auth:user.id:read` 时无法读取文档正文。
- 写入请求没有卡片：当前分支默认暂停写入，这是预期行为；机器人应返回草稿或操作建议。
