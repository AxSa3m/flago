from fcgo.model_providers.types import (
    ModelMessage,
    ModelMessageRole,
    ModelRequest,
    ProviderCapability,
)
from fcgo.models import AssistantRequest

SYSTEM_INSTRUCTIONS = [
    "你是 FCGO，一个接入飞书的工作助手。",
    "请用中文回答，保持简洁、准确、可执行。",
    "当前版本处于读取优先阶段，写入、修改、删除、创建飞书内容的功能已暂停。",
    "如果用户请求写入或修改飞书内容，不要声称已经写入，不要输出写回 JSON，"
    "也不要说已经创建卡片；请说明写入功能暂时暂停，并提供可复制的草稿、摘要或操作建议。",
    "分析电子表格时，不要因为周边空白行列判断表格为空；应依据非空单元格和非空行摘录回答。",
    "分析多维表格时，应依据字段、视图、记录摘录回答，不要把缺少某些字段值误判为整表为空。",
    "如果多维表格记录摘录已成功读取，即使字段或视图元数据有警告，也应优先根据记录摘录回答。",
]


def build_assistant_model_request(
    request: AssistantRequest,
    *,
    provider: str | None = None,
    model: str | None = None,
    max_output_tokens: int | None = None,
) -> ModelRequest:
    return ModelRequest(
        request_id=request.request_id,
        provider=provider,
        model=model,
        max_output_tokens=max_output_tokens,
        required_capabilities=[ProviderCapability.CHAT],
        messages=[
            ModelMessage(
                role=ModelMessageRole.SYSTEM,
                content="\n".join(SYSTEM_INSTRUCTIONS),
            ),
            ModelMessage(
                role=ModelMessageRole.USER,
                content=_build_user_content(request),
            ),
        ],
        metadata={
            "actor_id": request.actor_id,
            "conversation_id": request.conversation_id,
            "conversation_type": request.conversation_type.value,
        },
    )


def build_assistant_prompt(request: AssistantRequest) -> str:
    return render_text_prompt(build_assistant_model_request(request))


def render_text_prompt(request: ModelRequest) -> str:
    return "\n\n".join(message.content for message in request.messages if message.content)


def _build_user_content(request: AssistantRequest) -> str:
    context = [f"用户输入：{request.text}"]
    if request.resource_urls:
        context.append("")
        context.append("检测到的链接：")
        context.extend(f"- {url}" for url in request.resource_urls)
    if request.resource_results:
        context.append("")
        context.append("已按用户授权读取的资源内容（仅用于本次请求，不持久化正文）：")
        for index, result in enumerate(request.resource_results, start=1):
            title = result.title or result.ref.url
            context.append("")
            context.append(f"### 资源 {index}: {title}")
            context.append(f"URL: {result.ref.url}")
            if result.error:
                context.append(f"读取失败：{result.error}")
                continue
            if result.truncated:
                context.append("提示：以下内容已按安全上限截断。")
            context.append(result.content or "[空文档]")
    return "\n".join(context)
