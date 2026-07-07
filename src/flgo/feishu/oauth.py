from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from typing import Any
from urllib.parse import urlencode

import httpx

from flgo.config import Settings
from flgo.models import AuditEventType
from flgo.storage import SQLiteStore


@dataclass(frozen=True)
class AuthorizationStatus:
    authorized: bool
    usable: bool
    missing_scopes: list[str]
    expires_at: str = ""
    refresh_expires_at: str = ""
    reason: str = ""


class FeishuOAuthService:
    def __init__(self, settings: Settings, store: SQLiteStore) -> None:
        self.settings = settings
        self.store = store

    def authorization_url(self, state: str) -> str:
        params = {
            "app_id": self.settings.feishu_app_id,
            "redirect_uri": self.settings.oauth_redirect_uri,
            "state": state,
        }
        if self.settings.oauth_scope_list:
            params["scope"] = " ".join(self.settings.oauth_scope_list)
        query = urlencode(params)
        auth_base_url = self.settings.feishu_auth_base_url.rstrip("/")
        return f"{auth_base_url}/open-apis/authen/v1/authorize?{query}"

    async def create_authorization_url(self, subject_id: str) -> tuple[str, str]:
        state = token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=self.settings.oauth_state_ttl_seconds)
        await self.store.save_oauth_state(
            state=state,
            subject_id=subject_id,
            expires_at=expires_at,
        )
        await self.store.audit(
            AuditEventType.OAUTH_STATE_CREATED,
            actor_id=subject_id,
            detail={"expires_at": expires_at.isoformat()},
        )
        return self.authorization_url(state), state

    async def complete_authorization(self, code: str, state: str) -> dict[str, Any]:
        subject_id = await self.store.consume_oauth_state(state)
        if not subject_id:
            raise ValueError("invalid or expired OAuth state")
        token = await self.exchange_code(code)
        await self.store.save_oauth_token(subject_id, token)
        await self.store.audit(AuditEventType.OAUTH_AUTHORIZED, actor_id=subject_id)
        return {**token, "_subject_id": subject_id}

    async def exchange_code(self, code: str, *, redirect_uri: str | None = None) -> dict[str, Any]:
        url = f"{self.settings.feishu_base_url.rstrip('/')}/open-apis/authen/v2/oauth/token"
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.settings.feishu_app_id,
            "client_secret": self.settings.feishu_app_secret.get_secret_value(),
            "redirect_uri": redirect_uri or self.settings.oauth_redirect_uri,
        }
        async with httpx.AsyncClient(
            timeout=20,
            proxy=self.settings.feishu_http_proxy,
        ) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        return _normalize_token_response(response.json())

    async def get_valid_access_token(
        self,
        subject_id: str,
        *,
        required_scope_groups: Iterable[Iterable[str]] | None = None,
    ) -> str | None:
        token = await self.store.get_oauth_token(subject_id)
        if not token:
            return None
        access_token = _token_value(token, "access_token")
        if access_token and not _is_expired(token, "expires_at"):
            _raise_if_missing_scopes(token, required_scope_groups)
            return access_token
        refresh_token = _token_value(token, "refresh_token")
        if not refresh_token or _is_expired(token, "refresh_expires_at"):
            return None
        refreshed = _merge_refreshed_token(token, await self.refresh_access_token(refresh_token))
        await self.store.save_oauth_token(subject_id, refreshed)
        await self.store.audit(AuditEventType.OAUTH_TOKEN_REFRESHED, actor_id=subject_id)
        _raise_if_missing_scopes(refreshed, required_scope_groups)
        return _token_value(refreshed, "access_token")

    async def authorization_status(self, subject_id: str) -> AuthorizationStatus:
        token = await self.store.get_oauth_token(subject_id)
        if not token:
            return AuthorizationStatus(
                authorized=False,
                usable=False,
                missing_scopes=self.settings.oauth_required_scope_list,
                reason="not_authorized",
            )
        access_token = await self.get_valid_access_token(subject_id)
        token = await self.store.get_oauth_token(subject_id) or token
        if not access_token:
            return AuthorizationStatus(
                authorized=True,
                usable=False,
                missing_scopes=[],
                expires_at=str(token.get("expires_at") or ""),
                refresh_expires_at=str(token.get("refresh_expires_at") or ""),
                reason="expired",
            )
        granted = _scope_set(token)
        missing_scopes = [
            scope for scope in self.settings.oauth_required_scope_list if scope not in granted
        ]
        return AuthorizationStatus(
            authorized=True,
            usable=not missing_scopes,
            missing_scopes=missing_scopes,
            expires_at=str(token.get("expires_at") or ""),
            refresh_expires_at=str(token.get("refresh_expires_at") or ""),
            reason="missing_scopes" if missing_scopes else "",
        )

    async def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        url = f"{self.settings.feishu_base_url.rstrip('/')}/open-apis/authen/v2/oauth/token"
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.settings.feishu_app_id,
            "client_secret": self.settings.feishu_app_secret.get_secret_value(),
        }
        async with httpx.AsyncClient(
            timeout=20,
            proxy=self.settings.feishu_http_proxy,
        ) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        return _normalize_token_response(response.json())


