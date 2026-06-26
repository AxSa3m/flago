import asyncio
import json
import logging
import os
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from fcgo.config import Settings

logger = logging.getLogger(__name__)

_SEND_MAX_ATTEMPTS = 3
_SEND_RETRY_SECONDS = 1.0


class FeishuClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.feishu_app_id or not settings.feishu_app_secret.get_secret_value():
            raise ValueError("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
        self.settings = settings
        if settings.feishu_http_proxy:
            os.environ["HTTP_PROXY"] = settings.feishu_http_proxy
            os.environ["HTTPS_PROXY"] = settings.feishu_http_proxy
            os.environ["http_proxy"] = settings.feishu_http_proxy
            os.environ["https_proxy"] = settings.feishu_http_proxy
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
        text = _sanitize_text_content(text)
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
        response = await self._create_message_with_retry(request, operation="text")
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
        await self.send_interactive_card_to_receive_id(
            chat_id,
            card,
            receive_id_type="chat_id",
        )

    async def send_interactive_card_to_open_id(
        self,
        open_id: str,
        card: dict[str, Any],
    ) -> None:
        await self.send_interactive_card_to_receive_id(
            open_id,
            card,
            receive_id_type="open_id",
        )

    async def send_interactive_card_to_user_id(
        self,
        user_id: str,
        card: dict[str, Any],
    ) -> None:
        await self.send_interactive_card_to_receive_id(
            user_id,
            card,
            receive_id_type="user_id",
        )

    async def send_interactive_card_to_receive_id(
        self,
        receive_id: str,
        card: dict[str, Any],
        *,
        receive_id_type: str,
    ) -> None:
        content = json.dumps(card, ensure_ascii=False)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("interactive")
                .content(content)
                .build()
            )
            .build()
        )
        response = await self._create_message_with_retry(request, operation="card")
        if not response.success():
            logger.error(
                "feishu_card_send_failed code=%s msg=%s log_id=%s",
                response.code,
                response.msg,
                response.get_log_id(),
            )
            raise RuntimeError(f"Feishu card send failed: {response.code} {response.msg}")
        logger.info(
            "feishu_card_sent receive_id_type=%s receive_id=%s log_id=%s",
            receive_id_type,
            receive_id,
            response.get_log_id(),
        )

    async def _create_message_with_retry(
        self,
        request: CreateMessageRequest,
        *,
        operation: str,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, _SEND_MAX_ATTEMPTS + 1):
            try:
                return self.client.im.v1.message.create(request)
            except Exception as exc:  # noqa: BLE001 - SDK raises transport-specific errors
                last_error = exc
                if attempt >= _SEND_MAX_ATTEMPTS:
                    break
                logger.warning(
                    "feishu_message_send_retry operation=%s attempt=%s error_type=%s",
                    operation,
                    attempt,
                    type(exc).__name__,
                )
                await asyncio.sleep(_SEND_RETRY_SECONDS)
        assert last_error is not None
        raise last_error


def _sanitize_text_content(text: str) -> str:
    return "".join(
        char
        for char in text
        if char in {"\n", "\r", "\t"} or ord(char) >= 32
    )
