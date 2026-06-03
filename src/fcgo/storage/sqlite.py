import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import aiosqlite

from fcgo.logging import redact
from fcgo.models import ActionProposal, AuditEventType, ModelPreference, PendingActionStatus


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
