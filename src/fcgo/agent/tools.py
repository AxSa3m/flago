from typing import Any

from fcgo.models import (
    ModelToolSpec,
    ResourceReadRequest,
    ToolName,
    WritebackProposalRequest,
)


def model_tool_specs() -> list[ModelToolSpec]:
    return [
        ModelToolSpec(
            name=ToolName.READ_RESOURCE,
            description=(
                "Read user-authorized Feishu or web resources. Use this before answering "
                "questions that require linked document, sheet, bitable, or web content."
            ),
            parameters=_json_schema(ResourceReadRequest),
        ),
        ModelToolSpec(
            name=ToolName.PROPOSE_WRITEBACK,
            description=(
                "Create writeback proposals for user confirmation. This tool never executes "
                "writes directly; the service stores proposals and waits for an explicit "
                "Feishu card confirmation."
            ),
            parameters=_json_schema(WritebackProposalRequest),
        ),
    ]


def _json_schema(
    model: type[ResourceReadRequest] | type[WritebackProposalRequest],
) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema
