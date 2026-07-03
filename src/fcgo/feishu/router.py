import base64
import logging
import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal, Protocol
from uuid import uuid4

from fcgo.agent.protocols import AssistantHandler
from fcgo.config import Settings
from fcgo.feishu.client import FeishuClient
from fcgo.feishu.context import ChatContextLoader, ChatHistoryAPI, conversation_memory_source
from fcgo.feishu.oauth import AuthorizationStatus
from fcgo.logging import redact
from fcgo.model_providers.catalog import find_catalog_item
from fcgo.model_providers.registry import ModelRouter
from fcgo.models import (
    ActionProposal,
    AssistantAttachment,
    AssistantRequest,
    AssistantResponse,
    AuditEventType,
    ChatContextMessage,
    ConversationType,
    FeishuBotMenuEvent,
    FeishuMessage,
    FeishuMessageAttachment,
    MemoryItem,
    ModelPreference,
    WriteActionType,
    WritebackConfirmationMode,
)
from fcgo.storage import SQLiteStore
from fcgo.writeback.cards import assistant_response_card
from fcgo.writeback.service import WritebackService

logger = logging.getLogger(__name__)


class OAuthLinkService(Protocol):
    async def create_authorization_url(self, subject_id: str) -> tuple[str, str]: ...

    async def authorization_status(self, subject_id: str) -> AuthorizationStatus: ...


class MessageResourceAPI(Protocol):
    async def download_message_resource(
        self,
        message_id: str,
        file_key: str,
        *,
        resource_type: str = "image",
        max_bytes: int,
    ) -> Any: ...


