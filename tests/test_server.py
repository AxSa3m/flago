import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import respx
from fastapi.testclient import TestClient
from httpx import ConnectError, Response

from fcgo.config import Settings
from fcgo.model_providers.types import ModelResponse
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
    assert "小智 已保存你的飞书授权" in response.text
    assert "<title>小智 授权成功</title>" in response.text
    assert "5 秒后尝试自动关闭" in response.text
    assert "关闭页面" in response.text
    assert "window.close()" in response.text


@respx.mock
def test_oauth_callback_returns_readable_failure_page(tmp_path) -> None:
    settings = _settings(tmp_path)
    respx.post("https://open.feishu.test/open-apis/authen/v2/oauth/token").mock(
        side_effect=ConnectError("connect failed")
    )
    with TestClient(create_app(settings)) as client:
        start = client.get("/oauth/feishu/start", params={"subject_id": "ou_user"})
        state = start.json()["state"]
        response = client.get(
            "/oauth/feishu/callback",
            params={"code": "code-1", "state": state},
        )

    assert response.status_code == 502
    assert "授权失败" in response.text
    assert "ConnectError" in response.text
    assert "重新发送 /授权" in response.text
    assert "本页面不会自动关闭" in response.text


def test_oauth_callback_returns_failure_page_for_missing_params(tmp_path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        response = client.get("/oauth/feishu/callback")

    assert response.status_code == 400
    assert "授权失败" in response.text
    assert "缺少必要参数" in response.text
    assert "关闭页面" in response.text


@respx.mock
def test_admin_login_binds_owner_and_allows_assistant_update(tmp_path) -> None:
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
                    "scope": " ".join(settings.oauth_scope_list),
                },
            },
        )
    )
    respx.get("https://open.feishu.test/open-apis/authen/v1/user_info").mock(
        return_value=Response(200, json={"code": 0, "data": {"open_id": "ou_admin"}})
    )

    with TestClient(create_app(settings)) as client:
        home = client.get("/admin")
        login = client.get("/admin/login", follow_redirects=False)
        location = login.headers["location"]
        query = parse_qs(urlparse(location).query)
        callback = client.get(
            "/admin/oauth/callback",
            params={"code": "code-1", "state": query["state"][0]},
        )
        admin = client.get("/admin")
        csrf = re.search(r'name="csrf" value="([^"]+)"', admin.text)
        assert csrf is not None
        update = client.post(
            "/admin/assistant",
            content=(
                f"csrf={csrf.group(1)}&assistant_name=小飞&"
                "assistant_profile=简洁直接"
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )

    store = SQLiteStore(settings.sqlite_path)
    preference = asyncio.run(store.get_assistant_name_preference("ou_admin"))
    profile = asyncio.run(store.get_assistant_profile_preference("ou_admin"))
    owner = asyncio.run(store.get_app_setting("admin_owner_open_id"))
    assert "使用飞书登录" in home.text
    assert login.status_code == 303
    assert query["redirect_uri"] == ["http://testserver/admin/oauth/callback"]
    assert callback.status_code == 200
    assert "登录成功" in callback.text
    assert "ou_admin" in admin.text
    assert update.status_code == 303
    assert owner == "ou_admin"
    assert preference is not None
    assert preference.assistant_name == "小飞"
    assert profile is not None
    assert profile.assistant_profile == "简洁直接"


@respx.mock
def test_admin_page_updates_all_config_form_and_lists_menus(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    settings = _settings(tmp_path).model_copy(
        update={
            "admin_config_path": env_path,
            "gemini_api_key": "gemini-key",
            "comfyui_base_url": "http://127.0.0.1:8188",
        }
    )
    store = SQLiteStore(settings.sqlite_path)
    asyncio.run(store.init())
    asyncio.run(
        store.save_memory_item(
            id="memory-1",
            subject_id="ou_admin",
            kind="长期记忆",
            content="旧的长期记忆",
            source="test",
        )
    )
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
                    "scope": " ".join(settings.oauth_scope_list),
                },
            },
        )
    )
    respx.get("https://open.feishu.test/open-apis/authen/v1/user_info").mock(
        return_value=Response(200, json={"code": 0, "data": {"open_id": "ou_admin"}})
    )

    with TestClient(create_app(settings)) as client:
        login = client.get("/admin/login", follow_redirects=False)
        query = parse_qs(urlparse(login.headers["location"]).query)
        client.get(
            "/admin/oauth/callback",
            params={"code": "code-1", "state": query["state"][0]},
        )
        admin = client.get("/admin")
        setup = client.get("/admin/setup")
        advanced = client.get("/admin/advanced")
        csrf = re.search(r'name="csrf" value="([^"]+)"', admin.text)
        assert csrf is not None
        calls: list[Any] = []

        class FakeRouter:
            async def generate_model(self, request):
                calls.append(request)
                return ModelResponse(text="OK", provider=request.provider, model=request.model)

        monkeypatch.setattr("fcgo.server.admin.build_model_router", lambda settings: FakeRouter())
        test_model = client.post(
            "/admin/model/test",
            content=urlencode(
                {
                    "csrf": csrf.group(1),
                    "next": "/admin",
                    "provider": "gemini",
                }
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        test_model_json = client.post(
            "/admin/model/test",
            content=urlencode(
                {
                    "csrf": csrf.group(1),
                    "next": "/admin",
                    "provider": "gemini",
                }
            ),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "accept": "application/json",
            },
        )
        tested_admin = client.get(test_model.headers["location"])
        payload = {
            "csrf": csrf.group(1),
            "assistant_name": "小智",
            "assistant_profile": "简洁直接",
            "memory_enabled": "false",
            "memory_content": "更新后的长期记忆",
            "personal_provider": "gemini",
            "personal_model": "gemini-2.5-flash",
            "env__FCGO_BASE_URL": "https://fcgo.example.test",
            "env__FCGO_AGENT_MODE": "agent",
            "env__FCGO_WRITEBACK_ENABLED": "true",
            "env__FCGO_WRITEBACK_CONFIRMATION_MODE": "low_risk_direct",
            "env__GEMINI_DISPLAY_NAME": "小智专用 Gemini",
            "env__GEMINI_API_KEY": "gemini-key",
            "env__DEEPSEEK_API_KEY": "sk-deepseek-test",
            "env__COMFYUI_DISPLAY_NAME": "本地 Comfy 工作流",
            "env__COMFYUI_BASE_URL": "http://127.0.0.1:8188",
        }
        response = client.post(
            "/admin/config",
            content=urlencode(payload),
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        saved_admin = client.get("/admin")
        respx.get("http://127.0.0.1:8188").mock(return_value=Response(200))
        test_media_json = client.post(
            "/admin/media/test",
            content=urlencode(
                {
                    "csrf": csrf.group(1),
                    "next": "/admin",
                    "provider": "comfyui",
                }
            ),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "accept": "application/json",
            },
        )
        set_gemini_default = client.post(
            "/admin/model/default",
            content=urlencode(
                {
                    "csrf": csrf.group(1),
                    "next": "/admin",
                    "provider": "gemini",
                }
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        delete_default_model = client.post(
            "/admin/model/delete",
            content=urlencode(
                {
                    "csrf": csrf.group(1),
                    "next": "/admin",
                    "provider": "gemini",
                }
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        delete_media = client.post(
            "/admin/media/delete",
            content=urlencode(
                {
                    "csrf": csrf.group(1),
                    "next": "/admin",
                    "provider": "comfyui",
                }
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        mismatch = client.post(
            "/admin/config",
            content=urlencode(
                {
                    **payload,
                    "personal_provider": "deepseek",
                    "personal_model": "gemini-2.5-flash",
                }
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert set_gemini_default.status_code == 303
    assert "saved=model-default" in set_gemini_default.headers["location"]
    assert delete_default_model.status_code == 400
    assert delete_media.status_code == 303
    assert "saved=media-delete" in delete_media.headers["location"]
    assert mismatch.status_code == 400
    assert test_model.status_code == 303
    assert "model_test=ok" in test_model.headers["location"]
    assert test_model_json.status_code == 200
    assert test_model_json.json() == {
        "ok": True,
        "provider": "gemini",
        "message": "连接正常",
    }
    assert test_media_json.status_code == 200
    assert test_media_json.json() == {
        "ok": True,
        "provider": "comfyui",
        "message": "接口可达",
    }
    assert "连接正常" in tested_admin.text
    assert "模型连通性测试通过" not in tested_admin.text
    assert calls and calls[0].provider == "gemini"
    admin_text = admin.text
    setup_text = setup.text
    advanced_text = advanced.text
    assert "首次配置向导" not in admin_text
    assert "首次配置向导" in setup_text
    assert "欢迎配置你的飞书助手" in setup_text
    assert "开始配置" in setup_text
    assert "先跳过" in setup_text
    assert "保存配置" in setup_text
    assert 'name="config_scope" value="setup"' in setup_text
    assert "在飞书开放平台添加这个机器人授权回调地址" in setup_text
    assert "在飞书开放平台添加这个后台登录回调地址" in setup_text
    assert "http://localhost:8000/oauth/feishu/callback" in setup_text
    assert "http://localhost:8000/admin/oauth/callback" in setup_text
    assert 'data-setup-step="0"' in setup_text
    assert "FCGO_AGENT_MODE" not in setup_text
    assert "FEISHU_VERIFICATION_TOKEN" not in setup_text
    assert "fcgo.admin.open" in setup_text
    assert "测试完成" not in admin_text
    assert 'showTransientResult(slot, "pending", "测试中...")' in admin_text
    assert 'data-provider="gemini"' in admin_text
    assert "button.dataset.provider || button.value" in admin_text
    assert "new URLSearchParams()" in admin_text
    assert '"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"' in admin_text
    assert 'const fallback = ok ? "连接正常" : "连接失败";' in admin_text
    assert "FCGO_BASE_URL" in admin_text
    assert "DEEPSEEK_API_KEY" in admin_text
    assert "旧的长期记忆" in admin_text
    assert "fcgo.writeback.undo" in admin_text
    assert "fcgo.admin.open" in admin_text
    assert "添加模型接口" in admin_text
    assert "model-config-dialog" in admin_text
    assert "open-model-dialog" in admin_text
    assert 'data-config-provider="gemini"' in admin_text
    assert "/admin/model/default" in admin_text
    assert "/admin/model/delete" in admin_text
    assert "data-test-action=\"model\"" in admin_text
    assert "添加媒体/工作流接口" in admin_text
    assert "media-config-dialog" in admin_text
    assert 'data-config-provider="comfyui"' in saved_admin.text
    assert "/admin/media/delete" in saved_admin.text
    assert "data-test-action=\"media\"" in saved_admin.text
    assert 'value="assistant">保存</button>' in admin_text
    assert "input-action-row" in advanced_text
    assert "重置所有配置" in admin_text
    assert "高级配置" in admin_text
    assert "机器人菜单功能" in admin_text
    assert "飞书资源搜索" in admin_text
    assert "dev 用于本地开发" in admin_text
    assert "每次确认 (always)" in admin_text
    assert "FCGO_LOG_LEVEL" in admin_text
    assert "<option value=\"INFO\" selected>INFO</option>" in admin_text
    assert 'id="personal-provider"' in admin_text
    assert 'id="personal-model"' in admin_text
    assert "FCGO_ADMIN_ENABLED" not in admin_text
    assert "cli_test" not in admin_text
    assert str(env_path) not in admin_text
    assert "全部高级参数" in advanced_text
    assert "FCGO_SQLITE_PATH" in advanced_text
    assert "clear-button" in advanced_text
    saved_env = env_path.read_text(encoding="utf-8")
    assert "FCGO_BASE_URL=https://fcgo.example.test" in saved_env
    assert "FCGO_AGENT_MODE=agent" in saved_env
    assert "FCGO_DEFAULT_PROVIDER=gemini" in saved_env
    assert "FCGO_WRITEBACK_ENABLED=true" in saved_env
    assert "FCGO_WRITEBACK_CONFIRMATION_MODE=low_risk_direct" in saved_env
    assert "GEMINI_DISPLAY_NAME=" in saved_env
    assert "DEEPSEEK_API_KEY=sk-deepseek-test" in saved_env
    assert "COMFYUI_BASE_URL=" not in saved_env
    model_preference = asyncio.run(store.get_model_preference("user:ou_admin"))
    assert model_preference is not None
    assert model_preference.provider == "gemini"
    assert model_preference.model == "gemini-2.5-flash"
    assert asyncio.run(store.is_memory_enabled("ou_admin")) is False
    memory_items = asyncio.run(store.list_memory_items("ou_admin"))
    assert [item.content for item in memory_items] == ["更新后的长期记忆"]


def test_local_setup_bootstrap_allows_first_run_without_feishu_login(tmp_path) -> None:
    env_path = tmp_path / ".env"
    settings = _settings(tmp_path).model_copy(update={"admin_config_path": env_path})

    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        admin = client.get("/admin", follow_redirects=False)
        setup = client.get("/admin/setup")
        csrf = re.search(r'name="csrf" value="([^"]+)"', setup.text)
        assert csrf is not None
        update = client.post(
            "/admin/config",
            content=urlencode(
                {
                    "csrf": csrf.group(1),
                    "next": "/admin/setup",
                    "config_scope": "setup",
                    "env__FCGO_BASE_URL": "http://127.0.0.1:8000",
                    "env__FCGO_ENV": "dev",
                    "env__FCGO_AGENT_MODE": "agent",
                }
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )

    store = SQLiteStore(settings.sqlite_path)
    owner = asyncio.run(store.get_app_setting("admin_owner_open_id"))
    assert admin.status_code == 303
    assert admin.headers["location"] == "/admin/setup"
    assert setup.status_code == 200
    assert "首次安装模式" in setup.text
    assert "飞书登录绑定管理员" in setup.text
    assert update.status_code == 303
    assert "saved=config" in update.headers["location"]
    assert owner is None
    assert "FCGO_BASE_URL=http://127.0.0.1:8000" in env_path.read_text(
        encoding="utf-8"
    )


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


def test_card_callback_reports_disabled_when_writeback_paused(tmp_path) -> None:
    settings = Settings(
        env="test",
        sqlite_path=tmp_path / "fcgo.sqlite3",
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        gemini_api_key="",
        writeback_enabled=False,
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/callbacks/feishu/card",
            json={
                "actor_id": "ou_user",
                "action": "confirm",
                "action_id": "action-1",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert "写入功能当前已暂停" in response.json()["message"]


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
        writeback_enabled=True,
    )


async def _save_pending_action(settings: Settings, proposal: ActionProposal) -> None:
    store = SQLiteStore(settings.sqlite_path)
    await store.init()
    await store.save_pending_action(proposal)
