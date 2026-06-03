import json
import logging
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from fcgo.config import Settings

logger = logging.getLogger(__name__)


class FeishuClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.feishu_app_id or not settings.feishu_app_secret.get_secret_value():
            raise ValueError("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
        self.settings = settings
        self.client = (
            lark.Client.builder()
            .app_id(settings.feishu_app_id)
            .app_secret(settings.feishu_app_secret.get_secret_value())
            .domain(settings.feishu_base_url)
            .log_level(lark.LogLevel.INFO)
            .build()
        )

    async def reply_text(self, chat_id: str, text: str) -> None:
        await self.send_text(chat_id, text, receive_id_type="chat_id")

    async def send_text_to_open_id(self, open_id: str, text: str) -> None:
        await self.send_text(open_id, text, receive_id_type="open_id")

    async def send_text_to_user_id(self, user_id: str, text: str) -> None:
        await self.send_text(user_id, text, receive_id_type="user_id")

    async def send_text(self, receive_id: str, text: str, *, receive_id_type: str) -> None:
        content = json.dumps({"text": text}, ensure_ascii=False)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("text")
                .content(content)
                .build()
            )
            .build()
        )
        response = self.client.im.v1.message.create(request)
        if not response.success():
            logger.error(
                "feishu_reply_failed code=%s msg=%s log_id=%s",
                response.code,
                response.msg,
                response.get_log_id(),
            )
            raise RuntimeError(f"Feishu reply failed: {response.code} {response.msg}")
        logger.info(
            "feishu_text_sent receive_id_type=%s receive_id=%s log_id=%s",
            receive_id_type,
            receive_id,
            response.get_log_id(),
        )

    async def send_interactive_card(self, chat_id: str, card: dict[str, Any]) -> None:
        content = json.dumps(card, ensure_ascii=False)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(content)
                .build()
            )
            .build()
        )
        response = self.client.im.v1.message.create(request)
        if not response.success():
            logger.error(
                "feishu_card_send_failed code=%s msg=%s log_id=%s",
                response.code,
                response.msg,
                response.get_log_id(),
            )
            raise RuntimeError(f"Feishu card send failed: {response.code} {response.msg}")
        logger.info(
            "feishu_card_sent chat_id=%s log_id=%s",
            chat_id,
            response.get_log_id(),
        )
