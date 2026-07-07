from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

from flgo.feishu.card_event import card_action_response, parse_card_action_event


def test_parse_card_action_event() -> None:
    callback = parse_card_action_event(
        P2CardActionTrigger(
            {
                "schema": "2.0",
                "header": {"event_id": "evt-1"},
                "event": {
                    "operator": {"open_id": "ou_user"},
                    "action": {
                        "value": {
                            "flgo_action": "writeback.confirm",
                            "action_id": "action-1",
                        }
                    },
                },
            }
        )
    )

    assert callback.event_id == "evt-1"
    assert callback.actor_id == "ou_user"
    assert callback.action == "confirm"
    assert callback.action_key == "writeback.confirm"
    assert callback.action_id == "action-1"


def test_parse_memory_delete_card_action_event() -> None:
    callback = parse_card_action_event(
        P2CardActionTrigger(
            {
                "schema": "2.0",
                "header": {"event_id": "evt-1"},
                "event": {
                    "operator": {"open_id": "ou_user"},
                    "action": {
                        "value": {
                            "flgo_action": "flgo.memory.delete.confirm",
                            "action_id": "memory-delete-1",
                        }
                    },
                },
            }
        )
    )

    assert callback.actor_id == "ou_user"
    assert callback.action == "confirm"
    assert callback.action_key == "flgo.memory.delete.confirm"
    assert callback.action_id == "memory-delete-1"


def test_parse_memory_save_card_action_event() -> None:
    callback = parse_card_action_event(
        P2CardActionTrigger(
            {
                "schema": "2.0",
                "header": {"event_id": "evt-1"},
                "event": {
                    "operator": {"open_id": "ou_user"},
                    "action": {
                        "value": {
                            "flgo_action": "flgo.memory.save.confirm",
                            "action_id": "memory-save-1",
                            "memory_key": "nickname",
                            "memory_kind": "称呼",
                            "memory_content": "你叫Sa3m。",
                        }
                    },
                },
            }
        )
    )

    assert callback.actor_id == "ou_user"
    assert callback.action == "confirm"
    assert callback.action_key == "flgo.memory.save.confirm"
    assert callback.action_id == "memory-save-1"
    assert callback.value["memory_content"] == "你叫Sa3m。"


def test_card_action_response() -> None:
    response = card_action_response("executed", "写回已执行")

    assert response.toast is not None
    assert response.toast.type == "success"
    assert response.toast.content == "写回已执行"