class FeishuMessageRouter:
    def __init__(
        self,
        assistant: AssistantHandler,
        feishu_client: FeishuClient,
        store: SQLiteStore,
        oauth: OAuthLinkService | None = None,
        model_router: ModelRouter | None = None,
        settings: Settings | None = None,
        chat_history_api: ChatHistoryAPI | None = None,
        writeback_service: WritebackService | None = None,
        message_resource_api: MessageResourceAPI | None = None,
    ) -> None:
        self.assistant = assistant
        self.feishu_client = feishu_client
        self.store = store
        self.oauth = oauth
        self.model_router = model_router
        self.settings = settings or Settings()
        self.writeback_service = writeback_service
        self.message_resource_api = message_resource_api
        self.context_loader = (
            ChatContextLoader(settings=self.settings, store=store, api=chat_history_api)
            if chat_history_api is not None
            else None
        )

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
        if await self._maybe_handle_admin_command(message):
            return
        if await self._maybe_handle_help_command(message):
            return
        if await self._maybe_handle_auth_command(message):
            return
        if await self._maybe_handle_assistant_command(message):
            return
        if await self._maybe_handle_privacy_command(message):
            return
        if await self._maybe_handle_writeback_command(message):
            return
        if await self._maybe_handle_explicit_memory_statement(message):
            return
        if _is_writeback_history_command(message.text):
            await self._reply_writeback_history(message)
            return
        if _is_undo_command(message.text):
            if not self.settings.writeback_enabled:
                await self.feishu_client.reply_text(
                    message.chat_id,
                    "写入功能当前已暂停，因此撤回写入也暂不可用。",
                )
                return
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
        chat_context_messages: list[ChatContextMessage] = []
        chat_context_summary = ""
        chat_context_omitted_count = 0
        if self.context_loader is not None:
            try:
                (
                    chat_context_messages,
                    chat_context_summary,
                    chat_context_omitted_count,
                ) = (
                    await self.context_loader.load(message)
                )
            except RuntimeError as exc:
                logger.warning(
                    "chat_context_load_failed message_id=%s detail=%s",
                    message.message_id,
                    redact(str(exc)),
                )
                await self.store.audit(
                    AuditEventType.ERROR,
                    actor_id=message.sender_id,
                    detail={
                        "kind": "chat_context_load_failed",
                        "message_id": message.message_id,
                        "conversation_id": message.conversation_key or message.chat_id,
                        "conversation_type": message.conversation_type.value,
                        "error_type": type(exc).__name__,
                    },
                )
        memory_items = []
        if (
            message.conversation_type == ConversationType.PRIVATE
            and await self.store.is_memory_enabled(message.sender_id)
        ):
            memory_items = await self.store.list_memory_items(message.sender_id)
            if chat_context_summary.strip():
                current_summary_source = conversation_memory_source(
                    _conversation_context_scope(message)
                )
                memory_items = [
                    item
                    for item in memory_items
                    if item.source != current_summary_source
                ]
            memory_items = _budget_memory_items(
                memory_items,
                max_chars=self.settings.memory_context_max_chars,
            )
        attachments = await self._current_message_attachments(message)
        request = AssistantRequest(
            actor_id=message.sender_id,
            conversation_id=message.conversation_key or message.chat_id,
            conversation_type=message.conversation_type,
            text=message.text,
            assistant_name=await self._assistant_name_for_message(message),
            assistant_profile=await self._assistant_profile_for_message(message),
            model_provider=_request_model_provider(
                model_preference,
                attachments,
                settings=self.settings,
            ),
            model=_request_model(
                model_preference,
                attachments,
                settings=self.settings,
            ),
            chat_context_messages=chat_context_messages,
            chat_context_summary=chat_context_summary,
            chat_context_omitted_count=chat_context_omitted_count,
            memory_items=memory_items,
            attachments=attachments,
        )
        try:
            response = await self.assistant.handle(request)
        except Exception as exc:  # noqa: BLE001 - keep bot responsive on provider failures
            detail = str(redact(str(exc)))
            logger.exception("assistant_response_failed message_id=%s", message.message_id)
            await self.feishu_client.reply_text(message.chat_id, _model_failure_message(detail))
            return
        await self._send_assistant_response(message, response)

    async def _current_message_attachments(
        self,
        message: FeishuMessage,
    ) -> list[AssistantAttachment]:
        if not message.attachments or self.message_resource_api is None:
            return []
        items: list[AssistantAttachment] = []
        for attachment in message.attachments:
            if not _attachment_model_understanding_enabled(attachment, self.settings):
                continue
            try:
                downloaded = await self.message_resource_api.download_message_resource(
                    message.message_id,
                    attachment.key,
                    resource_type=_message_resource_type(attachment),
                    max_bytes=_attachment_max_bytes(attachment, self.settings),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "message_attachment_download_failed message_id=%s key=%s type=%s error=%s",
                    message.message_id,
                    attachment.key,
                    attachment.type,
                    redact(str(exc)),
                )
                continue
            media_type = (
                attachment.content_type
                or getattr(downloaded, "content_type", "")
                or _default_attachment_media_type(attachment)
            )
            content = getattr(downloaded, "content", b"")
            if not isinstance(content, bytes) or not content:
                continue
            items.append(
                AssistantAttachment(
                    type=_assistant_attachment_type(attachment),
                    media_type=(
                        str(media_type).split(";", 1)[0].strip()
                        or _default_attachment_media_type(attachment)
                    ),
                    data_base64=base64.b64encode(content).decode("ascii"),
                    filename=attachment.filename or getattr(downloaded, "filename", None),
                )
            )
        return items

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
        if event.event_key.strip().lower() == "fcgo.memory.delete":
            await self._send_memory_delete_confirmation(event, receive_id, receive_id_type)
            return
        if event.event_key.strip().lower() == "fcgo.auth.start":
            await self._send_menu_oauth_card(event, receive_id, receive_id_type, force_link=True)
            return
        if event.event_key.strip().lower() == "fcgo.auth.status":
            await self._send_menu_oauth_card(event, receive_id, receive_id_type, force_link=False)
            return
        if event.event_key.strip().lower() == "fcgo.writeback.undo":
            await self._send_menu_undo_card(event, receive_id, receive_id_type)
            return
        if event.event_key.strip().lower() == "fcgo.admin.open":
            await self._send_menu_admin_card(event, receive_id, receive_id_type)
            return
        text = await self._handle_menu_action(event)
        await self._send_menu_text(receive_id, receive_id_type, text)

    async def _maybe_handle_admin_command(self, message: FeishuMessage) -> bool:
        command = _parse_prefixed_command(message.text, "/配置", "配置", "/后台", "后台")
        if command is None:
            return False
        await self.feishu_client.send_interactive_card(
            message.chat_id,
            _admin_open_card(
                _admin_url(self.settings),
                assistant_name=await self._assistant_name_for_message(message),
            ),
        )
        return True

    async def _maybe_handle_help_command(self, message: FeishuMessage) -> bool:
        command = _parse_prefixed_command(message.text, "/帮助", "帮助", "/help", "help")
        if command is None:
            return False
        if command in {"", "菜单", "说明", "使用说明"}:
            await self.feishu_client.reply_text(
                message.chat_id,
                _menu_help_text(await self._assistant_name_for_message(message)),
            )
            return True
        await self.feishu_client.reply_text(
            message.chat_id,
            _menu_help_text(await self._assistant_name_for_message(message)),
        )
        return True

    async def _send_assistant_response(
        self,
        message: FeishuMessage,
        response: AssistantResponse,
    ) -> None:
        if not response.action_proposals:
            await self.feishu_client.reply_text(message.chat_id, response.text)
            return
        if not self.settings.writeback_enabled:
            logger.info(
                "writeback_disabled_dropping_proposals message_id=%s proposal_count=%s",
                message.message_id,
                len(response.action_proposals),
            )
            response.action_proposals.clear()
            await self.feishu_client.reply_text(
                message.chat_id,
                _writeback_disabled_text(response.text),
            )
            return
        confirmation_mode = await self._effective_writeback_confirmation_mode(message)
        if confirmation_mode == WritebackConfirmationMode.DRAFT_ONLY:
            await self.feishu_client.reply_text(
                message.chat_id,
                _draft_only_writeback_text(response),
            )
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
        if (
            confirmation_mode == WritebackConfirmationMode.LOW_RISK_DIRECT
            and duplicate_count == 0
            and self.writeback_service is not None
            and all(
                _is_low_risk_direct_writeback(proposal)
                for proposal in response.action_proposals
            )
        ):
            await self._execute_low_risk_writebacks(message, response)
            return
        for proposal in response.action_proposals:
            await self.store.save_pending_action(proposal)
        card_text = response.text
        if duplicate_count:
            card_text = (
                f"{response.text}\n\n"
                "提醒：系统检测到近期有相同目标和相同内容的写回记录。"
                "这可能是重复写入，也可能是你确实想多次写入。"
                "我不会替你拦截；请确认无误后再点击执行。"
            )
        card = assistant_response_card(
            card_text,
            response.action_proposals,
            assistant_name=await self._assistant_name_for_message(message),
        )
        await self.feishu_client.send_interactive_card(message.chat_id, card)

    async def _execute_low_risk_writebacks(
        self,
        message: FeishuMessage,
        response: AssistantResponse,
    ) -> None:
        if self.writeback_service is None:
            return
        result_lines = []
        for proposal in response.action_proposals:
            await self.store.save_pending_action(proposal)
            result = await self.writeback_service.confirm(proposal.id, message.sender_id)
            result_lines.append(f"- {proposal.target_title or '目标资源'}：{result.message}")
        text = response.text.strip()
        text = f"{text}\n\n" + "\n".join(result_lines) if text else "\n".join(result_lines)
        await self.feishu_client.reply_text(message.chat_id, text)

    async def _handle_menu_action(self, event: FeishuBotMenuEvent) -> str:
        key = event.event_key.strip().lower()
        assistant_name = await self._assistant_name(_menu_actor_id(event))
        if key == "fcgo.model.view":
            return await self._model_status_text(
                conversation_preference=None,
                user_preference=await self.store.get_model_preference(_menu_user_scope(event)),
                assistant_name=assistant_name,
            )
        if key == "fcgo.model.default":
            await self.store.clear_model_preference(
                _menu_user_scope(event),
                updated_by=_menu_actor_id(event),
            )
            return f"{assistant_name} 已恢复你的个人默认模型配置。"
        if key.startswith("fcgo.model.use."):
            provider = key.removeprefix("fcgo.model.use.")
            return await self._set_menu_user_model(event, provider, assistant_name=assistant_name)
        if key in {"fcgo.assistant.name.view", "fcgo.assistant.info.view"}:
            return await self._assistant_name_status_text(_menu_actor_id(event))
        if key == "fcgo.auth.start":
            return await self._menu_oauth_text(event, assistant_name=assistant_name)
        if key == "fcgo.auth.status":
            return await self._menu_oauth_status_text(event, assistant_name=assistant_name)
        if key == "fcgo.context.view":
            return await self._menu_context_status_text(event, assistant_name=assistant_name)
        if key == "fcgo.context.enable":
            return await self._enable_menu_context_text(event, assistant_name=assistant_name)
        if key == "fcgo.context.disable":
            return await self._disable_menu_context_text(event, assistant_name=assistant_name)
        if key == "fcgo.writeback.status":
            return await self._writeback_status_text(
                _menu_message(event),
                assistant_name=assistant_name,
            )
        if key == "fcgo.writeback.auto.enable":
            return await self._enable_writeback_auto_text(
                _menu_message(event),
                assistant_name=assistant_name,
            )
        if key == "fcgo.writeback.auto.disable":
            return await self._disable_writeback_auto_text(
                _menu_message(event),
                assistant_name=assistant_name,
            )
        if key == "fcgo.writeback.auto.clear":
            return await self._clear_writeback_auto_text(
                _menu_message(event),
                assistant_name=assistant_name,
            )
        if key == "fcgo.writeback.history":
            return await self._writeback_history_text(_menu_actor_id(event))
        if key == "fcgo.memory.view":
            return await self._memory_status_text(_menu_actor_id(event))
        if key == "fcgo.memory.disable":
            return await self._disable_memory_text(_menu_actor_id(event))
        if key == "fcgo.memory.enable":
            return await self._enable_memory_text(_menu_actor_id(event))
        if key == "fcgo.help":
            return _menu_help_text(assistant_name)
        return f"未识别的菜单事件：{event.event_key}\n\n{_menu_help_text(assistant_name)}"

    async def _maybe_handle_model_command(self, message: FeishuMessage) -> bool:
        command = _parse_model_command(message.text)
        if command is None:
            return False
        if self.model_router is None:
            await self.feishu_client.reply_text(
                message.chat_id,
                f"{await self._assistant_name_for_message(message)} 的模型路由暂未启用。",
            )
            return True
        if command in {"", "查看"}:
            await self._reply_model_status(message)
            return True
        await self.feishu_client.reply_text(
            message.chat_id,
            _model_menu_only_text(await self._assistant_name_for_message(message)),
        )
        return True

    async def _maybe_handle_writeback_command(self, message: FeishuMessage) -> bool:
        command = _parse_writeback_command(message.text)
        if command is None:
            return False
        assistant_name = await self._assistant_name_for_message(message)
        normalized = re.sub(r"\s+", "", command.strip())
        if normalized in {"", "状态", "查看", "策略"}:
            await self.feishu_client.reply_text(
                message.chat_id,
                await self._writeback_status_text(message, assistant_name=assistant_name),
            )
            return True
        if normalized in {"历史", "最近", "最近写入", "最近写回"}:
            await self._reply_writeback_history(message)
            return True
        if normalized in {"自动开启", "开启自动", "自动打开", "打开自动"}:
            await self.feishu_client.reply_text(
                message.chat_id,
                await self._enable_writeback_auto_text(message, assistant_name=assistant_name),
            )
            return True
        if normalized in {"自动关闭", "关闭自动", "自动暂停", "暂停自动"}:
            await self.feishu_client.reply_text(
                message.chat_id,
                await self._disable_writeback_auto_text(message, assistant_name=assistant_name),
            )
            return True
        if normalized in {"自动清除", "清除自动", "清除偏好", "恢复默认"}:
            await self.feishu_client.reply_text(
                message.chat_id,
                await self._clear_writeback_auto_text(message, assistant_name=assistant_name),
            )
            return True
        await self.feishu_client.reply_text(
            message.chat_id,
            _writeback_command_help(assistant_name),
        )
        return True

    async def _maybe_handle_auth_command(self, message: FeishuMessage) -> bool:
        command = _parse_prefixed_command(message.text, "/授权", "授权")
        if command is None:
            return False
        if command in {"", "开始", "链接"}:
            await self._reply_oauth_card(message, force_link=True)
            return True
        if command in {"状态", "查看", "检查"}:
            await self._reply_oauth_card(message, force_link=False)
            return True
        await self.feishu_client.reply_text(
            message.chat_id,
            f"{await self._assistant_name_for_message(message)} 的授权指令：/授权 或 /授权 状态",
        )
        return True

    async def _maybe_handle_assistant_command(self, message: FeishuMessage) -> bool:
        command = _parse_prefixed_command(message.text, "/助手", "助手")
        if command is None:
            return False
        if command in {"", "名称", "名字", "简介", "人设", "信息", "查看", "状态"}:
            await self.feishu_client.reply_text(
                message.chat_id,
                await self._assistant_name_status_text(message.sender_id),
            )
            return True
        if command in {
            "默认",
            "恢复默认",
            "重置",
            "默认名称",
            "恢复默认名称",
            "重置名称",
            "默认简介",
            "恢复默认简介",
            "重置简介",
            "默认人设",
            "恢复默认人设",
        }:
            await self.store.clear_assistant_name_preference(
                message.sender_id,
                updated_by=message.sender_id,
            )
            await self.store.clear_assistant_profile_preference(
                message.sender_id,
                updated_by=message.sender_id,
            )
            await self.feishu_client.reply_text(
                message.chat_id,
                "已恢复默认助手名称和简介。",
            )
            return True
        profile = _parse_assistant_profile_command(command)
        if profile is not None:
            cleaned_profile, error = _clean_assistant_profile(profile)
            if error is not None:
                await self.feishu_client.reply_text(message.chat_id, error)
                return True
            await self.store.save_assistant_profile_preference(
                subject_id=message.sender_id,
                assistant_profile=cleaned_profile,
                updated_by=message.sender_id,
            )
            await self.feishu_client.reply_text(message.chat_id, "已更新你的助手简介。")
            return True
        name = _parse_assistant_name_command(command)
        if name is not None:
            cleaned_name, error = _clean_assistant_name(name)
            if error is not None:
                await self.feishu_client.reply_text(message.chat_id, error)
                return True
            await self.store.save_assistant_name_preference(
                subject_id=message.sender_id,
                assistant_name=cleaned_name,
                updated_by=message.sender_id,
            )
            await self.feishu_client.reply_text(
                message.chat_id,
                f"已将你的助手名称设置为：{cleaned_name}。",
            )
            return True
        await self.feishu_client.reply_text(message.chat_id, _assistant_command_help())
        return True

    async def _maybe_handle_privacy_command(self, message: FeishuMessage) -> bool:
        context_command = _parse_prefixed_command(message.text, "/上下文", "上下文")
        if context_command is not None:
            await self._handle_context_command(message, context_command)
            return True
        memory_command = _parse_prefixed_command(message.text, "/记忆", "记忆")
        if memory_command is not None:
            await self._handle_memory_command(message, memory_command)
            return True
        return False

    async def _handle_context_command(self, message: FeishuMessage, command: str) -> None:
        assistant_name = await self._assistant_name_for_message(message)
        if command in {"", "查看", "状态", "开启", "打开", "启用", "关闭", "停用", "禁用"}:
            await self.feishu_client.reply_text(
                message.chat_id,
                _context_status_text(assistant_name),
            )
            return
        await self.feishu_client.reply_text(
            message.chat_id,
            _privacy_command_help(assistant_name),
        )

    async def _assistant_name_status_text(self, subject_id: str) -> str:
        preference = await self.store.get_assistant_name_preference(subject_id)
        if preference is None:
            assistant_name = self.settings.assistant_default_name
        else:
            assistant_name = preference.assistant_name
        profile_preference = await self.store.get_assistant_profile_preference(subject_id)
        assistant_profile = (
            profile_preference.assistant_profile
            if profile_preference is not None
            else self.settings.assistant_default_profile
        )
        return (
            "当前助手信息：\n"
            f"- 名称：{assistant_name}\n"
            f"- 简介：{assistant_profile}"
        )

    async def _assistant_name(self, subject_id: str) -> str:
        preference = await self.store.get_assistant_name_preference(subject_id)
        if preference is not None:
            return preference.assistant_name
        return self.settings.assistant_default_name

    async def _assistant_name_for_message(self, message: FeishuMessage) -> str:
        if message.conversation_type == ConversationType.PRIVATE:
            return await self._assistant_name(message.sender_id)
        return self.settings.feishu_bot_name or self.settings.assistant_default_name

    async def _assistant_profile(self, subject_id: str) -> str:
        preference = await self.store.get_assistant_profile_preference(subject_id)
        if preference is not None:
            return preference.assistant_profile
        return self.settings.assistant_default_profile

    async def _assistant_profile_for_message(self, message: FeishuMessage) -> str:
        if message.conversation_type == ConversationType.PRIVATE:
            return await self._assistant_profile(message.sender_id)
        return self.settings.assistant_default_profile

    async def _handle_memory_command(self, message: FeishuMessage, command: str) -> None:
        subject_id = message.sender_id
        if message.conversation_type != ConversationType.PRIVATE:
            await self.feishu_client.reply_text(
                message.chat_id,
                "记忆管理请在私聊中操作，避免把个人记忆展示到群聊。",
            )
            return
        if command in {"", "查看", "状态"}:
            await self.feishu_client.reply_text(
                message.chat_id,
                await self._memory_status_text(subject_id),
            )
            return
        if command.startswith("记住"):
            await self.feishu_client.reply_text(
                message.chat_id,
                await self._save_memory_command_text(subject_id, command),
            )
            return
        if command.startswith("修改"):
            await self.feishu_client.reply_text(
                message.chat_id,
                await self._update_memory_command_text(subject_id, command),
            )
            return
        if command.startswith("删除 "):
            await self.feishu_client.reply_text(
                message.chat_id,
                await self._delete_one_memory_text(subject_id, command.removeprefix("删除 ")),
            )
            return
        if command in {"删除", "清空"}:
            await self.feishu_client.send_interactive_card(
                message.chat_id,
                _memory_delete_confirmation_card(action_id=uuid4().hex),
            )
            return
        if command in {"开启", "打开", "启用"}:
            await self.feishu_client.reply_text(
                message.chat_id,
                await self._enable_memory_text(subject_id),
            )
            return
        if command in {"关闭", "停用", "禁用"}:
            await self.feishu_client.reply_text(
                message.chat_id,
                await self._disable_memory_text(subject_id),
            )
            return
        await self.feishu_client.reply_text(
            message.chat_id,
            _privacy_command_help(await self._assistant_name(subject_id)),
        )

    async def _save_memory_command_text(self, subject_id: str, command: str) -> str:
        if not await self.store.is_memory_enabled(subject_id):
            return "长期记忆已关闭，未保存这条内容。可通过 /记忆 开启 后再记录。"
        parsed = _parse_memory_command_value(command.removeprefix("记住"))
        if parsed is None:
            return "记忆内容不能为空。用法：/记忆 记住 输出格式=优先表格"
        key, kind, content = parsed
        clipped = _clip_text(content, max_chars=self.settings.memory_item_max_chars)
        await self.store.save_memory_item(
            id=_explicit_memory_id(subject_id, key),
            subject_id=subject_id,
            kind=kind,
            content=clipped,
            source="user.command",
        )
        return f"已记录：{kind}：{clipped}"

    async def _update_memory_command_text(self, subject_id: str, command: str) -> str:
        if not await self.store.is_memory_enabled(subject_id):
            return "长期记忆已关闭，未修改记忆。可通过 /记忆 开启 后再操作。"
        parsed = _parse_memory_key_value(command.removeprefix("修改"))
        if parsed is None:
            return "修改格式不正确。用法：/记忆 修改 语言风格=简洁中文"
        key, value = parsed
        kind = _memory_kind_from_key(key)
        content = _format_memory_content(kind, value)
        clipped = _clip_text(content, max_chars=self.settings.memory_item_max_chars)
        await self.store.save_memory_item(
            id=_explicit_memory_id(subject_id, key),
            subject_id=subject_id,
            kind=kind,
            content=clipped,
            source="user.command",
        )
        return f"已修改：{kind}：{clipped}"

    async def _delete_one_memory_text(self, subject_id: str, key_text: str) -> str:
        key = _normalize_memory_key(key_text)
        if not key:
            return "要删除哪条记忆？用法：/记忆 删除 语言风格"
        deleted_count = await self.store.delete_memory_item(
            subject_id=subject_id,
            item_id=_explicit_memory_id(subject_id, key),
            updated_by=subject_id,
        )
        kind = _memory_kind_from_key(key)
        if deleted_count:
            return f"已删除记忆：{kind}。"
        return f"没有找到名为“{kind}”的记忆。可发送 /记忆 查看。"

    async def _maybe_handle_explicit_memory_statement(self, message: FeishuMessage) -> bool:
        memory = _parse_explicit_memory_statement(message.text)
        if memory is None:
            return False
        if message.conversation_type != ConversationType.PRIVATE:
            await self.feishu_client.reply_text(
                message.chat_id,
                "长期记忆只在私聊中保存。请在私聊里告诉我需要记住的内容。",
            )
            return True
        if not await self.store.is_memory_enabled(message.sender_id):
            await self.feishu_client.reply_text(
                message.chat_id,
                "长期记忆已关闭，未保存这条内容。可通过菜单或 /记忆 开启 后再记录。",
            )
            return True
        await self.feishu_client.send_interactive_card(
            message.chat_id,
            _memory_save_confirmation_card(
                action_id=uuid4().hex,
                key=memory.key,
                kind=memory.kind,
                content=_clip_text(
                    memory.content,
                    max_chars=self.settings.memory_item_max_chars,
                ),
            ),
        )
        return True

    async def _memory_status_text(self, subject_id: str) -> str:
        if not await self.store.is_memory_enabled(subject_id):
            return "长期记忆已关闭。当前不会把你的长期记忆加入模型上下文。"
        items = await self.store.list_memory_items(subject_id)
        if not items:
            assistant_name = await self._assistant_name(subject_id)
            return f"暂时没有保存你的长期记忆。{assistant_name} 默认不会保存完整聊天原文。"
        lines = [f"- {item.kind}：{item.content}" for item in items]
        return "当前保存的长期记忆：\n" + "\n".join(lines)

    async def _delete_memory_text(self, subject_id: str) -> str:
        deleted_count = await self.store.clear_memory_items(
            subject_id,
            updated_by=subject_id,
        )
        return f"已删除你的长期记忆，共 {deleted_count} 条。长期记忆功能仍保持开启。"

    async def _disable_memory_text(self, subject_id: str) -> str:
        await self.store.set_memory_enabled(
            subject_id=subject_id,
            enabled=False,
            updated_by=subject_id,
        )
        return (
            "已关闭长期记忆。已有记忆会保留，但后续不会新增记忆，"
            "也不会把长期记忆加入模型上下文。"
        )

    async def _enable_memory_text(self, subject_id: str) -> str:
        await self.store.set_memory_enabled(
            subject_id=subject_id,
            enabled=True,
            updated_by=subject_id,
        )
        return (
            f"已开启长期记忆。{await self._assistant_name(subject_id)} 仍不会保存完整聊天原文，"
            "只会使用你可查看和管理的摘要或偏好。"
        )

    async def confirm_memory_delete(self, actor_id: str) -> str:
        return await self._delete_memory_text(actor_id)

    async def confirm_memory_save(
        self,
        actor_id: str,
        *,
        key: str,
        kind: str,
        content: str,
    ) -> str:
        if not await self.store.is_memory_enabled(actor_id):
            return "长期记忆已关闭，未保存这条内容。"
        clipped = _clip_text(content, max_chars=self.settings.memory_item_max_chars)
        if not clipped:
            return "记忆内容为空，未保存。"
        normalized_key = _normalize_memory_key(key) or f"fact:{_stable_suffix(clipped)}"
        normalized_kind = kind.strip() or _memory_kind_from_key(normalized_key)
        await self.store.save_memory_item(
            id=_explicit_memory_id(actor_id, normalized_key),
            subject_id=actor_id,
            kind=_clip_text(normalized_kind, max_chars=40),
            content=clipped,
            source="user.confirmed",
        )
        return f"已保存长期记忆：{normalized_kind}：{clipped}"

    async def _menu_context_status_text(
        self,
        event: FeishuBotMenuEvent,
        *,
        assistant_name: str,
    ) -> str:
        return _context_status_text(assistant_name)

    async def _enable_menu_context_text(
        self,
        event: FeishuBotMenuEvent,
        *,
        assistant_name: str,
    ) -> str:
        return _context_status_text(assistant_name)

    async def _disable_menu_context_text(
        self,
        event: FeishuBotMenuEvent,
        *,
        assistant_name: str,
    ) -> str:
        return _context_status_text(assistant_name)

    async def _send_memory_delete_confirmation(
        self,
        event: FeishuBotMenuEvent,
        receive_id: str,
        receive_id_type: str,
    ) -> None:
        card = _memory_delete_confirmation_card(action_id=uuid4().hex)
        if receive_id_type == "open_id":
            await self.feishu_client.send_interactive_card_to_open_id(receive_id, card)
            return
        await self.feishu_client.send_interactive_card_to_user_id(receive_id, card)

    async def _reply_model_status(self, message: FeishuMessage) -> None:
        assert self.model_router is not None
        text = await self._model_status_text(
            conversation_preference=await self.store.get_model_preference(
                _conversation_model_scope(message)
            ),
            user_preference=await self.store.get_model_preference(_user_model_scope(message)),
            assistant_name=await self._assistant_name_for_message(message),
        )
        await self.feishu_client.reply_text(message.chat_id, text)

    async def _model_status_text(
        self,
        *,
        conversation_preference: ModelPreference | None,
        user_preference: ModelPreference | None,
        assistant_name: str,
    ) -> str:
        assert self.model_router is not None
        current = user_preference or conversation_preference
        current_spec = _resolved_model_spec(self.model_router, current)
        default_spec = _resolved_model_spec(self.model_router, None)
        available = "\n".join(
            f"- {item.spec}{'（默认）' if item.spec == default_spec else ''}"
            for item in self.model_router.catalog
            if item.configured
        )
        unavailable = "、".join(
            item.provider for item in self.model_router.catalog if not item.configured
        )
        text = (
            f"{assistant_name} 当前使用的模型：{current_spec}\n\n"
            "支持的模型：\n"
            f"{available or '- 无'}\n"
            f"{unavailable and chr(10) + '未启用：' + unavailable or ''}\n\n"
            "请通过飞书机器人自定义菜单切换模型。"
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

    async def _set_menu_user_model(
        self,
        event: FeishuBotMenuEvent,
        provider: str,
        *,
        assistant_name: str,
    ) -> str:
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
        return f"{assistant_name} 当前使用的模型已切换为：{catalog_item.spec}"

    async def _menu_oauth_text(
        self,
        event: FeishuBotMenuEvent,
        *,
        assistant_name: str,
    ) -> str:
        if self.oauth is None:
            return "OAuth 授权暂未启用，请检查服务配置。"
        authorization_url, _ = await self.oauth.create_authorization_url(_menu_actor_id(event))
        return _oauth_status_text(
            AuthorizationStatus(
                authorized=False,
                usable=False,
                missing_scopes=[],
                reason="not_authorized",
            ),
            authorization_url=authorization_url,
            assistant_name=assistant_name,
        )

    async def _menu_oauth_status_text(
        self,
        event: FeishuBotMenuEvent,
        *,
        assistant_name: str,
    ) -> str:
        if self.oauth is None:
            return "OAuth 授权暂未启用，请检查服务配置。"
        status = await self.oauth.authorization_status(_menu_actor_id(event))
        authorization_url = ""
        if not status.usable:
            authorization_url, _ = await self.oauth.create_authorization_url(
                _menu_actor_id(event)
            )
        return _oauth_status_text(
            status,
            authorization_url=authorization_url,
            assistant_name=assistant_name,
        )

    async def _send_menu_oauth_card(
        self,
        event: FeishuBotMenuEvent,
        receive_id: str,
        receive_id_type: str,
        *,
        force_link: bool,
    ) -> None:
        if self.oauth is None:
            await self._send_menu_text(
                receive_id,
                receive_id_type,
                "OAuth 授权暂未启用，请检查服务配置。",
            )
            return
        status = await self.oauth.authorization_status(_menu_actor_id(event))
        authorization_url = ""
        if force_link or not status.usable:
            authorization_url, _ = await self.oauth.create_authorization_url(_menu_actor_id(event))
        card = _oauth_status_card(
            status,
            authorization_url=authorization_url,
            assistant_name=await self._assistant_name(_menu_actor_id(event)),
        )
        if receive_id_type == "open_id":
            await self.feishu_client.send_interactive_card_to_open_id(receive_id, card)
            return
        await self.feishu_client.send_interactive_card_to_user_id(receive_id, card)

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

    async def _send_menu_admin_card(
        self,
        event: FeishuBotMenuEvent,
        receive_id: str,
        receive_id_type: str,
    ) -> None:
        card = _admin_open_card(
            _admin_url(self.settings),
            assistant_name=await self._assistant_name(_menu_actor_id(event)),
        )
        if receive_id_type == "open_id":
            await self.feishu_client.send_interactive_card_to_open_id(receive_id, card)
            return
        await self.feishu_client.send_interactive_card_to_user_id(receive_id, card)

    async def _send_menu_undo_card(
        self,
        event: FeishuBotMenuEvent,
        receive_id: str,
        receive_id_type: str,
    ) -> None:
        actor_id = _menu_actor_id(event)
        if not self.settings.writeback_enabled:
            await self._send_menu_text(
                receive_id,
                receive_id_type,
                "写入功能当前已暂停，因此撤回写入也暂不可用。",
            )
            return
        text, card = await self._latest_undo_response(
            actor_id,
            assistant_name=await self._assistant_name(actor_id),
        )
        if card is None:
            await self._send_menu_text(receive_id, receive_id_type, text)
            return
        if receive_id_type == "open_id":
            await self.feishu_client.send_interactive_card_to_open_id(receive_id, card)
            return
        await self.feishu_client.send_interactive_card_to_user_id(receive_id, card)

    async def _resolve_model_preference(self, message: FeishuMessage) -> ModelPreference | None:
        if message.conversation_type == ConversationType.PRIVATE:
            user_preference = await self.store.get_model_preference(_user_model_scope(message))
            if user_preference is not None:
                return user_preference
        conversation_preference = await self.store.get_model_preference(
            _conversation_model_scope(message)
        )
        if conversation_preference is not None:
            return conversation_preference
        return None

    async def _effective_writeback_confirmation_mode(
        self,
        message: FeishuMessage,
    ) -> WritebackConfirmationMode:
        if self.settings.writeback_confirmation_mode == WritebackConfirmationMode.DRAFT_ONLY:
            return WritebackConfirmationMode.DRAFT_ONLY
        preference = await self.store.get_writeback_auto_execute(message.sender_id)
        if preference is not None and self.settings.writeback_auto_execute_enabled:
            return (
                WritebackConfirmationMode.LOW_RISK_DIRECT
                if preference.enabled
                else WritebackConfirmationMode.ALWAYS
            )
        return self.settings.writeback_confirmation_mode

    async def _writeback_status_text(
        self,
        message: FeishuMessage,
        *,
        assistant_name: str,
    ) -> str:
        preference = await self.store.get_writeback_auto_execute(message.sender_id)
        effective_mode = await self._effective_writeback_confirmation_mode(message)
        if preference is None:
            user_setting = "未设置"
        else:
            user_setting = "已开启" if preference.enabled else "已关闭"
        lines = [
            f"{assistant_name} 当前写入策略：{_writeback_confirmation_mode_label(effective_mode)}",
            f"- 你的自动写入偏好：{user_setting}",
            "- 服务是否允许用户开启自动写入："
            f"{'是' if self.settings.writeback_auto_execute_enabled else '否'}",
            f"- 写入功能：{'已开启' if self.settings.writeback_enabled else '已暂停'}",
        ]
        if effective_mode == WritebackConfirmationMode.LOW_RISK_DIRECT:
            lines.append("低风险文档开头/末尾追加会直接执行；修改、删除、表格和多维表仍会要求确认。")
        else:
            lines.append("默认会先生成确认卡片，不会直接写入。")
        return "\n".join(lines)

    async def _enable_writeback_auto_text(
        self,
        message: FeishuMessage,
        *,
        assistant_name: str,
    ) -> str:
        if not self.settings.writeback_enabled:
            return "写入功能当前已暂停，无法开启自动写入。"
        if not self.settings.writeback_auto_execute_enabled:
            return (
                "服务当前未开放用户自动写入开关。需要服务配置 "
                "`FCGO_WRITEBACK_AUTO_EXECUTE_ENABLED=true` 后才能开启。"
            )
        await self.store.set_writeback_auto_execute(
            subject_id=message.sender_id,
            enabled=True,
            updated_by=message.sender_id,
        )
        return (
            f"已开启你的个人自动写入偏好。{assistant_name} "
            "只会对明确目标的文档开头/末尾追加直接执行；"
            "修改、删除、表格、多维表和不明确目标仍会生成确认卡片。发送 /写入 自动关闭 可随时关闭。"
        )

    async def _disable_writeback_auto_text(
        self,
        message: FeishuMessage,
        *,
        assistant_name: str,
    ) -> str:
        await self.store.set_writeback_auto_execute(
            subject_id=message.sender_id,
            enabled=False,
            updated_by=message.sender_id,
        )
        return f"已关闭你的个人自动写入偏好。{assistant_name} 后续会先生成确认卡片。"

    async def _clear_writeback_auto_text(
        self,
        message: FeishuMessage,
        *,
        assistant_name: str,
    ) -> str:
        await self.store.clear_writeback_auto_execute(
            message.sender_id,
            updated_by=message.sender_id,
        )
        return f"已清除你的自动写入偏好。{assistant_name} 将使用系统默认的写入确认策略。"

    async def _reply_oauth_card(self, message: FeishuMessage, *, force_link: bool) -> None:
        if self.oauth is None:
            await self.feishu_client.reply_text(
                message.chat_id,
                "OAuth 授权暂未启用，请检查服务配置。",
            )
            return
        status = await self.oauth.authorization_status(message.sender_id)
        authorization_url = ""
        if force_link or not status.usable:
            authorization_url, _ = await self.oauth.create_authorization_url(message.sender_id)
        await self.feishu_client.send_interactive_card(
            message.chat_id,
            _oauth_status_card(
                status,
                authorization_url=authorization_url,
                assistant_name=await self._assistant_name_for_message(message),
            ),
        )

    async def _reply_latest_undo_card(self, message: FeishuMessage) -> None:
        text, card = await self._latest_undo_response(
            message.sender_id,
            assistant_name=await self._assistant_name_for_message(message),
        )
        if card is None:
            await self.feishu_client.reply_text(message.chat_id, text)
            return
        await self.feishu_client.send_interactive_card(message.chat_id, card)

    async def _latest_undo_response(
        self,
        actor_id: str,
        *,
        assistant_name: str,
    ) -> tuple[str, dict[str, Any] | None]:
        latest = await self.store.find_latest_reversible_writeback(actor_id)
        if latest is None:
            return (
                "暂时没有找到可撤回的写入记录。"
                "目前支持撤回最近一次文档追加、多维表新增记录或电子表格范围写入。",
                None,
            )
        try:
            action_type = WriteActionType(str(latest["undo_action_type"]))
        except ValueError:
            return "最近一次写入的撤回类型暂不支持。", None
        now = datetime.now(UTC)
        undo_target = _enriched_undo_target(latest)
        proposal = ActionProposal(
            actor_id=actor_id,
            action_type=action_type,
            target=undo_target,
            target_title=_target_title_from_mapping(undo_target),
            target_url=_target_url_from_mapping(undo_target),
            payload=latest["undo_payload"],
            preview=_friendly_undo_preview(latest),
            created_at=now,
            expires_at=now + timedelta(seconds=self.settings.pending_action_ttl_seconds),
        )
        await self.store.save_pending_action(proposal)
        card = assistant_response_card(
            (
                "我找到了最近一次可撤回的写入记录。"
                "请确认是否执行撤回；撤回也会经过一次确认。"
            ),
            [proposal],
            assistant_name=assistant_name,
        )
        return "", card

    async def _reply_writeback_history(self, message: FeishuMessage) -> None:
        await self.feishu_client.reply_text(
            message.chat_id,
            await self._writeback_history_text(message.sender_id),
        )

    async def _writeback_history_text(self, actor_id: str) -> str:
        history = await self.store.list_recent_writeback_executions(
            actor_id,
            limit=5,
        )
        if not history:
            return "暂时没有写入执行记录。"
        lines = ["最近写入："]
        for item in history:
            action_type = str(item["action_type"])
            status, reason = _writeback_history_status(item)
            target = _writeback_target_summary(action_type, item["target"])
            line = f"- {_write_action_label(action_type)} · {target} · {status}"
            if reason:
                line += f"（{reason}）"
            lines.append(line)
        lines.append("\n发送 /撤回 可撤回最近一次标记为“可撤回”的写入。")
        return "\n".join(lines)


def _model_failure_message(detail: str) -> str:
    lowered = detail.lower()
    if "user location is not supported" in lowered:
        return (
            "模型调用失败：当前网络或地区暂时无法使用对应模型 API。"
            "请配置可用代理或兼容 base URL 后再试。"
        )
    if "quota" in lowered or "resource_exhausted" in lowered:
        return "模型调用失败：当前 API 额度不足或被限流，请稍后再试或检查额度。"
    if "rate_limit" in lowered or "rate limit" in lowered or "overloaded" in lowered:
        return "模型调用失败：模型服务当前繁忙或被限流，请稍后再试。"
    if (
        "api key" in lowered
        or "unauthenticated" in lowered
        or "permission_denied" in lowered
        or "authentication_error" in lowered
        or "permission_error" in lowered
    ):
        return "模型调用失败：API Key 或权限配置不正确，请检查本地 .env。"
    return "模型调用失败：模型服务暂时不可用，请稍后再试。"


class _ParsedExplicitMemory:
    def __init__(self, *, key: str, kind: str, content: str) -> None:
        self.key = key
        self.kind = kind
        self.content = content


def _parse_explicit_memory_statement(text: str) -> _ParsedExplicitMemory | None:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return None

    nickname = _match_memory_value(
        cleaned,
        (
            r"^记住我叫\s*(.+)$",
            r"^记住我的名字是\s*(.+)$",
            r"^我的名字是\s*(.+)$",
        ),
    )
    if nickname:
        return _ParsedExplicitMemory(
            key="nickname",
            kind="称呼",
            content=f"你叫{nickname}。",
        )

    project_code = _match_memory_value(
        cleaned,
        (
            r"^我的项目代号是[:：]?\s*(.+)$",
            r"^记住我的项目代号是[:：]?\s*(.+)$",
        ),
    )
    if project_code:
        return _ParsedExplicitMemory(
            key="project_code",
            kind="项目代号",
            content=f"你的项目代号是“{project_code}”。",
        )

    output_preference = _match_memory_value(
        cleaned,
        (
            r"^输出偏好是[:：]?\s*(.+)$",
            r"^记住输出偏好是[:：]?\s*(.+)$",
        ),
    )
    if output_preference:
        return _ParsedExplicitMemory(
            key="output_preference",
            kind="偏好",
            content=f"输出偏好是{output_preference}。",
        )

    test_fact = _match_memory_value(cleaned, (r"^记忆摘要测试\s*\d*\s*[:：]\s*(.+)$",))
    if test_fact:
        return _ParsedExplicitMemory(
            key=f"fact:{_stable_suffix(test_fact)}",
            kind="事实",
            content=_ensure_sentence(test_fact),
        )

    remembered = _match_memory_value(cleaned, (r"^记住[:：]?\s*(.+)$",))
    if remembered:
        return _ParsedExplicitMemory(
            key=f"fact:{_stable_suffix(remembered)}",
            kind="事实",
            content=_ensure_sentence(remembered),
        )
    return None


def _parse_memory_command_value(raw_value: str) -> tuple[str, str, str] | None:
    value = raw_value.strip()
    if not value:
        return None
    key_value = _parse_memory_key_value(value)
    if key_value is not None:
        key, content_value = key_value
        kind = _memory_kind_from_key(key)
        return key, kind, _format_memory_content(kind, content_value)
    content = _ensure_sentence(value)
    key = "preference"
    return key, "偏好", content


def _parse_memory_key_value(raw_value: str) -> tuple[str, str] | None:
    value = raw_value.strip()
    if not value:
        return None
    for separator in ("=", "：", ":"):
        if separator not in value:
            continue
        key_text, content = value.split(separator, 1)
        key = _normalize_memory_key(key_text)
        cleaned_content = _clean_memory_value(content)
        if key and cleaned_content:
            return key, cleaned_content
        return None
    return None


def _normalize_memory_key(key_text: str) -> str:
    key = re.sub(r"\s+", "", key_text).strip().strip("。.!！：:=")
    aliases = {
        "称呼": "nickname",
        "名字": "nickname",
        "昵称": "nickname",
        "项目代号": "project_code",
        "项目": "project_code",
        "输出偏好": "output_preference",
        "输出格式": "output_format",
        "偏好": "preference",
        "语言风格": "language_style",
        "风格": "language_style",
        "工作习惯": "work_habit",
    }
    return aliases.get(key, key)


def _memory_kind_from_key(key: str) -> str:
    labels = {
        "nickname": "称呼",
        "project_code": "项目代号",
        "output_preference": "输出偏好",
        "output_format": "输出格式",
        "preference": "偏好",
        "language_style": "语言风格",
        "work_habit": "工作习惯",
    }
    return labels.get(key, key)


def _format_memory_content(kind: str, value: str) -> str:
    return _clean_memory_value(value)


def _budget_memory_items(items: list[MemoryItem], *, max_chars: int) -> list[MemoryItem]:
    if max_chars <= 0:
        return []
    budgeted: list[MemoryItem] = []
    used_chars = 0
    for item in items:
        content = item.content.strip()
        if not content:
            continue
        prefix_cost = len(item.kind) + 3
        remaining = max_chars - used_chars - prefix_cost
        if remaining <= 0:
            break
        clipped = _clip_text(content, max_chars=remaining)
        if not clipped:
            break
        budgeted.append(item.model_copy(update={"content": clipped}))
        used_chars += prefix_cost + len(clipped)
    return budgeted


def _clip_text(text: str, *, max_chars: int) -> str:
    cleaned = text.strip()
    if max_chars <= 0:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    if max_chars <= 3:
        return cleaned[:max_chars]
    return cleaned[: max_chars - 3].rstrip() + "..."


def _match_memory_value(text: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        match = re.match(pattern, text)
        if match:
            return _clean_memory_value(match.group(1))
    return ""


def _clean_memory_value(value: str) -> str:
    cleaned = value.strip().strip("。.!！ ")
    cleaned = cleaned.strip("\"'“”‘’")
    return cleaned[:200]


def _ensure_sentence(value: str) -> str:
    cleaned = _clean_memory_value(value)
    if not cleaned:
        return ""
    return cleaned if cleaned.endswith(("。", "！", "？", ".", "!", "?")) else f"{cleaned}。"


def _explicit_memory_id(subject_id: str, key: str) -> str:
    digest = sha256(f"{subject_id}:{key}".encode()).hexdigest()[:16]
    return f"explicit-memory-{digest}"


def _stable_suffix(value: str) -> str:
    return sha256(value.casefold().encode()).hexdigest()[:12]


def _oauth_status_text(
    status: AuthorizationStatus,
    *,
    authorization_url: str = "",
    assistant_name: str = "小智",
) -> str:
    if status.usable:
        lines = [f"{assistant_name} 的飞书资源授权状态：可用。"]
        if status.expires_at:
            lines.append(f"access token 过期时间：{status.expires_at}")
        if status.refresh_expires_at:
            lines.append(f"refresh token 过期时间：{status.refresh_expires_at}")
        return "\n".join(lines)
    if not status.authorized:
        lines = [f"{assistant_name} 的飞书资源授权状态：未授权。"]
    elif status.reason == "expired":
        lines = [f"{assistant_name} 的飞书资源授权状态：已过期，需要重新授权。"]
    elif status.missing_scopes:
        lines = [
            f"{assistant_name} 的飞书资源授权状态：缺少权限，需要重新授权。",
            "缺少 scope：" + "、".join(status.missing_scopes),
        ]
    else:
        lines = [f"{assistant_name} 的飞书资源授权状态：不可用，需要重新授权。"]
    if authorization_url:
        lines.append("授权链接：")
        lines.append(authorization_url)
    return "\n".join(lines)


def _oauth_status_card(
    status: AuthorizationStatus,
    *,
    authorization_url: str = "",
    assistant_name: str = "小智",
) -> dict[str, object]:
    if status.usable:
        template = "green"
        title = "飞书授权可用"
        content = (
            f"{assistant_name} 当前已保存可用授权。"
            "后续读取你有权限的飞书资源时会自动复用，无需重复授权。"
        )
    elif not status.authorized:
        template = "blue"
        title = "需要飞书授权"
        content = (
            f"完成一次授权后，{assistant_name} 才能按你的权限读取飞书文档、"
            "表格和多维表格。"
        )
    elif status.reason == "expired":
        template = "orange"
        title = "飞书授权已过期"
        content = f"当前授权已失效。重新授权后，{assistant_name} 会继续自动保存和刷新 token。"
    elif status.missing_scopes:
        template = "orange"
        title = "需要补充授权权限"
        content = (
            f"当前授权缺少 {len(status.missing_scopes)} 项权限。"
            "重新授权后即可继续使用资源读取。"
        )
    else:
        template = "orange"
        title = "飞书授权不可用"
        content = "当前授权不可用。重新授权后即可继续读取你有权限的飞书资源。"

    elements: list[dict[str, object]] = [
        {"tag": "markdown", "content": content},
    ]
    if authorization_url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "点击授权"},
                        "type": "primary",
                        "url": authorization_url,
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def _admin_open_card(admin_url: str, *, assistant_name: str = "小智") -> dict[str, object]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "本地配置网页"},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"打开 {assistant_name} 的本地配置网页。"
                    "首次打开需要使用飞书登录；首次登录的飞书用户会绑定为本机后台管理员。"
                ),
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "打开配置后台"},
                        "type": "primary",
                        "url": admin_url,
                    }
                ],
            },
        ],
    }


def _admin_url(settings: Settings) -> str:
    return f"{settings.base_url.rstrip('/')}/admin"


def _is_undo_command(text: str) -> bool:
    stripped = text.strip()
    return stripped in {
        "/撤回",
        "/写入 撤回",
        "/写回 撤回",
        "撤回",
        "撤回上一次写入",
        "撤回最近写入",
        "撤回上次写入",
        "撤回上一次写回",
        "撤回最近写回",
        "撤回上次写回",
    }


def _is_writeback_history_command(text: str) -> bool:
    return text.strip() in {
        "/查看最近写入",
        "/查看最近写回",
        "/写入 历史",
        "/写回 历史",
        "查看最近写入",
        "查看最近写回",
        "最近写入",
        "最近写回",
    }


def _writeback_history_status(item: dict[str, object]) -> tuple[str, str]:
    if item.get("reverted_at"):
        return "已撤回", ""
    if item.get("reversible"):
        return "可撤回", ""
    action_type = str(item.get("action_type") or "")
    reasons = {
        WriteActionType.DOC_CREATE.value: "创建后的文档不自动删除",
        WriteActionType.DOC_APPEND.value: "无法可靠确认新增块边界",
        WriteActionType.DOC_DELETE_BLOCK.value: "撤回动作不会再次生成撤回",
        WriteActionType.BITABLE_UPDATE_RECORD.value: "未保存更新前字段快照",
        WriteActionType.BITABLE_DELETE_RECORD.value: "删除操作不可自动恢复",
        WriteActionType.MESSAGE_SEND.value: "飞书消息默认不自动撤回",
    }
    return "不可撤回", reasons.get(action_type, "没有安全的回滚信息")


