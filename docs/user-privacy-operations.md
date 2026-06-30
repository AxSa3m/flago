# 用户授权与隐私操作指南

本文面向 FCGO 的普通使用者和本地部署管理员，说明飞书授权、助手命名、上下文读取、长期记忆和隐私验证方法。

## 用户常用操作

### 使用说明

没有配置飞书机器人自定义菜单时，可以直接发送：

```text
/帮助
```

预期回复会列出当前可用入口，例如助手、模型、授权、上下文和记忆。若用户已设置个人助手名称，帮助文案会使用该名称；群聊中不会展示某个成员的个人助手名称。

### 飞书授权

在飞书私聊机器人发送：

```text
/授权
```

或点击机器人自定义菜单中的“授权”。机器人会发送一张授权卡片，点击“点击授权”后在浏览器完成飞书 OAuth 授权。

授权成功后，FCGO 会把用户 token 保存到本地 SQLite。后续读取用户有权限的飞书文档、电子表格、多维表格和 Wiki 时会自动复用 token。若启用了 `FCGO_OAUTH_ENABLE_OFFLINE_ACCESS=true` 且飞书后台已开通 `offline_access`，FCGO 会在 access token 过期时用 refresh token 自动续期。

查看当前授权状态：

```text
/授权 状态
```

预期回复：

```text
小智 的飞书资源授权状态：可用。
```

如果提示缺少 scope，需要管理员先在飞书开发者后台开通对应权限并发布应用，然后用户重新授权。

### 上下文状态

当前会话上下文默认开启，不需要用户手动开启或关闭。查看当前策略：

```text
/上下文 查看
```

预期回复会说明：

- 近期消息会按当前会话范围进入回答上下文。
- 超出近期窗口或字符预算的旧消息会压缩成会话摘要。
- 系统不会保存完整聊天原文。

### 助手名称

查看当前助手名称：

```text
/助手 名称
```

默认回复：

```text
当前助手名称：小智（默认）。
```

设置个人助手名称：

```text
/助手 命名 小飞
```

预期回复：

```text
已将你的助手名称设置为：小飞。
```

恢复默认名称：

```text
/助手 默认名称
```

预期回复：

```text
已恢复默认助手名称：小智。
```

说明：

- 助手名称只影响回复文案和模型上下文中的自称，不会修改飞书开放平台里的 Bot 名称。
- 私聊会使用当前用户的个人助手名称。
- 群聊普通对话不会使用某个成员的个人助手名称，避免影响整个群；菜单点击结果会发给点击者本人，因此会使用点击者自己的名称。

### 长期记忆

查看当前用户长期记忆：

```text
/记忆 查看
```

没有记忆时会回复：

```text
暂时没有保存你的长期记忆。小智 默认不会保存完整聊天原文。
```

新增或更新一条带 key 的偏好记忆：

```text
/记忆 记住 输出格式=优先表格
```

预期回复：

```text
已记录：输出格式：优先表格
```

新增一条通用偏好：

```text
/记忆 记住 输出尽量用表格
```

预期回复：

```text
已记录：偏好：输出尽量用表格。
```

普通聊天中如果说“记住我叫 Sa3m”“我的项目代号是空杯”这类内容，FCGO 会先发送“确认保存长期记忆”卡片。只有点击“保存”后才会写入长期记忆；点击“取消”或不处理则不会保存。

修改指定偏好：

```text
/记忆 修改 语言风格=简洁中文
```

预期回复：

```text
已修改：语言风格：简洁中文
```

按 key 删除一条记忆：

```text
/记忆 删除 语言风格
```

预期回复：

```text
已删除记忆：语言风格。
```

通用偏好可以这样删除：

```text
/记忆 删除 偏好
```

关闭长期记忆：

```text
/记忆 关闭
```

关闭只会暂停新增记忆和暂停把长期记忆加入模型上下文，不会删除已有记忆。

重新开启长期记忆：

```text
/记忆 开启
```

删除长期记忆：

```text
/记忆 删除
```

删除会先发送确认卡片。点击“确认删除”后会清空当前用户已保存的长期记忆；记忆功能状态保持不变。

清空全部长期记忆：

```text
/记忆 清空
```

清空也会先发送确认卡片。确认后会删除当前用户全部长期记忆；不会关闭记忆功能。

记忆管理命令只允许在私聊中操作。群聊里发送 `/记忆 查看`、`/记忆 修改` 等命令时，会提示到私聊中管理，避免把个人记忆展示到群聊。

## 自定义菜单

推荐给机器人配置这些隐私相关菜单项：

