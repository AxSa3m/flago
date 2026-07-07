from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading
import webbrowser
from typing import TYPE_CHECKING, Any, cast

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from flago.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="flago")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="Run FastAPI server and optional Feishu worker")
    subparsers.add_parser("worker", help="Run only the Feishu long-connection worker")
    subparsers.add_parser("doctor", help="Validate local config and external credentials")
    service_parser = subparsers.add_parser("service", help="Control local FLAGO service")
    service_parser.add_argument(
        "action",
        choices=("start", "stop", "restart", "status"),
        help="local service action",
    )
    service_parser.add_argument(
        "--open-admin",
        action="store_true",
        help="open local admin page after start or restart",
    )
    package_parser = subparsers.add_parser("package", help="Build distributable FLAGO package")
    package_parser.add_argument("action", choices=("build",), help="package action")
    package_parser.add_argument(
        "--target",
        choices=("current", "all", "windows", "macos", "linux"),
        default="current",
        help="package target platform",
    )
    package_parser.add_argument(
        "--dist-dir",
        default=None,
        help="output directory; defaults to ./dist",
    )
    args = parser.parse_args()

    if args.command == "service":
        from flago.local_service import run_service_action

        result = run_service_action(args.action)
        if args.open_admin and args.action in {"start", "restart"} and result.get("running"):
            webbrowser.open(f"http://127.0.0.1:{result.get('port', 8000)}/admin")
        print(json.dumps(result, ensure_ascii=False))
        return

    if args.command == "package":
        from pathlib import Path

        from flago.packaging import PackageTarget, build_portable_package, current_target

        targets: tuple[PackageTarget, ...]
        if args.target == "all":
            targets = ("windows", "macos", "linux")
        elif args.target == "current":
            targets = (current_target(),)
        else:
            targets = (cast(PackageTarget, args.target),)
        results = [
            build_portable_package(
                target=target,
                dist_dir=Path(args.dist_dir) if args.dist_dir else None,
            )
            for target in targets
        ]
        print(json.dumps({"packages": results}, ensure_ascii=False))
        return

    from flago.config import get_settings
    from flago.logging import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level)

    if args.command == "serve":
        if settings.start_long_connection:
            thread = threading.Thread(target=_run_worker, daemon=True)
            thread.start()
        import uvicorn

        uvicorn.run(
            "flago.server:create_app",
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
    from flago.agent import AgentOrchestrator, Assistant, default_tool_registry
    from flago.agent.protocols import AssistantHandler
    from flago.agent.search_planner import ModelResourceSearchPlanner
    from flago.config import get_settings
    from flago.feishu.client import FeishuClient
    from flago.feishu.oauth import FeishuOAuthService
    from flago.feishu.openapi import FeishuOpenAPI
    from flago.feishu.router import FeishuMessageRouter
    from flago.feishu.worker import FeishuLongConnectionWorker
    from flago.resources.reader import (
        CompositeResourceReader,
        FeishuResourceReader,
        FeishuResourceSearcher,
        WebResourceReader,
    )
    from flago.storage import SQLiteStore
    from flago.writeback.executor import FeishuWriteExecutor
    from flago.writeback.service import WritebackService

    settings = get_settings()
    store = SQLiteStore(settings.sqlite_path)
    asyncio.run(store.init())
    model = _build_model_provider(settings, audit_recorder=store)
    openapi = FeishuOpenAPI(settings, store)
    resource_reader = CompositeResourceReader(
        FeishuResourceReader(settings, openapi, model_router=model),
        WebResourceReader(settings),
    )
    resource_searcher = FeishuResourceSearcher(settings, openapi)
    assistant: AssistantHandler
    if settings.agent_mode == "agent":
        assistant = AgentOrchestrator(
            model,
            resource_reader,
            resource_searcher,
            tool_registry=default_tool_registry(settings.agent_tool_timeout_seconds),
            audit_recorder=store,
            store=store,
            model_router=model,
            pending_action_ttl_seconds=settings.pending_action_ttl_seconds,
            enable_writeback=settings.writeback_enabled,
            writeback_confirmation_mode=settings.writeback_confirmation_mode,
            max_steps=settings.agent_max_steps,
        )
    else:
        assistant = Assistant(
            model,
            resource_reader,
            resource_searcher,
            resource_search_planner=ModelResourceSearchPlanner(model),
            audit_recorder=store,
            pending_action_ttl_seconds=settings.pending_action_ttl_seconds,
            enable_writeback=settings.writeback_enabled,
            resource_search_limit=settings.resource_search_result_limit,
            resource_search_read_limit=settings.resource_search_read_limit,
        )
    feishu_client = FeishuClient(settings)
    oauth = FeishuOAuthService(settings, store)
    writeback = (
        WritebackService(store, FeishuWriteExecutor(openapi, feishu_client), settings)
        if settings.writeback_enabled
        else None
    )
    router = FeishuMessageRouter(
        assistant,
        feishu_client,
        store,
        oauth,
        model,
        settings,
        chat_history_api=openapi,
        writeback_service=writeback,
        message_resource_api=openapi,
    )
    logger.info("starting_feishu_long_connection_worker")
    FeishuLongConnectionWorker(settings, router, writeback).run_forever()


async def _doctor() -> bool:
    from flago.config import get_settings
    from flago.feishu.openapi import FeishuOpenAPI
    from flago.model_providers.registry import build_model_router
    from flago.storage import SQLiteStore

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
    print(f"[INFO] FLAGO_START_LONG_CONNECTION={settings.start_long_connection}")
    return all(passed for _, passed, _ in checks)


async def _check_gemini_lightweight(settings: Settings) -> str:
    from google import genai
    from google.genai import types

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


def _build_model_provider(
    settings: Any,
    *,
    audit_recorder: Any | None = None,
) -> Any:
    from flago.model_providers.registry import build_model_router

    return build_model_router(settings, audit_recorder=audit_recorder)


if __name__ == "__main__":
    main()
