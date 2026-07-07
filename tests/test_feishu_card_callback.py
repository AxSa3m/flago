import pytest

from flgo.feishu.card_callback import is_url_verification, parse_card_callback


def test_parse_simple_card_callback_payload() -> None:
    callback = parse_card_callback(
        {
            "action_id": "action-1",
            "actor_id": "ou_user",
            "action": "confirm",
        }
    )

    assert callback.action_id == "action-1"
    assert callback.actor_id == "ou_user"
    assert callback.action == "confirm"


def test_parse_feishu_event_card_callback_payload() -> None:
    callback = parse_card_callback(
        {
            "header": {"event_id": "evt-1"},
            "event": {
                "operator": {"open_id": "ou_user"},
                "action": {
                    "value": {
                        "flgo_action": "writeback.cancel",
                        "action_id": "action-2",
                    }
                },
            },
        }
    )

    assert callback.event_id == "evt-1"
    assert callback.actor_id == "ou_user"
    assert callback.action_id == "action-2"
    assert callback.action == "cancel"


def test_parse_legacy_action_object_payload() -> None:
    callback = parse_card_callback(
        {
            "open_id": "ou_legacy",
            "action": {
                "value": {
                    "action": "confirm",
                    "action_id": "action-3",
                }
            },
        }
    )

    assert callback.actor_id == "ou_legacy"
    assert callback.action == "confirm"
    assert callback.action_id == "action-3"


def test_parse_card_callback_rejects_missing_action_id() -> None:
    with pytest.raises(ValueError, match="missing action_id"):
        parse_card_callback({"actor_id": "ou_user", "action": "confirm"})


def test_url_verification_detection() -> None:
    assert is_url_verification({"type": "url_verification", "challenge": "abc"})
    assert not is_url_verification({"challenge": "abc"})
