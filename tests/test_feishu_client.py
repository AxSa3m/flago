from types import SimpleNamespace
from typing import Any

import pytest

from flgo.config import Settings
from flgo.feishu import client as feishu_client_module
from flgo.feishu.client import FeishuClient


@pytest.mark.asyncio
async def test_send_text_retries_transient_transport_failure(monkeypatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(feishu_client_module.asyncio, "sleep", fake_sleep)
    message_resource = FlakyMessageResource()
    client = FeishuClient(
        Settings(
            env="test",
            feishu_app_id="app_id",
            feishu_app_secret="app_secret",
            gemini_api_key="",
        )
    )
    client.client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=message_resource))
    )

    await client.send_text("ou_user", "hello", receive_id_type="open_id")

    assert message_resource.calls == 2
    assert sleeps == [1.0]


class FlakyMessageResource:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, request: Any) -> "SuccessfulResponse":
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient proxy failure")
        return SuccessfulResponse()


class SuccessfulResponse:
    code = 0
    msg = "ok"

    def success(self) -> bool:
        return True

    def get_log_id(self) -> str:
        return "log-id"
