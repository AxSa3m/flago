import json
from collections.abc import Mapping
from typing import Any

from flago.models import FeishuBotMenuEvent


def parse_bot_menu_event(event: Any) -> FeishuBotMenuEvent | None:
    """Parse Lark bot menu event objects or dict payloads into a local model."""

    payload = _to_plain(event)
    if not isinstance(payload, dict):
        return None
    header = _get(payload, "header", {})
    event_obj = _get(payload, "event", payload)
    event_key = _get(event_obj, "event_key", "") or ""
    if not event_key:
        return None
    operator = _get(event_obj, "operator", {})
    operator_id = _get(operator, "operator_id", {})
    return FeishuBotMenuEvent(
        event_id=_get(header, "event_id", "") or "",
        event_key=event_key,
        operator_open_id=_get(operator_id, "open_id", "") or "",
        operator_user_id=_get(operator_id, "user_id", "") or "",
        operator_union_id=_get(operator_id, "union_id", "") or "",
        operator_name=_get(operator, "operator_name", "") or "",
        timestamp=_get(event_obj, "timestamp") or None,
        raw=payload,
    )


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _to_plain(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_to_plain(item) for item in value]
    if hasattr(value, "model_dump"):
        return _to_plain(value.model_dump(mode="json"))
    if hasattr(value, "to_dict"):
        return _to_plain(value.to_dict())
    if hasattr(value, "to_json"):
        try:
            return _to_plain(json.loads(value.to_json()))
        except (TypeError, json.JSONDecodeError):
            pass
    if hasattr(value, "__dict__"):
        return {
            key: _to_plain(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)
