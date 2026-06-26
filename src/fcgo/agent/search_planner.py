import json
import logging
import re
from typing import Any

from fcgo.agent.protocols import ModelProvider
from fcgo.model_providers.types import (
    ModelMessage,
    ModelMessageRole,
    ModelRequest,
    ModelResponse,
    ProviderCapability,
)
from fcgo.models import AssistantRequest, AssistantResponse, ResourceSearchPlan

logger = logging.getLogger(__name__)

_MAX_PLANNER_QUERIES = 8
_MAX_QUERY_LENGTH = 120
_PLANNER_MAX_OUTPUT_TOKENS = 768

_RESOURCE_TYPE_ALIASES = {
    "doc": "doc",
    "docs": "doc",
    "document": "doc",
    "docx": "doc",
    "wiki": "wiki",
    "sheet": "sheet",
    "sheets": "sheet",
    "spreadsheet": "sheet",
    "bitable": "bitable",
    "base": "bitable",
    "file": "file",
}

_PLANNER_SYSTEM_PROMPT = """你是飞书助手的资源搜索规划器。
你的任务是判断当前飞书私聊消息是否需要搜索用户可见的飞书资料，并输出可执行搜索计划。

只输出 JSON 对象，不要输出 Markdown、解释或额外文本。格式：
{
  "should_search": true,
  "queries": ["搜索词1", "搜索词2"],
  "resource_types": ["doc", "wiki", "sheet", "bitable", "file"],
  "constraints": ["用户明确给出的约束"],
  "reason": "一句话说明"
}

规则：
- 用户想定位、读取、查看、总结、分析飞书中的文档、知识库、表格、多维表格、
  文件或资料时，should_search=true。
- 用户只是在普通聊天、测试当前会话关键词、操作记忆/模型/菜单、
  或没有要求查找飞书资料时，should_search=false。
- queries 应该像用户会在飞书搜索框输入的短词；去掉“帮我找”“一篇文档”“名字包含”“相关资料”等任务话术。
- 可以给多个候选词，按最可能命中到最宽泛排序。例如先给完整标题片段，再给核心关键词。
- 如果用户要求“相关文档链接汇总”“把这些链接发我”“1/2/3 的文档链接都发我”，
  必须从最近聊天和会话摘要里找出每个被提到的文档主题，分别生成多个 queries；
  不要只搜索一个主题，也不要因为当前消息本身缺少关键词就返回 should_search=false。
- 不要编造飞书标题、URL、token 或搜索结果。
"""


class ResourceSearchPlanningError(RuntimeError):
    """Raised when the model does not return a usable search plan."""


class ModelResourceSearchPlanner:
    def __init__(self, model_provider: ModelProvider) -> None:
        self.model_provider = model_provider

    async def plan(self, request: AssistantRequest) -> ResourceSearchPlan:
        text = await self._generate_plan_text(request)
        return _parse_search_plan(text)

    async def _generate_plan_text(self, request: AssistantRequest) -> str:
        generate_model = getattr(self.model_provider, "generate_model", None)
        if callable(generate_model):
            response = await generate_model(_planner_model_request(request))
            if isinstance(response, ModelResponse):
                return response.text
            return str(getattr(response, "text", ""))
        planner_request = request.model_copy(update={"text": _planner_fallback_text(request)})
        response = await self.model_provider.generate(planner_request)
        if isinstance(response, AssistantResponse):
            return response.text
        return str(getattr(response, "text", ""))


def _planner_model_request(request: AssistantRequest) -> ModelRequest:
    return ModelRequest(
        request_id=request.request_id,
        provider=request.model_provider,
        model=request.model,
        required_capabilities=[ProviderCapability.CHAT],
        max_output_tokens=_PLANNER_MAX_OUTPUT_TOKENS,
        temperature=0,
        messages=[
            ModelMessage(role=ModelMessageRole.SYSTEM, content=_PLANNER_SYSTEM_PROMPT),
            ModelMessage(role=ModelMessageRole.USER, content=_planner_user_text(request)),
        ],
        metadata={
            "actor_id": request.actor_id,
            "conversation_id": request.conversation_id,
            "conversation_type": request.conversation_type.value,
            "purpose": "resource_search_plan",
        },
    )


def _planner_fallback_text(request: AssistantRequest) -> str:
    return f"{_PLANNER_SYSTEM_PROMPT}\n\n{_planner_user_text(request)}"


def _planner_user_text(request: AssistantRequest) -> str:
    parts = [f"当前用户消息：{request.text.strip()}"]
    if request.chat_context_summary.strip():
        parts.append("")
        parts.append("当前会话摘要：")
        parts.append(_truncate(request.chat_context_summary.strip(), 800))
    if request.chat_context_messages:
        parts.append("")
        parts.append("最近聊天：")
        for message in request.chat_context_messages[-10:]:
            parts.append(f"- {message.sender_id or 'unknown'}: {_truncate(message.text, 260)}")
    return "\n".join(parts)


def _parse_search_plan(text: str) -> ResourceSearchPlan:
    try:
        raw = json.loads(_extract_json_object(text))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.info("resource_search_plan_parse_failed", extra={"error": str(exc)})
        raise ResourceSearchPlanningError("model did not return a valid search plan") from exc
    if not isinstance(raw, dict):
        return ResourceSearchPlan()
    return ResourceSearchPlan(
        should_search=bool(raw.get("should_search")),
        queries=_clean_queries(raw.get("queries")),
        resource_types=_clean_resource_types(raw.get("resource_types")),
        constraints=_clean_strings(raw.get("constraints"), max_items=6, max_length=120),
        reason=_clean_string(raw.get("reason"), max_length=160),
    )


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fence_match:
        return fence_match.group(1)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found")
    return stripped[start : end + 1]


def _clean_queries(value: Any) -> list[str]:
    return _clean_strings(value, max_items=_MAX_PLANNER_QUERIES, max_length=_MAX_QUERY_LENGTH)


def _clean_resource_types(value: Any) -> list[str]:
    cleaned: list[str] = []
    for item in _listish(value):
        normalized = _RESOURCE_TYPE_ALIASES.get(str(item).strip().lower())
        if normalized and normalized not in cleaned:
            cleaned.append(normalized)
    return cleaned


def _clean_strings(value: Any, *, max_items: int, max_length: int) -> list[str]:
    cleaned: list[str] = []
    for item in _listish(value):
        normalized = _clean_string(item, max_length=max_length)
        if normalized and normalized not in cleaned:
            cleaned.append(normalized)
        if len(cleaned) >= max_items:
            break
    return cleaned


def _clean_string(value: Any, *, max_length: int) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip(" ：:，,。？?！!；;\"'`")
    return normalized[:max_length]


def _listish(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "..."