def _writeback_target_summary(action_type: str, target: object) -> str:
    if not isinstance(target, dict):
        return "未知目标"
    if action_type in {
        WriteActionType.BITABLE_CREATE_RECORD.value,
        WriteActionType.BITABLE_UPDATE_RECORD.value,
        WriteActionType.BITABLE_DELETE_RECORD.value,
    }:
        return f"多维表 {target.get('table_id') or '未知数据表'}"
    if action_type == WriteActionType.SHEET_WRITE_RANGE.value:
        return f"电子表格 {target.get('range') or '未知范围'}"
    if action_type in {
        WriteActionType.DOC_CREATE.value,
        WriteActionType.DOC_APPEND.value,
        WriteActionType.DOC_DELETE_BLOCK.value,
    }:
        return f"文档 {target.get('document_id') or target.get('folder_token') or '默认位置'}"
    if action_type == WriteActionType.MESSAGE_SEND.value:
        return "飞书消息"
    return "未知目标"


def _enriched_undo_target(latest: dict[str, Any]) -> dict[str, Any]:
    target = dict(latest.get("undo_target") or {})
    original = latest.get("target")
    if not isinstance(original, dict):
        return target
    for key in ("title", "target_title", "url", "target_url", "type", "token"):
        if not target.get(key) and original.get(key):
            target[key] = original[key]
    return target


