from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from flgo.agent import Assistant
from flgo.agent.assistant import _resource_search_query
from flgo.agent.search_planner import ModelResourceSearchPlanner, ResourceSearchPlanningError
from flgo.gemini.provider import EchoModelProvider
from flgo.models import (
    ActionProposal,
    AssistantRequest,
    AssistantResponse,
    AuditEventType,
    ChatContextMessage,
    ConversationType,
    ResourceReadResult,
    ResourceRef,
    ResourceSearchPlan,
    ResourceType,
    WriteActionType,
)


class RecordingModelProvider:
    def __init__(self) -> None:
        self.requests: list[AssistantRequest] = []

    async def generate(self, request: AssistantRequest) -> AssistantResponse:
        self.requests.append(request)
        return AssistantResponse(text="ok")


class PlannerJsonModelProvider:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[AssistantRequest] = []

    async def generate(self, request: AssistantRequest) -> AssistantResponse:
        self.requests.append(request)
        return AssistantResponse(text=self.text)


class DocsFeishuLinkModelProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "链接：[测试文档]"
                "(https://docs.feishu.cn/docx/docx123?from=old_context)"
            )
        )


class UnknownFeishuLinkModelProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text="找到文档：情感调优剧本，链接：https://docs.feishu.cn/docx/ABC123"
        )


class SheetWrongProposalModelProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        now = datetime.now(UTC)
        return AssistantResponse(
            text="模型正文说写入 Sheet1!C2",
            action_proposals=[
                ActionProposal(
                    actor_id="ou_user",
                    action_type=WriteActionType.SHEET_WRITE_RANGE,
                    target={"spreadsheet_token": "sht123", "range": "sheet1!A4:A4"},
                    payload={"values": [["FLGO 写回测试删除成功4"]]},
                    preview="向电子表格 sheet1!A4:A4 写入：FLGO 写回测试删除成功4",
                    created_at=now,
                    expires_at=now + timedelta(seconds=300),
                )
            ],
        )


class SheetModelTextTargetProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "好的，我已读取该电子表格。当前 C4 单元格为空，"
                "您希望将其内容更新为“FLGO 写回测试删除成功6”。\n\n"
                "| 位置 | 当前值 | 新值 |\n"
                "|------|--------|------|\n"
                "| Sheet1!C4 | （空） | FLGO 写回测试删除成功6 |"
            )
        )


class SheetWrongProposalWithModelTargetProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        now = datetime.now(UTC)
        return AssistantResponse(
            text=(
                "好的，我将生成写回预览。\n\n"
                "| 位置 | 当前值 | 新值 |\n"
                "|------|--------|------|\n"
                "| Sheet1!C4 | （空） | FLGO 写回测试删除成功6 |"
            ),
            action_proposals=[
                ActionProposal(
                    actor_id="ou_user",
                    action_type=WriteActionType.SHEET_WRITE_RANGE,
                    target={"spreadsheet_token": "sht123", "range": "sheet1!A5:A5"},
                    payload={"values": [["FLGO 写回测试删除成功6"]]},
                    preview="向电子表格 sheet1!A5:A5 写入：FLGO 写回测试删除成功6",
                    created_at=now,
                    expires_at=now + timedelta(seconds=300),
                )
            ],
        )


class BitableUpdateModelTextProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "根据读取的多维表格数据，我理解您的意图是：\n"
                "将 “测试内容”字段值为“测试列1” 的那一行记录（表中第10行），"
                "其 “测试列2”字段 设置为 “测试数据”。\n\n"
                "| 测试内容 | 测试列2（待更新） |\n"
                "|----------|------------------|\n"
                "| 测试列1  | 测试数据          |"
            )
        )


class BitableWrongCreateWithModelUpdateProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        now = datetime.now(UTC)
        return AssistantResponse(
            text=(
                "将 “测试内容”字段值为“测试列1” 的那一行记录（表中第10行），"
                "其 “测试列2”字段 设置为 “测试数据”。"
            ),
            action_proposals=[
                ActionProposal(
                    actor_id="ou_user",
                    action_type=WriteActionType.BITABLE_CREATE_RECORD,
                    target={"app_token": "app123", "table_id": "tbl1"},
                    payload={"fields": {"测试列2": "测试数据"}},
                    preview="向多维表格新增记录：测试列2 = 测试数据",
                    created_at=now,
                    expires_at=now + timedelta(seconds=300),
                )
            ],
        )


class DocGeneratedContentProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "好的，我将为你生成一段自我介绍，并准备写入到指定文档中。\n\n"
                "待写内容预览：\n\n"
                "> 我是 FLGO，一个接入飞书的工作助手。我擅长用中文提供简洁、"
                "准确、可执行的答案，也可以在你确认后处理飞书文档、表格和多维表格。"
            )
        )


class DocWrongProposalWithGeneratedContentProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        now = datetime.now(UTC)
        return AssistantResponse(
            text=(
                "待写内容预览：\n\n"
                "> 我是 FLGO，一个接入飞书的工作助手。我会先生成预览，"
                "再等待你确认后写入。"
            ),
            action_proposals=[
                ActionProposal(
                    actor_id="ou_user",
                    action_type=WriteActionType.DOC_APPEND,
                    target={"document_id": "docx123"},
                    payload={"content": "写一段你的自我介绍"},
                    preview="向文档追加文本：\n写一段你的自我介绍",
                    created_at=now,
                    expires_at=now + timedelta(seconds=300),
                )
            ],
        )


class DocPreviewInstructionBeforeValueProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "待写内容预览：\n"
                "将以下模型生成内容写入指定文档。\n\n"
                "写入值：\n"
                "> 我是 FLGO，一个接入飞书的工作助手。"
            )
        )


class DocConfirmationQuestionProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "好的，已准备好将“正在测试的文字”写入《测试文档》末尾。"
                "需要生成确认卡片吗？"
            )
        )


class SheetGeneratedContentProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "我会将生成后的摘要写入 C4。\n\n"
                "待写内容预览：\n\n"
                "> 这是模型生成的表格摘要内容。"
            )
        )


class SheetPreviewInstructionBeforeValueProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "根据读取到表格内容，简要总结如下。\n\n"
                "待写内容预览：\n"
                "目标位置：电子表格 Sheet1 的 D4 单元格。\n"
                "写入值：\n"
                "> 该表为写回测试记录，包含4条删除成功测试项。"
            )
        )


class BitableGeneratedUpdateProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "将 “测试内容”字段值为“测试列1” 的那一行记录，"
                "其 “测试列2”字段 设置为以下内容。\n\n"
                "待写内容预览：\n\n"
                "> 这是模型生成后的测试数据。"
            )
        )


class BitablePreviewInstructionBeforeValueProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "根据已读取的多维表数据，总结如下：\n\n"
                "待写内容预览：\n"
                "将总结结果写入 最后一行（第10条记录，record_id: rec10）"
                "的“测试列2”字段，覆盖原有值“测试数据”。\n\n"
                "写入值：\n"
                "> 该多维表共10条记录，主要用于FLGO写回测试功能验证。"
            )
        )


class BitableMarkdownTablePreviewProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "根据已读取的多维表记录，我为你总结如下：\n\n"
                "该多维表包含 10 条记录，字段为“测试内容”和“测试列2”。\n\n"
                "**待写内容预览：**\n\n"
                "| 操作 | 目标记录 | 目标字段 | 新值 |\n"
                "|------|----------|----------|------|\n"
                "| 更新 | 第10条（record_id=rec10） | 测试列2 | "
                "该多维表共10条记录，主要记录FLGO写回测试结果，其中8条为"
                "\"FLGO 写回测试成功\"，1条为\"FLGO 测试成功\"，1条为"
                "\"测试列1内容\"。最后一条的测试列2原值为\"测试数据\"。整体为功能验证用途。 |\n\n"
                "请在飞书卡片中确认后执行更新。"
            )
        )


class BitableCrudJsonPlanProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "我已根据语义判断这是 create 操作，会新增一条多维表记录。\n\n"
                "```json\n"
                "{\n"
                '  "flgo_writeback": {\n'
                '    "operation": "create",\n'
                '    "resource_type": "bitable",\n'
                '    "target": {"field": "测试列2", "record": "last"},\n'
                '    "payload": {"content": "模型结构化 CRUD 计划写入值"},\n'
                '    "preview": "向测试列2新增一条记录：模型结构化 CRUD 计划写入值"\n'
                "  }\n"
                "}\n"
                "```"
            )
        )


class BitableAppendSummaryProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "好的，已读取多维表格内容。按照您的指令，我计划新增一条记录作为最后一行。\n\n"
                "待写内容预览：\n"
                "- 操作：新增记录（追加到最后一行之后）\n"
                "- 字段：测试内容\n"
                "- 写入值：该多维表格共 10 条记录，主要记录 FLGO 写回测试结果，"
                "其中 8 条为“FLGO 写回测试成功”。整体为功能验证用途。\n"
            )
        )


class BitableIncreaseToFieldLastRowProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "根据读取到的记录，该多维表格共有10条记录，字段包括“测试内容”和“测试列2”。\n\n"
                "**待写内容预览：**\n"
                "- **操作**：新增一条记录（追加到最后一行之后）\n"
                "- **目标字段**：测试列2\n"
                "- **写入值**：该多维表格共10条记录，主要用于FLGO写回功能测试，"
                "其中7条记录测试内容为“FLGO写回测试成功”，1条为“FLGO测试成功”，"
                "1条记录含“测试列1内容”但测试列2数据异常，另有1条记录内容为无效预览文本。"
            )
        )


class BitableReplaceLastRowSummaryProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "已读取多维表格的 10 条记录，总结如下：\n\n"
                "该多维表格主要用于 FLGO 写回功能测试。\n\n"
                "**待写内容预览：**\n"
                "- **目标记录**：第 10 条（record_id: `rec10`）\n"
                "- **目标字段**：测试列2\n"
                "- **当前值**：`测试数据`\n"
                "- **新值**：`该多维表格共10条记录，其中8条为“FLGO写回测试成功”，"
                "1条为“FLGO测试成功”，最后1条原含“测试数据”；整体为FLGO功能验证测试。`\n\n"
                "请确认后，我将通过飞书卡片提交写入。"
            )
        )


class BitableNaturalUpdateFirstRowProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "### 多维表总结\n\n"
                "该表共有 10 条记录，字段为「测试内容」和「测试列2」。\n\n"
                "### 待写内容预览\n"
                "- **操作**：更新（覆盖）第 1 条记录的「测试列2」字段\n"
                "- **目标记录**：`rec1`\n"
                "- **写入值**：\n"
                "> 该多维表共 10 条记录，测试内容均为 FLGO 写回测试相关，"
                "测试列2仅最后一条含测试数据，其余为空。整体为功能验证测试。"
            )
        )


class BitableWrongUpdateForAppendProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        now = datetime.now(UTC)
        return AssistantResponse(
            text=(
                "待写内容预览：\n"
                "- 操作：新增记录（追加到最后一行之后）\n"
                "- 字段：测试内容\n"
                "- 写入值：该多维表格共 10 条记录，主要记录 FLGO 写回测试结果。\n"
            ),
            action_proposals=[
                ActionProposal(
                    actor_id="ou_user",
                    action_type=WriteActionType.BITABLE_UPDATE_RECORD,
                    target={"app_token": "app123", "table_id": "tbl1", "record_id": "rec7"},
                    payload={"fields": {"测试内容": "*待写内容预览：** - **操作**：新增记录"}},
                    preview=(
                        "更新多维表格记录 rec7：测试内容 = "
                        "*待写内容预览：** - **操作**：新增记录"
                    ),
                    created_at=now,
                    expires_at=now + timedelta(seconds=300),
                )
            ],
        )


class SheetAppendSummaryProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "根据读取到表格内容，简要总结如下。\n\n"
                "### 待写内容预览：\n"
                "- 目标位置：电子表格的 D4 单元格（当前数据区域最后一行对应的 D 列）\n"
                "- 写入内容：\n"
                "> 该表记录了 4 次写回测试删除成功的操作，其中第 4 次操作还包含一个关联数据。\n"
                "---\n"
                "请确认上述预览是否符合您的意图。"
            )
        )


class SheetAppendSummaryWithoutTargetProvider:
    async def generate(self, request: AssistantRequest) -> AssistantResponse:  # noqa: ARG002
        return AssistantResponse(
            text=(
                "根据读取到表格内容，简要总结如下。\n\n"
                "### 待写内容预览：\n"
                "- 写入内容：\n"
                "> 该表记录了 4 次写回测试删除成功的操作，其中第 4 次操作还包含一个关联数据。\n"
                "---\n"
                "请确认上述预览是否符合您的意图。"
            )
        )


class StubResourceReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:
        return ResourceReadResult(ref=ref, title="测试资源", content=f"{actor_id}: 资源正文")


class DocWithEmbeddedAssetReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:  # noqa: ARG002
        return ResourceReadResult(
            ref=ref,
            title="测试文档",
            content="文档正文\n\n内嵌文件与图片：\n- Claude Code登录指引.docx",
            metadata={
                "embedded_assets": [
                    {
                        "token": "file-docx",
                        "name": "Claude Code登录指引.docx",
                        "kind": "file",
                        "block_id": "file-1",
                        "parent_block_id": "docx123",
                        "index": 2,
                    }
                ]
            },
        )


class NoTitleResourceReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:
        return ResourceReadResult(ref=ref, content=f"{actor_id}: 资源正文")


class StubResourceSearcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def search(self, query: str, actor_id: str, *, limit: int) -> list[ResourceRef]:
        self.calls.append((query, actor_id, limit))
        return [
            ResourceRef(
                type=ResourceType.FEISHU_DOC,
                url="https://my.feishu.cn/docx/docx123",
                source_kind="search:doc",
                token="docx123",
            ),
            ResourceRef(
                type=ResourceType.FEISHU_SHEET,
                url="https://my.feishu.cn/sheets/sht123",
                source_kind="search:sheet",
                token="sht123",
            ),
        ]


class TitledDocResourceSearcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def search(self, query: str, actor_id: str, *, limit: int) -> list[ResourceRef]:
        self.calls.append((query, actor_id, limit))
        return [
            ResourceRef(
                type=ResourceType.FEISHU_DOC,
                url="https://my.feishu.cn/docx/docx123",
                title="测试文档",
                source_kind="search:doc",
                token="docx123",
            )
        ]


class QuerySpecificResourceSearcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def search(self, query: str, actor_id: str, *, limit: int) -> list[ResourceRef]:
        self.calls.append((query, actor_id, limit))
        return [
            ResourceRef(
                type=ResourceType.FEISHU_DOC,
                url=f"https://my.feishu.cn/docx/{query}-{index}",
                source_kind="search:doc",
                token=f"{query}-{index}",
            )
            for index in range(limit)
        ]


class StubResourceSearchPlanner:
    def __init__(self, plan: ResourceSearchPlan) -> None:
        self.plan_result = plan
        self.requests: list[AssistantRequest] = []

    async def plan(self, request: AssistantRequest) -> ResourceSearchPlan:
        self.requests.append(request)
        return self.plan_result


class FailingResourceSearchPlanner:
    async def plan(self, request: AssistantRequest) -> ResourceSearchPlan:  # noqa: ARG002
        raise RuntimeError("planner failed with sensitive query 情感调优")


class RecordingAuditRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[AuditEventType, str | None, dict[str, Any]]] = []

    async def audit(
        self,
        event_type: AuditEventType,
        *,
        actor_id: str | None = None,
        action_id: str | None = None,  # noqa: ARG002
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.events.append((event_type, actor_id, detail or {}))


class EmptyResourceSearcher:
    async def search(self, query: str, actor_id: str, *, limit: int) -> list[ResourceRef]:  # noqa: ARG002
        return []


class ResolvingWikiResourceReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:  # noqa: ARG002
        resolved = ref.model_copy(
            update={
                "source_kind": "wiki:docx",
                "token": "docx_from_wiki",
            }
        )
        return ResourceReadResult(ref=resolved, title="Wiki 文档", content="正文")


class ResolvingSheetResourceReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:  # noqa: ARG002
        resolved = ref.model_copy(update={"sheet_id": "sheet1"})
        return ResourceReadResult(
            ref=resolved,
            title="Sheet1范围 sheet1!A1:Z200",
            content="读取范围：sheet1!A1:Z200\n[空表格范围]",
        )


class ExplicitRangeSheetResourceReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:  # noqa: ARG002
        return ResourceReadResult(
            ref=ref,
            title="Sheet1范围 sheet1!B2",
            content="读取范围：sheet1!B2\n[空表格范围]",
        )


class NonEmptySheetResourceReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:  # noqa: ARG002
        resolved = ref.model_copy(update={"sheet_id": "sheet1"})
        return ResourceReadResult(
            ref=resolved,
            title="Sheet1范围 sheet1!A1:Z200",
            content=(
                "读取范围：sheet1!A1:Z200\n"
                "非空单元格数量：1\n"
                "建议追加起始行：2\n\n"
                "| FLGO 写回测试删除成功 |\n"
                "| --- |"
            ),
        )


class AppendRowFiveSheetResourceReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:  # noqa: ARG002
        resolved = ref.model_copy(update={"sheet_id": "sheet1"})
        return ResourceReadResult(
            ref=resolved,
            title="Sheet1范围 sheet1!A1:Z200",
            content=(
                "读取范围：sheet1!A1:Z200\n"
                "非空单元格数量：4\n"
                "建议追加起始行：5\n\n"
                "非空行摘录：\n"
                "- 第 1 行：FLGO 写回测试删除成功\n"
                "- 第 2 行：FLGO 写回测试删除成功2\n"
                "- 第 3 行：FLGO 写回测试删除成功3\n"
                "- 第 4 行：FLGO 写回测试删除成功4\n"
            ),
        )


class ResolvingBitableResourceReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:  # noqa: ARG002
        resolved = ref.model_copy(update={"table_id": "tbl1"})
        return ResourceReadResult(
            ref=resolved,
            title="测试多维表",
            content=(
                "字段：\n- 关键词（类型：1）\n- 标题（类型：1）"
                "\n\n记录摘录：\n[没有读取到记录]"
            ),
        )


class MatrixHeaderBitableResourceReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:  # noqa: ARG002
        resolved = ref.model_copy(update={"table_id": "tbl1"})
        return ResourceReadResult(
            ref=resolved,
            title="测试多维表",
            content="字段：\n- 文本\n\n记录摘录：\n[没有读取到记录]",
        )


class TestContentBitableResourceReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:  # noqa: ARG002
        resolved = ref.model_copy(update={"table_id": "tbl1"})
        return ResourceReadResult(
            ref=resolved,
            title="测试多维表",
            content="字段：\n- 测试内容\n\n记录摘录：\n[没有读取到记录]",
        )


class MixedFieldBitableResourceReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:  # noqa: ARG002
        resolved = ref.model_copy(update={"table_id": "tbl1"})
        return ResourceReadResult(
            ref=resolved,
            title="通用多维表",
            content=(
                "字段：\n"
                "- 日期（类型：5）\n"
                "- 附件（类型：17）\n"
                "- 客户需求（类型：1）\n"
                "\n记录摘录：\n[没有读取到记录]"
            ),
        )


class HeaderOnlyMixedBitableResourceReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:  # noqa: ARG002
        resolved = ref.model_copy(update={"table_id": "tbl1"})
        return ResourceReadResult(
            ref=resolved,
            title="表头多维表",
            content="记录摘录：\n| 附件 | 项目说明 | 日期 |\n| --- | --- | --- |\n",
        )


class NoFieldBitableResourceReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:  # noqa: ARG002
        resolved = ref.model_copy(update={"table_id": "tbl1"})
        return ResourceReadResult(
            ref=resolved,
            title="测试多维表",
            content="记录摘录：\n[没有读取到记录]",
        )


class BitableRecordsWithIdsResourceReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:  # noqa: ARG002
        resolved = ref.model_copy(update={"table_id": "tbl1"})
        return ResourceReadResult(
            ref=resolved,
            title="测试多维表",
            content=(
                "字段：\n"
                "- 测试内容（类型：1）\n"
                "- 测试列2（类型：1）\n"
                "\n记录索引：\n"
                "- 第 9 条：record_id=rec9；测试内容=其他行；测试列2=旧值\n"
                "- 第 10 条：record_id=rec10；测试内容=测试列1\n"
                "\n记录摘录：\n"
                "| 测试内容 | 测试列2 |\n"
                "| --- | --- |\n"
                "| 其他行 | 旧值 |\n"
                "| 测试列1 |  |"
            ),
        )


class BitableTenRecordsWithIdsResourceReader:
    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:  # noqa: ARG002
        resolved = ref.model_copy(update={"table_id": "tbl1"})
        return ResourceReadResult(
            ref=resolved,
            title="测试多维表",
            content=(
                "字段：\n"
                "- 测试内容（类型：1）\n"
                "- 测试列2（类型：1）\n"
                "\n记录索引：\n"
                "- 第 1 条：record_id=rec1；测试内容=FLGO 写回测试成功；测试列2=\n"
                "- 第 10 条：record_id=rec10；测试内容=测试列1内容；测试列2=测试数据\n"
                "\n记录摘录：\n"
                "| 测试内容 | 测试列2 |\n"
                "| --- | --- |\n"
                "| FLGO 写回测试成功 |  |\n"
                "| 测试列1内容 | 测试数据 |"
            ),
        )


def test_resource_search_query_strips_natural_language_noise() -> None:
    assert _resource_search_query("帮我找一篇文档，名字应该是包含 情感调优") == "情感调优"
    assert _resource_search_query("帮我找一篇 情感调优 的文档 是个剧本") == "情感调优 剧本"


@pytest.mark.asyncio
async def test_model_resource_search_planner_parses_structured_json_plan() -> None:
    provider = PlannerJsonModelProvider(
        "```json\n"
        "{\n"
        '  "should_search": true,\n'
        '  "queries": ["情感调优 无声对白 分镜", "情感调优"],\n'
        '  "resource_types": ["wiki", "docx"],\n'
        '  "constraints": ["剧本"],\n'
        '  "reason": "用户要找飞书文档"\n'
        "}\n"
        "```"
    )
    planner = ModelResourceSearchPlanner(provider)
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我找那篇情感调优的剧本",
    )

    plan = await planner.plan(request)

    assert plan.should_search is True
    assert plan.queries == ["情感调优 无声对白 分镜", "情感调优"]
    assert plan.resource_types == ["wiki", "doc"]
    assert plan.constraints == ["剧本"]
    assert "当前用户消息" in provider.requests[0].text


@pytest.mark.asyncio
async def test_model_resource_search_planner_returns_empty_plan_on_invalid_json() -> None:
    provider = PlannerJsonModelProvider("我觉得应该搜一下，但没有 JSON")
    planner = ModelResourceSearchPlanner(provider)
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我找文档",
    )

    with pytest.raises(ResourceSearchPlanningError):
        await planner.plan(request)


@pytest.mark.asyncio
async def test_assistant_falls_back_to_local_search_when_planner_returns_invalid_json() -> None:
    planner = ModelResourceSearchPlanner(PlannerJsonModelProvider("不是 JSON"))
    provider = RecordingModelProvider()
    searcher = StubResourceSearcher()
    assistant = Assistant(
        provider,
        StubResourceReader(),
        searcher,
        resource_search_planner=planner,
        resource_search_limit=5,
        resource_search_read_limit=1,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我搜索飞书文档 项目计划",
    )

    await assistant.handle(request)

    assert searcher.calls == [("项目计划", "ou_user", 5)]
    assert provider.requests[0].resource_results[0].content == "ou_user: 资源正文"


@pytest.mark.asyncio
async def test_assistant_extracts_urls() -> None:
    assistant = Assistant(EchoModelProvider())
    request = AssistantRequest(
        actor_id="user-1",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="总结 https://example.com",
    )

    response = await assistant.handle(request)

    assert request.resource_urls == ["https://example.com"]
    assert len(request.resource_refs) == 1
    assert request.resource_refs[0].type == ResourceType.WEB
    assert "收到" in response.text


@pytest.mark.asyncio
async def test_assistant_passes_first_pdf_pages_hint_to_doc_reader() -> None:
    assistant = Assistant(RecordingModelProvider(), StubResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="这个 PDF 前两页讲了什么 https://my.feishu.cn/docx/docx123",
    )

    await assistant.handle(request)

    assert request.resource_refs[0].range_hint == "pdf:first:2"


@pytest.mark.asyncio
async def test_assistant_passes_exact_pdf_page_hint_to_doc_reader() -> None:
    assistant = Assistant(RecordingModelProvider(), StubResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="读取 PDF 第 3 页 https://my.feishu.cn/docx/docx123",
    )

    await assistant.handle(request)

    assert request.resource_refs[0].range_hint == "pdf:page:3"


@pytest.mark.asyncio
async def test_assistant_reads_feishu_doc_resources_before_model() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, StubResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="总结 https://docs.feishu.cn/docx/docx123",
    )

    response = await assistant.handle(request)

    assert response.text == "ok"
    assert len(request.resource_results) == 1
    assert request.resource_results[0].content == "ou_user: 资源正文"
    assert provider.requests[0].resource_results[0].title == "测试资源"


@pytest.mark.asyncio
async def test_assistant_searches_user_visible_feishu_resources_on_demand() -> None:
    provider = RecordingModelProvider()
    searcher = StubResourceSearcher()
    assistant = Assistant(
        provider,
        StubResourceReader(),
        searcher,
        resource_search_limit=5,
        resource_search_read_limit=1,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我搜索飞书文档 项目计划",
    )

    await assistant.handle(request)

    assert searcher.calls == [("项目计划", "ou_user", 5)]
    assert request.resource_urls == [
        "https://my.feishu.cn/docx/docx123",
        "https://my.feishu.cn/sheets/sht123",
    ]
    assert len(request.resource_results) == 1
    assert request.resource_results[0].ref.token == "docx123"
    assert provider.requests[0].resource_results[0].content == "ou_user: 资源正文"


@pytest.mark.asyncio
async def test_assistant_treats_find_document_as_resource_search_intent() -> None:
    provider = RecordingModelProvider()
    searcher = StubResourceSearcher()
    assistant = Assistant(provider, StubResourceReader(), searcher)
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我找一篇 情感调优 的文档",
    )

    await assistant.handle(request)

    assert searcher.calls == [("情感调优", "ou_user", 5)]
    assert provider.requests


