# Flago（FLAGO） 小白安装与配置指南

这份指南按“照着做”的顺序写。你不需要理解源码结构，只需要完成：

1. 启动本地服务。
2. 在飞书开放平台创建机器人应用。
3. 填入飞书应用信息和模型 Key。
4. 配置机器人菜单。
5. 在飞书里授权并测试。

## 0. 你需要准备什么

- 一台 Windows、macOS 或 Linux 电脑。
- Python 3.13。
- uv。
- 一个飞书账号。
- 一个可用的大模型 API Key，例如 Gemini、DeepSeek、OpenAI、Qwen、Doubao、Minimax 或 Claude。

如果你只是本机自用，服务地址保持 `http://127.0.0.1:8000` 即可。
只有当你要让其他电脑访问这个后台，或部署到服务器时，才需要改成自己的公网 HTTPS 地址。

## 1. 启动 Flago（FLAGO）

### 方式 A：使用便携包

解压 `flago-portable-windows.zip` 后，双击：

```text
start-flago.cmd
```

macOS 或 Linux 解压后，在终端运行：

```bash
./start-flago.sh
```

第一次启动时，程序会自动从 `.env.example` 复制出 `.env`，然后打开本地配置后台。

### 方式 B：从项目目录启动

如果你拿到的是项目目录，打开终端进入项目目录后运行：

```bash
uv sync
uv run flago service start --open-admin
```

浏览器会打开：

```text
http://127.0.0.1:8000/admin
```

如果是第一次使用，直接打开首次配置向导：

```text
http://127.0.0.1:8000/admin/setup
```

## 2. 理解“服务地址”是干什么的

服务地址不是机器人聊天消息入口。机器人聊天消息通过飞书长连接进入 Flago（FLAGO）。

服务地址主要用于三件事：

- 用户点击“飞书授权”后，飞书授权页面把结果跳回这里。
- 管理员使用飞书账号登录本地配置后台。
- 机器人菜单发送“打开本地配置网页”链接。

本机自用时填写：

```text
http://127.0.0.1:8000
```

向导会自动生成两个飞书后台需要添加的回调地址：

```text
http://127.0.0.1:8000/oauth/feishu/callback
http://127.0.0.1:8000/admin/oauth/callback
```

飞书开放平台里添加的地址必须和页面显示的一模一样。协议、域名、端口和路径都不能多一个字或少一个字。

## 3. 创建飞书机器人应用

打开飞书开放平台，创建一个自建应用。

Flago（FLAGO） 不会自动帮你创建飞书应用，也不会默认连接作者或其他人的机器人。你在后台填写哪个
App ID 和 App Secret，程序就连接哪个自建应用。

需要开启：

- 机器人能力。
- 机器人自定义菜单。
- 长连接接收事件。

需要订阅事件：

- `im.message.receive_v1`：接收用户发给机器人的消息。
- `application.bot.menu_v6`：接收机器人菜单点击。

卡片确认按钮也通过长连接处理。写入确认、撤回确认、记忆保存确认都依赖它。

## 4. 配置飞书权限

在飞书开放平台的权限管理里开通下面这些权限。开通后需要发布应用版本，否则用户重新授权也拿不到新权限。

建议先复制下面这组默认权限：

```text
auth:user.id:read
drive:drive.search:readonly
search:docs:read
docx:document:readonly
docx:document
docs:document.media:download
wiki:node:read
wiki:wiki:readonly
sheets:spreadsheet:readonly
sheets:spreadsheet
bitable:app:readonly
bitable:app
base:table:read
base:record:read
base:record:create
base:record:update
base:record:delete
base:field:read
base:view:read
```

这些权限分别用于：

- 识别当前授权用户。
- 搜索用户能看的飞书文档。
- 读取飞书文档、知识库、电子表格、多维表格。
- 下载文档里的图片、PDF、Word 等附件。
- 在开启写入功能后，写入文档、表格或多维表。

如果你希望授权长期有效，还需要在飞书后台开通：

```text
offline_access
```

同时在配置后台开启“持续授权”。如果飞书后台没有开通 `offline_access`，不要开启持续授权，否则授权页会报应用权限不足。

## 5. 配置 OAuth 回调地址

在飞书开放平台的重定向 URL 或 OAuth 回调地址里添加：

```text
http://127.0.0.1:8000/oauth/feishu/callback
http://127.0.0.1:8000/admin/oauth/callback
```

如果你在配置后台把服务地址改成了公网地址，例如：

```text
https://your-domain.example.com
```

那飞书后台也要改成：

```text
https://your-domain.example.com/oauth/feishu/callback
https://your-domain.example.com/admin/oauth/callback
```

改完服务地址或飞书后台回调地址后，需要重启 Flago（FLAGO）。

## 6. 在配置后台填写飞书应用信息

打开：

```text
http://127.0.0.1:8000/admin/setup
```

按页面步骤填写：

- 飞书 App ID。
- 飞书 App Secret。
- 服务地址。

App ID 和 App Secret 来自飞书开放平台的应用凭证页面。
不要填写文档里的 `cli_xxx`、`xxx` 这类示例值；它们只是格式示例，不能让机器人真正工作。

首次配置完成后，点击“飞书登录绑定管理员”。第一次成功登录的飞书用户会成为本机后台管理员。
以后只有这个飞书用户能打开后台修改配置。

