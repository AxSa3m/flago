# FCGO

FCGO 是一个 **Python 3.13 + uv 本地飞书工作助手**。它通过飞书长连接机器人接收私聊或群聊 `@机器人` 消息，调用 Gemini 生成回复，并返回飞书；后续扩展为按需读取飞书文档、电子表格、多维表格、网页链接，并通过交互卡片确认后写回。

## 当前实现范围

- Python/uv 项目骨架、配置、日志和 SQLite 本地存储
- 多模型 Provider 协议、运行时路由、Gemini Provider 和 OpenAI-compatible Provider
- 飞书消息路由、长连接 worker 骨架、消息回复客户端
- 飞书 OAuth 回调和 token 持久化骨架
- 飞书文档/表格/多维表格读取，以及普通网页链接正文提取
- Gemini/模型写回提案的飞书交互卡片预览和确认/取消按钮
- 待确认写回动作存储、过期、幂等和执行器接口
- pytest 单元测试与模拟集成测试基础

## 快速开始

```powershell
uv sync
Copy-Item .env.example .env
uv run fcgo serve
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
uv run fcgo doctor
```

更多文档：

- [产品规格](docs/product-spec.md)
- [架构说明](docs/architecture.md)
- [多模型 Provider 架构规划](docs/multi-model-provider-architecture.md)
- [本地部署指南](docs/deployment.md)

## 配置

所有配置通过环境变量或 `.env` 注入。真实密钥不要提交到仓库。

关键变量：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_OAUTH_SCOPES`，用户发送 `/授权` 时申请的飞书 OAuth scope
- `FCGO_DEFAULT_PROVIDER`，当前可选 `gemini`、`openai`、`deepseek`、`qwen`、`doubao`、`minimax` 或本地开发用 `echo`
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
- `FCGO_MAX_MESSAGE_CHARS`，单条飞书消息进入模型前的最大字符数
- `FCGO_MAX_WRITEBACK_CHARS`，单次写回文本/载荷的最大字符数
- `FCGO_MAX_WRITEBACK_CELLS`，单次表格写入允许的最大单元格数量
- `FCGO_WRITEBACK_DEDUPE_WINDOW_SECONDS`，相同用户、目标和内容的写回去重窗口，默认 600 秒
- `FCGO_CARD_ACTION_ACK_TIMEOUT_SECONDS`，卡片确认等待写回完成的最长秒数；超时后先返回“正在执行”，后台继续写回

OpenAI 兼容 Provider 示例：

```env
FCGO_DEFAULT_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_BASE_URL=https://your-openai-compatible-endpoint/v1
DEEPSEEK_MODEL=your-model-name
```

如果只想临时切换当前飞书会话，也可以保持部署默认不变，在飞书中发送：

```text
/模型 使用 deepseek/your-model-name
```

## 飞书用户授权

用户在飞书里发送 `/授权`，机器人会返回一次性 OAuth 链接。授权成功后，FCGO
会把用户 token 保存到本地 SQLite，用于后续按用户权限读取飞书文档、电子表格和多维表格。

注意：飞书资源读取需要同时满足两层权限：

- 用户本人对目标文档/知识库/表格有访问权限。
- 飞书应用已在开发者后台开通对应 API 权限，并且用户通过 `/授权` 授予了这些 OAuth scope。

默认 `FEISHU_OAUTH_SCOPES` 会请求读写 MVP 所需权限：

```text
auth:user.id:read docx:document:readonly docx:document wiki:node:read sheets:spreadsheet:readonly sheets:spreadsheet bitable:app:readonly bitable:app base:record:read base:record:create base:record:update base:record:delete base:field:read base:view:read
```

如果旧授权缺少新加的 scope，例如 `docx:document`、`sheets:spreadsheet`、`bitable:app`、`base:record:create`、`base:record:update` 或 `base:record:delete`，需要用户在飞书中重新发送 `/授权` 并完成授权。

飞书开放平台中的 OAuth 回调地址需要配置为：

```text
{FCGO_BASE_URL}/oauth/feishu/callback
```

## 飞书模型指令

当前已支持在飞书中查看和切换会话模型：

- `/模型 查看`：查看部署默认模型、当前会话覆盖、个人偏好，以及所有支持 Provider 的配置状态。
- `/模型 使用 provider/model`：设置当前会话模型，例如 `/模型 使用 gemini/gemini-2.5-flash` 或 `/模型 使用 deepseek/deepseek-chat`。
- `/模型 默认`：清除当前会话模型覆盖，恢复部署默认。
- `/模型 我的 使用 provider/model`：设置你的个人默认模型，只在私聊中作为默认值使用。
- `/模型 我的 默认`：清除你的个人默认模型。

群聊中优先使用当前群聊/话题的会话级模型配置，不会自动套用某个成员的个人偏好。

`/模型 查看` 会把模型分成 `[可用]` 和 `[待配置]`。待配置项会提示缺少哪些 `.env`
字段；补齐后重启服务即可出现在可用列表，不需要把它设成默认 Provider。

## 飞书机器人菜单

飞书开发者后台可以给机器人配置自定义菜单。FCGO 已支持以下事件 key：

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

建议菜单结构：

```text
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

