from datetime import UTC

from fcgo.models import ActionProposal, WriteActionType

MAX_CARD_MARKDOWN_CHARS = 6000
MAX_PREVIEW_CHARS = 4000


def assistant_response_card(
    text: str,
    proposals: list[ActionProposal],
    *,
    assistant_name: str = "飞灵",
) -> dict[str, object]:
    elements: list[dict[str, object]] = []
    if text.strip():
        elements.append(
            {"tag": "markdown", "content": _trim(text.strip(), MAX_CARD_MARKDOWN_CHARS)}
        )
    for index, proposal in enumerate(proposals, start=1):
        if elements:
            elements.append({"tag": "hr"})
        elements.extend(_proposal_elements(proposal, index=index, total=len(proposals)))
    if not elements:
        elements.append({"tag": "markdown", "content": "已生成回复。"})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"{assistant_name} 回复"},
        },
        "elements": elements,
    }


def proposal_card(
    proposal: ActionProposal,
    *,
    assistant_name: str = "飞灵",
) -> dict[str, object]:
    return assistant_response_card("", [proposal], assistant_name=assistant_name)


def _proposal_elements(
    proposal: ActionProposal,
    *,
    index: int,
    total: int,
) -> list[dict[str, object]]:
    title = "待确认写入" if total == 1 else f"待确认写入 {index}/{total}"
    elements: list[dict[str, object]] = [
        {
            "tag": "markdown",
            "content": (
                f"**{title}**\n"
                f"- 动作：{_action_label(proposal.action_type)}\n"
                f"- 目标：{_target_summary(proposal)}\n"
                f"- 过期：{_format_expires_at(proposal)}"
            ),
        },
        {
            "tag": "markdown",
            "content": f"**预览**\n{_trim(proposal.preview, MAX_PREVIEW_CHARS)}",
        },
    ]
    actions: list[dict[str, object]] = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "确认执行"},
            "type": "primary",
            "value": {
                "action": "confirm",
                "fcgo_action": "writeback.confirm",
                "action_id": proposal.id,
            },
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "取消"},
            "type": "default",
            "value": {
                "action": "cancel",
                "fcgo_action": "writeback.cancel",
                "action_id": proposal.id,
            },
        },
    ]
    display_url = str(proposal.target_url or "").strip()
    if display_url.startswith(("http://", "https://")):
        actions.insert(
            0,
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打开目标"},
                "type": "default",
                "url": display_url,
            },
        )
    elements.append(
        {
            "tag": "action",
            "actions": actions,
        },
    )
    return elements


def _action_label(action_type: WriteActionType) -> str:
    labels = {
        WriteActionType.DOC_CREATE: "创建文档",
        WriteActionType.DOC_APPEND: "追加到文档",
        WriteActionType.DOC_DELETE_BLOCK: "撤回文档写入",
        WriteActionType.SHEET_WRITE_RANGE: "写入电子表格范围",
        WriteActionType.BITABLE_CREATE_RECORD: "创建多维表格记录",
        WriteActionType.BITABLE_UPDATE_RECORD: "更新多维表格记录",
        WriteActionType.BITABLE_DELETE_RECORD: "删除多维表格记录",
        WriteActionType.MESSAGE_SEND: "发送飞书消息",
    }
    return labels.get(action_type, action_type.value)


def _target_summary(proposal: ActionProposal) -> str:
    display_title = str(proposal.target_title or "").strip()
    display_url = str(proposal.target_url or "").strip()
    if display_title and display_url.startswith(("http://", "https://")):
        return f"[{display_title}]({display_url})"
    if display_title:
        return display_title
    if display_url.startswith(("http://", "https://")):
        return f"[打开目标资源]({display_url})"
    for key in (
        "title",
        "url",
    ):
        value = proposal.target.get(key)
        if value:
            return str(value)
    return "已识别目标资源"


def _format_expires_at(proposal: ActionProposal) -> str:
    expires_at = proposal.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _trim(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 20].rstrip()}\n\n...（内容已截断）"
