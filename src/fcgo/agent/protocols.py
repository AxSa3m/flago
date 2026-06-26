from typing import Any, Protocol

from fcgo.models import (
    ActionProposal,
    AssistantRequest,
    AssistantResponse,
    AuditEventType,
    ResourceReadResult,
    ResourceRef,
    ResourceSearchPlan,
)


class ModelProvider(Protocol):
    async def generate(self, request: AssistantRequest) -> AssistantResponse:
        """Generate an assistant response for a normalized request."""


class ResourceReader(Protocol):
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:
        """Read a resource through the actor's authorization context."""


class ResourceSearcher(Protocol):
    async def search(self, query: str, actor_id: str, *, limit: int) -> list[ResourceRef]:
        """Search user-visible resources through the actor's authorization context."""


class ResourceSearchPlanner(Protocol):
    async def plan(self, request: AssistantRequest) -> ResourceSearchPlan:
        """Plan whether and how to search resources before tool execution."""


class AuditRecorder(Protocol):
    async def audit(
        self,
        event_type: AuditEventType,
        *,
        actor_id: str | None = None,
        action_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Persist metadata-only audit events."""


class ActionProposalStore(Protocol):
    async def save_pending_action(self, proposal: ActionProposal) -> None:
        """Persist a model-proposed action until the user confirms it."""
