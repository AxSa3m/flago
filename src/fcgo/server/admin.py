import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from html import escape
from pathlib import Path
from secrets import token_urlsafe
from typing import Any, Literal, cast, get_args, get_origin
from urllib.parse import parse_qs, urlencode

import httpx
from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import SecretStr, ValidationError

from fcgo.config import Settings
from fcgo.feishu.oauth import FeishuOAuthService
from fcgo.logging import redact
from fcgo.model_providers.registry import build_model_router
from fcgo.model_providers.types import ModelMessage, ModelMessageRole, ModelRequest
from fcgo.models import WritebackConfirmationMode
from fcgo.storage import SQLiteStore

logger = logging.getLogger(__name__)

ADMIN_OWNER_SETTING = "admin_owner_open_id"
ADMIN_LOGIN_SUBJECT = "__admin_login__"
_ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=")


@dataclass(frozen=True)
class SettingFieldSpec:
    name: str
    env_name: str
    group: str
    title: str
    description: str
    value: str
    secret: bool
    write_only: bool
    configured: bool
    optional: bool
    multiline: bool
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelProviderOption:
    key: str
    label: str
    display_name: str
    model: str
    base_url: str
    default_base_url: str
    api_key_env_name: str
    display_name_env_name: str
    base_url_env_name: str
    model_env_name: str
    suggested_models: tuple[str, ...]
    configured: bool


MENU_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "助手",
        (
            ("fcgo.assistant.name.view", "查看助手信息"),
            ("fcgo.assistant.info.view", "查看助手信息（兼容 key）"),
        ),
    ),
    (
        "模型",
        (
            ("fcgo.model.view", "查看当前模型"),
            ("fcgo.model.default", "恢复默认模型"),
            ("fcgo.model.use.gemini", "使用 Gemini"),
            ("fcgo.model.use.deepseek", "使用 DeepSeek"),
            ("fcgo.model.use.openai", "使用 OpenAI"),
            ("fcgo.model.use.qwen", "使用 Qwen"),
            ("fcgo.model.use.doubao", "使用 Doubao"),
            ("fcgo.model.use.minimax", "使用 Minimax"),
            ("fcgo.model.use.claude", "使用 Claude"),
        ),
    ),
    (
        "授权",
        (
            ("fcgo.auth.start", "飞书授权"),
            ("fcgo.auth.status", "授权状态"),
        ),
    ),
    (
        "上下文",
        (
            ("fcgo.context.view", "查看上下文策略"),
            ("fcgo.context.enable", "兼容入口：上下文默认开启"),
            ("fcgo.context.disable", "兼容入口：上下文默认开启"),
        ),
    ),
    (
        "写入",
        (
            ("fcgo.writeback.status", "写入状态"),
            ("fcgo.writeback.auto.enable", "自动写入开启"),
            ("fcgo.writeback.auto.disable", "自动写入关闭"),
            ("fcgo.writeback.auto.clear", "写入恢复默认"),
            ("fcgo.writeback.history", "最近写入"),
            ("fcgo.writeback.undo", "撤回最近写入，需要确认"),
        ),
    ),
    (
        "记忆",
        (
            ("fcgo.memory.view", "查看记忆"),
            ("fcgo.memory.delete", "删除记忆，需要确认"),
            ("fcgo.memory.disable", "关闭记忆"),
            ("fcgo.memory.enable", "开启记忆"),
        ),
    ),
    (
        "帮助",
        (
            ("fcgo.help", "使用说明"),
            ("fcgo.admin.open", "本地配置网页"),
        ),
    ),
)

MODEL_PROVIDER_SPECS: tuple[dict[str, object], ...] = (
    {
        "key": "gemini",
        "label": "Gemini",
        "api_key": "GEMINI_API_KEY",
        "display_name": "GEMINI_DISPLAY_NAME",
        "base_url": "GEMINI_BASE_URL",
        "model": "GEMINI_MODEL",
        "default_base_url": "Google Gemini 默认接口",
        "suggested_models": ("gemini-2.5-flash", "gemini-2.5-pro"),
    },
    {
        "key": "deepseek",
        "label": "DeepSeek",
        "api_key": "DEEPSEEK_API_KEY",
        "display_name": "DEEPSEEK_DISPLAY_NAME",
        "base_url": "DEEPSEEK_BASE_URL",
        "model": "DEEPSEEK_MODEL",
        "default_base_url": "https://api.deepseek.com",
        "suggested_models": ("deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash"),
    },
    {
        "key": "openai",
        "label": "OpenAI",
        "api_key": "OPENAI_API_KEY",
        "display_name": "OPENAI_DISPLAY_NAME",
        "base_url": "OPENAI_BASE_URL",
        "model": "OPENAI_MODEL",
        "default_base_url": "https://api.openai.com/v1",
        "suggested_models": ("gpt-4.1", "gpt-4.1-mini", "gpt-5"),
    },
    {
        "key": "qwen",
        "label": "Qwen",
        "api_key": "QWEN_API_KEY",
        "display_name": "QWEN_DISPLAY_NAME",
        "base_url": "QWEN_BASE_URL",
        "model": "QWEN_MODEL",
        "default_base_url": "OpenAI 兼容接口地址",
        "suggested_models": ("qwen-plus", "qwen-max", "qwen-turbo"),
    },
    {
        "key": "doubao",
        "label": "Doubao",
        "api_key": "DOUBAO_API_KEY",
        "display_name": "DOUBAO_DISPLAY_NAME",
        "base_url": "DOUBAO_BASE_URL",
        "model": "DOUBAO_MODEL",
        "default_base_url": "OpenAI 兼容接口地址",
        "suggested_models": ("doubao-seed-1-6", "doubao-pro-32k"),
    },
    {
        "key": "minimax",
        "label": "Minimax",
        "api_key": "MINIMAX_API_KEY",
        "display_name": "MINIMAX_DISPLAY_NAME",
        "base_url": "MINIMAX_BASE_URL",
        "model": "MINIMAX_MODEL",
        "default_base_url": "OpenAI 兼容接口地址",
        "suggested_models": ("MiniMax-M2.7", "abab6.5s-chat"),
    },
    {
        "key": "claude",
        "label": "Claude",
        "api_key": "ANTHROPIC_API_KEY",
        "display_name": "ANTHROPIC_DISPLAY_NAME",
        "base_url": "ANTHROPIC_BASE_URL",
        "model": "ANTHROPIC_MODEL",
        "default_base_url": "https://api.anthropic.com",
        "suggested_models": ("claude-sonnet-4-5", "claude-opus-4-1"),
    },
)

MEDIA_PROVIDER_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "seedance",
        "Seedance",
        ("SEEDANCE_DISPLAY_NAME", "SEEDANCE_API_KEY", "SEEDANCE_BASE_URL", "SEEDANCE_MODEL"),
    ),
    ("comfyui", "ComfyUI", ("COMFYUI_DISPLAY_NAME", "COMFYUI_BASE_URL", "COMFYUI_API_KEY")),
    ("coze", "Coze", ("COZE_DISPLAY_NAME", "COZE_API_KEY", "COZE_BASE_URL")),
    ("dify", "Dify", ("DIFY_DISPLAY_NAME", "DIFY_API_KEY", "DIFY_BASE_URL")),
)

COMMON_ENV_NAMES: set[str] = {
    "FCGO_ENV",
    "FCGO_LOG_LEVEL",
    "FCGO_BASE_URL",
    "FCGO_AGENT_MODE",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_VERIFICATION_TOKEN",
    "FEISHU_ENCRYPT_KEY",
    "FEISHU_BOT_OPEN_ID",
    "FEISHU_BOT_NAME",
    "FCGO_OAUTH_ENABLE_OFFLINE_ACCESS",
    "FCGO_DEFAULT_PROVIDER",
    "FCGO_DEFAULT_MODEL",
    "GEMINI_API_KEY",
    "GEMINI_DISPLAY_NAME",
    "GEMINI_BASE_URL",
    "GEMINI_MODEL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_DISPLAY_NAME",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_DISPLAY_NAME",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "QWEN_API_KEY",
    "QWEN_DISPLAY_NAME",
    "QWEN_BASE_URL",
    "QWEN_MODEL",
    "DOUBAO_API_KEY",
    "DOUBAO_DISPLAY_NAME",
    "DOUBAO_BASE_URL",
    "DOUBAO_MODEL",
    "MINIMAX_API_KEY",
    "MINIMAX_DISPLAY_NAME",
    "MINIMAX_BASE_URL",
    "MINIMAX_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_DISPLAY_NAME",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "FCGO_RESOURCE_SEARCH_ENABLED",
    "FCGO_ATTACHMENT_OCR_ENABLED",
    "FCGO_ATTACHMENT_VISION_ENABLED",
    "FCGO_ATTACHMENT_MEDIA_UNDERSTANDING_ENABLED",
    "FCGO_WEB_READ_ENABLED",
    "FCGO_WRITEBACK_ENABLED",
    "FCGO_WRITEBACK_AUTO_EXECUTE_ENABLED",
    "FCGO_WRITEBACK_CONFIRMATION_MODE",
    "SEEDANCE_API_KEY",
    "SEEDANCE_DISPLAY_NAME",
    "SEEDANCE_BASE_URL",
    "SEEDANCE_MODEL",
    "COMFYUI_DISPLAY_NAME",
    "COMFYUI_BASE_URL",
    "COMFYUI_API_KEY",
    "COZE_API_KEY",
    "COZE_DISPLAY_NAME",
    "COZE_BASE_URL",
    "DIFY_API_KEY",
    "DIFY_DISPLAY_NAME",
    "DIFY_BASE_URL",
}

ADMIN_EXCLUDED_ENV_NAMES: set[str] = {"FCGO_ADMIN_ENABLED"}

WRITE_ONLY_ENV_NAMES: set[str] = {
    "FEISHU_APP_ID",
    "FEISHU_BOT_OPEN_ID",
    "FCGO_ADMIN_CONFIG_PATH",
}

FIELD_METADATA: dict[str, tuple[str, str]] = {
    "FCGO_ENV": (
        "运行模式",
        "dev 用于本地开发；test 用于自动测试；prod 用于正式部署。",
    ),
    "FCGO_LOG_LEVEL": (
        "日志级别",
        "控制服务日志详细程度，推荐 INFO；排查问题时可临时改 DEBUG。",
    ),
    "FCGO_BASE_URL": (
        "服务地址",
        "用于生成飞书 OAuth 回调地址和后台打开链接。填写你实际访问本服务的根地址，例如本地 http://127.0.0.1:8000 或公网 HTTPS 地址。",
    ),
    "FCGO_AGENT_MODE": ("Agent 模式", "legacy 使用旧解析链路；agent 使用 Agent + Tools 链路。"),
    "FEISHU_APP_ID": ("飞书 App ID", "飞书开放平台自建应用的 App ID。"),
    "FEISHU_APP_SECRET": ("飞书 App Secret", "飞书开放平台自建应用的 App Secret。"),
    "FEISHU_VERIFICATION_TOKEN": ("事件校验 Token", "用于验证飞书事件订阅请求。"),
    "FEISHU_ENCRYPT_KEY": ("事件加密 Key", "飞书事件订阅开启加密时填写。"),
    "FEISHU_BOT_OPEN_ID": ("机器人 Open ID", "机器人账号的 open_id，可留空自动依赖事件上下文。"),
    "FEISHU_BOT_NAME": ("机器人名称", "用于显示或排障，不影响飞书后台真实机器人名称。"),
    "FCGO_OAUTH_ENABLE_OFFLINE_ACCESS": (
        "持续授权",
        "开启后请求 offline_access，便于服务自动刷新用户授权。",
    ),
    "FCGO_DEFAULT_PROVIDER": ("系统默认 Provider", "没有个人偏好时使用的模型 Provider。"),
    "FCGO_DEFAULT_MODEL": ("系统默认模型", "留空时使用所选 Provider 的模型配置。"),
    "FCGO_RESOURCE_SEARCH_ENABLED": (
        "飞书资源搜索",
        "允许按名称或关键词搜索飞书文档、表格和多维表。",
    ),
    "FCGO_ATTACHMENT_OCR_ENABLED": ("图片 OCR", "用本地 OCR 工具提取图片文字。"),
    "FCGO_ATTACHMENT_VISION_ENABLED": ("图片理解", "用多模态模型理解图片内容。"),
    "FCGO_ATTACHMENT_MEDIA_UNDERSTANDING_ENABLED": (
        "音视频理解",
        "用多模态模型理解音频或视频内容。",
    ),
    "FCGO_WEB_READ_ENABLED": ("网页浏览", "允许读取用户提供的网页链接。"),
    "FCGO_WRITEBACK_ENABLED": ("写入功能", "允许生成并执行写入确认卡片。"),
    "FCGO_WRITEBACK_AUTO_EXECUTE_ENABLED": (
        "自动写入总开关",
        "允许用户开启低风险自动执行；关闭后所有写入都需要确认。",
    ),
    "FCGO_WRITEBACK_CONFIRMATION_MODE": (
        "写入确认策略",
        "每次确认：所有写入先生成确认卡；低风险自动执行：仅明确文档开头/末尾追加可自动执行；仅生成草稿：不执行写入，只返回草稿。",
    ),
}

