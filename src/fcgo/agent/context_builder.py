from fcgo.models import (
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
    if request.chat_context_summary.strip():
        observations.append(
            AgentObservation(
                kind=AgentObservationKind.CHAT_SUMMARY,
                title="当前会话滚动摘要",
                content=request.chat_context_summary.strip(),
                source="fcgo.context.summary",
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
                source="fcgo.memory",
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
                source="fcgo.resource.parser",
                metadata={"link_count": len(request.resource_urls)},
            )
        )
    observations.extend(_resource_observations(request.resource_results))
    return observations


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
                source=result.ref.source_kind or "fcgo.resource.reader",
                metadata={
                    "resource_type": result.ref.type.value,
                    "truncated": result.truncated,
                    "read_error": bool(result.error),
                },
            )
        )
    return observations
