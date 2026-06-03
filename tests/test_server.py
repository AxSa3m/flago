import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import respx
from fastapi.testclient import TestClient
from httpx import Response

from fcgo.config import Settings
from fcgo.models import ActionProposal, PendingActionStatus, WriteActionType
from fcgo.server import create_app
from fcgo.storage import SQLiteStore
from fcgo.writeback.executor import FeishuWriteExecutor


def test_healthz(tmp_path) -> None:
    settings = Settings(
        env="test",
        sqlite_path=tmp_path / "fcgo.sqlite3",
        gemini_api_key="",
        feishu_app_secret="",
    )
    client = TestClient(create_app(settings))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_oauth_start_returns_authorization_url(tmp_path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        response = client.get("/oauth/feishu/start", params={"subject_id": "ou_user"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["authorization_url"].startswith(
        "https://accounts.feishu.test/open-apis/authen/v1/authorize?"
    )
    assert payload["state"]


@respx.mock
def test_oauth_callback_consumes_state_and_saves_token(tmp_path) -> None:
    settings = _settings(tmp_path)
    respx.post("https://open.feishu.test/open-apis/authen/v2/oauth/token").mock(
        return_value=Response(
            200,
            json={
                "code": 0,
                "data": {
                    "access_token": "u-access",
                    "refresh_token": "u-refresh",
                    "expires_in": 7200,
                    "refresh_expires_in": 86400,
                },
            },
        )
    )
    with TestClient(create_app(settings)) as client:
        start = client.get("/oauth/feishu/start", params={"subject_id": "ou_user"})
        state = start.json()["state"]
        response = client.get(
            "/oauth/feishu/callback",
            params={"code": "code-1", "state": state},
        )

    assert response.status_code == 200
    assert "授权成功" in response.text


def test_card_callback_confirms_pending_action(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.MESSAGE_SEND,
        target={"chat_id": "oc_chat"},
        payload={"text": "hello"},
        preview="hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    asyncio.run(_save_pending_action(settings, proposal))
    calls: list[dict[str, Any]] = []

    async def fake_execute(self, **kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(FeishuWriteExecutor, "execute", fake_execute)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/callbacks/feishu/card",
            json={
                "header": {"event_id": "evt-card-1"},
                "event": {
                    "operator": {"open_id": "ou_user"},
                    "action": {
                        "value": {
                            "fcgo_action": "writeback.confirm",
                            "action_id": proposal.id,
                        }
                    },
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "executed"
    assert response.json()["toast"]["type"] == "success"
    assert len(calls) == 1
    saved = asyncio.run(SQLiteStore(settings.sqlite_path).get_pending_action(proposal.id))
    assert saved is not None
    assert saved["status"] == PendingActionStatus.EXECUTED.value


def test_card_callback_is_idempotent_by_event_id(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.MESSAGE_SEND,
        target={"chat_id": "oc_chat"},
        payload={"text": "hello"},
        preview="hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    asyncio.run(_save_pending_action(settings, proposal))
    calls: list[dict[str, Any]] = []

    async def fake_execute(self, **kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(FeishuWriteExecutor, "execute", fake_execute)
    payload = {
        "header": {"event_id": "evt-card-duplicate"},
        "event": {
            "operator": {"open_id": "ou_user"},
            "action": {"value": {"action": "confirm", "action_id": proposal.id}},
        },
    }
    with TestClient(create_app(settings)) as client:
        first = client.post("/callbacks/feishu/card", json=payload)
        second = client.post("/callbacks/feishu/card", json=payload)

    assert first.status_code == 200
    assert first.json()["status"] == "executed"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert len(calls) == 1


def test_card_callback_cancels_pending_action(tmp_path) -> None:
    settings = _settings(tmp_path)
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.MESSAGE_SEND,
        target={"chat_id": "oc_chat"},
        payload={"text": "hello"},
        preview="hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    asyncio.run(_save_pending_action(settings, proposal))

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/callbacks/feishu/card",
            json={
                "actor_id": "ou_user",
                "action": "cancel",
                "action_id": proposal.id,
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "canceled"
    saved = asyncio.run(SQLiteStore(settings.sqlite_path).get_pending_action(proposal.id))
    assert saved is not None
    assert saved["status"] == PendingActionStatus.CANCELED.value


def test_card_callback_rejects_wrong_actor(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    proposal = ActionProposal(
        actor_id="ou_user",
        action_type=WriteActionType.MESSAGE_SEND,
        target={"chat_id": "oc_chat"},
        payload={"text": "hello"},
        preview="hello",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    asyncio.run(_save_pending_action(settings, proposal))

    async def fake_execute(self, **kwargs):
        raise AssertionError("wrong actor must not execute")

    monkeypatch.setattr(FeishuWriteExecutor, "execute", fake_execute)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/callbacks/feishu/card",
            json={
                "actor_id": "ou_other",
                "action": "confirm",
                "action_id": proposal.id,
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "forbidden"


def test_card_callback_supports_url_verification(tmp_path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post(
            "/callbacks/feishu/card",
            json={"type": "url_verification", "challenge": "challenge-token"},
        )

    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-token"}


def _settings(tmp_path) -> Settings:
    return Settings(
        env="test",
        sqlite_path=tmp_path / "fcgo.sqlite3",
        base_url="http://localhost:8000",
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        feishu_base_url="https://open.feishu.test",
        feishu_auth_base_url="https://accounts.feishu.test",
        gemini_api_key="",
    )


async def _save_pending_action(settings: Settings, proposal: ActionProposal) -> None:
    store = SQLiteStore(settings.sqlite_path)
    await store.init()
    await store.save_pending_action(proposal)