SETTING_CHOICE_LABELS: dict[str, dict[str, str]] = {
    "FCGO_WRITEBACK_CONFIRMATION_MODE": {
        "always": "每次确认 (always)",
        "low_risk_direct": "低风险自动执行 (low_risk_direct)",
        "draft_only": "仅生成草稿 (draft_only)",
    },
}


def mount_admin_routes(
    app: FastAPI,
    *,
    settings: Settings,
    store: SQLiteStore,
    oauth: FeishuOAuthService,
) -> None:
    @app.get("/admin")
    async def admin_home(request: Request) -> Response:
        if not settings.admin_enabled:
            return HTMLResponse(
                _message_page("本地配置后台未启用", "请检查 FCGO_ADMIN_ENABLED。"),
                404,
            )
        user_open_id = _session_open_id(request)
        if not user_open_id:
            if await _bootstrap_setup_allowed(request, store):
                return RedirectResponse("/admin/setup", status_code=303)
            return HTMLResponse(_login_page(settings))
        owner_open_id = await _owner_open_id(store)
        if owner_open_id and owner_open_id != user_open_id:
            return HTMLResponse(
                _message_page("无权访问", "当前飞书用户不是本地配置后台管理员。"),
                403,
            )
        return HTMLResponse(
            await _admin_page(request, _admin_view_settings(settings), store, user_open_id)
        )

    @app.get("/admin/setup")
    async def admin_setup_home(request: Request) -> HTMLResponse:
        if not settings.admin_enabled:
            return HTMLResponse(
                _message_page("本地配置后台未启用", "请检查 FCGO_ADMIN_ENABLED。"),
                404,
            )
        user_open_id = _session_open_id(request)
        if not user_open_id:
            if await _bootstrap_setup_allowed(request, store):
                return HTMLResponse(
                    await _setup_admin_page(
                        request,
                        _admin_view_settings(settings),
                        "本机首次配置",
                        bootstrap=True,
                    )
                )
            return HTMLResponse(_login_page(settings))
        owner_open_id = await _owner_open_id(store)
        if owner_open_id and owner_open_id != user_open_id:
            return HTMLResponse(
                _message_page("无权访问", "当前飞书用户不是本地配置后台管理员。"),
                403,
            )
        return HTMLResponse(
            await _setup_admin_page(
                request,
                _admin_view_settings(settings),
                user_open_id,
                bootstrap=False,
            )
        )

    @app.get("/admin/advanced")
    async def admin_advanced_home(request: Request) -> HTMLResponse:
        if not settings.admin_enabled:
            return HTMLResponse(
                _message_page("本地配置后台未启用", "请检查 FCGO_ADMIN_ENABLED。"),
                404,
            )
        user_open_id = _session_open_id(request)
        if not user_open_id:
            return HTMLResponse(_login_page(settings))
        owner_open_id = await _owner_open_id(store)
        if owner_open_id and owner_open_id != user_open_id:
            return HTMLResponse(
                _message_page("无权访问", "当前飞书用户不是本地配置后台管理员。"),
                403,
            )
        return HTMLResponse(
            await _advanced_admin_page(request, _admin_view_settings(settings), user_open_id)
        )

    @app.get("/admin/login")
    async def admin_login(request: Request) -> RedirectResponse:
        state = token_urlsafe(32)
        authorization_url = _admin_authorization_url(
            settings,
            state,
            redirect_uri=_admin_redirect_uri_from_request(request),
        )
        await store.save_oauth_state(
            state=state,
            subject_id=ADMIN_LOGIN_SUBJECT,
            expires_at=oauth_login_expires_at(settings),
        )
        return RedirectResponse(authorization_url, status_code=303)

    @app.get("/admin/oauth/callback")
    async def admin_oauth_callback(
        request: Request,
        code: str = "",
        state: str = "",
    ) -> HTMLResponse:
        if not code or not state:
            return HTMLResponse(_message_page("登录失败", "飞书登录回调缺少必要参数。"), 400)
        subject = await store.consume_oauth_state(state)
        if subject != ADMIN_LOGIN_SUBJECT:
            return HTMLResponse(_message_page("登录失败", "登录链接已失效，请重新打开后台。"), 400)
        try:
            token = await oauth.exchange_code(
                code,
                redirect_uri=_admin_redirect_uri_from_request(request),
            )
            user_info = await _fetch_feishu_user_info(settings, token)
        except Exception as exc:  # noqa: BLE001 - render a local admin login failure
            logger.exception("admin_oauth_login_failed")
            return HTMLResponse(
                _message_page("登录失败", f"没有成功完成飞书登录：{type(exc).__name__}"),
                502,
            )
        user_open_id = _extract_open_id(user_info, token)
        if not user_open_id:
            return HTMLResponse(_message_page("登录失败", "飞书登录结果里没有 open_id。"), 502)
        owner_open_id = await _owner_open_id(store)
        if owner_open_id is None:
            await store.set_app_setting(
                ADMIN_OWNER_SETTING,
                user_open_id,
                updated_by=user_open_id,
            )
        elif owner_open_id != user_open_id:
            return HTMLResponse(
                _message_page("无权访问", "当前飞书用户不是本地配置后台管理员。"),
                403,
            )
        await store.save_oauth_token(user_open_id, token)
        request.session["admin_open_id"] = user_open_id
        request.session["admin_csrf"] = token_urlsafe(24)
        return HTMLResponse(
            _message_page(
                "登录成功",
                "已进入本地配置后台。页面不会暴露模型密钥明文。",
                action_url="/admin",
                action_label="打开配置后台",
            )
        )

    @app.post("/admin/assistant")
    async def admin_update_assistant(request: Request) -> RedirectResponse:
        user_open_id = await _require_admin_user(request, store)
        form = await _form_params(request)
        _require_csrf(request, form)
        if form.get("reset") == "1":
            await store.clear_assistant_name_preference(user_open_id, updated_by=user_open_id)
            await store.clear_assistant_profile_preference(user_open_id, updated_by=user_open_id)
            return RedirectResponse("/admin?saved=assistant", status_code=303)
        name = _clean_short_text(form.get("assistant_name", ""), max_chars=20)
        profile = _clean_short_text(form.get("assistant_profile", ""), max_chars=500)
        if not name or not profile:
            raise HTTPException(status_code=400, detail="助手名称和简介不能为空")
        await store.save_assistant_name_preference(
            subject_id=user_open_id,
            assistant_name=name,
            updated_by=user_open_id,
        )
        await store.save_assistant_profile_preference(
            subject_id=user_open_id,
            assistant_profile=profile,
            updated_by=user_open_id,
        )
        return RedirectResponse("/admin?saved=assistant", status_code=303)

    @app.post("/admin/model")
    async def admin_update_model(request: Request) -> RedirectResponse:
        user_open_id = await _require_admin_user(request, store)
        form = await _form_params(request)
        _require_csrf(request, form)
        scope = f"user:{user_open_id}"
        if form.get("reset") == "1":
            await store.clear_model_preference(scope, updated_by=user_open_id)
            return RedirectResponse("/admin?saved=model", status_code=303)
        provider = _clean_identifier(form.get("provider", ""))
        model = _clean_short_text(form.get("model", ""), max_chars=120) or None
        if not provider:
            raise HTTPException(status_code=400, detail="模型 Provider 不能为空")
        model = _validated_personal_model(settings, provider, model)
        await store.save_model_preference(
            scope=scope,
            provider=provider,
            model=model,
            updated_by=user_open_id,
        )
        return RedirectResponse("/admin?saved=model", status_code=303)

    @app.post("/admin/config")
    async def admin_update_config(request: Request) -> RedirectResponse:
        form = await _form_params(request)
        _require_csrf(request, form)
        config_scope = _clean_identifier(form.get("config_scope", "all")) or "all"
        _validate_config_scope(config_scope)
        if config_scope == "setup" and await _bootstrap_setup_allowed(request, store):
            user_open_id = "bootstrap-local"
        else:
            user_open_id = await _require_admin_user(request, store)
        view_settings = _admin_view_settings(settings)
        if config_scope in {"all", "assistant"}:
            name = _clean_short_text(form.get("assistant_name", ""), max_chars=20)
            profile = _clean_short_text(form.get("assistant_profile", ""), max_chars=500)
            if not name or not profile:
                raise HTTPException(status_code=400, detail="助手名称和简介不能为空")
            await store.save_assistant_name_preference(
                subject_id=user_open_id,
                assistant_name=name,
                updated_by=user_open_id,
            )
            await store.save_assistant_profile_preference(
                subject_id=user_open_id,
                assistant_profile=profile,
                updated_by=user_open_id,
            )
            await store.set_memory_enabled(
                subject_id=user_open_id,
                enabled=form.get("memory_enabled") == "true",
                updated_by=user_open_id,
            )
            existing_memory = _memory_items_text(await store.list_memory_items(user_open_id))
            new_memory = _clean_memory_text(form.get("memory_content", ""))
            if new_memory != existing_memory:
                await store.clear_memory_items(user_open_id, updated_by=user_open_id)
                if new_memory:
                    await store.save_memory_item(
                        id=f"admin-memory-{_clean_identifier(user_open_id)}",
                        subject_id=user_open_id,
                        kind="长期记忆",
                        content=new_memory,
                        source="admin.page",
                    )
        if config_scope in {"all", "model"}:
            provider = _clean_identifier(form.get("personal_provider", ""))
            model = _clean_short_text(form.get("personal_model", ""), max_chars=120) or None
            scope = f"user:{user_open_id}"
            if provider in {"", "__default__"}:
                await store.clear_model_preference(scope, updated_by=user_open_id)
            else:
                model = _validated_personal_model(view_settings, provider, model)
                await store.save_model_preference(
                    scope=scope,
                    provider=provider,
                    model=model,
                    updated_by=user_open_id,
                )
        try:
            env_update_count = _save_env_settings(
                view_settings,
                form,
                allowed_env_names=_config_scope_env_names(config_scope),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        next_path = _safe_admin_next_path(form.get("next", "/admin"))
        return RedirectResponse(
            f"{next_path}?saved=config&env={env_update_count}",
            status_code=303,
        )

    @app.post("/admin/model/test")
    async def admin_test_model(request: Request) -> Response:
        if not await _bootstrap_setup_allowed(request, store):
            await _require_admin_user(request, store)
        form = await _form_params(request)
        _require_csrf(request, form)
        provider = _clean_identifier(form.get("provider", ""))
        model = _clean_short_text(form.get("model", ""), max_chars=120) or None
        next_path = _safe_admin_next_path(form.get("next", "/admin"))
        if not provider:
            if _wants_json(request):
                return JSONResponse(
                    {"ok": False, "provider": provider, "message": "缺少 Provider。"},
                    status_code=400,
                )
            return RedirectResponse(
                f"{next_path}?model_test=missing_provider",
                status_code=303,
            )
        try:
            router = build_model_router(_admin_view_settings(settings))
            await router.generate_model(
                ModelRequest(
                    request_id=f"admin-test-{token_urlsafe(8)}",
                    provider=provider,
                    model=model,
                    max_output_tokens=16,
                    messages=[
                        ModelMessage(
                            role=ModelMessageRole.USER,
                            content="请只回复 OK，用于测试模型连通性。",
                        )
                    ],
                )
            )
        except Exception as exc:  # noqa: BLE001 - return a readable admin test result
            logger.warning("admin_model_test_failed provider=%s error=%s", provider, exc)
            if _wants_json(request):
                return JSONResponse(
                    {
                        "ok": False,
                        "provider": provider,
                        "message": f"连接失败：{type(exc).__name__}",
                    },
                    status_code=200,
                )
            error = urlencode(
                {
                    "model_test": "failed",
                    "provider": provider,
                    "error": type(exc).__name__,
                }
            )
            return RedirectResponse(f"{next_path}?{error}", status_code=303)
        if _wants_json(request):
            return JSONResponse({"ok": True, "provider": provider, "message": "连接正常"})
        query = urlencode({"model_test": "ok", "provider": provider})
        return RedirectResponse(f"{next_path}?{query}", status_code=303)

    @app.post("/admin/model/default")
    async def admin_set_default_model_provider(request: Request) -> RedirectResponse:
        if not await _bootstrap_setup_allowed(request, store):
            await _require_admin_user(request, store)
        form = await _form_params(request)
        _require_csrf(request, form)
        provider_key = _clean_identifier(form.get("provider", ""))
        next_path = _safe_admin_next_path(form.get("next", "/admin"))
        provider = _model_provider_by_key(_admin_view_settings(settings), provider_key)
        if provider is None or not provider.configured:
            raise HTTPException(status_code=400, detail="只能切换到已配置的模型接口")
        updates: dict[str, str | None] = {
            "FCGO_DEFAULT_PROVIDER": provider.key,
            "FCGO_DEFAULT_MODEL": provider.model or None,
        }
        _write_validated_env_settings(settings, updates)
        return RedirectResponse(f"{next_path}?saved=model-default", status_code=303)

    @app.post("/admin/model/delete")
    async def admin_delete_model_provider(request: Request) -> RedirectResponse:
        if not await _bootstrap_setup_allowed(request, store):
            await _require_admin_user(request, store)
        form = await _form_params(request)
        _require_csrf(request, form)
        provider_key = _clean_identifier(form.get("provider", ""))
        next_path = _safe_admin_next_path(form.get("next", "/admin"))
        if provider_key == _admin_view_settings(settings).default_provider:
            raise HTTPException(
                status_code=400,
                detail="系统默认模型接口不能删除，请先切换系统默认",
            )
        env_names = _model_provider_env_names(provider_key)
        if not env_names:
            raise HTTPException(status_code=400, detail="未知模型接口")
        _write_validated_env_settings(settings, dict.fromkeys(env_names, None))
        return RedirectResponse(f"{next_path}?saved=model-delete", status_code=303)

    @app.post("/admin/media/test")
    async def admin_test_media(request: Request) -> Response:
        if not await _bootstrap_setup_allowed(request, store):
            await _require_admin_user(request, store)
        form = await _form_params(request)
        _require_csrf(request, form)
        provider = _clean_identifier(form.get("provider", ""))
        next_path = _safe_admin_next_path(form.get("next", "/admin"))
        try:
            await _test_media_provider_connection(_admin_view_settings(settings), provider)
        except Exception as exc:  # noqa: BLE001 - return a readable admin test result
            logger.warning("admin_media_test_failed provider=%s error=%s", provider, exc)
            message = f"连接失败：{type(exc).__name__}"
            if _wants_json(request):
                return JSONResponse(
                    {"ok": False, "provider": provider, "message": message},
                    status_code=200,
                )
            query = urlencode(
                {
                    "media_test": "failed",
                    "provider": provider,
                    "error": type(exc).__name__,
                }
            )
            return RedirectResponse(f"{next_path}?{query}", status_code=303)
        if _wants_json(request):
            return JSONResponse({"ok": True, "provider": provider, "message": "接口可达"})
        query = urlencode({"media_test": "ok", "provider": provider})
        return RedirectResponse(f"{next_path}?{query}", status_code=303)

    @app.post("/admin/media/delete")
    async def admin_delete_media_provider(request: Request) -> RedirectResponse:
        if not await _bootstrap_setup_allowed(request, store):
            await _require_admin_user(request, store)
        form = await _form_params(request)
        _require_csrf(request, form)
        provider_key = _clean_identifier(form.get("provider", ""))
        next_path = _safe_admin_next_path(form.get("next", "/admin"))
        env_names = _media_provider_env_names(provider_key)
        if not env_names:
            raise HTTPException(status_code=400, detail="未知媒体或工作流接口")
        _write_validated_env_settings(settings, dict.fromkeys(env_names, None))
        return RedirectResponse(f"{next_path}?saved=media-delete", status_code=303)

    @app.get("/admin/logout")
    async def admin_logout(request: Request) -> RedirectResponse:
        request.session.pop("admin_open_id", None)
        request.session.pop("admin_csrf", None)
        return RedirectResponse("/admin", status_code=303)


def oauth_login_expires_at(settings: Settings) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=settings.oauth_state_ttl_seconds)


