from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class ConversationType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"


class ResourceType(StrEnum):
    FEISHU_DOC = "feishu_doc"
    FEISHU_SHEET = "feishu_sheet"
    FEISHU_BITABLE = "feishu_bitable"
    WEB = "web"
    UNKNOWN = "unknown"


class WriteActionType(StrEnum):
    DOC_CREATE = "doc_create"
    DOC_APPEND = "doc_append"
    DOC_DELETE_BLOCK = "doc_delete_block"
    SHEET_WRITE_RANGE = "sheet_write_range"
    BITABLE_CREATE_RECORD = "bitable_create_record"
    BITABLE_UPDATE_RECORD = "bitable_update_record"
    BITABLE_DELETE_RECORD = "bitable_delete_record"
    MESSAGE_SEND = "message_send"


class ToolName(StrEnum):
    READ_RESOURCE = "read_resource"
    PROPOSE_WRITEBACK = "propose_writeback"
    SEARCH_RESOURCES = "search_resources"
    INSPECT_DOC_STRUCTURE = "inspect_doc_structure"
    PREPARE_WRITEBACK = "prepare_writeback"
    GET_WRITEBACK_POLICY = "get_writeback_policy"
    LIST_MEMORY = "list_memory"
    UPSERT_MEMORY_DRAFT = "upsert_memory_draft"
    GET_MODEL_STATUS = "get_model_status"
    WEB_READ = "web_read"


class WritebackConfirmationMode(StrEnum):
    ALWAYS = "always"
    LOW_RISK_DIRECT = "low_risk_direct"
    DRAFT_ONLY = "draft_only"


class FeishuMention(BaseModel):
    key: str = ""
    open_id: str = ""
    user_id: str = ""
    union_id: str = ""
    name: str = ""


class FeishuMessage(BaseModel):
    message_id: str
    chat_id: str
    sender_id: str
    text: str
    conversation_type: ConversationType
    conversation_key: str = ""
    thread_id: str | None = None
    root_id: str | None = None
    parent_id: str | None = None
    mentions: list[FeishuMention] = Field(default_factory=list)
    is_bot_mentioned: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class FeishuBotMenuEvent(BaseModel):
    event_id: str = ""
    event_key: str
    operator_open_id: str = ""
    operator_user_id: str = ""
    operator_union_id: str = ""
    operator_name: str = ""
    timestamp: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ResourceRef(BaseModel):
    type: ResourceType
    url: str
    title: str | None = None
    source_kind: str | None = None
    token: str | None = None
    sheet_id: str | None = None
    table_id: str | None = None
    view_id: str | None = None
    range_hint: str | None = None


class ResourceReadResult(BaseModel):
    ref: ResourceRef
    title: str | None = None
    content: str = ""
    truncated: bool = False
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResourceSearchPlan(BaseModel):
    should_search: bool = False
    queries: list[str] = Field(default_factory=list)
    resource_types: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    reason: str = ""


class AgentObservationKind(StrEnum):
    CURRENT_MESSAGE = "current_message"
    CHAT_SUMMARY = "chat_summary"
    RECENT_CHAT = "recent_chat"
    RESOURCE_LINKS = "resource_links"
    RESOURCE_RESULT = "resource_result"
    USER_MEMORY = "user_memory"


class AgentObservation(BaseModel):
    kind: AgentObservationKind
    title: str
    content: str
    source: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryItem(BaseModel):
    id: str
    subject_id: str
    kind: str
    content: str
    source: str
    created_at: str
    updated_at: str


class AssistantRequest(BaseModel):
    request_id: str = Field(default_factory=lambda: uuid4().hex)
    actor_id: str
    conversation_id: str
    conversation_type: ConversationType
    text: str
    assistant_name: str = "小智"
    assistant_profile: str = ""
    writeback_enabled: bool = False
    model_provider: str | None = None
    model: str | None = None
    resource_urls: list[str] = Field(default_factory=list)
    resource_refs: list[ResourceRef] = Field(default_factory=list)
    resource_results: list[ResourceReadResult] = Field(default_factory=list)
    chat_context_messages: list["ChatContextMessage"] = Field(default_factory=list)
    chat_context_summary: str = ""
    chat_context_omitted_count: int = 0
    memory_items: list[MemoryItem] = Field(default_factory=list)


class ChatContextMessage(BaseModel):
    message_id: str
    sender_id: str = ""
    text: str
    created_at: str


class ResourceReadRequest(BaseModel):
    refs: list[ResourceRef]
    reason: str = ""


class ResourceSearchRequest(BaseModel):
    queries: list[str] = Field(default_factory=list)
    query: str = ""
    limit: int = 5
    resource_types: list[str] = Field(default_factory=list)
    reason: str = ""


class WebReadRequest(BaseModel):
    url: str
    reason: str = ""


class EmptyToolRequest(BaseModel):
    reason: str = ""


class MemoryDraftRequest(BaseModel):
    key: str
    kind: str = "记忆"
    content: str
    source: str = "agent.draft"


