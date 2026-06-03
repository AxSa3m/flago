from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from fcgo.config import Settings, get_settings
from fcgo.feishu.card_callback import is_url_verification, parse_card_callback
from fcgo.feishu.client import FeishuClient
from fcgo.feishu.oauth import FeishuOAuthService
from fcgo.feishu.openapi import FeishuOpenAPI
from fcgo.storage import SQLiteStore
from fcgo.writeback.executor import FeishuWriteExecutor
from fcgo.writeback.service import WritebackService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = SQLiteStore(settings.sqlite_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await store.init()
        yield

    app = FastAPI(title="FCGO", version="0.1.0", lifespan=lifespan)
    oauth = FeishuOAuthService(settings, store)
    openapi = FeishuOpenAPI(settings, store)
    feishu_client = _maybe_build_feishu_client(settings)
    writeback = WritebackService(store, FeishuWriteExecutor(openapi, feishu_client), settings)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/oauth/feishu/start")
    async def oauth_start(
        subject_id: str = Query(...),
    ) -> dict[str, str]:
        authorization_url, state = await oauth.create_authorization_url(subject_id)
        return {"authorization_url": authorization_url, "state": state}

    @app.get("/oauth/feishu/callback")
    async def oauth_callback(
        code: str = Query(...),
        state: str = Query(default=""),
    ) -> HTMLResponse:
        if not state:
            raise HTTPException(status_code=400, detail="missing OAuth state")
        try:
            await oauth.complete_authorization(code, state)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return HTMLResponse("<h1>FCGO 授权成功</h1><p>可以回到飞书继续使用。</p>")

    @app.post("/callbacks/feishu/card")
    async def feishu_card_callback(payload: dict[str, Any]) -> dict[str, Any]:
        if is_url_verification(payload):
            return {"challenge": payload["challenge"]}
        try:
            callback = parse_card_callback(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        event_key = _card_callback_idempotency_key(callback.event_id, payload)
        if event_key:
            fresh = await store.remember_idempotency_key(event_key, callback.action_id)
            if not fresh:
                return _card_callback_response("duplicate", "该操作已处理")
        if callback.action == "confirm":
            result = await writeback.confirm(callback.action_id, callback.actor_id)
        elif callback.action == "cancel":
            result = await writeback.cancel(callback.action_id, callback.actor_id)
        else:
            raise HTTPException(status_code=400, detail="unknown card action")
        return _card_callback_response(result.status, result.message)

    return app


def _card_callback_response(status: str, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "message": message,
        "toast": {
            "type": "success" if status in {"accepted", "executed", "canceled"} else "warning",
            "content": message,
        },
    }


def _card_callback_idempotency_key(event_id: str, payload: dict[str, Any]) -> str:
    if event_id:
        return f"feishu_card_callback:{event_id}"
    header = payload.get("header")
    if isinstance(header, dict) and header.get("event_id"):
        return f"feishu_card_callback:{header['event_id']}"
    return ""


def _maybe_build_feishu_client(settings: Settings) -> FeishuClient | None:
    if not settings.feishu_app_id or not settings.feishu_app_secret.get_secret_value():
        return None
    return FeishuClient(settings)
