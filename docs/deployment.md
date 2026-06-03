# 本地部署指南

## 1. 安装依赖

```powershell
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
- `GEMINI_API_KEY`
- `GEMINI_MODEL`
- `GEMINI_HTTP_PROXY`：可选；当前网络或地区无法访问 Google AI API 时填写本地代理，例如 `http://127.0.0.1:7890`
- `GEMINI_BASE_URL`：可选；使用 Gemini 兼容网关时填写
- `FCGO_DEFAULT_PROVIDER` / `FCGO_DEFAULT_MODEL`：可选；用于选择部署默认 Provider 和模型
- OpenAI 兼容 Provider：如需使用 OpenAI、DeepSeek、Qwen、Doubao 或 Minimax，填入对应
  `API_KEY`、`BASE_URL`、`MODEL`
- `FCGO_BASE_URL`

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

- 机器人消息事件订阅
- 长连接事件接收
- 应用权限管理中开通 OAuth 读写权限，例如 `docx:document:readonly`、`docx:document`、`wiki:node:read`、`sheets:spreadsheet:readonly`、`sheets:spreadsheet`、`bitable:app:readonly`、`bitable:app`、`base:record:read`、`base:record:create`、`base:record:update`、`base:record:delete`、`base:field:read`、`base:view:read`
- OAuth 回调地址：`{FCGO_BASE_URL}/oauth/feishu/callback`
- 交互卡片回调地址：`{FCGO_BASE_URL}/callbacks/feishu/card`

交互卡片回调用于处理“确认执行”和“取消”按钮。FCGO 会校验点击人是否为创建该
pending action 的用户，并通过飞书事件 ID 做幂等处理，避免重复点击或飞书重试导致重复执行。

### 机器人自定义菜单

在飞书开放平台的机器人能力中开启自定义菜单。菜单项选择事件类型，并按下面的事件 key
配置：

```text
fcgo.model.view          查看模型状态
fcgo.model.default       恢复个人默认模型
fcgo.model.use.gemini    使用 Gemini
fcgo.model.use.deepseek  使用 DeepSeek
fcgo.model.use.openai    使用 OpenAI
fcgo.model.use.qwen      使用 Qwen
fcgo.model.use.doubao    使用 Doubao
fcgo.model.use.minimax   使用 Minimax
fcgo.auth.start          发起飞书授权
fcgo.help                查看帮助
```

菜单事件需要订阅 `application.bot.menu_v6`，并确保长连接事件接收已启用。菜单点击后，
FCGO 会通过 open_id 给操作者发送结果；模型菜单设置的是该用户的个人默认模型。

用户授权入口：

- 用户在飞书中向机器人发送 `/授权`。
- 机器人返回一次性 OAuth 链接。
- 用户在浏览器完成授权后，回调会保存该用户的 token。
- 授权 state 默认 10 分钟过期，可通过 `FCGO_OAUTH_STATE_TTL_SECONDS` 调整。

飞书资源读取以用户授权为准，但不是“只要用户能看就一定能读”。它需要同时满足：

- 用户本人对目标文档、知识库、表格或多维表格有访问权限。
- 自建应用在开发者后台已经开通对应 API 权限。
- 用户最近一次 `/授权` 已授予 `FEISHU_OAUTH_SCOPES` 中配置的 scope。

如果之前已经授权过，但当时缺少新加的读取 scope，需要重新发送 `/授权` 获取新的 scope。默认配置：

```env
FEISHU_OAUTH_SCOPES=auth:user.id:read docx:document:readonly docx:document wiki:node:read sheets:spreadsheet:readonly sheets:spreadsheet bitable:app:readonly bitable:app base:record:read base:record:create base:record:update base:record:delete base:field:read base:view:read
```

表格读取默认按安全上限读取，避免把整张大表一次性塞进模型上下文：

```env
FCGO_MAX_SHEET_ROWS=200
FCGO_MAX_SHEET_COLUMNS=26
FCGO_WEB_TIMEOUT_SECONDS=20
```

普通网页链接会按 `FCGO_WEB_TIMEOUT_SECONDS` 抓取，并只读取 `text/html`、`text/plain`
和 `application/xhtml+xml` 这类可读文本内容；二进制或过大的内容会被安全跳过。

## 4. 本地启动

仅启动 HTTP 服务：

```powershell
uv run fcgo serve
```

启动 HTTP 服务并同时启动飞书长连接 worker：

```powershell
$env:FCGO_START_LONG_CONNECTION="true"
uv run fcgo serve
```

只启动长连接 worker：

```powershell
uv run fcgo worker
```

## 5. 健康检查

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
```

## 6. 测试

```powershell
uv run pytest
uv run ruff check .
```

## 7. 常见问题

- 模型无法访问：先确认当前 `FCGO_DEFAULT_PROVIDER` 是否已注册；Gemini 检查 `GEMINI_API_KEY`、`GEMINI_HTTP_PROXY`、`GEMINI_BASE_URL` 和网络环境；OpenAI 兼容 Provider 检查对应 `API_KEY`、`BASE_URL`、`MODEL` 以及 `FCGO_OPENAI_COMPATIBLE_HTTP_PROXY`。
- 飞书消息收不到：检查应用是否启用机器人和长连接事件订阅。
- 私有文档无法读取：先确认用户本人能打开文档；再确认开发者后台已开通对应 API 权限；最后让用户重新发送 `/授权`，确保 token 包含文档读取 scope。旧 token 只包含 `auth:user.id:read` 时无法读取文档正文。
- 写回未执行：确认动作是否过期、是否由创建人点击确认、是否已重复处理。
