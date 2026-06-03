import asyncio

import pytest
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

from fcgo.config import Settings
from fcgo.feishu.worker import FeishuLongConnectionWorker, _run_or_schedule, _run_sync


def test_run_or_schedule_without_running_loop() -> None:
    called = False

    async def work() -> None:
        nonlocal called
        called = True

    _run_or_schedule(work(), "test_failure")

    assert called


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
