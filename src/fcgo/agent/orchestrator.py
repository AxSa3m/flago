import json
import logging
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import ValidationError

from fcgo.agent.context_builder import build_agent_observations, render_agent_observations
from fcgo.agent.protocols import ResourceReader, ResourceSearcher
from fcgo.agent.tools import ToolExecutionContext, ToolRegistry, default_tool_registry
from fcgo.model_providers.types import (
    ModelMessage,
    ModelMessageRole,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ProviderCapability,
)
from fcgo.models import (
    AgentDecision,
    AssistantRequest,
    AssistantResponse,
    AuditEventType,
    ToolCall,
    ToolName,
    ToolResult,
    WritebackConfirmationMode,
    WritebackProposalRequest,
)
from fcgo.resources.parser import parse_resource_urls

logger = logging.getLogger(__name__)


class AgentModelProvider(Protocol):
    async def generate_model(self, request: ModelRequest) -> ModelResponse:
        """Generate a provider-agnostic model response."""


class AgentOrchestrator:
    def __init__(
        self,
        model_provider: AgentModelProvider,
        resource_reader: ResourceReader | None = None,
        resource_searcher: ResourceSearcher | None = None,
        *,
        tool_registry: ToolRegistry | None = None,
        audit_recorder: Any | None = None,
        store: Any | None = None,
        model_router: Any | None = None,
        pending_action_ttl_seconds: int = 1800,
        enable_writeback: bool = True,
        writeback_confirmation_mode: WritebackConfirmationMode = WritebackConfirmationMode.ALWAYS,
        max_steps: int = 4,
    ) -> None:
        self.model_provider = model_provider
        self.resource_reader = resource_reader
        self.resource_searcher = resource_searcher
        self.tool_registry = tool_registry or default_tool_registry()
        self.audit_recorder = audit_recorder
        self.store = store
        self.model_router = model_router
        self.pending_action_ttl_seconds = pending_action_ttl_seconds
        self.enable_writeback = enable_writeback
        self.writeback_confirmation_mode = writeback_confirmation_mode
        self.max_steps = max(max_steps, 1)

    async def handle(self, request: AssistantRequest) -> AssistantResponse:
        request.writeback_enabled = self.enable_writeback
        _inject_message_links(request)
        tool_results: list[ToolResult] = []
        context = ToolExecutionContext(
            actor_id=request.actor_id,
            conversation_type=request.conversation_type,
            writeback_enabled=self.enable_writeback,
            writeback_confirmation_mode=self.writeback_confirmation_mode,
            request=request,
            resource_reader=self.resource_reader,
            resource_searcher=self.resource_searcher,
            store=self.store,
            model_router=self.model_router or self.model_provider,
        )

        for _ in range(self.max_steps):
            decision, parse_error = await self._next_decision(
                request,
                tool_results=tool_results,
                repair=True,
            )
            if decision is None:
                return AssistantResponse(text=_parse_failure_text(parse_error))
            if decision.tool_calls:
                executed = await self._execute_tool_calls(decision.tool_calls, context)
                tool_results.extend(executed)
                writeback = self._writeback_response_from_tool_results(
                    request,
                    decision.final_response,
                    executed,
                )
                if writeback is not None:
                    return writeback
                continue
            if decision.writeback_drafts:
                return await self._writeback_response_from_drafts(
                    request,
                    decision.final_response,
                    decision.writeback_drafts,
                    context,
                )
            if decision.final_response and decision.final_response.strip():
                return AssistantResponse(text=decision.final_response.strip())
            return AssistantResponse(text="我没有得到可执行的下一步或可回复内容。")

        return AssistantResponse(text=_max_steps_text(tool_results))

    async def _next_decision(
        self,
        request: AssistantRequest,
        *,
        tool_results: list[ToolResult],
        repair: bool,
    ) -> tuple[AgentDecision | None, str]:
        response = await self.model_provider.generate_model(
            _build_agent_model_request(
                request,
                tool_specs=[
                    spec.model_dump(mode="json") for spec in self.tool_registry.specs()
                ],
                tool_results=tool_results,
            )
        )
        native_decision = _agent_decision_from_native_tool_calls(response.tool_calls)
        if native_decision is not None:
            return native_decision, ""
        decision, error = _parse_agent_decision(response.text)
        if decision is not None or not repair:
            return decision, error
        repair_response = await self.model_provider.generate_model(
            _build_repair_model_request(
                request,
                invalid_output=response.text,
                parse_error=error,
                tool_specs=[
                    spec.model_dump(mode="json") for spec in self.tool_registry.specs()
                ],
                tool_results=tool_results,
            )
        )
        return _parse_agent_decision(repair_response.text)

    async def _execute_tool_calls(
        self,
        tool_calls: Sequence[ToolCall],
        context: ToolExecutionContext,
    ) -> list[ToolResult]:
        results: list[ToolResult] = []
        for call in tool_calls:
            await self._audit_tool_planned(call)
            result = await self.tool_registry.execute(
                call_id=call.id,
                name=call.name,
                arguments=call.arguments,
                context=context,
            )
            await self._audit_tool_executed(result)
            results.append(result)
        return results

    async def _writeback_response_from_drafts(
        self,
        request: AssistantRequest,
        text: str | None,
        drafts: list[Any],
        context: ToolExecutionContext,
    ) -> AssistantResponse:
        tool_result = await self.tool_registry.execute(
            call_id="writeback-drafts",
            name=ToolName.PREPARE_WRITEBACK,
            arguments=WritebackProposalRequest(proposals=drafts).model_dump(mode="json"),
            context=context,
        )
        if not tool_result.ok:
            return AssistantResponse(text=tool_result.user_message or tool_result.error or "")
        return self._proposal_response_from_tool_result(request, text, tool_result)

    def _writeback_response_from_tool_results(
        self,
        request: AssistantRequest,
        text: str | None,
        results: list[ToolResult],
    ) -> AssistantResponse | None:
        for result in results:
            if result.name == ToolName.PREPARE_WRITEBACK:
                if not result.ok:
                    return AssistantResponse(
                        text=result.user_message or result.error or "写回草案无法生成。"
                    )
                return self._proposal_response_from_tool_result(request, text, result)
        return None

    def _proposal_response_from_tool_result(
        self,
        request: AssistantRequest,
        text: str | None,
        result: ToolResult,
    ) -> AssistantResponse:
        proposals = [
            draft.to_proposal(
                actor_id=request.actor_id,
                ttl_seconds=self.pending_action_ttl_seconds,
            )
            for draft in WritebackProposalRequest(
                proposals=result.content.get("writeback_drafts", [])
            ).proposals
        ]
        return AssistantResponse(
            text=(text or "我已准备好写回预览，请在卡片中确认后执行。").strip(),
            action_proposals=proposals,
        )

    async def _audit_tool_planned(self, call: ToolCall) -> None:
        if self.audit_recorder is None:
            return
        await self.audit_recorder.audit(
            AuditEventType.AGENT_TOOL_PLANNED,
            detail={
                "tool": call.name.value,
                "call_id": call.id,
                "argument_keys": sorted(call.arguments),
            },
        )

    async def _audit_tool_executed(self, result: ToolResult) -> None:
        if self.audit_recorder is None:
            return
        await self.audit_recorder.audit(
            AuditEventType.AGENT_TOOL_EXECUTED,
            detail={
                "tool": result.name.value,
                "call_id": result.call_id,
                "ok": result.ok,
                "error": result.error,
                "audit_metadata": result.audit_metadata,
            },
        )