def _target_title_from_mapping(target: dict[str, Any]) -> str | None:
    for key in ("target_title", "title", "name", "document_title"):
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _target_url_from_mapping(target: dict[str, Any]) -> str | None:
    for key in ("target_url", "url", "link"):
        value = target.get(key)
        if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
            return value.strip()
    return None


def _friendly_undo_preview(latest: dict[str, Any]) -> str:
    action_type = str(latest.get("undo_action_type") or "")
    if action_type == WriteActionType.DOC_DELETE_BLOCK.value:
        target = _enriched_undo_target(latest)
        title = _target_title_from_mapping(target)
        target_text = f"《{title}》" if title else "目标文档"
        original_payload = latest.get("payload")
        content = ""
        if isinstance(original_payload, dict):
            raw_content = original_payload.get("content")
            content = raw_content if isinstance(raw_content, str) else ""
        if content.strip():
            return f"撤回上一次写入：从{target_text}删除刚刚新增的文字：\n{_trim_text(content)}"
        return f"撤回上一次写入：从{target_text}删除刚刚新增的内容。"
    preview = latest.get("undo_preview")
    if isinstance(preview, str) and preview.strip():
        return preview.replace("写回", "写入")
    return "撤回上一次写入"


def _trim_text(text: str, max_chars: int = 400) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return f"{cleaned[: max_chars - 12].rstrip()}...（已截断）"