```text
助手
- 助手信息        fcgo.assistant.name.view

授权
- 飞书授权        fcgo.auth.start
- 授权状态        fcgo.auth.status

上下文
- 查看上下文        fcgo.context.view

写入
- 写入状态        fcgo.writeback.status
- 自动写入开启    fcgo.writeback.auto.enable
- 自动写入关闭    fcgo.writeback.auto.disable
- 写入恢复默认    fcgo.writeback.auto.clear
- 最近写入        fcgo.writeback.history
- 撤回            fcgo.writeback.undo

记忆
- 查看记忆        fcgo.memory.view
- 删除记忆        fcgo.memory.delete
- 关闭记忆        fcgo.memory.disable
- 开启记忆        fcgo.memory.enable

帮助
- 使用说明        fcgo.help
```

菜单事件需要订阅 `application.bot.menu_v6`，并启用长连接事件接收。

菜单里的“删除记忆”会触发清空全部长期记忆的确认卡片；按 key 删除单条记忆目前使用文本命令。

## 管理员配置

### OAuth 权限

默认读取权限配置：

```env
FEISHU_OAUTH_SCOPES=auth:user.id:read drive:drive.search:readonly search:docs:read docx:document:readonly docx:document docs:document.media:download wiki:node:read wiki:wiki:readonly sheets:spreadsheet:readonly sheets:spreadsheet bitable:app:readonly bitable:app base:table:read base:record:read base:record:create base:record:update base:record:delete base:field:read base:view:read
FCGO_OAUTH_ENABLE_OFFLINE_ACCESS=false
```

`FEISHU_OAUTH_SCOPES` 中的权限必须同时在飞书开发者后台开通并发布。若用户已经授权过，但后来新增了 scope，用户需要重新授权一次。

自动续期配置：

```env
FCGO_OAUTH_ENABLE_OFFLINE_ACCESS=true
```

只有当飞书开发者后台已经开通 `offline_access` 时才开启。否则飞书授权页会提示 `20027` 应用权限不足。开启后，用户重新授权并拿到 refresh token，FCGO 才能自动刷新 access token。

### 上下文配置

```env
FCGO_CONTEXT_RECENT_MESSAGE_LIMIT=50
FCGO_CONTEXT_RECENT_TIME_WINDOW_HOURS=24
FCGO_CONTEXT_CACHE_TTL_HOURS=24
FCGO_CONTEXT_CACHE_REFRESH_SECONDS=60
FCGO_CONTEXT_INJECT_MESSAGE_LIMIT=8
FCGO_CONTEXT_MAX_CHARS=6000
```

含义：

- `FCGO_CONTEXT_RECENT_MESSAGE_LIMIT`：最多读取的近期消息数量。
- `FCGO_CONTEXT_RECENT_TIME_WINDOW_HOURS`：近期消息时间窗口。
- `FCGO_CONTEXT_CACHE_TTL_HOURS`：本地短期聊天缓存保留时间。
- `FCGO_CONTEXT_CACHE_REFRESH_SECONDS`：同一会话缓存刷新间隔。
- `FCGO_CONTEXT_INJECT_MESSAGE_LIMIT`：每次模型请求最多注入的近期消息条数。
- `FCGO_CONTEXT_MAX_CHARS`：每次模型请求的聊天上下文字符预算。

聊天缓存用于减少重复调用飞书历史接口，不是长期记忆。缓存到期后可以清理，审计日志不会保存完整聊天正文。

### 记忆配置

```env
FCGO_MEMORY_STORE_RAW_TEXT=false
FCGO_MEMORY_ITEM_MAX_CHARS=2000
FCGO_MEMORY_CONTEXT_MAX_CHARS=4000
FCGO_ASSISTANT_DEFAULT_NAME=小智
```

含义：

- `FCGO_MEMORY_STORE_RAW_TEXT=false`：长期记忆不得保存完整聊天原文、飞书资源正文、网页正文或附件正文。
- `FCGO_MEMORY_ITEM_MAX_CHARS`：单条长期记忆最大长度。
- `FCGO_MEMORY_CONTEXT_MAX_CHARS`：每次进入模型上下文的长期记忆总字符预算。
- `FCGO_ASSISTANT_DEFAULT_NAME`：用户未设置个人助手名称时使用的默认名称。

私聊中超过近期窗口的旧消息可以被压缩成用户可查看、可删除的 `会话摘要` 记忆。该记忆不得包含完整聊天原文、消息 ID、发送人和时间戳等原始记录元数据。

## 私聊和群聊边界

