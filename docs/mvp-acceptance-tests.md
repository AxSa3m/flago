# MVP 验收测试矩阵

本文说明 FCGO 读取优先 MVP 的自动测试覆盖和人工飞书验收方法。自动测试全部使用临时 SQLite、mock HTTP 或 mock Provider，不依赖真实模型额度和真实飞书资源。

## 自动验收

运行：

```powershell
uv run pytest -q
uv run ruff check .
uv run mypy
```

### 消息到回复闭环

`tests/test_mvp_acceptance.py`：

- `test_mvp_private_message_reaches_model_and_replies_to_feishu`
  - 覆盖 `FeishuMessageRouter -> Assistant -> ModelRouter -> Provider -> Feishu reply`。
  - 验证模型审计只记录 provider/model 等元数据，不记录用户正文。
- `test_mvp_feishu_link_is_read_before_model_reply`
  - 覆盖飞书链接解析、资源读取和资源正文进入模型上下文。
- `test_mvp_duplicate_message_is_processed_once`
  - 覆盖消息幂等，重复事件只调用一次模型、只回复一次。
- `test_mvp_group_requires_bot_mention`
  - 覆盖群聊不 @ 时忽略，@ 后进入模型并回复。

### 配置和 Provider

- `tests/test_settings.py`：环境配置、OAuth scope 和隐私默认值。
- `tests/test_gemini_provider.py`：Gemini prompt、配置和 mock 模型响应。
- `tests/test_openai_compatible_provider.py`：OpenAI-compatible 请求格式和错误脱敏。
- `tests/test_claude_provider.py`：Claude Messages API 请求格式、usage 和错误脱敏。
- `tests/test_model_providers.py`：Provider 注册、路由、能力校验、成本限制和模型审计。

### 资源解析和读取

- `tests/test_parser.py`：飞书文档、Wiki、电子表格、多维表格和网页 URL 解析。
- `tests/test_resource_reader.py`：飞书资源、网页读取、搜索 URL、模糊查询、安全截断和审计。
- `tests/test_feishu_openapi.py`：用户 OAuth、scope、文档搜索和飞书 OpenAPI mock。

### OAuth、存储和安全

- `tests/test_oauth.py`：授权 URL、token 保存、refresh token 和缺失 scope。
- `tests/test_server.py`：`/healthz`、OAuth 成功/失败页面和 HTTP 回调。
- `tests/test_storage.py`：OAuth state、幂等键、模型偏好、记忆和审计。
- `tests/test_logging.py`：密钥脱敏。

### 写回默认值和遗留状态

- `tests/test_feishu_router.py::test_router_drops_writeback_proposals_when_disabled`
  验证读取基线不会保存待写回动作或发送写回确认卡片。
- `tests/test_writeback.py::test_confirm_expires_pending_action`
  验证实验写回动作过期。
- `tests/test_writeback.py::test_cancel_is_idempotent`
  验证实验写回取消幂等。

## 人工飞书验收

1. 私聊机器人发送普通问题，应收到模型回复。
2. 在群聊中不 @ 机器人发送消息，应无回复；@ 后应正常回复。
3. 连续发送同一条测试消息时，应避免重复事件产生重复回复。
4. 发送本人有权限的飞书文档链接，回答应基于文档真实内容。
5. 发送“帮我搜索飞书文档 关键词”，只应返回真实可读结果；无结果时不得编造。
6. 发送 `/授权`，完成 OAuth 后重新读取私有资源；缺少 scope 时应明确提示补充权限。
7. 发送 `/模型 查看`，确认当前使用模型和已配置 Provider。
8. 测试“刚才聊了什么”，确认近期上下文有效。
9. 发送候选记忆内容，确认只有点击保存卡片后才写入长期记忆。
10. 请求修改或创建飞书内容，应只返回草稿或建议，不实际写入。