def _write_action_label(action_type: str) -> str:
    labels = {
        WriteActionType.DOC_CREATE.value: "创建文档",
        WriteActionType.DOC_APPEND.value: "追加文档",
        WriteActionType.DOC_DELETE_BLOCK.value: "撤回文档写入",
        WriteActionType.SHEET_WRITE_RANGE.value: "写入电子表格",
        WriteActionType.BITABLE_CREATE_RECORD.value: "新增多维表记录",
        WriteActionType.BITABLE_UPDATE_RECORD.value: "更新多维表记录",
        WriteActionType.BITABLE_DELETE_RECORD.value: "删除多维表记录",
        WriteActionType.MESSAGE_SEND.value: "发送消息",
    }
    return labels.get(action_type, action_type)


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


def _writeback_disabled_text(text: str) -> str:
    cleaned = text.strip()
    notice = "写入功能当前已暂停，我不会创建写回卡片或执行写入。"
    if not cleaned:
        return notice
    if "我已准备好写回预览" in cleaned or "请在卡片中确认" in cleaned:
        return notice
    return f"{cleaned}\n\n{notice}"


def _draft_only_writeback_text(response: AssistantResponse) -> str:
    lines = []
    if response.text.strip():
        lines.append(response.text.strip())
    lines.append("当前写入确认策略为仅生成草稿，不会保存待确认动作，也不会执行写入。")
    for proposal in response.action_proposals:
        target = proposal.target_title or proposal.target_url or "目标资源"
        lines.append(f"- {_write_action_label(proposal.action_type.value)} · {target}")
        if proposal.preview.strip():
            lines.append(proposal.preview.strip())
    return "\n".join(lines)


