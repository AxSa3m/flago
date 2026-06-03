from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import Response

from fcgo.config import Settings
from fcgo.feishu.oauth import FeishuOAuthService, MissingOAuthScopeError
from fcgo.storage import SQLiteStore


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


@pytest.mark.asyncio
async def test_create_authorization_url_persists_state(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    service = FeishuOAuthService(_settings(tmp_path), store)

    authorization_url, state = await service.create_authorization_url("ou_user")

    assert authorization_url.startswith(
        "https://accounts.feishu.test/open-apis/authen/v1/authorize?"
    )
    query = parse_qs(urlparse(authorization_url).query)
    assert query["app_id"] == ["cli_test"]
    assert query["redirect_uri"] == ["http://localhost:8000/oauth/feishu/callback"]
    assert query["state"] == [state]
    assert "docx:document:readonly" in query["scope"][0].split()
    assert "wiki:node:read" in query["scope"][0].split()
    assert "base:record:read" in query["scope"][0].split()
    assert "base:field:read" in query["scope"][0].split()
    assert "base:view:read" in query["scope"][0].split()
    assert await store.consume_oauth_state(state) == "ou_user"


@pytest.mark.asyncio
@respx.mock
async def test_complete_authorization_exchanges_code_and_saves_token(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    service = FeishuOAuthService(_settings(tmp_path), store)
    _, state = await service.create_authorization_url("ou_user")
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
                    "token_type": "Bearer",
                    "scope": "docs:read",
                },
            },
        )
    )

    token = await service.complete_authorization("code-1", state)
    saved = await store.get_oauth_token("ou_user")

    assert token["access_token"] == "u-access"
    assert token["refresh_token"] == "u-refresh"
    assert saved is not None
    assert saved["access_token"] == "u-access"
    assert saved["expires_at"]


@pytest.mark.asyncio
@respx.mock
async def test_get_valid_access_token_refreshes_expired_token(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    service = FeishuOAuthService(_settings(tmp_path), store)
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "old-access",
            "refresh_token": "u-refresh",
            "expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            "refresh_expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    )
    respx.post("https://open.feishu.test/open-apis/authen/v2/oauth/token").mock(
        return_value=Response(
            200,
            json={
                "code": 0,
                "data": {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 7200,
                    "refresh_expires_in": 86400,
                },
            },
        )
    )

    assert await service.get_valid_access_token("ou_user") == "new-access"
    saved = await store.get_oauth_token("ou_user")
    assert saved is not None
    assert saved["refresh_token"] == "new-refresh"


@pytest.mark.asyncio
async def test_get_valid_access_token_rejects_missing_required_scope(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    service = FeishuOAuthService(_settings(tmp_path), store)
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read",
        },
    )

    with pytest.raises(MissingOAuthScopeError) as exc_info:
        await service.get_valid_access_token(
            "ou_user",
            required_scope_groups=(("docx:document:readonly", "docx:document"),),
        )

    assert exc_info.value.missing_scopes == ["docx:document:readonly"]
