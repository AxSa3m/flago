from typing import Any

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from flgo.feishu.card_callback import FeishuCardCallback, parse_card_callback


def parse_card_action_event(data: P2CardActionTrigger) -> FeishuCardCallback:
    event = data.event
    action_value = event.action.value if event and event.action and event.action.value else {}
    operator = event.operator if event and event.operator else None
    payload: dict[str, Any] = {
        "header": {
            "event_id": data.header.event_id if data.header else "",
        },
        "event": {
            "operator": {
                "open_id": operator.open_id if operator else "",
                "user_id": operator.user_id if operator else "",
                "union_id": operator.union_id if operator else "",
            },
            "action": {
                "value": action_value,
            },
        },
    }
    return parse_card_callback(payload)


def card_action_response(status: str, message: str) -> P2CardActionTriggerResponse:
    toast_type = "success" if status in {"accepted", "executed", "canceled"} else "warning"
    return P2CardActionTriggerResponse(
        {
            "toast": {
                "type": toast_type,
                "content": message,
            }
        }
    )