@pytest.mark.asyncio
async def test_assistant_uses_model_resource_search_plan_for_fuzzy_request() -> None:
    provider = RecordingModelProvider()
    searcher = StubResourceSearcher()
    planner = StubResourceSearchPlanner(
        ResourceSearchPlan(
            should_search=True,
            queries=["情感调优 无声对白 分镜", "情感调优"],
            resource_types=["wiki", "doc"],
        )
    )
    assistant = Assistant(
        provider,
        StubResourceReader(),
        searcher,
        resource_search_planner=planner,
        resource_search_limit=5,
        resource_search_read_limit=1,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我找那篇无声对白",
    )

    await assistant.handle(request)

    assert planner.requests == [request]
    assert searcher.calls == [
        ("情感调优 无声对白 分镜", "ou_user", 5),
        ("情感调优", "ou_user", 5),
    ]
    assert request.resource_urls == [
        "https://my.feishu.cn/docx/docx123",
        "https://my.feishu.cn/sheets/sht123",
    ]
    assert provider.requests[0].resource_results[0].content == "ou_user: 资源正文"


@pytest.mark.asyncio
async def test_assistant_audits_resource_search_plan_and_outcome_without_raw_query() -> None:
    provider = RecordingModelProvider()
    searcher = StubResourceSearcher()
    planner = StubResourceSearchPlanner(
        ResourceSearchPlan(
            should_search=True,
            queries=["情感调优 无声对白 分镜", "情感调优"],
            resource_types=["doc", "wiki", "doc"],
            constraints=["标题相关"],
            reason="用户要求查找文档",
        )
    )
    audit = RecordingAuditRecorder()
    assistant = Assistant(
        provider,
        StubResourceReader(),
        searcher,
        resource_search_planner=planner,
        audit_recorder=audit,
        resource_search_limit=5,
        resource_search_read_limit=1,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我找那篇无声对白",
    )

    await assistant.handle(request)

    assert [event[0] for event in audit.events] == [
        AuditEventType.AGENT_TOOL_PLANNED,
        AuditEventType.AGENT_TOOL_EXECUTED,
    ]
    planned = audit.events[0][2]
    assert planned == {
        "tool": "resource_search",
        "should_search": True,
        "query_count": 2,
        "query_lengths": [len("情感调优 无声对白 分镜"), len("情感调优")],
        "resource_types": ["doc", "wiki"],
        "constraint_count": 1,
        "reason_length": 8,
    }
    executed = audit.events[1][2]
    assert executed["result_count"] == 2
    assert executed["result_types"] == {
        ResourceType.FEISHU_DOC.value: 1,
        ResourceType.FEISHU_SHEET.value: 1,
    }
    assert "情感调优" not in str(planned)
    assert audit.events[0][1] == "ou_user"


@pytest.mark.asyncio
async def test_assistant_audits_resource_search_planner_failure_without_raw_error() -> None:
    provider = RecordingModelProvider()
    searcher = StubResourceSearcher()
    audit = RecordingAuditRecorder()
    assistant = Assistant(
        provider,
        StubResourceReader(),
        searcher,
        resource_search_planner=FailingResourceSearchPlanner(),
        audit_recorder=audit,
        resource_search_limit=5,
        resource_search_read_limit=1,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我搜索飞书文档 项目计划",
    )

    await assistant.handle(request)

    assert searcher.calls == [("项目计划", "ou_user", 5)]
    assert audit.events[0][0] == AuditEventType.ERROR
    assert audit.events[0][2] == {
        "kind": "resource_search_planner_failed",
        "tool": "resource_search",
        "error_type": "RuntimeError",
        "error_length": len("planner failed with sensitive query 情感调优"),
        "fallback_used": True,
    }
    assert "情感调优" not in str(audit.events[0][2])


@pytest.mark.asyncio
async def test_assistant_searches_each_planned_topic_with_fair_limit() -> None:
    provider = RecordingModelProvider()
    searcher = QuerySpecificResourceSearcher()
    planner = StubResourceSearchPlanner(
        ResourceSearchPlan(
            should_search=True,
            queries=["蓝色火箭测试", "情感调优", "Ozon"],
        )
    )
    assistant = Assistant(
        provider,
        StubResourceReader(),
        searcher,
        resource_search_planner=planner,
        resource_search_limit=2,
        resource_search_read_limit=1,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="1是蓝色火箭测试，2.情感调优相关文档，3，Ozon相关文档的链接都发我",
    )

    await assistant.handle(request)

    assert searcher.calls == [
        ("蓝色火箭测试", "ou_user", 2),
        ("情感调优", "ou_user", 2),
        ("Ozon", "ou_user", 2),
    ]
    assert len(request.resource_urls) == 6


@pytest.mark.asyncio
async def test_assistant_supplements_link_summary_search_queries_from_context() -> None:
    provider = RecordingModelProvider()
    searcher = QuerySpecificResourceSearcher()
    planner = StubResourceSearchPlanner(ResourceSearchPlan(should_search=False))
    assistant = Assistant(
        provider,
        StubResourceReader(),
        searcher,
        resource_search_planner=planner,
        resource_search_limit=1,
        resource_search_read_limit=1,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="把相关的文档链接汇总发我一下",
        chat_context_messages=[
            ChatContextMessage(
                message_id="om-summary",
                sender_id="assistant",
                text=(
                    "1. **“情感调优”文档搜索**：多次请求均未找到。\n"
                    "2. **“蓝色火箭”相关测试**：成功定位到包含编码的文档。\n"
                    "3. **Ozon项目搜索**：仅找到无标题资源链接。"
                ),
                created_at="2026-06-12T10:34:23+00:00",
            )
        ],
    )

    await assistant.handle(request)

    assert searcher.calls == [
        ("情感调优", "ou_user", 1),
        ("蓝色火箭", "ou_user", 1),
        ("Ozon", "ou_user", 1),
    ]
    assert len(request.resource_urls) == 3


@pytest.mark.asyncio
async def test_assistant_respects_model_resource_search_plan_no_search() -> None:
    provider = RecordingModelProvider()
    searcher = StubResourceSearcher()
    planner = StubResourceSearchPlanner(ResourceSearchPlan(should_search=False))
    assistant = Assistant(
        provider,
        StubResourceReader(),
        searcher,
        resource_search_planner=planner,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我搜索飞书文档 项目计划",
    )

    await assistant.handle(request)

    assert searcher.calls == []
    assert request.resource_results == []
    assert provider.requests[0].resource_results == []


@pytest.mark.asyncio
async def test_assistant_rewrites_model_generated_docs_feishu_links_to_current_ref_url() -> None:
    searcher = StubResourceSearcher()
    assistant = Assistant(
        DocsFeishuLinkModelProvider(),
        StubResourceReader(),
        searcher,
        resource_search_limit=5,
        resource_search_read_limit=1,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我搜索飞书文档 项目计划",
    )

    response = await assistant.handle(request)

    assert "https://my.feishu.cn/docx/docx123" in response.text
    assert "https://docs.feishu.cn/docx/docx123" not in response.text


@pytest.mark.asyncio
async def test_assistant_replaces_untrusted_model_feishu_links_with_real_search_results() -> None:
    searcher = StubResourceSearcher()
    assistant = Assistant(
        UnknownFeishuLinkModelProvider(),
        StubResourceReader(),
        searcher,
        resource_search_limit=5,
        resource_search_read_limit=1,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我找一篇 情感调优 的文档",
    )

    response = await assistant.handle(request)

    assert "ABC123" not in response.text
    assert "我只找到了以下真实可读取的飞书结果" in response.text
    assert "https://my.feishu.cn/docx/docx123" in response.text


@pytest.mark.asyncio
async def test_assistant_blocks_untrusted_model_feishu_links_without_search_results() -> None:
    assistant = Assistant(UnknownFeishuLinkModelProvider(), StubResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="随便聊聊",
    )

    response = await assistant.handle(request)

    assert "ABC123" not in response.text
    assert "https://docs.feishu.cn/docx/ABC123" not in response.text
    assert "无法确认的飞书链接" in response.text
    assert "安全策略拦截" in response.text


@pytest.mark.asyncio
async def test_assistant_does_not_search_group_messages_without_explicit_links() -> None:
    provider = RecordingModelProvider()
    searcher = StubResourceSearcher()
    assistant = Assistant(provider, StubResourceReader(), searcher)
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.GROUP,
        text="帮我搜索飞书文档 项目计划",
    )

    await assistant.handle(request)

    assert searcher.calls == []
    assert request.resource_results == []