## 7. 配置模型接口

在首次配置向导或后台“模型接口”里添加至少一个模型。

普通用户只需要填写：

- 供应商，例如 Gemini、DeepSeek、OpenAI、Qwen、Doubao、Minimax 或 Claude。
- 显示名称，例如“我的 DeepSeek”。
- API Key。
- 模型名。
- Base URL，如果该供应商有默认地址，可以保持默认。

填好后点“测试连接”。如果显示连接正常，再保存。

如果暂时没有模型 Key，可以先跳过。机器人能启动，但普通对话无法得到真实模型回答。

## 8. 配置机器人菜单

在飞书开放平台的机器人自定义菜单里添加菜单项。菜单项类型选择“事件”，事件 key 填下面的值。

建议菜单结构：

```text
助手
- 助手信息              flago.assistant.name.view

模型
- 查看模型              flago.model.view
- 恢复默认模型          flago.model.default
- 使用 Gemini           flago.model.use.gemini
- 使用 DeepSeek         flago.model.use.deepseek
- 使用 OpenAI           flago.model.use.openai
- 使用 Qwen             flago.model.use.qwen
- 使用 Doubao           flago.model.use.doubao
- 使用 Minimax          flago.model.use.minimax
- 使用 Claude           flago.model.use.claude

授权
- 飞书授权              flago.auth.start
- 授权状态              flago.auth.status

写入
- 写入状态              flago.writeback.status
- 自动写入开启          flago.writeback.auto.enable
- 自动写入关闭          flago.writeback.auto.disable
- 写入恢复默认          flago.writeback.auto.clear
- 最近写入              flago.writeback.history
- 撤回                  flago.writeback.undo

记忆
- 查看记忆              flago.memory.view
- 删除记忆              flago.memory.delete
- 关闭记忆              flago.memory.disable
- 开启记忆              flago.memory.enable

帮助
- 使用说明              flago.help
- 本地配置网页          flago.admin.open
```

可选兼容菜单：

```text
flago.assistant.info.view
flago.context.view
flago.context.enable
flago.context.disable
```

上下文现在默认开启，一般不需要放开启/关闭菜单。

## 9. 启动、停止和重启

打开终端进入 Flago（FLAGO） 目录。

查看服务状态：

```bash
uv run flago service status
```

启动并打开后台：

```bash
uv run flago service start --open-admin
```

重启：

```bash
uv run flago service restart
```

停止：

```bash
uv run flago service stop
```

如果你使用便携包，通常双击 `start-flago.cmd` 就够了。

## 10. 第一次验证

按这个顺序测试：

1. 打开配置后台，确认服务控制显示“运行中”。
2. 在飞书私聊机器人，点击“授权 > 飞书授权”。
3. 浏览器打开授权页后完成授权。
4. 点击“授权 > 授权状态”，确认授权可用。
5. 点击“模型 > 查看模型”，确认当前模型可用。
6. 给机器人发一句普通问题，例如“你好，总结一下你能做什么”。
7. 发送一个你自己能打开的飞书文档链接，让机器人读取。
8. 如果开启了写入功能，发送“帮我写一句测试文字到测试文档末尾”，确认出现写入确认卡片。

## 11. 常见问题

### 服务打不开

先运行：

```bash
uv run flago service status
```

如果端口被占用，关闭占用 8000 端口的程序，或在后台高级配置里修改端口。

### 飞书收不到消息

检查：

- 飞书应用是否启用了机器人能力。
- 是否启用了长连接接收事件。
- 是否订阅了 `im.message.receive_v1`。
- 程序是否正在运行。

### 菜单点击没有反应

检查：

- 菜单项类型是否选择“事件”。
- 事件 key 是否完全一致。
- 是否订阅了 `application.bot.menu_v6`。
- 是否发布了飞书应用版本。

### 授权页报重定向 URL 错误

检查配置后台显示的两个回调地址，必须和飞书开放平台里登记的一模一样：

```text
/oauth/feishu/callback
/admin/oauth/callback
```

如果你把服务地址从 `127.0.0.1` 改成了公网域名，飞书后台也要同步修改。

### 授权成功但读取不了文档

通常是三种原因：

- 你本人没有这个文档的权限。
- 飞书应用没有开通对应 API 权限。
- 你是在新增权限之前授权的，需要重新点“飞书授权”。

### 模型测试失败

检查：

- API Key 是否正确。
- Base URL 是否正确。
- 模型名是否属于当前供应商。
- 网络或代理是否能访问对应供应商。

### 写入没有执行

写入默认会先给确认卡片。只有你点击“确认执行”后才会真正写入。

如果写入功能关闭，机器人只会给草稿或提示。

### 修改配置后没有生效

保存全局配置后，点击后台“服务控制 > 重启服务”，或运行：

```bash
uv run flago service restart
```

## 12. 最小可用配置清单

完成下面几项，就可以开始使用：

- 本地服务能启动。
- 飞书 App ID 和 App Secret 已填写。
- 飞书开放平台已添加两个 OAuth 回调地址。
- 飞书开放平台已启用机器人、长连接事件和菜单事件。
- 飞书开放平台已开通并发布文档/表格读取权限。
- 至少配置并测试通过一个模型接口。
- 用户在飞书里完成授权。

完成后，机器人就可以在飞书里对话、读取你授权可见的资料，并按配置生成写入确认卡片。
