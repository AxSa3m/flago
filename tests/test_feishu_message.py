from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1

from fcgo.feishu.menu import parse_bot_menu_event
from fcgo.feishu.message import parse_text_message
from fcgo.models import ConversationType


def test_parse_text_message_from_lark_sdk_object() -> None:
    event = P2ImMessageReceiveV1(
        {
            "event": {
                "sender": {
                    "sender_id": {"open_id": "ou_user", "user_id": "user_1"},
                    "sender_type": "user",
                },
                "message": {
                    "message_id": "om_message",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": '{"text":"hello from feishu"}',
                    "mentions": [],
                },
            },
            "schema": "2.0",
            "header": {"event_id": "event_1"},
        }
    )

    message = parse_text_message(event)

    assert message is not None
    assert message.message_id == "om_message"
    assert message.chat_id == "oc_chat"
    assert message.sender_id == "ou_user"
    assert message.text == "hello from feishu"
    assert message.conversation_type == ConversationType.PRIVATE
    assert message.conversation_key == "private:oc_chat"
    assert message.is_bot_mentioned is True
    assert message.raw["event"]["message"]["message_id"] == "om_message"


def test_parse_text_message_from_dict_group_mention() -> None:
    message = parse_text_message(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_group_message",
                    "chat_id": "oc_group",
                    "chat_type": "group",
                    "thread_id": "omt_thread",
                    "message_type": "text",
                    "content": '{"text":"@_user_bot summarize this"}',
                    "mentions": [
                        {
                            "key": "@_user_bot",
                            "id": {"open_id": "ou_bot", "user_id": "bot_user"},
                            "name": "Gemini助手",
                        }
                    ],
                },
            }
        },
        bot_open_id="ou_bot",
    )

    assert message is not None
    assert message.text == "summarize this"
    assert message.conversation_type == ConversationType.GROUP
    assert message.conversation_key == "group:oc_group:thread:omt_thread"
    assert message.thread_id == "omt_thread"
    assert message.is_bot_mentioned is True
    assert len(message.mentions) == 1
    assert message.mentions[0].open_id == "ou_bot"
    assert message.mentions[0].name == "Gemini助手"


def test_parse_text_message_extracts_link_preview_url() -> None:
    message = parse_text_message(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_link",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": (
                        '{"text":"这篇文章呢",'
                        '"url":"https://my.feishu.cn/wiki/AiDzwcQLli2EQYkaKyWckTPAnjj?from=navigation"}'
                    ),
                    "mentions": [],
                },
            }
        }
    )

    assert message is not None
    assert "这篇文章呢" in message.text
    assert "https://my.feishu.cn/wiki/AiDzwcQLli2EQYkaKyWckTPAnjj?from=navigation" in message.text


def test_parse_post_message_extracts_nested_feishu_link() -> None:
    message = parse_text_message(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_post",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "post",
                    "content": (
                        '{"content":[[{"tag":"text","text":"看看 "},'
                        '{"tag":"a","text":"英国giffgaff实体SIM卡使用教程",'
                        '"href":"https://my.feishu.cn/wiki/AiDzwcQLli2EQYkaKyWckTPAnjj?from=navigation"}]]}'
                    ),
                    "mentions": [],
                },
            }
        }
    )

    assert message is not None
    assert "英国giffgaff实体SIM卡使用教程" in message.text
    assert "https://my.feishu.cn/wiki/AiDzwcQLli2EQYkaKyWckTPAnjj?from=navigation" in message.text


def test_parse_post_message_extracts_mention_doc_encoded_url() -> None:
    message = parse_text_message(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_doc_mention",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "post",
                    "content": (
                        '{"content":[[{"tag":"text","text":"读取 "},'
                        '{"tag":"mention_doc","mention_doc":{'
                        '"title":"测试文档",'
                        '"url":"https%3A%2F%2Fmy.feishu.cn%2Fdocx%2FUXYPdSrz5ovVk1x1cVqc4ReOnNb"'
                        "}}]]}"
                    ),
                    "mentions": [],
                },
            }
        }
    )

    assert message is not None
    assert "读取" in message.text
    assert "https://my.feishu.cn/docx/UXYPdSrz5ovVk1x1cVqc4ReOnNb" in message.text


