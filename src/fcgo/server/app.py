import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from html import escape
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from starlette.middleware.sessions import SessionMiddleware

from fcgo.config import Settings, get_settings
from fcgo.feishu.card_callback import is_url_verification, parse_card_callback
from fcgo.feishu.client import FeishuClient
from fcgo.feishu.oauth import FeishuOAuthService
from fcgo.feishu.openapi import FeishuOpenAPI
from fcgo.server.admin import mount_admin_routes
from fcgo.storage import SQLiteStore
from fcgo.writeback.executor import FeishuWriteExecutor
from fcgo.writeback.service import WritebackService

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = SQLiteStore(settings.sqlite_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await store.init()
        yield

    app = FastAPI(title=settings.assistant_default_name, version="0.1.0", lifespan=lifespan)
    oauth = FeishuOAuthService(settings, store)
    openapi = FeishuOpenAPI(settings, store)
    if settings.admin_enabled:
        app.add_middleware(
            SessionMiddleware,
            secret_key=_admin_session_secret(settings),
            same_site="lax",
            https_only=settings.env == "prod",
        )
        mount_admin_routes(app, settings=settings, store=store, oauth=oauth)
    feishu_client = _maybe_build_feishu_client(settings)
    writeback = (
        WritebackService(store, FeishuWriteExecutor(openapi, feishu_client), settings)
        if settings.writeback_enabled
        else None
    )

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
        code: str = Query(default=""),
        state: str = Query(default=""),
    ) -> HTMLResponse:
        if not code or not state:
            return HTMLResponse(
                _oauth_result_page(
                    ok=False,
                    title="授权失败",
                    message="飞书授权回调缺少必要参数。请回到飞书重新点击授权。",
                    detail="如果连续出现，请检查飞书应用的 OAuth 回调地址配置。",
                ),
                status_code=400,
            )
        try:
            authorization = await oauth.complete_authorization(code, state)
        except ValueError as exc:
            return HTMLResponse(
                _oauth_result_page(
                    ok=False,
                    title="授权失败",
                    message="授权链接已失效或已被使用。请回到飞书重新发送 /授权。",
                    detail=str(exc),
                ),
                status_code=400,
            )
        except Exception as exc:  # noqa: BLE001 - return a user-readable OAuth failure page
            logger.exception("feishu_oauth_callback_failed")
            return HTMLResponse(
                _oauth_result_page(
                    ok=False,
                    title="授权失败",
                    message="没有成功保存飞书授权。请回到飞书重新发送 /授权 后再试。",
                    detail=f"错误类型：{type(exc).__name__}",
                ),
                status_code=502,
            )
        subject_id = str(authorization.get("_subject_id") or "")
        name_preference = (
            await store.get_assistant_name_preference(subject_id)
            if subject_id
            else None
        )
        assistant_name = (
            name_preference.assistant_name
            if name_preference is not None
            else settings.assistant_default_name
        )
        return HTMLResponse(
            _oauth_result_page(
                ok=True,
                title="授权成功",
                message=f"{assistant_name} 已保存你的飞书授权。现在可以回到飞书继续使用。",
                detail="页面会在 5 秒后尝试自动关闭；如果浏览器拦截，请手动关闭。",
                assistant_name=assistant_name,
            )
        )

    @app.post("/callbacks/feishu/card")
    async def feishu_card_callback(payload: dict[str, Any]) -> dict[str, Any]:
        if is_url_verification(payload):
            return {"challenge": payload["challenge"]}
        try:
            callback = parse_card_callback(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if writeback is None:
            return _card_callback_response("disabled", "写入功能当前已暂停")
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


def _admin_session_secret(settings: Settings) -> str:
    return (
        settings.admin_session_secret.get_secret_value()
        or settings.feishu_app_secret.get_secret_value()
        or "fcgo-local-admin-session"
    )


def _oauth_result_page(
    *,
    ok: bool,
    title: str,
    message: str,
    detail: str,
    assistant_name: str = "小智",
) -> str:
    accent = "#16a34a" if ok else "#f97316"
    bg = "#ecfdf5" if ok else "#fff7ed"
    icon_path = (
        '<path d="M8.5 12.5 11 15l5-6" />'
        if ok
        else '<path d="M12 7v6" /><path d="M12 17h.01" />'
    )
    auto_close_script = (
        """
  <script>
    const countdown = document.getElementById("countdown");
    let remaining = 5;
    const tick = window.setInterval(() => {
      remaining -= 1;
      if (countdown) countdown.textContent = String(Math.max(remaining, 0));
      if (remaining <= 0) {
        window.clearInterval(tick);
        window.close();
      }
    }, 1000);
  </script>"""
        if ok
        else ""
    )
    hint = (
        '将在 <span id="countdown">5</span> 秒后自动关闭。'
        if ok
        else "请回到飞书重新发起授权；本页面不会自动关闭。"
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(assistant_name)} {escape(title)}</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: linear-gradient(180deg, #f8fafc 0%, #eef2f7 100%);
      color: #172033;
    }}
    main {{
      width: min(560px, calc(100vw - 40px));
      background: #ffffff;
      border: 1px solid #e5e7eb;
      border-radius: 16px;
      box-shadow: 0 24px 70px rgba(15, 23, 42, 0.14);
      padding: 40px;
      box-sizing: border-box;
    }}
    .mark {{
      width: 76px;
      height: 76px;
      display: grid;
      place-items: center;
      border-radius: 22px;
      background: {bg};
      color: {accent};
      margin-bottom: 28px;
    }}
    svg {{
      width: 44px;
      height: 44px;
      stroke: currentColor;
      stroke-width: 2.4;
      stroke-linecap: round;
      stroke-linejoin: round;
      fill: none;
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 30px;
      line-height: 1.2;
      letter-spacing: 0;
    }}
    p {{
      margin: 0;
      font-size: 16px;
      line-height: 1.75;
      color: #475569;
    }}
    .detail {{
      margin-top: 22px;
      padding: 14px 16px;
      border-radius: 10px;
      background: #f8fafc;
      color: #64748b;
      font-size: 14px;
    }}
    .hint {{
      margin-top: 26px;
      font-size: 13px;
      color: #94a3b8;
    }}
    .actions {{
      margin-top: 28px;
      display: flex;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
    }}
    button {{
      appearance: none;
      border: 0;
      border-radius: 8px;
      background: {accent};
      color: #fff;
      font-size: 15px;
      line-height: 1;
      padding: 13px 18px;
      cursor: pointer;
    }}
    button:focus {{
      outline: 3px solid {bg};
      outline-offset: 2px;
    }}
  </style>
</head>
<body>
  <main>
    <div class="mark" aria-hidden="true">
      <svg viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="9" />
        {icon_path}
      </svg>
    </div>
    <h1>{escape(title)}</h1>
    <p>{escape(message)}</p>
    <div class="detail">{escape(detail)}</div>
    <div class="actions">
      <button type="button" onclick="window.close()">关闭页面</button>
    </div>
    <div class="hint">{hint}</div>
  </main>
  {auto_close_script}
</body>
</html>"""