def _is_low_risk_direct_writeback(proposal: ActionProposal) -> bool:
    if proposal.action_type != WriteActionType.DOC_APPEND:
        return False
    document_id = str(proposal.target.get("document_id") or "").strip()
    block_id = str(proposal.target.get("block_id") or "").strip()
    index = _int_or_none(proposal.target.get("index", -1))
    if document_id and not block_id and index == -1:
        return True
    return bool(document_id and block_id == document_id and index == 0)


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _should_route(message: FeishuMessage) -> bool:
    if message.conversation_type.value == "private":
        return True
    return message.is_bot_mentioned


def _parse_model_command(text: str) -> str | None:
    return _parse_prefixed_command(text, "/模型", "模型")


def _parse_writeback_command(text: str) -> str | None:
    return _parse_prefixed_command(text, "/写入", "写入", "/写回", "写回")


def _parse_prefixed_command(text: str, *prefixes: str) -> str | None:
    stripped = text.strip()
    for prefix in prefixes:
        if stripped == prefix:
            return ""
        if stripped.startswith(f"{prefix} "):
            return stripped.removeprefix(prefix).strip()
    return None


def _parse_assistant_name_command(command: str) -> str | None:
    for prefix in ("名称", "名字", "命名", "设置名称", "设置名字", "改名", "叫"):
        if command == prefix:
            return ""
        if command.startswith(f"{prefix} "):
            return command.removeprefix(prefix).strip()
    return None


def _parse_assistant_profile_command(command: str) -> str | None:
    for prefix in ("简介", "人设", "设置简介", "设置人设", "角色", "语言风格"):
        if command == prefix:
            return ""
        if command.startswith(f"{prefix} "):
            return command.removeprefix(prefix).strip()
    return None


def _clean_assistant_name(name: str) -> tuple[str, str | None]:
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return "", "助手名称不能为空。用法：/助手 命名 小智"
    if len(cleaned) > 20:
        return "", "助手名称太长了，请控制在 20 个字符以内。"
    if any(marker in cleaned for marker in ("```", "<script", "</", "[", "]", "(", ")")):
        return "", "助手名称包含不支持的字符，请换一个更简短的名称。"
    return cleaned, None


def _clean_assistant_profile(profile: str) -> tuple[str, str | None]:
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", profile).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if not cleaned:
        return "", "助手简介不能为空。用法：/助手 简介 简洁、直接，擅长整理飞书文档。"
    if len(cleaned) > 500:
        return "", "助手简介太长了，请控制在 500 个字符以内。"
    if any(marker in cleaned.lower() for marker in ("```", "<script", "</script")):
        return "", "助手简介包含不支持的内容，请换成简短的自然语言描述。"
    return cleaned, None


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


def _conversation_context_scope(message: FeishuMessage) -> str:
    return f"conversation:{message.conversation_key or message.chat_id}"


def _request_model_provider(
    model_preference: ModelPreference | None,
    attachments: list[AssistantAttachment],
    *,
    settings: Settings,
) -> str | None:
    if _has_media_attachment(attachments):
        return settings.attachment_media_understanding_provider.strip() or None
    if _has_image_attachment(attachments):
        return settings.attachment_vision_provider.strip() or None
    return model_preference.provider if model_preference else None


