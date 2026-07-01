from fcgo.config import Settings
from fcgo.models import WritebackConfirmationMode


def test_context_and_memory_privacy_defaults_are_conservative() -> None:
    settings = Settings(_env_file=None, env="test", oauth_enable_offline_access=False)

    assert settings.context_recent_message_limit == 50
    assert settings.context_recent_time_window_hours == 24
    assert settings.context_cache_ttl_hours == 24
    assert settings.context_cache_refresh_seconds == 60
    assert settings.context_inject_message_limit == 8
    assert settings.context_max_chars == 6000
    assert settings.memory_store_raw_text is False
    assert settings.memory_item_max_chars == 2000
    assert settings.memory_context_max_chars == 4000
    assert settings.assistant_default_name == "小智"
    assert settings.assistant_default_profile == "简洁、可靠、直接，优先给出可执行的回答。"
    assert settings.agent_mode == "legacy"
    assert settings.agent_max_steps == 4
    assert settings.agent_tool_timeout_seconds == 30
    assert settings.doc_block_scan_limit == 1000
    assert settings.embedded_file_limit == 3
    assert settings.embedded_file_max_bytes == 20 * 1024 * 1024
    assert settings.embedded_file_max_chars == 40_000
    assert settings.attachment_ocr_enabled is False
    assert settings.attachment_ocr_command == "tesseract"
    assert settings.attachment_ocr_languages == "chi_sim+eng"
    assert settings.attachment_ocr_timeout_seconds == 15
    assert settings.attachment_ocr_max_pixels == 20_000_000
    assert settings.embedded_link_limit == 20
    assert settings.pdf_default_pages == 2
    assert settings.pdf_max_pages == 10
    assert settings.pdf_extract_timeout_seconds == 20
    assert settings.web_read_enabled is True
    assert settings.web_timeout_seconds == 20
    assert settings.web_max_bytes == 1_000_000
    assert settings.web_allowed_hosts == ""
    assert settings.web_blocked_hosts == ""
    assert settings.oauth_enable_offline_access is False
    assert "offline_access" not in settings.oauth_scope_list
    assert "base:table:read" in settings.oauth_scope_list
    assert "docs:document.media:download" in settings.oauth_scope_list
    assert settings.writeback_auto_execute_enabled is False
    assert settings.writeback_confirmation_mode == WritebackConfirmationMode.ALWAYS


def test_oauth_offline_access_is_opt_in() -> None:
    settings = Settings(_env_file=None, env="test", oauth_enable_offline_access=True)

    assert "offline_access" in settings.oauth_scope_list
    assert "offline_access" not in settings.oauth_required_scope_list


def test_oauth_scopes_filter_offline_access_when_disabled() -> None:
    settings = Settings(
        _env_file=None,
        env="test",
        feishu_oauth_scopes="auth:user.id:read offline_access docx:document:readonly",
        oauth_enable_offline_access=False,
    )

    assert settings.oauth_scope_list == ["auth:user.id:read", "docx:document:readonly"]


def test_context_and_memory_privacy_settings_can_be_overridden() -> None:
    settings = Settings(
        _env_file=None,
        env="test",
        memory_store_raw_text=True,
        memory_item_max_chars=100,
        memory_context_max_chars=200,
    )

    assert settings.memory_store_raw_text is True
    assert settings.memory_item_max_chars == 100
    assert settings.memory_context_max_chars == 200
