from dataclasses import dataclass

from flago.config import Settings


@dataclass(frozen=True)
class ModelCatalogItem:
    provider: str
    model: str
    configured: bool
    missing_fields: tuple[str, ...] = ()

    @property
    def spec(self) -> str:
        return f"{self.provider}/{self.model or '未设置模型'}"


def build_model_catalog(settings: Settings) -> list[ModelCatalogItem]:
    items = [
        _item(
            provider="gemini",
            model=settings.gemini_model,
            required={
                "GEMINI_API_KEY": settings.gemini_api_key.get_secret_value(),
                "GEMINI_MODEL": settings.gemini_model,
            },
        ),
        _item(
            provider="openai",
            model=settings.openai_model,
            required={
                "OPENAI_API_KEY": settings.openai_api_key.get_secret_value(),
                "OPENAI_BASE_URL": settings.openai_base_url,
                "OPENAI_MODEL": settings.openai_model,
            },
        ),
        _item(
            provider="deepseek",
            model=settings.deepseek_model,
            required={
                "DEEPSEEK_API_KEY": settings.deepseek_api_key.get_secret_value(),
                "DEEPSEEK_BASE_URL": settings.deepseek_base_url,
                "DEEPSEEK_MODEL": settings.deepseek_model,
            },
        ),
        _item(
            provider="qwen",
            model=settings.qwen_model,
            required={
                "QWEN_API_KEY": settings.qwen_api_key.get_secret_value(),
                "QWEN_BASE_URL": settings.qwen_base_url,
                "QWEN_MODEL": settings.qwen_model,
            },
        ),
        _item(
            provider="doubao",
            model=settings.doubao_model,
            required={
                "DOUBAO_API_KEY": settings.doubao_api_key.get_secret_value(),
                "DOUBAO_BASE_URL": settings.doubao_base_url,
                "DOUBAO_MODEL": settings.doubao_model,
            },
        ),
        _item(
            provider="minimax",
            model=settings.minimax_model,
            required={
                "MINIMAX_API_KEY": settings.minimax_api_key.get_secret_value(),
                "MINIMAX_BASE_URL": settings.minimax_base_url,
                "MINIMAX_MODEL": settings.minimax_model,
            },
        ),
        _item(
            provider="claude",
            model=settings.anthropic_model,
            required={
                "ANTHROPIC_API_KEY": settings.anthropic_api_key.get_secret_value(),
                "ANTHROPIC_BASE_URL": settings.anthropic_base_url,
                "ANTHROPIC_MODEL": settings.anthropic_model,
                "ANTHROPIC_VERSION": settings.anthropic_version,
            },
        ),
        ModelCatalogItem(provider="echo", model="echo", configured=True),
    ]
    return items


def find_catalog_item(
    catalog: list[ModelCatalogItem],
    provider: str,
) -> ModelCatalogItem | None:
    normalized = provider.strip().lower()
    for item in catalog:
        if item.provider == normalized:
            return item
    return None


def _item(
    *,
    provider: str,
    model: str,
    required: dict[str, str],
) -> ModelCatalogItem:
    missing = tuple(key for key, value in required.items() if not value.strip())
    return ModelCatalogItem(
        provider=provider,
        model=model,
        configured=not missing,
        missing_fields=missing,
    )
