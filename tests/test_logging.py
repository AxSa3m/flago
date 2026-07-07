from flgo.logging import redact


def test_redact_masks_common_secret_shapes() -> None:
    text = (
        "Authorization: Bearer abc.def "
        "GEMINI=AIzaSyExampleSecretValue123456 "
        "access_token=secret-token"
    )

    redacted = redact(text)

    assert "abc.def" not in redacted
    assert "AIzaSyExampleSecretValue123456" not in redacted
    assert "secret-token" not in redacted
    assert redacted.count("***REDACTED***") == 3


def test_redact_masks_secret_mapping_keys() -> None:
    redacted = redact({"api_key": "sk-secret", "nested": {"refresh_token": "token"}})

    assert redacted == {
        "api_key": "***REDACTED***",
        "nested": {"refresh_token": "***REDACTED***"},
    }
