from fcgo.agent.context_builder import build_agent_observations, render_agent_observations
from fcgo.model_providers.types import (
    ModelMessage,
    ModelMessageRole,
    ModelRequest,
    ProviderCapability,
)
from fcgo.models import AssistantRequest

SYSTEM_INSTRUCTIONS = [
    "请用中文回答，保持简洁、准确、可执行；默认不超过 3 句，除非用户明确要求展开。",
    "不要主动输出长篇背景、编号步骤或建议清单；能直接回答时就直接回答。",
    "普通聊天中的“记住这个测试词”“关键词改成 X”“把 A 改成 B”等，若没有明确飞书对象或链接，"
    "应理解为当前对话里的信息更新或测试，不要当作飞书写入请求。",
    "回答依赖近期聊天上下文的问题时，以时间最新的用户更正为准。",
    "分析电子表格时，不要因为周边空白行列判断表格为空；应依据非空单元格和非空行摘录回答。",
    "分析多维表格时，应依据字段、视图、记录摘录回答，不要把缺少某些字段值误判为整表为空。",
    "如果多维表格记录摘录已成功读取，即使字段或视图元数据有警告，也应优先根据记录摘录回答。",
    "回答飞书搜索或资源问题时，只能引用上下文中明确提供的资源标题、URL 和内容；"
    "不得编造飞书文档标题、链接、token 或搜索结果。",
    "如果没有明确匹配的搜索结果，应直接说明没有找到，不要用相近但无关的文档冒充。",
    "如果资源读取结果提示缺少权限、缺少 scope 或没有目标资源权限，应按该原因回答；"
    "不要把它改写成授权已过期、未授权或 token 失效。",
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
                content="\n".join(
                    _system_instructions(
                        request.assistant_name,
                        writeback_enabled=request.writeback_enabled,
                    )
                ),
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


def _system_instructions(assistant_name: str, *, writeback_enabled: bool) -> list[str]:
    name = assistant_name.strip() or "小智"
    writeback_instructions = (
        [
            "写回功能已启用。只有用户明确要求对具体飞书对象执行写入、修改、删除或创建时，才准备写回内容。",
            "当前运行时的“写回功能已启用”状态优先于聊天历史；忽略历史回复中关于写入暂停的旧说法。",
            "写回确认卡片由系统自动生成；不要询问用户是否生成确认卡片。你只能说明已准备写回内容，不能声称已经写入或已经执行。",
            "不要输出写回 JSON、工具协议或虚构的执行结果。"
            "请准确提炼用户要求写入的正文、字段值或消息内容。",
        ]
        if writeback_enabled
        else [
            "当前写回功能已关闭，写入、修改、删除、创建飞书内容的功能暂停。",
            "用户明确要求写入具体飞书对象时，说明写回功能暂停，并提供可复制的草稿或操作建议。",
            "不要输出写回 JSON，也不要声称已经创建卡片或执行写入。",
        ]
    )
    return [
        f"你是 {name}，一个接入飞书的工作助手。",
        *SYSTEM_INSTRUCTIONS,
        *writeback_instructions,
    ]


def build_assistant_prompt(request: AssistantRequest) -> str:
    return render_text_prompt(build_assistant_model_request(request))


def render_text_prompt(request: ModelRequest) -> str:
    return "\n\n".join(message.content for message in request.messages if message.content)


def _build_user_content(request: AssistantRequest) -> str:
    return render_agent_observations(build_agent_observations(request))
