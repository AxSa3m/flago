from typing import Protocol

from fcgo.models import (
    ActionProposal,
    AssistantRequest,
    AssistantResponse,
    ResourceReadResult,
    ResourceRef,
)


class ModelProvider(Protocol):
    async def generate(self, request: AssistantRequest) -> AssistantResponse:
        """Generate an assistant response for a normalized request."""


class ResourceReader(Protocol):
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:
        """Read a resource through the actor's authorization context."""


class ActionProposalStore(Protocol):
    async def save_pending_action(self, proposal: ActionProposal) -> None:
        """Persist a model-proposed action until the user confirms it."""
