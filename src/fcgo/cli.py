import argparse
import asyncio
import logging
import threading
from typing import Any

import uvicorn
from google import genai
from google.genai import types

from fcgo.agent import Assistant
from fcgo.config import Settings, get_settings
from fcgo.feishu.client import FeishuClient
from fcgo.feishu.oauth import FeishuOAuthService
from fcgo.feishu.openapi import FeishuOpenAPI
from fcgo.feishu.router import FeishuMessageRouter
from fcgo.feishu.worker import FeishuLongConnectionWorker
from fcgo.logging import configure_logging
from fcgo.model_providers.registry import ModelRouter, build_model_router
from fcgo.resources.reader import CompositeResourceReader, FeishuResourceReader, WebResourceReader
from fcgo.storage import SQLiteStore
from fcgo.writeback.executor import FeishuWriteExecutor
from fcgo.writeback.service import WritebackService

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(prog="fcgo")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="Run FastAPI server and optional Feishu worker")
    subparsers.add_parser("worker", help="Run only the Feishu long-connection worker")
    subparsers.add_parser("doctor", help="Validate local config and external credentials")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)

    if args.command == "serve":
        if settings.start_long_connection:
            thread = threading.Thread(target=_run_worker, daemon=True)
            thread.start()
        uvicorn.run(
            "fcgo.server:create_app",
            factory=True,
            host=settings.host,
            port=settings.port,
            reload=settings.env == "dev" and not settings.start_long_connection,
        )
    elif args.command == "worker":
        _run_worker()
    elif args.command == "doctor":
        ok = asyncio.run(_doctor())
        raise SystemExit(0 if ok else 1)


def _run_worker() -> None:
    settings = get_settings()
    store = SQLiteStore(settings.sqlite_path)
    asyncio.run(store.init())
    model = _build_model_provider(settings)
    openapi = FeishuOpenAPI(settings, store)
    assistant = Assistant(
        model,
        CompositeResourceReader(
            FeishuResourceReader(settings, openapi),
            WebResourceReader(settings),
        ),
        pending_action_ttl_seconds=settings.pending_action_ttl_seconds,
    )
    feishu_client = FeishuClient(settings)
    oauth = FeishuOAuthService(settings, store)
    router = FeishuMessageRouter(assistant, feishu_client, store, oauth, model, settings)
    writeback = WritebackService(store, FeishuWriteExecutor(openapi, feishu_client), settings)
    logger.info("starting_feishu_long_connection_worker")
    FeishuLongConnectionWorker(settings, router, writeback).run_forever()


async def _doctor() -> bool:
    settings = get_settings()
    store = SQLiteStore(settings.sqlite_path)
    await store.init()
    checks: list[tuple[str, bool, str]] = []

    checks.append(("Feishu App ID", bool(settings.feishu_app_id), "FEISHU_APP_ID is empty"))
    checks.append(
        (
            "Feishu App Secret",
            bool(settings.feishu_app_secret.get_secret_value()),
            "FEISHU_APP_SECRET is empty",
        )
    )
    model_router_ok = False
    try:
        router = build_model_router(settings)
        model_router_ok = True
        checks.append(
            (
                "Model provider router",
                True,
                (
                    f"default={router.default_provider}/"
                    f"{router.default_model or 'provider-default'}; "
                    f"available={','.join(router.registry.names())}"
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(("Model provider router", False, str(exc)))

    gemini_required = settings.default_provider.strip().lower() == "gemini"
    gemini_configured = bool(settings.gemini_api_key.get_secret_value())
    if gemini_required or gemini_configured:
        checks.append(
            (
                "Gemini API Key",
                gemini_configured,
                "GEMINI_API_KEY is empty",
            )
        )

    feishu_ok = False
    if checks[0][1] and checks[1][1]:
        try:
            token = await FeishuOpenAPI(settings, store)._tenant_access_token()
            feishu_ok = bool(token)
            checks.append(("Feishu tenant token", feishu_ok, "tenant token request failed"))
        except Exception as exc:  # noqa: BLE001
            checks.append(("Feishu tenant token", False, str(exc)))

    gemini_ok = False
    should_check_gemini = (
        model_router_ok
        and gemini_configured
        and settings.default_provider.strip().lower() == "gemini"
    )
    if should_check_gemini:
        try:
            text = await _check_gemini_lightweight(settings)
            gemini_ok = bool(text.strip())
            checks.append(("Gemini generate_content", gemini_ok, "empty response"))
        except Exception as exc:  # noqa: BLE001
            checks.append(("Gemini generate_content", False, str(exc)))

    for name, passed, detail in checks:
        marker = "OK" if passed else "FAIL"
        print(f"[{marker}] {name}")
        if not passed:
            print(f"      {detail}")
    print(f"[INFO] FCGO_START_LONG_CONNECTION={settings.start_long_connection}")
    return all(passed for _, passed, _ in checks)


async def _check_gemini_lightweight(settings: Settings) -> str:
    http_options: dict[str, Any] = {}
    if settings.gemini_base_url:
        http_options["base_url"] = settings.gemini_base_url
    if settings.gemini_http_proxy:
        http_options["client_args"] = {"proxy": settings.gemini_http_proxy}
    client = genai.Client(
        api_key=settings.gemini_api_key.get_secret_value(),
        http_options=types.HttpOptions(**http_options) if http_options else None,
    )
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=settings.default_model or settings.gemini_model,
        contents="Reply exactly: ok",
        config=types.GenerateContentConfig(
            max_output_tokens=32,
            temperature=0,
            thinking_config=types.ThinkingConfig(
                thinking_budget=0,
                include_thoughts=False,
            ),
        ),
    )
    return response.text or ""


def _build_model_provider(settings: Settings) -> ModelRouter:
    return build_model_router(settings)


if __name__ == "__main__":
    main()
