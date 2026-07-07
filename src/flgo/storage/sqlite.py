import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import aiosqlite

from flgo.logging import redact
from flgo.models import (
    ActionProposal,
    AssistantNamePreference,
    AssistantProfilePreference,
    AuditEventType,
    ChatContextMessage,
    ContextPreference,
    MemoryItem,
    ModelPreference,
    PendingActionStatus,
    WritebackAutoPreference,
)


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    subject_id TEXT PRIMARY KEY,
                    token_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_actions (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    target_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    preview TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    confirmed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    actor_id TEXT,
                    action_id TEXT,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_preferences (
                    scope TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assistant_name_preferences (
                    subject_id TEXT PRIMARY KEY,
                    assistant_name TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assistant_profile_preferences (
                    subject_id TEXT PRIMARY KEY,
                    assistant_profile TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_preferences (
                    scope TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_preferences (
                    subject_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS writeback_auto_preferences (
                    subject_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_items (
                    id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_message_cache (
                    scope TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    cached_at TEXT NOT NULL,
                    PRIMARY KEY(scope, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_context_message_cache_scope_created
                    ON context_message_cache(scope, created_at);
                CREATE INDEX IF NOT EXISTS idx_context_message_cache_cached_at
                    ON context_message_cache(cached_at);
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    scope TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    source_message_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS writeback_executions (
                    action_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    target_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    undo_action_type TEXT,
                    undo_target_json TEXT,
                    undo_payload_json TEXT,
                    undo_preview TEXT,
                    executed_at TEXT NOT NULL,
                    reverted_at TEXT
                );
                """
            )
            await db.commit()

    async def save_oauth_token(self, subject_id: str, token: dict[str, Any]) -> None:
        now = _now_iso()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO oauth_tokens(subject_id, token_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(subject_id) DO UPDATE SET
                    token_json = excluded.token_json,
                    updated_at = excluded.updated_at
                """,
                (subject_id, json.dumps(token, ensure_ascii=False), now),
            )
            await db.commit()

    async def get_oauth_token(self, subject_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            row = await db.execute_fetchall(
                "SELECT token_json FROM oauth_tokens WHERE subject_id = ?",
                (subject_id,),
            )
        rows = list(row)
        if not rows:
            return None
        return cast(dict[str, Any], json.loads(rows[0][0]))

    async def save_oauth_state(
        self,
        *,
        state: str,
        subject_id: str,
        expires_at: datetime,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO oauth_states(state, subject_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (state, subject_id, _now_iso(), expires_at.isoformat()),
            )
            await db.commit()

    async def consume_oauth_state(self, state: str) -> str | None:
        now = _now_iso()
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                """
                SELECT subject_id, expires_at, consumed_at
                FROM oauth_states
                WHERE state = ?
                """,
                (state,),
            )
            rows_list = list(rows)
            if not rows_list:
                return None
            subject_id, expires_at, consumed_at = rows_list[0]
            if consumed_at or expires_at <= now:
                return None
            await db.execute(
                "UPDATE oauth_states SET consumed_at = ? WHERE state = ?",
                (now, state),
            )
            await db.commit()
            return str(subject_id)

    async def remember_idempotency_key(self, key: str, value: str) -> bool:
        try:
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    "INSERT INTO idempotency_keys(key, value, created_at) VALUES (?, ?, ?)",
                    (key, value, _now_iso()),
                )
                await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def get_app_setting(self, key: str) -> Any | None:
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                "SELECT value_json FROM app_settings WHERE key = ?",
                (key,),
            )
        rows_list = list(rows)
        if not rows_list:
            return None
        return json.loads(rows_list[0][0])

    async def set_app_setting(self, key: str, value: Any, *, updated_by: str) -> None:
        now = _now_iso()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO app_settings(key, value_json, updated_by, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (key, _json_dumps(value), updated_by, now),
            )
            await db.commit()

    async def save_pending_action(self, proposal: ActionProposal) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO pending_actions(
                    id, actor_id, action_type, target_json, payload_json, preview,
                    status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal.id,
                    proposal.actor_id,
                    proposal.action_type.value,
                    _json_dumps(proposal.target),
                    _json_dumps(proposal.payload),
                    proposal.preview,
                    PendingActionStatus.PENDING.value,
                    proposal.created_at.isoformat(),
                    proposal.expires_at.isoformat(),
                ),
            )
            await db.commit()
        await self.audit(
            AuditEventType.ACTION_CREATED,
            actor_id=proposal.actor_id,
            action_id=proposal.id,
            detail={"action_type": proposal.action_type.value},
        )

    async def get_pending_action(self, action_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                """
                SELECT id, actor_id, action_type, target_json, payload_json, preview,
                       status, created_at, expires_at, confirmed_at
                FROM pending_actions
                WHERE id = ?
                """,
                (action_id,),
            )
        rows_list = list(rows)
        if not rows_list:
            return None
        row = rows_list[0]
        return {
            "id": row[0],
            "actor_id": row[1],
            "action_type": row[2],
            "target": json.loads(row[3]),
            "payload": json.loads(row[4]),
            "preview": row[5],
            "status": row[6],
            "created_at": row[7],
            "expires_at": row[8],
            "confirmed_at": row[9],
        }

    async def find_recent_matching_action(
        self,
        proposal: ActionProposal,
        *,
        within_seconds: int,
        statuses: set[PendingActionStatus] | None = None,
    ) -> dict[str, Any] | None:
        since = (datetime.now(UTC) - timedelta(seconds=within_seconds)).isoformat()
        status_values = [
            status.value
            for status in (
                statuses
                or {PendingActionStatus.CONFIRMED, PendingActionStatus.EXECUTED}
            )
        ]
        placeholders = ",".join("?" for _ in status_values)
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                f"""
                SELECT id, actor_id, action_type, target_json, payload_json, preview,
                       status, created_at, expires_at, confirmed_at
                FROM pending_actions
                WHERE actor_id = ?
                  AND action_type = ?
                  AND target_json = ?
                  AND payload_json = ?
                  AND status IN ({placeholders})
                  AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (
                    proposal.actor_id,
                    proposal.action_type.value,
                    _json_dumps(proposal.target),
                    _json_dumps(proposal.payload),
                    *status_values,
                    since,
                ),
            )
        rows_list = list(rows)
        if not rows_list:
            return None
        row = rows_list[0]
        return {
            "id": row[0],
            "actor_id": row[1],
            "action_type": row[2],
            "target": json.loads(row[3]),
            "payload": json.loads(row[4]),
            "preview": row[5],
            "status": row[6],
            "created_at": row[7],
            "expires_at": row[8],
            "confirmed_at": row[9],
        }

    async def set_pending_action_status(
        self, action_id: str, status: PendingActionStatus
    ) -> None:
        confirmed_at = _now_iso() if status == PendingActionStatus.CONFIRMED else None
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE pending_actions
                SET status = ?, confirmed_at = COALESCE(?, confirmed_at)
                WHERE id = ?
                """,
                (status.value, confirmed_at, action_id),
            )
            await db.commit()

    async def expire_due_actions(self) -> int:
        now = _now_iso()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE pending_actions
                SET status = ?
                WHERE status = ? AND expires_at <= ?
                """,
                (PendingActionStatus.EXPIRED.value, PendingActionStatus.PENDING.value, now),
            )
            await db.commit()
            return cursor.rowcount

    async def audit(
        self,
        event_type: AuditEventType,
        *,
        actor_id: str | None = None,
        action_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO audit_events(event_type, actor_id, action_id, detail_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event_type.value,
                    actor_id,
                    action_id,
                    _json_dumps(redact(detail or {})),
                    _now_iso(),
                ),
            )
            await db.commit()

    async def save_model_preference(
        self,
        *,
        scope: str,
        provider: str,
        model: str | None,
        updated_by: str,
    ) -> None:
        now = _now_iso()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO model_preferences(scope, provider, model, updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope) DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (scope, provider, model, updated_by, now),
            )
            await db.commit()
        await self.audit(
            AuditEventType.MODEL_PREFERENCE_SET,
            actor_id=updated_by,
            detail={"scope": scope, "provider": provider, "model": model},
        )

    async def get_model_preference(self, scope: str) -> ModelPreference | None:
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                """
                SELECT scope, provider, model, updated_by, updated_at
                FROM model_preferences
                WHERE scope = ?
                """,
                (scope,),
            )
        rows_list = list(rows)
        if not rows_list:
            return None
        row = rows_list[0]
        return ModelPreference(
            scope=str(row[0]),
            provider=str(row[1]),
            model=str(row[2]) if row[2] is not None else None,
            updated_by=str(row[3]),
            updated_at=str(row[4]),
        )

    async def clear_model_preference(self, scope: str, *, updated_by: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM model_preferences WHERE scope = ?", (scope,))
            await db.commit()
        await self.audit(
            AuditEventType.MODEL_PREFERENCE_CLEARED,
            actor_id=updated_by,
            detail={"scope": scope},
        )

    async def save_assistant_name_preference(
        self,
        *,
        subject_id: str,
        assistant_name: str,
        updated_by: str,
    ) -> None:
        now = _now_iso()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO assistant_name_preferences(
                    subject_id, assistant_name, updated_by, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(subject_id) DO UPDATE SET
                    assistant_name = excluded.assistant_name,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (subject_id, assistant_name, updated_by, now),
            )
            await db.commit()
        await self.audit(
            AuditEventType.ASSISTANT_NAME_SET,
            actor_id=updated_by,
            detail={"subject_id": subject_id, "assistant_name_length": len(assistant_name)},
        )

    async def get_assistant_name_preference(
        self,
        subject_id: str,
    ) -> AssistantNamePreference | None:
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                """
                SELECT subject_id, assistant_name, updated_by, updated_at
                FROM assistant_name_preferences
                WHERE subject_id = ?
                """,
                (subject_id,),
            )
        rows_list = list(rows)
        if not rows_list:
            return None
        row = rows_list[0]
        return AssistantNamePreference(
            subject_id=str(row[0]),
            assistant_name=str(row[1]),
            updated_by=str(row[2]),
            updated_at=str(row[3]),
        )

    async def clear_assistant_name_preference(
        self,
        subject_id: str,
        *,
        updated_by: str,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM assistant_name_preferences WHERE subject_id = ?",
                (subject_id,),
            )
            await db.commit()
        await self.audit(
            AuditEventType.ASSISTANT_NAME_CLEARED,
            actor_id=updated_by,
            detail={"subject_id": subject_id},
        )

    async def save_assistant_profile_preference(
        self,
        *,
        subject_id: str,
        assistant_profile: str,
        updated_by: str,
    ) -> None:
        now = _now_iso()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO assistant_profile_preferences(
                    subject_id, assistant_profile, updated_by, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(subject_id) DO UPDATE SET
                    assistant_profile = excluded.assistant_profile,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (subject_id, assistant_profile, updated_by, now),
            )
            await db.commit()
        await self.audit(
            AuditEventType.ASSISTANT_PROFILE_SET,
            actor_id=updated_by,
            detail={
                "subject_id": subject_id,
                "assistant_profile_length": len(assistant_profile),
            },
        )

    async def get_assistant_profile_preference(
        self,
        subject_id: str,
    ) -> AssistantProfilePreference | None:
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                """
                SELECT subject_id, assistant_profile, updated_by, updated_at
                FROM assistant_profile_preferences
                WHERE subject_id = ?
                """,
                (subject_id,),
            )
        rows_list = list(rows)
        if not rows_list:
            return None
        row = rows_list[0]
        return AssistantProfilePreference(
            subject_id=str(row[0]),
            assistant_profile=str(row[1]),
            updated_by=str(row[2]),
            updated_at=str(row[3]),
        )

    async def clear_assistant_profile_preference(
        self,
        subject_id: str,
        *,
        updated_by: str,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM assistant_profile_preferences WHERE subject_id = ?",
                (subject_id,),
            )
            await db.commit()
        await self.audit(
            AuditEventType.ASSISTANT_PROFILE_CLEARED,
            actor_id=updated_by,
            detail={"subject_id": subject_id},
        )

    async def set_context_preference(
        self,
        *,
        scope: str,
        enabled: bool,
        updated_by: str,
    ) -> None:
        now = _now_iso()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO context_preferences(scope, enabled, updated_by, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (scope, int(enabled), updated_by, now),
            )
            await db.commit()
        await self.audit(
            AuditEventType.CONTEXT_ENABLED if enabled else AuditEventType.CONTEXT_DISABLED,
            actor_id=updated_by,
            detail={"scope": scope},
        )

    async def get_context_preference(self, scope: str) -> ContextPreference | None:
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                """
                SELECT scope, enabled, updated_by, updated_at
                FROM context_preferences
                WHERE scope = ?
                """,
                (scope,),
            )
        rows_list = list(rows)
        if not rows_list:
            return None
        row = rows_list[0]
        return ContextPreference(
            scope=str(row[0]),
            enabled=bool(row[1]),
            updated_by=str(row[2]),
            updated_at=str(row[3]),
        )

    async def upsert_context_messages(
        self,
        *,
        scope: str,
        messages: list[ChatContextMessage],
    ) -> None:
        if not messages:
            return
        now = _now_iso()
        async with aiosqlite.connect(self.path) as db:
            await db.executemany(
                """
                INSERT INTO context_message_cache(
                    scope, message_id, sender_id, text, created_at, cached_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, message_id) DO UPDATE SET
                    sender_id = excluded.sender_id,
                    text = excluded.text,
                    created_at = excluded.created_at,
                    cached_at = excluded.cached_at
                """,
                [
                    (
                        scope,
                        message.message_id,
                        message.sender_id,
                        message.text,
                        message.created_at,
                        now,
                    )
                    for message in messages
                ],
            )
            await db.commit()

    async def list_context_messages(
        self,
        *,
        scope: str,
        since: str,
        limit: int,
    ) -> list[ChatContextMessage]:
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                """
                SELECT message_id, sender_id, text, created_at
                FROM context_message_cache
                WHERE scope = ? AND created_at >= ?
                ORDER BY created_at DESC, message_id DESC
                LIMIT ?
                """,
                (scope, since, limit),
            )
        messages = [
            ChatContextMessage(
                message_id=str(row[0]),
                sender_id=str(row[1]),
                text=str(row[2]),
                created_at=str(row[3]),
            )
            for row in rows
        ]
        return list(reversed(messages))

    async def latest_context_cache_time(self, scope: str) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                "SELECT MAX(cached_at) FROM context_message_cache WHERE scope = ?",
                (scope,),
            )
        rows_list = list(rows)
        if not rows_list or rows_list[0][0] is None:
            return None
        return str(rows_list[0][0])

    async def prune_context_message_cache(self, *, older_than: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "DELETE FROM context_message_cache WHERE cached_at < ?",
                (older_than,),
            )
            await db.commit()
            return cursor.rowcount

    async def save_conversation_summary(
        self,
        *,
        scope: str,
        summary: str,
        source_message_count: int,
    ) -> None:
        now = _now_iso()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO conversation_summaries(
                    scope, summary, source_message_count, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope) DO UPDATE SET
                    summary = excluded.summary,
                    source_message_count = excluded.source_message_count,
                    updated_at = excluded.updated_at
                """,
                (scope, summary, source_message_count, now),
            )
            await db.commit()

    async def get_conversation_summary(self, scope: str) -> str:
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                "SELECT summary FROM conversation_summaries WHERE scope = ?",
                (scope,),
            )
        rows_list = list(rows)
        if not rows_list:
            return ""
        return str(rows_list[0][0])

    async def save_memory_item(
        self,
        *,
        id: str,
        subject_id: str,
        kind: str,
        content: str,
        source: str,
    ) -> None:
        now = _now_iso()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO memory_items(
                    id, subject_id, kind, content, source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind = excluded.kind,
                    content = excluded.content,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (id, subject_id, kind, content, source, now, now),
            )
            await db.commit()
        await self.audit(
            AuditEventType.MEMORY_UPDATED,
            actor_id=subject_id,
            detail={
                "subject_id": subject_id,
                "kind": kind,
                "source": source,
                "content_length": len(content),
            },
        )

    async def list_memory_items(self, subject_id: str) -> list[MemoryItem]:
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                """
                SELECT id, subject_id, kind, content, source, created_at, updated_at
                FROM memory_items
                WHERE subject_id = ?
                ORDER BY updated_at DESC, id ASC
                """,
                (subject_id,),
            )
        return [
            MemoryItem(
                id=str(row[0]),
                subject_id=str(row[1]),
                kind=str(row[2]),
                content=str(row[3]),
                source=str(row[4]),
                created_at=str(row[5]),
                updated_at=str(row[6]),
            )
            for row in rows
        ]

    async def clear_memory_items(self, subject_id: str, *, updated_by: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "DELETE FROM memory_items WHERE subject_id = ?",
                (subject_id,),
            )
            await db.commit()
            deleted_count = cursor.rowcount
        await self.audit(
            AuditEventType.MEMORY_DELETED,
            actor_id=updated_by,
            detail={"subject_id": subject_id, "deleted_count": deleted_count},
        )
        return deleted_count

    async def delete_memory_item(
        self,
        *,
        subject_id: str,
        item_id: str,
        updated_by: str,
    ) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "DELETE FROM memory_items WHERE subject_id = ? AND id = ?",
                (subject_id, item_id),
            )
            await db.commit()
            deleted_count = cursor.rowcount
        await self.audit(
            AuditEventType.MEMORY_DELETED,
            actor_id=updated_by,
            detail={
                "subject_id": subject_id,
                "item_id": item_id,
                "deleted_count": deleted_count,
            },
        )
        return deleted_count

    async def set_memory_enabled(
        self,
        *,
        subject_id: str,
        enabled: bool,
        updated_by: str,
    ) -> None:
        now = _now_iso()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO memory_preferences(subject_id, enabled, updated_by, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(subject_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (subject_id, int(enabled), updated_by, now),
            )
            await db.commit()
        await self.audit(
            AuditEventType.MEMORY_ENABLED if enabled else AuditEventType.MEMORY_DISABLED,
            actor_id=updated_by,
            detail={"subject_id": subject_id},
        )

    async def is_memory_enabled(self, subject_id: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                "SELECT enabled FROM memory_preferences WHERE subject_id = ?",
                (subject_id,),
            )
        rows_list = list(rows)
        if not rows_list:
            return True
        return bool(rows_list[0][0])

    async def set_writeback_auto_execute(
        self,
        *,
        subject_id: str,
        enabled: bool,
        updated_by: str,
    ) -> None:
        now = _now_iso()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO writeback_auto_preferences(
                    subject_id, enabled, updated_by, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(subject_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (subject_id, int(enabled), updated_by, now),
            )
            await db.commit()
        await self.audit(
            AuditEventType.WRITEBACK_AUTO_ENABLED
            if enabled
            else AuditEventType.WRITEBACK_AUTO_DISABLED,
            actor_id=updated_by,
            detail={"subject_id": subject_id},
        )

    async def get_writeback_auto_execute(
        self,
        subject_id: str,
    ) -> WritebackAutoPreference | None:
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                """
                SELECT subject_id, enabled, updated_by, updated_at
                FROM writeback_auto_preferences
                WHERE subject_id = ?
                """,
                (subject_id,),
            )
        rows_list = list(rows)
        if not rows_list:
            return None
        row = rows_list[0]
        return WritebackAutoPreference(
            subject_id=str(row[0]),
            enabled=bool(row[1]),
            updated_by=str(row[2]),
            updated_at=str(row[3]),
        )

    async def clear_writeback_auto_execute(
        self,
        subject_id: str,
        *,
        updated_by: str,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM writeback_auto_preferences WHERE subject_id = ?",
                (subject_id,),
            )
            await db.commit()
        await self.audit(
            AuditEventType.WRITEBACK_AUTO_CLEARED,
            actor_id=updated_by,
            detail={"subject_id": subject_id},
        )

    async def save_writeback_execution(
        self,
        *,
        action_id: str,
        actor_id: str,
        action_type: str,
        target: dict[str, Any],
        payload: dict[str, Any],
        result: dict[str, Any],
        undo_action_type: str | None = None,
        undo_target: dict[str, Any] | None = None,
        undo_payload: dict[str, Any] | None = None,
        undo_preview: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO writeback_executions(
                    action_id, actor_id, action_type, target_json, payload_json,
                    result_json, undo_action_type, undo_target_json, undo_payload_json,
                    undo_preview, executed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(action_id) DO UPDATE SET
                    result_json = excluded.result_json,
                    undo_action_type = excluded.undo_action_type,
                    undo_target_json = excluded.undo_target_json,
                    undo_payload_json = excluded.undo_payload_json,
                    undo_preview = excluded.undo_preview
                """,
                (
                    action_id,
                    actor_id,
                    action_type,
                    _json_dumps(target),
                    _json_dumps(payload),
                    _json_dumps(result),
                    undo_action_type,
                    _json_dumps(undo_target) if undo_target is not None else None,
                    _json_dumps(undo_payload) if undo_payload is not None else None,
                    undo_preview,
                    _now_iso(),
                ),
            )
            await db.commit()

    async def find_latest_reversible_writeback(
        self,
        actor_id: str,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                """
                SELECT action_id, actor_id, action_type, target_json, payload_json,
                       result_json, undo_action_type, undo_target_json,
                       undo_payload_json, undo_preview, executed_at
                FROM writeback_executions
                WHERE actor_id = ?
                  AND undo_action_type IS NOT NULL
                  AND undo_target_json IS NOT NULL
                  AND undo_payload_json IS NOT NULL
                  AND reverted_at IS NULL
                ORDER BY executed_at DESC
                LIMIT 1
                """,
                (actor_id,),
            )
        rows_list = list(rows)
        if not rows_list:
            return None
        row = rows_list[0]
        return {
            "action_id": row[0],
            "actor_id": row[1],
            "action_type": row[2],
            "target": json.loads(row[3]),
            "payload": json.loads(row[4]),
            "result": json.loads(row[5]),
            "undo_action_type": row[6],
            "undo_target": json.loads(row[7]),
            "undo_payload": json.loads(row[8]),
            "undo_preview": row[9],
            "executed_at": row[10],
        }

    async def list_recent_writeback_executions(
        self,
        actor_id: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                """
                SELECT action_id, action_type, target_json, result_json,
                       undo_action_type, executed_at, reverted_at
                FROM writeback_executions
                WHERE actor_id = ?
                ORDER BY executed_at DESC
                LIMIT ?
                """,
                (actor_id, max(limit, 0)),
            )
        return [
            {
                "action_id": row[0],
                "action_type": row[1],
                "target": json.loads(row[2]),
                "result": json.loads(row[3]),
                "reversible": row[4] is not None and row[6] is None,
                "executed_at": row[5],
                "reverted_at": row[6],
            }
            for row in rows
        ]

    async def mark_writeback_reverted(self, action_id: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE writeback_executions
                SET reverted_at = COALESCE(reverted_at, ?)
                WHERE action_id = ?
                """,
                (_now_iso(), action_id),
            )
            await db.commit()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