class ActionProposalDraft(BaseModel):
    action_type: WriteActionType
    target: dict[str, Any]
    payload: dict[str, Any]
    preview: str

    def to_proposal(
        self,
        *,
        actor_id: str,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> "ActionProposal":
        created_at = now or datetime.now(UTC)
        return ActionProposal(
            actor_id=actor_id,
            action_type=self.action_type,
            target=self.target,
            target_title=_optional_string(
                self.target.get("target_title") or self.target.get("title")
            ),
            target_url=_optional_string(
                self.target.get("target_url") or self.target.get("url")
            ),
            payload=self.payload,
            preview=self.preview,
            created_at=created_at,
            expires_at=created_at + timedelta(seconds=ttl_seconds),
        )


class WritebackProposalRequest(BaseModel):
    proposals: list[ActionProposalDraft]
    reason: str = ""


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    name: ToolName
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    call_id: str
    name: ToolName
    ok: bool
    content: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    user_message: str | None = None
    audit_metadata: dict[str, Any] = Field(default_factory=dict)


class ModelToolSpec(BaseModel):
    name: ToolName
    description: str
    parameters: dict[str, Any]
    read_only: bool = True
    required_permissions: list[str] = Field(default_factory=list)
    allow_group: bool = True
    timeout_seconds: float = 30.0
    audit_event_type: str | None = None


class AgentDecision(BaseModel):
    final_response: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    writeback_drafts: list[ActionProposalDraft] = Field(default_factory=list)

    @field_validator("tool_calls", "writeback_drafts", mode="before")
    @classmethod
    def _none_lists_are_empty(cls, value: Any) -> Any:
        return [] if value is None else value


class ActionProposal(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    actor_id: str
    action_type: WriteActionType
    target: dict[str, Any]
    target_title: str | None = None
    target_url: str | None = None
    payload: dict[str, Any]
    preview: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime


class AssistantResponse(BaseModel):
    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    action_proposals: list[ActionProposal] = Field(default_factory=list)


class PendingActionStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    EXECUTED = "executed"
    FAILED = "failed"
    CANCELED = "canceled"
    EXPIRED = "expired"


class AuditEventType(StrEnum):
    OAUTH_STATE_CREATED = "oauth_state_created"
    OAUTH_AUTHORIZED = "oauth_authorized"
    OAUTH_TOKEN_REFRESHED = "oauth_token_refreshed"
    CONTEXT_ENABLED = "context_enabled"
    CONTEXT_DISABLED = "context_disabled"
    MEMORY_ENABLED = "memory_enabled"
    MEMORY_DELETED = "memory_deleted"
    MEMORY_DISABLED = "memory_disabled"
    MEMORY_UPDATED = "memory_updated"
    CONTEXT_HISTORY_READ = "context_history_read"
    RESOURCE_SEARCHED = "resource_searched"
    RESOURCE_READ = "resource_read"
    AGENT_TOOL_PLANNED = "agent_tool_planned"
    AGENT_TOOL_EXECUTED = "agent_tool_executed"
    ACTION_CREATED = "action_created"
    ACTION_CONFIRMED = "action_confirmed"
    ACTION_CANCELED = "action_canceled"
    ACTION_EXPIRED = "action_expired"
    WRITE_EXECUTED = "write_executed"
    WRITE_REVERTED = "write_reverted"
    WRITEBACK_AUTO_ENABLED = "writeback_auto_enabled"
    WRITEBACK_AUTO_DISABLED = "writeback_auto_disabled"
    WRITEBACK_AUTO_CLEARED = "writeback_auto_cleared"
    ASSISTANT_NAME_SET = "assistant_name_set"
    ASSISTANT_NAME_CLEARED = "assistant_name_cleared"
    ASSISTANT_PROFILE_SET = "assistant_profile_set"
    ASSISTANT_PROFILE_CLEARED = "assistant_profile_cleared"
    MODEL_PREFERENCE_SET = "model_preference_set"
    MODEL_PREFERENCE_CLEARED = "model_preference_cleared"
    MODEL_GENERATED = "model_generated"
    ERROR = "error"


class ConfirmationResult(BaseModel):
    status: Literal[
        "accepted",
        "executed",
        "canceled",
        "expired",
        "not_found",
        "forbidden",
        "duplicate",
        "failed",
    ]
    message: str


class ModelPreference(BaseModel):
    scope: str
    provider: str
    model: str | None = None
    updated_by: str
    updated_at: str


class AssistantNamePreference(BaseModel):
    subject_id: str
    assistant_name: str
    updated_by: str
    updated_at: str


class AssistantProfilePreference(BaseModel):
    subject_id: str
    assistant_profile: str
    updated_by: str
    updated_at: str


class ContextPreference(BaseModel):
    scope: str
    enabled: bool
    updated_by: str
    updated_at: str


class WritebackAutoPreference(BaseModel):
    subject_id: str
    enabled: bool
    updated_by: str
    updated_at: str


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
