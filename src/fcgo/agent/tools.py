from typing import Any

from fcgo.models import (
    ModelToolSpec,
    ResourceReadRequest,
    ToolName,
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
    ]


def _json_schema(
    model: type[ResourceReadRequest],
) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema
