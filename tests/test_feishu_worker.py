import asyncio

import pytest
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

from fcgo.config import Settings
from fcgo.feishu.worker import (
    FeishuLongConnectionWorker,
    _configure_lark_ws_proxy,
    _run_or_schedule,
    _run_sync,
)


def test_run_or_schedule_without_running_loop() -> None:
    called = False

    async def work() -> None:
        nonlocal called
        called = True

    _run_or_schedule(work(), "test_failure")

    assert called


def test_configure_lark_ws_proxy_overrides_sdk_direct_ws_default(monkeypatch) -> None:
    from lark_oapi.ws import client as lark_ws_client

    captured_kwargs = {}

    def fake_post(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return None

    monkeypatch.setattr(lark_ws_client, "_ws_connect_kwargs", lambda: {"proxy": None})
    monkeypatch.setattr(lark_ws_client.requests, "post", fake_post)

    _configure_lark_ws_proxy(
        Settings(env="test", feishu_http_proxy="http://127.0.0.1:7890")
    )

    assert lark_ws_client._ws_connect_kwargs() == {"proxy": "http://127.0.0.1:7890"}
    lark_ws_client.requests.post("https://open.feishu.cn/example")
    assert captured_kwargs["proxies"] == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


@pytest.mark.asyncio
async def test_run_or_schedule_inside_running_loop() -> None:
    called = asyncio.Event()

    async def work() -> None:
        called.set()

    _run_or_schedule(work(), "test_failure")

    await asyncio.wait_for(called.wait(), timeout=1)


def test_worker_handles_card_action_trigger() -> None:
    writeback = RecordingWriteback()
    worker = FeishuLongConnectionWorker(Settings(env="test"), router=None, writeback=writeback)

    response = worker._handle_card_action_trigger(
        P2CardActionTrigger(
            {
                "schema": "2.0",
                "header": {"event_id": "evt-1"},
                "event": {
                    "operator": {"open_id": "ou_user"},
                    "action": {
                        "value": {
                            "fcgo_action": "writeback.confirm",
                            "action_id": "action-1",
                        }
                    },
                },
            }
        )
    )

    assert writeback.confirm_calls == [("action-1", "ou_user")]
    assert response.toast is not None
    assert response.toast.content == "写回已执行"


def test_worker_handles_memory_delete_card_without_writeback() -> None:
    router = RecordingRouter()
    worker = FeishuLongConnectionWorker(Settings(env="test"), router=router, writeback=None)

    response = worker._handle_card_action_trigger(
        P2CardActionTrigger(
            {
                "schema": "2.0",
                "header": {"event_id": "evt-1"},
                "event": {
                    "operator": {"open_id": "ou_user"},
                    "action": {
                        "value": {
                            "fcgo_action": "fcgo.memory.delete.confirm",
                            "action_id": "memory-delete-1",
                        }
                    },
                },
            }
        )
    )

    assert router.memory_delete_calls == ["ou_user"]
    assert response.toast is not None
    assert response.toast.content == "已删除你的长期记忆，共 1 条。长期记忆功能仍保持开启。"


def test_worker_cancels_memory_delete_card_without_writeback() -> None:
    router = RecordingRouter()
    worker = FeishuLongConnectionWorker(Settings(env="test"), router=router, writeback=None)

    response = worker._handle_card_action_trigger(
        P2CardActionTrigger(
            {
                "schema": "2.0",
                "header": {"event_id": "evt-1"},
                "event": {
                    "operator": {"open_id": "ou_user"},
                    "action": {
                        "value": {
                            "fcgo_action": "fcgo.memory.delete.cancel",
                            "action_id": "memory-delete-1",
                        }
                    },
                },
            }
        )
    )

    assert router.memory_delete_calls == []
    assert response.toast is not None
    assert response.toast.content == "已取消删除长期记忆。"


def test_worker_handles_memory_save_card_without_writeback() -> None:
    router = RecordingRouter()
    worker = FeishuLongConnectionWorker(Settings(env="test"), router=router, writeback=None)

    response = worker._handle_card_action_trigger(
        P2CardActionTrigger(
            {
                "schema": "2.0",
                "header": {"event_id": "evt-1"},
                "event": {
                    "operator": {"open_id": "ou_user"},
                    "action": {
                        "value": {
                            "fcgo_action": "fcgo.memory.save.confirm",
                            "action_id": "memory-save-1",
                            "memory_key": "nickname",
                            "memory_kind": "称呼",
                            "memory_content": "你叫Sa3m。",
                        }
                    },
                },
            }
        )
    )

    assert router.memory_save_calls == [
        {
            "actor_id": "ou_user",
            "key": "nickname",
            "kind": "称呼",
            "content": "你叫Sa3m。",
        }
    ]
    assert response.toast is not None
    assert response.toast.content == "已保存长期记忆：称呼：你叫Sa3m。"


def test_worker_cancels_memory_save_card_without_writeback() -> None:
    router = RecordingRouter()
    worker = FeishuLongConnectionWorker(Settings(env="test"), router=router, writeback=None)

    response = worker._handle_card_action_trigger(
        P2CardActionTrigger(
            {
                "schema": "2.0",
                "header": {"event_id": "evt-1"},
                "event": {
                    "operator": {"open_id": "ou_user"},
                    "action": {
                        "value": {
                            "fcgo_action": "fcgo.memory.save.cancel",
                            "action_id": "memory-save-1",
                        }
                    },
                },
            }
        )
    )

    assert router.memory_save_calls == []
    assert response.toast is not None
    assert response.toast.content == "已取消保存长期记忆。"


def test_worker_returns_fast_ack_when_confirm_is_slow() -> None:
    writeback = SlowRecordingWriteback()
    worker = FeishuLongConnectionWorker(
        Settings(env="test", card_action_ack_timeout_seconds=0.01),
        router=None,
        writeback=writeback,
    )

    response = worker._handle_card_action_trigger(
        P2CardActionTrigger(
            {
                "schema": "2.0",
                "header": {"event_id": "evt-1"},
                "event": {
                    "operator": {"open_id": "ou_user"},
                    "action": {
                        "value": {
                            "fcgo_action": "writeback.confirm",
                            "action_id": "action-1",
                        }
                    },
                },
            }
        )
    )

    assert writeback.confirm_calls == [("action-1", "ou_user")]
    assert response.toast is not None
    assert response.toast.content == "已收到确认，正在执行写回。"


@pytest.mark.asyncio
async def test_run_sync_inside_running_loop() -> None:
    async def work() -> str:
        return "ok"

    assert _run_sync(work()) == "ok"


class Result:
    status = "executed"
    message = "写回已执行"


class RecordingWriteback:
    def __init__(self) -> None:
        self.confirm_calls = []

    async def confirm(self, action_id: str, actor_id: str) -> Result:
        self.confirm_calls.append((action_id, actor_id))
        return Result()

    async def cancel(self, action_id: str, actor_id: str) -> Result:
        raise AssertionError("cancel should not be called")


class SlowRecordingWriteback(RecordingWriteback):
    async def confirm(self, action_id: str, actor_id: str) -> Result:
        self.confirm_calls.append((action_id, actor_id))
        await asyncio.sleep(0.05)
        return Result()


class RecordingRouter:
    def __init__(self) -> None:
        self.memory_delete_calls: list[str] = []
        self.memory_save_calls: list[dict[str, str]] = []

    async def confirm_memory_delete(self, actor_id: str) -> str:
        self.memory_delete_calls.append(actor_id)
        return "已删除你的长期记忆，共 1 条。长期记忆功能仍保持开启。"

    async def confirm_memory_save(
        self,
        actor_id: str,
        *,
        key: str,
        kind: str,
        content: str,
    ) -> str:
        self.memory_save_calls.append(
            {
                "actor_id": actor_id,
                "key": key,
                "kind": kind,
                "content": content,
            }
        )
        return f"已保存长期记忆：{kind}：{content}"