def test_parse_post_message_builds_mention_doc_url_from_token() -> None:
    message = parse_text_message(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_doc_token",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "post",
                    "content": (
                        '{"content":[[{"tag":"text","text":"读取 "},'
                        '{"tag":"mention_doc","mention_doc":{'
                        '"title":"测试文档",'
                        '"docs_token":"UXYPdSrz5ovVk1x1cVqc4ReOnNb",'
                        '"docs_type":"docx"'
                        "}}]]}"
                    ),
                    "mentions": [],
                },
            }
        }
    )

    assert message is not None
    assert "读取" in message.text
    assert "https://my.feishu.cn/docx/UXYPdSrz5ovVk1x1cVqc4ReOnNb" in message.text


def test_parse_group_message_not_mentioning_configured_bot() -> None:
    message = parse_text_message(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_group_message",
                    "chat_id": "oc_group",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": '{"text":"@_user_other summarize this"}',
                    "mentions": [
                        {
                            "key": "@_user_other",
                            "id": {"open_id": "ou_other"},
                            "name": "Other",
                        }
                    ],
                },
            }
        },
        bot_open_id="ou_bot",
    )

    assert message is not None
    assert message.text == "summarize this"
    assert message.is_bot_mentioned is False


def test_parse_non_text_message_returns_none() -> None:
    message = parse_text_message(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_file",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "file",
                    "content": "{}",
                },
            }
        }
    )

    assert message is None


def test_parse_audio_message_extracts_file_key() -> None:
    message = parse_text_message(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_audio",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "audio",
                    "content": '{"file_key":"audio_v2_abc"}',
                },
            }
        }
    )

    assert message is not None
    assert message.text == "请转写或概述这段音频。"
    assert len(message.attachments) == 1
    assert message.attachments[0].key == "audio_v2_abc"
    assert message.attachments[0].type == "audio"


def test_parse_video_message_extracts_file_key() -> None:
    message = parse_text_message(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_video",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "media",
                    "content": '{"file_key":"video_v2_abc","file_name":"测试.mp4"}',
                },
            }
        }
    )

    assert message is not None
    assert message.text == "请描述这个视频。"
    assert len(message.attachments) == 1
    assert message.attachments[0].key == "video_v2_abc"
    assert message.attachments[0].type == "video"
    assert message.attachments[0].filename == "测试.mp4"


def test_parse_image_message_extracts_image_key() -> None:
    message = parse_text_message(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_image",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "image",
                    "content": '{"image_key":"img_v2_abc"}',
                },
            }
        }
    )

    assert message is not None
    assert message.text == "请描述这张图片。"
    assert len(message.attachments) == 1
    assert message.attachments[0].key == "img_v2_abc"


def test_parse_post_message_extracts_inline_image_key() -> None:
    message = parse_text_message(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_post_image",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "post",
                    "content": (
                        '{"content":[[{"tag":"img","image_key":"img_v2_inline"},'
                        '{"tag":"text","text":"这幅图讲了什么？"}]]}'
                    ),
                    "mentions": [],
                },
            }
        }
    )

    assert message is not None
    assert "这幅图讲了什么" in message.text
    assert len(message.attachments) == 1
    assert message.attachments[0].key == "img_v2_inline"


def test_parse_bot_menu_event_from_dict() -> None:
    event = parse_bot_menu_event(
        {
            "schema": "2.0",
            "header": {
                "event_id": "evt_menu_1",
                "event_type": "application.bot.menu_v6",
            },
            "event": {
                "operator": {
                    "operator_name": "木町",
                    "operator_id": {
                        "open_id": "ou_user",
                        "user_id": "user_1",
                        "union_id": "on_union",
                    },
                },
                "event_key": "fcgo.model.view",
                "timestamp": 1710000000,
            },
        }
    )

    assert event is not None
    assert event.event_id == "evt_menu_1"
    assert event.event_key == "fcgo.model.view"
    assert event.operator_open_id == "ou_user"
    assert event.operator_user_id == "user_1"
    assert event.operator_name == "木町"