def _admin_authorization_url(settings: Settings, state: str, *, redirect_uri: str) -> str:
    params = {
        "app_id": settings.feishu_app_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if settings.oauth_scope_list:
        params["scope"] = " ".join(settings.oauth_scope_list)
    auth_base_url = settings.feishu_auth_base_url.rstrip("/")
    return f"{auth_base_url}/open-apis/authen/v1/authorize?{urlencode(params)}"


def _admin_redirect_uri(settings: Settings) -> str:
    return f"{settings.base_url.rstrip('/')}/admin/oauth/callback"


def _admin_redirect_uri_from_request(request: Request) -> str:
    return str(request.url_for("admin_oauth_callback"))


async def _owner_open_id(store: SQLiteStore) -> str | None:
    value = await store.get_app_setting(ADMIN_OWNER_SETTING)
    return value if isinstance(value, str) and value.strip() else None


async def _bootstrap_setup_allowed(request: Request, store: SQLiteStore) -> bool:
    return await _owner_open_id(store) is None and _is_local_admin_request(request)


def _is_local_admin_request(request: Request) -> bool:
    host = (request.url.hostname or "").lower()
    if not host and request.client is not None:
        host = request.client.host.lower()
    return host in {"127.0.0.1", "localhost", "::1"}


async def _require_admin_user(request: Request, store: SQLiteStore) -> str:
    user_open_id = _session_open_id(request)
    if not user_open_id:
        raise HTTPException(status_code=401, detail="not logged in")
    owner_open_id = await _owner_open_id(store)
    if owner_open_id and owner_open_id != user_open_id:
        raise HTTPException(status_code=403, detail="not admin owner")
    return user_open_id


def _session_open_id(request: Request) -> str:
    value = request.session.get("admin_open_id")
    return str(value) if value else ""


async def _form_params(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _require_csrf(request: Request, form: Mapping[str, str]) -> None:
    session_token = str(request.session.get("admin_csrf") or "")
    form_token = form.get("csrf", "")
    if not session_token or form_token != session_token:
        raise HTTPException(status_code=400, detail="invalid csrf token")


def _safe_admin_next_path(value: str) -> str:
    if value in {"/admin", "/admin/advanced", "/admin/setup"}:
        return value
    return "/admin"


def _wants_json(request: Request) -> bool:
    return (
        "application/json" in request.headers.get("accept", "")
        or request.headers.get("x-requested-with") == "fetch"
    )


def _admin_view_settings(settings: Settings) -> Settings:
    env_path = _env_file_path(settings)
    if not env_path.exists():
        return settings
    try:
        return Settings(_env_file=env_path)  # type: ignore[call-arg]
    except ValidationError as exc:
        logger.warning("admin_view_settings_invalid error=%s", redact(str(exc)))
        return settings


async def _fetch_feishu_user_info(
    settings: Settings,
    token: Mapping[str, Any],
) -> dict[str, Any]:
    access_token = _token_value(token, "access_token")
    if not access_token:
        raise RuntimeError("missing access_token")
    url = f"{settings.feishu_base_url.rstrip('/')}/open-apis/authen/v1/user_info"
    async with httpx.AsyncClient(timeout=20, proxy=settings.feishu_http_proxy) as client:
        response = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
        response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("code", 0) != 0:
        raise RuntimeError(f"Feishu user info error {payload.get('code')}: {payload.get('msg')}")
    return payload if isinstance(payload, dict) else {}


def _extract_open_id(*payloads: Mapping[str, Any]) -> str:
    for payload in payloads:
        for source in _candidate_sources(payload):
            open_id = source.get("open_id")
            if open_id:
                return str(open_id)
    return ""


def _candidate_sources(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    sources: list[Mapping[str, Any]] = [payload]
    data = payload.get("data")
    if isinstance(data, Mapping):
        sources.append(data)
    raw = payload.get("raw")
    if isinstance(raw, Mapping):
        sources.append(raw)
        raw_data = raw.get("data")
        if isinstance(raw_data, Mapping):
            sources.append(raw_data)
    return sources


def _token_value(token: Mapping[str, Any], key: str) -> str:
    for source in _candidate_sources(token):
        value = source.get(key)
        if value:
            return str(value)
    return ""


def _save_env_settings(
    settings: Settings,
    form: Mapping[str, str],
    *,
    allowed_env_names: set[str] | None,
) -> int:
    updates: dict[str, str | None] = {}
    for spec in _setting_field_specs(settings):
        if spec.env_name in ADMIN_EXCLUDED_ENV_NAMES:
            continue
        if allowed_env_names is not None and spec.env_name not in allowed_env_names:
            continue
        input_name = _env_input_name(spec.env_name)
        clear_name = _env_clear_name(spec.env_name)
        if spec.write_only:
            if form.get(clear_name) == "1":
                updates[spec.env_name] = ""
            elif form.get(input_name, ""):
                updates[spec.env_name] = form[input_name]
            continue
        if input_name not in form:
            continue
        raw_value = form[input_name]
        if spec.optional and raw_value == "":
            updates[spec.env_name] = None
        else:
            updates[spec.env_name] = raw_value
    if not updates:
        return 0
    env_path = _env_file_path(settings)
    _validate_env_updates(settings, env_path, updates)
    _write_env_file(env_path, updates)
    return len(updates)


def _validate_config_scope(scope: str) -> None:
    if scope not in {
        "all",
        "assistant",
        "basic",
        "feishu",
        "capabilities",
        "model",
        "media",
        "setup",
    }:
        raise HTTPException(status_code=400, detail=f"不支持的配置范围：{scope}")


def _config_scope_env_names(scope: str) -> set[str] | None:
    if scope == "all":
        return None
    if scope == "assistant":
        return set()
    if scope == "basic":
        return {"FCGO_ENV", "FCGO_LOG_LEVEL", "FCGO_BASE_URL", "FCGO_AGENT_MODE"}
    if scope == "feishu":
        return {
            "FEISHU_APP_ID",
            "FEISHU_APP_SECRET",
            "FEISHU_VERIFICATION_TOKEN",
            "FEISHU_ENCRYPT_KEY",
            "FEISHU_BOT_OPEN_ID",
            "FEISHU_BOT_NAME",
            "FCGO_OAUTH_ENABLE_OFFLINE_ACCESS",
        }
    if scope == "capabilities":
        return {
            "FCGO_RESOURCE_SEARCH_ENABLED",
            "FCGO_ATTACHMENT_VISION_ENABLED",
            "FCGO_ATTACHMENT_MEDIA_UNDERSTANDING_ENABLED",
            "FCGO_ATTACHMENT_OCR_ENABLED",
            "FCGO_WEB_READ_ENABLED",
            "FCGO_WRITEBACK_ENABLED",
            "FCGO_WRITEBACK_AUTO_EXECUTE_ENABLED",
            "FCGO_WRITEBACK_CONFIRMATION_MODE",
        }
    if scope == "model":
        env_names = {"FCGO_DEFAULT_PROVIDER", "FCGO_DEFAULT_MODEL"}
        for raw in MODEL_PROVIDER_SPECS:
            env_names.update(
                {
                    str(raw["display_name"]),
                    str(raw["api_key"]),
                    str(raw["base_url"]),
                    str(raw["model"]),
                }
            )
        return env_names
    if scope == "media":
        env_names = set()
        for _key, _label, group_env_names in MEDIA_PROVIDER_GROUPS:
            env_names.update(group_env_names)
        return env_names
    if scope == "setup":
        env_names = {
            "FEISHU_APP_ID",
            "FEISHU_APP_SECRET",
            "FEISHU_VERIFICATION_TOKEN",
            "FEISHU_ENCRYPT_KEY",
            "FEISHU_BOT_OPEN_ID",
            "FEISHU_BOT_NAME",
            "FCGO_OAUTH_ENABLE_OFFLINE_ACCESS",
            "FCGO_BASE_URL",
            "FCGO_ENV",
            "FCGO_AGENT_MODE",
            "FCGO_DEFAULT_PROVIDER",
            "FCGO_DEFAULT_MODEL",
            "FCGO_RESOURCE_SEARCH_ENABLED",
            "FCGO_ATTACHMENT_VISION_ENABLED",
            "FCGO_ATTACHMENT_MEDIA_UNDERSTANDING_ENABLED",
            "FCGO_ATTACHMENT_OCR_ENABLED",
            "FCGO_WEB_READ_ENABLED",
            "FCGO_WRITEBACK_ENABLED",
            "FCGO_WRITEBACK_CONFIRMATION_MODE",
        }
        for raw in MODEL_PROVIDER_SPECS:
            env_names.update(
                {
                    str(raw["display_name"]),
                    str(raw["api_key"]),
                    str(raw["base_url"]),
                    str(raw["model"]),
                }
            )
        return env_names
    return set()


async def _test_media_provider_connection(settings: Settings, provider_key: str) -> None:
    if not provider_key:
        raise ValueError("missing provider")
    specs = _specs_by_env(settings)
    for key, label, env_names in MEDIA_PROVIDER_GROUPS:
        if key != provider_key:
            continue
        values = {env_name: specs[env_name].value for env_name in env_names}
        base_url = next(
            (value for env_name, value in values.items() if env_name.endswith("_BASE_URL")),
            "",
        ).strip()
        if not base_url:
            raise ValueError(f"{label} missing base url")
        requires_secret = key in {"seedance", "coze", "dify"}
        has_secret = any(
            specs[env_name].configured for env_name in env_names if specs[env_name].write_only
        )
        if requires_secret and not has_secret:
            raise ValueError(f"{label} missing api key")
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            response = await client.get(base_url)
        if response.status_code >= 500:
            raise RuntimeError(f"{label} returned {response.status_code}")
        return
    raise ValueError(f"unsupported provider: {provider_key}")


def _validate_env_updates(
    settings: Settings,
    env_path: Path,
    updates: Mapping[str, str | None],
) -> None:
    candidate = _env_values(env_path)
    for key, value in updates.items():
        if value is None:
            candidate.pop(key, None)
        else:
            candidate[key] = value
    temp_path = env_path.with_name(f".{env_path.name}.validate.tmp")
    try:
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(_env_text(candidate), encoding="utf-8")
        candidate_settings = Settings(_env_file=temp_path)  # type: ignore[call-arg]
        _validate_default_model_pair(candidate_settings)
    except ValidationError as exc:
        raise ValueError(f"配置校验失败：{exc.errors()[0]['msg']}") from exc
    finally:
        temp_path.unlink(missing_ok=True)


def _validate_default_model_pair(settings: Settings) -> None:
    if not settings.default_model:
        return
    providers = {provider.key: provider for provider in _model_provider_options(settings)}
    provider = providers.get(settings.default_provider)
    if provider is None:
        raise ValueError(f"不支持的系统默认 Provider：{settings.default_provider}")
    if settings.default_model not in provider.suggested_models:
        raise ValueError("系统默认模型与 Provider 不匹配")


def _write_env_file(env_path: Path, updates: Mapping[str, str | None]) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        match = _ENV_LINE_RE.match(line)
        if not match:
            output.append(line)
            continue
        key = match.group(1)
        if key not in updates:
            output.append(line)
            continue
        seen.add(key)
        value = updates[key]
        if value is not None:
            output.append(f"{key}={_format_env_value(value)}")
    for key, value in updates.items():
        if key in seen or value is None:
            continue
        output.append(f"{key}={_format_env_value(value)}")
    env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _write_validated_env_settings(
    settings: Settings,
    updates: Mapping[str, str | None],
) -> None:
    env_path = _env_file_path(settings)
    _validate_env_updates(settings, env_path, updates)
    _write_env_file(env_path, updates)


def _env_values(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}
    values = dotenv_values(env_path)
    return {str(key): str(value) for key, value in values.items() if value is not None}


def _env_text(values: Mapping[str, str]) -> str:
    return "\n".join(f"{key}={_format_env_value(value)}" for key, value in values.items())


def _format_env_value(value: str) -> str:
    if value == "":
        return ""
    if any(char.isspace() for char in value) or any(char in value for char in ('"', "'", "#")):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return value


def _env_file_path(settings: Settings) -> Path:
    path = settings.admin_config_path
    return path if path.is_absolute() else Path.cwd() / path


def _setting_field_specs(settings: Settings) -> list[SettingFieldSpec]:
    specs: list[SettingFieldSpec] = []
    for name, field in Settings.model_fields.items():
        env_name = str(field.alias or name)
        if env_name in ADMIN_EXCLUDED_ENV_NAMES:
            continue
        value = getattr(settings, name)
        secret = _contains_type(field.annotation, SecretStr)
        write_only = secret or env_name in WRITE_ONLY_ENV_NAMES
        text_value = "" if write_only else _setting_value_to_text(value)
        configured = bool(value.get_secret_value()) if isinstance(value, SecretStr) else bool(value)
        specs.append(
            SettingFieldSpec(
                name=name,
                env_name=env_name,
                group=_setting_group(env_name),
                title=_field_title(env_name),
                description=_field_description(env_name),
                value=text_value,
                secret=secret,
                write_only=write_only,
                configured=configured,
                optional=field.default is None,
                multiline=_is_multiline_setting(env_name, text_value),
                choices=_setting_choices_for(env_name, field.annotation),
            )
        )
    return specs


def _field_title(env_name: str) -> str:
    metadata = FIELD_METADATA.get(env_name)
    if metadata:
        return metadata[0]
    if env_name.endswith("_DISPLAY_NAME"):
        return "显示名称"
    return _humanize_env_name(env_name)


def _field_description(env_name: str) -> str:
    metadata = FIELD_METADATA.get(env_name)
    if metadata:
        return metadata[1]
    if env_name.endswith("_API_KEY"):
        return "对应服务的 API Key。"
    if env_name.endswith("_DISPLAY_NAME"):
        return "只用于后台展示和你自己识别，不影响真实模型名或接口地址。"
    if env_name.endswith("_BASE_URL"):
        return "对应服务的接口地址。"
    if env_name.endswith("_MODEL"):
        return "该 Provider 默认使用的模型名。"
    if env_name.endswith("_TIMEOUT_SECONDS"):
        return "请求超时时间，单位为秒。"
    if env_name.endswith("_MAX_BYTES"):
        return "允许读取的最大字节数。"
    if env_name.endswith("_MAX_CHARS"):
        return "允许注入或返回的最大字符数。"
    if env_name.endswith("_ENABLED"):
        return "是否启用这个功能。"
    return ""


def _humanize_env_name(env_name: str) -> str:
    cleaned = env_name
    for prefix in ("FCGO_", "FEISHU_", "GEMINI_", "OPENAI_", "DEEPSEEK_"):
        cleaned = cleaned.removeprefix(prefix)
    words = [word for word in cleaned.split("_") if word]
    if not words:
        return env_name
    return " ".join(word.capitalize() for word in words)


def _setting_value_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _setting_choices_for(env_name: str, annotation: object) -> tuple[str, ...]:
    if env_name == "FCGO_LOG_LEVEL":
        return ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
    return _setting_choices(annotation)


def _setting_choices(annotation: object) -> tuple[str, ...]:
    base = _base_annotation(annotation)
    if base is bool:
        return ("true", "false")
    if get_origin(base) is Literal:
        return tuple(str(item) for item in get_args(base))
    if isinstance(base, type) and issubclass(base, Enum):
        return tuple(str(item.value) for item in base)
    return ()


def _base_annotation(annotation: object) -> object:
    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    if len(args) == 1:
        return args[0]
    return annotation


def _contains_type(annotation: object, target: type[object]) -> bool:
    if annotation is target:
        return True
    return any(_contains_type(arg, target) for arg in get_args(annotation))


def _is_multiline_setting(env_name: str, value: str) -> bool:
    markers = ("SCOPES", "PROMPT", "ALLOWED_HOSTS", "BLOCKED_HOSTS")
    return "\n" in value or len(value) > 80 or any(marker in env_name for marker in markers)


def _setting_group(env_name: str) -> str:
    if env_name.startswith(("FEISHU_", "FCGO_OAUTH")):
        return "飞书应用"
    if env_name.startswith(
        (
            "GEMINI_",
            "OPENAI_",
            "DEEPSEEK_",
            "QWEN_",
            "DOUBAO_",
            "MINIMAX_",
            "ANTHROPIC_",
            "FCGO_DEFAULT_",
            "FCGO_MODEL_",
            "FCGO_OPENAI_COMPATIBLE_",
        )
    ):
        return "模型接口"
    if env_name.startswith(("SEEDANCE_", "COMFYUI_", "COZE_", "DIFY_")):
        return "媒体与工作流"
    if env_name.startswith(
        (
            "FCGO_ATTACHMENT_",
            "FCGO_EMBEDDED_",
            "FCGO_PDF_",
            "FCGO_MAX_RESOURCE_",
            "FCGO_MAX_MESSAGE_",
            "FCGO_MAX_SHEET_",
            "FCGO_MAX_BITABLE_",
            "FCGO_DOC_BLOCK_",
        )
    ):
        return "资源与附件读取"
    if env_name.startswith("FCGO_WEB_"):
        return "网页读取"
    if env_name.startswith(("FCGO_CONTEXT_", "FCGO_MEMORY_")):
        return "上下文与记忆"
    if env_name.startswith(
        (
            "FCGO_WRITEBACK_",
            "FCGO_PENDING_",
            "FCGO_CARD_",
            "FCGO_MAX_WRITEBACK_",
        )
    ):
        return "写入"
    if env_name.startswith("FCGO_AGENT_"):
        return "Agent"
    if env_name.startswith("FCGO_ADMIN_"):
        return "后台"
    return "基础服务"


def _env_input_name(env_name: str) -> str:
    return f"env__{env_name}"


def _env_clear_name(env_name: str) -> str:
    return f"clear__{env_name}"


async def _admin_page(
    request: Request,
    settings: Settings,
    store: SQLiteStore,
    user_open_id: str,
) -> str:
    csrf = str(request.session.get("admin_csrf") or "")
    if not csrf:
        csrf = token_urlsafe(24)
        request.session["admin_csrf"] = csrf
    name_preference = await store.get_assistant_name_preference(user_open_id)
    profile_preference = await store.get_assistant_profile_preference(user_open_id)
    model_preference = await store.get_model_preference(f"user:{user_open_id}")
    memory_enabled = await store.is_memory_enabled(user_open_id)
    memory_items = await store.list_memory_items(user_open_id)
    memory_content = _memory_items_text(memory_items)
    saved_banner = _saved_banner(request)
    assistant_name = (
        name_preference.assistant_name if name_preference else settings.assistant_default_name
    )
    assistant_profile = (
        profile_preference.assistant_profile
        if profile_preference
        else settings.assistant_default_profile
    )
    provider = model_preference.provider if model_preference else "__default__"
    model = model_preference.model if model_preference and model_preference.model else ""
    provider_options = _model_provider_options(settings)
    escaped_assistant_name = escape(assistant_name)
    escaped_assistant_profile = escape(assistant_profile)
    escaped_memory_content = escape(memory_content)
    assistant_profile_control = (
        '<textarea name="assistant_profile" maxlength="500">'
        f"{escaped_assistant_profile}</textarea>"
    )
    memory_control = (
        '<textarea name="memory_content" class="memory-editor" maxlength="12000">'
        f"{escaped_memory_content}</textarea>"
    )
    return _layout(
        "本地配置后台",
        f"""
        <header class="topbar">
          <div>
            <h1>本地配置后台</h1>
            <p>当前登录：<code>{escape(user_open_id)}</code></p>
          </div>
          <div class="top-actions">
            <a class="ghost" href="/admin/advanced">高级配置</a>
            <a class="ghost" href="/admin/logout">退出</a>
          </div>
        </header>

        <form method="post" action="/admin/config" class="page-form">
          <input type="hidden" name="csrf" value="{escape(csrf)}" />
          <input type="hidden" name="next" value="/admin" />
          {saved_banner}
          <section class="toolbar">
            <p>普通配置适合日常修改；高级配置用于排障。全局参数保存后需要重启服务才会生效。</p>
            <div class="toolbar-actions">
              <button type="submit" name="config_scope" value="all">保存所有配置</button>
              <button type="reset" class="secondary">重置所有配置</button>
            </div>
          </section>

          <section class="grid">
            <article>
              <h2>助手信息</h2>
              <label>
                名称
                <input name="assistant_name" maxlength="20" value="{escaped_assistant_name}" />
              </label>
              <label>
                简介
                {assistant_profile_control}
              </label>
              <label>
                长期记忆
                <select name="memory_enabled">
                  {_option("true", "开启", selected=memory_enabled)}
                  {_option("false", "关闭", selected=not memory_enabled)}
                </select>
                <span class="help">只控制当前登录用户的长期记忆偏好。</span>
              </label>
              <label>
                记忆内容
                {memory_control}
                <span class="help">可直接编辑。清空文本后保存，会删除当前长期记忆。</span>
              </label>
              {_card_actions("assistant")}
            </article>

            {_basic_settings_card(settings)}
          </section>

          <section class="grid">
            {_feishu_app_card(settings)}
            {_capability_toggles_card(settings)}
          </section>

          <section class="wide">
            <article>
              <h2>模型接口</h2>
              {_model_interfaces_card(settings, provider_options, provider, model, request)}
              {_card_actions("model")}
            </article>
          </section>

          <section class="grid">
            {_media_workflow_card(settings, request)}
          </section>

          <section class="wide">
            <article>
              <h2>机器人菜单功能</h2>
              <p class="hint">飞书开发者后台自定义菜单使用下面的事件 key。</p>
              {_menu_sections()}
            </article>
          </section>
          {_model_options_script(provider_options)}
          {_clear_button_script()}
          {_card_reset_script()}
        </form>
        """,
    )


async def _setup_admin_page(
    request: Request,
    settings: Settings,
    user_open_id: str,
    *,
    bootstrap: bool,
) -> str:
    csrf = str(request.session.get("admin_csrf") or "")
    if not csrf:
        csrf = token_urlsafe(24)
        request.session["admin_csrf"] = csrf
    provider_options = _model_provider_options(settings)
    setup_mode_hint = (
        "当前处于首次安装配置模式，仅允许本机访问。完成飞书应用配置后，请使用飞书登录绑定后台管理员。"
        if bootstrap
        else "当前已通过飞书登录。"
    )
    top_actions = (
        """
            <a class="ghost" href="/admin/login">飞书登录绑定管理员</a>
        """
        if bootstrap
        else """
            <a class="ghost" href="/admin">返回后台</a>
            <a class="ghost" href="/admin/advanced">高级配置</a>
            <a class="ghost" href="/admin/logout">退出</a>
        """
    )
    return _layout(
        "首次配置向导",
        f"""
        <header class="topbar">
          <div>
            <h1>首次配置向导</h1>
            <p>当前登录：<code>{escape(user_open_id)}</code></p>
          </div>
          <div class="top-actions">
            {top_actions}
          </div>
        </header>

        <form method="post" action="/admin/config" class="page-form">
          <input type="hidden" name="csrf" value="{escape(csrf)}" />
          <input type="hidden" name="next" value="/admin/setup" />
          {_saved_banner(request)}

          <section class="toolbar">
            <p>{escape(setup_mode_hint)} 按顺序完成这些配置后，就可以启动机器人并进行基础测试。</p>
            <div class="toolbar-actions">
              <button type="submit" name="config_scope" value="setup">保存向导配置</button>
              <button type="reset" class="secondary">重置</button>
            </div>
          </section>

          <section class="wide">
            <article>
              <h2>配置进度</h2>
              <div class="setup-status-grid">
                {_setup_status_cards(settings, provider_options)}
              </div>
            </article>
          </section>

          <section class="grid">
            <article>
              <h2>1. 服务地址</h2>
              <p class="hint">这里决定飞书 OAuth 回调地址和用户打开后台时看到的服务地址。</p>
              <div class="settings-list one-column">
                {_setting_fields_for(settings, ("FCGO_ENV", "FCGO_BASE_URL", "FCGO_AGENT_MODE"))}
              </div>
              <div class="setup-copy">
                <span>机器人授权回调地址</span>
                <code>{escape(settings.oauth_redirect_uri)}</code>
              </div>
              <div class="setup-copy">
                <span>后台登录回调地址</span>
                <code>{escape(_admin_redirect_uri(settings))}</code>
              </div>
              <p class="hint">
                以上两个地址都需要添加到飞书开放平台的“重定向 URL”。如果你用
                127.0.0.1 打开后台，就在飞书后台添加 127.0.0.1 版本；如果你用
                localhost 打开后台，也要添加 localhost 版本。
              </p>
            </article>

            <article>
              <h2>2. 飞书应用</h2>
              <p class="hint">
                先填写飞书开放平台里的 App ID 和 App Secret；事件校验信息可按你的事件模式补充。
              </p>
              <div class="settings-list one-column">
                {_setting_fields_for(
                    settings,
                    (
                        "FEISHU_APP_ID",
                        "FEISHU_APP_SECRET",
                        "FEISHU_VERIFICATION_TOKEN",
                        "FEISHU_ENCRYPT_KEY",
                        "FEISHU_BOT_OPEN_ID",
                        "FEISHU_BOT_NAME",
                        "FCGO_OAUTH_ENABLE_OFFLINE_ACCESS",
                    ),
                )}
              </div>
            </article>
          </section>

          <section class="wide">
            <article>
              <h2>3. 模型接口</h2>
              <p class="hint">
                至少配置一个模型 Provider，并点击测试连接确认 Key、地址和模型名可用。
              </p>
              <div class="settings-list">
                {_system_default_model_fields(settings, provider_options)}
              </div>
              <div class="provider-cards">
                {_configured_provider_cards(
                    provider_options,
                    system_default_provider=_specs_by_env(settings)["FCGO_DEFAULT_PROVIDER"].value,
                    personal_default_provider="",
                    request=request,
                ) or '<p class="hint">还没有配置可用模型接口。请点击下面的按钮添加。</p>'}
              </div>
              <button type="button" class="secondary open-model-dialog">
                添加模型接口
              </button>
              {_model_config_dialog(settings, provider_options)}
            </article>
          </section>

          <section class="grid">
            <article>
              <h2>4. 常用能力</h2>
              <p class="hint">首次安装建议先保留默认值；确认机器人可用后再按需调整。</p>
              <div class="settings-list one-column">
                {_setting_fields_for(
                    settings,
                    (
                        "FCGO_RESOURCE_SEARCH_ENABLED",
                        "FCGO_ATTACHMENT_VISION_ENABLED",
                        "FCGO_ATTACHMENT_MEDIA_UNDERSTANDING_ENABLED",
                        "FCGO_ATTACHMENT_OCR_ENABLED",
                        "FCGO_WEB_READ_ENABLED",
                        "FCGO_WRITEBACK_ENABLED",
                        "FCGO_WRITEBACK_CONFIRMATION_MODE",
                    ),
                )}
              </div>
            </article>

            <article>
              <h2>5. 飞书菜单</h2>
              <p class="hint">
                至少建议在飞书开发者后台添加下面三个入口，后续可以在普通后台查看完整菜单表。
              </p>
              <table class="menu-table compact-table">
                <tbody>
                  <tr><th>本地配置网页</th><td><code>fcgo.admin.open</code></td></tr>
                  <tr><th>使用说明</th><td><code>fcgo.help</code></td></tr>
                  <tr><th>授权状态</th><td><code>fcgo.auth.status</code></td></tr>
                </tbody>
              </table>
              <p class="hint">完整菜单清单在普通后台的“机器人菜单功能”区块。</p>
            </article>
          </section>

          <section class="wide">
            <article>
              <h2>6. 最终检查</h2>
              <ol class="setup-checklist">
                <li>保存向导配置。</li>
                <li>重启本地服务，让全局环境参数生效。</li>
                <li>回到本页面测试模型连接。</li>
                <li>在飞书里点击“本地配置网页”菜单，确认能打开后台。</li>
                <li>发送“帮助”或“帮我搜索飞书文档 测试”做一次机器人验证。</li>
              </ol>
            </article>
          </section>

          {_model_options_script(provider_options)}
          {_clear_button_script()}
          {_card_reset_script()}
        </form>
        """,
    )


async def _advanced_admin_page(
    request: Request,
    settings: Settings,
    user_open_id: str,
) -> str:
    csrf = str(request.session.get("admin_csrf") or "")
    if not csrf:
        csrf = token_urlsafe(24)
        request.session["admin_csrf"] = csrf
    return _layout(
        "高级配置",
        f"""
        <header class="topbar">
          <div>
            <h1>高级配置</h1>
            <p>当前登录：<code>{escape(user_open_id)}</code></p>
          </div>
          <div class="top-actions">
            <a class="ghost" href="/admin">返回普通配置</a>
            <a class="ghost" href="/admin/logout">退出</a>
          </div>
        </header>

        <form method="post" action="/admin/config" class="page-form">
          <input type="hidden" name="csrf" value="{escape(csrf)}" />
          <input type="hidden" name="next" value="/admin/advanced" />
          {_saved_banner(request)}
          <section class="toolbar">
            <p>这里是完整参数表。普通用户不需要修改；修改后通常需要重启服务。</p>
            <div class="toolbar-actions">
              <button type="submit" name="config_scope" value="all">保存高级配置</button>
              <button type="reset" class="secondary">重置高级配置</button>
            </div>
          </section>
          <section class="wide">
            <article>
              <h2>全部高级参数</h2>
              <p class="hint">
                密钥、敏感 ID 和本地路径不会回显明文；留空表示不修改，点击清空按钮后保存才会删除。
              </p>
              {_settings_sections(settings)}
            </article>
          </section>
          {_clear_button_script()}
          {_card_reset_script()}
        </form>
        """,
    )


def _saved_banner(request: Request) -> str:
    messages: list[str] = []
    if request.query_params.get("saved") == "config":
        env_count = request.query_params.get("env", "0")
        messages.append(
            f"配置已保存。已更新 {escape(env_count)} 个环境参数；"
            "全局参数需要重启服务后生效。"
        )
    if not messages:
        return ""
    return '<div class="banner">' + "<br />".join(messages) + "</div>"


def _setup_status_cards(settings: Settings, providers: list[ModelProviderOption]) -> str:
    specs = _specs_by_env(settings)
    feishu_ready = (
        specs["FEISHU_APP_ID"].configured
        and specs["FEISHU_APP_SECRET"].configured
    )
    base_ready = bool(specs["FCGO_BASE_URL"].value.strip())
    model_ready = any(provider.configured for provider in providers)
    default_provider = specs["FCGO_DEFAULT_PROVIDER"].value
    default_ready = any(
        provider.key == default_provider and provider.configured
        for provider in providers
    )
    return "\n".join(
        (
            _setup_status_card(
                "服务地址",
                base_ready,
                "用于生成 OAuth 回调地址和后台链接。",
            ),
            _setup_status_card(
                "飞书应用",
                feishu_ready,
                "需要 App ID 和 App Secret。",
            ),
            _setup_status_card(
                "模型接口",
                model_ready,
                "至少配置一个可用模型 Provider。",
            ),
            _setup_status_card(
                "默认模型",
                default_ready,
                "系统默认 Provider 应指向已配置接口。",
            ),
        )
    )


def _setup_status_card(title: str, ok: bool, description: str) -> str:
    status = "已完成" if ok else "待配置"
    css_class = "ok" if ok else "missing"
    return f"""
    <div class="setup-status-card">
      <strong>{escape(title)}</strong>
      <span class="{css_class}">{status}</span>
      <p>{escape(description)}</p>
    </div>
    """


def _model_provider_options(settings: Settings) -> list[ModelProviderOption]:
    specs_by_env = {spec.env_name: spec for spec in _setting_field_specs(settings)}
    providers: list[ModelProviderOption] = []
    for raw in MODEL_PROVIDER_SPECS:
        api_key_env = str(raw["api_key"])
        display_name_env = str(raw["display_name"])
        base_url_env = str(raw["base_url"])
        model_env = str(raw["model"])
        model = specs_by_env[model_env].value
        display_name = specs_by_env[display_name_env].value or str(raw["label"])
        suggested_model_values = cast(tuple[str, ...], raw["suggested_models"])
        suggested_models = tuple(str(item) for item in suggested_model_values)
        models = tuple(dict.fromkeys((model, *suggested_models))) if model else suggested_models
        providers.append(
            ModelProviderOption(
                key=str(raw["key"]),
                label=str(raw["label"]),
                display_name=display_name,
                model=model,
                base_url=specs_by_env[base_url_env].value,
                default_base_url=str(raw["default_base_url"]),
                api_key_env_name=api_key_env,
                display_name_env_name=display_name_env,
                base_url_env_name=base_url_env,
                model_env_name=model_env,
                suggested_models=models,
                configured=specs_by_env[api_key_env].configured,
            )
        )
    return providers


def _model_provider_by_key(settings: Settings, provider_key: str) -> ModelProviderOption | None:
    return next(
        (
            provider
            for provider in _model_provider_options(settings)
            if provider.key == provider_key
        ),
        None,
    )


def _model_provider_env_names(provider_key: str) -> tuple[str, ...]:
    for raw in MODEL_PROVIDER_SPECS:
        if raw["key"] == provider_key:
            return (
                str(raw["display_name"]),
                str(raw["api_key"]),
                str(raw["base_url"]),
                str(raw["model"]),
            )
    return ()


def _media_provider_env_names(provider_key: str) -> tuple[str, ...]:
    for key, _label, env_names in MEDIA_PROVIDER_GROUPS:
        if key == provider_key:
            return env_names
    return ()


def _validated_personal_model(
    settings: Settings,
    provider: str,
    model: str | None,
) -> str | None:
    providers = {item.key: item for item in _model_provider_options(settings)}
    selected = providers.get(provider)
    if selected is None:
        raise HTTPException(status_code=400, detail=f"不支持的模型 Provider：{provider}")
    if model is None:
        return selected.model or None
    if model not in selected.suggested_models:
        raise HTTPException(status_code=400, detail="模型名与 Provider 不匹配")
    return model


def _specs_by_env(settings: Settings) -> dict[str, SettingFieldSpec]:
    return {spec.env_name: spec for spec in _setting_field_specs(settings)}


def _setting_field_for(settings: Settings, env_name: str) -> str:
    spec = _specs_by_env(settings).get(env_name)
    return _setting_field(spec) if spec else ""


def _setting_fields_for(settings: Settings, env_names: tuple[str, ...]) -> str:
    return "\n".join(_setting_field_for(settings, env_name) for env_name in env_names)


def _card_actions(scope: str) -> str:
    return f"""
    <div class="card-actions">
      <button type="submit" name="config_scope" value="{escape(scope)}">保存</button>
      <button type="button" class="secondary reset-card">重置</button>
    </div>
    """


def _basic_settings_card(settings: Settings) -> str:
    return f"""
    <article>
      <h2>运行设置</h2>
      <div class="settings-list one-column">
        {_setting_fields_for(
            settings,
            ("FCGO_ENV", "FCGO_LOG_LEVEL", "FCGO_BASE_URL", "FCGO_AGENT_MODE"),
        )}
      </div>
      {_card_actions("basic")}
    </article>
    """


def _feishu_app_card(settings: Settings) -> str:
    return f"""
    <article>
      <h2>飞书应用</h2>
      <p class="hint">只填写飞书开放平台必要信息。接口域名、代理、scope 等放在高级配置。</p>
      <div class="settings-list one-column">
        {_setting_fields_for(
            settings,
            (
                "FEISHU_APP_ID",
                "FEISHU_APP_SECRET",
                "FEISHU_VERIFICATION_TOKEN",
                "FEISHU_ENCRYPT_KEY",
                "FEISHU_BOT_OPEN_ID",
                "FEISHU_BOT_NAME",
                "FCGO_OAUTH_ENABLE_OFFLINE_ACCESS",
            ),
        )}
      </div>
      {_card_actions("feishu")}
    </article>
    """


def _model_interfaces_card(
    settings: Settings,
    providers: list[ModelProviderOption],
    personal_provider: str,
    personal_model: str,
    request: Request,
) -> str:
    system_default = _specs_by_env(settings)["FCGO_DEFAULT_PROVIDER"].value
    configured_cards = _configured_provider_cards(
        providers,
        system_default_provider=system_default,
        personal_default_provider=personal_provider,
        request=request,
    )
    return f"""
    <div class="settings-list">
      {_system_default_model_fields(settings, providers)}
      {_personal_default_model_fields(providers, personal_provider, personal_model)}
    </div>
    <div class="provider-cards">
      {configured_cards or '<p class="hint">还没有配置可用模型接口。</p>'}
    </div>
    <button type="button" class="secondary open-model-dialog">
      添加模型接口
    </button>
    {_model_config_dialog(settings, providers)}
    """


def _configured_provider_cards(
    providers: list[ModelProviderOption],
    *,
    system_default_provider: str,
    personal_default_provider: str,
    request: Request,
) -> str:
    cards = []
    for provider in providers:
        if not provider.configured:
            continue
        model = provider.model or "使用 Provider 默认模型"
        test_result = _model_test_result(request, provider.key)
        badges = []
        if provider.key == system_default_provider:
            badges.append('<span class="badge">系统默认</span>')
        if provider.key == personal_default_provider:
            badges.append('<span class="badge">你的默认</span>')
        default_button = (
            ""
            if provider.key == system_default_provider
            else f"""
                <button
                  type="submit"
                  class="secondary small"
                  formaction="/admin/model/default"
                  formmethod="post"
                  name="provider"
                  value="{escape(provider.key)}"
                >
                  设为系统默认
                </button>
            """
        )
        delete_button = (
            ""
            if provider.key == system_default_provider
            else f"""
                <button
                  type="submit"
                  class="secondary danger small"
                  formaction="/admin/model/delete"
                  formmethod="post"
                  name="provider"
                  value="{escape(provider.key)}"
                  onclick="return confirm('确定删除这个模型接口配置吗？')"
                >
                  删除
                </button>
            """
        )
        cards.append(
            f"""
            <div class="provider-card">
              <strong>{escape(provider.display_name)}</strong>
              <span class="provider-kind">{escape(provider.label)}</span>
              <span class="ok">已配置</span>
              {"".join(badges)}
              <p>{escape(model)}</p>
              <div class="inline-actions">
                <button
                  type="button"
                  class="secondary small open-model-dialog"
                  data-config-provider="{escape(provider.key)}"
                >
                  编辑
                </button>
                <button
                  type="submit"
                  class="secondary small"
                  formaction="/admin/model/test"
                  formmethod="post"
                  name="provider"
                  value="{escape(provider.key)}"
                  data-provider="{escape(provider.key)}"
                  data-test-action="model"
                >
                  测试连接
                </button>
                {default_button}
                {delete_button}
                <span class="test-result-slot">{test_result}</span>
              </div>
            </div>
            """
        )
    return "\n".join(cards)


def _model_test_result(request: Request, provider_key: str) -> str:
    if request.query_params.get("provider") != provider_key:
        return ""
    result = request.query_params.get("model_test")
    if result == "ok":
        return '<span class="test-result ok">连接正常</span>'
    if result == "failed":
        error = redact(request.query_params.get("error", "未知错误"))
        return f'<span class="test-result error">连接失败：{escape(error)}</span>'
    return ""


def _system_default_model_fields(
    settings: Settings,
    providers: list[ModelProviderOption],
) -> str:
    specs = _specs_by_env(settings)
    selected_provider = specs["FCGO_DEFAULT_PROVIDER"].value
    selected_model = specs["FCGO_DEFAULT_MODEL"].value
    configured_providers = [provider for provider in providers if provider.configured]
    provider_options = "\n".join(
        _option(provider.key, provider.display_name, selected=provider.key == selected_provider)
        for provider in configured_providers
    )
    model_options = _personal_model_options(configured_providers, selected_provider, selected_model)
    disabled = " disabled" if not configured_providers else ""
    if not provider_options:
        provider_options = '<option value="">请先配置模型接口</option>'
    return f"""
    <label class="setting-field">
      {_field_meta(specs["FCGO_DEFAULT_PROVIDER"])}
      <select
        name="{escape(_env_input_name("FCGO_DEFAULT_PROVIDER"))}"
        id="system-provider"
        {disabled}
      >
        {provider_options}
      </select>
    </label>
    <label class="setting-field">
      {_field_meta(specs["FCGO_DEFAULT_MODEL"])}
      <select
        name="{escape(_env_input_name("FCGO_DEFAULT_MODEL"))}"
        id="system-model"
        data-selected="{escape(selected_model)}"
        {disabled}
      >
        {model_options}
      </select>
    </label>
    """


def _personal_default_model_fields(
    providers: list[ModelProviderOption],
    selected_provider: str,
    selected_model: str,
) -> str:
    provider_options = "\n".join(
        _option(provider.key, provider.display_name, selected=provider.key == selected_provider)
        for provider in providers
        if provider.configured
    )
    return f"""
    <label class="setting-field">
      <span class="field-title">你的默认 Provider</span>
      <code>个人偏好</code>
      <span class="field-desc">只影响当前登录用户。未设置时使用系统默认模型。</span>
      <select name="personal_provider" id="personal-provider">
        {_option("__default__", "使用系统默认", selected=selected_provider == "__default__")}
        {provider_options}
      </select>
    </label>
    <label class="setting-field">
      <span class="field-title">你的默认模型</span>
      <code>个人偏好</code>
      <span class="field-desc">模型列表会随 Provider 切换，只显示匹配模型。</span>
      <select
        name="personal_model"
        id="personal-model"
        data-selected="{escape(selected_model)}"
      >
        {_personal_model_options(providers, selected_provider, selected_model)}
      </select>
    </label>
    """


def _model_config_dialog(
    settings: Settings,
    providers: list[ModelProviderOption],
) -> str:
    provider_options = "\n".join(
        _option(provider.key, provider.display_name, selected=index == 0)
        for index, provider in enumerate(providers)
    )
    return f"""
    <dialog id="model-config-dialog">
      <div class="dialog-head">
        <h3>模型接口</h3>
        <button type="button" class="ghost close-model-dialog">
          关闭
        </button>
      </div>
      <p class="hint">
        当前版本每个 Provider 保存一套接口配置；同一 Provider 下可选择不同模型。
        多套同类 Provider 连接会在模型 profile 架构中支持。
      </p>
      <label>
        选择 Provider
        <select id="provider-config-selector">
          {provider_options}
        </select>
      </label>
      <div class="provider-configs">
        {_provider_config_panels(settings, providers)}
      </div>
      <div class="actions">
        <button type="submit" name="config_scope" value="model">保存配置</button>
        <button type="button" class="secondary close-model-dialog">
          取消
        </button>
      </div>
    </dialog>
    """


def _provider_config_panels(
    settings: Settings,
    providers: list[ModelProviderOption],
) -> str:
    panels = []
    for provider in providers:
        panels.append(
            f"""
            <div class="provider-panel" data-provider-panel="{escape(provider.key)}">
              <h4>{escape(provider.label)}</h4>
              <p class="hint">默认接口：{escape(provider.default_base_url)}</p>
              <div class="settings-list">
                {_setting_fields_for(
                    settings,
                    (
                        provider.display_name_env_name,
                        provider.api_key_env_name,
                        provider.base_url_env_name,
                        provider.model_env_name,
                    ),
                )}
              </div>
            </div>
            """
        )
    return "\n".join(panels)


def _capability_toggles_card(settings: Settings) -> str:
    return f"""
    <article>
      <h2>能力开关</h2>
      <p class="hint">普通模式只显示开关和策略；限制大小、超时、白名单等放在高级配置。</p>
      <div class="settings-list one-column">
        {_setting_fields_for(
            settings,
            (
                "FCGO_RESOURCE_SEARCH_ENABLED",
                "FCGO_ATTACHMENT_VISION_ENABLED",
                "FCGO_ATTACHMENT_MEDIA_UNDERSTANDING_ENABLED",
                "FCGO_ATTACHMENT_OCR_ENABLED",
                "FCGO_WEB_READ_ENABLED",
                "FCGO_WRITEBACK_ENABLED",
                "FCGO_WRITEBACK_AUTO_EXECUTE_ENABLED",
                "FCGO_WRITEBACK_CONFIRMATION_MODE",
            ),
        )}
      </div>
      {_card_actions("capabilities")}
    </article>
    """


def _media_workflow_card(settings: Settings, request: Request) -> str:
    configured = _configured_media_cards(settings, request)
    return f"""
    <article>
      <h2>媒体与工作流</h2>
      <div class="provider-cards compact">
        {configured or '<p class="hint">还没有配置媒体或工作流接口。</p>'}
      </div>
      <button type="button" class="secondary open-media-dialog">
        添加媒体/工作流接口
      </button>
      {_media_config_dialog(settings)}
      {_card_actions("media")}
    </article>
    """


def _configured_media_cards(settings: Settings, request: Request) -> str:
    specs = _specs_by_env(settings)
    cards = []
    for key, label, env_names in MEDIA_PROVIDER_GROUPS:
        display_name = specs[env_names[0]].value or label
        configured = any(specs[env].configured for env in env_names if specs[env].write_only)
        has_url = any(specs[env].value for env in env_names if env.endswith("_BASE_URL"))
        if not configured and not has_url:
            continue
        test_result = _media_test_result(request, key)
        cards.append(
            f"""
            <div class="provider-card">
              <strong>{escape(display_name)}</strong>
              <span class="provider-kind">{escape(label)}</span>
              <span class="ok">已配置</span>
              <div class="inline-actions">
                <button
                  type="button"
                  class="secondary small open-media-dialog"
                  data-config-provider="{escape(key)}"
                >
                  编辑
                </button>
                <button
                  type="submit"
                  class="secondary small"
                  formaction="/admin/media/test"
                  formmethod="post"
                  name="provider"
                  value="{escape(key)}"
                  data-provider="{escape(key)}"
                  data-test-action="media"
                >
                  测试连接
                </button>
                <button
                  type="submit"
                  class="secondary danger small"
                  formaction="/admin/media/delete"
                  formmethod="post"
                  name="provider"
                  value="{escape(key)}"
                  onclick="return confirm('确定删除这个媒体或工作流接口配置吗？')"
                >
                  删除
                </button>
                <span class="test-result-slot">{test_result}</span>
              </div>
            </div>
            """
        )
    return "\n".join(cards)


def _media_test_result(request: Request, provider_key: str) -> str:
    if request.query_params.get("provider") != provider_key:
        return ""
    result = request.query_params.get("media_test")
    if result == "ok":
        return '<span class="test-result ok">接口可达</span>'
    if result == "failed":
        error = redact(request.query_params.get("error", "未知错误"))
        return f'<span class="test-result error">连接失败：{escape(error)}</span>'
    return ""


def _media_config_dialog(settings: Settings) -> str:
    provider_options = "\n".join(
        _option(key, _media_display_name(settings, key, label, env_names), selected=index == 0)
        for index, (key, label, env_names) in enumerate(MEDIA_PROVIDER_GROUPS)
    )
    return f"""
    <dialog id="media-config-dialog">
      <div class="dialog-head">
        <h3>媒体/工作流接口</h3>
        <button type="button" class="ghost close-media-dialog">关闭</button>
      </div>
      <p class="hint">
        显示名称只用于你在后台识别配置；真正调用仍取决于接口地址、密钥和工作流参数。
        多套同类工作流 profile 会在后续 profile 架构中支持。
      </p>
      <label>
        选择接口类型
        <select id="media-config-selector">
          {provider_options}
        </select>
      </label>
      <div class="provider-configs">
        {_media_config_panels(settings)}
      </div>
      <div class="actions">
        <button type="submit" name="config_scope" value="media">保存配置</button>
        <button type="button" class="secondary close-media-dialog">取消</button>
      </div>
    </dialog>
    """


def _media_display_name(
    settings: Settings,
    key: str,
    label: str,
    env_names: tuple[str, ...],
) -> str:
    del key
    specs = _specs_by_env(settings)
    return specs[env_names[0]].value or label


def _media_config_panels(settings: Settings) -> str:
    panels = []
    for key, label, env_names in MEDIA_PROVIDER_GROUPS:
        panels.append(
            f"""
            <div class="provider-panel" data-media-provider-panel="{escape(key)}">
              <h4>{escape(label)}</h4>
              <div class="settings-list">
                {_setting_fields_for(settings, env_names)}
              </div>
            </div>
            """
        )
    return "\n".join(panels)


def _provider_options(
    providers: list[ModelProviderOption],
    selected_provider: str,
) -> str:
    return "\n".join(
        _option(provider.key, provider.label, selected=provider.key == selected_provider)
        for provider in providers
    )


def _personal_model_options(
    providers: list[ModelProviderOption],
    selected_provider: str,
    selected_model: str,
) -> str:
    provider = next((item for item in providers if item.key == selected_provider), None)
    models = provider.suggested_models if provider else ()
    options = [_option("", "使用 Provider 默认模型", selected=selected_model == "")]
    options.extend(
        _option(model, model, selected=model == selected_model)
        for model in models
        if model
    )
    return "\n".join(options)


def _model_options_script(providers: list[ModelProviderOption]) -> str:
    lines = []
    for provider in providers:
        models = ", ".join(f'"{escape(model)}"' for model in provider.suggested_models if model)
        lines.append(f'"{escape(provider.key)}": [{models}]')
    data = "{ " + ", ".join(lines) + " }"
    return f"""
    <script>
      const fcgoModelsByProvider = {data};
      function refreshModelSelect(providerId, modelId) {{
        const providerSelect = document.getElementById(providerId);
        const modelSelect = document.getElementById(modelId);
        if (!providerSelect || !modelSelect) return;
        const selected = modelSelect.dataset.selected || modelSelect.value || "";
        const provider = providerSelect.value;
        const models = fcgoModelsByProvider[provider] || [];
        modelSelect.innerHTML = "";
        const defaultOption = new Option("使用 Provider 默认模型", "");
        modelSelect.appendChild(defaultOption);
        for (const model of models) {{
          const option = new Option(model, model);
          modelSelect.appendChild(option);
        }}
        modelSelect.value = models.includes(selected) ? selected : "";
        modelSelect.dataset.selected = modelSelect.value;
      }}
      function bindModelSelect(providerId, modelId) {{
        const providerSelect = document.getElementById(providerId);
        const modelSelect = document.getElementById(modelId);
        if (!providerSelect || !modelSelect) return;
        providerSelect.addEventListener("change", () => refreshModelSelect(providerId, modelId));
        modelSelect.addEventListener("change", () => {{
          modelSelect.dataset.selected = modelSelect.value;
        }});
        refreshModelSelect(providerId, modelId);
      }}
      bindModelSelect("personal-provider", "personal-model");
      bindModelSelect("system-provider", "system-model");
      const configSelector = document.getElementById("provider-config-selector");
      function refreshProviderConfigPanel() {{
        if (!configSelector) return;
        document.querySelectorAll("[data-provider-panel]").forEach((panel) => {{
          panel.hidden = panel.dataset.providerPanel !== configSelector.value;
        }});
      }}
      if (configSelector) {{
        configSelector.addEventListener("change", refreshProviderConfigPanel);
        refreshProviderConfigPanel();
      }}
      const modelDialog = document.getElementById("model-config-dialog");
      function openModelConfig(provider) {{
        if (configSelector && provider) {{
          configSelector.value = provider;
          refreshProviderConfigPanel();
        }}
        if (modelDialog) modelDialog.showModal();
      }}
      document.querySelectorAll(".open-model-dialog").forEach((button) => {{
        button.addEventListener("click", () => {{
          openModelConfig(button.dataset.configProvider || "");
        }});
      }});
      document.querySelectorAll(".close-model-dialog").forEach((button) => {{
        button.addEventListener("click", () => modelDialog && modelDialog.close());
      }});
      const mediaSelector = document.getElementById("media-config-selector");
      function refreshMediaConfigPanel() {{
        if (!mediaSelector) return;
        document.querySelectorAll("[data-media-provider-panel]").forEach((panel) => {{
          panel.hidden = panel.dataset.mediaProviderPanel !== mediaSelector.value;
        }});
      }}
      if (mediaSelector) {{
        mediaSelector.addEventListener("change", refreshMediaConfigPanel);
        refreshMediaConfigPanel();
      }}
      const mediaDialog = document.getElementById("media-config-dialog");
      function openMediaConfig(provider) {{
        if (mediaSelector && provider) {{
          mediaSelector.value = provider;
          refreshMediaConfigPanel();
        }}
        if (mediaDialog) mediaDialog.showModal();
      }}
      document.querySelectorAll(".open-media-dialog").forEach((button) => {{
        button.addEventListener("click", () => {{
          openMediaConfig(button.dataset.configProvider || "");
        }});
      }});
      document.querySelectorAll(".close-media-dialog").forEach((button) => {{
        button.addEventListener("click", () => mediaDialog && mediaDialog.close());
      }});
      function showTransientResult(slot, status, message) {{
        if (!slot) return;
        slot.innerHTML = "";
        const result = document.createElement("span");
        result.className = `test-result ${{status}}`;
        result.textContent = message;
        slot.appendChild(result);
        window.setTimeout(() => {{
          if (slot.contains(result)) slot.innerHTML = "";
        }}, 5000);
      }}
      document.querySelectorAll("[data-test-action]").forEach((button) => {{
        button.addEventListener("click", async (event) => {{
          event.preventDefault();
          const form = button.form;
          const slot = button.closest(".provider-card")?.querySelector(".test-result-slot");
          if (!form || !slot) return;
          const data = new FormData(form);
          if (button.name) data.set(button.name, button.dataset.provider || button.value);
          const body = new URLSearchParams();
          data.forEach((value, key) => {{
            if (typeof value === "string") body.append(key, value);
          }});
          showTransientResult(slot, "pending", "测试中...");
          try {{
            const response = await fetch(button.formAction || button.getAttribute("formaction"), {{
              method: "POST",
              body,
              headers: {{
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "X-Requested-With": "fetch"
              }}
            }});
            let payload = {{}};
            try {{
              payload = await response.json();
            }} catch (error) {{
              payload = {{
                ok: false,
                message: response.ok ? "连接失败" : `请求失败：${{response.status}}`
              }};
            }}
            const ok = Boolean(payload.ok);
            const fallback = ok ? "连接正常" : "连接失败";
            showTransientResult(slot, ok ? "ok" : "error", payload.message || fallback);
          }} catch (error) {{
            showTransientResult(slot, "error", "连接失败");
          }}
        }});
      }});
      document.querySelectorAll(".test-result-slot .test-result").forEach((result) => {{
        window.setTimeout(() => {{
          const slot = result.closest(".test-result-slot");
          if (slot) slot.innerHTML = "";
        }}, 5000);
      }});
    </script>
    """


def _clear_button_script() -> str:
    return """
    <script>
      document.querySelectorAll("[data-clear-target]").forEach((button) => {
        button.addEventListener("click", () => {
          const target = document.getElementById(button.dataset.clearTarget);
          if (!target) return;
          const clearing = target.value !== "1";
          target.value = clearing ? "1" : "0";
          button.textContent = clearing ? "已标记清空" : "清空";
          button.classList.toggle("danger", clearing);
        });
      });
    </script>
    """


def _card_reset_script() -> str:
    return """
    <script>
      function rememberOriginalControlValues(root) {
        root.querySelectorAll("input, textarea, select").forEach((control) => {
          if (control.type === "checkbox" || control.type === "radio") {
            control.dataset.originalChecked = control.checked ? "1" : "0";
          } else {
            control.dataset.originalValue = control.value;
          }
        });
      }
      function resetControls(root) {
        root.querySelectorAll("input, textarea, select").forEach((control) => {
          if (control.type === "checkbox" || control.type === "radio") {
            control.checked = control.dataset.originalChecked === "1";
          } else if (control.dataset.originalValue !== undefined) {
            control.value = control.dataset.originalValue;
          }
          control.dispatchEvent(new Event("change", { bubbles: true }));
        });
        root.querySelectorAll("[data-clear-target]").forEach((button) => {
          const target = document.getElementById(button.dataset.clearTarget);
          if (target) target.value = "0";
          button.textContent = "清空";
          button.classList.remove("danger");
        });
      }
      rememberOriginalControlValues(document);
      document.querySelectorAll(".reset-card").forEach((button) => {
        button.addEventListener("click", () => {
          const card = button.closest("article") || button.closest("dialog");
          if (card) resetControls(card);
        });
      });
      document.querySelectorAll('button[type="reset"]').forEach((button) => {
        button.addEventListener("click", () => {
          window.setTimeout(() => resetControls(document), 0);
        });
      });
    </script>
    """


def _provider_status_items(providers: list[ModelProviderOption]) -> str:
    items = []
    for provider in providers:
        css_class = "ok" if provider.configured else "missing"
        status = "已配置" if provider.configured else "未配置"
        model = provider.model or "未设置模型"
        items.append(
            "<li>"
            f'<span class="{css_class}">{escape(provider.label)}：{status}</span>'
            f" · {escape(model)}"
            "</li>"
        )
    return "\n".join(items)


def _settings_sections(
    settings: Settings,
    *,
    exclude_env_names: set[str] | None = None,
) -> str:
    exclude = exclude_env_names or set()
    specs = [spec for spec in _setting_field_specs(settings) if spec.env_name not in exclude]
    ordered_groups = (
        "基础服务",
        "后台",
        "Agent",
        "飞书应用",
        "模型接口",
        "资源与附件读取",
        "网页读取",
        "上下文与记忆",
        "写入",
        "媒体与工作流",
    )
    sections = []
    for group in ordered_groups:
        group_specs = [spec for spec in specs if spec.group == group]
        if not group_specs:
            continue
        sections.append(
            f"""
            <details>
              <summary>{escape(group)}</summary>
              <div class="settings-list">
                {"".join(_setting_field(spec) for spec in group_specs)}
              </div>
            </details>
            """
        )
    return "\n".join(sections)


def _setting_field(spec: SettingFieldSpec) -> str:
    meta = _field_meta(spec)
    if spec.write_only:
        placeholder = "•••••••• 已配置，留空不修改" if spec.configured else "未配置"
        clear = (
            f"""
            <input
              type="hidden"
              name="{escape(_env_clear_name(spec.env_name))}"
              id="{escape(_env_clear_name(spec.env_name))}"
              value="0"
            />
            <button
              type="button"
              class="secondary small clear-button"
              data-clear-target="{escape(_env_clear_name(spec.env_name))}"
            >
              清空
            </button>
            """
            if spec.configured
            else ""
        )
        return f"""
        <div class="setting-field">
          {meta}
          <div class="input-action-row">
            <input
              type="password"
              name="{escape(_env_input_name(spec.env_name))}"
              placeholder="{escape(placeholder)}"
            />
            {clear}
          </div>
        </div>
        """
    if spec.choices:
        control = (
            f'<select name="{escape(_env_input_name(spec.env_name))}">'
            + "".join(
                _option(
                    choice,
                    _setting_choice_label(spec.env_name, choice),
                    selected=choice == spec.value,
                )
                for choice in spec.choices
            )
            + "</select>"
        )
    elif spec.multiline:
        control = (
            f'<textarea name="{escape(_env_input_name(spec.env_name))}">'
            f"{escape(spec.value)}</textarea>"
        )
    else:
        control = (
            f'<input name="{escape(_env_input_name(spec.env_name))}" '
            f'value="{escape(spec.value)}" />'
        )
    return f"""
    <label class="setting-field">
      {meta}
      {control}
      {_choice_help(spec.env_name)}
    </label>
    """


def _field_meta(spec: SettingFieldSpec) -> str:
    description = (
        f'<span class="field-desc">{escape(spec.description)}</span>'
        if spec.description
        else ""
    )
    return (
        f'<span class="field-title">{escape(spec.title)}</span>'
        f'<code>{escape(spec.env_name)}</code>'
        f"{description}"
    )


def _choice_help(env_name: str) -> str:
    if env_name == "FCGO_ENV":
        return (
            '<ul class="choice-help">'
            "<li><strong>dev</strong>：本地开发，适合调试。</li>"
            "<li><strong>test</strong>：自动测试，输出和调用限制更保守。</li>"
            "<li><strong>prod</strong>：正式部署，按生产配置运行。</li>"
            "</ul>"
        )
    if env_name == "FCGO_AGENT_MODE":
        return (
            '<ul class="choice-help">'
            "<li><strong>legacy</strong>：旧版解析链路。</li>"
            "<li><strong>agent</strong>：Agent + Tools 链路。</li>"
            "</ul>"
        )
    if env_name == "FCGO_WRITEBACK_CONFIRMATION_MODE":
        return (
            '<ul class="choice-help">'
            "<li><strong>每次确认 (always)</strong>：所有写入都先生成确认卡。</li>"
            "<li><strong>低风险自动执行 (low_risk_direct)</strong>：仅明确文档开头或末尾追加可自动执行。</li>"
            "<li><strong>仅生成草稿 (draft_only)</strong>：仅生成草稿，不执行写入。</li>"
            "</ul>"
        )
    return ""


def _setting_choice_label(env_name: str, value: str) -> str:
    return SETTING_CHOICE_LABELS.get(env_name, {}).get(value, value)


def _menu_sections() -> str:
    sections = []
    for group, items in MENU_GROUPS:
        rows = "\n".join(
            "<tr>"
            f"<td>{escape(group)}</td>"
            f"<td><code>{escape(key)}</code></td>"
            f"<td>{escape(label)}</td>"
            "</tr>"
            for key, label in items
        )
        sections.append(rows)
    return (
        '<table class="menu-table">'
        "<thead><tr><th>分类</th><th>事件 key</th><th>功能</th></tr></thead>"
        f"<tbody>{''.join(sections)}</tbody>"
        "</table>"
    )


def _option(value: str, label: str, *, selected: bool) -> str:
    selected_attr = " selected" if selected else ""
    return f'<option value="{escape(value)}"{selected_attr}>{escape(label)}</option>'


def _login_page(settings: Settings) -> str:
    return _layout(
        "登录本地配置后台",
        f"""
        <main class="center">
          <h1>本地配置后台</h1>
          <p>请使用飞书登录。首次登录的飞书用户会绑定为本机后台管理员。</p>
          <a class="button" href="/admin/login">使用飞书登录</a>
          <p class="hint">登录后只能修改本机配置；模型密钥不会在页面明文展示。</p>
          <p class="hint">当前服务地址：{escape(settings.base_url)}</p>
        </main>
        """,
    )


def _message_page(
    title: str,
    message: str,
    *,
    action_url: str = "",
    action_label: str = "",
) -> str:
    action = (
        f'<a class="button" href="{escape(action_url)}">{escape(action_label)}</a>'
        if action_url and action_label
        else ""
    )
    return _layout(
        title,
        f"""
        <main class="center">
          <h1>{escape(title)}</h1>
          <p>{escape(message)}</p>
          {action}
        </main>
        """,
    )


def _layout(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --primary: #3370ff;
      --primary-hover: #245bdb;
      --primary-soft: #eef4ff;
      --text: #1f2329;
      --muted: #646a73;
      --subtle: #8f959e;
      --border: rgba(31, 35, 41, 0.10);
      --border-strong: rgba(31, 35, 41, 0.16);
      --surface: rgba(255, 255, 255, 0.92);
      --surface-solid: #ffffff;
      --panel: #fbfcff;
      --page: #f5f7fb;
      --success: #2b8a3e;
      --warning: #b25f00;
      --danger: #d83931;
      --danger-soft: #fff2f0;
      --shadow: 0 16px 50px rgba(31, 35, 41, 0.08);
      --shadow-soft: 0 8px 24px rgba(31, 35, 41, 0.05);
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.92), rgba(245, 247, 251, 0.94)),
        repeating-linear-gradient(90deg, rgba(51, 112, 255, 0.035) 0 1px, transparent 1px 96px),
        repeating-linear-gradient(0deg, rgba(31, 35, 41, 0.025) 0 1px, transparent 1px 96px),
        var(--page);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
        "Microsoft YaHei", sans-serif;
    }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 24px;
      max-width: 1240px;
      margin: 0 auto;
      padding: 34px 24px 18px;
    }}
    .top-actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    h1 {{ margin: 0 0 8px; font-size: 30px; letter-spacing: 0; line-height: 1.2; }}
    h2 {{ margin: 0 0 18px; font-size: 19px; letter-spacing: 0; line-height: 1.35; }}
    p {{ margin: 0 0 16px; line-height: 1.7; color: var(--muted); }}
    code {{
      padding: 2px 6px;
      border-radius: 6px;
      background: var(--primary-soft);
      color: #245bdb;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
      max-width: 1240px;
      margin: 0 auto;
      padding: 12px 24px;
    }}
    article, .center {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      box-shadow: var(--shadow-soft);
      padding: 24px;
      box-sizing: border-box;
      backdrop-filter: blur(14px);
    }}
    .center {{
      width: min(520px, calc(100vw - 40px));
      margin: 10vh auto 0;
    }}
    dl {{
      display: grid;
      grid-template-columns: 110px 1fr;
      gap: 10px 16px;
      margin: 0;
    }}
    dt {{ color: var(--muted); }}
    dd {{ margin: 0; color: var(--text); word-break: break-word; }}
    label {{ display: block; margin-bottom: 16px; color: var(--text); }}
    .page-form {{
      max-width: 1240px;
      margin: 0 auto 48px;
    }}
    .toolbar {{
      position: sticky;
      top: 0;
      z-index: 2;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin: 0 24px 14px;
      padding: 14px 18px;
      border: 1px solid rgba(51, 112, 255, 0.20);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.88);
      backdrop-filter: blur(16px);
      box-shadow: 0 10px 34px rgba(31, 35, 41, 0.06);
    }}
    .toolbar p {{ margin: 0; }}
    .toolbar-actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .wide {{
      max-width: 1240px;
      margin: 0 auto;
      padding: 12px 24px;
      box-sizing: border-box;
    }}
    input, textarea, select {{
      width: 100%;
      margin-top: 6px;
      min-height: 44px;
      border: 1px solid var(--border-strong);
      border-radius: 8px;
      padding: 10px 13px;
      box-sizing: border-box;
      font: inherit;
      background: var(--surface-solid);
      color: var(--text);
      transition: border-color 140ms ease, box-shadow 140ms ease, background 140ms ease;
    }}
    input:focus, textarea:focus, select:focus {{
      outline: 0;
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(51, 112, 255, 0.12);
    }}
    textarea {{ min-height: 120px; resize: vertical; }}
    .memory-editor {{ min-height: 180px; }}
    select {{ cursor: pointer; }}
    details {{
      border-top: 1px solid #e5e7eb;
      padding: 16px 0;
    }}
    details:first-of-type {{ border-top: 0; }}
    summary {{
      cursor: pointer;
      font-weight: 700;
      color: var(--text);
      margin-bottom: 12px;
    }}
    .settings-list {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px 20px;
    }}
    .settings-list.one-column {{
      grid-template-columns: 1fr;
    }}
    .setting-field {{
      margin: 0;
    }}
    .field-title {{
      display: block;
      margin-bottom: 5px;
      color: var(--text);
      font-weight: 700;
    }}
    .field-desc {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .setting-field code {{
      display: inline-block;
      margin-bottom: 4px;
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .input-action-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
      margin-top: 8px;
    }}
    .input-action-row input {{
      margin-top: 0;
    }}
    .input-action-row .clear-button {{
      min-width: 72px;
      min-height: 44px;
      margin-top: 0;
      white-space: nowrap;
    }}
    .inline-check {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin-top: 8px;
      font-size: 13px;
      color: var(--muted);
    }}
    .inline-check input {{
      width: auto;
      margin: 0;
    }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .card-actions {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      justify-content: flex-end;
      margin-top: 22px;
      padding-top: 18px;
      border-top: 1px solid var(--border);
    }}
    button, .button, .ghost {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid var(--primary);
      border-radius: 8px;
      background: var(--primary);
      color: #fff;
      min-height: 40px;
      padding: 10px 16px;
      font: inherit;
      line-height: 1;
      text-decoration: none;
      cursor: pointer;
      transition: background 140ms ease, border-color 140ms ease, color 140ms ease,
        box-shadow 140ms ease, transform 140ms ease;
    }}
    button:hover, .button:hover {{
      background: var(--primary-hover);
      border-color: var(--primary-hover);
      box-shadow: 0 8px 20px rgba(51, 112, 255, 0.18);
    }}
    .secondary, .ghost {{
      background: #fff;
      color: var(--primary);
    }}
    .secondary:hover, .ghost:hover {{
      background: #f2f6ff;
      border-color: var(--primary);
      color: var(--primary);
    }}
    .small {{
      padding: 8px 10px;
      font-size: 14px;
    }}
    .danger {{
      border-color: var(--danger);
      color: var(--danger);
      background: var(--danger-soft);
    }}
    .danger:hover {{
      border-color: var(--danger);
      color: var(--danger);
      background: #fff0ee;
      box-shadow: 0 8px 20px rgba(216, 57, 49, 0.12);
    }}
    .badge {{
      display: inline-block;
      margin-left: 6px;
      padding: 2px 6px;
      border-radius: 999px;
      background: #e8f0ff;
      color: #245bdb;
      font-size: 12px;
    }}
    .status-list {{ margin: 0; padding-left: 18px; line-height: 1.9; }}
    .ok {{ color: var(--success); }}
    .missing {{ color: var(--warning); }}
    .error {{ color: var(--danger); }}
    .help {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .choice-help {{
      margin: 8px 0 0;
      padding-left: 18px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.6;
    }}
    .hint {{ margin-top: 14px; color: var(--muted); font-size: 14px; }}
    .setup-status-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }}
    .setup-status-card {{
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px;
      background: linear-gradient(180deg, #ffffff, var(--panel));
    }}
    .setup-status-card strong {{
      display: block;
      margin-bottom: 8px;
    }}
    .setup-status-card p {{
      margin: 8px 0 0;
      font-size: 13px;
      line-height: 1.6;
    }}
    .setup-copy {{
      display: grid;
      gap: 8px;
      margin-top: 14px;
      padding: 12px;
      border: 1px dashed var(--border-strong);
      border-radius: 10px;
      background: #fbfcff;
    }}
    .setup-copy span {{
      color: var(--muted);
      font-size: 13px;
    }}
    .setup-copy code {{
      overflow-wrap: anywhere;
    }}
    .setup-checklist {{
      margin: 0;
      padding-left: 20px;
      color: var(--text);
      line-height: 1.9;
    }}
    .compact-table th {{
      width: 150px;
    }}
    .provider-cards {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
      margin: 4px 0 18px;
    }}
    .provider-cards.compact {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .provider-card {{
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px;
      background: linear-gradient(180deg, #ffffff, var(--panel));
      box-shadow: 0 4px 16px rgba(31, 35, 41, 0.035);
    }}
    .provider-card strong {{
      display: block;
      margin-bottom: 8px;
      font-size: 16px;
    }}
    .provider-kind {{
      display: inline-block;
      margin-right: 8px;
      color: var(--muted);
      font-size: 13px;
    }}
    .provider-card p {{
      margin: 8px 0 0;
      color: var(--muted);
      word-break: break-word;
    }}
    .inline-actions {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin-top: 12px;
      flex-wrap: wrap;
    }}
    .test-result {{
      font-size: 13px;
      line-height: 1.5;
      min-height: 20px;
    }}
    .test-result.pending {{
      color: var(--muted);
    }}
    .panel-details {{
      border-top: 1px solid var(--border);
      margin-top: 8px;
    }}
    .provider-configs {{
      display: grid;
      gap: 10px;
    }}
    .provider-panel {{
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px;
      background: var(--surface-solid);
    }}
    .provider-panel[hidden] {{
      display: none;
    }}
    .provider-panel h4 {{
      margin: 0 0 8px;
      font-size: 16px;
    }}
    .provider-panel summary {{
      margin-bottom: 0;
    }}
    .provider-panel[open] summary {{
      margin-bottom: 12px;
    }}
    .banner {{
      margin: 0 24px 12px;
      padding: 12px 14px;
      border: 1px solid #b7ebc6;
      border-radius: 10px;
      background: #f0fff4;
      color: #1f7a35;
    }}
    dialog {{
      width: min(820px, calc(100vw - 32px));
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 24px;
      box-shadow: 0 28px 82px rgba(15, 23, 42, 0.24);
    }}
    dialog::backdrop {{
      background: rgba(15, 23, 42, 0.35);
    }}
    .dialog-head {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
      margin-bottom: 10px;
    }}
    .dialog-head h3 {{
      margin: 0;
      font-size: 20px;
    }}
    .menu-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
      background: var(--surface-solid);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
    }}
    .menu-table th,
    .menu-table td {{
      padding: 10px 8px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
    }}
    .menu-table th {{
      color: var(--muted);
      font-weight: 700;
    }}
    @media (max-width: 760px) {{
      .topbar {{ display: block; }}
      .grid {{ grid-template-columns: 1fr; }}
      dl {{ grid-template-columns: 1fr; }}
      .toolbar {{ display: block; }}
      .toolbar-actions {{ margin-top: 10px; display: grid; grid-template-columns: 1fr; }}
      .toolbar button {{ width: 100%; }}
      .settings-list {{ grid-template-columns: 1fr; }}
      .input-action-row {{ grid-template-columns: 1fr; }}
      .input-action-row .clear-button {{ width: 100%; }}
      .setup-status-grid {{ grid-template-columns: 1fr; }}
      .provider-cards,
      .provider-cards.compact {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
{body}
</body>
</html>"""


def _provider_status(label: str, secret: str) -> str:
    if secret:
        return f'<li><span class="ok">{escape(label)}：已配置</span></li>'
    return f'<li><span class="missing">{escape(label)}：未配置</span></li>'


def _mode_label(mode: WritebackConfirmationMode) -> str:
    labels = {
        WritebackConfirmationMode.ALWAYS: "每次确认",
        WritebackConfirmationMode.LOW_RISK_DIRECT: "低风险自动执行",
        WritebackConfirmationMode.DRAFT_ONLY: "仅生成草稿",
    }
    return labels[mode]


def _clean_short_text(value: str, *, max_chars: int) -> str:
    cleaned = " ".join(value.replace("\r", "\n").split()) if "\n" not in value else value.strip()
    return cleaned[:max_chars].strip()


def _clean_memory_text(value: str) -> str:
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    cleaned = "\n".join(lines).strip()
    return cleaned[:12000].strip()


def _memory_items_text(items: Sequence[object]) -> str:
    contents = []
    for item in items:
        content = getattr(item, "content", "")
        if isinstance(content, str) and content.strip():
            contents.append(content.strip())
    return "\n\n".join(contents).strip()


def _clean_identifier(value: str) -> str:
    cleaned = "".join(char for char in value.strip().lower() if char.isalnum() or char in "-_")
    return cleaned[:40]
