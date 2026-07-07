# 飞灵（FLGO）

飞灵（FLGO） 是一个 **Python 3.13 + uv 本地飞书工作助手**。它通过飞书长连接机器人接收私聊或群聊 `@机器人` 消息，按用户授权读取飞书文档、电子表格、多维表格和网页链接，可调用 Gemini 或OpenAI等模型生成回复，并返回飞书消息。主要解决飞书机器人或Aily配置外部模型时的限制问题，用户可自行配置所需的模型API接口。兼容Anthropic 与OpenAI,Gemini 接口调用格式。

## 当前实现范围

- Python/uv 项目骨架、配置、日志和 SQLite 本地存储
- 多模型 Provider 协议、运行时路由、Gemini Provider 和 OpenAI-compatible Provider
- 媒体/工作流 Provider 接口层和 mock 测试；真实 Seedance、ComfyUI、Coze、Dify adapter 待后续实现
- 飞书消息路由、长连接 worker 骨架、消息回复客户端
- 飞书 OAuth 回调和 token 持久化骨架
- 飞书文档/表格/多维表格读取，以及普通网页链接正文提取
- Agent + Tools 双轨编排基础；默认仍使用 legacy 编排，可通过配置切换到 Agent 模式
- 确认式写回能力；默认关闭，可按策略生成确认卡片、直接执行低风险追加或只返回草稿

## 快速开始

```powershell
uv sync
Copy-Item .env.example .env
uv run flgo serve
```

启动本地服务并打开配置后台：

```powershell
uv run flgo service start --open-admin
```

查看、重启、停止本地服务：

```powershell
uv run flgo service status
uv run flgo service restart
uv run flgo service stop
```

生成便携启动包：

```powershell
uv run flgo package build
uv run flgo package build --target all
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
```

运行测试：

```powershell
uv run pytest
```

检查本地配置和外部凭证：

```powershell
uv run flgo doctor
```

更多文档：

- [小白安装与配置指南](docs/beginner-installation-guide.md)
- [产品规格](docs/product-spec.md)
- [MVP 验收测试矩阵](docs/mvp-acceptance-tests.md)
- [架构说明](docs/architecture.md)
- [用户授权与隐私操作指南](docs/user-privacy-operations.md)
- [上下文读取、用户授权和长期记忆隐私规格](docs/context-privacy-memory.md)
- [助手命名与用户偏好记忆规格](docs/assistant-personalization-memory.md)
- [多模型 Provider 配置模板](docs/model-provider-configuration.md)
- [多模型 Provider 架构规划](docs/multi-model-provider-architecture.md)
- [本地部署指南](docs/deployment.md)
- [便携启动包](docs/portable-packaging.md)

## 配置

所有配置通过环境变量或 `.env` 注入。真实密钥不要提交到仓库。

关键变量：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_OAUTH_SCOPES`，用户发送 `/授权` 时申请的飞书 OAuth scope
- `FCGO_OAUTH_ENABLE_OFFLINE_ACCESS`，可选；飞书后台开通 `offline_access` 后设为 `true`，用于自动刷新用户 access token
- `FEISHU_HTTP_PROXY`，可选；飞书 OpenAPI 调用遇到本地网络或 TLS 问题时填写代理
- `FEISHU_DOCS_BASE_URL`，搜索接口没有返回可打开 URL 时用于拼接飞书资源链接；私有租户可设为 `https://my.feishu.cn`
- `FCGO_DEFAULT_PROVIDER`，当前可选 `gemini`、`openai`、`deepseek`、`qwen`、`doubao`、`minimax`、`claude` 或本地开发用 `echo`
- `FCGO_DEFAULT_MODEL`，可选；为空时使用 Provider 自己的默认模型
- `GEMINI_API_KEY`
- `GEMINI_MODEL`，Gemini Provider 的默认模型；如果设置了 `FCGO_DEFAULT_MODEL` 会被运行时路由覆盖
- `GEMINI_THINKING_BUDGET`，可选；默认不设置，正式运行让 Gemini 使用模型默认思考预算
- `GEMINI_HTTP_PROXY`，可选；当前网络或地区无法访问 Google AI API 时填写本地代理，例如 `http://127.0.0.1:7890`
- `GEMINI_BASE_URL`，可选；使用 Gemini 兼容网关时填写
- OpenAI 兼容 Provider：`OPENAI_*`、`DEEPSEEK_*`、`QWEN_*`、`DOUBAO_*`、`MINIMAX_*`
  各自需要同时设置 `API_KEY`、`BASE_URL` 和 `MODEL` 才会注册
