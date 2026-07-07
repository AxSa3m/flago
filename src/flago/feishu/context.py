import json
import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Protocol

from flago.config import Settings
from flago.feishu.message import _message_text as _extract_message_text
from flago.models import AuditEventType, ChatContextMessage, ConversationType, FeishuMessage
from flago.storage import SQLiteStore

CONVERSATION_MEMORY_KIND = "会话摘要"


class ChatHistoryAPI(Protocol):
    async def list_recent_messages(
        self,
        *,
        actor_id: str,
        container_id_type: str,
        container_id: str,
        start_time: datetime,
        end_time: datetime,
        page_size: int,
    ) -> dict[str, Any]: ...


class ChatContextLoader:
    def __init__(
        self,
        *,
        settings: Settings,
        store: SQLiteStore,
        api: ChatHistoryAPI,
    ) -> None:
        self.settings = settings
        self.store = store
        self.api = api

    async def load(self, message: FeishuMessage) -> tuple[list[ChatContextMessage], str, int]:
        scope = _conversation_context_scope(message)

        now = datetime.now(UTC)
        cache_cutoff = (now - timedelta(hours=self.settings.context_cache_ttl_hours)).isoformat()
        await self.store.prune_context_message_cache(older_than=cache_cutoff)
        since = now - timedelta(hours=self.settings.context_recent_time_window_hours)

        if await self._should_refresh_cache(scope, now, query=message.text):
            data = await self.api.list_recent_messages(
                actor_id=message.sender_id,
                container_id_type="thread" if message.thread_id else "chat",
                container_id=message.thread_id or message.chat_id,
                start_time=since,
                end_time=now,
                page_size=self.settings.context_recent_message_limit,
            )
            fetched = _parse_context_messages(data)
            fetched = [
                item
                for item in fetched
                if item.message_id != message.message_id
                and item.text.strip()
            ]
            await self.store.upsert_context_messages(scope=scope, messages=fetched)
            await self.store.audit(
                AuditEventType.CONTEXT_HISTORY_READ,
                actor_id=message.sender_id,
                detail={
                    "scope": scope,
                    "container_id_type": "thread" if message.thread_id else "chat",
                    "message_count": len(fetched),
                    "time_window_hours": self.settings.context_recent_time_window_hours,
                    "cached": False,
                },
            )

        cached = await self.store.list_context_messages(
            scope=scope,
            since=since.isoformat(),
            limit=self.settings.context_recent_message_limit,
        )
        selected, older = _select_recent_full_context(
            cached,
            max_messages=self.settings.context_inject_message_limit,
            max_chars=self.settings.context_max_chars,
        )
        summary = await self.store.get_conversation_summary(scope)
        if older:
            summary = _build_conversation_summary(
                previous_summary=summary,
                messages=older,
                max_chars=max(1000, self.settings.context_max_chars // 3),
            )
            await self.store.save_conversation_summary(
                scope=scope,
                summary=summary,
                source_message_count=len(cached),
            )
        await self._cache_current_message(message, scope)
        await self._maybe_save_conversation_memory(message, scope, summary)
        return selected, summary, len(older)

    async def _should_refresh_cache(self, scope: str, now: datetime, *, query: str) -> bool:
        if _is_context_dependent_query(query):
            return True
        latest_cached_at = await self.store.latest_context_cache_time(scope)
        if latest_cached_at is None:
            return True
        try:
            latest = datetime.fromisoformat(latest_cached_at)
        except ValueError:
            return True
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=UTC)
        refresh_after = timedelta(seconds=self.settings.context_cache_refresh_seconds)
        return now - latest >= refresh_after

    async def _maybe_save_conversation_memory(
        self,
        message: FeishuMessage,
        scope: str,
        summary: str,
    ) -> None:
        if message.conversation_type != ConversationType.PRIVATE:
            return
        if not summary.strip():
            return
        if not await self.store.is_memory_enabled(message.sender_id):
            return
        content = _conversation_memory_content(
            summary,
            max_chars=self.settings.memory_item_max_chars,
        )
        if not content:
            return
        await self.store.save_memory_item(
            id=_conversation_memory_id(message.sender_id, scope),
            subject_id=message.sender_id,
            kind=CONVERSATION_MEMORY_KIND,
            content=content,
            source=conversation_memory_source(scope),
        )

    async def _cache_current_message(self, message: FeishuMessage, scope: str) -> None:
        if not message.message_id or not message.text.strip():
            return
        await self.store.upsert_context_messages(
            scope=scope,
            messages=[
                ChatContextMessage(
                    message_id=message.message_id,
                    sender_id=message.sender_id,
                    text=message.text,
                    created_at=datetime.now(UTC).isoformat(),
                )
            ],
        )


def _parse_context_messages(data: dict[str, Any]) -> list[ChatContextMessage]:
    items = data.get("items")
    if not isinstance(items, list):
        items = data.get("messages")
    if not isinstance(items, list):
        return []
    messages: list[ChatContextMessage] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = _message_text(item)
        if not text.strip():
            continue
        messages.append(
            ChatContextMessage(
                message_id=str(item.get("message_id") or item.get("id") or ""),
                sender_id=_sender_id(item),
                text=text,
                created_at=_created_at(item),
            )
        )
    return [message for message in messages if message.message_id]


def _message_text(item: dict[str, Any]) -> str:
    body = item.get("body")
    content: Any = None
    if isinstance(body, dict):
        content = body.get("content")
    if content is None:
        content = item.get("content")
    try:
        content_obj = json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError:
        return str(content or "")
    if isinstance(content_obj, dict | list):
        return _extract_message_text(content_obj)
    return str(content_obj or "")