@pytest.mark.asyncio
async def test_assistant_reports_empty_resource_search_to_model() -> None:
    provider = RecordingModelProvider()
    audit = RecordingAuditRecorder()
    assistant = Assistant(
        provider,
        StubResourceReader(),
        EmptyResourceSearcher(),
        audit_recorder=audit,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="查找飞书资料 不存在的主题",
    )

    response = await assistant.handle(request)

    assert request.resource_results[0].title == "飞书资料搜索"
    assert request.resource_results[0].error == "没有搜索到匹配的飞书文档、电子表格或多维表格。"
    assert response.text == "没有搜索到匹配的飞书文档、电子表格或多维表格。"
    assert provider.requests == []
    assert audit.events[-1][0] == AuditEventType.AGENT_TOOL_EXECUTED
    assert audit.events[-1][2]["result_count"] == 0
    assert audit.events[-1][2]["has_error"] is False


@pytest.mark.asyncio
async def test_assistant_reads_web_resources_before_model() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, StubResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="总结 https://example.com/page",
    )

    await assistant.handle(request)

    assert len(request.resource_results) == 1
    assert request.resource_refs[0].type == ResourceType.WEB
    assert provider.requests[0].resource_results[0].content == "ou_user: 资源正文"


@pytest.mark.asyncio
async def test_assistant_reads_feishu_sheet_resources_before_model() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, StubResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="分析 https://docs.feishu.cn/sheets/sht123?sheet=sheet1",
    )

    await assistant.handle(request)

    assert len(request.resource_results) == 1
    assert request.resource_refs[0].type == ResourceType.FEISHU_SHEET
    assert provider.requests[0].resource_results[0].content == "ou_user: 资源正文"


@pytest.mark.asyncio
async def test_assistant_reads_feishu_bitable_resources_before_model() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, StubResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="分析 https://docs.feishu.cn/base/app123?table=tbl1",
    )

    await assistant.handle(request)

    assert len(request.resource_results) == 1
    assert request.resource_refs[0].type == ResourceType.FEISHU_BITABLE
    assert provider.requests[0].resource_results[0].content == "ou_user: 资源正文"


@pytest.mark.asyncio
async def test_assistant_infers_doc_append_proposal_for_explicit_writeback() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, pending_action_ttl_seconds=300)
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请把“FLGO 写回测试成功”写入这个文档：https://docs.feishu.cn/docx/docx123",
    )

    response = await assistant.handle(request)

    assert "我已准备好写回预览，请在卡片中确认后执行。" in response.text
    assert len(response.action_proposals) == 1
    proposal = response.action_proposals[0]
    assert proposal.actor_id == "ou_user"
    assert proposal.action_type.value == "doc_append"
    assert proposal.target == {"document_id": "docx123"}
    assert proposal.payload == {"content": "FLGO 写回测试成功"}
    assert (proposal.expires_at - proposal.created_at).total_seconds() == 300


@pytest.mark.asyncio
async def test_assistant_does_not_create_writeback_proposals_when_disabled() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, enable_writeback=False)
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请把“FLGO 写回测试成功”写入这个文档：https://docs.feishu.cn/docx/docx123",
    )

    response = await assistant.handle(request)

    assert response.action_proposals == []


@pytest.mark.asyncio
async def test_assistant_uses_resolved_wiki_doc_token_for_writeback() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, ResolvingWikiResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请把“FLGO 写回测试成功”写入这个文档：https://docs.feishu.cn/wiki/wiki123",
    )

    response = await assistant.handle(request)

    assert len(response.action_proposals) == 1
    proposal = response.action_proposals[0]
    assert proposal.target == {"document_id": "docx_from_wiki"}
    assert proposal.target_title == "Wiki 文档"
    assert proposal.target_url == "https://docs.feishu.cn/wiki/wiki123"


@pytest.mark.asyncio
async def test_assistant_understands_write_a_sentence_to_named_document() -> None:
    planner = StubResourceSearchPlanner(
        ResourceSearchPlan(
            should_search=True,
            queries=["测试文档"],
            reason="named write target",
        )
    )
    assistant = Assistant(
        RecordingModelProvider(),
        StubResourceReader(),
        StubResourceSearcher(),
        resource_search_planner=planner,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我写一句“正在测试的文字”到测试文档里",
    )

    response = await assistant.handle(request)

    assert len(response.action_proposals) == 1
    assert response.action_proposals[0].action_type == WriteActionType.DOC_APPEND
    assert response.action_proposals[0].target == {"document_id": "docx123"}
    assert response.action_proposals[0].target_title == "测试资源"
    assert response.action_proposals[0].target_url == "https://my.feishu.cn/docx/docx123"
    assert response.action_proposals[0].payload == {"content": "正在测试的文字"}


@pytest.mark.asyncio
async def test_assistant_rejects_doc_write_before_named_embedded_file() -> None:
    planner = StubResourceSearchPlanner(
        ResourceSearchPlan(
            should_search=True,
            queries=["测试文档"],
            reason="named write target",
        )
    )
    assistant = Assistant(
        RecordingModelProvider(),
        DocWithEmbeddedAssetReader(),
        StubResourceSearcher(),
        resource_search_planner=planner,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我写一句“卡片测试成功M2”到测试文档里《Claude Code登录指引》这个文件的前面一行",
    )

    response = await assistant.handle(request)

    assert response.action_proposals == []
    assert "当前版本还不能稳定写入文档中间位置" in response.text
    assert "文档开头或文档末尾" in response.text


@pytest.mark.asyncio
async def test_assistant_rejects_doc_write_after_named_embedded_file() -> None:
    planner = StubResourceSearchPlanner(
        ResourceSearchPlan(
            should_search=True,
            queries=["测试文档"],
            reason="named write target",
        )
    )
    assistant = Assistant(
        RecordingModelProvider(),
        StubResourceReader(),
        StubResourceSearcher(),
        resource_search_planner=planner,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我写一句“卡片测试成功M3”到测试文档里《Claude Code登录指引》这个文件的后面",
    )

    response = await assistant.handle(request)

    assert response.action_proposals == []
    assert "当前版本还不能稳定写入文档中间位置" in response.text
    assert "未指定位置时默认追加到文档末尾" in response.text


@pytest.mark.asyncio
async def test_assistant_uses_current_quoted_doc_content_over_model_history() -> None:
    planner = StubResourceSearchPlanner(
        ResourceSearchPlan(
            should_search=True,
            queries=["测试文档"],
            reason="named write target",
        )
    )
    assistant = Assistant(
        DocConfirmationQuestionProvider(),
        StubResourceReader(),
        StubResourceSearcher(),
        resource_search_planner=planner,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我写一句“卡片测试成功”到测试文档末尾",
        chat_context_messages=[
            ChatContextMessage(
                message_id="om_old",
                sender_id="ou_user",
                text="帮我写一句“正在测试的文字”到测试文档里",
                created_at="2026-06-25T08:00:00+00:00",
            )
        ],
    )

    response = await assistant.handle(request)

    assert len(response.action_proposals) == 1
    proposal = response.action_proposals[0]
    assert proposal.payload == {"content": "卡片测试成功"}
    assert "正在测试的文字" not in proposal.preview
    assert response.text == "我已准备好写回预览，请在卡片中确认后执行。"


@pytest.mark.asyncio
async def test_assistant_prioritizes_named_writeback_target_over_planner_query() -> None:
    planner = StubResourceSearchPlanner(
        ResourceSearchPlan(
            should_search=True,
            queries=["卡片测试成功123"],
            reason="bad model query",
        )
    )
    searcher = TitledDocResourceSearcher()
    assistant = Assistant(
        DocConfirmationQuestionProvider(),
        NoTitleResourceReader(),
        searcher,
        resource_search_planner=planner,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="帮我写一句“卡片测试成功123”到测试文档末尾",
        chat_context_messages=[
            ChatContextMessage(
                message_id="om_old",
                sender_id="ou_user",
                text="不是是卡片测试成功",
                created_at="2026-06-25T08:00:00+00:00",
            )
        ],
    )

    response = await assistant.handle(request)

    assert searcher.calls[0][0] == "测试文档"
    assert len(response.action_proposals) == 1
    proposal = response.action_proposals[0]
    assert proposal.payload == {"content": "卡片测试成功123"}
    assert proposal.target_title == "测试文档"
    assert proposal.target_url == "https://my.feishu.cn/docx/docx123"
    assert "不是是卡片测试成功" not in proposal.preview
    assert response.text == "我已准备好写回预览，请在卡片中确认后执行。"


@pytest.mark.asyncio
async def test_assistant_inherits_previous_write_request_for_position_clarification() -> None:
    planner = StubResourceSearchPlanner(
        ResourceSearchPlan(
            should_search=True,
            queries=["测试文档"],
            reason="continued write target",
        )
    )
    assistant = Assistant(
        RecordingModelProvider(),
        StubResourceReader(),
        StubResourceSearcher(),
        resource_search_planner=planner,
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="文章最开始",
        chat_context_messages=[
            ChatContextMessage(
                message_id="om_previous",
                sender_id="ou_user",
                text="帮我写一句“正在测试的文字”到测试文档里",
                created_at="2026-06-25T08:00:00+00:00",
            )
        ],
    )

    response = await assistant.handle(request)

    assert len(response.action_proposals) == 1
    proposal = response.action_proposals[0]
    assert proposal.target == {
        "document_id": "docx123",
        "block_id": "docx123",
        "index": 0,
    }
    assert proposal.target_title == "测试资源"
    assert proposal.target_url == "https://my.feishu.cn/docx/docx123"
    assert proposal.payload == {"content": "正在测试的文字"}
    assert "文档开头" in proposal.preview