私聊：

- 可读取当前私聊最近有限窗口内的消息。
- 可按用户 OAuth 搜索和读取用户本人可见的飞书资料。
- 可保存当前用户可查看、可删除的摘要或偏好记忆。
- 不全量扫描云空间，不建立长期资料索引。

群聊：

- 只处理 @ 机器人或菜单/命令触发的消息。
- 只读取当前群聊或当前话题范围内的有限上下文。
- 不跨群读取，不读取成员私聊内容。
- 群聊上下文默认不写入任何成员的个人长期记忆。
- 群聊中不能直接查看或修改个人长期记忆；请在私聊中执行记忆管理命令。

## 不会保存什么

FCGO 默认不会把以下内容写入长期存储：

- 完整聊天原文。
- 飞书文档、电子表格、多维表格正文。
- 网页正文或附件正文。
- OAuth token、API key、Authorization header。
- 模型思考过程。
- 完整搜索词或 Agent 决策原文。

审计日志只记录事件类型、actor_id、资源类型、结果数量、错误类型、截断状态、查询长度等元数据。

## 如何验证没有保存原始正文

在项目目录运行：

```powershell
cd D:\CodingSpace\feishuGemini\fcgo
@'
import json, sqlite3
from fcgo.config import get_settings

conn = sqlite3.connect(get_settings().sqlite_path)

print("memory_items")
for row in conn.execute("select kind, content, source from memory_items order by updated_at desc limit 10"):
    print(row)

print("\naudit_events")
for event_type, detail_json, created_at in conn.execute(
    "select event_type, detail_json, created_at from audit_events order by id desc limit 20"
):
    print(created_at, event_type, json.loads(detail_json))

print("\ncontext cache counts")
for row in conn.execute("select scope, count(*) from context_message_cache group by scope"):
    print(row)
'@ | uv run python -
```

检查重点：

- `memory_items.content` 应是摘要或偏好，不应是完整聊天流水。
- `audit_events.detail_json` 不应包含完整聊天正文、飞书正文、网页正文、token 或 API key。
- `context_message_cache` 可以短期存在近期聊天缓存，但它不是长期记忆，受 `FCGO_CONTEXT_CACHE_TTL_HOURS` 约束。

## 人工验收步骤

1. 发送 `/授权 状态`，确认授权可用或明确列出缺失 scope。
2. 发送 `/上下文 查看`，确认回复说明上下文默认开启和摘要边界。
3. 发送 `/助手 名称`，确认默认名称为 `小智`；发送 `/助手 命名 小飞` 后再 `/帮助`，确认帮助文案使用 `小飞`。
4. 发送 `/记忆 记住 输出格式=优先表格` 和 `/记忆 修改 语言风格=简洁中文`，再发送 `/记忆 查看`，确认能看到两条偏好。
5. 发送 `记住我叫 Sa3m`，确认出现“确认保存长期记忆”卡片；点“取消”后 `/记忆 查看` 不应新增；再次发送并点“保存”后 `/记忆 查看` 应出现称呼记忆。
6. 发送 `/记忆 删除 语言风格`，再发送 `/记忆 查看`，确认只删除了对应 key。
7. 发送 `/记忆 关闭`，再发送 `/记忆 记住 输出格式=列表`，确认不会新增；已有记忆仍保留。
8. 发送 `/记忆 开启`，恢复记忆功能。
9. 发送 `/记忆 删除` 或 `/记忆 清空`，确认需要卡片二次确认；确认后 `/记忆 查看` 应为空。
10. 在群聊 @ 机器人发送 `/记忆 查看`，确认提示到私聊中管理。
11. 搜索一个有权限的飞书文档，确认可读；搜索一个无权限或缺 scope 的资源，确认错误原因清晰。

## 常见问题

### 授权后仍提示缺权限

通常是飞书开发者后台未开通对应 API 权限，或开通后未发布应用。先开通并发布，再让用户重新授权。

### 授权页提示 20027

表示授权链接里包含应用未开通的 scope。若提示 `offline_access`，先关闭：

```env
FCGO_OAUTH_ENABLE_OFFLINE_ACCESS=false
```

如果确实需要自动续期，则先在飞书开发者后台开通 `offline_access`，再设置为 `true` 并重新授权。

### 为什么还需要重新授权

只有以下情况需要重新授权：

- 新增或变更了 OAuth scope。
- 用户 token 已过期且没有 refresh token。
- 用户撤销了应用授权。
- 应用重新发布权限后，旧授权不包含新权限。

服务重启不会清空 SQLite 中保存的 token。