- `FCGO_OPENAI_COMPATIBLE_HTTP_PROXY`，可选；OpenAI 兼容 Provider 共用代理
- `FCGO_BASE_URL`
- `FCGO_SQLITE_PATH`
- `FCGO_ASSISTANT_DEFAULT_NAME`，默认 `飞灵`；用户未设置个人助手名称时使用
- `FCGO_AGENT_MODE`，默认 `legacy`；可设为 `agent` 启用 Agent + Tools JSON parser 编排
- `FCGO_AGENT_MAX_STEPS`，默认 `4`；Agent 模式下单次请求最多工具轮数
- `FCGO_AGENT_TOOL_TIMEOUT_SECONDS`，默认 `30`；Agent 工具默认超时时间
- `FCGO_MAX_MESSAGE_CHARS`，单条飞书消息进入模型前的最大字符数
- `FCGO_RESOURCE_SEARCH_ENABLED`，默认 `true`；私聊中按需搜索用户可见飞书资料
- `FCGO_RESOURCE_SEARCH_RESULT_LIMIT`，默认 `5`；单次飞书资料搜索最多返回的候选数
- `FCGO_RESOURCE_SEARCH_READ_LIMIT`，默认 `3`；单次搜索后最多自动读取的候选数
- `FCGO_CONTEXT_RECENT_MESSAGE_LIMIT`，默认 `50`；最多读取的近期聊天消息数
- `FCGO_CONTEXT_RECENT_TIME_WINDOW_HOURS`，默认 `24`；近期聊天读取时间窗口
- `FCGO_CONTEXT_CACHE_TTL_HOURS`，默认 `24`；聊天原文短期缓存 TTL，到期后清理
- `FCGO_CONTEXT_CACHE_REFRESH_SECONDS`，默认 `60`；同一会话缓存刷新间隔，避免每条消息都调用飞书历史接口
- `FCGO_CONTEXT_INJECT_MESSAGE_LIMIT`，默认 `8`；每次模型请求最多注入的聊天摘录条数
- `FCGO_CONTEXT_MAX_CHARS`，默认 `6000`；每次模型请求注入的聊天上下文字符预算
- `FCGO_MEMORY_STORE_RAW_TEXT`，默认 `false`；长期记忆不得保存完整聊天或飞书资源正文
- `FCGO_WRITEBACK_ENABLED`，默认 `false`；保持关闭时不会生成写回卡片或执行写入
- `FCGO_WRITEBACK_AUTO_EXECUTE_ENABLED`，默认 `false`；开启后用户才能用 `/写回 自动开启`
  启用个人低风险自动写入
- `FCGO_WRITEBACK_CONFIRMATION_MODE`，默认 `always`；可选 `always`、`low_risk_direct`、`draft_only`

OpenAI 兼容 Provider 示例：

```env
FCGO_DEFAULT_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_BASE_URL=https://your-openai-compatible-endpoint/v1
DEEPSEEK_MODEL=your-model-name
```

如果不想修改部署默认模型，可以在飞书机器人自定义菜单中切换个人默认模型；
文本对话不会触发模型切换。

## 飞书用户授权

用户在飞书里发送 `/授权`，机器人会返回一次性 OAuth 链接。授权成功后，飞灵（FLGO）
会把用户 token 保存到本地 SQLite，用于后续按用户权限读取飞书文档、电子表格和多维表格。
后续会自动复用 token；如果启用了 `FCGO_OAUTH_ENABLE_OFFLINE_ACCESS=true` 且飞书后台已开通
`offline_access`，还会在 access token 过期时自动刷新。

注意：飞书资源读取需要同时满足两层权限：

- 用户本人对目标文档/知识库/表格有访问权限。
- 飞书应用已在开发者后台开通对应 API 权限，并且用户通过 `/授权` 授予了这些 OAuth scope。

默认 `FEISHU_OAUTH_SCOPES` 只请求读取所需权限：

```text
auth:user.id:read drive:drive.search:readonly search:docs:read docx:document:readonly docx:document docs:document.media:download wiki:node:read wiki:wiki:readonly sheets:spreadsheet:readonly sheets:spreadsheet bitable:app:readonly bitable:app base:table:read base:record:read base:record:create base:record:update base:record:delete base:field:read base:view:read
```

如果旧授权缺少读取 scope，例如 `drive:drive.search:readonly`、`search:docs:read`、`wiki:wiki:readonly`、`docx:document:readonly`、`docs:document.media:download`、`sheets:spreadsheet:readonly`、`bitable:app:readonly`、`base:table:read`、`base:record:read`、`base:field:read` 或 `base:view:read`，需要用户在飞书中重新发送 `/授权` 并完成授权。

飞书文档和知识库页面中的内嵌对象按以下规则读取：

- PDF 默认提取前 2 页，也可在问题中指定“第 3 页”。
- DOCX、XLSX、PPTX、TXT、Markdown、CSV、JSON、HTML 和常见源码文件提取可读文本。
- 图片默认返回格式、尺寸和飞书文档中的图片描述；开启 `FCGO_ATTACHMENT_OCR_ENABLED=true`
  且本机安装 Tesseract 后，会对图片执行本地 OCR。