@pytest.mark.asyncio
async def test_assistant_uses_model_generated_content_for_doc_writeback() -> None:
    assistant = Assistant(DocGeneratedContentProvider())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请写一段你的自我介绍并写入这个文档：https://docs.feishu.cn/docx/docx123",
    )

    response = await assistant.handle(request)

    proposal = response.action_proposals[0]
    assert proposal.action_type == WriteActionType.DOC_APPEND
    assert proposal.payload["content"].startswith("我是 FLGO，一个接入飞书的工作助手")
    assert proposal.payload["content"] != "写一段你的自我介绍"
    assert "向文档追加文本：\n我是 FLGO" in proposal.preview


@pytest.mark.asyncio
async def test_assistant_uses_explicit_value_not_instruction_for_doc_writeback() -> None:
    assistant = Assistant(DocPreviewInstructionBeforeValueProvider())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请生成一段自我介绍并写入这个文档：https://docs.feishu.cn/docx/docx123",
    )

    response = await assistant.handle(request)

    proposal = response.action_proposals[0]
    assert proposal.action_type == WriteActionType.DOC_APPEND
    assert proposal.payload == {"content": "我是 FLGO，一个接入飞书的工作助手。"}
    assert "将以下模型生成内容写入指定文档" not in proposal.preview


@pytest.mark.asyncio
async def test_assistant_corrects_doc_action_content_from_model_preview() -> None:
    assistant = Assistant(DocWrongProposalWithGeneratedContentProvider())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请写一段你的自我介绍并写入这个文档：https://docs.feishu.cn/docx/docx123",
    )

    response = await assistant.handle(request)

    proposal = response.action_proposals[0]
    assert proposal.payload["content"].startswith("我是 FLGO，一个接入飞书的工作助手")
    assert proposal.payload["content"] != "写一段你的自我介绍"


@pytest.mark.asyncio
async def test_assistant_infers_sheet_write_proposal_for_explicit_writeback() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, ResolvingSheetResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="测试表格请把“FLGO 写回测试成功”写入这个excel表 https://docs.feishu.cn/sheets/sht123",
    )

    response = await assistant.handle(request)

    assert "我已准备好写回预览，请在卡片中确认后执行。" in response.text
    assert len(response.action_proposals) == 1
    proposal = response.action_proposals[0]
    assert proposal.action_type.value == "sheet_write_range"
    assert proposal.target == {"spreadsheet_token": "sht123", "range": "sheet1!A1:A1"}
    assert proposal.payload == {"values": [["FLGO 写回测试成功"]]}


@pytest.mark.asyncio
async def test_assistant_appends_sheet_write_to_next_row_by_default() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, NonEmptySheetResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请把“FLGO 写回测试删除成功2”写入这个excel表 https://docs.feishu.cn/sheets/sht123",
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].target == {
        "spreadsheet_token": "sht123",
        "range": "sheet1!A2:A2",
    }
    assert response.action_proposals[0].preview == (
        "向电子表格 sheet1!A2:A2 写入：FLGO 写回测试删除成功2"
    )


@pytest.mark.asyncio
async def test_assistant_respects_sheet_cell_named_in_message() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, NonEmptySheetResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "请把“FLGO 写回测试删除成功3”填入B1格 "
            "https://docs.feishu.cn/sheets/sht123"
        ),
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].target == {
        "spreadsheet_token": "sht123",
        "range": "sheet1!B1:B1",
    }
    assert response.action_proposals[0].preview == (
        "向电子表格 sheet1!B1:B1 写入：FLGO 写回测试删除成功3"
    )


@pytest.mark.asyncio
async def test_assistant_respects_sheet_cell_named_after_join_word() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, NonEmptySheetResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "测试表格请把“FLGO 写回测试删除成功4”加入这个excel表的c2格 "
            "https://docs.feishu.cn/sheets/sht123"
        ),
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].target == {
        "spreadsheet_token": "sht123",
        "range": "sheet1!C2:C2",
    }
    assert response.action_proposals[0].preview == (
        "向电子表格 sheet1!C2:C2 写入：FLGO 写回测试删除成功4"
    )


@pytest.mark.asyncio
async def test_assistant_respects_sheet_row_column_named_in_message() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, NonEmptySheetResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "测试表格请把“FLGO 写回测试删除成功5”加入这个excel表第3行第4列 "
            "https://docs.feishu.cn/sheets/sht123"
        ),
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].target == {
        "spreadsheet_token": "sht123",
        "range": "sheet1!D3:D3",
    }
    assert response.action_proposals[0].preview == (
        "向电子表格 sheet1!D3:D3 写入：FLGO 写回测试删除成功5"
    )


@pytest.mark.asyncio
async def test_assistant_respects_chinese_sheet_row_column_named_in_message() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, NonEmptySheetResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "测试表格请把“FLGO 写回测试删除成功5”加入这个excel表第三行第四列 "
            "https://docs.feishu.cn/sheets/sht123"
        ),
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].target == {
        "spreadsheet_token": "sht123",
        "range": "sheet1!D3:D3",
    }


@pytest.mark.asyncio
async def test_assistant_corrects_model_sheet_range_when_message_names_cell() -> None:
    assistant = Assistant(SheetWrongProposalModelProvider(), NonEmptySheetResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "测试表格请把“FLGO 写回测试删除成功4”加入这个excel表的c2格 "
            "https://docs.feishu.cn/sheets/sht123"
        ),
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].target == {
        "spreadsheet_token": "sht123",
        "range": "sheet1!C2:C2",
    }
    assert response.action_proposals[0].preview == (
        "向电子表格 sheet1!C2:C2 写入：FLGO 写回测试删除成功4"
    )


@pytest.mark.asyncio
async def test_assistant_corrects_model_sheet_range_when_message_names_row_column() -> None:
    assistant = Assistant(SheetWrongProposalModelProvider(), NonEmptySheetResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "测试表格请把“FLGO 写回测试删除成功5”加入这个excel表第3行第4列 "
            "https://docs.feishu.cn/sheets/sht123"
        ),
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].target == {
        "spreadsheet_token": "sht123",
        "range": "sheet1!D3:D3",
    }


@pytest.mark.asyncio
async def test_assistant_uses_model_text_sheet_target_before_default_append() -> None:
    assistant = Assistant(SheetModelTextTargetProvider(), NonEmptySheetResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "测试表格请把“FLGO 写回测试删除成功6”加入这个excel表 "
            "https://docs.feishu.cn/sheets/sht123"
        ),
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].target == {
        "spreadsheet_token": "sht123",
        "range": "sheet1!C4:C4",
    }
    assert response.action_proposals[0].preview == (
        "向电子表格 sheet1!C4:C4 写入：FLGO 写回测试删除成功6"
    )


@pytest.mark.asyncio
async def test_assistant_corrects_wrong_tool_range_from_model_text_target() -> None:
    assistant = Assistant(
        SheetWrongProposalWithModelTargetProvider(),
        NonEmptySheetResourceReader(),
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "测试表格请把“FLGO 写回测试删除成功6”加入这个excel表 "
            "https://docs.feishu.cn/sheets/sht123"
        ),
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].target == {
        "spreadsheet_token": "sht123",
        "range": "sheet1!C4:C4",
    }
    assert response.action_proposals[0].preview == (
        "向电子表格 sheet1!C4:C4 写入：FLGO 写回测试删除成功6"
    )


@pytest.mark.asyncio
async def test_assistant_uses_model_generated_content_for_sheet_writeback() -> None:
    assistant = Assistant(SheetGeneratedContentProvider(), NonEmptySheetResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "请生成一段摘要并加入这个excel表的C4格 "
            "https://docs.feishu.cn/sheets/sht123"
        ),
    )

    response = await assistant.handle(request)

    proposal = response.action_proposals[0]
    assert proposal.target == {"spreadsheet_token": "sht123", "range": "sheet1!C4:C4"}
    assert proposal.payload == {"values": [["这是模型生成的表格摘要内容。"]]}
    assert proposal.preview == "向电子表格 sheet1!C4:C4 写入：这是模型生成的表格摘要内容。"


