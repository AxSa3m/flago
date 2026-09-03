import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any, Literal

from lark_oapi.core.utils import AESCipher
from pydantic import BaseModel, Field


class FeishuCardCallback(BaseModel):
    action_id: str
    actor_id: str
    action: Literal["confirm", "cancel"]
    action_key: str = ""
    event_id: str = ""
    value: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any]


class FeishuCardAuthenticationError(ValueError):
    pass


class FeishuCardAuthenticationNotConfiguredError(
    FeishuCardAuthenticationError
):
    pass


def authenticate_card_callback(
    raw_body: bytes,
    headers: Mapping[str, str],
    *,
    verification_token: str,
    encrypt_key: str = "",
) -> dict[str, Any]:
    token = verification_token.strip()
    if not token:
        raise FeishuCardAuthenticationNotConfiguredError(
            "Feishu card callback verification token is not configured"
        )

    payload = _decode_payload(raw_body, encrypt_key)
    if is_url_verification(payload):
        callback_token = str(payload.get("token") or "")
        if not hmac.compare_digest(token, callback_token):
            raise FeishuCardAuthenticationError("invalid verification token")
        return payload

    timestamp = headers.get("X-Lark-Request-Timestamp", "")
    nonce = headers.get("X-Lark-Request-Nonce", "")
    signature = headers.get("X-Lark-Signature", "")
    if not timestamp or not nonce or not signature:
        raise FeishuCardAuthenticationError("missing callback signature")
    expected = hashlib.sha1(
        (timestamp + nonce + token).encode("utf-8") + raw_body
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise FeishuCardAuthenticationError("invalid callback signature")
    return payload


def parse_card_callback(payload: dict[str, Any]) -> FeishuCardCallback:
    value = _action_value(payload)
    action_id = str(value.get("action_id") or "").strip()
    raw_action = value.get("flago_action") or value.get("action") or ""
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
        action_key=str(raw_action),
        event_id=_event_id(payload),
        value=value,
        raw=payload,
    )


def is_url_verification(payload: dict[str, Any]) -> bool:
    return bool(payload.get("challenge")) and payload.get("type") == "url_verification"


def _decode_payload(raw_body: bytes, encrypt_key: str) -> dict[str, Any]:
    try:
        envelope = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid card callback JSON") from exc
    if not isinstance(envelope, dict):
        raise ValueError("invalid card callback payload")

    encrypted = envelope.get("encrypt")
    if not encrypted:
        return envelope
    if not encrypt_key:
        raise FeishuCardAuthenticationNotConfiguredError(
            "Feishu card callback encrypt key is not configured"
        )
    try:
        payload = json.loads(AESCipher(encrypt_key).decrypt_str(str(encrypted)))
    except Exception as exc:
        raise FeishuCardAuthenticationError("invalid encrypted callback") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid card callback payload")
    return payload


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
    if normalized in {
        "confirm",
        "writeback.confirm",
        "flago.writeback.confirm",
        "memory.delete.confirm",
        "flago.memory.delete.confirm",
        "memory.save.confirm",
        "flago.memory.save.confirm",
    }:
        return "confirm"
    if normalized in {
        "cancel",
        "writeback.cancel",
        "flago.writeback.cancel",
        "memory.delete.cancel",
        "flago.memory.delete.cancel",
        "memory.save.cancel",
        "flago.memory.save.cancel",
    }:
        return "cancel"
    return normalized
