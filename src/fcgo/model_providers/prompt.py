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
    "如果用户请求写入或修改飞书内容，不要声称已经写入，也不要让用户手动操作；"
    "请说明会先生成写回预览，并等待用户在飞书卡片中确认。",
    "写回请求必须先由你识别 CRUD 意图：create=新增/添加/增加/追加/插入，"
    "update=更新/修改/替换/覆盖，delete=删除/清空，read=查询/读取。"
    "不要依赖用户使用固定关键词；请根据语义决定 operation。",
    "当你判断需要写回时，除自然语言预览外，必须额外输出一个 JSON 代码块，"
    "形如 ```json {\"fcgo_writeback\": {\"operation\": \"create|update|delete|read\", "
    "\"resource_type\": \"doc|sheet|bitable|message\", \"target\": {...}, "
    "\"payload\": {\"content\": \"最终写入内容\"}, \"preview\": \"给用户看的预览\"}} ```。",
    "多维表格写回时，target 应包含 field/record_id/record；"
    "电子表格写回时，target 应包含 range/cell；"
    "文档写回时 payload.content 是最终正文。程序会校验并等待卡片确认后才执行。",
    "处理写回请求时，用户输入是任务指令，不是默认写入正文；应先完成生成、总结、改写、"
    "拆解或字段匹配，再在回复中用“待写内容预览：”明确列出最终要写入的内容或新值。",
    "如果是电子表格或多维表格写回，请同时说明目标单元格、目标字段、匹配记录或新值，"
    "但不要把用户原始指令当作写入内容。",
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