@pytest.mark.asyncio
async def test_assistant_uses_explicit_value_not_instruction_for_sheet_writeback() -> None:
    assistant = Assistant(
        SheetPreviewInstructionBeforeValueProvider(),
        NonEmptySheetResourceReader(),
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "总结一下这个表格并把总结结果写到模型判断的位置 "
            "https://docs.feishu.cn/sheets/sht123"
        ),
    )

    response = await assistant.handle(request)

    proposal = response.action_proposals[0]
    assert proposal.action_type == WriteActionType.SHEET_WRITE_RANGE
    assert proposal.target == {"spreadsheet_token": "sht123", "range": "sheet1!D4:D4"}
    assert proposal.payload == {"values": [["该表为写回测试记录，包含4条删除成功测试项。"]]}
    assert "目标位置" not in proposal.preview


@pytest.mark.asyncio
async def test_assistant_appends_sheet_summary_when_user_requests_last_row() -> None:
    assistant = Assistant(SheetAppendSummaryProvider(), AppendRowFiveSheetResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "总结一下这个表格并把总结结果写到最后一行 "
            "https://docs.feishu.cn/sheets/sht123"
        ),
    )

    response = await assistant.handle(request)

    proposal = response.action_proposals[0]
    assert proposal.action_type == WriteActionType.SHEET_WRITE_RANGE
    assert proposal.target == {"spreadsheet_token": "sht123", "range": "sheet1!D4:D4"}
    assert proposal.payload == {
        "values": [["该表记录了 4 次写回测试删除成功的操作，其中第 4 次操作还包含一个关联数据。"]]
    }
    assert "目标位置" not in proposal.preview


@pytest.mark.asyncio
async def test_assistant_appends_sheet_summary_when_model_has_no_target() -> None:
    assistant = Assistant(
        SheetAppendSummaryWithoutTargetProvider(),
        AppendRowFiveSheetResourceReader(),
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "总结一下这个表格并把总结结果写到最后一行 "
            "https://docs.feishu.cn/sheets/sht123"
        ),
    )

    response = await assistant.handle(request)

    proposal = response.action_proposals[0]
    assert proposal.action_type == WriteActionType.SHEET_WRITE_RANGE
    assert proposal.target == {"spreadsheet_token": "sht123", "range": "sheet1!A5:A5"}
    assert proposal.payload == {
        "values": [["该表记录了 4 次写回测试删除成功的操作，其中第 4 次操作还包含一个关联数据。"]]
    }


@pytest.mark.asyncio
async def test_assistant_normalizes_single_cell_sheet_range_for_writeback() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, ExplicitRangeSheetResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "请把“FLGO 写回测试成功”写入这个表格 "
            "https://docs.feishu.cn/sheets/sht123?sheet=sheet1&range=B2"
        ),
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].target == {
        "spreadsheet_token": "sht123",
        "range": "sheet1!B2:B2",
    }


@pytest.mark.asyncio
async def test_assistant_respects_sheet_range_named_in_message() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, NonEmptySheetResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "请把“FLGO 写回测试成功”写入 B1:C1 范围 "
            "https://docs.feishu.cn/sheets/sht123"
        ),
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].target == {
        "spreadsheet_token": "sht123",
        "range": "sheet1!B1:C1",
    }


@pytest.mark.asyncio
async def test_assistant_does_not_append_sheet_for_replacement_without_range() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, NonEmptySheetResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请把这个表格里的旧内容替换成“新内容” https://docs.feishu.cn/sheets/sht123",
    )

    response = await assistant.handle(request)

    assert response.action_proposals == []


@pytest.mark.asyncio
async def test_assistant_does_not_append_doc_for_replacement_intent() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider)
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请把这个文档里的旧内容替换成“新内容” https://docs.feishu.cn/docx/docx123",
    )

    response = await assistant.handle(request)

    assert response.action_proposals == []


@pytest.mark.asyncio
async def test_assistant_infers_bitable_create_record_proposal_for_explicit_writeback() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, ResolvingBitableResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请把“FLGO 写回测试成功”写入这个多维表 https://docs.feishu.cn/base/app123",
    )

    response = await assistant.handle(request)

    assert len(response.action_proposals) == 1
    proposal = response.action_proposals[0]
    assert proposal.action_type.value == "bitable_create_record"
    assert proposal.target == {"app_token": "app123", "table_id": "tbl1"}
    assert proposal.payload == {"fields": {"关键词": "FLGO 写回测试成功"}}


@pytest.mark.asyncio
async def test_assistant_does_not_create_bitable_record_for_update_intent() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, ResolvingBitableResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请把这个多维表已有记录更新为“新内容” https://docs.feishu.cn/base/app123",
    )

    response = await assistant.handle(request)

    assert response.action_proposals == []


@pytest.mark.asyncio
async def test_assistant_infers_bitable_update_from_model_text_and_record_index() -> None:
    assistant = Assistant(BitableUpdateModelTextProvider(), BitableRecordsWithIdsResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "把“测试数据”添加到 测试列1 的 测试列2行 "
            "https://docs.feishu.cn/base/app123"
        ),
    )

    response = await assistant.handle(request)

    assert "我已准备好写回预览，请在卡片中确认后执行。" in response.text
    assert len(response.action_proposals) == 1
    proposal = response.action_proposals[0]
    assert proposal.action_type == WriteActionType.BITABLE_UPDATE_RECORD
    assert proposal.target == {
        "app_token": "app123",
        "table_id": "tbl1",
        "record_id": "rec10",
    }
    assert proposal.payload == {"fields": {"测试列2": "测试数据"}}
    assert "匹配 测试内容 = 测试列1" in proposal.preview


@pytest.mark.asyncio
async def test_assistant_corrects_bitable_create_to_update_from_model_text() -> None:
    assistant = Assistant(
        BitableWrongCreateWithModelUpdateProvider(),
        BitableRecordsWithIdsResourceReader(),
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "把“测试数据”添加到 测试列1 的 测试列2行 "
            "https://docs.feishu.cn/base/app123"
        ),
    )

    response = await assistant.handle(request)

    proposal = response.action_proposals[0]
    assert proposal.action_type == WriteActionType.BITABLE_UPDATE_RECORD
    assert proposal.target == {
        "app_token": "app123",
        "table_id": "tbl1",
        "record_id": "rec10",
    }
    assert proposal.payload == {"fields": {"测试列2": "测试数据"}}


@pytest.mark.asyncio
async def test_assistant_uses_model_generated_content_for_bitable_update() -> None:
    assistant = Assistant(BitableGeneratedUpdateProvider(), BitableRecordsWithIdsResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "请生成测试数据并添加到 测试列1 的 测试列2行 "
            "https://docs.feishu.cn/base/app123"
        ),
    )

    response = await assistant.handle(request)

    proposal = response.action_proposals[0]
    assert proposal.action_type == WriteActionType.BITABLE_UPDATE_RECORD
    assert proposal.target == {
        "app_token": "app123",
        "table_id": "tbl1",
        "record_id": "rec10",
    }
    assert proposal.payload == {"fields": {"测试列2": "这是模型生成后的测试数据。"}}


@pytest.mark.asyncio
async def test_assistant_uses_explicit_value_not_instruction_for_bitable_update() -> None:
    assistant = Assistant(
        BitablePreviewInstructionBeforeValueProvider(),
        BitableRecordsWithIdsResourceReader(),
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "总结一下这个多维表 并把总结结果替换掉测试列2的最后一行 "
            "https://docs.feishu.cn/base/app123"
        ),
    )

    response = await assistant.handle(request)

    proposal = response.action_proposals[0]
    assert proposal.action_type == WriteActionType.BITABLE_UPDATE_RECORD
    assert proposal.target == {
        "app_token": "app123",
        "table_id": "tbl1",
        "record_id": "rec10",
    }
    assert proposal.payload == {
        "fields": {"测试列2": "该多维表共10条记录，主要用于FLGO写回测试功能验证。"}
    }
    assert "record_id" not in proposal.payload["fields"]["测试列2"]
    assert "覆盖原有值" not in proposal.payload["fields"]["测试列2"]


@pytest.mark.asyncio
async def test_assistant_uses_markdown_table_new_value_for_bitable_update() -> None:
    assistant = Assistant(
        BitableMarkdownTablePreviewProvider(),
        BitableRecordsWithIdsResourceReader(),
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "总结一下这个多维表 并把总结结果替换掉测试列2的最后一行 "
            "https://docs.feishu.cn/base/app123"
        ),
    )

    response = await assistant.handle(request)

    assert "我已准备好写回预览，请在卡片中确认后执行。" in response.text
    assert len(response.action_proposals) == 1
    proposal = response.action_proposals[0]
    assert proposal.action_type == WriteActionType.BITABLE_UPDATE_RECORD
    assert proposal.target == {
        "app_token": "app123",
        "table_id": "tbl1",
        "record_id": "rec10",
    }
    assert proposal.payload == {
        "fields": {
            "测试列2": (
                "该多维表共10条记录，主要记录FLGO写回测试结果，其中8条为"
                '"FLGO 写回测试成功"，1条为"FLGO 测试成功"，1条为'
                '"测试列1内容"。最后一条的测试列2原值为"测试数据"。整体为功能验证用途。'
            )
        }
    }