def _build_agent_model_request(
    request: AssistantRequest,
    *,
    tool_specs: list[dict[str, Any]],
    tool_results: list[ToolResult],
) -> ModelRequest:
    return ModelRequest(
        request_id=request.request_id,
        provider=request.model_provider,
        model=request.model,
        required_capabilities=[ProviderCapability.CHAT],
        temperature=0,
        messages=[
            ModelMessage(
                role=ModelMessageRole.SYSTEM,
                content=_system_prompt(request, tool_specs),
            ),
            ModelMessage(
                role=ModelMessageRole.USER,
                content=_user_prompt(request, tool_results),
            ),
        ],
        tools=tool_specs,
        tool_choice="auto",
        metadata={
            "actor_id": request.actor_id,
            "conversation_id": request.conversation_id,
            "conversation_type": request.conversation_type.value,
            "agent_mode": "json_parser",
        },
    )


def _build_repair_model_request(
    request: AssistantRequest,
    *,
    invalid_output: str,
    parse_error: str,
    tool_specs: list[dict[str, Any]],
    tool_results: list[ToolResult],
) -> ModelRequest:
    prompt = (
        f"{_user_prompt(request, tool_results)}\n\n"
        "上一轮输出不是合法 AgentDecision JSON。\n"
        f"解析错误：{parse_error}\n"
        "请只返回修正后的 JSON，不要解释。\n\n"
        f"上一轮原始输出：\n{invalid_output[:4000]}"
    )
    return ModelRequest(
        request_id=request.request_id,
        provider=request.model_provider,
        model=request.model,
        required_capabilities=[ProviderCapability.CHAT],
        temperature=0,
        messages=[
            ModelMessage(
                role=ModelMessageRole.SYSTEM,
                content=_system_prompt(request, tool_specs),
            ),
            ModelMessage(role=ModelMessageRole.USER, content=prompt),
        ],
        tools=tool_specs,
        tool_choice="auto",
        metadata={
            "actor_id": request.actor_id,
            "conversation_id": request.conversation_id,
            "conversation_type": request.conversation_type.value,
            "agent_mode": "json_repair",
        },
    )