- 音频、视频、压缩包、旧版 Office 和可执行文件只返回类型与安全说明，不执行、不解压、不转码。
- 普通超链接、`@文档` 和内嵌网页返回飞书 blocks API 提供的显示文本、标题与 URL。

飞书不会通过 blocks API 统一返回任意外链的预览正文。飞灵（FLGO） 不会把链接显示文本当成已经读取的网页内容，
也不会自动递归抓取文档中的所有外链。普通网页读取只在用户或 Agent 明确读取外链时触发，并会拦截本机、
内网和保留地址；可通过 `FCGO_WEB_READ_ENABLED`、`FCGO_WEB_ALLOWED_HOSTS`、`FCGO_WEB_BLOCKED_HOSTS`
和 `FCGO_WEB_MAX_BYTES` 控制读取范围。

可发送 `/授权 状态` 检查当前用户是否已授权、是否缺少 scope，以及是否需要重新授权。

飞书开放平台中的 OAuth 回调地址需要配置为：

```text
{FCGO_BASE_URL}/oauth/feishu/callback
```

## 飞书隐私控制指令

当前上下文默认开启，已支持用户可见的助手名称和记忆控制入口：

- `/帮助`：查看可用命令；没有配置自定义菜单时也可使用。
- `/助手 名称`：查看当前助手名称。
- `/助手 命名 小飞`：设置当前用户的个人助手名称。
- `/助手 默认名称`：恢复默认助手名称，默认是 `飞灵`。
- `/上下文 查看`：查看当前上下文策略；不会读取历史、不会调用模型。
- `/记忆 查看`：查看当前用户的长期记忆摘要和偏好。
- `/记忆 记住 输出格式=优先表格`：新增或更新一条带 key 的偏好记忆。
- `/记忆 记住 输出尽量用表格`：新增或更新通用偏好，可用 `/记忆 删除 偏好` 删除。
- `/记忆 修改 语言风格=简洁中文`：修改指定偏好。
- `/记忆 删除 语言风格`：按 key 删除一条记忆。
- `/记忆 删除` 或 `/记忆 清空`：经确认后清空当前用户的长期记忆，记忆功能状态保持不变。
- `/记忆 关闭`：暂停当前用户的长期记忆；已有记忆保留，但不会新增或进入模型上下文。
- `/记忆 开启`：重新开启当前用户的长期记忆。

记忆管理命令只允许在私聊中操作，避免个人记忆展示到群聊。
普通聊天中识别到“记住我叫…”“我的项目代号是…”这类候选记忆时，飞灵（FLGO） 会先发送确认卡片；用户点击“保存”后才写入长期记忆。
飞灵（FLGO） 默认不会保存完整聊天原文、飞书资源正文、网页正文或附件正文。隐私边界见
[上下文读取、用户授权和长期记忆隐私规格](docs/context-privacy-memory.md)。

当前会话聊天历史读取使用应用权限 `im:message:readonly` 和 tenant token，不依赖用户 OAuth。
飞灵（FLGO） 会把最近聊天做短期 TTL 缓存，并在每次请求前把近期窗口内的消息按时间顺序作为
全文上下文注入；超过近期窗口或字符预算的旧消息会压缩成当前会话滚动摘要。长期记忆开启时，
私聊会话摘要会作为用户可查看、可删除的 `会话摘要` 记忆保存；不会保存完整聊天原文。

私聊中没有显式链接、且用户明确要求搜索/读取飞书文档、表格、多维表或资料时，飞灵（FLGO） 会使用
飞书“搜索云文档”和“搜索 Wiki”接口按用户 OAuth 搜索本人可见资源，只读取前几个支持的命中项进入本次模型请求。
如果搜索响应包含可打开 URL，会直接使用飞书返回的地址；否则使用 `FEISHU_DOCS_BASE_URL`
和资源 token 生成链接。系统不全量扫描云空间，不建立长期索引，也不会把搜索关键词正文或资源正文写入审计日志。

文档内嵌图片默认只返回图片元数据；如需让多模态模型直接理解图片，可设置
`FCGO_ATTACHMENT_VISION_ENABLED=true`，并使用支持视觉输入的 Provider，例如 `gemini`。
视觉理解失败时会回退到本地 OCR 或图片元数据。
音频和视频附件默认只返回安全说明；如需让多模态模型转写或理解内容，可设置
`FCGO_ATTACHMENT_MEDIA_UNDERSTANDING_ENABLED=true`，并使用支持音视频输入的 Provider。

## 飞书模型指令

当前模型切换以飞书机器人自定义菜单为准。文本命令只保留只读查看：

