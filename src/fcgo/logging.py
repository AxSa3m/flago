import logging
import re
from collections.abc import Mapping
from typing import Any

SECRET_KEYS = {
    "secret",
    "token",
    "api_key",
    "apikey",
    "password",
    "authorization",
    "cookie",
}

SECRET_PATTERN = re.compile(
    r"(?i)("
    r"sk-[a-z0-9_-]{8,}|"
    r"lin_api_[a-z0-9_-]+|"
    r"AIza[0-9A-Za-z_-]{20,}|"
    r"Bearer\s+[a-z0-9._-]+|"
    r"(access_token|refresh_token|api_key|app_secret|token)=([^&\s]+)"
    r")"
)


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "***REDACTED***" if _is_secret_key(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return SECRET_PATTERN.sub("***REDACTED***", value)
    return value


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SECRET_KEYS)
