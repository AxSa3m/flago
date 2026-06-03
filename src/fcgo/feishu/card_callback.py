from typing import Any, Literal

from pydantic import BaseModel


class FeishuCardCallback(BaseModel):
    action_id: str
    actor_id: str
    action: Literal["confirm", "cancel"]
    event_id: str = ""
    raw: dict[str, Any]


def parse_card_callback(payload: dict[str, Any]) -> FeishuCardCallback:
    value = _action_value(payload)
    action_id = str(value.get("action_id") or "").strip()
    raw_action = value.get("fcgo_action") or value.get("action") or ""
    normalized_action = _normalize_action(str(raw_action))
    actor_id = _actor_id(payload)
    if not action_id:
        raise ValueError("missing action_id")
    if normalized_action == "confirm":
        action: Literal["confirm", "cancel"] = "confirm"
    elif normalized_action == "cancel":
        action = "cancel"
    else:
        raise ValueError("unknown card action")
    if not actor_id:
        raise ValueError("missing actor_id")
    return FeishuCardCallback(
        action_id=action_id,
        actor_id=actor_id,
        action=action,
        event_id=_event_id(payload),
        raw=payload,
    )


def is_url_verification(payload: dict[str, Any]) -> bool:
    return bool(payload.get("challenge")) and payload.get("type") == "url_verification"


def _action_value(payload: dict[str, Any]) -> dict[str, Any]:
    if "action_id" in payload:
        return payload
    action = payload.get("action")
    if isinstance(action, dict):
        value = action.get("value")
        if isinstance(value, dict):
            return value
    elif isinstance(action, str):
        return payload
    event = payload.get("event")
    if isinstance(event, dict):
        event_action = event.get("action")
        if isinstance(event_action, dict):
            value = event_action.get("value")
            if isinstance(value, dict):
                return value
    return {}


def _actor_id(payload: dict[str, Any]) -> str:
    for key in ("actor_id", "open_id", "user_id", "union_id"):
        value = payload.get(key)
        if value:
            return str(value)
    event = payload.get("event")
    if isinstance(event, dict):
        operator = event.get("operator")
        if isinstance(operator, dict):
            for key in ("open_id", "user_id", "union_id"):
                value = operator.get(key)
                if value:
                    return str(value)
    operator = payload.get("operator")
    if isinstance(operator, dict):
        for key in ("open_id", "user_id", "union_id"):
            value = operator.get(key)
            if value:
                return str(value)
    return ""


def _event_id(payload: dict[str, Any]) -> str:
    for key in ("event_id", "uuid"):
        value = payload.get(key)
        if value:
            return str(value)
    header = payload.get("header")
    if isinstance(header, dict) and header.get("event_id"):
        return str(header["event_id"])
    event = payload.get("event")
    if isinstance(event, dict) and event.get("event_id"):
        return str(event["event_id"])
    return ""


def _normalize_action(action: str) -> str:
    normalized = action.strip().lower()
    if normalized in {"confirm", "writeback.confirm", "fcgo.writeback.confirm"}:
        return "confirm"
    if normalized in {"cancel", "writeback.cancel", "fcgo.writeback.cancel"}:
        return "cancel"
    return normalized