- `/模型 查看`：查看当前实际使用的模型、可用模型，以及尚未启用的 Provider。

菜单点击会设置点击者的个人默认模型；私聊会优先使用该个人偏好。文本对话中出现
“使用某模型”等内容只会作为普通消息处理，不会触发模型切换。

`/模型 查看` 会直接显示当前实际使用的模型。可用模型中会标记部署默认项；
尚未启用的 Provider 只列名称，不展示密钥或环境变量细节。

## 写回历史与撤回

启用写回实验线后，每次成功执行都会记录操作类型、目标、执行结果和可撤回元数据：

- `/写入 状态`：查看当前写入策略、个人自动写入偏好和服务开关。
- `/写入 自动开启`：在服务允许时开启个人低风险自动写入。仅明确目标的文档开头/末尾追加会自动执行。
- `/写入 自动关闭`：关闭个人自动写入，后续恢复确认卡片。
- `/写入 自动清除`：清除个人偏好，使用服务默认策略。
- `/查看最近写入`：查看最近 5 次写入及其“可撤回 / 已撤回 / 不可撤回”状态。
- `/撤回`：为最近一次可安全撤回的写入生成确认卡片，不会自动执行。
- 多维表新增记录可通过删除新记录撤回。
- 电子表格范围写入会在写入前保存同范围旧值，撤回时恢复旧值。
- 文档创建、文档追加、消息发送和未保存旧值的更新/删除操作会明确标记为不可安全撤回。

同一写回只能撤回一次。撤回仍执行权限检查、幂等保护和审计，不会生成无限“撤回撤回”链。

## 飞书机器人菜单

飞书开发者后台可以给机器人配置自定义菜单。飞灵（FLGO） 已支持以下事件 key：

```text
flgo.assistant.name.view 查看助手信息
flgo.assistant.info.view 查看助手信息（兼容 key）
flgo.model.view          查看模型状态
flgo.model.default       恢复个人默认模型
flgo.model.use.gemini    使用 Gemini
flgo.model.use.deepseek  使用 DeepSeek
flgo.model.use.openai    使用 OpenAI
flgo.model.use.qwen      使用 Qwen
flgo.model.use.doubao    使用 Doubao
flgo.model.use.minimax   使用 Minimax
flgo.model.use.claude    使用 Claude
flgo.auth.start          发起飞书授权
flgo.auth.status         查看授权状态
flgo.context.view        查看上下文策略
flgo.context.enable      兼容入口：上下文默认开启
flgo.context.disable     兼容入口：上下文默认开启
flgo.writeback.status    查看写入策略
flgo.writeback.auto.enable   开启个人自动写入
flgo.writeback.auto.disable  关闭个人自动写入
flgo.writeback.auto.clear    清除个人自动写入偏好
flgo.writeback.history       查看最近写入
flgo.writeback.undo          撤回最近写入，需要确认
flgo.memory.view         查看长期记忆
flgo.memory.delete       清空长期记忆，需要确认
flgo.memory.disable      关闭长期记忆
flgo.memory.enable       开启长期记忆
flgo.help                查看帮助
flgo.admin.open          打开本地配置网页
```

建议菜单结构：

```text
助手
- 助手信息

模型
- 查看模型
- Gemini
- DeepSeek
- OpenAI
- Qwen
- Doubao
- Minimax
- 恢复默认

授权
- 飞书授权
- 授权状态

写入
- 写入状态
- 自动写入开启
- 自动写入关闭
- 写入恢复默认
- 最近写入
- 撤回

记忆
- 查看记忆
- 删除记忆（清空全部）
- 关闭记忆
- 开启记忆

帮助
- 使用说明
- 本地配置网页
```

菜单点击会设置个人默认模型；文本命令不会修改模型偏好。
“本地配置网页”会打开 `/admin`，首次打开需要使用飞书登录；首次成功登录的飞书用户会绑定为本机后台管理员。

## 写入功能

`FCGO_WRITEBACK_ENABLED=true` 时，明确指向飞书文档、电子表格、多维表格或消息的写入请求
会生成确认卡片。模型只准备写入内容，不能直接声称已经执行；实际写入必须经过权限检查和用户确认。

设置为 `false` 时，系统不会创建写回卡片或执行写入，只返回可复制草稿或操作建议。

## 安全默认值

- 默认不持久化飞书正文内容
- 默认开启当前会话上下文读取；未授权或读取失败时降级为无历史上下文继续回答
- 默认不把完整聊天、飞书资源、网页或附件正文写入长期记忆
- 日志和错误信息会对常见密钥字段脱敏
- 资源读取器带有大小限制和截断提示
- 飞书资料搜索按需触发，带候选数和读取数上限
- 超长消息会在进入模型调用前被拒绝，并提示用户改用文档/表格链接
- 写入功能默认关闭，避免误操作飞书资料