def _system_prompt(request: AssistantRequest, tool_specs: list[dict[str, Any]]) -> str:
    assistant_name = request.assistant_name.strip() or "小智"
    assistant_profile = request.assistant_profile.strip()
    decision_schema = AgentDecision.model_json_schema()
    lines = [
        f"你是 {assistant_name}，一个接入飞书的工作助手。",
    ]
    if assistant_profile:
        lines.append(f"你的语言风格和任务角色简介：{assistant_profile}")
    lines.extend(
        [
            "你必须像 Agent 一样先判断用户意图，再决定是否调用工具。",
            "每轮只能输出一个合法 JSON 对象，不要 Markdown，不要代码块，不要额外解释。",
            "JSON 必须符合 AgentDecision schema，只允许包含 final_response、"
            "tool_calls、writeback_drafts。",
            "如果需要真实飞书资源，先调用 search_resources/read_resource，"
            "不得编造结果、标题、URL 或 token。",
            "如果用户只是普通聊天或测试上下文，不要调用写入工具。",
            "提取写入正文时，只能使用当前用户消息，或 Observation 中标题为"
            "“最近待补充位置的写回请求”的内容。",
            "不要从旧确认卡片、旧助手回复、旧待写预览里继承写入正文。",
            "写入、修改、删除必须先输出 writeback_drafts 或调用 prepare_writeback；"
            "程序会生成确认卡片。",
            "当前只稳定支持写到文档开头或文档末尾；用户要求附件前后或文档中间位置时，直接说明暂不支持稳定写入中间位置。",
            "用户要简短回答时，final_response 保持简洁。",
            f"写回启用状态：{request.writeback_enabled}。",
            f"AgentDecision schema: {json.dumps(decision_schema, ensure_ascii=False)}",
            f"Available tools: {json.dumps(tool_specs, ensure_ascii=False)}",
        ]
    )
    return "\n".join(lines)


def _user_prompt(request: AssistantRequest, tool_results: list[ToolResult]) -> str:
    parts = [render_agent_observations(build_agent_observations(request))]
    if tool_results:
        parts.append("Tool results（真实工具结果；只能据此回答）：")
        parts.append(
            json.dumps(
                [result.model_dump(mode="json") for result in tool_results],
                ensure_ascii=False,
            )
        )
    return "\n\n".join(parts)


def _parse_agent_decision(text: str) -> tuple[AgentDecision | None, str]:
    raw = _extract_json_object(text)
    if not raw:
        return None, "模型没有返回 JSON 对象。"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败：{exc.msg}"
    try:
        return AgentDecision.model_validate(data), ""
    except ValidationError as exc:
        return None, f"AgentDecision 校验失败：{exc.errors()}"


def _agent_decision_from_native_tool_calls(
    tool_calls: list[ModelToolCall],
) -> AgentDecision | None:
    calls = [
        ToolCall(
            id=call.id,
            name=ToolName(call.name),
            arguments=call.arguments,
        )
        for call in tool_calls
        if _is_known_tool_name(call.name)
    ]
    if not calls:
        return None
    return AgentDecision(tool_calls=calls)


def _is_known_tool_name(name: str) -> bool:
    try:
        ToolName(name)
    except ValueError:
        return False
    return True


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    if start < 0:
        return ""
    decoder = json.JSONDecoder()
    try:
        _, end = decoder.raw_decode(stripped[start:])
    except json.JSONDecodeError:
        return stripped[start:]
    return stripped[start : start + end]


def _inject_message_links(request: AssistantRequest) -> None:
    refs = parse_resource_urls(request.text)
    if not refs:
        return
    existing = {(ref.type, ref.url, ref.token) for ref in request.resource_refs}
    for ref in refs:
        key = (ref.type, ref.url, ref.token)
        if key in existing:
            continue
        request.resource_refs.append(ref)
        existing.add(key)
    urls = [ref.url for ref in request.resource_refs if ref.url]
    request.resource_urls[:] = urls


def _parse_failure_text(parse_error: str) -> str:
    if parse_error.startswith("AgentDecision 校验失败"):
        parse_error = "模型输出字段类型不正确。"
    return (
        "我没有解析出有效的 Agent 决策，因此没有执行任何工具。"
        f"原因：{parse_error or '模型输出格式不正确'}"
    )


def _max_steps_text(tool_results: list[ToolResult]) -> str:
    user_messages = [
        result.user_message.strip()
        for result in tool_results
        if result.user_message and result.user_message.strip()
    ]
    if user_messages:
        return user_messages[-1]
    return "本次请求需要的工具步骤超过当前上限，已停止执行；没有进行写入。"