def _sender_id(item: dict[str, Any]) -> str:
    sender = item.get("sender")
    if isinstance(sender, dict):
        sender_id = sender.get("id") or sender.get("sender_id")
        if isinstance(sender_id, dict):
            return str(sender_id.get("open_id") or sender_id.get("user_id") or "")
        if sender_id:
            return str(sender_id)
    return str(item.get("sender_id") or "")


def _created_at(item: dict[str, Any]) -> str:
    raw = item.get("create_time") or item.get("created_at") or item.get("update_time")
    if raw is None:
        return datetime.now(UTC).isoformat()
    raw_text = str(raw)
    if raw_text.isdigit():
        value = int(raw_text)
        if value > 10_000_000_000:
            value = value // 1000
        return datetime.fromtimestamp(value, UTC).isoformat()
    return raw_text


def _select_recent_full_context(
    messages: list[ChatContextMessage],
    *,
    max_messages: int,
    max_chars: int,
) -> tuple[list[ChatContextMessage], list[ChatContextMessage]]:
    if not messages or max_messages <= 0 or max_chars <= 0:
        return [], messages
    selected_reversed: list[ChatContextMessage] = []
    used_chars = 0
    older_count = len(messages)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        cost = len(message.text) + len(message.sender_id) + len(message.created_at) + 8
        if selected_reversed and used_chars + cost > max_chars:
            break
        if cost > max_chars:
            text_budget = max(0, max_chars - len(message.sender_id) - len(message.created_at) - 16)
            selected_reversed.append(
                message.model_copy(update={"text": message.text[:text_budget] + "..."})
            )
            older_count = index
            break
        selected_reversed.append(message)
        used_chars += cost
        older_count = index
        if len(selected_reversed) >= max_messages:
            break
    selected_reversed.reverse()
    return selected_reversed, messages[:older_count]


def _build_conversation_summary(
    *,
    previous_summary: str,
    messages: list[ChatContextMessage],
    max_chars: int,
) -> str:
    lines: list[str] = []
    if previous_summary.strip():
        lines.extend(previous_summary.strip().splitlines())
    for message in messages:
        text = _compact_text(message.text)
        if text:
            lines.append(f"- [{message.created_at}] {message.sender_id or 'unknown'}: {text}")
    deduped = _dedupe_keep_last(lines)
    summary = "\n".join(deduped)
    if len(summary) <= max_chars:
        return summary
    clipped_reversed: list[str] = []
    used_chars = 0
    for line in reversed(deduped):
        cost = len(line) + 1
        if clipped_reversed and used_chars + cost > max_chars:
            break
        clipped_reversed.append(line)
        used_chars += cost
    clipped_reversed.reverse()
    return "\n".join(clipped_reversed)


def _compact_text(text: str, *, limit: int = 240) -> str:
    compacted = re.sub(r"\s+", " ", text).strip()
    if len(compacted) <= limit:
        return compacted
    return compacted[: limit - 3] + "..."


def _dedupe_keep_last(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped_reversed: list[str] = []
    for line in reversed(lines):
        if line in seen:
            continue
        seen.add(line)
        deduped_reversed.append(line)
    deduped_reversed.reverse()
    return deduped_reversed


def conversation_memory_source(scope: str) -> str:
    return f"flago.context.summary:{scope}"


def _conversation_memory_id(subject_id: str, scope: str) -> str:
    digest = sha256(f"{subject_id}:{scope}".encode()).hexdigest()[:16]
    return f"conversation-summary-{digest}"


def _conversation_memory_content(summary: str, *, max_chars: int) -> str:
    lines: list[str] = []
    for line in summary.splitlines():
        cleaned = _strip_summary_metadata(line)
        if cleaned:
            lines.append(cleaned)
    compact = "\n".join(_dedupe_keep_last(lines))
    if len(compact) <= max_chars:
        return compact
    clipped: list[str] = []
    used_chars = 0
    for line in reversed(compact.splitlines()):
        cost = len(line) + 1
        if clipped and used_chars + cost > max_chars:
            break
        clipped.append(line)
        used_chars += cost
    clipped.reverse()
    return "\n".join(clipped)


def _strip_summary_metadata(line: str) -> str:
    cleaned = re.sub(r"\s+", " ", line).strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"^-\s*\[[^\]]+\]\s*[^:：]{0,80}[:：]\s*", "- ", cleaned)
    cleaned = re.sub(r"^-\s*[^:：]{1,40}[:：]\s*", "- ", cleaned)
    if not cleaned.startswith("- "):
        cleaned = f"- {cleaned}"
    return cleaned


def _is_context_dependent_query(text: str) -> bool:
    normalized = text.strip().casefold()
    if not normalized:
        return False
    markers = (
        "刚才",
        "上文",
        "前面",
        "之前",
        "刚刚",
        "上一条",
        "前一条",
        "上面",
        "这篇",
        "这份",
        "这个文档",
        "该文档",
        "这篇文档",
        "这篇文章",
        "那个文档",
        "这条",
        "这一个",
        "它",
        "发链接",
        "链接给我",
        "给我链接",
        "测试关键词",
        "记得",
        "我说的",
        "what did i say",
        "previous",
        "earlier",
        "写在开头",
        "写到开头",
        "写在结尾",
        "写到结尾",
        "写在末尾",
        "写到末尾",
    )
    return any(marker in normalized for marker in markers)


def _conversation_context_scope(message: FeishuMessage) -> str:
    return f"conversation:{message.conversation_key or message.chat_id}"
