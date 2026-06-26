import json
from collections.abc import Mapping
from typing import Any

from fcgo.models import ConversationType, FeishuMention, FeishuMessage


def parse_text_message(
    event: Any,
    *,
    bot_open_id: str = "",
    bot_name: str = "",
) -> FeishuMessage | None:
    """Parse lark-oapi message event objects or dict payloads into FeishuMessage."""

    payload = _to_plain(event)
    if not isinstance(payload, dict):
        return None

    event_obj = _get(payload, "event", payload)
    message = _get(event_obj, "message", {})
    sender = _get(event_obj, "sender", {})
    message_type = _get(message, "message_type") or _get(message, "msg_type")
    if message_type not in {"text", "post"}:
        return None

    content = _get(message, "content") or "{}"
    try:
        content_obj = json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError:
        content_obj = {"text": str(content)}
    text = _message_text(content_obj)
    chat_type = _get(message, "chat_type") or _get(message, "conversation_type")
    if str(chat_type).lower() in {"group", "p2p_group"}:
        conversation_type = ConversationType.GROUP
    else:
        conversation_type = ConversationType.PRIVATE
    mentions = _parse_mentions(_get(message, "mentions", []))
    normalized_text = _strip_mentions(text, mentions)
    thread_id = _get(message, "thread_id") or None
    root_id = _get(message, "root_id") or None
    parent_id = _get(message, "parent_id") or None
    chat_id = _get(message, "chat_id", "")
    sender_id_obj = _get(sender, "sender_id", {})
    sender_id = (
        _get(sender_id_obj, "open_id")
        or _get(sender_id_obj, "user_id")
        or _get(sender, "open_id")
        or ""
    )
    return FeishuMessage(
        message_id=_get(message, "message_id", ""),
        chat_id=chat_id,
        sender_id=sender_id,
        text=normalized_text,
        conversation_type=conversation_type,
        conversation_key=_conversation_key(conversation_type, chat_id, thread_id),
        thread_id=thread_id,
        root_id=root_id,
        parent_id=parent_id,
        mentions=mentions,
        is_bot_mentioned=_is_bot_mentioned(
            conversation_type,
            mentions,
            bot_open_id=bot_open_id,
            bot_name=bot_name,
        ),
        raw=payload,
    )


def _parse_mentions(raw_mentions: Any) -> list[FeishuMention]:
    mentions: list[FeishuMention] = []
    if not isinstance(raw_mentions, list):
        return mentions
    for raw_mention in raw_mentions:
        mention_id = _get(raw_mention, "id", {})
        mentions.append(
            FeishuMention(
                key=_get(raw_mention, "key", "") or "",
                open_id=_get(mention_id, "open_id", "") or "",
                user_id=_get(mention_id, "user_id", "") or "",
                union_id=_get(mention_id, "union_id", "") or "",
                name=_get(raw_mention, "name", "") or "",
            )
        )
    return mentions


def _strip_mentions(text: str, mentions: list[FeishuMention]) -> str:
    cleaned = text
    for mention in mentions:
        if mention.key:
            cleaned = cleaned.replace(mention.key, " ")
        if mention.name:
            cleaned = cleaned.replace(f"@{mention.name}", " ")
    return " ".join(part for part in cleaned.strip().split() if not part.startswith("@_user_"))


def _message_text(content_obj: Any) -> str:
    parts: list[str] = []
    _collect_text_parts(content_obj, parts)
    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = part.strip()
        if text and text not in seen:
            seen.add(text)
            deduped.append(text)
    return " ".join(deduped)


def _collect_text_parts(value: Any, parts: list[str]) -> None:
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            parts.append(value)
        return
    if isinstance(value, list):
        for item in value:
            _collect_text_parts(item, parts)
        return
    if not isinstance(value, Mapping):
        return

    for key in ("text", "content", "href", "url", "link", "preview_url"):
        item = value.get(key)
        if isinstance(item, str) and item:
            parts.append(item)
    for key in ("elements", "children", "items", "tag_content"):
        _collect_text_parts(value.get(key), parts)
    for item in value.values():
        if isinstance(item, list | Mapping):
            _collect_text_parts(item, parts)


def _is_bot_mentioned(
    conversation_type: ConversationType,
    mentions: list[FeishuMention],
    *,
    bot_open_id: str,
    bot_name: str,
) -> bool:
    if conversation_type == ConversationType.PRIVATE:
        return True
    if not mentions:
        return False
    if bot_open_id:
        return any(mention.open_id == bot_open_id for mention in mentions)
    if bot_name:
        normalized_bot_name = bot_name.casefold()
        return any(mention.name.casefold() == normalized_bot_name for mention in mentions)
    # Feishu usually only delivers group text events to the bot when it is mentioned.
    return True


def _conversation_key(
    conversation_type: ConversationType,
    chat_id: str,
    thread_id: str | None,
) -> str:
    if conversation_type == ConversationType.PRIVATE:
        return f"private:{chat_id}"
    if thread_id:
        return f"group:{chat_id}:thread:{thread_id}"
    return f"group:{chat_id}"


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