def _request_model(
    model_preference: ModelPreference | None,
    attachments: list[AssistantAttachment],
    *,
    settings: Settings,
) -> str | None:
    if _has_media_attachment(attachments):
        return settings.attachment_media_understanding_model
    if _has_image_attachment(attachments):
        return settings.attachment_vision_model
    return model_preference.model if model_preference else None


def _attachment_model_understanding_enabled(
    attachment: FeishuMessageAttachment,
    settings: Settings,
) -> bool:
    if attachment.type == "image":
        return settings.attachment_vision_enabled
    if attachment.type in {"audio", "video"}:
        return settings.attachment_media_understanding_enabled
    return False


def _attachment_max_bytes(attachment: FeishuMessageAttachment, settings: Settings) -> int:
    if attachment.type == "image":
        return settings.attachment_vision_max_bytes
    return settings.attachment_media_understanding_max_bytes


def _message_resource_type(attachment: FeishuMessageAttachment) -> str:
    if attachment.type in {"audio", "video"}:
        return "file"
    return "image"


def _assistant_attachment_type(
    attachment: FeishuMessageAttachment,
) -> Literal["image", "audio", "video"]:
    if attachment.type == "audio":
        return "audio"
    if attachment.type == "video":
        return "video"
    return "image"


def _default_attachment_media_type(attachment: FeishuMessageAttachment) -> str:
    if attachment.type == "audio":
        return "audio/mpeg"
    if attachment.type == "video":
        return "video/mp4"
    return "image/png"


def _has_media_attachment(attachments: list[AssistantAttachment]) -> bool:
    return any(attachment.type in {"audio", "video"} for attachment in attachments)


def _has_image_attachment(attachments: list[AssistantAttachment]) -> bool:
    return any(attachment.type == "image" for attachment in attachments)


def _resolved_model_spec(
    model_router: ModelRouter,
    preference: ModelPreference | None,
) -> str:
    provider = preference.provider if preference is not None else model_router.default_provider
    model = preference.model if preference is not None else model_router.default_model
    if not model:
        catalog_item = find_catalog_item(model_router.catalog, provider)
        if catalog_item is not None:
            model = catalog_item.model
    return f"{provider}/{model or 'provider-default'}"


def _model_command_help(assistant_name: str = "小智") -> str:
    return (
        f"{assistant_name} 的模型指令：\n"
        "- /模型 查看\n\n"
        "模型切换请通过飞书机器人自定义菜单操作。"
    )


def _model_menu_only_text(assistant_name: str = "小智") -> str:
    return (
        f"{assistant_name} 的模型切换请通过飞书机器人自定义菜单操作。"
        "可发送 /模型 查看 检查当前配置。"
    )


def _writeback_command_help(assistant_name: str = "小智") -> str:
    return (
        f"{assistant_name} 的写入指令：\n"
        "- /写入 状态\n"
        "- /写入 自动开启\n"
        "- /写入 自动关闭\n"
        "- /写入 自动清除\n"
        "- /写入 历史\n"
        "- /撤回"
    )


def _writeback_confirmation_mode_label(mode: WritebackConfirmationMode) -> str:
    labels = {
        WritebackConfirmationMode.ALWAYS: "每次确认",
        WritebackConfirmationMode.LOW_RISK_DIRECT: "低风险自动执行",
        WritebackConfirmationMode.DRAFT_ONLY: "仅生成草稿",
    }
    return labels[mode]


def _assistant_command_help() -> str:
    return (
        "助手信息指令：\n"
        "- /助手 信息\n"
        "- /助手 名称 小智\n"
        "- /助手 简介 简洁、直接，擅长整理飞书文档\n"
        "- /助手 恢复默认"
    )


def _menu_receive_target(event: FeishuBotMenuEvent) -> tuple[str, str]:
    if event.operator_open_id:
        return event.operator_open_id, "open_id"
    if event.operator_user_id:
        return event.operator_user_id, "user_id"
    return "", ""


def _menu_actor_id(event: FeishuBotMenuEvent) -> str:
    return event.operator_open_id or event.operator_user_id or event.operator_union_id


def _menu_message(event: FeishuBotMenuEvent) -> FeishuMessage:
    actor_id = _menu_actor_id(event)
    return FeishuMessage(
        message_id=f"menu:{event.event_id or event.timestamp or event.event_key}",
        chat_id="",
        sender_id=actor_id,
        text="",
        conversation_type=ConversationType.PRIVATE,
        conversation_key=f"user:{actor_id}",
        raw=event.raw,
    )


def _menu_user_scope(event: FeishuBotMenuEvent) -> str:
    return f"user:{_menu_actor_id(event)}"


def _menu_idempotency_key(event: FeishuBotMenuEvent) -> str:
    if event.event_id:
        return f"feishu_bot_menu:{event.event_id}"
    return f"feishu_bot_menu:{event.operator_open_id}:{event.event_key}:{event.timestamp}"


def _menu_help_text(assistant_name: str = "小智") -> str:
    return (
        f"{assistant_name} 菜单入口：\n"
        "- 助手：查看当前助手信息。\n"
        "- 模型：查看可用模型，或设置你的个人默认模型。\n"
        "- 授权：获取飞书 OAuth 授权链接，或查看授权状态。\n"
        "- 写入：查看写入策略、开启/关闭自动写入、查看最近写入或撤回。\n"
        "- 记忆：查看、删除、关闭或开启你的长期记忆。\n"
        "- 帮助：查看当前菜单说明，或打开本地配置网页。\n\n"
        "未放入当前菜单但仍可直接发送：\n"
        "- /配置 或 /后台：打开本地配置网页\n"
        "- /助手 名称 小飞：设置你的个人助手名称\n"
        "- /助手 简介 简洁、直接，擅长整理飞书文档：设置助手简介\n"
        "- /助手 恢复默认：恢复默认助手名称和简介\n"
        "- /上下文 查看：查看上下文读取策略\n"
        "- /记忆 记住 输出格式=优先表格：保存一条长期记忆，需要确认\n"
        "- /记忆 修改 语言风格=简洁中文：修改记忆，需要确认\n"
        "- /记忆 删除 语言风格：删除单条记忆\n"
        "- /写入 状态、/写入 历史、/撤回：写入相关文本入口"
    )


def _context_status_text(assistant_name: str = "小智") -> str:
    return (
        f"{assistant_name} 的上下文默认开启，无需手动开关。\n"
        "近期消息会按当前会话范围全文进入回答上下文；超出近期窗口的旧消息会压缩成会话摘要。"
        "长期记忆开启时，私聊会话摘要会出现在“查看记忆”里，但不会保存完整聊天原文。"
    )


def _privacy_command_help(assistant_name: str = "小智") -> str:
    return (
        f"{assistant_name} 的隐私控制指令：\n"
        "- /授权\n"
        "- /授权 状态\n"
        "- /上下文 查看\n"
        "- /写入 状态\n"
        "- /写入 自动开启\n"
        "- /写入 自动关闭\n"
        "- /记忆 查看\n"
        "- /记忆 记住 输出格式=优先表格\n"
        "- /记忆 修改 语言风格=简洁中文\n"
        "- /记忆 删除 语言风格\n"
        "- /记忆 删除\n"
        "- /记忆 关闭\n"
        "- /记忆 开启"
    )


def _memory_save_confirmation_card(
    *,
    action_id: str,
    key: str,
    kind: str,
    content: str,
) -> dict[str, object]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "确认保存长期记忆"},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    "我识别到一条可能有用的长期记忆。确认后才会保存，"
                    "取消则不会写入长期记忆。\n\n"
                    f"- {kind}：{content}"
                ),
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "保存"},
                        "type": "primary",
                        "value": {
                            "action": "confirm",
                            "fcgo_action": "fcgo.memory.save.confirm",
                            "action_id": action_id,
                            "memory_key": key,
                            "memory_kind": kind,
                            "memory_content": content,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "取消"},
                        "type": "default",
                        "value": {
                            "action": "cancel",
                            "fcgo_action": "fcgo.memory.save.cancel",
                            "action_id": action_id,
                        },
                    },
                ],
            },
        ],
    }


def _memory_delete_confirmation_card(action_id: str) -> dict[str, object]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": "确认删除长期记忆"},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    "此操作会清空你当前保存的长期记忆。"
                    "删除后，记忆功能状态保持不变，但已删除内容不会再进入模型上下文。"
                ),
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "确认删除"},
                        "type": "danger",
                        "value": {
                            "action": "confirm",
                            "fcgo_action": "fcgo.memory.delete.confirm",
                            "action_id": action_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "取消"},
                        "type": "default",
                        "value": {
                            "action": "cancel",
                            "fcgo_action": "fcgo.memory.delete.cancel",
                            "action_id": action_id,
                        },
                    },
                ],
            },
        ],
    }