@pytest.mark.asyncio
async def test_assistant_uses_model_crud_json_plan_without_keyword_matching() -> None:
    assistant = Assistant(
        BitableCrudJsonPlanProvider(),
        BitableRecordsWithIdsResourceReader(),
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请处理这个多维表 https://docs.feishu.cn/base/app123",
    )

    response = await assistant.handle(request)

    assert len(response.action_proposals) == 1
    proposal = response.action_proposals[0]
    assert proposal.action_type == WriteActionType.BITABLE_CREATE_RECORD
    assert proposal.target == {"app_token": "app123", "table_id": "tbl1"}
    assert proposal.payload == {"fields": {"测试列2": "模型结构化 CRUD 计划写入值"}}
    assert "flgo_writeback" not in response.text


@pytest.mark.asyncio
async def test_assistant_creates_bitable_record_when_user_requests_last_row_summary() -> None:
    assistant = Assistant(BitableAppendSummaryProvider(), BitableRecordsWithIdsResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "总结一下这个多维表 并把总结结果写到表的最后一行 "
            "https://docs.feishu.cn/base/app123"
        ),
    )

    response = await assistant.handle(request)

    proposal = response.action_proposals[0]
    assert proposal.action_type == WriteActionType.BITABLE_CREATE_RECORD
    assert proposal.target == {"app_token": "app123", "table_id": "tbl1"}
    assert proposal.payload == {
        "fields": {
            "测试内容": (
                "该多维表格共 10 条记录，主要记录 FLGO 写回测试结果，"
                "其中 8 条为“FLGO 写回测试成功”。整体为功能验证用途。"
            )
        }
    }
    assert "待写内容预览" not in proposal.preview
    assert "操作" not in proposal.payload["fields"]["测试内容"]


@pytest.mark.asyncio
async def test_assistant_creates_bitable_record_for_increase_to_field_last_row() -> None:
    assistant = Assistant(
        BitableIncreaseToFieldLastRowProvider(),
        BitableRecordsWithIdsResourceReader(),
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "总结一下这个多维表 并把总结结果增加到测试列2的最后一行 "
            "https://docs.feishu.cn/base/app123"
        ),
    )

    response = await assistant.handle(request)

    assert "我已准备好写回预览，请在卡片中确认后执行。" in response.text
    assert len(response.action_proposals) == 1
    proposal = response.action_proposals[0]
    assert proposal.action_type == WriteActionType.BITABLE_CREATE_RECORD
    assert proposal.target == {"app_token": "app123", "table_id": "tbl1"}
    assert proposal.payload == {
        "fields": {
            "测试列2": (
                "该多维表格共10条记录，主要用于FLGO写回功能测试，"
                "其中7条记录测试内容为“FLGO写回测试成功”，1条为“FLGO测试成功”，"
                "1条记录含“测试列1内容”但测试列2数据异常，另有1条记录内容为无效预览文本。"
            )
        }
    }


@pytest.mark.asyncio
async def test_assistant_updates_bitable_last_row_when_user_requests_replace() -> None:
    assistant = Assistant(
        BitableReplaceLastRowSummaryProvider(),
        BitableRecordsWithIdsResourceReader(),
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "总结一下这个多维表 并把总结结果替换掉测试列2的最后一行 "
            "https://docs.feishu.cn/base/app123"
        ),
    )

    response = await assistant.handle(request)

    assert "我已准备好写回预览，请在卡片中确认后执行。" in response.text
    assert len(response.action_proposals) == 1
    proposal = response.action_proposals[0]
    assert proposal.action_type == WriteActionType.BITABLE_UPDATE_RECORD
    assert proposal.target == {
        "app_token": "app123",
        "table_id": "tbl1",
        "record_id": "rec10",
    }
    assert proposal.payload == {
        "fields": {
            "测试列2": (
                "该多维表格共10条记录，其中8条为“FLGO写回测试成功”，"
                "1条为“FLGO测试成功”，最后1条原含“测试数据”；整体为FLGO功能验证测试。"
            )
        }
    }
    assert "待写内容预览" not in proposal.preview
    assert "`" not in proposal.payload["fields"]["测试列2"]


@pytest.mark.asyncio
async def test_assistant_respects_model_update_operation_over_user_append_wording() -> None:
    assistant = Assistant(
        BitableNaturalUpdateFirstRowProvider(),
        BitableTenRecordsWithIdsResourceReader(),
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "总结一下这个多维表 并把总结结果增加到测试列2的第一行 "
            "https://docs.feishu.cn/base/app123"
        ),
    )

    response = await assistant.handle(request)

    assert "我已准备好写回预览，请在卡片中确认后执行。" in response.text
    assert len(response.action_proposals) == 1
    proposal = response.action_proposals[0]
    assert proposal.action_type == WriteActionType.BITABLE_UPDATE_RECORD
    assert proposal.target == {
        "app_token": "app123",
        "table_id": "tbl1",
        "record_id": "rec1",
    }
    assert proposal.payload == {
        "fields": {
            "测试列2": (
                "该多维表共 10 条记录，测试内容均为 FLGO 写回测试相关，"
                "测试列2仅最后一条含测试数据，其余为空。整体为功能验证测试。"
            )
        }
    }
    assert "新增记录" not in proposal.preview


@pytest.mark.asyncio
async def test_assistant_corrects_bitable_update_to_create_for_last_row_summary() -> None:
    assistant = Assistant(
        BitableWrongUpdateForAppendProvider(),
        BitableRecordsWithIdsResourceReader(),
    )
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "总结一下这个多维表 并把总结结果写到表的最后一行 "
            "https://docs.feishu.cn/base/app123"
        ),
    )

    response = await assistant.handle(request)

    proposal = response.action_proposals[0]
    assert proposal.action_type == WriteActionType.BITABLE_CREATE_RECORD
    assert proposal.target == {"app_token": "app123", "table_id": "tbl1"}
    assert proposal.payload == {
        "fields": {"测试内容": "该多维表格共 10 条记录，主要记录 FLGO 写回测试结果。"}
    }


@pytest.mark.asyncio
async def test_assistant_uses_matrix_header_field_for_bitable_writeback() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, MatrixHeaderBitableResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请把“FLGO 写回测试成功”写入这个多维表 https://docs.feishu.cn/base/app123",
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].payload == {"fields": {"文本": "FLGO 写回测试成功"}}


@pytest.mark.asyncio
async def test_assistant_uses_test_content_field_for_bitable_writeback() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, TestContentBitableResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请把“FLGO 写回测试成功”写入这个多维表 https://docs.feishu.cn/base/app123",
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].payload == {
        "fields": {"测试内容": "FLGO 写回测试成功"}
    }


@pytest.mark.asyncio
async def test_assistant_respects_explicit_bitable_field_request() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, ResolvingBitableResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "请把“FLGO 写回测试成功”写入这个多维表的标题字段 "
            "https://docs.feishu.cn/base/app123"
        ),
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].payload == {"fields": {"标题": "FLGO 写回测试成功"}}


@pytest.mark.asyncio
async def test_assistant_selects_generic_text_like_bitable_field() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, MixedFieldBitableResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请把“FLGO 写回测试成功”写入这个多维表 https://docs.feishu.cn/base/app123",
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].payload == {"fields": {"客户需求": "FLGO 写回测试成功"}}


@pytest.mark.asyncio
async def test_assistant_scores_header_fields_without_type_metadata() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, HeaderOnlyMixedBitableResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请把“FLGO 写回测试成功”写入这个多维表 https://docs.feishu.cn/base/app123",
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].payload == {"fields": {"项目说明": "FLGO 写回测试成功"}}


@pytest.mark.asyncio
async def test_assistant_respects_explicit_lower_scored_bitable_field() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, MixedFieldBitableResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text=(
            "请把“2026-06-02”写入这个多维表的日期字段 "
            "https://docs.feishu.cn/base/app123"
        ),
    )

    response = await assistant.handle(request)

    assert response.action_proposals[0].payload == {"fields": {"日期": "2026-06-02"}}


@pytest.mark.asyncio
async def test_assistant_does_not_create_bitable_writeback_without_known_fields() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider, NoFieldBitableResourceReader())
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请把“FLGO 写回测试成功”写入这个多维表 https://docs.feishu.cn/base/app123",
    )

    response = await assistant.handle(request)

    assert response.action_proposals == []


@pytest.mark.asyncio
async def test_assistant_does_not_infer_writeback_without_quoted_content() -> None:
    provider = RecordingModelProvider()
    assistant = Assistant(provider)
    request = AssistantRequest(
        actor_id="ou_user",
        conversation_id="chat-1",
        conversation_type=ConversationType.PRIVATE,
        text="请总结这个文档：https://docs.feishu.cn/docx/docx123",
    )

    response = await assistant.handle(request)

    assert response.action_proposals == []
