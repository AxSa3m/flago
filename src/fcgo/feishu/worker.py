import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future, TimeoutError
from typing import Any

import lark_oapi as lark
from lark_oapi.api.application.v6.model.p2_application_bot_menu_v6 import (
    P2ApplicationBotMenuV6,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from lark_oapi.event.dispatcher_handler import P2ImMessageMessageReadV1, P2ImMessageReceiveV1

from fcgo.config import Settings
from fcgo.feishu.card_event import card_action_response, parse_card_action_event
from fcgo.feishu.menu import parse_bot_menu_event
from fcgo.feishu.message import parse_text_message
from fcgo.feishu.router import FeishuMessageRouter
from fcgo.models import ConfirmationResult
from fcgo.writeback.service import WritebackService

logger = logging.getLogger(__name__)


class FeishuLongConnectionWorker:
    def __init__(
        self,
        settings: Settings,
        router: FeishuMessageRouter,
        writeback: WritebackService | None = None,
    ) -> None:
        self.settings = settings
        self.router = router
        self.writeback = writeback

    def run_forever(self) -> None:
        event_handler = (
            lark.EventDispatcherHandler.builder(
                self.settings.feishu_verification_token.get_secret_value(),
                self.settings.feishu_encrypt_key.get_secret_value(),
            )
            .register_p2_im_message_receive_v1(self._handle_message_event)
            .register_p2_im_message_message_read_v1(self._handle_message_read_event)
            .register_p2_application_bot_menu_v6(self._handle_bot_menu_event)
            .register_p2_card_action_trigger(self._handle_card_action_trigger)
            .build()
        )
        cli = (
            lark.ws.Client(
                self.settings.feishu_app_id,
                self.settings.feishu_app_secret.get_secret_value(),
                event_handler=event_handler,
                log_level=lark.LogLevel.INFO,
            )
        )
        cli.start()

    def _handle_message_event(self, data: P2ImMessageReceiveV1) -> None:
        try:
            message = parse_text_message(
                data,
                bot_open_id=self.settings.feishu_bot_open_id,
                bot_name=self.settings.feishu_bot_name,
            )
        except Exception:
            logger.exception("feishu_message_parse_failed")
            return
        if message is None:
            return
        logger.info(
            "feishu_message_received message_id=%s chat_id=%s",
            message.message_id,
            message.chat_id,
        )
        _run_or_schedule(self.router.handle_message(message), "feishu_message_event_failed")

    def _handle_message_read_event(self, data: P2ImMessageMessageReadV1) -> None:
        logger.debug("feishu_message_read_event_ignored data_type=%s", type(data).__name__)

    def _handle_bot_menu_event(self, data: P2ApplicationBotMenuV6) -> None:
        try:
            event = parse_bot_menu_event(data)
        except Exception:
            logger.exception("feishu_bot_menu_parse_failed")
            return
        if event is None:
            return
        logger.info(
            "feishu_bot_menu_clicked event_id=%s event_key=%s operator_open_id=%s",
            event.event_id,
            event.event_key,
            event.operator_open_id,
        )
        _run_or_schedule(self.router.handle_bot_menu(event), "feishu_bot_menu_event_failed")

    def _handle_card_action_trigger(
        self,
        data: P2CardActionTrigger,
    ) -> P2CardActionTriggerResponse:
        if self.writeback is None:
            logger.error("feishu_card_action_writeback_not_configured")
            return card_action_response("error", "写回服务暂未启用")
        try:
            callback = parse_card_action_event(data)
        except Exception:
            logger.exception("feishu_card_action_parse_failed")
            return card_action_response("error", "卡片操作解析失败")
        try:
            if callback.action == "confirm":
                result = _run_sync_with_timeout(
                    self.writeback.confirm(callback.action_id, callback.actor_id),
                    timeout_seconds=self.settings.card_action_ack_timeout_seconds,
                    on_timeout=lambda: ConfirmationResult(
                        status="accepted",
                        message="已收到确认，正在执行写回。",
                    ),
                )
            else:
                result = _run_sync(
                    self.writeback.cancel(callback.action_id, callback.actor_id),
                )
        except Exception:
            logger.exception(
                "feishu_card_action_failed action_id=%s action=%s actor_id=%s",
                callback.action_id,
                callback.action,
                callback.actor_id,
            )
            return card_action_response("error", "写回执行失败，请稍后重试")
        logger.info(
            "feishu_card_action_handled action_id=%s action=%s actor_id=%s status=%s",
            callback.action_id,
            callback.action,
            callback.actor_id,
            result.status,
        )
        return card_action_response(result.status, result.message)


def _run_or_schedule(coro: Coroutine[Any, Any, None], failure_log_name: str) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(coro)
        except Exception:
            logger.exception(failure_log_name)
        return

    task = loop.create_task(coro)
    task.add_done_callback(lambda completed: _log_task_failure(completed, failure_log_name))


def _log_task_failure(task: asyncio.Task[None], failure_log_name: str) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        logger.warning("%s_cancelled", failure_log_name)
    except Exception:
        logger.exception(failure_log_name)


def _run_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    future: Future[Any] = Future()

    def runner() -> None:
        try:
            future.set_result(asyncio.run(coro))
        except Exception as exc:  # noqa: BLE001 - propagate to sync callback
            future.set_exception(exc)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return future.result()


def _run_sync_with_timeout(
    coro: Coroutine[Any, Any, Any],
    *,
    timeout_seconds: float,
    on_timeout: Callable[[], Any],
) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        future = _run_in_background_thread(coro)
        try:
            return future.result(timeout=timeout_seconds)
        except TimeoutError:
            _log_future_failure(future, "feishu_card_action_background_failed")
            return on_timeout()

    future = _run_in_background_thread(coro)
    try:
        return future.result(timeout=timeout_seconds)
    except TimeoutError:
        _log_future_failure(future, "feishu_card_action_background_failed")
        return on_timeout()


def _run_in_background_thread(coro: Coroutine[Any, Any, Any]) -> Future[Any]:
    future: Future[Any] = Future()

    def runner() -> None:
        try:
            future.set_result(asyncio.run(coro))
        except Exception as exc:  # noqa: BLE001 - propagate to waiting caller or log callback
            future.set_exception(exc)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return future


def _log_future_failure(future: Future[Any], failure_log_name: str) -> None:
    def on_done(completed: Future[Any]) -> None:
        try:
            completed.result()
        except Exception:  # noqa: BLE001 - background callback should only log failures
            logger.exception(failure_log_name)

    future.add_done_callback(on_done)