帮助
- 使用说明
```

菜单点击会设置个人默认模型；文本命令 `/模型 使用 provider/model` 仍可设置当前会话模型。

## 写回确认卡片

普通问答、总结、分析类任务会直接返回文本。只有当用户明确表达写入、创建、更新、追加或发送等写回意图，并且模型生成写回提案时，FCGO 才会先把提案保存为本地 `pending action`，然后向飞书发送交互卡片。卡片会展示模型回复、目标资源、拟执行动作、预览内容、动作 ID 和过期时间，并提供“确认执行”和“取消”按钮。

如果模型在非写回任务中误生成提案，路由会抑制卡片，只返回普通文本，避免打扰用户或造成误操作。

卡片回调地址：

```text
{FCGO_BASE_URL}/callbacks/feishu/card
```

飞书回调会执行操作者校验、过期校验和幂等处理。只有创建该写回提案的用户可以确认或取消；重复点击会返回“该操作已处理”。

当前确认后执行器支持：

- 创建飞书文档并可写入初始文本。
- 向飞书文档末尾或指定 block 后追加文本/blocks；默认不会覆盖原文。
- 写入飞书电子表格：默认追加到已读取内容的下一行；只有链接或请求明确指定范围时才写入指定单元格/范围。
- 创建或更新飞书多维表格记录。
- 向指定飞书会话发送消息。

## 写回撤回

FCGO 会记录已执行写回的结果。当前第一版支持撤回“多维表格新增记录”：当新增记录成功后，系统会保存飞书返回的 `record_id`。用户发送 `/撤回`、`撤回上一次写回` 或 `撤回最近写回` 时，机器人会生成一张新的确认卡片，确认后删除这条多维表格记录。

撤回同样必须点击卡片确认，不会自动执行。撤回多维表新增记录需要 OAuth scope 包含 `base:record:delete`，如果旧授权缺少该权限，请重新发送 `/授权`。

当前暂不自动撤回文档追加、电子表格覆盖和消息发送；这些类型需要保存更完整的旧值或插入位置，后续会逐步扩展。

## 安全默认值

- 默认不持久化飞书正文内容
- 日志和错误信息会对常见密钥字段脱敏
- 资源读取器带有大小限制和截断提示
- 超长消息会在进入模型调用前被拒绝，并提示用户改用文档/表格链接
- 写回执行前会检查文本长度和表格单元格数量，超限会被拦截
- 写回失败会记录脱敏审计事件，并返回权限、授权、参数等可读错误提示
- 写回动作必须先生成待确认动作，再由飞书交互卡片确认后执行
