import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

from fcgo.agent import Assistant
from fcgo.config import Settings
from fcgo.feishu.client import FeishuClient
from fcgo.logging import redact
from fcgo.model_providers.catalog import find_catalog_item
from fcgo.model_providers.registry import ModelRouter
from fcgo.models import (
    ActionProposal,
    AssistantRequest,
    AssistantResponse,
    AuditEventType,
    ConversationType,
    FeishuBotMenuEvent,
    FeishuMessage,
    ModelPreference,
    WriteActionType,
)
from fcgo.storage import SQLiteStore
from fcgo.writeback.cards import assistant_response_card

logger = logging.getLogger(__name__)


class OAuthLinkService(Protocol):
    async def create_authorization_url(self, subject_id: str) -> tuple[str, str]: ...


class FeishuMessageRouter:
    def __init__(
        self,
        assistant: Assistant,
        feishu_client: FeishuClient,
        store: SQLiteStore,
        oauth: OAuthLinkService | None = None,
        model_router: ModelRouter | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.assistant = assistant
        self.feishu_client = feishu_client
        self.store = store
        self.oauth = oauth
        self.model_router = model_router
        self.settings = settings or Settings()

    async def handle_message(self, message: FeishuMessage) -> None:
        if not _should_route(message):
            logger.info(
                "feishu_message_ignored message_id=%s reason=group_without_bot_mention",
                message.message_id,
            )
            return
        if not message.text.strip():
            return
        if len(message.text) > self.settings.max_message_chars:
            await self.store.audit(
                AuditEventType.ERROR,
                actor_id=message.sender_id,
                detail={
                    "kind": "message_too_large",
                    "message_id": message.message_id,
                    "length": len(message.text),
                    "limit": self.settings.max_message_chars,
                },
            )
            await self.feishu_client.reply_text(
                message.chat_id,
                f"这条消息太长了，当前 {len(message.text)} 字符，"
                f"上限是 {self.settings.max_message_chars} 字符。"
                "请缩短问题，或改为发送文档/表格链接让我按需读取。",
            )
            return
        if message.text.strip() in {"/授权", "授权"}:
            await self._reply_oauth_link(message)
            return
        if _is_undo_command(message.text):
            await self._reply_latest_undo_card(message)
            return
        if await self._maybe_handle_model_command(message):
            return
        if message.message_id:
            fresh = await self.store.remember_idempotency_key(
                f"feishu_message:{message.message_id}", message.chat_id
            )
            if not fresh:
                logger.info("duplicate_feishu_message message_id=%s", message.message_id)
                return
        model_preference = await self._resolve_model_preference(message)
        request = AssistantRequest(
            actor_id=message.sender_id,
            conversation_id=message.conversation_key or message.chat_id,
            conversation_type=message.conversation_type,
            text=message.text,
            model_provider=model_preference.provider if model_preference else None,
            model=model_preference.model if model_preference else None,
        )
        try:
            response = await self.assistant.handle(request)
        except Exception as exc:  # noqa: BLE001 - keep bot responsive on provider failures
            detail = str(redact(str(exc)))
            logger.exception("assistant_response_failed message_id=%s", message.message_id)
            await self.feishu_client.reply_text(message.chat_id, _model_failure_message(detail))
            return
        await self._send_assistant_response(message, response)

    async def handle_bot_menu(self, event: FeishuBotMenuEvent) -> None:
        receive_id, receive_id_type = _menu_receive_target(event)
        if not receive_id:
            logger.warning("feishu_bot_menu_missing_operator event_key=%s", event.event_key)
            return
        fresh = await self.store.remember_idempotency_key(
            _menu_idempotency_key(event),
            event.event_key,
        )
        if not fresh:
            logger.info("duplicate_feishu_bot_menu event_id=%s", event.event_id)
            return
        text = await self._handle_menu_action(event)
        await self._send_menu_text(receive_id, receive_id_type, text)

    async def _send_assistant_response(
        self,
        message: FeishuMessage,
        response: AssistantResponse,
    ) -> None:
        if not response.action_proposals:
            await self.feishu_client.reply_text(message.chat_id, response.text)
            return
        duplicate_count = 0
        for proposal in response.action_proposals:
            duplicate = await self.store.find_recent_matching_action(
                proposal,
                within_seconds=self.settings.writeback_dedupe_window_seconds,
            )
            if duplicate is not None:
                duplicate_count += 1
                logger.info(
                    "writeback_duplicate_warning message_id=%s action_id=%s "
                    "matched_action_id=%s matched_status=%s",
                    message.message_id,
                    proposal.id,
                    duplicate["id"],
                    duplicate["status"],
                )
            await self.store.save_pending_action(proposal)
        card_text = response.text
        if duplicate_count:
            card_text = (
                f"{response.text}\n\n"
                "提醒：系统检测到近期有相同目标和相同内容的写回记录。"
                "这可能是重复写入，也可能是你确实想多次写入。"
                "我不会替你拦截；请确认无误后再点击执行。"
            )
        card = assistant_response_card(card_text, response.action_proposals)
        await self.feishu_client.send_interactive_card(message.chat_id, card)

    async def _handle_menu_action(self, event: FeishuBotMenuEvent) -> str:
        key = event.event_key.strip().lower()
        if key == "fcgo.model.view":
            return await self._model_status_text(
                conversation_preference=None,
                user_preference=await self.store.get_model_preference(_menu_user_scope(event)),
                conversation_label="菜单入口不适用",
            )
        if key == "fcgo.model.default":
            await self.store.clear_model_preference(
                _menu_user_scope(event),
                updated_by=_menu_actor_id(event),
            )
            return "已恢复你的个人默认模型配置。"
        if key.startswith("fcgo.model.use."):
            provider = key.removeprefix("fcgo.model.use.")
            return await self._set_menu_user_model(event, provider)
        if key == "fcgo.auth.start":
            return await self._menu_oauth_text(event)
        if key == "fcgo.help":
            return _menu_help_text()
        return f"未识别的菜单事件：{event.event_key}\n\n{_menu_help_text()}"

    async def _maybe_handle_model_command(self, message: FeishuMessage) -> bool:
        command = _parse_model_command(message.text)
        if command is None:
            return False
        if self.model_router is None:
            await self.feishu_client.reply_text(message.chat_id, "模型路由暂未启用。")
            return True
        if command in {"", "查看"}:
            await self._reply_model_status(message)
            return True
        if command == "默认":
            await self.store.clear_model_preference(
                _conversation_model_scope(message),
                updated_by=message.sender_id,
            )
            await self.feishu_client.reply_text(
                message.chat_id,
                "已恢复当前会话的默认模型配置。",
            )
            return True
        if command in {"我的 默认", "个人 默认"}:
            await self.store.clear_model_preference(
                _user_model_scope(message),
                updated_by=message.sender_id,
            )
            await self.feishu_client.reply_text(
                message.chat_id,
                "已恢复你的个人默认模型配置。",
            )
            return True
        for prefix in ("使用 ", "切换 "):
            if command.startswith(prefix):
                try:
                    await self._set_conversation_model(message, command.removeprefix(prefix))
                except ValueError as exc:
                    await self.feishu_client.reply_text(message.chat_id, str(exc))
                return True
        for prefix in ("我的 使用 ", "个人 使用 "):
            if command.startswith(prefix):
                try:
                    await self._set_user_model(message, command.removeprefix(prefix))
                except ValueError as exc:
                    await self.feishu_client.reply_text(message.chat_id, str(exc))
                return True
        await self.feishu_client.reply_text(message.chat_id, _model_command_help())
        return True

    async def _reply_model_status(self, message: FeishuMessage) -> None:
        assert self.model_router is not None
        text = await self._model_status_text(
            conversation_preference=await self.store.get_model_preference(
                _conversation_model_scope(message)
            ),
            user_preference=await self.store.get_model_preference(_user_model_scope(message)),
        )
        await self.feishu_client.reply_text(message.chat_id, text)

    async def _model_status_text(
        self,
        *,
        conversation_preference: ModelPreference | None,
        user_preference: ModelPreference | None,
        conversation_label: str | None = None,
    ) -> str:
        assert self.model_router is not None
        available = "\n".join(
            f"- [可用] {item.spec}" for item in self.model_router.catalog if item.configured
        )
        pending = "\n".join(
            f"- [待配置] {item.spec}：缺少 {', '.join(item.missing_fields)}"
            for item in self.model_router.catalog
            if not item.configured
        )
        text = (
            "当前模型配置：\n"
            f"- 部署默认：{self.model_router.default_provider}/"
            f"{self.model_router.default_model or 'provider-default'}\n"
            f"- 当前会话：{conversation_label or _format_preference(conversation_preference)}\n"
            f"- 你的个人偏好：{_format_preference(user_preference)}\n\n"
            "支持的模型：\n"
            f"{available or '- 无'}\n"
            f"{pending and chr(10) + pending or ''}\n\n"
            "可发送：/模型 使用 provider/model"
        )
        return text

    async def _set_conversation_model(self, message: FeishuMessage, spec: str) -> None:
        provider, model = self._parse_and_validate_model_spec(spec)
        await self.store.save_model_preference(
            scope=_conversation_model_scope(message),
            provider=provider,
            model=model,
            updated_by=message.sender_id,
        )
        await self.feishu_client.reply_text(
            message.chat_id,
            f"已将当前会话模型设置为：{provider}/{model or 'provider-default'}",
        )

    async def _set_user_model(self, message: FeishuMessage, spec: str) -> None:
        provider, model = self._parse_and_validate_model_spec(spec)
        await self.store.save_model_preference(
            scope=_user_model_scope(message),
            provider=provider,
            model=model,
            updated_by=message.sender_id,
        )
        await self.feishu_client.reply_text(
            message.chat_id,
            f"已将你的个人默认模型设置为：{provider}/{model or 'provider-default'}",
        )

    def _parse_and_validate_model_spec(self, spec: str) -> tuple[str, str | None]:
        provider, model = _parse_model_spec(spec)
        if not provider:
            raise ValueError("模型配置缺少 provider")
        if self.model_router is None:
            raise ValueError("模型路由暂未启用")
        if not self.model_router.registry.has(provider):
            catalog_item = find_catalog_item(self.model_router.catalog, provider)
            if catalog_item is not None and not catalog_item.configured:
                missing = "、".join(catalog_item.missing_fields)
                raise ValueError(
                    f"{provider} 支持但尚未配置完整，缺少：{missing}。"
                    "请先补齐 .env 后重启服务。"
                )
            available = "、".join(self.model_router.registry.names())
            supported = "、".join(item.provider for item in self.model_router.catalog)
            raise ValueError(
                f"未知 Provider：{provider}。当前可用：{available}。支持：{supported}"
            )
        return provider, model

    async def _set_menu_user_model(self, event: FeishuBotMenuEvent, provider: str) -> str:
        assert self.model_router is not None
        catalog_item = find_catalog_item(self.model_router.catalog, provider)
        if catalog_item is None:
            supported = "、".join(item.provider for item in self.model_router.catalog)
            return f"未知 Provider：{provider}。支持：{supported}"
        if not catalog_item.configured:
            missing = "、".join(catalog_item.missing_fields)
            return f"{provider} 支持但尚未配置完整，缺少：{missing}。请先补齐 .env 后重启服务。"
        await self.store.save_model_preference(
            scope=_menu_user_scope(event),
            provider=catalog_item.provider,
            model=catalog_item.model,
            updated_by=_menu_actor_id(event),
        )
        return f"已将你的个人默认模型设置为：{catalog_item.spec}"

    async def _menu_oauth_text(self, event: FeishuBotMenuEvent) -> str:
        if self.oauth is None:
            return "OAuth 授权暂未启用，请检查服务配置。"
        authorization_url, _ = await self.oauth.create_authorization_url(_menu_actor_id(event))
        return "请打开下面的链接完成飞书授权，授权后回到当前会话继续使用：\n" f"{authorization_url}"

    async def _send_menu_text(
        self,
        receive_id: str,
        receive_id_type: str,
        text: str,
    ) -> None:
        if receive_id_type == "open_id":
            await self.feishu_client.send_text_to_open_id(receive_id, text)
            return
        await self.feishu_client.send_text_to_user_id(receive_id, text)

    async def _resolve_model_preference(self, message: FeishuMessage) -> ModelPreference | None:
        conversation_preference = await self.store.get_model_preference(
            _conversation_model_scope(message)
        )
        if conversation_preference is not None:
            return conversation_preference
        if message.conversation_type == ConversationType.PRIVATE:
            return await self.store.get_model_preference(_user_model_scope(message))
        return None

    async def _reply_oauth_link(self, message: FeishuMessage) -> None:
        if self.oauth is None:
            await self.feishu_client.reply_text(
                message.chat_id,
                "OAuth 授权暂未启用，请检查服务配置。",
            )
            return
        authorization_url, _ = await self.oauth.create_authorization_url(message.sender_id)
        await self.feishu_client.reply_text(
            message.chat_id,
            "请打开下面的链接完成飞书授权，授权后回到当前会话继续使用：\n"
            f"{authorization_url}",
        )

    async def _reply_latest_undo_card(self, message: FeishuMessage) -> None:
        latest = await self.store.find_latest_reversible_writeback(message.sender_id)
        if latest is None:
            await self.feishu_client.reply_text(
                message.chat_id,
                "暂时没有找到可撤回的写回记录。目前支持撤回最近一次多维表新增记录。",
            )
            return
        try:
            action_type = WriteActionType(str(latest["undo_action_type"]))
        except ValueError:
            await self.feishu_client.reply_text(
                message.chat_id,
                "最近一次写回的撤回类型暂不支持。",
            )
            return
        now = datetime.now(UTC)
        proposal = ActionProposal(
            actor_id=message.sender_id,
            action_type=action_type,
            target=latest["undo_target"],
            payload=latest["undo_payload"],
            preview=str(latest.get("undo_preview") or "撤回上一次写回"),
            created_at=now,
            expires_at=now + timedelta(seconds=self.settings.pending_action_ttl_seconds),
        )
        await self.store.save_pending_action(proposal)
        card = assistant_response_card(
            (
                "我找到了最近一次可撤回的写回记录。"
                "请确认是否执行撤回；撤回也会经过一次确认。"
            ),
            [proposal],
        )
        await self.feishu_client.send_interactive_card(message.chat_id, card)


def _model_failure_message(detail: str) -> str:
    lowered = detail.lower()
    if "user location is not supported" in lowered:
        return (
            "模型调用失败：当前网络或地区暂时无法使用对应模型 API。"
            "请配置可用代理或兼容 base URL 后再试。"
        )
    if "quota" in lowered or "resource_exhausted" in lowered:
        return "模型调用失败：当前 API 额度不足或被限流，请稍后再试或检查额度。"
    if "api key" in lowered or "unauthenticated" in lowered or "permission_denied" in lowered:
        return "模型调用失败：API Key 或权限配置不正确，请检查本地 .env。"
    return "模型调用失败：模型服务暂时不可用，请稍后再试。"


def _is_undo_command(text: str) -> bool:
    stripped = text.strip()
    return stripped in {
        "/撤回",
        "撤回",
        "撤回上一次写回",
        "撤回最近写回",
        "撤回上次写回",
    }


def _has_explicit_writeback_intent(text: str) -> bool:
    normalized = text.strip().lower()
    if not normalized:
        return False
    direct_phrases = (
        "写回",
        "确认写回",
        "保存到",
        "保存进",
        "记录到",
        "记录进",
        "填入",
        "填到",
        "填进",
        "追加到",
        "追加进",
        "添加",
        "添加到",
        "添加进",
        "增加",
        "增加到",
        "增加进",
        "加入",
        "加入到",
        "加入进",
        "写入",
        "写到",
        "写进",
        "替换",
        "替换掉",
        "覆盖",
        "改为",
        "改成",
        "插入到",
        "插入",
        "发送到",
        "发到",
        "发给",
        "send to",
        "write back",
        "write to",
        "save to",
        "append to",
        "insert into",
        "send to",
    )
    if any(phrase in normalized for phrase in direct_phrases):
        return True
    action_terms = (
        "创建",
        "新建",
        "新增",
        "添加",
        "增加",
        "更新",
        "修改",
        "替换",
        "覆盖",
        "加入",
        "改成",
        "改为",
        "建立",
        "create",
        "add",
        "update",
        "modify",
    )
    target_terms = (
        "文档",
        "表格",
        "多维表格",
        "记录",
        "消息",
        "飞书",
        "当前会话",
        "这个表",
        "这个文档",
        "sheet",
        "base",
        "doc",
        "message",
        "record",
    )
    request_markers = ("请", "帮我", "把", "将", "给我", "please")
    has_action = any(term in normalized for term in action_terms)
    has_target = any(term in normalized for term in target_terms)
    has_request_marker = any(marker in normalized for marker in request_markers)
    return has_action and has_target and has_request_marker


def _should_route(message: FeishuMessage) -> bool:
    if message.conversation_type.value == "private":
        return True
    return message.is_bot_mentioned


def _parse_model_command(text: str) -> str | None:
    stripped = text.strip()
    for prefix in ("/模型", "模型"):
        if stripped == prefix:
            return ""
        if stripped.startswith(f"{prefix} "):
            return stripped.removeprefix(prefix).strip()
    return None


def _parse_model_spec(spec: str) -> tuple[str, str | None]:
    stripped = spec.strip()
    if not stripped:
        return "", None
    if "/" in stripped:
        provider, model = stripped.split("/", 1)
        return provider.strip().lower(), model.strip() or None
    parts = stripped.split(maxsplit=1)
    provider = parts[0].strip().lower()
    model_name = parts[1].strip() if len(parts) > 1 else None
    return provider, model_name or None


def _conversation_model_scope(message: FeishuMessage) -> str:
    return f"conversation:{message.conversation_key or message.chat_id}"


def _user_model_scope(message: FeishuMessage) -> str:
    return f"user:{message.sender_id}"


def _format_preference(preference: ModelPreference | None) -> str:
    if preference is None:
        return "未设置"
    return f"{preference.provider}/{preference.model or 'provider-default'}"


def _model_command_help() -> str:
    return (
        "模型指令：\n"
        "- /模型 查看\n"
        "- /模型 使用 provider/model\n"
        "- /模型 默认\n"
        "- /模型 我的 使用 provider/model\n"
        "- /模型 我的 默认"
    )


def _menu_receive_target(event: FeishuBotMenuEvent) -> tuple[str, str]:
    if event.operator_open_id:
        return event.operator_open_id, "open_id"
    if event.operator_user_id:
        return event.operator_user_id, "user_id"
    return "", ""


def _menu_actor_id(event: FeishuBotMenuEvent) -> str:
    return event.operator_open_id or event.operator_user_id or event.operator_union_id


def _menu_user_scope(event: FeishuBotMenuEvent) -> str:
    return f"user:{_menu_actor_id(event)}"


def _menu_idempotency_key(event: FeishuBotMenuEvent) -> str:
    if event.event_id:
        return f"feishu_bot_menu:{event.event_id}"
    return f"feishu_bot_menu:{event.operator_open_id}:{event.event_key}:{event.timestamp}"


def _menu_help_text() -> str:
    return (
        "FCGO 菜单入口：\n"
        "- 模型：查看可用模型，或设置你的个人默认模型。\n"
        "- 授权：获取飞书 OAuth 授权链接。\n"
        "- 帮助：查看当前菜单说明。\n\n"
        "仍然可以直接发送 /模型 查看、/模型 使用 provider/model、/授权。"
    )