def _normalize_token_response(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("code", 0) != 0:
        raise RuntimeError(f"Feishu OAuth error {payload.get('code')}: {payload.get('msg')}")
    data = payload.get("data")
    token_data = data if isinstance(data, dict) else payload
    now = datetime.now(UTC)
    normalized = {
        "raw": payload,
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "token_type": token_data.get("token_type"),
        "scope": token_data.get("scope"),
    }
    expires_in = _int_or_none(token_data.get("expires_in") or token_data.get("expire"))
    refresh_expires_in = _int_or_none(
        token_data.get("refresh_expires_in") or token_data.get("refresh_token_expires_in")
    )
    if expires_in is not None:
        normalized["expires_at"] = (now + timedelta(seconds=expires_in)).isoformat()
    if refresh_expires_in is not None:
        normalized["refresh_expires_at"] = (
            now + timedelta(seconds=refresh_expires_in)
        ).isoformat()
    return normalized


def _merge_refreshed_token(
    previous: dict[str, Any],
    refreshed: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(refreshed)
    for key in ("refresh_token", "refresh_expires_at", "scope", "token_type"):
        if not merged.get(key) and previous.get(key):
            merged[key] = previous[key]
    if not _token_value(merged, "refresh_token"):
        previous_refresh = _token_value(previous, "refresh_token")
        if previous_refresh:
            merged["refresh_token"] = previous_refresh
    if not _scope_set(merged):
        previous_scopes = _scope_set(previous)
        if previous_scopes:
            merged["scope"] = " ".join(sorted(previous_scopes))
    return merged


class MissingOAuthScopeError(RuntimeError):
    def __init__(self, missing_scopes: list[str]) -> None:
        self.missing_scopes = missing_scopes
        super().__init__("missing OAuth scopes: " + ", ".join(missing_scopes))


def _token_value(token: dict[str, Any], key: str) -> str | None:
    value = token.get(key)
    if value:
        return str(value)
    raw = token.get("raw")
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, dict) and data.get(key):
            return str(data[key])
        if raw.get(key):
            return str(raw[key])
    data = token.get("data")
    if isinstance(data, dict) and data.get(key):
        return str(data[key])
    return None


def _raise_if_missing_scopes(
    token: dict[str, Any],
    required_scope_groups: Iterable[Iterable[str]] | None,
) -> None:
    if required_scope_groups is None:
        return
    granted = _scope_set(token)
    missing: list[str] = []
    for group in required_scope_groups:
        alternatives = [scope for scope in group if scope]
        if alternatives and granted.isdisjoint(alternatives):
            missing.append(alternatives[0])
    if missing:
        raise MissingOAuthScopeError(missing)


def _scope_set(token: dict[str, Any]) -> set[str]:
    scopes: set[str] = set()
    for source in (token, token.get("raw"), token.get("data")):
        if not isinstance(source, dict):
            continue
        value = source.get("scope")
        if value:
            scopes.update(str(value).replace(",", " ").split())
        data = source.get("data")
        if isinstance(data, dict) and data.get("scope"):
            scopes.update(str(data["scope"]).replace(",", " ").split())
    return scopes


def _is_expired(token: dict[str, Any], key: str) -> bool:
    value = token.get(key)
    if not value:
        return False
    try:
        expires_at = datetime.fromisoformat(str(value))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC) + timedelta(seconds=60)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
