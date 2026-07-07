import re

from flago.models import (
    AgentObservation,
    AgentObservationKind,
    AssistantRequest,
    ResourceReadResult,
)


def build_agent_observations(request: AssistantRequest) -> list[AgentObservation]:
    observations = [
        AgentObservation(
            kind=AgentObservationKind.CURRENT_MESSAGE,
            title="当前用户消息",
            content=request.text,
            source="feishu.message",
        )
    ]
    followup_source = _writeback_location_followup_source(request)
    if followup_source:
        observations.append(
            AgentObservation(
                kind=AgentObservationKind.RECENT_CHAT,
                title="最近待补充位置的写回请求",
                content=followup_source,
                source="flago.context.writeback_followup",
                metadata={
                    "priority": "high",
                    "instruction": "当前消息只补充位置时，应继承这条请求里的写入内容和目标",
                },
            )
        )
    if request.chat_context_summary.strip():
        observations.append(
            AgentObservation(
                kind=AgentObservationKind.CHAT_SUMMARY,
                title="当前会话滚动摘要",
                content=request.chat_context_summary.strip(),
                source="flago.context.summary",
                metadata={
                    "scope": "conversation",
                    "persisted_as_long_term_memory": False,
                    "omitted_message_count": request.chat_context_omitted_count,
                },
            )
        )
    if request.chat_context_messages:
        observations.append(
            AgentObservation(
                kind=AgentObservationKind.RECENT_CHAT,
                title="近期聊天上下文全文",
                content="\n".join(
                    f"- [{message.created_at}] {message.sender_id or 'unknown'}: "
                    f"{message.text}"
                    for message in request.chat_context_messages
                ),
                source="feishu.im.messages",
                metadata={
                    "message_count": len(request.chat_context_messages),
                    "persisted_as_long_term_memory": False,
                },
            )
        )
    if request.memory_items:
        observations.append(
            AgentObservation(
                kind=AgentObservationKind.USER_MEMORY,
                title="用户长期记忆摘要与偏好",
                content="\n".join(
                    f"- {item.kind}: {item.content}"
                    for item in request.memory_items
                    if item.content.strip()
                ),
                source="flago.memory",
                metadata={
                    "item_count": len(request.memory_items),
                    "raw_chat_text_stored": False,
                },
            )
        )
    if request.resource_urls:
        observations.append(
            AgentObservation(
                kind=AgentObservationKind.RESOURCE_LINKS,
                title="检测到的链接",
                content="\n".join(f"- {url}" for url in request.resource_urls),
                source="flago.resource.parser",
                metadata={"link_count": len(request.resource_urls)},
            )
        )
    observations.extend(_resource_observations(request.resource_results))
    return observations


def _writeback_location_followup_source(request: AssistantRequest) -> str:
    if not _is_location_only_writeback_followup(request.text):
        return ""
    for message in reversed(request.chat_context_messages):
        text = message.text.strip()
        if not text or text == request.text.strip():
            continue
        if message.sender_id and request.actor_id and message.sender_id != request.actor_id:
            continue
        if _has_explicit_writeback_content(text) or _is_bare_writeback_content(text):
            return text
    return ""


def _is_location_only_writeback_followup(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text.strip().casefold())
    if not normalized:
        return False
    location_markers = ("开头", "结尾", "末尾", "文末", "最后", "最前", "最开始", "top", "bottom")
    write_markers = ("写", "加", "追加", "插入", "放")
    if not any(marker in normalized for marker in location_markers):
        return False
    if not any(marker in normalized for marker in write_markers):
        return False
    return len(normalized) <= 24 or normalized in {"帮我写在开头吧", "写在开头吧", "写到开头"}


def _has_explicit_writeback_content(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    has_write = any(marker in normalized for marker in ("写", "添加", "追加", "插入", "记录"))
    has_target = any(marker in normalized for marker in ("到", "进", "文档", "表格", "多维表"))
    has_content = bool(re.search(r"[“\"']([^”\"']{1,200})[”\"']", normalized))
    has_sentence = "一句" in normalized
    return has_write and has_target and (has_content or has_sentence)


def _is_bare_writeback_content(text: str) -> bool:
    normalized = text.strip()
    if not normalized or len(normalized) > 200:
        return False
    if normalized.startswith(("/", "\\")):
        return False
    if _is_location_only_writeback_followup(normalized):
        return False
    return not any(
        marker in normalized for marker in ("？", "?", "吗", "帮我", "请", "写到", "写进")
    )


def render_agent_observations(observations: list[AgentObservation]) -> str:
    lines = [
        "Agent observations（代码已按授权、隐私和预算过滤；仅用于本次请求）："
    ]
    for index, observation in enumerate(observations, start=1):
        lines.append("")
        lines.append(f"### Observation {index}: {observation.title}")
        lines.append(f"kind: {observation.kind.value}")
        if observation.source:
            lines.append(f"source: {observation.source}")
        for key, value in observation.metadata.items():
            lines.append(f"{key}: {value}")
        lines.append(observation.content or "[空]")
    return "\n".join(lines)


def _resource_observations(results: list[ResourceReadResult]) -> list[AgentObservation]:
    observations: list[AgentObservation] = []
    for index, result in enumerate(results, start=1):
        title = result.title or result.ref.url
        content_lines = [f"URL: {result.ref.url}"]
        if result.error:
            content_lines.append(f"读取失败：{result.error}")
        else:
            if result.truncated:
                content_lines.append("提示：以下内容已按安全上限截断。")
            content_lines.append(result.content or "[空文档]")
        observations.append(
            AgentObservation(
                kind=AgentObservationKind.RESOURCE_RESULT,
                title=f"资源 {index}: {title}",
                content="\n".join(content_lines),
                source=result.ref.source_kind or "flago.resource.reader",
                metadata={
                    "resource_type": result.ref.type.value,
                    "truncated": result.truncated,
                    "read_error": bool(result.error),
                },
            )
        )
    return observations
